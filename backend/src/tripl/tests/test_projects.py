import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.domain_enums import FieldDefinitionType
from tripl.models.event_metric import EventMetric
from tripl.models.field_definition import FieldDefinition
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_breakdown_anomaly import MetricBreakdownAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.release_regression import ReleaseRegression
from tripl.models.scan_job import ScanJob
from tripl.models.schema_drift import SchemaDrift
from tripl.models.variable_value import VariableValue, VariableValueKind
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal

# Anchor the seeded event-type anomaly to a recent, hour-aligned bucket so its
# signal stays inside the freshness horizon (classify keeps a latest_scan signal
# only while its bucket is newer than now - 24h); the summary assertion below
# derives its expected ``bucket`` string from this same constant.
_METRIC_BUCKET = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)


async def _seed_project_operations(
    *,
    scan_config_id: str,
    event_type_id: str,
    destination_id: str,
) -> None:
    metric_bucket = _METRIC_BUCKET
    job_started_at = datetime(2026, 4, 18, 10, tzinfo=UTC)
    job_completed_at = datetime(2026, 4, 18, 10, 5, tzinfo=UTC)
    # A firing monitor needs an active scope whose last anomaly is inside the
    # recent window (summarize_monitor_states uses now - 24h), so anchor it to now.
    firing_anomaly_bucket = datetime.now(UTC)

    async with TestSessionLocal() as session:
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_config_id),
                event_id=None,
                event_type_id=uuid.UUID(event_type_id),
                bucket=metric_bucket,
                count=42,
            )
        )
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_config_id),
                scope_type="event_type",
                # Detector-written scope refs use the hyphenated UUID format.
                scope_ref=event_type_id,
                event_id=None,
                event_type_id=uuid.UUID(event_type_id),
                bucket=metric_bucket,
                actual_count=42,
                expected_count=21,
                stddev=3,
                z_score=7,
                direction="spike",
            )
        )
        session.add(
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_config_id),
                status="completed",
                started_at=job_started_at,
                completed_at=job_completed_at,
                result_summary={
                    "events_created": 4,
                    "signals_added": 1,
                    "alerts_queued": 1,
                },
                error_message=None,
                created_at=job_completed_at,
                updated_at=job_completed_at,
            )
        )
        rule_id = uuid.uuid4()
        session.add(
            AlertRule(
                id=rule_id,
                destination_id=uuid.UUID(destination_id),
                name="Spike monitor",
            )
        )
        session.add(
            AlertRuleState(
                id=uuid.uuid4(),
                rule_id=rule_id,
                scan_config_id=uuid.UUID(scan_config_id),
                scope_type="event_type",
                scope_ref=event_type_id,
                is_active=True,
                last_anomaly_bucket=firing_anomaly_bucket,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_create_project(client: AsyncClient):
    resp = await client.post(
        "/api/v1/projects",
        json={"name": "Test Project", "slug": "test-project", "description": "A test"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test Project"
    assert data["slug"] == "test-project"
    assert data["app_version_keep_releases"] == 5


@pytest.mark.asyncio
async def test_create_project_duplicate_slug(client: AsyncClient):
    await client.post(
        "/api/v1/projects",
        json={"name": "P1", "slug": "dup-slug"},
    )
    resp = await client.post(
        "/api/v1/projects",
        json={"name": "P2", "slug": "dup-slug"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_list_projects(client: AsyncClient):
    await client.post("/api/v1/projects", json={"name": "A", "slug": "list-a"})
    await client.post("/api/v1/projects", json={"name": "B", "slug": "list-b"})
    resp = await client.get("/api/v1/projects")
    assert resp.status_code == 200
    assert len(resp.json()) >= 2
    assert "summary" in resp.json()[0]


@pytest.mark.asyncio
async def test_get_project(client: AsyncClient):
    await client.post("/api/v1/projects", json={"name": "Get Me", "slug": "get-me"})
    resp = await client.get("/api/v1/projects/get-me")
    assert resp.status_code == 200
    assert resp.json()["slug"] == "get-me"
    assert resp.json()["summary"]["event_count"] == 0
    assert resp.json()["summary"]["latest_scan_job"] is None
    assert resp.json()["summary"]["latest_signal"] is None


@pytest.mark.asyncio
async def test_get_project_not_found(client: AsyncClient):
    resp = await client.get("/api/v1/projects/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_project(client: AsyncClient):
    await client.post("/api/v1/projects", json={"name": "Old", "slug": "upd-me"})
    resp = await client.patch("/api/v1/projects/upd-me", json={"name": "New"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "New"


@pytest.mark.asyncio
async def test_update_project_app_version_retention(client: AsyncClient):
    await client.post(
        "/api/v1/projects",
        json={"name": "Version policy", "slug": "version-policy"},
    )

    resp = await client.patch(
        "/api/v1/projects/version-policy",
        json={"app_version_keep_releases": 3},
    )

    assert resp.status_code == 200
    assert resp.json()["app_version_keep_releases"] == 3
    assert (await client.get("/api/v1/projects/version-policy")).json()[
        "app_version_keep_releases"
    ] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_value", [0, 101, None])
async def test_update_project_rejects_invalid_app_version_retention(
    client: AsyncClient,
    invalid_value: int | None,
):
    await client.post(
        "/api/v1/projects",
        json={"name": "Version validation", "slug": "version-validation"},
    )

    resp = await client.patch(
        "/api/v1/projects/version-validation",
        json={"app_version_keep_releases": invalid_value},
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_project_slug(client: AsyncClient):
    await client.post("/api/v1/projects", json={"name": "Slug me", "slug": "slug-old"})
    resp = await client.patch("/api/v1/projects/slug-old", json={"slug": "slug-new"})
    assert resp.status_code == 200
    assert resp.json()["slug"] == "slug-new"

    old = await client.get("/api/v1/projects/slug-old")
    assert old.status_code == 404
    new = await client.get("/api/v1/projects/slug-new")
    assert new.status_code == 200


@pytest.mark.asyncio
async def test_update_project_slug_conflict(client: AsyncClient):
    await client.post("/api/v1/projects", json={"name": "First", "slug": "taken-slug"})
    await client.post("/api/v1/projects", json={"name": "Second", "slug": "free-slug"})
    resp = await client.patch("/api/v1/projects/free-slug", json={"slug": "taken-slug"})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_project(client: AsyncClient):
    await client.post("/api/v1/projects", json={"name": "Del", "slug": "del-me"})
    resp = await client.delete("/api/v1/projects/del-me")
    assert resp.status_code == 204
    resp = await client.get("/api/v1/projects/del-me")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_project_summary_counts(client: AsyncClient):
    slug = "summary-proj"

    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Summary Project", "slug": slug},
    )
    assert project_resp.status_code == 201

    event_type_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page_view", "display_name": "Page View"},
    )
    assert event_type_resp.status_code == 201
    event_type_id = event_type_resp.json()["id"]

    active_event_resp = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type_id,
            "name": "Landing Viewed",
            "status": "implemented",
        },
    )
    assert active_event_resp.status_code == 201

    review_event_resp = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type_id,
            "name": "Checkout Started",
            "status": "in_review",
        },
    )
    assert review_event_resp.status_code == 201

    archived_event_resp = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type_id,
            "name": "Legacy Event",
            "status": "archived",
        },
    )
    assert archived_event_resp.status_code == 201

    variable_resp = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={"name": "user_id", "variable_type": "string"},
    )
    assert variable_resp.status_code == 201

    data_source_resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Warehouse",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
            "username": "default",
            "password": "",
        },
    )
    assert data_source_resp.status_code == 201
    data_source_id = data_source_resp.json()["id"]

    scan_resp = await client.post(
        f"/api/v1/projects/{slug}/scans",
        json={
            "data_source_id": data_source_id,
            "name": "Production scan",
            "base_query": "SELECT 1",
        },
    )
    assert scan_resp.status_code == 201
    scan_config_id = scan_resp.json()["id"]

    destination_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "slack",
            "name": "Ops",
            "webhook_url": "https://hooks.slack.com/services/T000/B000/XXX",
        },
    )
    assert destination_resp.status_code == 201
    destination_id = destination_resp.json()["id"]

    await _seed_project_operations(
        scan_config_id=scan_config_id,
        event_type_id=event_type_id,
        destination_id=destination_id,
    )

    resp = await client.get(f"/api/v1/projects/{slug}")
    assert resp.status_code == 200
    summary = resp.json()["summary"]

    assert summary["event_type_count"] == 1
    assert summary["event_count"] == 3
    assert summary["active_event_count"] == 2
    assert summary["implemented_event_count"] == 1
    assert summary["review_pending_event_count"] == 1
    assert summary["archived_event_count"] == 1
    assert summary["variable_count"] == 1
    assert summary["scan_count"] == 1
    assert summary["alert_destination_count"] == 1
    # One enabled rule is bound to that destination, so alerting can actually route.
    assert summary["alert_rule_count"] == 1
    assert summary["monitoring_signal_count"] == 1
    assert summary["firing_monitor_count"] == 1
    # The single scan config's latest (only) job completed, so nothing is failing.
    assert summary["failing_scan_config_count"] == 0
    assert summary["latest_scan_job"] == {
        "id": summary["latest_scan_job"]["id"],
        "scan_config_id": scan_config_id,
        "scan_name": "Production scan",
        "status": "completed",
        "started_at": "2026-04-18T10:00:00",
        "completed_at": "2026-04-18T10:05:00",
        "result_summary": {
            "events_created": 4,
            "signals_added": 1,
            "alerts_queued": 1,
        },
        "error_message": None,
        "created_at": "2026-04-18T10:05:00",
    }
    assert summary["latest_signal"] == {
        "scan_config_id": scan_config_id,
        "scan_name": "Production scan",
        "scope_type": "event_type",
        "scope_ref": event_type_id,
        "scope_name": "Page View",
        "state": "latest_scan",
        "bucket": _METRIC_BUCKET.isoformat().replace("+00:00", "Z"),
        "actual_count": 42,
        "expected_count": 21.0,
        "z_score": 7.0,
        "direction": "spike",
    }


