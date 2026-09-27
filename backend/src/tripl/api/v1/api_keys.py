"""Per-user API-key management.

Mounted under ``/api/v1/me/api-keys`` so each user manages their own keys —
no cross-user listing or revocation, even for owners. The full bearer token
is returned exactly once at creation; subsequent ``GET`` only exposes the
non-secret prefix.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, status

from tripl.api.deps import CurrentUserDep, SessionDep, WriteUserDep, require_org_member
from tripl.middleware.org_context import require_org_id
from tripl.schemas.api_key import (
    ApiKeyCreate,
    ApiKeyCreateResponse,
    ApiKeyResponse,
)
from tripl.services import api_key_service, audit_service, project_lookup
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
    row, raw_token = await api_key_service.create_key(
        session,
        current_user.id,
        name=data.name,
        scope=data.scope,
        expires_in_days=data.expires_in_days,
        project_id=project_id,
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


def _require_session_auth(request: Request) -> None:
    if getattr(request.state, "api_key_scope", None) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key management requires a user session",
        )
