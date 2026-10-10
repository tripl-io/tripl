"""Where an organization's photos go, what it may upload, and how a blob is found
again (F20 PR11).

* :class:`PhotoPolicy` — one organization's effective photo settings: the
  storage new uploads are written to, the ``orgs/{id}/`` key prefix, the size
  cap and the content-type allow-list. Resolved org override -> operator by
  ``app_settings_service`` (the storage group rules live in
  ``_org_settings_merge``). The operator half is the process's
  startup-applied ``settings``, as it always was; an organization's own
  values are read per request, with the size capped by the operator's (the
  request-body ceiling ``BodyLimitMiddleware`` enforces before any
  organization is known) and the allow-list narrowed to the operator's.
* :func:`ensure_config_row` — the immutable ``photo_storage_configs`` version a
  blob written with an organization's own storage points at.
* :func:`driver_for_blob` / :func:`driver_for_blob_sync` — the driver that can
  reach a stored blob: the operator's store its ``storage_backend`` names, or
  the organization storage version it was written with. Never the current
  setting: a bucket change must not send reads of older photos to the new
  bucket, where their keys name nothing.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, replace
from functools import partial

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tripl import crypto
from tripl.config import settings
from tripl.models.photo_storage_config import PhotoStorageConfig
from tripl.services import app_settings_service
from tripl.services._org_settings_merge import STORAGE_GROUP, narrow_list, split_list
from tripl.storage import (
    PhotoStorage,
    StorageConfig,
    driver_for_config,
    operator_storage_config,
    storage_for,
)
from tripl.storage.photo_storage import StoredObject

logger = logging.getLogger(__name__)

#: The key segment every organization's blobs live under.
ORG_KEY_ROOT = "orgs/"
#: Keys written before F20 PR11 (the operator's store only).
LEGACY_KEY_PREFIX = "events/"


def org_key_prefix(org_id: uuid.UUID) -> str:
    """``orgs/{org_id}/events/``: where an organization's uploads are keyed."""
    return f"{ORG_KEY_ROOT}{org_id}/{LEGACY_KEY_PREFIX}"


@dataclass(frozen=True)
class PhotoPolicy:
    """An organization's effective photo settings (``org_id`` ``None``: the operator's)."""

    org_id: uuid.UUID | None
    storage: StorageConfig
    max_size_mb: int
    allowed_mime: tuple[str, ...]

    @property
    def key_prefix(self) -> str:
        """Where this policy's new uploads are keyed (``events/`` with no organization)."""
        return LEGACY_KEY_PREFIX if self.org_id is None else org_key_prefix(self.org_id)

    @property
    def max_size_bytes(self) -> int:
        return self.max_size_mb * 1024 * 1024


def operator_policy() -> PhotoPolicy:
    """The operator's limits and storage, as this process runs them."""
    return PhotoPolicy(
        org_id=None,
        storage=operator_storage_config(),
        max_size_mb=max(1, settings.photo_max_size_mb),
        allowed_mime=tuple(split_list(settings.photo_allowed_mime)),
    )


def owns_storage(resolved: app_settings_service.ResolvedSettings) -> bool:
    """Whether the resolution's storage group is the organization's own."""
    return resolved.org_scope is not None and any(
        resolved.sources.get(field) == "org" for field in STORAGE_GROUP
    )


def policy_for(
    resolved: app_settings_service.ResolvedSettings, org_id: uuid.UUID | None
) -> PhotoPolicy:
    """The policy of a resolution; ``org_id`` names the key prefix owner.

    ``org_id`` is the project's organization even when its settings scope is
    the operator's (a self-hosted default organization): every new key is
    ``orgs/{org_id}/...``, whichever store it lands in.
    """
    operator = operator_policy()
    if resolved.org_scope is None:
        return replace(operator, org_id=org_id)
    values, sources = resolved.values, resolved.sources
    max_size_mb = operator.max_size_mb
    if sources.get("photo_max_size_mb") == "org":
        max_size_mb = max(1, min(int(values["photo_max_size_mb"]), operator.max_size_mb))
    allowed_mime = operator.allowed_mime
    if sources.get("photo_allowed_mime") == "org":
        allowed_mime = tuple(
            split_list(narrow_list(values["photo_allowed_mime"], ",".join(operator.allowed_mime)))
        )
    storage = operator.storage
    if owns_storage(resolved):
        storage = StorageConfig(
            owner_org_id=resolved.org_scope,
            backend=str(values["photo_storage_backend"]).lower().strip(),
            bucket=str(values["gcs_photo_bucket"]).strip(),
            public=bool(values["gcs_photo_public"]),
            signed_url_ttl_seconds=max(60, int(values["gcs_photo_signed_url_ttl_seconds"])),
            credentials_json=str(values["gcs_photo_credentials_json"]),
        )
    return PhotoPolicy(
        org_id=org_id, storage=storage, max_size_mb=max_size_mb, allowed_mime=allowed_mime
    )


