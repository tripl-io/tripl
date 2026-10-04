"""Backend asks from design-review batches 5-12.

JR-15 (``metric`` alert-rule filter), AU-9 (demo link template), MO-23
(``ScanConfig.monitoring_enabled``), MO-15 (failing alert destinations on the
project summary), SH-11 (branch-scoped summary counts), PL-21 (plan revision
``kind`` / ``branch_id``), JR-6 (incident on expanded signals), MO-36 (firing
scopes on the monitor detail) and MO-25 (project volume beside top events).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from tripl.alerting_matching import filter_matches_anomaly
from tripl.models.alert_correlation_state import AlertCorrelationState
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.event_metric import EventMetric
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.services.demo.builders.plan import _META_FIELD_SPECS
from tripl.tests.conftest import TestSessionLocal

# --------------------------------------------------------------------------- #
# Shared setup
# --------------------------------------------------------------------------- #


async def _project_with_scan(
    client: AsyncClient, slug: str, *, interval: str | None = None
) -> dict[str, str]:
    """Project + event type + event + scan config; returns their ids."""
    created = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert created.status_code == 201, created.text
    event_type = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page_view", "display_name": "Page View"},
    )
    assert event_type.status_code == 201, event_type.text
    event_type_id = event_type.json()["id"]
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": event_type_id, "name": "Landing Viewed", "status": "implemented"},
    )
    assert event.status_code == 201, event.text
    data_source = await client.post(
        "/api/v1/data-sources",
        json={
            "name": f"Warehouse {slug}",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
            "username": "default",
            "password": "",
        },
    )
    assert data_source.status_code == 201, data_source.text
    scan_body: dict[str, object] = {
        "data_source_id": data_source.json()["id"],
        "name": "Production scan",
        "base_query": "SELECT 1",
    }
    if interval is not None:
        scan_body["interval"] = interval
    scan = await client.post(f"/api/v1/projects/{slug}/scans", json=scan_body)
    assert scan.status_code == 201, scan.text
    project = await client.get(f"/api/v1/projects/{slug}")
    return {
        "project_id": project.json()["id"],
        "event_type_id": event_type_id,
        "event_id": event.json()["id"],
        "scan_config_id": scan.json()["id"],
    }


async def _destination_and_rule(
    client: AsyncClient, slug: str, *, name: str = "Slack", enabled: bool = True
) -> tuple[str, str]:
    destination = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "slack",
            "name": name,
            "enabled": enabled,
            "webhook_url": f"https://hooks.slack.com/services/T1/B1/{uuid.uuid4().hex[:8]}",
        },
    )
    assert destination.status_code == 201, destination.text
    destination_id = destination.json()["id"]
    rule = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules",
        json={"name": f"{name} rule", "enabled": True, "filters": []},
    )
    assert rule.status_code == 201, rule.text
    return destination_id, rule.json()["id"]


def _delivery(
    ids: dict[str, str],
    destination_id: str,
    rule_id: str,
    *,
    status: str,
    created_at: datetime,
) -> AlertDelivery:
    return AlertDelivery(
        id=uuid.uuid4(),
        project_id=uuid.UUID(ids["project_id"]),
        scan_config_id=uuid.UUID(ids["scan_config_id"]),
        destination_id=uuid.UUID(destination_id),
        rule_id=uuid.UUID(rule_id),
        status=status,
        channel="slack",
        matched_count=1,
        created_at=created_at,
    )


def _item(
    delivery_id: uuid.UUID,
    *,
    scope_type: str,
    scope_ref: str,
    bucket: datetime,
    group_id: uuid.UUID | None = None,
    event_id: str | None = None,
    scope_name: str = "Scope",
) -> AlertDeliveryItem:
    return AlertDeliveryItem(
        delivery_id=delivery_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        scope_name=scope_name,
        event_type_id=None,
        event_id=uuid.UUID(event_id) if event_id else None,
        bucket=bucket,
        direction="drop",
        actual_count=10,
        expected_count=100,
        absolute_delta=90,
        percent_delta=90.0,
        correlation_group_id=group_id,
    )


# --------------------------------------------------------------------------- #
# ``metric`` filter field
# --------------------------------------------------------------------------- #


def _candidate(scope_type: str, scope_ref: str) -> Any:
    return SimpleNamespace(
        scope_type=scope_type,
        scope_ref=scope_ref,
        event_id=None,
        event_type_id=None,
        direction="spike",
    )


def _filter(operator: str, values: list[str]) -> Any:
    return SimpleNamespace(field="metric", operator=operator, values=values)


def test_metric_filter_narrows_a_metric_signal_by_its_definition_id() -> None:
    watched, other = str(uuid.uuid4()), str(uuid.uuid4())

    assert filter_matches_anomaly(_filter("in", [watched]), _candidate("metric", watched))
    assert not filter_matches_anomaly(_filter("in", [watched]), _candidate("metric", other))
    assert not filter_matches_anomaly(_filter("not_in", [watched]), _candidate("metric", watched))
    assert filter_matches_anomaly(_filter("ne", [watched]), _candidate("metric", other))


def test_metric_filter_passes_every_non_metric_signal_through() -> None:
    """Symmetric with an ``event`` filter on a project-total signal: a signal
    that names no metric has nothing for the filter to judge."""
    watched = str(uuid.uuid4())

    for scope_type in ("project_total", "event_type", "event", "schema"):
        assert filter_matches_anomaly(_filter("in", [watched]), _candidate(scope_type, watched))


@pytest.mark.asyncio
async def test_rule_with_an_unknown_metric_filter_is_refused(client: AsyncClient) -> None:
    slug = "b512-metric-filter"
    await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    destination = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "slack",
            "name": "Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T1/B1/metric",
        },
    )
    destination_id = destination.json()["id"]

    resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules",
        json={
            "name": "One metric",
            "enabled": True,
            "include_metrics": True,
            "filters": [{"field": "metric", "operator": "in", "values": [str(uuid.uuid4())]}],
        },
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Filter metric not found"


@pytest.mark.asyncio
async def test_metric_filter_value_must_be_a_uuid(client: AsyncClient) -> None:
    slug = "b512-metric-filter-uuid"
    await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    destination = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "slack",
            "name": "Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T1/B1/uuid",
        },
    )

    resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination.json()['id']}/rules",
        json={
            "name": "Bad metric",
            "enabled": True,
            "filters": [{"field": "metric", "operator": "eq", "values": ["checkout_rate"]}],
        },
    )

    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# the demo Jira link template substitutes its value
# --------------------------------------------------------------------------- #


def test_demo_link_templates_use_the_real_placeholder() -> None:
    templates = [spec[3] for spec in _META_FIELD_SPECS if spec[3] is not None]

    assert templates, "the demo seeds at least one linked meta field"
    for template in templates:
        assert "${value}" in template, template


# --------------------------------------------------------------------------- #
# monitoring_enabled on ScanConfig
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_scan_config_reports_monitoring_enabled_from_its_interval(
    client: AsyncClient,
) -> None:
    monitored = await _project_with_scan(client, "b512-monitored", interval="1h")
    unmonitored = await _project_with_scan(client, "b512-unmonitored")

    on = await client.get(f"/api/v1/projects/b512-monitored/scans/{monitored['scan_config_id']}")
    off = await client.get(
        f"/api/v1/projects/b512-unmonitored/scans/{unmonitored['scan_config_id']}"
    )

    assert on.status_code == 200, on.text
    assert on.json()["monitoring_enabled"] is True
    assert off.json()["interval"] is None
    assert off.json()["monitoring_enabled"] is False


# --------------------------------------------------------------------------- #
# failing alert destinations on the project summary
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_summary_counts_enabled_destinations_whose_latest_delivery_failed(
    client: AsyncClient,
) -> None:
    slug = "b512-failing-destinations"
    ids = await _project_with_scan(client, slug)
    broken_dest, broken_rule = await _destination_and_rule(client, slug, name="Broken")
    recovered_dest, recovered_rule = await _destination_and_rule(client, slug, name="Recovered")
    disabled_dest, disabled_rule = await _destination_and_rule(
        client, slug, name="Disabled", enabled=False
    )
    now = datetime.now(UTC)
    async with TestSessionLocal() as session:
        session.add_all(
            [
                # Failing on its latest send, despite an older success.
                _delivery(
                    ids,
                    broken_dest,
                    broken_rule,
                    status="sent",
                    created_at=now - timedelta(hours=2),
                ),
                _delivery(
                    ids,
                    broken_dest,
                    broken_rule,
                    status="failed",
                    created_at=now - timedelta(hours=1),
                ),
                # Failed once, then delivered: not failing now.
                _delivery(
                    ids,
                    recovered_dest,
                    recovered_rule,
                    status="failed",
                    created_at=now - timedelta(hours=2),
                ),
                _delivery(
                    ids,
                    recovered_dest,
                    recovered_rule,
                    status="sent",
                    created_at=now - timedelta(hours=1),
                ),
                # Failing, but switched off, so it is not a live failure.
                _delivery(
                    ids,
                    disabled_dest,
                    disabled_rule,
                    status="failed",
                    created_at=now - timedelta(hours=1),
                ),
            ]
        )
        await session.commit()

    resp = await client.get(f"/api/v1/projects/{slug}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["summary"]["failing_alert_destination_count"] == 1


# --------------------------------------------------------------------------- #
# branch-scoped plan counters
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_summary_plan_counters_follow_the_branch_param(client: AsyncClient) -> None:
    slug = "b512-branch-summary"
    await _project_with_scan(client, slug)
    branch = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    assert branch.status_code == 201, branch.text
    branch_id = branch.json()["id"]
    added = await client.post(
        f"/api/v1/projects/{slug}/event-types?branch={branch_id}",
        json={"name": "purchase", "display_name": "Purchase"},
    )
    assert added.status_code == 201, added.text

    main = await client.get(f"/api/v1/projects/{slug}")
    on_branch = await client.get(f"/api/v1/projects/{slug}?branch={branch_id}")

    main_summary = main.json()["summary"]
    assert on_branch.status_code == 200, on_branch.text
    branch_summary = on_branch.json()["summary"]
    assert branch_summary["event_type_count"] == main_summary["event_type_count"] + 1
    # The branch's copy of each event is counted once, not added to main's.
    assert branch_summary["event_count"] == main_summary["event_count"]
    # Non-plan counters are the project's, whichever branch is read.
    assert branch_summary["scan_count"] == main_summary["scan_count"] == 1


@pytest.mark.asyncio
async def test_summary_refuses_another_projects_branch(client: AsyncClient) -> None:
    await _project_with_scan(client, "b512-branch-owner")
    await _project_with_scan(client, "b512-branch-other")
    branch = await client.post("/api/v1/projects/b512-branch-owner/branches", json={"name": "x"})

    resp = await client.get(f"/api/v1/projects/b512-branch-other?branch={branch.json()['id']}")

    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# plan revisions carry kind and branch_id
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_revisions_report_their_kind_and_branch(client: AsyncClient) -> None:
    slug = "b512-revision-kind"
    await _project_with_scan(client, slug)
    branch = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    assert branch.status_code == 201, branch.text
    snapshot = await client.post(f"/api/v1/projects/{slug}/revisions", json={"summary": "manual"})
    assert snapshot.status_code == 201, snapshot.text

    listed = await client.get(f"/api/v1/projects/{slug}/revisions")

    assert listed.status_code == 200, listed.text
    by_summary = {item["summary"]: item for item in listed.json()["items"]}
    base = by_summary["Base snapshot for branch 'feature'"]
    assert base["kind"] == "branch_base"
    assert base["branch_id"] == branch.json()["id"]
    assert by_summary["manual"]["kind"] == "snapshot"
    assert by_summary["manual"]["branch_id"] is None
    assert snapshot.json()["kind"] == "snapshot"

    detail = await client.get(f"/api/v1/projects/{slug}/revisions/{base['id']}")
    assert detail.json()["kind"] == "branch_base"


# --------------------------------------------------------------------------- #
# expanded signals name their incident
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_expanded_signal_carries_its_incident_and_status(client: AsyncClient) -> None:
    slug = "b512-signal-incident"
    ids = await _project_with_scan(client, slug)
    destination_id, rule_id = await _destination_and_rule(client, slug)
    bucket = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    group_id = uuid.uuid4()
    scan_config_id = uuid.UUID(ids["scan_config_id"])
    async with TestSessionLocal() as session:
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=scan_config_id,
                event_id=None,
                event_type_id=uuid.UUID(ids["event_type_id"]),
                bucket=bucket,
                count=10,
            )
        )
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=scan_config_id,
                scope_type="project_total",
                scope_ref=ids["scan_config_id"],
                bucket=bucket,
                actual_count=10,
                expected_count=100,
                stddev=10,
                z_score=-9,
                direction="drop",
                created_at=bucket,
            )
        )
        delivery = _delivery(ids, destination_id, rule_id, status="sent", created_at=bucket)
        session.add(delivery)
        await session.flush()
        session.add(
            _item(
                delivery.id,
                scope_type="project_total",
                scope_ref=ids["scan_config_id"],
                bucket=bucket,
                group_id=group_id,
            )
        )
        session.add(
            AlertCorrelationState(
                project_id=uuid.UUID(ids["project_id"]),
                correlation_group_id=group_id,
                status="acknowledged",
            )
        )
        await session.commit()

    expanded = await client.get(f"/api/v1/projects/{slug}/anomalies/signals?expanded=true")
    collapsed = await client.get(f"/api/v1/projects/{slug}/anomalies/signals")

    assert expanded.status_code == 200, expanded.text
    (signal,) = [s for s in expanded.json() if s["scope_type"] == "project_total"]
    assert signal["incident_id"] == str(group_id)
    assert signal["incident_status"] == "acknowledged"
    # Collapsed callers never pay for the lookup.
    assert all(s["incident_id"] is None for s in collapsed.json())

    # A cache hit still reads the status live.
    async with TestSessionLocal() as session:
        await session.execute(
            update(AlertCorrelationState)
            .where(AlertCorrelationState.correlation_group_id == group_id)
            .values(status="resolved")
        )
        await session.commit()
    again = await client.get(f"/api/v1/projects/{slug}/anomalies/signals?expanded=true")
    (signal,) = [s for s in again.json() if s["scope_type"] == "project_total"]
    assert signal["incident_status"] == "resolved"


@pytest.mark.asyncio
async def test_unrouted_signal_has_no_incident(client: AsyncClient) -> None:
    slug = "b512-signal-unrouted"
    ids = await _project_with_scan(client, slug)
    bucket = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    scan_config_id = uuid.UUID(ids["scan_config_id"])
    async with TestSessionLocal() as session:
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=scan_config_id,
                event_id=None,
                event_type_id=uuid.UUID(ids["event_type_id"]),
                bucket=bucket,
                count=10,
            )
        )
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=scan_config_id,
                scope_type="project_total",
                scope_ref=ids["scan_config_id"],
                bucket=bucket,
                actual_count=10,
                expected_count=100,
                stddev=10,
                z_score=-9,
                direction="drop",
                created_at=bucket,
            )
        )
        await session.commit()

    resp = await client.get(f"/api/v1/projects/{slug}/anomalies/signals?expanded=true")

    assert resp.status_code == 200, resp.text
    assert resp.json(), "the anomaly surfaces as a signal"
    for signal in resp.json():
        assert signal["incident_id"] is None
        assert signal["incident_status"] is None


# --------------------------------------------------------------------------- #
# monitor detail lists the scopes firing now
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_monitor_detail_lists_firing_scopes(client: AsyncClient) -> None:
    slug = "b512-monitor-firing"
    ids = await _project_with_scan(client, slug)
    destination_id, rule_id = await _destination_and_rule(client, slug)
    now = datetime.now(UTC).replace(microsecond=0)
    recent = now - timedelta(hours=1)
    async with TestSessionLocal() as session:
        delivery = _delivery(ids, destination_id, rule_id, status="sent", created_at=recent)
        session.add(delivery)
        await session.flush()
        session.add(
            _item(
                delivery.id,
                scope_type="event",
                scope_ref=ids["event_id"],
                bucket=recent,
                event_id=ids["event_id"],
                scope_name="Landing Viewed",
            )
        )
        session.add_all(
            [
                AlertRuleState(
                    rule_id=uuid.UUID(rule_id),
                    scan_config_id=uuid.UUID(ids["scan_config_id"]),
                    scope_type="event",
                    scope_ref=ids["event_id"],
                    is_active=True,
                    last_anomaly_bucket=recent,
                    last_notified_at=recent,
                    last_notified_delivery_id=delivery.id,
                ),
                # Active but stale: counted as warning, never listed as firing.
                AlertRuleState(
                    rule_id=uuid.UUID(rule_id),
                    scan_config_id=uuid.UUID(ids["scan_config_id"]),
                    scope_type="event_type",
                    scope_ref=ids["event_type_id"],
                    is_active=True,
                    last_anomaly_bucket=now - timedelta(days=5),
                ),
            ]
        )
        await session.commit()

    resp = await client.get(f"/api/v1/projects/{slug}/monitors/{rule_id}")

    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["firing_scope_count"] == 1
    (scope,) = detail["firing_scopes"]
    assert scope["scope_type"] == "event"
    assert scope["scope_ref"] == ids["event_id"]
    assert scope["scope_name"] == "Landing Viewed"
    assert scope["event_id"] == ids["event_id"]
    assert scope["direction"] == "drop"
    assert scope["scan_config_id"] == ids["scan_config_id"]


@pytest.mark.asyncio
async def test_healthy_monitor_has_no_firing_scopes(client: AsyncClient) -> None:
    slug = "b512-monitor-healthy"
    await _project_with_scan(client, slug)
    _destination_id, rule_id = await _destination_and_rule(client, slug)

    resp = await client.get(f"/api/v1/projects/{slug}/monitors/{rule_id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["firing_scopes"] == []


# --------------------------------------------------------------------------- #
# project volume beside the top events
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_top_events_carry_the_project_window_total(client: AsyncClient) -> None:
    slug = "b512-top-events-share"
    ids = await _project_with_scan(client, slug)
    bucket = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    scan_config_id = uuid.UUID(ids["scan_config_id"])
    async with TestSessionLocal() as session:
        session.add_all(
            [
                # Type-level: the project total, matched and unmatched alike.
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config_id,
                    event_id=None,
                    event_type_id=uuid.UUID(ids["event_type_id"]),
                    bucket=bucket,
                    count=1000,
                ),
                # Event-level: re-counts the matched share of the same rows.
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config_id,
                    event_id=uuid.UUID(ids["event_id"]),
                    event_type_id=None,
                    bucket=bucket,
                    count=400,
                ),
            ]
        )
        await session.commit()

    resp = await client.get(f"/api/v1/projects/{slug}/overview/top-events")

    assert resp.status_code == 200, resp.text
    (row,) = resp.json()
    assert row["total_count"] == 400
    assert row["window_total_count"] == 1000
