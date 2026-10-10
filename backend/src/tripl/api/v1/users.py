"""The members of the request's organization (F20 PR4).

Every route here works on ``organization_members`` of the bound organization:
the roster, organization roles (owner | admin | member) and invitations into
that organization. There is no instance-wide role.

* ``GET /users`` — any member of the organization; a non-member gets 403.
* invitations and ``PATCH /users/{id}`` — an owner or admin of the organization
  from a browser session (``OwnerUserDep``); making, inviting or unmaking an
  OWNER takes an owner (403 "Only an owner can manage owners").

A role change takes effect on the member's next request (roles are read from the
database every time) and does not sign them out.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status

from tripl import tenancy
from tripl.api.deps import (
    OrgMemberUserDep,
    OwnerUserDep,
    SessionDep,
    request_org_role,
)
from tripl.api.v1._members import OWNER_MANAGEMENT_REQUIRED, change_member_role
from tripl.middleware.org_context import require_org_id
from tripl.models.domain_enums import OrganizationRole
from tripl.schemas.auth import UserListItem, UserRoleUpdate
from tripl.schemas.invitation import (
    InvitationCreate,
    InvitationCreatedResponse,
    InvitationResponse,
)
from tripl.schemas.pagination import Offset
from tripl.services import audit_service, invitation_email, invitation_service, user_service

router = APIRouter(prefix="/users", tags=["users"])


@router.post(
    "/invitations",
    response_model=InvitationCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_invitation(
    request: Request,
    session: SessionDep,
    data: InvitationCreate,
    current_user: OwnerUserDep,
    background_tasks: BackgroundTasks,
) -> InvitationCreatedResponse:
    """Invite one person into the request's organization, at an organization role.

    ``OwnerUserDep`` is org owner/admin-only AND rejects API keys of any scope, so
    minting an account always requires an interactive session — an automation
    token can never conjure a new identity. Inviting at ``owner`` takes an
    owner: an admin cannot mint an account more privileged than their own.

    The redeem link is returned in the body, not merely emailed: SMTP is
    optional and unconfigured on many instances, so a body-only path is the one
    that always works. It appears here and nowhere else. When the operator has
    SMTP configured the link is also mailed, through the operator's relay (never
    an organization's), after the response.

    On a public demo the link is the only delivery: no mail is prepared or
    sent. Only the member organization role is allowed, with at most ten
    members plus unexpired pending invitations (409 when full), and ten mints
    per rolling hour per organization and inviter (429 when exhausted).

    The invitation belongs to the organization the request acts in: the one an
    ``/orgs/{org}/users/invitations`` URL names, else the legacy default.
    """
    if (
        data.role == OrganizationRole.owner
        and await request_org_role(request, session, current_user) != OrganizationRole.owner
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=OWNER_MANAGEMENT_REQUIRED)
    invitation, raw_token = await invitation_service.create_invitation(
        session,
        email=data.email,
        org_role=data.role,
        organization_id=require_org_id(),
        invited_by_user_id=current_user.id,
    )
    # The service files the ``user.invite`` audit row in the mint's transaction.
    accept_path = f"/invite/{raw_token}"
    if not tenancy.public_demo():
        mail = await invitation_email.prepare(
            session,
            recipient=invitation.email,
            organization_id=invitation.organization_id,
            accept_path=accept_path,
        )
        if mail is not None:
            background_tasks.add_task(invitation_email.send, mail)
    return InvitationCreatedResponse(
        invitation=InvitationResponse.model_validate(invitation),
        accept_path=accept_path,
        expires_at=invitation.expires_at,
    )


@router.get("/invitations", response_model=list[InvitationResponse])
async def list_invitations(
    session: SessionDep,
    current_user: OwnerUserDep,
) -> list[InvitationResponse]:
    """Outstanding invitations into the organization: the roster of pending access."""
    del current_user
    rows = await invitation_service.list_pending_invitations(session, require_org_id())
    return [InvitationResponse.model_validate(row) for row in rows]


@router.delete("/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(
    session: SessionDep,
    invitation_id: uuid.UUID,
    current_user: OwnerUserDep,
) -> None:
    """Revoke an invitation into the organization; its link stops working immediately."""
    await invitation_service.revoke_invitation(session, invitation_id, require_org_id())
    await audit_service.record(
        session,
        user=current_user,
        action="user.invite_revoke",
        target_type="invitation",
        target_id=invitation_id,
        target_name=str(invitation_id),
        payload={},
    )


@router.get("", response_model=list[UserListItem])
async def list_users(
    session: SessionDep,
    current_user: OrgMemberUserDep,
    limit: int = Query(user_service.ROSTER_PAGE_DEFAULT, ge=1, le=user_service.ROSTER_PAGE_MAX),
    offset: Offset = 0,
) -> list[UserListItem]:
    """The members of the request's organization with their organization role.

    Any member may see the roster (it feeds the member pickers); a signed-in
    account outside the organization gets 403. One page, oldest account first:
    page on with ``offset`` until a page comes back shorter than ``limit``.
    """
    del current_user
    return await user_service.list_org_users(session, require_org_id(), limit=limit, offset=offset)


@router.patch("/{user_id}", response_model=UserListItem)
async def update_user_role(
    session: SessionDep,
    user_id: uuid.UUID,
    data: UserRoleUpdate,
    current_user: OwnerUserDep,
) -> UserListItem:
    """Change a member's ORGANIZATION role (owner | admin | member).

    404 for an account outside the organization, 400 when it would leave the
    organization without an owner, 403 when an admin tries to make or unmake an
    owner. The member stays signed in; the new role applies from their next
    request. The same change, errors and ``org.member_role_update`` audit row
    as ``PATCH /orgs/{org}/members/{id}``.
    """
    return await change_member_role(
        session, org_id=require_org_id(), user_id=user_id, role=data.role, actor=current_user
    )
