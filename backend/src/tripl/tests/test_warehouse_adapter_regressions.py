"""Regression tests for the warehouse adapter layer.

* The in-memory warehouse read only a depth-0
  ``WHERE``, so the per-metric fact path (which wraps the fact SQL one paren
  level down) dropped the fact table's own predicate and disagreed with the
  batched path; a CTE-backed fact source lost its predicate on both.
* a trailing ``ORDER BY`` / ``LIMIT`` after a top-level
  ``WHERE`` was swept into the predicate and failed the whole collection.
* the in-memory warehouse answered ``0.0`` for ``sum`` over a
  set with no non-NULL measure, where every SQL engine answers NULL.
* A contract expectation an engine declined
  (a refused regex, a REPEATED BigQuery column, a non-finite bound) left only a
  log line, so "could not check" reported as "checked and clean".
* a naive or ``date`` bucket reached a ``timestamptz`` column
  as-is and was stored in the database session's timezone.
* PostgreSQL range contracts compare in exact numeric while the
  other engines compare in float64; the comment said they agreed, and the
  number guard refused digit runs the float64 engines parse.
* the parity page claimed the dialect lint runs at collection
  and before save; it runs only in the metric preview.
"""

from __future__ import annotations

import inspect
import re
import uuid
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

# ``tripl.worker.tasks`` participates in an import cycle that only resolves when
# ``celery_app`` starts the load; entering any other way raises ImportError on a
# partially initialised module.
import tripl.worker.celery_app  # noqa: F401
from tripl.core.adapters import synthetic as synth
from tripl.core.adapters.base import (
    AggregateSpec,
    BaseAdapter,
    ColumnInfo,
    FieldContractExpectation,
)
from tripl.core.adapters.bigquery import BigQueryAdapter
from tripl.core.adapters.clickhouse import ClickHouseAdapter
from tripl.core.adapters.databricks import DatabricksAdapter
from tripl.core.adapters.databricks_sql import Params
from tripl.core.adapters.measure_validator import dialect_for_db_type
from tripl.core.adapters.postgres import _FINITE_NUMBER_RE, PostgresAdapter
from tripl.core.adapters.snowflake import SnowflakeAdapter
from tripl.core.adapters.snowflake_sql import Params as SnowflakeParams
from tripl.core.adapters.synthetic import SyntheticAdapter, SyntheticCapabilityError
from tripl.core.adapters.trino import TrinoAdapter
from tripl.core.adapters.trino_sql import LiteralParams
from tripl.core.bucketing import stored_bucket
from tripl.models.domain_enums import MetricAggregation as MA
from tripl.models.fact_table import FactTable
from tripl.worker.tasks.metrics import catalog_sync as metrics_catalog_sync
from tripl.worker.tasks.metrics import chunk_processing as metrics_chunk_processing
from tripl.worker.tasks.metrics import metric_rows as metrics_metric_rows
from tripl.worker.tasks.metrics import schema_drift as metrics_schema_drift
from tripl.worker.tasks.metrics import tasks as metrics_tasks
from tripl.worker.tasks.metrics._fact_conditions import (
    _FactCondition,
    _FactOperand,
    _resolve_fact_operand_filter,
    _resolve_fact_operand_query,
)

ANCHOR = datetime(2026, 6, 1, tzinfo=UTC)
HISTORY_DAYS = 30
SEED = 7
_REPO_ROOT = Path(__file__).resolve().parents[4]
_PARITY_DOC = _REPO_ROOT / "website" / "docs" / "develop" / "warehouse-parity.md"

_ORDERS_COLUMNS = "created_at, amount, currency, user_id, country, status"
_ORDERS_SQL = f"SELECT {_ORDERS_COLUMNS} FROM orders"
_DIALECT = dialect_for_db_type("synthetic")


def _adapter() -> SyntheticAdapter:
    return SyntheticAdapter(seed=SEED, anchor=ANCHOR, history_days=HISTORY_DAYS)


def _fact_table(sql: str) -> FactTable:
    return FactTable(
        id=uuid.uuid4(),
        name="orders",
        sql=sql,
        timestamp_column="created_at",
        columns=[
            {"name": "created_at", "type": "timestamp"},
            {"name": "amount", "type": "number"},
            {"name": "currency", "type": "string"},
            {"name": "user_id", "type": "string"},
            {"name": "country", "type": "string"},
            {"name": "status", "type": "string"},
        ],
        row_filters=[],
    )


def _completed_operand(fact_table: FactTable) -> _FactOperand:
    return _FactOperand(
        fact_table_id=fact_table.id,
        aggregation=MA.sum,
        measure_column="amount",
        distinct_column=None,
        row_filters=(),
        filter_sql=None,
        conditions=(_FactCondition(column="status", operator="eq", value="completed"),),
    )


