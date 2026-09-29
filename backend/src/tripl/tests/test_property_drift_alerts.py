"""Property drift surfaced (F23, #306, tripl-1dz3.10).

A ``PropertyDrift`` row (new property, missing required property, type change)
reaches the same places a value drift does:

* alert candidates: one ``property_drift`` candidate per active row of the
  scan config, only for a rule with ``include_property_drifts`` (SAFE OFF),
  bypassing the volume thresholds; cooldown per row like value drift; the
  in-UI replay loads the same rows through the same mapping;
* the message: ``Missing required property ${plan}: on 40% of rows, required
  on 95%`` under the "Property drift" label, named ``<event>.<property>``;
* the bell: watchers of the event hear once per drift;
* the shared open counts (``_open_signals``): the project summary's
  ``open_property_drift_count`` and the health score's drifts component.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

import tripl.worker.celery_app  # noqa: F401  (import-order side effect, see test_batch4_replay)
from tripl.alert_templates import DriftLineFacts, alert_scope_label, build_drift_line
from tripl.alerting_matching import SCOPE_PROPERTY_DRIFT, rule_matches_anomaly
from tripl.alerting_property_drift import (
    property_drift_candidate,
    property_drift_sample,
    property_drift_scope_name,
)
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_filter import AlertRuleFilter
from tripl.models.data_source import DataSource
from tripl.models.event import Event, EventStatus
from tripl.models.event_type import EventType
from tripl.models.notification import Notification
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.property_drift import PropertyDrift, PropertyDriftKind
from tripl.models.scan_config import ScanConfig
from tripl.models.subscription import Subscription
from tripl.models.user import User
from tripl.models.variable import Variable
from tripl.services.health_score import EventHealthFacts, score_event
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import alerts as alerts_task
from tripl.worker.tasks import notification_producers
from tripl.worker.tasks.metrics import dispatch as metrics_dispatch
from tripl.worker.tasks.metrics.signals import _get_active_property_drift_candidates

# --- the mapping ------------------------------------------------------------------


def _drift(kind: PropertyDriftKind, detail: dict[str, Any], **overrides: Any) -> PropertyDrift:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "variable_id": uuid.uuid4(),
        "event_id": None if kind is PropertyDriftKind.type_change else uuid.uuid4(),
        "kind": kind.value,
        "detail": detail,
        "status": "open",
        "detected_at": datetime(2026, 10, 1, 9, tzinfo=UTC),
    }
    values.update(overrides)
    return PropertyDrift(**values)


def test_missing_required_candidate_carries_presence_against_threshold() -> None:
    drift = _drift(PropertyDriftKind.missing_required, {"presence_rate": 0.4, "threshold": 0.95})
    config_id = uuid.uuid4()
    candidate = property_drift_candidate(drift, variable_name="plan", scan_config_id=config_id)
    assert candidate.scope_type == SCOPE_PROPERTY_DRIFT
    assert candidate.scope_ref == str(drift.id)
    assert candidate.scan_config_id == config_id
    assert candidate.event_id == drift.event_id
    assert candidate.event_type_id is None
    assert candidate.direction == "spike"
    assert candidate.drift_field == "plan"
    assert candidate.drift_type == "missing_required"
    assert candidate.sample_value == "on 40% of rows, required on 95%"
    assert (candidate.actual_count, candidate.expected_count) == (40.0, 95.0)


def test_new_property_and_type_change_samples() -> None:
    assert (
        property_drift_sample("new_property", {"presence_rate": 0.125})
        == "on 12.5% of rows, not on the event's property list"
    )
    assert (
        property_drift_sample("type_change", {"observed_type": "number", "expected_type": "string"})
        == "observed number, typed string"
    )
    # An absent path reads as 0%, never as a crash.
    assert property_drift_sample("missing_required", {}) == "on 0% of rows, required on 0%"
    type_change = property_drift_candidate(
        _drift(PropertyDriftKind.type_change, {"observed_type": "number"}),
        variable_name="price",
        scan_config_id=None,
    )
    assert type_change.event_id is None
    assert (type_change.actual_count, type_change.expected_count) == (1.0, 0.0)


def test_scope_name_and_drift_line() -> None:
    assert property_drift_scope_name("signup", "plan") == "signup.plan"
    assert property_drift_scope_name(None, "price") == "All events.price"
    assert alert_scope_label(SCOPE_PROPERTY_DRIFT) == "Property drift"
    facts = DriftLineFacts(
        scope_type=SCOPE_PROPERTY_DRIFT,
        drift_field="plan",
        drift_type="missing_required",
        sample_value="on 40% of rows, required on 95%",
    )
    assert build_drift_line(facts) == (
        "\n  Missing required property ${plan}: on 40% of rows, required on 95%"
    )
    new = DriftLineFacts(
        scope_type=SCOPE_PROPERTY_DRIFT,
        drift_field="coupon",
        drift_type="new_property",
        sample_value=None,
    )
    assert build_drift_line(new) == "\n  New property ${coupon}"


# --- the rule gate -----------------------------------------------------------------


def _rule(**overrides: object) -> AlertRule:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "destination_id": uuid.uuid4(),
        "name": "Property drift",
        "enabled": True,
        "include_project_total": True,
        "include_event_types": True,
        "include_events": True,
        "notify_on_spike": True,
        "notify_on_drop": True,
        # Deliberately strict: the drift scopes bypass the volume thresholds.
        "min_percent_delta": 999,
        "min_absolute_delta": 999,
        "min_expected_count": 999,
        "cooldown_minutes": 60,
    }
    values.update(overrides)
    rule = AlertRule(**values)
    rule.filters = []
    return rule


def test_the_rule_gate_is_include_property_drifts() -> None:
    candidate = property_drift_candidate(
        _drift(PropertyDriftKind.new_property, {"presence_rate": 0.5}),
        variable_name="coupon",
        scan_config_id=uuid.uuid4(),
    )
    assert rule_matches_anomaly(_rule(), candidate) is False
    assert rule_matches_anomaly(_rule(include_property_drifts=True), candidate) is True
    # Spike like every drift scope: a rule that only notifies on drops gets none.
    assert (
        rule_matches_anomaly(_rule(include_property_drifts=True, notify_on_spike=False), candidate)
        is False
    )


def test_an_event_filter_narrows_per_event_drifts_and_passes_type_changes() -> None:
    per_event = property_drift_candidate(
        _drift(PropertyDriftKind.new_property, {"presence_rate": 0.5}),
        variable_name="coupon",
        scan_config_id=None,
    )
    type_change = property_drift_candidate(
        _drift(PropertyDriftKind.type_change, {"observed_type": "number"}),
        variable_name="price",
        scan_config_id=None,
    )
    rule = _rule(include_property_drifts=True)
    rule.filters = [AlertRuleFilter(field="event", operator="in", values=[str(uuid.uuid4())])]
    assert rule_matches_anomaly(rule, per_event) is False
    # Event-less, like a schema drift: an event filter cannot judge it.
    assert rule_matches_anomaly(rule, type_change) is True
    rule.filters = [AlertRuleFilter(field="event", operator="in", values=[str(per_event.event_id)])]
    assert rule_matches_anomaly(rule, per_event) is True


# --- the worker: candidates, dispatch, bell ---------------------------------------


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'property_drift_alerts.db'}")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _seed(session: Session, *, include_property_drifts: bool = True) -> dict[str, Any]:
    project = Project(id=uuid.uuid4(), name="Shop", slug=f"shop-{uuid.uuid4().hex[:6]}")
    data_source = DataSource(
        id=uuid.uuid4(),
        name="DS",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    session.add_all([project, data_source])
    session.flush()
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
    other_config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Other scan",
        base_query="SELECT 1",
    )
    event_type = EventType(id=uuid.uuid4(), project_id=project.id, name="auth", display_name="A")
    session.add_all([config, other_config, event_type])
    session.flush()
    event = Event(
        id=uuid.uuid4(), project_id=project.id, event_type_id=event_type.id, name="signup"
    )
    plan = Variable(id=uuid.uuid4(), project_id=project.id, name="plan", description="")
    coupon = Variable(id=uuid.uuid4(), project_id=project.id, name="coupon", description="")
    price = Variable(id=uuid.uuid4(), project_id=project.id, name="price", description="")
    muted = Variable(
        id=uuid.uuid4(),
        project_id=project.id,
        name="debug",
        description="",
        excluded_from_scans=True,
    )
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=project.id,
        type="slack",
        name="Slack",
        enabled=True,
        webhook_url_encrypted="secret",
    )
    session.add_all([event, plan, coupon, price, muted, destination])
    session.flush()
    session.add(
        AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Plan drift",
            enabled=True,
            include_project_total=False,
            include_event_types=False,
            include_events=False,
            include_property_drifts=include_property_drifts,
            notify_on_spike=True,
            notify_on_drop=True,
            min_percent_delta=100,
            min_absolute_delta=0,
            min_expected_count=10,
            cooldown_minutes=1440,
        )
    )
    now = datetime.now(UTC)
    missing = PropertyDrift(
        project_id=project.id,
        variable_id=plan.id,
        event_id=event.id,
        scan_config_id=config.id,
        kind="missing_required",
        detail={"presence_rate": 0.4, "threshold": 0.95},
        detected_at=now,
    )
    rows = [
        missing,
        # Snoozed into the future: not active.
        PropertyDrift(
            project_id=project.id,
            variable_id=coupon.id,
            event_id=event.id,
            scan_config_id=config.id,
            kind="new_property",
            detail={"presence_rate": 0.2},
            status="snoozed",
            snoozed_until=now + timedelta(days=3),
            detected_at=now,
        ),
        # Accepted: triaged.
        PropertyDrift(
            project_id=project.id,
            variable_id=coupon.id,
            event_id=event.id,
            scan_config_id=config.id,
            kind="missing_required",
            detail={"presence_rate": 0.2, "threshold": 0.95},
            status="accepted",
            detected_at=now,
        ),
        # Another scan's row: that config's run alerts on it.
        PropertyDrift(
            project_id=project.id,
            variable_id=price.id,
            event_id=None,
            scan_config_id=other_config.id,
            kind="type_change",
            detail={"observed_type": "number", "expected_type": "string"},
            detected_at=now,
        ),
        # A property taken out of scanning.
        PropertyDrift(
            project_id=project.id,
            variable_id=muted.id,
            event_id=event.id,
            scan_config_id=config.id,
            kind="new_property",
            detail={"presence_rate": 0.9},
            detected_at=now,
        ),
        # Past the 30-day retention.
        PropertyDrift(
            project_id=project.id,
            variable_id=price.id,
            event_id=event.id,
            scan_config_id=config.id,
            kind="new_property",
            detail={"presence_rate": 0.9},
            detected_at=now - timedelta(days=45),
        ),
    ]
    session.add_all(rows)
    session.commit()
    return {
        "project": project,
        "config": config,
        "other_config": other_config,
        "event": event,
        "missing": missing,
    }


def test_candidates_are_the_active_rows_of_this_scan(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        world = _seed(session)
        candidates = _get_active_property_drift_candidates(session, world["config"])
        assert list(candidates) == [(SCOPE_PROPERTY_DRIFT, str(world["missing"].id))]
        other = _get_active_property_drift_candidates(session, world["other_config"])
        (type_change,) = other.values()
        assert type_change.drift_type == "type_change"
        assert type_change.event_id is None


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


def test_dispatch_alerts_once_per_drift(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        world = _seed(session)
    config_id = world["config"].id
    for _ in range(3):
        _mark_sent(factory, _collect(factory, config_id))

    with factory() as session:
        items = list(
            session.execute(
                select(AlertDeliveryItem).where(
                    AlertDeliveryItem.scope_type == SCOPE_PROPERTY_DRIFT
                )
            ).scalars()
        )
        # Three collections, one item: the per-row cooldown holds it.
        assert len(items) == 1
        (item,) = items
        assert item.scope_name == "signup.plan"
        assert item.event_id == world["event"].id
        assert item.drift_type == "missing_required"
        assert item.drift_field == "plan"
        assert item.sample_value == "on 40% of rows, required on 95%"


def test_rules_without_the_toggle_get_nothing(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        world = _seed(session, include_property_drifts=False)
    assert _collect(factory, world["config"].id) == []


def test_watchers_of_the_event_hear_once_per_drift(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        world = _seed(session)
        anna = User(id=uuid.uuid4(), email="anna@example.com", name="Anna", password_hash="x")
        session.add(anna)
        session.flush()
        session.add(ProjectMember(project_id=world["project"].id, user_id=anna.id, role="editor"))
        session.add(
            Subscription(
                user_id=anna.id,
                project_id=world["project"].id,
                entity_type="event",
                entity_id=world["event"].id,
                reasons=["manual"],
            )
        )
        session.commit()
        config = session.get(ScanConfig, world["config"].id)
        assert config is not None
        assert notification_producers.produce_notifications(session, "property_drifts", config) == 1
        # The drift stays open; the next run does not say it again.
        assert notification_producers.produce_notifications(session, "property_drifts", config) == 0
        # The other scan only has a type change, which no one watches.
        other = session.get(ScanConfig, world["other_config"].id)
        assert notification_producers.produce_notifications(session, "property_drifts", other) == 0

        (notification,) = session.execute(select(Notification)).scalars().all()
        assert notification.kind == "property_drift"
        assert notification.user_id == anna.id
        assert notification.entity_id == world["event"].id
        assert notification.title == "Property drift: required property plan is missing on signup"
        assert notification.body == "plan: on 40% of rows, required on 95%."
        assert notification.url.endswith(
            f"/monitoring/event/{world['event'].id}?property_drift={world['missing'].id}"
        )


# --- health score (pure) --------------------------------------------------------------


def test_property_drifts_count_in_the_drifts_component() -> None:
    facts = EventHealthFacts(
        event_id=uuid.uuid4(),
        event_type_id=uuid.uuid4(),
        name="signup",
        status="live",
        last_seen_at=datetime.now(UTC),
        covered=True,
        property_drifts=2,
        value_drifts=1,
    )
    health = score_event(facts, datetime.now(UTC))
    drifts = next(c for c in health.components if c.key == "drifts")
    assert drifts.counts == {"schema": 0, "value": 1, "property": 2, "distribution": 0}
    assert drifts.value == pytest.approx(0.25)
    assert drifts.detail == "1 value drift, 2 property drifts"


# --- the API: rule flag, replay, summary count, health ---------------------------------


async def _post(client: AsyncClient, url: str, body: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post(url, json=body)
    assert resp.status_code in (200, 201), resp.text
    data = resp.json()
    assert isinstance(data, dict)
    return data


async def _api_world(client: AsyncClient, slug: str) -> dict[str, Any]:
    project = await _post(client, "/api/v1/projects", {"name": slug, "slug": slug})
    project_id = uuid.UUID(project["id"])
    base = f"/api/v1/projects/{slug}"
    event_type = await _post(client, f"{base}/event-types", {"name": "auth", "display_name": "A"})
    event_type_id = uuid.UUID(event_type["id"])
    event = await _post(
        client, f"{base}/events", {"event_type_id": str(event_type_id), "name": "signup"}
    )
    event_id = uuid.UUID(event["id"])
    now = datetime.now(UTC)
    async with TestSessionLocal() as session:
        await session.execute(
            update(Event)
            .where(Event.id == event_id)
            .values(status=EventStatus.live.value, last_seen_at=now)
        )
        data_source = DataSource(
            name=f"wh-{uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=9000,
            database_name="db",
            username="u",
            password_encrypted="",
        )
        session.add(data_source)
        await session.flush()
        config = ScanConfig(
            project_id=project_id,
            data_source_id=data_source.id,
            event_type_id=event_type_id,
            name="auth events",
            base_query="SELECT 1",
            cardinality_threshold=100,
        )
        plan = Variable(project_id=project_id, name="plan", description="")
        price = Variable(project_id=project_id, name="price", description="")
        session.add_all([config, plan, price])
        await session.flush()
        missing = PropertyDrift(
            project_id=project_id,
            variable_id=plan.id,
            event_id=event_id,
            scan_config_id=config.id,
            kind="missing_required",
            detail={"presence_rate": 0.4, "threshold": 0.95},
            detected_at=now - timedelta(hours=2),
        )
        type_change = PropertyDrift(
            project_id=project_id,
            variable_id=price.id,
            event_id=None,
            scan_config_id=config.id,
            kind="type_change",
            detail={"observed_type": "number", "expected_type": "string"},
            detected_at=now - timedelta(hours=2),
        )
        dismissed = PropertyDrift(
            project_id=project_id,
            variable_id=price.id,
            event_id=event_id,
            scan_config_id=config.id,
            kind="new_property",
            detail={"presence_rate": 0.5},
            status="false_positive",
            detected_at=now - timedelta(hours=2),
        )
        session.add_all([missing, type_change, dismissed])
        await session.commit()
    return {
        "base": base,
        "project_id": project_id,
        "event_id": event_id,
        "missing_id": missing.id,
    }


@pytest.mark.asyncio
async def test_alert_rule_api_round_trips_include_property_drifts(client: AsyncClient) -> None:
    await _post(client, "/api/v1/projects", {"name": "PD rules", "slug": "pd-rules"})
    destination = await _post(
        client,
        "/api/v1/projects/pd-rules/alert-destinations",
        {
            "type": "slack",
            "name": "Main Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T000/B000/XXX",
        },
    )
    rules_url = f"/api/v1/projects/pd-rules/alert-destinations/{destination['id']}/rules"
    default_rule = await _post(client, rules_url, {"name": "Default"})
    assert default_rule["include_property_drifts"] is False
    created = await _post(client, rules_url, {"name": "PD", "include_property_drifts": True})
    assert created["include_property_drifts"] is True
    patched = await client.patch(
        f"{rules_url}/{created['id']}", json={"include_property_drifts": False}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["include_property_drifts"] is False
    nulled = await client.patch(
        f"{rules_url}/{created['id']}", json={"include_property_drifts": None}
    )
    assert nulled.status_code == 422


@pytest.mark.asyncio
async def test_the_replay_fires_on_property_drift(client: AsyncClient) -> None:
    world = await _api_world(client, "pd-replay")
    base = world["base"]
    destination = await _post(
        client,
        f"{base}/alert-destinations",
        {
            "type": "slack",
            "name": "Replay Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T1/B1/replay",
        },
    )
    rule = await _post(
        client,
        f"{base}/alert-destinations/{destination['id']}/rules",
        {
            "name": "PD",
            "include_project_total": False,
            "include_event_types": False,
            "include_events": False,
            "include_property_drifts": True,
        },
    )
    resp = await client.post(
        f"{base}/alert-destinations/{destination['id']}/rules/{rule['id']}/simulate?days=7"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The dismissed row is not active; the other two are.
    assert body["anomalies_considered"] == 2
    firings = {firing["drift_type"]: firing for firing in body["firings"]}
    assert set(firings) == {"missing_required", "type_change"}
    missing = firings["missing_required"]
    assert missing["scope_type"] == SCOPE_PROPERTY_DRIFT
    assert missing["scope_ref"] == str(world["missing_id"])
    assert missing["scope_name"] == "signup.plan"
    assert missing["event_id"] == str(world["event_id"])
    assert firings["type_change"]["scope_name"] == "All events.price"
    assert (
        "Missing required property ${plan}: on 40% of rows, required on 95%"
        in body["rendered_message"]
    )


@pytest.mark.asyncio
async def test_summary_and_health_share_the_open_count(client: AsyncClient) -> None:
    world = await _api_world(client, "pd-counts")
    projects = await client.get("/api/v1/projects")
    assert projects.status_code == 200, projects.text
    (project,) = [p for p in projects.json() if p["slug"] == "pd-counts"]
    # The missing required property and the type change; the dismissed row is not open.
    assert project["summary"]["open_property_drift_count"] == 2
    # The badge of the Anomalies page is untouched.
    assert project["summary"]["monitoring_signal_count"] == 0

    health = await client.get(f"{world['base']}/events/{world['event_id']}/health")
    assert health.status_code == 200, health.text
    drifts = next(c for c in health.json()["components"] if c["key"] == "drifts")
    # Only the per-event drift is charged to the event; the type change is per property.
    assert drifts["counts"]["property"] == 1
    assert drifts["detail"] == "1 property drift"
