"""A Trino ``map`` column is path-expanded, like a ClickHouse ``Map`` or a Databricks ``map<>``.

The scan's split (``core.warehouse_types.classify_complex``) sends every map
column to the adapter as a nested column. The Trino adapter used to accept
only ``json`` there, so one map column in a base query (common in Hive and
Iceberg tables, and in any ``SELECT *``) failed the scan, its preview and
metric collection. It now reads a map through its JSON cast, so the map's keys
are properties. ``row`` and ``array`` columns stay values. Athena reports a
bare ``map`` type, which the split leaves a value. What the engine computes
from these statements is pinned by ``conformance/test_trino_value_conformance.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tripl.core.adapters.athena import AthenaAdapter
from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers.cardinality import analyze_cardinality
from tripl.core.warehouse_types import ComplexKind, classify_complex, is_complex_type
from tripl.tests.test_trino_adapter import FakeConnection, make_adapter

_FROM = datetime(2026, 4, 1, tzinfo=UTC)
_TO = datetime(2026, 4, 2, tzinfo=UTC)
_BASE = "SELECT ts, event_name, attrs, scores, r, tags FROM events"
_TYPES = {
    "ts": "timestamp(3) with time zone",
    "event_name": "varchar",
    "attrs": "map(varchar, varchar)",
    "scores": "map(bigint, double)",
    "r": "row(a integer, b varchar)",
    "tags": "array(varchar)",
}
_ATTRS = 'CAST("attrs" AS json)'


def _columns(*names: str) -> list[ColumnInfo]:
    return [ColumnInfo(name=name, type_name=_TYPES[name], is_nullable=True) for name in names]


def test_the_split_sends_a_map_as_nested_and_a_row_or_array_as_a_value() -> None:
    assert classify_complex(_TYPES["attrs"]) is ComplexKind.map
    assert classify_complex(_TYPES["scores"]) is ComplexKind.map
    assert not is_complex_type(_TYPES["r"])
    assert not is_complex_type(_TYPES["tags"])
    # Athena names no key type: its map is a value, never a document.
    assert not is_complex_type("map")


def test_a_map_is_read_through_its_json_cast() -> None:
    adapter, _ = make_adapter(_TYPES)
    assert adapter._json_paths_expression("attrs") == (
        "COALESCE(json_format(CAST(array_sort(map_keys("
        f"try_cast({_ATTRS} AS map(varchar, json)))) AS json)), '[]')"
    )
    assert adapter._json_path_expression("attrs", "plan") == (
        f"json_format(json_extract({_ATTRS}, '$[\"plan\"]'))"
    )
    assert adapter._property_value_expression("attrs", "plan") == (
        f"COALESCE(json_extract_scalar({_ATTRS}, '$[\"plan\"]'), "
        f"NULLIF(json_format(json_extract({_ATTRS}, '$[\"plan\"]')), 'null'))"
    )
    # Numeric keys cast to JSON object keys too.
    assert 'CAST("scores" AS json)' in adapter._json_paths_expression("scores")


@pytest.mark.parametrize("column", ["r", "tags", "event_name"])
def test_a_row_an_array_or_a_scalar_is_still_no_document(column: str) -> None:
    adapter, _ = make_adapter(_TYPES)
    with pytest.raises(ValueError, match="Only json and map columns can be path-expanded"):
        adapter._json_paths_expression(column)


def test_a_scan_over_a_map_column_groups_its_key_sets() -> None:
    adapter, conn = make_adapter(_TYPES)
    # (event_name, r, attrs keys, count): the layout of ``get_full_breakdown``.
    conn.answers.append(
        (
            [],
            [
                ("signup", '{"a":1,"b":"x"}', '["plan","tier"]', 5),
                ("signup", '{"a":2,"b":"y"}', '["plan"]', 2),
                ("login", None, "[]", 1),
            ],
        )
    )
    analysis = analyze_cardinality(adapter, _BASE, _columns("event_name", "attrs", "r"))
    sql = conn.sql[0]
    assert f"{adapter._json_paths_expression('attrs')} AS __np_0" in sql
    assert analysis.reg_names == ["event_name", "r"]
    assert analysis.json_names == ["attrs"]
    assert analysis.results["attrs"].json_path_combos == [("plan", "tier"), ("plan",), ()]
    assert analysis.results["r"].count == 2


def test_metric_collection_reads_a_map_property() -> None:
    adapter, conn = make_adapter(_TYPES)
    conn.answers.append(([], [(datetime(2026, 4, 1), "signup", '["plan"]', '"pro"', 3)]))
    names, value_names, rows = adapter.get_time_bucketed_counts(
        _BASE, "ts", "1d", ["event_name"], ["attrs"], {"attrs": ["plan"]}, _FROM, _TO
    )
    sql = conn.sql[0]
    assert f"json_format(json_extract({_ATTRS}, '$[\"plan\"]')) AS __nv_0" in sql
    assert names == ["event_name", "attrs"]
    assert value_names == ["attrs.plan"]
    assert rows == [(datetime(2026, 4, 1, tzinfo=UTC), "signup", ["plan"], '"pro"', 3)]


def test_athena_reads_its_bare_map_as_a_value() -> None:
    conn = FakeConnection()
    adapter = object.__new__(AthenaAdapter)
    adapter._conn = conn
    adapter._column_types = {"event_name": "varchar", "attrs": "map"}
    adapter._allowed_columns = {"event_name", "attrs"}
    conn.answers.append(([], [("signup", '{"plan":"pro"}', 4)]))
    columns = [
        ColumnInfo(name="event_name", type_name="varchar", is_nullable=True),
        ColumnInfo(name="attrs", type_name="map", is_nullable=True),
    ]
    analysis = analyze_cardinality(adapter, _BASE, columns)
    assert analysis.reg_names == ["event_name", "attrs"]
    assert analysis.json_names == []
    assert adapter._text("attrs") == 'json_format(CAST("attrs" AS json))'
    with pytest.raises(ValueError, match="holds no nested paths"):
        adapter._json_paths_expression("attrs")
