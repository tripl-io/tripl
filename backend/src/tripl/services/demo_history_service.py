"""The second phase of a demo build: the history the demo opened without.

A demo is promoted to ``ready`` with the last two days of its history replayed
and scored (``pipeline.FIRST_BUILD_HISTORY``), which takes seconds; the rest of
``DEMO_HISTORY_DAYS`` is replayed afterwards by ``fill_demo_history`` on the
worker, while the visitor is already looking around. The project says so in
``demo_history_status`` (``filling`` → ``complete``, or ``failed``) and the API
reports the fill's progress from its replay jobs. The fill's scoring pass files
the alerts two days of baseline could not (the volume incidents); they are
queued once the fill is done.

The fill runs on the build's clock (``demo_seeded_at``), so its windows meet
the first build's exactly. It stops quietly when the demo goes away under it —
deleted, or reset (a reset is a new project id with a fill of its own).

On the worker a fill runs in slices of :data:`FILL_SLICE`: when a slice is
spent, the task queues the rest behind whatever waits and returns. A fill takes
minutes and every visitor's demo starts one, so a fill holding its slot to the
end would keep the next visitor's build — and every scan — waiting for it.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import cache
from tripl.core.bucketing import to_utc
from tripl.core.demo_warehouse.calendar import PROMO_EVENT
from tripl.middleware.org_context import OrgRef, bound_org
from tripl.middleware.request_id import current_request_id
from tripl.models.domain_enums import DemoHistoryStatus, ProjectGenerationStatus
from tripl.models.event import Event
from tripl.models.metric_definition import MetricDefinition
from tripl.models.organization import Organization
from tripl.models.project import Project
from tripl.services import plan_branch_service
from tripl.services.demo import DemoContext, pipeline

logger = logging.getLogger(__name__)

DEMO_HISTORY_FILL_FAILED_EVENT = "demo.history_fill.failed"

#: How long a ``filling`` demo refuses a manual run or replay without a sign of
#: life from the fill. A fill whose worker died must not lock the scan's Run
#: button forever; a live one commits a replay job at least every few minutes.
HISTORY_FILL_STALL = timedelta(hours=1)

#: How long one fill task works before it queues the rest and frees its worker
#: slot. A replay job is never cut: the slice ends at the first job boundary
#: past it.
FILL_SLICE = timedelta(seconds=30)


class _Gone(Exception):
    """The demo is no longer the one this fill was for (deleted or reset)."""


class _Continued(Exception):
    """The slice is spent and the rest of the fill is queued."""


def needs_fill(now: datetime) -> bool:
    """Whether a demo built at ``now`` has history beyond the first build's."""
    return bool(pipeline.fill_windows(now))


async def finish_demo_history(
    session: AsyncSession, project_id: uuid.UUID, *, budget: timedelta | None = None
) -> str:
    """Fill a ready demo's history; returns ``complete``, ``continued``, ``failed`` or ``skipped``.

    ``skipped`` when there is nothing to fill: the project is gone, is not a
    ready demo, or is not ``filling`` (a redelivered task after the fill ended).
    A redelivered task while it is still ``filling`` resumes it: the replay
    windows a completed job already covers are not replayed again.

    With a ``budget`` (the worker passes :data:`FILL_SLICE`), the first replay
    job to end past it queues the rest of the fill as a task of its own and
    the call returns ``continued``; a broker that refuses it leaves this call
    to finish the fill itself.
    """
    project = await session.get(Project, project_id)
    if not _is_filling(project):
        return "skipped"
    assert project is not None
    seeded_at = project.demo_seeded_at
    deliveries: list[str] = []
    deadline = None if budget is None else time.monotonic() + budget.total_seconds()

    async def between_jobs() -> None:
        nonlocal deadline
        await _raise_if_gone(session, project_id)
        if deadline is not None and time.monotonic() >= deadline:
            deadline = None
            if await _queue_the_rest(session, project_id):
                raise _Continued

    org_slug = await session.scalar(
        select(Organization.slug).where(Organization.id == project.organization_id)
    )
    with bound_org(OrgRef(id=project.organization_id, slug=org_slug or "")):
        ctx = await _fill_context(session, project)
        if ctx is None or seeded_at is None:
            await set_fill_status(session, project_id, DemoHistoryStatus.complete)
            return "complete"
        try:
            await pipeline.fill_history(
                session, ctx, deliveries=deliveries, between_jobs=between_jobs
            )
        except _Gone:
            await session.rollback()
            return "skipped"
        except _Continued:
            return "continued"
        except Exception as exc:
            await session.rollback()
            if not _is_filling(await _fresh(session, project_id)):
                return "skipped"
            logger.warning(
                "%s project_id=%s error=%s",
                DEMO_HISTORY_FILL_FAILED_EVENT,
                project_id,
                type(exc).__name__,
                exc_info=exc,
            )
            await set_fill_status(session, project_id, DemoHistoryStatus.failed)
            return "failed"
    await set_fill_status(session, project_id, DemoHistoryStatus.complete)
    # Looked up at call time: the seam a suite without a broker replaces.
    from tripl.services import demo_service

    await demo_service.enqueue_alert_deliveries(deliveries)
    return "complete"


