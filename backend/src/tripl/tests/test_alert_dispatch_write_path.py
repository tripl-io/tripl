"""Seven defects around the alert write path.

the alert link base URL is resolved once, not once per link.
every scope label fits the column it is stored in.
the incident handle is documented as the thing it is.
loading a scan config does not load its whole scan history.
a metric scope's STATE and buffered rows store no scan config.
and its incident HANDLE hashes no scan config either.
the digest buffer holds one line per INCIDENT, and says so.

The last three carry their write-ups at their own section headers below rather
than here, because each pins a single connected argument and reading it beside
the tests is worth more than reading it four hundred lines above them.

## The alert link base URL

``app_base_url`` used to be read back out of the database inside every URL
builder in ``worker/tasks/metrics/urls.py``. ``get_runtime_config_sync`` has no
cache, and called with no session it checks out a SECOND pooled connection and
runs two ``app_settings`` SELECTs. ``_build_item_paths`` runs twice per alert
item — once for the typed ``AlertDeliveryItem`` row, once for the frozen
``payload_snapshot`` — so an ordinary item cost three of those reads and a
24-item digest cost 48-72, all of them taken while ``alert_flush._build_digest``
held FOR UPDATE locks on the whole buffer and the flush advisory lock on the
first connection.

Two properties are pinned below, because the fix has two halves:

* COUNT. ``dispatch._create_deliveries`` reads the runtime config exactly once,
  on the session it was handed, however many items the delivery carries.
* AGREEMENT. The read is fallible by design — ``get_runtime_config_sync``
  swallows every exception and falls back to the env config, whose
  ``app_base_url`` defaults to ``""`` — so independent reads let one delivery
  disagree with itself about the same item's link. This is the same class of
  defect ``alert_payload._build_delivery_snapshot`` already carries a long
  comment about for ``percent_delta``; here it is the links.

## Scope label width

``scope_name`` is VARCHAR(255) on ``alert_delivery_items``,
``alert_pending_items`` and ``anomaly_scope_overrides``, and its sources are
wider: ``Event.name`` is String(500), and the drift scopes append ``.{field}``
to an event or event-type name, so they overflow even when the name alone does
not. An alert for an event named over 255 characters failed its INSERT on
Postgres, and with no savepoint around ``_prepare_alert_deliveries`` that rolled
back the whole collection — anomaly recalculation, cooldown state and every
delivery for every scope of that scan config — on every tick, for as long as the
anomaly stayed active.

The Postgres ``DataError`` itself is unreachable here: the suite runs on SQLite,
which ignores ``VARCHAR(n)`` entirely. What is pinned instead is the invariant
behind it — what lands in the row fits the column — the way
``test_schema_drift_upsert.py`` pins the same shape one table over. Every test below also
asserts that its fixture genuinely overflows, so none of them can quietly stop
exercising the bug if someone shortens a name.

## The incident handle

``AlertDeliveryItem.correlation_group_id`` was documented as a per-delivery
co-firing tag, NULL when an item fired alone. It is neither: the id is a stable
per-incident handle, it is written on every item, and the whole alert inbox is
built on it. Code written from the old comment would label every alert
"correlated" and group unrelated deliveries together.

A comment fix has no runtime edge to catch, so one guard below asserts the two
replaced sentences are gone and that the block NAMES the owner of the key
rather than restating it — restating it is how it went stale in the first
place. What keeps that guard from being a spell-check is the three tests beside
it, which pin every claim the new text makes: the key carries no bucket and
splits per scope, the mint is unconditional, and a delivery carrying exactly
one item still gets a handle the inbox can see.

## Loading a scan config

``ScanConfig.scan_jobs`` was declared ``lazy="selectin"``, so loading a
ScanConfig ENTITY — as opposed to its columns — pulled in every scan job that
config had ever run, each with a JSON ``result_summary`` and an
``error_message``. Nothing reads the collection; it exists for the ORM delete
cascade. The alert inbox is the caller this issue was filed against
(``_INBOX_GROUP_SELECT`` selects the entity for its ``name``, at four call
sites), but the scheduler pays it on every beat tick for every scheduled
config, and so do ``alert_flush``, the Scans tab and search.

What makes it a defect rather than a waste is that the collection is
UNBOUNDED: a row per collection, pruned only for demo projects, so the cost
grows with deployment age x cadence forever.

The two halves are pinned separately, because a fix for one can break the
other. The eager load is gone (no ``scan_jobs`` round trip on either the plain
entity load or the real inbox query), AND the cascade still works — asserted
on a factory with FK enforcement OFF, so the ORM's own child DELETE is the
only thing that can empty the table. A source scan stands behind the "nothing
reads it" premise, since a lazy load on the async path does not merely cost a
query, it raises.

No network, and the DB is a throwaway sqlite file.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Engine, create_engine, delete, event, select, text
from sqlalchemy.orm import Session, sessionmaker

import tripl
from tripl.alerting_matching import (
    SCOPE_DISTRIBUTION_DRIFT,
    SCOPE_RELEASE_REGRESSION,
    SCOPE_VARIABLE_VALUE_DRIFT,
    DriftAlertCandidate,
)
from tripl.core.analyzers import _event_generator_merge, _event_generator_merge_refs
from tripl.core.analyzers._event_generator_merge_refs import move_dangling_event_references
from tripl.core.analyzers.anomaly_detector import SCOPE_EVENT
from tripl.core.bucketing import to_utc
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import SCOPE_NAME_MAX_LEN, AlertDeliveryItem, trim_scope_name
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_pending_item import AlertPendingItem
from tripl.models.alert_rule import AlertRule
from tripl.models.anomaly_scope_override import AnomalyScopeOverride
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import MetricScopeType
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob
from tripl.services import app_settings_service
from tripl.services._alerting_deliveries import _INBOX_GROUP_SELECT
from tripl.services.app_settings_service import RuntimeConfig
from tripl.worker.tasks.metrics import dispatch as metrics_dispatch
from tripl.worker.tasks.metrics import urls as metrics_urls
from tripl.worker.tasks.metrics._helpers import SCOPE_SCHEMA_DRIFT
from tripl.worker.tasks.metrics.alert_payload import _build_alert_scope_names
from tripl.worker.tasks.metrics.urls import _build_item_paths

# The base URL the caller hands in.
GIVEN = "https://given.example"
# The base URL a settings read would hand back. Deliberately different from
# GIVEN so "used the value it was given" and "read the value itself" cannot
# both be true of the same URL.
STALE = "https://stale.example"
SLUG = "acme-ios"

# Hour-aligned UTC bucket matching persisted values.
_BUCKET = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)


class _ConfigReads:
    """Stand-in for ``get_runtime_config_sync`` that records how it was called.

    ``vary=True`` hands back a DIFFERENT ``app_base_url`` on every call. That is
    not a contrived failure mode: the real helper falls back to the env config
    on any read error, so two calls a microsecond apart genuinely can return
    different values, and nothing but a ``logger.warning`` records it.
    """

    def __init__(self, *, base: str = GIVEN, vary: bool = False) -> None:
        self.sessions: list[Session | None] = []
        self._base = base
        self._vary = vary

    def __call__(self, session: Session | None = None) -> RuntimeConfig:
        self.sessions.append(session)
        base = f"https://read-{len(self.sessions)}.example" if self._vary else self._base
        return RuntimeConfig(
            app_base_url=base,
            scan_row_limit_default=1000,
            metrics_row_limit_default=1000,
        )

    @property
    def count(self) -> int:
        return len(self.sessions)


# --------------------------------------------------------------------------
# The builders: handed a base URL, and unable to reach for another one
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope_type", "correlation_group_id", "expected_links"),
    [
        # The incident arm. Every modern item takes it — ``_prepare_alert_deliveries``
        # mints a correlation id for every anomaly — so this is the one read per
        # item that the typed-row call used to pay for.
        ("event", uuid.UUID(int=1), 1),
        # The release-regression arm of the snapshot call, which passes no
        # correlation id: one audit URL, one read.
        ("release_regression", None, 1),
        # The event/monitoring pair: TWO builders, so two reads per item before
        # the fix, and the reason an ordinary scope cost three in total.
        ("event", None, 2),
    ],
)
def test_the_url_builders_use_the_base_they_are_given_and_read_no_settings(
    monkeypatch: pytest.MonkeyPatch,
    scope_type: str,
    correlation_group_id: uuid.UUID | None,
    expected_links: int,
) -> None:
    """Every arm of ``_build_item_paths``, against a settings read that would lie.

    Reverting the fix fails this on the signature alone (``app_base_url`` would
    be an unexpected keyword), but the assertions are written so a PARTIAL
    revert — keeping the parameter and re-reading the config anyway — fails too:
    the stub returns ``STALE``, and every link here has to carry ``GIVEN``.
    """
    reads = _ConfigReads(base=STALE)
    monkeypatch.setattr(app_settings_service, "get_runtime_config_sync", reads)

    paths = _build_item_paths(
        SLUG,
        org_slug="default",
        app_base_url=GIVEN,
        scope_type=scope_type,
        scope_ref=str(uuid.uuid4()),
        event_id=uuid.uuid4(),
        delivery_id=uuid.uuid4(),
        correlation_group_id=correlation_group_id,
    )

    links = [path for path in paths if path is not None]
    assert len(links) == expected_links
    for link in links:
        assert link.startswith(GIVEN)
        assert STALE not in link

    # The point is not that it read the right value. It is that it read nothing.
    assert reads.count == 0
    # And that it cannot: the module no longer imports the settings service at
    # all. This survives a revert that reaches for the config under a different
    # alias, which the call-count assertion above would not catch.
    assert not hasattr(metrics_urls, "app_settings_service")


def test_an_unconfigured_base_url_still_emits_no_link_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parity at the empty string, which is what an unconfigured instance has.

    ``RuntimeConfig.app_base_url`` is a non-optional ``str`` and ``config.py``
    defaults it to ``""``, so "no base URL configured" reaches these builders as
    an empty string rather than ``None``. Each one still guards for it, and the
    guard is what makes the threaded value behaviour-preserving rather than a
    quiet change to what an unconfigured instance sends.
    """
    reads = _ConfigReads(base=STALE)
    monkeypatch.setattr(app_settings_service, "get_runtime_config_sync", reads)

    for scope_type, group_id in (
        ("event", None),
        ("event", uuid.uuid4()),
        ("release_regression", None),
    ):
        assert _build_item_paths(
            SLUG,
            org_slug="default",
            app_base_url="",
            scope_type=scope_type,
            scope_ref=str(uuid.uuid4()),
            event_id=uuid.uuid4(),
            delivery_id=uuid.uuid4(),
            correlation_group_id=group_id,
        ) == (None, None)

    # Not "fell back to the stub's base": there is no fallback left to take.
    assert reads.count == 0


# --------------------------------------------------------------------------
# The delivery: one read, on the caller's own session
# --------------------------------------------------------------------------


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_dispatch.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _seed(session: Session) -> tuple[ScanConfig, AlertDestination, AlertRule]:
    """One project, one scan, one immediate Slack destination, one rule."""
    project = Project(
        id=uuid.uuid4(),
        name="Dispatch",
        slug=SLUG,
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
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=project.id,
        # Slack rather than Telegram on purpose: ``_MAX_ITEMS_PER_DELIVERY``
        # caps only Telegram at 8, so every item below lands in ONE delivery and
        # the read count cannot be confounded with the chunk count.
        type="slack",
        name="Main Slack",
        enabled=True,
        webhook_url_encrypted="secret",
        delivery_schedule_cron=None,
    )
    rule = AlertRule(
        id=uuid.uuid4(),
        destination_id=destination.id,
        name="Everything",
        enabled=True,
        include_project_total=False,
        include_event_types=True,
        include_events=True,
        notify_on_spike=True,
        notify_on_drop=True,
        min_percent_delta=0,
        min_absolute_delta=0,
        min_expected_count=0,
        cooldown_minutes=1440,
    )
    # Parents first, with a flush between: ``fk_session_factory`` turns SQLite's
    # foreign keys ON, and a single ``add_all`` leaves the insert order to the
    # unit of work, which sent the destination to the database ahead of its
    # project. The other fixture does not enforce keys, so this ordering was
    # invisible until the metric-scope tests started using the strict one.
    session.add_all([project, data_source])
    session.flush()
    session.add_all([config, destination, rule])
    session.commit()
    return config, destination, rule


def _candidates(config: ScanConfig, *, count: int) -> list[DriftAlertCandidate]:
    """``count`` event-scoped candidates, the scope that costs three reads each."""
    return [
        DriftAlertCandidate(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            scope_type="event",
            scope_ref=str(uuid.uuid4()),
            event_id=uuid.uuid4(),
            event_type_id=None,
            bucket=_BUCKET,
            direction="spike",
            actual_count=200.0,
            expected_count=100.0,
            drift_field=None,
            drift_type=None,
            sample_value=None,
        )
        for _ in range(count)
    ]


