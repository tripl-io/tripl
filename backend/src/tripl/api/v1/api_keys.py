"""Per-user API-key management.

Mounted under ``/api/v1/me/api-keys`` so each user manages their own keys —
no cross-user listing or revocation, even for owners. The full bearer token
is returned exactly once at creation; subsequent ``GET`` only exposes the
non-secret prefix.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.api.deps import (
    CurrentUserDep,
    SessionDep,
    SsoRequiredError,
    WriteUserDep,
    require_org_member,
)
from tripl.middleware.org_context import current_org, require_org_id
from tripl.models.user_session import AUTH_METHOD_SSO
from tripl.schemas.api_key import (
    ApiKeyCreate,
    ApiKeyCreateResponse,
    ApiKeyResponse,
)
from tripl.services import api_key_service, audit_service, org_sso_service, project_lookup
from tripl.services.project_access import member_role
from tripl.services.project_lookup import PROJECT_NOT_FOUND

router = APIRouter(prefix="/me/api-keys", tags=["api-keys"])


@router.get("", response_model=list[ApiKeyResponse])
async def list_api_keys(session: SessionDep, current_user: CurrentUserDep) -> list[ApiKeyResponse]:
    # Only the keys of the request's organization: a key acts in exactly one.
    rows = await api_key_service.list_keys(session, current_user.id, require_org_id())
    return [ApiKeyResponse.model_validate(row) for row in rows]


@router.post("", response_model=ApiKeyCreateResponse, status_code=201)
async def create_api_key(
    request: Request,
    session: SessionDep,
    current_user: WriteUserDep,
    data: ApiKeyCreate,
) -> ApiKeyCreateResponse:
    _require_session_auth(request)
    if data.scope == "write":
        # A write key needs membership of the organization it will act in; what
        # it may write inside a project is still decided per request by the
        # project role of the user who minted it.
        await require_org_member(session, current_user)

    # A project-bound key validates the slug up front so operators can't mint
    # a key pointing at a project that doesn't exist — or at one they are not a
    # member of, which answers the same 404 so the slug is no oracle. (An
    # unbound key acts with its user's membership on every request.)
    project_id = (
        await project_lookup.resolve_project_id(session, data.project_slug)
        if data.project_slug is not None
        else None
    )
    if project_id is not None and await member_role(session, current_user, project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=PROJECT_NOT_FOUND)
    sso_org_id = _sso_org_of_session(request)
    if sso_org_id is None:
        await _refuse_in_sso_required_org(session)
    row, raw_token = await api_key_service.create_key(
        session,
        current_user.id,
        name=data.name,
        scope=data.scope,
        expires_in_days=data.expires_in_days,
        project_id=project_id,
        created_with_sso_org_id=sso_org_id,
    )
    await audit_service.record(
        session,
        user=current_user,
        action="api_key.create",
        target_type="api_key",
        target_id=row.id,
        target_name=row.name,
        project_slug=data.project_slug,
        payload={"scope": row.scope, "project_id": str(project_id) if project_id else None},
    )
    return ApiKeyCreateResponse(
        id=row.id,
        name=row.name,
        key_prefix=row.key_prefix,
        scope=row.scope,
        project_id=row.project_id,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        token=raw_token,
    )


@router.delete("/{key_id}", status_code=204)
async def revoke_api_key(
    request: Request, session: SessionDep, current_user: WriteUserDep, key_id: uuid.UUID
) -> None:
    _require_session_auth(request)
    revoked = await api_key_service.revoke_key(session, current_user.id, key_id)
    if revoked is None:
        return
    row, project_slug = revoked
    await audit_service.record(
        session,
        user=current_user,
        action="api_key.revoke",
        target_type="api_key",
        target_id=key_id,
        target_name=row.name,
        project_slug=project_slug,
    )


def _sso_org_of_session(request: Request) -> uuid.UUID | None:
    """The request's organization when this session signed in through its SSO (F20).

    In an organization that requires SSO, a key minted from any other session
    would be refused at every use, so none is minted
    (:func:`_refuse_in_sso_required_org`).
    """
    org_id = require_org_id()
    if (
        getattr(request.state, "session_auth_method", None) == AUTH_METHOD_SSO
        and getattr(request.state, "session_sso_org_id", None) == org_id
    ):
        return org_id
    return None


async def _refuse_in_sso_required_org(session: AsyncSession) -> None:
    """403 ``SsoRequiredError`` when the request's organization requires SSO.

    For a session that did not sign in through it. A non-owner is stopped
    earlier (``deps.refuse_non_sso_session``); an owner's password session (the
    break-glass) passes that gate but mints no key there: owners' keys obey
    "SSO required" like everyone's (F20).
    """
    org = current_org()
    if org is None or org.step_in_user_id is not None:
        return
    if await org_sso_service.sso_required(session, org.id):
        raise SsoRequiredError(org.slug)


def _require_session_auth(request: Request) -> None:
    if getattr(request.state, "api_key_scope", None) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key management requires a user session",
        )
