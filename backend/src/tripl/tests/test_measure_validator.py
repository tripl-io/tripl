from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tripl.core.adapters.measure_validator import (
    SqlDialect,
    build_aggregate_sql,
    coerce_aggregation,
    dialect_for_db_type,
    lint_dialect_sql,
    parse_utc_timestamp,
    quote_identifier,
    quote_sql_literal,
    quote_sql_string_literal,
    quote_timestamp_literal,
    requires_measure,
    time_kind_of,
    validate_identifier,
    validate_measure_column,
    validate_select_sql,
    validate_select_sql_safety,
    validate_sql_fragment,
)
from tripl.core.warehouse_types import TimeKind
from tripl.models.domain_enums import MetricAggregation

_ALLOWED = {"time", "event_name", "amount", "user_id"}


# --- validate_measure_column ------------------------------------------------


def test_validate_measure_column_accepts_allowlisted() -> None:
    assert validate_measure_column("amount", _ALLOWED) == "amount"
    assert validate_measure_column("user_id", _ALLOWED) == "user_id"


def test_validate_measure_column_rejects_unknown_identifier() -> None:
    with pytest.raises(ValueError, match="not found in query result"):
        validate_measure_column("revenue", _ALLOWED)


def test_validate_measure_column_skips_membership_when_allowlist_empty() -> None:
    # Mirrors the adapters: before get_columns runs, only the identifier shape
    # is enforced.
    assert validate_measure_column("amount", set()) == "amount"