@pytest.mark.asyncio
async def test_project_summary_alert_rule_count_ignores_ruleless_and_disabled(
    client: AsyncClient,
) -> None:
    """``alert_rule_count`` must count only rules that can actually route.

    A destination on its own delivers nothing — a rule is what binds a signal to
    a channel — and a disabled rule delivers nothing either. The onboarding
    checklist reads both counters to decide whether alerting is wired up, so a
    project with destinations but no *enabled* rule has to report 0.
    """
    slug = "alert-rule-count"
    create_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Alert Rule Count", "slug": slug},
    )
    assert create_resp.status_code == 201

    async def _add_destination(name: str) -> str:
        resp = await client.post(
            f"/api/v1/projects/{slug}/alert-destinations",
            json={
                "type": "slack",
                "name": name,
                "webhook_url": "https://hooks.slack.com/services/T000/B000/XXX",
            },
        )
        assert resp.status_code == 201
        return str(resp.json()["id"])

    # A destination that never gets a rule: it must keep counting as a
    # destination while contributing nothing to the rule count.
    await _add_destination("Bare")
    ruled_destination_id = await _add_destination("Has rules")

    # Two destinations, zero rules: routing nothing.
    summary = (await client.get(f"/api/v1/projects/{slug}")).json()["summary"]
    assert summary["alert_destination_count"] == 2
    assert summary["alert_rule_count"] == 0

    # A DISABLED rule still routes nothing, so the count must stay at 0.
    async with TestSessionLocal() as session:
        session.add(
            AlertRule(
                id=uuid.uuid4(),
                destination_id=uuid.UUID(ruled_destination_id),
                name="Paused monitor",
                enabled=False,
            )
        )
        await session.commit()

    summary = (await client.get(f"/api/v1/projects/{slug}")).json()["summary"]
    assert summary["alert_destination_count"] == 2
    assert summary["alert_rule_count"] == 0

    # One enabled rule flips it — and the LEFT JOIN must not double-count the
    # rule-less destination alongside the one that now has two rules.
    async with TestSessionLocal() as session:
        session.add(
            AlertRule(
                id=uuid.uuid4(),
                destination_id=uuid.UUID(ruled_destination_id),
                name="Live monitor",
                enabled=True,
            )
        )
        await session.commit()

    summary = (await client.get(f"/api/v1/projects/{slug}")).json()["summary"]
    assert summary["alert_destination_count"] == 2
    assert summary["alert_rule_count"] == 1


