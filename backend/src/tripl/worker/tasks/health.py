"""Daily plan health snapshot (F15, #268).

``snapshot_project_health`` scores every project's main plan once a day and
upserts one ``project_health_snapshots`` row per (project, UTC day). The
Overview trend and the weekly digest's "Plan health" line read those rows; the
digest never scores inline. Rows older than ``SNAPSHOT_RETENTION_DAYS`` are
deleted in the same run.

Scoring reuses the async ``event_health_service``, so the task runs it through
``run_with_async_worker_session`` (a throwaway NullPool engine per invocation).
Each project is isolated: one failing project is logged and skipped, the rest
still get their row. Demo projects are included — the task has no egress.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.project import Project
from tripl.models.project_health_snapshot import ProjectHealthSnapshot
from tripl.services import event_health_service
from tripl.services.active_org_scope import project_in_active_org
from tripl.services.health_weights import SNAPSHOT_RETENTION_DAYS
from tripl.worker.celery_app import celery_app
from tripl.worker.db import run_with_async_worker_session

logger = logging.getLogger(__name__)


async def upsert_snapshot(
    session: AsyncSession,
    project_id: uuid.UUID,
    day: date,
    summary: event_health_service.HealthAggregate,
) -> ProjectHealthSnapshot:
    """Write ``summary`` as the project's row for ``day``, replacing an earlier one."""
    values = event_health_service.snapshot_payload(summary)
    existing: ProjectHealthSnapshot | None = await session.scalar(
        select(ProjectHealthSnapshot).where(
            ProjectHealthSnapshot.project_id == project_id,
            ProjectHealthSnapshot.day == day,
        )
    )
    if existing is None:
        existing = ProjectHealthSnapshot(project_id=project_id, day=day, **values)
        session.add(existing)
    else:
        for column, value in values.items():
            setattr(existing, column, value)
    await session.flush()
    return existing


async def snapshot_all_projects(session: AsyncSession, now: datetime) -> dict[str, int]:
    """Snapshot every project for ``now``'s UTC day and prune old rows."""
    today = now.astimezone(UTC).date()
    project_ids = list(
        (await session.execute(select(Project.id).where(project_in_active_org()))).scalars().all()
    )
    written = failed = 0
    for project_id in project_ids:
        try:
            summary = await event_health_service.project_summary_for_snapshot(
                session, project_id, now
            )
            await upsert_snapshot(session, project_id, today, summary)
            await session.commit()
            written += 1
        except Exception:
            await session.rollback()
            failed += 1
            logger.exception("Health snapshot failed for project %s", project_id)
    cutoff = today - timedelta(days=SNAPSHOT_RETENTION_DAYS)
    pruned = await session.execute(
        delete(ProjectHealthSnapshot).where(ProjectHealthSnapshot.day < cutoff)
    )
    await session.commit()
    return {
        "written": written,
        "failed": failed,
        "pruned": int(getattr(pruned, "rowcount", 0) or 0),
    }


@celery_app.task(name="tripl.worker.tasks.health.snapshot_project_health")  # type: ignore[untyped-decorator]
def snapshot_project_health() -> dict[str, int]:
    stats: dict[str, int] = {}

    async def _run(session: AsyncSession) -> None:
        stats.update(await snapshot_all_projects(session, datetime.now(UTC)))

    asyncio.run(run_with_async_worker_session(_run))
    return stats
