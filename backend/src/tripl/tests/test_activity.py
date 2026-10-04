import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from tripl.models.alert_delivery import AlertDelivery
from tripl.models.event import Event
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.project import Project
from tripl.models.scan_job import ScanJob
from tripl.services.activity_service import _scan_job_detail
from tripl.tests.conftest import TestSessionLocal


@pytest.mark.asyncio
async def test_project_activity_feed_uses_real_backend_records(client: AsyncClient):
    slug = "activity-proj"
    await client.post("/api/v1/projects", json={"name": "Activity Project", "slug": slug})

    event_type_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page_view", "display_name": "Page View"},
    )
    event_type_id = event_type_resp.json()["id"]

    await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type_id,
            "name": "Landing Viewed",
            "status": "in_review",
        },
    )

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
    scan_resp = await client.post(
        f"/api/v1/projects/{slug}/scans",
        json={
            "data_source_id": data_source_resp.json()["id"],
            "name": "Production scan",
            "base_query": "SELECT 1",
        },
    )
    scan_config_id = scan_resp.json()["id"]

    destination_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "slack",
            "name": "Ops",
            "webhook_url": "https://hooks.slack.com/services/T000/B000/XXX",
        },
    )
    destination_id = destination_resp.json()["id"]
    rule_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules",
        json={"name": "Spike alerts"},
    )
    rule_id = rule_resp.json()["id"]
    project_id = destination_resp.json()["project_id"]

    # The anomaly must be recent: the activity rail only surfaces anomalies
    # inside ANOMALY_RECENCY_WINDOW, so a fixed historical timestamp would be
    # filtered out and this assertion would flake as wall-clock time advances.
    anomaly_created_at = datetime.now(UTC)
    async with TestSessionLocal() as session:
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_config_id),
                scope_type="event_type",
                scope_ref=event_type_id,
                event_id=None,
                event_type_id=uuid.UUID(event_type_id),
                bucket=anomaly_created_at,
                actual_count=42,
                expected_count=21,
                stddev=3,
                z_score=7,
                direction="spike",
                created_at=anomaly_created_at,
            )
        )
        session.add(
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_config_id),
                status="completed",
                started_at=datetime(2026, 4, 18, 10, tzinfo=UTC),
                completed_at=datetime(2026, 4, 18, 10, 5, tzinfo=UTC),
                result_summary={"events_created": 4, "signals_added": 1},
                error_message=None,
                created_at=datetime(2026, 4, 18, 10, 5, tzinfo=UTC),
                updated_at=datetime(2026, 4, 18, 10, 5, tzinfo=UTC),
            )
        )
        session.add(
            AlertDelivery(
                id=uuid.uuid4(),
                project_id=uuid.UUID(project_id),
                scan_config_id=uuid.UUID(scan_config_id),
                scan_job_id=None,
                destination_id=uuid.UUID(destination_id),
                rule_id=uuid.UUID(rule_id),
                status="sent",
                channel="slack",
                matched_count=1,
                payload_snapshot={"preview": "one alert"},
                error_message=None,
                sent_at=datetime(2026, 4, 18, 11, tzinfo=UTC),
                created_at=datetime(2026, 4, 18, 10, 59, tzinfo=UTC),
                updated_at=datetime(2026, 4, 18, 11, tzinfo=UTC),
            )
        )
        await session.commit()

    resp = await client.get(f"/api/v1/activity/projects/{slug}?limit=10")
    assert resp.status_code == 200
    items = resp.json()

    assert all(item["project_slug"] == slug for item in items)
    assert any(
        item["type"] == "anomaly" and item["title"] == "Spike on Page View" for item in items
    )
    assert any(
        item["type"] == "scan" and item["title"] == "Scan completed: Production scan"
        for item in items
    )
    assert any(
        item["type"] == "alert" and item["title"] == "Alert sent: Spike alerts" for item in items
    )
    assert any(
        item["type"] == "event" and item["title"] == "Event needs review: Landing Viewed"
        for item in items
    )
    event_items = [item for item in items if item["type"] == "event"]
    assert event_items, "expected an event activity item"
    assert all(
        item["target_path"].startswith(f"/o/default/p/{slug}/monitoring/event/")
        for item in event_items
    )
    assert not any("/events/detail/" in (item["target_path"] or "") for item in items)

    # Scans moved to the top-level `/p/<slug>/scans` route. The feed's own deep
    # link has to follow, or every scan row in the activity rail lands on the
    # legacy path and has to be bounced through a redirect.
    scan_items = [item for item in items if item["type"] == "scan"]
    assert scan_items, "expected a scan activity item"
    assert all(item["target_path"] == f"/o/default/p/{slug}/scans" for item in scan_items)
    assert not any("/settings/scans" in (item["target_path"] or "") for item in items)


