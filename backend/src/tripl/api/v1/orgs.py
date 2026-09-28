"""Organization management (F20 PR6, GH #273).

Real routes under ``/api/v1/orgs``; ``orgs`` is not in ``ORG_REWRITE_PREFIXES``,
so ``OrgPathRewriteMiddleware`` never rewrites them. Everything under
``/orgs/{org}`` is gated on a membership of THAT organization
(``deps._resolve_path_org``): anyone else — an unknown slug, a ``deleting``
organization, a non-member, an API key of another organization — gets the same
404, before any 403.

* ``GET /orgs`` — the caller's organizations with their role (an API key: its own).
* ``POST /orgs`` — a platform admin, from a browser session, in both deployment
  modes until hosted sign-up (critique #24). The creator becomes the owner.
* ``GET /orgs/{org}`` and ``GET /orgs/{org}/members`` — any member.
* ``PATCH /orgs/{org}`` (name only; the slug is permanent), member role change
  and removal — an owner or admin. Owners are managed by owners only, and the
  last owner can be neither demoted nor removed.
* ``DELETE /orgs/{org}`` and ``POST /orgs/{org}/transfer-ownership`` — an owner.

Invitations into an organization stay where they were: ``/orgs/{org}/users/
invitations`` is the rewritten form of ``/users/invitations``.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, Request, status

from tripl.api.deps import (
    CurrentUserDep,
    ManagedOrgDep,
    PathOrgAdminUserDep,
    PathOrgMemberUserDep,
    PathOrgOwnerUserDep,
    PlatformAdminUserDep,
    SessionDep,
)
from tripl.models.domain_enums import OrganizationRole, OrganizationStatus
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orgs", tags=["organizations"])

OWNER_MANAGEMENT_REQUIRED = "Only an owner can manage owners"
LAST_OWNER = "Cannot remove or demote the last remaining owner"
MEMBER_NOT_FOUND = "Member not found"


@router.get("", response_model=list[OrgResponse])
async def list_orgs(
    request: Request, session: SessionDep, current_user: CurrentUserDep
) -> list[OrgResponse]:
    """The organizations the caller belongs to, with their role in each.

    An API key belongs to one organization and lists only that one.
    """
    return await org_service.list_my_orgs(
        session,
        current_user.id,
        only_org_id=getattr(request.state, "api_key_org_id", None),
    )


@router.post("", response_model=OrgResponse, status_code=status.HTTP_201_CREATED)
async def create_org(
    session: SessionDep, data: OrgCreate, current_user: PlatformAdminUserDep
) -> OrgResponse:
    """Create an organization; the platform admin who creates it becomes its owner."""
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
async def rename_org(
    session: SessionDep,
    data: OrgUpdate,
    current_user: PathOrgAdminUserDep,
    org: ManagedOrgDep,
) -> OrgResponse:
    """Rename the organization. Only the name: a ``slug`` in the body is a 422."""
    renamed = await org_service.rename_org(session, org, data.name)
    await audit_service.record(
        session,
        user=current_user,
        action="org.rename",
        target_type="organization",
        target_id=org.id,
        target_name=org.slug,
        payload={"old_name": org.name, "new_name": renamed.name},
    )
    return org_service.org_response(renamed)


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
