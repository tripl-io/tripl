"""The value assertions shared by the Trino and Athena conformance modules.

Athena engine version 3 is Trino SQL, so one set of assertions covers both: each
``test_*_value_conformance`` module star-imports these tests and supplies an
``engine`` fixture (a live :class:`TrinoAdapter` or :class:`AthenaAdapter`). The
expectations all come from :mod:`tripl.tests.conformance.dataset`, i.e. from
``floor_to_bucket`` and the Python contract reference.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from tripl.core.adapters.base import AggregateSpec, FieldContractExpectation
from tripl.core.adapters.trino import TrinoAdapter
from tripl.core.bucketing import floor_to_bucket
from tripl.core.warehouse_types import TimeKind, classify_complex, classify_time
from tripl.models.domain_enums import MetricAggregation
from tripl.tests.conformance.dataset import (
    FROM_TIME,
    IN_WINDOW_IDS,
    INTERVALS,
    TO_TIME,
    contract_expectations,
    expected_bucket_counts,
    expected_bucket_sums,
    expected_contract_violations,
    expected_json_leaf_paths,
    in_window_rows,
)
from tripl.tests.conformance.trino_live import BASE

# timestamp(6) with time zone, timestamp(3) with time zone, timestamp(6) and DATE:
# a DATE refuses sub-day intervals.
TIME_INTERVALS = {
    "ts": INTERVALS,
    "ts_ms": INTERVALS,
    "naive": INTERVALS,
    "d": ("1d", "1w"),
}

_CITIES = {'"Berlin"', '"Paris"', '"Tokyo"', '"Lisbon"', '"Oslo"'}


def _utc_bucket(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    raise AssertionError(f"unexpected bucket type: {type(value).__name__}")


def test_the_fixture_is_typed_as_the_adapter_expects(engine: TrinoAdapter) -> None:
    types = {column.name: column.type_name.lower() for column in engine.get_columns(BASE)}
    assert types["ts"].endswith("with time zone")
    assert types["ts_ms"].endswith("with time zone")
    assert not types["naive"].endswith("with time zone")
    assert classify_time(types["naive"]) is TimeKind.timestamp
    assert classify_time(types["d"]) is TimeKind.date
    assert classify_complex(types["doc"]) is not None
    assert types["doc_text"].startswith("varchar")


@pytest.mark.parametrize(
    ("time_column", "interval"),
    [
        (time_column, interval)
        for time_column, intervals in TIME_INTERVALS.items()
        for interval in intervals
    ],
)
def test_bucket_values_and_counts_match_the_reference(
    engine: TrinoAdapter, time_column: str, interval: str
) -> None:
    _, _, rows = engine.get_time_bucketed_counts(
        BASE, time_column, interval, [], [], None, FROM_TIME, TO_TIME
    )
    actual = {_utc_bucket(row[0]): int(str(row[-1])) for row in rows}
    assert actual == expected_bucket_counts(interval)
    if interval == "1w":
        assert actual and all(bucket.weekday() == 0 for bucket in actual)


@pytest.mark.parametrize("time_column", TIME_INTERVALS)
def test_windows_are_half_open_for_every_time_type(engine: TrinoAdapter, time_column: str) -> None:
    _, _, _, rows = engine.get_full_breakdown(
        BASE,
        ["id"],
        [],
        None,
        time_column=time_column,
        time_from=FROM_TIME,
        time_to=TO_TIME,
    )
    ids = {int(str(row[0])) for row in rows}
    # The window's bounds are midnights, so a DATE column selects the same rows.
    assert ids == set(IN_WINDOW_IDS)
    assert 1 in ids
    assert 8 not in ids
    assert 9 not in ids


@pytest.mark.parametrize("time_column", TIME_INTERVALS)
def test_bucketed_sums_match_the_reference(engine: TrinoAdapter, time_column: str) -> None:
    _, _, rows = engine.get_time_bucketed_aggregate(
        BASE,
        time_column,
        "1d",
        MetricAggregation.sum,
        "amount",
        [],
        [],
        None,
        FROM_TIME,
        TO_TIME,
    )
    actual = {_utc_bucket(row[0]): None if row[-1] is None else float(str(row[-1])) for row in rows}
    assert actual == expected_bucket_sums("1d")


def test_breakdowns_and_multi_aggregates_match_the_reference(engine: TrinoAdapter) -> None:
    _, _, rows = engine.get_time_bucketed_breakdown_counts(
        BASE, "ts", "1d", "event_name", ["event_name"], [], None, FROM_TIME, TO_TIME
    )
    actual_breakdowns = {(_utc_bucket(row[0]), str(row[1])): int(str(row[-1])) for row in rows}
    expected_breakdowns: dict[tuple[datetime, str], int] = {}
    for fixture_row in in_window_rows():
        key = (floor_to_bucket(fixture_row.ts, "1d"), fixture_row.event_name)
        expected_breakdowns[key] = expected_breakdowns.get(key, 0) + 1
    assert actual_breakdowns == expected_breakdowns

    names, aggregate_rows = engine.get_time_bucketed_multi_aggregate(
        BASE,
        "ts",
        "1d",
        [
            AggregateSpec(key="count", aggregation=MetricAggregation.count),
            AggregateSpec(key="sum", aggregation=MetricAggregation.sum, column="amount"),
            AggregateSpec(
                key="users", aggregation=MetricAggregation.count_distinct, column="user_id"
            ),
            # A filter no row passes: every bucket's count must be NULL, not 0.
            AggregateSpec(
                key="none", aggregation=MetricAggregation.count, filter_sql='"amount" < 0'
            ),
            AggregateSpec(
                key="none_users",
                aggregation=MetricAggregation.count_distinct,
                column="user_id",
                filter_sql='"amount" < 0',
            ),
            AggregateSpec(
                key="clicks",
                aggregation=MetricAggregation.count,
                filter_sql="\"event_name\" = 'click'",
            ),
        ],
        FROM_TIME,
        TO_TIME,
    )
    assert names == ["bucket", "count", "sum", "users", "none", "none_users", "clicks"]
    assert {_utc_bucket(row[0]): int(str(row[1])) for row in aggregate_rows} == (
        expected_bucket_counts("1d")
    )
    assert {
        _utc_bucket(row[0]): None if row[2] is None else float(str(row[2]))
        for row in aggregate_rows
    } == expected_bucket_sums("1d")
    expected_users: dict[datetime, set[str]] = {}
    expected_clicks: dict[datetime, int] = {}
    for fixture_row in in_window_rows():
        bucket = floor_to_bucket(fixture_row.ts, "1d")
        expected_users.setdefault(bucket, set()).add(fixture_row.user_id)
        if fixture_row.event_name == "click":
            expected_clicks[bucket] = expected_clicks.get(bucket, 0) + 1
    assert {_utc_bucket(row[0]): int(str(row[3])) for row in aggregate_rows} == {
        bucket: len(users) for bucket, users in expected_users.items()
    }
    assert all(row[4] is None for row in aggregate_rows)
    assert all(row[5] is None for row in aggregate_rows)
    # A bucket with no click is a gap (NULL), never 0.
    assert {
        _utc_bucket(row[0]): int(str(row[6])) for row in aggregate_rows if row[6] is not None
    } == expected_clicks


def test_aggregate_breakdown_folds_into_other(engine: TrinoAdapter) -> None:
    _, _, rows = engine.get_time_bucketed_aggregate_breakdown(
        BASE,
        "naive",
        "1w",
        MetricAggregation.count,
        None,
        "event_name",
        ["event_name"],
        [],
        None,
        FROM_TIME,
        TO_TIME,
        values_limit=2,
    )
    # (bucket, breakdown_value, is_other, event_name, value)
    assert {str(row[1]) for row in rows} == {"click", "Other"}
    assert all(row[3] == row[1] for row in rows)
    assert {int(str(row[2])) for row in rows if row[1] == "Other"} == {1}
    assert sum(int(str(row[-1])) for row in rows) == len(IN_WINDOW_IDS)

    names, multi_rows = engine.get_time_bucketed_multi_aggregate_breakdown(
        BASE,
        "ts",
        "1w",
        "event_name",
        [AggregateSpec(key="amount", aggregation=MetricAggregation.sum, column="amount")],
        FROM_TIME,
        TO_TIME,
        values_limit=2,
    )
    assert names == ["bucket", "breakdown_value", "is_other", "amount"]
    assert {str(row[1]) for row in multi_rows} == {"click", "Other"}


def test_top_n_keeps_the_most_frequent_value_and_folds_the_rest(engine: TrinoAdapter) -> None:
    # click 3, view 2, buy 2 in the window: values_limit=2 keeps one value.
    _, _, rows = engine.get_time_bucketed_breakdown_counts(
        BASE,
        "ts",
        "1w",
        "event_name",
        ["event_name"],
        [],
        None,
        FROM_TIME,
        TO_TIME,
        values_limit=2,
    )
    assert {str(row[1]) for row in rows} == {"click", "Other"}
    assert sum(int(str(row[-1])) for row in rows) == len(IN_WINDOW_IDS)


def test_json_values_execute(engine: TrinoAdapter) -> None:
    samples = engine.get_json_path_samples(
        BASE,
        ["doc"],
        time_column="ts",
        time_from=FROM_TIME,
        time_to=TO_TIME,
        sample_limit=10,
    )
    assert set(samples["doc"]) == set(expected_json_leaf_paths())

    regular, complex_columns, value_names, rows = engine.get_full_breakdown(
        BASE,
        ["event_name"],
        ["doc"],
        {"doc": ["user.address.city"]},
        time_column="ts",
        time_from=FROM_TIME,
        time_to=TO_TIME,
    )
    assert regular == ["event_name"]
    assert complex_columns == ["doc"]
    assert value_names == ["doc.user.address.city"]
    assert sum(int(str(row[-1])) for row in rows) == len(IN_WINDOW_IDS)
    # JSON shapes are the sorted TOP-LEVEL keys of each document.
    expected_shapes = {tuple(sorted(row.doc)) for row in in_window_rows()}
    assert {tuple(row[1]) for row in rows} == expected_shapes
    assert {row[2] for row in rows} - {None, "null"} == _CITIES


def test_property_breakdowns_extract_the_scalar(engine: TrinoAdapter) -> None:
    _, _, rows = engine.get_time_bucketed_breakdown_counts_multi(
        BASE,
        "ts",
        "1w",
        ["doc.user.address.city"],
        [],
        ["doc"],
        None,
        FROM_TIME,
        TO_TIME,
    )
    # (bucket, column, value, is_other, doc shape, count)
    cities = {str(row[2]) for row in rows}
    assert cities - {""} == {city.strip('"') for city in _CITIES}


def test_a_text_column_parses_as_json(engine: TrinoAdapter) -> None:
    source = engine.json_string_source(BASE, ["doc_text"])
    samples = engine.get_json_path_samples(
        source,
        ["doc_text"],
        time_column="ts",
        time_from=FROM_TIME,
        time_to=TO_TIME,
        sample_limit=10,
    )
    assert set(samples["doc_text"]) == set(expected_json_leaf_paths())


@pytest.mark.parametrize("time_column", TIME_INTERVALS)
def test_field_contract_counts_match_the_reference(engine: TrinoAdapter, time_column: str) -> None:
    violations = engine.validate_field_contracts(
        BASE,
        contract_expectations(),
        time_column=time_column,
        time_from=FROM_TIME,
        time_to=TO_TIME,
    )
    actual = {
        (violation.field_name, violation.drift_type): (
            violation.bad_count,
            violation.total_count,
        )
        for violation in violations
    }
    assert actual == expected_contract_violations()
    assert ("user_id", "regex_violation") not in actual


def test_python_named_groups_are_refused_by_joni(engine: TrinoAdapter) -> None:
    assert engine.contract_regex_is_compilable("^u[0-9]+$") is True
    # Trino's joni takes lookahead, which RE2 engines refuse ...
    assert engine.contract_regex_is_compilable("^(?!test_)") is True
    # ... and refuses Python's named-group spelling.
    assert engine.contract_regex_is_compilable("(?P<sku>x)") is False


def test_values_with_quotes_reach_the_engine_as_data(engine: TrinoAdapter) -> None:
    hostile = "it's'); DROP TABLE x; --"
    escaped = hostile.replace("'", "''")
    source = f"SELECT TIMESTAMP '2026-04-02 00:00:00 UTC' AS ts, '{escaped}' AS v"
    # One adapter reads one base query's columns; this test reads another.
    engine.get_columns(source)
    expectation = FieldContractExpectation(
        field_name="v", drift_type="enum_violation", threshold=0.0, enum_options=(hostile,)
    )
    # The group filter and the enum option both carry the quote: the row matches
    # the group and is in the enum, so nothing is a violation and nothing failed.
    assert (
        engine.validate_field_contracts(
            source, [expectation], group_column="v", group_value=hostile
        )
        == []
    )
    wrong = FieldContractExpectation(
        field_name="v", drift_type="enum_violation", threshold=0.0, enum_options=("x",)
    )
    [violation] = engine.validate_field_contracts(
        source, [wrong], group_column="v", group_value=hostile
    )
    assert (violation.bad_count, violation.total_count, violation.sample_value) == (1, 1, hostile)
    engine.get_columns(BASE)
