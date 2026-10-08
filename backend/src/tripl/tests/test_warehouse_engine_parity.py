"""Cross-engine parity: one contract, four warehouse engines.

Each section below covers one engine contract; the fakes at the top are shared.

``BaseAdapter``'s top-N breakdown contract, plus a row layout
the same file contradicted itself about.

* The ABC promised the top ``values_limit`` values "ranked deterministically".
  Both halves were wrong. Every implementation keeps ``values_limit - 1``,
  because ``'Other'`` takes one of the slots; and none of the three SQL adapters
  ranked deterministically at all — their top-values pre-query ordered by count
  alone, so two equally-counted values at the cutoff could swap places between
  two rankings of the same window, and the value at the boundary lands in its
  own series one time and inside ``'Other'`` the next. The cut now lives in
  exactly one docstring, and all three SQL adapters carry the value tie-break
  the in-memory adapter already had.
* Separately, the row layouts stated for ``get_time_bucketed_aggregate`` and
  ``get_time_bucketed_aggregate_breakdown`` omitted the ``keep_json_value``
  block both of them emit, while the four count methods documented it.

These are fake-client tests: nothing here contacts a warehouse. A SQL string
proves shape, not validity, so the new ORDER BY keys are only *executed* by
``tests/conformance/`` (live credentials, skipped otherwise). What they do prove
is that the ranking key is present on every entry point of every engine, which
is the part a live one-engine run cannot show.
"""

from __future__ import annotations

import inspect
import logging
import re
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel, ValidationError

from tripl.core.adapters.base import (
    FIELD_CONTRACT_EXPECTATIONS_PER_QUERY,
    AggregateSpec,
    BaseAdapter,
    ColumnInfo,
    FieldContractExpectation,
    FieldContractViolation,
    contract_bound_literal,
    field_contract_is_inert,
    field_contract_verdict,
)
from tripl.core.adapters.bigquery import BigQueryAdapter
from tripl.core.adapters.clickhouse import ClickHouseAdapter
from tripl.core.adapters.databricks import DatabricksAdapter
from tripl.core.adapters.greenplum import GreenplumAdapter
from tripl.core.adapters.postgres import PostgresAdapter
from tripl.core.adapters.redshift import RedshiftAdapter
from tripl.core.adapters.snowflake import SnowflakeAdapter
from tripl.core.adapters.synthetic import SyntheticAdapter
from tripl.models.domain_enums import MetricAggregation
from tripl.schemas.field_definition import FieldDefinitionCreate, FieldDefinitionUpdate
from tripl.worker.tasks._errors import ScanError
from tripl.worker.tasks.metrics import catalog_sync as metrics_catalog_sync
from tripl.worker.tasks.metrics import schema_drift as metrics_schema_drift
from tripl.worker.tasks.metrics import tasks as metrics_tasks

_FROM = datetime(2026, 4, 1)
_TO = datetime(2026, 4, 2)
_BASE = "SELECT time, event_name, amount FROM events"
_ALLOWED = {"time", "event_name", "amount"}
_SPECS = [AggregateSpec(key="k_cnt", aggregation=MetricAggregation.count)]

# The synthetic dataset's own fixture values, copied from test_synthetic_adapter.py
# so both files describe the same warehouse.
_ANCHOR = datetime(2026, 6, 1, tzinfo=UTC)
_HISTORY_DAYS = 30
_SEED = 7


# --------------------------------------------------------------------------- #
# Fakes. Every adapter is the real class entered through ``object.__new__``, the
# way core/adapters/multi_aggregate_sql.py enters one for disclosure: no
# __init__, so no connection and no credentials, and the SQL builders under test
# are the production ones.
# --------------------------------------------------------------------------- #


class _CHResult:
    column_names: list[str] = []
    result_rows: list[tuple[object, ...]] = []


class _CHClient:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def query(self, sql: str, **_kwargs: object) -> _CHResult:
        self.sql.append(sql)
        return _CHResult()


class _PGCursor:
    def __init__(self, conn: _PGConn) -> None:
        self._conn = conn
        self.description: list[object] = []

    def __enter__(self) -> _PGCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self._conn.sql.append(sql)

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


class _PGConn:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def cursor(self) -> _PGCursor:
        return _PGCursor(self)


class _BQResult:
    """An empty row iterator: every assertion here is about the SQL sent."""

    schema: list[object] = []

    def __iter__(self) -> Iterator[object]:
        return iter(())


class _BQJob:
    def result(self, **_kwargs: object) -> _BQResult:
        return _BQResult()


class _BQClient:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def query(self, sql: str) -> _BQJob:
        self.sql.append(sql)
        return _BQJob()


class _DBXCursor:
    """Records each statement with its bound values written back in.

    The adapter sends every analyst value as a named parameter (``:p0``), so the
    statement text alone never carries one. Several assertions below look for a
    value in the SQL; inlining it here keeps them engine-blind. The inlining is
    the fake's, never the adapter's.
    """

    def __init__(self, conn: _DBXConn) -> None:
        self._conn = conn
        self.description: list[tuple[object, ...]] = []

    def execute(self, sql: str, parameters: dict[str, str] | None = None) -> None:
        for name in sorted(parameters or {}, key=len, reverse=True):
            value = (parameters or {})[name].replace("'", "\\'")
            sql = sql.replace(f":{name}", f"'{value}'")
        self._conn.sql.append(sql)
        if self._conn.refusing:
            _refuse_like_a_regex_library(sql, self._conn.refused, self._conn.offline)

    def fetchall(self) -> list[tuple[object, ...]]:
        if self._conn.seed_first:
            return list(_TOP_VALUE_ROWS) if len(self._conn.sql) == 1 else []
        return list(self._conn.rows or [])

    def cancel(self) -> None:
        return None

    def close(self) -> None:
        return None


class _DBXConn:
    def __init__(
        self, *, refusing: bool = False, refused: str | None = None, offline: bool = False
    ) -> None:
        self.sql: list[str] = []
        self.rows: list[tuple[object, ...]] | None = None
        self.refusing = refusing
        self.refused = refused
        self.offline = offline
        # Answer the top-values pre-query (the first statement), as _seeded does.
        self.seed_first = False

    def cursor(self) -> _DBXCursor:
        return _DBXCursor(self)


def _dbx_adapter(conn: _DBXConn) -> DatabricksAdapter:
    adapter = object.__new__(DatabricksAdapter)
    adapter._conn = conn
    adapter._allowed_columns = set(_ALLOWED)
    # Seeded so no DESCRIBE probe lands at sql[0], as for BigQuery.
    adapter._column_types = {"time": "timestamp", "event_name": "string", "amount": "double"}
    adapter._struct_paths = {}
    adapter._catalog = "main"
    return adapter


def _dbx() -> tuple[BaseAdapter, list[str]]:
    conn = _DBXConn()
    return _dbx_adapter(conn), conn.sql


class _SFCursor(_DBXCursor):
    """Snowflake's driver binds ``:1, :2 ...`` (the ``numeric`` paramstyle)."""

    def execute(  # type: ignore[override]
        self, sql: str, params: list[str] | None = None, timeout: int | None = None
    ) -> None:
        del timeout
        values = list(params or [])
        for number in range(len(values), 0, -1):
            value = values[number - 1].replace("'", "\\'")
            sql = sql.replace(f":{number}", f"'{value}'")
        super().execute(sql)


class _SFConn(_DBXConn):
    def cursor(self) -> _SFCursor:
        return _SFCursor(self)


def _sf_adapter(conn: _DBXConn) -> SnowflakeAdapter:
    adapter = object.__new__(SnowflakeAdapter)
    adapter._conn = conn
    adapter._allowed_columns = set(_ALLOWED)
    # Seeded so no describe call is needed, as for BigQuery and Databricks.
    adapter._column_types = {"time": "TIMESTAMP_NTZ", "event_name": "STRING", "amount": "FLOAT"}
    adapter._database = "ANALYTICS"
    return adapter


def _sf() -> tuple[BaseAdapter, list[str]]:
    conn = _SFConn()
    return _sf_adapter(conn), conn.sql


def _ch() -> tuple[BaseAdapter, list[str]]:
    client = _CHClient()
    adapter = object.__new__(ClickHouseAdapter)
    adapter._client = client
    adapter._allowed_columns = set(_ALLOWED)
    adapter._json_path_discovery = "dynamic"
    return adapter, client.sql


def _pg() -> tuple[BaseAdapter, list[str]]:
    conn = _PGConn()
    adapter = object.__new__(PostgresAdapter)
    adapter._conn = conn
    adapter._allowed_columns = set(_ALLOWED)
    return adapter, conn.sql


def _gp() -> tuple[BaseAdapter, list[str]]:
    conn = _PGConn()
    adapter = object.__new__(GreenplumAdapter)
    adapter._conn = conn
    adapter._allowed_columns = set(_ALLOWED)
    return adapter, conn.sql


def _rs() -> tuple[BaseAdapter, list[str]]:
    conn = _PGConn()
    adapter = object.__new__(RedshiftAdapter)
    adapter._conn = conn
    adapter._allowed_columns = set(_ALLOWED)
    return adapter, conn.sql


def _bq() -> tuple[BaseAdapter, list[str]]:
    client = _BQClient()
    adapter = object.__new__(BigQueryAdapter)
    adapter._client = client
    adapter._project = "tripl-test"
    adapter._dataset = "wh"
    adapter._allowed_columns = set(_ALLOWED)
    # Seeded so ``_ensure_column_types`` issues no schema probe; a probe would
    # otherwise land at sql[0] and displace the top-values pre-query under test.
    adapter._column_types = {"time": "TIMESTAMP", "event_name": "STRING", "amount": "FLOAT64"}
    adapter._struct_paths = {}
    adapter._repeated_columns = set()
    return adapter, client.sql


_SQL_ENGINES: dict[str, Callable[[], tuple[BaseAdapter, list[str]]]] = {
    "clickhouse": _ch,
    "postgres": _pg,
    "greenplum": _gp,
    "redshift": _rs,
    "bigquery": _bq,
    "databricks": _dbx,
    "snowflake": _sf,
}


# --------------------------------------------------------------------------- #
# the top-N breakdown contract, and a self-contradicting layout
# --------------------------------------------------------------------------- #


def _multi_aggregate_breakdown(adapter: BaseAdapter) -> None:
    adapter.get_time_bucketed_multi_aggregate_breakdown(
        _BASE, "time", "1d", "event_name", _SPECS, _FROM, _TO, values_limit=3
    )


def _aggregate_breakdown(adapter: BaseAdapter) -> None:
    adapter.get_time_bucketed_aggregate_breakdown(
        _BASE,
        "time",
        "1d",
        MetricAggregation.count,
        None,
        "event_name",
        ["event_name"],
        [],
        None,
        _FROM,
        _TO,
        values_limit=3,
    )


def _breakdown_counts(adapter: BaseAdapter) -> None:
    adapter.get_time_bucketed_breakdown_counts_multi(
        _BASE, "time", "1d", ["event_name"], ["event_name"], [], None, _FROM, _TO, values_limit=3
    )


# The ABC declares four ``values_limit`` methods, and all three SQL adapters
# implement the single-column count one by delegating to the multi one, so these
# three cover every distinct path into the top-values pre-query. Driving all of
# them is the point: a fix applied to one entry point and missed on another is
# the likely partial failure, and a single-method test would wave it by.
_ENTRY_POINTS: dict[str, Callable[[BaseAdapter], None]] = {
    "multi_aggregate_breakdown": _multi_aggregate_breakdown,
    "aggregate_breakdown": _aggregate_breakdown,
    "breakdown_counts_multi": _breakdown_counts,
}

# The tie-break each dialect spells, exactly as it lands in the pre-query.
# ClickHouse partitions with LIMIT BY and so sorts by the column label first;
# the other two rank inside a window function.
_TIE_BREAK = {
    "clickhouse": "ORDER BY _breakdown_column, _cnt DESC, _breakdown_value ",
    "postgres": 'ORDER BY _cnt DESC, _breakdown_value COLLATE "C") AS rn ',
    "greenplum": 'ORDER BY _cnt DESC, _breakdown_value COLLATE "C") AS rn ',
    "redshift": "ORDER BY _cnt DESC, _breakdown_value) AS rn ",
    "bigquery": "ORDER BY _cnt DESC, _breakdown_value) AS rn ",
    "databricks": "ORDER BY _cnt DESC, _breakdown_value) AS rn ",
    "snowflake": "ORDER BY _cnt DESC, _breakdown_value) AS rn ",
}

# How each dialect cuts the list. ``values_limit=3`` must ask for 2, because
# 'Other' occupies the third slot.
_CUT_AT_TWO = {
    "clickhouse": "LIMIT 2 BY _breakdown_column",
    "postgres": "WHERE rn <= 2",
    "greenplum": "WHERE rn <= 2",
    "redshift": "WHERE rn <= 2",
    "bigquery": "WHERE rn <= 2",
    "databricks": "WHERE rn <= 2",
    "snowflake": "WHERE rn <= 2",
}


@pytest.mark.parametrize("engine", sorted(_SQL_ENGINES))
@pytest.mark.parametrize("entry_point", sorted(_ENTRY_POINTS))
def test_top_values_pre_query_breaks_count_ties_by_value(engine: str, entry_point: str) -> None:
    """Every SQL engine ranks on (count DESC, value ASC), on every entry point.

    Red on revert: before the fix each pre-query read ``ORDER BY _cnt DESC`` with
    no second key, so the substring asserted here does not occur at all. Dropping
    the key from one adapter fails three of these nine cases.
    """
    adapter, sql = _SQL_ENGINES[engine]()
    _ENTRY_POINTS[entry_point](adapter)

    pre_query = sql[0]
    assert _TIE_BREAK[engine] in pre_query
    # Ascending, not descending: the in-memory adapter ranks ``(-count, value)``
    # and a DESC here would desynchronize the engines in a new direction.
    assert "_breakdown_value DESC" not in pre_query


