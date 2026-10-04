"""Batch 5, lane W1: the PostgreSQL adapter and the shared read-only SQL gate.

Five findings, one file:

* a field-contract range check compares in ``numeric``, and the
  guard in front of the cast bounds MAGNITUDE as well as syntax, so no single event
  row can abort the contract query for every other expectation in it.
* an array column reports ``jsonb[]`` rather than ``jsonb``, and
  an array classifies as an opaque scalar rather than as a JSON document.
* the libpq startup ``options`` pin
  ``standard_conforming_strings`` and ``default_transaction_read_only``.
* a fact condition's time literal is typed from the NATIVE
  warehouse type, which is the only thing that tells BigQuery DATE from DATETIME.
* the read-only text gate scans SQL, not the values inside its
  string literals.

Everything here is SQL text, a classifier answer or a captured kwarg: none of it
needs a warehouse. The live halves — that PostgreSQL actually accepts the emitted
SQL, and that its verdicts match the Python fallback row for row — belong in
``tests/conformance/``, which skips when no server is reachable.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal

import psycopg
import pytest

# Imported first and for its side effect: the worker task package is import-order
# sensitive, and entering it anywhere but ``celery_app`` raises ImportError on a
# half-initialised module. Only the _fact_conditions block needs it, but it has to
# run before that import.
import tripl.worker.celery_app  # noqa: F401
from tripl.core.adapters import postgres as postgres_module
from tripl.core.adapters.base import ColumnInfo, FieldContractExpectation
from tripl.core.adapters.measure_validator import (
    SqlDialect,
    _mask_quoted_spans,
    validate_select_sql_safety,
    validate_sql_fragment,
)
from tripl.core.adapters.postgres import _FINITE_NUMBER_RE, PostgresAdapter
from tripl.core.warehouse_types import ComplexKind, TimeKind, classify_complex, classify_time
from tripl.models.fact_table import FactTable
from tripl.worker.tasks.metrics import _fact_conditions

# --- the contract range comparison ----------------------------


def _range_condition(**bounds: float) -> str:
    """The BAD-row predicate for one range expectation, with no connection."""
    adapter = object.__new__(PostgresAdapter)
    adapter._allowed_columns = {"amount"}
    expectation = FieldContractExpectation(
        field_name="amount",
        drift_type="range_violation",
        threshold=0.0,
        **bounds,  # type: ignore[arg-type]
    )
    condition = adapter._contract_bad_condition(expectation)
    assert condition is not None
    return condition


def test_the_range_comparison_never_touches_double_precision() -> None:
    """float8's input function raises on overflow AND on underflow-to-zero.

    Both raises were reachable from event data through a guard that only checked
    syntax, and every expectation shares one statement, so one row took down a
    whole config's contract check. ``numeric`` has no float range to leave.
    """
    condition = _range_condition(min_value=0.0, max_value=50.0)

    assert "::double precision" not in condition
    assert "::numeric" in condition
    assert "'Infinity'::numeric" in condition
    assert "'-Infinity'::numeric" in condition


def test_both_bounds_are_compared_as_numeric() -> None:
    """The bound must not be the one float8 left in the expression.

    ``numeric >= double precision`` resolves by casting the numeric side to
    float8, which would reinstate exactly the overflow this finding removes.
    """
    condition = _range_condition(min_value=0.0, max_value=50.0)

    assert ">= 0.0::numeric" in condition
    assert "<= 50.0::numeric" in condition


def test_a_one_sided_range_still_emits_only_its_own_bound() -> None:
    assert ">= 0.0::numeric" in _range_condition(min_value=0.0)
    assert "<=" not in _range_condition(min_value=0.0)
    assert "<= 50.0::numeric" in _range_condition(max_value=50.0)
    assert ">=" not in _range_condition(max_value=50.0)


def test_the_guard_uses_only_repetition_counts_postgres_will_compile() -> None:
    """POSIX ARE bounds accept 0-255; a wider count is a regex compile error.

    A compile error here is not a bad row, it is the whole contract statement
    failing — the exact failure mode the guard exists to prevent. Reverting to
    the unbounded ``[0-9]+`` leaves no bounds to check at all.
    """
    counts = re.findall(r"\{(\d+),(\d+)\}", _FINITE_NUMBER_RE)

    assert counts, "the guard must bound its repetitions, not just its syntax"
    assert all(int(high) <= 255 for _low, high in counts)


# PostgreSQL's numeric holds up to 131072 digits before and 16383 digits after the
# decimal point. Anything the guard admits is cast to numeric, so anything it
# admits must fit inside those.
_NUMERIC_MAX_INTEGER_DIGITS = 131072
_NUMERIC_MAX_FRACTION_DIGITS = 16383

_CANDIDATE_VALUES = (
    "0",
    "1.",
    ".5",
    "+3",
    "-0.25",
    "50.0",
    "1e400",
    "1e-400",
    "-1e-400",
    "5e-324",
    "1.7976931348623157e+308",
    "1e999999999",
    "9" * 310,
)


def test_no_value_the_guard_admits_can_overflow_the_numeric_cast() -> None:
    admitted = [text for text in _CANDIDATE_VALUES if re.match(_FINITE_NUMBER_RE, text)]

    # The float8 overflow and underflow cases must still be COMPARED rather than
    # dropped: they are the values whose verdict used to be an aborted query.
    assert "1e400" in admitted
    assert "1e-400" in admitted
    # A magnitude no numeric can hold must be refused by the guard instead, and
    # falls through to NULL -> BAD like any other unparseable value.
    assert "1e999999999" not in admitted

    for text in admitted:
        value = Decimal(text)
        exponent = value.as_tuple().exponent
        assert isinstance(exponent, int)
        assert value.adjusted() < _NUMERIC_MAX_INTEGER_DIGITS, text
        assert exponent > -_NUMERIC_MAX_FRACTION_DIGITS, text


def test_the_guard_still_rejects_what_is_not_a_number_at_all() -> None:
    """NaN and the infinity spellings are routed around the cast, not through it."""
    for text in ("twelve", "", "1,5", "0x10", "nan", "inf", "-infinity"):
        assert re.match(_FINITE_NUMBER_RE, text) is None, text


# --- array types ----------------------------------------------


class _Description:
    """The two attributes ``get_columns`` reads off a psycopg cursor description."""

    def __init__(self, name: str, type_code: int) -> None:
        self.name = name
        self.type_code = type_code


class _TypeCursor:
    def __init__(self, parent: _TypeConn) -> None:
        self._parent = parent
        self.description = parent.description

    def __enter__(self) -> _TypeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self._parent.sql.append(sql)


class _TypeConn:
    """A connection stub that carries psycopg's REAL type registry.

    The registry is static data shipped with the driver, so resolving an oid to a
    ``TypeInfo`` needs no server — which is the whole point: the array-vs-element
    distinction this test pins is a property of that registry.
    """

    def __init__(self, description: list[_Description]) -> None:
        self.description = description
        self.sql: list[str] = []
        self.adapters = psycopg.adapters

    def cursor(self) -> _TypeCursor:
        return _TypeCursor(self)


def _columns_for(description: list[_Description]) -> list[ColumnInfo]:
    adapter = object.__new__(PostgresAdapter)
    adapter._conn = _TypeConn(description)
    adapter._type_names = {}
    return adapter.get_columns("SELECT * FROM events")


def test_an_array_column_is_not_reported_as_its_element_type() -> None:
    """psycopg resolves an array oid to the ELEMENT's TypeInfo.

    Reporting that name verbatim told every classifier that ``jsonb[]`` was a
    JSON document, which routes the column into a path walk that casts it
    ``::jsonb`` — an error PostgreSQL raises for an array, failing the scan.
    """
    columns = _columns_for(
        [
            _Description("doc", 3807),  # jsonb[]
            _Description("tags", 1007),  # int4[]
            _Description("count", 23),  # int4, the element itself
            _Description("seen_at", 1185),  # timestamptz[]
            _Description("at", 1184),  # timestamptz, the element itself
        ]
    )

    assert [(column.name, column.type_name) for column in columns] == [
        ("doc", "jsonb[]"),
        ("tags", "int4[]"),
        ("count", "int4"),
        ("seen_at", "timestamptz[]"),
        ("at", "timestamptz"),
    ]


def test_an_oid_the_driver_does_not_know_keeps_its_placeholder_name() -> None:
    columns = _columns_for([_Description("custom", 999999)])

    assert columns[0].type_name == "oid_999999"


@pytest.mark.parametrize("type_name", ["jsonb[]", "json[]", "JSONB[]", "record[]"])
def test_an_array_of_a_complex_type_is_an_opaque_scalar(type_name: str) -> None:
    """ClickHouse's ``Array(JSON)`` already answers None here; Postgres now agrees."""
    assert classify_complex(type_name) is None