@pytest.mark.asyncio
async def test_activity_feed_excludes_stale_anomalies(client: AsyncClient):
    """A week-old anomaly must drop off the rail while a fresh one stays.

    Regression for the 'Now' rail pinning weeks-old high-z anomalies at the top
    of the feed on every page.
    """
    slug = "activity-recency"
    await client.post("/api/v1/projects", json={"name": "Recency Project", "slug": slug})

    event_type_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page_view", "display_name": "Page View"},
    )
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
    scan_resp = await client.post(
        f"/api/v1/projects/{slug}/scans",
        json={
            "data_source_id": data_source_resp.json()["id"],
            "name": "Production scan",
            "base_query": "SELECT 1",
        },
    )
    scan_config_id = scan_resp.json()["id"]

    now = datetime.now(UTC)
    fresh_at = now - timedelta(hours=1)
    # Comfortably outside the 7-day window, mirroring the reported z=16.7 rows.
    stale_at = now - timedelta(days=30)
    async with TestSessionLocal() as session:
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_config_id),
                scope_type="event_type",
                scope_ref=event_type_id,
                event_id=None,
                event_type_id=uuid.UUID(event_type_id),
                bucket=fresh_at,
                actual_count=42,
                expected_count=21,
                stddev=3,
                z_score=7,
                direction="spike",
                created_at=fresh_at,
            )
        )
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_config_id),
                scope_type="event_type",
                scope_ref=event_type_id,
                event_id=None,
                event_type_id=uuid.UUID(event_type_id),
                bucket=stale_at,
                actual_count=900,
                expected_count=10,
                stddev=2,
                z_score=16,
                direction="spike",
                created_at=stale_at,
            )
        )
        await session.commit()

    resp = await client.get(f"/api/v1/activity/projects/{slug}?limit=20")
    assert resp.status_code == 200
    anomaly_items = [item for item in resp.json() if item["type"] == "anomaly"]

    assert len(anomaly_items) == 1, "only the fresh anomaly should surface"
    detail = anomaly_items[0]["detail"]
    assert "42 actual" in detail
    assert "z=7" in detail
    # The 30-day-old anomaly (z=16) must not leak into the rail.
    assert "900 actual" not in detail
    assert not any("z=16" in item["detail"] for item in anomaly_items)