async def test_project_summary_counts_configs_whose_latest_run_failed(client: AsyncClient):
    """A config that fails every run must surface even when a newer config succeeds.

    ``latest_scan_job`` only reports the single newest job across the whole
    project, so a scan config that fails every hourly run becomes invisible the
    moment a *different* config logs a newer success. ``failing_scan_config_count``
    ranks jobs per config instead and counts the configs whose latest run failed,
    so the workspace "failed jobs" rollup stays honest.
    """
    slug = "failing-configs-proj"

    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Failing Configs Project", "slug": slug},
    )
    assert project_resp.status_code == 201

    data_source_resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Warehouse",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
            "username": "default",
            "password": "",
        },
    )
    assert data_source_resp.status_code == 201
    data_source_id = data_source_resp.json()["id"]

    healthy_scan_resp = await client.post(
        f"/api/v1/projects/{slug}/scans",
        json={"data_source_id": data_source_id, "name": "Healthy scan", "base_query": "SELECT 1"},
    )
    assert healthy_scan_resp.status_code == 201
    healthy_config_id = healthy_scan_resp.json()["id"]

    failing_scan_resp = await client.post(
        f"/api/v1/projects/{slug}/scans",
        json={"data_source_id": data_source_id, "name": "Failing scan", "base_query": "SELECT 2"},
    )
    assert failing_scan_resp.status_code == 201
    failing_config_id = failing_scan_resp.json()["id"]

    async with TestSessionLocal() as session:
        # The failing config runs hourly and fails every time; its newest run is
        # OLDER than the healthy config's latest success, so the project's single
        # newest job overall is a completed one.
        session.add(
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(failing_config_id),
                status="failed",
                started_at=datetime(2026, 4, 18, 9, tzinfo=UTC),
                completed_at=datetime(2026, 4, 18, 9, 1, tzinfo=UTC),
                error_message="Scan failed: could not connect to the data source.",
                created_at=datetime(2026, 4, 18, 9, 1, tzinfo=UTC),
                updated_at=datetime(2026, 4, 18, 9, 1, tzinfo=UTC),
            )
        )
        # The healthy config's most recent job succeeded — and is the newest in
        # the whole project, which is exactly what hides the failing config.
        session.add(
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(healthy_config_id),
                status="failed",
                started_at=datetime(2026, 4, 18, 8, tzinfo=UTC),
                completed_at=datetime(2026, 4, 18, 8, 1, tzinfo=UTC),
                error_message="Scan failed due to an internal error.",
                created_at=datetime(2026, 4, 18, 8, 1, tzinfo=UTC),
                updated_at=datetime(2026, 4, 18, 8, 1, tzinfo=UTC),
            )
        )
        session.add(
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(healthy_config_id),
                status="completed",
                started_at=datetime(2026, 4, 18, 10, tzinfo=UTC),
                completed_at=datetime(2026, 4, 18, 10, 5, tzinfo=UTC),
                error_message=None,
                created_at=datetime(2026, 4, 18, 10, 5, tzinfo=UTC),
                updated_at=datetime(2026, 4, 18, 10, 5, tzinfo=UTC),
            )
        )
        await session.commit()

    resp = await client.get(f"/api/v1/projects/{slug}")
    assert resp.status_code == 200
    summary = resp.json()["summary"]

    # Only the "Failing scan" config's latest run failed → exactly one failing config.
    # The "Healthy scan" config's OLDER failed run does not count (its latest succeeded).
    assert summary["failing_scan_config_count"] == 1
    # The single newest job across the project is the healthy config's success, so
    # the legacy latest_scan_job surface alone would have reported "healthy".
    assert summary["latest_scan_job"]["scan_name"] == "Healthy scan"
    assert summary["latest_scan_job"]["status"] == "completed"


