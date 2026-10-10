"""Seed a demo project on the worker (phase 2 of demo provisioning).

``POST /projects/demo`` commits a hidden ``seeding`` shell and returns at once;
this task seeds it and promotes it to ``ready`` (or marks it ``failed``) through
:func:`tripl.services.demo_service.finish_demo_provision`. The seed is written
on the async services, so the task bridges into them with a throwaway async
engine per run.

Idempotent: a redelivered task, or one whose shell a cancel already removed,
finds no seeding shell and does nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.middleware.request_id import bound_request_id
from tripl.services import demo_service
from tripl.worker.celery_app import celery_app
from tripl.worker.db import run_with_async_worker_session

logger = logging.getLogger(__name__)

TASK_NAME = "tripl.worker.tasks.demo_provision.seed_demo_project"


@celery_app.task(name=TASK_NAME)  # type: ignore[untyped-decorator]
def seed_demo_project(project_id: str, request_id: str | None = None) -> str:
    """Seed one demo shell; returns ``ready``, ``failed``, ``cancelled`` or ``skipped``.

    ``request_id`` is the id of the request that created the shell, bound here
    so the seed's log lines correlate with it.
    """
    outcome = "skipped"

    async def _run(session: AsyncSession) -> None:
        nonlocal outcome
        outcome = await demo_service.finish_demo_provision(session, uuid.UUID(project_id))

    with bound_request_id(request_id) if request_id else contextlib.nullcontext():
        asyncio.run(run_with_async_worker_session(_run))
    logger.info("demo.provision.%s project_id=%s", outcome, project_id)
    return outcome
