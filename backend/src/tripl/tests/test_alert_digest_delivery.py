"""Scheduled alert digests: buffer while held, deliver on the cadence.

A destination with ``delivery_schedule_cron`` set stops delivering after every
metrics collection. Its matched signals accumulate in ``alert_pending_items``
and ``flush_due_alert_digests`` turns them into ordinary deliveries when a cron
boundary passes.

The properties worth pinning are the ones the feature would be worthless
without: an alert is never lost across a flush boundary, never sent twice, and
the message carries the numbers as of the moment it was SENT rather than the
moment the incident opened.

Sync sqlite fixtures mirror ``test_metric_anomaly_scope.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_pending_item import AlertPendingItem
from tripl.models.alert_rule import AlertRule
from tripl.models.data_source import DataSource
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import alert_flush
from tripl.worker.tasks.metrics import dispatch as metrics_dispatch

# Recent, hour-aligned UTC bucket matching persisted values.
_BUCKET = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
# Fires every minute, so any flush with a watermark in the past is due. Used
# wherever the test is about the flush mechanism rather than about cron itself.
_ALWAYS_DUE = "* * * * *"
# Fires once a day at 09:00; with a watermark of "just now" nothing is due.
_DAILY = "0 9 * * *"


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'alert_digest.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _seed(
    session: Session,
    *,
    cron: str | None,
    last_flushed_at: datetime | None = None,
    timezone: str = "UTC",
) -> tuple[ScanConfig, AlertDestination, AlertRule, EventType]:
    """A project with one scan, one destination, one rule and one event type."""
    project = Project(
        id=uuid.uuid4(),
        name="Digest",
        slug=f"digest-{uuid.uuid4().hex[:8]}",
        description="",
        timezone=timezone,
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
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=project.id,
        type="slack",
        name="Main Slack",
        enabled=True,
        webhook_url_encrypted="secret",
        delivery_schedule_cron=cron,
        last_flushed_at=last_flushed_at,
    )
    rule = AlertRule(
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
        cooldown_minutes=1440,
    )
    session.add_all([project, data_source, config, settings, event_type, destination, rule])
    session.commit()
    return config, destination, rule, event_type


def _fire_anomaly(
    session: Session,
    config: ScanConfig,
    event_type: EventType,
    *,
    actual: float,
    bucket: datetime | None = None,
) -> None:
    """Replace the scope's live anomaly, the way a recalculation would.

    The stored bucket goes in too: dispatch only treats a scope as a live
    candidate while the scan has metrics for it, so an anomaly with no
    EventMetric behind it is invisible to the alert path.
    """
    at = bucket or _BUCKET
    session.execute(
        MetricAnomaly.__table__.delete().where(MetricAnomaly.scan_config_id == config.id)
    )
    session.add(
        EventMetric(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            event_id=None,
            event_type_id=event_type.id,
            bucket=at,
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
            bucket=at,
            direction="spike",
            actual_count=actual,
            expected_count=10.0,
            stddev=1.0,
            z_score=10.0,
        )
    )
    session.commit()


def _run_flush(
    monkeypatch: pytest.MonkeyPatch,
    factory: sessionmaker[Session],
    digests: list[list[str]] | None = None,
) -> tuple[dict[str, int], list[str]]:
    """Run one flush tick, capturing BOTH dispatch routes.

    `digests` collects the combined `send_alert_digest` calls. Every caller
    that does not pass one still asserts on the per-delivery list, so an
    accidental change of routing fails loudly instead of silently emptying it.
    """
    enqueued: list[str] = []
    from tripl.worker.tasks import alert_digest_send as digest_module
    from tripl.worker.tasks import alerts as alerts_module

    monkeypatch.setattr(alert_flush, "_get_sync_session", factory)
    monkeypatch.setattr(
        alerts_module.send_alert_delivery,
        "delay",
        lambda delivery_id: enqueued.append(delivery_id),
    )
    sink = digests if digests is not None else []
    monkeypatch.setattr(
        digest_module.send_alert_digest,
        "delay",
        lambda delivery_ids: sink.append(list(delivery_ids)),
    )
    return alert_flush.flush_due_alert_digests.run(), enqueued


# ── buffering ─────────────────────────────────────────────────────────────


def test_a_scheduled_destination_buffers_instead_of_delivering(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, destination, rule, event_type = _seed(session, cron=_DAILY)
        _fire_anomaly(session, config, event_type, actual=200.0)

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

        assert delivery_ids == []
        assert session.execute(select(AlertDelivery)).scalars().all() == []
        buffered = session.execute(select(AlertPendingItem)).scalars().all()
        assert len(buffered) == 1
        assert buffered[0].destination_id == destination.id
        assert buffered[0].rule_id == rule.id
        assert buffered[0].actual_count == 200.0
        assert buffered[0].observation_count == 1


def test_an_immediate_destination_still_delivers_on_the_spot(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The upgrade must be invisible to anyone who sets no cadence."""
    with sync_session_factory() as session:
        config, _destination, _rule, event_type = _seed(session, cron=None)
        _fire_anomaly(session, config, event_type, actual=200.0)

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

        assert len(delivery_ids) == 1
        assert session.execute(select(AlertPendingItem)).scalars().all() == []
        assert len(session.execute(select(AlertDeliveryItem)).scalars().all()) == 1


