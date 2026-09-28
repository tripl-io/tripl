"""Batch 17 regressions: activity rail, monitor rollup, reconciliation, contracts.

Each test names the finding it pins (tripl-0zpq.N) and fails if that fix is
reverted.
"""

from __future__ import annotations

import socket
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.alerting_validation import reject_private_host
from tripl.models import Base
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.data_source import DataSource
from tripl.models.event import Event, EventStatus
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.shadow_event_candidate import SHADOW_STATUS_NEW, ShadowEventCandidate
from tripl.schemas.alerting import AlertInboxActionRequest, AlertInboxBulkActionRequest
from tripl.schemas.schema_drift import SchemaDriftActionRequest
from tripl.schemas.variable_value_drift import VariableValueDriftActionRequest
from tripl.services.monitoring_utils import summarize_monitor_states
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks.metrics import collect as metrics_collect

# --- shared helpers -----------------------------------------------------------


async def _project(client: AsyncClient, slug: str) -> uuid.UUID:
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text
    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == slug))
    assert project_id is not None
    return project_id


async def _event_type(client: AsyncClient, slug: str, name: str) -> uuid.UUID:
    resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": name, "display_name": name.title()},
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _scan_config(project_id: uuid.UUID, *, interval: str) -> uuid.UUID:
    async with TestSessionLocal() as session:
        ds = DataSource(
            name=f"wh-{uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=9000,
            database_name="db",
            username="u",
        )
        session.add(ds)
        await session.flush()
        config = ScanConfig(
            project_id=project_id,
            data_source_id=ds.id,
            name=f"scan-{interval}",
            base_query="SELECT 1",
            interval=interval,
        )
        session.add(config)
        await session.commit()
        return config.id


async def _rule(client: AsyncClient, slug: str) -> uuid.UUID:
    dest = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "slack",
            "name": "Ops",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T000/B000/XXX",
        },
    )
    assert dest.status_code == 201, dest.text
    rule = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{dest.json()['id']}/rules",
        json={"name": "Watch", "enabled": True},
    )
    assert rule.status_code == 201, rule.text
    return uuid.UUID(rule.json()["id"])


# --- tripl-0zpq.162: monitor rollup follows dispatch's interval floor ---------


def test_rollup_judges_each_state_on_its_own_grid() -> None:
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    state = SimpleNamespace(
        is_active=True,
        last_anomaly_bucket=now - timedelta(hours=48),
        last_notified_at=now - timedelta(hours=40),
    )
    # A daily grid: dispatch keeps a 48h-old bucket live (horizon 72h).
    daily = summarize_monitor_states([state], now=now, interval_of=lambda _s: timedelta(days=1))
    assert daily.status == "firing"
    assert daily.firing_scope_count == 1
    # No resolvable grid keeps the bare 24h window.
    assert summarize_monitor_states([state], now=now).status == "warning"


