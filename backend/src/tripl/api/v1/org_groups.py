"""Organization groups (F20, GH #273): ``/api/v1/orgs/{org}/groups``.

Real routes: ``groups`` is not in ``ORG_REWRITE_PREFIXES`` and has no legacy
form. Every route is gated on the path's organization first
(``deps._resolve_path_org``): a stranger gets 404 before any 403, and a group id
of another organization answers 404 like an unknown one.

* ``GET /orgs/{org}/groups`` and ``GET /orgs/{org}/groups/{group_id}`` — any
  member of the organization (an API key of it too).
* create, rename/describe, delete, member add/remove — an owner or admin of
  the organization, from a browser session. A member added must be a member of
  the organization (404 otherwise).

Every change is audited as ``org.group.*``. A group managed by the
organization's SCIM provisioning (``managed_by_scim``) answers 409 to a rename,
a delete and a member change here: the identity provider owns it. Its
description stays editable. Consumers (F24 note sharing, event-type owners, alert routing) read
groups through ``org_group_service.group_member_ids``.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Response, status

from tripl.api.deps import (
    ManagedOrgDep,
    PathOrgAdminUserDep,
    PathOrgMemberUserDep,
    SessionDep,
)
from tripl.api.v1._members import MEMBER_NOT_FOUND
from tripl.models.organization_group import OrganizationGroup
from tripl.schemas.organization_group import (
    OrgGroupCreate,
    OrgGroupDetail,
    OrgGroupMemberAdd,
    OrgGroupMemberResponse,
    OrgGroupResponse,
    OrgGroupUpdate,
)
from tripl.services import audit_service, org_group_service

router = APIRouter(prefix="/orgs/{org}/groups", tags=["organizations"])

GROUP_NOT_FOUND = "Group not found"
MANAGED_BY_SCIM = "This group is managed by SCIM provisioning; change it in the identity provider"


def _managed() -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=MANAGED_BY_SCIM)


def _name_taken(name: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"A group named '{name}' already exists in this organization",
    )


async def _group(session: SessionDep, org: ManagedOrgDep, group_id: uuid.UUID) -> OrganizationGroup:
    try:
        return await org_group_service.get_group(session, org.id, group_id)
    except org_group_service.GroupNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GROUP_NOT_FOUND) from None


@router.get("", response_model=list[OrgGroupResponse])
async def list_groups(
    session: SessionDep, current_user: PathOrgMemberUserDep, org: ManagedOrgDep
) -> list[OrgGroupResponse]:
    del current_user
    return await org_group_service.list_groups(session, org.id)


@router.post("", response_model=OrgGroupDetail, status_code=status.HTTP_201_CREATED)
async def create_group(
    session: SessionDep,
    data: OrgGroupCreate,
    current_user: PathOrgAdminUserDep,
    org: ManagedOrgDep,
) -> OrgGroupDetail:
    try:
        group = await org_group_service.create_group(
            session, org.id, name=data.name, description=data.description
        )
    except org_group_service.GroupNameTakenError:
        raise _name_taken(data.name) from None
    detail = await org_group_service.group_detail(session, group)
    await audit_service.record(
        session,
        user=current_user,
        action="org.group.create",
        target_type="organization_group",
        target_id=group.id,
        target_name=group.name,
        payload={"name": group.name, "description": group.description},
    )
    return detail


@router.get("/{group_id}", response_model=OrgGroupDetail)
async def get_group(
    session: SessionDep,
    group_id: uuid.UUID,
    current_user: PathOrgMemberUserDep,
    org: ManagedOrgDep,
) -> OrgGroupDetail:
    del current_user
    return await org_group_service.group_detail(session, await _group(session, org, group_id))


@router.patch("/{group_id}", response_model=OrgGroupDetail)
async def update_group(
    session: SessionDep,
    group_id: uuid.UUID,
    data: OrgGroupUpdate,
    current_user: PathOrgAdminUserDep,
    org: ManagedOrgDep,
) -> OrgGroupDetail:
    """Rename the group and/or change its description."""
    group = await _group(session, org, group_id)
    try:
        changes = await org_group_service.update_group(
            session, group, name=data.name, description=data.description
        )
    except org_group_service.GroupNameTakenError:
        raise _name_taken(data.name or "") from None
    except org_group_service.GroupManagedByScimError:
        raise _managed() from None
    detail = await org_group_service.group_detail(session, group)
    if changes:
        await audit_service.record(
            session,
            user=current_user,
            action="org.group.update",
            target_type="organization_group",
            target_id=group.id,
            target_name=group.name,
            payload={"changes": changes},
        )
    return detail


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    session: SessionDep,
    group_id: uuid.UUID,
    current_user: PathOrgAdminUserDep,
    org: ManagedOrgDep,
) -> Response:
    group = await _group(session, org, group_id)
    name = group.name
    try:
        members = await org_group_service.delete_group(session, group)
    except org_group_service.GroupManagedByScimError:
        raise _managed() from None
    await audit_service.record(
        session,
        user=current_user,
        action="org.group.delete",
        target_type="organization_group",
        target_id=group_id,
        target_name=name,
        payload={"name": name, "members": members},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{group_id}/members",
    response_model=OrgGroupMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_group_member(
    session: SessionDep,
    group_id: uuid.UUID,
    data: OrgGroupMemberAdd,
    current_user: PathOrgAdminUserDep,
    org: ManagedOrgDep,
) -> OrgGroupMemberResponse:
    """Add a member of the organization to the group."""
    group = await _group(session, org, group_id)
    try:
        user = await org_group_service.add_member(session, group, data.user_id)
    except org_group_service.NotAnOrgMemberError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MEMBER_NOT_FOUND
        ) from None
    except org_group_service.AlreadyInGroupError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Already a member of this group"
        ) from None
    except org_group_service.GroupNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GROUP_NOT_FOUND) from None
    except org_group_service.GroupManagedByScimError:
        raise _managed() from None
    detail = await org_group_service.group_detail(session, group)
    added = next(m for m in detail.members if m.user_id == user.id)
    await audit_service.record(
        session,
        user=current_user,
        action="org.group.member_add",
        target_type="organization_group",
        target_id=group.id,
        target_name=group.name,
        payload={"user_id": str(user.id), "email": user.email},
    )
    return added


@router.delete("/{group_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_group_member(
    session: SessionDep,
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    current_user: PathOrgAdminUserDep,
    org: ManagedOrgDep,
) -> Response:
    group = await _group(session, org, group_id)
    try:
        user = await org_group_service.remove_member(session, group, user_id)
    except org_group_service.NotInGroupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MEMBER_NOT_FOUND
        ) from None
    except org_group_service.GroupManagedByScimError:
        raise _managed() from None
    await audit_service.record(
        session,
        user=current_user,
        action="org.group.member_remove",
        target_type="organization_group",
        target_id=group.id,
        target_name=group.name,
        payload={"user_id": str(user.id), "email": user.email},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