@pytest.mark.parametrize(
    "bad",
    [
        "amount; DROP TABLE users",
        "amount, other",
        "amount)",
        "count(*)",
        "amount'",
        "amount--",
        "amount OR 1=1",
        "DROP",
        "1amount",
        "",
        "amount UNION SELECT password",
        "amount\nDROP",
        "amount/*x*/",
    ],
)
def test_validate_measure_column_rejects_injection(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_measure_column(bad, _ALLOWED)


# --- coerce_aggregation -----------------------------------------------------


@pytest.mark.parametrize(
    "agg",
    ["count", "sum", "avg", "min", "max", "count_distinct"],
)
def test_coerce_aggregation_accepts_dsl(agg: str) -> None:
    assert coerce_aggregation(agg) is MetricAggregation(agg)


def test_coerce_aggregation_accepts_enum_member() -> None:
    assert coerce_aggregation(MetricAggregation.sum) is MetricAggregation.sum


@pytest.mark.parametrize("bad", ["median", "stddev", "first", "drop", "", "SUM()"])
def test_coerce_aggregation_rejects_unknown(bad: str) -> None:
    with pytest.raises(ValueError, match="Unsupported aggregation function"):
        coerce_aggregation(bad)


# --- build_aggregate_sql ----------------------------------------------------


def test_build_aggregate_sql_count_ignores_measure() -> None:
    assert build_aggregate_sql(MetricAggregation.count) == "count(*)"
    assert build_aggregate_sql(MetricAggregation.count, "`amount`") == "count(*)"


@pytest.mark.parametrize(
    ("agg", "expected"),
    [
        (MetricAggregation.sum, "sum(`amount`)"),
        (MetricAggregation.avg, "avg(`amount`)"),
        (MetricAggregation.min, "min(`amount`)"),
        (MetricAggregation.max, "max(`amount`)"),
        (MetricAggregation.count_distinct, "count(DISTINCT `amount`)"),
    ],
)
def test_build_aggregate_sql_with_measure(agg: MetricAggregation, expected: str) -> None:
    assert build_aggregate_sql(agg, "`amount`") == expected


@pytest.mark.parametrize(
    "agg",
    [
        MetricAggregation.sum,
        MetricAggregation.avg,
        MetricAggregation.min,
        MetricAggregation.max,
        MetricAggregation.count_distinct,
    ],
)
def test_build_aggregate_sql_requires_measure(agg: MetricAggregation) -> None:
    with pytest.raises(ValueError, match="requires a measure column"):
        build_aggregate_sql(agg, None)
    with pytest.raises(ValueError, match="requires a measure column"):
        build_aggregate_sql(agg, "")


def test_build_aggregate_sql_rejects_disallowed_function() -> None:
    with pytest.raises(ValueError, match="Unsupported aggregation function"):
        build_aggregate_sql("median", "`amount`")


def test_requires_measure() -> None:
    assert requires_measure(MetricAggregation.count) is False
    assert requires_measure(MetricAggregation.sum) is True
    assert requires_measure("count_distinct") is True


# --- validate_select_sql ----------------------------------------------------


def test_validate_select_sql_accepts_clean_select() -> None:
    sql = "SELECT count(*) AS value, ts AS time FROM events GROUP BY ts"
    assert validate_select_sql(sql, value_column="value", time_column="time") == sql


def test_validate_select_sql_strips_trailing_semicolon() -> None:
    sql = "SELECT sum(amount) AS value, ts AS time FROM events GROUP BY ts ;"
    cleaned = validate_select_sql(sql, value_column="value", time_column="time")
    assert not cleaned.endswith(";")
    assert cleaned == "SELECT sum(amount) AS value, ts AS time FROM events GROUP BY ts"


def test_validate_select_sql_accepts_value_and_time_without_from() -> None:
    sql = "SELECT 1 AS value, current_timestamp() AS time"
    assert validate_select_sql(sql, value_column="value", time_column="time") == sql


def test_validate_select_sql_accepts_top_level_with_cte() -> None:
    sql = (
        "WITH x AS (SELECT 1 AS amount, current_timestamp() AS ts) "
        "SELECT amount AS value, ts AS time FROM x"
    )
    assert validate_select_sql(sql, value_column="value", time_column="time") == sql


def test_validate_select_sql_accepts_clickhouse_with_aliases_and_ctes() -> None:
    sql = (
        "WITH today() - 30 AS start_time, "
        "fv AS (SELECT device_id, min(time) AS first_visit_time FROM events GROUP BY device_id) "
        "SELECT count(*) AS value, first_visit_time AS time FROM fv "
        "WHERE first_visit_time >= start_time"
    )
    assert validate_select_sql(sql, value_column="value", time_column="time") == sql


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO events VALUES (1)",
        "UPDATE events SET value = 1",
        "DELETE FROM events",
        "DROP TABLE events",
        "ALTER TABLE events ADD COLUMN x INT",
        "CREATE TABLE t AS SELECT 1 AS value, ts AS time FROM events",
        "TRUNCATE TABLE events",
        "GRANT SELECT ON events TO bob",
        "COPY events TO '/tmp/x'",
        "SELECT value, time INTO dump FROM events",
    ],
)
def test_validate_select_sql_rejects_ddl_dml(sql: str) -> None:
    with pytest.raises(ValueError):
        validate_select_sql(sql, value_column="value", time_column="time")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT value, time FROM events; DROP TABLE events",
        "SELECT value, time FROM events; SELECT 1",
        "SELECT value, time FROM events ; DELETE FROM events",
    ],
)
def test_validate_select_sql_rejects_multi_statement(sql: str) -> None:
    with pytest.raises(ValueError, match="single statement|read-only|disallowed keyword"):
        validate_select_sql(sql, value_column="value", time_column="time")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT value, time FROM events -- comment",
        "SELECT value, time FROM events /* x */",
        "SELECT value, time FROM events # mysql comment",
        "SELECT value, time FROM events WHERE x = 1 ;-- DROP TABLE events",
    ],
)
def test_validate_select_sql_rejects_comments(sql: str) -> None:
    with pytest.raises(ValueError, match="comment"):
        validate_select_sql(sql, value_column="value", time_column="time")


def test_validate_select_sql_rejects_non_select_leading() -> None:
    with pytest.raises(ValueError, match="read-only SELECT"):
        validate_select_sql(
            "EXPLAIN SELECT value, time FROM events",
            value_column="value",
            time_column="time",
        )


def test_validate_select_sql_requires_value_and_time_projection() -> None:
    with pytest.raises(ValueError, match="must project the value column"):
        validate_select_sql(
            "SELECT other AS something FROM events",
            value_column="value",
            time_column="time",
        )
    with pytest.raises(ValueError, match="must project the time column"):
        validate_select_sql(
            "SELECT count(*) AS value FROM events",
            value_column="value",
            time_column="time",
        )


def test_validate_select_sql_checks_final_select_projection_after_cte() -> None:
    with pytest.raises(ValueError, match="must project the time column"):
        validate_select_sql(
            "WITH x AS (SELECT value, time FROM events) SELECT count(*) AS value FROM x",
            value_column="value",
            time_column="time",
        )