@pytest.mark.asyncio
async def test_a_daily_scan_monitor_that_dispatch_keeps_live_reads_firing(
    client: AsyncClient,
) -> None:
    slug = "b17-daily-monitor"
    project_id = await _project(client, slug)
    scan_config_id = await _scan_config(project_id, interval="1d")
    rule_id = await _rule(client, slug)
    async with TestSessionLocal() as session:
        session.add(
            AlertRuleState(
                rule_id=rule_id,
                scan_config_id=scan_config_id,
                scope_type="project_total",
                scope_ref=str(scan_config_id),
                is_active=True,
                last_anomaly_bucket=datetime.now(UTC) - timedelta(hours=48),
            )
        )
        await session.commit()

    summary = await client.get(f"/api/v1/projects/{slug}/monitors-summary")
    assert summary.status_code == 200, summary.text
    assert summary.json()["firing_count"] == 1
    assert summary.json()["monitors"][0]["status"] == "firing"

    detail = await client.get(f"/api/v1/projects/{slug}/monitors/{rule_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["status"] == "firing"

    projects = await client.get("/api/v1/projects")
    assert projects.status_code == 200
    (project,) = [p for p in projects.json() if p["slug"] == slug]
    assert project["summary"]["firing_monitor_count"] == 1


@pytest.mark.asyncio
async def test_a_weekly_catalog_metric_monitor_reads_firing_on_the_metric_grid(
    client: AsyncClient,
) -> None:
    slug = "b17-weekly-metric-monitor"
    project_id = await _project(client, slug)
    rule_id = await _rule(client, slug)
    async with TestSessionLocal() as session:
        metric = MetricDefinition(
            project_id=project_id,
            name="conversion",
            display_name="Conversion",
            kind="sql",
            interval="1w",
        )
        session.add(metric)
        await session.flush()
        session.add(
            AlertRuleState(
                rule_id=rule_id,
                scan_config_id=None,
                scope_type="metric",
                scope_ref=str(metric.id),
                is_active=True,
                # Five days old: inside a weekly grid's 21-day horizon.
                last_anomaly_bucket=datetime.now(UTC) - timedelta(days=5),
            )
        )
        await session.commit()

    summary = await client.get(f"/api/v1/projects/{slug}/monitors-summary")
    assert summary.status_code == 200, summary.text
    assert summary.json()["firing_count"] == 1


# --- tripl-0zpq.193 / .302 / .195: activity rail anomalies --------------------


@pytest.mark.asyncio
async def test_rail_dates_anomalies_by_bucket_not_by_re_detection(client: AsyncClient) -> None:
    slug = "b17-rail-bucket"
    project_id = await _project(client, slug)
    scan_config_id = await _scan_config(project_id, interval="1d")
    now = datetime.now(UTC)
    fresh_bucket = (now - timedelta(days=2)).replace(microsecond=0)
    async with TestSessionLocal() as session:
        for bucket, actual in ((now - timedelta(days=26), 900), (fresh_bucket, 42)):
            session.add(
                MetricAnomaly(
                    scan_config_id=scan_config_id,
                    scope_type="project_total",
                    scope_ref=str(scan_config_id),
                    bucket=bucket,
                    actual_count=actual,
                    expected_count=10,
                    stddev=2,
                    z_score=9,
                    direction="spike",
                    # The detector re-inserts its trailing window every tick,
                    # so both rows carry a created_at of "just now".
                    created_at=now,
                )
            )
        await session.commit()

    resp = await client.get(f"/api/v1/activity/projects/{slug}?limit=20")
    assert resp.status_code == 200, resp.text
    anomalies = [item for item in resp.json() if item["type"] == "anomaly"]
    assert len(anomalies) == 1, anomalies
    assert anomalies[0]["detail"].startswith("42 actual")
    occurred = datetime.fromisoformat(anomalies[0]["occurred_at"])
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=UTC)
    assert occurred == fresh_bucket


@pytest.mark.asyncio
async def test_catalog_metric_anomaly_reaches_the_rail(client: AsyncClient) -> None:
    slug = "b17-rail-metric"
    project_id = await _project(client, slug)
    async with TestSessionLocal() as session:
        metric = MetricDefinition(
            project_id=project_id,
            name="checkout_rate",
            display_name="Checkout rate",
            kind="sql",
            interval="1h",
        )
        session.add(metric)
        await session.flush()
        metric_id = metric.id
        session.add(
            MetricAnomaly(
                scan_config_id=None,
                scope_type="metric",
                scope_ref=str(metric_id),
                bucket=datetime.now(UTC) - timedelta(hours=2),
                actual_count=0.04,
                expected_count=0.25,
                stddev=0.02,
                z_score=-8,
                direction="drop",
            )
        )
        await session.commit()

    for url in (f"/api/v1/activity/projects/{slug}", "/api/v1/activity"):
        resp = await client.get(url)
        assert resp.status_code == 200, resp.text
        anomalies = [item for item in resp.json() if item["type"] == "anomaly"]
        assert len(anomalies) == 1, (url, anomalies)
        item = anomalies[0]
        assert item["title"] == "Drop on Checkout rate"
        assert item["project_slug"] == slug
        assert item["detail"].startswith("0.04 actual vs 0.25 expected")
        assert item["target_path"] == f"/o/default/p/{slug}/monitoring/metric/{metric_id}"


