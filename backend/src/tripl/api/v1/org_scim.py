"""An organization's SCIM provisioning settings: ``/api/v1/orgs/{org}/scim`` (F20, GH #273).

Every route is for an OWNER of the path's organization, from a browser session
(``get_path_org_owner_user``): an admin, a member and an API key get 403, a
stranger 404. A SCIM token provisions and deprovisions accounts, so it is an
owner's to hand to an identity provider.

* ``GET/POST /scim/tokens``, ``DELETE /scim/tokens/{token_id}`` — the bearer
  tokens. The raw token is in the ``POST`` answer and never again; at most
  ``scim_token_service.MAX_LIVE_TOKENS`` live ones (409 beyond).
* ``GET/PUT /scim/config`` — the SCIM base URL to give the provider and the
  admin-group mapping (members of that group hold organization role
  ``admin``; applied at once).

The SCIM protocol itself is served at ``/scim/v2/{org}`` (:mod:`tripl.api.scim`).
Every change is audited as ``org.scim.*``, never with a token in the payload.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status

from tripl.api.deps import ManagedOrgDep, PathOrgOwnerUserDep, SessionDep
from tripl.api.v1.auth_sso import app_base_url
from tripl.schemas.org_scim import (
    OrgScimConfigResponse,
    OrgScimConfigUpdate,
    OrgScimTokenCreated,
    OrgScimTokenResponse,
)
from tripl.services import (
    audit_service,
    org_group_service,
    scim_config_service,
    scim_token_service,
)

router = APIRouter(prefix="/orgs/{org}/scim", tags=["organizations"])

TOKEN_NOT_FOUND = "SCIM token not found"
GROUP_NOT_FOUND = "Group not found"


async def _base_url(session: SessionDep, request: Request, org_slug: str) -> str:
    return scim_config_service.scim_base_url(await app_base_url(session, request), org_slug)


@router.get("/tokens", response_model=list[OrgScimTokenResponse])
async def list_scim_tokens(
    session: SessionDep, current_user: PathOrgOwnerUserDep, org: ManagedOrgDep
) -> list[OrgScimTokenResponse]:
    del current_user
    return await scim_token_service.list_tokens(session, org.id)


@router.post("/tokens", response_model=OrgScimTokenCreated, status_code=status.HTTP_201_CREATED)
async def create_scim_token(
    session: SessionDep, current_user: PathOrgOwnerUserDep, org: ManagedOrgDep
) -> OrgScimTokenCreated:
    """A new token. The answer carries it in ``token``; it is not shown again."""
    try:
        token, raw = await scim_token_service.create_token(
            session, org.id, created_by=current_user.id
        )
    except scim_token_service.TooManyTokensError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"An organization may hold at most {scim_token_service.MAX_LIVE_TOKENS} "
                "live SCIM tokens; revoke one first"
            ),
        ) from None
    body = OrgScimTokenCreated(
        **scim_token_service.token_response(token, current_user.email).model_dump(),
        token=raw,
    )
    await audit_service.record(
        session,
        user=current_user,
        action="org.scim.token_create",
        target_type="scim_token",
        target_id=token.id,
        target_name=token.prefix,
        payload={"token_prefix": token.prefix},
        organization_id=org.id,
    )
    return body


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_scim_token(
    session: SessionDep,
    token_id: uuid.UUID,
    current_user: PathOrgOwnerUserDep,
    org: ManagedOrgDep,
) -> Response:
    """Revoke a token at once. Revoking a revoked one is a no-op (204, no audit row)."""
    try:
        token, was_live = await scim_token_service.revoke_token(session, org.id, token_id)
    except scim_token_service.TokenNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=TOKEN_NOT_FOUND) from None
    if was_live:
        await audit_service.record(
            session,
            user=current_user,
            action="org.scim.token_revoke",
            target_type="scim_token",
            target_id=token.id,
            target_name=token.prefix,
            payload={"token_prefix": token.prefix},
            organization_id=org.id,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/config", response_model=OrgScimConfigResponse)
async def get_scim_config(
    request: Request, session: SessionDep, current_user: PathOrgOwnerUserDep, org: ManagedOrgDep
) -> OrgScimConfigResponse:
    del current_user
    return await scim_config_service.config_response(
        session, org.id, base_url=await _base_url(session, request, org.slug)
    )


@router.put("/config", response_model=OrgScimConfigResponse)
async def put_scim_config(
    request: Request,
    session: SessionDep,
    data: OrgScimConfigUpdate,
    current_user: PathOrgOwnerUserDep,
    org: ManagedOrgDep,
) -> OrgScimConfigResponse:
    """Map a group to organization role ``admin`` (null unmaps); applied at once.

    404 for a group this organization does not have.
    """
    try:
        saved = await scim_config_service.set_admin_group(session, org.id, data.admin_group_id)
    except org_group_service.GroupNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GROUP_NOT_FOUND) from None
    body = await scim_config_service.config_response(
        session, org.id, base_url=await _base_url(session, request, org.slug)
    )
    if saved.old_group_id != saved.new_group_id:
        await audit_service.record(
            session,
            user=current_user,
            action="org.scim.config_update",
            target_type="organization",
            target_id=org.id,
            target_name=org.slug,
            payload={
                "admin_group_id": {
                    "old": str(saved.old_group_id) if saved.old_group_id else None,
                    "new": str(saved.new_group_id) if saved.new_group_id else None,
                },
                "role_changes": len(saved.changes),
            },
            organization_id=org.id,
        )
    else:
        await session.commit()
    return body