def test_validate_select_sql_rejects_empty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        validate_select_sql("   ", value_column="value", time_column="time")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT value, time FROM events UNION SELECT password, ts FROM secrets",
        "SELECT value, time FROM events UNION ALL SELECT x, y FROM other",
    ],
)
def test_validate_select_sql_rejects_union(sql: str) -> None:
    with pytest.raises(ValueError, match="disallowed keyword: UNION"):
        validate_select_sql(sql, value_column="value", time_column="time")


@pytest.mark.parametrize(
    "sql",
    [
        # FROM inside function arguments must NOT be mistaken for the FROM clause.
        "SELECT EXTRACT(EPOCH FROM ts) AS value, ts AS time FROM events",
        "SELECT SUBSTRING(label FROM 1) AS value, ts AS time FROM events",
    ],
)
def test_validate_select_sql_accepts_from_inside_function_args(sql: str) -> None:
    assert validate_select_sql(sql, value_column="value", time_column="time") == sql


# --- validate_identifier ----------------------------------------------------


@pytest.mark.parametrize("name", ["amount", "user_id", "_private", "schema.table"])
def test_validate_identifier_accepts_good(name: str) -> None:
    assert validate_identifier(name) == name


@pytest.mark.parametrize("bad", ["a; DROP", 'a"b', "a b", "1amount", "", "a()", "a-b"])
def test_validate_identifier_rejects_injection(bad: str) -> None:
    with pytest.raises(ValueError, match="Invalid identifier"):
        validate_identifier(bad)


# --- validate_sql_fragment --------------------------------------------------


def test_validate_sql_fragment_accepts_realistic_filter() -> None:
    fragment = "status IN ('a','b') AND created_at > '2026-01-01'"
    assert validate_sql_fragment(fragment) == fragment


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        ("1=1 UNION SELECT password", "disallowed keyword: UNION"),
        ("status = 'x'; DROP TABLE users", "';'"),
        ("status = 'x' -- comment", "comment"),
        ("status = 'x' /* injected", "comment"),
    ],
)
def test_validate_sql_fragment_rejects_injection(bad: str, match: str) -> None:
    """The gate's own cases. The adversarial ones live next to the masker.

    The gate masks closed literals before it looks for
    keywords, so the interesting rejections are the ones that probe the MASKER —
    an unterminated literal, an empty literal, a quoted identifier — and they are
    parametrized alongside the accept cases they have to stay distinct from, in
    ``test_postgres_adapter_and_sql_gate.py``. Do not "simplify" this list on the
    assumption that it is all the coverage there is.
    """
    with pytest.raises(ValueError, match=match):
        validate_sql_fragment(bad)


# --- quote_sql_literal -------------------------------------------------------


@pytest.mark.parametrize("dialect", list(SqlDialect))
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("trial", "'trial'"),
        ("3", "3"),
        ("-2.5", "-2.5"),
        ("true", "TRUE"),
        (False, "FALSE"),
        (4, "4"),
        (1.25, "1.25"),
    ],
)
def test_quote_sql_literal_renders_safe_literals(
    value: str | int | float | bool, expected: str, dialect: SqlDialect
) -> None:
    """Numeric / boolean / quote-free literals are spelled the same everywhere."""
    assert quote_sql_literal(value, dialect) == expected


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        # PostgreSQL: DOUBLE the quote. A backslash is a literal backslash under
        # standard_conforming_strings, so the backslash form is an unterminated string.
        (SqlDialect.postgres, "'O''Reilly'"),
        # BigQuery: NO '' escape exists. 'O''Reilly' is read as two adjacent string
        # literals ("concatenated string literals must be separated by whitespace")
        # -- verified against ZetaSQL. GoogleSQL escapes with a backslash.
        (SqlDialect.bigquery, "'O\\'Reilly'"),
        # Databricks: Spark reads backslash escapes and joins adjacent literals,
        # so 'O''Reilly' would silently become OReilly. Backslash form, as BigQuery.
        (SqlDialect.databricks, "'O\\'Reilly'"),
        # ClickHouse accepts both; it gets the backslash form its adapter already uses.
        (SqlDialect.clickhouse, "'O\\'Reilly'"),
    ],
)
def test_quote_sql_literal_escapes_apostrophes_per_dialect(
    dialect: SqlDialect, expected: str
) -> None:
    assert quote_sql_literal("O'Reilly", dialect) == expected