def _daily_sums(adapter: SyntheticAdapter, query: str) -> dict[datetime, object]:
    time_from, time_to = ANCHOR - timedelta(days=10), ANCHOR
    _cols, _json, rows = adapter.get_time_bucketed_aggregate(
        query, "created_at", "1d", MA.sum, "amount", [], [], None, time_from, time_to
    )
    return {bucket: value for bucket, value in rows}


def _daily_spec_sums(
    adapter: SyntheticAdapter, query: str, filter_sql: str | None
) -> dict[datetime, object]:
    time_from, time_to = ANCHOR - timedelta(days=10), ANCHOR
    spec = AggregateSpec(key="rev", aggregation=MA.sum, column="amount", filter_sql=filter_sql)
    _columns, rows = adapter.get_time_bucketed_multi_aggregate(
        query, "created_at", "1d", [spec], time_from, time_to
    )
    return {bucket: value for bucket, value in rows if value is not None}


def _ground_truth(adapter: SyntheticAdapter, keep: object) -> dict[datetime, float]:
    """Daily ``sum(amount)`` over the orders ``keep`` selects, computed directly."""
    time_from, time_to = ANCHOR - timedelta(days=10), ANCHOR
    out: dict[datetime, float] = {}
    for row in adapter._orders:
        created = row["created_at"]
        assert isinstance(created, datetime)
        if not time_from <= created < time_to or not keep(row):  # type: ignore[operator]
            continue
        bucket = created.replace(hour=0, minute=0, second=0, microsecond=0)
        amount = row["amount"]
        if amount is not None:
            out[bucket] = out.get(bucket, 0.0) + float(amount)  # type: ignore[arg-type]
    return out


# --------------------------------------------------------------------------- #
# The fact table's own WHERE
# --------------------------------------------------------------------------- #


def test_per_metric_path_keeps_the_fact_tables_own_where() -> None:
    """The wrapped fact SQL's inner ``WHERE`` is applied, so both paths agree."""
    fact_table = _fact_table(f"{_ORDERS_SQL} WHERE currency = 'USD'")
    operand = _completed_operand(fact_table)
    per_metric_query = _resolve_fact_operand_query(fact_table, operand, dialect=_DIALECT)
    batched_filter = _resolve_fact_operand_filter(operand, fact_table=fact_table, dialect=_DIALECT)
    assert per_metric_query.startswith("SELECT * FROM (")
    assert batched_filter is not None

    adapter = _adapter()
    per_metric = _daily_sums(adapter, per_metric_query)
    batched = _daily_spec_sums(adapter, fact_table.sql, batched_filter)
    truth = _ground_truth(
        adapter, lambda row: row["currency"] == "USD" and row["status"] == "completed"
    )

    assert truth, "the fixture holds no completed USD orders in the window"
    assert set(per_metric) == set(truth) == set(batched)
    for bucket, value in truth.items():
        assert per_metric[bucket] == pytest.approx(value), bucket
        assert batched[bucket] == pytest.approx(value), bucket


def test_a_cte_backed_fact_source_keeps_its_where() -> None:
    cte = (
        "WITH completed AS (SELECT * FROM orders WHERE status = 'completed') "
        "SELECT * FROM completed"
    )
    adapter = _adapter()
    truth = _ground_truth(adapter, lambda row: row["status"] == "completed")

    unwrapped = _daily_sums(adapter, cte)
    wrapped = _daily_sums(adapter, f"SELECT * FROM ({cte}) AS _filtered WHERE country = 'US'")
    truth_us = _ground_truth(
        adapter, lambda row: row["status"] == "completed" and row["country"] == "US"
    )

    assert truth and truth_us
    assert unwrapped == pytest.approx(truth)
    assert wrapped == pytest.approx(truth_us)


def test_a_nested_where_the_scanner_cannot_read_is_refused() -> None:
    """A WHERE in some other subquery is refused, never silently dropped."""
    query = (
        "SELECT created_at, amount FROM orders "
        "JOIN (SELECT user_id FROM orders WHERE status = 'completed') AS u ON TRUE"
    )
    with pytest.raises(SyntheticCapabilityError, match="nested inside a subquery"):
        synth._where_predicates(query)
    two_ctes = (
        "WITH a AS (SELECT * FROM orders WHERE status = 'completed'), b AS (SELECT 1) "
        "SELECT * FROM a"
    )
    with pytest.raises(SyntheticCapabilityError, match="nested inside a subquery"):
        synth._where_predicates(two_ctes)