@pytest.mark.parametrize("engine", sorted(_SQL_ENGINES))
@pytest.mark.parametrize("entry_point", sorted(_ENTRY_POINTS))
def test_one_of_the_values_limit_slots_is_reserved_for_other(engine: str, entry_point: str) -> None:
    """``values_limit=3`` asks the warehouse for 2 values, in all three dialects.

    This is the behavioural pin for the corrected sentence in ``BaseAdapter``:
    the ABC used to promise the top ``values_limit``. Red if anyone "fixes the
    code to match the old docstring" by dropping the ``- 1`` — the cut would read
    3 and none of these assertions would hold.
    """
    adapter, sql = _SQL_ENGINES[engine]()
    _ENTRY_POINTS[entry_point](adapter)

    assert _CUT_AT_TWO[engine] in sql[0]


@pytest.mark.parametrize("engine", sorted(_SQL_ENGINES))
def test_all_breakdown_entry_points_share_one_ranking_query(engine: str) -> None:
    """Counts, single aggregate and multi-aggregate rank with the SAME statement.

    The three methods must agree on which values are 'Other' or a count series
    and an aggregate series over one column show different sets. Asserting the
    statements are byte-identical is stronger than asserting each contains the
    tie-break separately, and cheaper than reasoning about three call paths.

    Red on revert: the identity holds without the fix too (all three shared the
    untie-broken query), so the tie-break is asserted here as well, and that half
    fails.
    """
    statements = []
    for entry_point in sorted(_ENTRY_POINTS):
        adapter, sql = _SQL_ENGINES[engine]()
        _ENTRY_POINTS[entry_point](adapter)
        statements.append(sql[0])

    assert len(set(statements)) == 1
    assert _TIE_BREAK[engine] in statements[0]


def test_synthetic_breakdown_keeps_values_limit_minus_one_real_values() -> None:
    """The in-memory warehouse, end to end: 3 slots means 2 values plus 'Other'.

    The SQL adapters' cut is asserted on generated text above; this asserts the
    resulting shape on the one adapter that can be run without a warehouse, so
    the contract is pinned behaviourally somewhere and not only as a string.

    Red on revert of the ``- 1``: an unfolded run over this window returns six
    countries, so a cut of 3 would leave three non-'Other' values here.
    """
    adapter = SyntheticAdapter(seed=_SEED, anchor=_ANCHOR, history_days=_HISTORY_DAYS)
    time_from, time_to = _ANCHOR - timedelta(days=20), _ANCHOR
    specs = [AggregateSpec(key="cnt", aggregation=MetricAggregation.count)]

    _names, unfolded = adapter.get_time_bucketed_multi_aggregate_breakdown(
        "SELECT * FROM orders", "created_at", "1d", "country", specs, time_from, time_to
    )
    # Precondition, asserted rather than assumed: the window must hold more
    # values than the limit or the fold has nothing to prove.
    assert len({row[1] for row in unfolded}) >= 4
    assert {row[2] for row in unfolded} == {0}

    _names, folded = adapter.get_time_bucketed_multi_aggregate_breakdown(
        "SELECT * FROM orders",
        "created_at",
        "1d",
        "country",
        specs,
        time_from,
        time_to,
        values_limit=3,
    )
    assert len({row[1] for row in folded if row[2] == 0}) == 2
    assert any(row[2] == 1 for row in folded)


def test_the_tie_break_direction_the_sql_adapters_copy_is_the_in_memory_one() -> None:
    """The oracle keeps the code-point-SMALLER value when counts tie.

    Two values tie at the cutoff here: ``'a'`` and ``'z'`` both appear once and
    only one slot is left. The in-memory adapter keeps ``'a'``, which is why the
    three SQL adapters sort the value ASCENDING; sorting it descending would be
    just as deterministic and just as wrong.

    Red on revert: the SQL half asserts the ascending key is present, and before
    the fix there was no value key in any of the three statements.
    """
    adapter = SyntheticAdapter(seed=_SEED, anchor=_ANCHOR, history_days=_HISTORY_DAYS)
    tied = [{"c": "b"}, {"c": "b"}, {"c": "a"}, {"c": "z"}]

    kept = adapter._top_values(tied, "c", 3)

    assert kept == {"b", "a"}
    for engine, build in _SQL_ENGINES.items():
        sql_adapter, sql = build()
        _multi_aggregate_breakdown(sql_adapter)
        assert _TIE_BREAK[engine] in sql[0]


# Methods whose return tuple carries ``json_value_names``, and which therefore
# emit one row column per kept JSON path. Found by grepping the ABC for the
# 3- and 4-tuple returns that include that element.
_KEEPS_JSON_VALUES = (
    "get_full_breakdown",
    "get_time_bucketed_counts",
    "get_time_bucketed_breakdown_counts",
    "get_time_bucketed_breakdown_counts_multi",
    "get_time_bucketed_aggregate",
    "get_time_bucketed_aggregate_breakdown",
)

# The multi-aggregate pair deliberately returns no json element, so documenting
# the slot there would be the mirror-image lie.
_KEEPS_NO_JSON_VALUES = (
    "get_time_bucketed_multi_aggregate",
    "get_time_bucketed_multi_aggregate_breakdown",
)


def test_every_method_that_returns_json_value_names_documents_the_row_slot() -> None:
    """The ABC's row layouts must not omit a column the implementations emit.

    Asserting prose is unusual here, and justified for this one file: the ABC's
    docstrings are the only specification an adapter author reads, so a layout
    that lists the columns and skips one is the defect itself, not a description
    of it.

    Red on revert: the two aggregate methods' layouts named the regular and
    JSON-path columns and then jumped straight to the aggregate value, so
    ``keep_json_value`` appeared in four docstrings out of six.
    """
    for name in _KEEPS_JSON_VALUES:
        doc = inspect.getdoc(getattr(BaseAdapter, name))
        assert doc is not None
        assert "keep_json_value" in doc, name

    for name in _KEEPS_NO_JSON_VALUES:
        doc = inspect.getdoc(getattr(BaseAdapter, name))
        assert doc is not None
        assert "keep_json_value" not in doc, name


def test_the_top_n_cut_is_stated_in_exactly_one_place() -> None:
    """One number, one home, and every ``values_limit`` method points at it.

    The defect was two live copies of this rule disagreeing, so the invariant
    worth pinning is not the wording but the absence of a second copy: a method
    that restates the cut is free to drift from it again.

    Red on revert: ``BaseAdapter`` carried no class docstring at all, and
    ``get_time_bucketed_aggregate_breakdown`` restated ``values_limit - 1``
    while ``get_time_bucketed_multi_aggregate_breakdown`` restated it wrongly.
    """
    contract = BaseAdapter.__doc__
    assert contract is not None
    assert "values_limit - 1" in contract

    methods = [
        name
        for name, member in inspect.getmembers(BaseAdapter, inspect.isfunction)
        if "values_limit" in inspect.signature(member).parameters
    ]
    # Guard the guard: if the ABC loses its breakdown methods this test must not
    # silently pass by iterating nothing.
    assert len(methods) == 4

    for name in methods:
        doc = inspect.getdoc(getattr(BaseAdapter, name))
        assert doc is not None
        assert "values_limit - 1" not in doc, name
        assert "top-N contract" in doc, name


# --------------------------------------------------------------------------- #
# 'Other' is ONE row, on every engine
#
# ``get_time_bucketed_aggregate_breakdown`` grouped by the folded value AND by
# the raw breakdown column, which it also carries as a regular column — the
# three SQL adapters require the breakdown to be one, and the single production
# caller passes it as the only one. So the ``'Other'`` rollup came back split one
# row per raw value that fell into it, and a nullable column's NULL and ``''``
# rows stayed apart even though both render to ``''``. The breakdown column now
# keeps its positional slot but carries the FOLDED value there and is no longer a
# grouping key, which is what ``get_time_bucketed_multi_aggregate_breakdown``
# already did — it is the sibling these tests measure against below.
# --------------------------------------------------------------------------- #


def _split_terms(text: str) -> list[str]:
    """Split a SELECT or GROUP BY list on its TOP-LEVEL commas.

    Substring matching is not usable for this assertion: the folded expression
    contains the raw column, so ``"`event_name`" in clause`` is true either way.
    The question is whether the raw column is a term of its own, which needs the
    list actually split. Commas inside ``IN ('a', 'b')`` and inside function
    calls sit at paren depth > 0; a doubled quote inside a literal toggles
    ``in_literal`` twice and so cancels out, which is exactly how the adapters'
    own ``_quote_string`` escapes one.
    """
    terms: list[str] = []
    depth = 0
    start = 0
    in_literal = False
    for index, char in enumerate(text):
        if char == "'":
            in_literal = not in_literal
        elif in_literal:
            continue
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            terms.append(text[start:index].strip())
            start = index + 1
    terms.append(text[start:].strip())
    return terms


_TRAILING_ALIAS = re.compile(r"""\s+AS\s+(?:`[^`]+`|"[^"]+"|[A-Za-z_]\w*)$""")


def _select_terms(sql: str) -> list[str]:
    """The outer SELECT list. The outer ``FROM`` is the first one: the select
    list holds no subquery, while ``base_query`` is wrapped after it."""
    body = sql[sql.index("SELECT ") + len("SELECT ") : sql.index(" FROM ")]
    return _split_terms(body)


def _grouping_keys(engine: str, sql: str) -> frozenset[str]:
    """What the statement actually groups on, as a set, per dialect.

    ClickHouse writes ``GROUP BY ALL``, so its grouping keys are its own
    non-aggregate SELECT terms — every term but the trailing aggregate, of
    which both methods driven here emit exactly one — read without their
    aliases. Postgres and BigQuery spell a list, and group by the
    aliases bound earlier in the same SELECT. A set is the right shape on both
    sides: the fix deliberately emits the folded expression twice under two
    names, and two keys that always hold the same value are one group.
    """
    if engine == "clickhouse":
        assert " GROUP BY ALL " in sql, sql
        return frozenset(_TRAILING_ALIAS.sub("", term) for term in _select_terms(sql)[:-1])
    match = re.search(r" GROUP BY (.*?) ORDER BY ", sql)
    assert match is not None, sql
    return frozenset(_split_terms(match.group(1)))


#: A bare, optionally-backticked column reference — the ONLY expression shape
#: ZetaSQL will match against an identical GROUP BY term. Anything computed has
#: to be grouped by its alias instead.
_BARE_COLUMN = re.compile(r"`[^`]+`|[A-Za-z_]\w*")


def _folded_expression(sql: str) -> str:
    """Whatever ``_breakdown_value`` is bound to, read out of the statement."""
    term = next(t for t in _select_terms(sql) if t.endswith(" AS _breakdown_value"))
    return _TRAILING_ALIAS.sub("", term)


def _grouping_groups(engine: str, sql: str) -> frozenset[str]:
    """The distinct groups the statement forms, with re-spellings collapsed.

    A dialect may require the folded expression to appear in the grouping more
    than once under different names. BigQuery does: ZetaSQL will not match a
    repeated expression against the grouping key an alias is bound to, and
    answers ``SELECT list expression references column <name> which is neither
    grouped nor aggregated`` — it refuses to analyze the statement at all, which
    is how the ZetaSQL gate caught it after the fake-client tests passed.

    Those repeats are not extra groups. Two keys that always hold the same value
    cut the rows exactly the same way, so a key that IS the folded expression is
    read here as ``_breakdown_value``, the name it is bound to. What comes back
    then describes how the rows are actually divided rather than how many times
    a dialect made us spell it — which is the property these tests are about.
    """
    folded = _folded_expression(sql)
    # alias -> the expression it is bound to, so a key naming an alias can be
    # resolved instead of compared as text. BigQuery groups the breakdown slot
    # by its ALIAS: ZetaSQL will not match a repeated expression against an
    # identical one in GROUP BY, measured against the real analyzer, so naming
    # the alias is the only spelling it accepts.
    bound: dict[str, str] = {}
    for term in _select_terms(sql):
        expr = _TRAILING_ALIAS.sub("", term)
        alias = term[len(expr) :].strip()
        if alias.upper().startswith("AS "):
            bound[alias[3:].strip().strip('`"')] = expr

    raw_term = _RAW_BREAKDOWN_TERM[engine]

    def _resolve(key: str) -> str:
        if key == raw_term:
            # Never resolved, whatever it is bound to. A key spelling the raw
            # column is the thing these tests exist to catch, and on BigQuery an
            # alias may legally carry that name — resolving it away would let
            # "the raw column is not grouped" be satisfied BY the raw column,
            # which is how a revert to the shadowing alias slipped past every
            # test in this file.
            return key
        if key == folded:
            return "_breakdown_value"
        if bound.get(key.strip('`"')) == folded:
            return "_breakdown_value"
        return key

    return frozenset(_resolve(key) for key in _grouping_keys(engine, sql))


# The raw breakdown column as a bare grouping term would be spelled exactly this.
_RAW_BREAKDOWN_TERM = {
    "clickhouse": "`event_name`",
    "postgres": '"event_name"',
    "greenplum": '"event_name"',
    "redshift": '"event_name"',
    "bigquery": "`event_name`",
    "databricks": "`event_name`",
    "snowflake": '"event_name"',
}

# The top-values pre-query returns ``(column, value)`` pairs in all three
# adapters, so one seed serves them all.
_TOP_VALUE_ROWS: list[tuple[object, ...]] = [("event_name", "click"), ("event_name", "view")]


class _SeededCHClient(_CHClient):
    def query(self, sql: str, **kwargs: object) -> _CHResult:
        result = super().query(sql, **kwargs)
        if len(self.sql) == 1:
            result.result_rows = list(_TOP_VALUE_ROWS)
        return result


