"""End-to-end tests for catalog MetricDefinition collection + beat dispatch.

Mirrors the sync-sqlite fixture style of ``test_metrics_tasks.py``: a file-backed
SQLite engine built from ``Base.metadata.create_all`` (no migrations, no FK
cascades), with the task module's ``_get_sync_session`` / ``_build_adapter`` /
``_resolve_value_window`` globals monkey-patched per test.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import AggregateSpec, ColumnInfo
from tripl.core.adapters.measure_validator import SqlDialect
from tripl.core.bucketing import floor_to_bucket
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import (
    MetricAggregation,
    MetricComposition,
    MetricKind,
    MetricStatus,
    ScanInterval,
)
from tripl.models.event_metric import EventMetric
from tripl.models.fact_table import FactTable
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.metric_value_breakdown import MetricValueBreakdown
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.worker.analyzers.metric_composition import evaluate_composition
from tripl.worker.tasks._errors import ScanError
from tripl.worker.tasks.metrics import _fact_conditions, metric_collect
from tripl.worker.tasks.metrics import schedule as metrics_schedule


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'metric_collection.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _seed_project_and_ds(session: Session) -> tuple[Project, DataSource]:
    project = Project(
        id=uuid.uuid4(),
        name="Metrics Project",
        slug=f"metrics-{uuid.uuid4().hex[:8]}",
        description="",
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"Metrics DS {uuid.uuid4().hex[:8]}",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    session.add_all([project, data_source])
    session.commit()
    return project, data_source


def _seed_scan_config(session: Session, project: Project, data_source: DataSource) -> ScanConfig:
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Source Scan",
        base_query="SELECT ts, user_id FROM events",
        time_column="ts",
        cardinality_threshold=100,
        interval="1h",
    )
    session.add(config)
    session.commit()
    return config


# ── fact ───────────────────────────────────────────────────────────────────


class _FactAdapter:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def test_connection(self) -> bool:
        return True

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        return [
            ColumnInfo(name="ts", type_name="DateTime"),
            ColumnInfo(name="amount", type_name="Float64"),
            ColumnInfo(name="user_id", type_name="String"),
        ]

    def get_time_bucketed_aggregate(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        agg_fn: MetricAggregation,
        measure_column: str | None,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        return ([], [], self._rows)

    def close(self) -> None:
        return None


class _FactRatioAdapter:
    """Returns a queued rowset per ``get_time_bucketed_aggregate`` call.

    A fact ratio collect drives the adapter once for the numerator and once for
    the denominator (in that order), so the two rowsets are popped in turn.
    """

    def __init__(self, rowsets: list[list[tuple[object, ...]]]) -> None:
        self._rowsets = rowsets
        self._calls = 0

    def test_connection(self) -> bool:
        return True

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        return [
            ColumnInfo(name="ts", type_name="DateTime"),
            ColumnInfo(name="amount", type_name="Float64"),
            ColumnInfo(name="user_id", type_name="String"),
        ]

    def get_time_bucketed_aggregate(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        agg_fn: MetricAggregation,
        measure_column: str | None,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        rows = self._rowsets[self._calls]
        self._calls += 1
        return ([], [], rows)

    def close(self) -> None:
        return None


class _FactRatioBreakdownAdapter(_FactRatioAdapter):
    def __init__(
        self,
        rowsets: list[list[tuple[object, ...]]],
        breakdown_rows: list[tuple[datetime, str, bool, tuple[object, ...]]],
    ) -> None:
        super().__init__(rowsets)
        self._breakdown_rows = breakdown_rows
        self.breakdown_calls: list[tuple[str, int | None, list[AggregateSpec]]] = []

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        return [
            ColumnInfo(name="ts", type_name="DateTime"),
            ColumnInfo(name="amount", type_name="Float64"),
            ColumnInfo(name="user_id", type_name="String"),
            ColumnInfo(name="subscription", type_name="String"),
        ]

    def get_time_bucketed_multi_aggregate_breakdown(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        breakdown_column: str,
        specs: list[AggregateSpec],
        time_from: datetime,
        time_to: datetime,
        *,
        values_limit: int | None = None,
        limit: int = 100000,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        self.breakdown_calls.append((breakdown_column, values_limit, list(specs)))
        col_names = ["bucket", "breakdown_value", "is_other", *[spec.key for spec in specs]]
        rows: list[tuple[object, ...]] = []
        for bucket, value, is_other, cells in self._breakdown_rows:
            rows.append((bucket, value, is_other, *cells))
        return col_names, rows


def _seed_fact_table(
    session: Session, project: Project, data_source: DataSource, **overrides: object
) -> FactTable:
    fact_table = FactTable(
        id=uuid.uuid4(),
        project_id=project.id,
        name=f"ft-{uuid.uuid4().hex[:6]}",
        display_name="Revenue Facts",
        data_source_id=data_source.id,
        sql="SELECT ts, amount, user_id FROM revenue",
        timestamp_column="ts",
        columns=[
            {"name": "ts", "type": "timestamp"},
            {"name": "amount", "type": "number"},
            {"name": "user_id", "type": "string"},
        ],
        identifier_columns=["user_id"],
        row_filters=[],
        **overrides,
    )
    session.add(fact_table)
    session.commit()
    return fact_table


def _make_fact_metric(
    session: Session, project: Project, fact_table: FactTable, **overrides: object
) -> MetricDefinition:
    definition = MetricDefinition(
        id=uuid.uuid4(),
        project_id=project.id,
        name=f"fact-{uuid.uuid4().hex[:6]}",
        display_name="Revenue",
        kind=MetricKind.fact,
        composition=MetricComposition.single,
        aggregation=MetricAggregation.sum,
        fact_table_id=fact_table.id,
        config={"measure_column": "amount"},
        interval=ScanInterval.h1,
        status=MetricStatus.active,
        **overrides,
    )
    session.add(definition)
    session.commit()
    return definition


def test_collect_fact_writes_metric_values(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        fact_table = _seed_fact_table(session, project, data_source)
        definition = _make_fact_metric(session, project, fact_table)
        def_id = str(definition.id)

    adapter = _FactAdapter([(datetime(2026, 1, 1, 10), 12.5), (datetime(2026, 1, 1, 11), 7.0)])
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metric_collect,
        "_resolve_value_window",
        lambda *a, **k: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 12)),
    )

    result = metric_collect.collect_metric_definitions.run(def_id)

    assert result["values"] == 2
    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == uuid.UUID(def_id))
            )
            .scalars()
            .all()
        )
        assert {(row.bucket, row.value) for row in rows} == {
            (datetime(2026, 1, 1, 10, tzinfo=UTC), 12.5),
            (datetime(2026, 1, 1, 11, tzinfo=UTC), 7.0),
        }
        assert all(row.scan_config_id is None for row in rows)
        definition = session.get(MetricDefinition, uuid.UUID(def_id))
        assert definition is not None
        assert definition.last_collection_status == "success"
        assert definition.last_collected_at is not None


def test_collect_fact_is_idempotent_on_rerun(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        fact_table = _seed_fact_table(session, project, data_source)
        definition = _make_fact_metric(session, project, fact_table)
        def_id = str(definition.id)

    adapter = _FactAdapter([(datetime(2026, 1, 1, 10), 5.0), (datetime(2026, 1, 1, 11), 6.0)])
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metric_collect,
        "_resolve_value_window",
        lambda *a, **k: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 12)),
    )

    metric_collect.collect_metric_definitions.run(def_id)
    metric_collect.collect_metric_definitions.run(def_id)

    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == uuid.UUID(def_id))
            )
            .scalars()
            .all()
        )
        # Window-delete-then-insert means the re-run replaces, never duplicates,
        # even though scan_config_id is NULL (NULLs are distinct in the unique
        # index, so ON CONFLICT alone would not dedupe).
        assert len(rows) == 2
        assert {(row.bucket, row.value) for row in rows} == {
            (datetime(2026, 1, 1, 10, tzinfo=UTC), 5.0),
            (datetime(2026, 1, 1, 11, tzinfo=UTC), 6.0),
        }


def test_collect_fact_floors_naive_bucket_to_utc(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """fact buckets are interval-floored + UTC-coerced like the sql path.

    A warehouse adapter returning a naive, interval-unaligned datetime must be
    normalized through ``_coerce_bucket`` before storage (so a later
    ``_resolve_value_window`` cannot mix naive and aware bounds), not stored
    verbatim as the old ``cast(datetime, row[0])`` did.
    """
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        fact_table = _seed_fact_table(session, project, data_source)
        definition = _make_fact_metric(session, project, fact_table)
        def_id = str(definition.id)

    # 10:37:12 (naive) must floor to the 10:00 bucket of the 1h interval.
    adapter = _FactAdapter([(datetime(2026, 1, 1, 10, 37, 12), 12.5)])
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metric_collect,
        "_resolve_value_window",
        lambda *a, **k: (
            datetime(2026, 1, 1, 10, tzinfo=UTC),
            datetime(2026, 1, 1, 12, tzinfo=UTC),
        ),
    )

    metric_collect.collect_metric_definitions.run(def_id)

    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == uuid.UUID(def_id))
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        # Floored to the hour boundary and restored as UTC on ORM load.
        assert rows[0].bucket == datetime(2026, 1, 1, 10, tzinfo=UTC)


def test_collect_fact_ratio_handles_divide_by_zero(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A fact ratio divides numerator / denominator; a zero denominator is a gap.

    The denominator may reference a different fact table (here a second one); a
    divide-by-zero bucket maps to ``None`` and is dropped by the NOT-NULL row
    builder rather than stored as a misleading ``0``.
    """
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        numerator_ft = _seed_fact_table(session, project, data_source)
        denominator_ft = _seed_fact_table(session, project, data_source)
        definition = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project.id,
            name=f"fact-ratio-{uuid.uuid4().hex[:6]}",
            display_name="Conversion",
            kind=MetricKind.fact,
            composition=MetricComposition.ratio,
            aggregation=MetricAggregation.count,
            fact_table_id=numerator_ft.id,
            config={
                "numerator": {
                    "fact_table_id": str(numerator_ft.id),
                    "aggregation": "count",
                    "measure_column": None,
                    "distinct_column": None,
                    "row_filter": None,
                },
                "denominator": {
                    "fact_table_id": str(denominator_ft.id),
                    "aggregation": "count_distinct",
                    "measure_column": None,
                    "distinct_column": "user_id",
                    "row_filter": None,
                },
            },
            interval=ScanInterval.h1,
            status=MetricStatus.active,
        )
        session.add(definition)
        session.commit()
        def_id = str(definition.id)

    # Numerator series then denominator series (b11 denominator 0 -> divide-by-zero).
    adapter = _FactRatioAdapter(
        [
            [(datetime(2026, 1, 1, 10), 10.0), (datetime(2026, 1, 1, 11), 20.0)],
            [(datetime(2026, 1, 1, 10), 2.0), (datetime(2026, 1, 1, 11), 0.0)],
        ]
    )
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metric_collect,
        "_resolve_value_window",
        lambda *a, **k: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 12)),
    )

    result = metric_collect.collect_metric_definitions.run(def_id)

    # Only the finite-ratio bucket is stored; the divide-by-zero bucket is absent.
    assert result["values"] == 1
    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == uuid.UUID(def_id))
            )
            .scalars()
            .all()
        )
        assert {(row.bucket, row.value) for row in rows} == {
            (datetime(2026, 1, 1, 10, tzinfo=UTC), 5.0)
        }