@pytest.mark.asyncio
async def test_activity_feed_collapses_one_incident_into_one_rail_item(client: AsyncClient):
    """A project-total spike and its child event_type spike are one incident.

    They share a scan, bucket and direction, so the Now rail must surface the
    single parent project_total item and suppress the co-firing child instead of
    stacking both. Layered on top of Wave 1's recency filter.
    """
    slug = "activity-incident"
    await client.post("/api/v1/projects", json={"name": "Incident Rail", "slug": slug})

    event_type_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page_view", "display_name": "Page View"},
    )
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
    scan_resp = await client.post(
        f"/api/v1/projects/{slug}/scans",
        json={
            "data_source_id": data_source_resp.json()["id"],
            "name": "Production scan",
            "base_query": "SELECT 1",
        },
    )
    scan_config_id = scan_resp.json()["id"]

    at = datetime.now(UTC) - timedelta(hours=1)
    async with TestSessionLocal() as session:
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_config_id),
                scope_type="project_total",
                scope_ref=scan_config_id,
                event_id=None,
                event_type_id=None,
                bucket=at,
                actual_count=480,
                expected_count=120,
                stddev=40,
                z_score=9,
                direction="spike",
                created_at=at,
            )
        )
        # Child of the SAME incident: same scan, same bucket, same direction.
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_config_id),
                scope_type="event_type",
                scope_ref=event_type_id,
                event_id=None,
                event_type_id=uuid.UUID(event_type_id),
                bucket=at,
                actual_count=300,
                expected_count=90,
                stddev=30,
                z_score=7,
                direction="spike",
                created_at=at,
            )
        )
        await session.commit()

    resp = await client.get(f"/api/v1/activity/projects/{slug}?limit=20")
    assert resp.status_code == 200
    anomaly_items = [item for item in resp.json() if item["type"] == "anomaly"]

    assert len(anomaly_items) == 1, anomaly_items
    # The surviving rail item is the parent project_total, not the child scope.
    assert anomaly_items[0]["title"] == "Spike on Incident Rail total"
    assert "/monitoring/project-total/" in anomaly_items[0]["target_path"]


@pytest.mark.asyncio
async def test_project_activity_feed_returns_404_for_unknown_project(client: AsyncClient):
    resp = await client.get("/api/v1/activity/projects/missing-project")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_an_open_branch_does_not_add_a_second_unreachable_copy_of_each_event(
    client: AsyncClient,
):
    """A working branch deep-copies the plan; the rail must stay on main.

    Previously the event query joined Event to Project alone, so every
    open branch contributed its own clone of each event. The rail linked to
    whichever copy won an arbitrary tiebreak, and a branch-local id 404s: the
    monitoring page resolves ids against main. Asserting on the row count alone
    would still pass on a feed that dropped the branch copy but kept linking to
    it, so this follows the emitted link to the API the page calls.
    """
    slug = "branch-rail"
    await client.post("/api/v1/projects", json={"name": "Branch Rail", "slug": slug})
    event_type_id = (
        await client.post(
            f"/api/v1/projects/{slug}/event-types",
            json={"name": "checkout", "display_name": "Checkout"},
        )
    ).json()["id"]
    main_event_id = (
        await client.post(
            f"/api/v1/projects/{slug}/events",
            json={"event_type_id": event_type_id, "name": "Order Placed"},
        )
    ).json()["id"]

    branch_resp = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "wip"})
    assert branch_resp.status_code == 201
    # The clone is what the rail used to double-count, so prove it exists rather
    # than assuming deep_copy_plan_to_branch ran.
    branch_id = branch_resp.json()["id"]
    branch_events = (await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")).json()[
        "items"
    ]
    assert [event["name"] for event in branch_events] == ["Order Placed"]
    assert branch_events[0]["id"] != main_event_id

    items = (await client.get(f"/api/v1/activity/projects/{slug}?limit=50")).json()
    event_items = [item for item in items if item["type"] == "event"]

    assert len(event_items) == 1, f"one event, one rail row; got {len(event_items)}"
    assert event_items[0]["target_path"] == f"/o/default/p/{slug}/monitoring/event/{main_event_id}"

    linked_id = event_items[0]["target_path"].rsplit("/", 1)[-1]
    followed = await client.get(f"/api/v1/projects/{slug}/events/{linked_id}")
    assert followed.status_code == 200, "the rail linked to an id the detail page cannot resolve"


