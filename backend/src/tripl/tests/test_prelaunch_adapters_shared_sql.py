"""The SQL the warehouse adapters share, written once: the dialect base, the deadline, the helpers.

Trino (and Athena), Snowflake and Databricks used to carry a copy each of
every statement builder, and the copies had drifted: the Trino copy of the
statement runner never cancelled anything, so the 30-second cap on a schema
browse was not enforced. The statements now live once on ``DialectSqlAdapter``,
the deadline once in ``deadline.StatementDeadline``, and the identifier
grammar, log cap and result decoders once in ``sql_common``. These tests pin
that structure and the behaviour that came with it. The engines' own SQL is
pinned by their adapter tests and by ``test_warehouse_engine_parity.py``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tripl.core.adapters import measure_validator, sql_common, synthetic
from tripl.core.adapters.athena import AthenaAdapter
from tripl.core.adapters.base import BaseAdapter
from tripl.core.adapters.bigquery import BigQueryAdapter
from tripl.core.adapters.clickhouse import ClickHouseAdapter
from tripl.core.adapters.databricks import DatabricksAdapter
from tripl.core.adapters.deadline import StatementDeadline
from tripl.core.adapters.dialect_sql_adapter import DialectSqlAdapter
from tripl.core.adapters.postgres import PostgresAdapter
from tripl.core.adapters.snowflake import SnowflakeAdapter
from tripl.core.adapters.snowflake_expr import SnowflakeExpressions
from tripl.core.adapters.sql_common import (
    as_utc_bucket,
    count_cell,
    decode_array_value,
    decode_json_list,
    json_path_parts,
    truncate_sql,
    validate_identifier_column,
)
from tripl.core.adapters.synthetic import SyntheticAdapter
from tripl.core.adapters.trino import TrinoAdapter
from tripl.core.adapters.trino_expr import TrinoExpressions
from tripl.models.domain_enums import MetricAggregation

_FROM = datetime(2026, 4, 1, tzinfo=UTC)
_TO = datetime(2026, 4, 2, tzinfo=UTC)
_BASE = "SELECT ts, country, amount FROM events"


class CancelledError(Exception):
    """What the Trino client raises for its own cancel: no ``error_name`` to recognise."""


class StatementCursor:
    """A DB-API cursor whose statement runs until it is cancelled, or answers at once.

    ``query_id`` stays empty until the submission returns (``submit_delay``),
    the way the Trino client and pyathena have nothing to cancel before then.
    """

    def __init__(
        self,
        *,
        hang: bool = True,
        submit_delay: float = 0.0,
        quiet_cancel: bool = False,
        rows: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.query_id: str | None = None
        self.description: list[tuple[object, ...]] = []
        self.statements: list[str] = []
        self.cancelled: list[str | None] = []
        self.closed = False
        self._hang = hang
        self._submit_delay = submit_delay
        self._quiet_cancel = quiet_cancel
        self._rows = rows or []
        self._stopped = threading.Event()

    def execute(self, sql: str, parameters: object = None) -> None:
        self.statements.append(sql)
        if self._submit_delay:
            threading.Event().wait(self._submit_delay)
        self.query_id = "q1"
        if not self._hang:
            return
        if not self._stopped.wait(5):
            raise AssertionError("the deadline never cancelled the statement")
        if not self._quiet_cancel:
            raise CancelledError("Query has been cancelled")

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)

    def cancel(self) -> None:
        self.cancelled.append(self.query_id)
        self._stopped.set()

    def close(self) -> None:
        self.closed = True


class OneCursorConnection:
    def __init__(self, cursor: StatementCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> StatementCursor:
        return self._cursor

    def close(self) -> None:
        return None


def _trino(cursor: StatementCursor, *, timeout: float | None) -> TrinoAdapter:
    adapter = object.__new__(TrinoAdapter)
    adapter._conn = OneCursorConnection(cursor)
    adapter._timeout_seconds = timeout
    adapter._catalog = "hive"
    adapter._schema = "web"
    adapter._column_types = {"ts": "timestamp(3)", "country": "varchar", "amount": "double"}
    adapter._allowed_columns = set(adapter._column_types)
    return adapter


# --------------------------------------------------------------------------- #
# Trino enforces the deadline from the client (the schema browse cap included)
# --------------------------------------------------------------------------- #


def test_trino_cancels_a_statement_at_a_cap_below_the_source_timeout() -> None:
    cursor = StatementCursor()
    adapter = _trino(cursor, timeout=300.0)
    with pytest.raises(TimeoutError, match=r"^Trino: query exceeded its 0\.05s cap"):
        adapter._run("SELECT 1", timeout_cap=0.05)
    assert cursor.cancelled == ["q1"]
    assert cursor.closed


def test_the_trino_schema_browse_is_cut_off_at_its_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tripl.core.adapters.trino.SCHEMA_QUERY_TIMEOUT_SECONDS", 0.05)
    cursor = StatementCursor()
    adapter = _trino(cursor, timeout=300.0)
    with pytest.raises(TimeoutError, match="cap and was cancelled") as raised:
        adapter.get_schema_tables()
    # The message names what to change: the cap does not move with the source's timeout.
    assert "schema allowlist" in str(raised.value)
    assert "information_schema.columns" in cursor.statements[0]
    assert cursor.cancelled == ["q1"]


def test_trino_reports_the_source_timeout_when_that_is_the_deadline() -> None:
    adapter = _trino(StatementCursor(), timeout=0.05)
    with pytest.raises(TimeoutError, match=r"exceeded the 0\.05s timeout configured for this data"):
        adapter._run("SELECT 1")


def test_a_deadline_before_the_query_has_an_id_cancels_once_it_has_one() -> None:
    cursor = StatementCursor(submit_delay=0.3)
    adapter = _trino(cursor, timeout=0.05)
    with pytest.raises(TimeoutError):
        adapter._run("SELECT 1")
    # Cancelled by its id, after the submission returned: a cancel before then
    # would have done nothing and left the statement running.
    assert cursor.cancelled == ["q1"]


def test_a_cancel_that_ends_the_read_without_an_error_is_still_a_timeout() -> None:
    cursor = StatementCursor(quiet_cancel=True, rows=[("partial",)])
    adapter = _trino(cursor, timeout=0.05)
    with pytest.raises(TimeoutError, match="0.05s timeout"):
        adapter._run("SELECT 1")


def test_a_statement_that_ends_in_time_is_never_cancelled() -> None:
    cursor = StatementCursor(hang=False, rows=[(1,)])
    adapter = _trino(cursor, timeout=0.1)
    assert adapter._run("SELECT 1 AS ok") == ([], [(1,)])
    threading.Event().wait(0.3)
    assert cursor.cancelled == []
    assert cursor.closed


def test_trino_describe_runs_under_the_same_deadline() -> None:
    cursor = StatementCursor()
    adapter = _trino(cursor, timeout=0.05)
    with pytest.raises(TimeoutError):
        adapter.get_columns(_BASE)
    assert cursor.cancelled == ["q1"]


# --------------------------------------------------------------------------- #
# StatementDeadline
# --------------------------------------------------------------------------- #


class _IdCursor:
    def __init__(self, *, fail: bool = False) -> None:
        self.query_id = "q1"
        self.cancelled = threading.Event()
        self._fail = fail

    def cancel(self) -> None:
        self.cancelled.set()
        if self._fail:
            raise RuntimeError("network down")


def test_a_deadline_cancels_the_statement_and_records_that_it_fired() -> None:
    cursor = _IdCursor()
    guard = StatementDeadline(cursor, 0.01, engine="Test")
    assert cursor.cancelled.wait(2)
    assert guard.fired


def test_a_disarmed_deadline_never_fires() -> None:
    cursor = _IdCursor()
    guard = StatementDeadline(cursor, 0.05, engine="Test")
    guard.disarm()
    assert not cursor.cancelled.wait(0.2)
    assert not guard.fired


def test_no_deadline_arms_nothing() -> None:
    cursor = _IdCursor()
    guard = StatementDeadline(cursor, None, engine="Test")
    assert not cursor.cancelled.wait(0.1)
    assert not guard.fired


def test_a_failed_cancel_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    cursor = _IdCursor(fail=True)

    def logged() -> bool:
        return any("Test: could not cancel" in r.getMessage() for r in caplog.records)

    with caplog.at_level(logging.WARNING, logger="tripl.core.adapters.deadline"):
        guard = StatementDeadline(cursor, 0.01, engine="Test")
        assert cursor.cancelled.wait(2)
        # The warning is written on the timer thread, just after the cancel raised.
        for _ in range(40):
            if logged():
                break
            threading.Event().wait(0.05)
    assert guard.fired
    assert logged()


# --------------------------------------------------------------------------- #
# one dialect base, one copy of each statement
# --------------------------------------------------------------------------- #

#: Statements and plumbing written once on ``DialectSqlAdapter``.
_SHARED_ON_DIALECT_BASE = (
    "get_preview_rows",
    "get_full_breakdown",
    "get_time_bucketed_counts",
    "get_time_bucketed_aggregate",
    "get_time_bucketed_aggregate_breakdown",
    "build_time_bucketed_multi_aggregate_sql",
    "get_time_bucketed_multi_aggregate",
    "get_time_bucketed_multi_aggregate_breakdown",
    "get_time_bucketed_breakdown_counts_multi",
    "_query_top_breakdown_values_multi",
    "validate_field_contracts",
    "json_string_source",
    "test_connection",
    "close",
    "_cursor_under_deadline",
    "_timeout_error",
    "_decode_rows",
)

#: Written once on ``BaseAdapter`` for every adapter.
_SHARED_ON_BASE = (
    "get_time_bucketed_breakdown_counts",
    "_query_deadline",
    "_validate_column",
    "_aggregate_value_sql",
)

_DIALECT_ENGINE_CLASSES = (
    TrinoExpressions,
    TrinoAdapter,
    AthenaAdapter,
    SnowflakeExpressions,
    SnowflakeAdapter,
    DatabricksAdapter,
)


@pytest.mark.parametrize("cls", _DIALECT_ENGINE_CLASSES, ids=lambda cls: cls.__name__)
def test_no_engine_carries_its_own_copy_of_a_shared_statement(cls: type) -> None:
    assert issubclass(cls, DialectSqlAdapter)
    copies = [name for name in (*_SHARED_ON_DIALECT_BASE, *_SHARED_ON_BASE) if name in vars(cls)]
    assert copies == []


@pytest.mark.parametrize(
    "cls",
    [DialectSqlAdapter, PostgresAdapter, BigQueryAdapter, ClickHouseAdapter],
    ids=lambda cls: cls.__name__,
)
def test_the_base_helpers_have_one_copy(cls: type) -> None:
    assert [name for name in _SHARED_ON_BASE if name in vars(cls)] == []


def test_a_dialect_must_spell_every_hook() -> None:
    class Partial(DialectSqlAdapter):
        engine_label = "Partial"

    hooks = {
        "_run",
        "_new_params",
        "_quote_ident",
        "_time_literal",
        "_bucket_expression",
        "_string_value_expression",
        "_field_value_expression",
        "_property_value_expression",
        "_json_path_expression",
        "_json_paths_expression",
        "_json_object_or_null",
        "_cast_text",
        "_contract_text",
        "_regex_mismatch",
        "_try_double",
        "_probe_contract_regex",
    }
    assert hooks <= Partial.__abstractmethods__


@pytest.mark.parametrize(
    ("cls", "label"),
    [
        (TrinoAdapter, "Trino"),
        (AthenaAdapter, "Athena"),
        (SnowflakeAdapter, "Snowflake"),
        (DatabricksAdapter, "Databricks"),
    ],
)
def test_every_message_names_its_own_engine(cls: type[DialectSqlAdapter], label: str) -> None:
    adapter = object.__new__(cls)
    adapter._timeout_seconds = 10.0
    if isinstance(adapter, DatabricksAdapter):
        adapter._struct_paths = {}
    assert str(adapter._timeout_error(10.0, None)).startswith(
        f"{label}: query exceeded the 10s timeout configured for this data source"
    )
    assert str(adapter._timeout_error(5.0, None)).startswith(f"{label}: query exceeded its 5s cap")
    with pytest.raises(ValueError, match=f"^{label}: could not decode a grouped key list"):
        adapter._key_list_decoder("props")("{not json")


def test_the_single_breakdown_is_the_multi_breakdown_without_its_column_cell() -> None:
    bucket = datetime(2026, 4, 1, 6)
    cursor = StatementCursor(hang=False, rows=[(bucket, "country", "US", 0, "US", 3)])
    adapter = _trino(cursor, timeout=None)
    names, json_value_names, rows = adapter.get_time_bucketed_breakdown_counts(
        _BASE, "ts", "1h", "country", ["country"], [], None, _FROM, _TO
    )
    assert (names, json_value_names) == (["country"], [])
    assert rows == [(bucket.replace(tzinfo=UTC), "US", 0, "US", 3)]
    assert "GROUP BY GROUPING SETS" in cursor.statements[0]


@pytest.mark.parametrize(
    ("cls", "quoted"),
    [
        (PostgresAdapter, '"amount"'),
        (BigQueryAdapter, "`amount`"),
        (ClickHouseAdapter, "`amount`"),
        (TrinoAdapter, '"amount"'),
        (SnowflakeAdapter, '"amount"'),
        (DatabricksAdapter, "`amount`"),
    ],
    ids=lambda value: value.__name__ if isinstance(value, type) else value,
)
def test_one_aggregate_builder_quotes_the_measure_per_dialect(
    cls: type[BaseAdapter], quoted: str
) -> None:
    adapter = object.__new__(cls)
    adapter._allowed_columns = {"amount"}
    assert adapter._aggregate_value_sql(MetricAggregation.sum, "amount") == f"sum({quoted})"
    assert adapter._aggregate_value_sql(MetricAggregation.count, None) == "count(*)"
    with pytest.raises(ValueError, match="not found in query result"):
        adapter._aggregate_value_sql(MetricAggregation.max, "price")


def test_the_synthetic_adapter_builds_no_sql_to_quote() -> None:
    adapter = object.__new__(SyntheticAdapter)
    with pytest.raises(NotImplementedError):
        adapter._quote_ident("amount")


def test_databricks_sends_the_shared_shape_for_a_breakdown_without_keys() -> None:
    cursor = StatementCursor(hang=False)
    adapter = object.__new__(DatabricksAdapter)
    adapter._conn = OneCursorConnection(cursor)
    adapter._column_types = {"ts": "timestamp", "props": "variant"}
    adapter._allowed_columns = set(adapter._column_types)
    adapter._struct_paths = {}
    adapter.get_full_breakdown(_BASE, [], [])
    adapter.get_full_breakdown(_BASE, [], ["props"])
    # One total row needs no GROUP BY at all, on every engine.
    assert "GROUP BY" not in cursor.statements[0]
    # The nested expressions are computed once over the base query's own columns.
    assert "FROM (SELECT _src.*, to_json(" in cursor.statements[1]


# --------------------------------------------------------------------------- #
# sql_common: the helpers every adapter shares
# --------------------------------------------------------------------------- #


def test_every_adapter_holds_identifiers_to_one_grammar() -> None:
    assert measure_validator.IDENTIFIER_RE is sql_common.IDENTIFIER_RE
    assert synthetic.IDENTIFIER_RE is sql_common.IDENTIFIER_RE
    for adapter_cls in (PostgresAdapter, BigQueryAdapter, ClickHouseAdapter, TrinoAdapter):
        assert adapter_cls._validate_column is BaseAdapter._validate_column


def test_validate_identifier_column() -> None:
    assert validate_identifier_column("events.country", set()) == "events.country"
    assert validate_identifier_column("country", {"country"}) == "country"
    with pytest.raises(ValueError, match="Invalid column name"):
        validate_identifier_column("country; DROP", set())
    with pytest.raises(ValueError, match="not found in query result"):
        validate_identifier_column("city", {"country"})


def test_json_path_parts_hold_every_segment_to_the_grammar() -> None:
    assert json_path_parts("device.os") == ["device", "os"]
    for path in ("", "a'b", "a.b]", "a..'"):
        with pytest.raises(ValueError, match="Unsupported JSON path"):
            json_path_parts(path)


def test_truncate_sql_caps_what_reaches_a_log_line() -> None:
    assert truncate_sql("SELECT 1") == "SELECT 1"
    long = "SELECT " + "x" * 500
    assert truncate_sql(long) == long[:300] + "..."


def test_buckets_read_back_as_aware_utc() -> None:
    assert as_utc_bucket(datetime(2026, 4, 1, 6)) == datetime(2026, 4, 1, 6, tzinfo=UTC)
    plus_two = timezone(timedelta(hours=2))
    assert as_utc_bucket(datetime(2026, 4, 1, 8, tzinfo=plus_two)) == datetime(
        2026, 4, 1, 6, tzinfo=UTC
    )
    assert as_utc_bucket("2026-04-01 06:00:00.000") == datetime(2026, 4, 1, 6, tzinfo=UTC)
    assert as_utc_bucket(date(2026, 4, 1)) == datetime(2026, 4, 1, tzinfo=UTC)
    assert as_utc_bucket("not a time") == "not a time"
    assert as_utc_bucket(None) is None


def test_count_cells() -> None:
    assert count_cell(None, "Test") == 0
    assert count_cell(Decimal(7), "Test") == 7
    assert count_cell("7", "Test") == 7
    with pytest.raises(ValueError, match="^Test: expected a count"):
        count_cell([1], "Test")


def test_grouped_key_lists_decode_to_lists() -> None:
    assert decode_json_list('["a", "b"]', "Test") == ["a", "b"]
    assert decode_json_list(("a",), "Test") == ["a"]
    assert decode_json_list(["a"], "Test") == ["a"]
    assert decode_json_list(None, "Test") is None
    with pytest.raises(ValueError, match="^Test: could not decode"):
        decode_json_list("[", "Test")
    with pytest.raises(ValueError, match="^Test: a grouped key list decoded to dict"):
        decode_json_list("{}", "Test")
    with pytest.raises(ValueError, match="^Test: expected a JSON array string"):
        decode_json_list(3, "Test")


def test_array_values_decode_to_lists() -> None:
    assert decode_array_value(("a", "b")) == ["a", "b"]
    assert decode_array_value('["a"]') == ["a"]
    assert decode_array_value("plain text") == "plain text"
    assert decode_array_value('{"a": 1}') == '{"a": 1}'
    assert decode_array_value(5) == 5


class _ClickHouseResult:
    def __init__(self) -> None:
        self.result_rows: list[tuple[object, ...]] = []


class _ClickHouseClient:
    def query(self, sql: str) -> _ClickHouseResult:
        return _ClickHouseResult()


def _clickhouse() -> ClickHouseAdapter:
    adapter = object.__new__(ClickHouseAdapter)
    adapter._client = _ClickHouseClient()
    return adapter


def _bigquery() -> BigQueryAdapter:
    adapter = object.__new__(BigQueryAdapter)
    adapter._query_rows = lambda sql, **kwargs: ([], [])
    adapter._column_types = {"payload": "STRING"}
    return adapter


@pytest.mark.parametrize(
    ("build", "logger_name"),
    [
        (_clickhouse, "tripl.core.adapters.clickhouse"),
        (_bigquery, "tripl.core.adapters.bigquery"),
    ],
    ids=["clickhouse", "bigquery"],
)
def test_breakdown_statements_reach_debug_only_and_truncated(
    build: Callable[[], BaseAdapter], logger_name: str, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = build()
    base_query = "SELECT payload FROM t WHERE secret = '" + "s" * 500 + "'"
    with caplog.at_level(logging.DEBUG, logger=logger_name):
        adapter.get_full_breakdown(base_query, [], [])
    statements = [r for r in caplog.records if "breakdown query" in r.getMessage()]
    assert [r.levelno for r in statements] == [logging.DEBUG]
    assert statements[0].getMessage().endswith("...")
    assert all("s" * 300 not in r.getMessage() for r in caplog.records if r.levelno >= logging.INFO)