async def test_project_summary_failing_config_count_isolates_one_failing_among_healthy(
    client: AsyncClient,
) -> None:
    """One failing config among several succeeding configs counts as exactly 1,
    and an all-healthy project stays at 0.

    Complements ``test_project_summary_counts_configs_whose_latest_run_failed`` by
    proving the per-config rollup isolates a single failing config from a majority
    of healthy ones, and that a project whose configs all last succeeded — even one
    with an OLDER failed run — is never flagged.
    """
    data_source_resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Warehouse",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
            "username": "default",
            "password": "",
        },
    )
    assert data_source_resp.status_code == 201
    data_source_id = data_source_resp.json()["id"]

    async def _make_scan(slug: str, name: str) -> str:
        resp = await client.post(
            f"/api/v1/projects/{slug}/scans",
            json={"data_source_id": data_source_id, "name": name, "base_query": "SELECT 1"},
        )
        assert resp.status_code == 201
        scan_id: str = resp.json()["id"]
        return scan_id

    def _job(scan_config_id: str, status: str, hour: int) -> ScanJob:
        ts = datetime(2026, 4, 18, hour, tzinfo=UTC)
        error = None if status == "completed" else "Scan failed due to an internal error."
        return ScanJob(
            id=uuid.uuid4(),
            scan_config_id=uuid.UUID(scan_config_id),
            status=status,
            started_at=ts,
            completed_at=ts,
            error_message=error,
            created_at=ts,
            updated_at=ts,
        )

    # Project A: three configs — two whose latest run completed and one whose
    # latest run failed → exactly one failing config among succeeding ones.
    mixed_slug = "mixed-scan-proj"
    project_a = await client.post(
        "/api/v1/projects", json={"name": "Mixed Scan Project", "slug": mixed_slug}
    )
    assert project_a.status_code == 201
    healthy_a1 = await _make_scan(mixed_slug, "Healthy A1")
    healthy_a2 = await _make_scan(mixed_slug, "Healthy A2")
    failing_a = await _make_scan(mixed_slug, "Failing A")

    # Project B: two configs whose latest run completed — one of them has an OLDER
    # failed run that must NOT flip the count → the all-healthy case stays 0.
    healthy_slug = "all-healthy-scan-proj"
    project_b = await client.post(
        "/api/v1/projects", json={"name": "All Healthy Project", "slug": healthy_slug}
    )
    assert project_b.status_code == 201
    healthy_b1 = await _make_scan(healthy_slug, "Healthy B1")
    healthy_b2 = await _make_scan(healthy_slug, "Healthy B2")

    async with TestSessionLocal() as session:
        session.add_all(
            [
                _job(healthy_a1, "completed", 10),
                _job(healthy_a2, "completed", 11),
                _job(failing_a, "failed", 12),
                _job(healthy_b1, "completed", 10),
                # healthy_b2 failed earlier but its latest (newer) run succeeded.
                _job(healthy_b2, "failed", 8),
                _job(healthy_b2, "completed", 12),
            ]
        )
        await session.commit()

    resp_a = await client.get(f"/api/v1/projects/{mixed_slug}")
    assert resp_a.status_code == 200
    summary_a = resp_a.json()["summary"]
    assert summary_a["scan_count"] == 3
    # Only "Failing A" has a failing latest run — the two healthy configs do not count.
    assert summary_a["failing_scan_config_count"] == 1

    resp_b = await client.get(f"/api/v1/projects/{healthy_slug}")
    assert resp_b.status_code == 200
    summary_b = resp_b.json()["summary"]
    assert summary_b["scan_count"] == 2
    # Every config's LATEST run succeeded (an older failure does not count) → 0.
    assert summary_b["failing_scan_config_count"] == 0


async def test_project_summary_variable_count_counts_distinct_variables(client: AsyncClient):
    """The summary variable_count is the number of DISTINCT variables.

    A single variable can be referenced across many events, producing one
    ``VariableValue`` context row per (variable, event) pair. The sidebar badge
    must count the variable once, not once per context row — otherwise it drifts
    from the number of variables a user actually defined.
    """
    slug = "var-count-proj"

    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Var Count Project", "slug": slug},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    event_type_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page_view", "display_name": "Page View"},
    )
    assert event_type_resp.status_code == 201
    event_type_id = uuid.UUID(event_type_resp.json()["id"])

    event_ids: list[uuid.UUID] = []
    for name in ("Landing Viewed", "Checkout Started", "Order Placed"):
        event_resp = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={
                "event_type_id": str(event_type_id),
                "name": name,
                "status": "implemented",
            },
        )
        assert event_resp.status_code == 201
        event_ids.append(uuid.UUID(event_resp.json()["id"]))

    variable_resp = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={"name": "spot_id", "variable_type": "string"},
    )
    assert variable_resp.status_code == 201
    variable_id = uuid.UUID(variable_resp.json()["id"])

    # One variable referenced by every event => one context row per event. The
    # summary count must stay 1 (distinct variables), not 3 (context rows).
    async with TestSessionLocal() as session:
        field_def = FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=event_type_id,
            name="spot",
            display_name="Spot",
            field_type=FieldDefinitionType.string.value,
        )
        session.add(field_def)
        await session.flush()
        for event_id in event_ids:
            session.add(
                VariableValue(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    variable_id=variable_id,
                    event_id=event_id,
                    field_definition_id=field_def.id,
                    source_column="spot",
                    value_kind=VariableValueKind.low.value,
                    observed_count=1,
                    values=["a"],
                )
            )
        await session.commit()

    resp = await client.get(f"/api/v1/projects/{slug}")
    assert resp.status_code == 200
    assert resp.json()["summary"]["variable_count"] == 1


# --- Danger zone: project-wide detection reset ------------------------------

# Half-open reset window: rows strictly before ``_RESET_BEFORE`` are cleared.
_IN_PERIOD = datetime(2026, 1, 15, 12, tzinfo=UTC)
_OUT_OF_PERIOD = datetime(2026, 6, 15, 12, tzinfo=UTC)
_RESET_BEFORE = "2026-03-01T00:00:00Z"


