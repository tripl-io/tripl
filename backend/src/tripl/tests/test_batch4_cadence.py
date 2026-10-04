"""Batch 4, the cadence lane: switching a destination back to "Immediately".

a held scope is delivered by exactly ONE of the two paths.

A destination on a cadence holds its matched signals in ``alert_pending_items``
instead of delivering them. Turning the cadence off used to leave that buffer
in place, and TWO independent things then shipped it:

* the IMMEDIATE path, because a held scope has never been sent, so its
  ``AlertRuleState.last_notified_at`` is still NULL and
  ``dispatch._prepare_alert_deliveries`` fires on a NULL regardless of the
  rule's cooldown; and
* the flusher's DRAIN arm, which mints a digest from any buffer sitting under a
  destination with no cadence, within a minute of the switch.

Both fire off the same rows, so an operator who asked for immediate alerts got
every held scope twice. The fix puts the handoff where the transition happens —
``_alerting_destinations.update_destination`` SPLITS the buffer in the same
transaction that clears the column — and the split is on the one column
dispatch's re-send gate actually reads:

* ``AlertRuleState.last_notified_at`` NULL — nobody was ever told about this
  scope, the gate fires on the NULL regardless of cooldown, so the row is
  DISCARDED and the immediate path delivers it. It re-reads the scope, so what
  arrives is the current number rather than a reading up to a cadence period
  old;
* STAMPED — a digest this destination already sent reported this scope, because
  ``alerts._stamp_rule_state`` runs for a digest send exactly as it does for an
  immediate one. The gate then needs a strictly newer bucket AND an elapsed
  cooldown, and neither holds at the switch, so the row is KEPT and the drain
  arm ships it.

Discarding the whole buffer instead — on the premise that a held scope "has
never been sent, so its ``last_notified_at`` is still NULL" — is only safe for a
destination that has never shipped a digest. ``AlertRuleState`` carries no
direction while a buffered row does, so a held DROP sits on the state row an
unrelated SPIKE stamped, with its own ``correlation_group_id`` and its own Inbox
card; discarding it destroys an incident nothing re-offers and leaves the scope
silent for up to ``cooldown_minutes``. That is the same trap
``dispatch._buffer_pending_items`` argues against, and the same one the drain
arm refuses to filter on ``last_notified_at`` to avoid: the predicate would
destroy a buffered DROP whenever an unrelated SPIKE on the same scope was sent.

The drain arm keeps its other job too — rows that COMMIT after the switch, from
an in-flight ``collect_metrics`` that read the cadence before it was cleared.

Sync sqlite fixtures mirror ``test_alert_digest_delivery.py``. The service is
async and the worker is sync, so both run against one sqlite FILE rather than
the suite's shared in-memory engine — that is the only way one test can drive
the operator's PATCH and then the two delivery paths it decides between.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_pending_item import AlertPendingItem
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.data_source import DataSource
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.schemas.alerting import AlertDestinationResponse, AlertDestinationUpdate
from tripl.services._alerting_destinations import update_destination
from tripl.worker.tasks import alert_flush
from tripl.worker.tasks.metrics import dispatch as metrics_dispatch

# Recent, hour-aligned and tz-naive, matching the sync fixtures' bucket columns.
_BUCKET = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
# Fires once a day at 09:00; with a watermark of "just now" nothing is due, so
# the buffer accumulates and no digest can go out on its own schedule.
_DAILY = "0 9 * * *"
_HOURLY = "0 * * * *"


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    """One sqlite FILE, reachable from both the sync worker and the async service."""
    path = tmp_path / "cadence.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    yield path


@pytest.fixture
def sync_session_factory(db_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


async def _patch_cadence(
    db_path: Path,
    slug: str,
    destination_id: uuid.UUID,
    *,
    cron: str | None,
) -> AlertDestinationResponse:
    """Drive the REAL service call an operator's PATCH would.

    Through ``update_destination`` rather than a hand-written UPDATE, because the
    whole fix is what that function does around the column write — a test that
    cleared the cadence itself would pass with the fix reverted.

    The async engine is disposed before returning so no aiosqlite connection is
    still holding the file when the sync worker paths run next.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            return await update_destination(
                session,
                slug,
                destination_id,
                AlertDestinationUpdate(delivery_schedule_cron=cron),
            )
    finally:
        await engine.dispose()


