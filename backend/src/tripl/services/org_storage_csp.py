"""Which organizations serve photos from their own GCS bucket, for the CSP (F20 PR11).

The Content-Security-Policy's ``img-src`` must admit ``storage.googleapis.com``
wherever a page shows photos from a GCS bucket: signed and public URLs both
point there. Before organizations had storage of their own that was a fixed
question (``PHOTO_STORAGE_BACKEND=gcs``), answered once at startup; now it is
asked per request (``SecurityHeadersMiddleware``):

* a request that names or binds an organization — the organization's own
  storage is GCS, or some of its photos were written to a GCS version of its
  own storage and are still read from there;
* a page of the SPA — any organization on the instance has its own GCS
  storage, because the SPA switches organizations without reloading the
  document whose CSP applies.

The middleware runs before any session exists, so this module keeps the set in
memory: written through by an organization's settings save in this process
(:func:`note_org_backend`), and reloaded from ``app_settings`` at most every
:data:`REFRESH_SECONDS` by the middleware itself (:func:`refresh_if_stale`), so
a save made by another process is picked up within that window.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import Select, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.app_setting import SERVICE_SETTINGS_KEY, AppSetting
from tripl.models.event_photo import EventPhoto
from tripl.models.organization import Organization
from tripl.models.photo_storage_config import PhotoStorageConfig

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 60.0
_LOAD_TIMEOUT_SECONDS = 2.0


@dataclass
class _Registry:
    #: organization id -> slug, for every organization whose own storage is GCS.
    gcs: dict[uuid.UUID, str] = field(default_factory=dict)
    loaded_at: float | None = None


_REGISTRY = _Registry()

Loader = Callable[[], Awaitable[dict[uuid.UUID, str]]]


def _stores_gcs(value: object) -> bool:
    return (
        isinstance(value, dict)
        and str(value.get("photo_storage_backend") or "").lower().strip() == "gcs"
    )


async def load_from_database() -> dict[uuid.UUID, str]:
    """Every organization whose photos may be served from a GCS bucket of its own.

    Two sources: a stored storage override naming the GCS backend (where new
    uploads go), and a GCS storage VERSION some photo row still points at.
    The second outlives the first: a photo is read with the version it was
    written with, so an organization that moved back to the operator's store
    still serves its older photos from ``storage.googleapis.com``.
    """
    from tripl.database import async_session

    async with async_session() as session:
        rows = await session.execute(
            select(AppSetting.organization_id, AppSetting.value, Organization.slug)
            .join(Organization, Organization.id == AppSetting.organization_id)
            .where(AppSetting.key == SERVICE_SETTINGS_KEY, AppSetting.organization_id.is_not(None))
        )
        found = {
            org_id: slug
            for org_id, value, slug in rows.all()
            if org_id is not None and _stores_gcs(value)
        }
        versions = await session.execute(
            select(Organization.id, Organization.slug).where(
                Organization.id.in_(_orgs_with_gcs_photos())
            )
        )
        found.update({org_id: slug for org_id, slug in versions.all()})
        return found


def _orgs_with_gcs_photos() -> Select[uuid.UUID]:
    """Organizations with a GCS storage version that a photo row still references."""
    return (
        select(PhotoStorageConfig.organization_id)
        .where(
            PhotoStorageConfig.backend == "gcs",
            exists().where(EventPhoto.storage_config_id == PhotoStorageConfig.id),
        )
        .distinct()
    )


async def org_has_gcs_photos(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Whether a photo of ``org_id`` is still served from a GCS version of its own."""
    found = await session.scalar(
        select(PhotoStorageConfig.id)
        .where(
            PhotoStorageConfig.organization_id == org_id,
            PhotoStorageConfig.backend == "gcs",
            exists().where(EventPhoto.storage_config_id == PhotoStorageConfig.id),
        )
        .limit(1)
    )
    return found is not None


#: Swapped by tests; production reads the database.
loader: Loader = load_from_database


async def refresh_if_stale(now: float | None = None) -> None:
    """Reload the set when it is older than :data:`REFRESH_SECONDS`. Never raises.

    A failed load keeps what the process already knew and waits a full window
    before trying again, so an unreachable database costs one slow request per
    window, not every request.
    """
    at = time.monotonic() if now is None else now
    if _REGISTRY.loaded_at is not None and at - _REGISTRY.loaded_at < REFRESH_SECONDS:
        return
    _REGISTRY.loaded_at = at
    try:
        _REGISTRY.gcs = dict(await asyncio.wait_for(loader(), timeout=_LOAD_TIMEOUT_SECONDS))
    except Exception:  # noqa: BLE001 - a header decision must never fail a request
        logger.warning("Could not reload the organizations with GCS photo storage", exc_info=True)


def note_org_backend(
    org_id: uuid.UUID, slug: str, backend: str, *, has_gcs_photos: bool = False
) -> None:
    """Write-through after an organization's storage changed in this process.

    ``has_gcs_photos``: older photos are still read from a GCS version of the
    organization's own (:func:`org_has_gcs_photos`), whatever ``backend`` is now.
    """
    if backend.lower().strip() == "gcs" or has_gcs_photos:
        _REGISTRY.gcs[org_id] = slug
    else:
        _REGISTRY.gcs.pop(org_id, None)


def reset() -> None:
    _REGISTRY.gcs = {}
    _REGISTRY.loaded_at = None


def org_uses_gcs(*, org_id: uuid.UUID | None = None, slug: str | None = None) -> bool:
    if org_id is not None:
        return org_id in _REGISTRY.gcs
    if slug is not None:
        return slug in _REGISTRY.gcs.values()
    return False


def any_org_uses_gcs() -> bool:
    return bool(_REGISTRY.gcs)