def test_collect_fact_ratio_writes_breakdown_values(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        fact_table = _seed_fact_table(session, project, data_source)
        definition = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project.id,
            name=f"fact-ratio-bd-{uuid.uuid4().hex[:6]}",
            display_name="Paid conversion",
            kind=MetricKind.fact,
            composition=MetricComposition.ratio,
            aggregation=MetricAggregation.count,
            fact_table_id=fact_table.id,
            config={
                "numerator": {
                    "fact_table_id": str(fact_table.id),
                    "aggregation": "count",
                },
                "denominator": {
                    "fact_table_id": str(fact_table.id),
                    "aggregation": "count_distinct",
                    "distinct_column": "user_id",
                },
            },
            interval=ScanInterval.h1,
            status=MetricStatus.active,
            breakdown_columns=["subscription"],
            breakdown_values_limit=2,
        )
        session.add(definition)
        session.commit()
        def_id = str(definition.id)

    adapter = _FactRatioBreakdownAdapter(
        [
            [(datetime(2026, 1, 1, 10), 10.0), (datetime(2026, 1, 1, 11), 20.0)],
            [(datetime(2026, 1, 1, 10), 2.0), (datetime(2026, 1, 1, 11), 0.0)],
        ],
        [
            (datetime(2026, 1, 1, 10), "paid", False, (8.0, 2.0)),
            (datetime(2026, 1, 1, 10), "free", False, (2.0, 0.0)),
            (datetime(2026, 1, 1, 10), "Other", True, (None, 4.0)),
        ],
    )
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metric_collect,
        "_resolve_value_window",
        lambda *a, **k: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 12)),
    )

    result = metric_collect.collect_metric_definitions.run(def_id)

    assert result["values"] == 1
    assert result["breakdown_values"] == 2
    assert [(column, limit) for column, limit, _specs in adapter.breakdown_calls] == [
        ("subscription", 2)
    ]
    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValueBreakdown).where(
                    MetricValueBreakdown.metric_definition_id == uuid.UUID(def_id)
                )
            )
            .scalars()
            .all()
        )
        assert {
            (row.breakdown_column, row.breakdown_value, row.is_other, row.value) for row in rows
        } == {
            ("subscription", "paid", False, 4.0),
            ("subscription", "Other", True, 0.0),
        }


