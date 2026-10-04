"""Golden conformance suite for the warehouse bucket contract.

:mod:`tripl.core.bucketing` is the spec; this file is the executable copy of it.
Every adapter translates an interval *code* into its own dialect, and each
translation has to agree with :func:`floor_to_bucket` for the same UTC instant.
Nothing here mocks the contract — ``floor_to_bucket`` is called for real, and the
synthetic adapter (the in-memory reference) is *executed* against it rather than
having its SQL inspected.

The real adapters cannot be executed without a container, so their conformance is
pinned at the SQL-string level: the exact dialect expression each one emits for all
five intervals. Executing that SQL against live warehouses is tripl-64n8.9.

Two production bugs motivated this file, and each has a test that fails without the
fix:

* ``1w`` used to bin 7 days off the Unix epoch, which is a **Thursday** — so "weekly"
  buckets started on Thursday. See :func:`test_week_bucket_always_lands_on_monday`.
* BigQuery emitted ``TIMESTAMP_BIN``, which is not a GoogleSQL function at all, so
  every bucketed BigQuery query was a syntax error. See
  :func:`test_bigquery_bucket_sql`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from tripl.core.adapters.bigquery import BigQueryAdapter
from tripl.core.adapters.clickhouse import ClickHouseAdapter
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.postgres import PostgresAdapter
from tripl.core.adapters.synthetic import SyntheticAdapter
from tripl.core.bucketing import (
    EPOCH,
    WEEK_ORIGIN,
    floor_to_bucket,
    format_utc_literal,
    to_utc,
)
from tripl.core.intervals import INTERVALS, get_interval

#: Every interval code the contract defines. Parametrizing off the production table
#: means a newly added code cannot skip this suite by being forgotten here.
CODES = ["15m", "1h", "6h", "1d", "1w"]


def test_codes_cover_the_whole_interval_table() -> None:
    assert set(CODES) == set(INTERVALS)


# --------------------------------------------------------------------------- #
# floor_to_bucket: boundaries
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("code", "boundary"),
    [
        ("15m", datetime(2026, 7, 12, 14, 30, 0, tzinfo=UTC)),
        ("1h", datetime(2026, 7, 12, 14, 0, 0, tzinfo=UTC)),
        ("6h", datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)),
        ("1d", datetime(2026, 7, 12, 0, 0, 0, tzinfo=UTC)),
        # 2026-07-06 is a Monday.
        ("1w", datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC)),
    ],
)
def test_floor_to_bucket_boundary_behavior(code: str, boundary: datetime) -> None:
    """A timestamp ON a boundary, just before it, and just after it.

    The three cases pin the two things a bucket function can get wrong: which side of
    the boundary a row on it falls (it is the *start* of its own bucket, never the end
    of the previous one), and whether the grid is anchored where the contract says.
    """
    width = get_interval(code).delta
    tick = timedelta(microseconds=1)

    # ON the boundary: the instant is the start of its own bucket, and it is a fixed
    # point of the flooring.
    assert floor_to_bucket(boundary, code) == boundary

    # JUST BEFORE: still belongs to the *previous* bucket.
    assert floor_to_bucket(boundary - tick, code) == boundary - width

    # JUST AFTER: belongs to the bucket the boundary opened.
    assert floor_to_bucket(boundary + tick, code) == boundary

    # And one full width past the boundary opens the next bucket exactly.
    assert floor_to_bucket(boundary + width, code) == boundary + width


@pytest.mark.parametrize("code", CODES)
def test_floor_to_bucket_is_idempotent_and_grid_aligned(code: str) -> None:
    """Flooring a floored value changes nothing, and the result sits on the grid."""
    spec = get_interval(code)
    origin = WEEK_ORIGIN if code == "1w" else EPOCH
    moment = datetime(2026, 7, 12, 14, 37, 42, 123456, tzinfo=UTC)

    bucket = floor_to_bucket(moment, code)
    assert floor_to_bucket(bucket, code) == bucket
    # Exactly a whole number of widths from the interval's declared origin.
    assert (bucket - origin) % spec.delta == timedelta(0)
    # The bucket never runs ahead of the instant, and is within one width of it.
    assert bucket <= moment < bucket + spec.delta


@pytest.mark.parametrize(
    ("code", "moment", "expected"),
    [
        # Sub-week buckets are anchored at the Unix epoch, so they land on natural
        # clock boundaries (:00/:15/:30/:45, the hour, 00/06/12/18, midnight).
        (
            "15m",
            datetime(2026, 7, 12, 14, 44, 59, 999999, tzinfo=UTC),
            datetime(2026, 7, 12, 14, 30, tzinfo=UTC),
        ),
        (
            "15m",
            datetime(2026, 7, 12, 14, 45, tzinfo=UTC),
            datetime(2026, 7, 12, 14, 45, tzinfo=UTC),
        ),
        (
            "1h",
            datetime(2026, 7, 12, 14, 59, 59, tzinfo=UTC),
            datetime(2026, 7, 12, 14, 0, tzinfo=UTC),
        ),
        (
            "6h",
            datetime(2026, 7, 12, 5, 59, 59, tzinfo=UTC),
            datetime(2026, 7, 12, 0, 0, tzinfo=UTC),
        ),
        (
            "6h",
            datetime(2026, 7, 12, 18, 0, 1, tzinfo=UTC),
            datetime(2026, 7, 12, 18, 0, tzinfo=UTC),
        ),
        (
            "1d",
            datetime(2026, 7, 12, 23, 59, 59, 999999, tzinfo=UTC),
            datetime(2026, 7, 12, 0, 0, tzinfo=UTC),
        ),
    ],
)
def test_floor_to_bucket_known_values(code: str, moment: datetime, expected: datetime) -> None:
    assert floor_to_bucket(moment, code) == expected


def test_epoch_and_week_origin_are_the_documented_anchors() -> None:
    assert datetime(1970, 1, 1, tzinfo=UTC) == EPOCH
    assert datetime(1970, 1, 5, tzinfo=UTC) == WEEK_ORIGIN
    # 1970-01-01 was a Thursday (weekday 3); the week origin is the first Monday after.
    assert EPOCH.weekday() == 3
    assert WEEK_ORIGIN.weekday() == 0


# --------------------------------------------------------------------------- #
# 1w lands on a Monday — the bug the contract exists to prevent
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 7, 6, 0, 0, tzinfo=UTC),  # Monday, exactly on the boundary
        datetime(2026, 7, 6, 23, 59, 59, tzinfo=UTC),  # Monday, late
        datetime(2026, 7, 7, 9, 30, tzinfo=UTC),  # Tuesday
        datetime(2026, 7, 8, 12, 0, tzinfo=UTC),  # Wednesday
        datetime(2026, 7, 9, 0, 0, 1, tzinfo=UTC),  # Thursday
        datetime(2026, 7, 10, 18, 45, tzinfo=UTC),  # Friday
        datetime(2026, 7, 11, 6, 0, tzinfo=UTC),  # Saturday
        datetime(2026, 7, 12, 23, 59, 59, 999999, tzinfo=UTC),  # Sunday, last instant
    ],
)
def test_week_bucket_always_lands_on_monday(moment: datetime) -> None:
    """Every day of one week floors to that week's Monday.

    A 7-day bin measured from the Unix epoch starts weeks on a **Thursday** (1970-01-01
    was a Thursday) — that was the bug. The eight inputs above span a full Mon..Sun
    week, so a Thursday-anchored grid would split them across two buckets and fail here.
    """
    bucket = floor_to_bucket(moment, "1w")
    assert bucket.weekday() == 0, f"{moment} floored to {bucket}, a {bucket.strftime('%A')}"
    assert bucket == datetime(2026, 7, 6, tzinfo=UTC)
    assert bucket.time() == datetime(1970, 1, 1).time()  # midnight


def test_week_buckets_tile_without_gaps_or_overlap() -> None:
    """Consecutive days across two weeks produce exactly two Mondays, 7 days apart."""
    days = [datetime(2026, 7, 6, tzinfo=UTC) + timedelta(days=n) for n in range(14)]
    buckets = sorted({floor_to_bucket(d, "1w") for d in days})
    assert buckets == [datetime(2026, 7, 6, tzinfo=UTC), datetime(2026, 7, 13, tzinfo=UTC)]
    assert buckets[1] - buckets[0] == timedelta(days=7)


# --------------------------------------------------------------------------- #
# timezone handling
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("code", CODES)
def test_naive_datetime_is_treated_as_utc(code: str) -> None:
    """A naive datetime is *assumed* UTC — never reinterpreted in the host's local TZ.

    The worker's TZ must not decide which bucket a row lands in.
    """
    naive = datetime(2026, 7, 12, 14, 37, 42)
    aware = naive.replace(tzinfo=UTC)
    assert floor_to_bucket(naive, code) == floor_to_bucket(aware, code)
    assert to_utc(naive) == aware


def test_aware_non_utc_datetime_is_converted_not_stripped() -> None:
    """An aware non-UTC datetime is *converted*; its offset is not dropped.

    Dropping the offset (what ``strftime`` silently does) keeps the wall clock and
    changes the instant — an entire-window shift. Tokyo 08:30+09:00 is 23:30Z the
    *previous day*, so the correct 1d bucket is the 11th, not the 12th.
    """
    tokyo = timezone(timedelta(hours=9))
    moment = datetime(2026, 7, 12, 8, 30, tzinfo=tokyo)

    assert to_utc(moment) == datetime(2026, 7, 11, 23, 30, tzinfo=UTC)
    # Converted: previous UTC day. Had the offset been stripped, this would be the 12th.
    assert floor_to_bucket(moment, "1d") == datetime(2026, 7, 11, tzinfo=UTC)
    assert floor_to_bucket(moment, "1h") == datetime(2026, 7, 11, 23, 0, tzinfo=UTC)
    # Same instant, expressed two ways, buckets identically.
    assert floor_to_bucket(moment, "1d") == floor_to_bucket(to_utc(moment), "1d")


def test_negative_offset_datetime_is_converted() -> None:
    """The mirror case: a west-of-UTC offset moves the instant *forward* in UTC."""
    la = timezone(timedelta(hours=-7))
    moment = datetime(2026, 7, 12, 20, 15, tzinfo=la)  # 2026-07-13T03:15Z

    assert to_utc(moment) == datetime(2026, 7, 13, 3, 15, tzinfo=UTC)
    assert floor_to_bucket(moment, "1d") == datetime(2026, 7, 13, tzinfo=UTC)
    assert floor_to_bucket(moment, "6h") == datetime(2026, 7, 13, 0, 0, tzinfo=UTC)


def test_utc_literal_carries_an_explicit_offset() -> None:
    """Window bounds are rendered with ``+00:00`` and keep their microseconds.

    An offset-less literal is read in the session/column timezone by ClickHouse and
    PostgreSQL, which is exactly the whole-window shift the contract prevents.
    """
    assert format_utc_literal(datetime(2026, 7, 12, 14, 30, 0, 123456, tzinfo=UTC)) == (
        "2026-07-12 14:30:00.123456+00:00"
    )
    # A naive bound is stamped UTC, not localized.
    assert format_utc_literal(datetime(2026, 7, 12, 14, 30)) == "2026-07-12 14:30:00.000000+00:00"
    # An aware non-UTC bound is converted before rendering.
    tokyo = timezone(timedelta(hours=9))
    assert format_utc_literal(datetime(2026, 7, 12, 8, 30, tzinfo=tokyo)) == (
        "2026-07-11 23:30:00.000000+00:00"
    )


@pytest.mark.parametrize("bad", ["1 hour", "1 day", "1 DAY", "1 week", "1 month", "", "1; DROP x"])
def test_unknown_interval_code_is_rejected(bad: str) -> None:
    """Dialect fragments are not interval codes. The five codes are the whole surface."""
    with pytest.raises(ValueError, match="Unknown interval code"):
        floor_to_bucket(datetime(2026, 7, 12, tzinfo=UTC), bad)


# --------------------------------------------------------------------------- #
# half-open windows: [time_from, time_to)
# --------------------------------------------------------------------------- #

_ANCHOR = datetime(2026, 6, 1, tzinfo=UTC)


def _synthetic() -> SyntheticAdapter:
    return SyntheticAdapter(seed=7, anchor=_ANCHOR, history_days=30)


def test_window_is_half_open_row_on_from_is_in_row_on_to_is_out() -> None:
    """A row exactly on ``time_from`` is IN; a row exactly on ``time_to`` is OUT.

    This is what lets adjacent windows tile without double-counting the boundary row.
    Executed against the synthetic adapter, whose window filter is the same one every
    scan path uses.
    """
    adapter = _synthetic()
    times = sorted({r["event_time"] for r in adapter._events})
    # Two real row timestamps to use as the exact window bounds.
    t_from, t_to = times[10], times[40]

    _cols, rows = adapter.get_preview_rows(
        "SELECT * FROM events",
        limit=100000,
        time_column="event_time",
        time_from=t_from,
        time_to=t_to,
    )
    returned = {row[0] for row in rows}

    assert t_from in returned, "a row exactly on time_from must be included"
    assert t_to not in returned, "a row exactly on time_to must be excluded"
    assert all(t_from <= t < t_to for t in returned)

    # The window's contents are exactly the half-open set — no row silently dropped.
    expected = {r["event_time"] for r in adapter._events if t_from <= r["event_time"] < t_to}
    assert returned == expected


def test_adjacent_windows_tile_without_double_counting() -> None:
    """Splitting a window at any instant partitions its rows exactly once each."""
    adapter = _synthetic()
    lo = _ANCHOR - timedelta(days=10)
    mid = _ANCHOR - timedelta(days=5)
    hi = _ANCHOR

    def _count(time_from: datetime, time_to: datetime) -> int:
        _cols, rows = adapter.get_preview_rows(
            "SELECT * FROM events",
            limit=100000,
            time_column="event_time",
            time_from=time_from,
            time_to=time_to,
        )
        return len(rows)

    assert _count(lo, mid) + _count(mid, hi) == _count(lo, hi)


# --------------------------------------------------------------------------- #
# synthetic adapter conformance — the one adapter we can actually execute
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("code", CODES)
def test_synthetic_bucket_start_delegates_to_the_contract(code: str) -> None:
    """The adapter's own flooring is the contract's, not a second implementation."""
    adapter = _synthetic()
    for row in adapter._events[:200]:
        moment = row["event_time"]
        assert adapter._bucket_start(moment, code) == floor_to_bucket(moment, code)


@pytest.mark.parametrize("code", CODES)
def test_synthetic_bucketed_counts_agree_with_floor_to_bucket(code: str) -> None:
    """End-to-end: the buckets a real scan returns are the ones the contract predicts.

    Not a spot-check of a helper — this runs the adapter's normal bucketed-count path
    over its whole in-memory dataset and rebuilds the same grouping independently from
    ``floor_to_bucket``. Bucket keys *and* per-bucket counts must match exactly.
    """
    adapter = _synthetic()
    time_from = _ANCHOR - timedelta(days=21)
    time_to = _ANCHOR

    _cols, _json, rows = adapter.get_time_bucketed_counts(
        "SELECT * FROM events",
        "event_time",
        code,
        ["event_type"],
        [],
        None,
        time_from,
        time_to,
    )
    # Row layout: (_bucket, event_type, count) -> collapse the breakdown away.
    got: dict[datetime, int] = {}
    for bucket, _event_type, count in rows:
        got[bucket] = got.get(bucket, 0) + count

    expected: dict[datetime, int] = {}
    for row in adapter._events:
        moment = row["event_time"]
        if time_from <= moment < time_to:
            bucket = floor_to_bucket(moment, code)
            expected[bucket] = expected.get(bucket, 0) + 1

    assert got == expected
    assert sum(got.values()) > 0, "fixture must actually exercise the window"
    # Every bucket the adapter reports is on the contract's grid.
    for bucket in got:
        assert floor_to_bucket(bucket, code) == bucket


def test_synthetic_week_buckets_are_mondays() -> None:
    """The Monday anchor survives the full adapter path, not just the helper."""
    adapter = _synthetic()
    _cols, _json, rows = adapter.get_time_bucketed_counts(
        "SELECT * FROM events",
        "event_time",
        "1w",
        ["event_type"],
        [],
        None,
        _ANCHOR - timedelta(days=28),
        _ANCHOR,
    )
    buckets = {row[0] for row in rows}
    assert buckets, "fixture must produce at least one week bucket"
    for bucket in buckets:
        assert bucket.weekday() == 0, f"week bucket {bucket} is a {bucket.strftime('%A')}"


# --------------------------------------------------------------------------- #
# dialect SQL conformance — string-level, per adapter, all five intervals
# --------------------------------------------------------------------------- #

_COL = "time"


def _clickhouse() -> ClickHouseAdapter:
    adapter = object.__new__(ClickHouseAdapter)
    adapter._allowed_columns = {_COL}
    adapter._json_path_discovery = "dynamic"
    return adapter


def _postgres() -> PostgresAdapter:
    adapter = object.__new__(PostgresAdapter)
    adapter._allowed_columns = {_COL}
    return adapter


def _bigquery(time_type: str = "TIMESTAMP") -> BigQueryAdapter:
    adapter = object.__new__(BigQueryAdapter)
    adapter._allowed_columns = {_COL}
    adapter._column_types = {_COL: time_type}
    adapter._struct_paths = {}
    adapter._repeated_columns = set()
    return adapter


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("15m", "toStartOfInterval(`time`, INTERVAL 15 MINUTE, 'UTC')"),
        ("1h", "toStartOfInterval(`time`, INTERVAL 1 HOUR, 'UTC')"),
        ("6h", "toStartOfInterval(`time`, INTERVAL 6 HOUR, 'UTC')"),
        ("1d", "toStartOfInterval(`time`, INTERVAL 1 DAY, 'UTC')"),
        # toStartOfInterval(.., INTERVAL 1 WEEK) is already Monday-anchored, but returns
        # a Date rather than a DateTime; toDateTime(toMonday(..)) keeps the bucket
        # column's type consistent across all five intervals.
        ("1w", "toDateTime(toMonday(`time`, 'UTC'), 'UTC')"),
    ],
)
def test_clickhouse_bucket_sql(code: str, expected: str) -> None:
    # The explicit 'UTC' argument is load-bearing: without it toStartOfInterval buckets
    # in the *column's* timezone, so a DateTime('Asia/Tokyo') column lands rows in the
    # wrong day.
    assert _clickhouse()._bucket_expression(_COL, code) == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (
            "15m",
            "date_bin(INTERVAL '15 minutes', \"time\", "
            "TIMESTAMPTZ '1970-01-01 00:00:00.000000+00:00')",
        ),
        (
            "1h",
            "date_bin(INTERVAL '1 hours', \"time\", "
            "TIMESTAMPTZ '1970-01-01 00:00:00.000000+00:00')",
        ),
        (
            "6h",
            "date_bin(INTERVAL '6 hours', \"time\", "
            "TIMESTAMPTZ '1970-01-01 00:00:00.000000+00:00')",
        ),
        (
            "1d",
            "date_bin(INTERVAL '1 days', \"time\", TIMESTAMPTZ '1970-01-01 00:00:00.000000+00:00')",
        ),
        # A 7-day date_bin off the epoch would start weeks on a Thursday, so the week
        # origin is passed explicitly.
        (
            "1w",
            "date_bin(INTERVAL '7 days', \"time\", TIMESTAMPTZ '1970-01-05 00:00:00.000000+00:00')",
        ),
    ],
)
def test_postgres_bucket_sql(code: str, expected: str) -> None:
    assert _postgres()._bucket_expression(_COL, code) == expected


def test_postgres_week_origin_literal_is_a_monday() -> None:
    """Guard the literal itself: 1970-01-05 is the first Monday of the epoch."""
    sql = _postgres()._bucket_expression(_COL, "1w")
    assert format_utc_literal(WEEK_ORIGIN) in sql
    assert WEEK_ORIGIN.weekday() == 0
    # The epoch origin must NOT appear on the week path — that is the Thursday bug.
    assert format_utc_literal(EPOCH) not in sql


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (
            "15m",
            "TIMESTAMP_BUCKET(`time`, INTERVAL 15 MINUTE, "
            "TIMESTAMP '1970-01-01 00:00:00.000000+00:00')",
        ),
        (
            "1h",
            "TIMESTAMP_BUCKET(`time`, INTERVAL 1 HOUR, "
            "TIMESTAMP '1970-01-01 00:00:00.000000+00:00')",
        ),
        (
            "6h",
            "TIMESTAMP_BUCKET(`time`, INTERVAL 6 HOUR, "
            "TIMESTAMP '1970-01-01 00:00:00.000000+00:00')",
        ),
        (
            "1d",
            "TIMESTAMP_BUCKET(`time`, INTERVAL 1 DAY, "
            "TIMESTAMP '1970-01-01 00:00:00.000000+00:00')",
        ),
        ("1w", "TIMESTAMP_TRUNC(`time`, WEEK(MONDAY), 'UTC')"),
    ],
)
def test_bigquery_bucket_sql(code: str, expected: str) -> None:
    """``TIMESTAMP_BUCKET``/``TIMESTAMP_TRUNC`` — never ``TIMESTAMP_BIN``.

    ``TIMESTAMP_BIN`` is not a GoogleSQL function. The adapter used to emit it, so every
    bucketed BigQuery query it built was a syntax error that could never have executed.
    """
    sql = _bigquery()._bucket_expression(_COL, code)
    assert sql == expected
    assert "TIMESTAMP_BIN" not in sql


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (
            "15m",
            "DATETIME_BUCKET(`time`, INTERVAL 15 MINUTE, DATETIME '1970-01-01 00:00:00.000000')",
        ),
        ("1h", "DATETIME_BUCKET(`time`, INTERVAL 1 HOUR, DATETIME '1970-01-01 00:00:00.000000')"),
        ("6h", "DATETIME_BUCKET(`time`, INTERVAL 6 HOUR, DATETIME '1970-01-01 00:00:00.000000')"),
        ("1d", "DATETIME_BUCKET(`time`, INTERVAL 1 DAY, DATETIME '1970-01-01 00:00:00.000000')"),
        ("1w", "DATETIME_TRUNC(`time`, WEEK(MONDAY))"),
    ],
)
def test_bigquery_datetime_column_bucket_sql(code: str, expected: str) -> None:
    """GoogleSQL rejects a TIMESTAMP_* function on a DATETIME column, so the function
    family follows the column's *declared* type. DATETIME is zone-less: no 'UTC' arg,
    and no offset in the origin literal (BigQuery rejects one)."""
    assert _bigquery(time_type="DATETIME")._bucket_expression(_COL, code) == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("1d", "DATE_BUCKET(`time`, INTERVAL 1 DAY, DATE '1970-01-01')"),
        ("1w", "DATE_TRUNC(`time`, WEEK(MONDAY))"),
    ],
)
def test_bigquery_date_column_bucket_sql(code: str, expected: str) -> None:
    assert _bigquery(time_type="DATE")._bucket_expression(_COL, code) == expected


@pytest.mark.parametrize("code", ["15m", "1h", "6h"])
def test_bigquery_date_column_rejects_sub_day_intervals(code: str) -> None:
    """A DATE column has no time-of-day, so a sub-day bucket is refused rather than
    silently rounded. Nothing configuration-time catches it — neither scan-config save
    path compares the interval against the column's declared type, they run only
    ``check_scalar_columns_unreserved`` and ``check_replay_chunk_against_interval`` —
    so the first thing to run the combination is a collection tick. Hence
    ``WarehouseCapabilityError``: one of the three classes the worker's sanitiser
    surfaces verbatim instead of flattening to "Scan failed due to an internal
    error." (``worker.tasks._errors._CURATED_ERRORS``)."""
    with pytest.raises(WarehouseCapabilityError, match="no time-of-day"):
        _bigquery(time_type="DATE")._bucket_expression(_COL, code)


@pytest.mark.parametrize("code", CODES)
def test_every_adapter_states_its_week_and_epoch_anchor_explicitly(code: str) -> None:
    """No adapter may rely on its dialect's default bucket origin.

    The dialects disagree by default and disagree *differently*, so a default origin is
    never load-bearing: a week expression must name MONDAY, and a sub-week expression
    must name the epoch.
    """
    ch = _clickhouse()._bucket_expression(_COL, code)
    pg = _postgres()._bucket_expression(_COL, code)
    bq = _bigquery()._bucket_expression(_COL, code)

    if code == "1w":
        assert "toMonday" in ch
        assert "1970-01-05" in pg  # WEEK_ORIGIN, the first Monday
        assert "WEEK(MONDAY)" in bq
    else:
        assert "'UTC'" in ch  # bucket in UTC, not the column's zone
        assert "1970-01-01" in pg  # EPOCH origin, stated
        assert "1970-01-01" in bq
