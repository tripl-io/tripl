"""Credentialed value conformance against a real Amazon Redshift endpoint.

The mocked suites (``test_greenplum_redshift_adapters.py`` and the parity file)
pin the SQL ``RedshiftAdapter`` sends; this module is the only place that proves
Redshift accepts it and computes what the shared reference says. It reads a
table-less fixture (``redshift_live.BASE``), so nothing is created or left behind.
JSON is out of scope: Redshift sources do not support JSON columns.

Not part of the ordinary ``conformance`` CI job, which has no Redshift: it
carries the ``redshift_value`` marker that job excludes, and skips without
credentials. ``redshift-value-conformance.yml`` runs it on release tags when the
repository has an endpoint configured; run it by hand, one file:

    TRIPL_CONF_RS_HOST=... TRIPL_CONF_RS_USER=... TRIPL_CONF_RS_PASSWORD=... \\
    TRIPL_RS_VALUE_REQUIRED=1 uv run pytest -q -m redshift_value \\
        src/tripl/tests/conformance/test_redshift_value_conformance.py
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from tripl.core.adapters.base import AggregateSpec
from tripl.core.adapters.redshift import RedshiftAdapter
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
    in_window_rows,
)
from tripl.tests.conformance.redshift_live import BASE, new_adapter, unavailable

pytestmark = pytest.mark.redshift_value

_TIME_COLUMNS = ("ts", "ts_naive")


@pytest.fixture(scope="session")
def rs_real() -> Iterator[RedshiftAdapter]:
    adapter: RedshiftAdapter | None = None
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


def _utc(value: object) -> datetime:
    assert isinstance(value, datetime), type(value).__name__
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@pytest.mark.parametrize(
    ("time_column", "interval"),
    [(column, interval) for column in _TIME_COLUMNS for interval in INTERVALS],
)
def test_bucket_values_and_counts_match_the_reference(
    rs_real: RedshiftAdapter, time_column: str, interval: str
) -> None:
    _, _, rows = rs_real.get_time_bucketed_counts(
        BASE, time_column, interval, [], [], None, FROM_TIME, TO_TIME
    )
    actual = {_utc(row[0]): int(str(row[-1])) for row in rows}
    assert actual == expected_bucket_counts(interval)
    if interval == "1w":
        assert actual and all(bucket.weekday() == 0 for bucket in actual)


@pytest.mark.parametrize("time_column", _TIME_COLUMNS)
def test_windows_are_half_open(rs_real: RedshiftAdapter, time_column: str) -> None:
    _, _, _, rows = rs_real.get_full_breakdown(
        BASE, ["id"], [], None, time_column=time_column, time_from=FROM_TIME, time_to=TO_TIME
    )
    assert {int(str(row[0])) for row in rows} == set(IN_WINDOW_IDS)


def test_bucketed_sums_match_the_reference(rs_real: RedshiftAdapter) -> None:
    _, _, rows = rs_real.get_time_bucketed_aggregate(
        BASE, "ts", "1d", MetricAggregation.sum, "amount", [], [], None, FROM_TIME, TO_TIME
    )
    actual = {_utc(row[0]): None if row[-1] is None else float(str(row[-1])) for row in rows}
    assert actual == expected_bucket_sums("1d")


def test_breakdowns_and_conditional_aggregates_match_the_reference(
    rs_real: RedshiftAdapter,
) -> None:
    _, _, rows = rs_real.get_time_bucketed_breakdown_counts(
        BASE, "ts", "1d", "event_name", ["event_name"], [], None, FROM_TIME, TO_TIME
    )
    actual_breakdowns = {(_utc(row[0]), str(row[1])): int(str(row[-1])) for row in rows}
    expected_breakdowns: dict[tuple[datetime, str], int] = {}
    for fixture_row in in_window_rows():
        key = (floor_to_bucket(fixture_row.ts, "1d"), fixture_row.event_name)
        expected_breakdowns[key] = expected_breakdowns.get(key, 0) + 1
    assert actual_breakdowns == expected_breakdowns

    names, aggregate_rows = rs_real.get_time_bucketed_multi_aggregate(
        BASE,
        "ts",
        "1d",
        [
            AggregateSpec(key="count", aggregation=MetricAggregation.count),
            AggregateSpec(
                key="users", aggregation=MetricAggregation.count_distinct, column="user_id"
            ),
            # Folded into CASE (no FILTER on Redshift): a filter every row passes
            # equals the unfiltered count, and one no row passes is NULL, not 0.
            AggregateSpec(key="all", aggregation=MetricAggregation.count, filter_sql='"id" > 0'),
            AggregateSpec(
                key="none", aggregation=MetricAggregation.count, filter_sql='"amount" < 0'
            ),
        ],
        FROM_TIME,
        TO_TIME,
    )
    assert names == ["bucket", "count", "users", "all", "none"]
    counts = {_utc(row[0]): int(str(row[1])) for row in aggregate_rows}
    assert counts == expected_bucket_counts("1d")
    assert {_utc(row[0]): int(str(row[3])) for row in aggregate_rows} == counts
    expected_users: dict[datetime, set[str]] = {}
    for fixture_row in in_window_rows():
        expected_users.setdefault(floor_to_bucket(fixture_row.ts, "1d"), set()).add(
            fixture_row.user_id
        )
    assert {_utc(row[0]): int(str(row[2])) for row in aggregate_rows} == {
        bucket: len(users) for bucket, users in expected_users.items()
    }
    assert all(row[4] is None for row in aggregate_rows)


def test_top_n_keeps_the_most_frequent_value_and_folds_the_rest(
    rs_real: RedshiftAdapter,
) -> None:
    _, _, rows = rs_real.get_time_bucketed_breakdown_counts(
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


@pytest.mark.parametrize("time_column", _TIME_COLUMNS)
def test_field_contract_counts_match_the_reference(
    rs_real: RedshiftAdapter, time_column: str
) -> None:
    violations = rs_real.validate_field_contracts(
        BASE,
        contract_expectations(),
        time_column=time_column,
        time_from=FROM_TIME,
        time_to=TO_TIME,
    )
    actual = {
        (violation.field_name, violation.drift_type): (violation.bad_count, violation.total_count)
        for violation in violations
    }
    assert actual == expected_contract_violations()


def test_a_backslash_value_stays_inside_its_literal(rs_real: RedshiftAdapter) -> None:
    """The literal quoting is what keeps a value ending in a backslash from
    closing the string early: the server must read it back unchanged."""
    values = ("a\\", "b'\\")
    literals = ", ".join(rs_real._quote_string(value) for value in values)  # noqa: SLF001
    with rs_real._conn.cursor() as cur:  # noqa: SLF001 - one literal, no fixture table
        cur.execute(f"SELECT {literals}")
        assert cur.fetchone() == values