@pytest.mark.asyncio
async def test_rail_keeps_weekly_anomalies_on_their_own_grid(client: AsyncClient) -> None:
    # Settling withholds the head week from emission, so the newest anomaly a
    # weekly series can carry starts in (now-14d, now-7d]. A flat 7-day bucket
    # cutoff hid every one of them; the window is floored at 3 x the grid.
    slug = "b17-rail-weekly"
    project_id = await _project(client, slug)
    weekly_scan = await _scan_config(project_id, interval="1w")
    daily_scan = await _scan_config(project_id, interval="1d")
    ten_days_ago = datetime.now(UTC) - timedelta(days=10)
    async with TestSessionLocal() as session:
        metric = MetricDefinition(
            project_id=project_id,
            name="weekly_revenue",
            display_name="Weekly revenue",
            kind="sql",
            interval="1w",
        )
        session.add(metric)
        await session.flush()
        metric_id = metric.id
        for scan_config_id, actual in ((weekly_scan, 700), (daily_scan, 300)):
            session.add(
                MetricAnomaly(
                    scan_config_id=scan_config_id,
                    scope_type="project_total",
                    scope_ref=str(scan_config_id),
                    bucket=ten_days_ago,
                    actual_count=actual,
                    expected_count=10,
                    stddev=2,
                    z_score=9,
                    direction="spike",
                )
            )
        session.add(
            MetricAnomaly(
                scan_config_id=None,
                scope_type="metric",
                scope_ref=str(metric_id),
                bucket=ten_days_ago,
                actual_count=5,
                expected_count=50,
                stddev=5,
                z_score=-9,
                direction="drop",
            )
        )
        await session.commit()

    resp = await client.get(f"/api/v1/activity/projects/{slug}?limit=20")
    assert resp.status_code == 200, resp.text
    anomalies = [item for item in resp.json() if item["type"] == "anomaly"]
    details = sorted(item["detail"].split(" ")[0] for item in anomalies)
    # The weekly scan and the weekly metric show; the 10-day-old daily one
    # stays outside its 7-day window.
    assert details == ["5", "700"], anomalies
    assert {item["target_path"] for item in anomalies} >= {
        f"/o/default/p/{slug}/monitoring/metric/{metric_id}"
    }


# --- tripl-0zpq.194: a last_seen bump is not a plan edit ----------------------


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch17.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def test_last_seen_bump_leaves_updated_at_alone(
    sync_session_factory: sessionmaker[Session],
) -> None:
    edited_at = datetime(2026, 1, 1, 9, tzinfo=UTC)
    bucket = datetime(2026, 5, 1, 10, tzinfo=UTC)
    with sync_session_factory() as session:
        project = Project(id=uuid.uuid4(), name="Bump", slug="bump", description="")
        event_type = EventType(
            id=uuid.uuid4(),
            project_id=project.id,
            name="t",
            display_name="T",
            description="",
        )
        session.add_all([project, event_type])
        event = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=event_type.id,
            name="already_live",
            status=EventStatus.live.value,
            created_at=edited_at,
            updated_at=edited_at,
        )
        session.add(event)
        session.commit()

        metrics_collect._bump_event_last_seen(
            session, event_agg={(uuid.uuid4(), event.id, bucket): 5}
        )
        session.commit()
        session.expire_all()

        row = session.get(Event, event.id)
        assert row is not None
        last_seen = row.last_seen_at
        assert last_seen is not None
        assert last_seen.replace(tzinfo=UTC) == bucket
        assert row.updated_at.replace(tzinfo=UTC) == edited_at


# --- tripl-0zpq.223: shadow identity is per event type ------------------------


async def _candidate(
    project_id: uuid.UUID,
    scan_config_id: uuid.UUID,
    *,
    event_type_id: uuid.UUID,
    event_name: str,
) -> uuid.UUID:
    async with TestSessionLocal() as session:
        candidate = ShadowEventCandidate(
            project_id=project_id,
            scan_config_id=scan_config_id,
            event_type_id=event_type_id,
            event_name=event_name,
            observed_count=10,
            first_seen_at=datetime.now(UTC) - timedelta(days=1),
            last_seen_at=datetime.now(UTC),
            status=SHADOW_STATUS_NEW,
        )
        session.add(candidate)
        await session.commit()
        return candidate.id