@pytest.mark.parametrize("dialect", list(SqlDialect))
def test_quote_sql_literal_keeps_injection_inside_the_string(dialect: SqlDialect) -> None:
    """A value can only ever add PAIRED quotes, so it cannot end the literal early."""
    rendered = quote_sql_literal("x'; DROP TABLE users --", dialect)
    assert rendered.startswith("'")
    assert rendered.endswith("'")
    # The payload survives as DATA, its quote escaped rather than closing the literal:
    # exactly one escaped quote, and no bare one left over.
    body = rendered[1:-1]
    escaped = "''" if dialect is SqlDialect.postgres else "\\'"
    assert body.count(escaped) == 1
    assert body.replace(escaped, "").count("'") == 0


@pytest.mark.parametrize("dialect", list(SqlDialect))
def test_quote_sql_literal_rejects_invalid_values(dialect: SqlDialect) -> None:
    with pytest.raises(ValueError, match="empty"):
        quote_sql_literal("   ", dialect)
    with pytest.raises(ValueError, match="finite"):
        quote_sql_literal(float("nan"), dialect)


@pytest.mark.parametrize("dialect", list(SqlDialect))
def test_quote_sql_string_literal_keeps_numeric_text_quoted(dialect: SqlDialect) -> None:
    assert quote_sql_string_literal("3", dialect) == "'3'"


@pytest.mark.parametrize(
    "dialect", [SqlDialect.bigquery, SqlDialect.clickhouse, SqlDialect.databricks]
)
def test_quote_sql_string_literal_doubles_backslash_first(dialect: SqlDialect) -> None:
    """Backslash is escaped BEFORE the quote, so a trailing one cannot escape the close."""
    assert quote_sql_string_literal("a\\", dialect) == "'a\\\\'"
    assert quote_sql_string_literal("a\\'b", dialect) == "'a\\\\\\'b'"


def test_quote_sql_string_literal_leaves_backslash_alone_on_postgres() -> None:
    """Under standard_conforming_strings a backslash is DATA, not an escape."""
    assert quote_sql_string_literal("a\\b", SqlDialect.postgres) == "'a\\b'"


@pytest.mark.parametrize(
    "dialect", [SqlDialect.bigquery, SqlDialect.clickhouse, SqlDialect.databricks]
)
def test_quote_sql_string_literal_escapes_newlines(dialect: SqlDialect) -> None:
    """A raw newline in a single-quoted BigQuery literal is an "Unclosed string literal"."""
    assert quote_sql_string_literal("a\nb", dialect) == "'a\\nb'"


# --- dialect_for_db_type -----------------------------------------------------


@pytest.mark.parametrize(
    ("db_type", "expected"),
    [
        ("clickhouse", SqlDialect.clickhouse),
        ("postgres", SqlDialect.postgres),
        ("bigquery", SqlDialect.bigquery),
        ("databricks", SqlDialect.databricks),
        ("snowflake", SqlDialect.snowflake),
        # The synthetic demo warehouse mimics ClickHouse semantics.
        ("synthetic", SqlDialect.clickhouse),
    ],
)
def test_dialect_for_db_type_maps_every_supported_source(
    db_type: str, expected: SqlDialect
) -> None:
    assert dialect_for_db_type(db_type) == expected


def test_dialect_for_db_type_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Unsupported warehouse db_type"):
        dialect_for_db_type("duckdb")


# --- quote_identifier --------------------------------------------------------


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        (SqlDialect.clickhouse, "`order`"),
        (SqlDialect.bigquery, "`order`"),
        (SqlDialect.databricks, "`order`"),
        (SqlDialect.postgres, '"order"'),
        (SqlDialect.snowflake, '"order"'),
    ],
)
def test_quote_identifier_quotes_reserved_word_per_dialect(
    dialect: SqlDialect, expected: str
) -> None:
    assert quote_identifier("order", dialect) == expected


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        (SqlDialect.clickhouse, "`t`.`col`"),
        (SqlDialect.bigquery, "`t`.`col`"),
        (SqlDialect.databricks, "`t`.`col`"),
        (SqlDialect.postgres, '"t"."col"'),
    ],
)
def test_quote_identifier_quotes_each_dot_part(dialect: SqlDialect, expected: str) -> None:
    """A single-quoted "t.col" is read as ONE odd column name and fails on all three."""
    assert quote_identifier("t.col", dialect) == expected