# --------------------------------------------------------------------------- #
# a trailing ORDER BY / LIMIT
# --------------------------------------------------------------------------- #


def test_trailing_order_by_and_limit_end_the_predicate() -> None:
    adapter = _adapter()
    time_from, time_to = ANCHOR - timedelta(days=2), ANCHOR

    names, rows = adapter.get_preview_rows(
        "SELECT * FROM events WHERE platform = 'ios' ORDER BY event_time",
        limit=500,
        time_column="event_time",
        time_from=time_from,
        time_to=time_to,
    )
    platform = names.index("platform")
    assert rows
    assert {row[platform] for row in rows} == {"ios"}

    names, rows = adapter.get_preview_rows(
        "SELECT * FROM events WHERE amount > 5 LIMIT 100",
        limit=500,
        time_column="event_time",
        time_from=time_from,
        time_to=time_to,
    )
    amount = names.index("amount")
    assert rows
    assert all(
        row[amount] is not None and row[amount] > 5  # type: ignore[operator]
        for row in rows
    )

    completed = _daily_sums(
        adapter, f"{_ORDERS_SQL} WHERE status = 'completed' ORDER BY created_at"
    )
    truth = _ground_truth(adapter, lambda row: row["status"] == "completed")
    assert completed == pytest.approx(truth)


def test_a_trailing_clause_that_reshapes_rows_is_refused_by_name() -> None:
    with pytest.raises(SyntheticCapabilityError, match="GROUP BY clause after its WHERE"):
        synth._where_predicates("SELECT * FROM orders WHERE status = 'x' GROUP BY country")
    # A value or a longer identifier is not a clause keyword.
    assert synth._where_predicates("SELECT * FROM orders WHERE status = 'order by'") == [
        "status = 'order by'"
    ]


# --------------------------------------------------------------------------- #
# sum over an all-NULL measure
# --------------------------------------------------------------------------- #


def test_sum_over_rows_whose_measure_is_all_null_is_a_gap() -> None:
    adapter = _adapter()
    template = dict(adapter._events[0])
    bucket = ANCHOR - timedelta(days=1)
    template.update(event_time=bucket + timedelta(hours=1), amount=None, platform="ios")
    adapter._events = [dict(template), dict(template)]
    time_from, time_to = ANCHOR - timedelta(days=2), ANCHOR

    _cols, _json, rows = adapter.get_time_bucketed_aggregate(
        "SELECT * FROM events",
        "event_time",
        "1d",
        MA.sum,
        "amount",
        [],
        [],
        None,
        time_from,
        time_to,
    )
    assert rows == [(bucket, None)]

    spec = AggregateSpec(
        key="s", aggregation=MA.sum, column="amount", filter_sql="platform = 'ios'"
    )
    _columns, spec_rows = adapter.get_time_bucketed_multi_aggregate(
        "SELECT * FROM events", "event_time", "1d", [spec], time_from, time_to
    )
    assert spec_rows == [(bucket, None)]


# --------------------------------------------------------------------------- #
# Skipped expectations are counted
# --------------------------------------------------------------------------- #


@dataclass
class _StubFieldDefinition:
    name: str
    contract_regex: str | None = None
    is_required: bool = False
    contract_required_max_null_rate: float | None = None
    enum_options: list[str] | None = None
    field_type: str = "string"
    contract_min_value: float | None = None
    contract_max_value: float | None = None
    contract_max_bad_rate: float = 0.0


@dataclass
class _StubEventType:
    field_definitions: list[_StubFieldDefinition]
    id: uuid.UUID = dataclass_field(default_factory=uuid.uuid4)


class _RecordingSession:
    def __init__(self) -> None:
        self.bind = None
        self.executed: list[object] = []

    def execute(self, statement: object) -> None:
        self.executed.append(statement)