@pytest.mark.asyncio
async def test_an_anomaly_whose_event_was_deleted_links_nowhere_rather_than_to_a_dead_id(
    client: AsyncClient,
):
    """``scope_ref`` outlives the row it names; the FK does not.

    ``MetricAnomaly.event_id`` is ``ondelete=SET NULL`` while ``scope_ref`` is a
    plain ``String(64)`` the detector writes for dedupe keying, so deleting an
    event leaves a stale uuid there. Routing off it produced a link to a 404.

    The row is seeded in its post-deletion shape (FK NULL, ``scope_ref`` still
    holding the uuid) rather than by deleting through the API: SQLite does not
    enforce ``ON DELETE`` actions in this suite, so a delete would not reproduce
    the state the fix is about.
    """
    slug = "dead-link-rail"
    await client.post("/api/v1/projects", json={"name": "Dead Link Rail", "slug": slug})
    data_source_id = (
        await client.post(
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
    ).json()["id"]
    scan_config_id = (
        await client.post(
            f"/api/v1/projects/{slug}/scans",
            json={"data_source_id": data_source_id, "name": "Scan", "base_query": "SELECT 1"},
        )
    ).json()["id"]

    deleted_event_id = uuid.uuid4()
    created_at = datetime.now(UTC)
    async with TestSessionLocal() as session:
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_config_id),
                scope_type="event",
                scope_ref=str(deleted_event_id),
                event_id=None,
                event_type_id=None,
                bucket=created_at,
                actual_count=99,
                expected_count=10,
                stddev=2,
                z_score=8,
                direction="spike",
                created_at=created_at,
            )
        )
        await session.commit()

    items = (await client.get(f"/api/v1/activity/projects/{slug}?limit=50")).json()
    anomaly_items = [item for item in items if item["type"] == "anomaly"]

    assert len(anomaly_items) == 1, "the anomaly still belongs in the feed"
    assert anomaly_items[0]["target_path"] is None


@pytest.mark.asyncio
async def test_another_projects_main_branch_does_not_satisfy_the_main_branch_filter(
    client: AsyncClient,
):
    """ "Main" means main OF THIS PROJECT, and the join has to say so.

    ``events.branch_id`` is an FK to ``plan_branches.id`` with nothing at the
    schema level tying the two to the same project, so a filter on
    ``kind == main`` alone would also accept a different project's main branch.
    No write path can produce that today — the API validates the override in
    ``resolve_branch_id``, the column default derives the branch from the row's
    own ``project_id`` — so the state is seeded directly, which is the only way
    to hold the invariant the join now expresses.
    """
    await client.post("/api/v1/projects", json={"name": "Rail Host", "slug": "rail-host"})
    await client.post("/api/v1/projects", json={"name": "Rail Other", "slug": "rail-other"})
    host_type_id = (
        await client.post(
            "/api/v1/projects/rail-host/event-types",
            json={"name": "signup", "display_name": "Signup"},
        )
    ).json()["id"]
    host_event_id = (
        await client.post(
            "/api/v1/projects/rail-host/events",
            json={"event_type_id": host_type_id, "name": "Signed Up"},
        )
    ).json()["id"]

    async with TestSessionLocal() as session:
        other_main_branch_id = await session.scalar(
            select(PlanBranch.id)
            .join(Project, Project.id == PlanBranch.project_id)
            .where(Project.slug == "rail-other", PlanBranch.kind == BranchKind.main.value)
        )
        assert other_main_branch_id is not None, "the other project must have a main branch"
        await session.execute(
            update(Event)
            .where(Event.id == uuid.UUID(host_event_id))
            .values(branch_id=other_main_branch_id)
        )
        await session.commit()

    items = (await client.get("/api/v1/activity/projects/rail-host?limit=50")).json()
    assert [item for item in items if item["type"] == "event"] == []