async def _setup_reset_project(client: AsyncClient, slug: str) -> dict[str, str]:
    """Create project + data source + scan config + event type + metric def."""
    project_resp = await client.post(
        "/api/v1/projects", json={"name": f"Reset {slug}", "slug": slug}
    )
    assert project_resp.status_code == 201, project_resp.text
    project_id = project_resp.json()["id"]

    ds_resp = await client.post(
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
    assert ds_resp.status_code == 201, ds_resp.text
    data_source_id = ds_resp.json()["id"]

    scan_resp = await client.post(
        f"/api/v1/projects/{slug}/scans",
        json={
            "data_source_id": data_source_id,
            "name": "Production scan",
            "base_query": "SELECT 1",
        },
    )
    assert scan_resp.status_code == 201, scan_resp.text
    scan_config_id = scan_resp.json()["id"]

    et_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page_view", "display_name": "Page View"},
    )
    assert et_resp.status_code == 201, et_resp.text
    event_type_id = et_resp.json()["id"]

    metric_definition_id = str(uuid.uuid4())
    async with TestSessionLocal() as session:
        session.add(
            MetricDefinition(
                id=uuid.UUID(metric_definition_id),
                project_id=uuid.UUID(project_id),
                name="conversion_rate",
                display_name="Conversion Rate",
                kind="sql",
                config={},
                data_source_id=uuid.UUID(data_source_id),
                interval="1h",
                status="active",
                unit="%",
            )
        )
        await session.commit()

    return {
        "project_id": project_id,
        "slug": slug,
        "scan_config_id": scan_config_id,
        "event_type_id": event_type_id,
        "metric_definition_id": metric_definition_id,
    }


def _anomaly(
    scan_config_id: str | None,
    scope_type: str,
    scope_ref: str,
    bucket: datetime,
    *,
    event_type_id: str | None = None,
) -> MetricAnomaly:
    return MetricAnomaly(
        id=uuid.uuid4(),
        scan_config_id=uuid.UUID(scan_config_id) if scan_config_id else None,
        scope_type=scope_type,
        scope_ref=scope_ref,
        event_id=None,
        event_type_id=uuid.UUID(event_type_id) if event_type_id else None,
        bucket=bucket,
        actual_count=42,
        expected_count=21,
        stddev=3,
        z_score=7,
        direction="spike",
    )


def _breakdown(
    scan_config_id: str, event_type_id: str, bucket: datetime, breakdown_value: str
) -> MetricBreakdownAnomaly:
    return MetricBreakdownAnomaly(
        id=uuid.uuid4(),
        scan_config_id=uuid.UUID(scan_config_id),
        scope_type="event_type",
        scope_ref=event_type_id,
        event_id=None,
        event_type_id=uuid.UUID(event_type_id),
        bucket=bucket,
        breakdown_column="platform",
        breakdown_value=breakdown_value,
        is_other=False,
        actual_count=42,
        expected_count=21,
        stddev=3,
        z_score=7,
        direction="spike",
    )


@pytest.mark.asyncio
async def test_project_summary_counts_catalog_metric_signal(client: AsyncClient) -> None:
    """A fresh catalog-metric anomaly (scan_config_id NULL) is counted.

    Catalog metric anomalies carry a NULL scan_config_id and are keyed by
    ``str(metric_definition_id)``, so the project-summary query that inner-joins
    ScanConfig on ``scan_config_id`` silently drops them. ``_populate_monitoring_signals``
    must fold them into ``monitoring_signal_count`` by reusing the same open-signal
    logic the AnomaliesPage uses (``metrics_insights_service._get_active_metric_signals``),
    so the sidebar / ProjectsPage badge agrees with the AnomaliesPage list. There is
    no event/scan-scope anomaly here, so the metric signal is the only one counted —
    proving the count is not gated by the ScanConfig-joined query's early return.
    """
    slug = "catalog-metric-signal-proj"
    project_resp = await client.post(
        "/api/v1/projects", json={"name": "Catalog Metric Signal", "slug": slug}
    )
    assert project_resp.status_code == 201
    project_id = project_resp.json()["id"]

    ds_resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Warehouse",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
            "username": "default",
            "password": "",
        },
    )
    assert ds_resp.status_code == 201
    data_source_id = ds_resp.json()["id"]

    metric_definition_id = uuid.uuid4()
    # Anchor the anomaly and its latest stored value to the same fresh bucket so
    # classify_signal_state keeps the signal open. The metric path passes no scan
    # interval, so the freshness horizon is the default 24h window and a bucket one
    # hour old stays open.
    fresh_bucket = _METRIC_BUCKET
    async with TestSessionLocal() as session:
        session.add(
            MetricDefinition(
                id=metric_definition_id,
                project_id=uuid.UUID(project_id),
                name="conversion_rate",
                display_name="Conversion Rate",
                kind="sql",
                config={},
                data_source_id=uuid.UUID(data_source_id),
                interval="1h",
                status="active",
                unit="%",
            )
        )
        # A stored value bucket is what classify_signal_state anchors recency on;
        # without it the metric signal is treated as closed.
        session.add(
            MetricValue(
                id=uuid.uuid4(),
                metric_definition_id=metric_definition_id,
                scan_config_id=None,
                bucket=fresh_bucket,
                value=1.0,
            )
        )
        # NULL scan_config_id, keyed by the hyphenated metric_definition_id — the
        # metric path matches scope_ref via a Python string, so no cast-hex quirk.
        session.add(_anomaly(None, "metric", str(metric_definition_id), fresh_bucket))
        await session.commit()

    resp = await client.get(f"/api/v1/projects/{slug}")
    assert resp.status_code == 200
    summary = resp.json()["summary"]
    assert summary["monitoring_signal_count"] == 1
    # Metric-scope signals have no scan_config_id and so cannot populate
    # latest_signal (a ProjectLatestSignal requires one); it stays None here.
    assert summary["latest_signal"] is None