class _Re2LikeAdapter(SyntheticAdapter):
    """The Python fallback, over fixed rows, with an RE2-like regex probe."""

    def __init__(self, rows: list[tuple[object, ...]]) -> None:  # no dataset needed
        self._rows = rows

    def get_preview_rows(  # type: ignore[override]
        self, base_query: str, limit: int = 10, **_kwargs: object
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        return ["sku", "country", "time"], self._rows

    def _probe_contract_regex(self, pattern: str) -> None:
        if "(?!" in pattern or "(?=" in pattern:
            msg = f"invalid perl operator: {pattern}"
            raise ValueError(msg)
        re.compile(pattern)


def _detect(
    adapter: object, event_type: _StubEventType
) -> metrics_schema_drift.FieldContractOutcome:
    return metrics_schema_drift._detect_field_contract_violations(
        _RecordingSession(),  # type: ignore[arg-type]
        adapter=adapter,  # type: ignore[arg-type]
        event_type=event_type,  # type: ignore[arg-type]
        base_query="SELECT sku, country, time FROM events",
        columns=[
            ColumnInfo(name="sku", type_name="String"),
            ColumnInfo(name="country", type_name="String"),
            ColumnInfo(name="time", type_name="DateTime"),
        ],
        skip_columns={"time"},
        scan_config_id=uuid.uuid4(),
        time_column="time",
        time_from=ANCHOR - timedelta(days=1),
        time_to=ANCHOR,
    )


def test_an_engine_refused_regex_is_reported_and_the_other_contract_still_runs() -> None:
    adapter = _Re2LikeAdapter([("test_a", "usa", ANCHOR), ("b", "US", ANCHOR)])
    event_type = _StubEventType(
        field_definitions=[
            _StubFieldDefinition(name="sku", contract_regex="^(?!test_)"),
            _StubFieldDefinition(name="country", contract_regex="^[A-Z]{2}$"),
        ]
    )

    outcome = _detect(adapter, event_type)

    assert outcome.violations_detected == 1
    assert outcome.checks_failed == 0
    assert outcome.expectations_skipped == 1
    # Drained: the next check does not inherit this one's skip.
    assert adapter.take_skipped_field_contracts() == []


def test_a_clean_check_reports_no_skip() -> None:
    adapter = _Re2LikeAdapter([("b", "US", ANCHOR)])
    event_type = _StubEventType(
        field_definitions=[_StubFieldDefinition(name="country", contract_regex="^[A-Z]{2}$")]
    )
    outcome = _detect(adapter, event_type)
    assert outcome.expectations_skipped == 0
    assert outcome.violations_detected == 0


def _regex(field: str = "sku", pattern: str = "^(?!test_)") -> FieldContractExpectation:
    return FieldContractExpectation(
        field_name=field, drift_type="regex_violation", threshold=0.0, regex=pattern
    )


def test_every_sql_engine_records_a_refused_regex() -> None:
    expectation = _regex()

    postgres = object.__new__(PostgresAdapter)
    postgres._allowed_columns = {"sku"}
    postgres._contract_regex_support = {expectation.regex: False}  # type: ignore[dict-item]
    assert postgres._contract_bad_condition(expectation) is None
    assert postgres.take_skipped_field_contracts() == [expectation]

    clickhouse = object.__new__(ClickHouseAdapter)
    clickhouse._allowed_columns = {"sku"}
    clickhouse._contract_regex_support = {expectation.regex: False}  # type: ignore[dict-item]
    assert clickhouse._contract_aggregates(expectation) is None
    assert clickhouse.take_skipped_field_contracts() == [expectation]

    bigquery = object.__new__(BigQueryAdapter)
    bigquery._allowed_columns = set()
    bigquery._column_types = {}
    bigquery._struct_paths = {}
    bigquery._repeated_columns = set()
    bigquery._contract_regex_support = {expectation.regex: False}  # type: ignore[dict-item]
    assert bigquery._contract_fragments(expectation, index=0) is None
    assert bigquery.take_skipped_field_contracts() == [expectation]

    databricks = object.__new__(DatabricksAdapter)
    databricks._allowed_columns = {"sku"}
    databricks._column_types = {"sku": "string"}
    databricks._struct_paths = {}
    databricks._contract_regex_support = {expectation.regex: False}  # type: ignore[dict-item]
    assert databricks._contract_bad_condition(expectation, Params()) is None
    assert databricks.take_skipped_field_contracts() == [expectation]

    snowflake = object.__new__(SnowflakeAdapter)
    snowflake._allowed_columns = {"sku"}
    snowflake._column_types = {"sku": "STRING"}
    snowflake._contract_regex_support = {expectation.regex: False}  # type: ignore[dict-item]
    assert snowflake._contract_bad_condition(expectation, SnowflakeParams()) is None
    assert snowflake.take_skipped_field_contracts() == [expectation]

    trino = object.__new__(TrinoAdapter)
    trino._allowed_columns = {"sku"}
    trino._column_types = {"sku": "varchar"}
    trino._contract_regex_support = {expectation.regex: False}  # type: ignore[dict-item]
    assert trino._contract_bad_condition(expectation, LiteralParams()) is None
    assert trino.take_skipped_field_contracts() == [expectation]


def test_bigquery_records_a_contract_on_a_repeated_column() -> None:
    expectation = FieldContractExpectation(
        field_name="labels",
        drift_type="enum_violation",
        threshold=0.0,
        enum_options=("a",),
    )
    adapter = object.__new__(BigQueryAdapter)
    adapter._allowed_columns = set()
    adapter._column_types = {}
    adapter._struct_paths = {}
    adapter._repeated_columns = {"labels"}

    assert adapter._contract_fragments(expectation, index=0) is None
    assert adapter.take_skipped_field_contracts() == [expectation]


def test_a_non_finite_bound_is_a_skip_and_an_empty_enum_is_not() -> None:
    adapter = object.__new__(PostgresAdapter)
    adapter._allowed_columns = {"amount", "status"}
    non_finite = FieldContractExpectation(
        field_name="amount",
        drift_type="range_violation",
        threshold=0.0,
        min_value=float("-inf"),
    )
    empty_enum = FieldContractExpectation(
        field_name="status", drift_type="enum_violation", threshold=0.0
    )
    assert adapter._contract_bad_condition(non_finite) is None
    assert adapter._contract_bad_condition(empty_enum) is None
    assert adapter.take_skipped_field_contracts() == [non_finite]


def test_the_skip_count_reaches_the_run_summary() -> None:
    sync_source = inspect.getsource(metrics_catalog_sync.sync_catalog)
    assert (
        sync_source.count("out.contract_expectations_skipped += contracts.expectations_skipped")
        == 2
    )
    summary_source = inspect.getsource(metrics_tasks.collect_metrics)
    assert '"contract_expectations_skipped": contract_expectations_skipped' in summary_source


# --------------------------------------------------------------------------- #
# the stored bucket instant
# --------------------------------------------------------------------------- #


def test_stored_bucket_is_an_aware_utc_instant_whatever_the_driver_decoded() -> None:
    wall = datetime(2026, 3, 2, 13, 0)
    assert stored_bucket(wall) == datetime(2026, 3, 2, 13, 0, tzinfo=UTC)
    assert stored_bucket(wall).tzinfo is UTC
    assert stored_bucket(date(2026, 3, 2)) == datetime(2026, 3, 2, tzinfo=UTC)
    tokyo = datetime(2026, 3, 2, 22, 0, tzinfo=timezone(timedelta(hours=9)))
    assert stored_bucket(tokyo) == datetime(2026, 3, 2, 13, 0, tzinfo=UTC)
    with pytest.raises(TypeError):
        stored_bucket("2026-03-02")


def test_every_bucket_writer_normalizes_column_zero() -> None:
    rows_source = inspect.getsource(metrics_metric_rows)
    chunk_source = inspect.getsource(metrics_chunk_processing)
    assert "cast(datetime, row[0])" not in rows_source
    assert "cast(datetime, row[0])" not in chunk_source
    assert rows_source.count("bucket = stored_bucket(row[0])") == 3
    assert chunk_source.count("bucket = stored_bucket(row[0])") == 1


# --------------------------------------------------------------------------- #
# the PostgreSQL range comparison domain
# --------------------------------------------------------------------------- #


def test_the_number_guard_admits_the_float64_digit_range() -> None:
    assert re.match(_FINITE_NUMBER_RE, "9" * 300)
    assert re.match(_FINITE_NUMBER_RE, "9" * 510)
    assert re.match(_FINITE_NUMBER_RE, "0." + "0" * 1073 + "5")
    assert re.match(_FINITE_NUMBER_RE, "." + "1" * 1275)
    assert re.match(_FINITE_NUMBER_RE, "9" * 511) is None
    assert re.match(_FINITE_NUMBER_RE, "1e99999") is None
    # Still only counts PostgreSQL's ARE compiles.
    counts = re.findall(r"\{(\d+),(\d+)\}", _FINITE_NUMBER_RE)
    assert all(int(high) <= 255 for _low, high in counts)


def test_the_range_divergence_is_declared_not_denied() -> None:
    source = inspect.getsource(PostgresAdapter._contract_bad_condition)
    assert "agrees with them on\n            # the verdict" not in source
    assert "ClickHouse's toFloat64OrNull and BigQuery's SAFE_CAST" in source
    contract = inspect.getdoc(BaseAdapter)
    assert contract is not None
    assert "PostgreSQL's range comparison domain" in contract
    assert "PostgreSQL range contracts compare exactly" in _PARITY_DOC.read_text()


# --------------------------------------------------------------------------- #
# where the dialect lint runs
# --------------------------------------------------------------------------- #


def test_the_parity_page_does_not_claim_a_collection_time_lint() -> None:
    text = _PARITY_DOC.read_text()
    assert "again at collection" not in text
    assert "before it is saved" not in text
    # The save was added later; collection still does not lint.
    assert "Dialect pre-flight lint (metric preview and metric save [9])" in text
