"""Contract tests for the local synthetic warehouse adapter.

The synthetic adapter serves a bounded, deterministic in-memory dataset and must
honour the same return shapes as the real warehouse adapters (ClickHouse /
Postgres / BigQuery) so the normal preview / scan / collect paths run unchanged —
with NO network or filesystem access.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tripl.core.adapters.base import AggregateSpec
from tripl.core.adapters.synthetic import (
    SYNTHETIC_MAX_ROWS,
    SyntheticAdapter,
    SyntheticCapabilityError,
)
from tripl.models.domain_enums import MetricAggregation as MA

ANCHOR = datetime(2026, 6, 1, tzinfo=UTC)
HISTORY_DAYS = 30
SEED = 7

# A window comfortably wider than the whole dataset.
FULL_FROM = ANCHOR - timedelta(days=HISTORY_DAYS + 5)
FULL_TO = ANCHOR + timedelta(days=2)


def _adapter(seed: int = SEED) -> SyntheticAdapter:
    return SyntheticAdapter(seed=seed, anchor=ANCHOR, history_days=HISTORY_DAYS)


def _day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


# --------------------------------------------------------------------------- #
# schema / columns / preview
# --------------------------------------------------------------------------- #


def test_schema_tables_lists_events_and_orders() -> None:
    tables = {t.name: [c.name for c in t.columns] for t in _adapter().get_schema_tables()}
    assert set(tables) == {"events", "orders"}
    assert tables["events"][0] == "event_time"
    assert "session_id" in tables["events"]
    assert tables["orders"] == [
        "created_at",
        "amount",
        "currency",
        "user_id",
        "country",
        "status",
    ]


def test_get_columns_selects_table_from_query() -> None:
    adapter = _adapter()
    orders_cols = [c.name for c in adapter.get_columns("SELECT * FROM orders")]
    assert orders_cols == ["created_at", "amount", "currency", "user_id", "country", "status"]

    event_cols = [c.name for c in adapter.get_columns("SELECT * FROM events")]
    assert "session_id" in event_cols and "platform" in event_cols

    # Column validation is keyed on the QUERY's table, and this adapter keeps no
    # memory of the last introspection: `session_id` exists on `events`, was just
    # returned by the call above, and is still refused for an `orders` query on the
    # same instance. This replaces an assertion on a `_allowed_columns` attribute
    # that `get_columns` wrote and nothing read — its comment claimed the allowlist
    # fed "downstream measure validation", which `_validate_column` never consulted.
    with pytest.raises(ValueError, match="not found in orders query result"):
        adapter.get_preview_rows("SELECT * FROM orders", limit=1, time_column="session_id")


def test_preview_rows_returns_scan_rows_within_window() -> None:
    adapter = _adapter()
    cols, rows = adapter.get_preview_rows(
        "SELECT * FROM events",
        limit=5,
        time_column="event_time",
        time_from=ANCHOR - timedelta(days=3),
        time_to=ANCHOR,
    )
    assert cols[0] == "event_time"
    assert len(rows) == 5  # honours the limit
    time_idx = cols.index("event_time")
    for row in rows:
        assert ANCHOR - timedelta(days=3) <= row[time_idx] < ANCHOR


def test_preview_time_window_is_a_strict_subset() -> None:
    adapter = _adapter()
    _, wide = adapter.get_preview_rows(
        "SELECT * FROM events",
        limit=100000,
        time_column="event_time",
        time_from=FULL_FROM,
        time_to=FULL_TO,
    )
    _, narrow = adapter.get_preview_rows(
        "SELECT * FROM events",
        limit=100000,
        time_column="event_time",
        time_from=ANCHOR - timedelta(days=2),
        time_to=ANCHOR,
    )
    assert 0 < len(narrow) < len(wide)


# --------------------------------------------------------------------------- #
# sql-metric support + capability errors
# --------------------------------------------------------------------------- #

_ACTIVE_SESSIONS_SQL = (
    "SELECT toStartOfDay(event_time) AS ts, count(DISTINCT session_id) AS value FROM events"
)


def test_active_sessions_sql_metric_computes_distinct_sessions_per_day() -> None:
    adapter = _adapter()
    cols, rows = adapter.get_preview_rows(
        _ACTIVE_SESSIONS_SQL,
        limit=1000,
        time_column="ts",
        time_from=ANCHOR - timedelta(days=7),
        time_to=ANCHOR,
    )
    assert cols == ["ts", "value"]
    assert len(rows) == 7  # one row per day in the 7-day window
    for ts, value in rows:
        assert ts == _day(ts)  # day-aligned buckets
        assert ANCHOR - timedelta(days=7) <= ts < ANCHOR
        assert isinstance(value, int) and value > 0

    # Independently verify the newest day's distinct-session count.
    newest = max(ts for ts, _ in rows)
    expected = len({r["session_id"] for r in adapter._events if _day(r["event_time"]) == newest})
    assert dict(rows)[newest] == expected


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) AS value, ts FROM events GROUP BY ts",
        "SELECT a.id FROM events a JOIN orders b ON a.user_id = b.user_id",
        "SELECT sum(amount) AS value, day AS ts FROM orders GROUP BY day",
        "SELECT DISTINCT platform FROM events",
    ],
)
def test_unsupported_sql_raises_capability_error(sql: str) -> None:
    # Unsupported SQL must raise — never fabricate a fallback result.
    with pytest.raises(SyntheticCapabilityError):
        _adapter().get_preview_rows(sql, time_column="ts")


# --------------------------------------------------------------------------- #
# time-bucketed counts + breakdowns
# --------------------------------------------------------------------------- #


def test_time_bucketed_counts_sum_to_windowed_rows() -> None:
    adapter = _adapter()
    tf, tt = ANCHOR - timedelta(days=3), ANCHOR
    col_names, json_names, rows = adapter.get_time_bucketed_counts(
        "SELECT * FROM events", "event_time", "1d", ["event_type"], [], None, tf, tt
    )
    assert col_names == ["event_type"]
    assert json_names == []
    # Row layout: (_bucket, event_type, count). Counts sum to the events in window.
    expected = sum(1 for r in adapter._events if tf <= r["event_time"] < tt)
    assert sum(row[-1] for row in rows) == expected
    for bucket, _et, count in rows:
        assert bucket == _day(bucket) and count > 0


def test_anchor_advances_to_current_hour_so_live_window_is_covered() -> None:
    """A live scan's current window must never read 0 events.

    The adapter floors its anchor to the current HOUR, so the newest event bucket
    is the last COMPLETE hour — exactly the newest bucket a live scan evaluates
    (its window ends at the half-open ``floor(now, interval)``). Flooring the
    anchor to the start of the *day* instead left every hour of "today" empty, so
    a scan after midnight read 0 for every series and the detector stamped a
    z=-20 drop-to-zero flood. A mid-day, mid-hour anchor makes the two flooring
    strategies diverge, so this pins the fix: the last complete hour carries rows.
    """
    anchor = datetime(2026, 6, 1, 14, 37, 11, tzinfo=UTC)
    adapter = SyntheticAdapter(seed=SEED, anchor=anchor, history_days=HISTORY_DAYS)
    interval = timedelta(hours=1)
    # What _resolve_collection_window computes for a live (non-manual) scan:
    # time_to = floor(now, interval); the window is half-open [time_from, time_to).
    time_to = anchor.replace(minute=0, second=0, microsecond=0)
    last_complete = time_to - interval
    time_from = time_to - timedelta(hours=6)
    _cols, _json, rows = adapter.get_time_bucketed_counts(
        "SELECT * FROM events", "event_time", "1h", [], [], None, time_from, time_to
    )
    counts_by_bucket = {bucket: count for bucket, count in rows}
    # Under the old day-flooring the newest event bucket was yesterday 23:00, so
    # every hour of "today" (including last_complete) was absent -> 0 events.
    assert last_complete in counts_by_bucket, "last complete hour missing: data did not reach 'now'"
    assert counts_by_bucket[last_complete] > 0


def test_bucketed_breakdown_counts_topn_folds_other() -> None:
    adapter = _adapter()
    tf, tt = ANCHOR - timedelta(days=5), ANCHOR
    _cols, _json, rows = adapter.get_time_bucketed_breakdown_counts(
        "SELECT * FROM events",
        "event_time",
        "1d",
        "platform",
        [],
        [],
        None,
        tf,
        tt,
        values_limit=2,
    )
    # Row layout: (_bucket, breakdown_value, is_other, count).
    non_other = {r[1] for r in rows if r[2] == 0}
    assert len(non_other) <= 1  # top (values_limit - 1) kept
    assert all(r[2] in (0, 1) for r in rows)
    assert any(r[2] == 1 for r in rows)  # platform has 3 values -> some fold to Other
    assert all(r[1] == "Other" for r in rows if r[2] == 1)


def test_bucketed_breakdown_counts_multi_covers_each_column() -> None:
    adapter = _adapter()
    tf, tt = ANCHOR - timedelta(days=3), ANCHOR
    _cols, _json, rows = adapter.get_time_bucketed_breakdown_counts_multi(
        "SELECT * FROM events",
        "event_time",
        "1d",
        ["platform", "event_type"],
        [],
        [],
        None,
        tf,
        tt,
    )
    # Row layout: (_bucket, breakdown_column, breakdown_value, is_other, count).
    assert {r[1] for r in rows} == {"platform", "event_type"}
    # Each breakdown column's counts independently sum to the windowed row total.
    expected = sum(1 for r in adapter._events if tf <= r["event_time"] < tt)
    for column in ("platform", "event_type"):
        assert sum(r[-1] for r in rows if r[1] == column) == expected


# --------------------------------------------------------------------------- #
# time-bucketed aggregates
# --------------------------------------------------------------------------- #


def _orders_in(adapter: SyntheticAdapter, tf: datetime, tt: datetime) -> list[dict]:
    return [r for r in adapter._orders if tf <= r["created_at"] < tt]


def test_bucketed_aggregate_sum_matches_manual() -> None:
    adapter = _adapter()
    tf, tt = ANCHOR - timedelta(days=6), ANCHOR
    _cols, _json, rows = adapter.get_time_bucketed_aggregate(
        "SELECT * FROM orders", "created_at", "1d", MA.sum, "amount", [], [], None, tf, tt
    )
    got = {bucket: value for bucket, value in rows}
    manual: dict[datetime, float] = {}
    for r in _orders_in(adapter, tf, tt):
        manual[_day(r["created_at"])] = manual.get(_day(r["created_at"]), 0.0) + r["amount"]
    assert set(got) == set(manual)
    for bucket, total in manual.items():
        assert got[bucket] == pytest.approx(total)


def test_bucketed_aggregate_count_avg_and_count_distinct() -> None:
    adapter = _adapter()
    tf, tt = ANCHOR - timedelta(days=4), ANCHOR
    orders = _orders_in(adapter, tf, tt)

    _c, _j, count_rows = adapter.get_time_bucketed_aggregate(
        "SELECT * FROM orders", "created_at", "1d", MA.count, None, [], [], None, tf, tt
    )
    assert sum(r[-1] for r in count_rows) == len(orders)

    _c, _j, avg_rows = adapter.get_time_bucketed_aggregate(
        "SELECT * FROM orders", "created_at", "1d", MA.avg, "amount", [], [], None, tf, tt
    )
    for bucket, value in avg_rows:
        day_orders = [r["amount"] for r in orders if _day(r["created_at"]) == bucket]
        assert value == pytest.approx(sum(day_orders) / len(day_orders))

    _c, _j, distinct_rows = adapter.get_time_bucketed_aggregate(
        "SELECT * FROM orders",
        "created_at",
        "1d",
        MA.count_distinct,
        "user_id",
        [],
        [],
        None,
        tf,
        tt,
    )
    for bucket, value in distinct_rows:
        day_users = {r["user_id"] for r in orders if _day(r["created_at"]) == bucket}
        assert value == len(day_users)


def test_aggregate_breakdown_folds_other_and_sums() -> None:
    adapter = _adapter()
    tf, tt = ANCHOR - timedelta(days=8), ANCHOR
    _c, _j, rows = adapter.get_time_bucketed_aggregate_breakdown(
        "SELECT * FROM orders",
        "created_at",
        "1d",
        MA.sum,
        "amount",
        "country",
        [],
        [],
        None,
        tf,
        tt,
        values_limit=3,
    )
    # Row layout: (_bucket, breakdown_value, is_other, aggregate_value).
    assert all(r[2] in (0, 1) for r in rows)
    non_other = {r[1] for r in rows if r[2] == 0}
    assert len(non_other) <= 2  # values_limit - 1
    # Each bucket's per-breakdown sums reconstruct the bucket's total order amount.
    per_bucket_total: dict[datetime, float] = {}
    for r in _orders_in(adapter, tf, tt):
        per_bucket_total[_day(r["created_at"])] = (
            per_bucket_total.get(_day(r["created_at"]), 0.0) + r["amount"]
        )
    summed: dict[datetime, float] = {}
    for bucket, _value, _is_other, agg in rows:
        summed[bucket] = summed.get(bucket, 0.0) + agg
    for bucket, total in per_bucket_total.items():
        assert summed[bucket] == pytest.approx(total)


def test_multi_aggregate_one_scan_many_specs() -> None:
    adapter = _adapter()
    tf, tt = ANCHOR - timedelta(days=5), ANCHOR
    specs = [
        AggregateSpec(key="rev", aggregation=MA.sum, column="amount"),
        AggregateSpec(key="cnt", aggregation=MA.count),
    ]
    col_names, rows = adapter.get_time_bucketed_multi_aggregate(
        "SELECT * FROM orders", "created_at", "1d", specs, tf, tt
    )
    assert col_names == ["bucket", "rev", "cnt"]
    for bucket, rev, cnt in rows:
        day_orders = [r for r in _orders_in(adapter, tf, tt) if _day(r["created_at"]) == bucket]
        assert cnt == len(day_orders)
        assert rev == pytest.approx(sum(r["amount"] for r in day_orders))


def test_multi_aggregate_breakdown_with_filter_and_null_sentinel() -> None:
    adapter = _adapter()
    tf, tt = ANCHOR - timedelta(days=10), ANCHOR
    specs = [
        AggregateSpec(
            key="rev", aggregation=MA.sum, column="amount", filter_sql="status = 'completed'"
        ),
        AggregateSpec(key="cnt", aggregation=MA.count),
    ]
    col_names, rows = adapter.get_time_bucketed_multi_aggregate_breakdown(
        "SELECT * FROM orders", "created_at", "1d", "country", specs, tf, tt, values_limit=4
    )
    assert col_names == ["bucket", "breakdown_value", "is_other", "rev", "cnt"]
    # 'Other' folds the countries outside the top (values_limit - 1) by row count.
    windowed = _orders_in(adapter, tf, tt)
    top = adapter._top_values(windowed, "country", 4)
    assert top is not None and len(top) <= 3
    for bucket, value, is_other, rev, cnt in rows:
        group = [
            r
            for r in windowed
            if _day(r["created_at"]) == bucket
            and (r["country"] not in top if is_other else r["country"] == value)
        ]
        assert cnt == len(group)
        completed = [r["amount"] for r in group if r["status"] == "completed"]
        if completed:
            assert rev == pytest.approx(sum(completed))
        else:
            # No matching rows -> NULL sentinel, matching the ClickHouse path.
            assert rev is None


def test_unsupported_filter_sql_raises_capability_error() -> None:
    adapter = _adapter()
    tf, tt = ANCHOR - timedelta(days=5), ANCHOR
    specs = [
        AggregateSpec(
            key="x",
            aggregation=MA.count,
            filter_sql="user_id IN (SELECT user_id FROM orders)",
        )
    ]
    with pytest.raises(SyntheticCapabilityError):
        adapter.get_time_bucketed_multi_aggregate(
            "SELECT * FROM orders", "created_at", "1d", specs, tf, tt
        )


# --------------------------------------------------------------------------- #
# limits / determinism / isolation / safety
# --------------------------------------------------------------------------- #


def test_row_limit_and_time_window_are_honoured() -> None:
    adapter = _adapter()
    _cols, rows = adapter.get_preview_rows(
        "SELECT * FROM orders",
        limit=3,
        time_column="created_at",
        time_from=FULL_FROM,
        time_to=FULL_TO,
    )
    assert len(rows) == 3

    # A window before any data yields nothing (no fabrication).
    _cols, empty = adapter.get_preview_rows(
        "SELECT * FROM events",
        limit=100,
        time_column="event_time",
        time_from=ANCHOR - timedelta(days=400),
        time_to=ANCHOR - timedelta(days=390),
    )
    assert empty == []


def test_dataset_is_bounded() -> None:
    adapter = _adapter()
    assert 0 < len(adapter._events) <= SYNTHETIC_MAX_ROWS
    assert 0 < len(adapter._orders) <= SYNTHETIC_MAX_ROWS


def test_determinism_same_seed_and_anchor_are_identical() -> None:
    a, b = _adapter(), _adapter()
    assert a._events == b._events
    assert a._orders == b._orders
    # Identical method output too, not just identical raw data.
    tf, tt = ANCHOR - timedelta(days=5), ANCHOR
    args = ("SELECT * FROM orders", "created_at", "1d", MA.sum, "amount", [], [], None, tf, tt)
    assert a.get_time_bucketed_aggregate(*args) == b.get_time_bucketed_aggregate(*args)


def test_isolation_between_two_synthetic_sources() -> None:
    # Two demo projects -> two sources with distinct seeds -> independent datasets.
    a, b = _adapter(seed=101), _adapter(seed=202)
    assert a._orders != b._orders
    _c, rows_a = a.get_preview_rows(
        "SELECT * FROM orders",
        limit=100000,
        time_column="created_at",
        time_from=FULL_FROM,
        time_to=FULL_TO,
    )
    _c, rows_b = b.get_preview_rows(
        "SELECT * FROM orders",
        limit=100000,
        time_column="created_at",
        time_from=FULL_FROM,
        time_to=FULL_TO,
    )
    assert rows_a != rows_b
    # Each source stays internally consistent (re-query is stable).
    _c, rows_a2 = a.get_preview_rows(
        "SELECT * FROM orders",
        limit=100000,
        time_column="created_at",
        time_from=FULL_FROM,
        time_to=FULL_TO,
    )
    assert rows_a == rows_a2


def test_test_connection_is_local_and_close_is_noop() -> None:
    adapter = _adapter()
    # Honest local check — no host contacted — and close never raises.
    assert adapter.test_connection() is True
    assert adapter.close() is None


def test_unknown_column_is_rejected() -> None:
    adapter = _adapter()
    tf, tt = ANCHOR - timedelta(days=2), ANCHOR
    with pytest.raises(ValueError, match="not found"):
        adapter.get_time_bucketed_counts(
            "SELECT * FROM events", "event_time", "1d", ["nope"], [], None, tf, tt
        )


# --------------------------------------------------------------------------- #
# ongoing (live-scan) volume — bd tripl-yfsj.14
# --------------------------------------------------------------------------- #


def test_ongoing_window_carries_seeded_scale_per_event_volume() -> None:
    """The most-recent hours carry each event at roughly its ``ongoing_base``.

    A live scan reads back its current window from this dataset; if the ongoing
    per-event volume sat at the old ~3-8 rows/hour TOTAL it read as a huge drop
    against the demo's seeded 200-1800/hour baseline. Each
    seeded event must now appear in the newest hour near its base scale.
    """
    from tripl.core.adapters.synthetic import _EVENT_DEFS

    adapter = _adapter()
    newest = ANCHOR - timedelta(hours=1)
    _cols, _json, rows = adapter.get_time_bucketed_counts(
        "SELECT * FROM events", "event_time", "1h", ["event_name"], [], None, newest, ANCHOR
    )
    counts = {name: count for _bucket, name, count in rows}
    # Retired identities emit nothing by design.
    bases = {ev.event_name: ev.ongoing_base for ev in _EVENT_DEFS if not ev.retired}

    for name, base in bases.items():
        assert name in counts, f"{name} absent from the ongoing window"
        # Within the daily/weekly envelope of its base — never the tiny old scale.
        assert 0.4 * base <= counts[name] <= 1.6 * base, (name, counts[name], base)
    assert sum(counts.values()) > 1000, "ongoing window must be at seeded scale, not ~3-8/hour"


def test_ongoing_window_is_shaped_not_flat() -> None:
    """The ongoing series has a daily shape (mild noise), not a flat line."""
    from tripl.core.adapters.synthetic import SYNTHETIC_ONGOING_HOURS

    adapter = _adapter()
    window_start = ANCHOR - timedelta(hours=SYNTHETIC_ONGOING_HOURS)
    _cols, _json, rows = adapter.get_time_bucketed_counts(
        "SELECT * FROM events", "event_time", "1h", ["event_name"], [], None, window_start, ANCHOR
    )
    home_by_bucket = {bucket: count for bucket, name, count in rows if name == "Home Screen View"}
    assert len(home_by_bucket) >= 2
    assert len(set(home_by_bucket.values())) > 1, "ongoing volume must vary hour to hour, not flat"
