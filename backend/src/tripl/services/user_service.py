"""Organization member operations that are more than a read (F20 PR4).

The users API manages the members of the request's organization, with their
ORGANIZATION role (``organization_members.role``: owner | admin | member).
``users.role`` is neither read nor written here.

Role changes carry invariants the router is the wrong place to hold: an
organization's owner set must never empty, and only an owner may create or
demote an owner. Both are enforced here so ``api/v1/users.py`` stays the thin
layer the rest of the API is.

A role change does NOT sign the user out (critique #7). Organization roles are
read from the database on every request (``api.deps.request_org_role``,
``services.project_access``), so the change takes effect on the user's next
request anyway, and purging every session would sign them out of every other
organization too.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.domain_enums import OrganizationRole
from tripl.models.organization import OrganizationMember
from tripl.models.user import User
from tripl.schemas.auth import UserListItem
from tripl.services import auth_service, invitation_service, scim_token_service


class LastOwnerError(Exception):
    """Raised when a demotion would leave the organization with no owner at all."""


class OwnerManagementError(Exception):
    """Raised when a non-owner tries to make, or unmake, an organization owner."""


def _item(user: User, role: str) -> UserListItem:
    return UserListItem(
        id=user.id,
        email=user.email,
        name=user.name,
        role=OrganizationRole(role),
        created_at=user.created_at,
    )


async def list_org_users(
    session: AsyncSession, org_id: uuid.UUID, *, limit: int, offset: int
) -> list[UserListItem]:
    """The members of ``org_id`` with their organization role, oldest account first."""
    rows = await session.execute(
        select(User, OrganizationMember.role)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(OrganizationMember.organization_id == org_id)
        .order_by(User.created_at, User.id)
        .limit(limit)
        .offset(offset)
    )
    return [_item(user, str(role)) for user, role in rows.all()]


async def update_org_role(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    role: OrganizationRole,
    *,
    actor_id: uuid.UUID,
) -> tuple[UserListItem, str, int]:
    """Change one member's organization role.

    Returns the member, the old role, and how many of the member's pending
    invitations were dropped because the new role could no longer issue them
    (a demotion must not leave the old role reachable through a link minted
    before it).

    The actor's role is read HERE, under the owner-set lock, not taken from the
    request's gate: a role read before the lock is stale by the time the guard
    runs, and an owner demoted in between could still manage owners.

    Raises :class:`LookupError` when ``user_id`` is not a member of ``org_id``,
    :class:`OwnerManagementError` when the actor is no longer an owner or admin,
    or is not an owner but the target is, or would become, one, and
    :class:`LastOwnerError` when the change would empty the organization's owner
    set — plain exceptions rather than HTTP ones, because this layer does not
    know it is behind HTTP.

    Does NOT commit: the caller commits once, after it has written its audit
    entry, so the role change and its record land together.
    """
    # Before the target is read, not just before the guard: the organization's
    # owner-set advisory lock serialises demotion here with the first-owner
    # decision at registration. Without it the guard below is a plain
    # check-then-write, and two concurrent demotions of the last two owners each
    # see the other as the survivor, both pass, and the organization is left
    # with no owner at all — recoverable only from the database.
    await auth_service.acquire_owner_set_xact_lock(session, org_id)
    actor_role = await org_role_under_lock(session, org_id, actor_id)
    if actor_role not in (OrganizationRole.owner.value, OrganizationRole.admin.value):
        raise OwnerManagementError

    row = (
        await session.execute(
            select(OrganizationMember, User)
            .join(User, User.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_id == user_id,
            )
        )
    ).first()
    if row is None:
        raise LookupError(user_id)
    membership, target = row
    old_role = str(membership.role)
    new_role = OrganizationRole(role).value
    owner = OrganizationRole.owner.value

    if owner in (old_role, new_role) and actor_role != owner:
        raise OwnerManagementError

    if old_role == owner and new_role != owner:
        other_owner = await session.scalar(
            select(OrganizationMember.id).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.role == owner,
                OrganizationMember.user_id != user_id,
            )
        )
        if other_owner is None:
            raise LastOwnerError

    membership.role = new_role
    dropped = 0
    if _outranks(old_role, new_role):
        dropped = await invitation_service.drop_pending_invitations(
            session, org_id, invited_by_user_id=user_id, above_role=OrganizationRole(new_role)
        )
    if old_role == owner and new_role != owner:
        # A SCIM token is an owner's credential; a former owner keeps none.
        await scim_token_service.revoke_tokens_created_by(session, org_id, user_id)
    await session.flush()
    return _item(target, new_role), old_role, dropped


_RANK = {
    OrganizationRole.member.value: 0,
    OrganizationRole.admin.value: 1,
    OrganizationRole.owner.value: 2,
}


def _outranks(role: str, other: str) -> bool:
    return _RANK[role] > _RANK[other]


async def org_role_under_lock(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> str | None:
    """``user_id``'s current role in ``org_id``; ``None`` for a non-member.

    For owner-set mutations: call it after ``acquire_owner_set_xact_lock`` so
    the answer cannot change before the transaction commits.
    """
    role: str | None = await session.scalar(
        select(OrganizationMember.role).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    return None if role is None else str(role)