@pytest.mark.asyncio
async def test_project_list_counts_catalog_metric_signals_per_project(client: AsyncClient) -> None:
    """The batched metric-scope counter attributes counts to the right project.

    ``_populate_monitoring_signals`` now counts open metric-scope signals for all
    listed projects in one batched query instead of a per-project N+1 loop. This
    guards the batching: two projects each own one fresh catalog-metric anomaly,
    so the LIST endpoint must report ``monitoring_signal_count == 1`` for EACH —
    a per-project grouping bug (double-counting or cross-attribution) would skew
    one of them.
    """
    ds_resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Warehouse",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
            "username": "default",
            "password": "",
        },
    )
    assert ds_resp.status_code == 201
    data_source_id = ds_resp.json()["id"]

    slugs = ["batch-metric-a", "batch-metric-b"]
    async with TestSessionLocal() as session:
        for slug in slugs:
            project_resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
            assert project_resp.status_code == 201
            project_id = uuid.UUID(project_resp.json()["id"])
            metric_definition_id = uuid.uuid4()
            session.add(
                MetricDefinition(
                    id=metric_definition_id,
                    project_id=project_id,
                    name="conversion_rate",
                    display_name="Conversion Rate",
                    kind="sql",
                    config={},
                    data_source_id=uuid.UUID(data_source_id),
                    interval="1h",
                    status="active",
                    unit="%",
                )
            )
            session.add(
                MetricValue(
                    id=uuid.uuid4(),
                    metric_definition_id=metric_definition_id,
                    scan_config_id=None,
                    bucket=_METRIC_BUCKET,
                    value=1.0,
                )
            )
            session.add(_anomaly(None, "metric", str(metric_definition_id), _METRIC_BUCKET))
        await session.commit()

    resp = await client.get("/api/v1/projects")
    assert resp.status_code == 200
    counts = {
        item["slug"]: item["summary"]["monitoring_signal_count"]
        for item in resp.json()
        if item["slug"] in slugs
    }
    assert counts == {"batch-metric-a": 1, "batch-metric-b": 1}, counts


@pytest.mark.asyncio
async def test_project_summary_plan_counts_unchanged_by_plan_branch(client: AsyncClient) -> None:
    """Opening a working branch must not move the plan counters.

    Creating a branch deep-copies every plan entity (event types, events,
    variables) into the SAME tables under a new ``branch_id``. The summary
    counters power the sidebar / project cards, which describe the live plan
    (main), so branch copies must not be counted — production showed "12 event
    types" where main had 3 (3 real x (main + 3 branches)).
    """
    slug = "branch-scoped-summary"
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Branch Scoped Summary", "slug": slug},
    )
    assert project_resp.status_code == 201

    event_type_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page_view", "display_name": "Page View"},
    )
    assert event_type_resp.status_code == 201
    event_type_id = event_type_resp.json()["id"]

    for name, status in (
        ("Landing Viewed", "implemented"),
        ("Checkout Started", "in_review"),
        ("Legacy Event", "archived"),
    ):
        event_resp = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={"event_type_id": event_type_id, "name": name, "status": status},
        )
        assert event_resp.status_code == 201

    variable_resp = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={"name": "user_id", "variable_type": "string"},
    )
    assert variable_resp.status_code == 201

    plan_count_fields = (
        "event_type_count",
        "event_count",
        "active_event_count",
        "implemented_event_count",
        "review_pending_event_count",
        "archived_event_count",
        "variable_count",
    )

    before = await client.get(f"/api/v1/projects/{slug}")
    assert before.status_code == 200
    baseline = {field: before.json()["summary"][field] for field in plan_count_fields}
    assert baseline["event_type_count"] == 1
    assert baseline["event_count"] == 3
    assert baseline["variable_count"] == 1

    branch_resp = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature-x"})
    assert branch_resp.status_code == 201

    after = await client.get(f"/api/v1/projects/{slug}")
    assert after.status_code == 200
    assert {field: after.json()["summary"][field] for field in plan_count_fields} == baseline


@pytest.mark.asyncio
async def test_monitoring_signal_count_includes_significant_event_scope_anomalies(
    client: AsyncClient,
) -> None:
    """Per-event anomalies now count toward the badge when significant.

    The AnomaliesPage now lists every open scope as a flat, magnitude-filtered list,
    so the badge counts the same population — project_total +
    event_type + per-event — and the "Significant" magnitude gate (not scope
    exclusion) is what keeps trivial per-event wobble out.
    Here both the event_type and the per-event anomaly clear the gate (42 vs 21 ->
    relative effect 1.0), so both count.
    """
    slug = "event-scope-signal-proj"
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Event Scope Signal", "slug": slug},
    )
    assert project_resp.status_code == 201

    event_type_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page_view", "display_name": "Page View"},
    )
    assert event_type_resp.status_code == 201
    event_type_id = event_type_resp.json()["id"]

    event_resp = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": event_type_id, "name": "Landing Viewed"},
    )
    assert event_resp.status_code == 201
    event_id = event_resp.json()["id"]

    data_source_resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Warehouse",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
            "username": "default",
            "password": "",
        },
    )
    assert data_source_resp.status_code == 201
    data_source_id = data_source_resp.json()["id"]

    scan_resp = await client.post(
        f"/api/v1/projects/{slug}/scans",
        json={
            "data_source_id": data_source_id,
            "name": "Production scan",
            "base_query": "SELECT 1",
        },
    )
    assert scan_resp.status_code == 201
    scan_config_id = scan_resp.json()["id"]

    async with TestSessionLocal() as session:
        # Fresh metric buckets for BOTH scopes so classify_signal_state would keep
        # each scope's latest anomaly open — without the event-scope bucket the
        # event anomaly is dropped as stale and the regression would not reproduce.
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_config_id),
                event_id=None,
                event_type_id=uuid.UUID(event_type_id),
                bucket=_METRIC_BUCKET,
                count=42,
            )
        )
        # Per-event metric rows are keyed (scan_config, event, bucket) with a NULL
        # event_type_id — uq_event_metric_config_type_bucket forbids a second row
        # per (scan_config, event_type, bucket), so per-event rows cannot carry one.
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_config_id),
                event_id=uuid.UUID(event_id),
                event_type_id=None,
                bucket=_METRIC_BUCKET,
                count=42,
            )
        )
        # Page-visible scope: counted with the detector's UUID representation.
        session.add(
            _anomaly(
                scan_config_id,
                "event_type",
                event_type_id,
                _METRIC_BUCKET,
                event_type_id=event_type_id,
            )
        )
        # Event scope: the expanded AnomaliesPage now lists it and it clears the
        # magnitude gate (42 vs 21 -> relative effect 1.0), so it counts.
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_config_id),
                scope_type="event",
                scope_ref=event_id,
                event_id=uuid.UUID(event_id),
                event_type_id=uuid.UUID(event_type_id),
                bucket=_METRIC_BUCKET,
                actual_count=42,
                expected_count=21,
                stddev=3,
                z_score=7,
                direction="spike",
            )
        )
        await session.commit()

    resp = await client.get(f"/api/v1/projects/{slug}")
    assert resp.status_code == 200
    summary = resp.json()["summary"]
    # Both the event_type and the significant per-event anomaly count now — there is
    # no project_total parent, so no incident dedup applies. Pre-fix (scope
    # exclusion) this was 1.
    assert summary["monitoring_signal_count"] == 2
    # The two share a bucket, so latest_signal is deterministically one of them.
    assert summary["latest_signal"]["scope_type"] in {"event_type", "event"}


