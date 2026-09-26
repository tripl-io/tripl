"""Source freshness: is a scan's warehouse data arriving on time? (#269)

When a warehouse load is delayed every scope of the scan reads as a volume
drop. Freshness names that situation directly so the detector can HOLD those
drops and the alerting layer can raise one "data is late" alert instead.

Freshness is computed, never stored, from two facts the metrics worker records
on ``ScanConfig`` after each successful collection:

* ``last_event_at`` — the newest event time observed (bucket resolution);
* ``last_collection_at`` — when that collection finished reading the warehouse.

Rules (``interval`` = the scan's bucket interval):

* ``unknown`` — no interval or no time column (manual-only scan: nothing is
  expected on a schedule), or nothing recorded yet.
* ``overdue`` — ``now - last_collection_at > 2 x interval``: the scan itself is
  not running. Wins over ``late``.
* ``late`` — ``now - last_event_at >= late_threshold(interval, settling)``,
  where ``settling`` is the project's ingestion-settling allowance (see
  ``late_threshold``: the moment the first missing bucket would have been
  scored, capped at three intervals).
* ``fresh`` — otherwise.

Everything except ``load_settling_delay`` / ``compute_config_freshness`` is pure.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.core.analyzers.anomaly_detector import settling_buckets_for
from tripl.core.bucketing import to_utc
from tripl.core.intervals import INTERVALS
from tripl.models.project_anomaly_settings import (
    DEFAULT_ANOMALY_INGESTION_SETTLING_MINUTES,
    ProjectAnomalySettings,
)
from tripl.schemas.scan_config import SourceFreshness, SourceFreshnessStatus

DEFAULT_SETTLING = timedelta(minutes=DEFAULT_ANOMALY_INGESTION_SETTLING_MINUTES)
# Upper bound, in intervals, on how old the newest event may get before the
# source is late (see ``late_threshold``).
LATE_INTERVALS = 3
# How many intervals without a completed collection before the scan is overdue.
OVERDUE_INTERVALS = 2

STATUS_FRESH: SourceFreshnessStatus = "fresh"
STATUS_LATE: SourceFreshnessStatus = "late"
STATUS_OVERDUE: SourceFreshnessStatus = "overdue"
STATUS_UNKNOWN: SourceFreshnessStatus = "unknown"
# Statuses under which the detector withholds new volume drops.
HOLDING_STATUSES: frozenset[str] = frozenset({STATUS_LATE, STATUS_OVERDUE})


class FreshnessSubject(Protocol):
    """The ``ScanConfig`` attributes freshness reads (ORM row or a stand-in)."""

    @property
    def interval(self) -> str | None: ...
    @property
    def time_column(self) -> str | None: ...
    @property
    def last_event_at(self) -> datetime | None: ...
    @property
    def last_collection_at(self) -> datetime | None: ...


def interval_delta(interval_code: str | None) -> timedelta | None:
    """The bucket width of a scan interval code, ``None`` when unset/unknown."""
    if interval_code is None:
        return None
    spec = INTERVALS.get(str(interval_code))
    return spec.delta if spec is not None else None


def late_threshold(interval: timedelta, settling: timedelta = DEFAULT_SETTLING) -> timedelta:
    """How old the newest event may get before the source counts as late.

    ``min(3 x interval, (k + 2) x interval)`` with ``k`` the detector's own
    settling withhold in buckets (``settling_buckets_for``). ``last_event_at``
    is the START of the newest non-empty bucket ``B``; the next bucket closes
    at ``B + 2 x interval`` and the detector first scores it ``k`` buckets
    later. A source is therefore late NO LATER than the moment its first
    missing bucket could be emitted as a drop — which is what lets the hold
    catch that drop instead of racing it. The three-interval cap keeps a long
    settling allowance from pushing the alert out indefinitely. Callers compare
    with ``>=``.
    """
    buckets = settling_buckets_for(interval, settling)
    return min(LATE_INTERVALS * interval, (buckets + 2) * interval)


def overdue_threshold(interval: timedelta) -> timedelta:
    """How long since the last completed collection before the scan is overdue."""
    return OVERDUE_INTERVALS * interval


def evaluate_freshness(
    *,
    interval_code: str | None,
    last_event_at: datetime | None,
    last_collection_at: datetime | None,
    now: datetime,
    settling: timedelta = DEFAULT_SETTLING,
    scheduled: bool = True,
) -> SourceFreshness:
    """Freshness from explicit facts. ``scheduled=False`` forces ``unknown``.

    Every datetime is stamped aware UTC on the way in: SQLite hands back naive
    values where Postgres hands back aware ones.
    """
    now = to_utc(now)
    event_at = to_utc(last_event_at) if last_event_at is not None else None
    collection_at = to_utc(last_collection_at) if last_collection_at is not None else None
    lag_seconds = max(0, int((now - event_at).total_seconds())) if event_at is not None else None

    delta = interval_delta(interval_code)
    if delta is None or not scheduled:
        return SourceFreshness(
            status=STATUS_UNKNOWN,
            lag_seconds=lag_seconds,
            last_event_at=event_at,
            last_collection_at=collection_at,
        )

    allowance = late_threshold(delta, settling)
    expected_by = event_at + allowance if event_at is not None else None

    status: SourceFreshnessStatus
    if collection_at is not None and now - collection_at > overdue_threshold(delta):
        status = STATUS_OVERDUE
    elif event_at is None:
        status = STATUS_UNKNOWN
    elif now - event_at >= allowance:
        status = STATUS_LATE
    else:
        status = STATUS_FRESH

    return SourceFreshness(
        status=status,
        lag_seconds=lag_seconds,
        last_event_at=event_at,
        last_collection_at=collection_at,
        expected_by=expected_by,
    )


def compute_freshness(
    config: FreshnessSubject,
    now: datetime,
    *,
    settling: timedelta | None = None,
) -> SourceFreshness:
    """Freshness of one scan config at ``now``.

    ``settling`` is the project's ingestion-settling allowance; ``None`` uses
    the system default (callers with a session should pass the project's,
    see ``load_settling_delay`` / ``compute_config_freshness``). A config the
    scheduler never collects (no interval or no time column) is ``unknown``.
    """
    return evaluate_freshness(
        interval_code=config.interval,
        last_event_at=config.last_event_at,
        last_collection_at=config.last_collection_at,
        now=now,
        settling=DEFAULT_SETTLING if settling is None else settling,
        scheduled=bool(config.time_column),
    )


def is_holding(freshness: SourceFreshness) -> bool:
    """Whether new drop-direction volume anomalies are withheld for the scan."""
    return freshness.status in HOLDING_STATUSES


def advance_last_event_at(current: datetime | None, observed: datetime | None) -> datetime | None:
    """The newer of two event watermarks; a stored value never moves backwards."""
    if observed is None:
        return to_utc(current) if current is not None else None
    observed = to_utc(observed)
    if current is None:
        return observed
    return max(to_utc(current), observed)


def format_duration(value: timedelta) -> str:
    """Compact human duration for alert copy: ``45m``, ``7h``, ``3d``."""
    seconds = max(0, int(value.total_seconds()))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 2 * 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def load_settling_delay(session: Session, project_id: uuid.UUID) -> timedelta:
    """The project's ingestion-settling allowance (sync), default when unset."""
    minutes = session.execute(
        select(ProjectAnomalySettings.anomaly_ingestion_settling_minutes).where(
            ProjectAnomalySettings.project_id == project_id
        )
    ).scalar()
    return DEFAULT_SETTLING if minutes is None else timedelta(minutes=minutes)


def compute_config_freshness(
    session: Session,
    config: FreshnessSubject,
    project_id: uuid.UUID,
    now: datetime,
) -> SourceFreshness:
    """``compute_freshness`` with the project's settling window loaded (sync)."""
    return compute_freshness(config, now, settling=load_settling_delay(session, project_id))