def _mint(
    session: Session,
    config: ScanConfig,
    destination: AlertDestination,
    rule: AlertRule,
    candidates: list[DriftAlertCandidate],
) -> list[uuid.UUID]:
    return metrics_dispatch._create_deliveries(
        session,
        config,
        project_slug=SLUG,
        rule=rule,
        destination=destination,
        anomalies=list(candidates),
        scope_names={
            (candidate.scope_type, candidate.scope_ref): f"scope-{index}"
            for index, candidate in enumerate(candidates)
        },
        # Every anomaly carries one on the modern path, which is
        # what sends the typed row down the audit-URL branch.
        correlation_by_anomaly={id(candidate): uuid.uuid4() for candidate in candidates},
        scan_job_id=None,
    )


def test_one_settings_read_per_delivery_whatever_the_item_count(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six items, one read — and the read happens on the caller's session.

    Both halves catch a revert on their own:

    * ``reads.count == 1`` was 18 before the fix (six items x three reads: one
      for the typed row's audit link, two for the snapshot's event/monitoring
      pair). Pinned as a constant rather than a bound, because a change that
      merely halves it has not fixed anything.
    * ``reads.sessions == [session]`` is the half that keeps the pool safe.
      ``get_runtime_config_sync()`` called with NO session is the arm that opens
      a second pooled connection, and on the digest path it did that while
      ``_build_digest`` held FOR UPDATE locks on the buffer and the flush
      advisory lock on the first one. Reverting to the per-link reads records
      ``None`` here, not a Session, so this fails even if the count somehow did
      not.
    """
    reads = _ConfigReads()
    monkeypatch.setattr(app_settings_service, "get_runtime_config_sync", reads)

    with sync_session_factory() as session:
        config, destination, rule = _seed(session)
        delivery_ids = _mint(session, config, destination, rule, _candidates(config, count=6))
        session.flush()

        assert len(delivery_ids) == 1
        assert reads.count == 1
        assert reads.sessions == [session]


def test_the_typed_items_and_the_frozen_snapshot_cannot_disagree_about_a_link(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One delivery, one base URL — even when every read would answer differently.

    This is the correctness half, and it is not hypothetical: the real helper
    catches every exception and falls back to the env config, where
    ``app_base_url`` defaults to ``""``. A single failed read therefore used to
    give an item's typed row a link and its own snapshot entry none, inside one
    frozen audit record. ``_build_delivery_snapshot`` already carries a comment
    about exactly this shape of self-disagreement for ``percent_delta``.

    With the fix, ``_create_deliveries`` reads once and hands the same string to
    both encodings, so the varying stub below can only ever be observed once.
    Reverting the fix takes six reads for these two items and the assertion sees
    four or five distinct bases.
    """
    reads = _ConfigReads(vary=True)
    monkeypatch.setattr(app_settings_service, "get_runtime_config_sync", reads)

    with sync_session_factory() as session:
        config, destination, rule = _seed(session)
        (delivery_id,) = _mint(session, config, destination, rule, _candidates(config, count=2))
        session.flush()

        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery is not None
        snapshot = delivery.payload_snapshot
        assert isinstance(snapshot, dict)
        snapshot_items = snapshot["items"]
        assert isinstance(snapshot_items, list)
        assert len(snapshot_items) == 2

        rows = (
            session.execute(
                select(AlertDeliveryItem).where(AlertDeliveryItem.delivery_id == delivery_id)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2

        links = [row.details_path for row in rows]
        links += [item["details_path"] for item in snapshot_items]
        links += [item["monitoring_path"] for item in snapshot_items]
        present = [link for link in links if link]
        # Six links in total: two typed audit URLs, and the snapshot's
        # event-details + monitoring pair for each of the two items.
        assert len(present) == 6

        bases = {link.split("/o/")[0] for link in present}
        # One value, and specifically the value of the FIRST read — so a revert
        # that reads again later cannot pass by coincidence.
        assert bases == {"https://read-1.example"}


# --------------------------------------------------------------------------
# the label an alert carries fits the column that stores it
# --------------------------------------------------------------------------

# 418 characters: over the 255-wide column, under the 500 ``schemas/event.py``
# admits and under the 500 ``event_plan.truncate_event_name`` cuts the
# generator's own names at. Both bounds are asserted below rather than trusted.
_LONG_EVENT_NAME = "Checkout: " + "a very long descriptive segment / " * 12
# 250 characters, which fits ``EventType.display_name`` (String(255)) on its
# own. The drift scopes are what push it over: they render
# ``f"{display_name}.{drift_field}"``, so a legal event-type name plus a legal
# field name overflows a column neither of them could overflow alone.
_LONG_EVENT_TYPE_NAME = "Screen " + "x" * 243

_NOW = datetime(2026, 9, 14, 9, 30, tzinfo=UTC)


def _seed_long_named_event(session: Session, *, project_id: uuid.UUID) -> tuple[EventType, Event]:
    """An event type and an event whose names are both longer than the column."""
    event_type = EventType(
        id=uuid.uuid4(),
        project_id=project_id,
        name=f"screen-{uuid.uuid4().hex[:8]}",
        display_name=_LONG_EVENT_TYPE_NAME,
    )
    session.add(event_type)
    session.flush()
    event = Event(
        id=uuid.uuid4(),
        project_id=project_id,
        event_type_id=event_type.id,
        name=_LONG_EVENT_NAME,
    )
    session.add(event)
    session.commit()
    return event_type, event


def _drift_candidate(
    config: ScanConfig,
    *,
    scope_type: str,
    scope_ref: str,
    event_id: uuid.UUID | None = None,
    event_type_id: uuid.UUID | None = None,
    drift_field: str | None = None,
) -> DriftAlertCandidate:
    return DriftAlertCandidate(
        id=uuid.uuid4(),
        scan_config_id=config.id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        event_id=event_id,
        event_type_id=event_type_id,
        bucket=_BUCKET,
        direction="spike",
        actual_count=200.0,
        expected_count=100.0,
        drift_field=drift_field,
        drift_type=None,
        sample_value=None,
    )


def _long_name_candidates(
    config: ScanConfig, event_type: EventType, event: Event
) -> list[DriftAlertCandidate]:
    """One candidate per scope family that derives its label from a wide name.

    Event-anchored candidates carry ``event_type_id=None`` deliberately — that
    is the shape the detectors actually write, and the reason
    ``_build_event_type_by_event_id`` exists.
    """
    return [
        _drift_candidate(
            config, scope_type=SCOPE_EVENT, scope_ref=str(event.id), event_id=event.id
        ),
        _drift_candidate(
            config,
            scope_type=SCOPE_VARIABLE_VALUE_DRIFT,
            scope_ref=str(uuid.uuid4()),
            event_id=event.id,
            drift_field="country",
        ),
        _drift_candidate(
            config,
            scope_type=SCOPE_SCHEMA_DRIFT,
            scope_ref=str(uuid.uuid4()),
            event_type_id=event_type.id,
            drift_field="user_agent",
        ),
        _drift_candidate(
            config,
            scope_type=SCOPE_DISTRIBUTION_DRIFT,
            scope_ref=str(uuid.uuid4()),
            event_type_id=event_type.id,
            drift_field="locale",
        ),
        _drift_candidate(
            config,
            scope_type=SCOPE_RELEASE_REGRESSION,
            scope_ref=str(event.id),
            event_id=event.id,
        ),
    ]


def test_the_three_scope_name_columns_still_share_one_width() -> None:
    """One constant, three tables — and one call site that depends on the tie.

    ``_alerting_deliveries._record_false_positive_ratchet`` copies
    ``AlertDeliveryItem.scope_name`` straight into
    ``AnomalyScopeOverride.scope_name`` with no trim of its own, and that is
    safe only while the two columns are the same width. Narrowing either one
    without the other reopens the same overflow on the false-positive button,
    which is why this is asserted rather than left as a comment.
    """
    widths = {
        model.__tablename__: cast(Any, model.__table__.c.scope_name.type).length
        for model in (AlertDeliveryItem, AlertPendingItem, AnomalyScopeOverride)
    }
    assert widths == {
        "alert_delivery_items": SCOPE_NAME_MAX_LEN,
        "alert_pending_items": SCOPE_NAME_MAX_LEN,
        "anomaly_scope_overrides": SCOPE_NAME_MAX_LEN,
    }


def test_a_label_that_fits_is_returned_untouched_and_a_long_one_is_marked() -> None:
    """The two ends of ``trim_scope_name``, including the exact boundary.

    ``<=`` rather than ``<`` is the whole difference between a label that fits
    and one the database rejects, so a name of exactly the column width has to
    come back identical, with no ellipsis and nothing dropped.
    """
    exact = "e" * SCOPE_NAME_MAX_LEN
    assert trim_scope_name(exact) is exact
    assert trim_scope_name("Checkout") == "Checkout"

    trimmed = trim_scope_name(_LONG_EVENT_NAME)
    assert len(trimmed) == SCOPE_NAME_MAX_LEN
    assert trimmed.endswith("...")
    # Lossy, but not misleading: what survives is the real beginning of the
    # real name, so an operator still recognises what fired.
    assert _LONG_EVENT_NAME.startswith(trimmed[:-3])


def test_every_label_the_dispatcher_builds_fits_the_column(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """``_build_alert_scope_names`` is the choke point, so it is what is pinned.

    Reverting the trim at the bottom of that function fails this on the event
    scope alone — SQLite stores all 418 characters happily, so the length
    assertion is the only thing standing between the fixture and the Postgres
    ``DataError`` it stands in for. The two drift families are here because
    they overflow from inputs that individually fit, and the release
    regression is here because it borrows a label rather than building one:
    trimming at the wrong point in the function would leave it holding a bare
    UUID instead.
    """
    with sync_session_factory() as session:
        config, _destination, _rule = _seed(session)
        event_type, event = _seed_long_named_event(session, project_id=config.project_id)
        candidates = _long_name_candidates(config, event_type, event)

        # Fixture guards: the inputs really are what the issue describes, and
        # really are values the product can produce.
        assert SCOPE_NAME_MAX_LEN < len(event.name) <= 500
        assert len(event_type.display_name) <= 255
        assert len(f"{event_type.display_name}.user_agent") > SCOPE_NAME_MAX_LEN

        names = _build_alert_scope_names(session, list(candidates))

        # Every candidate is labelled, plus the event TYPE the drift candidates
        # resolve through — which an event-type-scoped alert stores under its
        # own key, so it is covered by the same sweep rather than exempt.
        assert set(names) == {
            (candidate.scope_type, candidate.scope_ref) for candidate in candidates
        } | {("event_type", str(event_type.id))}
        for key, value in names.items():
            assert len(value) <= SCOPE_NAME_MAX_LEN, key

        event_label = names[(SCOPE_EVENT, str(event.id))]
        assert len(event_label) == SCOPE_NAME_MAX_LEN
        assert event.name.startswith(event_label[:-3])
        # The borrow still resolves to the event's own label, not to the
        # ``scope_ref`` fallback.
        assert names[(SCOPE_RELEASE_REGRESSION, str(event.id))] == event_label

        for candidate in candidates:
            if candidate.drift_field is None:
                continue
            label = names[(candidate.scope_type, candidate.scope_ref)]
            assert len(label) == SCOPE_NAME_MAX_LEN, candidate.scope_type


def test_a_long_named_event_still_writes_delivery_and_buffer_rows_that_fit(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both persisted writers, from the same labels, in one transaction.

    ``dispatch.py`` carries no guard of its own — that is the point of trimming
    at the source — so this is what proves the source covers both of its
    consumers: the typed ``AlertDeliveryItem`` rows minted immediately, and the
    ``AlertPendingItem`` buffer a digest destination writes instead. The
    buffer's ON CONFLICT arm rewrites ``scope_name`` from ``excluded`` on every
    collection, so on the digest path the untrimmed value used to re-raise on
    every tick, not only the first.

    ``== SCOPE_NAME_MAX_LEN`` rather than ``<=``: at least one row must sit
    exactly on the boundary, which is true only if something trimmed it. A
    revert stores 418 characters and fails here.
    """
    monkeypatch.setattr(app_settings_service, "get_runtime_config_sync", _ConfigReads())

    with sync_session_factory() as session:
        config, destination, rule = _seed(session)
        event_type, event = _seed_long_named_event(session, project_id=config.project_id)
        candidates = _long_name_candidates(config, event_type, event)
        scope_names = _build_alert_scope_names(session, list(candidates))
        correlation = {id(candidate): uuid.uuid4() for candidate in candidates}

        (delivery_id,) = metrics_dispatch._create_deliveries(
            session,
            config,
            project_slug=SLUG,
            rule=rule,
            destination=destination,
            anomalies=list(candidates),
            scope_names=scope_names,
            correlation_by_anomaly=correlation,
            scan_job_id=None,
        )
        metrics_dispatch._buffer_pending_items(
            session,
            config,
            rule=rule,
            destination=destination,
            anomalies=list(candidates),
            scope_names=scope_names,
            correlation_by_anomaly=correlation,
            scan_job_id=None,
            now=_NOW,
        )
        session.flush()

        items = (
            session.execute(
                select(AlertDeliveryItem).where(AlertDeliveryItem.delivery_id == delivery_id)
            )
            .scalars()
            .all()
        )
        pending = session.execute(select(AlertPendingItem)).scalars().all()
        assert len(items) == len(candidates)
        assert len(pending) == len(candidates)

        for row in (*items, *pending):
            assert len(row.scope_name) <= SCOPE_NAME_MAX_LEN, row.scope_type
        assert max(len(row.scope_name) for row in items) == SCOPE_NAME_MAX_LEN
        assert max(len(row.scope_name) for row in pending) == SCOPE_NAME_MAX_LEN

        # The frozen snapshot quotes the same dict, so it cannot disagree with
        # the rows beside it about what the scope was called.
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery is not None
        snapshot = delivery.payload_snapshot
        assert isinstance(snapshot, dict)
        snapshot_items = snapshot["items"]
        assert isinstance(snapshot_items, list)
        by_ref = {row.scope_ref: row.scope_name for row in items}
        for entry in snapshot_items:
            assert entry["scope_name"] == by_ref[entry["scope_ref"]]


def _add_override(
    session: Session,
    *,
    project_id: uuid.UUID,
    scan_config_id: uuid.UUID,
    scope_ref: str,
) -> AnomalyScopeOverride:
    row = AnomalyScopeOverride(
        id=uuid.uuid4(),
        project_id=project_id,
        scan_config_id=scan_config_id,
        scope_type=MetricScopeType.event.value,
        scope_ref=scope_ref,
        scope_name="Checkout step 1",
        sigma_threshold=5.0,
        min_expected_count=30,
        false_positive_count=1,
    )
    session.add(row)
    session.flush()
    return row


def test_merging_into_a_long_named_group_relabels_within_the_column(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Both arms of ``_move_anomaly_scope_overrides``, against a 418-char survivor.

    The issue named only the relabel-in-place arm. The fold arm four lines
    below it writes the same ``target.name`` into the same column and is just
    as reachable — an event tuned on two scan configs where the survivor was
    already tuned on one of them takes it — so both are pinned here, and
    reverting either one fails on the row it wrote.

    This module may not raise: it runs inside ``apply_event_groups`` and
    ``run_scan``, whose handlers mark the whole ``ScanJob`` failed. An override
    that could not be relabelled would cost a scan.
    """
    with sync_session_factory() as session:
        config, _destination, _rule = _seed(session)
        event_type, target = _seed_long_named_event(session, project_id=config.project_id)
        source = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=event_type.id,
            name="Checkout step 1",
        )
        session.add(source)
        session.flush()

        second_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=config.data_source_id,
            project_id=config.project_id,
            name="Scan 2",
            base_query="SELECT time, event_name FROM events",
            time_column="time",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add(second_config)
        session.flush()

        # Arm one: nothing tuned on the survivor for this config, so the
        # source's own row is re-pointed and relabelled in place.
        relabelled = _add_override(
            session,
            project_id=config.project_id,
            scan_config_id=config.id,
            scope_ref=str(source.id),
        )
        # Arm two: both events tuned on the second config, so the two rows fold
        # and the survivor's row takes the new label.
        _add_override(
            session,
            project_id=config.project_id,
            scan_config_id=second_config.id,
            scope_ref=str(source.id),
        )
        folded = _add_override(
            session,
            project_id=config.project_id,
            scan_config_id=second_config.id,
            scope_ref=str(target.id),
        )
        session.commit()

        assert len(target.name) > SCOPE_NAME_MAX_LEN, "the survivor must overflow the column"

        move_dangling_event_references(session, source=source, target=target)
        session.flush()

        # Both arms ran: one row moved onto the survivor, the other folded away.
        assert relabelled.scope_ref == str(target.id)
        assert (
            session.execute(
                select(AnomalyScopeOverride).where(AnomalyScopeOverride.scope_ref == str(source.id))
            )
            .scalars()
            .all()
            == []
        )
        for row in (relabelled, folded):
            assert len(row.scope_name) == SCOPE_NAME_MAX_LEN
            assert target.name.startswith(row.scope_name[:-3])


# The merge's third writer is a Core ``UPDATE`` inside ``_merge_event_into_group``
# with no seam a unit test can reach without standing up the whole grouping
# pipeline. It is pinned by source instead, the way ``test_metric_scope_purge.py`` pins
# its ordering rule: cheaper than the fixture, and unlike the fixture it also
# catches a writer added later. The counts are asserted so the scan cannot
# silently stop finding anything.
_SCOPE_NAME_WRITE_COUNTS = {
    # ``.values(... scope_name=...)`` on the AlertDeliveryItem UPDATE.
    _event_generator_merge: 1,
    # ``row.scope_name =`` and ``prior.scope_name =``, the two override arms.
    _event_generator_merge_refs: 2,
}


def test_no_event_merge_stores_a_scope_label_it_has_not_trimmed() -> None:
    for module, expected in _SCOPE_NAME_WRITE_COUNTS.items():
        writes: list[tuple[int, ast.expr]] = []
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.keyword) and node.arg == "scope_name":
                writes.append((node.value.lineno, node.value))
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Attribute) and target.attr == "scope_name"
                for target in node.targets
            ):
                writes.append((node.lineno, node.value))

        assert len(writes) == expected, f"{module.__name__} writes {len(writes)} scope_name values"
        for lineno, value in writes:
            assert (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "trim_scope_name"
            ), f"{module.__name__}:{lineno} stores scope_name without trim_scope_name()"


# --------------------------------------------------------------------------
# the incident handle is documented as the thing it is
# --------------------------------------------------------------------------


def _column_comment(model: type, column: str) -> str:
    """The comment block sitting immediately above ``column``'s declaration."""
    lines = inspect.getsource(model).splitlines()
    for index, line in enumerate(lines):
        if not line.strip().startswith(f"{column}:"):
            continue
        block: list[str] = []
        cursor = index - 1
        while cursor >= 0 and lines[cursor].strip().startswith("#"):
            block.append(lines[cursor].strip().lstrip("#").strip())
            cursor -= 1
        return "\n".join(reversed(block))
    raise AssertionError(f"{model.__name__} declares no column named {column}")


def test_the_incident_column_is_not_documented_as_a_per_delivery_co_firing_tag() -> None:
    """The fix is prose, so one assertion has to be on prose.

    The two banned strings are verbatim from the comment this issue replaced,
    and they are banned as SENTENCES rather than as words: the block still
    mentions the bucket (to say it is absent) and still mentions co-firing (to
    say it is a peer count, not this column). What is required instead is the
    structural property whose absence made the comment unrepairable last time —
    that it names ``dispatch._correlation_group_id`` as the owner of the key
    instead of restating the key, so that changing the tuple cannot silently
    falsify a model file. A later fix changed exactly that tuple — a
    project-global metric scope hashes a literal where a config id used to go —
    and this comment needed no edit, which is the property being bought.

    On its own this is a string check. The three tests below are what make it
    one: each pins a claim the replacement text makes.
    """
    block = _column_comment(AlertDeliveryItem, "correlation_group_id")

    assert "inside one delivery share" not in block
    assert "NULL when the item is a singleton" not in block

    # The owner of the key, named rather than copied.
    assert "_correlation_group_id" in block
    # And the three places a reader can check the claims that remain: the inbox
    # gate that makes a missing handle fatal, the query that rescues the legacy
    # rows, and the NOT NULL twin that proves nothing writes NULL today.
    assert "_INBOX_GROUP_SELECT" in block
    assert "ungrouped" in block
    assert "AlertPendingItem" in block


def test_the_handle_carries_no_bucket_and_splits_per_scope() -> None:
    """Both halves of the replaced sentence, as facts about the minting function.

    Event scopes only, deliberately — not because the metric arm is untested but
    because it is tested somewhere else. The partition a metric scope hashes is
    the subject of a later fix and is pinned in its own section at the end of this
    file; keeping it out of here means these three assertions stay about the
    BUCKET and the SCOPE, which is all that earlier fix ever claimed.
    """
    signature = inspect.signature(metrics_dispatch._correlation_group_id)
    # "Items that fired in the same bucket ... share this id" cannot be true of
    # a function that is never told what the bucket is, and "NULL when the item
    # is a singleton" cannot be true of one that is never told about peers.
    # Pinned as the whole parameter set rather than two absences, so that
    # smuggling either back in under another name fails here too.
    assert set(signature.parameters) == {
        "scan_config_id",
        "rule_id",
        "scope_type",
        "scope_ref",
        "direction",
    }

    config_id = uuid.UUID(int=7)
    rule_id = uuid.UUID(int=8)

    def group(scope_ref: str, *, direction: str = "spike") -> uuid.UUID:
        return metrics_dispatch._correlation_group_id(
            scan_config_id=config_id,
            rule_id=rule_id,
            scope_type=SCOPE_EVENT,
            scope_ref=scope_ref,
            direction=direction,
        )

    # One incident keeps one handle across collections. Structural, not lucky:
    # none of the five inputs above is a clock or a bucket, so a caller has no
    # way to make the second hour of an incident answer differently.
    assert group("checkout") == group("checkout")
    # Two scopes co-firing in a single delivery get DIFFERENT handles. This is
    # the row pair the replaced comment said would share one id.
    assert group("checkout") != group("signup")
    # Direction still splits, which is the one clause that was never wrong.
    assert group("checkout") != group("checkout", direction="drop")


def test_every_anomaly_is_minted_a_handle_with_no_condition_in_between() -> None:
    """Every item gets one, and it is ``_prepare_alert_deliveries`` that decides so.

    The shape asserted is the one a regression would break: a ``for`` loop over
    a plain name whose entire body is the unconditional assignment. Reserving
    the handle for co-fired items again means either an ``if`` inside that body
    or a filtered iterable in its header, and both fail here — which is the
    difference between this and asserting the call merely exists somewhere.
    """
    tree = ast.parse(inspect.getsource(metrics_dispatch._prepare_alert_deliveries))
    mints = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.For)
        # A bare name, not ``[a for a in anomalies if peers[a] > 1]``.
        and isinstance(node.iter, ast.Name)
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Assign)
        and isinstance(node.body[0].value, ast.Call)
        and isinstance(node.body[0].value.func, ast.Name)
        and node.body[0].value.func.id == "_correlation_group_id"
    ]
    assert len(mints) == 1, (
        "_prepare_alert_deliveries no longer mints one correlation handle per "
        "anomaly unconditionally; see AlertDeliveryItem.correlation_group_id"
    )