class _SeededPGCursor(_PGCursor):
    def fetchall(self) -> list[tuple[object, ...]]:
        return list(_TOP_VALUE_ROWS) if len(self._conn.sql) == 1 else []


class _SeededPGConn(_PGConn):
    def cursor(self) -> _SeededPGCursor:
        return _SeededPGCursor(self)


class _SeededBQRow:
    def __init__(self, values: tuple[object, ...]) -> None:
        self._values = values

    def values(self) -> tuple[object, ...]:
        return self._values


class _SeededBQResult:
    schema: list[object] = []

    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[object]:
        return iter(_SeededBQRow(row) for row in self._rows)


class _SeededBQJob:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def result(self, **_kwargs: object) -> _SeededBQResult:
        return _SeededBQResult(self._rows)


class _SeededBQClient(_BQClient):
    def query(self, sql: str) -> _SeededBQJob:
        self.sql.append(sql)
        return _SeededBQJob(list(_TOP_VALUE_ROWS) if len(self.sql) == 1 else [])


def _seeded(engine: str) -> tuple[BaseAdapter, list[str]]:
    """The same real adapter, but with the top-values pre-query answered.

    Built by swapping the connection on the shared builder rather than by
    repeating its attribute seeding, so a change to how an adapter is entered
    lands in one place. Unseeded, the pre-query returns nothing and every value
    folds to the constant ``'Other'``; that degenerate shape is worth testing
    too (it is parametrized below) but it never exercises the CASE/if the fold
    normally compiles to.
    """
    adapter, _ = _SQL_ENGINES[engine]()
    if engine == "clickhouse":
        ch_client = _SeededCHClient()
        adapter._client = ch_client
        return adapter, ch_client.sql
    if engine in ("postgres", "greenplum", "redshift"):
        pg_conn = _SeededPGConn()
        adapter._conn = pg_conn
        return adapter, pg_conn.sql
    if engine == "databricks":
        dbx_conn = _DBXConn()
        dbx_conn.seed_first = True
        adapter._conn = dbx_conn
        return adapter, dbx_conn.sql
    if engine == "snowflake":
        sf_conn = _SFConn()
        sf_conn.seed_first = True
        adapter._conn = sf_conn
        return adapter, sf_conn.sql
    bq_client = _SeededBQClient()
    adapter._client = bq_client
    return adapter, bq_client.sql


# (builder, values_limit). The three shapes the fold compiles to, because each
# one reaches the reg-column loop with a different ``breakdown_expr``:
# the bare string rendering, a real CASE/if over kept values, and the constant
# ``'Other'`` when the pre-query came back empty.
_FOLD_SHAPES: dict[str, tuple[Callable[[str], tuple[BaseAdapter, list[str]]], int | None]] = {
    "unfolded": (lambda engine: _SQL_ENGINES[engine](), None),
    "case_over_kept_values": (_seeded, 3),
    "everything_is_other": (lambda engine: _SQL_ENGINES[engine](), 3),
}


def _call_aggregate_breakdown(adapter: BaseAdapter, values_limit: int | None) -> None:
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
        values_limit=values_limit,
    )


@pytest.mark.parametrize("engine", sorted(_SQL_ENGINES))
@pytest.mark.parametrize("fold_shape", sorted(_FOLD_SHAPES))
def test_aggregate_breakdown_never_groups_the_raw_breakdown_column(
    engine: str, fold_shape: str
) -> None:
    """Three keys, none of them the raw column, on every engine and every fold.

    Red on revert: the raw column goes back into the grouping — a further
    Postgres/BigQuery GROUP BY term, and a further non-aggregate SELECT term
    under ClickHouse's ``GROUP BY ALL`` — so both assertions fail. Reverting one
    adapter fails three of these nine cases.

    Read through :func:`_grouping_groups`, so a dialect that has to spell the
    folded expression twice still reports three GROUPS. The raw column is not
    the folded expression in any dialect, so it never collapses and the absence
    assertion keeps its teeth.
    """
    build, values_limit = _FOLD_SHAPES[fold_shape]
    adapter, sql = build(engine)
    _call_aggregate_breakdown(adapter, values_limit)

    groups = _grouping_groups(engine, sql[-1])
    assert _RAW_BREAKDOWN_TERM[engine] not in groups, groups
    # Exactly the bucket, the folded value and the is_other flag. Asserted as a
    # count as well as an absence, so a fix that renamed the raw term instead of
    # removing it would still be caught.
    assert len(groups) == 3, groups


@pytest.mark.parametrize("engine", sorted(_SQL_ENGINES))
def test_the_breakdown_columns_own_slot_repeats_the_folded_value(engine: str) -> None:
    """The row keeps its width; the breakdown slot just stops being raw.

    The projected column is asserted to be byte-identical to whatever
    ``_breakdown_value`` is bound to, read out of the statement rather than
    re-spelled here, so the test says "the same expression" instead of
    re-encoding three dialects' fold syntax and drifting from them.

    Red on revert: the slot was the bare raw column, which is not the folded
    expression in any of the three dialects.
    """
    adapter, sql = _seeded(engine)
    _call_aggregate_breakdown(adapter, 3)

    terms = _select_terms(sql[-1])
    folded = next(term for term in terms if term.endswith(" AS _breakdown_value"))
    folded = _TRAILING_ALIAS.sub("", folded)
    # Precondition, asserted rather than assumed: with the pre-query seeded the
    # fold really is a conditional and not the degenerate constant.
    assert folded != "'Other'", folded
    # The ALIAS differs by dialect and is cosmetic — consumers read these rows
    # positionally and take their names from col_names. BigQuery deliberately
    # does NOT alias the slot to the column's own name: that would shadow the
    # source column, and GoogleSQL resolves GROUP BY names against SELECT
    # aliases first, so the grouping term would bind to this slot instead of to
    # the column and ZetaSQL would reject the statement. What this test pins is
    # that the slot carries the FOLDED value rather than the raw column.
    slot = next(
        term for term in terms if term.startswith(f"{folded} AS ") and "_breakdown" not in term
    )
    assert _TRAILING_ALIAS.sub("", slot) == folded, slot
    if engine == "bigquery":
        # The one dialect where the alias is load-bearing rather than cosmetic,
        # so it is asserted rather than skipped. Aliasing the slot to the
        # column's own name analyzes perfectly well — measured — but then a
        # grouping term naming it could mean the source column or this slot, and
        # those hold DIFFERENT values (raw vs folded). Grouping by the raw one is
        # the defect being removed, so the name must be unambiguous.
        assert not slot.endswith(f"AS {_RAW_BREAKDOWN_TERM[engine]}"), slot
    else:
        assert f"{folded} AS {_RAW_BREAKDOWN_TERM[engine]}" in terms, terms


@pytest.mark.parametrize("engine", sorted(_SQL_ENGINES))
def test_aggregate_breakdown_groups_exactly_like_its_batched_sibling(engine: str) -> None:
    """The idiom being copied, pinned as an equality rather than by eye.

    ``get_time_bucketed_multi_aggregate_breakdown`` groups by (bucket, folded
    value, is_other) and nothing else; both methods run over the same window
    with the same seeded top values, so their grouping keys must come out equal.
    Comparing the two is stronger than asserting a hard-coded list on each:
    if a later change moves the sibling, this notices instead of quietly
    letting the pair drift apart again.

    Red on revert: the single-aggregate side gains the raw column as a further
    key, the batched side has no regular columns and cannot, so the sets differ.

    Compared as GROUPS rather than as literal keys: the single-aggregate side
    also projects the breakdown column's own slot, which on BigQuery forces the
    folded expression into the grouping a second time, and the batched side has
    no such slot. That is a difference in spelling, not in how the rows divide,
    and the equality being asserted here is about the latter.
    """
    adapter, sql = _seeded(engine)
    _call_aggregate_breakdown(adapter, 3)
    single = _grouping_groups(engine, sql[-1])

    sibling_adapter, sibling_sql = _seeded(engine)
    sibling_adapter.get_time_bucketed_multi_aggregate_breakdown(
        _BASE, "time", "1d", "event_name", _SPECS, _FROM, _TO, values_limit=3
    )
    batched = _grouping_groups(engine, sibling_sql[-1])

    assert single == batched, (single, batched)


def test_bigquery_groups_every_non_aggregate_it_projects() -> None:
    """ZetaSQL's rule, checked without ZetaSQL.

    BigQuery refuses to analyze a statement whose SELECT list holds a
    non-aggregate expression the GROUP BY does not also hold. It does NOT accept
    the expression the alias of an existing grouping key is bound to — which is
    the mistake this file's first version shipped, and which every fake-client
    test here passed straight over, because a fake client answers any string.

    So the property is pinned here in the only way a fake can: every projected
    non-aggregate expression must appear in the GROUP BY list, either literally
    or as the alias it is bound to. That is the analyzer's rule restated, not
    the current code restated, which is why it would also have caught the
    original defect.

    Red on revert: drop ``group_parts.append(breakdown_expr)`` from
    ``get_time_bucketed_aggregate_breakdown`` and the breakdown column's own
    projected slot is a non-aggregate that nothing groups, exactly as the
    ZetaSQL gate reported. Without this test the revert is invisible to every
    test that runs on a laptop.
    """
    adapter, sql = _seeded("bigquery")
    _call_aggregate_breakdown(adapter, 3)
    statement = sql[-1]

    match = re.search(r" GROUP BY (.*?) ORDER BY ", statement)
    assert match is not None, statement
    grouped = set(_split_terms(match.group(1)))

    ungrouped = []
    for term in _select_terms(statement):
        expr = _TRAILING_ALIAS.sub("", term)
        # _TRAILING_ALIAS does not capture, so take the alias as what it strips.
        alias = term[len(expr) :].strip()
        alias = alias[3:].strip() if alias.upper().startswith("AS ") else ""
        if expr.startswith(("sum(", "count(", "avg(", "min(", "max(")):
            continue  # an aggregate needs no grouping key
        if alias and alias.strip("`") in {key.strip("`") for key in grouped}:
            continue  # grouped by the alias bound to it — always accepted
        if _BARE_COLUMN.fullmatch(expr) and expr in grouped:
            continue  # a bare column reference matches itself in GROUP BY
        # Anything else is NOT grouped as far as ZetaSQL is concerned — in
        # particular a COMPUTED expression repeated verbatim in GROUP BY, which
        # reads as grouped to the eye and is rejected by the analyzer. Measured
        # against the emulator CI runs: the same IFNULL(CAST(...)) in both lists
        # is refused, the alias bound to it is accepted. Accepting the repeat
        # here is what made an earlier version of this test wave through the
        # exact defect it was written to catch.
        ungrouped.append(term)

    assert not ungrouped, (
        "BigQuery will refuse to analyze this statement: these projected "
        f"non-aggregates are not grouped: {ungrouped}\nGROUP BY {sorted(grouped)}"
    )


# --- the same fix, executed: the in-memory warehouse is the only one that runs --


_SEEDED_EVENT_TIME = _ANCHOR - timedelta(days=2)


def _seeded_event(button_id: str | None, offset_minutes: int) -> dict[str, object]:
    """One synthetic ``events`` row, every column of the table filled.

    ``button_id`` is one of the table's nullable columns, which is what lets the
    NULL-vs-``''`` half of this finding be exercised at all.
    """
    return {
        "event_time": _SEEDED_EVENT_TIME + timedelta(minutes=offset_minutes),
        "event_type": "click_event",
        "event_name": "tap",
        "screen_name": "home",
        "platform": "ios",
        "button_id": button_id,
        "product_id": "p1",
        "amount": 1.0,
        "currency": "USD",
        "app_version": "1.0.0",
        "user_id": "u1",
        "session_id": "s1",
    }


def _adapter_over(counts: tuple[tuple[str | None, int], ...]) -> SyntheticAdapter:
    """A synthetic adapter whose ``events`` table is exactly ``counts``.

    The generated dataset has no column holding both NULL and ``''``, and no
    cheap way to force a chosen count per value, so the rows are supplied
    directly. Everything else — bucketing, folding, aggregation — is the real
    adapter's.
    """
    adapter = SyntheticAdapter(seed=_SEED, anchor=_ANCHOR, history_days=_HISTORY_DAYS)
    values = [value for value, count in counts for _ in range(count)]
    adapter._events = [_seeded_event(value, offset) for offset, value in enumerate(values)]
    return adapter


def _button_breakdown(
    adapter: SyntheticAdapter, values_limit: int | None
) -> list[tuple[object, ...]]:
    _cols, _json, rows = adapter.get_time_bucketed_aggregate_breakdown(
        "SELECT * FROM events",
        "event_time",
        "1d",
        MetricAggregation.count,
        None,
        "button_id",
        ["button_id"],
        [],
        None,
        _ANCHOR - timedelta(days=5),
        _ANCHOR,
        values_limit=values_limit,
    )
    return rows


def test_other_comes_back_as_one_row_per_bucket() -> None:
    """The finding itself, executed end to end on the in-memory warehouse.

    ``values_limit=3`` keeps 'a' and 'b'; 'c', 'd' and the NULL/'' rows all fold
    into ``'Other'``, which must be ONE row carrying their combined count.

    Red on revert: the raw value re-enters the group key and this bucket comes
    back with four ``is_other == 1`` rows (``c``, ``d``, ``None``, ``''``) of
    counts 2, 1, 1, 1 instead of one row of 5. That fan-out is also a
    CardinalityViolation waiting for the caller:
    ``_upsert_metric_value_breakdown_rows`` keys on (definition, config, bucket,
    breakdown_column, breakdown_value, is_other), and the raw value is not one
    of them, so four such rows in one INSERT ... ON CONFLICT collide.
    """
    adapter = _adapter_over((("a", 4), ("b", 3), ("c", 2), ("d", 1), (None, 1), ("", 1)))

    rows = _button_breakdown(adapter, 3)

    other = [row for row in rows if row[2] == 1]
    assert len(other) == 1, rows
    assert other[0][1] == "Other"
    assert other[0][-1] == 5
    # The kept values are untouched by the fix.
    assert {(row[1], row[-1]) for row in rows if row[2] == 0} == {("a", 4), ("b", 3)}


