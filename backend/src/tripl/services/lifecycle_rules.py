"""The arithmetic of the lifecycle watch (#258) — pure, no database.

Shared by the daily sweep (``worker.tasks.lifecycle``, sync) and the API reads
(``services.lifecycle_service``, async), so "received volume in the last 24
hours" means the same thing on both sides.

A window counts every bucket that OVERLAPS it, not only the buckets that start
inside it. ``event_metrics.bucket`` is the bucket's START, and a daily scan's
newest bucket starts at midnight: measured by start alone, "the last 24 hours"
at 06:00 would miss yesterday's bucket entirely and a daily event would flap
between flagged and resolved from one run to the next. The bucket width comes
from the scan config's interval; a scan without one is treated as hourly.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta

from tripl.core.bucketing import to_utc
from tripl.services.monitoring_utils import scan_interval_to_timedelta

#: "Still receiving volume" for a deprecated event past its sunset.
SUNSET_VOLUME_WINDOW = timedelta(hours=24)
#: "Receiving no volume" for a successor.
SUCCESSOR_SILENCE_WINDOW = timedelta(days=7)
#: The window the migration progress averages over (``daily_avg_7d``).
ADOPTION_WINDOW = timedelta(days=7)
#: Width assumed for a bucket whose scan has no interval.
DEFAULT_BUCKET_WIDTH = timedelta(hours=1)
#: The widest bucket a scan can write (``ScanInterval.w1``). Queries look back
#: ``window + MAX_BUCKET_WIDTH`` so an overlapping weekly bucket is in the rows.
MAX_BUCKET_WIDTH = timedelta(weeks=1)


def bucket_width(interval: str | None) -> timedelta:
    return scan_interval_to_timedelta(interval) or DEFAULT_BUCKET_WIDTH


def lookback_start(now: datetime, window: timedelta) -> datetime:
    """The earliest bucket start a window query must fetch."""
    return to_utc(now) - window - MAX_BUCKET_WIDTH


def window_volume(
    rows: Iterable[tuple[uuid.UUID, datetime, int, str | None]],
    *,
    now: datetime,
    window: timedelta,
    since: dict[uuid.UUID, datetime] | None = None,
) -> dict[uuid.UUID, int]:
    """Sum ``count`` per event over the buckets overlapping ``(now - window, now]``.

    ``rows`` are ``(event_id, bucket_start, count, scan_interval)``. ``since``
    narrows the window per event: a bucket that ENDED at or before
    ``since[event_id]`` is left out — the sunset watch passes each event's
    ``sunset_at``, so traffic from before the sunset never counts against it.
    """
    now_utc = to_utc(now)
    window_start = now_utc - window
    totals: dict[uuid.UUID, int] = {}
    for event_id, bucket, count, interval in rows:
        if count <= 0:
            continue
        start = to_utc(bucket)
        end = start + bucket_width(interval)
        if start > now_utc or end <= window_start:
            continue
        cutoff = since.get(event_id) if since else None
        if cutoff is not None and end <= to_utc(cutoff):
            continue
        totals[event_id] = totals.get(event_id, 0) + int(count)
    return totals


def prorated_window_volume(
    rows: Iterable[tuple[uuid.UUID, datetime, int, str | None]],
    *,
    now: datetime,
    window: timedelta,
) -> dict[uuid.UUID, float]:
    """Volume per event inside ``(now - window, now]``, each bucket prorated.

    Unlike :func:`window_volume` (which counts a bucket whole as soon as it
    overlaps the window — right for "did anything arrive"), an average must not
    inflate: a bucket contributes ``count`` times the fraction of its span that
    lies inside the window. A daily bucket that started 7.5 days ago counts
    half; a weekly bucket straddling the window edge counts its overlapping
    share. Only the window's OLD edge prorates: a bucket still in progress
    (ending after ``now``) holds only what arrived so far, so it counts whole —
    clipping it at ``now`` would shave real traffic off the average. A bucket
    that starts after ``now`` is left out.
    """
    now_utc = to_utc(now)
    window_start = now_utc - window
    totals: dict[uuid.UUID, float] = {}
    for event_id, bucket, count, interval in rows:
        if count <= 0:
            continue
        start = to_utc(bucket)
        width = bucket_width(interval)
        end = start + width
        if start > now_utc:
            continue
        overlap = end - max(start, window_start)
        if overlap <= timedelta(0) or width <= timedelta(0):
            continue
        fraction = min(1.0, overlap / width)
        totals[event_id] = totals.get(event_id, 0.0) + int(count) * fraction
    return totals


def daily_average(total: float, window: timedelta = ADOPTION_WINDOW) -> float:
    """``total`` spread over the window's days, rounded to one decimal."""
    days = window / timedelta(days=1)
    return round(total / days, 1) if days > 0 else 0.0


def adoption_ratio(old_daily: float, new_daily: float) -> float | None:
    """How many times the old event's volume the successor now receives.

    ``new_daily / old_daily`` — 1240/day -> 3800/day reads as ``3.06``: the
    successor gets about three times what the old event still gets. Above 1 the
    successor has overtaken the old event; the old event going silent sends it
    up without bound. ``None`` when the old event received nothing in the
    window (the ratio is undefined; the migration is done, or never had data).
    Rounded to two decimals.
    """
    if old_daily <= 0:
        return None
    return round(new_daily / old_daily, 2)