def test_a_delivery_carrying_one_item_still_gets_a_handle_the_inbox_can_see(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A solitary alert — precisely the case the replaced comment called NULL.

    The row is re-selected through the inbox's own predicate rather than
    checked for non-NULL in Python, because that predicate is what made a
    missing handle mean "unacknowledgeable" instead of merely "unlabelled":
    ``_alerting_deliveries._INBOX_GROUP_SELECT`` filters ``is_not(None)``, and
    an item it cannot see can never be acknowledged, muted or resolved.
    """
    monkeypatch.setattr(app_settings_service, "get_runtime_config_sync", _ConfigReads())

    with sync_session_factory() as session:
        config, destination, rule = _seed(session)
        (candidate,) = _candidates(config, count=1)
        expected = metrics_dispatch._correlation_group_id(
            scan_config_id=config.id,
            rule_id=rule.id,
            scope_type=candidate.scope_type,
            scope_ref=candidate.scope_ref,
            direction=candidate.direction,
        )

        (delivery_id,) = metrics_dispatch._create_deliveries(
            session,
            config,
            project_slug=SLUG,
            rule=rule,
            destination=destination,
            anomalies=[candidate],
            scope_names={(candidate.scope_type, candidate.scope_ref): "Checkout"},
            correlation_by_anomaly={id(candidate): expected},
            scan_job_id=None,
        )
        session.flush()

        visible = (
            session.execute(
                select(AlertDeliveryItem).where(
                    AlertDeliveryItem.delivery_id == delivery_id,
                    AlertDeliveryItem.correlation_group_id.is_not(None),
                )
            )
            .scalars()
            .all()
        )
        # One item, no peers, and still an incident the inbox lists.
        assert [row.correlation_group_id for row in visible] == [expected]

    # The remaining nullability is history, not a live state: the buffer the
    # digest path writes instead cannot even express NULL, and ``alert_flush``
    # copies that value straight onto the item it mints.
    assert AlertDeliveryItem.__table__.c.correlation_group_id.nullable is True
    assert AlertPendingItem.__table__.c.correlation_group_id.nullable is False


# --------------------------------------------------------------------------
# a scan config load does not drag in its scan history
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _captured_sql(engine: Engine) -> Iterator[list[str]]:
    """Every statement ``engine`` executes inside the block."""
    statements: list[str] = []

    def _record(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", _record)


def _engine_of(session: Session) -> Engine:
    engine = session.get_bind()
    assert isinstance(engine, Engine)
    return engine


def _tables_read(statements: list[str]) -> set[str]:
    """Which table each captured SELECT reads FROM.

    The FROM table only — a joined parent is part of the SAME round trip and is
    not what this issue is about. What it does show is one entry per EXTRA
    query, which is exactly what an eager collection adds.
    """
    tables: set[str] = set()
    for statement in statements:
        flat = " ".join(statement.split())
        if not flat.upper().startswith("SELECT"):
            continue
        match = re.search(r"\bFROM\s+([a-z_]+)", flat, re.IGNORECASE)
        if match is not None:
            tables.add(match.group(1).lower())
    return tables


def _seed_scan_jobs(session: Session, config: ScanConfig, *, count: int) -> None:
    """``count`` completed jobs, each carrying the JSON blob a real one does."""
    for index in range(count):
        session.add(
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                status="completed",
                result_summary={
                    "n_event_metrics": index,
                    "details": [f"a sentence about run {index}"] * 8,
                },
            )
        )
    session.commit()


def test_loading_a_scan_config_does_not_load_its_scan_history(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """One statement, and none of it about ``scan_jobs``.

    Restoring ``lazy="selectin"`` on the relationship takes this to two
    statements and puts ``scan_jobs`` in the table set, so both assertions fail
    on a revert — and they fail on the plainest possible caller, which is the
    point: the inbox was only the caller the issue was filed against.

    A FRESH session for the read is what gives it teeth. An instance already in
    the identity map is not re-populated, so a capture taken on the seeding
    session would emit nothing either way.
    """
    with sync_session_factory() as session:
        config, _destination, _rule = _seed(session)
        _seed_scan_jobs(session, config, count=25)

    with sync_session_factory() as session, _captured_sql(_engine_of(session)) as statements:
        loaded = session.execute(select(ScanConfig)).scalars().all()

    assert [row.id for row in loaded] == [config.id]
    assert _tables_read(statements) == {"scan_configs"}
    assert len(statements) == 1, statements


def test_the_inbox_query_reads_scan_config_columns_and_no_scan_history(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real ``_INBOX_GROUP_SELECT``, not a lookalike rebuilt here.

    Imported from the service so the statement under test is the one all four
    inbox call sites execute — the list's source rows, the silenced-orphan
    rescue, the single-card rebuild and the per-group unwindowed fallback. A
    hand-rolled copy would go on passing after someone changed the real one.

    The exact table set is asserted rather than just the absence of
    ``scan_jobs``. Inbox cards read selected entity columns and do not need
    their relationships; a new eager collection would add SQL here.
    """
    monkeypatch.setattr(app_settings_service, "get_runtime_config_sync", _ConfigReads())

    with sync_session_factory() as session:
        config, destination, rule = _seed(session)
        _mint(session, config, destination, rule, _candidates(config, count=1))
        session.commit()
        _seed_scan_jobs(session, config, count=25)

    with sync_session_factory() as session, _captured_sql(_engine_of(session)) as statements:
        rows = session.execute(_INBOX_GROUP_SELECT).tuples().all()
        # Read INSIDE the capture: the four names the card renders are columns
        # of entities the query already loaded, so reading them may not cost a
        # further statement. That is half the contract — the other half is that
        # the fix did not achieve its saving by making the card unrenderable.
        _item, _delivery, row_destination, row_rule, row_scan = rows[0]
        rendered = (row_destination.name, row_rule.name, row_scan.name)

    assert len(rows) == 1
    assert rendered == ("Main Slack", "Everything", "Scan")
    assert "scan_jobs" not in " ".join(statements)
    assert _tables_read(statements) == {"alert_delivery_items"}
    assert len(statements) == 1, statements


def test_deleting_a_scan_config_still_removes_its_jobs(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The other half of the contract: making the collection lazy strands no rows.

    The collection exists for this cascade and nothing else, so the loading
    change must not cost it. What this actually catches, measured rather than
    assumed, is a later edit to the OTHER two keywords on the relationship:
    dropping ``delete-orphan`` from ``cascade``, or adding
    ``passive_deletes=True``. Either stops the unit of work emitting the child
    DELETEs and fails here. It does NOT catch the loader strategy — ``lazy="raise"``
    leaves the cascade loading and deleting exactly as it does today — which is
    worth knowing, because the reverse is the natural guess.

    The fixture guard below is what gives the assertion teeth:
    ``sync_session_factory`` deliberately does not set ``PRAGMA foreign_keys=ON``
    the way the async suite engine does (tests/_sqlite.py), so SQLite is NOT
    enforcing the ``ondelete="CASCADE"`` on ``ScanJob.scan_config_id`` and the
    ORM's own child DELETE is the only thing that can empty this table. Without
    it the test would keep passing against a relationship that had stopped
    cascading altogether.
    """
    with sync_session_factory() as session:
        (fk_enforcement,) = session.execute(text("PRAGMA foreign_keys")).one()
        assert fk_enforcement == 0, (
            "the database would cascade on its own here, which would leave the "
            "ORM-side cascade this relationship exists for untested"
        )

        config, _destination, _rule = _seed(session)
        _seed_scan_jobs(session, config, count=25)
        assert len(session.execute(select(ScanJob)).scalars().all()) == 25

        session.delete(config)
        session.commit()

    with sync_session_factory() as session:
        assert session.execute(select(ScanJob)).scalars().all() == []


def test_nothing_reads_the_scan_job_collection_off_an_instance() -> None:
    """The premise of the fix, kept true by a scan instead of by a comment.

    "Nothing reads the attribute" is what makes lazy loading free here, and it
    is the kind of claim that decays without anyone noticing. A single
    ``config.scan_jobs`` in a service does not merely cost a query on the async
    request path — the lazy load cannot do IO outside the greenlet, so it raises
    ``MissingGreenlet`` — and in the sync worker it is an N+1 over a table with
    no retention. Neither reads as anything but an attribute access in review.

    CLASS-bound reads are allowed deliberately: ``selectinload(ScanConfig.scan_jobs)``
    is the supported way for a caller that genuinely needs the collection to ask
    for it, and it is safe precisely because it is explicit at the call site.
    Only ``<instance>.scan_jobs`` is rejected.
    """
    root = Path(tripl.__file__).resolve().parent
    offenders: list[str] = []
    scanned = 0
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts:
            continue
        scanned += 1
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Attribute) and node.attr == "scan_jobs"):
                continue
            if isinstance(node.value, ast.Name) and node.value.id == "ScanConfig":
                continue
            offenders.append(f"{path.relative_to(root)}:{node.lineno}")

    # The scan cannot silently stop finding files.
    assert scanned > 200, scanned
    assert offenders == [], (
        "these read ScanConfig.scan_jobs off an instance, which is a lazy load "
        f"since the selectin removal — see models/scan_config.py: {offenders}"
    )


# --------------------------------------------------------------------------
# a metric scope is project-global, so its state and its
# buffered alerts store NO scan config at all
# --------------------------------------------------------------------------
#
# ``metric``-scope signals are project-global — the anomaly row itself carries a
# NULL ``scan_config_id`` — but ``AlertRuleState`` and ``AlertPendingItem`` could
# not say so, because the column was a NOT-NULL FK. Dispatch faked it by
# anchoring every metric row on the project's LOWEST config id, and uuid4 has no
# order: creating a config whose id sorted below the anchor MOVED it, so the
# shared state became unreachable, the cooldown reset, a duplicate notification
# shipped, and the abandoned row stayed ``is_active=True`` forever. Deleting the
# anchor did the same through ON DELETE CASCADE, and took the undelivered digest
# lines with it.
#
# Both halves are pinned below. So is the half that exists only because the
# column became nullable: a NULL-anchored buffered row must still be DELIVERED
# by the digest, not skipped by its ``config is None`` guard and then deleted as
# claimed. That is the assertion to gate a revert on — it is the one place in
# this change that can destroy an alert silently and permanently.
#
# The two tests that used to cover this area computed the anchor AFTER both
# configs existed (``canonical = min(config1.id, config2.id)``), which is
# precisely why they passed while the bug was live. Every id below that has to
# sort a particular way is CONSTRUCTED, never drawn.

_METRIC_SCOPE = MetricScopeType.metric.value


@pytest.fixture
def fk_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """Like ``sync_session_factory``, but with ``PRAGMA foreign_keys=ON``.

    A second fixture rather than a change to the first: the scan-config
    cascade test above needs enforcement OFF, so that the ORM's own child DELETE
    is the only actor that could empty the table. Here the DATABASE's cascade is
    the thing under test — deleting a scan config used to destroy the shared
    metric state and the alerts buffered for the next digest — and a test of a
    cascade the test database does not enforce cannot fail.
    """
    from tripl.tests._sqlite import enable_sqlite_foreign_keys

    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_metric_scope.db'}")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _utc_now() -> datetime:
    """Current UTC time for persisted alert state fixtures."""
    return datetime.now(UTC).replace(microsecond=0)


def _metric_states(session: Session) -> list[Any]:
    from tripl.models.alert_rule_state import AlertRuleState

    return list(
        session.execute(
            select(AlertRuleState).where(AlertRuleState.scope_type == _METRIC_SCOPE)
        ).scalars()
    )


def _add_sibling_config(session: Session, config: ScanConfig, *, id_int: int) -> ScanConfig:
    """Another scan config in the same project, with a CONSTRUCTED id.

    ``uuid.UUID(int=0)`` sorts below every uuid4 there has ever been. Relying on
    chance instead would reproduce the bug only 1/(n+1) of the time, which is
    exactly how this defect survived two tests that looked like they covered it.
    """
    sibling = ScanConfig(
        id=uuid.UUID(int=id_int),
        data_source_id=config.data_source_id,
        project_id=config.project_id,
        name=f"Scan {id_int}",
        base_query="SELECT time, event_name FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval="1h",
    )
    session.add(sibling)
    session.commit()
    return sibling


def _buffer_metric(
    session: Session,
    config: ScanConfig,
    destination: AlertDestination,
    rule: AlertRule,
    *,
    scope_ref: str,
    bucket: datetime | None = None,
) -> None:
    """Buffer one project-global metric signal, the way dispatch would from ``config``."""
    candidate = DriftAlertCandidate(
        id=uuid.uuid4(),
        # A metric candidate carries no scan config, which is the fact the
        # buffer row is finally able to record.
        scan_config_id=None,
        scope_type=_METRIC_SCOPE,
        scope_ref=scope_ref,
        event_id=None,
        event_type_id=None,
        bucket=bucket or _BUCKET,
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
        scope_names={(_METRIC_SCOPE, scope_ref): "Signups"},
        # Exactly what dispatch computes, and through the same helper it calls:
        # a metric scope hashes NO config, so every scan of the project arrives
        # at one handle. Mirrored rather than hard-coded, so
        # this fixture cannot drift from the caller it stands in for.
        correlation_by_anomaly={
            id(candidate): metrics_dispatch._correlation_group_id(
                scan_config_id=metrics_dispatch._scope_partition_id(
                    _METRIC_SCOPE, config_id=config.id
                ),
                rule_id=rule.id,
                scope_type=_METRIC_SCOPE,
                scope_ref=scope_ref,
                direction="spike",
            )
        },
        scan_job_id=None,
        # The wall clock, not ``_NOW``: the flush tick these rows feed reads
        # ``datetime.now`` and sweeps anything older than 14 days, so a fixed
        # stamp turned every test here red once the calendar passed it.
        now=datetime.now(UTC),
    )
    session.commit()


def _put_on_a_cadence(session: Session, destination: AlertDestination) -> None:
    """Give the destination a due digest window, so one flush tick ships it."""
    from datetime import UTC, timedelta

    destination.delivery_schedule_cron = "* * * * *"
    destination.last_flushed_at = datetime.now(UTC) - timedelta(hours=2)
    session.commit()


def _run_flush(
    monkeypatch: pytest.MonkeyPatch,
    factory: sessionmaker[Session],
) -> tuple[dict[str, int], list[str]]:
    """One flush tick with both dispatch routes captured instead of enqueued."""
    from tripl.worker.tasks import alert_digest_send as digest_module
    from tripl.worker.tasks import alert_flush
    from tripl.worker.tasks import alerts as alerts_module

    enqueued: list[str] = []
    digests: list[list[str]] = []
    monkeypatch.setattr(alert_flush, "_get_sync_session", factory)
    monkeypatch.setattr(
        alerts_module.send_alert_delivery,
        "delay",
        lambda delivery_id: enqueued.append(delivery_id),
    )
    monkeypatch.setattr(
        digest_module.send_alert_digest,
        "delay",
        lambda delivery_ids: digests.append(list(delivery_ids)),
    )
    return alert_flush.flush_due_alert_digests.run(), enqueued


def test_creating_a_scan_config_cannot_move_the_metric_cooldown_anchor(
    fk_session_factory: sessionmaker[Session],
) -> None:
    """The filed defect: creating a scan config re-sent an already-open metric alert.

    Under the anchor scheme the second dispatch recomputed ``min(config ids)``,
    got the config created between the two runs, found no state under it, and
    took the ``current_state is None`` branch — which sets ``should_send`` with
    no cooldown consult at all. One duplicate send, a reset clock, and a
    permanently unreachable ``is_active=True`` row left behind on the old anchor.

    Reverting either half of the dispatch fix fails this: put a config-id
    equality back on the metric state load in place of ``.is_(None)`` and the
    second dispatch stops finding the row; make ``_scope_partition_id`` hand a
    metric scope ``config.id`` back and the first dispatch stores an id where
    the assertion below demands a NULL.
    """
    from tripl.models.alert_rule_state import AlertRuleState
    from tripl.tests.test_metric_anomaly_scope import _add_rule, _seed_spiked_metric

    with fk_session_factory() as session:
        config, _metric = _seed_spiked_metric(session)
        _add_rule(session, config, include_metrics=True)

        first = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        assert len(first) == 1
        state = session.execute(
            select(AlertRuleState).where(AlertRuleState.scope_type == _METRIC_SCOPE)
        ).scalar_one()
        assert state.scan_config_id is None, "a metric scope belongs to the project, not a scan"

        # The send path stamps this on a successful delivery; do it by hand so
        # the cooldown below is live rather than NULL ("never told them").
        notified_at = _utc_now()
        state.last_notified_at = notified_at
        session.commit()

        # THE MOVE. Created after the state exists, with an id below every other
        # config in the project — the old anchor rule would elect it.
        _add_sibling_config(session, config, id_int=0)

        assert (
            metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None) == []
        ), (
            "the open metric incident is inside its cooldown; creating a scan "
            "config must not make it a first-ever firing again"
        )

        states = _metric_states(session)
        assert len(states) == 1, "one project-global scope, one state row"
        assert states[0].scan_config_id is None
        assert states[0].last_notified_at == notified_at, "the cooldown clock was reset"
        assert [s for s in states if s.scan_config_id is not None] == []


def test_deleting_a_scan_config_no_longer_destroys_the_shared_metric_state(
    fk_session_factory: sessionmaker[Session],
) -> None:
    """The half the "pick the oldest config" alternative could not have fixed.

    While the state was anchored on a real config, ``ondelete="CASCADE"`` took it
    away with that config: the next collection found nothing, treated a still-open
    anomaly as first-ever, and alerted again with the cooldown reset. Storing NULL
    puts the row out of the cascade's reach, which is the point of the column
    change rather than a side effect of it.

    Revert ``scan_config_id`` to NOT NULL and this cannot even be expressed.
    """
    from sqlalchemy import text as sa_text

    from tripl.tests.test_metric_anomaly_scope import _add_rule, _seed_spiked_metric

    with fk_session_factory() as session:
        # Without enforcement this test asserts the survival of a row that only
        # survived because nothing was checking.
        assert session.execute(sa_text("PRAGMA foreign_keys")).scalar() == 1

        config, _metric = _seed_spiked_metric(session)
        _add_rule(session, config, include_metrics=True)
        survivor_config = _add_sibling_config(session, config, id_int=0)

        assert (
            len(metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)) == 1
        )
        state = _metric_states(session)[0]
        notified_at = _utc_now()
        state.last_notified_at = notified_at
        session.commit()

        # Delete the scan the state was born under, exactly as
        # ``scan_service.delete_scan_config`` does.
        session.delete(config)
        session.commit()

        assert (
            metrics_dispatch._prepare_alert_deliveries(session, survivor_config, scan_job_id=None)
            == []
        ), "deleting a scan config must not reset a project-global cooldown"

        states = _metric_states(session)
        assert len(states) == 1
        assert states[0].scan_config_id is None
        assert states[0].last_notified_at == notified_at


def test_a_buffered_metric_alert_survives_deleting_a_scan_config(
    fk_session_factory: sessionmaker[Session],
) -> None:
    """The quieter half of the same cascade: alerts held for the next digest.

    ``AlertPendingItem.scan_config_id`` is ``ondelete="CASCADE"`` too, so while a
    metric row anchored on a real config, deleting that config destroyed the
    metric alerts waiting for the next digest window — never delivered, never
    recovered, and with nothing anywhere to say so. On a daily cadence that
    window is a whole day wide.
    """
    with fk_session_factory() as session:
        config, destination, rule = _seed(session)
        _add_sibling_config(session, config, id_int=0)
        scope_ref = str(uuid.uuid4())
        _buffer_metric(session, config, destination, rule, scope_ref=scope_ref)

        buffered = session.execute(select(AlertPendingItem)).scalars().one()
        assert buffered.scan_config_id is None
        held_id = buffered.id

        session.delete(config)
        session.commit()

        still_held = session.execute(select(AlertPendingItem)).scalars().one()
        assert still_held.id == held_id


def test_a_project_global_metric_alert_is_delivered_by_the_digest(
    fk_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE GATE. A NULL-anchored buffered row must be SENT, not skipped and deleted.

    ``_build_digest`` groups by ``(rule_id, scan_config_id)``, resolves the config
    from a dict loaded by id, and ``continue``s when it finds none — while every
    claimed row is deleted unconditionally at the end of the same function. A
    metric row storing NULL would therefore be dropped from every digest AND
    destroyed, silently and permanently, within a minute of the new worker
    booting (the flusher ticks every 60s).

    So the assertion is deliberately two-sided: a delivery EXISTS, carrying the
    metric item and a real scan name, and the buffer emptied. A test that only
    checked the buffer emptied would pass while losing the alert.

    Delete the ``project_global_config`` fallback in ``alert_flush`` and the
    delivery assertions go red while the buffer one still passes — which is the
    whole reason both are here.
    """
    with fk_session_factory() as session:
        config, destination, rule = _seed(session)
        _put_on_a_cadence(session, destination)
        scope_ref = str(uuid.uuid4())
        _buffer_metric(session, config, destination, rule, scope_ref=scope_ref)
        config_id, config_name = config.id, config.name

    result, enqueued = _run_flush(monkeypatch, fk_session_factory)

    assert result["deliveries"] == 1
    assert len(enqueued) == 1
    with fk_session_factory() as session:
        delivery = session.execute(select(AlertDelivery)).scalars().one()
        items = (
            session.execute(
                select(AlertDeliveryItem).where(AlertDeliveryItem.delivery_id == delivery.id)
            )
            .scalars()
            .all()
        )
        assert [item.scope_type for item in items] == [_METRIC_SCOPE]
        assert [item.scope_ref for item in items] == [scope_ref]
        # The delivery is ATTRIBUTED to a scan (its column is NOT NULL and the
        # inbox INNER JOINs on it) even though the signal belongs to no scan.
        # Presentation, resolved deterministically — it keys nothing.
        assert delivery.scan_config_id == config_id
        snapshot = delivery.payload_snapshot
        assert isinstance(snapshot, dict)
        assert snapshot["scan_name"] == config_name
        assert session.execute(select(AlertPendingItem)).scalars().all() == []


def test_a_project_global_metric_row_with_no_scan_left_is_skipped_not_raised(
    fk_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The genuinely unresolvable case still degrades the way the deleted-rule one does.

    A project whose every scan config has been deleted has nothing to render a
    digest against, so the ``continue`` has to stand — and the row still has to
    be claimed, or the flusher would retry it every 60 seconds forever.
    """
    with fk_session_factory() as session:
        config, destination, rule = _seed(session)
        _put_on_a_cadence(session, destination)
        _buffer_metric(session, config, destination, rule, scope_ref=str(uuid.uuid4()))
        session.delete(config)
        session.commit()

    result, enqueued = _run_flush(monkeypatch, fk_session_factory)

    assert result["deliveries"] == 0
    assert enqueued == []
    with fk_session_factory() as session:
        assert session.execute(select(AlertDelivery)).scalars().all() == []
        assert session.execute(select(AlertPendingItem)).scalars().all() == []


def test_two_scans_buffer_one_row_and_one_digest_line_for_one_metric(
    fk_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upsert has to CONFLICT on the partial index, or the dedupe fails open.

    ``uq_alert_pending_item_scope`` includes ``scan_config_id`` and SQL treats
    NULLs as DISTINCT, so it can never fire for a metric row. If
    ``_buffer_pending_items`` keeps naming it as the conflict target, the second
    collection does not update the first row — and the digest carries the same
    project-wide anomaly once per scan.

    Reverting the ``index_where`` branch turns this red either way: on a database
    that has the partial index the second insert raises IntegrityError, and on one
    that does not it silently buffers two rows and mints two digest lines.
    """
    with fk_session_factory() as session:
        config, destination, rule = _seed(session)
        second = _add_sibling_config(session, config, id_int=0)
        _put_on_a_cadence(session, destination)
        scope_ref = str(uuid.uuid4())

        _buffer_metric(session, config, destination, rule, scope_ref=scope_ref)
        _buffer_metric(session, second, destination, rule, scope_ref=scope_ref)

        buffered = session.execute(select(AlertPendingItem)).scalars().all()
        assert len(buffered) == 1, "one project-global scope buffers ONE row, not one per scan"
        assert buffered[0].scan_config_id is None
        assert buffered[0].observation_count == 2, "the second collection updated, not inserted"

    result, _enqueued = _run_flush(monkeypatch, fk_session_factory)

    assert result["deliveries"] == 1
    with fk_session_factory() as session:
        items = session.execute(select(AlertDeliveryItem)).scalars().all()
        assert len(items) == 1


def test_the_partial_unique_indexes_exist_on_sqlite_and_actually_dedupe(
    fk_session_factory: sessionmaker[Session],
) -> None:
    """``sqlite_where`` is load-bearing, and this is the only thing that proves it.

    The unit suite builds its schema from ``Base.metadata.create_all`` and never
    runs the Postgres-gated migration, so declaring only ``postgresql_where``
    would leave both tables un-deduped everywhere the suite can see — passing
    every other test and failing only in production, which is the exact failure
    mode the partial index exists to prevent.

    Structural and behavioural on purpose: the first half fails if a dialect
    predicate is dropped, the second if the index stops being unique.
    """
    from sqlalchemy.exc import IntegrityError

    from tripl.models.alert_rule_state import AlertRuleState

    for model, index_name in (
        (AlertRuleState, "uq_alert_rule_state_metric_scope"),
        (AlertPendingItem, "uq_alert_pending_item_metric_scope"),
    ):
        index = next(i for i in model.__table__.indexes if i.name == index_name)
        assert index.unique is True
        assert index.dialect_options["postgresql"]["where"] is not None
        assert index.dialect_options["sqlite"]["where"] is not None, (
            f"{index_name} would not exist on the test database, so nothing in "
            "the unit suite could ever catch a duplicate project-global row"
        )

    with fk_session_factory() as session:
        _config, _destination, rule = _seed(session)
        scope_ref = str(uuid.uuid4())

        def _state() -> Any:
            return AlertRuleState(
                id=uuid.uuid4(),
                rule_id=rule.id,
                scan_config_id=None,
                scope_type=_METRIC_SCOPE,
                scope_ref=scope_ref,
                is_active=True,
            )

        session.add(_state())
        session.commit()
        session.add(_state())
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_the_send_stamp_finds_the_project_global_state_and_only_that_one(
    fk_session_factory: sessionmaker[Session],
) -> None:
    """``_stamp_rule_state`` filters on the NULL instead of skipping the column.

    Skipping it — which is what it used to do for a metric scope — stamped EVERY
    metric state of the rule and scope in the project. That is what kept the
    abandoned rows of a moved anchor looking freshly notified while nothing could
    ever load them again: permanently ``is_active=True``, permanently re-stamped,
    and counted as a firing scope on the Monitors screen.

    The decoy below is the row a straggling old worker could still write during a
    rolling deploy. The SEND path must leave it alone; delete the ``.is_(None)``
    else-arm and it gets stamped too, which is what kept an unreachable row
    looking freshly notified forever. Not stamping it is only half an answer,
    though — retiring it belongs to dispatch, and the test after this one is what
    pins that.
    """
    from tripl.models.alert_rule_state import AlertRuleState
    from tripl.worker.tasks import alerts as alerts_task

    with fk_session_factory() as session:
        config, destination, rule = _seed(session)
        scope_ref = str(uuid.uuid4())
        shared = AlertRuleState(
            id=uuid.uuid4(),
            rule_id=rule.id,
            scan_config_id=None,
            scope_type=_METRIC_SCOPE,
            scope_ref=scope_ref,
            is_active=True,
        )
        straggler = AlertRuleState(
            id=uuid.uuid4(),
            rule_id=rule.id,
            scan_config_id=config.id,
            scope_type=_METRIC_SCOPE,
            scope_ref=scope_ref,
            is_active=True,
        )
        session.add_all([shared, straggler])
        session.commit()

        sent_at = _utc_now()
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=config.project_id,
            scan_config_id=config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            status="sent",
            channel="slack",
            matched_count=1,
            payload_snapshot=None,
            sent_at=sent_at,
        )
        session.add(delivery)
        session.flush()
        session.add(
            AlertDeliveryItem(
                id=uuid.uuid4(),
                delivery_id=delivery.id,
                scope_type=_METRIC_SCOPE,
                scope_ref=scope_ref,
                scope_name="Signups",
                bucket=_BUCKET,
                direction="spike",
                actual_count=200.0,
                expected_count=10.0,
                absolute_delta=190.0,
                percent_delta=1900.0,
            )
        )
        session.commit()
        session.expire_all()

        stored = session.get(AlertDelivery, delivery.id)
        assert stored is not None
        alerts_task._stamp_rule_state(session, stored)
        session.commit()

        assert to_utc(session.get(AlertRuleState, shared.id).last_notified_at) == sent_at
        assert session.get(AlertRuleState, straggler.id).last_notified_at is None


def test_the_send_stamp_stamps_duplicates_instead_of_raising_after_the_send(
    fk_session_factory: sessionmaker[Session],
) -> None:
    """The one case where the stamp's loop and ``scalar_one_or_none`` differ.

    Both of its arms are single-row by key, so on a healthy schema the two forms
    are indistinguishable and the loop looks like something to tighten. The one
    that holds the NULL arm together is a PARTIAL index, which SQL does not give
    for free — the composite constraint stops deduping the moment the column is
    NULL — and which each dialect has to be told about separately: drop
    ``sqlite_where`` from the model and every database this suite builds loses
    it while the code keeps running. So the index is dropped here, which is the
    only way to reach the branch the comment in ``_stamp_rule_state`` is about.

    What it pins: the stamp writes both rows and returns. Replace the loop with
    ``scalar_one_or_none()`` and this reddens with ``MultipleResultsFound``,
    raised from inside the ``try`` that has already posted the message — the
    handler there rolls ``sent`` back to ``failed``, and a failed row is exactly
    what the Inbox's Retry button re-dispatches, over Slack, which keeps no
    delivered-marker to skip on. A bookkeeping write would have sent the alert
    twice.
    """
    from tripl.models.alert_rule_state import AlertRuleState
    from tripl.worker.tasks import alerts as alerts_task

    with fk_session_factory() as session:
        config, destination, rule = _seed(session)
        scope_ref = str(uuid.uuid4())
        # Raises "no such index" if the model ever stops declaring it, so this
        # test cannot quietly degrade into asserting nothing.
        session.execute(text("DROP INDEX uq_alert_rule_state_metric_scope"))
        session.commit()

        def _state() -> Any:
            return AlertRuleState(
                id=uuid.uuid4(),
                rule_id=rule.id,
                scan_config_id=None,
                scope_type=_METRIC_SCOPE,
                scope_ref=scope_ref,
                is_active=True,
            )

        # What two collections of one project leave behind once nothing
        # serialises their look-then-insert over the project-global row.
        first, second = _state(), _state()
        session.add_all([first, second])
        session.commit()

        sent_at = _utc_now()
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=config.project_id,
            scan_config_id=config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            status="sent",
            channel="slack",
            matched_count=1,
            payload_snapshot=None,
            sent_at=sent_at,
        )
        session.add(delivery)
        session.flush()
        session.add(
            AlertDeliveryItem(
                id=uuid.uuid4(),
                delivery_id=delivery.id,
                scope_type=_METRIC_SCOPE,
                scope_ref=scope_ref,
                scope_name="Signups",
                bucket=_BUCKET,
                direction="spike",
                actual_count=200.0,
                expected_count=10.0,
                absolute_delta=190.0,
                percent_delta=1900.0,
            )
        )
        session.commit()
        session.expire_all()

        stored = session.get(AlertDelivery, delivery.id)
        assert stored is not None
        alerts_task._stamp_rule_state(session, stored)
        session.commit()

        for state_id in (first.id, second.id):
            stamped = session.get(AlertRuleState, state_id)
            assert to_utc(stamped.last_notified_at) == sent_at
            assert stamped.last_notified_delivery_id == delivery.id


def test_dispatch_retires_a_metric_state_an_old_worker_anchored_on_a_config(
    fk_session_factory: sessionmaker[Session],
) -> None:
    """The straggler no write path can reach is deleted, or it is counted forever.

    ``migrate`` is a one-shot that app, celery-worker and celery-beat only WAIT
    on, so the previous release's worker is still collecting while the collapse
    commits and can insert one more config-anchored metric state after it — a row
    the migration cannot clear, because it did not exist when the migration ran.
    Neither state load in ``_prepare_alert_deliveries`` can see it (one demands
    this config AND a non-metric scope, the other a NULL config), so the close
    loop never closes it and ``_stamp_rule_state`` never stamps it, while
    ``_alerting_monitors`` and ``project_service`` load EVERY state of a rule with
    no scope or config predicate. Left alone it is one permanently active scope
    and the monitor never reads "healthy" again — the earlier metric-scope rot,
    re-created after the migration written to clear it.

    The ROLLUP is asserted, not only the surviving rows, because the rollup is
    the symptom. Drop the ``delete`` in ``_retire_config_anchored_metric_states``
    or its call above the loads, and the project-global row still closes
    correctly while this reads "warning" with ``active_scope_count == 1``.

    The event-scope row is the other half of the predicate: a sweep that stopped
    filtering on ``metric`` would delete the config-partitioned states every
    other scope's cooldown is kept in.
    """
    from tripl.models.alert_rule_state import AlertRuleState
    from tripl.services.monitoring_utils import summarize_monitor_states

    with fk_session_factory() as session:
        config, _destination, rule = _seed(session)
        scope_ref = str(uuid.uuid4())
        opened_at = _utc_now()

        def _state(scan_config_id: uuid.UUID | None, scope_type: str) -> Any:
            return AlertRuleState(
                id=uuid.uuid4(),
                rule_id=rule.id,
                scan_config_id=scan_config_id,
                scope_type=scope_type,
                scope_ref=scope_ref,
                is_active=True,
                opened_at=opened_at,
                last_anomaly_bucket=_BUCKET,
            )

        shared = _state(None, _METRIC_SCOPE)
        # What a pre-0zpq.28 worker writes: the metric state anchored on a config.
        straggler = _state(config.id, _METRIC_SCOPE)
        event_scoped = _state(config.id, SCOPE_EVENT)
        session.add_all([shared, straggler, event_scoped])
        session.commit()
        rule_id, shared_id, event_scoped_id = rule.id, shared.id, event_scoped.id

        # Nothing is firing, so this collection only reconciles state: the rows
        # dispatch CAN read close by the ordinary path and nothing is delivered.
        assert metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None) == []
        session.commit()

    with fk_session_factory() as session:
        # The Monitors screen's own load: every state of the rule, with no scope
        # and no config predicate (``_alerting_monitors``, ``project_service``).
        states = list(
            session.execute(
                select(AlertRuleState).where(AlertRuleState.rule_id == rule_id)
            ).scalars()
        )
        assert {state.id for state in states} == {shared_id, event_scoped_id}, (
            "the config-anchored metric row is unreadable by every write path, so "
            "dispatch has to retire it — and it must retire nothing else"
        )

        rollup = summarize_monitor_states(states, now=_utc_now())
        assert rollup.active_scope_count == 0
        assert rollup.status == "healthy", (
            "one unreachable state left open pins this monitor to 'warning' for "
            "good, which is the symptom that was reported originally"
        )