async def policy_for_org(session: AsyncSession, org_id: uuid.UUID | None) -> PhotoPolicy:
    """The organization's policy. A settings read failure propagates (fail closed)."""
    return policy_for(await app_settings_service.resolve_for_org(session, org_id), org_id)


async def policy_for_project(session: AsyncSession, project_id: uuid.UUID) -> PhotoPolicy:
    """The policy of the organization that owns ``project_id``."""
    org_id = await app_settings_service.project_org_id(session, project_id)
    return await policy_for_org(session, org_id)


# ── storage versions ────────────────────────────────────────────────────────


def _row_value(config: StorageConfig) -> dict[str, object]:
    return {
        "bucket": config.bucket,
        "public": config.public,
        "signed_url_ttl_seconds": config.signed_url_ttl_seconds,
        "credentials_json": (
            crypto.encrypt_value(config.credentials_json) if config.credentials_json else ""
        ),
    }


def config_from_row(row: PhotoStorageConfig) -> StorageConfig:
    """The driver values a version row holds (credential JSON decrypted)."""
    value = row.value if isinstance(row.value, dict) else {}
    encrypted = str(value.get("credentials_json") or "")
    credentials = ""
    if encrypted:
        try:
            credentials = crypto.decrypt_value(encrypted)
        except crypto.InvalidToken:
            # The driver then refuses to build (no JSON of its own), and every
            # photo of this version answers 409 rather than using the
            # operator's identity on the organization's bucket.
            logger.warning("Cannot decrypt the credentials of photo storage version %s", row.id)
    return StorageConfig(
        owner_org_id=row.organization_id,
        backend=row.backend,
        bucket=str(value.get("bucket") or ""),
        public=bool(value.get("public")),
        signed_url_ttl_seconds=int(value.get("signed_url_ttl_seconds") or 3600),
        credentials_json=credentials,
    )


#: Version rows never change once written, so a decrypted version is cached
#: by id for the life of the process.
_VERSIONS: dict[uuid.UUID, StorageConfig] = {}


def forget_cached_versions() -> None:
    _VERSIONS.clear()


def _version_query(config: StorageConfig) -> Select[uuid.UUID]:
    return select(PhotoStorageConfig.id).where(
        PhotoStorageConfig.organization_id == config.owner_org_id,
        PhotoStorageConfig.config_hash == config.fingerprint(),
    )


async def ensure_config_row(session: AsyncSession, config: StorageConfig) -> uuid.UUID | None:
    """The id of ``config``'s version row, written if new; ``None`` for the operator's.

    Inside the caller's transaction, in a savepoint: two uploads racing to
    write the same new version both end up pointing at the one row.
    """
    if config.owner_org_id is None:
        return None
    found: uuid.UUID | None = await session.scalar(_version_query(config))
    if found is not None:
        return found
    row = PhotoStorageConfig(
        organization_id=config.owner_org_id,
        config_hash=config.fingerprint(),
        backend=config.backend,
        value=_row_value(config),
    )
    try:
        async with session.begin_nested():
            session.add(row)
    except IntegrityError:
        found = await session.scalar(_version_query(config))
        if found is None:
            raise
        return found
    _VERSIONS[row.id] = config
    return row.id


async def version_config(session: AsyncSession, config_id: uuid.UUID) -> StorageConfig | None:
    cached = _VERSIONS.get(config_id)
    if cached is not None:
        return cached
    row = await session.get(PhotoStorageConfig, config_id)
    if row is None:
        return None
    config = config_from_row(row)
    _VERSIONS[config_id] = config
    return config


