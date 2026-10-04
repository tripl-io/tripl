"""Gate 2: the real ClickHouseAdapter against a real ``clickhouse-server``.

Same shape as the PostgreSQL gate — the SQL is EXECUTED and the rows are compared
against ``floor_to_bucket`` — with one deliberate twist: **the fixture's time column
is declared in a non-UTC timezone** (``DateTime64(6, 'Asia/Tokyo')``).

That is not decoration. ClickHouse reads an offset-less timestamp literal in the
*column's* timezone, so the pre-fix adapter, which emitted
``ts >= '2026-04-02 00:00:00'``, compared the bound as Tokyo wall clock and silently
scanned a nine-hour-shifted window. Every assertion in this file runs against that
column, so the whole gate is the regression test.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tripl.core.adapters.base import AggregateSpec
from tripl.core.adapters.clickhouse import ClickHouseAdapter
from tripl.core.bucketing import floor_to_bucket
from tripl.models.domain_enums import MetricAggregation
from tripl.tests.conformance.conftest import TABLE
from tripl.tests.conformance.dataset import (
    FROM_TIME,
    IN_WINDOW_IDS,
    INTERVALS,
    SHIFTED_WINDOW_IDS,
    TO_TIME,
    contract_expectations,
    expected_bucket_counts,
    expected_bucket_sums,
    expected_contract_violations,
    expected_json_leaf_paths,
    in_window_rows,
    json_null_only_leaf_paths,
)

BASE = f"SELECT * FROM {TABLE}"


def _utc(value: object) -> datetime:
    assert isinstance(value, datetime)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _bucket_counts(adapter: ClickHouseAdapter, interval: str) -> dict[datetime, int]:
    adapter.get_columns(BASE)
    _, _, rows = adapter.get_time_bucketed_counts(
        BASE, "ts", interval, [], [], None, FROM_TIME, TO_TIME
    )
    return {_utc(row[0]): int(row[-1]) for row in rows}  # type: ignore[arg-type]


def _window_ids(adapter: ClickHouseAdapter) -> set[int]:
    adapter.get_columns(BASE)
    _, _, _, rows = adapter.get_full_breakdown(
        BASE,
        ["id"],
        [],
        None,
        time_column="ts",
        time_from=FROM_TIME,
        time_to=TO_TIME,
    )
    return {int(row[0]) for row in rows}  # type: ignore[arg-type]


# --- the bucket contract, on a Tokyo-declared column ---------------------------


@pytest.mark.parametrize("interval", INTERVALS)
def test_buckets_match_floor_to_bucket(ch: ClickHouseAdapter, interval: str) -> None:
    assert _bucket_counts(ch, interval) == expected_bucket_counts(interval)


def test_week_buckets_land_on_monday(ch: ClickHouseAdapter) -> None:
    buckets = _bucket_counts(ch, "1w")
    assert buckets, "the 1w scan returned no buckets at all"
    assert all(bucket.weekday() == 0 for bucket in buckets), sorted(buckets)


def test_week_buckets_stay_datetimes(ch: ClickHouseAdapter) -> None:
    # toStartOfInterval(col, INTERVAL 1 WEEK) returns a Date, which would come back
    # as datetime.date while every other interval yields datetime.datetime. The
    # adapter's toDateTime(toMonday(...)) keeps the bucket column type uniform.
    ch.get_columns(BASE)
    _, _, rows = ch.get_time_bucketed_counts(BASE, "ts", "1w", [], [], None, FROM_TIME, TO_TIME)
    assert rows
    assert all(isinstance(row[0], datetime) for row in rows), [type(r[0]) for r in rows]


def test_window_is_half_open(ch: ClickHouseAdapter) -> None:
    ids = _window_ids(ch)
    assert ids == set(IN_WINDOW_IDS)
    assert 1 in ids  # exactly on time_from
    assert 8 not in ids  # exactly on time_to
    assert 9 not in ids  # one microsecond before time_from


def test_the_tokyo_column_fixture_actually_has_teeth(ch: ClickHouseAdapter) -> None:
    """Prove the non-UTC column is a live trap, not a decorative one.

    Runs the *pre-fix* SQL — an offset-less literal, which is what
    ``strftime("%Y-%m-%d %H:%M:%S")`` produced — against the same Tokyo-declared
    column and shows it selects a DIFFERENT SET of rows. If it ever stops doing so,
    the gate above has quietly gone toothless and this test says so.
    """
    client = ch._client  # noqa: SLF001 — deliberately bypassing the adapter's own SQL
    result = client.query(
        f"SELECT id FROM {TABLE} "
        "WHERE ts >= '2026-04-02 00:00:00' AND ts < '2026-04-09 00:00:00' "
        "ORDER BY id"
    )
    shifted = {int(row[0]) for row in result.result_rows}
    assert shifted != set(IN_WINDOW_IDS)
    assert shifted == set(SHIFTED_WINDOW_IDS)


# --- aggregates, breakdowns, multi-aggregates ---------------------------------


def test_bucketed_aggregate_sums_match_the_contract(ch: ClickHouseAdapter) -> None:
    ch.get_columns(BASE)
    _, _, rows = ch.get_time_bucketed_aggregate(
        BASE,
        "ts",
        "1d",
        MetricAggregation.sum,
        "amount",
        [],
        [],
        None,
        FROM_TIME,
        TO_TIME,
    )
    actual = {_utc(row[0]): (None if row[-1] is None else float(row[-1])) for row in rows}  # type: ignore[arg-type]
    assert actual == expected_bucket_sums("1d")


def test_bucketed_breakdown_counts_group_in_the_database(ch: ClickHouseAdapter) -> None:
    ch.get_columns(BASE)
    _, _, rows = ch.get_time_bucketed_breakdown_counts(
        BASE, "ts", "1d", "event_name", ["event_name"], [], None, FROM_TIME, TO_TIME
    )
    actual: dict[tuple[datetime, str], int] = {
        (_utc(row[0]), str(row[1])): int(row[-1])  # type: ignore[arg-type]
        for row in rows
    }
    expected: dict[tuple[datetime, str], int] = {}
    for fixture_row in in_window_rows():
        key = (floor_to_bucket(fixture_row.ts, "1d"), fixture_row.event_name)
        expected[key] = expected.get(key, 0) + 1
    assert actual == expected


def test_multi_aggregate_runs_one_scan_and_agrees_with_the_single_path(
    ch: ClickHouseAdapter,
) -> None:
    ch.get_columns(BASE)
    names, rows = ch.get_time_bucketed_multi_aggregate(
        BASE,
        "ts",
        "1d",
        [
            AggregateSpec(key="k_cnt", aggregation=MetricAggregation.count),
            AggregateSpec(key="k_sum", aggregation=MetricAggregation.sum, column="amount"),
            AggregateSpec(
                key="k_dist", aggregation=MetricAggregation.count_distinct, column="user_id"
            ),
        ],
        FROM_TIME,
        TO_TIME,
    )
    assert names == ["bucket", "k_cnt", "k_sum", "k_dist"]
    counts = {_utc(row[0]): int(row[1]) for row in rows}  # type: ignore[arg-type]
    assert counts == expected_bucket_counts("1d")


# --- nested JSON --------------------------------------------------------------


def test_nested_json_leaves_are_discovered_as_dotted_paths(ch: ClickHouseAdapter) -> None:
    ch.get_columns(BASE)
    samples = ch.get_json_path_samples(
        BASE,
        ["doc"],
        time_column="ts",
        time_from=FROM_TIME,
        time_to=TO_TIME,
        sample_limit=10,
    )
    paths = set(samples["doc"])
    # Full nested leaf paths, not top-level keys.
    assert "user.address.city" in paths
    # ClickHouse's JSON type does not materialize a null-valued dynamic subcolumn,
    # so JSONAllPaths cannot see a key whose only value is a JSON null — where
    # PostgreSQL's walk (and the local reference) do. That is a REAL divergence and
    # it is stated exactly, not smoothed over: null-only paths are the ONLY thing
    # ClickHouse may be missing. Any other missing path fails here, and so does
    # ClickHouse ever starting to report the null-only ones (tighten this then).
    assert paths == set(expected_json_leaf_paths()) - set(json_null_only_leaf_paths())
    assert json_null_only_leaf_paths(), "the fixture must actually contain a JSON null leaf"
    cities = {str(v) for v in samples["doc"]["user.address.city"]}
    assert cities == {'"Berlin"', '"Paris"', '"Tokyo"', '"Lisbon"', '"Oslo"'}


def test_json_paths_group_as_a_value_in_a_bucketed_scan(ch: ClickHouseAdapter) -> None:
    ch.get_columns(BASE)
    col_names, json_value_names, rows = ch.get_time_bucketed_counts(
        BASE,
        "ts",
        "1d",
        ["event_name"],
        ["doc"],
        {"doc": ["user.address.city"]},
        FROM_TIME,
        TO_TIME,
    )
    assert col_names == ["event_name", "doc"]
    assert json_value_names == ["doc.user.address.city"]
    assert sum(int(row[-1]) for row in rows) == len(IN_WINDOW_IDS)  # type: ignore[arg-type]


# --- field contracts / drift --------------------------------------------------


def test_field_contracts_find_exactly_the_drift_that_is_there(ch: ClickHouseAdapter) -> None:
    """ClickHouse validates contracts with a NATIVE aggregate query, not row sampling.

    This is the same assertion the PostgreSQL gate makes against BaseAdapter's Python
    fallback. Two unrelated implementations, one required answer — and the clean
    contract must yield no violation on either.
    """
    ch.get_columns(BASE)
    violations = ch.validate_field_contracts(
        BASE,
        contract_expectations(),
        time_column="ts",
        time_from=FROM_TIME,
        time_to=TO_TIME,
    )
    actual = {(v.field_name, v.drift_type): (v.bad_count, v.total_count) for v in violations}
    assert actual == expected_contract_violations()
    assert ("user_id", "regex_violation") not in actual


# --- the other two nested families: Map and Tuple ------------------------------


def test_map_and_tuple_columns_do_not_break_a_scan(ch: ClickHouseAdapter) -> None:
    """A Map and a Tuple column in the source must not kill the whole scan.

    Previously the adapter emitted ``JSONAllPaths`` for every column the
    caller classified as nested, and ClickHouse answers that with
    ``Code: 43 ... requires argument with type JSON`` for a Map or a Tuple. One such
    column anywhere in the query took down every scan and every metrics collection
    for the config. This is the assertion that would have caught it: on revert the
    server raises and the test ERRORS, where a fake-client SQL-string comparison
    would only have pinned the adapter's own opinion of the right function name.
    """
    ch.get_columns(BASE)
    reg, nested, _values, rows = ch.get_full_breakdown(
        BASE,
        ["event_name"],
        ["doc", "props", "tup"],
        None,
        time_column="ts",
        time_from=FROM_TIME,
        time_to=TO_TIME,
    )
    assert rows
    # The name lists come back unchanged: cardinality.py indexes row[n_reg + j]
    # off them, so a fix that reshuffled a column between buckets would break it.
    assert reg == ["event_name"]
    assert nested == ["doc", "props", "tup"]

    n_reg = len(reg)
    props_shapes = {tuple(row[n_reg + 1]) for row in rows}  # type: ignore[arg-type]
    tup_shapes = {tuple(row[n_reg + 2]) for row in rows}  # type: ignore[arg-type]
    # The Map reports the row's key set, sorted as strings.
    assert props_shapes == {("odd", "shared"), ("shared",)}
    # A Tuple's shape cannot vary: the declared field names, every row.
    assert tup_shapes == {("a", "b")}


def test_map_and_tuple_columns_survive_a_bucketed_scan(ch: ClickHouseAdapter) -> None:
    """Same guarantee on the bucketed path, which is what metrics collection runs."""
    ch.get_columns(BASE)
    col_names, _values, rows = ch.get_time_bucketed_counts(
        BASE, "ts", "1d", ["event_name"], ["doc", "props", "tup"], None, FROM_TIME, TO_TIME
    )
    assert rows
    assert col_names == ["event_name", "doc", "props", "tup"]


def test_json_path_discovery_ignores_map_and_tuple_columns(ch: ClickHouseAdapter) -> None:
    """Discovery is JSON-only and must not fail the JSON column it CAN enumerate.

    Both enumerators reject a Map/Tuple argument, so before the fix a single such
    column in the request failed discovery for every column in the same call.
    """
    ch.get_columns(BASE)
    samples = ch.get_json_path_samples(
        BASE, ["doc", "props", "tup"], time_column="ts", time_from=FROM_TIME, time_to=TO_TIME
    )
    assert samples["props"] == {}
    assert samples["tup"] == {}
    assert samples["doc"], "the JSON column must still be enumerated"
