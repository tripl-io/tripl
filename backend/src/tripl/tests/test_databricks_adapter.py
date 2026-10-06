"""The Databricks adapter against a fake driver: the SQL it sends, and what it reads back.

Nothing here contacts a warehouse. The adapter is the real class entered through
``object.__new__`` (no connection, no credentials), with a fake DB-API
connection that records every ``(sql, parameters)`` pair and answers canned
rows. What a SQL string proves is shape, not validity: the statements are only
executed by a live check against a SQL warehouse.

What a live check must exercise, because nothing here can:

* connection test with a PAT and with OAuth M2M (token exchange at
  ``/oidc/v1/token``), and the error a wrong token or HTTP path gives;
* ``DESCRIBE QUERY`` over a base query, the schema browse over the default
  schema plus an allowlist, and preview rows (complex values arrive as JSON text);
* every interval code over ``TIMESTAMP``, ``TIMESTAMP_NTZ`` and ``DATE`` time
  columns, with the bucket values compared to ``floor_to_bucket`` (``WEEK`` must
  land on Monday);
* named parameters (``:p0``) accepted by the warehouse in ``IN (...)``,
  ``RLIKE`` and equality positions;
* a scan over ``VARIANT``, ``STRUCT``, ``MAP`` and ``ARRAY`` columns: shapes,
  nested value extraction, and ``json_string_source`` over a STRING column;
* top-N breakdowns (``GROUPING SETS`` + ``ROW_NUMBER``), all aggregates and
  multi-aggregates with filters, against the reference values;
* field contracts of every kind, a regex Java refuses, and a range over text
  holding ``NaN`` and malformed numbers;
* a statement that outruns ``timeout_seconds``: the warehouse's
  ``STATEMENT_TIMEOUT`` or the client-side cancel ends it.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from tripl.core.adapters import databricks_auth, databricks_sql
from tripl.core.adapters.base import AggregateSpec, FieldContractExpectation
from tripl.core.adapters.databricks import DatabricksAdapter
from tripl.core.adapters.databricks_sql import Params, quote_ident
from tripl.core.adapters.databricks_types import declared_struct_paths, parse_spark_type
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.measure_validator import (
    SqlDialect,
    dialect_for_db_type,
    lint_dialect_sql,
    quote_identifier,
    quote_sql_literal,
    quote_timestamp_literal,
    validate_select_sql_safety,
)
from tripl.core.adapters.multi_aggregate_sql import compile_time_bucketed_multi_aggregate_sql
from tripl.core.adapters.registry import build_adapter, supported_db_types
from tripl.core.bucketing import floor_to_bucket
from tripl.core.warehouse_types import ComplexKind, TimeKind, classify_complex, classify_time
from tripl.crypto import encrypt_value
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import MetricAggregation
from tripl.schemas.data_source import (
    ConnectionSettingsError,
    DatabricksSettings,
    parse_connection_settings,
)
from tripl.services.fact_table_introspection_service import bucket_warehouse_type
from tripl.services.safe_http import HttpResponse

_FROM = datetime(2026, 4, 1, tzinfo=UTC)
_TO = datetime(2026, 4, 2, tzinfo=UTC)
_BASE = "SELECT time, event_name, amount FROM events"
_TYPES = {"time": "timestamp", "event_name": "string", "amount": "double"}


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn
        self.description: list[tuple[object, ...]] = []
        self.cancelled = False
        self.closed = False

    def execute(self, sql: str, parameters: dict[str, str] | None = None) -> None:
        self._conn.statements.append((sql, dict(parameters or {})))
        if self._conn.block is not None:
            # Stands in for a long statement: returns only once cancelled.
            self._conn.block.wait(5)
            if self.cancelled:
                raise RuntimeError("Operation canceled by the client")
        if self._conn.fail_on is not None and self._conn.fail_on in sql:
            raise RuntimeError("boom")
        answer = self._conn.answers.pop(0) if self._conn.answers else ([], [])
        self.description = [(name,) for name in answer[0]]
        self._rows = answer[1]

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)

    def cancel(self) -> None:
        self.cancelled = True
        if self._conn.block is not None:
            self._conn.block.set()

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, dict[str, str]]] = []
        self.answers: list[tuple[list[str], list[tuple[object, ...]]]] = []
        self.block: threading.Event | None = None
        self.fail_on: str | None = None
        self.cursors: list[FakeCursor] = []

    def cursor(self) -> FakeCursor:
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        return None

    @property
    def sql(self) -> list[str]:
        return [sql for sql, _params in self.statements]


def make_adapter(types: dict[str, str] | None = None) -> tuple[DatabricksAdapter, FakeConnection]:
    conn = FakeConnection()
    adapter = object.__new__(DatabricksAdapter)
    adapter._conn = conn
    column_types = dict(types or _TYPES)
    adapter._column_types = column_types
    adapter._allowed_columns = set(column_types)
    adapter._struct_paths = {
        name: declared_struct_paths(type_name)
        for name, type_name in column_types.items()
        if classify_complex(type_name) is ComplexKind.struct
    }
    adapter._catalog = "main"
    adapter._schema = "analytics"
    return adapter, conn


# --------------------------------------------------------------------------- #
# introspection
# --------------------------------------------------------------------------- #


def test_get_columns_reads_full_types_from_describe_query() -> None:
    adapter, conn = make_adapter({})
    conn.answers = [
        (
            ["col_name", "data_type", "comment"],
            [
                ("time", "timestamp", None),
                ("props", "struct<user:struct<id:bigint>,`odd name`:string>", None),
                ("tags", "array<string>", None),
                ("doc", "variant", None),
            ],
        )
    ]

    columns = adapter.get_columns(_BASE)

    assert conn.sql == [f"DESCRIBE QUERY SELECT * FROM ({_BASE}) AS _src"]
    assert [(c.name, c.type_name) for c in columns][:2] == [
        ("time", "timestamp"),
        ("props", "struct<user:struct<id:bigint>,`odd name`:string>"),
    ]
    assert adapter._struct_paths == {"props": {"odd name": True, "user.id": True}}
    assert adapter._allowed_columns == {"time", "props", "tags", "doc"}


def test_spark_type_parser_reads_nesting_names_and_modifiers() -> None:
    parsed = parse_spark_type("struct<a:decimal(10,2) NOT NULL,b:map<string,array<int>>>")
    assert parsed.kind == "struct"
    assert [name for name, _ in parsed.fields] == ["a", "b"]
    assert parsed.fields[1][1].kind == "map"
    # A struct under an array is listed but cannot be addressed with dots.
    assert declared_struct_paths("struct<items:array<struct<sku:string>>,n:int>") == {
        "items.sku": False,
        "n": True,
    }
    # Garbage is an opaque scalar, never an exception.
    assert parse_spark_type("struct<").kind == "scalar"


def test_schema_browse_is_one_statement_with_bound_schema_names() -> None:
    adapter, conn = make_adapter()
    adapter._schema_allowlist = ("marts", "analytics")
    conn.answers = [
        (
            [],
            [
                ("analytics", "events", "time", "TIMESTAMP"),
                ("marts", "orders", "id", "BIGINT"),
            ],
        )
    ]

    tables = adapter.get_schema_tables()

    sql, params = conn.statements[0]
    assert "FROM `main`.information_schema.columns" in sql
    assert "WHERE table_schema IN (:p0, :p1)" in sql
    assert params == {"p0": "analytics", "p1": "marts"}
    # Bare in the default schema, schema.table elsewhere in the catalog.
    assert [t.name for t in tables] == ["events", "marts.orders"]


def test_test_connection_runs_a_trivial_select() -> None:
    adapter, conn = make_adapter()
    conn.answers = [(["ok"], [(1,)])]
    assert adapter.test_connection() is True
    assert conn.sql == ["SELECT 1 AS ok"]


# --------------------------------------------------------------------------- #
# time: buckets, literals, UTC
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("15m", "timestamp_seconds(floor((unix_seconds(CAST(`time` AS TIMESTAMP))) / 900) * 900)"),
        ("1h", "date_trunc('HOUR', CAST(`time` AS TIMESTAMP))"),
        (
            "6h",
            "timestamp_seconds(floor((unix_seconds(CAST(`time` AS TIMESTAMP))) / 21600) * 21600)",
        ),
        ("1d", "date_trunc('DAY', CAST(`time` AS TIMESTAMP))"),
        ("1w", "date_trunc('WEEK', CAST(`time` AS TIMESTAMP))"),
    ],
)
def test_bucket_sql_for_every_interval(code: str, expected: str) -> None:
    adapter, _conn = make_adapter()
    assert adapter._bucket_expression("time", code) == expected


@pytest.mark.parametrize("code", ["15m", "6h"])
def test_epoch_arithmetic_agrees_with_floor_to_bucket(code: str) -> None:
    """The arithmetic the SQL spells, executed in Python, is the contract's floor."""
    width = {"15m": 900, "6h": 21600}[code]
    moment = datetime(2026, 7, 12, 14, 37, 42, 123456, tzinfo=UTC)
    seconds = int(moment.timestamp())  # unix_seconds truncates the fraction
    floored = datetime.fromtimestamp((seconds // width) * width, tz=UTC)
    assert floored == floor_to_bucket(moment, code)


def test_a_date_column_refuses_a_sub_day_interval() -> None:
    adapter, _conn = make_adapter({"day": "date"})
    with pytest.raises(WarehouseCapabilityError, match="DATE"):
        adapter._bucket_expression("day", "1h")
    assert adapter._bucket_expression("day", "1d") == "date_trunc('DAY', CAST(`day` AS TIMESTAMP))"


def test_window_literals_follow_the_column_type_family() -> None:
    adapter, _conn = make_adapter({"ts": "timestamp", "ntz": "timestamp_ntz", "d": "date"})
    moment = datetime(2026, 4, 1, 1, 2, 3, tzinfo=UTC)
    assert adapter._time_literal("ts", moment) == "TIMESTAMP '2026-04-01 01:02:03.000000+00:00'"
    assert adapter._time_literal("ntz", moment) == "TIMESTAMP_NTZ '2026-04-01 01:02:03.000000'"
    assert adapter._time_literal("d", moment) == "DATE '2026-04-01'"


def test_a_time_column_without_a_date_is_refused_with_an_actionable_message() -> None:
    adapter, _conn = make_adapter({"t": "interval day to second"})
    with pytest.raises(WarehouseCapabilityError, match="TIMESTAMP_NTZ or DATE"):
        adapter._time_literal("t", _FROM)


def test_buckets_come_back_as_aware_utc() -> None:
    adapter, conn = make_adapter()
    conn.answers = [([], [(datetime(2026, 4, 1), "click", 3)])]
    _cols, _json, rows = adapter.get_time_bucketed_counts(
        _BASE, "time", "1d", ["event_name"], [], None, _FROM, _TO
    )
    assert rows == [(datetime(2026, 4, 1, tzinfo=UTC), "click", 3)]


# --------------------------------------------------------------------------- #
# bound parameters
# --------------------------------------------------------------------------- #


def test_top_n_values_are_bound_never_inlined() -> None:
    adapter, conn = make_adapter()
    hostile = "x') OR 1=1 --"
    conn.answers = [([], [("event_name", hostile)]), ([], [])]

    adapter.get_time_bucketed_multi_aggregate_breakdown(
        _BASE,
        "time",
        "1d",
        "event_name",
        [AggregateSpec(key="c", aggregation=MetricAggregation.count)],
        _FROM,
        _TO,
        values_limit=3,
    )

    pre_query, pre_params = conn.statements[0]
    assert "ORDER BY _cnt DESC, _breakdown_value) AS rn" in pre_query
    assert "WHERE rn <= 2" in pre_query
    assert pre_params == {"p0": "event_name"}
    statement, params = conn.statements[1]
    assert hostile not in statement
    assert "IN (:p0)" in statement
    assert params == {"p0": hostile}


def test_contract_values_and_the_group_filter_are_bound() -> None:
    adapter, conn = make_adapter()
    conn.answers = [([], []), ([], [(1, 10, "buy", 0, 10, None)])]
    expectations = [
        FieldContractExpectation(
            field_name="event_name",
            drift_type="enum_violation",
            threshold=0.0,
            enum_options=("click", "view"),
        ),
        FieldContractExpectation(
            field_name="event_name", drift_type="regex_violation", threshold=0.0, regex="^[a-z]+$"
        ),
    ]

    violations = adapter.validate_field_contracts(
        _BASE,
        expectations,
        time_column="time",
        time_from=_FROM,
        time_to=_TO,
        group_column="event_name",
        group_value="checkout's",
    )

    probe, probe_params = conn.statements[0]
    assert probe == "SELECT '' RLIKE :p0"
    assert probe_params == {"p0": "^[a-z]+$"}
    sql, params = conn.statements[1]
    assert "checkout's" not in sql and "click" not in sql and "^[a-z]+$" not in sql
    assert params == {"p0": "checkout's", "p1": "click", "p2": "view", "p3": "^[a-z]+$"}
    assert "NOT IN (:p1, :p2)" in sql
    assert "RLIKE :p3" in sql
    assert sql.count(f"FROM ({_BASE}) AS _src") == 1
    assert [v.drift_type for v in violations] == ["enum_violation"]
    assert violations[0].bad_rate == 0.1


def test_a_range_contract_parses_without_raising_and_keeps_nan_out() -> None:
    adapter, conn = make_adapter()
    adapter.validate_field_contracts(
        _BASE,
        [
            FieldContractExpectation(
                field_name="amount",
                drift_type="range_violation",
                threshold=0.0,
                min_value=0.0,
                max_value=10.0,
            )
        ],
    )
    sql = conn.sql[0]
    assert "try_cast(coalesce(CAST(`amount` AS STRING), '') AS DOUBLE)" in sql
    assert "NOT isnan(" in sql
    assert "< 0.0" in sql and "> 10.0" in sql


def test_a_contract_on_a_dropped_column_is_skipped_and_recorded() -> None:
    adapter, conn = make_adapter()
    gone = FieldContractExpectation(
        field_name="removed", drift_type="required_null_violation", threshold=0.0
    )
    assert adapter.validate_field_contracts(_BASE, [gone]) == []
    assert conn.statements == []
    assert adapter.take_skipped_field_contracts() == [gone]


# --------------------------------------------------------------------------- #
# nested columns
# --------------------------------------------------------------------------- #

_NESTED = {
    **_TYPES,
    "doc": "variant",
    "st": "struct<a:int,b:struct<c:string>>",
    "m": "map<string,string>",
    "tags": "array<string>",
}


def test_nested_shapes_and_values_per_kind() -> None:
    adapter, conn = make_adapter(_NESTED)
    conn.answers = [
        (
            [],
            [
                (
                    datetime(2026, 4, 1),
                    '["x","y"]',
                    '["a","b"]',
                    '["k"]',
                    None,
                    '"u1"',
                    "1",
                    '"v"',
                    7,
                )
            ],
        )
    ]

    cols, value_names, rows = adapter.get_time_bucketed_counts(
        _BASE,
        "time",
        "1d",
        ["tags"],
        ["doc", "m", "st"],
        {"doc": ["user.id"], "st": ["a"], "m": ["k"]},
        _FROM,
        _TO,
    )

    sql = conn.sql[0]
    assert "json_object_keys(to_json(`doc`))" in sql
    assert "transform(map_keys(`m`), _k -> CAST(_k AS STRING))" in sql
    assert "to_json(`doc`:['user']['id'])" in sql
    assert "try_element_at(`m`, 'k')" in sql
    assert "`st`.`a`" in sql
    # Grouped by the prepared aliases only: no expression is repeated in GROUP BY.
    assert "GROUP BY _bucket, `tags`, __np_0, __np_1, __np_2, __nv_0, __nv_1, __nv_2" in sql
    assert cols == ["tags", "doc", "m", "st"]
    assert sorted(value_names) == ["doc.user.id", "m.k", "st.a"]
    # Key lists decode to lists, the STRUCT gets its declared paths, ARRAY decodes.
    assert rows[0][1:5] == (["x", "y"], ["a", "b"], ["k"], ["a", "b.c"])


def test_property_extraction_and_its_limits() -> None:
    adapter, _conn = make_adapter(_NESTED)
    assert adapter._property_value_expression("doc", "a.b") == "CAST(`doc`:['a']['b'] AS STRING)"
    assert adapter._property_value_expression("st", "b.c") == "CAST(`st`.`b`.`c` AS STRING)"
    with pytest.raises(ValueError, match="one level of keys"):
        adapter._property_value_expression("m", "a.b")
    with pytest.raises(ValueError, match="not a declared field"):
        adapter._property_value_expression("st", "zzz")
    with pytest.raises(ValueError, match="scalar type"):
        adapter._property_value_expression("event_name", "a")
    with pytest.raises(ValueError, match="Unsupported JSON path"):
        adapter._property_value_expression("doc", "a'b")


def test_json_string_source_keeps_column_order_and_parses_safely() -> None:
    adapter, conn = make_adapter({})
    conn.answers = [
        (
            [],
            [("time", "timestamp", None), ("props", "string", None), ("n", "int", None)],
        )
    ]
    adapter.get_columns(_BASE)

    wrapped = adapter.json_string_source(_BASE, ["props"])

    assert len(conn.statements) == 1, "the column list just read is reused"
    assert wrapped == (
        "SELECT `time`, CASE WHEN startswith(schema_of_variant(try_parse_json(`props`)), "
        "'OBJECT') THEN try_parse_json(`props`) END AS `props`, `n` "
        f"FROM ({_BASE}) AS _json_src"
    )
    with pytest.raises(ValueError, match="invalid column name"):
        adapter.json_string_source(_BASE, ["a.b"])


# --------------------------------------------------------------------------- #
# aggregates
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (
            AggregateSpec("c", MetricAggregation.count, filter_sql="x > 0"),
            "NULLIF(count_if(x > 0), 0)",
        ),
        (
            AggregateSpec("d", MetricAggregation.count_distinct, "event_name", "x > 0"),
            "CASE WHEN count_if(x > 0) = 0 THEN NULL "
            "ELSE count(DISTINCT IF(x > 0, `event_name`, NULL)) END",
        ),
        (
            AggregateSpec("s", MetricAggregation.sum, "amount", "x > 0"),
            "sum(CASE WHEN x > 0 THEN `amount` END)",
        ),
        (AggregateSpec("a", MetricAggregation.avg, "amount"), "avg(`amount`)"),
        (
            AggregateSpec("u", MetricAggregation.count_distinct, "event_name"),
            "count(DISTINCT `event_name`)",
        ),
    ],
)
def test_conditional_aggregates(spec: AggregateSpec, expected: str) -> None:
    adapter, _conn = make_adapter()
    assert adapter._spec_aggregate_sql(spec) == expected


