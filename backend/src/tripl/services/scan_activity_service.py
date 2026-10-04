"""Per-scan run activity for the Scans list, aggregated in SQL.

The list used to derive three figures from a capped page of each scan's jobs —
the last run, the current failing streak, and the rows read in the last 24 hours
— so a scan that ran more often than the page held showed the streak and the
24h total as floors ("failed last 10+ runs", "1.2M+"). Loading every job to make
them exact is what tripl-jfm3.107 capped in the first place, so each figure is
one grouped query here instead, over every scan config in the project at once.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Float, ScalarSelect, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.schemas.scan_job import ScanActivityItem, ScanActivityResponse, ScanJobResponse
from tripl.services.project_lookup import resolve_project_id

ACTIVITY_WINDOW = timedelta(hours=24)

# How long before the window a job can have been CREATED and still finish inside
# it. A run is bounded by Celery's hard time limit (60 min) and a queued or
# running job older than STALE_ACTIVE_SCAN_JOB_TIMEOUT (75 min) is reaped, so a
# day is a wide margin. It turns the 24h sum into a range scan on
# ``ix_scan_job_config_created`` instead of a read of every retained job.
_JOB_LIFETIME_MARGIN = timedelta(days=1)

# A settled job that is not a failure ends a failing streak. Queued and running
# jobs are looked past, as the frontend's ``consecutiveFailedRuns`` does: a retry
# waiting behind five failures must not hide them.
_STREAK_BREAKING_STATUSES = (ScanJobStatus.completed.value, ScanJobStatus.cancelled.value)

# Every query below is per scan config and walks ``ix_scan_job_config_created``
# (scan_config_id, created_at) from its newest end, so a request costs a few
# index probes per scan however much history is retained — the endpoint is
# refetched every 10 s while a scan runs, and a window function or an
# unindexed filter over 90 days of jobs would read all of it every time.


def _newest_job_id() -> ScalarSelect[Any]:
    """This scan's newest job, correlated to the enclosing ``ScanConfig`` row."""
    return (
        select(ScanJob.id)
        .where(ScanJob.scan_config_id == ScanConfig.id)
        .order_by(ScanJob.created_at.desc(), ScanJob.id.desc())
        .limit(1)
        .correlate(ScanConfig)
        .scalar_subquery()
    )


def _newest_break_at() -> ScalarSelect[Any]:
    """When this scan's newest completed or cancelled job was created, if any.

    Correlated to the enclosing ``ScanConfig`` row.
    """
    return (
        select(ScanJob.created_at)
        .where(
            ScanJob.scan_config_id == ScanConfig.id,
            ScanJob.status.in_(_STREAK_BREAKING_STATUSES),
        )
        .order_by(ScanJob.created_at.desc())
        .limit(1)
        .correlate(ScanConfig)
        .scalar_subquery()
    )


async def _latest_jobs(
    session: AsyncSession, scan_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ScanJob]:
    newest = select(_newest_job_id().label("job_id")).where(ScanConfig.id.in_(scan_ids)).subquery()
    rows = await session.execute(select(ScanJob).join(newest, newest.c.job_id == ScanJob.id))
    return {job.scan_config_id: job for job in rows.scalars().all()}