def test_null_and_empty_string_are_one_breakdown_row_not_two() -> None:
    """The second half of the finding, and it needs no ``values_limit`` at all.

    Both NULL and ``''`` render to ``''`` in every engine —
    ``ifNull(toString(x), '')`` on ClickHouse, ``COALESCE(x::text, '')`` on
    Postgres, ``IFNULL(CAST(x AS STRING), '')`` on BigQuery, ``_bval`` here — so
    they are one breakdown value and must be one row. Grouping by the raw made
    them two rows sharing a breakdown value, which is the same duplicate-key
    collision as the ``'Other'`` fan-out but reachable without any folding.

    Red on revert: two rows come back, ``(bucket, '', 0, None, 1)`` and
    ``(bucket, '', 0, '', 1)``.
    """
    adapter = _adapter_over((("a", 2), (None, 1), ("", 1)))

    rows = _button_breakdown(adapter, None)

    empty = [row for row in rows if row[1] == ""]
    assert len(empty) == 1, rows
    assert empty[0][-1] == 2
    # And the regular-column slot carries the folded value, not one of the two
    # raw ones — the row layout BaseAdapter now states.
    assert empty[0][3] == ""


def test_the_breakdown_slot_is_the_folded_value_on_every_row() -> None:
    """The row-layout half of the contract, on the adapter that returns rows.

    The SQL engines are pinned on generated text above; this is the same claim
    executed. It is worth its own test because it is the assertion a reviewer
    would otherwise have to take from the docstring: column 3 is not merely
    *consistent* with column 1, it is the same value.

    Red on revert: the slot carries the raw value, so the NULL rows show
    ``None`` there against a ``''`` breakdown value on the unfolded pass, and
    ``c`` / ``None`` against ``'Other'`` on the folded one.
    """
    adapter = _adapter_over((("a", 4), ("b", 3), ("c", 2), (None, 1)))

    for values_limit in (None, 3):
        rows = _button_breakdown(adapter, values_limit)
        assert rows
        for row in rows:
            assert row[3] == row[1], (values_limit, row)


def test_the_grouping_rule_is_stated_where_the_contract_lives() -> None:
    """One sentence, in the ABC, because four implementations must obey it.

    The same argument as the top-N cut above: these docstrings are the entire
    specification an adapter author reads, and a rule kept only in three
    adapters' comments is a rule the fourth author never sees.

    Red on revert: the method's docstring said only that it mirrors the count
    path, and said nothing about which column may be a grouping key.
    """
    doc = inspect.getdoc(BaseAdapter.get_time_bucketed_aggregate_breakdown)
    assert doc is not None
    assert "FOLDED value ONLY" in doc
    # The count path deliberately still groups raw, and the ABC has to say so or
    # the next reader "fixes" the inconsistency in the wrong direction.
    assert "get_time_bucketed_breakdown_counts(_multi) still" in doc


# --------------------------------------------------------------------------- #
# a filtered count_distinct: 0 is a value, absent is a gap
#
# ``NULLIF(count(DISTINCT m) FILTER (WHERE cond), 0)`` asks the aggregate a
# question only a row count can answer. A distinct count is 0 both for a bucket
# nothing matched and for a bucket whose matching rows all have ``m IS NULL``,
# so PostgreSQL and BigQuery reported the second as a gap — the caller drops
# NULL cells — while ClickHouse, which gates on ``countIf(cond)``, stored the 0.
# Every engine now gates on a count of matching ROWS.
# --------------------------------------------------------------------------- #


_FILTER = "amount > 0"
_DISTINCT_FILTERED = AggregateSpec(
    key="d",
    aggregation=MetricAggregation.count_distinct,
    column="event_name",
    filter_sql=_FILTER,
)
# One statement carrying all four shapes, so a change to the filtered distinct
# count cannot quietly reshape its neighbours: a plain filtered count, a filtered
# sum (gated on ClickHouse only — see ``_FILTERED_SUM`` for why that is correct
# rather than a divergence) and an UNFILTERED distinct count, which this part of
# the contract does not reach at all.
_SPECS_57 = [
    _DISTINCT_FILTERED,
    AggregateSpec(key="c", aggregation=MetricAggregation.count, filter_sql=_FILTER),
    AggregateSpec(key="s", aggregation=MetricAggregation.sum, column="amount", filter_sql=_FILTER),
    AggregateSpec(key="u", aggregation=MetricAggregation.count_distinct, column="event_name"),
]


def _flat_multi_aggregate(adapter: BaseAdapter, sql: list[str]) -> str:
    adapter.get_time_bucketed_multi_aggregate(_BASE, "time", "1d", _SPECS_57, _FROM, _TO)
    return sql[-1]


def _breakdown_multi_aggregate(adapter: BaseAdapter, sql: list[str]) -> str:
    adapter.get_time_bucketed_multi_aggregate_breakdown(
        _BASE, "time", "1d", "event_name", _SPECS_57, _FROM, _TO, values_limit=3
    )
    # The top-values pre-query lands first; the aggregate statement is last.
    return sql[-1]


def _disclosed_multi_aggregate(adapter: BaseAdapter, sql: list[str]) -> str:
    # The statement the user is SHOWN (services/metric_preview_service.py, via
    # core/adapters/multi_aggregate_sql.py). It executes nothing, so it is read
    # from the return value rather than from the capture list.
    _col_names, statement = adapter.build_time_bucketed_multi_aggregate_sql(
        _BASE, "time", "1d", _SPECS_57, _FROM, _TO
    )
    return statement


# Three entry points reach ``_spec_aggregate_sql`` / ``_conditional_aggregate_sql``
# on each SQL adapter. The scheduler runs the first two
# (worker/tasks/metrics/metric_collect.py:890, :1911, :1944) and the third is what
# the preview discloses. On all three adapters today the flat read delegates to
# the disclosure builder, so those two statements are identical by construction —
# which is the point of asserting both rather than an accident of it: the day they
# stop sharing a builder, the disclosed statement quietly stops being the executed
# one, and the breakdown is a third builder that already does not share.
_SPEC_ENTRY_POINTS: dict[str, Callable[[BaseAdapter, list[str]], str]] = {
    "multi_aggregate": _flat_multi_aggregate,
    "multi_aggregate_breakdown": _breakdown_multi_aggregate,
    "disclosed_sql": _disclosed_multi_aggregate,
}

# The gate each dialect spells for a FILTERED count_distinct.
_DISTINCT_GATE = {
    "clickhouse": "if(countIf(amount > 0) = 0, NULL, uniqExactIf(`event_name`, amount > 0))",
    "postgres": (
        "CASE WHEN count(*) FILTER (WHERE amount > 0) = 0 "
        'THEN NULL ELSE count(DISTINCT "event_name") FILTER (WHERE amount > 0) END'
    ),
    "greenplum": (
        "CASE WHEN count(*) FILTER (WHERE amount > 0) = 0 "
        'THEN NULL ELSE count(DISTINCT "event_name") FILTER (WHERE amount > 0) END'
    ),
    "redshift": (
        "CASE WHEN count(CASE WHEN amount > 0 THEN 1 END) = 0 "
        'THEN NULL ELSE count(DISTINCT CASE WHEN amount > 0 THEN "event_name" END) END'
    ),
    "bigquery": (
        "CASE WHEN COUNTIF(amount > 0) = 0 "
        "THEN NULL ELSE count(DISTINCT IF(amount > 0, `event_name`, NULL)) END"
    ),
    "databricks": (
        "CASE WHEN count_if(amount > 0) = 0 "
        "THEN NULL ELSE count(DISTINCT IF(amount > 0, `event_name`, NULL)) END"
    ),
    "snowflake": (
        "CASE WHEN COUNT_IF(amount > 0) = 0 "
        'THEN NULL ELSE COUNT(DISTINCT IFF(amount > 0, "event_name", NULL)) END'
    ),
}

# The row-presence probe alone: a COUNT OF ROWS compared to zero. The ``= 0``
# matters — without it the PostgreSQL fragment also matches the plain filtered
# count's ``NULLIF(count(*) FILTER (WHERE amount > 0), 0)``, which is there
# either way.
_ROW_PRESENCE_PROBE = {
    "clickhouse": "countIf(amount > 0) = 0",
    "postgres": "count(*) FILTER (WHERE amount > 0) = 0",
    "greenplum": "count(*) FILTER (WHERE amount > 0) = 0",
    "redshift": "count(CASE WHEN amount > 0 THEN 1 END) = 0",
    "bigquery": "COUNTIF(amount > 0) = 0",
    "databricks": "count_if(amount > 0) = 0",
    "snowflake": "COUNT_IF(amount > 0) = 0",
}

# The plain filtered count keeps the compact spelling: its filtered value IS the
# row-presence count, so NULLIF asks the same question in half the text.
_PLAIN_COUNT = {
    "clickhouse": "if(countIf(amount > 0) = 0, NULL, countIf(amount > 0)) AS `c`",
    "postgres": 'NULLIF(count(*) FILTER (WHERE amount > 0), 0) AS "c"',
    "greenplum": 'NULLIF(count(*) FILTER (WHERE amount > 0), 0) AS "c"',
    "redshift": 'NULLIF(count(CASE WHEN amount > 0 THEN 1 END), 0) AS "c"',
    "bigquery": "NULLIF(count(CASE WHEN amount > 0 THEN 1 END), 0) AS `c`",
    "databricks": "NULLIF(count_if(amount > 0), 0) AS `c`",
    "snowflake": 'NULLIF(COUNT_IF(amount > 0), 0) AS "c"',
}

# An unfiltered spec is unconditional and must stay exactly what the
# single-aggregate path emits.
_UNFILTERED_DISTINCT = {
    "clickhouse": "count(DISTINCT `event_name`) AS `u`",
    "postgres": 'count(DISTINCT "event_name") AS "u"',
    "greenplum": 'count(DISTINCT "event_name") AS "u"',
    "redshift": 'count(DISTINCT "event_name") AS "u"',
    "bigquery": "count(DISTINCT `event_name`) AS `u`",
    "databricks": "count(DISTINCT `event_name`) AS `u`",
    "snowflake": 'count(DISTINCT "event_name") AS "u"',
}

# A filtered sum, where the engines legitimately differ and the difference is
# documented rather than accidental: PostgreSQL's ``FILTER`` and BigQuery's
# ``CASE WHEN`` both return NULL over zero matching rows, so neither needs a
# gate, while ClickHouse's ``sumIf`` returns 0 for an empty group and so carries
# the same gate as its counts. All three agree on the VALUE, which is the only
# thing the contract asks of them.
_FILTERED_SUM = {
    "clickhouse": "if(countIf(amount > 0) = 0, NULL, sumIf(`amount`, amount > 0)) AS `s`",
    "postgres": 'sum("amount") FILTER (WHERE amount > 0) AS "s"',
    "greenplum": 'sum("amount") FILTER (WHERE amount > 0) AS "s"',
    "redshift": 'sum(CASE WHEN amount > 0 THEN "amount" END) AS "s"',
    "bigquery": "sum(CASE WHEN amount > 0 THEN `amount` END) AS `s`",
    "databricks": "sum(CASE WHEN amount > 0 THEN `amount` END) AS `s`",
    "snowflake": 'sum(CASE WHEN amount > 0 THEN "amount" END) AS "s"',
}


@pytest.mark.parametrize("engine", sorted(_SQL_ENGINES))
@pytest.mark.parametrize("entry_point", sorted(_SPEC_ENTRY_POINTS))
def test_a_filtered_distinct_count_is_gated_on_rows_not_on_its_own_zero(
    engine: str, entry_point: str
) -> None:
    """Every SQL engine, every entry point: the gate counts rows.

    Red on revert: PostgreSQL and BigQuery emit ``NULLIF(count(DISTINCT ...), 0)``
    again, which is the banned substring here and is not the gate asserted here,
    so six of these nine cases fail. The three ClickHouse cases are the control —
    they were already right, and they fail only if someone "unifies" the engines
    by moving the reference in the wrong direction.
    """
    adapter, sql = _SQL_ENGINES[engine]()
    statement = _SPEC_ENTRY_POINTS[entry_point](adapter, sql)

    assert _DISTINCT_GATE[engine] in statement
    # The defect itself, named: a distinct count compared to 0 cannot tell "no
    # matching rows" from "matching rows, measure NULL on all of them".
    assert "NULLIF(count(DISTINCT" not in statement
    # The neighbours are untouched: an unfiltered spec is unconditional, and a
    # filtered sum keeps whichever of the two documented shapes its engine uses.
    assert _UNFILTERED_DISTINCT[engine] in statement
    assert _FILTERED_SUM[engine] in statement


@pytest.mark.parametrize("engine", sorted(_SQL_ENGINES))
def test_count_and_count_distinct_are_spelled_differently_on_purpose(engine: str) -> None:
    """Plain ``count`` keeps NULLIF; only ``count_distinct`` grows a probe.

    The asymmetry is the decision this issue turns on and it is worth pinning:
    a filtered ``count`` IS the row-presence count, so ``NULLIF(..., 0)`` and the
    CASE gate ask the identical question, and the compact form was kept rather
    than interpolating ``filter_sql`` a second time for no verdict change.
    ClickHouse gates both because its ``-If`` aggregates return 0 for an empty
    group whatever the function.

    Red on revert: the count_distinct half reads ``NULLIF(count(DISTINCT ...), 0)``
    and the gate asserted here is absent on PostgreSQL and BigQuery.
    """
    adapter, sql = _SQL_ENGINES[engine]()
    statement = _flat_multi_aggregate(adapter, sql)

    assert _PLAIN_COUNT[engine] in statement
    assert _DISTINCT_GATE[engine] in statement