class TestScanJobDetail:
    """Unit coverage for the scan/collection activity detail line.

    A healthy run on an established catalog routinely creates 0 new events while
    still writing metric points and scanning rows; the detail must read as work
    done, never as a bare "0 events created" that looks like a no-op / failure.
    """

    def test_zero_events_with_metric_points_reads_as_work_not_a_no_op(self):
        detail = _scan_job_detail(
            "completed",
            {
                "events_created": 0,
                "event_metrics": 1200,
                "breakdown_event_metrics": 248,
                "query_rows_scanned": 1700,
            },
            None,
        )
        assert detail == "1448 metric points · 1700 rows scanned"
        assert "0 events" not in detail

    def test_true_zero_completed_run_says_no_new_events(self):
        detail = _scan_job_detail("completed", {"events_created": 0}, None)
        assert detail == "no new events discovered"

    def test_true_zero_run_still_reports_what_it_scanned(self):
        detail = _scan_job_detail(
            "completed",
            {"events_created": 0, "scan_rows_processed": 512},
            None,
        )
        assert detail == "no new events discovered · 512 column combinations"

    def test_catalog_run_names_combinations_not_rows(self):
        """scan_rows_processed counts the catalog analyzer's GROUP BY breakdown:
        distinct column combinations, not warehouse rows. The scan page prints
        "153 combos" for the same run, so this line must not say "rows" (#247).
        A run reporting query_rows_scanned still reads as rows."""
        catalog = _scan_job_detail("completed", {"scan_rows_processed": 153}, None)
        assert catalog == "153 column combinations"
        assert "row" not in catalog
        both = _scan_job_detail(
            "completed",
            {"scan_rows_processed": 900, "query_rows_scanned": 12345},
            None,
        )
        assert both == "12345 rows scanned"

    def test_non_zero_events_and_signals_are_summarised(self):
        detail = _scan_job_detail(
            "completed",
            {"events_created": 4, "signals_added": 1, "alerts_queued": 2},
            None,
        )
        assert detail == "4 new events · 1 new signal · 2 alerts queued"

    def test_signals_added_is_labelled_as_a_delta_not_a_total(self):
        """signals_added is a per-run delta. A bare "1 signal" beside a run that
        detected 560 anomalies read as the project's open-signal total and could
        not be reconciled with the Anomalies headline."""
        detail = _scan_job_detail(
            "completed",
            {"events_created": 0, "anomalies_detected": 560, "signals_added": 1},
            None,
        )
        assert "1 new signal" in detail
        assert "· 1 signal" not in detail

    def test_singular_units(self):
        detail = _scan_job_detail(
            "completed",
            {"events_created": 1, "type_metrics": 1, "query_rows_scanned": 1},
            None,
        )
        assert detail == "1 new event · 1 metric point · 1 row scanned"
        catalog = _scan_job_detail("completed", {"scan_rows_processed": 1}, None)
        assert catalog == "1 column combination"

    def test_failed_status_surfaces_error_message(self):
        detail = _scan_job_detail("failed", {"events_created": 3}, "connection refused")
        assert detail == "connection refused"

    def test_boolean_flags_are_not_counted_as_quantities(self):
        # scan_truncated is a bool; it must never leak into a count. With no real
        # deltas this is a healthy completed run → "no new events discovered".
        detail = _scan_job_detail(
            "completed",
            {"events_created": 0, "scan_truncated": True},
            None,
        )
        assert detail == "no new events discovered"

    def test_running_job_without_summary_reports_status(self):
        assert _scan_job_detail("running", None, None) == "Run status changed"

    def test_no_fallback_uses_the_api_spelling_job(self):
        """The activity rail is a web-UI surface, so it says *run*.

        feature-reference.md states the `job` spelling lives in the API and the
        CLI only, and activity-panel.tsx renders `detail` verbatim.
        """
        for status in ("pending", "running", "completed", "cancelled", "failed"):
            detail = _scan_job_detail(status, None, None)
            assert "job" not in detail.lower(), f"{status!r} detail still says job: {detail!r}"

    def test_completed_fallback_does_not_claim_a_metrics_collection(self):
        """A completed metrics collection ALWAYS carries ``events_created``
        (collect_metrics writes it unconditionally), so this branch is never one.
        It is reached by an event-groups apply, which the old wording
        "Metrics collection job updated" mislabelled as metrics work."""
        detail = _scan_job_detail(
            "completed",
            {"mode": "event_groups_apply", "events_merged": 0, "event_types_processed": 4},
            None,
        )
        assert detail == "Run finished with no counts to report"
        assert "Metrics collection" not in detail