def version_config_sync(session: Session, config_id: uuid.UUID) -> StorageConfig | None:
    cached = _VERSIONS.get(config_id)
    if cached is not None:
        return cached
    row = session.get(PhotoStorageConfig, config_id)
    if row is None:
        return None
    config = config_from_row(row)
    _VERSIONS[config_id] = config
    return config


class UnknownStorageVersion(RuntimeError):
    """A photo row names a storage version that no longer exists."""


async def driver_for_blob(
    session: AsyncSession, backend: str, config_id: uuid.UUID | None
) -> PhotoStorage:
    """The driver of the store a blob was written to. Raises when it cannot be built."""
    if config_id is None:
        return storage_for(backend)
    config = await version_config(session, config_id)
    if config is None:
        raise UnknownStorageVersion(f"photo storage version {config_id} does not exist")
    return driver_for_config(config)


def driver_for_blob_sync(
    session: Session, backend: str, config_id: uuid.UUID | None
) -> PhotoStorage:
    if config_id is None:
        return storage_for(backend)
    config = version_config_sync(session, config_id)
    if config is None:
        raise UnknownStorageVersion(f"photo storage version {config_id} does not exist")
    return driver_for_config(config)


# ── the stores a prefix lives in (orphan sweep, organization purge) ─────────


def operator_photo_backends() -> list[str]:
    """The operator's photo backends this process can reach, whatever new uploads use.

    Rows written before a backend switch still point at the old store,
    so its blobs are listed too. GCS only when a bucket is
    configured: without one the driver cannot even be built.
    """
    backends = ["local"]
    if settings.gcs_photo_bucket:
        backends.append("gcs")
    return backends


def operator_store_identity(backend: str) -> tuple[str, str]:
    return StorageConfig(
        owner_org_id=None, backend=backend, bucket=settings.gcs_photo_bucket
    ).store_identity()


@dataclass(frozen=True)
class OrgStore:
    """One physical store of an organization's own, and every version that writes to it.

    ``configs`` is newest first: rotating a bucket's key is the usual reason
    for a new version of the same store, and the older keys are then often
    revoked, so a caller builds its driver from the newest version and falls
    back to older ones only when that one cannot list (:func:`list_first`).
    """

    org_id: uuid.UUID
    identity: tuple[str, str]
    configs: tuple[StorageConfig, ...]
    version_ids: frozenset[uuid.UUID]

    @property
    def backend(self) -> str:
        return self.identity[0]

    def builders(self) -> list[Callable[[], PhotoStorage]]:
        return [partial(driver_for_config, config) for config in self.configs]


def group_org_stores(
    rows: Iterable[PhotoStorageConfig], *, skip: Collection[tuple[str, str]] = ()
) -> list[OrgStore]:
    """Version rows grouped by (organization, store), in the order given.

    Pass ``rows`` newest first (``ORDER BY created_at DESC``): each store's
    ``configs`` keep that order.

    ``skip`` names stores listed some other way (the operator's own, which a
    self-hosted organization on the local backend writes into).
    """
    grouped: dict[
        tuple[uuid.UUID, tuple[str, str]], list[tuple[PhotoStorageConfig, StorageConfig]]
    ] = {}
    for row in rows:
        config = config_from_row(row)
        identity = config.store_identity()
        if identity in skip:
            continue
        grouped.setdefault((row.organization_id, identity), []).append((row, config))
    return [
        OrgStore(
            org_id=org_id,
            identity=identity,
            configs=tuple(config for _row, config in versions),
            version_ids=frozenset(row.id for row, _config in versions),
        )
        for (org_id, identity), versions in grouped.items()
    ]


def list_first(
    builders: Iterable[Callable[[], PhotoStorage]], prefixes: Iterable[str]
) -> tuple[PhotoStorage, list[StoredObject]]:
    """The first driver that can build and list every prefix, and what it listed.

    Synchronous (the GCS client is): an async caller runs it in a thread.
    ``NotImplementedError`` (a backend with no listing API) is raised at once;
    any other failure moves on to the next builder, and the last one's is
    raised when none works.
    """
    wanted = sorted(prefixes)
    failure: Exception | None = None
    for build in builders:
        try:
            storage = build()
            return storage, [obj for prefix in wanted for obj in storage.list_objects(prefix)]
        except NotImplementedError:
            raise
        except Exception as exc:  # noqa: BLE001 - try the next version's credentials
            failure = exc
    if failure is None:
        raise LookupError("no driver to list with")
    raise failure
