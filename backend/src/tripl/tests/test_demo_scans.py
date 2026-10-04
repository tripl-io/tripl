"""Demo scans + governance.

The demo's scan surface is a local synthetic warehouse: scan history, coverage,
and reconciliation are seeded coherently with the scanned volume, and the real
warehouse-facing data path (Preview / Run now / Replay) works over the synthetic
source with no network. Full Celery worker-transition E2E is covered separately.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.core.adapters.registry import build_adapter
from tripl.models.data_source import DataSource, DBType
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.field_definition import FieldDefinition
from tripl.models.scan_config import ScanConfig
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks.scan import _scan_with_grouping


async def _demo_slug(client: AsyncClient) -> str:
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 201
    return resp.json()["slug"]


async def _scan_id(client: AsyncClient, slug: str) -> str:
    resp = await client.get(f"/api/v1/projects/{slug}/scans")
    assert resp.status_code == 200
    configs = resp.json()
    assert configs, "demo should have a seeded scan config"
    return configs[0]["id"]


@pytest.mark.asyncio
async def test_demo_has_scan_history_latest_successful(client: AsyncClient) -> None:
    slug = await _demo_slug(client)
    scan_id = await _scan_id(client, slug)

    jobs_resp = await client.get(f"/api/v1/projects/{slug}/scans/{scan_id}/jobs")
    assert jobs_resp.status_code == 200
    jobs = jobs_resp.json()
    assert len(jobs) >= 2, "expected a run cadence of scan jobs"

    statuses = [j["status"] for j in jobs]
    assert "completed" in statuses
    assert "failed" in statuses, "expected one safely-explained historical failure"

    # Latest job (jobs are returned newest-first) is a success.
    assert jobs[0]["status"] == "completed"
    assert jobs[0]["result_summary"] is not None


@pytest.mark.asyncio
async def test_demo_project_summary_is_healthy(client: AsyncClient) -> None:
    slug = await _demo_slug(client)
    resp = await client.get(f"/api/v1/projects/{slug}")
    summary = resp.json()["summary"]
    # A config whose LATEST job failed would count here; the demo's latest is
    # completed, so a demo reads healthy despite the historical failure.
    assert summary["failing_scan_config_count"] == 0
    assert summary["latest_scan_job"] is not None
    assert summary["latest_scan_job"]["status"] == "completed"


@pytest.mark.asyncio
async def test_demo_coverage_reconciles_with_volume(client: AsyncClient) -> None:
    slug = await _demo_slug(client)
    resp = await client.get(f"/api/v1/projects/{slug}/reconciliation/coverage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"], "coverage should be non-empty"
    # Coverage is internally consistent: matched never exceeds scanned total.
    for bucket in body["items"]:
        assert bucket["matched_count"] <= bucket["total_count"]
    assert body["summary"]["matched_count"] <= body["summary"]["total_count"]
    assert body["summary"]["matched_count"] > 0


@pytest.mark.asyncio
async def test_demo_has_shadow_candidates_outside_plan(client: AsyncClient) -> None:
    slug = await _demo_slug(client)
    resp = await client.get(f"/api/v1/projects/{slug}/reconciliation/shadow-events")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    names = {item["event_name"] for item in body["items"]}
    # The seeded candidate is a warehouse identity with no plan event.
    assert "app_heartbeat_v1" in names


@pytest.mark.asyncio
async def test_demo_shadow_candidate_can_be_accepted(client: AsyncClient) -> None:
    slug = await _demo_slug(client)
    listed = await client.get(f"/api/v1/projects/{slug}/reconciliation/shadow-events")
    assert listed.status_code == 200
    candidate = next(
        item for item in listed.json()["items"] if item["event_name"] == "app_heartbeat_v1"
    )

    accepted = await client.post(
        f"/api/v1/projects/{slug}/reconciliation/shadow-events/{candidate['id']}/accept",
        json={},
    )

    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"


@pytest.mark.asyncio
async def test_demo_synthetic_source_is_labeled_and_never_real(client: AsyncClient) -> None:
    await _demo_slug(client)
    ds_resp = await client.get("/api/v1/data-sources")
    assert ds_resp.status_code == 200
    synthetic = [d for d in ds_resp.json() if d["is_synthetic"]]
    assert synthetic, "the demo warehouse must be labeled as local synthetic data"
    # It is never presented as a real connection type.
    assert all(d["db_type"] == "synthetic" for d in synthetic)


@pytest.mark.asyncio
async def test_demo_warehouse_path_runs_over_synthetic_no_network(client: AsyncClient) -> None:
    # The real warehouse-facing path (what Preview / Run now / Replay traverse)
    # works over the synthetic source with no network: build the same adapter the
    # worker builds and read rows from the in-memory dataset.
    slug = await _demo_slug(client)
    async with TestSessionLocal() as session:
        ds = (
            await session.execute(select(DataSource).where(DataSource.db_type == DBType.synthetic))
        ).scalar_one()
        adapter = build_adapter(ds)

    assert adapter.test_connection() is True  # honest local check, no host contacted

    now = datetime.now(UTC)
    columns, rows = adapter.get_preview_rows(
        "SELECT * FROM events",
        limit=20,
        time_column="event_time",
        time_from=now - timedelta(days=2),
        time_to=now,
    )
    assert rows, "synthetic source should return real preview rows"
    assert "event_type" in columns
    # The scanned event types are exactly the authored plan's types.
    type_idx = columns.index("event_type")
    seen_types = {row[type_idx] for row in rows}
    assert seen_types <= {"screen_view", "click", "purchase"}
    adapter.close()
    # Sanity: the demo slug and source resolve without any real host.
    assert slug.startswith("demo-")


def _non_archived(items: list[dict]) -> list[dict]:
    return [item for item in items if item["status"] != "archived"]


async def _observed_values(client: AsyncClient, slug: str) -> dict[str, tuple[int, list[str]]]:
    """``{variable name: (event_count, sample_values)}`` — the Variables table's two columns."""
    resp = await client.get(f"/api/v1/projects/{slug}/variables")
    assert resp.status_code == 200
    return {
        variable["name"]: (variable["event_count"], sorted(variable["sample_values"]))
        for variable in resp.json()["items"]
    }


