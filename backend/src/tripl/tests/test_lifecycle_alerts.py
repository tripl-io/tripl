"""The "Event lifecycle" alert family (GH #258).

The daily sunset watch stores open ``lifecycle_findings``; each open finding is
ONE ``lifecycle`` alert candidate which

* only a rule with ``include_lifecycle`` receives (SAFE OFF), whatever its
  direction or volume thresholds say;
* is emitted by EVERY scan config of the project, and its ``AlertRuleState``
  lives in the project-global partition (``scan_config_id`` NULL, like a
  catalog metric's), so a multi-scan project alerts once per finding — and
  adding or removing a scan config does not re-send it; the ordinary cooldown
  machinery makes one finding one alert, not one per collection;
* hangs on the DEPRECATED event (``event_id``) and names a silent successor
  only in its drift columns;
* renders "Sunset overdue: <event> still receives 1,240/day, sunset 2026-09-01"
  or "Successor silent: <event> received no events in 7 days";
* closes its state when the finding resolves.

Sync sqlite, one file per test, like ``test_source_freshness_alerts.py``.
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

from tripl.alert_templates import (
    LIFECYCLE_KIND_SUCCESSOR_SILENT,
    LIFECYCLE_KIND_SUNSET_OVERDUE,
    DriftLineFacts,
    alert_scope_label,
    build_drift_line,
    lifecycle_line,
)
from tripl.alerting_matching import (
    SCOPE_LIFECYCLE,
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
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.lifecycle_finding import LifecycleFinding
from tripl.models.plan_branch import BranchKind, BranchStatus, PlanBranch
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.worker.tasks import alerts as alerts_task
from tripl.worker.tasks.metrics import dispatch as metrics_dispatch
from tripl.worker.tasks.metrics.lifecycle_alerts import (
    _get_lifecycle_candidates,
    _lifecycle_detail,
    format_per_day,
    lifecycle_scope_ref,
)

_SUNSET = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'lifecycle.db'}")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


# --- rendering ----------------------------------------------------------------


def test_sunset_overdue_line_matches_the_issue_wording() -> None:
    detail = _lifecycle_detail(
        kind=LIFECYCLE_KIND_SUNSET_OVERDUE,
        event_name="signup",
        volume_24h=1240,
        sunset_at=_SUNSET,
    )
    facts = DriftLineFacts(
        scope_type=SCOPE_LIFECYCLE,
        drift_type=LIFECYCLE_KIND_SUNSET_OVERDUE,
        drift_field="signup",
        sample_value=detail,
    )
    assert lifecycle_line(facts) == (
        "Sunset overdue: signup still receives 1,240/day, sunset 2026-09-01"
    )
    assert build_drift_line(facts) == (
        "\n  Sunset overdue: signup still receives 1,240/day, sunset 2026-09-01"
    )


def test_successor_silent_line_matches_the_issue_wording() -> None:
    detail = _lifecycle_detail(
        kind=LIFECYCLE_KIND_SUCCESSOR_SILENT,
        event_name="signup_v2",
        volume_24h=None,
        sunset_at=None,
    )
    facts = DriftLineFacts(
        scope_type=SCOPE_LIFECYCLE,
        drift_type=LIFECYCLE_KIND_SUCCESSOR_SILENT,
        drift_field="signup_v2",
        sample_value=detail,
    )
    assert lifecycle_line(facts) == "Successor silent: signup_v2 received no events in 7 days"


def test_lifecycle_scope_label_and_helpers() -> None:
    assert alert_scope_label(SCOPE_LIFECYCLE) == "Event lifecycle"
    assert format_per_day(1240.4) == "1,240"
    assert format_per_day(None) == "0"
    event_id = uuid.uuid4()
    ref = lifecycle_scope_ref(LIFECYCLE_KIND_SUCCESSOR_SILENT, event_id)
    assert ref == f"successor_silent:{event_id.hex}"
    # Fits AlertRuleState.scope_ref / AlertDeliveryItem.scope_ref (String(64)).
    assert len(ref) <= 64


# --- matching -----------------------------------------------------------------


def _candidate(**overrides: object) -> DriftAlertCandidate:
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "scan_config_id": uuid.uuid4(),
        "scope_type": SCOPE_LIFECYCLE,
        "scope_ref": lifecycle_scope_ref(LIFECYCLE_KIND_SUNSET_OVERDUE, uuid.uuid4()),
        "event_id": uuid.uuid4(),
        "event_type_id": None,
        "bucket": datetime(2026, 9, 26, 3, tzinfo=UTC),
        "direction": "spike",
        "actual_count": 1240.0,
        "expected_count": 0.0,
        "drift_field": "signup",
        "drift_type": LIFECYCLE_KIND_SUNSET_OVERDUE,
        "sample_value": "signup still receives 1,240/day, sunset 2026-09-01",
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
        "include_source_freshness": False,
        "include_lifecycle": True,
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


def test_the_rule_gate_is_include_lifecycle() -> None:
    candidate = _candidate()
    assert rule_matches_anomaly(_rule(include_lifecycle=True), candidate)
    assert not rule_matches_anomaly(_rule(include_lifecycle=False), candidate)


def test_lifecycle_ignores_direction_and_volume_thresholds() -> None:
    """A rule that only wants drops, with a high floor, still gets a sunset overdue."""
    rule = _rule(
        notify_on_spike=False,
        notify_on_drop=True,
        min_expected_count=1_000_000.0,
        min_percent_delta=10_000.0,
    )
    assert rule_matches_anomaly(rule, _candidate(direction="spike"))
    assert rule_matches_anomaly(rule, _candidate(direction="drop", actual_count=0.0))


def test_a_scan_bound_rule_still_receives_lifecycle_findings() -> None:
    """The scan that emitted it is a delivery detail, not a property of the finding."""
    rule = _rule(scan_config_id=uuid.uuid4())
    assert rule_matches_anomaly(rule, _candidate())


# --- candidates + dispatch ----------------------------------------------------


def _scan_config(
    project_id: uuid.UUID, *, name: str, scheduled: bool = True
) -> tuple[DataSource, ScanConfig]:
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
        name=name,
        base_query="SELECT time, event_name FROM events",
        time_column="time" if scheduled else None,
        cardinality_threshold=100,
        interval="1h" if scheduled else None,
    )
    return data_source, config


def _seed(
    session: Session,
    *,
    include_lifecycle: bool = True,
    scan_count: int = 1,
    kind: str = LIFECYCLE_KIND_SUNSET_OVERDUE,
) -> tuple[list[ScanConfig], Event, Event, LifecycleFinding]:
    """A project with a deprecated ``signup`` superseded by ``signup_v2``, one open finding."""
    project = Project(
        id=uuid.uuid4(),
        name="Lifecycle",
        slug=f"lifecycle-{uuid.uuid4().hex[:8]}",
        description="",
        timezone="UTC",
    )
    branch = PlanBranch(
        id=uuid.uuid4(),
        project_id=project.id,
        name="main",
        kind=BranchKind.main.value,
        status=BranchStatus.merged.value,
    )
    event_type = EventType(
        id=uuid.uuid4(),
        project_id=project.id,
        branch_id=branch.id,
        name="auth",
        display_name="Auth",
        description="",
    )
    successor = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        branch_id=branch.id,
        event_type_id=event_type.id,
        name="signup_v2",
        status="live",
    )
    deprecated = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        branch_id=branch.id,
        event_type_id=event_type.id,
        name="signup",
        status="deprecated",
        sunset_at=_SUNSET,
        superseded_by_event_id=successor.id,
    )
    pairs = [_scan_config(project.id, name=f"Scan {index}") for index in range(scan_count)]
    data_sources = [data_source for data_source, _config in pairs]
    configs = [config for _data_source, config in pairs]
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
        include_lifecycle=include_lifecycle,
        notify_on_spike=True,
        notify_on_drop=True,
        min_percent_delta=100,
        min_absolute_delta=0,
        min_expected_count=10,
        cooldown_minutes=1440,
    )
    now = datetime.now(UTC).replace(microsecond=0)
    finding = LifecycleFinding(
        project_id=project.id,
        event_id=deprecated.id,
        kind=kind,
        related_event_id=successor.id if kind == LIFECYCLE_KIND_SUCCESSOR_SILENT else None,
        first_seen_at=now - timedelta(hours=2),
        last_seen_at=now,
        resolved_at=None,
        volume_24h=1240 if kind == LIFECYCLE_KIND_SUNSET_OVERDUE else None,
        successor_volume_7d=0 if kind == LIFECYCLE_KIND_SUCCESSOR_SILENT else None,
    )
    session.add_all([project, branch, event_type, *data_sources])
    session.flush()
    session.add_all([successor])
    session.flush()
    session.add_all([deprecated, *configs, destination, rule])
    session.flush()
    session.add(finding)
    session.commit()
    return configs, deprecated, successor, finding


def _collect(factory: sessionmaker[Session], config_id: uuid.UUID) -> list[uuid.UUID]:
    with factory() as session:
        config = session.get(ScanConfig, config_id)
        assert config is not None
        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()
        return delivery_ids


def _mark_sent(factory: sessionmaker[Session], delivery_ids: list[uuid.UUID]) -> None:
    with factory() as session:
        for delivery_id in delivery_ids:
            delivery = session.get(AlertDelivery, delivery_id)
            assert delivery is not None
            delivery.sent_at = datetime.now(UTC)
            alerts_task._stamp_rule_state(session, delivery)
        session.commit()


def _lifecycle_items(session: Session) -> list[AlertDeliveryItem]:
    return list(
        session.execute(
            select(AlertDeliveryItem).where(AlertDeliveryItem.scope_type == SCOPE_LIFECYCLE)
        ).scalars()
    )


def test_one_candidate_per_open_finding(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        configs, deprecated, _successor, finding = _seed(session)
        candidates = _get_lifecycle_candidates(session, configs[0])

    key = (SCOPE_LIFECYCLE, lifecycle_scope_ref(LIFECYCLE_KIND_SUNSET_OVERDUE, deprecated.id))
    assert list(candidates) == [key]
    candidate = candidates[key]
    assert candidate.id == finding.id
    assert candidate.event_id == deprecated.id
    assert candidate.drift_type == LIFECYCLE_KIND_SUNSET_OVERDUE
    assert candidate.drift_field == "signup"
    assert candidate.actual_count == 1240.0
    assert candidate.sample_value == "signup still receives 1,240/day, sunset 2026-09-01"


def test_a_silent_successor_finding_names_the_successor(
    session_factory: sessionmaker[Session],
) -> None:
    """Hangs on the deprecated event; only the message names the silent successor."""
    with session_factory() as session:
        configs, _deprecated, successor, _finding = _seed(
            session, kind=LIFECYCLE_KIND_SUCCESSOR_SILENT
        )
        (candidate,) = _get_lifecycle_candidates(session, configs[0]).values()

    assert candidate.event_id == _deprecated.id
    assert candidate.event_id != successor.id
    assert candidate.drift_type == LIFECYCLE_KIND_SUCCESSOR_SILENT
    assert candidate.drift_field == "signup_v2"
    assert candidate.direction == "drop"
    assert candidate.sample_value == "signup_v2 received no events in 7 days"


def test_resolved_findings_are_not_candidates(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        configs, _deprecated, _successor, finding = _seed(session)
        row = session.get(LifecycleFinding, finding.id)
        assert row is not None
        row.resolved_at = datetime.now(UTC)
        session.commit()
        assert _get_lifecycle_candidates(session, configs[0]) == {}


def test_every_config_emits_the_same_project_global_candidates(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        configs, _deprecated, _successor, finding = _seed(session, scan_count=3)
        per_config = [_get_lifecycle_candidates(session, config) for config in configs]

    assert all(len(candidates) == 1 for candidates in per_config)
    keys = {key for candidates in per_config for key in candidates}
    assert len(keys) == 1
    for candidates in per_config:
        (candidate,) = candidates.values()
        assert candidate.id == finding.id
        # Project-global: no scan config on the candidate itself.
        assert candidate.scan_config_id is None


def test_dispatch_sends_one_alert_per_finding(session_factory: sessionmaker[Session]) -> None:
    """Three scans, four collections each: ONE lifecycle item."""
    with session_factory() as session:
        configs, deprecated, _successor, _finding = _seed(session, scan_count=3)

    minted: list[uuid.UUID] = []
    for _ in range(4):
        for config in configs:
            delivery_ids = _collect(session_factory, config.id)
            minted.extend(delivery_ids)
            _mark_sent(session_factory, delivery_ids)

    with session_factory() as session:
        items = _lifecycle_items(session)
        assert len(items) == 1
        (item,) = items
        assert item.scope_name == "signup"
        assert item.event_id == deprecated.id
        assert item.drift_type == LIFECYCLE_KIND_SUNSET_OVERDUE
        assert item.drift_field == "signup"
        assert item.sample_value == "signup still receives 1,240/day, sunset 2026-09-01"
        # One state, in the project-global partition.
        states = list(
            session.execute(
                select(AlertRuleState).where(AlertRuleState.scope_type == SCOPE_LIFECYCLE)
            ).scalars()
        )
        assert len(states) == 1
        assert states[0].scan_config_id is None
        assert states[0].last_notified_at is not None


def test_a_new_scan_config_does_not_re_alert(session_factory: sessionmaker[Session]) -> None:
    """Two configs, one alert — and a config added later changes nothing.

    Under the old "anchor config" scheme the state was keyed on the lowest
    config id, so a new config sorting first moved the anchor, found no state
    under its own id and sent the finding a second time.
    """
    with session_factory() as session:
        configs, _deprecated, _successor, _finding = _seed(session, scan_count=2)
        project_id = configs[0].project_id

    for config in configs:
        delivery_ids = _collect(session_factory, config.id)
        _mark_sent(session_factory, delivery_ids)
    with session_factory() as session:
        assert len(_lifecycle_items(session)) == 1

    # A config whose id sorts before every existing one.
    with session_factory() as session:
        data_source, newcomer = _scan_config(project_id, name="Newcomer")
        newcomer.id = uuid.UUID(int=0)
        session.add(data_source)
        session.flush()
        session.add(newcomer)
        session.commit()

    for config_id in [uuid.UUID(int=0), *(config.id for config in configs)]:
        delivery_ids = _collect(session_factory, config_id)
        _mark_sent(session_factory, delivery_ids)

    with session_factory() as session:
        assert len(_lifecycle_items(session)) == 1
        states = list(
            session.execute(
                select(AlertRuleState).where(AlertRuleState.scope_type == SCOPE_LIFECYCLE)
            ).scalars()
        )
        assert len(states) == 1
        assert states[0].scan_config_id is None
        assert states[0].is_active is True


def test_rules_without_include_lifecycle_get_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        configs, _deprecated, _successor, _finding = _seed(session, include_lifecycle=False)

    _collect(session_factory, configs[0].id)
    with session_factory() as session:
        assert _lifecycle_items(session) == []


def test_a_resolved_finding_closes_its_state(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        configs, _deprecated, _successor, finding = _seed(session)

    delivery_ids = _collect(session_factory, configs[0].id)
    assert len(delivery_ids) == 1
    _mark_sent(session_factory, delivery_ids)

    with session_factory() as session:
        row = session.get(LifecycleFinding, finding.id)
        assert row is not None
        row.resolved_at = datetime.now(UTC)
        session.commit()

    _collect(session_factory, configs[0].id)
    with session_factory() as session:
        state = session.execute(
            select(AlertRuleState).where(AlertRuleState.scope_type == SCOPE_LIFECYCLE)
        ).scalar_one()
        assert state.is_active is False
        assert state.closed_at is not None


def test_a_broken_findings_query_never_sinks_the_dispatch(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tripl.worker.tasks.metrics import lifecycle_alerts

    def boom(session: Session, config: ScanConfig) -> dict[object, object]:
        raise RuntimeError("findings table missing")

    monkeypatch.setattr(lifecycle_alerts, "_load_lifecycle_candidates", boom)
    with session_factory() as session:
        configs, _deprecated, _successor, _finding = _seed(session)
        assert _get_lifecycle_candidates(session, configs[0]) == {}


# --- API ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alert_rule_api_round_trips_include_lifecycle(client: AsyncClient) -> None:
    project = await client.post(
        "/api/v1/projects",
        json={"name": "Lifecycle rules", "slug": "lifecycle-rules", "description": ""},
    )
    assert project.status_code == 201, project.text
    destination = await client.post(
        "/api/v1/projects/lifecycle-rules/alert-destinations",
        json={
            "type": "slack",
            "name": "Main Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T000/B000/XXX",
        },
    )
    assert destination.status_code == 201, destination.text
    destination_id = destination.json()["id"]
    rules_url = f"/api/v1/projects/lifecycle-rules/alert-destinations/{destination_id}/rules"

    default_rule = await client.post(rules_url, json={"name": "Default"})
    assert default_rule.status_code == 201, default_rule.text
    # SAFE OFF: a rule that never asked for lifecycle findings gets none.
    assert default_rule.json()["include_lifecycle"] is False

    created = await client.post(rules_url, json={"name": "Lifecycle", "include_lifecycle": True})
    assert created.status_code == 201, created.text
    assert created.json()["include_lifecycle"] is True

    patched = await client.patch(
        f"{rules_url}/{created.json()['id']}", json={"include_lifecycle": False}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["include_lifecycle"] is False

    nulled = await client.patch(
        f"{rules_url}/{created.json()['id']}", json={"include_lifecycle": None}
    )
    assert nulled.status_code == 422