async def fill_progress(session: AsyncSession, project: Project) -> int | None:
    """0-100 through a ``filling`` demo's history fill; None otherwise."""
    if not _is_filling(project) or project.demo_seeded_at is None:
        return None
    config_id = await pipeline.demo_scan_config_id(session, project.id)
    if config_id is None:
        return 0
    summaries = await pipeline.replay_summaries(session, config_id)
    return pipeline.fill_progress(summaries, to_utc(project.demo_seeded_at))


async def fill_in_progress(session: AsyncSession, project_id: uuid.UUID) -> bool:
    """Whether a live history fill owns the project's scan (a manual run must wait).

    ``filling`` alone is not enough: a fill whose worker died leaves the status
    behind, so a fill counts as live only while its newest replay job, or the
    build itself, is younger than :data:`HISTORY_FILL_STALL`.
    """
    project = await session.get(Project, project_id)
    if not _is_filling(project):
        return False
    assert project is not None
    from tripl.models.scan_config import ScanConfig
    from tripl.models.scan_job import ScanJob

    newest = await session.scalar(
        select(ScanJob.updated_at)
        .join(ScanConfig, ScanConfig.id == ScanJob.scan_config_id)
        .where(ScanConfig.project_id == project_id)
        .order_by(ScanJob.updated_at.desc())
        .limit(1)
    )
    markers = [m for m in (newest, project.demo_seeded_at) if m is not None]
    if not markers:
        return False
    return datetime.now(UTC) - max(to_utc(m) for m in markers) < HISTORY_FILL_STALL


def _is_filling(project: Project | None) -> bool:
    return (
        project is not None
        and project.is_demo
        and project.generation_status == ProjectGenerationStatus.ready.value
        and project.demo_history_status == DemoHistoryStatus.filling.value
    )


async def _fresh(session: AsyncSession, project_id: uuid.UUID) -> Project | None:
    project = await session.get(Project, project_id)
    if project is not None:
        await session.refresh(project)
    return project


async def _raise_if_gone(session: AsyncSession, project_id: uuid.UUID) -> None:
    if not _is_filling(await _fresh(session, project_id)):
        raise _Gone


async def _queue_the_rest(session: AsyncSession, project_id: uuid.UUID) -> bool:
    """Hand the rest of the fill to a task of its own; False if the broker refuses."""
    # Looked up at call time: the seam a suite without a broker replaces.
    from tripl.services import demo_service

    try:
        await demo_service.enqueue_demo_history_fill(session, project_id, current_request_id())
    except Exception:
        logger.warning(
            "demo.history_fill.requeue_failed project_id=%s error=broker_unavailable",
            project_id,
            exc_info=True,
        )
        return False
    return True


async def _fill_context(session: AsyncSession, project: Project) -> DemoContext | None:
    """What the fill needs of the build, read back from what the build wrote."""
    config_id = await pipeline.demo_scan_config_id(session, project.id)
    if config_id is None or project.demo_seeded_at is None:
        return None
    branch_id = await plan_branch_service.ensure_main_branch_id(session, project.id)
    ctx = DemoContext(
        project_id=project.id,
        branch_id=branch_id,
        slug=project.slug,
        now=to_utc(project.demo_seeded_at),
        created_by=project.created_by_user_id,
        scan_config_id=config_id,
    )
    promo_id = await session.scalar(
        select(Event.id).where(
            Event.project_id == project.id,
            Event.branch_id == branch_id,
            Event.name == PROMO_EVENT,
        )
    )
    if promo_id is not None:
        ctx.event_ids[PROMO_EVENT] = promo_id
    metrics = await session.execute(
        select(MetricDefinition.name, MetricDefinition.id).where(
            MetricDefinition.project_id == project.id
        )
    )
    ctx.metric_ids.update({name: metric_id for name, metric_id in metrics.tuples()})
    return ctx


async def set_fill_status(
    session: AsyncSession, project_id: uuid.UUID, status: DemoHistoryStatus
) -> None:
    """Move a ``filling`` demo on; a demo no longer filling is left alone."""
    await session.execute(
        update(Project)
        .where(
            Project.id == project_id,
            Project.demo_history_status == DemoHistoryStatus.filling.value,
        )
        .values(demo_history_status=status.value)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    await cache.delete_prefix(cache.prefix_projects())
