"""The Snowflake adapter against a fake driver: the SQL it sends, and what it reads back.

Nothing here contacts Snowflake. The adapter is the real class entered through
``object.__new__`` (no connection, no credentials), with a fake DB-API
connection that records every ``(sql, parameters)`` pair and answers canned
rows. What a SQL string proves is shape, not validity: the statements are only
executed by the value conformance suite against a real account
(``test_snowflake_value_conformance.py``).

What a live check must exercise, because nothing here can:

* connection test with a password and with a key pair, and the error a wrong
  account, warehouse or key gives;
* the describe-only column read, the schema browse over the default schema
  plus an allowlist, and preview rows (VARIANT values arrive as JSON text);
* every interval code over ``TIMESTAMP_NTZ``, ``TIMESTAMP_LTZ``,
  ``TIMESTAMP_TZ`` and ``DATE`` time columns, compared to ``floor_to_bucket``;
* numeric parameters (``:1``) in ``IN (...)``, ``REGEXP_INSTR`` and equality;
* a scan over ``VARIANT`` and ``OBJECT`` columns: shapes, nested values, and
  ``json_string_source`` over a STRING column;
* top-N breakdowns (``GROUPING SETS`` + ``ROW_NUMBER``), aggregates and
  multi-aggregates with filters, against the reference values;
* field contracts of every kind, and a statement past ``timeout_seconds``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tripl.core.adapters import snowflake_sql
from tripl.core.adapters.base import AggregateSpec, FieldContractExpectation
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
from tripl.core.adapters.snowflake import SnowflakeAdapter
from tripl.core.adapters.snowflake_sql import Params, resolve_host
from tripl.core.bucketing import WEEK_ORIGIN, floor_to_bucket
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
    "TIME": "TIMESTAMP_NTZ",
    "EVENT_NAME": "STRING",
    "AMOUNT": "FLOAT",
    "PROPS": "VARIANT",
}


@dataclass
class Meta:
    """The fields of the driver's ``ResultMetadata`` the adapter reads."""

    name: str
    type_code: int
    precision: int | None = None
    scale: int | None = None
    is_nullable: bool | None = True


class DriverError(Exception):
    def __init__(self, msg: str, errno: int) -> None:
        super().__init__(msg)
        self.errno = errno


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn
        self.description: list[tuple[object, ...]] = []
        self.closed = False
        self._rows: list[tuple[object, ...]] = []

    def execute(
        self, sql: str, params: list[str] | None = None, timeout: int | None = None
    ) -> None:
        self._conn.statements.append((sql, list(params or [])))
        self._conn.timeouts.append(timeout)
        if self._conn.raise_errno is not None:
            raise DriverError("SQL execution canceled", self._conn.raise_errno)
        if self._conn.refused_param is not None and self._conn.refused_param in (params or []):
            raise DriverError("Invalid regular expression", 100048)
        answer = self._conn.answers.pop(0) if self._conn.answers else ([], [])
        self.description = [(name,) for name in answer[0]]
        self._rows = answer[1]

    def describe(self, sql: str, timeout: int | None = None) -> list[Meta]:
        self._conn.statements.append((sql, []))
        self._conn.timeouts.append(timeout)
        return list(self._conn.metadata)

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, list[str]]] = []
        self.timeouts: list[int | None] = []
        self.answers: list[tuple[list[str], list[tuple[object, ...]]]] = []
        self.metadata: list[Meta] = []
        self.raise_errno: int | None = None
        self.refused_param: str | None = None
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


def make_adapter(types: dict[str, str] | None = None) -> tuple[SnowflakeAdapter, FakeConnection]:
    conn = FakeConnection()
    adapter = object.__new__(SnowflakeAdapter)
    adapter._conn = conn
    column_types = dict(types or _TYPES)
    adapter._column_types = column_types
    adapter._allowed_columns = set(column_types)
    adapter._database = "ANALYTICS"
    adapter._schema = "PUBLIC"
    return adapter, conn


# --------------------------------------------------------------------------- #
# introspection
# --------------------------------------------------------------------------- #


