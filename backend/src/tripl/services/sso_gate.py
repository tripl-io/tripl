"""An organization's "SSO required" gate (F20), reached through the extension hooks.

Moved out of ``api/deps.py`` so the core's request dependencies call only
:mod:`tripl.extensions`; the bundled extension (``tripl._bundled_enterprise``)
wires these functions to the hooks.
"""

from __future__ import annotations

import uuid

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.extensions import GateRefused
from tripl.middleware.org_context import OrgRef, current_org
from tripl.models.domain_enums import OrganizationRole
from tripl.models.user import User
from tripl.models.user_session import AUTH_METHOD_SSO
from tripl.services import org_sso_service, project_access

SSO_REQUIRED = "This organization requires single sign-on"


class SsoRequiredError(GateRefused):
    """403 of an organization that requires SSO (F20).

    Answered as ``{"detail": ..., "sso_start": <login path>}`` so the SPA can
    offer the organization's sign-in.
    """

    def __init__(self, org_slug: str) -> None:
        self.sso_start = org_sso_service.login_path(org_slug)
        super().__init__(SSO_REQUIRED, extra={"sso_start": self.sso_start})


async def _sso_required(request: Request, session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Whether ``org_id`` requires SSO; asked once per request and organization."""
    cache: dict[uuid.UUID, bool] | None = getattr(request.state, "sso_required_orgs", None)
    if cache is None:
        cache = {}
        request.state.sso_required_orgs = cache
    if org_id not in cache:
        cache[org_id] = await org_sso_service.sso_required(session, org_id)
    return cache[org_id]


async def refuse_non_sso_session(
    request: Request,
    session: AsyncSession,
    user: User,
    org: OrgRef,
    *,
    role: OrganizationRole | None = None,
) -> None:
    """403 :class:`SsoRequiredError` for a browser session that did not sign in through ``org``.

    The gate of an organization's "SSO required" (F20), for cookie sessions (API
    keys: :func:`refuse_key_without_sso`). Passes a session of
    ``auth_method='sso'`` for this very organization, the organization's OWNERS
    (break-glass: an owner can always sign in with a password and fix a broken
    provider), a platform admin's read-only step-in, and ``/api/v1/auth/``.
    ``role`` saves a lookup when the caller already knows it.
    """
    from tripl.api.deps import _UNVERIFIED_ALLOWED_PREFIX, _app_path

    if org.step_in_user_id is not None:
        return
    if getattr(request.state, "api_key_scope", None) is not None:
        return
    if _app_path(request).startswith(_UNVERIFIED_ALLOWED_PREFIX):
        return
    if not await _sso_required(request, session, org.id):
        return
    if (
        getattr(request.state, "session_auth_method", None) == AUTH_METHOD_SSO
        and getattr(request.state, "session_sso_org_id", None) == org.id
    ):
        return
    if role is None:
        role = await project_access.org_role_of(session, user.id, org.id)
    if role == OrganizationRole.owner:
        return
    raise SsoRequiredError(org.slug)


async def refuse_key_without_sso(
    request: Request,
    session: AsyncSession,
    user: User,
    org_id: uuid.UUID,
    api_key: object,
) -> None:
    """An API key in an organization requiring SSO must be minted from its SSO session.

    Owners included: their break-glass is a password sign-in to the app, not a
    key that outlives turning "SSO required" on (such keys are revoked then).
    """
    if getattr(api_key, "created_with_sso_org_id", None) == org_id:
        return
    if not await _sso_required(request, session, org_id):
        return
    org = current_org()
    raise SsoRequiredError(org.slug if org is not None else "")


async def refuse_key_mint(session: AsyncSession, org: OrgRef) -> None:
    """403 when ``org`` requires SSO and the minting session did not sign in through it.

    A non-owner is stopped earlier (:func:`refuse_non_sso_session`); an owner's
    password session (the break-glass) passes that gate but mints no key there:
    owners' keys obey "SSO required" like everyone's (F20).
    """
    if await org_sso_service.sso_required(session, org.id):
        raise SsoRequiredError(org.slug)
