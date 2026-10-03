"""Purge an organization marked ``deleting`` (F20 PR6, critique #26).

``DELETE /api/v1/orgs/{org}`` marks the row and queues this task; the work is
:func:`tripl.services.org_deletion_service.purge_organization`, run through
``run_with_async_worker_session`` like the other tasks that reuse async
services. Idempotent: a re-delivered or retried message finds the row gone (or
half-purged) and finishes the job. :func:`requeue_stranded_org_deletions` is the
hourly chaser for a purge that ran out of retries or was never delivered.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.services import org_deletion_service, org_idle_service
from tripl.worker.celery_app import celery_app
from tripl.worker.db import run_with_async_worker_session

logger = logging.getLogger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.org_delete.purge_organization",
    bind=True,
    max_retries=3,
)
def purge_organization(self: Any, org_id: str) -> dict[str, object]:
    result: dict[str, object] = {"purged": False}

    async def _run(session: AsyncSession) -> None:
        purged = await org_deletion_service.purge_organization(session, uuid.UUID(org_id))
        if purged is not None:
            result.update(
                purged=True,
                slug=purged.slug,
                projects=purged.projects,
                blobs_deleted=purged.blobs_deleted,
                blobs_failed=purged.blobs_failed,
            )

    try:
        asyncio.run(run_with_async_worker_session(_run))
    except Exception as exc:
        logger.exception("organization purge failed for %s", org_id)
        # Backing off (1, 2, 4 min) gives a database blip or a concurrent delete
        # time to clear. After the last retry the chaser below takes over.
        raise self.retry(exc=exc, countdown=60 * 2**self.request.retries) from exc
    return result


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.org_delete.requeue_stranded_org_deletions",
)
def requeue_stranded_org_deletions() -> dict[str, object]:
    """Queue the purge again for ``deleting`` organizations no job is working on.

    Without it an organization whose purge ran out of retries (or whose message
    was lost) would stay ``deleting`` for good: its slug taken, its data kept,
    and no way for its owner to retry, since it answers 404.
    """
    claimed: list[uuid.UUID] = []

    async def _run(session: AsyncSession) -> None:
        claimed.extend(await org_deletion_service.requeue_stranded_deletions(session))

    asyncio.run(run_with_async_worker_session(_run))
    queued: list[str] = []
    for org_id in claimed:
        try:
            purge_organization.delay(str(org_id))
        except Exception:  # noqa: BLE001 - claimed again after the grace period
            logger.exception("could not re-queue the purge of organization %s", org_id)
            continue
        queued.append(str(org_id))
    if queued:
        logger.warning("re-queued stranded organization purges: %s", ", ".join(queued))
    return {"requeued": queued}


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.org_delete.retire_idle_organizations",
)
def retire_idle_organizations() -> dict[str, object]:
    """Delete organizations nobody has used for ``IDLE_ORG_RETENTION_DAYS`` (tripl-sav5.5).

    Off unless the setting is above zero on a hosted instance. Each one goes
    through the owner-delete purge; a purge that cannot be queued now is picked
    up by :func:`requeue_stranded_org_deletions`.
    """
    retired: list[uuid.UUID] = []

    async def _run(session: AsyncSession) -> None:
        retired.extend(await org_idle_service.retire_idle_organizations(session))

    asyncio.run(run_with_async_worker_session(_run))
    for org_id in retired:
        try:
            purge_organization.delay(str(org_id))
        except Exception:  # noqa: BLE001 - the chaser queues it after the grace period
            logger.exception("could not queue the purge of idle organization %s", org_id)
    if retired:
        logger.info("retired %d idle organizations", len(retired))
    # Accounts an earlier day's purges left in no organization. Today's retired
    # organizations are still being purged, so their members go on a later run.
    orphans: list[int] = []

    async def _sweep(session: AsyncSession) -> None:
        orphans.append(await org_idle_service.delete_orphan_accounts(session))

    asyncio.run(run_with_async_worker_session(_sweep))
    if orphans[0]:
        logger.info("deleted %d accounts left in no organization", orphans[0])
    return {"retired": [str(org_id) for org_id in retired], "accounts_deleted": orphans[0]}
