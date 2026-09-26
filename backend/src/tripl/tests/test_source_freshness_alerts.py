"""The "Source freshness" alert family (issue #269).

A delayed warehouse load used to surface as a volume drop on every scope. The
alert family added here names the real problem instead: while a scan config is
late (its newest event is older than the allowed lag) or overdue (the scan has
not completed a collection in time) it contributes ONE ``source_freshness``
candidate, keyed on the scan config, which

* only a rule with ``include_source_freshness`` receives (SAFE OFF);
* goes through the ordinary per-scope ``AlertRuleState`` / cooldown machinery,
  so one delay produces one alert, not one per collection;
* renders "Data late: <scan> — newest event 7h ago (expected within 3h)".

Sync sqlite, one file per test, like ``test_batch4_cadence.py``: the worker
dispatch is sync and needs a real session.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.alert_templates import (
    DriftLineFacts,
    build_drift_line,
    get_default_items_template,
    render_alert_template,
)
from tripl.alerting_matching import (
    SCOPE_SOURCE_FRESHNESS,
    DriftAlertCandidate,
    rule_matches_anomaly,
)
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import AlertDriftType
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.worker.tasks import alerts as alerts_task
from tripl.worker.tasks.alerts_messages import _build_item_template_context
from tripl.worker.tasks.metrics import dispatch as metrics_dispatch
from tripl.worker.tasks.metrics import freshness_sweep
from tripl.worker.tasks.metrics.signals import _get_source_freshness_candidates

_SCAN_NAME = "Warehouse events"


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'freshness.db'}")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _seed(
    session: Session,
    *,
    include_source_freshness: bool = True,
    last_event_age: timedelta = timedelta(hours=7),
    last_collection_age: timedelta = timedelta(minutes=5),
    cooldown_minutes: int = 1440,
) -> tuple[ScanConfig, AlertRule]:
    """An hourly scan whose newest event is ``last_event_age`` old.

    The project settles ingestion for 120 minutes, so the late threshold is
    ``max(3 × 1h, 2h + 1h) = 3h`` — a 7h-old newest event is late, while the
    scan itself collected minutes ago and is not overdue.
    """
    now = _now()
    project = Project(
        id=uuid.uuid4(),
        name="Freshness",
        slug=f"freshness-{uuid.uuid4().hex[:8]}",
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
        name=_SCAN_NAME,
        base_query="SELECT time, event_name FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval="1h",
        last_event_at=now - last_event_age,
        last_collection_at=now - last_collection_age,
    )
    settings = ProjectAnomalySettings(
        project_id=project.id,
        anomaly_detection_enabled=True,
        sigma_threshold=3.0,
        min_expected_count=10,
        anomaly_ingestion_settling_minutes=120,
    )
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=project.id,
        type="slack",
        name="Slack",
        enabled=True,
        webhook_url_encrypted="secret",
    )
    rule = AlertRule(
        id=uuid.uuid4(),
        destination_id=destination.id,
        name="Everything",
        enabled=True,
        include_project_total=True,
        include_event_types=True,
        include_events=True,
        include_source_freshness=include_source_freshness,
        notify_on_spike=True,
        notify_on_drop=True,
        # The measured volume defaults. A delay must not be gated on them:
        # its actual/expected are hours, not counts.
        min_percent_delta=100,
        min_absolute_delta=0,
        min_expected_count=10,
        cooldown_minutes=cooldown_minutes,
    )
    session.add_all([project, data_source, config, settings, destination, rule])
    session.commit()
    return config, rule


def _collect(factory: sessionmaker[Session], config_id: uuid.UUID) -> list[uuid.UUID]:
    """One collection's alert pass."""
    with factory() as session:
        config = session.get(ScanConfig, config_id)
        assert config is not None
        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()
        return delivery_ids


def _mark_sent(factory: sessionmaker[Session], delivery_ids: list[uuid.UUID]) -> None:
    """What a successful send does: stamp ``last_notified_at`` on the scope's state."""
    with factory() as session:
        for delivery_id in delivery_ids:
            delivery = session.get(AlertDelivery, delivery_id)
            assert delivery is not None
            delivery.sent_at = datetime.now(UTC)
            alerts_task._stamp_rule_state(session, delivery)
        session.commit()