@pytest.mark.parametrize(
    ("type_name", "expected"),
    [
        ("jsonb", ComplexKind.json),
        ("json", ComplexKind.json),
        ("RECORD", ComplexKind.struct),
        ("Map(String, String)", ComplexKind.map),
        ("int4", None),
    ],
)
def test_a_scalar_type_still_classifies_as_it_did(
    type_name: str, expected: ComplexKind | None
) -> None:
    assert classify_complex(type_name) is expected


@pytest.mark.parametrize("type_name", ["timestamptz[]", "date[]", "timestamp[]", "TIMESTAMPTZ[]"])
def test_an_array_of_a_time_type_cannot_be_a_time_column(type_name: str) -> None:
    """``timestamptz[]`` names many instants; no window bound compares against it.

    Rejecting it here fails the configuration with an actionable error instead of
    failing inside a worker when the bucket expression is built.
    """
    assert classify_time(type_name) is TimeKind.unsupported


@pytest.mark.parametrize(
    ("type_name", "expected"),
    [
        ("timestamptz", TimeKind.timestamp),
        ("DATETIME", TimeKind.datetime),
        ("Nullable(DateTime64(3))", TimeKind.datetime),
        ("Date32", TimeKind.date),
        ("time", TimeKind.unsupported),
    ],
)
def test_a_scalar_time_type_still_classifies_as_it_did(type_name: str, expected: TimeKind) -> None:
    assert classify_time(type_name) is expected


