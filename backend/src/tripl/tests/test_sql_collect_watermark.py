"""The sql collect path: the manual-backfill watermark and NULL value cells.

Two defects on the same function, ``metric_collect._collect_sql``:

* a bounded "collect now" window REPLACED a lagging metric's resume window
  instead of only widening it, so the un-queried backlog was stranded behind an
  advancing collection progress;
* a ``NULL`` value cell went straight into ``float()`` and killed the whole
  collection with "Scan failed due to an internal error.".

The worker cases use the sync-sqlite fixture style of ``test_metric_collection``
(a file-backed engine from ``Base.metadata.create_all``, with the task module's
``_get_sync_session`` / ``_build_adapter`` / ``_resolve_value_window`` globals
monkey-patched); the dispatcher and preview cases go through the app.
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

import tripl.core.adapters.registry as adapter_registry
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import MetricKind, MetricStatus, ScanInterval
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks.metrics import metric_collect

# The metric is far behind: its own resume window opens ten days before the
# bounded slice a "collect now" click would hand it.
LAGGING_FROM = datetime(2026, 1, 1, tzinfo=UTC)
WINDOW_TO = datetime(2026, 1, 11, 12, tzinfo=UTC)
MANUAL_FROM = WINDOW_TO - timedelta(hours=48)


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch3_c1.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


class _SqlAdapter:
    """Fixed-rows warehouse fake that RECORDS the windows it was scanned over."""

    def __init__(self, column_names: list[str], rows: list[tuple[object, ...]]) -> None:
        self._column_names = column_names
        self._rows = rows
        self.seen_windows: list[tuple[datetime | None, datetime | None]] = []

    def test_connection(self) -> bool:
        return True

    def get_preview_rows(
        self,
        base_query: str,
        limit: int = 10,
        *,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        self.seen_windows.append((time_from, time_to))
        return self._column_names, self._rows

    def close(self) -> None:
        return None


def _seed_sql_metric(session: Session) -> str:
    """Seed a project + data source + active 1h sql metric; return the metric id."""
    project = Project(
        id=uuid.uuid4(),
        name="Batch3 C1",
        slug=f"batch3-c1-{uuid.uuid4().hex[:8]}",
        description="",
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"Batch3 DS {uuid.uuid4().hex[:8]}",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    session.add_all([project, data_source])
    session.commit()
    definition = MetricDefinition(
        id=uuid.uuid4(),
        project_id=project.id,
        name="sql-metric",
        display_name="Active sessions",
        kind=MetricKind.sql,
        config={
            "metric_sql": "SELECT ts AS bucket_ts, count() AS value FROM t GROUP BY bucket_ts",
            "time_column": "bucket_ts",
        },
        data_source_id=data_source.id,
        interval=ScanInterval.h1,
        status=MetricStatus.active,
    )
    session.add(definition)
    session.commit()
    return str(definition.id)


def _patch_collector(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_factory: sessionmaker[Session],
    adapter: _SqlAdapter,
    resume_window: tuple[datetime, datetime],
) -> None:
    monkeypatch.setattr(metric_collect, "_get_sync_session", session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(metric_collect, "_resolve_value_window", lambda *a, **k: resume_window)


# ── the manual backfill window ────────────────────────────────


def test_manual_sql_collect_keeps_the_backlog_of_a_lagging_metric(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Collect now" on a lagging sql metric widens its window, never replaces it.

    The click derives a bounded slice from ``compute_manual_collect_window``, but
    collection progress advances off the rows it writes, so querying only that
    slice would leave every bucket between the resume point and the slice start
    permanently uncollected under a green status.
    """
    with sync_session_factory() as session:
        def_id = _seed_sql_metric(session)

    adapter = _SqlAdapter(["bucket_ts", "value"], [(datetime(2026, 1, 5, tzinfo=UTC), 3)])
    _patch_collector(
        monkeypatch,
        session_factory=sync_session_factory,
        adapter=adapter,
        resume_window=(LAGGING_FROM, WINDOW_TO),
    )

    metric_collect.collect_metric_definitions.run(
        def_id,
        MANUAL_FROM.isoformat(),
        WINDOW_TO.isoformat(),
        True,
        True,
    )

    # One covering scan (replay_chunk_interval is unset), reaching back to where
    # the metric actually left off rather than to the bounded manual start.
    assert adapter.seen_windows == [(LAGGING_FROM, WINDOW_TO)]


