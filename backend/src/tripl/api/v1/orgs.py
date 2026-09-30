"""Organization management (F20 PR6, GH #273).

Real routes under ``/api/v1/orgs``; ``orgs`` is not in ``ORG_REWRITE_PREFIXES``,
so ``OrgPathRewriteMiddleware`` never rewrites them. Everything under
``/orgs/{org}`` is gated on a membership of THAT organization
(``deps._resolve_path_org``): anyone else — an unknown slug, a ``deleting``
organization, a non-member, an API key of another organization — gets the same
404, before any 403.

* ``GET /orgs`` — the caller's organizations with their role and status (an API
  key: its own); a platform admin's live step-ins too, flagged ``step_in``.
* ``POST /orgs`` — from a browser session: a platform admin on a self-hosted
  instance; any signed-in account on a hosted one (critique #24), which the
  hosted email-verification gate has already held to a verified address. The
  creator becomes the owner. Hosted sign-up (``POST /auth/register``) creates
  the account's first organization the same way.
* ``GET /orgs/{org}`` and ``GET /orgs/{org}/members`` — any member.
* ``PATCH /orgs/{org}`` (the name and the default project role; the slug is
  permanent), member role change and removal — an owner or admin. Owners are
  managed by owners only, and the last owner can be neither demoted nor removed.
* ``DELETE /orgs/{org}`` and ``POST /orgs/{org}/transfer-ownership`` — an owner.

Invitations into an organization stay where they were: ``/orgs/{org}/users/
invitations`` is the rewritten form of ``/users/invitations``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from tripl.api.deps import (
    CurrentUserDep,
    ManagedOrgDep,
    PathOrgAdminUserDep,
    PathOrgMemberUserDep,
    PathOrgOwnerUserDep,
    SessionDep,
    require_org_creator,
)
from tripl.models.domain_enums import OrganizationRole, OrganizationStatus
from tripl.models.user import User
from tripl.schemas.auth import UserListItem, UserRoleUpdate
from tripl.schemas.organization import (
    OrgCreate,
    OrgDeleteRequest,
    OrgMemberRemoved,
    OrgResponse,
    OrgTransferOwnership,
    OrgUpdate,
)
from tripl.services import (
    audit_service,
    org_deletion_service,
    org_service,
    user_service,
)
from tripl.services._celery_dispatch import dispatch
from tripl.services.org_resolution import ORG_NOT_FOUND, suspended_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orgs", tags=["organizations"])

OWNER_MANAGEMENT_REQUIRED = "Only an owner can manage owners"
BROWSER_SESSION_REQUIRED = "A browser session is required"
LAST_OWNER = "Cannot remove or demote the last remaining owner"
MEMBER_NOT_FOUND = "Member not found"


@router.get("", response_model=list[OrgResponse])
async def list_orgs(
    request: Request, session: SessionDep, current_user: CurrentUserDep
) -> list[OrgResponse]:
    """The organizations the caller belongs to, with their role in each.

    An API key belongs to one organization and lists only that one. A
    suspended organization is listed with its ``status`` (F20 PR14). A platform
    admin's browser session also lists the organizations they have a live
    read-only step-in to, flagged ``step_in``.
    """
    key_org_id = getattr(request.state, "api_key_org_id", None)
    return await org_service.list_my_orgs(
        session,
        current_user.id,
        only_org_id=key_org_id,
        include_step_ins=bool(current_user.is_platform_admin) and key_org_id is None,
    )


OrgCreatorDep = Annotated[User, Depends(require_org_creator)]


@router.post("", response_model=OrgResponse, status_code=status.HTTP_201_CREATED)
async def create_org(
    session: SessionDep, data: OrgCreate, current_user: OrgCreatorDep
) -> OrgResponse:
    """Create an organization; its creator becomes its owner."""
    try:
        org = await org_service.create_org(
            session, creator=current_user, slug=data.slug, name=data.name
        )
    except org_service.OrgSlugTakenError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An organization with slug '{data.slug}' already exists",
        ) from None
    await audit_service.record(
        session,
        user=current_user,
        action="org.create",
        target_type="organization",
        target_id=org.id,
        target_name=org.slug,
        payload={"slug": org.slug, "name": org.name},
        organization_id=org.id,
    )
    return org_service.org_response(org)


@router.get("/{org}", response_model=OrgResponse)
async def get_org(current_user: PathOrgMemberUserDep, org: ManagedOrgDep) -> OrgResponse:
    del current_user
    return org_service.org_response(org)


@router.patch("/{org}", response_model=OrgResponse)
async def update_org(
    session: SessionDep,
    data: OrgUpdate,
    current_user: PathOrgAdminUserDep,
    org: ManagedOrgDep,
) -> OrgResponse:
    """Rename the organization and/or set its default project role.

    The slug is permanent: a ``slug`` in the body is a 422, and so is a
    ``default_project_role`` of ``owner``. Audited as ``org.update`` with the
    changed fields before and after.
    """
    updated = await org_service.update_org(
        session, org, name=data.name, default_project_role=data.default_project_role
    )
    before: dict[str, str] = {}
    after: dict[str, str] = {}
    for field in ("name", "default_project_role"):
        old, new = getattr(org, field), getattr(updated, field)
        if old != new:
            before[field], after[field] = old, new
    if after:
        await audit_service.record(
            session,
            user=current_user,
            action="org.update",
            target_type="organization",
            target_id=org.id,
            target_name=org.slug,
            payload={"before": before, "after": after},
        )
    else:
        await session.commit()
    return org_service.org_response(updated)


@router.delete("/{org}", response_model=OrgResponse, status_code=status.HTTP_202_ACCEPTED)
async def delete_org(
    session: SessionDep,
    data: OrgDeleteRequest,
    current_user: PathOrgOwnerUserDep,
    org: ManagedOrgDep,
) -> OrgResponse:
    """Start deleting the organization: 202, then a background job purges it.

    The body must repeat the slug (``{"confirm_slug": "<slug>"}``). The default
    organization cannot be deleted. From this response on the organization
    answers 404 everywhere.
    """
    try:
        await org_deletion_service.request_deletion(
            session, org_id=org.id, slug=org.slug, confirm_slug=data.confirm_slug
        )
    except org_deletion_service.DefaultOrgUndeletableError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The default organization cannot be deleted",
        ) from None
    except org_deletion_service.ConfirmationMismatchError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Type the organization's slug exactly to confirm the deletion",
        ) from None
    except org_deletion_service.OrgNotActiveError as exc:
        # Suspended (or deleted) after the gate admitted the request.
        if exc.suspended:
            raise suspended_error() from None
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ORG_NOT_FOUND) from None
    await audit_service.record(
        session,
        user=current_user,
        action="org.delete_request",
        target_type="organization",
        target_id=org.id,
        target_name=org.slug,
        payload={"slug": org.slug, "name": org.name},
    )

    from tripl.worker.tasks.org_delete import purge_organization

    try:
        await dispatch(purge_organization.delay, str(org.id))
    except Exception:
        logger.exception("could not queue the purge of organization %s", org.slug)
        await org_deletion_service.cancel_deletion(session, org.id, user=current_user)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The deletion could not be queued; the organization is still active",
        ) from None
    return org_service.org_response(
        org_service.ManagedOrg(
            id=org.id,
            slug=org.slug,
            name=org.name,
            role=org.role,
            status=OrganizationStatus.deleting.value,
            created_at=org.created_at,
            default_project_role=org.default_project_role,
        )
    )


@router.get("/{org}/members", response_model=list[UserListItem])
async def list_members(
    session: SessionDep,
    current_user: PathOrgMemberUserDep,
    org: ManagedOrgDep,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[UserListItem]:
    del current_user
    return await user_service.list_org_users(session, org.id, limit=limit, offset=offset)


@router.patch("/{org}/members/{user_id}", response_model=UserListItem)
async def update_member_role(
    session: SessionDep,
    user_id: uuid.UUID,
    data: UserRoleUpdate,
    current_user: PathOrgAdminUserDep,
    org: ManagedOrgDep,
) -> UserListItem:
    """Change a member's organization role (owner | admin | member)."""
    try:
        target, old_role, invitations = await user_service.update_org_role(
            session, org.id, user_id, data.role, actor_id=current_user.id
        )
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MEMBER_NOT_FOUND
        ) from None
    except user_service.LastOwnerError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=LAST_OWNER) from None
    except user_service.OwnerManagementError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=OWNER_MANAGEMENT_REQUIRED
        ) from None
    await audit_service.record(
        session,
        user=current_user,
        action="org.member_role_update",
        target_type="user",
        target_id=target.id,
        target_name=target.email,
        payload={
            "old_role": old_role,
            "new_role": OrganizationRole(data.role).value,
            "invitations_revoked": invitations,
        },
    )
    return target