def test_a_scope_that_keeps_firing_collapses_to_one_row_with_the_latest_numbers(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The whole point of "aggregated up to the moment it is sent".

    Twenty-four collections of one still-broken scope must not become
    twenty-four lines, and the line that ships must carry the LAST reading, not
    the first.
    """
    with sync_session_factory() as session:
        config, _destination, _rule, event_type = _seed(session, cron=_DAILY)

        _fire_anomaly(session, config, event_type, actual=200.0, bucket=_BUCKET)
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

        _fire_anomaly(
            session, config, event_type, actual=999.0, bucket=_BUCKET + timedelta(hours=1)
        )
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

        buffered = session.execute(select(AlertPendingItem)).scalars().all()
        assert len(buffered) == 1
        assert buffered[0].actual_count == 999.0
        assert buffered[0].observation_count == 2


def test_a_later_collection_of_an_older_bucket_never_rewinds_the_numbers(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, _destination, _rule, event_type = _seed(session, cron=_DAILY)

        _fire_anomaly(
            session, config, event_type, actual=999.0, bucket=_BUCKET + timedelta(hours=1)
        )
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

        _fire_anomaly(session, config, event_type, actual=200.0, bucket=_BUCKET)
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

        buffered = session.execute(select(AlertPendingItem)).scalars().one()
        assert buffered.actual_count == 999.0


# ── flushing ──────────────────────────────────────────────────────────────


def test_the_flusher_mints_a_delivery_and_empties_the_buffer(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config, destination, _rule, event_type = _seed(
            session,
            cron=_ALWAYS_DUE,
            last_flushed_at=datetime.now(UTC) - timedelta(hours=2),
        )
        _fire_anomaly(session, config, event_type, actual=200.0)
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

    result, enqueued = _run_flush(monkeypatch, sync_session_factory)

    assert result["flushed"] == 1
    assert result["deliveries"] == 1
    assert len(enqueued) == 1

    with sync_session_factory() as session:
        delivery = session.execute(select(AlertDelivery)).scalars().one()
        assert delivery.destination_id == destination.id
        assert delivery.status == "pending"
        assert delivery.matched_count == 1
        item = session.execute(select(AlertDeliveryItem)).scalars().one()
        assert item.actual_count == 200.0
        # The buffer is claimed by deletion in the same transaction that minted
        # the delivery, so it is empty and cannot be shipped a second time.
        assert session.execute(select(AlertPendingItem)).scalars().all() == []
        refreshed = session.get(AlertDestination, destination.id)
        assert refreshed is not None and refreshed.last_flushed_at is not None


def test_a_second_flush_inside_the_same_window_sends_nothing(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compare-and-set on last_flushed_at is the no-double-send guard."""
    with sync_session_factory() as session:
        config, _destination, _rule, event_type = _seed(
            session,
            cron=_DAILY,
            last_flushed_at=datetime.now(UTC) - timedelta(days=2),
        )
        _fire_anomaly(session, config, event_type, actual=200.0)
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

    first, first_enqueued = _run_flush(monkeypatch, sync_session_factory)
    second, second_enqueued = _run_flush(monkeypatch, sync_session_factory)

    assert first["flushed"] == 1
    assert second["flushed"] == 0
    assert len(first_enqueued) == 1
    assert second_enqueued == []

    with sync_session_factory() as session:
        assert len(session.execute(select(AlertDelivery)).scalars().all()) == 1


def test_an_alert_that_arrives_after_the_claim_lands_in_the_next_digest_exactly_once(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary race, which is the user's literal requirement.

    A buffered row is claimed by DELETING it in the transaction that mints the
    delivery — no timestamp predicate anywhere. So a row that becomes visible
    after a flush has taken its snapshot is simply still buffered, and ships in
    the NEXT digest. It is never dropped, and never sent twice.
    """
    with sync_session_factory() as session:
        config, _destination, _rule, event_type = _seed(
            session,
            cron=_ALWAYS_DUE,
            last_flushed_at=datetime.now(UTC) - timedelta(hours=2),
        )
        _fire_anomaly(session, config, event_type, actual=200.0)
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

    _run_flush(monkeypatch, sync_session_factory)

    # Arrives after the first digest was claimed and sent.
    with sync_session_factory() as session:
        config = session.execute(select(ScanConfig)).scalars().one()
        event_type = session.execute(select(EventType)).scalars().one()
        _fire_anomaly(
            session, config, event_type, actual=555.0, bucket=_BUCKET + timedelta(hours=1)
        )
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

    with sync_session_factory() as session:
        destination = session.execute(select(AlertDestination)).scalars().one()
        destination.last_flushed_at = datetime.now(UTC) - timedelta(hours=1)
        session.commit()

    _run_flush(monkeypatch, sync_session_factory)

    with sync_session_factory() as session:
        items = session.execute(select(AlertDeliveryItem).join(AlertDelivery)).scalars().all()
        actuals = sorted(item.actual_count for item in items)
        # Both readings delivered, each exactly once.
        assert actuals == [200.0, 555.0]
        assert len(session.execute(select(AlertDelivery)).scalars().all()) == 2
        assert session.execute(select(AlertPendingItem)).scalars().all() == []


def test_an_empty_window_sends_nothing_but_still_advances_the_watermark(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not cosmetic: a watermark that did not advance would make the next
    alert to arrive flush within the minute, turning a daily destination back
    into a near-immediate one — the exact bug being fixed."""
    before = datetime.now(UTC) - timedelta(days=2)
    with sync_session_factory() as session:
        _seed(session, cron=_DAILY, last_flushed_at=before)

    result, enqueued = _run_flush(monkeypatch, sync_session_factory)

    assert result["flushed"] == 0
    assert enqueued == []
    with sync_session_factory() as session:
        assert session.execute(select(AlertDelivery)).scalars().all() == []
        destination = session.execute(select(AlertDestination)).scalars().one()
        assert destination.last_flushed_at is not None
        assert destination.last_flushed_at > before


def test_a_destination_whose_cadence_has_not_come_round_is_left_alone(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config, _destination, _rule, event_type = _seed(
            session, cron=_DAILY, last_flushed_at=datetime.now(UTC)
        )
        _fire_anomaly(session, config, event_type, actual=200.0)
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

    result, enqueued = _run_flush(monkeypatch, sync_session_factory)

    assert result["flushed"] == 0
    assert enqueued == []
    with sync_session_factory() as session:
        # Held, not lost.
        assert len(session.execute(select(AlertPendingItem)).scalars().all()) == 1


def test_a_destination_with_no_watermark_adopts_the_clock_without_dumping_a_backlog(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config, _destination, _rule, event_type = _seed(
            session, cron=_ALWAYS_DUE, last_flushed_at=None
        )
        _fire_anomaly(session, config, event_type, actual=200.0)
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

    result, enqueued = _run_flush(monkeypatch, sync_session_factory)

    assert result["flushed"] == 0
    assert enqueued == []
    with sync_session_factory() as session:
        destination = session.execute(select(AlertDestination)).scalars().one()
        assert destination.last_flushed_at is not None
        assert len(session.execute(select(AlertPendingItem)).scalars().all()) == 1


def test_muting_a_monitor_during_the_hold_window_keeps_it_out_of_the_digest(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a daily cadence the hold window is a whole day, so suppression has to
    be re-checked when the digest is built, not only when it was buffered."""
    with sync_session_factory() as session:
        config, _destination, rule, event_type = _seed(
            session,
            cron=_ALWAYS_DUE,
            last_flushed_at=datetime.now(UTC) - timedelta(hours=2),
        )
        _fire_anomaly(session, config, event_type, actual=200.0)
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

        session.get(AlertRule, rule.id).muted_until = datetime.now(UTC) + timedelta(hours=6)
        session.commit()

    result, enqueued = _run_flush(monkeypatch, sync_session_factory)

    assert result["flushed"] == 0
    assert enqueued == []
    with sync_session_factory() as session:
        assert session.execute(select(AlertDelivery)).scalars().all() == []
        # Claimed and dropped, not left to re-deliver the moment the mute lapses.
        assert session.execute(select(AlertPendingItem)).scalars().all() == []


def test_a_disabled_destination_is_never_flushed(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config, destination, _rule, event_type = _seed(
            session,
            cron=_ALWAYS_DUE,
            last_flushed_at=datetime.now(UTC) - timedelta(hours=2),
        )
        _fire_anomaly(session, config, event_type, actual=200.0)
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.get(AlertDestination, destination.id).enabled = False
        session.commit()

    result, enqueued = _run_flush(monkeypatch, sync_session_factory)

    assert result["checked"] == 0
    assert enqueued == []


def test_buffer_rows_older_than_the_max_age_are_swept(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backstop for a cadence that will not fire again in any useful time."""
    with sync_session_factory() as session:
        config, _destination, _rule, event_type = _seed(
            session, cron=_DAILY, last_flushed_at=datetime.now(UTC)
        )
        _fire_anomaly(session, config, event_type, actual=200.0)
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()
        stale = session.execute(select(AlertPendingItem)).scalars().one()
        stale.updated_at = datetime.now(UTC) - alert_flush.PENDING_ITEM_MAX_AGE * 2
        session.commit()

    result, _enqueued = _run_flush(monkeypatch, sync_session_factory)

    assert result["swept"] == 1
    with sync_session_factory() as session:
        assert session.execute(select(AlertPendingItem)).scalars().all() == []


def test_switching_back_to_immediate_ships_what_was_still_being_held(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning the cadence off must not strand a day of alerts.

    The scheduled loop only looks at destinations that HAVE a cadence, so
    without the drain arm these rows would sit until the age sweep quietly
    dropped them — a silent loss of exactly the alerts the operator turned the
    schedule off to start receiving again.
    """
    with sync_session_factory() as session:
        config, destination, _rule, event_type = _seed(
            session, cron=_DAILY, last_flushed_at=datetime.now(UTC)
        )
        _fire_anomaly(session, config, event_type, actual=200.0)
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()
        assert len(session.execute(select(AlertPendingItem)).scalars().all()) == 1

        # Back to immediate.
        session.get(AlertDestination, destination.id).delivery_schedule_cron = None
        session.commit()

    result, enqueued = _run_flush(monkeypatch, sync_session_factory)

    assert result["flushed"] == 1
    assert len(enqueued) == 1
    with sync_session_factory() as session:
        assert len(session.execute(select(AlertDelivery)).scalars().all()) == 1
        assert session.execute(select(AlertPendingItem)).scalars().all() == []
        refreshed = session.execute(select(AlertDestination)).scalars().one()
        assert refreshed.last_flushed_at is None


@pytest.mark.asyncio
async def test_disabling_a_destination_discards_what_it_was_holding(client: AsyncClient) -> None:
    """Disabling already means "forget this destination's alerting state".

    A buffer that survived it would make a re-enable ship measurements from
    before the disable as though they were current — on a daily cadence, up to
    a day stale, rendered as a live page.
    """
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Digest Disable", "slug": "digest-disable", "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    destination_resp = await client.post(
        "/api/v1/projects/digest-disable/alert-destinations",
        json={
            "type": "slack",
            "name": "Daily Slack",
            "webhook_url": "https://hooks.slack.com/services/T0/B0/xxxxxxxxxxxxxxxxxxxxxxxx",
            "delivery_schedule_cron": "0 9 * * *",
        },
    )
    assert destination_resp.status_code == 201, destination_resp.text
    destination = destination_resp.json()
    assert destination["delivery_schedule_cron"] == "0 9 * * *"
    # A destination born with a cadence adopts the clock, so its first digest is
    # the next real fire rather than an immediate backlog dump.
    assert destination["last_digest_at"] is not None
    assert destination["next_digest_at"] is not None
    destination_id = uuid.UUID(destination["id"])

    rule_resp = await client.post(
        f"/api/v1/projects/digest-disable/alert-destinations/{destination_id}/rules",
        json={"name": "Everything"},
    )
    assert rule_resp.status_code == 201
    rule_id = uuid.UUID(rule_resp.json()["id"])

    async with TestSessionLocal() as session:
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
            project_id=project_id,
            name="Scan",
            base_query="SELECT time, event_name FROM events",
            time_column="time",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all([data_source, config])
        await session.flush()
        session.add(
            AlertPendingItem(
                id=uuid.uuid4(),
                project_id=project_id,
                destination_id=destination_id,
                rule_id=rule_id,
                scan_config_id=config.id,
                scope_type="event_type",
                scope_ref=str(uuid.uuid4()),
                scope_name="Page",
                bucket=datetime.now(UTC),
                direction="spike",
                actual_count=200.0,
                expected_count=10.0,
                correlation_group_id=uuid.uuid4(),
            )
        )
        await session.commit()

    disable_resp = await client.patch(
        f"/api/v1/projects/digest-disable/alert-destinations/{destination_id}",
        json={"enabled": False},
    )
    assert disable_resp.status_code == 200
    assert disable_resp.json()["enabled"] is False
    assert disable_resp.json()["last_digest_at"] is None

    async with TestSessionLocal() as session:
        held = (
            (
                await session.execute(
                    select(AlertPendingItem).where(
                        AlertPendingItem.destination_id == destination_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert held == []


def test_the_buffered_incident_handle_is_the_one_the_digest_will_deliver(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A `metric` scope is project-global; two scans must not open two incidents.

    The buffer keys metric scopes on NO scan config at all — `_scope_partition_id`
    answers NULL for `metric`, and `_correlation_group_id` hashes that same
    project-global partition, so now the two AGREE by
    construction. This test hands the buffer the OLD handle instead, derived from
    the FIRING config: the shape an older worker buffers during a
    rolling deploy. So the second scan's collection computes a DIFFERENT group id
    than the row already carries. Touching that computed id would leave a stray
    AlertCorrelationState: an inbox row an operator can acknowledge, holding a
    decision the digest can never honour because the delivered item references
    the other id.

    Exercised directly on `_buffer_pending_items` rather than through metric
    detection, so the assertion is about the id bookkeeping and nothing else.
    """
    from tripl.alerting_matching import DriftAlertCandidate
    from tripl.models.alert_correlation_state import AlertCorrelationState

    with sync_session_factory() as session:
        config_a, destination, rule, _event_type = _seed(session, cron=_DAILY)
        config_b = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=config_a.data_source_id,
            project_id=config_a.project_id,
            name="Scan B",
            base_query="SELECT time, event_name FROM events",
            time_column="time",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add(config_b)
        session.commit()
        scope_ref = str(uuid.uuid4())

        def buffer_from(config: ScanConfig) -> None:
            candidate = DriftAlertCandidate(
                id=uuid.uuid4(),
                scan_config_id=None,
                scope_type="metric",
                scope_ref=scope_ref,
                event_id=None,
                event_type_id=None,
                bucket=_BUCKET,
                direction="spike",
                actual_count=200.0,
                expected_count=10.0,
                drift_field=None,
                drift_type=None,
                sample_value=None,
            )
            metrics_dispatch._buffer_pending_items(
                session,
                config,
                rule=rule,
                destination=destination,
                anomalies=[candidate],
                scope_names={("metric", scope_ref): "Signups"},
                # The OLD handle, keyed on the FIRING config — what a
                # older worker buffers. Today's dispatch hashes
                # ``_scope_partition_id`` here, which is NULL for a metric scope.
                correlation_by_anomaly={
                    id(candidate): metrics_dispatch._correlation_group_id(
                        scan_config_id=config.id,
                        rule_id=rule.id,
                        scope_type="metric",
                        scope_ref=scope_ref,
                        direction="spike",
                    )
                },
                scan_job_id=None,
                now=datetime.now(UTC),
            )
            session.commit()

        buffer_from(config_a)
        buffer_from(config_b)

        buffered = session.execute(select(AlertPendingItem)).scalars().all()
        assert len(buffered) == 1, "a project-global metric buffers ONE row, not one per scan"
        assert buffered[0].observation_count == 2
        assert buffered[0].scan_config_id is None

        states = session.execute(select(AlertCorrelationState)).scalars().all()
        assert [state.correlation_group_id for state in states] == [
            buffered[0].correlation_group_id
        ], "one incident, keyed on the id the digest will actually deliver"


# ── one message per destination ──────────────────────────────


def _add_rule(session: Session, destination: AlertDestination, name: str) -> AlertRule:
    rule = AlertRule(
        id=uuid.uuid4(),
        destination_id=destination.id,
        name=name,
        enabled=True,
        include_project_total=False,
        include_event_types=True,
        include_events=False,
        notify_on_spike=True,
        notify_on_drop=True,
        min_percent_delta=0,
        min_absolute_delta=0,
        min_expected_count=0,
        cooldown_minutes=1440,
    )
    session.add(rule)
    session.commit()
    return rule


def _run_digest(
    monkeypatch: pytest.MonkeyPatch,
    factory: sessionmaker[Session],
    delivery_ids: list[str],
    *,
    fail_with: Exception | None = None,
) -> tuple[dict[str, object], list[tuple[str, str]]]:
    """Run the combined send, capturing the outbound Slack calls."""
    from tripl.worker.tasks import alert_digest_send as digest_module
    from tripl.worker.tasks import alerts as alerts_module

    posts: list[tuple[str, str]] = []

    def fake_slack(webhook_url: str, text: str, *, message_format: str) -> None:
        if fail_with is not None:
            raise fail_with
        posts.append((text, message_format))

    monkeypatch.setattr(digest_module, "_get_sync_session", factory)
    monkeypatch.setattr(alerts_module, "_send_slack_message", fake_slack)
    # The fixture stores a placeholder rather than a real encrypted webhook;
    # credential resolution is `send_alert_delivery`'s contract and is covered
    # there, so it is stubbed out of the way here.
    monkeypatch.setattr(
        alerts_module, "_resolve_slack_webhook", lambda destination: "https://hooks.slack.com/x"
    )
    return digest_module.send_alert_digest.run(delivery_ids), posts


def test_two_rules_on_one_destination_become_one_message(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: a digest is one message, not one per rule."""
    with sync_session_factory() as session:
        config, destination, _rule_a, event_type = _seed(
            session,
            cron=_ALWAYS_DUE,
            last_flushed_at=datetime.now(UTC) - timedelta(hours=2),
        )
        _add_rule(session, destination, "Second monitor")
        _fire_anomaly(session, config, event_type, actual=200.0)
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

    digests: list[list[str]] = []
    result, per_delivery = _run_flush(monkeypatch, sync_session_factory, digests)

    assert result["deliveries"] == 2, "two rules matched, so two audit rows"
    # ...but ONE outbound task, carrying both.
    assert per_delivery == [], "a combinable multi-rule digest must not fan out"
    assert len(digests) == 1
    assert len(digests[0]) == 2


def test_a_single_rule_keeps_the_ordinary_per_delivery_path(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing new happens when there is nothing to combine."""
    with sync_session_factory() as session:
        config, _destination, _rule, event_type = _seed(
            session,
            cron=_ALWAYS_DUE,
            last_flushed_at=datetime.now(UTC) - timedelta(hours=2),
        )
        _fire_anomaly(session, config, event_type, actual=200.0)
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

    digests: list[list[str]] = []
    _result, per_delivery = _run_flush(monkeypatch, sync_session_factory, digests)

    assert len(per_delivery) == 1
    assert digests == []


def test_several_deliveries_of_one_rule_are_not_combined(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routing counts RULES, not delivery rows.

    `_delivery_chunks` already splits one rule's matches into several deliveries
    for a channel with a per-message item cap. Bundling those back together
    would stack two "N alerts" banners for the same rule in one message and
    re-do the split the chunking exists to avoid.
    """
    with sync_session_factory() as session:
        config, destination, rule, event_type = _seed(
            session,
            cron=_ALWAYS_DUE,
            last_flushed_at=datetime.now(UTC) - timedelta(hours=2),
        )
        _fire_anomaly(session, config, event_type, actual=200.0)
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

    # A second buffered row for the SAME rule, as chunking would produce.
    with sync_session_factory() as session:
        first = session.execute(select(AlertPendingItem)).scalars().one()
        session.add(
            AlertPendingItem(
                id=uuid.uuid4(),
                project_id=first.project_id,
                destination_id=first.destination_id,
                rule_id=first.rule_id,
                scan_config_id=first.scan_config_id,
                scope_type="event_type",
                scope_ref=str(uuid.uuid4()),
                scope_name="Another scope",
                bucket=first.bucket,
                direction="drop",
                actual_count=1.0,
                expected_count=50.0,
                correlation_group_id=uuid.uuid4(),
            )
        )
        session.commit()

    digests: list[list[str]] = []
    _result, per_delivery = _run_flush(monkeypatch, sync_session_factory, digests)

    assert digests == [], "one rule must never take the combined path"
    assert len(per_delivery) >= 1


def test_the_combined_send_posts_once_and_marks_every_member_sent(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config, destination, rule_a, event_type = _seed(
            session,
            cron=_ALWAYS_DUE,
            last_flushed_at=datetime.now(UTC) - timedelta(hours=2),
        )
        _add_rule(session, destination, "Second monitor")
        _fire_anomaly(session, config, event_type, actual=200.0)
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

    digests: list[list[str]] = []
    _run_flush(monkeypatch, sync_session_factory, digests)
    assert len(digests) == 1

    result, posts = _run_digest(monkeypatch, sync_session_factory, digests[0])

    assert result["messages"] == 1, "one destination, one outbound call"
    assert result["sent"] == 2
    # Both rules are in the one body.
    assert len(posts) == 1
    body = posts[0][0]
    assert rule_a.name in body or "Page" in body
    assert body.count("Page") >= 2, "both rules' sections are present"

    with sync_session_factory() as session:
        deliveries = session.execute(select(AlertDelivery)).scalars().all()
        assert len(deliveries) == 2
        assert {d.status for d in deliveries} == {"sent"}
        # One transaction, so they share an instant — a crash mid-loop cannot
        # leave half of them pending for the reaper to send a second time.
        assert len({d.sent_at for d in deliveries}) == 1
        assert all(d.error_message is None for d in deliveries)


def test_a_failed_combined_send_marks_every_member_failed(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nobody got the message, so no member may claim it was delivered."""
    with sync_session_factory() as session:
        config, destination, _rule_a, event_type = _seed(
            session,
            cron=_ALWAYS_DUE,
            last_flushed_at=datetime.now(UTC) - timedelta(hours=2),
        )
        _add_rule(session, destination, "Second monitor")
        _fire_anomaly(session, config, event_type, actual=200.0)
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

    digests: list[list[str]] = []
    _run_flush(monkeypatch, sync_session_factory, digests)

    result, posts = _run_digest(
        monkeypatch,
        sync_session_factory,
        digests[0],
        fail_with=RuntimeError("slack said no"),
    )

    assert posts == []
    assert result["sent"] == 0
    assert result["failed"] == 2

    with sync_session_factory() as session:
        deliveries = session.execute(select(AlertDelivery)).scalars().all()
        assert {d.status for d in deliveries} == {"failed"}
        # Each row carries the real cause, so the Inbox Retry button — which
        # only accepts `failed` — reaches every one of them.
        assert all("slack said no" in (d.error_message or "") for d in deliveries)


def test_a_member_already_sent_is_never_sent_again(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """acks-late can re-queue this task after a successful send."""
    with sync_session_factory() as session:
        config, destination, _rule_a, event_type = _seed(
            session,
            cron=_ALWAYS_DUE,
            last_flushed_at=datetime.now(UTC) - timedelta(hours=2),
        )
        _add_rule(session, destination, "Second monitor")
        _fire_anomaly(session, config, event_type, actual=200.0)
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

    digests: list[list[str]] = []
    _run_flush(monkeypatch, sync_session_factory, digests)

    first_result, first_posts = _run_digest(monkeypatch, sync_session_factory, digests[0])
    assert first_result["sent"] == 2
    assert len(first_posts) == 1

    second_result, second_posts = _run_digest(monkeypatch, sync_session_factory, digests[0])

    assert second_result["status"] == "already_sent"
    assert second_posts == [], "a re-run must not put the message in the channel twice"


def test_a_held_alert_is_visible_from_outside_the_database(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """`alerts_queued: 0` cannot distinguish "held 12" from "nothing matched".

    On a cadence that is the difference between the feature working and it
    swallowing every alert for a whole window — which is exactly what could not
    be confirmed when this shipped to production. The collection
    now reports what it buffered.
    """
    with sync_session_factory() as session:
        config, _destination, _rule, event_type = _seed(session, cron=_DAILY)
        _fire_anomaly(session, config, event_type, actual=200.0)

        buffered: list[int] = []
        delivery_ids = metrics_dispatch._prepare_alert_deliveries(
            session, config, scan_job_id=None, buffered=buffered
        )
        session.commit()

        assert delivery_ids == [], "a scheduled destination mints no delivery"
        assert sum(buffered) == 1, "...but it must SAY that it held one"
        assert len(session.execute(select(AlertPendingItem)).scalars().all()) == 1


def test_an_immediate_destination_reports_nothing_held(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, _destination, _rule, event_type = _seed(session, cron=None)
        _fire_anomaly(session, config, event_type, actual=200.0)

        buffered: list[int] = []
        delivery_ids = metrics_dispatch._prepare_alert_deliveries(
            session, config, scan_job_id=None, buffered=buffered
        )
        session.commit()

        assert len(delivery_ids) == 1
        assert sum(buffered) == 0


def test_a_digest_delivery_is_not_split_by_the_item_cap(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telegram caps a delivery at 8 items — an estimate sized for the verbose
    layout, where a digest line is about a seventh as long. It split a
    perfectly deliverable digest into three messages."""
    with sync_session_factory() as session:
        config, destination, rule, event_type = _seed(
            session,
            cron=_ALWAYS_DUE,
            last_flushed_at=datetime.now(UTC) - timedelta(hours=2),
        )
        destination.type = "telegram"
        session.commit()
        # Twelve buffered scopes for one rule: over the 8-item cap.
        for index in range(12):
            session.add(
                AlertPendingItem(
                    id=uuid.uuid4(),
                    project_id=config.project_id,
                    destination_id=destination.id,
                    rule_id=rule.id,
                    scan_config_id=config.id,
                    scope_type="event_type",
                    scope_ref=str(uuid.uuid4()),
                    scope_name=f"scope {index}",
                    bucket=_BUCKET,
                    direction="spike",
                    actual_count=200.0,
                    expected_count=10.0,
                    correlation_group_id=uuid.uuid4(),
                )
            )
        session.commit()

    _run_flush(monkeypatch, sync_session_factory, [])

    with sync_session_factory() as session:
        deliveries = session.execute(select(AlertDelivery)).scalars().all()
        assert len(deliveries) == 1, "one digest is ONE delivery, not two"
        assert deliveries[0].matched_count == 12
        assert deliveries[0].payload_snapshot.get("digest") is True, (
            "the send path has no other way to know it is rendering a digest"
        )


def test_the_combined_send_uses_the_digest_layout(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Slack digest is a digest, and the readability work has to reach it.

    Two send paths carry a digest: `send_alert_delivery` for Telegram and this
    one for Slack and email. Only the first read the `digest` flag, so a Slack
    destination on a cadence kept receiving the verbose per-item layout the
    compact one replaced — the same buffered rows, rendered two different ways
    depending on which channel they happened to be routed to.
    """
    with sync_session_factory() as session:
        config, destination, _rule_a, event_type = _seed(
            session,
            cron=_ALWAYS_DUE,
            last_flushed_at=datetime.now(UTC) - timedelta(hours=2),
        )
        _add_rule(session, destination, "Second monitor")
        _fire_anomaly(session, config, event_type, actual=200.0)
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

    digests: list[list[str]] = []
    _run_flush(monkeypatch, sync_session_factory, digests)

    _result, posts = _run_digest(monkeypatch, sync_session_factory, digests[0])

    body = posts[0][0]
    # The compact line leads with a direction arrow; the verbose one opens
    # "- Event type X: up, actual=..." and carries no arrow anywhere.
    assert "▲" in body or "▼" in body
    assert "actual=" not in body, "the verbose per-item layout is still being rendered"