def _seed(
    session: Session,
    *,
    cron: str | None,
    destination_count: int = 1,
) -> tuple[Project, ScanConfig, list[AlertDestination], EventType]:
    """A project with one scan, one event type, and N destinations on a cadence."""
    project = Project(
        id=uuid.uuid4(),
        name="Cadence",
        slug=f"cadence-{uuid.uuid4().hex[:8]}",
        description="",
        timezone="UTC",
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"DS {uuid.uuid4().hex[:8]}",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Scan",
        base_query="SELECT time, event_name FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval="1h",
    )
    settings = ProjectAnomalySettings(
        project_id=project.id,
        anomaly_detection_enabled=True,
        sigma_threshold=3.0,
        min_expected_count=10,
    )
    event_type = EventType(
        id=uuid.uuid4(),
        project_id=project.id,
        name="page",
        display_name="Page",
        description="",
    )
    session.add_all([project, data_source, config, settings, event_type])

    destinations: list[AlertDestination] = []
    for index in range(destination_count):
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project.id,
            type="slack",
            name=f"Slack {index}",
            enabled=True,
            webhook_url_encrypted="secret",
            delivery_schedule_cron=cron,
            # A destination on a cadence carries a fresh watermark, so nothing
            # is due and the buffer is genuinely being HELD.
            last_flushed_at=datetime.now(UTC) if cron is not None else None,
        )
        session.add(destination)
        session.add(
            AlertRule(
                id=uuid.uuid4(),
                destination_id=destination.id,
                name="Everything",
                enabled=True,
                include_project_total=False,
                include_event_types=True,
                include_events=False,
                notify_on_spike=True,
                notify_on_drop=True,
                min_percent_delta=0,
                min_absolute_delta=0,
                min_expected_count=0,
                # The default. Load-bearing for this lane: it is long enough
                # that a scope already reported stays quiet, so a second
                # delivery cannot be explained away as the cooldown lapsing.
                cooldown_minutes=1440,
            )
        )
        destinations.append(destination)
    session.commit()
    return project, config, destinations, event_type


def _fire_anomaly(
    session: Session,
    config: ScanConfig,
    event_type: EventType,
    *,
    actual: float = 200.0,
    direction: str = "spike",
    bucket: datetime = _BUCKET,
) -> None:
    """Replace the scope's live anomaly, the way a recalculation would.

    The stored bucket goes in too: dispatch only treats a scope as a live
    candidate while the scan has metrics for it.

    ``direction`` and ``bucket`` are what the FLIP case needs. The same scope
    moving the other way is a different incident — its own buffered row, its own
    ``correlation_group_id`` — and it has to arrive on a strictly newer bucket,
    or dispatch's freshness half refuses to re-offer the scope at all once it
    has been notified.
    """
    session.execute(delete(MetricAnomaly).where(MetricAnomaly.scan_config_id == config.id))
    session.add(
        EventMetric(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            event_id=None,
            event_type_id=event_type.id,
            bucket=bucket,
            count=int(actual),
        )
    )
    session.add(
        MetricAnomaly(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            event_type_id=event_type.id,
            event_id=None,
            scope_type="event_type",
            scope_ref=str(event_type.id),
            bucket=bucket,
            direction=direction,
            actual_count=actual,
            expected_count=10.0,
            stddev=1.0,
            z_score=10.0 if direction == "spike" else -10.0,
        )
    )
    session.commit()


def _collect(factory: sessionmaker[Session], config_id: uuid.UUID) -> list[uuid.UUID]:
    """One metrics collection's alert pass."""
    with factory() as session:
        config = session.get(ScanConfig, config_id)
        assert config is not None
        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()
        return delivery_ids


