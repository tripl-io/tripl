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

One fill of a project runs at a time: a slice holds the project's fill lock
(:func:`fill_lock_key`), and a task that finds it taken leaves the fill to its
holder. Any error marks the fill ``failed``; a worker that dies outright leaves
it ``filling``, and the stall sweep (``check_metrics_due``) re-queues it, or
fails it after :data:`HISTORY_FILL_MAX_REQUEUES` tries
(:func:`stalled_fill_action`).
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

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
from tripl.models.scan_job import ScanJobStatus
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

#: How many times the stall sweep re-queues a fill before it marks it ``failed``.
HISTORY_FILL_MAX_REQUEUES = 3

# Tag mixed into the per-project fill lock key, so it is stable across releases
# and apart from the other per-project keys (see auth_service.owner_set_lock_key).
_FILL_LOCK_TAG = b"trplfil1"


class _Gone(Exception):
    """The demo is no longer the one this fill was for (deleted or reset)."""


class _Continued(Exception):
    """The slice is spent and the rest of the fill is queued."""


class _Busy(Exception):
    """Another fill task of the project holds its lock; that one goes on."""


def fill_lock_key(project_id: uuid.UUID) -> int:
    """The signed 64-bit advisory-lock key of ``project_id``'s history fill."""
    digest = hashlib.blake2b(_FILL_LOCK_TAG + project_id.bytes, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


class _FillLock:
    """A project's fill lock: a session advisory lock in tripl's database.

    On a connection of its own (AUTOCOMMIT), so the fill's commits and
    rollbacks never release it, and a worker that dies releases it with its
    connection. None off PostgreSQL (SQLite in tests): acquiring succeeds.
    """

    def __init__(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        self._bind = session.bind
        self._key = fill_lock_key(project_id)
        self._conn: AsyncConnection | None = None

    async def acquire(self) -> bool:
        bind = self._bind
        if bind is None or bind.dialect.name != "postgresql":
            return True
        engine = bind if isinstance(bind, AsyncEngine) else bind.engine
        conn = await engine.connect()
        try:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            taken = bool(
                await conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": self._key})
            )
        except BaseException:
            await conn.close()
            raise
        if not taken:
            await conn.close()
            return False
        self._conn = conn
        return True

    async def release(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self._key})
        except Exception:  # best effort: closing the connection releases it too
            logger.warning("demo.history_fill.unlock_failed", exc_info=True)
        finally:
            await conn.close()


def needs_fill(now: datetime) -> bool:
    """Whether a demo built at ``now`` has history beyond the first build's."""
    return bool(pipeline.fill_windows(now))


async def finish_demo_history(
    session: AsyncSession, project_id: uuid.UUID, *, budget: timedelta | None = None
) -> str:
    """Fill a ready demo's history.

    Returns ``complete``, ``continued``, ``failed``, ``busy`` or ``skipped``.
    ``skipped`` when there is nothing to fill: the project is gone, is not a
    ready demo, or is not ``filling`` (a redelivered task after the fill ended).
    ``busy`` when another task of the project's fill holds its lock: that one
    goes on. A redelivered task while it is still ``filling`` resumes it: the
    replay windows a completed job already covers are not replayed again.

    With a ``budget`` (the worker passes :data:`FILL_SLICE`), the first replay
    job to end past it queues the rest of the fill as a task of its own and
    the call returns ``continued``; a broker that refuses it leaves this call
    to finish the fill itself.

    Any error marks the fill ``failed`` (unless the demo went away meanwhile),
    wherever it is raised: a demo is never left ``filling`` by an exception.
    """
    if not _is_filling(await session.get(Project, project_id)):
        return "skipped"
    lock = _FillLock(session, project_id)
    if not await lock.acquire():
        return "busy"
    try:
        return await _fill(session, project_id, budget=budget, lock=lock)
    except _Gone:
        await session.rollback()
        return "skipped"
    except _Continued:
        return "continued"
    except _Busy:
        return "busy"
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
    finally:
        await lock.release()


async def _fill(
    session: AsyncSession, project_id: uuid.UUID, *, budget: timedelta | None, lock: _FillLock
) -> str:
    """:func:`finish_demo_history` under the project's fill lock."""
    # Read again under the lock: the task that held it may have just finished
    # the fill, and its last steps (the scoring pass, the deliveries) must not
    # run twice.
    project = await _fresh(session, project_id)
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
            # Free the lock first, so the task queued next finds it free.
            await lock.release()
            if await _queue_the_rest(session, project_id):
                raise _Continued
            if not await lock.acquire():
                raise _Busy

    org_slug = await session.scalar(
        select(Organization.slug).where(Organization.id == project.organization_id)
    )
    with bound_org(OrgRef(id=project.organization_id, slug=org_slug or "")):
        ctx = await _fill_context(session, project)
        if ctx is None or seeded_at is None:
            await set_fill_status(session, project_id, DemoHistoryStatus.complete)
            return "complete"
        await pipeline.fill_history(session, ctx, deliveries=deliveries, between_jobs=between_jobs)
    if not await set_fill_status(session, project_id, DemoHistoryStatus.complete):
        return "skipped"
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
    summaries = await pipeline.replay_summaries(
        session, config_id, statuses=(ScanJobStatus.completed, ScanJobStatus.running)
    )
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


def stalled_fill_action(
    last_activity: datetime, attempts: int, now: datetime
) -> Literal["wait", "requeue", "fail"]:
    """What the stall sweep does with a ``filling`` demo last alive at ``last_activity``.

    ``last_activity`` is the newest of the build and its scan's jobs (as in
    :func:`fill_in_progress`); ``attempts`` the re-queues already made. Each
    re-queue waits one :data:`HISTORY_FILL_STALL` longer than the one before,
    so a fill merely queued behind other work is not queued twice, and one
    that dies the same way every time is failed after
    :data:`HISTORY_FILL_MAX_REQUEUES`, not retried forever.
    """
    if now - to_utc(last_activity) < HISTORY_FILL_STALL * (attempts + 1):
        return "wait"
    return "requeue" if attempts < HISTORY_FILL_MAX_REQUEUES else "fail"


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
) -> bool:
    """Move a ``filling`` demo on; a demo no longer filling is left alone (False)."""
    result = await session.execute(
        update(Project)
        .where(
            Project.id == project_id,
            Project.demo_history_status == DemoHistoryStatus.filling.value,
        )
        .values(demo_history_status=status.value)
        .execution_options(synchronize_session=False)
    )
    moved = bool(getattr(result, "rowcount", 0))
    await session.commit()
    await cache.delete_prefix(cache.prefix_projects())
    return moved