def test_get_columns_describes_without_executing() -> None:
    adapter, conn = make_adapter({})
    conn.metadata = [
        Meta("TIME", 8),
        Meta("EVENT_NAME", 2, is_nullable=False),
        Meta("AMOUNT", 0, precision=38, scale=2),
        Meta("PROPS", 5),
        Meta("TAGS", 10),
        Meta("SEEN_AT", 7),
    ]

    columns = adapter.get_columns(_BASE)

    assert conn.sql == [f"SELECT * FROM ({_BASE}) AS _src"]
    assert [(c.name, c.type_name, c.is_nullable) for c in columns] == [
        ("TIME", "TIMESTAMP_NTZ", True),
        ("EVENT_NAME", "STRING", False),
        ("AMOUNT", "NUMBER(38,2)", True),
        ("PROPS", "VARIANT", True),
        ("TAGS", "ARRAY", True),
        ("SEEN_AT", "TIMESTAMP_TZ", True),
    ]
    assert adapter._allowed_columns == {"TIME", "EVENT_NAME", "AMOUNT", "PROPS", "TAGS", "SEEN_AT"}


def test_schema_browse_is_one_statement_with_bound_schema_names() -> None:
    adapter, conn = make_adapter()
    adapter._schema_allowlist = ("marts", "Mixed-Case")
    conn.answers = [
        (
            [],
            [
                ("PUBLIC", "EVENTS", "TIME", "TIMESTAMP_NTZ"),
                ("PUBLIC", "EVENTS", "PROPS", "VARIANT"),
                ("MARTS", "DAILY", "DAY", "DATE"),
            ],
        )
    ]

    tables = adapter.get_schema_tables()

    sql, params = conn.statements[0]
    assert "FROM ANALYTICS.INFORMATION_SCHEMA.COLUMNS" in sql
    assert "WHERE table_schema IN (:1, :2, :3)" in sql
    # Unquoted names are stored upper-cased; a name that needs quoting is exact.
    assert params == ["PUBLIC", "Mixed-Case", "MARTS"]
    assert [(t.name, [c.name for c in t.columns]) for t in tables] == [
        ("EVENTS", ["TIME", "PROPS"]),
        ("MARTS.DAILY", ["DAY"]),
    ]


def test_test_connection_runs_a_trivial_select() -> None:
    adapter, conn = make_adapter()
    conn.answers = [(["OK"], [(Decimal(1),)])]
    assert adapter.test_connection() is True
    assert conn.sql == ["SELECT 1 AS ok"]


# --------------------------------------------------------------------------- #
# time: buckets, literals, UTC
# --------------------------------------------------------------------------- #

_WEEK = int(WEEK_ORIGIN.timestamp())


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (
            "15m",
            'TO_TIMESTAMP_NTZ(FLOOR((DATE_PART(EPOCH_SECOND, "TIME")) / 900) * 900)',
        ),
        ("1h", "DATE_TRUNC('HOUR', \"TIME\")"),
        (
            "6h",
            'TO_TIMESTAMP_NTZ(FLOOR((DATE_PART(EPOCH_SECOND, "TIME")) / 21600) * 21600)',
        ),
        ("1d", "DATE_TRUNC('DAY', \"TIME\")"),
        (
            "1w",
            f'TO_TIMESTAMP_NTZ(FLOOR((DATE_PART(EPOCH_SECOND, "TIME") - {_WEEK}) / 604800) '
            f"* 604800 + {_WEEK})",
        ),
    ],
)
def test_bucket_sql_for_every_interval(code: str, expected: str) -> None:
    adapter, _conn = make_adapter()
    assert adapter._bucket_expression("TIME", code) == expected