def _run_flush(
    monkeypatch: pytest.MonkeyPatch,
    factory: sessionmaker[Session],
) -> tuple[dict[str, int], list[str]]:
    """One flush tick, capturing both dispatch routes."""
    enqueued: list[str] = []
    digests: list[str] = []
    from tripl.worker.tasks import alert_digest_send as digest_module
    from tripl.worker.tasks import alerts as alerts_module

    monkeypatch.setattr(alert_flush, "_get_sync_session", factory)
    monkeypatch.setattr(
        alerts_module.send_alert_delivery,
        "delay",
        lambda delivery_id: enqueued.append(delivery_id),
    )
    monkeypatch.setattr(
        digest_module.send_alert_digest,
        "delay",
        lambda delivery_ids: digests.extend(list(delivery_ids)),
    )
    result = alert_flush.flush_due_alert_digests.run()
    return result, enqueued + digests


# ── the transition ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clearing_the_cadence_discards_a_held_scope_nobody_was_told_about(
    db_path: Path,
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The NULL half of the split: the immediate path can re-deliver, so it owns it.

    Reverting the fix leaves the row in place and the card still reading
    ``1 held`` for a destination that has no window left to hold it for — and
    the scope then arrives twice, once from each path.

    This destination has never shipped a digest, which is the ONLY condition
    under which discarding a held row is free. The stamped case is the sibling
    test below.
    """
    with sync_session_factory() as session:
        project, config, destinations, event_type = _seed(session, cron=_DAILY)
        _fire_anomaly(session, config, event_type)
    _collect(sync_session_factory, config.id)

    with sync_session_factory() as session:
        assert len(session.execute(select(AlertPendingItem)).scalars().all()) == 1
        state = session.execute(select(AlertRuleState)).scalars().one()
        # The precondition that makes the double send possible at all, and the
        # precondition for discarding the row: nothing has SENT this scope, so
        # nothing stamped this column and dispatch's gate fires on the NULL.
        assert state.last_notified_at is None

    response = await _patch_cadence(db_path, project.slug, destinations[0].id, cron=None)

    assert response.delivery_schedule_cron is None
    assert response.next_digest_at is None
    assert response.last_digest_at is None
    # The `12 held` badge is backed by this count, and it must not go on
    # promising a digest that will never be assembled.
    assert response.held_count == 0

    with sync_session_factory() as session:
        assert session.execute(select(AlertPendingItem)).scalars().all() == []
        # Rule states SURVIVE, unlike the disable path. Off a cadence,
        # `last_notified_at` is the rate limiter, so dropping these rows would
        # re-announce every scope the last digest already reported.
        surviving = session.execute(select(AlertRuleState)).scalars().one()
        assert surviving.last_notified_at is None
        assert surviving.is_active is True


@pytest.mark.asyncio
async def test_a_held_scope_is_delivered_exactly_once_after_the_switch(
    db_path: Path,
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect itself: both paths used to ship the same scope.

    Runs the two of them in the order production does — the flusher beats every
    60s, ``check-metrics-due`` every 300s — and counts what the operator
    actually receives.

    With the discard reverted this fails three ways: the drain arm mints a
    digest (``flushed == 1`` and one enqueued id), the next collection mints an
    immediate delivery off the same NULL ``last_notified_at``, and the totals
    come to two deliveries carrying two items for ONE scope.
    """
    with sync_session_factory() as session:
        project, config, destinations, event_type = _seed(session, cron=_DAILY)
        _fire_anomaly(session, config, event_type)
    assert _collect(sync_session_factory, config.id) == []

    await _patch_cadence(db_path, project.slug, destinations[0].id, cron=None)

    # (1) The flusher's next tick. It has nothing to drain, because the
    # transition already handed these scopes to the immediate path.
    result, enqueued = _run_flush(monkeypatch, sync_session_factory)
    assert result["flushed"] == 0
    assert result["deliveries"] == 0
    assert enqueued == []
    assert result["swept"] == 0

    # (2) The next collection. THIS is the path that delivers the held scope,
    # and it fires on the NULL `last_notified_at` regardless of the 1440-minute
    # cooldown.
    delivery_ids = _collect(sync_session_factory, config.id)
    assert len(delivery_ids) == 1

    with sync_session_factory() as session:
        deliveries = session.execute(select(AlertDelivery)).scalars().all()
        assert len(deliveries) == 1
        items = session.execute(select(AlertDeliveryItem)).scalars().all()
        assert len(items) == 1
        assert items[0].scope_ref == str(event_type.id)
        # Minted by the immediate path, not by a digest: `_create_deliveries`
        # stamps `digest` into the snapshot only when the flush calls it.
        snapshot = deliveries[0].payload_snapshot
        assert isinstance(snapshot, dict)
        assert snapshot.get("digest") is None
        assert session.execute(select(AlertPendingItem)).scalars().all() == []