@pytest.mark.asyncio
async def test_accept_allows_the_same_identity_on_another_event_type(
    client: AsyncClient,
) -> None:
    slug = "b17-shadow-accept"
    project_id = await _project(client, slug)
    type_a = await _event_type(client, slug, "alpha")
    type_b = await _event_type(client, slug, "beta")
    scan_config_id = await _scan_config(project_id, interval="1h")
    base = f"/api/v1/projects/{slug}/reconciliation/shadow-events"

    first = await _candidate(project_id, scan_config_id, event_type_id=type_a, event_name="x")
    resp = await client.post(f"{base}/{first}/accept", json={})
    assert resp.status_code == 200, resp.text

    # ``uq`` (scan_config_id, event_name): the same identity on another type
    # arrives from another scan.
    other_scan = await _scan_config(project_id, interval="1h")
    second = await _candidate(project_id, other_scan, event_type_id=type_b, event_name="x")
    resp = await client.post(f"{base}/{second}/accept", json={"name": "x on beta"})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_archived_identity_hides_only_its_own_types_candidate(client: AsyncClient) -> None:
    slug = "b17-shadow-archived"
    project_id = await _project(client, slug)
    type_a = await _event_type(client, slug, "alpha")
    type_b = await _event_type(client, slug, "beta")
    scan_config_id = await _scan_config(project_id, interval="1h")
    async with TestSessionLocal() as session:
        session.add(
            Event(
                project_id=project_id,
                event_type_id=type_a,
                name="archived x",
                source_name="x",
                status=EventStatus.archived.value,
            )
        )
        await session.commit()
    await _candidate(project_id, scan_config_id, event_type_id=type_a, event_name="x")
    other_scan = await _scan_config(project_id, interval="1h")
    kept = await _candidate(project_id, other_scan, event_type_id=type_b, event_name="x")

    resp = await client.get(f"/api/v1/projects/{slug}/reconciliation/shadow-events")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [item["id"] for item in body["items"]] == [str(kept)]
    assert body["total"] == 1
    assert body["new_count"] == 1


# --- tripl-0zpq.199: a 6h scan does not resolve an hour -----------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("interval", "expected"), [("6h", False), ("1h", True)])
async def test_heatmap_hourly_resolution_needs_an_hourly_grid(
    client: AsyncClient, interval: str, expected: bool
) -> None:
    slug = f"b17-heatmap-{interval}"
    project_id = await _project(client, slug)
    scan_config_id = await _scan_config(project_id, interval=interval)
    resp = await client.get(
        f"/api/v1/projects/{slug}/scans/{scan_config_id}/seasonality",
        params={"scope_type": "project_total", "scope_ref": str(scan_config_id)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["hourly_resolution"] is expected


# --- tripl-0zpq.308: the suite does not need live DNS -------------------------


def test_placeholder_public_hosts_resolve_without_the_live_resolver() -> None:
    # Imported here so a revert fails THIS test rather than the whole module.
    from tripl.tests.conftest import HERMETIC_DNS_ADDRESS

    for host in ("example.com", "example.atlassian.net", "hooks.example.com"):
        addresses = {entry[4][0] for entry in socket.getaddrinfo(host, 443)}
        assert addresses == {HERMETIC_DNS_ADDRESS}
    # And so the SSRF guard passes them deterministically.
    reject_private_host("example.atlassian.net", field="base_url")


# --- tripl-0zpq.325: a timestamp sent with the wrong action is refused --------


def test_mismatched_timestamps_are_refused_on_every_action_body() -> None:
    later = datetime.now(UTC) + timedelta(days=3)
    with pytest.raises(ValidationError, match="only meaningful when action is snooze"):
        SchemaDriftActionRequest(action="accept", snoozed_until=later)
    with pytest.raises(ValidationError, match="only meaningful when action is snooze"):
        VariableValueDriftActionRequest(action="reopen", snoozed_until=later)
    with pytest.raises(ValidationError, match="only meaningful when action is mute"):
        AlertInboxActionRequest(action="acknowledge", muted_until=later)
    with pytest.raises(ValidationError, match="only meaningful when action is mute"):
        AlertInboxBulkActionRequest(
            correlation_group_ids=[uuid.uuid4()], action="resolve", muted_until=later
        )
    # The matching action still takes it.
    assert SchemaDriftActionRequest(action="snooze", snoozed_until=later).snoozed_until
    assert VariableValueDriftActionRequest(action="snooze", snoozed_until=later).snoozed_until
    assert AlertInboxActionRequest(action="mute", muted_until=later).muted_until
