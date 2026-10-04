"""Column guards and the bounded resume region of the per-metric collectors.

Three defects, one worker module (``worker/tasks/metrics/metric_collect.py``):

* ``_collect_distinct_user_series`` queried the warehouse without
  ever calling ``get_columns``, so the adapter's column allowlist stayed empty and
  every membership check short-circuited; and a fact metric's missing measure
  column escaped as a plain ``ValueError``, which ``user_facing_error`` replaces
  with the generic internal-error summary.
* ``_collect_event_composition`` re-derived from the FULL retained
  event-metric history on every dispatch, so a ``per_distinct_user`` metric
  re-queried the warehouse over its whole history every time and errored
  permanently once that history passed ``METRIC_QUERY_ROW_LIMIT`` buckets.
* the per-metric fact collectors (kept deliberately as the
  independent oracle the Gate-4 conformance run compares the batched path
  against) had drifted: an unguarded ``float(None)`` on an all-NULL aggregate
  cell, no collection-time condition-column recheck, and no column allowlist at
  all for a ``count`` metric's breakdown.

Same sync-SQLite fixture style as ``test_metric_collection.py``: a file-backed
engine from ``Base.metadata.create_all`` with the task module's
``_get_sync_session`` / ``_build_adapter`` / ``_resolve_value_window`` globals
monkey-patched per test.
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

from tripl.core.adapters.base import ColumnInfo
from tripl.core.bucketing import to_utc
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
from tripl.worker.tasks._errors import ScanError
from tripl.worker.tasks.metrics import metric_collect

BASE = datetime(2026, 1, 1, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _b(hour: int) -> datetime:
    """The bucket ``hour`` hours after the fixture's base instant."""
    return BASE + HOUR * hour


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch3_c2.db'}")
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


def _seed_fact_table(session: Session, project: Project, data_source: DataSource) -> FactTable:
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
    )
    session.add(fact_table)
    session.commit()
    return fact_table


def _make_fact_metric(
    session: Session, project: Project, fact_table: FactTable, **overrides: object
) -> MetricDefinition:
    values: dict[str, object] = {
        "composition": MetricComposition.single,
        "aggregation": MetricAggregation.sum,
        "config": {"measure_column": "amount"},
    }
    values.update(overrides)
    definition = MetricDefinition(
        id=uuid.uuid4(),
        project_id=project.id,
        name=f"fact-{uuid.uuid4().hex[:6]}",
        display_name="Revenue",
        kind=MetricKind.fact,
        fact_table_id=fact_table.id,
        interval=ScanInterval.h1,
        status=MetricStatus.active,
        **values,
    )
    session.add(definition)
    session.commit()
    return definition


class _FactAdapter:
    """Fake warehouse adapter recording what the collector introspects.

    ``columns`` is what ``get_columns`` reports for ANY query, so a test can
    withhold a column the metric references and watch the collector notice.
    """

    def __init__(
        self,
        rows: list[tuple[object, ...]],
        *,
        columns: list[str] | None = None,
        breakdown_rows: list[tuple[object, ...]] | None = None,
    ) -> None:
        self._rows = rows
        self._columns = ["ts", "amount", "user_id"] if columns is None else columns
        self._breakdown_rows = breakdown_rows or []
        self.column_queries: list[str] = []
        self.aggregate_calls = 0
        self.breakdown_calls: list[str] = []

    def test_connection(self) -> bool:
        return True

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        self.column_queries.append(base_query)
        return [ColumnInfo(name=name, type_name="String") for name in self._columns]

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
        self.aggregate_calls += 1
        return ([], [], self._rows)

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
        self.breakdown_calls.append(breakdown_column)
        return ([breakdown_column], [], self._breakdown_rows)

    def close(self) -> None:
        return None


class _RefusingFactAdapter(_FactAdapter):
    """Fails the test if the collector reaches the warehouse at all.

    A column guard that fires only AFTER the query has run is no guard; this is
    what pins the ordering.
    """

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
        msg = "the aggregate query must not run once a column guard has failed"
        raise AssertionError(msg)