def test_disclosed_batch_sql_is_the_executed_statement() -> None:
    specs = [AggregateSpec("c", MetricAggregation.count)]
    _cols, disclosed = compile_time_bucketed_multi_aggregate_sql(
        db_type="databricks",
        base_query=_BASE,
        time_column="time",
        interval="1d",
        specs=specs,
        time_from=_FROM,
        time_to=_TO,
        column_types={"time": "timestamp_ntz", "amount": "double"},
    )
    assert "TIMESTAMP_NTZ '2026-04-01 00:00:00.000000'" in disclosed

    adapter, conn = make_adapter({"time": "timestamp_ntz", "amount": "double"})
    adapter.get_time_bucketed_multi_aggregate(_BASE, "time", "1d", specs, _FROM, _TO)
    assert conn.sql == [disclosed]


def test_aggregate_breakdown_groups_by_the_folded_value_only() -> None:
    adapter, conn = make_adapter()
    conn.answers = [([], [("event_name", "click")]), ([], [])]
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
        values_limit=3,
    )
    sql = conn.sql[-1]
    assert "GROUP BY _bucket, _breakdown_value, _is_other ORDER BY" in sql
    assert "ELSE 'Other' END AS `event_name`" in sql


# --------------------------------------------------------------------------- #
# timeouts
# --------------------------------------------------------------------------- #