@pytest.mark.asyncio
async def test_monitoring_signal_count_excludes_below_significant_signals(
    client: AsyncClient,
) -> None:
    """The magnitude gate — not scope exclusion — bounds the badge.

    A fresh open anomaly whose relative effect is below the "Significant" threshold
    (|actual - expected| / max(expected, 1) < 0.5) is hidden by the AnomaliesPage's
    default filter, so it must not count toward the badge either. Here 44 vs 42 is a
    ~4.8% wobble, well under the gate, so the badge reads 0 despite an open signal.
    """
    slug = "below-significant-proj"
    project_resp = await client.post(
        "/api/v1/projects", json={"name": "Below Significant", "slug": slug}
    )
    assert project_resp.status_code == 201

    event_type_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page_view", "display_name": "Page View"},
    )
    assert event_type_resp.status_code == 201
    event_type_id = event_type_resp.json()["id"]

    data_source_resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Warehouse",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
            "username": "default",
            "password": "",
        },
    )
    assert data_source_resp.status_code == 201
    data_source_id = data_source_resp.json()["id"]

    scan_resp = await client.post(
        f"/api/v1/projects/{slug}/scans",
        json={
            "data_source_id": data_source_id,
            "name": "Production scan",
            "base_query": "SELECT 1",
        },
    )
    assert scan_resp.status_code == 201
    scan_config_id = scan_resp.json()["id"]

    async with TestSessionLocal() as session:
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_config_id),
                event_id=None,
                event_type_id=uuid.UUID(event_type_id),
                bucket=_METRIC_BUCKET,
                count=42,
            )
        )
        # Fresh and open, but a ~4.8% wobble (44 vs 42) — below the magnitude gate.
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_config_id),
                scope_type="event_type",
                scope_ref=event_type_id,
                event_id=None,
                event_type_id=uuid.UUID(event_type_id),
                bucket=_METRIC_BUCKET,
                actual_count=44,
                expected_count=42,
                stddev=3,
                z_score=2,
                direction="spike",
            )
        )
        await session.commit()

    resp = await client.get(f"/api/v1/projects/{slug}")
    assert resp.status_code == 200
    summary = resp.json()["summary"]
    assert summary["monitoring_signal_count"] == 0
    assert summary["latest_signal"] is None


@pytest.mark.asyncio
async def test_reset_anomalies_clears_period_and_covers_metric_scope(client: AsyncClient) -> None:
    ids = await _setup_reset_project(client, "reset-anom")
    scan_config_id = ids["scan_config_id"]
    event_type_id = ids["event_type_id"]
    metric_definition_id = ids["metric_definition_id"]

    event_metric_id = uuid.uuid4()
    regression_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        # In-period event-scope anomaly (deleted) + out-of-period one (kept).
        anomaly_in = _anomaly(
            scan_config_id, "event_type", event_type_id, _IN_PERIOD, event_type_id=event_type_id
        )
        anomaly_out = _anomaly(
            scan_config_id, "event_type", event_type_id, _OUT_OF_PERIOD, event_type_id=event_type_id
        )
        # Project-global metric-scope anomaly: NULL scan_config_id, keyed by the
        # metric definition id. Must be covered by the dual scoping branch.
        anomaly_metric = _anomaly(None, "metric", metric_definition_id, _IN_PERIOD)
        breakdown_in = _breakdown(scan_config_id, event_type_id, _IN_PERIOD, "ios")
        breakdown_out = _breakdown(scan_config_id, event_type_id, _OUT_OF_PERIOD, "android")
        session.add_all([anomaly_in, anomaly_out, anomaly_metric, breakdown_in, breakdown_out])
        # EventMetric + ReleaseRegression must survive the reset (regression guard).
        session.add(
            EventMetric(
                id=event_metric_id,
                scan_config_id=uuid.UUID(scan_config_id),
                event_id=None,
                event_type_id=uuid.UUID(event_type_id),
                bucket=_IN_PERIOD,
                count=5,
            )
        )
        session.add(
            ReleaseRegression(
                id=regression_id,
                scan_config_id=uuid.UUID(scan_config_id),
                scope_type="event_type",
                scope_ref=event_type_id,
                event_id=None,
                event_type_id=uuid.UUID(event_type_id),
                app_version_column="app_version",
                version="2.0.0",
                previous_version="1.0.0",
                kind="volume_drop",
                observed_count=1,
                expected_count=2.0,
                ratio=0.5,
                share_prev=0.5,
                share_new=0.25,
                release_share=0.5,
                window_from=_IN_PERIOD,
                window_to=_OUT_OF_PERIOD,
            )
        )
        await session.commit()
        kept_anomaly_id = anomaly_out.id
        kept_breakdown_id = breakdown_out.id

    resp = await client.post(
        "/api/v1/projects/reset-anom/danger/reset-anomalies",
        json={"before": _RESET_BEFORE},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "metric_anomalies": 2,
        "metric_breakdown_anomalies": 1,
        "metric_baselines": 0,
    }

    async with TestSessionLocal() as session:
        remaining_anomalies = (await session.execute(select(MetricAnomaly))).scalars().all()
        assert {a.id for a in remaining_anomalies} == {kept_anomaly_id}
        remaining_breakdowns = (
            (await session.execute(select(MetricBreakdownAnomaly))).scalars().all()
        )
        assert {b.id for b in remaining_breakdowns} == {kept_breakdown_id}
        # Regression guard: values + release regressions are untouched.
        event_metrics = (await session.execute(select(EventMetric))).scalars().all()
        assert {m.id for m in event_metrics} == {event_metric_id}
        regressions = (await session.execute(select(ReleaseRegression))).scalars().all()
        assert {r.id for r in regressions} == {regression_id}

    # Idempotent: a second reset over the same window deletes nothing.
    again = await client.post(
        "/api/v1/projects/reset-anom/danger/reset-anomalies",
        json={"before": _RESET_BEFORE},
    )
    assert again.status_code == 200
    assert again.json() == {
        "metric_anomalies": 0,
        "metric_breakdown_anomalies": 0,
        "metric_baselines": 0,
    }


