"""Credentialed value conformance against a real Snowflake account.

The mocked suites (``test_snowflake_adapter.py`` and the parity files) pin the
SQL the adapter sends; this module is the only place that proves Snowflake
accepts it and computes what the shared reference says. It reads a table-less
fixture (``snowflake_live.BASE``), so nothing is created or left behind.

Not part of the ordinary ``conformance`` CI job, which has no Snowflake: it
carries the ``snowflake_value`` marker that job excludes, and skips without
credentials. ``snowflake-value-conformance.yml`` runs it on release tags when
the repository has an account configured; run it by hand, one file:

    TRIPL_CONF_SF_ACCOUNT=... TRIPL_CONF_SF_USER=... TRIPL_CONF_SF_WAREHOUSE=... \\
    TRIPL_CONF_SF_PRIVATE_KEY="$(cat rsa_key.p8)" TRIPL_SF_VALUE_REQUIRED=1 \\
    uv run pytest -q -m snowflake_value \\
        src/tripl/tests/conformance/test_snowflake_value_conformance.py
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest

from tripl.core.adapters.base import AggregateSpec
from tripl.core.adapters.snowflake import SnowflakeAdapter
from tripl.core.bucketing import floor_to_bucket
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
from tripl.tests.conformance.snowflake_live import BASE, new_adapter, unavailable

pytestmark = pytest.mark.snowflake_value

# TIMESTAMP_TZ, TIMESTAMP_NTZ, TIMESTAMP_LTZ and DATE: a DATE refuses sub-day intervals.
_TIME_INTERVALS = {
    "ts": INTERVALS,
    "ntz": INTERVALS,
    "ltz": INTERVALS,
    "d": ("1d", "1w"),
}

_CITIES = {'"Berlin"', '"Paris"', '"Tokyo"', '"Lisbon"', '"Oslo"'}


@pytest.fixture(scope="session")
def sf_real() -> Iterator[SnowflakeAdapter]:
    adapter: SnowflakeAdapter | None = None
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
    raise AssertionError(f"unexpected Snowflake bucket type: {type(value).__name__}")


def test_the_fixture_is_typed_as_the_adapter_expects(sf_real: SnowflakeAdapter) -> None:
    types = {column.name: column.type_name for column in sf_real.get_columns(BASE)}
    assert types["ts"] == "TIMESTAMP_TZ"
    assert types["ntz"] == "TIMESTAMP_NTZ"
    assert types["ltz"] == "TIMESTAMP_LTZ"
    assert types["d"] == "DATE"
    assert types["doc"] == "VARIANT"
    assert types["doc_text"] == "STRING"


@pytest.mark.parametrize(
    ("time_column", "interval"),
    [
        (time_column, interval)
        for time_column, intervals in _TIME_INTERVALS.items()
        for interval in intervals
    ],
)
def test_bucket_values_and_counts_match_the_reference(
    sf_real: SnowflakeAdapter, time_column: str, interval: str
) -> None:
    _, _, rows = sf_real.get_time_bucketed_counts(
        BASE, time_column, interval, [], [], None, FROM_TIME, TO_TIME
    )
    actual = {_utc_bucket(row[0]): int(str(row[-1])) for row in rows}
    assert actual == expected_bucket_counts(interval)
    if interval == "1w":
        assert actual and all(bucket.weekday() == 0 for bucket in actual)


@pytest.mark.parametrize("time_column", _TIME_INTERVALS)
def test_windows_are_half_open_for_every_time_type(
    sf_real: SnowflakeAdapter, time_column: str
) -> None:
    _, _, _, rows = sf_real.get_full_breakdown(
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
def test_bucketed_sums_match_the_reference(sf_real: SnowflakeAdapter, time_column: str) -> None:
    _, _, rows = sf_real.get_time_bucketed_aggregate(
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


def test_breakdowns_and_multi_aggregates_match_the_reference(sf_real: SnowflakeAdapter) -> None:
    _, _, rows = sf_real.get_time_bucketed_breakdown_counts(
        BASE, "ts", "1d", "event_name", ["event_name"], [], None, FROM_TIME, TO_TIME
    )
    actual_breakdowns = {(_utc_bucket(row[0]), str(row[1])): int(str(row[-1])) for row in rows}
    expected_breakdowns: dict[tuple[datetime, str], int] = {}
    for fixture_row in in_window_rows():
        key = (floor_to_bucket(fixture_row.ts, "1d"), fixture_row.event_name)
        expected_breakdowns[key] = expected_breakdowns.get(key, 0) + 1
    assert actual_breakdowns == expected_breakdowns

    names, aggregate_rows = sf_real.get_time_bucketed_multi_aggregate(
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
    sf_real: SnowflakeAdapter,
) -> None:
    # click 3, view 2, buy 2 in the window: values_limit=2 keeps one value.
    _, _, rows = sf_real.get_time_bucketed_breakdown_counts(
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


def test_variant_values_execute(sf_real: SnowflakeAdapter) -> None:
    samples = sf_real.get_json_path_samples(
        BASE,
        ["doc"],
        time_column="ts",
        time_from=FROM_TIME,
        time_to=TO_TIME,
        sample_limit=10,
    )
    assert set(samples["doc"]) == set(expected_json_leaf_paths())

    regular, complex_columns, value_names, rows = sf_real.get_full_breakdown(
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
    # VARIANT shapes are the sorted top-level keys.
    assert all(isinstance(row[1], list) and row[1] == sorted(row[1]) for row in rows)
    assert {row[2] for row in rows} - {None, "null"} == _CITIES


def test_a_text_column_parses_as_json(sf_real: SnowflakeAdapter) -> None:
    source = sf_real.json_string_source(BASE, ["doc_text"])
    samples = sf_real.get_json_path_samples(
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
    sf_real: SnowflakeAdapter, time_column: str
) -> None:
    violations = sf_real.validate_field_contracts(
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


def test_a_lookahead_is_refused_by_posix_regex(sf_real: SnowflakeAdapter) -> None:
    assert sf_real.contract_regex_is_compilable("^u[0-9]+$") is True
    assert sf_real.contract_regex_is_compilable("^(?!test_)") is False