@pytest.mark.asyncio
async def test_demo_rescan_folds_synthetic_events_without_flooding_catalog(
    client: AsyncClient,
) -> None:
    """A Run now / Replay over the synthetic source creates 0 new events.

    Regression: the demo ScanConfig used to seed
    ``event_type_column`` but no ``event_group_rules``, so ``generate_events``
    derived a raw ``col=value | col=value`` identity for every synthetic row and
    flooded the catalog with ~21 pipe-named draft events (17 -> 38). The demo now
    ships one anchored ``event_name`` group rule per distinct synthetic event
    name, so every synthetic identity folds back onto its curated catalog event.
    """
    slug = await _demo_slug(client)
    scan_id = await _scan_id(client, slug)

    # Baseline catalog + reconciliation coverage before the rescan.
    events_before = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"]
    catalog_before = len(_non_archived(events_before))
    assert catalog_before == 17, "demo seeds 17 curated (non-archived) events"

    cov_before = (await client.get(f"/api/v1/projects/{slug}/reconciliation/coverage")).json()
    matched_before = cov_before["summary"]["matched_count"]
    assert matched_before > 0

    # Run the real grouped-scan path (what Run now / Replay traverse) over the
    # synthetic source. generate_events needs a sync Session, so bridge into the
    # same in-memory DB via ``run_sync`` (the SQLAlchemy-sanctioned async->sync
    # bridge) exactly as the worker's run_scan would drive it.
    async with TestSessionLocal() as session:
        ds = (
            await session.execute(select(DataSource).where(DataSource.db_type == DBType.synthetic))
        ).scalar_one()
        project_id = ds.project_id
        adapter = build_adapter(ds)

        def _rescan(sync_session: Session) -> object:
            config = sync_session.get(ScanConfig, uuid.UUID(scan_id))
            assert config is not None
            # The demo rules must be present — this is the fix under test.
            assert config.event_group_rules, "demo scan config must ship event_group_rules"
            columns = [
                column
                for column in adapter.get_columns(config.base_query)
                if column.name != config.time_column
            ]
            result, _groups, _rows, _warehouse_rows = _scan_with_grouping(
                sync_session,
                config.project_id,
                config,
                adapter,
                columns,
                scan_window=None,  # scan the whole bounded dataset
                row_limit=100000,
            )
            return result

        result = await session.run_sync(_rescan)
        await session.commit()
        adapter.close()

    # Every synthetic identity folded onto an existing curated event: nothing new
    # was created, and the rows were grouped rather than turned into fresh drafts.
    assert result.events_created == 0
    assert result.events_grouped > 0

    # No raw pipe-named draft leaked into the catalog.
    async with TestSessionLocal() as session:
        names = (
            (await session.execute(select(Event.name).where(Event.project_id == project_id)))
            .scalars()
            .all()
        )
    assert all(" | " not in name for name in names), names

    # Catalog and reconciliation coverage are unchanged by the rescan.
    events_after = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"]
    assert len(_non_archived(events_after)) == catalog_before

    cov_after = (await client.get(f"/api/v1/projects/{slug}/reconciliation/coverage")).json()
    assert cov_after["summary"]["matched_count"] == matched_before