def test_explicit_sql_replay_window_is_honoured_verbatim(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the manual-backfill flag an explicit window is still exact.

    The other half of ``_effective_value_window``: a legacy replay caller asks for
    one precise range and must not have it silently widened to the resume point.
    """
    with sync_session_factory() as session:
        def_id = _seed_sql_metric(session)

    adapter = _SqlAdapter(["bucket_ts", "value"], [(datetime(2026, 1, 10, tzinfo=UTC), 3)])
    _patch_collector(
        monkeypatch,
        session_factory=sync_session_factory,
        adapter=adapter,
        resume_window=(LAGGING_FROM, WINDOW_TO),
    )

    metric_collect.collect_metric_definitions.run(
        def_id,
        MANUAL_FROM.isoformat(),
        WINDOW_TO.isoformat(),
        True,
    )

    assert adapter.seen_windows == [(MANUAL_FROM, WINDOW_TO)]


# ── NULL value cells ──────────────────────────────────────────


def test_collect_sql_metric_records_a_null_value_bucket_as_absent(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NULL value cell is a gap, not a collection failure.

    ``avg`` over an all-NULL column, ``sum(x)/nullif(count(*),0)`` and an
    unmatched LEFT JOIN all project NULL for a bucket; the fact path already
    reads that as "absent" (``_index_multi_aggregate``).
    """
    with sync_session_factory() as session:
        def_id = _seed_sql_metric(session)

    adapter = _SqlAdapter(
        ["bucket_ts", "value"],
        [(datetime(2026, 1, 1, 10), 5), (datetime(2026, 1, 1, 11), None)],
    )
    _patch_collector(
        monkeypatch,
        session_factory=sync_session_factory,
        adapter=adapter,
        resume_window=(datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 12)),
    )

    result = metric_collect.collect_metric_definitions.run(def_id)

    assert result["values"] == 1
    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == uuid.UUID(def_id))
            )
            .scalars()
            .all()
        )
        # The 11:00 bucket is missing entirely — not stored as 0.0.
        assert {(row.bucket, row.value) for row in rows} == {
            (datetime(2026, 1, 1, 10, tzinfo=UTC), 5.0)
        }
        definition = session.get(MetricDefinition, uuid.UUID(def_id))
        assert definition is not None
        assert definition.last_collection_status == metric_collect.COLLECTION_STATUS_SUCCESS
        assert definition.last_collection_error is None