@pytest.mark.parametrize("code", ["15m", "6h", "1w"])
def test_epoch_arithmetic_agrees_with_floor_to_bucket(code: str) -> None:
    """The arithmetic the SQL spells, executed in Python, is the contract's floor."""
    width = {"15m": 900, "6h": 21600, "1w": 604800}[code]
    origin = _WEEK if code == "1w" else 0
    moment = datetime(2026, 7, 12, 14, 37, 42, 123456, tzinfo=UTC)
    seconds = int(moment.timestamp())  # EPOCH_SECOND truncates the fraction
    floored = datetime.fromtimestamp((seconds - origin) // width * width + origin, tz=UTC)
    assert floored == floor_to_bucket(moment, code)


def test_zoned_and_date_columns_bucket_on_the_utc_wall_clock() -> None:
    adapter, _conn = make_adapter({"TS": "TIMESTAMP_TZ", "LTZ": "TIMESTAMP_LTZ", "D": "DATE"})
    assert adapter._bucket_expression("TS", "1d") == (
        "DATE_TRUNC('DAY', TO_TIMESTAMP_NTZ(CONVERT_TIMEZONE('UTC', \"TS\")))"
    )
    assert adapter._bucket_expression("LTZ", "1h") == (
        "DATE_TRUNC('HOUR', TO_TIMESTAMP_NTZ(CONVERT_TIMEZONE('UTC', \"LTZ\")))"
    )
    assert adapter._bucket_expression("D", "1d") == "DATE_TRUNC('DAY', TO_TIMESTAMP_NTZ(\"D\"))"
    with pytest.raises(WarehouseCapabilityError, match="DATE"):
        adapter._bucket_expression("D", "1h")


def test_window_literals_follow_the_column_type_family() -> None:
    adapter, _conn = make_adapter({"NTZ": "TIMESTAMP_NTZ", "TZ": "TIMESTAMP_TZ", "D": "DATE"})
    moment = datetime(2026, 4, 1, 1, 2, 3, tzinfo=UTC)
    assert adapter._time_literal("NTZ", moment) == (
        "TO_TIMESTAMP_NTZ('2026-04-01 01:02:03.000000', 'YYYY-MM-DD HH24:MI:SS.FF6')"
    )
    assert adapter._time_literal("TZ", moment) == (
        "TO_TIMESTAMP_TZ('2026-04-01 01:02:03.000000 +00:00', 'YYYY-MM-DD HH24:MI:SS.FF6 TZH:TZM')"
    )
    assert adapter._time_literal("D", moment) == "TO_DATE('2026-04-01', 'YYYY-MM-DD')"


def test_a_time_column_without_a_date_is_refused_with_an_actionable_message() -> None:
    adapter, _conn = make_adapter({"T": "TIME"})
    with pytest.raises(WarehouseCapabilityError, match="TIMESTAMP_NTZ, TIMESTAMP_LTZ"):
        adapter._time_literal("T", _FROM)


def test_buckets_come_back_as_aware_utc() -> None:
    adapter, conn = make_adapter()
    conn.answers = [([], [(datetime(2026, 4, 1), "click", 3)])]
    _cols, _json, rows = adapter.get_time_bucketed_counts(
        _BASE, "TIME", "1d", ["EVENT_NAME"], [], None, _FROM, _TO
    )
    assert rows == [(datetime(2026, 4, 1, tzinfo=UTC), "click", 3)]
    sql = conn.sql[0]
    assert 'GROUP BY _bucket, "EVENT_NAME" ORDER BY _bucket LIMIT 100000' in sql
    assert 'WHERE "TIME" >= TO_TIMESTAMP_NTZ(' in sql


# --------------------------------------------------------------------------- #
# bound parameters
# --------------------------------------------------------------------------- #


def test_top_n_values_are_bound_never_inlined() -> None:
    adapter, conn = make_adapter()
    hostile = "x') OR 1=1 --"
    conn.answers = [([], [("EVENT_NAME", hostile)]), ([], [])]

    adapter.get_time_bucketed_breakdown_counts(
        _BASE, "TIME", "1h", "EVENT_NAME", ["EVENT_NAME"], [], None, _FROM, _TO, values_limit=3
    )

    top_sql, top_params = conn.statements[0]
    assert "GROUP BY GROUPING SETS ((__bd_raw_0))" in top_sql
    assert "ORDER BY _cnt DESC, _breakdown_value) AS rn" in top_sql
    assert top_params == ["EVENT_NAME"]
    fold_sql, fold_params = conn.statements[1]
    assert hostile not in fold_sql
    assert hostile in fold_params
    assert "IN (:1)" in fold_sql


def test_params_number_their_markers_in_bind_order() -> None:
    params = Params()
    assert [params.bind("a"), params.bind("b")] == [":1", ":2"]
    assert params.values == ["a", "b"]


def test_contract_values_and_the_group_filter_are_bound() -> None:
    adapter, conn = make_adapter()
    adapter._contract_regex_support = {"^[a-z]+$": True}
    conn.answers = [([], [(1, 10, "BAD", 0, 10, None, 2, 10, "x")])]
    expectations = [
        FieldContractExpectation(
            field_name="EVENT_NAME",
            drift_type="enum_violation",
            threshold=0.0,
            enum_options=("view", "it's"),
        ),
        FieldContractExpectation(
            field_name="PROPS.plan", drift_type="regex_violation", threshold=0.0, regex="^[a-z]+$"
        ),
        FieldContractExpectation(
            field_name="AMOUNT", drift_type="range_violation", threshold=0.0, min_value=0
        ),
    ]

    adapter.validate_field_contracts(
        _BASE, expectations, group_column="EVENT_NAME", group_value="signup"
    )

    sql, params = conn.statements[0]
    assert "it's" not in sql and "signup" not in sql
    assert params[0] == "signup"
    assert "it's" in params and "^[a-z]+$" in params
    assert "REGEXP_INSTR(" in sql and "= 0" in sql
    assert "TRY_TO_DOUBLE(" in sql and "<> 'NaN'::FLOAT" in sql
    assert "PROPS\"::VARIANT['plan']" in sql
    assert "COUNT_IF(" in sql and "MIN(IFF(" in sql


def test_a_refused_regex_is_skipped_after_a_table_free_probe() -> None:
    adapter, conn = make_adapter()
    conn.refused_param = "(?<=a)b"
    expectation = FieldContractExpectation(
        field_name="EVENT_NAME", drift_type="regex_violation", threshold=0.0, regex="(?<=a)b"
    )

    assert adapter.validate_field_contracts(_BASE, [expectation]) == []

    # The pattern is refused while the control pattern compiles: the refusal is
    # about the pattern, so the contract is skipped and no table is read.
    probe, control = conn.statements
    assert probe == ("SELECT REGEXP_INSTR('', :1)", ["(?<=a)b"])
    assert control[0] == "SELECT REGEXP_INSTR('', :1)"
    assert adapter.take_skipped_field_contracts() == [expectation]


# --------------------------------------------------------------------------- #
# nested columns
# --------------------------------------------------------------------------- #


def test_nested_shapes_and_values() -> None:
    adapter, conn = make_adapter()
    conn.answers = [([], [("view", '["a","b"]', '"pro"', 4)])]

    _reg, _json, names, rows = adapter.get_full_breakdown(
        _BASE, ["EVENT_NAME"], ["PROPS"], {"PROPS": ["plan"]}
    )

    sql = conn.sql[0]
    assert (
        'TO_JSON(ARRAY_SORT(COALESCE(IFF(IS_OBJECT("PROPS"::VARIANT), '
        'OBJECT_KEYS("PROPS"::VARIANT::OBJECT), NULL), ARRAY_CONSTRUCT()))) AS __np_0'
    ) in sql
    assert "TO_JSON(\"PROPS\"::VARIANT['plan']) AS __nv_0" in sql
    assert names == ["PROPS.plan"]
    assert rows == [("view", ["a", "b"], '"pro"', 4)]


def test_a_breakdown_without_grouping_keys_has_no_group_by() -> None:
    adapter, conn = make_adapter()
    conn.answers = [([], [(7,)])]
    adapter.get_full_breakdown(_BASE, [], [])
    assert "GROUP BY" not in conn.sql[0]


def test_properties_read_as_nullable_strings_and_scalars_refuse_paths() -> None:
    adapter, _conn = make_adapter()
    assert adapter._property_value_expression("PROPS", "a.b") == (
        "IFF(IS_NULL_VALUE(\"PROPS\"::VARIANT['a']['b']), NULL, "
        "\"PROPS\"::VARIANT['a']['b']::STRING)"
    )
    with pytest.raises(ValueError, match="holds no nested"):
        adapter._json_path_expression("EVENT_NAME", "x")
    with pytest.raises(ValueError, match="Unsupported JSON path"):
        adapter._json_path_expression("PROPS", "a'b")


def test_json_string_source_keeps_column_order_and_parses_safely() -> None:
    adapter, _conn = make_adapter()
    adapter._described = (_BASE, ["TIME", "RAW", "AMOUNT"])
    sql = adapter.json_string_source(_BASE, ["RAW"])
    assert sql == (
        'SELECT "TIME", IFF(IS_OBJECT(TRY_PARSE_JSON("RAW")), TRY_PARSE_JSON("RAW"), NULL) '
        f'AS "RAW", "AMOUNT" FROM ({_BASE}) AS _json_src'
    )
    with pytest.raises(ValueError, match="invalid column"):
        adapter.json_string_source(_BASE, ['RAW"'])


# --------------------------------------------------------------------------- #
# aggregates
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (AggregateSpec("c", MetricAggregation.count, None, "amount > 0"), "NULLIF(COUNT_IF("),
        (
            AggregateSpec("u", MetricAggregation.count_distinct, "EVENT_NAME", "amount > 0"),
            'COUNT(DISTINCT IFF(amount > 0, "EVENT_NAME", NULL))',
        ),
        (
            AggregateSpec("s", MetricAggregation.sum, "AMOUNT", "amount > 0"),
            'sum(CASE WHEN amount > 0 THEN "AMOUNT" END)',
        ),
        (AggregateSpec("a", MetricAggregation.avg, "AMOUNT", None), 'avg("AMOUNT")'),
    ],
)
def test_conditional_aggregates(spec: AggregateSpec, expected: str) -> None:
    adapter, _conn = make_adapter()
    assert expected in adapter._spec_aggregate_sql(spec)