class _FactBreakdownAdapter(_FactAdapter):
    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        # ``country`` on top of the base fact columns: the collector now
        # rejects a breakdown dimension the fact table does not project.
        return [
            ColumnInfo(name="ts", type_name="DateTime"),
            ColumnInfo(name="amount", type_name="Float64"),
            ColumnInfo(name="user_id", type_name="String"),
            ColumnInfo(name="country", type_name="String"),
        ]

    def get_time_bucketed_aggregate_breakdown(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        agg_fn: MetricAggregation,
        measure_column: str | None,
        breakdown_column: str,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None = None,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        # Row layout: (_bucket, _breakdown_value, _is_other, col_val, aggregate).
        return (
            [breakdown_column],
            [],
            [
                (datetime(2026, 1, 1, 10), "US", False, "US", 8.0),
                (datetime(2026, 1, 1, 10), "Other", True, "FR", 4.0),
            ],
        )


def test_collect_fact_writes_breakdown_values(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        fact_table = _seed_fact_table(session, project, data_source)
        definition = _make_fact_metric(
            session,
            project,
            fact_table,
            breakdown_columns=["country"],
            breakdown_values_limit=2,
        )
        def_id = str(definition.id)

    adapter = _FactBreakdownAdapter([(datetime(2026, 1, 1, 10), 12.0)])
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metric_collect,
        "_resolve_value_window",
        lambda *a, **k: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11)),
    )

    result = metric_collect.collect_metric_definitions.run(def_id)

    assert result["breakdown_values"] == 2
    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValueBreakdown).where(
                    MetricValueBreakdown.metric_definition_id == uuid.UUID(def_id)
                )
            )
            .scalars()
            .all()
        )
        assert {
            (row.breakdown_column, row.breakdown_value, row.is_other, row.value) for row in rows
        } == {
            ("country", "US", False, 8.0),
            ("country", "Other", True, 4.0),
        }


def test_truncated_breakdown_query_fails_instead_of_erasing_the_window(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A capped read must not become a window-delete plus a short re-insert.

    Collection deletes the chunk window before UPSERTing, so a query that hit the
    row ceiling used to erase rows it would never write back — silently, leaving
    a metric that reads as a clean series with a hole in it.
    """
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        fact_table = _seed_fact_table(session, project, data_source)
        definition = _make_fact_metric(
            session,
            project,
            fact_table,
            breakdown_columns=["country"],
            breakdown_values_limit=2,
        )
        def_id = str(definition.id)

    adapter = _FactBreakdownAdapter([(datetime(2026, 1, 1, 10), 12.0)])
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metric_collect,
        "_resolve_value_window",
        lambda *a, **k: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11)),
    )

    # A good run first, so there is a populated window to lose.
    assert metric_collect.collect_metric_definitions.run(def_id)["breakdown_values"] == 2

    # Now the same two rows come back over a ceiling of one: the probe row makes
    # that detectable, where asking for exactly the limit could not tell a full
    # window from a cut-off one.
    monkeypatch.setattr(metric_collect, "METRIC_QUERY_ROW_LIMIT", 1)

    # ScanError, not ValueError: a hit row limit is user-actionable, so its
    # message has to survive user_facing_error verbatim.
    with pytest.raises(ScanError, match="reached the metric query row limit"):
        metric_collect.collect_metric_definitions.run(def_id)

    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValueBreakdown).where(
                    MetricValueBreakdown.metric_definition_id == uuid.UUID(def_id)
                )
            )
            .scalars()
            .all()
        )
        # Untouched: the guard runs before the window delete, so the last good
        # collection still stands.
        assert {(row.breakdown_value, row.value) for row in rows} == {("US", 8.0), ("Other", 4.0)}


# ── sql ──────────────────────────────────────────────────────────────────────


class _SqlAdapter:
    def __init__(self, column_names: list[str], rows: list[tuple[object, ...]]) -> None:
        self._column_names = column_names
        self._rows = rows

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
        return self._column_names, self._rows

    def close(self) -> None:
        return None


def test_collect_sql_metric_writes_metric_values(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
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
        def_id = str(definition.id)

    adapter = _SqlAdapter(
        ["bucket_ts", "value"],
        [(datetime(2026, 1, 1, 10), 5), (datetime(2026, 1, 1, 11), 9)],
    )
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metric_collect,
        "_resolve_value_window",
        lambda *a, **k: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 12)),
    )

    result = metric_collect.collect_metric_definitions.run(def_id)

    assert result["values"] == 2
    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == uuid.UUID(def_id))
            )
            .scalars()
            .all()
        )
        assert {(row.bucket, row.value) for row in rows} == {
            (datetime(2026, 1, 1, 10, tzinfo=UTC), 5.0),
            (datetime(2026, 1, 1, 11, tzinfo=UTC), 9.0),
        }


def test_collect_sql_metric_honors_custom_value_column(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """config.value_column redirects the measure read off the projected row
    — the documented ``value`` alias is only the default."""
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        definition = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project.id,
            name="sql-metric-custom-col",
            display_name="Sessions",
            kind=MetricKind.sql,
            config={
                "metric_sql": (
                    "SELECT ts AS bucket_ts, count() AS sessions FROM t GROUP BY bucket_ts"
                ),
                "time_column": "bucket_ts",
                "value_column": "sessions",
            },
            data_source_id=data_source.id,
            interval=ScanInterval.h1,
            status=MetricStatus.active,
        )
        session.add(definition)
        session.commit()
        def_id = str(definition.id)

    adapter = _SqlAdapter(
        ["bucket_ts", "sessions"],
        [(datetime(2026, 1, 1, 10), 5), (datetime(2026, 1, 1, 11), 9)],
    )
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metric_collect,
        "_resolve_value_window",
        lambda *a, **k: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 12)),
    )

    result = metric_collect.collect_metric_definitions.run(def_id)

    assert result["values"] == 2
    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == uuid.UUID(def_id))
            )
            .scalars()
            .all()
        )
        assert {(row.bucket, row.value) for row in rows} == {
            (datetime(2026, 1, 1, 10, tzinfo=UTC), 5.0),
            (datetime(2026, 1, 1, 11, tzinfo=UTC), 9.0),
        }


def _seed_draft_sql_metric(session: Session, project: Project, data_source: DataSource) -> str:
    definition = MetricDefinition(
        id=uuid.uuid4(),
        project_id=project.id,
        name="draft-sql-metric",
        display_name="Draft sql metric",
        kind=MetricKind.sql,
        config={
            "metric_sql": "SELECT ts AS bucket_ts, count() AS value FROM t GROUP BY bucket_ts",
            "time_column": "bucket_ts",
        },
        data_source_id=data_source.id,
        interval=ScanInterval.h1,
        status=MetricStatus.draft,
    )
    session.add(definition)
    session.commit()
    return str(definition.id)


def test_collect_sql_metric_skipped_when_not_active_without_force(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The scheduler path (force=False) skips a non-active metric and writes nothing."""
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        def_id = _seed_draft_sql_metric(session, project, data_source)

    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)

    result = metric_collect.collect_metric_definitions.run(def_id)

    assert result == {"skipped": True, "metric_definition_id": def_id}
    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == uuid.UUID(def_id))
            )
            .scalars()
            .all()
        )
        assert rows == []


