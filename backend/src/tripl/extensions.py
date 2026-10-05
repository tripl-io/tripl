"""Extension points: how a separately installed package adds to the server.

Community tripl runs with no extensions. A package that ships more — the
commercial ``tripl-enterprise`` package is the one intended user — declares an
entry point in the ``tripl.extensions`` group whose object is an
:class:`Extension`, and the server calls its hooks at fixed points:

* **routers** — :meth:`Extension.api_routers` under ``/api/v1``, and anything
  else on the app itself through :meth:`Extension.install_app`;
* **access gates** — :meth:`Extension.org_session_gate`,
  :meth:`Extension.api_key_use_gate` and :meth:`Extension.api_key_mint_gate`
  may refuse a request by raising :class:`GateRefused`;
* **lifecycle** — a member leaving an organization, an owner stepping down, a
  removed member invited back, an organization being deleted, an organization
  group changing membership;
* **audit** — every audit row as it is written, in the writing transaction;
* **errors** — an extension may answer errors on paths it owns in a format of
  its own (:meth:`Extension.error_response`);
* **worker** — Celery task modules and beat schedule entries.

ORM model modules are a separate entry point group, ``tripl.models``, whose
values are module paths: ``tripl.models`` imports them while it is itself still
initialising, before any service can be imported, so they cannot be reached
through the extension object.

Every hook has a no-op default, so an extension overrides only what it needs and
a server with none behaves exactly as Community. Hooks run in the order the
extensions are loaded; a gate's first refusal wins.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, Literal

from fastapi import HTTPException, status

if TYPE_CHECKING:
    import uuid

    from fastapi import APIRouter, FastAPI, Request
    from sqlalchemy.ext.asyncio import AsyncSession
    from starlette.responses import Response

    from tripl.middleware.org_context import OrgRef
    from tripl.models.audit_log import AuditLog
    from tripl.models.domain_enums import OrganizationRole
    from tripl.models.user import User

logger = logging.getLogger(__name__)

EXTENSION_GROUP = "tripl.extensions"
MODEL_GROUP = "tripl.models"

ErrorKind = Literal["http", "validation", "too_large"]


class GateRefused(HTTPException):
    """An extension's access gate refusing a request.

    Answered as JSON ``{"detail": ..., **extra}`` with ``status_code``, so an
    extension can tell the client how to get through (a sign-in URL, say).
    """

    def __init__(
        self,
        detail: str,
        *,
        extra: Mapping[str, Any] | None = None,
        status_code: int = status.HTTP_403_FORBIDDEN,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.extra: dict[str, Any] = dict(extra or {})


@dataclass(frozen=True)
class ExtensionRouter:
    """A router an extension mounts under ``/api/v1``.

    ``protected`` routes get the API's authentication and project-membership
    dependencies; ``outbound`` names, for the public-demo refusal, what a write
    on the router would send out of the instance (None when it sends nothing).
    """

    router: APIRouter
    protected: bool = True
    outbound: str | None = None


class Extension:
    """Base class for extensions: every hook is a no-op."""

    name = "extension"

    # -- routes and app --------------------------------------------------
    def api_routers(self) -> Sequence[ExtensionRouter]:
        """Routers mounted under ``/api/v1``, ahead of the core's."""
        return ()

    def install_app(self, app: FastAPI) -> None:
        """Anything else on the app: routers outside ``/api/v1``, exception handlers."""

    def error_response(
        self,
        path: str,
        kind: ErrorKind,
        status_code: int,
        detail: str,
        headers: Mapping[str, str] | None = None,
    ) -> Response | None:
        """An error on ``path`` in the extension's own format, or None for the default."""
        return None

    # -- access gates ----------------------------------------------------
    async def org_session_gate(
        self,
        request: Request,
        session: AsyncSession,
        user: User,
        org: OrgRef,
        *,
        role: OrganizationRole | None = None,
    ) -> None:
        """Refuse (raise :class:`GateRefused`) a browser session acting in ``org``."""

    async def api_key_use_gate(
        self,
        request: Request,
        session: AsyncSession,
        user: User,
        org_id: uuid.UUID,
        api_key: object,
    ) -> None:
        """Refuse an API key used in ``org_id``."""

    async def api_key_mint_gate(self, session: AsyncSession, org: OrgRef) -> None:
        """Refuse minting an API key in ``org`` from a session that is not tied to it."""

    # -- lifecycle -------------------------------------------------------
    async def on_member_removed(
        self, session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID, *, by_admin: bool
    ) -> Mapping[str, int]:
        """A member is leaving ``org_id``; returns counts of what was cleaned up, for audit."""
        return {}

    async def on_owner_demoted(
        self, session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        """``user_id`` stopped being an owner of ``org_id``."""

    async def on_membership_restored(
        self, session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        """``user_id`` joined ``org_id`` again through an invitation."""

    async def on_org_deleting(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        """``org_id`` is being deleted: drop the extension's rows, before its groups go."""

    async def on_group_change(
        self,
        session: AsyncSession,
        org_id: uuid.UUID,
        group_id: uuid.UUID,
        *,
        added: Sequence[uuid.UUID] = (),
        removed: Sequence[uuid.UUID] = (),
    ) -> None:
        """Members were added to or removed from an organization group."""

    # -- audit -----------------------------------------------------------
    async def on_audit_recorded(
        self, session: AsyncSession, entry: AuditLog, org_id: uuid.UUID
    ) -> None:
        """An organization's audit row was added, in the writing transaction."""

    # -- worker ----------------------------------------------------------
    def celery_task_modules(self) -> Sequence[str]:
        """Modules to import so their Celery tasks register."""
        return ()

    def beat_schedule(self) -> Mapping[str, Mapping[str, Any]]:
        """Celery beat entries to add."""
        return {}


_loaded: list[Extension] | None = None


def _load() -> list[Extension]:
    found: list[Extension] = []
    for point in sorted(entry_points(group=EXTENSION_GROUP), key=lambda p: p.name):
        found.append(point.load())
    for extension in found:
        if not isinstance(extension, Extension):
            raise TypeError(f"{extension!r} is not a tripl.extensions.Extension")
    if found:
        logger.info("extensions.loaded names=%s", ",".join(e.name for e in found))
    return found


def extensions() -> list[Extension]:
    """The installed extensions, loaded once."""
    global _loaded
    if _loaded is None:
        _loaded = _load()
    return _loaded


@contextmanager
def override_extensions(replacement: Sequence[Extension]) -> Iterator[None]:
    """Run with exactly ``replacement`` installed (tests)."""
    global _loaded
    previous = _loaded
    _loaded = list(replacement)
    try:
        yield
    finally:
        _loaded = previous


def import_model_modules() -> None:
    """Import every extension's ORM models, so ``Base.metadata`` has their tables."""
    for point in entry_points(group=MODEL_GROUP):
        importlib.import_module(point.value)


# -- dispatch helpers the core calls ---------------------------------------


async def org_session_gate(
    request: Request,
    session: AsyncSession,
    user: User,
    org: OrgRef,
    *,
    role: OrganizationRole | None = None,
) -> None:
    for extension in extensions():
        await extension.org_session_gate(request, session, user, org, role=role)


async def api_key_use_gate(
    request: Request, session: AsyncSession, user: User, org_id: uuid.UUID, api_key: object
) -> None:
    for extension in extensions():
        await extension.api_key_use_gate(request, session, user, org_id, api_key)


async def api_key_mint_gate(session: AsyncSession, org: OrgRef) -> None:
    for extension in extensions():
        await extension.api_key_mint_gate(session, org)


async def on_member_removed(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID, *, by_admin: bool
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for extension in extensions():
        counts.update(
            await extension.on_member_removed(session, org_id, user_id, by_admin=by_admin)
        )
    return counts


async def on_owner_demoted(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
    for extension in extensions():
        await extension.on_owner_demoted(session, org_id, user_id)


async def on_membership_restored(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    for extension in extensions():
        await extension.on_membership_restored(session, org_id, user_id)


async def on_org_deleting(session: AsyncSession, org_id: uuid.UUID) -> None:
    for extension in extensions():
        await extension.on_org_deleting(session, org_id)


async def on_group_change(
    session: AsyncSession,
    org_id: uuid.UUID,
    group_id: uuid.UUID,
    *,
    added: Sequence[uuid.UUID] = (),
    removed: Sequence[uuid.UUID] = (),
) -> None:
    for extension in extensions():
        await extension.on_group_change(session, org_id, group_id, added=added, removed=removed)


async def on_audit_recorded(session: AsyncSession, entry: AuditLog, org_id: uuid.UUID) -> None:
    for extension in extensions():
        await extension.on_audit_recorded(session, entry, org_id)


def error_response(
    path: str,
    kind: ErrorKind,
    status_code: int,
    detail: str,
    headers: Mapping[str, str] | None = None,
) -> Response | None:
    for extension in extensions():
        response = extension.error_response(path, kind, status_code, detail, headers)
        if response is not None:
            return response
    return None