# --- the libpq session GUCs --------------------------------------------------


class _FakeConn:
    def close(self) -> None:
        return None


def _captured_options(
    monkeypatch: pytest.MonkeyPatch,
    *,
    timeout_seconds: int | None = None,
    search_path: str | None = None,
) -> str:
    """The libpq ``options`` string a real construction would send.

    ``localhost`` keeps this offline in every sense: it resolves to sslmode
    ``prefer`` with no certificate material, so nothing is written to disk either.
    """
    captured: dict[str, object] = {}

    def fake_connect(**connect_kwargs: object) -> _FakeConn:
        captured.update(connect_kwargs)
        return _FakeConn()

    monkeypatch.setattr(postgres_module.psycopg, "connect", fake_connect)
    adapter = PostgresAdapter(
        host="localhost",
        port=5432,
        database="analytics",
        timeout_seconds=timeout_seconds,
        search_path=search_path,
    )
    adapter.close()
    options = captured["options"]
    assert isinstance(options, str)
    return options


def test_the_connection_pins_the_gucs_our_sql_depends_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three invariant GUCs, then the conditional ones, in one options string.

    ``standard_conforming_strings`` is not cosmetic: ``_quote_string`` escapes a
    warehouse-derived value by doubling the quote and nothing else, which under
    the legacy ``off`` lets a value ending in a backslash close its own literal —
    over an autocommit connection whose parameterless statements go out on the
    simple query protocol, i.e. injection that commits.
    ``default_transaction_read_only`` is defence in depth behind the credential.
    """
    options = _captured_options(monkeypatch, timeout_seconds=90)

    assert options == (
        "-c timezone=UTC "
        "-c standard_conforming_strings=on "
        "-c default_transaction_read_only=on "
        "-c statement_timeout=90000"
    )


def test_the_invariant_gucs_are_pinned_even_with_no_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _captured_options(monkeypatch)

    assert options == (
        "-c timezone=UTC -c standard_conforming_strings=on -c default_transaction_read_only=on"
    )


def test_a_search_path_is_appended_after_the_invariant_gucs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The options string is whitespace-delimited, so ordering has to be pinned."""
    options = _captured_options(monkeypatch, search_path="analytics, public")

    assert options == (
        "-c timezone=UTC "
        "-c standard_conforming_strings=on "
        "-c default_transaction_read_only=on "
        "-c search_path=analytics,public"
    )


# --- the native type decides the time literal's family ---------


