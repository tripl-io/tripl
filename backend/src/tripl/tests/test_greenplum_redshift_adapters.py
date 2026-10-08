"""The Greenplum and Redshift dialects of ``PostgresAdapter``, as generated SQL.

Greenplum's SQL is executed by the conformance gate (``TRIPL_CONF_PG_ENGINE=
greenplum``) and Redshift's by its release workflow; these pin the dialect seams
that make the two differ from PostgreSQL, without a server.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tripl.core.adapters.base import AggregateSpec, FieldContractExpectation
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.greenplum import GreenplumAdapter
from tripl.core.adapters.measure_validator import (
    SqlDialect,
    dialect_for_db_type,
    lint_dialect_sql,
    quote_sql_string_literal,
    quote_timestamp_literal,
)
from tripl.core.adapters.postgres import PostgresAdapter, epoch_bucket_expression
from tripl.core.adapters.redshift import RedshiftAdapter
from tripl.core.bucketing import EPOCH, WEEK_ORIGIN, floor_to_bucket
from tripl.core.intervals import IntervalUnit, get_interval
from tripl.core.warehouse_types import TimeKind
from tripl.models.domain_enums import MetricAggregation

_FROM = datetime(2026, 4, 1, tzinfo=UTC)
_TO = datetime(2026, 4, 8, tzinfo=UTC)
_INTERVALS = ["15m", "1h", "6h", "1d", "1w"]


class _Info:
    def __init__(self, version: int) -> None:
        self.server_version = version


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self._conn.sql.append(sql)

    def fetchall(self) -> list[tuple[object, ...]]:
        return []

    def fetchone(self) -> tuple[object, ...]:
        return self._conn.one


class _Conn:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.one: tuple[object, ...] = ("public",)
        self.info = _Info(120012)

    def cursor(self) -> _Cursor:
        return _Cursor(self)


def _adapter(cls: type[PostgresAdapter]) -> tuple[Any, list[str]]:
    adapter = object.__new__(cls)
    conn = _Conn()
    adapter._conn = conn
    adapter._allowed_columns = {"ts", "event_name", "amount", "doc"}
    adapter._type_names = {}
    return adapter, conn.sql


# --- the bucket ----------------------------------------------------------------


@pytest.mark.parametrize("cls", [GreenplumAdapter, RedshiftAdapter])
@pytest.mark.parametrize("code", _INTERVALS)
def test_neither_engine_buckets_with_date_bin(cls: type[PostgresAdapter], code: str) -> None:
    adapter, _ = _adapter(cls)
    sql = adapter._bucket_expression("ts", code)
    assert "date_bin" not in sql
    assert sql == epoch_bucket_expression('"ts"', code)


@pytest.mark.parametrize(
    ("code", "origin", "width"),
    [
        ("15m", "1970-01-01 00:00:00.000000+00:00", 900),
        ("1d", "1970-01-01 00:00:00.000000+00:00", 86400),
        # Weeks bin from the first Monday, 1970-01-05, not from the Thursday epoch.
        ("1w", "1970-01-05 00:00:00.000000+00:00", 604800),
    ],
)
def test_the_epoch_bucket_names_its_origin_and_width(code: str, origin: str, width: int) -> None:
    sql = epoch_bucket_expression('"ts"', code)
    assert f"TIMESTAMPTZ '{origin}'" in sql
    assert f"/ {width}) * {width} AS BIGINT) * INTERVAL '1 second'" in sql
    assert "floor(" in sql


@pytest.mark.parametrize("code", _INTERVALS)
def test_the_epoch_arithmetic_matches_floor_to_bucket(code: str) -> None:
    """The expression's arithmetic, done in Python, against the reference grid.

    Includes an instant before the origin: ``floor`` must take it to the bucket
    that starts before it, where truncation would round it up to the origin.
    """
    spec = get_interval(code)
    origin = WEEK_ORIGIN if spec.unit is IntervalUnit.week else EPOCH
    width = int(spec.delta.total_seconds())
    for moment in (
        datetime(2026, 4, 3, 13, 47, 12, 999999, tzinfo=UTC),
        datetime(1969, 12, 31, 23, 59, 59, tzinfo=UTC),
        datetime(1970, 1, 2, tzinfo=UTC),
    ):
        seconds = (moment - origin).total_seconds()
        bucket = origin + timedelta(seconds=(seconds // width) * width)
        assert bucket == floor_to_bucket(moment, code)


# --- conditional aggregates ----------------------------------------------------


def test_redshift_folds_every_conditional_aggregate_into_case() -> None:
    adapter, _ = _adapter(RedshiftAdapter)
    specs = [
        AggregateSpec(key="c", aggregation=MetricAggregation.count, filter_sql="amount > 0"),
        AggregateSpec(
            key="u",
            aggregation=MetricAggregation.count_distinct,
            column="event_name",
            filter_sql="amount > 0",
        ),
        AggregateSpec(
            key="s", aggregation=MetricAggregation.sum, column="amount", filter_sql="amount > 0"
        ),
    ]
    _, sql = adapter.build_time_bucketed_multi_aggregate_sql(
        "SELECT 1", "ts", "1d", specs, _FROM, _TO
    )
    assert "FILTER" not in sql
    assert 'NULLIF(count(CASE WHEN amount > 0 THEN 1 END), 0) AS "c"' in sql
    assert 'count(DISTINCT CASE WHEN amount > 0 THEN "event_name" END)' in sql
    assert 'sum(CASE WHEN amount > 0 THEN "amount" END) AS "s"' in sql


def test_greenplum_keeps_the_postgres_filter_clause() -> None:
    adapter, _ = _adapter(GreenplumAdapter)
    spec = AggregateSpec(key="c", aggregation=MetricAggregation.count, filter_sql="amount > 0")
    _, sql = adapter.build_time_bucketed_multi_aggregate_sql(
        "SELECT 1", "ts", "1d", [spec], _FROM, _TO
    )
    assert "count(*) FILTER (WHERE amount > 0)" in sql


def test_redshift_contracts_use_no_filter_clause_and_no_text_type() -> None:
    adapter, sql = _adapter(RedshiftAdapter)
    adapter.validate_field_contracts(
        "SELECT * FROM events",
        [
            FieldContractExpectation(
                field_name="amount",
                drift_type="range_violation",
                threshold=0.0,
                min_value=0.0,
                max_value=50.0,
            ),
            FieldContractExpectation(
                field_name="event_name", drift_type="required_null_violation", threshold=0.0
            ),
        ],
    )
    statement = sql[-1]
    assert "FILTER" not in statement
    assert "::text" not in statement
    assert "::varchar(65535)" in statement
    # Redshift's numeric holds 38 digits; the comparison is in float8, behind a
    # guard that admits only magnitudes float8 holds.
    assert "::double precision" in statement
    assert "::numeric" not in statement
    assert "{1,150}" in statement


# --- literals, session, catalog --------------------------------------------------


def test_redshift_doubles_the_backslash_in_a_literal() -> None:
    adapter, _ = _adapter(RedshiftAdapter)
    # A trailing backslash must not escape the closing quote.
    assert adapter._quote_string("a\\") == "'a\\\\'"
    assert adapter._quote_string("O'Reilly") == "'O''Reilly'"


def test_greenplum_keeps_the_standard_conforming_literal() -> None:
    adapter, _ = _adapter(GreenplumAdapter)
    assert adapter._quote_string("a\\") == "'a\\'"


def test_redshift_sets_only_what_it_has_after_connecting() -> None:
    adapter, _ = _adapter(RedshiftAdapter)
    assert dict(adapter._session_settings(30, "analytics,public")) == {
        "timezone": "UTC",
        "statement_timeout": "30000",
        "search_path": "analytics,public",
    }
    assert RedshiftAdapter.session_settings_at_startup is False


def test_redshift_reads_svv_columns_and_skips_its_own_schemas() -> None:
    adapter, sql = _adapter(RedshiftAdapter)
    adapter.get_schema_tables()
    assert sql[0] == "SELECT current_schema()"
    assert "FROM svv_columns" in sql[1]
    assert "(table_schema = 'public') AS is_current_schema" in sql[1]
    assert "left(table_schema, 3) <> 'pg_'" in sql[1]


def test_greenplum_hides_its_catalog_schemas() -> None:
    adapter, sql = _adapter(GreenplumAdapter)
    adapter.get_schema_tables()
    assert "'gp_toolkit'" in sql[0]
    assert "information_schema.columns" in sql[0]


def test_redshift_ranks_ties_without_a_collation() -> None:
    adapter, sql = _adapter(RedshiftAdapter)
    adapter._query_top_breakdown_values_multi("SELECT 1", "ts", ["event_name"], _FROM, _TO, 3)
    assert "COLLATE" not in sql[0]
    assert "ORDER BY _cnt DESC, _breakdown_value) AS rn" in sql[0]


def test_redshift_names_its_super_type() -> None:
    adapter, _ = _adapter(RedshiftAdapter)
    assert adapter._type_name(4000) == "super"


# --- JSON is not supported on Redshift ------------------------------------------


def test_redshift_refuses_json_paths_and_properties() -> None:
    adapter, _ = _adapter(RedshiftAdapter)
    with pytest.raises(WarehouseCapabilityError, match="do not support JSON"):
        adapter._json_paths_expression("doc")
    with pytest.raises(WarehouseCapabilityError, match="do not support JSON"):
        adapter._field_operand("doc.user.id")
    assert adapter.get_json_path_samples("SELECT 1", ["doc"]) == {"doc": {}}


# --- the version floor -----------------------------------------------------------


def test_greenplum_6_is_accepted_and_anything_older_is_refused() -> None:
    adapter, _ = _adapter(GreenplumAdapter)
    adapter._conn.info = _Info(90426)
    adapter._conn.one = (1,)
    assert adapter.test_connection() is True

    adapter._conn.info = _Info(80323)
    with pytest.raises(WarehouseCapabilityError, match=r"Greenplum server \(PostgreSQL 8\.323\)"):
        adapter.test_connection()


def test_redshift_has_no_version_floor() -> None:
    adapter, _ = _adapter(RedshiftAdapter)
    adapter._conn.info = _Info(80002)
    adapter._conn.one = (1,)
    assert adapter.test_connection() is True


# --- user SQL fragments -----------------------------------------------------------


def test_both_db_types_compile_their_own_dialect() -> None:
    assert dialect_for_db_type("greenplum") is SqlDialect.greenplum
    assert dialect_for_db_type("redshift") is SqlDialect.redshift


@pytest.mark.parametrize("dialect", [SqlDialect.greenplum, SqlDialect.redshift])
def test_date_bin_is_refused_with_a_date_trunc_hint(dialect: SqlDialect) -> None:
    message = lint_dialect_sql(
        "SELECT date_bin(INTERVAL '1 day', ts, TIMESTAMPTZ '1970-01-01') FROM t", dialect
    )
    assert message is not None
    assert "date_trunc('day', <time column>)" in message


def test_only_redshift_refuses_an_aggregate_filter_clause() -> None:
    sql = "SELECT count(*) FILTER (WHERE amount > 0) FROM t"
    message = lint_dialect_sql(sql, SqlDialect.redshift)
    assert message is not None
    assert "FILTER" in message
    assert lint_dialect_sql(sql, SqlDialect.greenplum) is None


def test_literals_follow_each_engine() -> None:
    assert quote_sql_string_literal("a\\", SqlDialect.greenplum) == "'a\\'"
    assert quote_sql_string_literal("a\\", SqlDialect.redshift) == "'a\\\\'"
    moment = datetime(2026, 4, 1, tzinfo=UTC)
    for dialect in (SqlDialect.greenplum, SqlDialect.redshift):
        assert quote_timestamp_literal(moment, dialect, kind=TimeKind.timestamp) == (
            "TIMESTAMPTZ '2026-04-01 00:00:00.000000+00:00'"
        )
