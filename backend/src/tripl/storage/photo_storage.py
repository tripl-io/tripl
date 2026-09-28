"""Photo storage drivers.

Two backends are supported:

* ``LocalPhotoStorage`` — writes to ``settings.photo_local_dir`` and returns
  ``None`` from :meth:`public_url`. Callers serve bytes via an authenticated
  API endpoint.
* ``GCSPhotoStorage`` — writes to a Google Cloud Storage bucket. Returns a
  V4 signed URL (or the bucket's public URL when ``gcs_photo_public`` is
  ``True``) from :meth:`public_url` so the client can stream images directly.

The OPERATOR's driver is selected from ``settings.photo_storage_backend`` and
cached per process under its backend name (:func:`storage_for`). An
organization with storage of its own (F20 PR11) gets a driver built from a
:class:`StorageConfig` — its bucket, flags and service-account JSON — cached
per ``(organization, config fingerprint)`` (:func:`driver_for_config`). All IO
is offloaded to a worker thread because the GCS client is synchronous and local
writes block the event loop too.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from tripl.config import settings

if TYPE_CHECKING:
    from google.cloud.storage import Bucket, Client


class StoredObject(NamedTuple):
    """One blob as the orphan sweep sees it: its key and when it was written."""

    key: str
    written_at: datetime


class PhotoStorage(ABC):
    backend_name: str

    def list_objects(self, prefix: str) -> Iterator[StoredObject]:
        """Every object whose key starts with *prefix*, for the orphan sweep.

        Synchronous on purpose: the only caller is a Celery task, which runs
        outside an event loop. A backend without a listing API leaves this
        unimplemented and the sweep skips it (tripl-0zpq.291).
        """
        raise NotImplementedError(f"{self.backend_name} backend cannot list its objects")

    def delete_blocking(self, key: str) -> None:
        """:meth:`delete` for a caller with no event loop (the orphan sweep)."""
        raise NotImplementedError(f"{self.backend_name} backend has no blocking delete")

    @abstractmethod
    async def save(self, key: str, data: bytes, content_type: str) -> None: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def read(self, key: str) -> bytes:
        """Return raw bytes. Used by the local-backend download endpoint."""

    @abstractmethod
    async def public_url(self, key: str, content_type: str) -> str | None:
        """Return a URL the client can fetch directly, or ``None``.

        ``None`` signals that the resource must be served through the
        authenticated API endpoint (used by the local backend).
        """


class LocalPhotoStorage(PhotoStorage):
    backend_name = "local"

    def __init__(self, root: str) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        # Reject any traversal — keys are generated server-side, but guard
        # anyway so a malformed row can't escape the storage root.
        path = (self._root / key).resolve()
        if self._root not in path.parents and path != self._root:
            raise ValueError(f"storage key escapes root: {key!r}")
        return path

    async def save(self, key: str, data: bytes, content_type: str) -> None:
        del content_type  # unused for local fs
        path = self._path_for(key)
        await asyncio.to_thread(self._write, path, data)

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def delete(self, key: str) -> None:
        path = self._path_for(key)
        await asyncio.to_thread(self._unlink, path)

    @staticmethod
    def _unlink(path: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

    def list_objects(self, prefix: str) -> Iterator[StoredObject]:
        # Only regular files, and only under the prefix: the root is an
        # operator-chosen directory and may hold things tripl did not write.
        top = self._path_for(prefix) if prefix else self._root
        if not top.is_dir():
            return
        for path in top.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError:
                continue
            yield StoredObject(
                key=path.relative_to(self._root).as_posix(),
                written_at=datetime.fromtimestamp(mtime, tz=UTC),
            )

    def delete_blocking(self, key: str) -> None:
        self._unlink(self._path_for(key))

    async def read(self, key: str) -> bytes:
        path = self._path_for(key)
        return await asyncio.to_thread(path.read_bytes)

    async def public_url(self, key: str, content_type: str) -> str | None:
        del key, content_type
        return None


class GCSPhotoStorage(PhotoStorage):
    backend_name = "gcs"

    def __init__(
        self,
        *,
        bucket_name: str,
        credentials_path: str = "",
        credentials_info: dict[str, Any] | None = None,
        public: bool = False,
        signed_url_ttl_seconds: int = 3600,
    ) -> None:
        if not bucket_name:
            raise RuntimeError("photo_storage_backend=gcs requires GCS_PHOTO_BUCKET to be set")
        # Local import — google-cloud-storage is only needed when this backend
        # is active. Keeps the local-only path import-light.
        from google.cloud import storage as gcs

        if credentials_info is not None:
            # An organization's own bucket (F20 PR11): its own service account,
            # never the server's credential file or ambient identity.
            client = gcs.Client.from_service_account_info(credentials_info)
        elif credentials_path:
            client = gcs.Client.from_service_account_json(credentials_path)
        else:
            client = gcs.Client()
        self._client: Client = client
        self._bucket: Bucket = client.bucket(bucket_name)
        self._bucket_name = bucket_name
        self._public = public
        self._ttl = max(60, signed_url_ttl_seconds)

    async def save(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(self._upload, key, data, content_type)

    def _upload(self, key: str, data: bytes, content_type: str) -> None:
        blob = self._bucket.blob(key)
        blob.upload_from_string(data, content_type=content_type)
        if self._public:
            # Bucket may be configured uniform-bucket-level-access; ignore the
            # ACL call failure in that case — callers can mark the whole
            # bucket public instead.
            with contextlib.suppress(Exception):
                blob.make_public()

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._delete, key)

    def _delete(self, key: str) -> None:
        # Local import for the same reason as the client above: google-* is only
        # installed when this backend is active.
        from google.api_core import exceptions as gcs_exceptions

        blob = self._bucket.blob(key)
        try:
            blob.delete()
        except gcs_exceptions.NotFound:
            # Already gone is the end state the caller wanted, so this is a no-op
            # (``ignore=`` is not available on every client version). Everything
            # else — permission, network, quota — must propagate: this used to
            # be ``suppress(Exception)``, so a delete that failed still reported
            # success while the object stayed fetchable at a stable key
            # (tripl-jfm3.118). The local backend narrows to FileNotFoundError
            # for the same reason.
            return

    def list_objects(self, prefix: str) -> Iterator[StoredObject]:
        for blob in self._client.list_blobs(self._bucket, prefix=prefix):
            written_at = blob.time_created or blob.updated
            if written_at is None:
                # No timestamp means no age, and an object of unknown age is
                # never old enough to delete.
                continue
            yield StoredObject(key=blob.name, written_at=written_at)

    def delete_blocking(self, key: str) -> None:
        self._delete(key)

    async def read(self, key: str) -> bytes:
        # The GCS backend does not stream through our API by default — the
        # client uses signed URLs. Reading here is still useful for audit /
        # debug tooling, so the method is implemented.
        return await asyncio.to_thread(self._download, key)

    def _download(self, key: str) -> bytes:
        return self._bucket.blob(key).download_as_bytes()  # type: ignore[no-any-return]

    async def public_url(self, key: str, content_type: str) -> str | None:
        del content_type
        if self._public:
            return f"https://storage.googleapis.com/{self._bucket_name}/{key}"
        return await asyncio.to_thread(self._signed_url, key)

    def _signed_url(self, key: str) -> str:
        blob = self._bucket.blob(key)
        return blob.generate_signed_url(  # type: ignore[no-any-return]
            version="v4",
            expiration=timedelta(seconds=self._ttl),
            method="GET",
        )


_BY_NAME: dict[str, PhotoStorage] = {}


class UnknownPhotoBackend(RuntimeError):
    """A photo names a backend this process cannot build a driver for."""


def storage_for(backend: str) -> PhotoStorage:
    """The driver for a NAMED backend, built once and cached under that name.

    Every ``EventPhoto`` records the backend its blob was written to, and that
    is the only store the key means anything in. An instance can be switched
    from ``local`` to ``gcs`` (or back) while rows from the other one are still
    in the database, so reading has to follow the row rather than the current
    setting: with one driver per process, every photo taken before the switch
    was looked up in the new store, where that key names nothing, and 404ed
    (tripl-0zpq.295). Writing still follows the setting — that is what
    ``get_photo_storage`` is for.

    Raises :class:`UnknownPhotoBackend` for a name neither driver answers to.
    A stored row cannot carry one — ``EventPhoto.storage_backend`` is a database
    enum of ``local`` and ``gcs`` — so that is for a bad setting, or a backend a
    future build drops while its rows are still there. Everything else fails
    where it always did: a ``gcs`` row on an instance whose bucket setting is
    gone builds a driver here and raises from the client. Callers that serve a
    photo treat every one of those the same way, so a store they cannot reach
    costs that photo and not the page.
    """
    name = backend.lower().strip()
    cached = _BY_NAME.get(name)
    if cached is not None:
        return cached
    if name == "gcs":
        built: PhotoStorage = GCSPhotoStorage(
            bucket_name=settings.gcs_photo_bucket,
            credentials_path=settings.gcs_photo_credentials_path,
            public=settings.gcs_photo_public,
            signed_url_ttl_seconds=settings.gcs_photo_signed_url_ttl_seconds,
        )
    elif name == "local":
        built = LocalPhotoStorage(default_local_dir())
    else:
        raise UnknownPhotoBackend(
            f"Unknown photo storage backend {backend!r} (expected 'local' or 'gcs')"
        )
    _BY_NAME[name] = built
    return built


def get_photo_storage() -> PhotoStorage:
    """The driver new uploads are written to: whatever ``settings`` names now.

    Reads go through :func:`storage_for` with the row's own backend instead.
    Tests override ``photo_storage_backend`` and ``photo_local_dir`` and clear
    the cache with :func:`reset_photo_storage`.
    """
    backend = settings.photo_storage_backend.lower().strip()
    try:
        return storage_for(backend)
    except UnknownPhotoBackend as exc:
        # A misconfigured instance, not a stored row that named something odd:
        # the old message names the setting, and startup-time callers expect it.
        raise RuntimeError(
            f"Unknown photo_storage_backend={settings.photo_storage_backend!r} "
            "(expected 'local' or 'gcs')"
        ) from exc


def default_local_dir() -> str:
    return settings.photo_local_dir or str(Path.cwd() / "var" / "photos")


@dataclass(frozen=True)
class StorageConfig:
    """Everything a photo driver is built from, for one scope (F20 PR11).

    ``owner_org_id`` ``None`` is the OPERATOR's storage (``settings``, applied
    at startup; its driver is :func:`storage_for`'s). Otherwise it is the
    organization whose OWN storage this is: a GCS bucket written with
    ``credentials_json`` (never the operator's file or ambient identity), or on
    a self-hosted instance the operator's local directory. The server paths
    are the operator's in either case and never come from an organization.
    """

    owner_org_id: uuid.UUID | None
    backend: str
    bucket: str = ""
    public: bool = False
    signed_url_ttl_seconds: int = 3600
    #: An organization's service-account JSON (decrypted); "" for the operator.
    credentials_json: str = ""

    def fingerprint(self) -> str:
        """A digest of every value the driver is built from: one per version."""
        material = json.dumps(
            {
                "backend": self.backend,
                "bucket": self.bucket,
                "public": self.public,
                "ttl": self.signed_url_ttl_seconds,
                "credentials": self.credentials_json,
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode()).hexdigest()

    def store_identity(self) -> tuple[str, str]:
        """Which physical store the blobs are in: a bucket or the local root.

        Two configurations of the same bucket (say, before and after turning
        public URLs on) write into one store, and the orphan sweep has to see
        them as one: a key written under either is referenced in both.
        """
        if self.backend == "gcs":
            return ("gcs", self.bucket)
        return ("local", str(Path(default_local_dir()).resolve()))


def operator_storage_config() -> StorageConfig:
    """The operator's storage as this process runs it (startup-applied settings)."""
    return StorageConfig(
        owner_org_id=None,
        backend=settings.photo_storage_backend.lower().strip(),
        bucket=settings.gcs_photo_bucket,
        public=settings.gcs_photo_public,
        signed_url_ttl_seconds=settings.gcs_photo_signed_url_ttl_seconds,
    )


#: The only OAuth token endpoint an organization's service account is exchanged
#: at. google-auth POSTs a signed JWT to the key file's ``token_uri`` on every
#: token refresh, so a value taken from an organization's JSON would let a
#: tenant point the server at any host (an internal one included).
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
#: The only universe an organization's bucket may live in: another one moves
#: every storage and token request to ``*.{universe_domain}``.
GOOGLE_UNIVERSE_DOMAIN = "googleapis.com"


class UnsafeServiceAccount(ValueError):
    """A service-account key names a universe other than Google's public one."""


def pinned_service_account_info(info: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of an organization's service-account key that only talks to Google.

    ``token_uri`` is overwritten with :data:`GOOGLE_TOKEN_URI` whatever the key
    said, and a ``universe_domain`` other than :data:`GOOGLE_UNIVERSE_DOMAIN`
    raises :class:`UnsafeServiceAccount`. Applied when the key is saved and
    again every time a driver is built, so a key stored before the check
    existed cannot reach another host either.
    """
    universe = info.get("universe_domain")
    if universe not in (None, "", GOOGLE_UNIVERSE_DOMAIN):
        raise UnsafeServiceAccount(
            f"service-account universe_domain must be {GOOGLE_UNIVERSE_DOMAIN!r}"
        )
    pinned = {key: value for key, value in info.items() if key != "universe_domain"}
    pinned["token_uri"] = GOOGLE_TOKEN_URI
    return pinned


class MissingStorageCredentials(RuntimeError):
    """An organization's GCS storage has no service-account JSON of its own."""


_BY_CONFIG: dict[tuple[uuid.UUID, str], PhotoStorage] = {}


def driver_for_config(config: StorageConfig) -> PhotoStorage:
    """The driver of ``config``, built once per ``(organization, fingerprint)``.

    The operator's configuration goes through :func:`storage_for` (one driver
    per backend name, built from ``settings``). An organization's GCS storage
    is built with ITS service-account JSON only: an organization bucket with no
    JSON raises :class:`MissingStorageCredentials` rather than fall back to the
    server's credential file or ambient identity, which are the operator's.
    """
    if config.owner_org_id is None:
        return storage_for(config.backend)
    cache_key = (config.owner_org_id, config.fingerprint())
    cached = _BY_CONFIG.get(cache_key)
    if cached is not None:
        return cached
    name = config.backend.lower().strip()
    if name == "gcs":
        if not config.credentials_json:
            raise MissingStorageCredentials(
                f"Organization {config.owner_org_id} storage names a GCS bucket without "
                "service-account credentials of its own"
            )
        built: PhotoStorage = GCSPhotoStorage(
            bucket_name=config.bucket,
            credentials_info=pinned_service_account_info(json.loads(config.credentials_json)),
            public=config.public,
            signed_url_ttl_seconds=config.signed_url_ttl_seconds,
        )
    elif name == "local":
        built = LocalPhotoStorage(default_local_dir())
    else:
        raise UnknownPhotoBackend(
            f"Unknown photo storage backend {config.backend!r} (expected 'local' or 'gcs')"
        )
    _BY_CONFIG[cache_key] = built
    return built


def reset_photo_storage() -> None:
    _BY_NAME.clear()
    _BY_CONFIG.clear()