def test_collect_sql_metric_forced_collects_draft(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The manual path (force=True) collects a draft metric anyway, writing values."""
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        def_id = _seed_draft_sql_metric(session, project, data_source)

    adapter = _SqlAdapter(
        ["bucket_ts", "value"],
        [(datetime(2026, 1, 1, 10), 5), (datetime(2026, 1, 1, 11), 9)],
    )
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metric_collect,
        "_resolve_value_window",
        lambda *a, **k: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 12)),
    )

    result = metric_collect.collect_metric_definitions.run(def_id, force=True)

    # Not skipped despite draft status; values are written.
    assert result.get("skipped") is None
    assert result["values"] == 2
    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == uuid.UUID(def_id))
            )
            .scalars()
            .all()
        )
        assert {(row.bucket, row.value) for row in rows} == {
            (datetime(2026, 1, 1, 10, tzinfo=UTC), 5.0),
            (datetime(2026, 1, 1, 11, tzinfo=UTC), 9.0),
        }


# ── event_composition ────────────────────────────────────────────────────────


def _seed_numerator_metrics(
    session: Session,
    scan_config: ScanConfig,
    event_id: uuid.UUID,
    counts: dict[datetime, int],
) -> None:
    for bucket, count in counts.items():
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=scan_config.id,
                event_id=event_id,
                event_type_id=None,
                bucket=bucket,
                count=count,
            )
        )
    session.commit()


def test_collect_event_composition_single_counts(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    numerator_event_id = uuid.uuid4()
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        scan_config = _seed_scan_config(session, project, data_source)
        _seed_numerator_metrics(
            session,
            scan_config,
            numerator_event_id,
            {datetime(2026, 1, 1, 10): 10, datetime(2026, 1, 1, 11): 20},
        )
        definition = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project.id,
            name="composition-single",
            display_name="Logins",
            kind=MetricKind.event_composition,
            composition=MetricComposition.single,
            numerator_event_id=numerator_event_id,
            status=MetricStatus.active,
        )
        session.add(definition)
        session.commit()
        def_id = str(definition.id)
        scan_config_id = scan_config.id

    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)

    result = metric_collect.collect_metric_definitions.run(def_id)

    assert result["values"] == 2
    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == uuid.UUID(def_id))
            )
            .scalars()
            .all()
        )
        assert {(row.bucket, row.value) for row in rows} == {
            (datetime(2026, 1, 1, 10, tzinfo=UTC), 10.0),
            (datetime(2026, 1, 1, 11, tzinfo=UTC), 20.0),
        }
        # event_composition values are keyed by the source scan grid.
        assert all(row.scan_config_id == scan_config_id for row in rows)


def test_collect_event_composition_ratio_handles_divide_by_zero(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    numerator_event_id = uuid.uuid4()
    denominator_event_id = uuid.uuid4()
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        scan_config = _seed_scan_config(session, project, data_source)
        _seed_numerator_metrics(
            session,
            scan_config,
            numerator_event_id,
            {
                datetime(2026, 1, 1, 10): 10,
                datetime(2026, 1, 1, 11): 20,
                datetime(2026, 1, 1, 12): 30,
            },
        )
        # Denominator: b10=2 (ratio 5), b11=0 (divide-by-zero -> None),
        # b12 absent (also zero -> None).
        for bucket, count in (
            (datetime(2026, 1, 1, 10), 2),
            (datetime(2026, 1, 1, 11), 0),
        ):
            session.add(
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=denominator_event_id,
                    event_type_id=None,
                    bucket=bucket,
                    count=count,
                )
            )
        definition = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project.id,
            name="composition-ratio",
            display_name="Conversion",
            kind=MetricKind.event_composition,
            composition=MetricComposition.ratio,
            numerator_event_id=numerator_event_id,
            denominator_event_id=denominator_event_id,
            status=MetricStatus.active,
        )
        session.add(definition)
        session.commit()
        def_id = str(definition.id)

    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)

    result = metric_collect.collect_metric_definitions.run(def_id)

    # Only the one finite-ratio bucket is stored; divide-by-zero buckets are not
    # written (the NOT NULL value column cannot hold None).
    assert result["values"] == 1
    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == uuid.UUID(def_id))
            )
            .scalars()
            .all()
        )
        assert {(row.bucket, row.value) for row in rows} == {
            (datetime(2026, 1, 1, 10, tzinfo=UTC), 5.0)
        }


def test_collect_event_composition_ratio_clears_out_of_range_denominator_bucket(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The delete window must cover the UNION of numerator + denominator buckets.

    A ratio denominator bucket that precedes every numerator bucket densifies to a
    value row (num=0 -> 0.0). When that bucket's denominator later drops to zero the
    ratio becomes None (no UPSERT row), so only a union-derived delete window clears
    the now-stale row; a numerator-only window would strand it permanently.
    """
    numerator_event_id = uuid.uuid4()
    denominator_event_id = uuid.uuid4()
    b10 = datetime(2026, 1, 1, 10)
    b11 = datetime(2026, 1, 1, 11)
    b12 = datetime(2026, 1, 1, 12)
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        scan_config = _seed_scan_config(session, project, data_source)
        # Numerator only at b11, b12 -- b10 is denominator-only (out of range).
        _seed_numerator_metrics(session, scan_config, numerator_event_id, {b11: 10, b12: 20})
        denom_b10 = EventMetric(
            id=uuid.uuid4(),
            scan_config_id=scan_config.id,
            event_id=denominator_event_id,
            event_type_id=None,
            bucket=b10,
            count=2,
        )
        session.add(denom_b10)
        session.add_all(
            [
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=denominator_event_id,
                    event_type_id=None,
                    bucket=b11,
                    count=5,
                ),
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=denominator_event_id,
                    event_type_id=None,
                    bucket=b12,
                    count=5,
                ),
            ]
        )
        definition = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project.id,
            name="composition-ratio-oob",
            display_name="Conversion",
            kind=MetricKind.event_composition,
            composition=MetricComposition.ratio,
            numerator_event_id=numerator_event_id,
            denominator_event_id=denominator_event_id,
            status=MetricStatus.active,
        )
        session.add(definition)
        session.commit()
        def_id = str(definition.id)
        denom_b10_id = denom_b10.id

    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)

    # Run 1: b10 = 0/2 = 0.0 is stored alongside the finite b11, b12 ratios.
    metric_collect.collect_metric_definitions.run(def_id)
    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == uuid.UUID(def_id))
            )
            .scalars()
            .all()
        )
        assert {(row.bucket, row.value) for row in rows} == {
            (b10.replace(tzinfo=UTC), 0.0),
            (b11.replace(tzinfo=UTC), 2.0),
            (b12.replace(tzinfo=UTC), 4.0),
        }
        # Flip the out-of-range denominator bucket to zero: b10 now divides by zero
        # (-> None, no UPSERT row). Only a union-derived window deletes the stale row.
        denom_row = session.get(EventMetric, denom_b10_id)
        assert denom_row is not None
        denom_row.count = 0
        session.commit()

    # Run 2: b10 must be cleared (its window is part of the union), leaving only
    # the finite buckets -- a numerator-only window would strand the b10=0.0 row.
    metric_collect.collect_metric_definitions.run(def_id)
    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == uuid.UUID(def_id))
            )
            .scalars()
            .all()
        )
        assert {(row.bucket, row.value) for row in rows} == {
            (b11.replace(tzinfo=UTC), 2.0),
            (b12.replace(tzinfo=UTC), 4.0),
        }