def _freshness_items(session: Session) -> list[AlertDeliveryItem]:
    return list(
        session.execute(
            select(AlertDeliveryItem).where(AlertDeliveryItem.scope_type == SCOPE_SOURCE_FRESHNESS)
        ).scalars()
    )


# --- matching -----------------------------------------------------------------


def _candidate(**overrides: object) -> DriftAlertCandidate:
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "scan_config_id": uuid.uuid4(),
        "scope_type": SCOPE_SOURCE_FRESHNESS,
        "scope_ref": str(uuid.uuid4()),
        "event_id": None,
        "event_type_id": None,
        "bucket": datetime(2026, 9, 26, 3, tzinfo=UTC),
        "direction": "drop",
        "actual_count": 7.0,
        "expected_count": 3.0,
        "drift_field": _SCAN_NAME,
        "drift_type": AlertDriftType.source_late.value,
        "sample_value": "newest event 7h ago (expected within 3h)",
    }
    fields.update(overrides)
    return DriftAlertCandidate(**fields)  # type: ignore[arg-type]


def _rule(**overrides: object) -> AlertRule:
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "destination_id": uuid.uuid4(),
        "scan_config_id": None,
        "name": "Rule",
        "enabled": True,
        "include_project_total": True,
        "include_event_types": True,
        "include_events": True,
        "include_schema_drifts": False,
        "include_distribution_drifts": False,
        "include_variable_value_drifts": False,
        "include_release_regressions": False,
        "include_metrics": False,
        "include_source_freshness": True,
        "notify_on_spike": True,
        "notify_on_drop": True,
        "min_percent_delta": 100.0,
        "min_absolute_delta": 0.0,
        "min_expected_count": 10.0,
        "cooldown_minutes": 60,
        "message_format": "plain",
    }
    fields.update(overrides)
    rule = AlertRule(**fields)
    rule.filters = []
    return rule


def test_the_rule_gate_is_include_source_freshness() -> None:
    candidate = _candidate()
    assert rule_matches_anomaly(_rule(include_source_freshness=True), candidate)
    assert not rule_matches_anomaly(_rule(include_source_freshness=False), candidate)


def test_a_delay_bypasses_the_volume_thresholds() -> None:
    """Hours are not counts: min_expected_count=10 must not silence a 3h allowance."""
    candidate = _candidate(actual_count=3.5, expected_count=3.0)
    assert rule_matches_anomaly(
        _rule(min_expected_count=10.0, min_percent_delta=100.0, min_absolute_delta=5.0),
        candidate,
    )


def test_a_delay_is_a_drop_and_honours_notify_on_drop() -> None:
    assert not rule_matches_anomaly(_rule(notify_on_drop=False), _candidate())


# --- rendering ----------------------------------------------------------------


def test_the_drift_line_names_the_scan_and_the_delay() -> None:
    line = build_drift_line(
        DriftLineFacts(
            scope_type=SCOPE_SOURCE_FRESHNESS,
            drift_type=AlertDriftType.source_late.value,
            drift_field=_SCAN_NAME,
            sample_value="newest event 7h ago (expected within 3h)",
        )
    )
    assert line == (f"\n  Data late: {_SCAN_NAME} — newest event 7h ago (expected within 3h)")


def test_an_overdue_scan_reads_as_overdue() -> None:
    line = build_drift_line(
        DriftLineFacts(
            scope_type=SCOPE_SOURCE_FRESHNESS,
            drift_type=AlertDriftType.source_overdue.value,
            drift_field=_SCAN_NAME,
            sample_value="last collection 5h ago (expected every 1h)",
        )
    )
    assert line.startswith(f"\n  Scan overdue: {_SCAN_NAME} — ")


# --- candidates ---------------------------------------------------------------