@pytest.mark.parametrize("dialect", list(SqlDialect))
@pytest.mark.parametrize("bad", ["a..b", "a.", "1abc", "col name", "col`x", 'col"x', "col;--"])
def test_quote_identifier_rejects_unsafe_names(bad: str, dialect: SqlDialect) -> None:
    """The allowlist runs FIRST, so the quote character can never be broken out of."""
    with pytest.raises(ValueError):
        quote_identifier(bad, dialect)


# --- parse_utc_timestamp / quote_timestamp_literal ---------------------------


@pytest.mark.parametrize(
    "text",
    ["2026-01-01", "2026-01-01 00:00:00", "2026-01-01T00:00:00", "2026-01-01T00:00:00Z"],
)
def test_parse_utc_timestamp_accepts_iso_forms(text: str) -> None:
    parsed = parse_utc_timestamp(text)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.year == 2026


@pytest.mark.parametrize("text", ["trial", "2026", "12-34", "", "not a date"])
def test_parse_utc_timestamp_returns_none_for_non_timestamps(text: str) -> None:
    """A non-timestamp falls back to the plain string literal, never a guess."""
    assert parse_utc_timestamp(text) is None


def test_parse_utc_timestamp_normalizes_offset_to_utc() -> None:
    parsed = parse_utc_timestamp("2026-01-01T05:30:00+03:00")
    assert parsed is not None
    assert parsed.hour == 2
    assert parsed.minute == 30


@pytest.mark.parametrize(
    ("dialect", "kind", "expected"),
    [
        (
            SqlDialect.clickhouse,
            TimeKind.timestamp,
            "parseDateTime64BestEffort('2026-01-01 00:00:00.000000+00:00', 6, 'UTC')",
        ),
        (
            SqlDialect.postgres,
            TimeKind.timestamp,
            "TIMESTAMPTZ '2026-01-01 00:00:00.000000+00:00'",
        ),
        (SqlDialect.bigquery, TimeKind.timestamp, "TIMESTAMP '2026-01-01 00:00:00.000000+00:00'"),
        # BigQuery DATETIME is a zone-less wall clock and REJECTS an offset
        # ("Invalid DATETIME literal"), so its literal must not carry one.
        (SqlDialect.bigquery, TimeKind.datetime, "DATETIME '2026-01-01 00:00:00.000000'"),
        (SqlDialect.bigquery, TimeKind.date, "DATE '2026-01-01'"),
        (
            SqlDialect.databricks,
            TimeKind.timestamp,
            "TIMESTAMP '2026-01-01 00:00:00.000000+00:00'",
        ),
        (SqlDialect.databricks, TimeKind.date, "DATE '2026-01-01'"),
    ],
)
def test_quote_timestamp_literal_pins_utc_per_dialect(
    dialect: SqlDialect, kind: TimeKind, expected: str
) -> None:
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    assert quote_timestamp_literal(moment, dialect, kind=kind) == expected


# --- time_kind_of ------------------------------------------------------------


@pytest.mark.parametrize(
    ("native_type", "expected"),
    [
        ("TIMESTAMP", TimeKind.timestamp),
        ("DATE", TimeKind.date),
        ("DATETIME", TimeKind.datetime),
        ("DateTime64(3)", TimeKind.datetime),
        ("Nullable(Date32)", TimeKind.date),
        ("Float64", TimeKind.unsupported),
    ],
)
def test_time_kind_of_reads_the_NATIVE_type(native_type: str, expected: TimeKind) -> None:
    """The contract is "native warehouse type in, TimeKind out".

    The old fixture fed the BUCKETED ``"timestamp"``, which is the one value that
    cannot tell DATE from DATETIME — fact-table introspection folds all four
    BigQuery time types into it. Passing it here collapses every BigQuery time
    column to ``TimeKind.timestamp`` and makes the DATE/DATETIME arms of
    ``quote_timestamp_literal`` unreachable, so the test encoded the bug rather
    than the contract.
    """
    assert time_kind_of("ts", {"ts": native_type}) is expected


@pytest.mark.parametrize("types", [None, {}, {"other": "timestamp"}])
def test_time_kind_of_unknown_column_is_unsupported(types: dict[str, str] | None) -> None:
    """Unknown type -> do NOT retype the literal; fall back to the plain string."""
    assert time_kind_of("ts", types) is TimeKind.unsupported