class _DistinctUserAdapter:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def test_connection(self) -> bool:
        return True

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        # The columns the source scan's base_query projects: the collector
        # introspects them to arm the adapter allowlist before it can hand
        # the distinct-user column to the aggregate.
        return [
            ColumnInfo(name="ts", type_name="DateTime"),
            ColumnInfo(name="user_id", type_name="String"),
        ]

    def get_time_bucketed_aggregate(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        agg_fn: MetricAggregation,
        measure_column: str | None,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        assert agg_fn is MetricAggregation.count_distinct
        assert measure_column == "user_id"
        return ([], [], self._rows)

    def close(self) -> None:
        return None


def test_collect_event_composition_per_distinct_user(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    numerator_event_id = uuid.uuid4()
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        scan_config = _seed_scan_config(session, project, data_source)
        _seed_numerator_metrics(
            session,
            scan_config,
            numerator_event_id,
            {datetime(2026, 1, 1, 10): 100, datetime(2026, 1, 1, 11): 60},
        )
        definition = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project.id,
            name="composition-per-user",
            display_name="Events per user",
            kind=MetricKind.event_composition,
            composition=MetricComposition.per_distinct_user,
            numerator_event_id=numerator_event_id,
            status=MetricStatus.active,
        )
        session.add(definition)
        session.commit()
        def_id = str(definition.id)

    distinct_adapter = _DistinctUserAdapter(
        [(datetime(2026, 1, 1, 10), 20), (datetime(2026, 1, 1, 11), 0)]
    )
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: distinct_adapter)

    result = metric_collect.collect_metric_definitions.run(def_id)

    # b10: 100 / 20 = 5.0; b11: distinct 0 -> None (not stored).
    assert result["values"] == 1
    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == uuid.UUID(def_id))
            )
            .scalars()
            .all()
        )
        assert {(row.bucket, row.value) for row in rows} == {
            (datetime(2026, 1, 1, 10, tzinfo=UTC), 5.0)
        }


# ── pure evaluator ─────────────────────────────────────────────────────────────


def test_evaluate_composition_densifies_misaligned_buckets() -> None:
    b10 = datetime(2026, 1, 1, 10, tzinfo=UTC)
    b11 = datetime(2026, 1, 1, 11, tzinfo=UTC)
    b12 = datetime(2026, 1, 1, 12, tzinfo=UTC)
    # Numerator on {b10, b12}, denominator on {b10, b11}: the union aligns them.
    values = evaluate_composition(
        MetricComposition.ratio,
        numerator={b10: 10.0, b12: 30.0},
        denominator={b10: 2.0, b11: 5.0},
    )
    assert values == {b10: 5.0, b11: 0.0, b12: None}


def test_evaluate_composition_ratio_zero_denominator_is_none() -> None:
    bucket = datetime(2026, 1, 1, 10, tzinfo=UTC)
    values = evaluate_composition(
        MetricComposition.ratio,
        numerator={bucket: 7.0},
        denominator={bucket: 0.0},
    )
    assert values == {bucket: None}


def test_coerce_bucket_makes_naive_datetime_utc_aware_and_floored() -> None:
    # A naive, unaligned warehouse bucket is coerced to UTC and floored to the
    # interval -- the normalization the fact path relies on so stored
    # buckets never mix naive/aware bounds across collection runs.
    coerced = metric_collect._coerce_bucket(datetime(2026, 1, 1, 10, 37), "1h")
    assert coerced == datetime(2026, 1, 1, 10, tzinfo=UTC)
    assert coerced.tzinfo is UTC


def test_coerce_weekly_bucket_uses_canonical_monday_grid() -> None:
    coerced = metric_collect._coerce_bucket(datetime(2026, 1, 8, 10, 37), "1w")

    assert coerced == datetime(2026, 1, 5, tzinfo=UTC)


def test_manual_weekly_window_ends_on_canonical_monday() -> None:
    time_from, time_to = metric_collect.compute_manual_collect_window("1w")

    assert time_to.weekday() == 0
    assert time_to.hour == 0
    assert time_to.minute == 0
    assert time_to - time_from == timedelta(weeks=4)


# ── beat dispatch ──────────────────────────────────────────────────────────────