def _patch_fact_collector(
    monkeypatch: MonkeyPatch,
    *,
    session_factory: sessionmaker[Session],
    adapter: object,
    window: tuple[datetime, datetime] = (_b(10), _b(12)),
) -> None:
    monkeypatch.setattr(metric_collect, "_get_sync_session", session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(metric_collect, "_resolve_value_window", lambda *a, **k: window)


def _collection_error(session_factory: sessionmaker[Session], def_id: str) -> str:
    with session_factory() as session:
        definition = session.get(MetricDefinition, uuid.UUID(def_id))
        assert definition is not None
        assert definition.last_collection_status == metric_collect.COLLECTION_STATUS_ERROR
        assert definition.last_collection_error is not None
        return definition.last_collection_error


# ── NULL aggregate cells on the per-metric fact path ────────────


def test_collect_fact_records_a_null_aggregate_bucket_as_absent(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """An all-NULL aggregate cell is an absent bucket, not a failed collection.

    ``sum``/``avg`` over a bucket whose measure is NULL in every row projects
    NULL. The batched path already reads that as absent
    (``_index_multi_aggregate``); the per-metric path fed it straight to
    ``float()`` and lost the whole chunk to a ``TypeError``.
    """
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        fact_table = _seed_fact_table(session, project, data_source)
        def_id = str(_make_fact_metric(session, project, fact_table).id)

    adapter = _FactAdapter([(_b(10), 5.0), (_b(11), None)])
    _patch_fact_collector(monkeypatch, session_factory=sync_session_factory, adapter=adapter)

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
        assert {(row.bucket, row.value) for row in rows} == {(_b(10), 5.0)}
        definition = session.get(MetricDefinition, uuid.UUID(def_id))
        assert definition is not None
        assert definition.last_collection_status == metric_collect.COLLECTION_STATUS_SUCCESS
        assert definition.last_collection_error is None


def test_collect_fact_breakdown_skips_an_all_null_group(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A breakdown group whose aggregate is NULL records no row, and no crash.

    The conformance fixture refused to construct such a group precisely because
    this path could not survive it.
    """
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        fact_table = _seed_fact_table(session, project, data_source)
        def_id = str(
            _make_fact_metric(
                session,
                project,
                fact_table,
                breakdown_columns=["country"],
                breakdown_values_limit=2,
            ).id
        )

    adapter = _FactAdapter(
        [(_b(10), 12.0)],
        columns=["ts", "amount", "user_id", "country"],
        breakdown_rows=[
            (_b(10), "US", False, "US", 8.0),
            (_b(10), "FR", False, "FR", None),
        ],
    )
    _patch_fact_collector(
        monkeypatch,
        session_factory=sync_session_factory,
        adapter=adapter,
        window=(_b(10), _b(11)),
    )

    result = metric_collect.collect_metric_definitions.run(def_id)

    assert result["breakdown_values"] == 1
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
        assert {(row.breakdown_value, row.value) for row in rows} == {("US", 8.0)}


# ── column guards the per-metric path had lost ──────────────────


def test_collect_fact_condition_column_missing_from_the_fact_table_fails_with_its_name(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A stored condition is rechecked against the fact table at collection time.

    Condition columns are verified when the metric is saved and never again, so a
    column dropped or renamed in the warehouse afterwards used to compile into a
    query that died deep inside the worker. The batched path already refuses it by
    name (``_resolve_batch_operand`` -> ``_validate_condition_columns``).

    This is also the input that discriminates RAW fact SQL from the operand's
    filtered wrapper, so the allowlist's source is pinned here rather than on an
    unfiltered metric where the two strings are identical.
    """
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        fact_table = _seed_fact_table(session, project, data_source)
        fact_sql = fact_table.sql
        def_id = str(
            _make_fact_metric(
                session,
                project,
                fact_table,
                config={
                    "measure_column": "amount",
                    "conditions": [{"column": "country", "operator": "eq", "value": "US"}],
                },
            ).id
        )

    adapter = _RefusingFactAdapter([])
    _patch_fact_collector(monkeypatch, session_factory=sync_session_factory, adapter=adapter)

    with pytest.raises(ScanError):
        metric_collect.collect_metric_definitions.run(def_id)

    message = _collection_error(sync_session_factory, def_id)
    assert "'country'" in message
    assert "not columns of the fact table" in message
    # The allowlist is taken from the RAW fact SQL, never the operand's filtered
    # wrapper — and THIS metric is what makes that a testable claim: its condition
    # compiles INTO the wrapper, which ``_resolve_fact_operand_query`` renders as
    # ``SELECT * FROM (<fact sql>) AS _filtered WHERE (<quoted country> = 'US')``
    # — a different string from ``fact_table.sql``. Probing the wrapper would hand
    # the warehouse the unknown column inside the probe itself and die there,
    # before ``_validate_condition_columns`` could name it, which is the entire
    # reason the call was moved off the wrapper. The message assertions above
    # cannot see that: the adapter answers ``get_columns`` with the same column
    # list for any query, so only the recorded query distinguishes the two.
    assert adapter.column_queries == [fact_sql]


def test_collect_fact_count_breakdown_rejects_an_unknown_breakdown_column(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A ``count`` metric introspects its fact table like every other metric.

    ``requires_measure`` is False for ``count``, and the only ``get_columns`` call
    on this path sat inside that branch — so a ``count`` metric reached the
    warehouse with an EMPTY adapter allowlist, which disables the adapters' own
    ``_validate_column`` membership check (it short-circuits on a falsy allowlist)
    for the breakdown query's dimension.
    """
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        fact_table = _seed_fact_table(session, project, data_source)
        fact_sql = fact_table.sql
        def_id = str(
            _make_fact_metric(
                session,
                project,
                fact_table,
                aggregation=MetricAggregation.count,
                config={},
                breakdown_columns=["country"],
                breakdown_values_limit=2,
            ).id
        )

    adapter = _FactAdapter([(_b(10), 3.0)])
    _patch_fact_collector(
        monkeypatch,
        session_factory=sync_session_factory,
        adapter=adapter,
        window=(_b(10), _b(11)),
    )

    with pytest.raises(ScanError):
        metric_collect.collect_metric_definitions.run(def_id)

    message = _collection_error(sync_session_factory, def_id)
    assert "'country'" in message
    assert "not columns of the fact table" in message
    # Exactly ONE introspection, and it happened at all: ``requires_measure`` is
    # False for ``count``, and the call this path used to make sat inside that
    # branch, so before the fix this list was EMPTY. It says nothing about the
    # allowlist's SOURCE — this metric has no conditions, no ``filter_sql`` and no
    # row filters, so ``_resolve_fact_operand_query`` returns the fact SQL
    # verbatim and the raw form and the filtered wrapper are the same string here.
    # Raw-vs-wrapper is pinned where it can differ, in
    # ``test_collect_fact_condition_column_missing_from_the_fact_table_fails_with_its_name``.
    assert adapter.column_queries == [fact_sql]
    # No breakdown query ran: a column guard that fires after the query is no guard.
    assert adapter.breakdown_calls == []


def test_collect_fact_ratio_validates_the_denominator_operand_columns(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """Each ratio operand answers to its OWN fact table's allowlist.

    Pins that the denominator's allowlist is actually wired through: a
    denominator-only condition on a missing column must fail by name even though
    the numerator is clean.
    """
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        fact_table = _seed_fact_table(session, project, data_source)
        fact_sql = fact_table.sql
        def_id = str(
            _make_fact_metric(
                session,
                project,
                fact_table,
                composition=MetricComposition.ratio,
                aggregation=MetricAggregation.count,
                config={
                    "numerator": {
                        "fact_table_id": str(fact_table.id),
                        "aggregation": "count",
                    },
                    "denominator": {
                        "fact_table_id": str(fact_table.id),
                        "aggregation": "count",
                        "conditions": [
                            {"column": "subscription", "operator": "eq", "value": "paid"}
                        ],
                    },
                },
            ).id
        )

    adapter = _FactAdapter([(_b(10), 4.0)])
    _patch_fact_collector(monkeypatch, session_factory=sync_session_factory, adapter=adapter)

    with pytest.raises(ScanError):
        metric_collect.collect_metric_definitions.run(def_id)

    message = _collection_error(sync_session_factory, def_id)
    assert "'subscription'" in message
    assert "not columns of the fact table" in message
    # ONE introspection PER OPERAND — the two adapters are separate instances even
    # for a same-table ratio, so each needs its own ``_allowed_columns`` armed —
    # and both taken from the RAW fact SQL. The denominator is what makes the
    # second half testable: it carries the condition, so probing its filtered
    # wrapper would record ``SELECT * FROM (...) AS _filtered WHERE (...)`` here
    # instead. ``_patch_fact_collector`` hands the same adapter object to both
    # operands, so one list holds both queries, numerator first.
    assert adapter.column_queries == [fact_sql, fact_sql]


# ── the fact path's missing measure column is named ─────────────


def test_collect_fact_names_the_missing_measure_column(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A measure column the fact table does not have is named, not swallowed.

    ``validate_measure_column`` lives in ``core`` and raises ``ValueError``, which
    is not in ``_CURATED_ERRORS`` — so the message read "Scan failed due to an
    internal error." with nothing pointing at the column.

    This is the PER-METRIC collector, which production no longer dispatches (every
    fact metric goes through ``collect_fact_metrics_batch``); it survives as the
    conformance oracle. The operator-visible half is
    ``test_batch_fact_collection_names_the_missing_measure_column`` below — both
    now answer to the one ``_validated_measure_column`` helper.
    """
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        fact_table = _seed_fact_table(session, project, data_source)
        def_id = str(
            _make_fact_metric(session, project, fact_table, config={"measure_column": "revenue"}).id
        )

    adapter = _RefusingFactAdapter([])
    _patch_fact_collector(monkeypatch, session_factory=sync_session_factory, adapter=adapter)

    with pytest.raises(ScanError):
        metric_collect.collect_metric_definitions.run(def_id)

    message = _collection_error(sync_session_factory, def_id)
    assert "revenue" in message
    assert "internal error" not in message


# ── event_composition fixtures ───────────────────────────────────────────────


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


def _seed_numerator_metrics(
    session: Session, scan_config: ScanConfig, event_id: uuid.UUID, counts: dict[datetime, int]
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


def _make_per_user_metric(
    session: Session, project: Project, numerator_event_id: uuid.UUID, **overrides: object
) -> MetricDefinition:
    definition = MetricDefinition(
        id=uuid.uuid4(),
        project_id=project.id,
        name=f"per-user-{uuid.uuid4().hex[:6]}",
        display_name="Events per user",
        kind=MetricKind.event_composition,
        composition=MetricComposition.per_distinct_user,
        numerator_event_id=numerator_event_id,
        status=MetricStatus.active,
        **overrides,
    )
    session.add(definition)
    session.commit()
    return definition


class _DistinctUserAdapter:
    """Denominator adapter that records its window and answers over it.

    Returning one row per bucket of the REQUESTED window (rather than a canned
    list) is what lets a test see how far back the collector reached.
    """

    def __init__(self, *, columns: list[str] | None = None, users_per_bucket: float = 10.0) -> None:
        self._columns = ["ts", "user_id"] if columns is None else columns
        self._users_per_bucket = users_per_bucket
        self.column_queries: list[str] = []
        self.windows: list[tuple[datetime, datetime]] = []
        self.measure_columns: list[str | None] = []

    def test_connection(self) -> bool:
        return True

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        self.column_queries.append(base_query)
        return [ColumnInfo(name=name, type_name="String") for name in self._columns]

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
        self.windows.append((to_utc(time_from), to_utc(time_to)))
        self.measure_columns.append(measure_column)
        rows: list[tuple[object, ...]] = []
        bucket = to_utc(time_from)
        while bucket < to_utc(time_to):
            rows.append((bucket, self._users_per_bucket))
            bucket += HOUR
        return ([], [], rows)

    def close(self) -> None:
        return None


class _RefusingDistinctUserAdapter(_DistinctUserAdapter):
    """The denominator counterpart of ``_RefusingFactAdapter``."""

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
        msg = "the denominator query must not run once the column guard has failed"
        raise AssertionError(msg)


# ── the distinct-user column is validated ───────────────────────


def test_per_distinct_user_rejects_a_column_the_source_scan_does_not_project(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The only collection path that never populated the adapter's allowlist.

    Both ``validate_measure_column`` and the adapters' ``_validate_column``
    short-circuit their membership check when the allowlist is empty, and only
    ``get_columns`` ever fills it — so ``user_id_column`` reached the warehouse
    checked against nothing but the bare-identifier regex.
    """
    numerator_event_id = uuid.uuid4()
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        scan_config = _seed_scan_config(session, project, data_source)
        _seed_numerator_metrics(session, scan_config, numerator_event_id, {_b(10): 100})
        def_id = str(_make_per_user_metric(session, project, numerator_event_id).id)

    adapter = _RefusingDistinctUserAdapter(columns=["ts", "uid"])
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)

    with pytest.raises(ScanError):
        metric_collect.collect_metric_definitions.run(def_id)

    message = _collection_error(sync_session_factory, def_id)
    assert message.startswith("Scan failed")
    assert "user_id" in message
    assert "internal error" not in message


def test_per_distinct_user_fails_loudly_when_the_source_query_has_no_columns(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """An empty allowlist must raise, never be read as "everything is allowed"."""
    numerator_event_id = uuid.uuid4()
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        scan_config = _seed_scan_config(session, project, data_source)
        _seed_numerator_metrics(session, scan_config, numerator_event_id, {_b(10): 100})
        def_id = str(_make_per_user_metric(session, project, numerator_event_id).id)

    adapter = _RefusingDistinctUserAdapter(columns=[])
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)

    with pytest.raises(ScanError):
        metric_collect.collect_metric_definitions.run(def_id)

    assert "no columns" in _collection_error(sync_session_factory, def_id)


def test_per_distinct_user_validates_the_configured_column_not_the_default(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The guard answers to ``config["user_id_column"]``, not ``DEFAULT_USER_ID_COLUMN``.

    The source scan here does NOT project ``user_id``, so a guard that checked the
    default would reject a perfectly valid metric.
    """
    numerator_event_id = uuid.uuid4()
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        scan_config = _seed_scan_config(session, project, data_source)
        _seed_numerator_metrics(session, scan_config, numerator_event_id, {_b(10): 100})
        def_id = str(
            _make_per_user_metric(
                session,
                project,
                numerator_event_id,
                config={"user_id_column": "device_id"},
            ).id
        )

    adapter = _DistinctUserAdapter(columns=["ts", "device_id"], users_per_bucket=20.0)
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)

    result = metric_collect.collect_metric_definitions.run(def_id)

    assert result["values"] == 1
    assert adapter.measure_columns == ["device_id"]
    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == uuid.UUID(def_id))
            )
            .scalars()
            .all()
        )
        assert {(row.bucket, row.value) for row in rows} == {(_b(10), 5.0)}


# ── the bounded resume region ──────────────────────────────────


def _seed_per_user_grid(
    session_factory: sessionmaker[Session], *, buckets: int, count: int = 100
) -> tuple[str, uuid.UUID, ScanConfig]:
    numerator_event_id = uuid.uuid4()
    with session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        scan_config = _seed_scan_config(session, project, data_source)
        _seed_numerator_metrics(
            session,
            scan_config,
            numerator_event_id,
            {_b(hour): count for hour in range(buckets)},
        )
        def_id = str(_make_per_user_metric(session, project, numerator_event_id).id)
    return def_id, numerator_event_id, scan_config


def _append_numerator_bucket(
    session_factory: sessionmaker[Session],
    scan_config: ScanConfig,
    numerator_event_id: uuid.UUID,
    hour: int,
    count: int = 100,
) -> None:
    with session_factory() as session:
        _seed_numerator_metrics(session, scan_config, numerator_event_id, {_b(hour): count})


def test_per_distinct_user_denominator_query_resumes_from_the_last_stored_bucket(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The second run asks the warehouse for a resume window, not all of history.

    ``event_composition`` has no interval and no watermark, so its own stored
    buckets are the resume signal: two buckets of overlap (the same overlap
    ``_resolve_value_window`` keeps for late data) plus whatever is new.
    """
    def_id, numerator_event_id, scan_config = _seed_per_user_grid(sync_session_factory, buckets=6)

    adapter = _DistinctUserAdapter()
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)

    metric_collect.collect_metric_definitions.run(def_id)
    _append_numerator_bucket(sync_session_factory, scan_config, numerator_event_id, 6)
    metric_collect.collect_metric_definitions.run(def_id)

    # Run 1 has nothing stored, so it reaches the whole (short) series; run 2
    # resumes at stored_max(b05) - 2h and ends one interval past the new head.
    assert adapter.windows == [
        (to_utc(_b(0)), to_utc(_b(6))),
        (to_utc(_b(3)), to_utc(_b(7))),
    ]


def test_per_distinct_user_survives_a_history_longer_than_the_row_limit(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """Past a certain history the unbounded query tripped the row ceiling forever.

    One denominator row per non-empty bucket, so a metric whose retained history
    exceeded ``METRIC_QUERY_ROW_LIMIT`` buckets raised on EVERY run and could
    never recover. The resume region makes the per-run cost independent of how
    much history the grid holds.
    """
    def_id, numerator_event_id, scan_config = _seed_per_user_grid(sync_session_factory, buckets=20)

    adapter = _DistinctUserAdapter()
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)

    metric_collect.collect_metric_definitions.run(def_id)

    # A ceiling the resume window clears (4 buckets: b17..b20) and the full
    # history does not (21 buckets).
    monkeypatch.setattr(metric_collect, "METRIC_QUERY_ROW_LIMIT", 4)
    _append_numerator_bucket(sync_session_factory, scan_config, numerator_event_id, 20)

    result = metric_collect.collect_metric_definitions.run(def_id)

    assert result["values"] == 4
    assert adapter.windows[-1] == (to_utc(_b(17)), to_utc(_b(21)))
    with sync_session_factory() as session:
        definition = session.get(MetricDefinition, uuid.UUID(def_id))
        assert definition is not None
        assert definition.last_collection_status == metric_collect.COLLECTION_STATUS_SUCCESS
        head = session.execute(
            select(MetricValue.value).where(
                MetricValue.metric_definition_id == uuid.UUID(def_id),
                MetricValue.bucket == _b(20),
            )
        ).scalar_one()
        assert head == 10.0


def test_event_composition_leaves_history_outside_the_resume_region_alone(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The honest record of what bounding the work gives up.

    Before this change every run re-derived and re-wrote the ENTIRE stored series,
    so a stale historical value was silently repaired whenever any newer bucket
    arrived. It is not repaired any more — and the window-delete no longer reaches
    it either, which is what keeps the value below intact rather than erased.
    """
    def_id, numerator_event_id, scan_config = _seed_per_user_grid(sync_session_factory, buckets=6)

    adapter = _DistinctUserAdapter()
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)

    metric_collect.collect_metric_definitions.run(def_id)
    with sync_session_factory() as session:
        stale = session.execute(
            select(MetricValue).where(
                MetricValue.metric_definition_id == uuid.UUID(def_id),
                MetricValue.bucket == _b(0),
            )
        ).scalar_one()
        stale.value = 999.0
        session.commit()

    _append_numerator_bucket(sync_session_factory, scan_config, numerator_event_id, 6)
    metric_collect.collect_metric_definitions.run(def_id)

    with sync_session_factory() as session:
        stored = {
            bucket: value
            for bucket, value in session.execute(
                select(MetricValue.bucket, MetricValue.value).where(
                    MetricValue.metric_definition_id == uuid.UUID(def_id)
                )
            ).all()
        }
    # b00 is outside the resume region: neither recomputed nor deleted.
    assert stored[_b(0)] == 999.0
    # The region itself is recomputed as usual.
    assert stored[_b(6)] == 10.0


def test_per_distinct_user_first_collection_is_capped(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A grid with nothing stored still gets a bounded reach.

    The resume floor alone does not cover the metric that never stores anything —
    a ``per_distinct_user`` whose denominator is always zero writes no row, so
    ``max(MetricValue.bucket)`` stays NULL and the dispatcher re-fires on every
    tick forever. ``EVENT_COMPOSITION_BACKFILL_BUCKETS`` is what bounds that run.
    """
    def_id, _numerator_event_id, _scan_config = _seed_per_user_grid(
        sync_session_factory, buckets=10
    )

    adapter = _DistinctUserAdapter()
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(metric_collect, "EVENT_COMPOSITION_BACKFILL_BUCKETS", 3)

    metric_collect.collect_metric_definitions.run(def_id)

    assert adapter.windows == [(to_utc(_b(6)), to_utc(_b(10)))]


# ── the LIVE fact path answers to the same column guards ─────────────────────


class _RefusingBatchAdapter(_FactAdapter):
    """Fails the test if the BATCHED collector reaches the warehouse at all.

    The batch path's own scans are ``get_time_bucketed_multi_aggregate`` and its
    breakdown sibling; a column guard that fires only after one of those has run
    is no guard, and a guard that lets the query run and reports the adapter's
    bare ``ValueError`` is the generic internal-error message again.
    """

    def get_time_bucketed_multi_aggregate(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        specs: list[object],
        time_from: datetime,
        time_to: datetime,
        limit: int = 100000,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        msg = "the batched multi-aggregate must not run once a column guard has failed"
        raise AssertionError(msg)

    def get_time_bucketed_multi_aggregate_breakdown(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        breakdown_column: str,
        specs: list[object],
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None = None,
        limit: int = 100000,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        msg = "the batched breakdown scan must not run once a column guard has failed"
        raise AssertionError(msg)


def test_batch_fact_collection_names_the_missing_measure_column(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The path an operator actually hits must name the column too.

    Every fact metric is dispatched through ``collect_fact_metrics_batch`` — both
    by the scheduler and by "collect now" — so a fix that only reached
    ``_aggregate_fact_window`` fixed nothing anyone sees. ``_resolve_batch_operand``
    raised the bare ``ValueError`` straight into ``_stamp_metric_error``, which
    persisted "Scan failed due to an internal error." on the metric card.
    """
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        fact_table = _seed_fact_table(session, project, data_source)
        def_id = str(
            _make_fact_metric(session, project, fact_table, config={"measure_column": "revenue"}).id
        )

    adapter = _RefusingBatchAdapter([])
    _patch_fact_collector(monkeypatch, session_factory=sync_session_factory, adapter=adapter)

    result = metric_collect.collect_fact_metrics_batch.run([def_id])

    assert result["errors"] == 1
    message = _collection_error(sync_session_factory, def_id)
    assert "revenue" in message
    assert "internal error" not in message


def test_batch_fact_collection_names_an_unknown_breakdown_column(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A breakdown dimension the fact table does not project is named, not scanned.

    Nothing validates ``breakdown_columns`` against the fact table when the metric
    is SAVED — ``_verify_fact_operand`` covers the measure, distinct, condition and
    row-filter columns only — so this needs no schema drift to reach: the metric can
    be created this way. Before the guard the planner registered the dimension
    unchecked and the failure surfaced from inside the adapter as a bare
    ``ValueError``, i.e. as the generic internal-error summary.
    """
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        fact_table = _seed_fact_table(session, project, data_source)
        def_id = str(
            _make_fact_metric(
                session,
                project,
                fact_table,
                breakdown_columns=["country"],
            ).id
        )

    # ``country`` is absent from the fact table's projection.
    adapter = _RefusingBatchAdapter([], columns=["ts", "amount", "user_id"])
    _patch_fact_collector(monkeypatch, session_factory=sync_session_factory, adapter=adapter)

    result = metric_collect.collect_fact_metrics_batch.run([def_id])

    assert result["errors"] == 1
    message = _collection_error(sync_session_factory, def_id)
    assert "country" in message
    assert "internal error" not in message


def test_batch_ratio_breakdown_names_an_unknown_breakdown_column(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """``_plan_ratio_metric`` carries the same guard as ``_plan_single_metric``.

    A ratio metric reaches the breakdown registry through a different planner; the
    guard has to be on both or the asymmetry simply moves.
    """
    with sync_session_factory() as session:
        project, data_source = _seed_project_and_ds(session)
        fact_table = _seed_fact_table(session, project, data_source)
        operand = {
            "fact_table_id": str(fact_table.id),
            "aggregation": MetricAggregation.sum.value,
            "measure_column": "amount",
        }
        def_id = str(
            _make_fact_metric(
                session,
                project,
                fact_table,
                composition=MetricComposition.ratio,
                aggregation=None,
                config={"numerator": operand, "denominator": dict(operand)},
                breakdown_columns=["country"],
            ).id
        )

    adapter = _RefusingBatchAdapter([], columns=["ts", "amount", "user_id"])
    _patch_fact_collector(monkeypatch, session_factory=sync_session_factory, adapter=adapter)

    result = metric_collect.collect_fact_metrics_batch.run([def_id])

    assert result["errors"] == 1
    message = _collection_error(sync_session_factory, def_id)
    assert "country" in message
    assert "internal error" not in message


# ── the resume floor is not a one-way ratchet ────────────────────────────────


def _stored_buckets(session_factory: sessionmaker[Session], def_id: str) -> set[datetime]:
    """Every bucket this metric has stored, on the canonical comparison footing.

    ``to_utc`` because ``MetricValue.bucket`` reads back naive on sqlite and aware
    on PostgreSQL, and the composed values that went in were aware either way
    (``metric_composition.normalize_series``). The EXPECTATION therefore has to be
    stamped as well — ``_b()`` is the file's naive fixture anchor, and an aware set
    never equals a naive one even bucket for bucket.
    """
    with session_factory() as session:
        return {
            to_utc(bucket)
            for bucket in session.execute(
                select(MetricValue.bucket).where(
                    MetricValue.metric_definition_id == uuid.UUID(def_id)
                )
            )
            .scalars()
            .all()
        }


def test_composition_backfill_region_is_none_before_anything_is_stored() -> None:
    """The first run's cap owns the reach; the backward frontier starts after it."""
    assert (
        metric_collect._composition_backfill_region(
            stored_min=None,
            resume_floor=_b(6),
            oldest_source=_b(0),
            delta=HOUR,
        )
        is None
    )


def test_composition_backfill_region_stops_at_the_oldest_source_bucket() -> None:
    """Nothing older than the source series exists, so the walk terminates there."""
    assert (
        metric_collect._composition_backfill_region(
            stored_min=_b(0),
            resume_floor=_b(6),
            oldest_source=_b(0),
            delta=HOUR,
        )
        is None
    )


def test_event_composition_backfills_pre_history_over_successive_runs(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """History the first run could not reach must not be stranded for good.

    ``_composition_series_floor`` is anchored on ``max(MetricValue.bucket)``, which
    only ever moves forward — so the resume region alone is a one-way ratchet: a
    grid holding more history than ``EVENT_COMPOSITION_BACKFILL_BUCKETS`` intervals
    would be truncated at whatever the first run happened to reach, permanently.
    That also made a definition edit destructive, because
    ``_clear_collected_metric_data`` deletes every stored value on any material
    change and only the capped tail would come back.

    ``_composition_backfill_region`` walks the frontier down ONE bounded step per
    run instead, so the work per run stays bounded by the same constant while the
    whole series is reached eventually.
    """
    def_id, numerator_event_id, scan_config = _seed_per_user_grid(sync_session_factory, buckets=10)

    adapter = _DistinctUserAdapter()
    monkeypatch.setattr(metric_collect, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(metric_collect, "EVENT_COMPOSITION_BACKFILL_BUCKETS", 3)

    metric_collect.collect_metric_definitions.run(def_id)
    # Run 1 is capped three buckets back from the head: b06..b09 and nothing else.
    assert _stored_buckets(sync_session_factory, def_id) == {
        to_utc(_b(hour)) for hour in range(6, 10)
    }

    for hour in (10, 11):
        _append_numerator_bucket(sync_session_factory, scan_config, numerator_event_id, hour)
        metric_collect.collect_metric_definitions.run(def_id)

    # Two further runs, each taking one bounded step backwards, and the whole
    # retained history is composed -- b00 included.
    assert _stored_buckets(sync_session_factory, def_id) == {to_utc(_b(hour)) for hour in range(12)}
    # ...without any single run asking the warehouse for more than the bound.
    assert adapter.windows, "the denominator query never ran"
    assert all(window_to - window_from <= HOUR * 4 for window_from, window_to in adapter.windows)

    # Once the frontier reaches the oldest source bucket the extra pass stops:
    # the next run issues the resume query and nothing else.
    before = len(adapter.windows)
    _append_numerator_bucket(sync_session_factory, scan_config, numerator_event_id, 12)
    metric_collect.collect_metric_definitions.run(def_id)
    assert len(adapter.windows) == before + 1
