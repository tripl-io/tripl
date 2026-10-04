"""Scan and data-source follow-ups (lane F7).

* A catalog run reports the warehouse rows behind its breakdown
  (``catalog_rows_scanned``), not only the grouped combinations.
* ``GET /scans/{id}`` carries ``last_metrics_run_at`` and
  ``next_metrics_run_at`` from the scheduler's own due check.
* The metrics collector keeps up to five sample property dicts
  per shadow event candidate, and the shadow inbox serves them.
* A data source lists the scans that read it, with links.
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

from tripl.core.analyzers.cardinality import BreakdownAnalysis
from tripl.models import Base
from tripl.models.event_metric import EventMetric
from tripl.models.project import Project
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.models.shadow_event_candidate import (
    SHADOW_SAMPLE_LIMIT,
    SHADOW_STATUS_NEW,
    ShadowEventCandidate,
)
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks.metrics.chunk_processing import (
    _add_shadow_sample,
    _build_shadow_candidate_rows,
    _shadow_sample,
)
from tripl.worker.tasks.metrics.metric_rows import _upsert_shadow_event_candidates
from tripl.worker.tasks.metrics.tasks import METRICS_COLLECTION_MODE
from tripl.worker.tasks.scan import _warehouse_rows

HOUR = timedelta(hours=1)


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _analysis(rows: list[tuple[object, ...]]) -> BreakdownAnalysis:
    # Two regular columns; a row one wider carries the adapter's ``_cnt``.
    return BreakdownAnalysis(results={}, rows=rows, reg_names=["event", "screen"], json_names=[])


# --------------------------------------------------------------------------- #
# warehouse rows behind a catalog run's combinations
# --------------------------------------------------------------------------- #


def test_warehouse_rows_sum_the_count_every_breakdown_row_carries() -> None:
    grouped = [
        _analysis([("click", "home", 120), ("click", "cart", 30)]),
        _analysis([("view", "home", 3)]),
    ]
    # Three combinations, 153 warehouse rows behind them.
    assert _warehouse_rows(grouped) == 153


def test_warehouse_rows_are_unknown_when_a_row_carries_no_count() -> None:
    # A hand-built row without ``_cnt``: its last value is a column value, and
    # reporting it as a row count is the exact mistake this guards.
    assert _warehouse_rows([_analysis([("click", "home", 7), ("view", "cart")])]) is None


def test_warehouse_rows_of_an_empty_breakdown_are_zero() -> None:
    assert _warehouse_rows([]) == 0
    assert _warehouse_rows([_analysis([])]) == 0


# --------------------------------------------------------------------------- #
# sample properties on shadow events
# --------------------------------------------------------------------------- #


def test_shadow_sample_keeps_the_row_properties_without_empty_values() -> None:
    sample = _shadow_sample(
        ("Checkout", "", None, "x" * 300),
        reg_index={"event": 0, "screen": 1, "variant": 2, "url": 3},
        json_index={},
        n_reg=4,
        json_value_names=[],
        event_type_column=None,
        time_column=None,
    )
    assert sample == {"event": "Checkout", "url": "x" * 200}


def test_shadow_samples_stop_at_the_limit_and_skip_duplicates() -> None:
    samples: list[dict[str, str]] = []
    for index in range(SHADOW_SAMPLE_LIMIT + 3):
        _add_shadow_sample(samples, {"screen": f"s{index}"})
        _add_shadow_sample(samples, {"screen": f"s{index}"})
    _add_shadow_sample(samples, {})
    assert samples == [{"screen": f"s{index}"} for index in range(SHADOW_SAMPLE_LIMIT)]


def test_folded_shadow_rows_pool_their_samples() -> None:
    type_a, type_b = uuid.uuid4(), uuid.uuid4()
    t0 = datetime(2026, 9, 1, 8, tzinfo=UTC)
    rows = _build_shadow_candidate_rows(
        {
            (type_a, "open"): [10, t0, t0, [{"screen": "home"}]],
            (type_b, "open"): [30, t0, t0, [{"screen": "home"}, {"screen": "cart"}]],
            # An entry from before samples existed still folds.
            (type_a, "close"): [4, t0, t0],
        },
        project_id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
    )
    by_name = {row["event_name"]: row for row in rows}
    assert by_name["open"]["sample_properties"] == [{"screen": "home"}, {"screen": "cart"}]
    assert by_name["close"]["sample_properties"] == []


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'followups_scans.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_shadow_upsert_replaces_samples_with_the_latest_window(
    sync_session_factory: sessionmaker[Session],
) -> None:
    project_id, scan_id = uuid.uuid4(), uuid.uuid4()
    now = datetime(2026, 9, 26, 12, tzinfo=UTC)

    def row(samples: list[dict[str, str]]) -> dict[str, object]:
        return {
            "id": uuid.uuid4(),
            "project_id": project_id,
            "scan_config_id": scan_id,
            "event_type_id": None,
            "event_name": "shadow | x",
            "observed_count": 3,
            "first_seen_at": now - HOUR,
            "last_seen_at": now,
            "status": SHADOW_STATUS_NEW,
            "sample_properties": samples,
        }

    with sync_session_factory() as session:
        _upsert_shadow_event_candidates(session, rows=[row([{"screen": "home"}])])
        session.commit()
        _upsert_shadow_event_candidates(session, rows=[row([{"screen": "cart"}])])
        session.commit()
        stored = session.execute(select(ShadowEventCandidate)).scalar_one()
        assert stored.sample_properties == [{"screen": "cart"}]


async def _new_project(client: AsyncClient, slug: str) -> uuid.UUID:
    created = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert created.status_code == 201, created.text
    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == slug))
    assert project_id is not None
    return project_id


async def _data_source(client: AsyncClient, name: str) -> str:
    response = await client.post(
        "/api/v1/data-sources",
        json={
            "name": name,
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
            "username": "default",
            "password": "",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _scan(
    client: AsyncClient,
    slug: str,
    data_source_id: str,
    name: str,
    *,
    interval: str | None = "1h",
) -> str:
    body: dict[str, object] = {
        "data_source_id": data_source_id,
        "name": name,
        "base_query": "SELECT 1",
        "event_type_column": "event_name",
        "time_column": "created_at",
    }
    if interval is not None:
        body["interval"] = interval
    response = await client.post(f"/api/v1/projects/{slug}/scans", json=body)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest.mark.asyncio
async def test_shadow_inbox_serves_the_sample_properties(client: AsyncClient) -> None:
    slug = "f7-shadow-samples"
    project_id = await _new_project(client, slug)
    scan_id = await _scan(client, slug, await _data_source(client, "F7 shadow wh"), "Shadow scan")
    async with TestSessionLocal() as session:
        session.add(
            ShadowEventCandidate(
                project_id=project_id,
                scan_config_id=uuid.UUID(scan_id),
                event_type_id=None,
                event_name="checkout | step",
                observed_count=12,
                first_seen_at=datetime.now(UTC) - HOUR,
                last_seen_at=datetime.now(UTC),
                status=SHADOW_STATUS_NEW,
                sample_properties=[{"step": "pay", "platform": "ios"}],
            )
        )
        await session.commit()

    response = await client.get(f"/api/v1/projects/{slug}/reconciliation/shadow-events")
    assert response.status_code == 200, response.text
    (item,) = response.json()["items"]
    assert item["sample_properties"] == [{"step": "pay", "platform": "ios"}]


# --------------------------------------------------------------------------- #
# the next metrics run on the scan read
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_scan_read_carries_the_last_and_next_metrics_run(client: AsyncClient) -> None:
    slug = "f7-scan-schedule"
    await _new_project(client, slug)
    scan_id = await _scan(client, slug, await _data_source(client, "F7 schedule wh"), "Hourly")
    now = datetime.now(UTC)
    boundary = now.replace(minute=0, second=0, microsecond=0)
    completed_at = boundary + timedelta(minutes=2)
    async with TestSessionLocal() as session:
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_id),
                event_id=None,
                event_type_id=None,
                bucket=boundary - HOUR,
                count=10,
            )
        )
        session.add(
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_id),
                status=ScanJobStatus.completed.value,
                completed_at=completed_at,
                result_summary={"mode": METRICS_COLLECTION_MODE, "time_to": boundary.isoformat()},
            )
        )
        await session.commit()

    response = await client.get(f"/api/v1/projects/{slug}/scans/{scan_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert _parse(body["last_metrics_run_at"]) == completed_at
    # Caught up to this hour's boundary: next due at the next one, not "last + 1h".
    assert _parse(body["next_metrics_run_at"]) == boundary + HOUR
    assert body["monitoring_enabled"] is True


@pytest.mark.asyncio
async def test_a_scan_never_collected_is_due_now(client: AsyncClient) -> None:
    slug = "f7-scan-due"
    await _new_project(client, slug)
    scan_id = await _scan(client, slug, await _data_source(client, "F7 due wh"), "Fresh")
    before = datetime.now(UTC)

    body = (await client.get(f"/api/v1/projects/{slug}/scans/{scan_id}")).json()
    assert body["last_metrics_run_at"] is None
    next_run = _parse(body["next_metrics_run_at"])
    assert before - timedelta(seconds=1) <= next_run <= datetime.now(UTC) + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_a_catalog_only_scan_has_no_next_metrics_run(client: AsyncClient) -> None:
    slug = "f7-scan-catalog"
    await _new_project(client, slug)
    scan_id = await _scan(
        client, slug, await _data_source(client, "F7 catalog wh"), "Catalog", interval=None
    )

    body = (await client.get(f"/api/v1/projects/{slug}/scans/{scan_id}")).json()
    assert body["next_metrics_run_at"] is None
    assert body["last_metrics_run_at"] is None
    # The list rows do not carry the schedule at all.
    listed = (await client.get(f"/api/v1/projects/{slug}/scans")).json()
    assert "next_metrics_run_at" not in listed[0]


# --------------------------------------------------------------------------- #
# the scans that read a data source
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_data_source_lists_the_scans_that_read_it(client: AsyncClient) -> None:
    source_id = await _data_source(client, "F7 shared wh")
    await _new_project(client, "f7-ds-app")
    await _new_project(client, "f7-ds-web")
    app_scan = await _scan(client, "f7-ds-app", source_id, "App events")
    web_scan = await _scan(client, "f7-ds-web", source_id, "Web events", interval=None)

    listed = (await client.get("/api/v1/data-sources")).json()
    source = next(item for item in listed if item["id"] == source_id)
    assert source["scan_count"] == 2
    assert {(ref["id"], ref["name"], ref["project_slug"]) for ref in source["scans"]} == {
        (app_scan, "App events", "f7-ds-app"),
        (web_scan, "Web events", "f7-ds-web"),
    }

    single = (await client.get(f"/api/v1/data-sources/{source_id}")).json()
    assert [ref["name"] for ref in single["scans"]] == ["App events", "Web events"]


@pytest.mark.asyncio
async def test_an_unused_data_source_lists_no_scans(client: AsyncClient) -> None:
    source_id = await _data_source(client, "F7 idle wh")
    single = (await client.get(f"/api/v1/data-sources/{source_id}")).json()
    assert single["scan_count"] == 0
    assert single["scans"] == []