@router.delete("/{org}/members/{user_id}", response_model=OrgMemberRemoved)
async def remove_member(
    session: SessionDep,
    user_id: uuid.UUID,
    current_user: PathOrgAdminUserDep,
    org: ManagedOrgDep,
) -> OrgMemberRemoved:
    """Remove a member: their membership, project rows, keys and pending invitations here."""
    try:
        removed = await org_service.remove_member(
            session, org.id, user_id, actor_id=current_user.id
        )
    except org_service.MemberNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MEMBER_NOT_FOUND
        ) from None
    except user_service.LastOwnerError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=LAST_OWNER) from None
    except user_service.OwnerManagementError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=OWNER_MANAGEMENT_REQUIRED
        ) from None
    await audit_service.record(
        session,
        user=current_user,
        action="org.member_remove",
        target_type="user",
        target_id=removed.user.id,
        target_name=removed.user.email,
        payload={
            "old_role": removed.old_role,
            "project_memberships": removed.project_memberships,
            "api_keys": removed.api_keys,
            "invitations": removed.invitations,
            "group_memberships": removed.group_memberships,
            "sso_identities": removed.sso_identities,
            "scim_tokens": removed.scim_tokens,
        },
    )
    return OrgMemberRemoved(
        user_id=removed.user.id,
        project_memberships_removed=removed.project_memberships,
        api_keys_revoked=removed.api_keys,
        invitations_revoked=removed.invitations,
        group_memberships_removed=removed.group_memberships,
    )


@router.post("/{org}/transfer-ownership", response_model=UserListItem)
async def transfer_ownership(
    session: SessionDep,
    data: OrgTransferOwnership,
    current_user: PathOrgOwnerUserDep,
    org: ManagedOrgDep,
) -> UserListItem:
    """Make another member an owner and step the caller down to admin."""
    try:
        target, old_role = await org_service.transfer_ownership(
            session, org.id, actor_id=current_user.id, target_id=data.user_id
        )
    except org_service.SelfTransferError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You already own this organization; pick another member",
        ) from None
    except org_service.MemberNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MEMBER_NOT_FOUND
        ) from None
    except user_service.OwnerManagementError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=OWNER_MANAGEMENT_REQUIRED
        ) from None
    await audit_service.record(
        session,
        user=current_user,
        action="org.transfer_ownership",
        target_type="user",
        target_id=target.id,
        target_name=target.email,
        payload={"old_role": old_role, "previous_owner": current_user.email},
    )
    return UserListItem(
        id=target.id,
        email=target.email,
        name=target.name,
        role=OrganizationRole.owner,
        created_at=target.created_at,
    )