@pytest.mark.asyncio
async def test_clearing_the_cadence_keeps_an_incident_whose_scope_a_digest_stamped(
    db_path: Path,
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the split — the half "still NULL" got wrong.

    "A held scope has never been sent" holds only until this destination ships
    its FIRST digest: ``alert_digest_send`` calls ``alerts._stamp_rule_state``
    for every member of a sent digest, exactly as the immediate path does, so
    from then on every scope that digest reported carries a non-NULL
    ``last_notified_at``.

    ``AlertRuleState`` has no direction column while the buffer is keyed per
    direction, so the scope's NEXT movement buffers a SECOND, distinct incident
    — its own ``correlation_group_id``, its own Inbox card — on top of that
    stamp. Discard it and nothing re-offers it: dispatch's gate then wants a
    strictly newer bucket (``last_anomaly_bucket`` was already advanced while
    buffering) AND an elapsed cooldown (1440 minutes here). Step (4) runs that
    argument rather than asserting it.

    Revert the ``~already_notified`` predicate on the DELETE in
    ``_alerting_destinations.update_destination`` — discard the whole buffer
    again, as the "still NULL" premise said was safe — and this goes red three
    times: ``held_count``, the surviving row, and the drain arm's delivery.
    """
    with sync_session_factory() as session:
        project, config, destinations, event_type = _seed(session, cron=_HOURLY)
        # Put an hourly window in the past so the digest below actually fires;
        # `_seed` hands out a fresh watermark precisely to stop that happening.
        destinations[0].last_flushed_at = datetime.now(UTC) - timedelta(hours=2)
        session.commit()
        _fire_anomaly(session, config, event_type)
    assert _collect(sync_session_factory, config.id) == []

    # (1) The cadence fires and its digest is SENT. `_run_flush` stubs the send
    # task, so the two lines of its sent block that this test turns on are run
    # here directly — `alert_digest_send` sets `delivery.sent_at` and then calls
    # `_stamp_rule_state(session, delivery)` for every member.
    result, _enqueued = _run_flush(monkeypatch, sync_session_factory)
    assert result["flushed"] == 1
    with sync_session_factory() as session:
        from tripl.worker.tasks.alerts import _stamp_rule_state

        digest = session.execute(select(AlertDelivery)).scalars().one()
        digest_id = digest.id
        digest.sent_at = datetime.now(UTC) - timedelta(minutes=30)
        _stamp_rule_state(session, digest)
        session.commit()
        stamped = session.execute(select(AlertRuleState)).scalars().one()
        # From here the "a held scope's clock is still NULL" premise is false
        # for this scope, on a destination that is still on its cadence.
        assert stamped.last_notified_at is not None
        assert session.execute(select(AlertPendingItem)).scalars().all() == []

    # (2) The scope FLIPS. The drop is a different incident, and it buffers on
    # top of the state row the spike above stamped, because that row carries no
    # direction to tell the two apart.
    with sync_session_factory() as session:
        _fire_anomaly(
            session,
            config,
            event_type,
            actual=2.0,
            direction="drop",
            bucket=_BUCKET + timedelta(hours=1),
        )
    assert _collect(sync_session_factory, config.id) == []
    with sync_session_factory() as session:
        held = session.execute(select(AlertPendingItem)).scalars().one()
        assert held.direction == "drop"

    # (3) The operator switches to Immediately, precisely to stop waiting an
    # hour for this drop.
    response = await _patch_cadence(db_path, project.slug, destinations[0].id, cron=None)

    assert response.delivery_schedule_cron is None
    # KEPT, where the never-notified row of the sibling test above is discarded.
    assert response.held_count == 1
    with sync_session_factory() as session:
        surviving = session.execute(select(AlertPendingItem)).scalars().one()
        assert surviving.direction == "drop"

    # (4) The immediate path CANNOT deliver this one, which is why discarding
    # the row loses the incident outright instead of handing it over: the stamp
    # defeats the NULL short-circuit, the bucket is not newer than the one
    # buffering already recorded, and the cooldown has 1440 minutes to run.
    assert _collect(sync_session_factory, config.id) == []

    # (5) The drain arm delivers it, once, with the numbers it was buffered
    # with.
    result, enqueued = _run_flush(monkeypatch, sync_session_factory)
    assert result["flushed"] == 1
    assert len(enqueued) == 1
    with sync_session_factory() as session:
        deliveries = session.execute(select(AlertDelivery)).scalars().all()
        assert len(deliveries) == 2
        drained = next(delivery for delivery in deliveries if delivery.id != digest_id)
        assert [item.direction for item in drained.items] == ["drop"]
        assert session.execute(select(AlertPendingItem)).scalars().all() == []


@pytest.mark.asyncio
async def test_changing_between_two_cadences_still_keeps_what_is_held(
    db_path: Path,
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The discard is for cadence -> NULL only.

    Daily -> hourly still HAS a next window, and the refreshed watermark starts
    its clock there, so the held alerts are delivered on the new schedule. A
    discard widened to every cadence change would silently drop them, and this
    is what says so.
    """
    with sync_session_factory() as session:
        project, config, destinations, event_type = _seed(session, cron=_DAILY)
        _fire_anomaly(session, config, event_type)
    _collect(sync_session_factory, config.id)

    response = await _patch_cadence(db_path, project.slug, destinations[0].id, cron=_HOURLY)

    assert response.delivery_schedule_cron == _HOURLY
    assert response.held_count == 1
    assert response.last_digest_at is not None
    assert response.next_digest_at is not None
    with sync_session_factory() as session:
        assert len(session.execute(select(AlertPendingItem)).scalars().all()) == 1


@pytest.mark.asyncio
async def test_clearing_one_cadence_leaves_another_destination_holding(
    db_path: Path,
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The discard is scoped to the destination whose column changed.

    Two destinations on one project hold the same scope independently; losing
    the WHERE clause would empty a destination nobody touched, and on a daily
    cadence that is a day of alerts gone with no trace.
    """
    with sync_session_factory() as session:
        project, config, destinations, event_type = _seed(session, cron=_DAILY, destination_count=2)
        _fire_anomaly(session, config, event_type)
    _collect(sync_session_factory, config.id)

    with sync_session_factory() as session:
        assert len(session.execute(select(AlertPendingItem)).scalars().all()) == 2

    switched, untouched = destinations[0], destinations[1]
    await _patch_cadence(db_path, project.slug, switched.id, cron=None)

    with sync_session_factory() as session:
        remaining = session.execute(select(AlertPendingItem)).scalars().all()
        assert len(remaining) == 1
        assert remaining[0].destination_id == untouched.id


# ── the backstop ──────────────────────────────────────────────────────────


def test_the_drain_arm_still_ships_a_buffer_that_outlived_the_switch(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows that commit AFTER the transition are still the flusher's to deliver.

    ``collect_metrics`` reads the cadence at dispatch time and commits minutes
    later, so a collection already in flight when the operator saves writes its
    buffer rows against a destination that no longer has a cadence. The
    scheduled loop only looks at destinations that HAVE one, so without this arm
    those rows would sit until the 14-day sweep dropped them.

    The cadence is cleared on the row directly here, deliberately: that is
    exactly the state the race leaves behind, and it is the one case the service
    never saw. Deleting the drain arm — the tempting "simplification" once the
    service settles the buffer itself — turns this red.
    """
    with sync_session_factory() as session:
        _project, config, destinations, event_type = _seed(session, cron=_DAILY)
        _fire_anomaly(session, config, event_type)
    _collect(sync_session_factory, config.id)

    with sync_session_factory() as session:
        # NOT through the service: this models the in-flight collection, whose
        # rows land after the column was already cleared.
        raced = session.get(AlertDestination, destinations[0].id)
        assert raced is not None
        raced.delivery_schedule_cron = None
        session.commit()
        assert len(session.execute(select(AlertPendingItem)).scalars().all()) == 1

    result, enqueued = _run_flush(monkeypatch, sync_session_factory)

    assert result["flushed"] == 1
    assert len(enqueued) == 1
    with sync_session_factory() as session:
        assert len(session.execute(select(AlertDelivery)).scalars().all()) == 1
        assert session.execute(select(AlertPendingItem)).scalars().all() == []
        refreshed = session.get(AlertDestination, destinations[0].id)
        assert refreshed is not None
        assert refreshed.last_flushed_at is None


def test_the_drain_arm_does_not_filter_on_the_notification_clock(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rejected alternative, pinned so it is not adopted later.

    "Skip buffered rows whose scope has been notified since they were buffered"
    reads like a cheap guard against the double send. It is not available:
    ``AlertRuleState`` carries no direction while a buffered row does, so a
    buffered DROP shares its state row with the SPIKE that was just delivered
    and the predicate would destroy an incident that was never sent and that
    nothing re-offers — the trap ``dispatch._buffer_pending_items`` argues in
    full.

    Here the scope's state is stamped as freshly notified and the buffered row
    is older than the stamp. It must still ship.
    """
    with sync_session_factory() as session:
        _project, config, destinations, event_type = _seed(session, cron=_DAILY)
        _fire_anomaly(session, config, event_type)
    _collect(sync_session_factory, config.id)

    with sync_session_factory() as session:
        state = session.execute(select(AlertRuleState)).scalars().one()
        state.last_notified_at = datetime.now(UTC) + timedelta(hours=1)
        buffered = session.execute(select(AlertPendingItem)).scalars().one()
        assert buffered.updated_at is not None
        switched = session.get(AlertDestination, destinations[0].id)
        assert switched is not None
        switched.delivery_schedule_cron = None
        session.commit()

    result, enqueued = _run_flush(monkeypatch, sync_session_factory)

    assert result["flushed"] == 1
    assert len(enqueued) == 1
    with sync_session_factory() as session:
        assert len(session.execute(select(AlertDelivery)).scalars().all()) == 1


def test_the_drain_arm_honours_a_rule_mute(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backstop is a delivery path, so a muted monitor silences it too.

    The drain arm mints its digest through ``_build_digest`` rather than
    shortcutting to ``_create_deliveries``, and that function re-reads
    ``AlertRule.muted_until`` for every group it assembles. Take the check out
    (or let the drain arm skip the function) and muting a monitor, then
    switching its destination back to "Immediately", ships the whole held
    buffer within a minute — the mute DELIVERING the alerts it was pressed to
    suppress, on the one path the operator had no reason to expect.

    A skipped group is still claimed and deleted, so the rows do not sit
    waiting to arrive in the first digest after the mute lapses.
    """
    with sync_session_factory() as session:
        _project, config, destinations, event_type = _seed(session, cron=_DAILY)
        _fire_anomaly(session, config, event_type)
    _collect(sync_session_factory, config.id)

    with sync_session_factory() as session:
        assert len(session.execute(select(AlertPendingItem)).scalars().all()) == 1
        # The race the drain arm exists for, plus a mute pressed during the
        # hold window: the buffer predates both.
        raced = session.get(AlertDestination, destinations[0].id)
        assert raced is not None
        raced.delivery_schedule_cron = None
        rule = session.execute(select(AlertRule)).scalars().one()
        rule.muted_until = datetime.now(UTC) + timedelta(hours=6)
        session.commit()

    result, enqueued = _run_flush(monkeypatch, sync_session_factory)

    assert result["flushed"] == 0
    assert result["deliveries"] == 0
    assert enqueued == []
    # Not the 14-day sweep either: these rows are minutes old, and the mute is
    # what dropped them.
    assert result["swept"] == 0
    with sync_session_factory() as session:
        assert session.execute(select(AlertDelivery)).scalars().all() == []
        assert session.execute(select(AlertPendingItem)).scalars().all() == []


# ── the comments that carry the argument ──────────────────────────────────


def test_the_code_says_which_path_delivers_a_held_scope() -> None:
    """Two paths can ship a held scope, so the source has to name the owner.

    A comment that is no longer true is the defect that produced this one: the
    drain arm described itself as the thing that delivers what a cadence switch
    left behind, which is precisely the behaviour that double-sent. These are
    guards on the claims the fix rests on, not spell-checks — each names the
    OTHER site a reader has to visit.
    """
    flush_source = inspect.getsource(alert_flush)
    # The stale claim, verbatim from before the fix.
    assert 'which is what "immediate" now means for that channel' not in flush_source
    assert "is not the handoff" in flush_source
    assert "SPLITS its buffer" in flush_source
    assert "update_destination" in flush_source

    transition = inspect.getsource(update_destination)
    # BOTH halves, named where they are decided. A reader who finds only the
    # discard is back at the premise retired below.
    assert "DISCARDED" in transition
    assert "KEPT" in transition
    # The cooldown argument is the reason rule states are NOT cleared here, and
    # it is the difference from the disable branch directly below it.
    assert (
        "clear_rule_states"
        not in transition.split("if cadence is None:")[1].split('if "enabled" in update_dict:')[0]
    )

    # The buffer's delete list is exhaustive by construction; a new exit that
    # is not listed there is how the next reader gets this wrong again.
    buffer_doc = metrics_dispatch._buffer_pending_items.__doc__ or ""
    assert "cadence" in buffer_doc.split("the only")[1].split("so a scope that fired")[0]

    model_doc = AlertPendingItem.__doc__ or ""
    assert "four ways" in model_doc

    # The premise the split retired, verbatim from before it. It justified
    # discarding a held row on the grounds that the immediate path always
    # re-delivers the scope, which is true only for a destination that has never
    # shipped a digest — and it was repeated at each site a reader lands on, so
    # every one of them sent that reader the same wrong way.
    stale = "last_notified_at`` is still NULL"
    assert stale not in transition
    assert stale not in flush_source
    assert stale not in inspect.getsource(metrics_dispatch)


def test_the_rule_mute_comment_reads_as_history_and_the_model_agrees() -> None:
    """A comment that quotes a sibling file has to age with it.

    The rule-mute block narrates its own bug: ``AlertRule.muted_until`` shipped
    with a model comment calling worker-side suppression "a separate
    follow-up", so the Mute button wrote a column nothing read. That model
    comment has since been corrected — it now names the worker readers — while
    the block here went on saying "the model comment called ... a separate
    follow-up", which reads as the sibling's CURRENT contents rather than as
    the history it is. A reader who followed the pointer found the opposite
    text and had to guess which of the two was stale.

    So these assertions do not spell-check the prose; they check the claims the
    block makes about a file it does not own. Revert either half — the tense
    here, or the model comment there — and one of them goes red.
    """
    dispatch_source = inspect.getsource(metrics_dispatch)
    assert "# A muted monitor delivers nothing." in dispatch_source
    assert "rule_muted_until = _as_utc" in dispatch_source
    mute_block = dispatch_source.split("# A muted monitor delivers nothing.")[1].split(
        "rule_muted_until = _as_utc"
    )[0]
    # Verbatim from before the fix.
    assert 'model comment called worker-side suppression "a separate' not in mute_block
    assert "USED TO call worker-side" in mute_block

    model_module = inspect.getmodule(AlertRule)
    assert model_module is not None
    model_source = inspect.getsource(model_module)
    # What the block above now asserts about that file, verified instead of
    # trusted: the stale sentence is gone, and every reader is named.
    assert "is a separate follow-up" not in model_source
    assert "_prepare_alert_deliveries" in model_source
    assert "alert_flush._build_digest" in model_source

    # All worker readers must stay named in the model comment. The send-time
    # guard added a third reader after the original dispatch/flush pair.
    worker_root = Path(alert_flush.__file__).resolve().parent.parent
    readers = sorted(
        path.name
        for path in worker_root.rglob("*.py")
        if "rule.muted_until" in path.read_text(encoding="utf-8")
    )
    assert readers == [
        "alert_flush.py",
        "alert_owner_notify.py",
        "alerts.py",
        "dispatch.py",
        "freshness_sweep.py",
    ]
    assert "alerts._assert_rule_still_active" in model_source
    assert "metrics.freshness_sweep" in model_source
    assert "alert_owner_notify._rule_is_muted" in model_source