def test_disclosed_batch_sql_is_the_executed_statement() -> None:
    specs = [AggregateSpec("n", MetricAggregation.count, None, None)]
    names, disclosed = compile_time_bucketed_multi_aggregate_sql(
        db_type="snowflake",
        base_query=_BASE,
        time_column="TIME",
        interval="1h",
        specs=specs,
        time_from=_FROM,
        time_to=_TO,
        column_types=_TYPES,
    )
    adapter, conn = make_adapter()
    adapter.get_time_bucketed_multi_aggregate(_BASE, "TIME", "1h", specs, _FROM, _TO)
    assert names == ["bucket", "n"]
    assert conn.sql == [disclosed]


def test_aggregate_breakdown_groups_by_the_folded_value_only() -> None:
    adapter, conn = make_adapter()
    conn.answers = [([], [])]
    adapter.get_time_bucketed_aggregate_breakdown(
        _BASE,
        "TIME",
        "1h",
        MetricAggregation.sum,
        "AMOUNT",
        "EVENT_NAME",
        ["EVENT_NAME"],
        [],
        None,
        _FROM,
        _TO,
    )
    sql = conn.sql[0]
    assert "GROUP BY _bucket, _breakdown_value, _is_other ORDER BY" in sql
    assert 'COALESCE("EVENT_NAME"::STRING, \'\') AS "EVENT_NAME"' in sql


