"""Keep a demo collection inside the hours the demo warehouse still holds.

A ``demo`` source reads the shared demo warehouse, which keeps
``DEMO_WAREHOUSE_RETENTION_DAYS`` and drops older day partitions. A collection
window reaching further back than that reads nothing for the dropped hours,
and the collector deletes a window before rewriting it and counts a completed
job's window as covered — so those hours would be stored and scored as ZEROS:
a drop to zero on every scope, the rot the old synthetic demo suffered after a
long pause. That happens when a demo resumes after a pause longer than the
retention, or when someone replays a demo further back than the warehouse goes.

:func:`clamp_demo_window_start` moves the start forward to the oldest retained
hour instead. The hours in between were never collected and stay uncovered,
which the detector reads as missing data, not as zeros.

The horizon per engine:

* PostgreSQL: the writer's own bookkeeping (``warehouse_status().oldest``);
* in memory: the same retention measured from now — the model can answer any
  hour, but a demo must not hold history the warehouse would not, so both
  engines give one demo the same series.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tripl.config import settings
from tripl.core.bucketing import floor_to_bucket, to_utc
from tripl.core.demo_warehouse import warehouse_status
from tripl.core.intervals import get_interval
from tripl.worker.tasks._errors import ScanError


def demo_warehouse_horizon(now: datetime) -> datetime:
    """The oldest instant the demo warehouse holds at ``now`` (aware UTC)."""
    status = warehouse_status()
    if status.engine == "postgres" and status.oldest is not None:
        return to_utc(status.oldest)
    hour = to_utc(now).replace(minute=0, second=0, microsecond=0)
    return hour - timedelta(days=settings.demo_warehouse_retention_days)


def clamp_demo_window_start(
    time_from: datetime,
    time_to: datetime,
    *,
    interval_code: str,
    now: datetime,
) -> datetime:
    """``time_from``, moved forward to the warehouse horizon when it reaches past it.

    The clamped start is rounded UP to the config's grid, so it stays a bucket
    boundary inside the retained data. Raises :class:`ScanError` when nothing
    of the window is left, rather than collecting an empty window as zeros.
    """
    horizon = demo_warehouse_horizon(now)
    start = to_utc(time_from)
    if start >= horizon:
        return time_from
    floored = floor_to_bucket(horizon, interval_code)
    clamped = floored if floored == horizon else floored + get_interval(interval_code).delta
    if clamped >= to_utc(time_to):
        msg = (
            f"the demo warehouse holds data from {horizon:%Y-%m-%d %H:%M} UTC only; "
            "nothing of this window is left to collect."
        )
        raise ScanError(msg)
    # Hand the bound back in the awareness it arrived in (naive on SQLite).
    return clamped if time_from.tzinfo is not None else clamped.replace(tzinfo=None)