def _fact_table(*columns: dict[str, str]) -> FactTable:
    """A transient fact table; nothing here touches a session."""
    return FactTable(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        name="orders_ft",
        display_name="Orders",
        data_source_id=uuid.uuid4(),
        sql="SELECT order_date, amount FROM orders",
        timestamp_column="order_date",
        columns=list(columns),
        identifier_columns=[],
        row_filters=[],
    )


def _gte_fragment(column: dict[str, str], dialect: SqlDialect) -> str | None:
    return _fact_conditions._resolve_combined_filter(
        _fact_table(column),
        dialect=dialect,
        row_filters=(),
        filter_sql=None,
        conditions=(_fact_conditions._FactCondition("order_date", "gte", "2026-01-01"),),
    )


@pytest.mark.parametrize(
    ("native_type", "expected"),
    [
        ("DATE", "(`order_date` >= DATE '2026-01-01')"),
        # No offset: DATETIME is a zone-less wall clock and BigQuery rejects
        # `DATETIME '...+00:00'` outright.
        ("DATETIME", "(`order_date` >= DATETIME '2026-01-01 00:00:00.000000')"),
        ("TIMESTAMP", "(`order_date` >= TIMESTAMP '2026-01-01 00:00:00.000000+00:00')"),
    ],
)
def test_a_bigquery_time_bound_is_typed_from_the_native_type(
    native_type: str, expected: str
) -> None:
    """Introspection folds DATE/DATETIME/TIMESTAMP into one bucket, ``timestamp``.

    Classifying from that bucket collapsed every BigQuery time column to
    TIMESTAMP and made the DATE and DATETIME arms unreachable from production.
    """
    column = {"name": "order_date", "type": "timestamp", "native_type": native_type}

    assert _gte_fragment(column, SqlDialect.bigquery) == expected


def test_a_fact_table_saved_before_native_types_keeps_its_old_literal() -> None:
    """The fallback is back-compat, not laziness: no native_type, no change."""
    column = {"name": "order_date", "type": "timestamp"}

    assert _gte_fragment(column, SqlDialect.bigquery) == (
        "(`order_date` >= TIMESTAMP '2026-01-01 00:00:00.000000+00:00')"
    )


def test_an_empty_native_type_falls_back_rather_than_losing_the_time_typing() -> None:
    column = {"name": "order_date", "type": "timestamp", "native_type": ""}

    assert _gte_fragment(column, SqlDialect.bigquery) == (
        "(`order_date` >= TIMESTAMP '2026-01-01 00:00:00.000000+00:00')"
    )


@pytest.mark.parametrize("native_type", ["DATE", "DATETIME", "TIMESTAMP"])
def test_postgres_renders_one_timestamptz_literal_whatever_the_native_type(
    native_type: str,
) -> None:
    """PostgreSQL and ClickHouse ignore the kind, so this must not have moved."""
    column = {"name": "order_date", "type": "timestamp", "native_type": native_type}

    assert _gte_fragment(column, SqlDialect.postgres) == (
        "(\"order_date\" >= TIMESTAMPTZ '2026-01-01 00:00:00.000000+00:00')"
    )


def test_the_normalized_type_still_drives_value_validation() -> None:
    """The two maps are not interchangeable: ``number`` is the validation word.

    Feeding native types into the normalized map would silently disable this
    check, because ``Float64`` matches none of its branches.
    """
    fact_table = _fact_table(
        {"name": "order_date", "type": "timestamp", "native_type": "DATE"},
        {"name": "amount", "type": "number", "native_type": "Float64"},
    )

    with pytest.raises(_fact_conditions.ScanError, match="numeric column"):
        _fact_conditions._resolve_combined_filter(
            fact_table,
            dialect=SqlDialect.bigquery,
            row_filters=(),
            filter_sql=None,
            conditions=(_fact_conditions._FactCondition("amount", "gte", "lots"),),
        )


# --- the read-only gate reads code, not data ------------------