def test_the_demo_builder_seeds_a_metric_state_with_no_scan_config() -> None:
    """The demo seeds a catalog-metric firing, so it writes a metric state too.

    ``build_alerts`` seeds a metric-scope anomaly, ``_select_firing_anomalies``
    appends it and ``_build_firings`` turns every anomaly into a firing, so the
    state loop writes one for the metric scope on every demo provision. Anchored
    on ``ctx.scan_config_id`` that row is one live dispatch can never load again
    — the demo would ship the very defect this issue fixes.

    Asserted on the source rather than by provisioning a whole demo project: the
    builder is async, runs after the monitoring and catalog builders, and the
    property worth pinning is simply that the metric arm stores NULL.
    """
    source = Path(tripl.__file__).resolve().parent / "services" / "demo" / "builders" / "alerts.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "AlertRuleState"
    ]
    assert len(calls) == 1, "the builder writes rule states in exactly one place"
    keyword = next(kw for kw in calls[0].keywords if kw.arg == "scan_config_id")
    rendered = ast.unparse(keyword.value)
    assert isinstance(keyword.value, ast.IfExp), (
        "a metric firing is project-global and must store NULL, so this cannot "
        f"be an unconditional scan config: {rendered}"
    )
    assert "MetricScopeType.metric" in rendered
    assert rendered.startswith("None if")