@pytest.mark.asyncio
async def test_demo_rescan_keeps_seeded_variable_values_and_templates(
    client: AsyncClient,
) -> None:
    """The demo's variable story survives its own guided first scan.

    Regression: the coached "Variables & value drift"
    chapter tells the user to run a scan and then compare ``${product_id}``'s
    observed values against its documented list — but the scan rewrote the
    curated ``${product_id}`` / ``${platform}`` templates with literal warehouse
    values and then wiped EVERY variable context of the scanned event types, so
    the chapter opened on two empty columns (events "—", observed values "—").
    """
    slug = await _demo_slug(client)
    scan_id = await _scan_id(client, slug)

    observed_before = await _observed_values(client, slug)
    assert sorted(observed_before["product_id"][1]) == [
        "prod_annual",
        "prod_lifetime",
        "prod_monthly",
    ], observed_before
    assert all(count > 0 for count, _ in observed_before.values()), observed_before

    async with TestSessionLocal() as session:
        ds = (
            await session.execute(select(DataSource).where(DataSource.db_type == DBType.synthetic))
        ).scalar_one()
        project_id = ds.project_id
        adapter = build_adapter(ds)

        def _rescan(sync_session: Session) -> None:
            config = sync_session.get(ScanConfig, uuid.UUID(scan_id))
            assert config is not None
            columns = [
                column
                for column in adapter.get_columns(config.base_query)
                if column.name != config.time_column
            ]
            _scan_with_grouping(
                sync_session,
                config.project_id,
                config,
                adapter,
                columns,
                scan_window=None,
                row_limit=100000,
            )

        await session.run_sync(_rescan)
        await session.commit()
        adapter.close()

    assert await _observed_values(client, slug) == observed_before

    # The templates the chapter points at are still templates, not literals.
    async with TestSessionLocal() as session:
        rows = (
            await session.execute(
                select(Event.name, FieldDefinition.name, EventFieldValue.value)
                .join(EventFieldValue, EventFieldValue.event_id == Event.id)
                .join(
                    FieldDefinition,
                    FieldDefinition.id == EventFieldValue.field_definition_id,
                )
                .where(Event.project_id == project_id)
            )
        ).all()
    values_by_event: dict[str, dict[str, str]] = {}
    for event_name, field_name, value in rows:
        values_by_event.setdefault(event_name, {})[field_name] = value
    assert values_by_event["Purchase Completed"]["product_id"] == "${product_id}"
    assert values_by_event["Home Screen View"]["platform"] == "${platform}"