# --- lint_dialect_sql --------------------------------------------------------


def test_lint_flags_date_trunc_string_form_on_bigquery() -> None:
    message = lint_dialect_sql(
        "SELECT date_trunc('day', created_at) AS bucket FROM events", SqlDialect.bigquery
    )
    assert message is not None
    assert "TIMESTAMP_TRUNC" in message


@pytest.mark.parametrize(
    "dialect", [SqlDialect.clickhouse, SqlDialect.postgres, SqlDialect.databricks]
)
def test_lint_accepts_date_trunc_string_form_where_it_exists(dialect: SqlDialect) -> None:
    """date_trunc('day', ts) is REAL on ClickHouse, PostgreSQL and Databricks: never flag it."""
    assert lint_dialect_sql("SELECT date_trunc('day', created_at) FROM events", dialect) is None


def test_lint_accepts_bigquery_native_date_trunc() -> None:
    """GoogleSQL's own DATE_TRUNC(date_expr, part) must not be mistaken for the bug."""
    assert lint_dialect_sql("SELECT DATE_TRUNC(day_col, MONTH) FROM t", SqlDialect.bigquery) is None


def test_lint_does_not_flag_countif_on_bigquery() -> None:
    """GoogleSQL is case-INSENSITIVE, so countIf resolves to the real COUNTIF."""
    assert lint_dialect_sql("SELECT countIf(ok) FROM t", SqlDialect.bigquery) is None


@pytest.mark.parametrize(
    ("sql", "dialect", "needle"),
    [
        ("SELECT toStartOfInterval(ts, INTERVAL 1 DAY) FROM t", SqlDialect.postgres, "date_bin"),
        ("SELECT toStartOfInterval(ts, INTERVAL 1 DAY) FROM t", SqlDialect.bigquery, "BigQuery"),
        ("SELECT date_bin(INTERVAL '1 day', ts, now()) FROM t", SqlDialect.bigquery, "BigQuery"),
        (
            "SELECT toStartOfInterval(ts, INTERVAL 1 DAY) FROM t",
            SqlDialect.databricks,
            "date_trunc",
        ),
        ("SELECT TIMESTAMP_TRUNC(ts, DAY) FROM t", SqlDialect.databricks, "date_trunc"),
        (
            "SELECT date_bin(INTERVAL '1 day', ts, now()) FROM t",
            SqlDialect.clickhouse,
            "ClickHouse",
        ),
        ("SELECT TIMESTAMP_TRUNC(ts, DAY) FROM t", SqlDialect.postgres, "PostgreSQL"),
        ("SELECT TIMESTAMP_TRUNC(ts, DAY) FROM t", SqlDialect.clickhouse, "ClickHouse"),
        ("SELECT countIf(ok) FROM t", SqlDialect.postgres, "FILTER"),
        ("SELECT `status` FROM t", SqlDialect.postgres, "double quotes"),
    ],
)
def test_lint_flags_cross_dialect_functions(sql: str, dialect: SqlDialect, needle: str) -> None:
    message = lint_dialect_sql(sql, dialect)
    assert message is not None
    assert needle in message


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        (
            "SELECT toStartOfInterval(created_at, INTERVAL 1 DAY, 'UTC') AS bucket, "
            "count(*) AS value FROM events GROUP BY 1 ORDER BY 1",
            SqlDialect.clickhouse,
        ),
        (
            "SELECT date_bin(INTERVAL '1 day', created_at, "
            "TIMESTAMPTZ '1970-01-01 00:00:00+00:00') AS bucket, count(*) AS value "
            "FROM events GROUP BY 1 ORDER BY 1",
            SqlDialect.postgres,
        ),
        (
            "SELECT TIMESTAMP_TRUNC(created_at, DAY, 'UTC') AS bucket, count(*) AS value "
            "FROM events GROUP BY 1 ORDER BY 1",
            SqlDialect.bigquery,
        ),
        (
            "SELECT date_trunc('DAY', created_at) AS bucket, count(*) AS value "
            "FROM events GROUP BY 1 ORDER BY 1",
            SqlDialect.databricks,
        ),
    ],
)
def test_lint_passes_each_dialects_own_starter_template(sql: str, dialect: SqlDialect) -> None:
    """The exact SQL the frontend starter templates emit, per warehouse."""
    assert lint_dialect_sql(sql, dialect) is None