def test_a_statement_past_the_deadline_is_cancelled_and_reported() -> None:
    adapter, conn = make_adapter()
    adapter._timeout_seconds = 0.05
    conn.block = threading.Event()

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="timeout configured for this data source"):
        adapter.test_connection()

    assert time.monotonic() - started < 5
    assert conn.cursors[0].cancelled is True
    assert conn.cursors[0].closed is True


def test_schema_browse_is_capped_below_the_source_timeout() -> None:
    adapter, _conn = make_adapter()
    adapter._timeout_seconds = 300.0
    assert adapter._query_deadline(30) == 30
    adapter._timeout_seconds = 10.0
    assert adapter._query_deadline(30) == 10


# --------------------------------------------------------------------------- #
# the factory, settings and the outbound rule
# --------------------------------------------------------------------------- #


def _source(**settings: object) -> DataSource:
    return DataSource(
        name="dbx",
        db_type="databricks",
        host="dbc-a1b2c3d4-e5f6.cloud.databricks.com",
        port=443,
        database_name="main",
        username="",
        password_encrypted=encrypt_value("dapi-secret"),
        timeout_seconds=120,
        extra_params={"http_path": "/sql/1.0/warehouses/abc123", **settings},
    )


class _FakeDriver:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    def connect(self, **kwargs: object) -> FakeConnection:
        self.kwargs = kwargs
        return FakeConnection()


