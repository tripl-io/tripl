"""The canonical time-window and bucket contract for every warehouse adapter.

This module is the reference implementation. ClickHouse, BigQuery, PostgreSQL and
the synthetic adapter each translate an interval code into their own dialect, and
every translation must agree with :func:`floor_to_bucket` for the same UTC input.
The conformance suite asserts exactly that, so the rules below are executable, not
aspirational.

The contract
------------
**Everything is UTC.** A naive ``datetime`` is *assumed* to already be UTC; an
aware one is converted. Adapters must pin the session/column timezone to UTC
rather than inheriting the server's, so a warehouse running in a non-UTC timezone
cannot shift a bucket.

**Windows are half-open**: ``time_from <= t < time_to``. A row exactly on
``time_to`` belongs to the next window, so adjacent windows tile without
double-counting a boundary row.

**Sub-week buckets are anchored at the Unix epoch** (1970-01-01T00:00:00Z). 15m,
1h, 6h and 1d all divide a UTC day evenly, so an epoch anchor also puts every
bucket boundary on a natural clock boundary.

**Week buckets start on Monday**, anchored at :data:`WEEK_ORIGIN`
(1970-01-05T00:00:00Z, the first Monday of the epoch).

The dialects do *not* agree here by default, and they disagree in different ways,
so each adapter states "Monday" explicitly rather than trusting its default:

* A plain 7-day bin measured from the epoch starts weeks on a **Thursday**, because
  1970-01-01 was a Thursday. That is what PostgreSQL's ``date_bin`` and BigQuery's
  ``*_BUCKET`` do if handed the epoch as the origin, so those two adapters pass
  :data:`WEEK_ORIGIN` / use ``*_TRUNC(..., WEEK(MONDAY))`` instead.
* ClickHouse's ``toStartOfInterval(col, INTERVAL 1 WEEK)`` is the exception: it is
  *already* Monday-aligned on :data:`WEEK_ORIGIN`, verified against a live server.
  It nonetheless returns a ``Date`` rather than a ``DateTime``, so a 1w bucket would
  come back as ``datetime.date`` while every other interval yields
  ``datetime.datetime``. The adapter uses ``toDateTime(toMonday(...))`` to keep the
  bucket type consistent across all five intervals, not to fix the origin.

There is no DST hazard anywhere in here *because* the contract is UTC-only: UTC
has no DST transitions, so a fixed-width bin never straddles a clock change.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from tripl.core.intervals import IntervalUnit, get_interval

#: Anchor for every sub-week bucket.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: Anchor for week buckets: the first Monday at or after the epoch. 1970-01-01 was
#: a Thursday, so binning weeks straight off :data:`EPOCH` would start weeks on a
#: Thursday. Weeks start on Monday.
WEEK_ORIGIN = datetime(1970, 1, 5, tzinfo=UTC)


def to_utc(value: datetime) -> datetime:
    """Normalize a datetime to an aware UTC datetime.

    A naive datetime is *assumed* to be UTC and is stamped as such rather than
    being interpreted in the host's local timezone — the worker's TZ must never
    change which bucket a row lands in.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def optional_to_utc(value: datetime | None) -> datetime | None:
    """:func:`to_utc` for a nullable column: ``None`` passes through unchanged.

    For the readers of a ``muted_until`` / ``last_notified_at`` style column,
    where NULL carries its own meaning and must reach the caller as ``None``.
    """
    return None if value is None else to_utc(value)


def stored_bucket(value: object) -> datetime:
    """A warehouse ``_bucket`` cell as the aware UTC instant it is STORED at.

    The one conversion every writer of a bucketed row applies before the value
    reaches a ``DateTime(timezone=True)`` column (``EventMetric.bucket``,
    ``MetricValueBreakdown.bucket``, ``DistributionDrift`` …). Those writers used
    to ``cast(datetime, row[0])`` — a typing no-op — and hand whatever the driver
    decoded to SQLAlchemy. A naive value written to a ``timestamptz`` is read in
    the DATABASE SESSION's timezone, so the stored instant depended on a server
    setting rather than on the bucket. ``db_config`` now pins
    the application's own sessions to UTC, and this closes the other half: a
    naive cell is stamped UTC here, a ``date`` becomes that day at 00:00 UTC, and
    an aware one is converted, whatever adapter produced it.

    ``datetime`` is tested BEFORE ``date`` because it is a subclass of it. A
    value that is no kind of date raises instead of being coerced: it means the
    row layout changed and column 0 stopped being the bucket.

    Rows already stored are NOT rewritten by this. A BigQuery ``DATETIME`` /
    ``DATE`` bucket used to reach the database naive (``BigQueryAdapter`` now
    normalizes it too, ``_as_utc_bucket``), so on an install whose database
    session timezone was not UTC when those rows were written they sit at
    ``wall clock - offset`` while every new row lands at ``wall clock``. The
    unique key then sees two rows for one logical bucket at the edges of an
    overlapping re-collection. ``TIMESTAMP`` buckets, and every install whose
    database session ran in UTC (the default of a PostgreSQL image initialised
    without a ``TZ``, as ``compose.yaml`` starts it), are unaffected; the check
    and the remedy for the rest are in ``website/docs/develop/warehouse-parity.md``
    ("BigQuery ``DATETIME`` is zone-less").
    """
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    msg = f"Expected a bucket datetime in column 0, got {type(value).__name__}: {value!r}"
    raise TypeError(msg)


def bucket_origin(interval_code: str) -> datetime:
    """The anchor a given interval's buckets are measured from."""
    spec = get_interval(interval_code)
    return WEEK_ORIGIN if spec.unit is IntervalUnit.week else EPOCH


def floor_to_bucket(value: datetime, interval_code: str) -> datetime:
    """Floor a timestamp to the start of its bucket, in UTC.

    This is the definition every adapter's generated SQL is measured against.
    """
    spec = get_interval(interval_code)
    moment = to_utc(value)
    origin = bucket_origin(interval_code)
    elapsed: timedelta = moment - origin
    return origin + (elapsed // spec.delta) * spec.delta


def format_utc_literal(value: datetime) -> str:
    """Render a UTC timestamp for embedding in warehouse SQL.

    Includes the explicit ``+00:00`` offset: an offset-less literal is read in the
    session timezone by some dialects, which is exactly the window shift this
    contract exists to prevent. Microseconds are preserved so a half-open window
    bound cannot silently swallow or drop a sub-second row.
    """
    return to_utc(value).strftime("%Y-%m-%d %H:%M:%S.%f+00:00")