# --------------------------------------------------------------------------- #
# timeouts
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("errno", [604, 630])
def test_a_cancelled_statement_is_reported_as_the_source_timeout(errno: int) -> None:
    adapter, conn = make_adapter()
    adapter._timeout_seconds = 12.5
    conn.raise_errno = errno
    with pytest.raises(TimeoutError, match="13s timeout|12.5s timeout"):
        adapter.test_connection()
    assert conn.timeouts == [13]
    assert conn.cursors[0].closed is True


def test_other_driver_errors_pass_through_unchanged() -> None:
    adapter, conn = make_adapter()
    adapter._timeout_seconds = 30.0
    conn.raise_errno = 2003  # object does not exist
    with pytest.raises(DriverError):
        adapter.test_connection()


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
        name="sf",
        db_type="snowflake",
        host="myorg-myaccount",
        port=443,
        database_name="ANALYTICS",
        username="TRIPL",
        password_encrypted=encrypt_value("hunter2"),
        timeout_seconds=120,
        extra_params={"warehouse": "COMPUTE_WH", **settings},
    )


class _FakeDriver:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    def connect(self, **kwargs: object) -> FakeConnection:
        self.kwargs = kwargs
        return FakeConnection()


def _private_key_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def test_registry_lists_snowflake() -> None:
    assert "snowflake" in supported_db_types()
    assert dialect_for_db_type("snowflake") is SqlDialect.snowflake


