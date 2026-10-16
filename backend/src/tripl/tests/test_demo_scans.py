"""Demo scans + governance.

The demo's scan surface is the demo warehouse (here its in-memory engine): the
scan history is the provisioning's own catalog scan and replay jobs, coverage
reconciles with the scanned volume, and the real warehouse-facing data path
(Preview / Run now / Replay) works over the demo source with no network. Full
Celery worker-transition E2E is covered separately.

One provisioned demo carries every check: a provisioning is the expensive part.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.core.adapters.registry import build_adapter
from tripl.core.demo_warehouse import EVENTS_SCAN_SQL, roster
from tripl.models.data_source import DataSource, DBType
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.field_definition import FieldDefinition
from tripl.models.scan_config import ScanConfig
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks.scan import _scan_with_grouping

#: How far back the rescan below reads: the catalog scan's own lookback.
_RESCAN_HOURS = 3


async def _observed_values(client: AsyncClient, slug: str) -> dict[str, tuple[int, list[str]]]:
    """``{variable name: (event_count, sample_values)}`` — the Variables table's two columns."""
    resp = await client.get(f"/api/v1/projects/{slug}/variables")
    assert resp.status_code == 200
    return {
        variable["name"]: (variable["event_count"], sorted(variable["sample_values"]))
        for variable in resp.json()["items"]
    }


async def _assert_history_and_coverage(client: AsyncClient, slug: str, scan_id: str) -> None:
    jobs_resp = await client.get(f"/api/v1/projects/{slug}/scans/{scan_id}/jobs")
    assert jobs_resp.status_code == 200
    jobs = jobs_resp.json()
    # The catalog scan, the replay and the first live collection: real runs,
    # every one of which finished (a failed stage fails the whole provisioning).
    assert len(jobs) >= 2
    assert {job["status"] for job in jobs} == {"completed"}
    assert jobs[0]["result_summary"] is not None

    summary = (await client.get(f"/api/v1/projects/{slug}")).json()["summary"]
    assert summary["failing_scan_config_count"] == 0
    assert summary["latest_scan_job"] is not None
    assert summary["latest_scan_job"]["status"] == "completed"

    coverage = await client.get(f"/api/v1/projects/{slug}/reconciliation/coverage")
    assert coverage.status_code == 200
    body = coverage.json()
    assert body["items"], "coverage should be non-empty"
    for bucket in body["items"]:
        assert bucket["matched_count"] <= bucket["total_count"]
    assert 0 < body["summary"]["matched_count"] <= body["summary"]["total_count"]

    sources = (await client.get("/api/v1/data-sources")).json()
    warehouses = [source for source in sources if source["db_type"] == "demo"]
    assert len(warehouses) == 1
    # Badged as demo data, never presented as a real connection.
    assert warehouses[0]["is_synthetic"] is True


@pytest.mark.asyncio
async def test_the_demo_scan_surface_runs_on_the_demo_warehouse(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202
    slug = resp.json()["slug"]
    configs = (await client.get(f"/api/v1/projects/{slug}/scans")).json()
    assert configs, "demo should have its catalog scan config"
    scan_id = configs[0]["id"]

    await _assert_history_and_coverage(client, slug, scan_id)

    async with TestSessionLocal() as session:
        ds = (
            await session.execute(select(DataSource).where(DataSource.db_type == DBType.demo))
        ).scalar_one()
        project_id = ds.project_id
        adapter = build_adapter(ds)

    # The real warehouse-facing path (what Preview / Run now / Replay traverse)
    # reads rows with no network: an honest local check, no host contacted.
    assert adapter.test_connection() is True
    now = datetime.now(UTC)
    columns, rows = adapter.get_preview_rows(
        EVENTS_SCAN_SQL,
        limit=20,
        time_column="event_time",
        time_from=now - timedelta(days=1),
        time_to=now,
    )
    assert rows, "the demo source should return real preview rows"
    type_idx = columns.index("event_type")
    known_types = {event_type.name for event_type in roster.event_types()}
    assert {row[type_idx] for row in rows} <= known_types

    events_before = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"]
    observed_before = await _observed_values(client, slug)
    # The scan read the products the warehouse sells, the undocumented one too.
    assert observed_before["product_id"][0] > 0
    cov_before = (await client.get(f"/api/v1/projects/{slug}/reconciliation/coverage")).json()

    # A rescan over the real grouped-scan path (what Run now / Replay traverse).
    # generate_events needs a sync Session, so bridge into the same in-memory DB
    # via ``run_sync`` exactly as the worker's run_scan would drive it.
    window = (now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=_RESCAN_HOURS), now)
    async with TestSessionLocal() as session:

        def _rescan(sync_session: Session) -> object:
            config = sync_session.get(ScanConfig, uuid.UUID(scan_id))
            assert config is not None
            scan_columns = [
                column
                for column in adapter.get_columns(config.base_query)
                if column.name != config.time_column
            ]
            result, _groups, _rows, _warehouse_rows = _scan_with_grouping(
                sync_session,
                config.project_id,
                config,
                adapter,
                scan_columns,
                scan_window=window,
                row_limit=100000,
            )
            return result

        result = await session.run_sync(_rescan)
        await session.commit()
        names = set(
            (
                await session.execute(select(Event.name).where(Event.project_id == project_id))
            ).scalars()
        )
        rows_after = (
            await session.execute(
                select(Event.name, FieldDefinition.name, EventFieldValue.value)
                .join(EventFieldValue, EventFieldValue.event_id == Event.id)
                .join(FieldDefinition, FieldDefinition.id == EventFieldValue.field_definition_id)
                .where(Event.project_id == project_id)
            )
        ).all()
    adapter.close()

    # Every planned name folds onto its plan event; nothing pipe-named leaks in.
    # (Whatever the warehouse sends that the plan lacks is the reconcile
    # chapter's business, not this one's.) The scan names events by the
    # ``event_name`` column, no group rule, so the rows land on existing
    # events and none is minted.
    assert result.events_skipped > 0
    assert result.events_created == 0
    assert all(" | " not in name for name in names), names
    planned = {event.name for event in roster.events()}
    unplanned = {event.name for event in roster.unplanned_events()}
    assert names <= planned | unplanned
    events_after = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"]
    assert {ev["name"] for ev in events_before} & planned == {
        ev["name"] for ev in events_after
    } & planned

    # The variable story survives its own guided rescan: the observed values the
    # chapter compares against the documented list are still there, and the
    # authored templates are still templates, not warehouse literals.
    observed_after = await _observed_values(client, slug)
    for name, (count, values) in observed_before.items():
        assert observed_after[name][0] >= count, name
        assert set(values) <= set(observed_after[name][1]), name
    values_by_event: dict[str, dict[str, str]] = {}
    for event_name, field_name, value in rows_after:
        values_by_event.setdefault(event_name, {})[field_name] = value
    assert values_by_event["Purchase Completed"]["product_id"] == "${product_id}"
    assert values_by_event["Home Screen View"]["platform"] == "${platform}"

    cov_after = (await client.get(f"/api/v1/projects/{slug}/reconciliation/coverage")).json()
    assert cov_after["summary"]["matched_count"] >= cov_before["summary"]["matched_count"]
