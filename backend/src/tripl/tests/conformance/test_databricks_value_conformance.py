"""Credentialed value conformance against a real Databricks SQL warehouse.

The mocked suites (``test_databricks_adapter.py`` and the parity files) pin the
SQL the adapter sends; this module is the only place that proves Databricks
accepts it and computes what the shared reference says. It reads a table-less
fixture (``databricks_live.BASE``), so nothing is created or left behind.

Not part of the ordinary ``conformance`` CI job, which has no Databricks: it
carries the ``databricks_value`` marker that job excludes, and skips without
credentials. Run it by hand, one file:

    TRIPL_CONF_DBX_HOST=... TRIPL_CONF_DBX_HTTP_PATH=... TRIPL_CONF_DBX_TOKEN=... \\
    TRIPL_DBX_VALUE_REQUIRED=1 \\
    uv run pytest -q -m databricks_value \\
        src/tripl/tests/conformance/test_databricks_value_conformance.py
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest

from tripl.core.adapters.base import AggregateSpec
from tripl.core.adapters.databricks import DatabricksAdapter
from tripl.core.bucketing import floor_to_bucket
from tripl.models.domain_enums import MetricAggregation
from tripl.tests.conformance.databricks_live import BASE, new_adapter, unavailable
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

pytestmark = pytest.mark.databricks_value

# TIMESTAMP, TIMESTAMP_NTZ and DATE: a DATE column refuses sub-day intervals.
_TIME_INTERVALS = {
    "ts": INTERVALS,
    "ntz": INTERVALS,
    "d": ("1d", "1w"),
}

_CITIES = {'"Berlin"', '"Paris"', '"Tokyo"', '"Lisbon"', '"Oslo"'}


@pytest.fixture(scope="session")
def dbx_real() -> Iterator[DatabricksAdapter]:
    adapter: DatabricksAdapter | None = None
    try:
        try:
            adapter = new_adapter()
            adapter.test_connection()
            adapter.get_columns(BASE)
        except Exception as exc:  # noqa: BLE001 - any auth or network failure is unavailable
            unavailable(str(exc))
        yield adapter
    finally:
        if adapter is not None:
            adapter.close()


def _utc_bucket(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    raise AssertionError(f"unexpected Databricks bucket type: {type(value).__name__}")


def test_the_fixture_is_typed_as_the_adapter_expects(dbx_real: DatabricksAdapter) -> None:
    types = {column.name: column.type_name.lower() for column in dbx_real.get_columns(BASE)}
    assert types["ts"] == "timestamp"
    assert types["ntz"] == "timestamp_ntz"
    assert types["d"] == "date"
    assert types["doc"] == "variant"
    assert types["props"].startswith("struct<")


@pytest.mark.parametrize(
    ("time_column", "interval"),
    [
        (time_column, interval)
        for time_column, intervals in _TIME_INTERVALS.items()
        for interval in intervals
    ],
)
def test_bucket_values_and_counts_match_the_reference(
    dbx_real: DatabricksAdapter, time_column: str, interval: str
) -> None:
    _, _, rows = dbx_real.get_time_bucketed_counts(
        BASE, time_column, interval, [], [], None, FROM_TIME, TO_TIME
    )
    actual = {_utc_bucket(row[0]): int(str(row[-1])) for row in rows}
    assert actual == expected_bucket_counts(interval)
    if interval == "1w":
        assert actual and all(bucket.weekday() == 0 for bucket in actual)


@pytest.mark.parametrize("time_column", _TIME_INTERVALS)
def test_windows_are_half_open_for_every_time_type(
    dbx_real: DatabricksAdapter, time_column: str
) -> None:
    _, _, _, rows = dbx_real.get_full_breakdown(
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


@pytest.mark.parametrize("time_column", _TIME_INTERVALS)
def test_bucketed_sums_match_the_reference(dbx_real: DatabricksAdapter, time_column: str) -> None:
    _, _, rows = dbx_real.get_time_bucketed_aggregate(
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


def test_breakdowns_and_multi_aggregates_match_the_reference(
    dbx_real: DatabricksAdapter,
) -> None:
    _, _, rows = dbx_real.get_time_bucketed_breakdown_counts(
        BASE, "ts", "1d", "event_name", ["event_name"], [], None, FROM_TIME, TO_TIME
    )
    actual_breakdowns = {(_utc_bucket(row[0]), str(row[1])): int(str(row[-1])) for row in rows}
    expected_breakdowns: dict[tuple[datetime, str], int] = {}
    for fixture_row in in_window_rows():
        key = (floor_to_bucket(fixture_row.ts, "1d"), fixture_row.event_name)
        expected_breakdowns[key] = expected_breakdowns.get(key, 0) + 1
    assert actual_breakdowns == expected_breakdowns

    names, aggregate_rows = dbx_real.get_time_bucketed_multi_aggregate(
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
            AggregateSpec(key="none", aggregation=MetricAggregation.count, filter_sql="amount < 0"),
        ],
        FROM_TIME,
        TO_TIME,
    )
    assert names == ["bucket", "count", "sum", "users", "none"]
    assert {_utc_bucket(row[0]): int(str(row[1])) for row in aggregate_rows} == (
        expected_bucket_counts("1d")
    )
    assert {
        _utc_bucket(row[0]): None if row[2] is None else float(str(row[2]))
        for row in aggregate_rows
    } == expected_bucket_sums("1d")
    expected_users: dict[datetime, set[str]] = {}
    for fixture_row in in_window_rows():
        bucket = floor_to_bucket(fixture_row.ts, "1d")
        expected_users.setdefault(bucket, set()).add(fixture_row.user_id)
    assert {_utc_bucket(row[0]): int(str(row[3])) for row in aggregate_rows} == {
        bucket: len(users) for bucket, users in expected_users.items()
    }
    assert all(row[4] is None for row in aggregate_rows)


def test_top_n_keeps_the_most_frequent_value_and_folds_the_rest(
    dbx_real: DatabricksAdapter,
) -> None:
    # click 3, view 2, buy 2 in the window: values_limit=2 keeps one value.
    _, _, rows = dbx_real.get_time_bucketed_breakdown_counts(
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


def test_variant_and_struct_values_execute(dbx_real: DatabricksAdapter) -> None:
    samples = dbx_real.get_json_path_samples(
        BASE,
        ["doc"],
        time_column="ts",
        time_from=FROM_TIME,
        time_to=TO_TIME,
        sample_limit=10,
    )
    assert set(samples["doc"]) == set(expected_json_leaf_paths())

    regular, complex_columns, value_names, rows = dbx_real.get_full_breakdown(
        BASE,
        ["event_name"],
        ["doc", "props"],
        {"doc": ["user.address.city"], "props": ["address.city"]},
        time_column="ts",
        time_from=FROM_TIME,
        time_to=TO_TIME,
    )
    assert regular == ["event_name"]
    assert complex_columns == ["doc", "props"]
    assert value_names == ["doc.user.address.city", "props.address.city"]
    assert sum(int(str(row[-1])) for row in rows) == len(IN_WINDOW_IDS)
    # VARIANT shapes are top-level keys; the STRUCT's are its declared paths.
    assert all(row[1] is None or isinstance(row[1], list) for row in rows)
    assert all(isinstance(row[2], list) for row in rows)
    assert {row[3] for row in rows} - {None, "null"} == _CITIES
    assert {row[4] for row in rows} - {None, "null"} == _CITIES


def test_a_text_column_parses_as_json(dbx_real: DatabricksAdapter) -> None:
    source = dbx_real.json_string_source(BASE, ["doc_text"])
    samples = dbx_real.get_json_path_samples(
        source,
        ["doc_text"],
        time_column="ts",
        time_from=FROM_TIME,
        time_to=TO_TIME,
        sample_limit=10,
    )
    assert set(samples["doc_text"]) == set(expected_json_leaf_paths())


@pytest.mark.parametrize("time_column", _TIME_INTERVALS)
def test_field_contract_counts_match_the_reference(
    dbx_real: DatabricksAdapter, time_column: str
) -> None:
    violations = dbx_real.validate_field_contracts(
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


def test_a_python_named_group_is_refused_by_java_regex(dbx_real: DatabricksAdapter) -> None:
    assert dbx_real.contract_regex_is_compilable("^u[0-9]+$") is True
    assert dbx_real.contract_regex_is_compilable("(?P<sku>x)") is False