def test_factory_wires_settings_timeout_and_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = _FakeDriver()
    monkeypatch.setattr(snowflake_sql, "import_driver", lambda: driver)

    adapter = build_adapter(
        _source(role="ANALYST", schema_name="events", schema_allowlist=["marts"])
    )

    assert isinstance(adapter, SnowflakeAdapter)
    kwargs = driver.kwargs
    assert kwargs["account"] == "myorg-myaccount"
    assert kwargs["host"] == "myorg-myaccount.snowflakecomputing.com"
    assert kwargs["user"] == "TRIPL"
    assert kwargs["password"] == "hunter2"
    assert "private_key" not in kwargs
    assert kwargs["warehouse"] == "COMPUTE_WH"
    assert kwargs["database"] == "ANALYTICS"
    assert kwargs["schema"] == "events"
    assert kwargs["role"] == "ANALYST"
    assert kwargs["paramstyle"] == "numeric"
    assert kwargs["session_parameters"] == {
        "TIMEZONE": "UTC",
        "QUERY_TAG": "tripl",
        "CLIENT_TELEMETRY_ENABLED": False,
        "STATEMENT_TIMEOUT_IN_SECONDS": 120,
    }
    assert kwargs["login_timeout"] == 120
    assert kwargs["network_timeout"] == 120
    assert adapter._schema_allowlist == ("marts",)
    assert adapter._schemas_in_scope() == ["events", "marts"]


def test_key_pair_sign_in_reads_a_pem_pasted_on_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    driver = _FakeDriver()
    monkeypatch.setattr(snowflake_sql, "import_driver", lambda: driver)
    source = _source(auth_type="key_pair")
    source.password_encrypted = encrypt_value(_private_key_pem().replace("\n", " "))

    build_adapter(source)

    assert driver.kwargs["authenticator"] == "SNOWFLAKE_JWT"
    assert isinstance(driver.kwargs["private_key"], rsa.RSAPrivateKey)
    assert "password" not in driver.kwargs


def test_key_pair_sign_in_refuses_text_that_is_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snowflake_sql, "import_driver", _FakeDriver)
    with pytest.raises(WarehouseCapabilityError, match="PEM"):
        build_adapter(_source(auth_type="key_pair"))


@pytest.mark.parametrize(
    ("host", "account", "hostname"),
    [
        ("myorg-myaccount", "myorg-myaccount", "myorg-myaccount.snowflakecomputing.com"),
        ("MyOrg-My_Account", "myorg-my_account", "myorg-my-account.snowflakecomputing.com"),
        (
            "xy12345.eu-central-1.aws.snowflakecomputing.com",
            "xy12345.eu-central-1.aws",
            "xy12345.eu-central-1.aws.snowflakecomputing.com",
        ),
        (
            "https://xy12345.snowflakecomputing.com/",
            "xy12345",
            "xy12345.snowflakecomputing.com",
        ),
    ],
)
def test_the_host_resolves_to_an_account_and_a_snowflake_hostname(
    host: str, account: str, hostname: str
) -> None:
    target = resolve_host(host)
    assert (target.account, target.hostname) == (account, hostname)


@pytest.mark.parametrize(
    "host",
    ["evil.com:80", "acct.snowflakecomputing.com/path", "[::1]", "a b", "", "acct:443"],
)
def test_anything_but_an_account_or_its_hostname_is_refused(host: str) -> None:
    with pytest.raises(WarehouseCapabilityError, match="account identifier"):
        resolve_host(host)