def _make_active_fact_metric_for_dispatch(
    session: Session, project: Project, data_source: DataSource
) -> MetricDefinition:
    fact_table = _seed_fact_table(session, project, data_source)
    definition = MetricDefinition(
        id=uuid.uuid4(),
        project_id=project.id,
        name="due-metric",
        display_name="Due Metric",
        kind=MetricKind.fact,
        composition=MetricComposition.single,
        aggregation=MetricAggregation.count,
        fact_table_id=fact_table.id,
        config={},
        interval=ScanInterval.h1,
        status=MetricStatus.active,
    )
    session.add(definition)
    session.commit()
    return definition


def test_check_metric_definitions_due_dispatches_and_marks_running(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        definition = _make_active_fact_metric_for_dispatch(session, project, data_source)
        def_id = definition.id

    dispatched: list[list[str]] = []
    monkeypatch.setattr(metrics_schedule, "_get_sync_session", sync_session_factory)
    # Fact metrics route through the batched collector (grouped by interval).
    monkeypatch.setattr(
        metrics_schedule.collect_fact_metrics_batch,
        "delay",
        lambda metric_ids: dispatched.append(metric_ids),
    )

    result = metrics_schedule.check_metric_definitions_due.run()

    assert result == {"checked": 1, "dispatched": 1}
    assert dispatched == [[str(def_id)]]
    with sync_session_factory() as session:
        reloaded = session.get(MetricDefinition, def_id)
        assert reloaded is not None
        assert reloaded.last_collection_status == "running"


def test_check_metric_definitions_due_skips_when_running(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        definition = _make_active_fact_metric_for_dispatch(session, project, data_source)
        # A fresh in-progress run holds the one-active guard.
        definition.last_collection_status = "running"
        session.commit()

    dispatched: list[str] = []
    monkeypatch.setattr(metrics_schedule, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(
        metrics_schedule.collect_metric_definitions,
        "delay",
        lambda metric_definition_id: dispatched.append(metric_definition_id),
    )

    result = metrics_schedule.check_metric_definitions_due.run()

    assert result == {"checked": 1, "dispatched": 0}
    assert dispatched == []


def test_check_metric_definitions_due_reaps_stale_running(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    from tripl.worker.tasks.metrics._helpers import STALE_ACTIVE_SCAN_JOB_TIMEOUT

    stale_at = datetime.now(UTC) - STALE_ACTIVE_SCAN_JOB_TIMEOUT - timedelta(minutes=10)
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        definition = _make_active_fact_metric_for_dispatch(session, project, data_source)
        definition.last_collection_status = "running"
        definition.updated_at = stale_at
        session.commit()
        def_id = definition.id

    dispatched: list[list[str]] = []
    monkeypatch.setattr(metrics_schedule, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(
        metrics_schedule.collect_fact_metrics_batch,
        "delay",
        lambda metric_ids: dispatched.append(metric_ids),
    )

    result = metrics_schedule.check_metric_definitions_due.run()

    # A stale running marker is reclaimed and the metric is re-dispatched.
    assert result == {"checked": 1, "dispatched": 1}
    assert dispatched == [[str(def_id)]]


def test_check_metric_definitions_due_skips_non_active(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """Scheduled collection stays active-only: a draft metric is never dispatched."""
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        fact_table = _seed_fact_table(session, project, data_source)
        definition = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project.id,
            name="draft-due-metric",
            display_name="Draft Due Metric",
            kind=MetricKind.fact,
            composition=MetricComposition.single,
            aggregation=MetricAggregation.count,
            fact_table_id=fact_table.id,
            config={},
            interval=ScanInterval.h1,
            status=MetricStatus.draft,
        )
        session.add(definition)
        session.commit()

    dispatched: list[list[str]] = []
    monkeypatch.setattr(metrics_schedule, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(
        metrics_schedule.collect_fact_metrics_batch,
        "delay",
        lambda metric_ids: dispatched.append(metric_ids),
    )

    result = metrics_schedule.check_metric_definitions_due.run()

    # The dispatcher query only loads active metrics, so the draft is invisible.
    assert result == {"checked": 0, "dispatched": 0}
    assert dispatched == []


def test_weekly_catalog_metric_due_uses_canonical_monday_grid(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A weekly metric must not become due early on the legacy Saturday grid."""
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        definition = _make_active_fact_metric_for_dispatch(session, project, data_source)
        definition.interval = ScanInterval.w1
        # On Saturday Jul 18, the latest complete canonical Monday bucket is
        # Jul 6. The next bucket (Jul 13) completes on Monday Jul 20.
        session.add(
            MetricValue(
                metric_definition_id=definition.id,
                bucket=datetime(2026, 7, 6, tzinfo=UTC),
                value=1.0,
            )
        )
        session.commit()

        assert (
            metrics_schedule._metric_definition_due(
                session,
                definition,
                now=datetime(2026, 7, 18, 12, tzinfo=UTC),
            )
            is False
        )


def test_successful_empty_window_is_not_immediately_due(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The source-window watermark, not value existence, is scheduler progress."""
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        definition = _make_active_fact_metric_for_dispatch(session, project, data_source)
        definition.interval = ScanInterval.d1
        # No MetricValue rows were produced, but the source was successfully
        # scanned through the current UTC boundary.
        definition.last_collection_window_to = datetime(2026, 7, 18, tzinfo=UTC)
        definition.last_collection_status = "success"
        session.commit()

        assert (
            metrics_schedule._metric_definition_due(
                session,
                definition,
                now=datetime(2026, 7, 18, 12, tzinfo=UTC),
            )
            is False
        )


def test_empty_window_watermark_bounds_the_next_resume_window(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A zero-row success resumes near its watermark, not at the default lookback."""
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        definition = _make_active_fact_metric_for_dispatch(session, project, data_source)
        definition.interval = ScanInterval.d1
        current_boundary = floor_to_bucket(datetime.now(UTC), "1d")
        definition.last_collection_window_to = current_boundary - timedelta(days=1)
        session.commit()

        time_from, time_to = metric_collect._resolve_value_window(
            session,
            metric_definition_id=definition.id,
            interval_code="1d",
        )

        assert time_to == current_boundary
        assert time_from == definition.last_collection_window_to - timedelta(days=2)


# ── fact row-filter WHERE assembly (multiple named filters + free-text SQL) ──


def _filter_fact_table() -> FactTable:
    """A transient FactTable with two named row filters (no session needed)."""
    return FactTable(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        name="orders_ft",
        display_name="Orders",
        data_source_id=uuid.uuid4(),
        sql="SELECT ts, amount FROM orders",
        timestamp_column="ts",
        columns=[
            {"name": "ts", "type": "timestamp"},
            {"name": "amount", "type": "number"},
            {"name": "user_id", "type": "string"},
        ],
        identifier_columns=[],
        row_filters=[
            {"name": "positive", "sql": "amount > 0"},
            {"name": "big", "sql": "amount > 100"},
        ],
    )


def _filter_operand(
    *,
    row_filters: tuple[str, ...] = (),
    filter_sql: str | None = None,
    conditions: tuple[_fact_conditions._FactCondition, ...] = (),
) -> _fact_conditions._FactOperand:
    return _fact_conditions._FactOperand(
        fact_table_id=uuid.uuid4(),
        aggregation=MetricAggregation.count,
        measure_column=None,
        distinct_column=None,
        row_filters=row_filters,
        filter_sql=filter_sql,
        conditions=conditions,
    )


#: The dialect most of the assembly tests below run under. Assembly (AND-joining,
#: parenthesising, named-filter lookup) is dialect-independent; the tests that are
#: ABOUT the dialect parametrise over all three explicitly.
_CH = SqlDialect.clickhouse


def test_combined_filter_empty_is_none() -> None:
    fact_table = _filter_fact_table()
    assert (
        _fact_conditions._resolve_combined_filter(
            fact_table, dialect=_CH, row_filters=(), filter_sql=None
        )
        is None
    )


def test_combined_filter_single_named_is_parenthesised() -> None:
    fact_table = _filter_fact_table()
    combined = _fact_conditions._resolve_combined_filter(
        fact_table, dialect=_CH, row_filters=("positive",), filter_sql=None
    )
    assert combined == "(amount > 0)"


def test_combined_filter_multiple_named_are_anded() -> None:
    fact_table = _filter_fact_table()
    combined = _fact_conditions._resolve_combined_filter(
        fact_table, dialect=_CH, row_filters=("positive", "big"), filter_sql=None
    )
    assert combined == "(amount > 0) AND (amount > 100)"


def test_combined_filter_free_text_only() -> None:
    fact_table = _filter_fact_table()
    combined = _fact_conditions._resolve_combined_filter(
        fact_table, dialect=_CH, row_filters=(), filter_sql="status = 'paid'"
    )
    assert combined == "(status = 'paid')"


@pytest.mark.parametrize("dialect", list(SqlDialect))
def test_free_text_filters_stay_verbatim_on_every_dialect(dialect: SqlDialect) -> None:
    """Free-text SQL is the user's own dialect-specific text: never rewritten."""
    fact_table = _filter_fact_table()
    combined = _fact_conditions._resolve_combined_filter(
        fact_table,
        dialect=dialect,
        row_filters=("positive",),
        filter_sql="status = 'paid'",
    )
    assert combined == "(amount > 0) AND (status = 'paid')"


def test_combined_filter_named_and_free_text_are_anded() -> None:
    fact_table = _filter_fact_table()
    combined = _fact_conditions._resolve_combined_filter(
        fact_table, dialect=_CH, row_filters=("positive",), filter_sql="status = 'paid'"
    )
    assert combined == "(amount > 0) AND (status = 'paid')"


def test_combined_filter_condition_only() -> None:
    fact_table = _filter_fact_table()
    combined = _fact_conditions._resolve_combined_filter(
        fact_table,
        dialect=_CH,
        row_filters=(),
        filter_sql=None,
        conditions=(_fact_conditions._FactCondition("amount", "gt", "3"),),
    )
    assert combined == "(`amount` > 3)"


def test_numeric_looking_string_stays_quoted_for_string_column() -> None:
    """Persisted string-column values must not be retyped just because they look numeric."""
    fact_table = _filter_fact_table()
    combined = _fact_conditions._resolve_combined_filter(
        fact_table,
        dialect=_CH,
        row_filters=(),
        filter_sql=None,
        conditions=(_fact_conditions._FactCondition("user_id", "eq", "001"),),
    )
    assert combined == "(`user_id` = '001')"


def test_invalid_numeric_literal_is_rejected_for_number_column() -> None:
    """Old string configs get type-directed validation from fact-table metadata."""
    fact_table = _filter_fact_table()
    with pytest.raises(ScanError, match="numeric column 'amount' requires a numeric value"):
        _fact_conditions._resolve_combined_filter(
            fact_table,
            dialect=_CH,
            row_filters=(),
            filter_sql=None,
            conditions=(_fact_conditions._FactCondition("amount", "gt", "many"),),
        )


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        # PostgreSQL and BigQuery BOTH reject an unquoted reserved column ("syntax
        # error at or near \"order\"" / "Unexpected keyword ORDER"); ClickHouse
        # accepts it, which is why an unquoted compiler shipped green.
        (SqlDialect.clickhouse, "(`order` = 'o1')"),
        (SqlDialect.postgres, "(\"order\" = 'o1')"),
        (SqlDialect.bigquery, "(`order` = 'o1')"),
        (SqlDialect.databricks, "(`order` = 'o1')"),
    ],
)
def test_condition_quotes_reserved_identifier_per_dialect(
    dialect: SqlDialect, expected: str
) -> None:
    fact_table = _filter_fact_table()
    combined = _fact_conditions._resolve_combined_filter(
        fact_table,
        dialect=dialect,
        row_filters=(),
        filter_sql=None,
        conditions=(_fact_conditions._FactCondition("order", "eq", "o1"),),
    )
    assert combined == expected


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        # PostgreSQL DOUBLES the quote; a backslash there is a literal backslash and
        # would leave the string unterminated. BigQuery has NO '' escape at all --
        # 'u'' ...' is read as two adjacent literals and is a hard parse error -- so
        # it (and ClickHouse, which accepts both) get the backslash form.
        (SqlDialect.clickhouse, "(`user_id` = 'u\\' OR 1=1 --')"),
        (SqlDialect.postgres, "(\"user_id\" = 'u'' OR 1=1 --')"),
        (SqlDialect.bigquery, "(`user_id` = 'u\\' OR 1=1 --')"),
        (SqlDialect.databricks, "(`user_id` = 'u\\' OR 1=1 --')"),
    ],
)
def test_condition_escapes_string_values_per_dialect(dialect: SqlDialect, expected: str) -> None:
    fact_table = _filter_fact_table()
    combined = _fact_conditions._resolve_combined_filter(
        fact_table,
        dialect=dialect,
        row_filters=(),
        filter_sql=None,
        conditions=(_fact_conditions._FactCondition("user_id", "eq", "u' OR 1=1 --"),),
    )
    assert combined == expected


@pytest.mark.parametrize("dialect", list(SqlDialect))
def test_condition_value_cannot_break_out_of_its_string(dialect: SqlDialect) -> None:
    """Whatever the escape, a value only ever adds PAIRED quotes."""
    fact_table = _filter_fact_table()
    combined = _fact_conditions._resolve_combined_filter(
        fact_table,
        dialect=dialect,
        row_filters=(),
        filter_sql=None,
        conditions=(_fact_conditions._FactCondition("user_id", "eq", "x' OR TRUE OR 'y"),),
    )
    assert combined is not None
    # The compiled fragment is one comparison against one literal: the injected
    # OR never becomes a predicate.
    assert combined.count(" OR TRUE OR ") == 1
    assert combined.startswith("(") and combined.endswith("')")


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        # A time column's bound is pinned to UTC with the dialect's own typed literal
        # -- the same form each adapter renders its window bounds with. A bare string
        # parses everywhere, but is re-read in the COLUMN's timezone (ClickHouse) or
        # the SESSION's (PostgreSQL), silently shifting the comparison.
        (
            SqlDialect.clickhouse,
            "(`ts` > parseDateTime64BestEffort('2026-01-01 00:00:00.000000+00:00', 6, 'UTC'))",
        ),
        (SqlDialect.postgres, "(\"ts\" > TIMESTAMPTZ '2026-01-01 00:00:00.000000+00:00')"),
        (SqlDialect.bigquery, "(`ts` > TIMESTAMP '2026-01-01 00:00:00.000000+00:00')"),
        (SqlDialect.databricks, "(`ts` > TIMESTAMP '2026-01-01 00:00:00.000000+00:00')"),
    ],
)
def test_condition_on_time_column_emits_typed_utc_literal(
    dialect: SqlDialect, expected: str
) -> None:
    fact_table = _filter_fact_table()
    combined = _fact_conditions._resolve_combined_filter(
        fact_table,
        dialect=dialect,
        row_filters=(),
        filter_sql=None,
        conditions=(_fact_conditions._FactCondition("ts", "gt", "2026-01-01 00:00:00"),),
    )
    assert combined == expected


def test_condition_on_non_time_column_keeps_plain_literal() -> None:
    """A timestamp-shaped value in a NON-time column is still a plain string."""
    fact_table = _filter_fact_table()
    combined = _fact_conditions._resolve_combined_filter(
        fact_table,
        dialect=SqlDialect.bigquery,
        row_filters=(),
        filter_sql=None,
        # `user_id` is not in the introspected columns at all -> unknown type.
        conditions=(_fact_conditions._FactCondition("user_id", "eq", "2026-01-01"),),
    )
    assert combined == "(`user_id` = '2026-01-01')"


@pytest.mark.parametrize(
    ("operator", "value", "expected"),
    [
        ("ne", "trial", "`user_id` != 'trial'"),
        ("contains", "wind", "`user_id` LIKE '%wind%'"),
        ("not_contains", "test", "`user_id` NOT LIKE '%test%'"),
        ("like", "u_%", "`user_id` LIKE 'u_%'"),
        ("in", "a, b", "`user_id` IN ('a', 'b')"),
        ("not_in", ["a", "b"], "`user_id` NOT IN ('a', 'b')"),
        ("is_null", None, "`user_id` IS NULL"),
        ("is_not_null", None, "`user_id` IS NOT NULL"),
        ("is_true", None, "`user_id` = TRUE"),
        ("is_false", None, "`user_id` = FALSE"),
    ],
)
def test_combined_filter_condition_operators_compile(
    operator: str, value: object | None, expected: str
) -> None:
    fact_table = _filter_fact_table()
    combined = _fact_conditions._resolve_combined_filter(
        fact_table,
        dialect=_CH,
        row_filters=(),
        filter_sql=None,
        conditions=(_fact_conditions._FactCondition("user_id", operator, value),),
    )
    assert combined == f"({expected})"


def test_combined_filter_named_free_text_and_condition_are_anded() -> None:
    fact_table = _filter_fact_table()
    combined = _fact_conditions._resolve_combined_filter(
        fact_table,
        dialect=_CH,
        row_filters=("positive",),
        filter_sql="status = 'paid'",
        conditions=(_fact_conditions._FactCondition("amount", "lte", "100"),),
    )
    assert combined == "(amount > 0) AND (status = 'paid') AND (`amount` <= 100)"


def test_combined_filter_unknown_name_raises() -> None:
    fact_table = _filter_fact_table()
    with pytest.raises(ScanError):
        _fact_conditions._resolve_combined_filter(
            fact_table, dialect=_CH, row_filters=("ghost",), filter_sql=None
        )


def test_per_metric_query_wraps_with_combined_filter() -> None:
    fact_table = _filter_fact_table()
    operand = _filter_operand(
        row_filters=("positive", "big"),
        filter_sql="status = 'paid'",
        conditions=(_fact_conditions._FactCondition("amount", "lte", "100"),),
    )
    query = _fact_conditions._resolve_fact_operand_query(fact_table, operand, dialect=_CH)
    assert query == (
        "SELECT * FROM (SELECT ts, amount FROM orders) AS _filtered "
        "WHERE (amount > 0) AND (amount > 100) AND (status = 'paid') AND (`amount` <= 100)"
    )


def test_per_metric_query_unfiltered_returns_source() -> None:
    fact_table = _filter_fact_table()
    operand = _filter_operand()
    assert (
        _fact_conditions._resolve_fact_operand_query(fact_table, operand, dialect=_CH)
        == fact_table.sql
    )


@pytest.mark.parametrize("dialect", list(SqlDialect))
def test_per_metric_and_batch_filter_are_value_identical(dialect: SqlDialect) -> None:
    """The WHERE the per-metric path embeds == the conditional the batch injects."""
    fact_table = _filter_fact_table()
    operand = _filter_operand(
        row_filters=("positive", "big"),
        filter_sql="status = 'paid'",
        conditions=(_fact_conditions._FactCondition("amount", "lte", "100"),),
    )

    batch_filter = _fact_conditions._resolve_fact_operand_filter(
        operand, fact_table=fact_table, dialect=dialect
    )
    per_metric_query = _fact_conditions._resolve_fact_operand_query(
        fact_table, operand, dialect=dialect
    )

    assert batch_filter is not None
    # The exact boolean expression used in both paths must be identical.
    assert per_metric_query.endswith(f"WHERE {batch_filter}")


def test_effective_filter_names_folds_legacy_row_filter() -> None:
    assert _fact_conditions._effective_filter_names({"row_filter": "positive"}) == ("positive",)


def test_effective_filter_names_merges_list_and_legacy() -> None:
    config = {"row_filters": ["positive"], "row_filter": "big"}
    assert _fact_conditions._effective_filter_names(config) == ("positive", "big")


def test_effective_filter_names_dedups_legacy_already_present() -> None:
    config = {"row_filters": ["positive"], "row_filter": "positive"}
    assert _fact_conditions._effective_filter_names(config) == ("positive",)