# --------------------------------------------------------------------------
# the metric scope's INCIDENT HANDLE hashes no scan config either
# --------------------------------------------------------------------------
#
# An earlier fix moved a metric scope's STATE row and its BUFFERED row onto a NULL
# scan config. This is the third copy of the same identity, and the one left
# behind: ``_prepare_alert_deliveries`` built ``correlation_by_anomaly`` in one
# unconditional loop over ``config.id``, so ``_correlation_group_id`` hashed the
# FIRING scan even for a scope that belongs to no scan.
#
# A project with three scans — ``acme-ios`` runs three — therefore minted THREE
# incident handles for one project-wide catalog metric. That is not cosmetic:
# ``suppressed_group_ids`` is a set of these ids and is the entire mechanism
# behind an Inbox acknowledgement or mute. Acknowledge the handle scan A
# delivered and scan B's next collection computes one that is not in the set,
# sends anyway, and opens an AlertCorrelationState of its own — the operator's
# decision silently bypassed, and one incident listed in the Inbox up to N times
# against a user guide that promises one row per incident.
#
# The release path carried the mirror of it. ``_reopen_closed_incidents`` takes
# ONE partition per call and rebuilds the ids itself, while ``closed_keys`` can
# hold metric and non-metric scopes together, so the call is split in two.
#
# The rule implemented is the one established everywhere else:
# HASH THE PARTITION THE ROW ACTUALLY STORES. ``_scope_partition_id`` is the one
# answer for all three copies, and ``_correlation_group_id`` renders its NULL
# through a non-UUID literal so the project-global id space is provably disjoint
# from every config-scoped one.