def test_the_engines_agree_a_null_measure_over_matching_rows_is_a_zero() -> None:
    """Both directions of the contract: 0 is a value, absent is a gap.

    The executed half runs on the in-memory warehouse, the only adapter that
    answers without a server: two rows that MATCH the filter and carry
    ``button_id IS NULL`` must come back as ``0``, while a filter nothing matches
    must come back as ``None``. That is the answer the three SQL engines have to
    reproduce, and it is what the row-presence probe asserted below buys — the
    SQL half is text, not execution, so the value identity itself is only
    *executed* by tests/conformance/ against live warehouses.

    Red on revert: PostgreSQL and BigQuery lose the probe (they compare the
    distinct count itself to 0), so the second half of this test fails on two
    engines. The in-memory half stays green either way — the oracle was already
    right, which is precisely why it is the reference.
    """
    adapter = _adapter_over(((None, 2),))
    _names, rows = adapter.get_time_bucketed_multi_aggregate(
        "SELECT * FROM events",
        "event_time",
        "1d",
        [
            # ``button_id`` because it is one of the synthetic table's NULLABLE
            # columns, which is what lets "matching rows, measure NULL on every
            # one of them" exist at all.
            AggregateSpec(
                key="d",
                aggregation=MetricAggregation.count_distinct,
                column="button_id",
                filter_sql=_FILTER,
            ),
            AggregateSpec(
                key="nothing_matched",
                aggregation=MetricAggregation.count_distinct,
                column="button_id",
                filter_sql="amount > 100",
            ),
        ],
        _ANCHOR - timedelta(days=5),
        _ANCHOR,
    )
    assert len(rows) == 1, rows
    # Rows matched, every measure NULL: a data point of 0, NOT a gap.
    assert rows[0][1] == 0
    # Nothing matched: a gap, which the caller drops rather than storing as 0.
    assert rows[0][2] is None

    for engine, build in sorted(_SQL_ENGINES.items()):
        sql_adapter, sql = build()
        statement = _flat_multi_aggregate(sql_adapter, sql)
        assert _ROW_PRESENCE_PROBE[engine] in statement, engine


def test_the_conditional_aggregate_rule_is_stated_where_the_contract_lives() -> None:
    """One rule, in the ABC, because four implementations have to agree on it.

    All three SQL adapters kept the reasoning in their own docstrings, and two of
    them stated the same wrong version of it in very nearly the same words — a
    copied justification copies the error with it, which is the argument for one
    statement in the place an adapter author actually reads.

    Red on revert: the section does not exist, and ``AggregateSpec`` says nothing
    about when its cell is NULL.
    """
    contract = inspect.getdoc(BaseAdapter)
    assert contract is not None
    assert "Conditional aggregates (``AggregateSpec.filter_sql``)" in contract
    # The rule, not the symptom: the presence test is a count of ROWS.
    assert "never the aggregate's own value" in contract
    # And the consequence that makes a wrong gate invisible rather than loud.
    assert "skips NULL cells" in contract

    spec_doc = inspect.getdoc(AggregateSpec)
    assert spec_doc is not None
    assert "contract on :class:`BaseAdapter`" in spec_doc


# --------------------------------------------------------------------------- #
# one window scan per field-contract statement, and one place
# that decides whether a contract was violated
# --------------------------------------------------------------------------- #
#
# PostgreSQL and ClickHouse both built their contract query as a UNION ALL of one
# aggregate subquery per expectation. Neither engine shares an identical inline
# subquery between the arms, so E contracts read the window E times, and
# ``catalog_sync`` repeats the whole call once per event-type group: G x E scans
# of the same rows for one collection. BigQuery had already been given the single
# pass (it bills by bytes scanned, so the cost was visible there first); the other
# two now have it too.
#
# Collapsing the arms means the per-expectation thresholds can no longer ride in
# the SQL, so the comparison moved to ``field_contract_verdict`` — which is the
# parity half of this issue, and the reason these tests live here: one rule in
# base.py instead of four spellings of it in four files.


class _ContractCursor:
    def __init__(self, conn: _ContractConn) -> None:
        self._conn = conn

    def __enter__(self) -> _ContractCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self._conn.sql.append(sql)

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._conn.rows


class _ContractConn:
    """Like ``_PGConn`` above, but it can hand a counted row back."""

    def __init__(self) -> None:
        self.sql: list[str] = []
        self.rows: list[tuple[object, ...]] = []

    def cursor(self) -> _ContractCursor:
        return _ContractCursor(self)


class _ContractCHResult:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.result_rows = rows


class _ContractCHClient:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.rows: list[tuple[object, ...]] = []

    def query(self, sql: str, **_kwargs: object) -> _ContractCHResult:
        self.sql.append(sql)
        return _ContractCHResult(self.rows)