def test_a_late_scan_yields_exactly_one_candidate(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        config, _rule_row = _seed(session)
        candidates = _get_source_freshness_candidates(session, config)

    assert list(candidates) == [(SCOPE_SOURCE_FRESHNESS, str(config.id))]
    candidate = candidates[(SCOPE_SOURCE_FRESHNESS, str(config.id))]
    assert candidate.direction == "drop"
    assert candidate.scan_config_id == config.id
    assert candidate.drift_type == AlertDriftType.source_late.value
    assert candidate.drift_field == _SCAN_NAME
    assert candidate.sample_value == "newest event 7h ago (expected within 3h)"
    assert candidate.actual_count == pytest.approx(7.0, abs=0.1)
    assert candidate.expected_count == 3.0


def test_a_fresh_scan_yields_no_candidate(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        config, _rule_row = _seed(session, last_event_age=timedelta(minutes=30))
        assert _get_source_freshness_candidates(session, config) == {}


def test_a_manual_scan_yields_no_candidate(session_factory: sessionmaker[Session]) -> None:
    """No interval means no expectation, so freshness is ``unknown``."""
    with session_factory() as session:
        config, _rule_row = _seed(session)
        config.interval = None
        session.commit()
        assert _get_source_freshness_candidates(session, config) == {}


# --- dispatch -----------------------------------------------------------------


def test_one_delay_produces_one_alert(session_factory: sessionmaker[Session]) -> None:
    """The issue's "done when": a delayed source pages once, not per collection."""
    with session_factory() as session:
        config, rule = _seed(session)

    first = _collect(session_factory, config.id)
    assert len(first) == 1
    with session_factory() as session:
        items = _freshness_items(session)
        assert len(items) == 1
        item = items[0]
        assert item.scope_ref == str(config.id)
        assert item.scope_name == _SCAN_NAME
        assert item.direction == "drop"
        rendered = render_alert_template(
            get_default_items_template("plain"),
            _build_item_template_context(item, message_format="plain"),
        )
        assert f"Data late: {_SCAN_NAME} — newest event 7h ago" in rendered
        assert "(expected within 3h)" in rendered
    _mark_sent(session_factory, first)

    # The load is still delayed on the next collections: same stale bucket, so
    # the scope's state stays open and nothing new is sent.
    assert _collect(session_factory, config.id) == []
    assert _collect(session_factory, config.id) == []
    with session_factory() as session:
        assert len(_freshness_items(session)) == 1
        state = session.execute(
            select(AlertRuleState).where(
                AlertRuleState.rule_id == rule.id,
                AlertRuleState.scope_type == SCOPE_SOURCE_FRESHNESS,
            )
        ).scalar_one()
        assert state.is_active is True


def test_the_cooldown_dedups_a_trickle_of_late_data(
    session_factory: sessionmaker[Session],
) -> None:
    """A few late events move the stale bucket forward; the cooldown still holds."""
    with session_factory() as session:
        config, _rule_row = _seed(session, cooldown_minutes=1440)

    first = _collect(session_factory, config.id)
    assert len(first) == 1
    _mark_sent(session_factory, first)

    with session_factory() as session:
        stored = session.get(ScanConfig, config.id)
        assert stored is not None
        # Still late (5h > 3h), but on a newer bucket than the one notified.
        stored.last_event_at = _now() - timedelta(hours=5)
        session.commit()

    assert _collect(session_factory, config.id) == []


def test_recovery_closes_the_incident(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        config, rule = _seed(session)

    first = _collect(session_factory, config.id)
    assert len(first) == 1
    _mark_sent(session_factory, first)

    with session_factory() as session:
        stored = session.get(ScanConfig, config.id)
        assert stored is not None
        stored.last_event_at = _now() - timedelta(minutes=20)
        session.commit()

    assert _collect(session_factory, config.id) == []
    with session_factory() as session:
        state = session.execute(
            select(AlertRuleState).where(
                AlertRuleState.rule_id == rule.id,
                AlertRuleState.scope_type == SCOPE_SOURCE_FRESHNESS,
            )
        ).scalar_one()
        assert state.is_active is False


def test_a_rule_without_the_flag_stays_silent(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        config, _rule_row = _seed(session, include_source_freshness=False)

    assert _collect(session_factory, config.id) == []
    with session_factory() as session:
        assert _freshness_items(session) == []


# --- drift_type persistence ---------------------------------------------------


def test_the_freshness_drift_type_is_a_valid_alert_drift_type(
    session_factory: sessionmaker[Session],
) -> None:
    """The item row flushes: ``drift_type`` is an ``alert_drift_type`` member.

    The bare freshness status (``"late"``) is not one, and the Enum column's
    ``validate_strings`` rejects it on bind — on Postgres the INSERT fails on
    the native enum and takes the whole collection transaction with it.
    """
    with session_factory() as session:
        config, _rule_row = _seed(session)
        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.flush()
        assert len(delivery_ids) == 1
        session.commit()

    with session_factory() as session:
        (item,) = _freshness_items(session)
        assert item.drift_type == AlertDriftType.source_late.value


def test_repeated_collections_during_one_delay_mint_one_item(
    session_factory: sessionmaker[Session],
) -> None:
    """Across N collections of one delay, ``_prepare_alert_deliveries`` mints ONE item."""
    with session_factory() as session:
        config, _rule_row = _seed(session)

    minted: list[uuid.UUID] = []
    for _ in range(4):
        delivery_ids = _collect(session_factory, config.id)
        minted.extend(delivery_ids)
        _mark_sent(session_factory, delivery_ids)

    assert len(minted) == 1
    with session_factory() as session:
        items = _freshness_items(session)
        assert len(items) == 1
        assert items[0].delivery_id == minted[0]


# --- overdue sweep ------------------------------------------------------------


def _sweep(factory: sessionmaker[Session]) -> tuple[dict[str, int], list[uuid.UUID]]:
    with factory() as session:
        return freshness_sweep._sweep_overdue_sources(session)


def test_the_sweep_alerts_once_on_a_stopped_scan(
    session_factory: sessionmaker[Session],
) -> None:
    """No collection for 5h on an hourly scan: overdue, one alert, then quiet."""
    with session_factory() as session:
        config, _rule_row = _seed(
            session,
            last_event_age=timedelta(hours=6),
            last_collection_age=timedelta(hours=5),
        )

    summary, first = _sweep(session_factory)
    assert summary["overdue"] == 1
    assert len(first) == 1
    with session_factory() as session:
        (item,) = _freshness_items(session)
        assert item.drift_type == AlertDriftType.source_overdue.value
        assert item.scope_ref == str(config.id)
        assert item.sample_value is not None
        assert item.sample_value.startswith("last collection 5h ago")
    _mark_sent(session_factory, first)

    # Still stopped on the next ticks: the bucket (last_collection_at) has not
    # moved, so the open state sends nothing more.
    assert _sweep(session_factory)[1] == []
    assert _sweep(session_factory)[1] == []
    with session_factory() as session:
        assert len(_freshness_items(session)) == 1


def test_the_sweep_ignores_a_fresh_scan(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        _seed(session, last_event_age=timedelta(minutes=30))

    summary, delivery_ids = _sweep(session_factory)
    assert summary["checked"] == 1
    assert summary["overdue"] == 0
    assert delivery_ids == []
    with session_factory() as session:
        assert _freshness_items(session) == []


def test_the_sweep_leaves_a_late_scan_to_the_dispatch(
    session_factory: sessionmaker[Session],
) -> None:
    """Late (collecting, data old) is the per-run dispatch's; the sweep must not double it."""
    with session_factory() as session:
        _seed(session)

    assert _sweep(session_factory)[1] == []
    with session_factory() as session:
        assert _freshness_items(session) == []


def test_the_sweep_skips_projects_without_a_freshness_rule(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        _seed(
            session,
            include_source_freshness=False,
            last_collection_age=timedelta(hours=5),
        )

    summary, delivery_ids = _sweep(session_factory)
    assert summary["checked"] == 0
    assert delivery_ids == []