def test_a_project_global_incident_handle_hashes_a_literal_no_scan_config_can_produce() -> None:
    """The sentinel, and the disjointness it exists to buy.

    A metric scope has no config to hash and the two obvious alternatives are
    both worse. Hashing the project id puts a different KIND of uuid into the
    slot a config id otherwise occupies, so the two spaces overlap in principle
    — and it adds no discriminating power, since ``rule_id`` is already in the
    key and a rule belongs to exactly one project. Letting ``None`` render
    itself hides the decision behind a repr.

    A literal is checkable, which is what the first assertion does: it is not a
    uuid, so nothing in ``scan_configs`` can ever render as it. It is also
    drift-proof, because there is no row behind it to create, delete or
    re-elect — precisely what went wrong with the lowest-id anchor it replaced.
    """
    rule_id = uuid.UUID(int=11)
    scope_ref = str(uuid.uuid4())

    def handle(scan_config_id: uuid.UUID | None) -> uuid.UUID:
        return metrics_dispatch._correlation_group_id(
            scan_config_id=scan_config_id,
            rule_id=rule_id,
            scope_type=_METRIC_SCOPE,
            scope_ref=scope_ref,
            direction="spike",
        )

    with pytest.raises(ValueError):
        uuid.UUID(metrics_dispatch._PROJECT_GLOBAL_PARTITION)

    project_global = handle(None)
    # Spelled out rather than compared against itself. A revert that simply
    # dropped the branch would hash ``str(None)``: still stable, still disjoint,
    # and still wrong — the string being hashed has to say what it means, or the
    # next reader has no way to tell a deliberate partition from an accident.
    assert project_global == uuid.uuid5(
        metrics_dispatch._CORRELATION_NAMESPACE,
        f"{metrics_dispatch._PROJECT_GLOBAL_PARTITION}:{rule_id}:{_METRIC_SCOPE}:{scope_ref}:spike",
    )
    # ``uuid.UUID(int=0)`` included on purpose: it is the id most likely to be a
    # project's lowest, i.e. exactly what the retired anchor would have elected.
    assert project_global != handle(uuid.UUID(int=0))
    assert project_global not in {handle(uuid.uuid4()) for _ in range(500)}


def test_every_scan_of_a_project_resolves_one_handle_for_a_metric_scope() -> None:
    """One helper answers for the state, the buffered row and the handle.

    ``_scope_partition_id`` exists so the three cannot drift, and they drifted
    for exactly as long as the branch was written out twice and forgotten the
    third time.

    The non-metric half is asserted beside it because a fix that collapsed EVERY
    scope onto the project would be the opposite defect and just as real: one
    scan's acknowledgement would silence another scan's genuinely separate
    incident on the same event.
    """
    first, second = uuid.uuid4(), uuid.uuid4()
    rule_id = uuid.uuid4()
    scope_ref = str(uuid.uuid4())

    def handle(config_id: uuid.UUID, scope_type: str) -> uuid.UUID:
        return metrics_dispatch._correlation_group_id(
            scan_config_id=metrics_dispatch._scope_partition_id(scope_type, config_id=config_id),
            rule_id=rule_id,
            scope_type=scope_type,
            scope_ref=scope_ref,
            direction="spike",
        )

    assert metrics_dispatch._scope_partition_id(_METRIC_SCOPE, config_id=first) is None
    assert metrics_dispatch._scope_partition_id(SCOPE_EVENT, config_id=first) == first

    assert handle(first, _METRIC_SCOPE) == handle(second, _METRIC_SCOPE)
    assert handle(first, SCOPE_EVENT) != handle(second, SCOPE_EVENT)