async def _failing_streaks(
    session: AsyncSession, scan_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Failed jobs newer than each scan's newest completed or cancelled job."""
    breaks = (
        select(
            ScanConfig.id.label("scan_config_id"),
            _newest_break_at().label("created_at"),
        )
        .where(ScanConfig.id.in_(scan_ids))
        .subquery()
    )
    rows = await session.execute(
        select(ScanJob.scan_config_id, func.count())
        .join(breaks, breaks.c.scan_config_id == ScanJob.scan_config_id)
        .where(
            ScanJob.status == ScanJobStatus.failed.value,
            or_(breaks.c.created_at.is_(None), ScanJob.created_at > breaks.c.created_at),
        )
        .group_by(ScanJob.scan_config_id)
    )
    return {scan_id: int(count or 0) for scan_id, count in rows.all()}


@dataclass(frozen=True)
class _RowsRead:
    total: int = 0
    warehouse_rows: int = 0
    catalog_combinations: int = 0


async def _rows_read_since(
    session: AsyncSession, scan_ids: list[uuid.UUID], window_from: datetime
) -> dict[uuid.UUID, _RowsRead]:
    """Sum of each job's rows read, for jobs stamped inside the window.

    A job's stamp is ``completed_at``, else ``started_at`` — the one the list
    used — and its rows are ``query_rows_scanned``, else
    ``catalog_rows_scanned``, else ``scan_rows_processed`` (``jobRowsScanned``
    in the frontend). Extracted as floats: the counters are JSON numbers, and a
    BigQuery run can read past a 32-bit integer.

    The counters are not the same unit: ``query_rows_scanned`` is warehouse
    rows a metrics run read, ``catalog_rows_scanned`` the warehouse rows behind
    a catalog run's breakdown (newer runs only), and ``scan_rows_processed`` the
    GROUP BY ALL combinations a catalog run got back. The mixed total stays for
    older clients; the split sums name their unit (B15), and a catalog run that
    reports warehouse rows counts there rather than as combinations.
    """
    summary = ScanJob.result_summary
    warehouse = func.coalesce(
        summary["query_rows_scanned"].as_float(),
        summary["catalog_rows_scanned"].as_float(),
    )
    combinations = summary["scan_rows_processed"].as_float()
    zero = cast(0, Float)
    rows_read = func.coalesce(warehouse, combinations, zero)
    # Only counted when the job reported no warehouse rows, the same precedence
    # as the mixed total, so the two split sums add up to it.
    catalog_only = case((warehouse.is_(None), func.coalesce(combinations, zero)), else_=zero)
    rows = await session.execute(
        select(
            ScanJob.scan_config_id,
            func.sum(rows_read),
            func.sum(func.coalesce(warehouse, zero)),
            func.sum(catalog_only),
        )
        .where(
            ScanJob.scan_config_id.in_(scan_ids),
            # Sargable bound first (see _JOB_LIFETIME_MARGIN); the exact stamp
            # test then runs on the day or so of jobs it leaves.
            ScanJob.created_at >= window_from - _JOB_LIFETIME_MARGIN,
            func.coalesce(ScanJob.completed_at, ScanJob.started_at) >= window_from,
        )
        .group_by(ScanJob.scan_config_id)
    )
    return {
        scan_id: _RowsRead(
            total=round(total or 0),
            warehouse_rows=round(warehouse_total or 0),
            catalog_combinations=round(combination_total or 0),
        )
        for scan_id, total, warehouse_total, combination_total in rows.all()
    }


_NO_ROWS = _RowsRead()


async def get_scan_activity(
    session: AsyncSession, slug: str, *, now: datetime
) -> ScanActivityResponse:
    """Every scan config's latest job, failing streak and 24h rows read.

    Items follow the scan list's own order (newest config first), and a config
    that never ran is present with no job, a zero streak and zero rows.
    """
    project_id = await resolve_project_id(session, slug)
    scan_ids = list(
        (
            await session.scalars(
                select(ScanConfig.id)
                .where(ScanConfig.project_id == project_id)
                .order_by(ScanConfig.created_at.desc())
            )
        ).all()
    )
    window_from = now - ACTIVITY_WINDOW
    if not scan_ids:
        return ScanActivityResponse(window_from=window_from, window_to=now, items=[])

    latest = await _latest_jobs(session, scan_ids)
    streaks = await _failing_streaks(session, scan_ids)
    rows_read = await _rows_read_since(session, scan_ids, window_from)
    items = [
        ScanActivityItem(
            scan_config_id=scan_id,
            latest_job=(
                ScanJobResponse.model_validate(latest[scan_id]) if scan_id in latest else None
            ),
            failing_streak=streaks.get(scan_id, 0),
            rows_read_24h=rows_read.get(scan_id, _NO_ROWS).total,
            warehouse_rows_24h=rows_read.get(scan_id, _NO_ROWS).warehouse_rows,
            catalog_combinations_24h=rows_read.get(scan_id, _NO_ROWS).catalog_combinations,
        )
        for scan_id in scan_ids
    ]
    return ScanActivityResponse(window_from=window_from, window_to=now, items=items)