def test_public_hosts_only_refuses_a_private_address(monkeypatch: pytest.MonkeyPatch) -> None:
    from tripl.config import settings

    monkeypatch.setattr(settings, "outbound_public_hosts_only", True)
    monkeypatch.setattr(snowflake_sql, "import_driver", _FakeDriver)
    resolved: list[str] = []

    def fake_resolve(host: str, *_a: object, **_k: object) -> list[object]:
        resolved.append(host)
        return [(None, None, None, None, ("10.0.0.5", 443))]

    monkeypatch.setattr("tripl.services.safe_http._resolve", fake_resolve)
    source = _source()
    source.host = "acct.privatelink"
    with pytest.raises(WarehouseCapabilityError, match="private or internal address"):
        build_adapter(source)
    assert resolved == ["acct.privatelink.snowflakecomputing.com"]


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"warehouse": ""},
        {"warehouse": "WH; DROP"},
        {"warehouse": "WH", "auth_type": "oauth_m2m"},
        {"warehouse": "WH", "http_path": "/sql/1.0/warehouses/x"},
        {"warehouse": "WH", "schema_allowlist": ["ok", "bad name"]},
    ],
)
def test_malformed_settings_are_rejected(raw: dict[str, object]) -> None:
    with pytest.raises(ConnectionSettingsError):
        parse_connection_settings("snowflake", raw)


def test_settings_are_trimmed_and_deduplicated() -> None:
    parsed = parse_connection_settings(
        "snowflake",
        {"warehouse": " WH ", "role": " ", "schema_allowlist": ["a", " a ", "", "B"]},
    )
    assert parsed is not None
    assert parsed.model_dump(exclude_none=True) == {
        "warehouse": "WH",
        "schema_allowlist": ["a", "B"],
    }


# --------------------------------------------------------------------------- #
# dialect helpers, the SQL gate and type classification
# --------------------------------------------------------------------------- #


def test_dialect_rendering_helpers() -> None:
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    assert quote_identifier("t.order", SqlDialect.snowflake) == '"t"."order"'
    assert quote_sql_literal("o'brien\\", SqlDialect.snowflake) == "'o\\'brien\\\\'"
    assert quote_timestamp_literal(moment, SqlDialect.snowflake, kind=TimeKind.timestamp) == (
        "TO_TIMESTAMP_TZ('2026-01-01 00:00:00.000000 +00:00', 'YYYY-MM-DD HH24:MI:SS.FF6 TZH:TZM')"
    )
    assert quote_timestamp_literal(moment, SqlDialect.snowflake, kind=TimeKind.date) == (
        "TO_DATE('2026-01-01', 'YYYY-MM-DD')"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT toStartOfInterval(ts, INTERVAL 1 DAY) FROM t",
        "SELECT date_bin(INTERVAL '1 day', ts, now()) FROM t",
        "SELECT countIf(ok) FROM t",
        "SELECT `status` FROM t",
    ],
)
def test_other_engines_sql_is_flagged_before_it_reaches_the_warehouse(sql: str) -> None:
    assert lint_dialect_sql(sql, SqlDialect.snowflake) is not None


def test_snowflake_sql_is_not_flagged() -> None:
    sql = (
        "SELECT DATE_TRUNC('DAY', ts) AS bucket, COUNT_IF(props:plan = 'pro') "
        'FROM "EVENTS" GROUP BY 1'
    )
    assert lint_dialect_sql(sql, SqlDialect.snowflake) is None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SYSTEM$CANCEL_ALL_QUERIES(1)",
        "SELECT SNOWFLAKE.CORTEX.COMPLETE('m', 'hi')",
        "SELECT AI_COMPLETE('m', 'hi')",
    ],
)
def test_the_read_only_gate_refuses_snowflake_side_effects(sql: str) -> None:
    with pytest.raises(ValueError, match="read-only"):
        validate_select_sql_safety(sql)


def test_type_classification_knows_snowflake_spellings() -> None:
    assert classify_complex("VARIANT") is ComplexKind.json
    assert classify_complex("OBJECT") is ComplexKind.json
    assert classify_complex("ARRAY") is None
    for name in ("TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ"):
        assert classify_time(name) is TimeKind.timestamp
    assert classify_time("DATE") is TimeKind.date
    assert classify_time("TIME") is TimeKind.unsupported
    assert is_string_type("STRING")
    assert bucket_warehouse_type("NUMBER(38,0)") == "number"
    assert bucket_warehouse_type("FLOAT") == "number"
    assert bucket_warehouse_type("TIMESTAMP_TZ") == "timestamp"
    assert bucket_warehouse_type("BOOLEAN") == "bool"