class _SampledRows:
    """Stands in for a warehouse that can only be sampled.

    ``BaseAdapter.validate_field_contracts`` — the fallback, and the reference the
    conformance gate compares the SQL engines against — reaches its rows through
    ``get_preview_rows`` and counts them in Python. Nothing else of an adapter is
    involved, so this is the whole of what it needs.
    """

    # The fallback records skipped contracts on the adapter, so
    # the double borrows that bookkeeping from BaseAdapter as well.
    _skipped_field_contracts = None
    _field_contract_is_inert = BaseAdapter._field_contract_is_inert
    _skip_field_contract = BaseAdapter._skip_field_contract

    def __init__(self, column_names: list[str], rows: list[tuple[object, ...]]) -> None:
        self._column_names = column_names
        self._rows = rows

    def get_preview_rows(
        self,
        _base_query: str,
        *,
        limit: int = 50000,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        return self._column_names, self._rows[:limit]


def _pg_contracts() -> tuple[BaseAdapter, _ContractConn]:
    conn = _ContractConn()
    adapter = object.__new__(PostgresAdapter)
    adapter._conn = conn
    adapter._allowed_columns = set(_ALLOWED)
    return adapter, conn


def _gp_contracts() -> tuple[BaseAdapter, _ContractConn]:
    conn = _ContractConn()
    adapter = object.__new__(GreenplumAdapter)
    adapter._conn = conn
    adapter._allowed_columns = set(_ALLOWED)
    return adapter, conn


def _rs_contracts() -> tuple[BaseAdapter, _ContractConn]:
    conn = _ContractConn()
    adapter = object.__new__(RedshiftAdapter)
    adapter._conn = conn
    adapter._allowed_columns = set(_ALLOWED)
    return adapter, conn


def _ch_contracts() -> tuple[BaseAdapter, _ContractCHClient]:
    client = _ContractCHClient()
    adapter = object.__new__(ClickHouseAdapter)
    adapter._client = client
    adapter._allowed_columns = set(_ALLOWED)
    return adapter, client


def _dbx_contracts() -> tuple[BaseAdapter, _DBXConn]:
    conn = _DBXConn()
    return _dbx_adapter(conn), conn


def _sf_contracts() -> tuple[BaseAdapter, _DBXConn]:
    conn = _SFConn()
    return _sf_adapter(conn), conn


# The two engines this issue moved (and Databricks, which was born single-pass and
# judges in Python like them). BigQuery is covered separately below: it was
# already single-pass, and it is the documented exception on where the verdict is
# applied, so lumping it in here would assert the wrong thing about it.
_CONTRACT_ENGINES: dict[str, Callable[[], tuple[BaseAdapter, object]]] = {
    "clickhouse": _ch_contracts,
    "postgres": _pg_contracts,
    "greenplum": _gp_contracts,
    "redshift": _rs_contracts,
    "databricks": _dbx_contracts,
    "snowflake": _sf_contracts,
}

_CONTRACTS = [
    FieldContractExpectation(
        field_name="event_name",
        drift_type="enum_violation",
        threshold=0.0,
        enum_options=("click",),
    ),
    FieldContractExpectation(
        field_name="amount", drift_type="required_null_violation", threshold=0.0
    ),
    FieldContractExpectation(
        field_name="amount",
        drift_type="range_violation",
        threshold=0.25,
        min_value=0.0,
        max_value=50.0,
    ),
]

# One row, three triples, in expectation order: (bad, total, sample) each. This is
# the entire result of the new statement, and the layout both engines decode.
_COUNTED_ROW: tuple[object, ...] = (1, 10, "buy", 2, 11, "<NULL>", 5, 10, "99")

# The lower bound of the scan window as each dialect spells it. Counting it counts
# how many times the statement selects the window.
_CONTRACT_WINDOW = {
    "clickhouse": "`time` >= parseDateTime64BestEffort(",
    "postgres": '"time" >= TIMESTAMPTZ ',
    "greenplum": '"time" >= TIMESTAMPTZ ',
    "redshift": '"time" >= TIMESTAMPTZ ',
    "databricks": "`time` >= TIMESTAMP '",
    "snowflake": '"time" >= TO_TIMESTAMP_NTZ(',
}

# What those counts mean, judged once. The third contract is the interesting one:
# 5/10 is strictly above its 0.25 threshold, so it fires.
_EXPECTED_VIOLATIONS = [
    FieldContractViolation(
        field_name="event_name",
        drift_type="enum_violation",
        bad_count=1,
        total_count=10,
        bad_rate=0.1,
        threshold=0.0,
        sample_value="buy",
    ),
    FieldContractViolation(
        field_name="amount",
        drift_type="required_null_violation",
        bad_count=2,
        total_count=11,
        bad_rate=2 / 11,
        threshold=0.0,
        sample_value="<NULL>",
    ),
    FieldContractViolation(
        field_name="amount",
        drift_type="range_violation",
        bad_count=5,
        total_count=10,
        bad_rate=0.5,
        threshold=0.25,
        sample_value="99",
    ),
]


def _run_contracts(
    engine: str, rows: list[tuple[object, ...]], expectations: list[FieldContractExpectation]
) -> tuple[list[FieldContractViolation], list[str]]:
    adapter, handle = _CONTRACT_ENGINES[engine]()
    handle.rows = rows  # type: ignore[attr-defined]
    violations = adapter.validate_field_contracts(
        _BASE, expectations, time_column="time", time_from=_FROM, time_to=_TO
    )
    return violations, handle.sql  # type: ignore[attr-defined]


@pytest.mark.parametrize("engine", sorted(_CONTRACT_ENGINES))
def test_every_field_contract_rides_one_scan_of_the_window(engine: str) -> None:
    """Three contracts, one statement, one ``FROM (base_query)`` inside it.

    This is a structural assertion about cost rather than a text assertion about a
    fragment: what it counts is how many times the window appears, which is what
    the warehouse will read.

    Red on revert: each expectation gets its own embedded
    ``FROM (base_query) AS _src`` inside its own UNION ALL arm, so the base query
    appears three times and ``UNION ALL`` twice. The warehouse returns nothing
    here on purpose, so what fails on a revert is that count and not the decode —
    the decode has its own test below.
    """
    _violations, statements = _run_contracts(engine, [], _CONTRACTS)

    assert len(statements) == 1, statements
    sql = statements[0]
    assert sql.count(f"FROM ({_BASE}) AS _src") == 1, sql
    assert "UNION ALL" not in sql
    # ...and all three contracts really are in that one pass, positionally.
    for index in range(len(_CONTRACTS)):
        assert f"AS _bad_{index}" in sql
        assert f"AS _total_{index}" in sql
        assert f"AS _sample_{index}" in sql


@pytest.mark.parametrize("engine", sorted(_CONTRACT_ENGINES))
def test_nothing_but_counts_crosses_the_wire(engine: str) -> None:
    """The threshold is not in the statement, so the engine cannot apply it.

    A contract's threshold used to be interpolated twice per expectation — once as
    a returned column and once inside the arm's ``WHERE`` — which is what made the
    comparison an engine's opinion rather than a shared rule.

    Red on revert: the statement carries ``> 0.25`` and the ``bad_count`` /
    ``total_count`` aliases the outer wrapper selected through.
    """
    _violations, statements = _run_contracts(engine, [], _CONTRACTS)
    sql = statements[0]

    assert "0.25" not in sql, "the threshold belongs to field_contract_verdict"
    assert "AS bad_count" not in sql
    assert "AS total_count" not in sql
    assert "AS bad_rate" not in sql
    # Selecting the window is still the engine's job — and it states it once, not
    # once per expectation the way an arm per contract had to.
    assert sql.count(_CONTRACT_WINDOW[engine]) == 1


def test_the_two_engines_read_one_row_of_counts_into_the_same_verdicts() -> None:
    """Same counts in, same violations out, in the caller's order.

    The row layout is the parity surface now: ``(bad, total, sample)`` per
    expectation, positionally, on both engines. If one of them numbered its
    aliases differently or decoded a different stride, this is where the two
    engines stop agreeing — and every value below (the rate, the clamped
    threshold, the sample) comes from the one shared judge.

    Red on revert: both adapters expect one ROW per violation, in the old
    seven-column ``(field, drift, bad, total, threshold, rate, sample)`` shape, so
    a row of counts decodes into nonsense or raises.
    """
    pg_violations, _pg_sql = _run_contracts("postgres", [_COUNTED_ROW], _CONTRACTS)
    ch_violations, _ch_sql = _run_contracts("clickhouse", [_COUNTED_ROW], _CONTRACTS)

    assert pg_violations == _EXPECTED_VIOLATIONS
    assert ch_violations == _EXPECTED_VIOLATIONS


@pytest.mark.parametrize("engine", sorted(_CONTRACT_ENGINES))
def test_an_empty_window_is_no_violation_and_no_division(engine: str) -> None:
    """A quiet event type counts zero of everything and reports nothing.

    ``total_count`` of 0 is an ordinary scan, not an error, and it used to be one
    ``AND`` away from a division by zero on the engine that spelled the rate as
    plain ``/``. The guard is now in front of the division, in Python, where it
    does not depend on how a planner orders the conjuncts.

    Red on revert: a row of zeros is decoded as a violation row instead of as
    counts, so the call returns a bogus violation rather than nothing.
    """
    empty = [(0, 0, None, 0, 0, None, 0, 0, None)]
    violations, statements = _run_contracts(engine, empty, _CONTRACTS)

    assert violations == []
    assert len(statements) == 1, "the statement still ran; it just found nothing"


def test_the_fallback_and_the_sql_engines_reach_the_same_verdict() -> None:
    """Counting in Python and counting in SQL end at one function.

    The conformance gate's headline assertion is native == fallback against a real
    server. It can only hold if the two implementations share the *rule* as well as
    the arithmetic, so here the fallback counts ten sampled rows while the SQL
    engines are handed the counts those rows produce, and all three are compared.

    Red on revert: ``field_contract_verdict`` does not exist, and the three
    implementations each carry their own copy of the comparison.
    """
    enum_contract = [_CONTRACTS[0]]
    # Ten rows, one of them off the enum: bad 1, total 10, rate 0.1.
    sampled = _SampledRows(["event_name"], [("buy",)] + [("click",)] * 9)
    fallback = BaseAdapter.validate_field_contracts(sampled, _BASE, enum_contract)  # type: ignore[arg-type]

    assert fallback == [_EXPECTED_VIOLATIONS[0]]
    for engine in sorted(_CONTRACT_ENGINES):
        violations, _sql = _run_contracts(engine, [(1, 10, "buy")], enum_contract)
        assert violations == fallback, engine


@pytest.mark.parametrize("engine", sorted(_CONTRACT_ENGINES))
def test_a_contract_set_too_wide_for_one_target_list_is_chunked_not_dropped(engine: str) -> None:
    """Past the cap, another statement — still one window scan each.

    Three columns per expectation means a very wide event type could otherwise
    build a target list PostgreSQL refuses outright, failing the whole collection
    rather than one contract. Chunking keeps every realistic configuration at a
    single scan and makes the pathological one cost a few, not hundreds.

    Red on revert: all of them go into one statement as UNION ALL arms, so there
    is exactly one statement and it scans the window 300 times.
    """
    wide = [
        FieldContractExpectation(
            field_name="amount", drift_type="required_null_violation", threshold=0.0
        )
    ] * 300
    _none, statements = _run_contracts(engine, [], wide)

    assert FIELD_CONTRACT_EXPECTATIONS_PER_QUERY == 256
    assert len(statements) == 2
    assert [sql.count(f"FROM ({_BASE}) AS _src") for sql in statements] == [1, 1]
    assert [sql.count("AS _bad_") for sql in statements] == [256, 44]

    # ...and the second statement's counts are judged like the first's: every
    # expectation comes back, in order, rather than the tail being dropped.
    violations, _statements = _run_contracts(engine, [tuple([1, 10, "x"] * 300)], wide)
    assert len(violations) == 300, "chunking must not lose an expectation"


def test_bigquery_ships_the_threshold_exactly_because_it_still_judges_in_sql() -> None:
    """The documented exception, and the condition that makes it safe.

    BigQuery keeps applying the rule warehouse-side — its STRUCT array already
    hands it one row per expectation to filter, so filtering costs it nothing and
    keeps a passing contract off the wire. That is only the SAME rule while the
    threshold in the SQL is the same double ``field_contract_verdict`` would have
    compared against, so the literal has to round-trip.

    Red on revert: the literal is ``%.12g`` of the threshold, which renders 1/3 as
    ``0.333333333333`` — a bad rate of exactly 1/3 then clears the threshold on
    BigQuery and not on the two engines that compare in Python.
    """
    adapter, statements = _bq()
    adapter.validate_field_contracts(
        _BASE,
        [
            FieldContractExpectation(
                field_name="event_name", drift_type="required_null_violation", threshold=1 / 3
            )
        ],
        time_column="time",
        time_from=_FROM,
        time_to=_TO,
    )

    sql = statements[0]
    assert f"CAST({1 / 3!r} AS FLOAT64) AS threshold" in sql
    assert "CAST(0.333333333333 AS FLOAT64)" not in sql
    # The exception itself: the comparison is still in the statement, and it is
    # still reached in a single pass over the window.
    assert "SAFE_DIVIDE(_c.bad_count, _c.total_count) > _c.threshold" in sql
    assert sql.count(f"FROM ({_BASE}) AS _src") == 1

    # And the two halves agree on the verdict at the boundary the literal decides:
    # a rate of exactly 1/3 does not clear a threshold of 1/3.
    at_the_boundary = field_contract_verdict(
        FieldContractExpectation(
            field_name="event_name", drift_type="required_null_violation", threshold=1 / 3
        ),
        bad_count=1,
        total_count=3,
        sample_value="<NULL>",
    )
    assert at_the_boundary is None


def test_the_field_contract_rule_is_stated_where_the_contract_lives() -> None:
    """One scan and one judge, written down in the ABC rather than per adapter.

    The UNION ALL shape was not a bug in one adapter: two engines grew it
    independently, and the docstring above it read "one query for all
    expectations", which is true and is exactly what stops a reader asking how
    many times that one query reads the table.

    Red on revert: the section does not exist, and neither does the function it
    names.
    """
    contract = inspect.getdoc(BaseAdapter)
    assert contract is not None
    assert "Field contracts (``validate_field_contracts``)" in contract
    # The cost rule...
    assert "ONE scan of the window per statement" in contract
    # ...and the verdict rule, including the one engine that meets it differently.
    assert "The warehouse COUNTS; the verdict is decided in Python" in contract
    assert "BigQuery is the documented exception" in contract

    verdict_doc = inspect.getdoc(field_contract_verdict)
    assert verdict_doc is not None
    assert "single definition of what a field-contract violation IS" in verdict_doc


# --------------------------------------------------------------------------- #
# a contract an engine cannot compile: inert everywhere, fatal
# nowhere
# --------------------------------------------------------------------------- #
#
# Same expectation, four answers. A range contract whose bound is not a finite
# number RAISED on PostgreSQL out of a private ``_float_literal`` (ending the
# whole collection, since neither ``schema_drift`` nor ``catalog_sync`` catches
# anything), compiled into ``< -inf`` on BigQuery — a literal GoogleSQL has no
# spelling for, so the statement only fails against the real service — and
# compiled quietly on ClickHouse, which does have the literal and therefore
# computed a comparison no row can be on the wrong side of. The fallback, being
# Python, agreed with ClickHouse by accident.
#
# An enum/regex/range contract on a REPEATED (ARRAY) column was the same shape
# from the other end: BigQuery raised before any SQL existed, while the other
# two rendered the array to text and checked it.
#
# The shared answer is now stated in ``BaseAdapter``: an expectation that cannot
# be compiled is INERT — no columns, no violation, and it never takes the
# expectations beside it or the collection around them down with it. The list of
# universally-inert cases lives in ``field_contract_is_inert``; the one
# engine-specific case (BigQuery's REPEATED columns) is documented there as a
# declared divergence rather than an accidental one.


def _bq_repeated() -> tuple[BaseAdapter, list[str]]:
    """A BigQuery adapter whose ``labels`` column is an ``ARRAY<STRING>``.

    ``labels`` is deliberately absent from ``_BASE``'s text, so an assertion that
    the column is missing from the statement cannot be satisfied by the base
    query quoting it back.
    """
    adapter, sql = _bq()
    adapter._allowed_columns = {*_ALLOWED, "labels"}
    adapter._column_types = {**adapter._column_types, "labels": "STRING"}
    adapter._repeated_columns = {"labels"}
    return adapter, sql


def _contracts_on(engine: str, expectations: list[FieldContractExpectation]) -> list[str]:
    """Run ``validate_field_contracts`` on one engine, return the statements."""
    adapter, sql = _SQL_ENGINES[engine]()
    adapter.validate_field_contracts(
        _BASE, expectations, time_column="time", time_from=_FROM, time_to=_TO
    )
    return sql


# The sibling contract that must survive whatever happens to the one beside it.
# It needs no rendering of any value, so no engine can decline it.
_SURVIVOR = FieldContractExpectation(
    field_name="event_name", drift_type="required_null_violation", threshold=0.0
)

# What "this engine compiled a range comparison" looks like in each dialect.
_RANGE_COMPILED = {
    "clickhouse": "toFloat64OrNull",
    "postgres": "::numeric",
    "greenplum": "::numeric",
    "redshift": "::double precision",
    "bigquery": "SAFE_CAST",
    "databricks": "try_cast",
    "snowflake": "TRY_TO_DOUBLE",
}

# A numeric literal for infinity or NaN, however spelled. ``\b`` keeps it off
# ``IFNULL`` and ``isNull``, which are ordinary parts of these statements.
_NON_FINITE_LITERAL = re.compile(r"[-+]?\b(inf(inity)?|nan)\b", re.IGNORECASE)


@pytest.mark.parametrize("engine", sorted(_SQL_ENGINES))
@pytest.mark.parametrize("bound", ["-inf", "inf", "nan"])
def test_a_non_finite_range_bound_is_inert_on_every_engine(engine: str, bound: str) -> None:
    """One statement, no literal for it, and the contract beside it still runs.

    Red on revert, differently on each of the three: PostgreSQL raises
    ``ValueError`` out of ``validate_field_contracts`` (``_float_literal``) and
    never reaches an assertion; ClickHouse and BigQuery both interpolate the
    float, so ``< -inf`` lands in the statement and the literal assertion fails.
    """
    bad_bound = FieldContractExpectation(
        field_name="amount",
        drift_type="range_violation",
        threshold=0.0,
        min_value=float(bound),
    )

    statements = _contracts_on(engine, [bad_bound, _SURVIVOR])

    assert len(statements) == 1
    sql = statements[0]
    assert _NON_FINITE_LITERAL.search(sql) is None, sql
    assert _RANGE_COMPILED[engine] not in sql
    # Exactly one expectation compiled, and it is the survivor.
    assert sql.count("AS _bad_") == 1
    assert sql.count("AS _total_") == 1


@pytest.mark.parametrize("engine", sorted(_SQL_ENGINES))
def test_one_non_finite_bound_takes_the_whole_range_contract_with_it(engine: str) -> None:
    """``min=0.0, max=+inf`` compiles neither bound, on all three engines.

    Rendering the finite bound and dropping the other was the rejected
    alternative, and this is the case that rejects it: it reads correctly for a
    ``-inf`` minimum and inverts the contract for a ``+inf`` one, where every row
    is below the bound and the honest reading is "every row is bad".

    Red on revert: PostgreSQL raises; the other two emit ``> inf`` next to a
    perfectly ordinary ``>= 0.0``, so the range comparison is compiled and the
    marker assertion fails.
    """
    half_finite = FieldContractExpectation(
        field_name="amount",
        drift_type="range_violation",
        threshold=0.0,
        min_value=0.0,
        max_value=float("inf"),
    )

    sql = _contracts_on(engine, [half_finite, _SURVIVOR])[0]

    assert _RANGE_COMPILED[engine] not in sql
    assert _NON_FINITE_LITERAL.search(sql) is None, sql


def test_the_bound_helper_refuses_to_render_what_no_dialect_can_parse() -> None:
    """The last line under the decision: a bound is rendered or it raises.

    The skip above is the guard; this is what makes "never emit ``< -inf``" true
    of the rendering layer itself, so a future caller that forgets to ask
    ``field_contract_is_inert`` fails loudly instead of shipping SQL BigQuery
    cannot parse.

    Red on revert: ``contract_bound_literal`` does not exist — the helper was
    PostgreSQL's private ``_float_literal`` and the other two engines had none —
    so this module fails at import.
    """
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValueError, match="finite"):
            contract_bound_literal(bad)

    # repr, not %g: the bound has to reach the warehouse as the double it is, for
    # the same reason BigQuery's threshold literal does.
    assert contract_bound_literal(1 / 3) == repr(1 / 3)
    assert float(contract_bound_literal(1 / 3)) == 1 / 3
    # And it always carries a '.' or an 'e', which is what keeps PostgreSQL from
    # resolving the comparison as `numeric >= int8`.
    assert contract_bound_literal(7) == "7.0"


@pytest.mark.parametrize(
    "expectation",
    [
        FieldContractExpectation(
            field_name="labels", drift_type="enum_violation", threshold=0.0, enum_options=("a",)
        ),
        FieldContractExpectation(
            field_name="labels", drift_type="regex_violation", threshold=0.0, regex="^a$"
        ),
        FieldContractExpectation(
            field_name="labels", drift_type="range_violation", threshold=0.0, min_value=0.0
        ),
    ],
    ids=["enum", "regex", "range"],
)
def test_a_contract_on_a_repeated_column_is_skipped_not_fatal(
    expectation: FieldContractExpectation,
) -> None:
    """BigQuery declines the one it cannot render and runs the rest.

    This is a deliberate behaviour change: the raise it replaces was correct
    about the SQL (there is no ``CAST(<array> AS STRING)``) and wrong about the
    blast radius. ``validate_field_contracts`` is called by a worker replaying
    contracts a user declared long ago, bare in ``schema_drift`` and once per
    event-type group in ``catalog_sync``, so one stale contract on a column that
    has since become an ARRAY ended the whole collection.

    Red on revert: ``_string_value_expression`` raises ``ValueError`` and no
    statement is generated at all, for all three parametrized cases.
    """
    adapter, statements = _bq_repeated()

    adapter.validate_field_contracts(
        _BASE, [expectation, _SURVIVOR], time_column="time", time_from=_FROM, time_to=_TO
    )

    assert len(statements) == 1
    sql = statements[0]
    assert "labels" not in sql
    assert sql.count("AS _bad_") == 1
    assert "COUNTIF(`event_name` IS NULL)" in sql

    # The scope of the skip: required-ness is pure NULL logic, needs no STRING
    # rendering, and stays legal on the very same column. A skip that swallowed
    # this one would silently stop checking whether an ARRAY column is populated.
    required, required_sql = _bq_repeated()
    required.validate_field_contracts(
        _BASE,
        [
            FieldContractExpectation(
                field_name="labels", drift_type="required_null_violation", threshold=0.0
            )
        ],
    )
    assert "COUNTIF(`labels` IS NULL) AS _bad_0" in required_sql[0]


