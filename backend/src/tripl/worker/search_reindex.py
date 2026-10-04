from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from tripl.config import settings
from tripl.db_config import postgres_connect_args
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.project import Project
from tripl.services.search_service import reindex_project_branch

logger = logging.getLogger(__name__)


def reindex_main_branch_from_worker(session: Session, project_id: uuid.UUID) -> None:
    """Rebuild the project's MAIN branch index, as every scan and collection does.

    This is the path that makes main self-healing: because it runs after each
    catalog refresh, a change to how documents are BUILT reaches main within the
    hour without anybody asking for it. Working branches have no equivalent
    trigger, which is what :func:`reindex_branch_from_worker` and the staleness
    sweep in ``worker/tasks/search.py`` exist to cover.
    """
    branch_id = session.scalar(
        select(PlanBranch.id).where(
            PlanBranch.project_id == project_id,
            PlanBranch.kind == BranchKind.main.value,
        )
    )
    if branch_id is None:
        return
    reindex_branch_from_worker(session, project_id, branch_id)


def reindex_branch_from_worker(
    session: Session, project_id: uuid.UUID, branch_id: uuid.UUID
) -> None:
    bind = session.bind
    if bind is None or bind.dialect.name != "postgresql":
        return

    project = session.get(Project, project_id)
    if project is None:
        return

    slug = project.slug

    async def _run() -> None:
        # A Celery prefork worker is long-lived and reaches this via a fresh
        # asyncio.run() (a brand-new event loop) on every scan/metrics refresh.
        # asyncpg binds each connection to the loop that opened it, so reusing
        # the module-global pooled async engine would hand a later loop a
        # connection created under an already-closed loop ("got Future attached
        # to a different loop"). Build a throwaway NullPool engine whose
        # connections live and die entirely inside this loop, and dispose it
        # before the loop closes. See tripl.db_config for the documented
        # async(api)/sync(Celery)/async-NullPool(worker-bridge) split — this is
        # the worker-bridge case, so it intentionally skips the shared pool
        # constants and uses NullPool.
        engine = create_async_engine(
            settings.database_url,
            poolclass=NullPool,
            connect_args=postgres_connect_args(settings.database_url),
        )
        try:
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as async_db:
                await reindex_project_branch(
                    async_db,
                    project_id=project_id,
                    branch_id=branch_id,
                    slug=slug,
                )
        finally:
            await engine.dispose()

    try:
        asyncio.run(_run())
    except Exception:
        logger.exception(
            "Failed to reindex search documents for project %s branch %s",
            project_id,
            branch_id,
        )