@pytest.mark.asyncio
async def test_reset_drifts_clears_schema_and_distribution_in_period(client: AsyncClient) -> None:
    ids = await _setup_reset_project(client, "reset-drift")
    scan_config_id = ids["scan_config_id"]
    event_type_id = ids["event_type_id"]

    async with TestSessionLocal() as session:
        schema_in = SchemaDrift(
            id=uuid.uuid4(),
            event_type_id=uuid.UUID(event_type_id),
            scan_config_id=uuid.UUID(scan_config_id),
            field_name="payload.old",
            drift_type="new_field",
            detected_at=_IN_PERIOD,
        )
        schema_out = SchemaDrift(
            id=uuid.uuid4(),
            event_type_id=uuid.UUID(event_type_id),
            scan_config_id=uuid.UUID(scan_config_id),
            field_name="payload.new",
            drift_type="new_field",
            detected_at=_OUT_OF_PERIOD,
        )
        dist_in = DistributionDrift(
            id=uuid.uuid4(),
            scan_config_id=uuid.UUID(scan_config_id),
            event_type_id=uuid.UUID(event_type_id),
            field_name="platform",
            bucket=_IN_PERIOD,
            psi=0.42,
            band="significant",
            baseline_total=1000,
            current_total=1000,
            top_movers=[],
        )
        dist_out = DistributionDrift(
            id=uuid.uuid4(),
            scan_config_id=uuid.UUID(scan_config_id),
            event_type_id=uuid.UUID(event_type_id),
            field_name="country",
            bucket=_OUT_OF_PERIOD,
            psi=0.30,
            band="significant",
            baseline_total=10,
            current_total=10,
            top_movers=[],
        )
        session.add_all([schema_in, schema_out, dist_in, dist_out])
        await session.commit()
        kept_schema_id = schema_out.id
        kept_dist_id = dist_out.id

    resp = await client.post(
        "/api/v1/projects/reset-drift/danger/reset-drifts",
        json={"before": _RESET_BEFORE},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"schema_drifts": 1, "distribution_drifts": 1}

    async with TestSessionLocal() as session:
        remaining_schema = (await session.execute(select(SchemaDrift))).scalars().all()
        assert {d.id for d in remaining_schema} == {kept_schema_id}
        remaining_dist = (await session.execute(select(DistributionDrift))).scalars().all()
        assert {d.id for d in remaining_dist} == {kept_dist_id}


@pytest.mark.asyncio
async def test_reset_endpoints_are_owner_only(anon_client: AsyncClient) -> None:
    # First registered user is the owner; create the project as owner.
    owner_reg = await anon_client.post(
        "/api/v1/auth/register",
        json={"email": "reset-owner@example.com", "password": "Password123!", "name": "Owner"},
    )
    assert owner_reg.status_code == 201, owner_reg.text
    await anon_client.post("/api/v1/projects", json={"name": "Guarded", "slug": "reset-rbac"})

    # An owner-scope key is still rejected: owner actions need an interactive session.
    key_resp = await anon_client.post(
        "/api/v1/me/api-keys", json={"name": "agent", "scope": "write"}
    )
    assert key_resp.status_code == 201, key_resp.text
    owner_key = key_resp.json()["token"]

    # Second user defaults to editor (non-owner).
    await anon_client.post("/api/v1/auth/logout")
    editor_reg = await anon_client.post(
        "/api/v1/auth/register",
        json={"email": "reset-editor@example.com", "password": "Password123!", "name": "Editor"},
    )
    assert editor_reg.status_code == 201, editor_reg.text
    # A non-member would get 404; the owner-only gate is what is under test.
    await add_member_by_slug("reset-rbac", "reset-editor@example.com", "editor")

    for path in ("reset-anomalies", "reset-drifts"):
        editor_denied = await anon_client.post(
            f"/api/v1/projects/reset-rbac/danger/{path}", json={"before": None}
        )
        assert editor_denied.status_code == 403, editor_denied.text

        key_denied = await anon_client.post(
            f"/api/v1/projects/reset-rbac/danger/{path}",
            json={"before": None},
            headers={"Authorization": f"Bearer {owner_key}"},
        )
        assert key_denied.status_code == 403, key_denied.text
        assert "owner session" in key_denied.json()["detail"].lower()
