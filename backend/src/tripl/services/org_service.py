"""Organization management: create, rename, members, ownership (F20 PR6, GH #273).

The HTTP layer is ``api/v1/orgs.py``; this module holds the rules and raises
plain exceptions, never HTTP ones:

* :class:`OrgNotFoundError` — no such ``active`` organization, or the caller
  cannot see it (not a member, or an API key of another organization). One
  error for all of them, so the answer is no oracle for which slugs exist.
* :class:`OrgSlugTakenError` — create with a slug another organization holds.
* :class:`user_service.LastOwnerError` / :class:`user_service.OwnerManagementError`
  — the owner-set invariants, shared with ``PATCH /users/{id}``.

Deleting an organization lives in :mod:`tripl.services.org_deletion_service`.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.api_key import ApiKey
from tripl.models.domain_enums import OrganizationRole, OrganizationStatus
from tripl.models.organization import DEFAULT_ORG_ID, Organization, OrganizationMember
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.schemas.organization import ORG_SLUG_PATTERN, OrgResponse
from tripl.services import (
    auth_service,
    invitation_service,
    org_group_service,
    project_member_service,
    user_service,
)
from tripl.services.org_resolution import ORG_IS_ACTIVE

_ORG_SLUG = re.compile(ORG_SLUG_PATTERN)


class OrgNotFoundError(LookupError):
    """The organization does not exist, is being deleted, or is not the caller's."""


class OrgSlugTakenError(Exception):
    """Another organization already holds the slug."""


class MemberNotFoundError(LookupError):
    """The user is not a member of the organization."""


class SelfTransferError(Exception):
    """An owner tried to transfer the organization to themselves."""


@dataclass(frozen=True)
class RemovedMember:
    """What :func:`remove_member` took away with the membership."""

    user: User
    old_role: str
    project_memberships: int
    api_keys: int
    invitations: int
    group_memberships: int


@dataclass(frozen=True)
class ManagedOrg:
    """The organization a ``/orgs/{org}/...`` request manages, with the caller's role."""

    id: uuid.UUID
    slug: str
    name: str
    role: OrganizationRole
    status: str
    created_at: datetime


def org_response(org: ManagedOrg) -> OrgResponse:
    return OrgResponse(
        id=org.id,
        slug=org.slug,
        name=org.name,
        role=org.role,
        status=OrganizationStatus(org.status),
        is_default=org.id == DEFAULT_ORG_ID,
        created_at=org.created_at,
    )


async def resolve_managed_org(
    session: AsyncSession,
    *,
    slug: str,
    user_id: uuid.UUID,
    key_org_id: uuid.UUID | None,
) -> ManagedOrg:
    """The ``active`` organization ``slug`` if the caller may see it.

    A member of it, through a browser session or an API key of that same
    organization. Everything else — an unknown slug, a ``deleting``
    organization, a non-member, a key of another organization — raises the one
    :class:`OrgNotFoundError`. Unlike the project routes' org resolution, the
    default organization gets no exception for non-members here: managing an
    organization takes a membership of it.
    """
    if not _ORG_SLUG.fullmatch(slug):
        # A NUL or any other byte no slug can hold never reaches a query.
        raise OrgNotFoundError(slug)
    row = (
        await session.execute(
            select(Organization, OrganizationMember.role)
            .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
            .where(
                Organization.slug == slug,
                ORG_IS_ACTIVE,
                OrganizationMember.user_id == user_id,
            )
        )
    ).first()
    if row is None:
        raise OrgNotFoundError(slug)
    org, role = cast(tuple[Organization, str], tuple(row))
    if key_org_id is not None and key_org_id != org.id:
        raise OrgNotFoundError(slug)
    return ManagedOrg(
        id=org.id,
        slug=org.slug,
        name=org.name,
        role=OrganizationRole(str(role)),
        status=str(org.status),
        created_at=org.created_at,
    )


async def list_my_orgs(
    session: AsyncSession, user_id: uuid.UUID, *, only_org_id: uuid.UUID | None = None
) -> list[OrgResponse]:
    """Every ``active`` organization ``user_id`` belongs to, by name.

    ``only_org_id`` narrows it to one: an API key belongs to one organization
    and must not list the others its user happens to be in.
    """
    query = (
        select(Organization, OrganizationMember.role)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(OrganizationMember.user_id == user_id, ORG_IS_ACTIVE)
        .order_by(Organization.name, Organization.slug)
    )
    if only_org_id is not None:
        query = query.where(Organization.id == only_org_id)
    rows = (await session.execute(query)).all()
    return [
        org_response(
            ManagedOrg(
                id=org.id,
                slug=org.slug,
                name=org.name,
                role=OrganizationRole(str(role)),
                status=str(org.status),
                created_at=org.created_at,
            )
        )
        for org, role in rows
    ]


async def create_org(session: AsyncSession, *, creator: User, slug: str, name: str) -> ManagedOrg:
    """Create an organization with ``creator`` as its first owner. Does NOT commit.

    The caller commits once, after its audit row, so the organization, the
    membership and the record land together.
    """
    taken: uuid.UUID | None = await session.scalar(
        select(Organization.id).where(Organization.slug == slug)
    )
    if taken is not None:
        raise OrgSlugTakenError(slug)
    org = Organization(slug=slug, name=name, status=OrganizationStatus.active.value)
    session.add(org)
    await session.flush()
    auth_service.add_organization_membership(
        session, creator, organization_id=org.id, org_role=OrganizationRole.owner
    )
    await session.flush()
    await session.refresh(org)
    return ManagedOrg(
        id=org.id,
        slug=org.slug,
        name=org.name,
        role=OrganizationRole.owner,
        status=str(org.status),
        created_at=org.created_at,
    )


