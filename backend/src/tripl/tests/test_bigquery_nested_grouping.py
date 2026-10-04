"""BigQuery: nested/array columns must produce GROUP-BY-able SQL, and must still
come back as lists.

The bug this file pins down: ``_json_paths_expression`` returned an
``ARRAY<STRING>`` and every caller put it straight into ``GROUP BY``. GoogleSQL rejects
that outright — verified against Google's real ZetaSQL analyzer:

    GROUP BY ['a','b']                      -> Cannot GROUP BY literal values
    GROUP BY (SELECT ARRAY_AGG(...) ...)    -> Grouping by expressions of type ARRAY is
                                               not allowed

So *every* BigQuery scan touching a JSON, STRUCT/RECORD or REPEATED column was invalid
SQL that could never have run. It stayed invisible because the adapter tests assert SQL
strings against a fake client, and a fake client accepts anything.

Two invariants are therefore pinned here, because either one alone is insufficient:

1. SQL SHAPE — no array-valued expression may reach a GROUP BY. A test that only checked
   this would still pass an implementation that grouped correctly but handed the JSON
   *string* back to callers.
2. ROW CONTRACT — the grouped value must be decoded back into a ``list`` before it leaves
   the adapter. ``BaseAdapter`` documents the json-paths column as an ARRAY of paths, and
   ``cardinality._process_breakdown`` branches on ``isinstance(paths, (list, tuple))``:
   handed a raw ``'["a","user.name"]'`` string it reads the whole blob as ONE path and
   silently corrupts every cardinality count. That corruption is silent — no exception,
   no invalid SQL — so it needs its own test.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime

import pytest

from tripl.core.adapters.base import AggregateSpec, ColumnInfo
from tripl.core.adapters.bigquery import BigQueryAdapter, _decode_grouped_array
from tripl.core.analyzers.cardinality import analyze_cardinality
from tripl.models.domain_enums import MetricAggregation
from tripl.tests.conformance.bigquery_values import _string_literal

_FROM = datetime(2026, 4, 1)
_TO = datetime(2026, 4, 2)
# An explicit column list, not `SELECT *` — the tests below assert on whether the
# *adapter* re-exposes raw columns with a `SELECT *`, so the base query must not
# smuggle one in.
_BASE = "SELECT time, event_name, amount, props, usr, tags FROM events"

# `props` is a JSON document (paths discovered from the data), `usr` a RECORD (paths
# declared by the schema), `tags` an ARRAY<STRING>. BigQuery gives an array no distinct
# *type* — an ARRAY<STRING> column reports field_type STRING — so array-ness is carried
# by mode=REPEATED alone, which is exactly why `tags` was mistaken for a plain scalar.
_TYPES = {
    "time": "TIMESTAMP",
    "event_name": "STRING",
    "amount": "FLOAT64",
    "props": "JSON",
    "usr": "RECORD",
    "tags": "STRING",
}
_STRUCT_PATHS = {"usr": {"id": True, "name": True}}


class _BQRow:
    def __init__(self, values: tuple[object, ...]) -> None:
        self._values = values

    def values(self) -> tuple[object, ...]:
        return self._values


class _BQResult:
    schema: list[object] = []

    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[object]:
        return iter(_BQRow(r) for r in self._rows)


class _BQJob:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def result(self, **_kwargs: object) -> _BQResult:
        return _BQResult(self._rows)


class _BQClient:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.rows: list[tuple[object, ...]] = []

    def query(self, sql: str) -> _BQJob:
        self.sql.append(sql)
        return _BQJob(self.rows)


def _bq() -> tuple[BigQueryAdapter, _BQClient]:
    client = _BQClient()
    adapter = object.__new__(BigQueryAdapter)
    adapter._client = client
    adapter._allowed_columns = set(_TYPES)
    adapter._column_types = dict(_TYPES)
    adapter._struct_paths = {k: dict(v) for k, v in _STRUCT_PATHS.items()}
    adapter._repeated_columns = {"tags"}
    return adapter, client


def _group_by_clause(sql: str) -> str:
    """The top-level GROUP BY clause, which is where an ARRAY is fatal."""
    match = re.search(r" GROUP BY (.*?) ORDER BY ", sql)
    assert match is not None, f"expected a GROUP BY ... ORDER BY in: {sql}"
    return match.group(1)


# Every method that selects a nested/array column. Each is invoked with a JSON column, a
# RECORD column and a REPEATED scalar column at once, so one case covers all three shapes.
def _call(adapter: BigQueryAdapter, method: str) -> None:
    reg = ["event_name", "tags"]
    nested = ["props", "usr"]
    paths = {"props": ["a"]}
    if method == "get_full_breakdown":
        adapter.get_full_breakdown(
            _BASE,
            reg,
            nested,
            json_value_paths=paths,
            time_column="time",
            time_from=_FROM,
            time_to=_TO,
        )
    elif method == "get_time_bucketed_counts":
        adapter.get_time_bucketed_counts(_BASE, "time", "1h", reg, nested, paths, _FROM, _TO)
    elif method == "get_time_bucketed_aggregate":
        adapter.get_time_bucketed_aggregate(
            _BASE,
            "time",
            "1h",
            MetricAggregation.sum,
            "amount",
            reg,
            nested,
            paths,
            _FROM,
            _TO,
        )
    elif method == "get_time_bucketed_aggregate_breakdown":
        adapter.get_time_bucketed_aggregate_breakdown(
            _BASE,
            "time",
            "1d",
            MetricAggregation.sum,
            "amount",
            "event_name",
            reg,
            nested,
            paths,
            _FROM,
            _TO,
        )
    elif method == "get_time_bucketed_breakdown_counts":
        adapter.get_time_bucketed_breakdown_counts(
            _BASE, "time", "1d", "event_name", reg, nested, paths, _FROM, _TO
        )
    elif method == "get_time_bucketed_breakdown_counts_multi":
        adapter.get_time_bucketed_breakdown_counts_multi(
            _BASE, "time", "1d", ["event_name"], reg, nested, paths, _FROM, _TO
        )
    else:  # pragma: no cover - guards a typo in the parametrize list
        raise AssertionError(f"unknown method {method}")


_NESTED_METHODS = [
    "get_full_breakdown",
    "get_time_bucketed_counts",
    "get_time_bucketed_aggregate",
    "get_time_bucketed_aggregate_breakdown",
    "get_time_bucketed_breakdown_counts",
    "get_time_bucketed_breakdown_counts_multi",
]


@pytest.mark.parametrize("method", _NESTED_METHODS)
def test_no_array_expression_reaches_group_by(method: str) -> None:
    """The regression guard: GoogleSQL cannot GROUP BY an ARRAY, in any spelling.

    Each of these fragments is array-valued, and each is what the buggy adapter grouped by:
      - ``JSON_KEYS(...)`` / ``ARRAY_AGG(...)`` — the computed leaf-path array of a JSON
        column ("Grouping by expressions of type ARRAY is not allowed")
      - ``['user.id', ...]`` / ``ARRAY<STRING>[]`` — the constant path array of a STRUCT
        column ("Cannot GROUP BY literal values")
    They may appear in the SELECT list or in a prepared subquery; they may never appear in
    a GROUP BY.
    """
    adapter, client = _bq()
    _call(adapter, method)
    clause = _group_by_clause(client.sql[-1])

    for array_fragment in ("JSON_KEYS(", "ARRAY_AGG(", "ARRAY<STRING>", "['"):
        assert array_fragment not in clause, (
            f"{method}: array-valued {array_fragment!r} reached GROUP BY — "
            f"GoogleSQL rejects this. Clause: {clause}"
        )


@pytest.mark.parametrize("method", _NESTED_METHODS)
def test_repeated_scalar_column_is_rendered_to_a_scalar_before_grouping(method: str) -> None:
    """A REPEATED scalar column is an ARRAY value, so it can never be grouped as itself.

    ``tags`` reports field_type STRING, so it was classified as a plain scalar and grouped
    by directly — ``GROUP BY `tags``` — which is the same ARRAY error. The one and only way
    it may enter the query is through ``TO_JSON_STRING``, whether the method groups by the
    expression (flat paths) or by an alias bound to it (the GROUPING SETS path).
    """
    adapter, client = _bq()
    _call(adapter, method)
    sql = client.sql[-1]
    clause = _group_by_clause(sql)
    terms = {term.strip() for term in clause.split(",")}

    # Whatever the shape, the array is rebound to a scalar STRING before it is grouped.
    assert "TO_JSON_STRING(`tags`) AS `tags`" in sql, (
        f"{method}: REPEATED column is never rendered to a groupable scalar: {sql}"
    )

    if "GROUPING SETS" in sql:
        # The prepared projection rebinds the *name* `tags` to the TO_JSON_STRING value, so
        # the GROUP BY's ``tags`` term is that scalar alias. This only holds because the
        # projection is explicit — a `SELECT *` would re-expose the raw ARRAY under the
        # same name and silently un-fix the grouping.
        assert "SELECT *" not in sql, f"{method}: prepared projection re-exposes raw columns"
    else:
        # Here the GROUP BY sits over a source that still exposes the raw ARRAY column, so
        # it must group the TO_JSON_STRING *expression* and never the column itself.
        assert "TO_JSON_STRING(`tags`)" in clause, f"{method}: clause={clause}"
        assert "`tags`" not in terms, (
            f"{method}: REPEATED column grouped raw as an ARRAY. Clause: {clause}"
        )


@pytest.mark.parametrize("method", _NESTED_METHODS)
def test_nested_paths_are_still_grouped(method: str) -> None:
    """The fix must not silently *drop* the nested columns from the grouping.

    Dropping them would also make the SQL valid, and would also break the breakdown — so
    "no array in GROUP BY" is only half the invariant. Each nested column must still be
    grouped: by its pre-materialized ``__np_``/``__nv_`` alias on the flat paths, or by the
    column-named alias the GROUPING SETS path binds in its own prepared subquery.
    """
    adapter, client = _bq()
    _call(adapter, method)
    sql = client.sql[-1]
    clause = _group_by_clause(sql)

    for name, alias in (("props", "__np_0"), ("usr", "__np_1"), ("props.a", "__nv_0")):
        assert alias in clause or f"`{name}`" in clause, (
            f"{method}: nested column {name!r} is not grouped at all: {clause}"
        )
        # BigQuery result-field names cannot contain dots, even when quoted with
        # backticks. Dotted public names are returned separately by the adapter, so
        # path values stay positional under their safe internal alias in GoogleSQL.
        output_alias = alias if "." in name else name
        assert f"AS `{output_alias}`" in sql, (
            f"{method}: nested column {name!r} is not selected under {output_alias!r}"
        )
        if "." in name:
            assert f"AS `{name}`" not in sql, (
                f"{method}: dotted result alias is rejected by real BigQuery: {sql}"
            )


# --- row contract: the JSON string must never escape the adapter ----------------


def test_full_breakdown_decodes_paths_back_into_lists() -> None:
    """The grouped value is a JSON string in SQL, but a ``list`` in the returned rows."""
    adapter, client = _bq()
    # Row layout: (event_name, tags, props, usr, props.a, _cnt)
    client.rows = [("click", '["x","y"]', '["a","user.name"]', '["id","name"]', "1", 3)]
    _reg, _json, values, rows = adapter.get_full_breakdown(
        _BASE,
        ["event_name", "tags"],
        ["props", "usr"],
        json_value_paths={"props": ["a"]},
        time_column="time",
        time_from=_FROM,
        time_to=_TO,
    )
    assert values == ["props.a"]
    assert rows == [("click", ["x", "y"], ["a", "user.name"], ["id", "name"], "1", 3)]
    # The kept JSON *value* stays a scalar string, exactly as ClickHouse's
    # toJSONString(...) returns it — only the path arrays are decoded.
    assert rows[0][4] == "1"


def test_bigquery_value_fixture_escapes_control_characters() -> None:
    rendered = _string_literal("line 1\nline 2\r\t\\'\0")
    assert "\n" not in rendered
    assert "\r" not in rendered
    assert "\\n" in rendered
    assert "\\r" in rendered
    assert "\\t" in rendered
    assert "\\\\" in rendered
    assert "\\'" in rendered
    assert "\\000" in rendered


@pytest.mark.parametrize(
    ("method", "offset"),
    [
        ("get_time_bucketed_counts", 1),
        ("get_time_bucketed_aggregate", 1),
        ("get_time_bucketed_aggregate_breakdown", 3),
        ("get_time_bucketed_breakdown_counts_multi", 4),
    ],
)
def test_bucketed_methods_decode_paths_back_into_lists(method: str, offset: int) -> None:
    """Every method that selects a json-paths column must decode it, not just one."""
    adapter, client = _bq()
    lead: tuple[object, ...] = tuple(range(offset))
    client.rows = [(*lead, "click", '["x"]', '["a","user.name"]', '["id"]', "1", 7)]
    if method == "get_time_bucketed_counts":
        _c, _v, rows = adapter.get_time_bucketed_counts(
            _BASE,
            "time",
            "1h",
            ["event_name", "tags"],
            ["props", "usr"],
            {"props": ["a"]},
            _FROM,
            _TO,
        )
    elif method == "get_time_bucketed_aggregate":
        _c, _v, rows = adapter.get_time_bucketed_aggregate(
            _BASE,
            "time",
            "1h",
            MetricAggregation.sum,
            "amount",
            ["event_name", "tags"],
            ["props", "usr"],
            {"props": ["a"]},
            _FROM,
            _TO,
        )
    elif method == "get_time_bucketed_aggregate_breakdown":
        _c, _v, rows = adapter.get_time_bucketed_aggregate_breakdown(
            _BASE,
            "time",
            "1d",
            MetricAggregation.sum,
            "amount",
            "event_name",
            ["event_name", "tags"],
            ["props", "usr"],
            {"props": ["a"]},
            _FROM,
            _TO,
        )
    else:
        _c, _v, rows = adapter.get_time_bucketed_breakdown_counts_multi(
            _BASE,
            "time",
            "1d",
            ["event_name"],
            ["event_name", "tags"],
            ["props", "usr"],
            {"props": ["a"]},
            _FROM,
            _TO,
        )

    row = rows[0]
    assert row[offset + 1] == ["x"], f"{method}: REPEATED column not decoded: {row}"
    assert row[offset + 2] == ["a", "user.name"], f"{method}: json paths not decoded: {row}"
    assert row[offset + 3] == ["id"], f"{method}: struct paths not decoded: {row}"


def test_cardinality_is_not_corrupted_by_the_grouped_json_string() -> None:
    """The corruption this bug would have caused, if the decode were skipped.

    ``cardinality._process_breakdown`` counts one "value" per unique *set of paths* and
    branches on ``isinstance(paths, (list, tuple))``. Handed the raw grouped string it
    falls into the ``else`` and treats ``'["a","user.name"]'`` as a single path — the
    combos come back as one opaque blob each instead of the real paths, and no error is
    ever raised. This is the silent failure mode, so it gets an explicit test.
    """
    adapter, client = _bq()
    # Row layout: (event_name, props, _cnt) — two distinct path sets.
    client.rows = [
        ("click", '["a","user.name"]', 3),
        ("view", '["a"]', 1),
    ]
    columns = [
        ColumnInfo(name="event_name", type_name="STRING"),
        ColumnInfo(name="props", type_name="JSON"),
    ]

    analysis = analyze_cardinality(adapter, _BASE, columns, threshold=100)

    props = analysis.results["props"]
    assert props.count == 2
    # The real leaf paths, as separate elements — NOT one '["a","user.name"]' blob.
    assert props.json_path_combos == [("a", "user.name"), ("a",)]
    for combo in props.json_path_combos or []:
        for path in combo:
            assert not path.startswith("["), f"path {path!r} is an undecoded JSON blob"


# --- a breakdown on an array column must fail loudly, not emit invalid SQL ------


@pytest.mark.parametrize(
    "method",
    [
        "get_time_bucketed_aggregate_breakdown",
        "get_time_bucketed_breakdown_counts_multi",
        "get_time_bucketed_multi_aggregate_breakdown",
    ],
)
def test_breakdown_on_repeated_column_is_rejected(method: str) -> None:
    """``CAST(<array> AS STRING)`` is not a legal GoogleSQL cast.

    A breakdown on an ARRAY column has no meaning and cannot be compiled, so it must be
    refused while the caller is still choosing the column — not compiled into SQL that
    only explodes once a worker runs it.
    """
    adapter, _ = _bq()
    with pytest.raises(ValueError, match="REPEATED"):
        if method == "get_time_bucketed_aggregate_breakdown":
            adapter.get_time_bucketed_aggregate_breakdown(
                _BASE,
                "time",
                "1d",
                MetricAggregation.count,
                None,
                "tags",
                ["event_name", "tags"],
                [],
                None,
                _FROM,
                _TO,
            )
        elif method == "get_time_bucketed_breakdown_counts_multi":
            adapter.get_time_bucketed_breakdown_counts_multi(
                _BASE, "time", "1d", ["tags"], ["event_name", "tags"], [], None, _FROM, _TO
            )
        else:
            # A real spec: with no specs this method short-circuits before it ever builds
            # the breakdown expression, so an empty list would not exercise the guard.
            adapter.get_time_bucketed_multi_aggregate_breakdown(
                _BASE,
                "time",
                "1d",
                "tags",
                [AggregateSpec(key="k", aggregation=MetricAggregation.count)],
                _FROM,
                _TO,
            )


# --- the decoder itself ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('["a","user.name"]', ["a", "user.name"]),
        ("[]", []),
        # TO_JSON_STRING of a NULL array renders the string "null".
        ("null", None),
        (None, None),
        # A fake/mock client (or a natively-arrayed column) may hand back a real list.
        (["a", "b"], ["a", "b"]),
        # A REPEATED INT64 column decodes to ints, matching what ClickHouse returns for
        # an Array(Int64).
        ("[1,2]", [1, 2]),
    ],
)
def test_decode_grouped_array(raw: object, expected: object) -> None:
    assert _decode_grouped_array(raw) == expected


def test_decode_grouped_array_rejects_garbage() -> None:
    """A value that is not valid JSON is a bug, not something to pass through silently.

    Passing it through is precisely how the whole blob would end up read as one path.
    """
    with pytest.raises(ValueError, match="could not decode"):
        _decode_grouped_array("not json")


@pytest.mark.parametrize("raw", ['{"a":1}', '"a string"', "42", "true"])
def test_decode_grouped_array_rejects_valid_json_that_is_not_a_list(raw: str) -> None:
    """Valid JSON is not enough — it has to decode to a LIST.

    ``json.loads`` will cheerfully hand back a dict, a bare string or a number. Any of
    those reaching the json-paths column defeats the entire point of this function:
    ``cardinality._process_breakdown`` branches on ``isinstance(paths, (list, tuple))``
    and, when that is False, reads the value as ONE path — silently corrupting every
    cardinality count with no error raised. That is the exact failure this decode step
    exists to prevent, so a non-list has to be loud.

    ``null`` stays allowed — it is how TO_JSON_STRING renders a NULL array — and is
    covered above.
    """
    with pytest.raises(ValueError, match="not a list"):
        _decode_grouped_array(raw)


def test_get_columns_records_repeated_mode() -> None:
    """Array-ness lives in ``mode``, not in the type — so it has to be captured there."""

    class _Field:
        def __init__(self, name: str, field_type: str, mode: str) -> None:
            self.name = name
            self.field_type = field_type
            self.mode = mode
            self.fields: list[object] = []

    class _SchemaResult:
        schema = [
            _Field("event_name", "STRING", "NULLABLE"),
            _Field("tags", "STRING", "REPEATED"),
        ]

        def __iter__(self) -> Iterator[object]:
            return iter(())

    class _SchemaJob:
        def result(self, **_kwargs: object) -> _SchemaResult:
            return _SchemaResult()

    class _SchemaClient:
        def query(self, _sql: str) -> _SchemaJob:
            return _SchemaJob()

    adapter = object.__new__(BigQueryAdapter)
    adapter._client = _SchemaClient()
    columns = adapter.get_columns(_BASE)

    assert [c.name for c in columns] == ["event_name", "tags"]
    # The declared type is a plain STRING — which is exactly why the mode must be kept.
    assert adapter._column_types["tags"] == "STRING"
    assert adapter._repeated_columns == {"tags"}