def test_an_inbox_acknowledgement_on_a_metric_incident_is_honoured_by_every_scan(
    fk_session_factory: sessionmaker[Session],
) -> None:
    """THE FILED DEFECT, end to end: acknowledge on scan A, and scan B stays quiet.

    Driven through ``_prepare_alert_deliveries`` rather than through the minting
    helper, because the bug was never in the helper — it was in the one call
    site that handed it ``config.id`` for every scope. A unit test of the hash
    would have passed throughout.

    Three assertions, each red under the same one-line revert
    (``scan_config_id=config.id`` back in the ``correlation_by_anomaly`` loop):
    the delivered item carries the project-global handle; scan B delivers
    nothing; and one project-wide scope still has exactly ONE inbox row, rather
    than a second card the operator would have to acknowledge again.
    """
    from tripl.models.alert_correlation_state import AlertCorrelationState
    from tripl.tests.test_metric_anomaly_scope import _add_rule, _seed_spiked_metric

    with fk_session_factory() as session:
        config, _metric = _seed_spiked_metric(session)
        rule = _add_rule(session, config, include_metrics=True)
        other = _add_sibling_config(session, config, id_int=0)

        (delivery_id,) = metrics_dispatch._prepare_alert_deliveries(
            session, config, scan_job_id=None
        )
        session.commit()

        item = (
            session.execute(
                select(AlertDeliveryItem).where(AlertDeliveryItem.delivery_id == delivery_id)
            )
            .scalars()
            .one()
        )
        assert item.scope_type == _METRIC_SCOPE
        assert item.correlation_group_id == metrics_dispatch._correlation_group_id(
            scan_config_id=None,
            rule_id=rule.id,
            scope_type=_METRIC_SCOPE,
            scope_ref=item.scope_ref,
            direction=item.direction,
        ), "the handle a project-global scope delivers must hash no scan config"

        # What the operator does in the Inbox, against the row this send opened.
        state = session.execute(select(AlertCorrelationState)).scalars().one()
        assert state.correlation_group_id == item.correlation_group_id
        state.status = "acknowledged"
        session.commit()

        assert metrics_dispatch._prepare_alert_deliveries(session, other, scan_job_id=None) == [], (
            "an acknowledged project-global incident must not page from another scan"
        )
        session.commit()

        assert len(session.execute(select(AlertDelivery)).scalars().all()) == 1
        surviving = session.execute(select(AlertCorrelationState)).scalars().all()
        assert [row.correlation_group_id for row in surviving] == [item.correlation_group_id], (
            "one project-global scope is ONE incident; a second row is a second "
            "Inbox card for the same metric, each needing its own acknowledgement"
        )


def test_closing_a_metric_scope_releases_the_acknowledgement_it_was_carrying(
    fk_session_factory: sessionmaker[Session],
) -> None:
    """The release path needs the same partition, or suppression becomes permanent.

    ``_reopen_closed_incidents`` rebuilds the ids itself from ONE partition per
    call, while ``closed_keys`` can hold metric and non-metric scopes together —
    which is why the call site splits it in two. Collapse it back into a single
    ``scan_config_id=config.id`` call and the metric ids rebuild under a config
    that never appeared in the hash: they match no stored row, the
    acknowledgement is never cleared, and since ``_correlation_group_id`` carries
    no bucket there is nothing downstream that will ever mint a fresh handle for
    that scope. The result is a catalog metric that can never alert again,
    silently, with nothing in the Inbox to explain it.
    """
    from tripl.models.alert_correlation_state import AlertCorrelationState
    from tripl.models.metric_anomaly import MetricAnomaly
    from tripl.tests.test_metric_anomaly_scope import _add_rule, _seed_spiked_metric

    with fk_session_factory() as session:
        config, _metric = _seed_spiked_metric(session)
        _add_rule(session, config, include_metrics=True)
        assert (
            len(metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)) == 1
        )
        session.commit()

        acknowledged = session.execute(select(AlertCorrelationState)).scalars().one()
        acknowledged.status = "acknowledged"
        session.commit()
        group_id = acknowledged.correlation_group_id

        # The incident ends. ``_recalculate_metric_anomalies`` deletes and
        # rewrites these rows on every collection, so a scope that stops firing
        # simply stops having one — which is what closes its AlertRuleState.
        session.execute(delete(MetricAnomaly))
        session.commit()

        assert metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None) == []
        session.commit()
        session.expire_all()

        # Proof the run really reached the release branch, rather than the test
        # asserting a status nothing was asked to change.
        assert _metric_states(session)[0].is_active is False

        released = session.execute(select(AlertCorrelationState)).scalars().one()
        assert released.correlation_group_id == group_id
        assert released.status == "open", (
            "the incident is over, so the scope's next firing is a new one and "
            "must not inherit the decision taken about the old one"
        )


def test_the_digest_buffer_carries_the_handle_the_immediate_path_would_mint(
    fk_session_factory: sessionmaker[Session],
) -> None:
    """The third copy of the identity, on the path that holds alerts for a digest.

    ``AlertPendingItem.correlation_group_id`` is stamped from the value dispatch
    computed and is never re-derived at flush time, so the buffer inherits
    whatever the mint decided. While the mint hashed the firing config, the row a
    cadence destination held carried scan A's handle and scan B's collection
    recomputed a different one — the divergence both that column's comment and
    ``_buffer_pending_items``' closing comment used to describe.

    ``alert_flush`` still filters suppression on the id the ROW carries, and that
    defence is deliberately kept: it covers the late-bucket read-back and a row
    buffered by an old worker mid-deploy. What is asserted here is that it is no
    longer papering over a disagreement. Revert the mint and the handle assertion
    goes red while the one-row assertions stay green — which is exactly how this
    survived the earlier buffer tests.
    """
    from tripl.models.alert_correlation_state import AlertCorrelationState
    from tripl.tests.test_metric_anomaly_scope import _add_rule, _seed_spiked_metric

    with fk_session_factory() as session:
        config, _metric = _seed_spiked_metric(session)
        rule = _add_rule(session, config, include_metrics=True)
        other = _add_sibling_config(session, config, id_int=0)
        destination = session.execute(select(AlertDestination)).scalars().one()
        _put_on_a_cadence(session, destination)

        held: list[int] = []
        for scan in (config, other):
            assert (
                metrics_dispatch._prepare_alert_deliveries(
                    session, scan, scan_job_id=None, buffered=held
                )
                == []
            ), "a destination on a cadence mints nothing now; it buffers"
        session.commit()
        assert held == [1, 1], "both scans offered the same project-wide signal"

        buffered = session.execute(select(AlertPendingItem)).scalars().one()
        assert buffered.scan_config_id is None
        assert buffered.observation_count == 2
        assert buffered.correlation_group_id == metrics_dispatch._correlation_group_id(
            scan_config_id=None,
            rule_id=rule.id,
            scope_type=_METRIC_SCOPE,
            scope_ref=buffered.scope_ref,
            direction=buffered.direction,
        ), "the buffered row's key and the incident's key have to be the same value"

        states = session.execute(select(AlertCorrelationState)).scalars().all()
        assert [row.correlation_group_id for row in states] == [buffered.correlation_group_id]