def test_registry_lists_databricks() -> None:
    assert "databricks" in supported_db_types()
    assert dialect_for_db_type("databricks") is SqlDialect.databricks


def test_factory_wires_settings_timeout_and_a_pat(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = _FakeDriver()
    monkeypatch.setattr(databricks_sql, "import_driver", lambda: driver)

    adapter = build_adapter(_source(schema_name="analytics", schema_allowlist=["marts"]))

    assert isinstance(adapter, DatabricksAdapter)
    kwargs = driver.kwargs
    assert kwargs["server_hostname"] == "dbc-a1b2c3d4-e5f6.cloud.databricks.com"
    assert kwargs["http_path"] == "/sql/1.0/warehouses/abc123"
    assert kwargs["access_token"] == "dapi-secret"
    assert kwargs["catalog"] == "main"
    assert kwargs["schema"] == "analytics"
    assert kwargs["session_configuration"] == {"TIMEZONE": "UTC", "STATEMENT_TIMEOUT": "120"}
    assert kwargs["_socket_timeout"] == 120.0
    assert kwargs["use_cloud_fetch"] is False
    assert kwargs["enable_telemetry"] is False
    assert adapter._schema_allowlist == ("marts",)


def test_no_default_schema_leaves_the_session_in_the_catalogs_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A catalog without a ``default`` schema (``samples``) must still connect."""
    driver = _FakeDriver()
    monkeypatch.setattr(databricks_sql, "import_driver", lambda: driver)
    adapter = build_adapter(_source())
    assert driver.kwargs["schema"] is None
    assert isinstance(adapter, DatabricksAdapter)
    assert adapter._schemas_in_scope() == ["default"]


def test_oauth_m2m_needs_a_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(databricks_sql, "import_driver", _FakeDriver)
    with pytest.raises(WarehouseCapabilityError, match="client ID"):
        build_adapter(_source(auth_type="oauth_m2m"))


def test_oauth_m2m_exchanges_client_credentials_through_safe_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _FakeDriver()
    monkeypatch.setattr(databricks_sql, "import_driver", lambda: driver)
    sent: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def fake_send(
        method: str, url: str, headers: dict[str, str], body: bytes | None, **_kw: object
    ) -> HttpResponse:
        sent.append((method, url, headers, body))
        return HttpResponse(200, json.dumps({"access_token": "tok", "expires_in": 3600}).encode())

    from tripl.services import safe_http

    monkeypatch.setattr(safe_http, "send", fake_send)
    source = _source(auth_type="oauth_m2m")
    source.username = "client-123"

    build_adapter(source)
    provider = driver.kwargs["credentials_provider"]
    assert "access_token" not in driver.kwargs
    header_factory = provider()  # type: ignore[operator]
    assert header_factory() == {"Authorization": "Bearer tok"}
    assert header_factory() == {"Authorization": "Bearer tok"}

    assert len(sent) == 1, "the token is cached until it nears expiry"
    method, url, headers, body = sent[0]
    assert (method, url) == ("POST", "https://dbc-a1b2c3d4-e5f6.cloud.databricks.com/oidc/v1/token")
    assert headers["Authorization"].startswith("Basic ")
    assert body == b"grant_type=client_credentials&scope=all-apis"


def test_a_refused_token_request_is_an_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from tripl.services import safe_http

    monkeypatch.setattr(safe_http, "send", lambda *_a, **_k: HttpResponse(401, b"{}"))
    provider = databricks_auth.ServicePrincipalCredentials("h", "id", "secret", timeout=5)
    with pytest.raises(WarehouseCapabilityError, match="client ID and secret"):
        provider.token()


def test_public_hosts_only_requires_a_databricks_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    from tripl.config import settings

    monkeypatch.setattr(settings, "outbound_public_hosts_only", True)
    with pytest.raises(WarehouseCapabilityError, match="Databricks workspace hostname"):
        databricks_auth.check_workspace_host("warehouse.internal.example")
    databricks_auth.check_workspace_host("adb-123.4.azuredatabricks.net")

    monkeypatch.setattr(settings, "outbound_public_hosts_only", False)
    databricks_auth.check_workspace_host("warehouse.internal.example")


@pytest.mark.parametrize("public_only", [True, False])
@pytest.mark.parametrize(
    "host",
    [
        "evil.com:80.cloud.databricks.com",
        "https://dbc-1.cloud.databricks.com",
        "dbc-1.cloud.databricks.com/sql",
        "[::1]",
        "dbc-1.cloud.databricks.com:443",
    ],
)
def test_the_host_must_be_a_bare_hostname(
    monkeypatch: pytest.MonkeyPatch, host: str, public_only: bool
) -> None:
    from tripl.config import settings

    monkeypatch.setattr(settings, "outbound_public_hosts_only", public_only)
    with pytest.raises(WarehouseCapabilityError, match="hostname alone"):
        databricks_auth.check_workspace_host(host)


def test_public_hosts_only_refuses_a_private_address(monkeypatch: pytest.MonkeyPatch) -> None:
    from tripl.config import settings

    monkeypatch.setattr(settings, "outbound_public_hosts_only", True)
    monkeypatch.setattr(databricks_sql, "import_driver", _FakeDriver)
    source = _source()
    source.host = "10.0.0.5.cloud.databricks.com"
    monkeypatch.setattr(
        "tripl.services.safe_http._resolve",
        lambda *_a, **_k: [(None, None, None, None, ("10.0.0.5", 443))],
    )
    with pytest.raises(WarehouseCapabilityError, match="private or internal address"):
        build_adapter(source)


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"http_path": "https://dbc.cloud.databricks.com/sql/1.0/warehouses/x"},
        {"http_path": "/sql/1.0/warehouses/x y"},
        {"http_path": "/sql/1.0/warehouses/x", "auth_type": "password"},
        {"http_path": "/sql/1.0/warehouses/x", "schema_allowlist": ["ok", "bad name"]},
        {"http_path": "/sql/1.0/warehouses/x", "location": "EU"},
    ],
)
def test_malformed_settings_are_rejected(raw: dict[str, object]) -> None:
    with pytest.raises(ConnectionSettingsError):
        parse_connection_settings("databricks", raw)


def test_settings_are_trimmed_and_deduplicated() -> None:
    parsed = DatabricksSettings.model_validate(
        {
            "http_path": " /sql/1.0/warehouses/abc ",
            "schema_name": " analytics ",
            "schema_allowlist": ["marts", " marts", ""],
        }
    )
    assert parsed.http_path == "/sql/1.0/warehouses/abc"
    assert parsed.schema_name == "analytics"
    assert parsed.schema_allowlist == ["marts"]


# --------------------------------------------------------------------------- #
# dialect helpers and the read-only gate
# --------------------------------------------------------------------------- #


def test_dialect_rendering_helpers() -> None:
    assert quote_ident("we`ird") == "`we``ird`"
    assert quote_identifier("t.order", SqlDialect.databricks) == "`t`.`order`"
    assert quote_sql_literal("o'brien\\", SqlDialect.databricks) == "'o\\'brien\\\\'"
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    assert (
        quote_timestamp_literal(moment, SqlDialect.databricks, kind=TimeKind.timestamp)
        == "TIMESTAMP '2026-01-01 00:00:00.000000+00:00'"
    )
    assert (
        quote_timestamp_literal(moment, SqlDialect.databricks, kind=TimeKind.date)
        == "DATE '2026-01-01'"
    )
    params = Params()
    assert [params.bind("a"), params.bind("b")] == [":p0", ":p1"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT toStartOfInterval(ts, INTERVAL 1 DAY) FROM t",
        "SELECT TIMESTAMP_TRUNC(ts, DAY) FROM t",
        "SELECT countIf(x > 1) FROM t",
        "SELECT date_bin(INTERVAL '1 day', ts, TIMESTAMP '1970-01-01') FROM t",
    ],
)
def test_other_engines_functions_are_flagged_before_they_reach_the_warehouse(sql: str) -> None:
    assert lint_dialect_sql(sql, SqlDialect.databricks) is not None


def test_databricks_sql_is_not_flagged() -> None:
    sql = "SELECT date_trunc('DAY', ts) AS d, count_if(x > 1) FROM main.analytics.events"
    assert lint_dialect_sql(sql, SqlDialect.databricks) is None


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t SELECT * FROM s",
        "INSERT OVERWRITE t SELECT * FROM s",
        "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE",
        "COPY INTO t FROM '/Volumes/main/raw/files'",
        "CREATE OR REPLACE TABLE t AS SELECT 1",
        "ALTER TABLE t ADD COLUMN c INT",
        "DROP TABLE t",
        "OPTIMIZE t ZORDER BY (c)",
        "VACUUM t RETAIN 0 HOURS",
        "SET spark.sql.ansi.enabled = false",
        "USE CATALOG other",
        "REFRESH TABLE t",
        "CACHE SELECT * FROM t",
        "RESTORE TABLE t TO VERSION AS OF 1",
        "MSCK REPAIR TABLE t",
        "WITH s AS (SELECT 1) INSERT INTO t SELECT * FROM s",
        "SELECT 1; DROP TABLE t",
        "SELECT * FROM t; SET x = 1",
    ],
)
def test_the_read_only_gate_refuses_databricks_writes(sql: str) -> None:
    with pytest.raises(ValueError):
        validate_select_sql_safety(sql)


def test_the_read_only_gate_admits_databricks_reads() -> None:
    sql = (
        "SELECT event_time, raw:user.id AS user_id FROM main.analytics.events "
        "WHERE event_name = 'Delete Account' AND event_time >= current_timestamp() - INTERVAL 1 DAY"
    )
    assert validate_select_sql_safety(sql) == sql


def test_type_classification_knows_databricks_spellings() -> None:
    assert classify_complex("variant") is ComplexKind.json
    assert classify_complex("struct<a:int>") is ComplexKind.struct
    assert classify_complex("map<string,string>") is ComplexKind.map
    assert classify_complex("array<struct<a:int>>") is None
    # ClickHouse's Variant is a union of scalars, not a document.
    assert classify_complex("Variant(String, UInt64)") is None
    assert classify_time("timestamp_ntz") is TimeKind.timestamp
    assert classify_time("array<timestamp>") is TimeKind.unsupported
    assert bucket_warehouse_type("tinyint") == "number"
    assert bucket_warehouse_type("decimal(10,2)") == "number"
    assert bucket_warehouse_type("timestamp_ntz") == "timestamp"


def test_window_bounds_survive_a_naive_input() -> None:
    adapter, _conn = make_adapter()
    naive = datetime(2026, 4, 1, 3, 0)
    aware = naive.replace(tzinfo=UTC) + timedelta(0)
    assert adapter._time_condition("time", naive, naive) == adapter._time_condition(
        "time", aware, aware
    )