async def rename_org(session: AsyncSession, org: ManagedOrg, name: str) -> ManagedOrg:
    """Change the display name. The slug never changes (owner decision 6). No commit."""
    await session.execute(update(Organization).where(Organization.id == org.id).values(name=name))
    await session.flush()
    return ManagedOrg(
        id=org.id,
        slug=org.slug,
        name=name,
        role=org.role,
        status=org.status,
        created_at=org.created_at,
    )


async def remove_member(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
) -> RemovedMember:
    """Take ``user_id`` out of the organization, and everything that came with it.

    Deletes the membership, the user's ``project_members`` rows in every project
    of the organization (with the event-type ownerships and reviewer seats those
    carried), revokes every live API key of theirs bound to the organization
    (critique #28), and deletes the organization's unused invitations they sent
    or that are addressed to them: a link minted before the removal would
    otherwise let them back in, at the role they had. They leave every group of
    the organization too. Their account and their other organizations are
    untouched.

    The actor's role is re-read under the owner-set lock (see
    :func:`user_service.update_org_role`). Raises :class:`MemberNotFoundError`,
    :class:`user_service.OwnerManagementError` (the actor is no longer an owner
    or admin, or is an admin removing an owner) and
    :class:`user_service.LastOwnerError`. Does NOT commit.
    """
    await auth_service.acquire_owner_set_xact_lock(session, org_id)
    actor_role = await user_service.org_role_under_lock(session, org_id, actor_id)
    if actor_role not in (OrganizationRole.owner.value, OrganizationRole.admin.value):
        raise user_service.OwnerManagementError
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
        raise MemberNotFoundError(user_id)
    membership, target = cast(tuple[OrganizationMember, User], tuple(row))
    old_role = str(membership.role)
    owner = OrganizationRole.owner.value
    if old_role == owner:
        if actor_role != owner:
            raise user_service.OwnerManagementError
        if not await _has_another_owner(session, org_id, user_id):
            raise user_service.LastOwnerError

    project_ids = list(
        (await session.scalars(select(Project.id).where(Project.organization_id == org_id))).all()
    )
    removed = 0
    if project_ids:
        rows = list(
            (
                await session.scalars(
                    select(ProjectMember).where(
                        ProjectMember.user_id == user_id,
                        ProjectMember.project_id.in_(project_ids),
                    )
                )
            ).all()
        )
        for project_member in rows:
            await session.delete(project_member)
        removed = len(rows)
        await project_member_service.drop_grants_in_projects(session, project_ids, user_id)

    revoked = await _revoke_org_keys(session, org_id, user_id)
    invitations = await invitation_service.drop_pending_invitations(
        session, org_id, invited_by_user_id=user_id, email=target.email
    )
    groups = await org_group_service.drop_user_from_org_groups(session, org_id, user_id)
    await session.delete(membership)
    await session.flush()
    return RemovedMember(
        user=target,
        old_role=old_role,
        project_memberships=removed,
        api_keys=revoked,
        invitations=invitations,
        group_memberships=groups,
    )


async def transfer_ownership(
    session: AsyncSession, org_id: uuid.UUID, *, actor_id: uuid.UUID, target_id: uuid.UUID
) -> tuple[User, str]:
    """Make ``target_id`` an owner and step the acting owner down to admin. No commit.

    The route admits owners only, and the actor's role is checked again here
    under the owner-set lock: an owner demoted by another owner after the gate
    ran must not hand ownership out. The actor's pending invitations at the
    ``owner`` role are dropped with the step-down. Returns the new owner and
    their old role. Raises :class:`SelfTransferError`,
    :class:`MemberNotFoundError` and :class:`user_service.OwnerManagementError`.
    """
    if actor_id == target_id:
        raise SelfTransferError
    await auth_service.acquire_owner_set_xact_lock(session, org_id)
    actor_membership: OrganizationMember | None = await session.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == actor_id,
        )
    )
    if actor_membership is None or actor_membership.role != OrganizationRole.owner.value:
        raise user_service.OwnerManagementError
    target_row = (
        await session.execute(
            select(OrganizationMember, User)
            .join(User, User.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_id == target_id,
            )
        )
    ).first()
    if target_row is None:
        raise MemberNotFoundError(target_id)
    target_membership, target = cast(tuple[OrganizationMember, User], tuple(target_row))
    old_role = str(target_membership.role)
    target_membership.role = OrganizationRole.owner.value
    actor_membership.role = OrganizationRole.admin.value
    await invitation_service.drop_pending_invitations(
        session, org_id, invited_by_user_id=actor_id, above_role=OrganizationRole.admin
    )
    await session.flush()
    return target, old_role


async def _has_another_owner(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    other: uuid.UUID | None = await session.scalar(
        select(OrganizationMember.id).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.role == OrganizationRole.owner.value,
            OrganizationMember.user_id != user_id,
        )
    )
    return other is not None


async def _revoke_org_keys(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> int:
    """Revoke ``user_id``'s live keys bound to ``org_id``; returns how many."""
    keys = list(
        (
            await session.scalars(
                select(ApiKey).where(
                    ApiKey.organization_id == org_id,
                    ApiKey.user_id == user_id,
                    ApiKey.revoked_at.is_(None),
                )
            )
        ).all()
    )
    now = datetime.now(UTC)
    for key in keys:
        key.revoked_at = now
    return len(keys)
