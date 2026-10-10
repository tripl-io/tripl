"""Small shared helpers for the metrics task package.

Keeps timestamp math, the sync-session factory, the adapter builder, and the
"stale active scan job" guard out of the main task module so the entrypoints
(`collect_metrics`, `check_metrics_due`) can stay focused on orchestration.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.core.bucketing import EPOCH, WEEK_ORIGIN, to_utc
from tripl.models.domain_enums import MetricScopeType
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.worker.db import _build_adapter, _get_sync_session

__all__ = [
    "ACTIVE_SCAN_JOB_STATUSES",
    "MAX_BREAKDOWN_VALUE_LENGTH",
    "SCOPE_SCHEMA_DRIFT",
    "STALE_ACTIVE_SCAN_JOB_TIMEOUT",
    "_build_adapter",
    "_ceil_to_interval",
    "_fail_stale_active_scan_job",
    "_floor_to_interval",
    "_get_active_scan_job",
    "_get_active_scan_jobs",
    "_get_scan_job_activity_at",
    "_get_sync_session",
    "_parse_task_datetime",
]

logger = logging.getLogger(__name__)

ACTIVE_SCAN_JOB_STATUSES = (
    ScanJobStatus.pending.value,
    ScanJobStatus.running.value,
)
# Must exceed the default Celery hard ``task_time_limit`` (60 min, see
# celery_app.py). Metrics collection has a longer per-task override for replay,
# and replay jobs additionally heartbeat ``ScanJob.updated_at`` per chunk so
# progress is visible to ``_get_scan_job_activity_at``. A genuinely dead job
# (worker OOM/redeploy, no heartbeat) is still cleaned up after this window.
STALE_ACTIVE_SCAN_JOB_TIMEOUT = timedelta(minutes=75)
MAX_BREAKDOWN_VALUE_LENGTH = 500
SCOPE_SCHEMA_DRIFT = MetricScopeType.schema.value


#: A week, as ``get_interval("1w").delta``. Only the weekly grid has an origin of
#: its own, so this is the test for "is this the week interval" without needing
#: the interval code here — the callers only ever have the delta.
_WEEK = timedelta(weeks=1)


def _floor_to_interval(dt: datetime, delta: timedelta) -> datetime:
    """Floor a datetime to the start of its bucket, on the contract's grid.

    MUST agree with :func:`tripl.core.bucketing.floor_to_bucket` for every
    interval code: these bounds are the window the warehouse's own ``GROUP BY``
    is evaluated over, so a bound off the bucket grid splits a bucket across two
    chunks and lets an incomplete bucket be written as complete. Weeks therefore
    bin from ``WEEK_ORIGIN`` (Monday) like every adapter does; everything else
    bins from ``EPOCH``. The anchor this replaces was 2000-01-01, a *Saturday*,
    so weekly windows opened and closed five days off the buckets they covered.

    Kept on a ``(dt, delta)`` signature rather than an interval code because
    ``_resolve_collection_window`` lives where it does precisely so tests can
    monkey-patch this name; the delta is all its callers hold.
    """
    origin = WEEK_ORIGIN if delta % _WEEK == timedelta(0) else EPOCH
    if dt.tzinfo is None:
        origin = origin.replace(tzinfo=None)
    return origin + ((dt - origin) // delta) * delta


def _ceil_to_interval(dt: datetime, delta: timedelta) -> datetime:
    floored = _floor_to_interval(dt, delta)
    if floored == dt:
        return floored
    return floored + delta


def _parse_task_datetime(value: str) -> datetime:
    """An ISO-8601 task argument as aware UTC; malformed text raises ``ValueError``."""
    return to_utc(datetime.fromisoformat(value))


def _get_active_scan_job(session: Session, scan_config_id: uuid.UUID) -> ScanJob | None:
    """Return the newest pending/running job for a scan config, if any."""
    return session.execute(
        select(ScanJob)
        .where(
            ScanJob.scan_config_id == scan_config_id,
            ScanJob.status.in_(ACTIVE_SCAN_JOB_STATUSES),
        )
        .order_by(ScanJob.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _get_active_scan_jobs(session: Session, scan_config_id: uuid.UUID) -> list[ScanJob]:
    """Return ALL pending/running jobs for a scan config, oldest first.

    The dispatcher must examine every active job, not just the newest: a stuck
    old ``running`` job could otherwise be permanently shadowed by newer pending
    rows and never reaped (the staleness check would only ever look at the fresh
    one). Oldest-first ordering surfaces the genuinely stale job for reaping.
    """
    return list(
        session.execute(
            select(ScanJob)
            .where(
                ScanJob.scan_config_id == scan_config_id,
                ScanJob.status.in_(ACTIVE_SCAN_JOB_STATUSES),
            )
            .order_by(ScanJob.created_at.asc())
        )
        .scalars()
        .all()
    )


def _get_scan_job_activity_at(job: ScanJob) -> datetime:
    activity_times = [
        to_utc(dt) for dt in (job.created_at, job.started_at, job.updated_at) if dt is not None
    ]
    return max(activity_times)


def _fail_stale_active_scan_job(
    session: Session,
    job: ScanJob,
    *,
    now: datetime,
    scan_name: str,
) -> bool:
    activity_at = _get_scan_job_activity_at(job)
    if now - activity_at < STALE_ACTIVE_SCAN_JOB_TIMEOUT:
        return False

    logger.warning(
        "Marking stale active job %s for %r as failed; status=%s, last_activity=%s",
        job.id,
        scan_name,
        job.status,
        activity_at.isoformat(),
    )
    job.status = ScanJobStatus.failed.value
    job.completed_at = now
    job.error_message = (
        "Marked failed by scheduler after "
        f"{int(STALE_ACTIVE_SCAN_JOB_TIMEOUT.total_seconds() // 60)} minutes without progress"
    )
    session.commit()
    return True
