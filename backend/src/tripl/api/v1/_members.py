"""One member role change behind two addresses, and the member refusal texts.

``PATCH /users/{id}`` acts in the request's organization (and its
``/orgs/{org}/users/{id}`` rewrite); ``PATCH /orgs/{org}/members/{id}`` acts in
the organization its path names. Each route keeps its own gate. Everything
after the gate is here, so both answer with the same errors and file the same
audit action.

The refusal texts are also the other member routes' (removal and ownership
transfer in ``orgs.py``, inviting at ``owner`` in ``users.py``).
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.domain_enums import OrganizationRole
from tripl.models.user import User
from tripl.schemas.auth import UserListItem
from tripl.services import audit_service, user_service

OWNER_MANAGEMENT_REQUIRED = "Only an owner can manage owners"
LAST_OWNER = "Cannot remove or demote the last remaining owner"
MEMBER_NOT_FOUND = "Member not found"


async def change_member_role(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    role: OrganizationRole,
    actor: User,
) -> UserListItem:
    """Change a member's organization role, audited as ``org.member_role_update``.

    404 for an account outside the organization, 400 when the change would
    leave it without an owner, 403 when an admin tries to make or unmake an
    owner. The role change and its audit row are committed together. The
    member stays signed in; the new role applies from their next request.
    """
    try:
        target, old_role, invitations = await user_service.update_org_role(
            session, org_id, user_id, role, actor_id=actor.id
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
        user=actor,
        action="org.member_role_update",
        target_type="user",
        target_id=target.id,
        target_name=target.email,
        payload={
            "old_role": old_role,
            "new_role": OrganizationRole(role).value,
            "invitations_revoked": invitations,
        },
        organization_id=org_id,
    )
    return target