def test_a_repeated_column_contract_is_not_silently_dropped_from_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The skip is loud enough to diagnose, because nothing else reports it.

    A contract that stops being checked and says nothing is the risk this fix
    takes on, so the warning has to name the column and the drift type — those
    two are what identify the FieldDefinition an operator must fix.

    Red on revert: the call raises before any log record is emitted.
    """
    adapter, _statements = _bq_repeated()

    with caplog.at_level(logging.WARNING, logger=BigQueryAdapter.__module__):
        adapter.validate_field_contracts(
            _BASE,
            [
                FieldContractExpectation(
                    field_name="labels",
                    drift_type="enum_violation",
                    threshold=0.0,
                    enum_options=("a",),
                ),
                _SURVIVOR,
            ],
        )

    assert "labels" in caplog.text
    assert "enum_violation" in caplog.text
    assert "REPEATED" in caplog.text


def test_a_contract_that_constrains_nothing_is_inert_on_all_four_engines() -> None:
    """An enum with no options: no statement, no violation, no invented drift.

    The three SQL adapters each declined this locally and the fallback did not
    decline it at all — it asked ``text not in ()``, true of every row, and
    reported a contract that constrains nothing as total drift. That is the
    conformance gate's headline assertion (native == fallback) failing on an
    input none of the four had agreed about.

    Red on revert: the fallback returns one violation with bad_count == 2 while
    the three SQL engines return none.
    """
    empty_enum = FieldContractExpectation(
        field_name="event_name", drift_type="enum_violation", threshold=0.0
    )

    sampled = _SampledRows(["event_name"], [("click",), ("buy",)])
    fallback = BaseAdapter.validate_field_contracts(  # type: ignore[arg-type]
        sampled, _BASE, [empty_enum]
    )
    assert fallback == []

    for engine in sorted(_SQL_ENGINES):
        statements = _contracts_on(engine, [empty_enum])
        # Not merely "no violation": an inert set must leave the warehouse alone.
        assert [sql for sql in statements if not sql.endswith("LIMIT 0")] == [], engine


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (FieldDefinitionCreate, "contract_min_value"),
        (FieldDefinitionCreate, "contract_max_value"),
        (FieldDefinitionUpdate, "contract_min_value"),
        (FieldDefinitionUpdate, "contract_max_value"),
    ],
)
@pytest.mark.parametrize("payload", ["-Infinity", "1e400", "NaN"])
def test_a_non_finite_bound_is_a_422_at_the_save_boundary(
    model: type[BaseModel], field: str, payload: str
) -> None:
    """The belt: a bound the adapters would decline never gets stored.

    It does not replace the adapter-side skip — branch copy, branch revert and
    branch merge all write these columns straight onto the ORM model, and rows
    predating this rule exist — but it turns a contract that silently checks
    nothing into an error the operator sees while typing it.

    Red on revert: ``allow_inf_nan`` defaults to True, pydantic accepts all three
    spellings (``1e400`` overflows to ``inf`` on the way in), and nothing raises.

    Both classes are parametrized because they are deliberate near-duplicates
    with no shared base: a rule added to Create alone makes PATCH the way around
    it.
    """
    rest = (
        '"name": "amount", "display_name": "Amount", "field_type": "number", '
        if model is FieldDefinitionCreate
        else ""
    )

    # The control first: the identical payload with a finite bound is accepted,
    # so what the raise below reports is the bound and not a missing field.
    assert model.model_validate_json(f'{{{rest}"{field}": 1.5}}') is not None

    with pytest.raises(ValidationError, match="finite"):
        model.model_validate_json(f'{{{rest}"{field}": {payload}}}')


def test_the_inert_rule_is_stated_where_the_contract_lives() -> None:
    """Three engines, one sentence, and the divergence declared rather than left.

    The engines had drifted apart on this precisely because each decided locally
    what it could not compile. The universally-inert list is now one function and
    the one real dialect limitation is named in the ABC — including why forcing
    the other two engines to match it would mean deleting a working check rather
    than adding one.

    Red on revert: neither the paragraphs nor the function exists.
    """
    contract = inspect.getdoc(BaseAdapter)
    assert contract is not None
    assert "An expectation that cannot be compiled is INERT, never fatal" in contract
    # The divergence, declared: which engine, what it cannot do, why the other
    # two are not made to match.
    assert "What an engine can compile is NOT itself shared" in contract
    assert "REPEATED" in contract

    inert_doc = inspect.getdoc(field_contract_is_inert)
    assert inert_doc is not None
    assert "cannot be evaluated by ANY engine" in inert_doc


def test_required_null_is_never_inert_because_it_renders_nothing() -> None:
    """The one drift type no engine can decline, stated as a property.

    It is the reason the REPEATED skip is scoped to the branches that need a
    STRING rendering, and the reason an "inert" answer can never mean "this
    column cannot be checked at all".

    Red on revert: ``field_contract_is_inert`` does not exist and this module
    fails at import.
    """
    bare = FieldContractExpectation(
        field_name="labels", drift_type="required_null_violation", threshold=0.0
    )
    assert field_contract_is_inert(bare) is False

    # ...while every other drift type with nothing to check is inert, and so is a
    # drift type no adapter implements.
    for inert in (
        FieldContractExpectation(field_name="a", drift_type="enum_violation", threshold=0.0),
        FieldContractExpectation(field_name="a", drift_type="regex_violation", threshold=0.0),
        FieldContractExpectation(field_name="a", drift_type="range_violation", threshold=0.0),
        FieldContractExpectation(field_name="a", drift_type="not_a_drift_type", threshold=0.0),
    ):
        assert field_contract_is_inert(inert) is True, inert.drift_type


# --------------------------------------------------------------------------- #
# a contract regex the warehouse will not compile
# --------------------------------------------------------------------------- #
#
# The save gate is Python's ``re`` and the evaluators are not: RE2 on ClickHouse
# and BigQuery, POSIX ARE on PostgreSQL, ``re`` only in the in-memory fallback.
# A pattern that saved cleanly could therefore be one the engine refuses, and it
# refused it from inside the contract statement — the statement every OTHER
# expectation rides in — so one pattern took down the whole event type's contract
# check. Nothing between there and the task caught it either, so the scan failed,
# the config stayed due, and it failed again on every retry with the generic
# "Scan failed due to an internal error."
#
# Two halves. The engine is asked about the pattern before it reaches a
# statement, so a refusal is inert here and never fatal (the third fixed rule in
# ``BaseAdapter``'s field contract section, which this issue extends from "cannot
# be compiled" to "this engine's regex library will not take it"); and the
# caller contains a contract failure of ANY kind, counts it, and reports the
# count, so no contract can wedge a config again.
#
# These are fake-client tests: the refusals below are the fakes', standing in for
# each engine's regex library. That a live ClickHouse rejects ``(?!`` and a live
# PostgreSQL rejects ``(?P<`` is what the conformance package is for; what these
# prove is the part a live one-engine run cannot show — that all four engines
# route a refusal to the same place, and that the statement beside it survives.


# The pattern each engine's own library refuses, and which the save gate cannot
# screen: Python `re` compiles BOTH of these. RE2 has no lookaround at all
# (ClickHouse `match()`, BigQuery `REGEXP_CONTAINS`); POSIX ARE has lookaround
# but no Python-style named group (PostgreSQL `~`). The divergences do not point
# the same way, which is exactly why no single "portable subset" screen at the
# save boundary could have replaced this.
_REFUSED_BY = {
    "clickhouse": "^(?!test_)",
    "bigquery": "^(?!test_)",
    "postgres": "(?P<sku>x)",
    # Java's java.util.regex (RLIKE) spells a named group (?<name>), not (?P<name>).
    "databricks": "(?P<sku>x)",
    # Snowflake's POSIX ERE has no lookaround.
    "snowflake": "^(?!test_)",
}

# Portable in all three dialects and in Python: literals, a character class, an
# anchor, a quantifier. This one must survive everything that happens to the
# pattern beside it.
_PORTABLE_PATTERN = "^u[0-9]+$"

# The probe each dialect issues, spelled exactly. It reads no table, which is the
# property that makes a round trip affordable per pattern — on BigQuery it is
# also what makes it free, since a statement with no FROM processes no bytes.
_PROBE_SQL = {
    "clickhouse": "SELECT match('', '{pattern}')",
    "postgres": "SELECT '' ~ '{pattern}'",
    "bigquery": "SELECT REGEXP_CONTAINS('', '{pattern}')",
    # Bound as :p0 by the adapter; the fake writes the value back in.
    "databricks": "SELECT '' RLIKE '{pattern}'",
    # Bound as :1; the fake writes the value back in.
    "snowflake": "SELECT REGEXP_INSTR('', '{pattern}')",
}


def _refuse_like_a_regex_library(sql: str, refused: str | None, offline: bool) -> None:
    """Stand in for the engine: refuse one pattern, or refuse to answer at all.

    ``offline`` is the second failure mode and the one the fix must NOT treat as
    a refusal — a connection that dropped says nothing about the pattern.
    """
    if offline:
        raise RuntimeError("connection reset by peer")
    if refused is not None and refused in sql:
        raise RuntimeError(f"cannot compile regular expression: {refused}")


class _RefusingCHClient:
    def __init__(self, refused: str | None, offline: bool) -> None:
        self.sql: list[str] = []
        self._refused = refused
        self._offline = offline

    def query(self, sql: str, **_kwargs: object) -> _ContractCHResult:
        self.sql.append(sql)
        _refuse_like_a_regex_library(sql, self._refused, self._offline)
        return _ContractCHResult([])


class _RefusingPGCursor:
    def __init__(self, conn: _RefusingPGConn) -> None:
        self._conn = conn

    def __enter__(self) -> _RefusingPGCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self._conn.sql.append(sql)
        _refuse_like_a_regex_library(sql, self._conn.refused, self._conn.offline)

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


class _RefusingPGConn:
    def __init__(self, refused: str | None, offline: bool) -> None:
        self.sql: list[str] = []
        self.refused = refused
        self.offline = offline

    def cursor(self) -> _RefusingPGCursor:
        return _RefusingPGCursor(self)


class _RefusingBQClient:
    def __init__(self, refused: str | None, offline: bool) -> None:
        self.sql: list[str] = []
        self._refused = refused
        self._offline = offline

    def query(self, sql: str) -> _BQJob:
        self.sql.append(sql)
        # BigQuery rejects an unparseable regex when the JOB is created, which is
        # this call and not `result()`.
        _refuse_like_a_regex_library(sql, self._refused, self._offline)
        return _BQJob()


def _ch_refusing(
    refused: str | None = None, *, offline: bool = False
) -> tuple[BaseAdapter, list[str]]:
    client = _RefusingCHClient(refused, offline)
    adapter = object.__new__(ClickHouseAdapter)
    adapter._client = client
    adapter._allowed_columns = set(_ALLOWED)
    adapter._json_path_discovery = "dynamic"
    return adapter, client.sql


def _pg_refusing(
    refused: str | None = None, *, offline: bool = False
) -> tuple[BaseAdapter, list[str]]:
    conn = _RefusingPGConn(refused, offline)
    adapter = object.__new__(PostgresAdapter)
    adapter._conn = conn
    adapter._allowed_columns = set(_ALLOWED)
    return adapter, conn.sql


def _bq_refusing(
    refused: str | None = None, *, offline: bool = False
) -> tuple[BaseAdapter, list[str]]:
    client = _RefusingBQClient(refused, offline)
    adapter = object.__new__(BigQueryAdapter)
    adapter._client = client
    adapter._project = "tripl-test"
    adapter._dataset = "wh"
    adapter._allowed_columns = set(_ALLOWED)
    # Seeded like ``_bq()``: no schema probe, so the regex probe is sql[0].
    adapter._column_types = {"time": "TIMESTAMP", "event_name": "STRING", "amount": "FLOAT64"}
    adapter._struct_paths = {}
    adapter._repeated_columns = set()
    adapter._timeout_seconds = None
    adapter._maximum_bytes_billed = None
    return adapter, client.sql


def _dbx_refusing(
    refused: str | None = None, *, offline: bool = False
) -> tuple[BaseAdapter, list[str]]:
    conn = _DBXConn(refusing=True, refused=refused, offline=offline)
    return _dbx_adapter(conn), conn.sql


def _sf_refusing(
    refused: str | None = None, *, offline: bool = False
) -> tuple[BaseAdapter, list[str]]:
    conn = _SFConn(refusing=True, refused=refused, offline=offline)
    return _sf_adapter(conn), conn.sql


_REFUSING_ENGINES: dict[str, Callable[..., tuple[BaseAdapter, list[str]]]] = {
    "clickhouse": _ch_refusing,
    "postgres": _pg_refusing,
    "bigquery": _bq_refusing,
    "databricks": _dbx_refusing,
    "snowflake": _sf_refusing,
}


def _regex_contract(pattern: str) -> FieldContractExpectation:
    return FieldContractExpectation(
        field_name="event_name", drift_type="regex_violation", threshold=0.0, regex=pattern
    )


@pytest.mark.parametrize("engine", sorted(_REFUSING_ENGINES))
def test_a_pattern_this_engine_refuses_drops_only_its_own_expectation(engine: str) -> None:
    """One refused pattern, and the two contracts beside it still get answered.

    The blast radius is the entire point: the refused pattern used to be
    compiled into the same statement as every other expectation, so the engine's
    "cannot compile" ended the contract check for the whole event type — and,
    because nothing between here and the task caught it, the collection too.

    Red on revert: the pattern is interpolated into the contract statement, the
    fake engine raises on it exactly as a real one would, and the call never
    returns.
    """
    refused = _REFUSED_BY[engine]
    adapter, statements = _REFUSING_ENGINES[engine](refused)

    adapter.validate_field_contracts(
        _BASE,
        [_regex_contract(refused), _SURVIVOR, _regex_contract(_PORTABLE_PATTERN)],
        time_column="time",
        time_from=_FROM,
        time_to=_TO,
    )

    contract_statements = [sql for sql in statements if f"FROM ({_BASE})" in sql]
    assert len(contract_statements) == 1, statements
    sql = contract_statements[0]
    assert refused not in sql
    # Not merely "it did not crash": the other two expectations were evaluated,
    # and the portable pattern is still being checked.
    assert sql.count("AS _bad_") == 2
    assert _PORTABLE_PATTERN in sql


@pytest.mark.parametrize("engine", sorted(_REFUSING_ENGINES))
def test_the_engine_is_asked_about_a_pattern_before_it_reaches_a_statement(engine: str) -> None:
    """The probe: this dialect's own regex function, over no rows at all.

    Asking the engine is the design. A static screen for a "portable subset"
    would have to reject the lookahead PostgreSQL and Python both accept, and
    would still be guessing at the grammar of a library that can be asked — see
    ``BaseAdapter.contract_regex_is_compilable``.

    Red on revert: no probe statement is issued at all, so the first statement is
    the contract query and the equality fails.
    """
    adapter, statements = _REFUSING_ENGINES[engine]()

    adapter.validate_field_contracts(
        _BASE,
        [_regex_contract(_PORTABLE_PATTERN)],
        time_column="time",
        time_from=_FROM,
        time_to=_TO,
    )

    assert statements[0] == _PROBE_SQL[engine].format(pattern=_PORTABLE_PATTERN)
    # It reads nothing: a probe that scanned the window would cost more than the
    # failure it prevents, and on BigQuery it would be billed for the privilege.
    assert _BASE not in statements[0]
    # ...and the pattern it authorized is the one that got compiled.
    assert _PORTABLE_PATTERN in statements[1]


@pytest.mark.parametrize("engine", sorted(_REFUSING_ENGINES))
def test_a_pattern_is_probed_once_per_adapter_however_often_it_is_used(engine: str) -> None:
    """``catalog_sync`` calls this once per event-type group; the probe does not.

    A round trip per contract per group would put the cost of the guard on the
    same multiplier the single-scan rule above exists to remove. The answer is
    memoized on the adapter, which lives exactly one task.

    Red on revert: there is no probe to count, so the count is 0 and not 1.
    """
    adapter, statements = _REFUSING_ENGINES[engine]()
    contracts = [_regex_contract(_PORTABLE_PATTERN)]

    for _group in range(3):
        adapter.validate_field_contracts(
            _BASE, contracts, time_column="time", time_from=_FROM, time_to=_TO
        )

    probe = _PROBE_SQL[engine].format(pattern=_PORTABLE_PATTERN)
    assert statements.count(probe) == 1, statements
    # Three groups, three contract statements: only the probe is memoized.
    assert len([sql for sql in statements if f"FROM ({_BASE})" in sql]) == 3


@pytest.mark.parametrize("engine", sorted(_REFUSING_ENGINES))
def test_a_probe_that_cannot_run_is_not_read_as_a_refusal(engine: str) -> None:
    """A dropped connection must not retire a working contract.

    The two failure modes are told apart by asking the same question about a
    pattern that cannot be the problem, rather than by reading driver exception
    types — three libraries, three taxonomies, none of them checkable without a
    live warehouse. Here nothing answers, so the honest verdict is "I do not
    know", the contract stays in the scan, and the statement that follows fails
    into the caller's containment.

    Red on revert: ``contract_regex_is_compilable`` does not exist and this
    raises ``AttributeError``.
    """
    adapter, statements = _REFUSING_ENGINES[engine](offline=True)

    assert adapter.contract_regex_is_compilable(_REFUSED_BY[engine]) is True
    # Both asked: the pattern, then the control that says whether the engine is
    # answering questions at all.
    assert len(statements) == 2
    # And nothing is remembered — the next call may be past whatever was wrong.
    assert adapter._contract_regex_support == {}


def test_a_refusal_is_logged_with_the_engine_and_the_pattern(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The only signal a user's contract stopped being enforced.

    A skipped expectation produces no drift row, and a contract that is not
    evaluated looks exactly like one that is being met, so the warning carries
    the two things that identify what to change — which engine refused, and which
    pattern — plus the engine's own message, which names the construct.

    Red on revert: the pattern goes into the statement instead and the call
    raises before any record is emitted.
    """
    adapter, _statements = _ch_refusing(_REFUSED_BY["clickhouse"])

    with caplog.at_level(logging.WARNING, logger=BaseAdapter.__module__):
        adapter.validate_field_contracts(_BASE, [_regex_contract(_REFUSED_BY["clickhouse"])])

    assert "ClickHouseAdapter" in caplog.text
    assert _REFUSED_BY["clickhouse"] in caplog.text
    # The engine's own text, carried through: exc_info, not a bare message.
    assert "cannot compile regular expression" in caplog.text


