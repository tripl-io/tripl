"""Deleting an organization: mark it, then purge it in a Celery job (F20 PR6, critique #26).

A cascade cannot do it. Metric-scope anomalies have no foreign key to anything
(see ``project_service.purge_project_rows``), photo blobs live in object
storage, and ``projects``, ``data_sources``, ``api_keys`` and ``invitations``
reference the organization ``ON DELETE RESTRICT`` on purpose. So:

1. :func:`request_deletion` (the ``DELETE /orgs/{org}`` request) flips the row
   to ``deleting``. From that commit on every read of the organization answers
   404 (``org_resolution.ORG_IS_ACTIVE``): its URLs, its API keys, its members'
   ``/auth/me`` list, its invitation links.
2. :func:`purge_organization` (the ``org_delete.purge_organization`` task) purges
   each project through ``purge_project_rows`` and deletes its photo blobs, then
   the organization's data sources, API keys, invitations, docs, settings rows
   and memberships, then the row, and files ``org.delete_complete`` at platform
   scope (``organization_id`` NULL, the slug in the payload): the organization
   the row would belong to no longer exists.

The purge is idempotent: a job that died half-way is simply run again, each
project committed as it goes. A job that ran out of retries, or a message the
broker lost, is picked up by :func:`requeue_stranded_deletions`, run hourly by
the ``org_delete.requeue_stranded_org_deletions`` beat task.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import cache
from tripl.models.api_key import ApiKey
from tripl.models.app_setting import AppSetting
from tripl.models.data_source import DataSource
from tripl.models.doc_file import DocFile
from tripl.models.domain_enums import EventPhotoKind, OrganizationStatus
from tripl.models.event_photo import EventPhoto
from tripl.models.invitation import Invitation
from tripl.models.organization import DEFAULT_ORG_ID, Organization, OrganizationMember
from tripl.models.project import Project
from tripl.models.user import User
from tripl.services import audit_service, project_service
from tripl.storage import storage_for

logger = logging.getLogger(__name__)

_PHOTO_KIND = EventPhotoKind.photo.value

#: How long a ``deleting`` organization may go untouched before the chaser
#: queues its purge again. Above the Celery hard time limit (60 min), so a job
#: still running is never doubled, and each purge touches the row as it goes.
STRANDED_DELETION_GRACE = timedelta(hours=2)


class DefaultOrgUndeletableError(Exception):
    """The default organization cannot be deleted."""


class ConfirmationMismatchError(Exception):
    """The typed confirmation is not the organization's slug."""


@dataclass(frozen=True)
class PurgeResult:
    slug: str
    projects: int
    blobs_deleted: int
    blobs_failed: int


async def request_deletion(
    session: AsyncSession, *, org_id: uuid.UUID, slug: str, confirm_slug: str
) -> None:
    """Mark the organization ``deleting``. Does NOT commit (the caller audits first)."""
    if org_id == DEFAULT_ORG_ID:
        raise DefaultOrgUndeletableError
    if confirm_slug != slug:
        raise ConfirmationMismatchError
    org = await session.get(Organization, org_id)
    if org is None:  # pragma: no cover - the gate resolved it in this request
        raise LookupError(org_id)
    org.status = OrganizationStatus.deleting.value
    await session.flush()


async def cancel_deletion(session: AsyncSession, org_id: uuid.UUID, *, user: User) -> None:
    """Put a ``deleting`` organization back, when its purge job could not be queued.

    The ``org.delete_request`` row is already committed (the request must be
    durable before the job is queued, or the worker could find the row still
    ``active`` and skip it), so the undo is filed as ``org.delete_cancel`` in
    the same commit that restores the status: the organization's feed never
    shows a deletion request for an organization that is still there without
    also showing that it was called off.
    """
    org = await session.get(Organization, org_id)
    if org is None or org.status != OrganizationStatus.deleting.value:
        return
    org.status = OrganizationStatus.active.value
    await audit_service.record(
        session,
        user=user,
        action="org.delete_cancel",
        target_type="organization",
        target_id=org.id,
        target_name=org.slug,
        payload={"slug": org.slug, "reason": "the purge job could not be queued"},
        organization_id=org.id,
        commit=False,
    )
    await session.commit()