def test_lint_is_not_a_security_control() -> None:
    """The read-only gate is unchanged, and the lint only ever ADDS a rejection."""
    with pytest.raises(ValueError):
        validate_select_sql_safety("SELECT 1; DROP TABLE users")
    with pytest.raises(ValueError):
        validate_select_sql_safety("INSERT INTO t VALUES (1)")
    assert lint_dialect_sql("SELECT 1", SqlDialect.bigquery) is None


# --- validate_select_sql_safety ---------------------------------------------


def test_validate_select_sql_safety_accepts_clean_select() -> None:
    sql = "SELECT count(*) AS value, ts AS time FROM events GROUP BY ts"
    assert validate_select_sql_safety(sql) == sql


def test_validate_select_sql_safety_accepts_top_level_with() -> None:
    sql = (
        "WITH today() - 30 AS start_time, "
        "fv AS (SELECT device_id, min(time) AS first_visit_time FROM events GROUP BY device_id) "
        "SELECT device_id, first_visit_time AS timestamp FROM fv "
        "WHERE first_visit_time >= start_time"
    )
    assert validate_select_sql_safety(sql) == sql


def test_validate_select_sql_safety_strips_trailing_semicolon() -> None:
    sql = "SELECT 1 AS value, ts AS time FROM events ;"
    cleaned = validate_select_sql_safety(sql)
    assert not cleaned.endswith(";")
    assert cleaned == "SELECT 1 AS value, ts AS time FROM events"


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO events VALUES (1)",
        "UPDATE events SET value = 1",
        "DELETE FROM events",
        "DROP TABLE events",
        "ALTER TABLE events ADD COLUMN x INT",
        "TRUNCATE TABLE events",
        "SELECT value INTO dump FROM events",
        "SELECT value FROM events; DROP TABLE events",
        "SELECT value FROM events; SELECT 1",
        "SELECT value FROM events -- comment",
        "SELECT value FROM events /* x */",
        "SELECT value FROM a UNION SELECT b FROM c",
        "SELECT value FROM a UNION ALL SELECT b FROM c",
        "WITH x AS (SELECT 1)",
        "WITH x AS (SELECT 1) INSERT INTO events SELECT * FROM x",
        "WITH x AS (SELECT value FROM a) SELECT value FROM x UNION SELECT b FROM c",
    ],
)
def test_validate_select_sql_safety_rejects_unsafe(sql: str) -> None:
    with pytest.raises(ValueError):
        validate_select_sql_safety(sql)


# ``\'`` is an escaped quote on ClickHouse, BigQuery and Databricks and a
# backslash plus the closing quote on PostgreSQL. The masker used to read only
# the doubling rule, so the tail after such a literal was masked as data while
# the backslash engines ran it as code.
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'x\\'' ; DROP TABLE t ; SELECT 'y'",
        "SELECT 'x\\'' UNION ALL SELECT secret FROM vault WHERE 'y' = 'y'",
    ],
)
def test_select_gate_scans_past_a_backslash_escaped_quote(sql: str) -> None:
    with pytest.raises(ValueError):
        validate_select_sql_safety(sql)


def test_fragment_gate_scans_past_a_backslash_escaped_quote() -> None:
    with pytest.raises(ValueError, match="SELECT"):
        validate_sql_fragment("col = 'a\\'' OR col IN (SELECT s FROM secrets) OR col = 'b'")


@pytest.mark.parametrize(
    "fragment",
    ["path = 'C:\\temp'", "name = 'O''Brien'", "event_name IN ('Delete Account')"],
)
def test_fragment_gate_still_accepts_plain_literals(fragment: str) -> None:
    assert validate_sql_fragment(fragment) == fragment


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_files('s3://other-bucket/')",
        "SELECT http_request(conn => 'x', method => 'GET', path => '/') AS r",
        "SELECT ai_query ('endpoint', payload) FROM events",
    ],
)
def test_select_gate_refuses_functions_that_leave_the_warehouse(sql: str) -> None:
    with pytest.raises(ValueError, match="disallowed keyword"):
        validate_select_sql_safety(sql)


def test_a_column_named_like_a_refused_function_still_passes() -> None:
    assert validate_select_sql_safety("SELECT read_files FROM t") == "SELECT read_files FROM t"
    assert validate_sql_fragment("reflect = 1") == "reflect = 1"