def test_no_call_site_hashes_a_scan_config_without_asking_the_partition() -> None:
    """The belt for the fourth call site nobody has written yet.

    The defect was never a wrong hash — it was one of three call sites reasoning
    about the partition on its own and getting it wrong, in a file where the
    other two got it right. So what is pinned is the calling CONVENTION: every
    caller either forwards a partition it was handed, passes the project-global
    ``None`` outright, or asks ``_scope_partition_id``. Nobody reaches for
    ``config.id``.

    A behavioural test cannot cover a site that does not exist yet; this one
    fails the moment it is added wrong.
    """
    # ``None`` is the project-global partition written out; ``scan_config_id``
    # is ``_reopen_closed_incidents`` forwarding the one its caller chose.
    allowed = {"None", "scan_config_id"}
    tree = ast.parse(Path(metrics_dispatch.__file__).read_text(encoding="utf-8"))
    passed = [
        ast.unparse(next(kw for kw in node.keywords if kw.arg == "scan_config_id").value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_correlation_group_id"
    ]
    assert passed, "the mint moved or was renamed; this guard no longer guards anything"
    for rendered in passed:
        assert rendered in allowed or rendered.startswith("_scope_partition_id("), (
            "_correlation_group_id must be handed the partition the scope's rows "
            f"STORE, never the config that happens to be collecting: {rendered}"
        )


# --------------------------------------------------------------------------
# a scope that flips direction ships one line per INCIDENT,
# and the prose finally says so
# --------------------------------------------------------------------------
#
# ``_buffer_pending_items`` promised that "a scope firing all day still occupies
# exactly one line" while the buffer's key ends in ``direction``. So a scope that
# DROPS at 03:00 and SPIKES at 11:00 buffers two rows and the daily digest
# carries both. The mechanism is real, current and common rather than a corner:
# 106 of 223 live scopes fired in BOTH directions inside one day
# (``models/alert_rule``).
#
# What is NOT real is the "one of them is stale" framing, or either remedy the
# issue proposed. ``direction`` is in the buffer key because it is in the
# INCIDENT: ``_correlation_group_id`` is "one rule, one scope, one direction",
# ``AlertPendingItem.correlation_group_id`` is ONE non-null column, and the user
# guide promises the Inbox is one row per incident. Both remedies break that.
#
#   * DROP ``direction`` from ``uq_alert_pending_item_scope`` and one row stands
#     for two incidents while naming only one of them. ``_build_digest`` filters
#     on exactly that value, so the surviving handle either swallows the
#     incident nobody acknowledged or ignores the acknowledgement that was made.
#   * DELETE the sibling on a flip and an incident that was buffered is
#     destroyed before it was ever delivered, and nothing re-offers it:
#     ``AlertRuleState`` has no direction column, so the flipped scope is
#     already reusing that one state row, and ``last_notified_at`` is stamped
#     only on a SENT delivery.
#
# This section therefore changes no behaviour. It pins the behaviour that was
# always right and that nothing tested — reverting to ``config.id``-style
# collapse broke not one test in the repo before these — and it pins the four
# prose sites that had gone false around it. Each test names the line it dies
# on.

# 03:00 drops, 11:00 spikes: the filed scenario's two collections, inside one
# daily window.
_FLIP_DROP_BUCKET = datetime(2026, 9, 14, 3, 0, tzinfo=UTC)
_FLIP_SPIKE_BUCKET = datetime(2026, 9, 14, 11, 0, tzinfo=UTC)


def _buffer_scope(
    session: Session,
    config: ScanConfig,
    destination: AlertDestination,
    rule: AlertRule,
    *,
    scope_ref: str,
    direction: str,
    actual: float,
    bucket: datetime,
) -> uuid.UUID:
    """Buffer one event-scoped signal in ``direction``, the way dispatch would.

    Returns the incident handle the row was offered under. Derived through the
    same two helpers the live caller uses rather than hard-coded, so this
    fixture cannot drift from ``_prepare_alert_deliveries``.
    """
    candidate = DriftAlertCandidate(
        id=uuid.uuid4(),
        scan_config_id=config.id,
        scope_type=SCOPE_EVENT,
        scope_ref=scope_ref,
        event_id=None,
        event_type_id=None,
        bucket=bucket,
        direction=direction,
        actual_count=actual,
        expected_count=100.0,
        drift_field=None,
        drift_type=None,
        sample_value=None,
    )
    group_id = metrics_dispatch._correlation_group_id(
        scan_config_id=metrics_dispatch._scope_partition_id(SCOPE_EVENT, config_id=config.id),
        rule_id=rule.id,
        scope_type=SCOPE_EVENT,
        scope_ref=scope_ref,
        direction=direction,
    )
    metrics_dispatch._buffer_pending_items(
        session,
        config,
        rule=rule,
        destination=destination,
        anomalies=[candidate],
        scope_names={(SCOPE_EVENT, scope_ref): "Checkout"},
        correlation_by_anomaly={id(candidate): group_id},
        scan_job_id=None,
        now=_NOW,
    )
    session.commit()
    return group_id


def _buffered(session: Session) -> list[AlertPendingItem]:
    return list(
        session.execute(select(AlertPendingItem).order_by(AlertPendingItem.direction))
        .scalars()
        .all()
    )


def test_a_scope_that_flips_direction_buffers_a_line_for_each_incident(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """THE FILED SCENARIO: drop at 03:00, spike at 11:00, two rows in the window.

    Red on BOTH remedies that were proposed. Take ``"direction"`` out of
    ``dispatch._PENDING_ITEM_CONFLICT_KEYS`` and out of
    ``uq_alert_pending_item_scope`` and the 11:00 spike UPDATES the 03:00 drop
    instead of inserting beside it, leaving one row where this asserts two. Add
    a delete-the-sibling pass to ``_buffer_pending_items`` and the drop row
    disappears the same way.

    The numbers are asserted PER ROW, because the point of keeping both is that
    each carries its own incident's last reading — not merely that two rows
    exist. The drop still carrying its 03:00 bucket and ``actual_count`` is what
    says the 11:00 collection did not reach across and rewrite it.
    """
    with sync_session_factory() as session:
        config, destination, rule = _seed(session)
        scope_ref = str(uuid.uuid4())

        _buffer_scope(
            session,
            config,
            destination,
            rule,
            scope_ref=scope_ref,
            direction="drop",
            actual=20.0,
            bucket=_FLIP_DROP_BUCKET,
        )
        _buffer_scope(
            session,
            config,
            destination,
            rule,
            scope_ref=scope_ref,
            direction="spike",
            actual=400.0,
            bucket=_FLIP_SPIKE_BUCKET,
        )

        rows = _buffered(session)
        assert [row.direction for row in rows] == ["drop", "spike"], (
            "a scope that flipped inside one window is two incidents and holds "
            "two buffered lines; folding them loses one of them"
        )
        assert [row.actual_count for row in rows] == [20.0, 400.0]
        assert [to_utc(row.bucket) for row in rows] == [_FLIP_DROP_BUCKET, _FLIP_SPIKE_BUCKET], (
            "the later collection must not rewrite the earlier incident's row"
        )
        assert [row.observation_count for row in rows] == [1, 1], (
            "each was seen once; a bumped count here would mean the spike had "
            "upserted onto the drop"
        )


def test_each_buffered_direction_carries_its_own_incident_handle(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """One row, one ``correlation_group_id`` — the mechanical reason the pair cannot fold.

    Red if ``direction`` leaves ``_correlation_group_id``'s hashed string: the
    two handles collapse to one value and the inequality fails. Red if it leaves
    the buffer key instead: one row survives and the per-direction lookup raises
    instead of returning a row.

    The column assertions are the argument itself. ``correlation_group_id`` is a
    single NOT NULL column, so a folded row would have to name one of two
    incidents and abandon the other — there is nowhere to put the second. And
    two Inbox cards already exist by the time the digest is assembled, because
    ``_buffer_pending_items`` touches one per handle; folding the rows would
    leave one of those cards holding a decision no delivery could honour, which
    is precisely the earlier project-global scope failure one level down.
    """
    from tripl.models.alert_correlation_state import AlertCorrelationState

    with sync_session_factory() as session:
        config, destination, rule = _seed(session)
        scope_ref = str(uuid.uuid4())

        drop_handle = _buffer_scope(
            session,
            config,
            destination,
            rule,
            scope_ref=scope_ref,
            direction="drop",
            actual=20.0,
            bucket=_FLIP_DROP_BUCKET,
        )
        spike_handle = _buffer_scope(
            session,
            config,
            destination,
            rule,
            scope_ref=scope_ref,
            direction="spike",
            actual=400.0,
            bucket=_FLIP_SPIKE_BUCKET,
        )

        assert drop_handle != spike_handle, (
            "one scope firing both ways is TWO incidents; equal handles would "
            "make the Inbox unable to tell them apart"
        )

        column = AlertPendingItem.__table__.c.correlation_group_id
        assert not column.nullable and len(column.base_columns) == 1, (
            "a single non-null column can name exactly one incident, which is "
            "why one row cannot stand for two directions"
        )

        by_direction = {row.direction: row for row in _buffered(session)}
        assert by_direction["drop"].correlation_group_id == drop_handle
        assert by_direction["spike"].correlation_group_id == spike_handle

        cards = {
            state.correlation_group_id
            for state in session.execute(select(AlertCorrelationState)).scalars()
        }
        assert cards == {drop_handle, spike_handle}, (
            "the buffer opens one Inbox card per incident while it waits; "
            "two cards over one folded row is a decision the digest cannot honour"
        )


def test_acknowledging_one_direction_leaves_the_other_deliverable(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consequence that makes folding the pair unsafe, end to end.

    An operator acknowledges the 03:00 drop. The 11:00 spike is a different
    incident and must still arrive. ``_build_digest`` filters on the handle the
    ROW carries, so this only works while the two rows carry different handles.

    Red under the fold, whichever handle the surviving row keeps: with the
    acknowledged one it is suppressed and the digest delivers NOTHING, so the
    ``deliveries == 1`` and one-item assertions fail; with the other, the
    acknowledgement is ignored and ``direction == "spike"`` fails because the
    drop is what ships. The sibling-delete remedy fails here too — it destroys
    the drop before the operator has anything to acknowledge.
    """
    from tripl.models.alert_correlation_state import AlertCorrelationState

    with sync_session_factory() as session:
        config, destination, rule = _seed(session)
        _put_on_a_cadence(session, destination)
        scope_ref = str(uuid.uuid4())

        drop_handle = _buffer_scope(
            session,
            config,
            destination,
            rule,
            scope_ref=scope_ref,
            direction="drop",
            actual=20.0,
            bucket=_FLIP_DROP_BUCKET,
        )
        _buffer_scope(
            session,
            config,
            destination,
            rule,
            scope_ref=scope_ref,
            direction="spike",
            actual=400.0,
            bucket=_FLIP_SPIKE_BUCKET,
        )

        acknowledged = session.execute(
            select(AlertCorrelationState).where(
                AlertCorrelationState.correlation_group_id == drop_handle
            )
        ).scalar_one()
        acknowledged.status = "acknowledged"
        session.commit()

    result, _enqueued = _run_flush(monkeypatch, sync_session_factory)

    assert result["deliveries"] == 1, (
        "acknowledging the drop must not silence the spike — they are two incidents"
    )
    with sync_session_factory() as session:
        items = session.execute(select(AlertDeliveryItem)).scalars().all()
        assert len(items) == 1
        assert items[0].direction == "spike"
        assert items[0].actual_count == 400.0


def test_a_scope_that_keeps_firing_the_same_way_still_collapses_onto_one_line(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The half of the promise that IS true, and that the correction must not cost.

    Red if anyone "fixes" the flip by widening the key — adding ``bucket`` to
    ``_PENDING_ITEM_CONFLICT_KEYS`` and the constraint, or turning the upsert
    back into a plain insert. The second drop would then insert beside the
    first, and a scope broken all day would be back to twenty-four lines, which
    is the thing the buffer exists to prevent.

    It also pins what "aggregate up to the moment it is sent" means for the
    surviving row: the LATEST numbers and bucket, with ``observation_count``
    carrying how many collections re-offered it.
    """
    with sync_session_factory() as session:
        config, destination, rule = _seed(session)
        scope_ref = str(uuid.uuid4())

        for actual, bucket in ((20.0, _FLIP_DROP_BUCKET), (5.0, _FLIP_SPIKE_BUCKET)):
            _buffer_scope(
                session,
                config,
                destination,
                rule,
                scope_ref=scope_ref,
                direction="drop",
                actual=actual,
                bucket=bucket,
            )

        rows = _buffered(session)
        assert len(rows) == 1, "a scope re-firing the SAME way still occupies one line"
        assert rows[0].actual_count == 5.0
        assert to_utc(rows[0].bucket) == _FLIP_SPIKE_BUCKET
        assert rows[0].observation_count == 2


def test_nothing_prunes_a_buffered_row_when_its_scope_falls_quiet(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The digest's roster is "what fired in this window", not "what is broken now".

    This is the sentence that makes the flipped drop unremarkable rather than
    stale, and it is now carried by ``_buffer_pending_items`` and by
    ``alert_flush``'s module docstring: a scope that fired once and went quiet
    keeps its line until its digest ships, so the flipped drop is no staler than
    a line the design already accepts everywhere.

    Red if someone adds a "prune the scopes that stopped firing" pass to the
    collection path — the shape the filed remedy generalises to, and one that
    would silently destroy alerts the operator never saw. The only things that
    may remove a buffered row are the flush's own claim, the 14-day sweep and
    disabling the destination.
    """
    with sync_session_factory() as session:
        config, destination, rule = _seed(session)
        scope_ref = str(uuid.uuid4())

        _buffer_scope(
            session,
            config,
            destination,
            rule,
            scope_ref=scope_ref,
            direction="drop",
            actual=20.0,
            bucket=_FLIP_DROP_BUCKET,
        )

        # A later collection in which this scope produces no anomaly at all —
        # the "it stopped firing" case, driven through the real entry point.
        assert metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None) == []
        session.commit()

        rows = _buffered(session)
        assert len(rows) == 1, "a quiet scope keeps its buffered line until the digest ships"
        assert rows[0].actual_count == 20.0


def test_the_buffer_key_is_the_incident_key_plus_the_destination() -> None:
    """Three files' prose claims this identity; this is the thing that checks it.

    ``_correlation_group_id``'s five parameters plus ``destination_id`` must be
    exactly ``uq_alert_pending_item_scope``'s columns, and the metric partial
    index must be that list minus the config a metric scope stores NULL in.

    Red if ``direction`` is dropped from the constraint, from
    ``_PENDING_ITEM_CONFLICT_KEYS``, or from the mint's signature. Red too if
    the constraint and the conflict target merely stop agreeing with each
    other: the upsert names one as its ON CONFLICT target while the model
    declares the other, and that divergence is an IntegrityError in production
    and silence here.
    """
    minted = {
        name
        for name, parameter in inspect.signature(
            metrics_dispatch._correlation_group_id
        ).parameters.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
    }
    assert minted == {"scan_config_id", "rule_id", "scope_type", "scope_ref", "direction"}, (
        "the incident key moved; the buffer's key and this guard both describe it"
    )

    constraint = next(
        c
        for c in AlertPendingItem.__table__.constraints
        if getattr(c, "name", None) == "uq_alert_pending_item_scope"
    )
    assert {column.name for column in constraint.columns} == minted | {"destination_id"}
    assert set(metrics_dispatch._PENDING_ITEM_CONFLICT_KEYS) == {
        column.name for column in constraint.columns
    }, "the ON CONFLICT target and the constraint have to stay the same list"

    partial = next(
        index
        for index in AlertPendingItem.__table__.indexes
        if index.name == "uq_alert_pending_item_metric_scope"
    )
    assert {column.name for column in partial.columns} == (minted | {"destination_id"}) - {
        "scan_config_id"
    }
    assert set(metrics_dispatch._PENDING_ITEM_METRIC_CONFLICT_KEYS) == {
        column.name for column in partial.columns
    }


def _comment_block_containing(source: str, anchor: str) -> str:
    """The whole run of ``#`` lines that contains ``anchor``.

    Anchored on a phrase that survives the correction, so the guard below fails
    LOUDLY if the prose it polices is moved or renamed rather than quietly
    passing over nothing.
    """
    lines = source.splitlines()
    hits = [index for index, line in enumerate(lines) if anchor in line]
    assert len(hits) == 1, f"expected exactly one comment to anchor on {anchor!r}, found {hits}"
    start = end = hits[0]
    while start > 0 and lines[start - 1].strip().startswith("#"):
        start -= 1
    while end + 1 < len(lines) and lines[end + 1].strip().startswith("#"):
        end += 1
    return "\n".join(lines[start : end + 1])


def test_the_four_collapse_claims_all_say_per_direction() -> None:
    """The one-line promise is restated in four places and they move together.

    This is the defect that was actually found: the CODE was right and
    the PROSE was wrong, in four files at once, each saying a scope occupies one
    line without the qualifier that makes it true. That is not cosmetic here —
    those paragraphs are the argument the next reader uses to decide whether
    ``direction`` belongs in the key, and the filed issue, which proposed two
    remedies that would each have broken the Inbox, is what happens when
    someone reads them and believes them.

    Each span below is the paragraph making the collapse claim. Each must still
    name ``direction``; none of the four did before. Reverting any one of them
    turns this red on that span alone.
    """
    from tripl.worker.tasks import alert_flush

    dispatch_source = Path(metrics_dispatch.__file__).read_text(encoding="utf-8")
    model_source = Path(
        inspect.getfile(AlertPendingItem)  # models/alert_pending_item.py
    ).read_text(encoding="utf-8")

    buffer_doc = metrics_dispatch._buffer_pending_items.__doc__ or ""
    cadence_comment = _comment_block_containing(
        dispatch_source, "collapses a scope that re-fires all day"
    )
    table_args_comment = _comment_block_containing(model_source, "collapses into ONE")
    flush_doc = alert_flush.__doc__ or ""
    sending_paragraph = next(
        paragraph
        for paragraph in flush_doc.split("\n\n")
        if "up to the moment of sending" in paragraph
    )

    spans = {
        "dispatch._buffer_pending_items docstring": buffer_doc,
        "dispatch cadence comment": cadence_comment,
        "AlertPendingItem.__table_args__ comment": table_args_comment,
        "alert_flush module docstring": sending_paragraph,
    }
    for label, span in spans.items():
        assert span.strip(), f"{label} no longer resolves; this guard guards nothing"
        assert "direction" in span.lower(), (
            f"{label} promises a collapse without saying it is PER DIRECTION — "
            "which is the false sentence that was reported"
        )