def test_the_in_memory_engine_declines_a_pattern_python_itself_refuses() -> None:
    """The fourth engine, whose regex library is ``re`` — and it had the bug too.

    ``re.compile`` of the stored pattern used to sit unguarded in the fallback's
    row loop. The save gate makes that hard to reach but not unreachable: branch
    copy (``plan_branch_service``), branch revert
    (``plan_branch_revert_service``) and branch merge
    (``plan_branch_merge_service``) all write ``contract_regex`` straight onto
    the ORM model without passing through the schema, and rows predating the
    gate exist.

    Red on revert: ``re.PatternError`` propagates out of
    ``validate_field_contracts`` instead of the expectation being skipped.
    """
    adapter = SyntheticAdapter(seed=_SEED, anchor=_ANCHOR, history_days=_HISTORY_DAYS)
    orders = "SELECT * FROM orders"
    uncompilable = FieldContractExpectation(
        field_name="country", drift_type="regex_violation", threshold=0.0, regex="["
    )

    assert adapter.validate_field_contracts(orders, [uncompilable], limit=50) == []

    # The control: a pattern Python DOES compile is still evaluated against every
    # row, so what the skip above removed is the uncompilable contract and not
    # regex checking.
    never_matches = FieldContractExpectation(
        field_name="country", drift_type="regex_violation", threshold=0.0, regex="^zzz"
    )
    violations = adapter.validate_field_contracts(orders, [never_matches], limit=50)
    assert [v.drift_type for v in violations] == ["regex_violation"]
    assert violations[0].bad_rate == 1.0


def test_the_regex_divergence_is_declared_where_the_contract_lives() -> None:
    """One rule in ``BaseAdapter``, and the save gate says what it does NOT promise.

    The engines disagree about which patterns they accept and cannot be made to
    agree — that is a property of three regex libraries, not a defect to fix — so
    the declaration is the deliverable: what diverges, how a divergence behaves
    (inert here, never fatal), and why the screen is not moved to save time.

    Red on revert: every one of these sentences is absent, and the second
    divergence is not even counted — the ABC says "the one known divergence".
    """
    contract = inspect.getdoc(BaseAdapter)
    assert contract is not None
    assert "the known divergences" in contract
    assert "The first is the regex dialect." in contract
    assert "contract_regex_is_compilable" in contract

    probe_doc = inspect.getdoc(BaseAdapter.contract_regex_is_compilable)
    assert probe_doc is not None
    assert "Asking the engine is the whole design" in probe_doc

    # The save gate is a typo screen and now says so, next to the `re.compile`
    # that is the entire promise. Both near-duplicate models carry it, because a
    # rule that lands on Create alone makes PATCH the way around it.
    for model in (FieldDefinitionCreate, FieldDefinitionUpdate):
        gate = inspect.getdoc(model.validate_contract_regex)
        assert gate is not None, model.__name__
        assert "portability guarantee" in gate, model.__name__

    # And the hazard PostgresAdapter documented for as long as nothing defended
    # against it is no longer described as the current behaviour.
    postgres_doc = inspect.getdoc(PostgresAdapter)
    assert postgres_doc is not None
    assert "It is one now." in postgres_doc


# --- the caller: a contract failure of any kind costs only the contract check --


@dataclass
class _StubFieldDefinition:
    """The attributes ``_field_contract_expectations`` reads off a FieldDefinition.

    A stand-in rather than the ORM model because this half of the fix never
    reaches the database: the adapter raises first.
    """

    name: str = "sku"
    contract_regex: str | None = "^(?!test_)"
    is_required: bool = False
    contract_required_max_null_rate: float | None = None
    enum_options: list[str] | None = None
    field_type: str = "string"
    contract_min_value: float | None = None
    contract_max_value: float | None = None
    contract_max_bad_rate: float = 0.0


@dataclass
class _StubEventType:
    id: uuid.UUID = dataclass_field(default_factory=uuid.uuid4)
    field_definitions: list[_StubFieldDefinition] = dataclass_field(
        default_factory=lambda: [_StubFieldDefinition()]
    )


class _RecordingSession:
    """Enough Session for ``_upsert_schema_drifts``: a dialect and an execute.

    ``bind is None`` takes the PostgreSQL branch, which is built and recorded but
    never sent anywhere.
    """

    def __init__(self) -> None:
        self.bind = None
        self.executed: list[object] = []

    def execute(self, statement: object) -> None:
        self.executed.append(statement)


class _RaisingContractAdapter:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def validate_field_contracts(self, *_args: object, **_kwargs: object) -> list[object]:
        raise self._error


class _ViolatingContractAdapter:
    def validate_field_contracts(self, *_args: object, **_kwargs: object) -> list[object]:
        return [
            FieldContractViolation(
                field_name="sku",
                drift_type="regex_violation",
                bad_count=1,
                total_count=10,
                bad_rate=0.1,
                threshold=0.0,
                sample_value="bad",
            )
        ]


def _detect(adapter: object) -> metrics_schema_drift.FieldContractOutcome:
    return metrics_schema_drift._detect_field_contract_violations(
        _RecordingSession(),  # type: ignore[arg-type]
        adapter=adapter,  # type: ignore[arg-type]
        event_type=_StubEventType(),  # type: ignore[arg-type]
        base_query=_BASE,
        columns=[
            ColumnInfo(name="sku", type_name="String"),
            ColumnInfo(name="time", type_name="DateTime"),
        ],
        skip_columns={"time"},
        scan_config_id=uuid.uuid4(),
        time_column="time",
        time_from=_FROM.replace(tzinfo=UTC),
        time_to=_TO.replace(tzinfo=UTC),
    )


def test_a_contract_check_that_fails_costs_the_check_and_not_the_collection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The containment, with the three things that make an ``except Exception`` legible.

    This is the half that holds whatever the probe above does not: a column that
    changed type under a contract, a driver error, an adapter bug. The failure is
    counted, not hidden, and the traceback is logged — a swallow that reports
    nothing is the anti-pattern this is deliberately not.

    Red on revert: the ``RuntimeError`` propagates out of
    ``_detect_field_contract_violations``, exactly as it propagated through
    ``catalog_sync``'s per-group loop and out of the task.
    """
    with caplog.at_level(logging.ERROR, logger=metrics_schema_drift.__name__):
        outcome = _detect(_RaisingContractAdapter(RuntimeError("engine said no")))

    assert outcome == metrics_schema_drift.FieldContractOutcome(
        violations_detected=0, checks_failed=1
    )
    # logger.exception, not logger.error: the traceback is the only record of a
    # genuine adapter bug that lands in this branch.
    assert [record.exc_info is not None for record in caplog.records] == [True]
    assert "engine said no" in caplog.text


def test_a_curated_failure_still_reaches_the_user_verbatim() -> None:
    """The swallow is scoped: an author-written, actionable message is re-raised.

    ``ScanError`` / ``NameFormatError`` / ``WarehouseCapabilityError`` name the
    setting to change and ``user_facing_error`` surfaces them verbatim. Catching
    one here would trade a diagnosable failure for an undiagnosable success,
    which is the objection this whole branch has to answer.

    Red on revert: nothing is caught at all, so this passes for the wrong reason
    — which is why the test above, not this one, is the one that proves the fix.
    """
    with pytest.raises(ScanError, match="raise the row limit"):
        _detect(_RaisingContractAdapter(ScanError("Scan failed: raise the row limit")))


def test_a_check_that_ran_reports_what_it_found_and_no_failure() -> None:
    """The control: the happy path still counts drifts, and counts zero failures.

    Red on revert: ``_detect_field_contract_violations`` returns a bare ``int``,
    so the attribute access fails and the two numbers cannot be told apart at the
    call site at all.
    """
    outcome = _detect(_ViolatingContractAdapter())

    assert outcome.violations_detected == 1
    assert outcome.checks_failed == 0


def test_the_failure_count_reaches_the_job_summary() -> None:
    """A counted failure that no summary carries is a silent one.

    The reason this is asserted on the source rather than on a collected run:
    ``collect_metrics`` needs a database, a warehouse and a Celery session, and
    the wiring it would prove — dataclass field, both call sites, summary key —
    is exactly three lines that a partial fix drops. The behaviour those lines
    carry is proven above.

    Red on revert: the field does not exist, neither call site increments it, and
    the summary has no such key.
    """
    result = metrics_catalog_sync.CatalogSyncResult()
    assert result.contract_checks_failed == 0

    # Both call sites: the grouped loop (once per event type) and the single
    # event-type path. A fix applied to one and missed on the other is the
    # likely partial failure here.
    sync_source = inspect.getsource(metrics_catalog_sync.sync_catalog)
    assert sync_source.count("out.contract_checks_failed += contracts.checks_failed") == 2

    summary_source = inspect.getsource(metrics_tasks.collect_metrics)
    assert '"contract_checks_failed": contract_checks_failed' in summary_source
