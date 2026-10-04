"""ClickHouse adapter: nested columns that are not JSON documents.

ClickHouse has three unrelated nested families (``JSON``,
``Map``, ``Tuple``) and one shape function per family. The adapter used to emit
``arraySort(JSONAllPaths(col))`` for every column the caller put in
``json_columns``, and the caller's split is
``warehouse_types.is_complex_type``, which counts all three as nested. So a
single ``Map`` or ``Tuple`` column anywhere in a source query made the warehouse
reject the statement outright — verified against ClickHouse 26.5.4.14::

    SELECT arraySort(JSONAllPaths(map('a','b')))
    Code: 43. Function JSONAllPaths requires argument with type JSON,
             got: Map(String, String). (ILLEGAL_TYPE_OF_ARGUMENT)

and the same for a Tuple. That killed every scan, every metrics collection and
JSON-key discovery for the whole scan config, with an opaque warehouse error.

These are fake-client SQL-string tests, and ``warehouse-parity.md`` is right that
a SQL string alone proves nothing about validity. What makes them worth writing
anyway is that each expression they pin was *executed* against a real
``clickhouse local`` 26.5.4.14 while the fix was written, including the whole
``GROUP BY ALL`` statement over a table carrying all three families at once. The
executable half belongs in ``tests/conformance/test_clickhouse_conformance.py``,
whose fixture has no Map or Tuple column today; widening that fixture is reported
as a follow-up rather than done here.

Four docstrings in the same file each carried their own copy
of the return contract and three were wrong. Nothing here asserts prose; the
executable statement of those shapes is the conformance gate, which unpacks four
values from ``get_full_breakdown`` and three from ``get_time_bucketed_counts``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from tripl.core.adapters.base import AggregateSpec
from tripl.core.adapters.clickhouse import ClickHouseAdapter
from tripl.core.adapters.multi_aggregate_sql import compile_time_bucketed_multi_aggregate_sql
from tripl.models.domain_enums import MetricAggregation

_BASE = "SELECT * FROM events"
_FROM = datetime(2026, 4, 1, 0, 0)
_TO = datetime(2026, 4, 2, 0, 0)

# One column per nested family, plus the scalars the emitters need for a time
# bucket and a breakdown. The type strings are the ones clickhouse-connect
# actually reports: it re-renders tuple field names with backticks, so a column
# declared ``Tuple(a Int32, b String)`` comes back as ``Tuple(`a` Int32, ...)``.
_COLUMN_TYPES = {
    "time": "DateTime64(6, 'Asia/Tokyo')",
    "event_name": "String",
    "doc": "JSON",
    "props": "Map(String, String)",
    "tup": "Tuple(`a` Int32, `b` String)",
}

_JSON_SHAPE = "arraySort(JSONAllPaths(`doc`))"
_MAP_SHAPE = "arraySort(arrayMap(k -> toString(k), mapKeys(`props`)))"
_TUPLE_SHAPE = "arraySort(tupleNames(`tup`))"


class _Result:
    """A result with no rows: every assertion here is about the SQL sent."""

    column_names: list[str] = []
    result_rows: list[tuple[object, ...]] = []


class _Client:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def query(self, sql: str, **_kwargs: object) -> _Result:
        self.sql.append(sql)
        return _Result()


def _adapter(*, column_types: dict[str, str] | None = None) -> tuple[ClickHouseAdapter, _Client]:
    """A connection-free adapter, built the way production's SQL compiler builds one.

    ``object.__new__`` deliberately: ``core/adapters/multi_aggregate_sql.py`` does
    the same, and the point of several assertions below is that an attribute the
    adapter reads while building SQL must survive a bypassed ``__init__``.
    """
    client = _Client()
    adapter = object.__new__(ClickHouseAdapter)
    adapter._client = client
    adapter._allowed_columns = set(_COLUMN_TYPES)
    adapter._json_path_discovery = "dynamic"
    if column_types is not None:
        adapter._column_types = column_types
    return adapter, client


def _nested_read(adapter: ClickHouseAdapter, method: str, json_columns: list[str]) -> None:
    """Invoke one of the five emitters that renders a nested column's shape.

    Every one of them is a separate hand-written loop over ``json_columns`` in
    the adapter, which is exactly why the tests below are parametrized over all
    five: a partial fix that patches one loop and misses another is the likely
    failure, and a single-method test would wave it through.
    """
    if method == "get_full_breakdown":
        adapter.get_full_breakdown(
            _BASE,
            ["event_name"],
            json_columns,
            None,
            time_column="time",
            time_from=_FROM,
            time_to=_TO,
        )
    elif method == "get_time_bucketed_counts":
        adapter.get_time_bucketed_counts(
            _BASE, "time", "1h", ["event_name"], json_columns, None, _FROM, _TO
        )
    elif method == "get_time_bucketed_aggregate":
        adapter.get_time_bucketed_aggregate(
            _BASE,
            "time",
            "1h",
            MetricAggregation.count,
            None,
            ["event_name"],
            json_columns,
            None,
            _FROM,
            _TO,
        )
    elif method == "get_time_bucketed_aggregate_breakdown":
        adapter.get_time_bucketed_aggregate_breakdown(
            _BASE,
            "time",
            "1h",
            MetricAggregation.count,
            None,
            "event_name",
            ["event_name"],
            json_columns,
            None,
            _FROM,
            _TO,
            # None: no top-values pre-query, so the emitted statement is the only one.
            values_limit=None,
        )
    elif method == "get_time_bucketed_breakdown_counts_multi":
        adapter.get_time_bucketed_breakdown_counts_multi(
            _BASE,
            "time",
            "1h",
            ["event_name"],
            ["event_name"],
            json_columns,
            None,
            _FROM,
            _TO,
            values_limit=None,
        )
    else:  # pragma: no cover - guards a typo in the parametrize list
        msg = f"unknown emitter {method!r}"
        raise AssertionError(msg)


_EMITTERS = [
    "get_full_breakdown",
    "get_time_bucketed_counts",
    "get_time_bucketed_aggregate",
    "get_time_bucketed_aggregate_breakdown",
    "get_time_bucketed_breakdown_counts_multi",
]


@pytest.mark.parametrize("method", _EMITTERS)
def test_map_and_tuple_columns_never_reach_jsonallpaths(method: str) -> None:
    """The defect itself, on all five emitters at once.

    Reverting the fix at any ONE of the five call sites turns exactly that
    parameter red, which is the whole reason this is parametrized.
    """
    adapter, client = _adapter(column_types=dict(_COLUMN_TYPES))

    _nested_read(adapter, method, ["doc", "props", "tup"])

    assert len(client.sql) == 1, client.sql
    sql = client.sql[0]
    # The two calls the warehouse refuses outright...
    assert "JSONAllPaths(`props`)" not in sql
    assert "JSONAllPaths(`tup`)" not in sql
    # ...and its equally JSON-only sibling, in case a fix reached for that instead.
    assert "JSONDynamicPaths" not in sql
    # The per-family replacements, verbatim.
    assert _MAP_SHAPE in sql
    assert _TUPLE_SHAPE in sql
    # Anti-overshoot: a real JSON column must still get JSONAllPaths. A fix that
    # narrows too far — say, one that stops emitting a shape array for anything it
    # cannot name a family for — fails here rather than silently flattening JSON.
    assert _JSON_SHAPE in sql


@pytest.mark.parametrize("method", _EMITTERS)
def test_nested_shape_columns_keep_their_positions_and_names(method: str) -> None:
    """The shape array stays one SELECT item per nested column, in order.

    ``cardinality.py`` reads a nested column's value at ``row[n_reg + j]`` using the
    name lists the adapter returns, so the fix had to replace the *expression* and
    leave the column count, the order and the names alone. A fix that dropped
    Map/Tuple columns, or demoted them into the regular bucket, would pass the
    test above and break here.
    """
    adapter, client = _adapter(column_types=dict(_COLUMN_TYPES))

    _nested_read(adapter, method, ["doc", "props", "tup"])

    sql = client.sql[0]
    positions = [sql.index(shape) for shape in (_JSON_SHAPE, _MAP_SHAPE, _TUPLE_SHAPE)]
    assert positions == sorted(positions), sql
    # The scalar column is referenced before any shape array in every emitter —
    # either as the plain regular column or inside the breakdown expression built
    # from it, both of which precede the nested block.
    assert sql.index("`event_name`") < positions[0], sql


def test_grouping_sets_aliases_every_nested_shape_to_its_column_name() -> None:
    """The GROUPING SETS path aliases the shape expression; the alias must survive.

    ``get_time_bucketed_breakdown_counts_multi`` is the one emitter that does not
    select the shape expression directly — it builds a prepared subquery and then
    groups by the ALIAS. Replacing the expression without keeping ``AS `col``` would
    produce a statement that references a column that is no longer there.
    """
    adapter, client = _adapter(column_types=dict(_COLUMN_TYPES))

    _nested_read(adapter, "get_time_bucketed_breakdown_counts_multi", ["doc", "props", "tup"])

    sql = client.sql[0]
    assert f"{_JSON_SHAPE} AS `doc`" in sql
    assert f"{_MAP_SHAPE} AS `props`" in sql
    assert f"{_TUPLE_SHAPE} AS `tup`" in sql
    # Each alias is then a grouping key in every grouping set.
    assert "`doc`, `props`, `tup`" in sql


def test_get_columns_records_the_declared_type_of_every_column() -> None:
    """Introspection is what arms the per-family choice; wire it end to end.

    Not a getter test: it runs ``get_columns`` against a fake client that reports
    what clickhouse-connect reports, then runs a real emitter on the SAME adapter
    and checks the Map column got Map SQL. Before the fix ``_column_types`` did not
    exist at all, so this is red on revert at the first assertion.
    """

    class _TypeInfo:
        def __init__(self, name: str) -> None:
            self.name = name

    class _SchemaResult(_Result):
        column_names = list(_COLUMN_TYPES)
        column_types = [_TypeInfo(t) for t in _COLUMN_TYPES.values()]

    class _SchemaClient(_Client):
        def query(self, sql: str, **_kwargs: object) -> _Result:
            self.sql.append(sql)
            # Only the LIMIT 0 introspection needs a schema; the read that follows
            # needs rows, and there are none. The read's own limit is 100000, so
            # this discriminator cannot match it.
            return _SchemaResult() if "LIMIT 0" in sql else _Result()

    client = _SchemaClient()
    adapter = object.__new__(ClickHouseAdapter)
    adapter._client = client

    columns = adapter.get_columns(_BASE)

    assert adapter._column_types == _COLUMN_TYPES
    assert [c.name for c in columns] == list(_COLUMN_TYPES)

    _nested_read(adapter, "get_time_bucketed_counts", ["doc", "props", "tup"])
    read_sql = client.sql[-1]
    assert _MAP_SHAPE in read_sql
    assert _TUPLE_SHAPE in read_sql
    assert _JSON_SHAPE in read_sql


def test_json_path_discovery_skips_map_and_tuple_columns() -> None:
    """Discovery is JSON-only: no query, no error, and an empty sample map.

    ``JSONDynamicPaths``/``JSONAllPaths`` reject a Map or Tuple argument the same
    way the scan path did, so the pre-fix adapter failed discovery for the whole
    request as soon as one non-JSON nested column was in the list — including for
    the JSON columns that were in the same call. Reverting restores the discovery
    query for ``props`` and turns the first assertion red.
    """
    adapter, client = _adapter(column_types=dict(_COLUMN_TYPES))

    samples = adapter.get_json_path_samples(_BASE, ["doc", "props", "tup"])

    assert all("`props`" not in sql for sql in client.sql), client.sql
    assert all("`tup`" not in sql for sql in client.sql), client.sql
    assert samples["props"] == {}
    assert samples["tup"] == {}
    # The JSON column is still discovered, with the configured enumerator.
    assert any("JSONDynamicPaths(`doc`)" in sql for sql in client.sql), client.sql
    assert samples["doc"] == {}  # the fake client returns no paths


def test_a_scalar_column_in_the_nested_list_is_named_not_guessed() -> None:
    """A scalar that reaches ``json_columns`` is a caller bug; say so in Python.

    Before the fix it produced ``JSONAllPaths(`event_name`)`` and a warehouse
    round-trip ending in ILLEGAL_TYPE_OF_ARGUMENT several layers below the mistake.
    Reverting makes the call succeed and emit that SQL, so this goes red.
    """
    adapter, client = _adapter(column_types=dict(_COLUMN_TYPES))

    with pytest.raises(ValueError, match="scalar type String"):
        _nested_read(adapter, "get_full_breakdown", ["event_name"])

    assert client.sql == []


def test_an_uninspected_adapter_keeps_the_pre_fix_json_behaviour() -> None:
    """No type map at all must mean JSON, not ``AttributeError``.

    This is the failure mode the sibling BigQuery adapter is exposed to: it declares
    its type map in ``__init__``, and five call sites in this repo build a
    ClickHouse adapter with ``object.__new__``. Two of them
    (``tests/test_warehouse_bucketing.py``, ``tests/test_schema_introspection.py``)
    set neither ``_column_types`` nor anything like it, so the default has to live
    on the class. Deliberately NOT revert-sensitive — it is red for the most likely
    *wrong* implementation of this fix, not for its absence.
    """
    adapter, client = _adapter()  # no column_types: nothing was ever introspected

    _nested_read(adapter, "get_full_breakdown", ["doc", "props"])

    sql = client.sql[0]
    assert "arraySort(JSONAllPaths(`doc`))" in sql
    assert "arraySort(JSONAllPaths(`props`))" in sql


def test_compiled_batch_sql_primes_the_adapter_with_its_column_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connection-free SQL compiler builds a faithful adapter, not a partial one.

    ``compile_time_bucketed_multi_aggregate_sql`` hand-builds a ClickHouse adapter
    with ``object.__new__`` and has to prime whatever state the builder reads. The
    multi-aggregate statement takes no nested columns, so the priming leaves no mark
    on its output and this has to reach for the adapter itself. Worth pinning anyway:
    an un-primed stand-in is exactly the bug that would resurface the moment any
    type-directed SQL reaches that path, which it already has on BigQuery — the same
    function's BigQuery branch primes ``_column_types`` because the bucket and bound
    literals are chosen from it.
    """
    seen: list[dict[str, str]] = []
    original = ClickHouseAdapter.build_time_bucketed_multi_aggregate_sql

    def _capture(self: ClickHouseAdapter, *args: Any, **kwargs: Any) -> Any:
        seen.append(dict(self._column_types))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ClickHouseAdapter, "build_time_bucketed_multi_aggregate_sql", _capture)
    col_names, sql = compile_time_bucketed_multi_aggregate_sql(
        db_type="clickhouse",
        base_query=_BASE,
        time_column="time",
        interval="1d",
        specs=[AggregateSpec(key="k_count", aggregation=MetricAggregation.count)],
        time_from=_FROM,
        time_to=_TO,
        column_types=_COLUMN_TYPES,
    )

    assert seen == [_COLUMN_TYPES]
    assert col_names == ["bucket", "k_count"]
    assert "count(*) AS `k_count`" in sql