async def purge_organization(session: AsyncSession, org_id: uuid.UUID) -> PurgeResult | None:
    """Remove everything the organization owns, then the organization. Commits.

    ``None`` when there is nothing to do: the row is gone (a re-delivered job),
    or it is not ``deleting`` (a job for an organization whose deletion was
    cancelled). The default organization is never purged.
    """
    org = await session.get(Organization, org_id)
    if org is None or org.id == DEFAULT_ORG_ID or org.status != OrganizationStatus.deleting.value:
        return None
    slug, name = org.slug, org.name
    # A heartbeat: the stranded-deletion chaser leaves a row alone while a job
    # keeps touching it, so it never queues a second purge beside a live one.
    await _touch(session, org_id)
    await session.commit()

    projects = list(
        (await session.scalars(select(Project).where(Project.organization_id == org_id))).all()
    )
    blobs_deleted = blobs_failed = 0
    for project in projects:
        project_id = project.id
        blobs = await _photo_blobs(session, project_id)
        await project_service.purge_project_rows(session, project)
        await _touch(session, org_id)
        await session.commit()
        deleted, failed = await _delete_blobs(blobs)
        blobs_deleted += deleted
        blobs_failed += failed
        await _forget_project(project_id)

    # Workspace-wide sources (project-owned ones went with their project), then
    # the RESTRICT references, then what would cascade anyway, spelled out.
    await session.execute(delete(DataSource).where(DataSource.organization_id == org_id))
    await session.execute(delete(ApiKey).where(ApiKey.organization_id == org_id))
    await session.execute(delete(Invitation).where(Invitation.organization_id == org_id))
    await session.execute(delete(DocFile).where(DocFile.organization_id == org_id))
    await session.execute(delete(AppSetting).where(AppSetting.organization_id == org_id))
    await session.execute(
        delete(OrganizationMember).where(OrganizationMember.organization_id == org_id)
    )
    await session.execute(delete(Organization).where(Organization.id == org_id))
    await audit_service.record(
        session,
        user=None,
        action="org.delete_complete",
        target_type="organization",
        target_id=org_id,
        target_name=slug,
        payload={"slug": slug, "name": name, "projects": len(projects)},
        organization_id=None,
        commit=False,
    )
    await session.commit()
    await _forget_org_lists()
    return PurgeResult(
        slug=slug, projects=len(projects), blobs_deleted=blobs_deleted, blobs_failed=blobs_failed
    )


async def requeue_stranded_deletions(
    session: AsyncSession, *, now: datetime | None = None
) -> list[uuid.UUID]:
    """Claim the ``deleting`` organizations no purge job has touched for a while. Commits.

    The purge task retries a few times and then gives up, and a message can be
    lost outright; nothing else ever looks at a ``deleting`` row, and its owner
    cannot retry (the organization answers 404). So a row untouched for
    :data:`STRANDED_DELETION_GRACE` — longer than a task may run — has no job
    working on it. Its ``updated_at`` is bumped so the next pass leaves it alone
    for another grace period, and the caller queues a purge for each id
    returned. Safe because the purge is idempotent.
    """
    at = now if now is not None else datetime.now(UTC)
    ids = list(
        (
            await session.scalars(
                select(Organization.id).where(
                    Organization.status == OrganizationStatus.deleting.value,
                    Organization.updated_at < at - STRANDED_DELETION_GRACE,
                    Organization.id != DEFAULT_ORG_ID,
                )
            )
        ).all()
    )
    if ids:
        await session.execute(
            update(Organization).where(Organization.id.in_(ids)).values(updated_at=at)
        )
        await session.commit()
    return ids


async def _touch(session: AsyncSession, org_id: uuid.UUID) -> None:
    await session.execute(
        update(Organization).where(Organization.id == org_id).values(updated_at=datetime.now(UTC))
    )


async def _photo_blobs(session: AsyncSession, project_id: uuid.UUID) -> set[tuple[str, str]]:
    """``(backend, key)`` of every stored photo of the project, read before the purge."""
    rows = (
        await session.execute(
            select(EventPhoto.storage_backend, EventPhoto.storage_key).where(
                EventPhoto.project_id == project_id,
                EventPhoto.kind == _PHOTO_KIND,
                EventPhoto.storage_key.is_not(None),
            )
        )
    ).all()
    return {(str(backend), str(key)) for backend, key in rows if backend and key}


async def _delete_blobs(blobs: set[tuple[str, str]]) -> tuple[int, int]:
    """Delete each blob through the backend its row named. Best-effort.

    The rows are already gone, so a blob that cannot be deleted now is an
    orphan the maintenance sweep (``sweep_orphan_photo_blobs``) removes later.
    """
    deleted = failed = 0
    for backend, key in sorted(blobs):
        try:
            storage = storage_for(backend)
            await storage.delete(key)
            deleted += 1
        except Exception:  # noqa: BLE001 - logged; the orphan sweep retries
            failed += 1
            logger.warning("org purge: could not delete photo blob %s on %s", key, backend)
    return deleted, failed


async def _forget_project(project_id: uuid.UUID) -> None:
    try:
        await project_service.forget_purged_project(project_id)
    except Exception:  # noqa: BLE001 - caches expire on their own
        logger.warning("org purge: could not drop caches of project %s", project_id)


async def _forget_org_lists() -> None:
    try:
        await cache.delete_prefix(cache.prefix_projects())
        await cache.delete_prefix(cache.prefix_data_sources())
    except Exception:  # noqa: BLE001 - caches expire on their own
        logger.warning("org purge: could not drop the project and source list caches")