def test_collect_sql_metric_null_value_clears_a_previously_stored_bucket(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bucket that turns NULL loses its old value instead of going stale.

    The window delete runs over the whole chunk and the ``None`` entry is dropped
    on write, so the bucket reads as absent afterwards. This is what distinguishes
    recording ``None`` from skipping the row.
    """
    with sync_session_factory() as session:
        def_id = _seed_sql_metric(session)
        session.add(
            MetricValue(
                metric_definition_id=uuid.UUID(def_id),
                bucket=datetime(2026, 1, 1, 11),
                value=42.0,
            )
        )
        session.commit()

    adapter = _SqlAdapter(
        ["bucket_ts", "value"],
        [(datetime(2026, 1, 1, 10), 5), (datetime(2026, 1, 1, 11), None)],
    )
    _patch_collector(
        monkeypatch,
        session_factory=sync_session_factory,
        adapter=adapter,
        resume_window=(datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 12)),
    )

    metric_collect.collect_metric_definitions.run(def_id)

    with sync_session_factory() as session:
        buckets = set(
            session.execute(
                select(MetricValue.bucket).where(
                    MetricValue.metric_definition_id == uuid.UUID(def_id)
                )
            ).scalars()
        )
        assert buckets == {datetime(2026, 1, 1, 10, tzinfo=UTC)}


# ── the API surfaces ─────────────────────────────────────────────────────────


@pytest.fixture
async def project(client: AsyncClient) -> dict:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": "Batch3 C1", "slug": "batch3-c1", "description": ""},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def data_source(client: AsyncClient, project: dict) -> dict:
    resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Batch3 CH",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "test_db",
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    # A data source belongs to a project through a ScanConfig; the sql-metric save
    # and preview paths refuse an unbound id, because that id selects the warehouse
    # credential the free-text SELECT below would run under.
    async with TestSessionLocal() as session:
        session.add(
            ScanConfig(
                project_id=uuid.UUID(project["id"]),
                data_source_id=uuid.UUID(created["id"]),
                name="batch3-c1-ds-binding",
                base_query="SELECT 1",
            )
        )
        await session.commit()
    return created


class _FakeAsyncResult:
    def __init__(self, task_id: str) -> None:
        self.id = task_id


class _DispatchRecorder:
    """Records the positional args of a patched task ``.delay`` call."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *args: object) -> _FakeAsyncResult:
        self.calls.append(args)
        return _FakeAsyncResult(self.task_id)


async def test_sql_collect_now_dispatches_the_manual_backfill_flag(
    client: AsyncClient,
    project: dict,
    data_source: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker-side widening is dead code unless the click sets the flag.

    The fifth positional task argument is what tells the worker its bounded
    window is a floor to widen from, not a replacement.
    """
    recorder = _DispatchRecorder("task-sql")
    monkeypatch.setattr(metric_collect.collect_metric_definitions, "delay", recorder)

    created = await client.post(
        f"/api/v1/projects/{project['slug']}/metrics",
        json={
            "kind": "sql",
            "name": "batch3_sql",
            "display_name": "Batch3 SQL",
            "data_source_id": data_source["id"],
            "interval": "1d",
            "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
        },
    )
    assert created.status_code == 201, created.text
    metric = created.json()

    resp = await client.post(f"/api/v1/projects/{project['slug']}/metrics/{metric['id']}/collect")
    assert resp.status_code == 202, resp.text

    assert len(recorder.calls) == 1
    args = recorder.calls[0]
    assert args[0] == metric["id"]
    assert args[3] is True  # force, so a draft metric is not skipped on status
    assert args[4] is True  # manual_backfill: widen the resume window, never replace it


class _PreviewStubAdapter:
    """Warehouse stub for the preview endpoint."""

    def __init__(self, columns: list[str], rows: list[tuple[object, ...]]) -> None:
        self.columns = columns
        self.rows = rows

    def get_preview_rows(
        self,
        base_query: str,
        limit: int = 10,
        *,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        return self.columns, self.rows

    def close(self) -> None:
        return None


async def _preview(
    client: AsyncClient,
    slug: str,
    data_source_id: str,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[tuple[object, ...]],
) -> dict:
    adapter = _PreviewStubAdapter(["t", "value"], rows)
    monkeypatch.setattr(adapter_registry, "build_adapter", lambda ds: adapter)
    resp = await client.post(
        f"/api/v1/projects/{slug}/metrics/preview",
        json={
            "data_source_id": data_source_id,
            "sql": "SELECT toStartOfHour(ts) AS t, count() AS value FROM e GROUP BY t",
            "time_column": "t",
            "interval": "1h",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_preview_sql_metric_skips_a_null_value_cell(
    client: AsyncClient,
    project: dict,
    data_source: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preview points must equal what a collection would store, gaps included.

    Before the fix the NULL cell surfaced CPython's own "float() argument must be
    a string or a real number, not 'NoneType'" as the preview error, rejecting
    SQL the collector is now happy to run.
    """
    body = await _preview(
        client,
        project["slug"],
        data_source["id"],
        monkeypatch,
        [
            (datetime(2026, 7, 1, 10, 0, tzinfo=UTC), 5),
            (datetime(2026, 7, 1, 11, 0, tzinfo=UTC), None),
        ],
    )

    assert body["error"] is None
    assert body["point_count"] == 1
    assert [point["value"] for point in body["points"]] == [5.0]


async def test_preview_sql_metric_still_rejects_a_non_numeric_value_cell(
    client: AsyncClient,
    project: dict,
    data_source: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The NULL guard narrows the coercion error path, it does not delete it."""
    body = await _preview(
        client,
        project["slug"],
        data_source["id"],
        monkeypatch,
        [(datetime(2026, 7, 1, 10, 0, tzinfo=UTC), "abc")],
    )

    assert body["error"] is not None
    assert body["points"] == []
