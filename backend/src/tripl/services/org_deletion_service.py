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
   each project through ``purge_project_rows`` and deletes its photo blobs,
   then every blob left under its ``orgs/{id}/events/`` prefix (in the
   operator's store and its own), then the organization's data sources, API
   keys, invitations, docs, settings rows and memberships, then the row, and
   files ``org.delete_complete`` at platform scope (``organization_id`` NULL,
   the slug in the payload): the organization the row would belong to no
   longer exists. While a blob under the prefix cannot be deleted, the row and
   its storage versions stay (``deleting``), so the prefix is not forgotten.

The purge is idempotent: a job that died half-way is simply run again, each
project committed as it goes. A job that ran out of retries, or a message the
broker lost, is picked up by :func:`requeue_stranded_deletions`, run hourly by
the ``org_delete.requeue_stranded_org_deletions`` beat task.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import cache
from tripl.models.api_key import ApiKey
from tripl.models.app_setting import AppSetting
from tripl.models.data_source import DataSource
from tripl.models.doc_file import DocFile
from tripl.models.doc_share import DocFolderSetting
from tripl.models.domain_enums import EventPhotoKind, OrganizationStatus
from tripl.models.event_photo import EventPhoto
from tripl.models.invitation import Invitation
from tripl.models.org_scim import OrgScimConfig, OrgScimToken
from tripl.models.organization import DEFAULT_ORG_ID, Organization, OrganizationMember
from tripl.models.photo_storage_config import PhotoStorageConfig
from tripl.models.project import Project
from tripl.models.user import User
from tripl.services import (
    audit_service,
    audit_webhook_service,
    org_group_service,
    org_sso_service,
    project_service,
    scim_group_service,
    scim_user_service,
)
from tripl.services.event_photo_service import BlobRef
from tripl.services.photo_storage_service import (
    driver_for_blob,
    group_org_stores,
    list_first,
    operator_photo_backends,
    operator_store_identity,
    org_key_prefix,
)
from tripl.storage import PhotoStorage, storage_for

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


class OrgNotActiveError(Exception):
    """The organization stopped being ``active`` between the gate and the update.

    ``suspended`` is ``True`` when a platform admin suspended it meanwhile (the
    caller answers the suspended 403); ``False`` when it is gone or already
    ``deleting`` (404).
    """

    def __init__(self, *, suspended: bool) -> None:
        super().__init__("suspended" if suspended else "gone")
        self.suspended = suspended


@dataclass(frozen=True)
class PurgeResult:
    slug: str
    projects: int
    blobs_deleted: int
    blobs_failed: int
    #: Blobs (or whole stores) under ``orgs/{id}/events/`` the final pass could
    #: not delete or list. Non-zero: the organization row and its storage
    #: versions are kept, still ``deleting``, and the chaser runs the purge again.
    leftovers_failed: int = 0

    @property
    def complete(self) -> bool:
        return self.leftovers_failed == 0


async def request_deletion(
    session: AsyncSession, *, org_id: uuid.UUID, slug: str, confirm_slug: str
) -> None:
    """Mark the organization ``deleting``. Does NOT commit (the caller audits first)."""
    if org_id == DEFAULT_ORG_ID:
        raise DefaultOrgUndeletableError
    if confirm_slug != slug:
        raise ConfirmationMismatchError
    # Compare-and-set: the gate read the organization ``active``, but a platform
    # admin may suspend it before this runs. Flipping only an ``active`` row
    # keeps a suspension from being overwritten by a deletion its owner could
    # no longer have asked for.
    result = await session.execute(
        update(Organization)
        .where(
            Organization.id == org_id,
            Organization.status == OrganizationStatus.active.value,
        )
        .values(status=OrganizationStatus.deleting.value)
        # "fetch": a loaded instance takes the new status and never flushes its
        # stale ``active`` back over the row.
        .execution_options(synchronize_session="fetch")
    )
    if getattr(result, "rowcount", 0) == 0:
        current: str | None = await session.scalar(
            select(Organization.status).where(Organization.id == org_id)
        )
        raise OrgNotActiveError(suspended=current == OrganizationStatus.suspended.value)
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
        deleted, failed = await _delete_blobs(session, blobs)
        blobs_deleted += deleted
        blobs_failed += failed
        await _forget_project(project_id)

    # Every blob still under the organization's prefix, in the operator's store
    # and in each store of its own: what the loop above could not delete, and
    # what an upload racing the deletion wrote. The rows naming them are gone,
    # and once the organization row is gone too no sweep lists this prefix
    # again, so the organization stays until the prefix is empty.
    blobs_cleared, leftovers_failed = await _clear_org_prefix(session, org_id)
    blobs_deleted += blobs_cleared

    # Workspace-wide sources (project-owned ones went with their project), then
    # the RESTRICT references, then what would cascade anyway, spelled out.
    await session.execute(delete(DataSource).where(DataSource.organization_id == org_id))
    await session.execute(delete(ApiKey).where(ApiKey.organization_id == org_id))
    await session.execute(delete(Invitation).where(Invitation.organization_id == org_id))
    await session.execute(delete(DocFile).where(DocFile.organization_id == org_id))
    await session.execute(
        delete(DocFolderSetting).where(DocFolderSetting.organization_id == org_id)
    )
    await session.execute(delete(AppSetting).where(AppSetting.organization_id == org_id))
    # SCIM first: its config and group links reference the groups.
    await scim_group_service.delete_org_scim_groups(session, org_id)
    await scim_user_service.delete_org_scim_users(session, org_id)
    await session.execute(delete(OrgScimConfig).where(OrgScimConfig.organization_id == org_id))
    await session.execute(delete(OrgScimToken).where(OrgScimToken.organization_id == org_id))
    await org_group_service.delete_org_groups(session, org_id)
    await org_sso_service.delete_org_sso(session, org_id)
    await audit_webhook_service.delete_org_webhook(session, org_id)
    await session.execute(
        delete(OrganizationMember).where(OrganizationMember.organization_id == org_id)
    )
    if leftovers_failed:
        # Kept: the row (``deleting``, so still 404 everywhere) and the storage
        # versions whose credentials reach the organization's own bucket. The
        # stranded-deletion chaser runs this purge again after its grace.
        await _touch(session, org_id)
        await session.commit()
        await _forget_org_lists()
        logger.warning(
            "org purge: %d photo blob(s) of organization %s could not be deleted; "
            "the organization is kept until a later run deletes them",
            leftovers_failed,
            org_id,
        )
        return PurgeResult(
            slug=slug,
            projects=len(projects),
            blobs_deleted=blobs_deleted,
            blobs_failed=blobs_failed,
            leftovers_failed=leftovers_failed,
        )
    await session.execute(
        delete(PhotoStorageConfig).where(PhotoStorageConfig.organization_id == org_id)
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


async def _photo_blobs(session: AsyncSession, project_id: uuid.UUID) -> set[BlobRef]:
    """``(backend, key, storage version)`` of every stored photo of the project,
    read before the purge."""
    rows = (
        await session.execute(
            select(
                EventPhoto.storage_backend, EventPhoto.storage_key, EventPhoto.storage_config_id
            ).where(
                EventPhoto.project_id == project_id,
                EventPhoto.kind == _PHOTO_KIND,
                EventPhoto.storage_key.is_not(None),
            )
        )
    ).all()
    return {
        (str(backend), str(key), config_id) for backend, key, config_id in rows if backend and key
    }


async def _driver(session: AsyncSession, backend: str, config_id: uuid.UUID | None) -> PhotoStorage:
    """The operator's store by backend name, or the organization storage version.

    The organization's own versions are still readable here: they are deleted
    with the organization row, after every project's blobs (F20 PR11).
    """
    if config_id is None:
        return storage_for(backend)
    return await driver_for_blob(session, backend, config_id)


async def _delete_blobs(session: AsyncSession, blobs: set[BlobRef]) -> tuple[int, int]:
    """Delete each blob through the store its row named. Best-effort.

    An organization's own bucket is reached with ITS credentials (the storage
    version the blob was written with), the operator's store by backend name.
    The rows are already gone, so a blob that cannot be deleted now is retried
    by :func:`_clear_org_prefix`, which keeps the organization (and so its
    prefix and storage versions) until it succeeds. A legacy ``events/`` key
    in the operator's store is left to the maintenance sweep, which always
    lists that prefix.
    """
    deleted = failed = 0
    for backend, key, config_id in sorted(blobs, key=lambda ref: (ref[0], ref[1])):
        try:
            storage = await _driver(session, backend, config_id)
            await storage.delete(key)
            deleted += 1
        except Exception:  # noqa: BLE001 - logged; retried by _clear_org_prefix
            failed += 1
            logger.warning("org purge: could not delete photo blob %s on %s", key, backend)
    return deleted, failed


async def _org_stores(
    session: AsyncSession, org_id: uuid.UUID
) -> list[tuple[str, list[Callable[[], PhotoStorage]]]]:
    """Every store the organization's blobs can be in, with the drivers to try.

    The operator's (by backend name) and each of the organization's own, newest
    version first: an older version's key may have been revoked since.
    """
    stores: list[tuple[str, list[Callable[[], PhotoStorage]]]] = []
    operator_identities: set[tuple[str, str]] = set()
    for backend in operator_photo_backends():
        operator_identities.add(operator_store_identity(backend))
        stores.append((backend, [partial(storage_for, backend)]))
    rows = (
        await session.scalars(
            select(PhotoStorageConfig)
            .where(PhotoStorageConfig.organization_id == org_id)
            .order_by(PhotoStorageConfig.created_at.desc())
        )
    ).all()
    for org_store in group_org_stores(rows, skip=operator_identities):
        stores.append((f"org:{org_store.backend}:{org_store.identity[1]}", org_store.builders()))
    return stores


async def _clear_org_prefix(session: AsyncSession, org_id: uuid.UUID) -> tuple[int, int]:
    """Delete every unreferenced blob under ``orgs/{org_id}/events/``: (deleted, failed).

    A store that cannot be listed counts as one failure. One with no listing
    API at all is skipped: nothing here could ever list it.
    """
    prefix = org_key_prefix(org_id)
    referenced = set(
        (
            await session.scalars(
                select(EventPhoto.storage_key).where(EventPhoto.storage_key.startswith(prefix))
            )
        ).all()
    )
    deleted = failed = 0
    for label, builders in await _org_stores(session, org_id):
        try:
            storage, listed = await asyncio.to_thread(list_first, builders, [prefix])
        except NotImplementedError:
            logger.warning("org purge: the %s photo store cannot be listed; skipped", label)
            continue
        except Exception:  # noqa: BLE001 - retried by the next purge run
            logger.warning("org purge: cannot list the %s photo store", label, exc_info=True)
            failed += 1
            continue
        for obj in listed:
            if obj.key in referenced:
                continue
            try:
                await storage.delete(obj.key)
                deleted += 1
            except Exception:  # noqa: BLE001 - retried by the next purge run
                failed += 1
                logger.warning("org purge: could not delete photo blob %s on %s", obj.key, label)
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