@pytest.mark.parametrize(
    "fragment",
    [
        "event_name IN ('Delete Account','Create Account')",
        "event_name = 'Sign in with Apple'",
        "plan = 'Select Plan'",
        "utm_campaign = '#launch'",
        "tag = 'call-center'",
        "note = 'a;b'",
        "note = 'a -- b'",
        "label = 'Copy of X'",
        "job = 'System Heartbeat'",
        # The pre-existing accept case, which must not have moved.
        "status IN ('a','b') AND created_at > '2026-01-01'",
    ],
)
def test_a_keyword_inside_a_literal_is_a_value_not_an_attack(fragment: str) -> None:
    """A keyword inside a closed literal is a string; it can execute nothing.

    Every one of these but the last was rejected before, so an analyst could not
    filter on an event called "Delete Account" or a campaign called "#launch".
    The last one already passed and is here to prove it still does.
    """
    assert validate_sql_fragment(fragment) == fragment


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        # Nothing is quoted: unchanged behaviour.
        ("1=1 UNION SELECT password", "disallowed keyword: UNION"),
        # The literal CLOSED before the payload, so the payload is code.
        ("status = 'x'; DROP TABLE users", "';'"),
        ("x = 'a' UNION SELECT p", "disallowed keyword: UNION"),
        ("x = 'a' -- comment", "comment"),
        ("x = 'a' /* injected", "comment"),
        # An unterminated literal is scanned RAW, so nothing hides behind it.
        ("x = 'a DROP TABLE t", "disallowed keyword: DROP"),
        # An empty literal must not swallow the rest of the fragment.
        ("x = '' DROP TABLE t ''", "disallowed keyword: DROP"),
        # A quoted IDENTIFIER is masked too, and must not hide what follows it.
        ('"col" = 1; DROP TABLE t', "';'"),
    ],
)
def test_the_gate_still_rejects_what_is_actually_code(bad: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        validate_sql_fragment(bad)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ts, v FROM t WHERE event_name != 'System Heartbeat'",
        "SELECT ts, v FROM t WHERE label = 'Copy of X'",
        "SELECT ts, v FROM t WHERE tag = 'call-center'",
        "SELECT ts, v FROM t WHERE note = 'a;b'",
    ],
)
def test_a_statement_may_compare_against_a_value_that_reads_like_sql(sql: str) -> None:
    assert validate_select_sql_safety(sql) == sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT v FROM t; DROP TABLE t",
        "SELECT v FROM t WHERE a = 'x'; DROP TABLE t",
        "SELECT v FROM t WHERE a = 'x' -- tail",
        "SELECT v FROM t WHERE a = 'x' UNION SELECT b FROM u",
        "DROP TABLE t",
    ],
)
def test_a_statement_that_is_actually_unsafe_is_still_refused(sql: str) -> None:
    with pytest.raises(ValueError):
        validate_select_sql_safety(sql)


def test_the_trailing_semicolon_is_still_stripped_off_the_real_statement() -> None:
    """The mask preserves length, so the slice indexes the original correctly.

    If it did not, this would strip the wrong character — or strip from the masked
    copy and return a statement with its literal blanked out.
    """
    sql = "SELECT ts, v FROM t WHERE note = 'a;b' ;"

    assert validate_select_sql_safety(sql) == "SELECT ts, v FROM t WHERE note = 'a;b'"


@pytest.mark.parametrize(
    "text",
    [
        "event_name IN ('Delete Account')",
        "x = 'a''b'",
        "x = 'unterminated",
        '"quoted ident" = 1',
        "`ch ident` = 1",
        "",
    ],
)
def test_masking_only_ever_blanks_characters_in_place(text: str) -> None:
    """Length and offsets are the contract; callers index the ORIGINAL by them."""
    masked = _mask_quoted_spans(text)

    assert len(masked) == len(text)
    assert all(new == old or new == " " for new, old in zip(masked, text, strict=True))


def test_masking_keeps_the_delimiters_and_blanks_what_is_between_them() -> None:
    assert _mask_quoted_spans("a = 'x' AND b = 'yy'") == "a = ' ' AND b = '  '"


def test_an_unterminated_literal_leaves_its_tail_exposed_to_the_scan() -> None:
    """The conservative direction: what never closes is never trusted.

    It is also what keeps the ClickHouse/BigQuery backslash escape working — the
    gate is dialect-agnostic and cannot know which convention applies, so it
    keeps scanning rather than assuming the span is a value.
    """
    masked = _mask_quoted_spans("x = 'a DROP TABLE t")

    assert masked == "x = 'a DROP TABLE t"
