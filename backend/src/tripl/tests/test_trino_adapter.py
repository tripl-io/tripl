"""The Trino adapter against a fake DB-API connection: the SQL it sends, and what it reads.

Nothing here contacts Trino. The adapter is the real class entered through
``object.__new__`` (no connection), with a fake connection that records every
statement and answers canned rows. What a SQL string proves is shape, not
validity: the statements are executed — on every pull request — by
``conformance/test_trino_value_conformance.py`` against a real coordinator.
Athena's connection is covered in ``test_athena_adapter.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import requests

from tripl.core.adapters import trino_sql
from tripl.core.adapters.base import AggregateSpec, FieldContractExpectation
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.measure_validator import (
    SqlDialect,
    dialect_for_db_type,
    lint_dialect_sql,
    quote_identifier,
    quote_sql_literal,
    quote_sql_string_literal,
    quote_timestamp_literal,
)
from tripl.core.adapters.multi_aggregate_sql import compile_time_bucketed_multi_aggregate_sql
from tripl.core.adapters.registry import build_adapter, supported_db_types
from tripl.core.adapters.sql_common import as_utc_bucket
from tripl.core.adapters.trino import TrinoAdapter
from tripl.core.adapters.trino_net import PinnedSession
from tripl.core.adapters.trino_sql import quote_literal
from tripl.core.bucketing import WEEK_ORIGIN
from tripl.core.warehouse_types import (
    ComplexKind,
    TimeKind,
    classify_complex,
    classify_time,
    is_string_type,
)
from tripl.crypto import encrypt_value
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import MetricAggregation
from tripl.schemas.data_source import ConnectionSettingsError, parse_connection_settings
from tripl.services.fact_table_introspection_service import bucket_warehouse_type

_FROM = datetime(2026, 4, 1, tzinfo=UTC)
_TO = datetime(2026, 4, 2, tzinfo=UTC)
_BASE = "SELECT time, event_name, amount, props FROM events"
_TYPES = {
    "time": "timestamp(6) with time zone",
    "naive": "timestamp(3)",
    "d": "date",
    "event_name": "varchar",
    "amount": "double",
    "props": "json",
    "tags": "array(varchar)",
}


class DriverError(Exception):
    def __init__(self, msg: str, error_name: str) -> None:
        super().__init__(msg)
        self.error_name = error_name


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn
        self.description: list[tuple[object, ...]] = []
        self._rows: list[tuple[object, ...]] = []

    def execute(self, sql: str, params: object = None) -> None:
        assert params is None, "values are rendered as literals, never bound"
        self._conn.sql.append(sql)
        if self._conn.raise_error is not None:
            raise self._conn.raise_error
        if self._conn.refused is not None and quote_literal(self._conn.refused) in sql:
            raise DriverError("undefined group option", "INVALID_FUNCTION_ARGUMENT")
        names, rows = self._conn.answers.pop(0) if self._conn.answers else ([], [])
        self.description = [(name, type_name) for name, type_name in names]
        self._rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)

    def close(self) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.answers: list[tuple[list[tuple[str, str]], list[tuple[object, ...]]]] = []
        self.raise_error: Exception | None = None
        self.refused: str | None = None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        return None


def make_adapter(types: dict[str, str] | None = None) -> tuple[TrinoAdapter, FakeConnection]:
    conn = FakeConnection()
    adapter = object.__new__(TrinoAdapter)
    adapter._conn = conn
    column_types = dict(_TYPES if types is None else types)
    adapter._column_types = column_types
    adapter._allowed_columns = set(column_types)
    adapter._catalog = "hive"
    adapter._schema = "events"
    return adapter, conn


# --------------------------------------------------------------------------- #
# introspection
# --------------------------------------------------------------------------- #


def test_get_columns_reads_a_limit_zero_wrapper() -> None:
    adapter, conn = make_adapter({})
    conn.answers.append(([("ts", "timestamp(3) with time zone"), ("doc", "json")], []))
    columns = adapter.get_columns(_BASE)
    assert conn.sql == [f"SELECT * FROM ({_BASE}) AS _src LIMIT 0"]
    assert [(c.name, c.type_name) for c in columns] == [
        ("ts", "timestamp(3) with time zone"),
        ("doc", "json"),
    ]
    assert adapter._allowed_columns == {"ts", "doc"}


def test_schema_browse_quotes_the_catalog_and_renders_schemas_as_literals() -> None:
    adapter, conn = make_adapter()
    adapter._schema_allowlist = ("Marts", "o'hare")
    conn.answers.append(
        (
            [],
            [
                ("events", "clicks", "ts", "timestamp(3)"),
                ("marts", "daily", "n", "bigint"),
            ],
        )
    )
    tables = adapter.get_schema_tables()
    assert conn.sql[0].startswith(
        'SELECT table_schema, table_name, column_name, data_type FROM "hive".information_schema'
    )
    assert "table_schema IN ('events', 'marts', 'o''hare')" in conn.sql[0]
    assert [t.name for t in tables] == ["clicks", "marts.daily"]


def test_schema_browse_without_a_schema_lists_the_whole_catalog() -> None:
    adapter, conn = make_adapter()
    adapter._schema = None
    adapter.get_schema_tables()
    assert "WHERE table_schema <> 'information_schema'" in conn.sql[0]


def test_test_connection_runs_a_trivial_select() -> None:
    adapter, conn = make_adapter()
    conn.answers.append(([("ok", "integer")], [(1,)]))
    assert adapter.test_connection() is True
    assert conn.sql == ["SELECT 1 AS ok"]


# --------------------------------------------------------------------------- #
# time
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("code", "fragment"),
    [
        ("1h", "CAST(date_trunc('hour', (\"time\" AT TIME ZONE 'UTC')) AS timestamp(3))"),
        ("1d", "CAST(date_trunc('day', (\"time\" AT TIME ZONE 'UTC')) AS timestamp(3))"),
        ("15m", "/ 900) AS bigint) * 900, TIMESTAMP '1970-01-01 00:00:00.000')"),
        ("6h", "/ 21600) AS bigint) * 21600, TIMESTAMP '1970-01-01 00:00:00.000')"),
    ],
)
def test_bucket_sql_for_every_interval(code: str, fragment: str) -> None:
    adapter, _ = make_adapter()
    assert fragment in adapter._bucket_expression("time", code)


def test_weeks_are_anchored_at_week_origin_on_whole_seconds() -> None:
    adapter, _ = make_adapter()
    origin = int(WEEK_ORIGIN.timestamp())
    sql = adapter._bucket_expression("time", "1w")
    assert f"- {origin} AS double) / 604800) AS bigint) * 604800 + {origin}" in sql
    # Truncated to the minute and counted in integer seconds: no fraction is ever
    # rounded through a double into the next bucket.
    assert "date_trunc('minute'" in sql
    assert "date_diff('second', TIMESTAMP '1970-01-01 00:00:00.000'" in sql
    assert "to_unixtime" not in sql


def test_naive_and_date_columns_bucket_without_a_zone_conversion() -> None:
    adapter, _ = make_adapter()
    assert adapter._bucket_expression("naive", "1h") == (
        "CAST(date_trunc('hour', \"naive\") AS timestamp(3))"
    )
    assert adapter._bucket_expression("d", "1d") == (
        "CAST(date_trunc('day', CAST(\"d\" AS timestamp(3))) AS timestamp(3))"
    )


@pytest.mark.parametrize("code", ["15m", "1h", "6h"])
def test_a_date_column_refuses_sub_day_intervals(code: str) -> None:
    adapter, _ = make_adapter()
    with pytest.raises(WarehouseCapabilityError, match="no time-of-day"):
        adapter._bucket_expression("d", code)


def test_window_literals_follow_the_column_type_family() -> None:
    adapter, _ = make_adapter()
    assert adapter._time_condition("time", _FROM, _TO) == (
        "\"time\" >= TIMESTAMP '2026-04-01 00:00:00.000000 UTC' "
        "AND \"time\" < TIMESTAMP '2026-04-02 00:00:00.000000 UTC'"
    )
    assert "\"naive\" >= TIMESTAMP '2026-04-01 00:00:00.000000' AND" in adapter._time_condition(
        "naive", _FROM, _TO
    )
    assert adapter._time_condition("d", _FROM, _TO).startswith("\"d\" >= DATE '2026-04-01'")


def test_a_time_column_without_a_date_is_refused_with_an_actionable_message() -> None:
    adapter, _ = make_adapter({"t": "time(3)"})
    with pytest.raises(WarehouseCapabilityError, match="carries no date"):
        adapter._bucket_expression("t", "1d")


def test_buckets_come_back_as_aware_utc() -> None:
    assert as_utc_bucket(datetime(2026, 4, 1, 6)) == datetime(2026, 4, 1, 6, tzinfo=UTC)
    assert as_utc_bucket("2026-04-01 06:00:00.000") == datetime(2026, 4, 1, 6, tzinfo=UTC)
    adapter, conn = make_adapter()
    conn.answers.append(([], [(datetime(2026, 4, 1, 6), 3)]))
    _, _, rows = adapter.get_time_bucketed_counts(_BASE, "time", "6h", [], [], None, _FROM, _TO)
    assert rows == [(datetime(2026, 4, 1, 6, tzinfo=UTC), 3)]
    # Trino refuses an output alias in GROUP BY: the bucket is grouped by ordinal.
    assert " GROUP BY 1 ORDER BY _bucket " in conn.sql[0]


# --------------------------------------------------------------------------- #
# values reach the engine as literals, quoted
# --------------------------------------------------------------------------- #


def test_literals_double_the_quote_and_nothing_else() -> None:
    assert quote_literal("o'brien") == "'o''brien'"
    assert quote_literal("back\\slash") == "'back\\slash'"
    assert quote_literal("x'); DROP TABLE t; --") == "'x''); DROP TABLE t; --'"


def test_top_n_values_are_quoted_literals() -> None:
    adapter, conn = make_adapter()
    conn.answers.append(([], [("event_name", "it's"), ("event_name", "b")]))
    adapter.get_time_bucketed_breakdown_counts(
        _BASE, "time", "1d", "event_name", ["event_name"], [], None, _FROM, _TO, values_limit=3
    )
    assert "WHERE rn <= 2" in conn.sql[0]
    assert "ORDER BY _cnt DESC, _breakdown_value) AS rn" in conn.sql[0]
    assert "IN ('it''s', 'b')" in conn.sql[1]
    assert "GROUP BY GROUPING SETS" in conn.sql[1]


def test_contract_values_and_the_group_filter_are_quoted() -> None:
    adapter, conn = make_adapter()
    conn.answers.append(([], []))  # the regex probe
    conn.answers.append(([], [(1, 4, "x", 0, 4, None)]))
    expectations = [
        FieldContractExpectation(
            field_name="event_name",
            drift_type="enum_violation",
            threshold=0.0,
            enum_options=("a'b", "c"),
        ),
        FieldContractExpectation(
            field_name="event_name", drift_type="regex_violation", threshold=0.0, regex="^u'"
        ),
    ]
    violations = adapter.validate_field_contracts(
        _BASE, expectations, group_column="event_name", group_value="g'1"
    )
    assert conn.sql[0] == "SELECT regexp_like('', '^u''')"
    statement = conn.sql[1]
    assert "NOT IN ('a''b', 'c')" in statement
    assert "NOT regexp_like(COALESCE(\"event_name\", ''), '^u''')" in statement
    assert "WHERE COALESCE(\"event_name\", '') = 'g''1'" in statement
    assert [(v.field_name, v.bad_count, v.total_count) for v in violations] == [
        ("event_name", 1, 4)
    ]


def test_a_refused_regex_is_skipped_after_a_table_free_probe() -> None:
    adapter, conn = make_adapter()
    conn.refused = "(?P<x>a)"
    expectation = FieldContractExpectation(
        field_name="event_name", drift_type="regex_violation", threshold=0.0, regex="(?P<x>a)"
    )
    assert adapter.validate_field_contracts(_BASE, [expectation]) == []
    assert conn.sql[0] == "SELECT regexp_like('', '(?P<x>a)')"
    assert adapter.take_skipped_field_contracts() == [expectation]


def test_range_contracts_parse_with_try_cast_and_render_floats_plainly() -> None:
    adapter, _ = make_adapter()
    expectation = FieldContractExpectation(
        field_name="amount",
        drift_type="range_violation",
        threshold=0.0,
        min_value=0.0,
        max_value=50.0,
    )
    predicate = adapter._contract_bad_condition(expectation, adapter._new_params())
    assert predicate is not None
    assert "try_cast(COALESCE(format('%s', \"amount\"), '') AS double) < 0.0" in predicate
    assert "> 50.0" in predicate


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #


def test_nested_shapes_and_values() -> None:
    adapter, _ = make_adapter()
    assert adapter._json_paths_expression("props") == (
        "COALESCE(json_format(CAST(array_sort(map_keys("
        "try_cast(\"props\" AS map(varchar, json)))) AS json)), '[]')"
    )
    assert adapter._json_path_expression("props", "user.city") == (
        'json_format(json_extract("props", \'$["user"]["city"]\'))'
    )
    assert adapter._property_value_expression("props", "plan") == (
        'COALESCE(json_extract_scalar("props", \'$["plan"]\'), '
        "NULLIF(json_format(json_extract(\"props\", '$[\"plan\"]')), 'null'))"
    )


@pytest.mark.parametrize("path", ["a'b", 'a"b', "a]b", "", "..", "$.a"])
def test_a_path_segment_outside_the_identifier_grammar_is_refused(path: str) -> None:
    adapter, _ = make_adapter()
    with pytest.raises(ValueError, match="Unsupported JSON path"):
        adapter._json_path_expression("props", path)


def test_scalar_columns_hold_no_paths() -> None:
    adapter, _ = make_adapter()
    with pytest.raises(ValueError, match="holds no nested paths"):
        adapter._json_paths_expression("event_name")


def test_json_string_source_keeps_column_order_and_parses_safely() -> None:
    adapter, conn = make_adapter({})
    conn.answers.append(([("ts", "timestamp(3)"), ("props", "varchar"), ("n", "bigint")], []))
    sql = adapter.json_string_source(_BASE, ["props"])
    assert sql == (
        'SELECT "ts", IF(try_cast(try(json_parse("props")) AS map(varchar, json)) IS NULL, '
        'NULL, try(json_parse("props"))) AS "props", "n" '
        f"FROM ({_BASE}) AS _json_src"
    )
    with pytest.raises(ValueError, match="invalid column name"):
        adapter.json_string_source(_BASE, ['x" OR 1=1'])


def test_values_render_as_text_by_type() -> None:
    adapter, _ = make_adapter()
    assert adapter._text("amount") == "format('%s', \"amount\")"
    assert adapter._text("props") == 'json_format("props")'
    assert adapter._text("tags") == 'json_format(CAST("tags" AS json))'
    assert adapter._text("event_name") == '"event_name"'
    assert adapter._text("time") == 'CAST("time" AS varchar)'


# --------------------------------------------------------------------------- #
# aggregates
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (
            AggregateSpec(key="c", aggregation=MetricAggregation.count, filter_sql="x > 0"),
            "NULLIF(count_if(x > 0), 0)",
        ),
        (
            AggregateSpec(
                key="u",
                aggregation=MetricAggregation.count_distinct,
                column="event_name",
                filter_sql="x > 0",
            ),
            'CASE WHEN count_if(x > 0) = 0 THEN NULL ELSE count(DISTINCT IF(x > 0, "event_name", '
            "NULL)) END",
        ),
        (
            AggregateSpec(
                key="s", aggregation=MetricAggregation.sum, column="amount", filter_sql="x > 0"
            ),
            'sum(CASE WHEN x > 0 THEN "amount" END)',
        ),
        (
            AggregateSpec(key="s", aggregation=MetricAggregation.sum, column="amount"),
            'sum("amount")',
        ),
    ],
)
def test_conditional_aggregates(spec: AggregateSpec, expected: str) -> None:
    adapter, _ = make_adapter()
    assert adapter._spec_aggregate_sql(spec) == expected


@pytest.mark.parametrize("db_type", ["trino", "athena"])
def test_disclosed_batch_sql_is_the_executed_statement(db_type: str) -> None:
    specs = [AggregateSpec(key="c", aggregation=MetricAggregation.count)]
    _, disclosed = compile_time_bucketed_multi_aggregate_sql(
        db_type=db_type,
        base_query=_BASE,
        time_column="time",
        interval="1d",
        specs=specs,
        time_from=_FROM,
        time_to=_TO,
        column_types={"time": _TYPES["time"]},
    )
    adapter, conn = make_adapter({"time": _TYPES["time"]})
    adapter.get_time_bucketed_multi_aggregate(_BASE, "time", "1d", specs, _FROM, _TO)
    assert conn.sql == [disclosed]
    assert " GROUP BY 1 ORDER BY _bucket " in disclosed


def test_aggregate_breakdown_groups_by_the_folded_value_only() -> None:
    adapter, conn = make_adapter()
    adapter.get_time_bucketed_aggregate_breakdown(
        _BASE,
        "time",
        "1d",
        MetricAggregation.sum,
        "amount",
        "event_name",
        ["event_name"],
        [],
        None,
        _FROM,
        _TO,
    )
    assert "GROUP BY 1, 2, 3 ORDER BY" in conn.sql[0]
    assert 'COALESCE("event_name", \'\') AS "event_name"' in conn.sql[0]


def test_a_breakdown_without_grouping_keys_has_no_group_by() -> None:
    adapter, conn = make_adapter()
    adapter.get_full_breakdown(_BASE, [], [])
    assert "GROUP BY" not in conn.sql[0]


# --------------------------------------------------------------------------- #
# deadlines
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("error_name", ["EXCEEDED_TIME_LIMIT", "USER_CANCELED"])
def test_a_statement_past_its_deadline_is_reported_as_the_source_timeout(error_name: str) -> None:
    adapter, conn = make_adapter()
    adapter._timeout_seconds = 30.0
    conn.raise_error = DriverError("Query exceeded maximum time limit", error_name)
    with pytest.raises(TimeoutError, match="30s timeout configured for this data source"):
        adapter._run("SELECT 1")


def test_a_client_side_read_timeout_is_reported_the_same_way() -> None:
    adapter, conn = make_adapter()
    adapter._timeout_seconds = 5.0
    conn.raise_error = requests.exceptions.ReadTimeout("read timed out")
    with pytest.raises(TimeoutError, match="5s timeout"):
        adapter._run("SELECT 1")


def test_other_driver_errors_pass_through_unchanged() -> None:
    adapter, conn = make_adapter()
    adapter._timeout_seconds = 30.0
    conn.raise_error = DriverError("Table not found", "TABLE_NOT_FOUND")
    with pytest.raises(DriverError, match="Table not found"):
        adapter._run("SELECT 1")


def test_schema_browse_is_capped_below_the_source_timeout() -> None:
    adapter, _ = make_adapter()
    adapter._timeout_seconds = 300.0
    assert adapter._query_deadline(30) == 30


# --------------------------------------------------------------------------- #
# the factory, settings and the outbound rule
# --------------------------------------------------------------------------- #


def _source(password: str = "hunter2", **settings: object) -> DataSource:
    return DataSource(
        name="trino",
        db_type="trino",
        host="trino.example.com",
        port=443,
        database_name="hive",
        username="tripl",
        password_encrypted=encrypt_value(password),
        timeout_seconds=120,
        extra_params=dict(settings),
    )


class _FakeDriver:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    def connect(self, **kwargs: object) -> FakeConnection:
        self.kwargs = kwargs
        return FakeConnection()


def test_registry_lists_trino_and_athena() -> None:
    assert {"trino", "athena"} <= set(supported_db_types())
    assert dialect_for_db_type("trino") is SqlDialect.trino
    assert dialect_for_db_type("athena") is SqlDialect.athena


def test_factory_wires_settings_timeout_and_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    from trino.auth import BasicAuthentication

    driver = _FakeDriver()
    monkeypatch.setattr(trino_sql, "import_trino", lambda: driver)
    adapter = build_adapter(_source(schema_name="events", schema_allowlist=["marts"]))
    assert isinstance(adapter, TrinoAdapter)
    kwargs = driver.kwargs
    assert kwargs["host"] == "trino.example.com"
    assert kwargs["port"] == 443
    assert kwargs["catalog"] == "hive"
    assert kwargs["schema"] == "events"
    assert kwargs["http_scheme"] == "https"
    assert kwargs["timezone"] == "UTC"
    assert kwargs["session_properties"] == {"query_max_run_time": "120s"}
    assert kwargs["request_timeout"] == 120
    assert isinstance(kwargs["auth"], BasicAuthentication)
    assert "http_session" not in kwargs
    assert adapter._schemas_in_scope() == ["events", "marts"]


def test_no_password_connects_without_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = _FakeDriver()
    monkeypatch.setattr(trino_sql, "import_trino", lambda: driver)
    build_adapter(_source(password="", http_scheme="http"))
    assert "auth" not in driver.kwargs
    assert driver.kwargs["http_scheme"] == "http"


def test_a_password_is_never_sent_over_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trino_sql, "import_trino", _FakeDriver)
    with pytest.raises(WarehouseCapabilityError, match="only sent over HTTPS"):
        build_adapter(_source(http_scheme="http"))


def _public_hosts_only(monkeypatch: pytest.MonkeyPatch, address: str) -> list[str]:
    from tripl.config import settings

    monkeypatch.setattr(settings, "outbound_public_hosts_only", True)
    resolved: list[str] = []

    def fake_resolve(host: str, *_a: object, **_k: object) -> list[object]:
        resolved.append(host)
        return [(None, None, None, None, (address, 443))]

    monkeypatch.setattr("tripl.services.safe_http._resolve", fake_resolve)
    return resolved


def test_public_hosts_only_refuses_a_private_coordinator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trino_sql, "import_trino", _FakeDriver)
    resolved = _public_hosts_only(monkeypatch, "10.0.0.5")
    with pytest.raises(WarehouseCapabilityError, match="private or internal address"):
        build_adapter(_source())
    assert resolved == ["trino.example.com"]


def test_public_hosts_only_pins_the_vetted_address(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = _FakeDriver()
    monkeypatch.setattr(trino_sql, "import_trino", lambda: driver)
    _public_hosts_only(monkeypatch, "93.184.216.34")
    build_adapter(_source())
    session = driver.kwargs["http_session"]
    assert isinstance(session, PinnedSession)
    assert session.max_redirects == 0
    assert session.trust_env is False


def test_the_pinned_session_dials_the_address_and_refuses_other_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, str]] = []

    def fake_send(_self: object, request: requests.PreparedRequest, *_a: object, **_k: object):
        sent.append((request.url or "", request.headers["Host"]))
        response = requests.Response()
        response.status_code = 200
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
    session = PinnedSession("trino.example.com", "93.184.216.34", 443, tls=True)
    session.get("https://trino.example.com/v1/statement/queued/1")
    assert sent == [("https://93.184.216.34:443/v1/statement/queued/1", "trino.example.com:443")]
    # A nextUri naming another host (or port) never leaves the process.
    with pytest.raises(requests.ConnectionError, match="only reaches trino.example.com:443"):
        session.get("https://169.254.169.254/latest/meta-data/")
    with pytest.raises(requests.ConnectionError):
        session.get("https://trino.example.com:8443/v1/statement")
    assert len(sent) == 1


@pytest.mark.parametrize(
    "raw",
    [
        {"http_scheme": "ftp"},
        {"schema_name": "a b"},
        {"schema_name": 'x"; DROP'},
        {"schema_allowlist": ["ok", "bad name"]},
        {"warehouse": "WH"},
    ],
)
def test_malformed_settings_are_rejected(raw: dict[str, object]) -> None:
    with pytest.raises(ConnectionSettingsError):
        parse_connection_settings("trino", raw)


def test_settings_are_trimmed_and_deduplicated() -> None:
    parsed = parse_connection_settings(
        "trino", {"schema_name": " events ", "schema_allowlist": ["a", " a ", "", "b"]}
    )
    assert parsed is not None
    assert parsed.model_dump(exclude_none=True) == {
        "schema_name": "events",
        "schema_allowlist": ["a", "b"],
    }


# --------------------------------------------------------------------------- #
# the SQL gate and type classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("dialect", [SqlDialect.trino, SqlDialect.athena])
def test_dialect_rendering_helpers(dialect: SqlDialect) -> None:
    assert quote_identifier("order", dialect) == '"order"'
    assert quote_sql_literal("o'brien", dialect) == "'o''brien'"
    # No escape sequences in a Trino literal: a backslash is data.
    assert quote_sql_string_literal("a\\b", dialect) == "'a\\b'"
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    assert quote_timestamp_literal(moment, dialect, kind=TimeKind.timestamp) == (
        "TIMESTAMP '2026-01-01 00:00:00.000000 UTC'"
    )
    assert quote_timestamp_literal(moment, dialect, kind=TimeKind.date) == "DATE '2026-01-01'"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT toStartOfInterval(ts, INTERVAL 1 DAY) FROM t",
        "SELECT date_bin(INTERVAL '1 day', ts, TIMESTAMP '1970-01-01') FROM t",
        "SELECT TIMESTAMP_TRUNC(ts, DAY) FROM t",
        "SELECT TIME_SLICE(ts, 1, 'DAY') FROM t",
        "SELECT countIf(x > 1) FROM t",
        "SELECT `x` FROM t",
    ],
)
def test_other_engines_sql_is_flagged_before_it_reaches_the_warehouse(sql: str) -> None:
    for dialect in (SqlDialect.trino, SqlDialect.athena):
        message = lint_dialect_sql(sql, dialect)
        assert message is not None
        assert ("Trino" if dialect is SqlDialect.trino else "Athena") in message


def test_trino_sql_is_not_flagged() -> None:
    sql = (
        "SELECT date_trunc('day', ts) AS d, count_if(x > 1), json_extract_scalar(p, '$.a') "
        "FROM t WHERE ts >= TIMESTAMP '2026-01-01 00:00:00 UTC' GROUP BY 1"
    )
    assert lint_dialect_sql(sql, SqlDialect.trino) is None


def test_type_classification_knows_trino_spellings() -> None:
    assert classify_time("timestamp(6) with time zone") is TimeKind.timestamp
    assert classify_time("timestamp(3)") is TimeKind.timestamp
    assert classify_time("date") is TimeKind.date
    assert classify_time("time(3)") is TimeKind.unsupported
    assert classify_complex("json") is ComplexKind.json
    assert classify_complex("map(varchar, json)") is ComplexKind.map
    assert classify_complex("array(json)") is None
    assert is_string_type("varchar")
    assert is_string_type("varchar(64)")
    assert not is_string_type("json")
    assert bucket_warehouse_type("bigint") == "number"
    assert bucket_warehouse_type("decimal(10, 2)") == "number"
    assert bucket_warehouse_type("timestamp(3) with time zone") == "timestamp"
    assert bucket_warehouse_type("varchar(64)") == "string"
    assert bucket_warehouse_type("boolean") == "bool"
