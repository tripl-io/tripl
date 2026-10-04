"""Organization groups (F20, GH #273): named sets of an organization's members.

The HTTP layer is ``api/v1/org_groups.py``; this module holds the rules and
raises plain exceptions:

* :class:`GroupNotFoundError` — no such group IN THIS ORGANIZATION. Every lookup
  is keyed by ``(organization_id, group_id)``, so another organization's group
  id answers exactly like an id that does not exist.
* :class:`GroupNameTakenError` — the organization already has a group of that
  name (compared case-insensitively).
* :class:`NotAnOrgMemberError` — the user to add is not a member of the
  organization (or does not exist).
* :class:`AlreadyInGroupError` / :class:`NotInGroupError` — member add/remove.
* :class:`GroupManagedByScimError` — a manual rename, delete or member change of
  a group the organization's SCIM provisioning manages (``managed_by_scim``):
  the identity provider owns it, and a manual edit would be undone at its next
  sync. Its description stays editable. SCIM passes ``via_scim=True``.

Nothing here commits: the route commits once, with its audit row.

Every member add/remove and group deletion runs the extensions' group hook
(:func:`tripl.extensions.on_group_change`; the SCIM admin-group mapping is
one), whoever makes it.

Reuse: :func:`group_member_ids` resolves groups to the users in them, for note
sharing (F24), event-type owners and alert routing. SCIM group sync
(``scim_group_service``) writes through :func:`add_member` / :func:`remove_member`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from typing import cast

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import extensions
from tripl.models.organization import OrganizationMember
from tripl.models.organization_group import OrganizationGroup, OrganizationGroupMember
from tripl.models.user import User
from tripl.schemas.organization_group import (
    OrgGroupDetail,
    OrgGroupMemberResponse,
    OrgGroupResponse,
)
from tripl.services import auth_service, docs_folders

_UNIQUE_VIOLATION_SQLSTATE = "23505"
_FOREIGN_KEY_VIOLATION_SQLSTATE = "23503"


class GroupNotFoundError(LookupError):
    """No such group in the organization."""


class GroupNameTakenError(Exception):
    """The organization already has a group with that name."""


class NotAnOrgMemberError(LookupError):
    """The user is not a member of the group's organization."""


class AlreadyInGroupError(Exception):
    """The user is already in the group."""


class NotInGroupError(LookupError):
    """The user is not in the group."""


class GroupManagedByScimError(Exception):
    """The group is managed by SCIM provisioning; only SCIM changes its name or members."""


def _refuse_manual_edit(group: OrganizationGroup, via_scim: bool) -> None:
    if group.managed_by_scim and not via_scim:
        raise GroupManagedByScimError(group.id)


async def get_group(
    session: AsyncSession, org_id: uuid.UUID, group_id: uuid.UUID
) -> OrganizationGroup:
    group: OrganizationGroup | None = await session.scalar(
        select(OrganizationGroup).where(
            OrganizationGroup.organization_id == org_id, OrganizationGroup.id == group_id
        )
    )
    if group is None:
        raise GroupNotFoundError(group_id)
    return group


def _is_org_member() -> ColumnElement[bool]:
    """Join condition: the group member is still a member of the group's organization.

    Every read of a group's members goes through it, like :func:`group_member_ids`,
    so a membership row left behind for someone who has left the organization
    is never listed, counted or honoured.
    """
    return (OrganizationMember.user_id == OrganizationGroupMember.user_id) & (
        OrganizationMember.organization_id == OrganizationGroup.organization_id
    )


async def _member_counts(
    session: AsyncSession, group_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not group_ids:
        return {}
    rows = (
        await session.execute(
            select(OrganizationGroupMember.group_id, func.count())
            .join(OrganizationGroup, OrganizationGroup.id == OrganizationGroupMember.group_id)
            .join(OrganizationMember, _is_org_member())
            .where(OrganizationGroupMember.group_id.in_(list(group_ids)))
            .group_by(OrganizationGroupMember.group_id)
        )
    ).all()
    return {cast(uuid.UUID, gid): int(count) for gid, count in rows}


def _response(group: OrganizationGroup, member_count: int) -> OrgGroupResponse:
    return OrgGroupResponse(
        id=group.id,
        name=group.name,
        description=group.description,
        member_count=member_count,
        managed_by_scim=bool(group.managed_by_scim),
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


async def list_groups(session: AsyncSession, org_id: uuid.UUID) -> list[OrgGroupResponse]:
    """Every group of the organization, by name."""
    groups = list(
        (
            await session.scalars(
                select(OrganizationGroup)
                .where(OrganizationGroup.organization_id == org_id)
                .order_by(func.lower(OrganizationGroup.name), OrganizationGroup.id)
            )
        ).all()
    )
    counts = await _member_counts(session, [g.id for g in groups])
    return [_response(g, counts.get(g.id, 0)) for g in groups]


async def group_detail(session: AsyncSession, group: OrganizationGroup) -> OrgGroupDetail:
    """The group with its members, ordered by name then email."""
    rows = (
        await session.execute(
            select(User, OrganizationGroupMember.created_at)
            .join(OrganizationGroupMember, OrganizationGroupMember.user_id == User.id)
            .join(OrganizationGroup, OrganizationGroup.id == OrganizationGroupMember.group_id)
            .join(OrganizationMember, _is_org_member())
            .where(OrganizationGroupMember.group_id == group.id)
            .order_by(func.lower(User.name), User.email)
        )
    ).all()
    members = [
        OrgGroupMemberResponse(user_id=user.id, email=user.email, name=user.name, added_at=added_at)
        for user, added_at in rows
    ]
    return OrgGroupDetail(
        **_response(group, len(members)).model_dump(),
        members=members,
    )


async def _name_taken(
    session: AsyncSession,
    org_id: uuid.UUID,
    name: str,
    *,
    except_id: uuid.UUID | None = None,
) -> bool:
    query = select(OrganizationGroup.id).where(
        OrganizationGroup.organization_id == org_id,
        func.lower(OrganizationGroup.name) == name.lower(),
    )
    if except_id is not None:
        query = query.where(OrganizationGroup.id != except_id)
    return (await session.scalar(query)) is not None


def _violation(exc: IntegrityError, sqlstate: str, sqlite_name: str) -> bool:
    """Whether ``exc`` is the given integrity violation.

    PostgreSQL drivers carry the SQLSTATE as ``sqlstate`` (asyncpg adapter,
    psycopg 3) or ``pgcode`` (psycopg2); SQLite (tests) names it instead.
    """
    orig = exc.orig
    codes = (getattr(orig, "sqlstate", None), getattr(orig, "pgcode", None))
    return sqlstate in codes or getattr(orig, "sqlite_errorname", None) == sqlite_name


def _is_unique_violation(exc: IntegrityError) -> bool:
    return _violation(exc, _UNIQUE_VIOLATION_SQLSTATE, "SQLITE_CONSTRAINT_UNIQUE")


def _is_foreign_key_violation(exc: IntegrityError) -> bool:
    return _violation(exc, _FOREIGN_KEY_VIOLATION_SQLSTATE, "SQLITE_CONSTRAINT_FOREIGNKEY")


@asynccontextmanager
async def _savepoint_or_conflict(session: AsyncSession, error: Exception) -> AsyncIterator[None]:
    """Make the body's changes and flush them in a savepoint.

    A unique-constraint race the pre-check missed becomes ``error``. Any other
    integrity failure (a foreign key, say) is re-raised as is: it is not a
    conflict, and answering it as one would invite a pointless retry. Either
    way only the savepoint rolls back and the session stays usable. The body
    must make its changes INSIDE the block: ``begin_nested`` flushes whatever is
    already pending before the SAVEPOINT, outside its protection.
    """
    try:
        async with session.begin_nested():
            yield
            await session.flush()
    except IntegrityError as exc:
        if _is_unique_violation(exc):
            raise error from None
        raise


async def create_group(
    session: AsyncSession, org_id: uuid.UUID, *, name: str, description: str
) -> OrganizationGroup:
    if await _name_taken(session, org_id, name):
        raise GroupNameTakenError(name)
    group = OrganizationGroup(organization_id=org_id, name=name, description=description)
    async with _savepoint_or_conflict(session, GroupNameTakenError(name)):
        session.add(group)
    await session.refresh(group)
    return group


async def update_group(
    session: AsyncSession,
    group: OrganizationGroup,
    *,
    name: str | None,
    description: str | None,
    via_scim: bool = False,
) -> dict[str, dict[str, str]]:
    """Apply a rename and/or new description; returns ``{field: {old, new}}`` of what changed.

    A SCIM-managed group is renamed by SCIM only (:class:`GroupManagedByScimError`).
    """
    changes: dict[str, dict[str, str]] = {}
    if name is not None and name != group.name:
        _refuse_manual_edit(group, via_scim)
        if await _name_taken(session, group.organization_id, name, except_id=group.id):
            raise GroupNameTakenError(name)
        changes["name"] = {"old": group.name, "new": name}
    if description is not None and description != group.description:
        changes["description"] = {"old": group.description, "new": description}
    if changes:
        async with _savepoint_or_conflict(session, GroupNameTakenError(name or group.name)):
            if "name" in changes:
                group.name = changes["name"]["new"]
            if "description" in changes:
                group.description = changes["description"]["new"]
        await session.refresh(group)
    return changes


async def delete_group(
    session: AsyncSession, group: OrganizationGroup, *, via_scim: bool = False
) -> int:
    """Delete the group and its memberships; returns how many members it had.

    The members of the SCIM admin group lose the ``admin`` role it gave them.
    """
    _refuse_manual_edit(group, via_scim)
    member_ids = list(
        (
            await session.scalars(
                select(OrganizationGroupMember.user_id).where(
                    OrganizationGroupMember.group_id == group.id
                )
            )
        ).all()
    )
    members = len(member_ids)
    await extensions.on_group_change(session, group.organization_id, group.id, removed=member_ids)
    await session.execute(
        delete(OrganizationGroupMember).where(OrganizationGroupMember.group_id == group.id)
    )
    # Notes and folders shared with the group (F24); the FK cascades as well.
    await docs_folders.drop_group_shares(session, [group.id])
    await session.delete(group)
    await session.flush()
    return int(members or 0)


async def add_member(
    session: AsyncSession,
    group: OrganizationGroup,
    user_id: uuid.UUID,
    *,
    via_scim: bool = False,
) -> User:
    """Put ``user_id`` in the group; they must be a member of the group's organization.

    Refused for a SCIM-managed group unless ``via_scim``; joining the SCIM
    admin group promotes a plain member to ``admin``.

    Takes the organization's owner-set lock first, the one
    ``org_service.remove_member`` holds while it drops the user's group rows and
    then their organization membership. Without it, an add that saw the user
    still in the organization could commit its row after the removal's DELETE
    ran, leaving a group membership for someone outside the organization that
    would count again if they rejoined. Each caller holds just this one lock.

    A foreign-key failure at the insert means the group was deleted meanwhile
    (:class:`GroupNotFoundError`) or the user's account was
    (:class:`NotAnOrgMemberError`).
    """
    _refuse_manual_edit(group, via_scim)
    await auth_service.acquire_owner_set_xact_lock(session, group.organization_id)
    user: User | None = await session.scalar(
        select(User)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(
            OrganizationMember.organization_id == group.organization_id,
            OrganizationMember.user_id == user_id,
        )
    )
    if user is None:
        raise NotAnOrgMemberError(user_id)
    existing = await session.scalar(
        select(OrganizationGroupMember.id).where(
            OrganizationGroupMember.group_id == group.id,
            OrganizationGroupMember.user_id == user_id,
        )
    )
    if existing is not None:
        raise AlreadyInGroupError(user_id)
    try:
        async with _savepoint_or_conflict(session, AlreadyInGroupError(user_id)):
            session.add(OrganizationGroupMember(group_id=group.id, user_id=user_id))
    except IntegrityError as exc:
        if not _is_foreign_key_violation(exc):
            raise
        still_there = await session.scalar(
            select(OrganizationGroup.id).where(OrganizationGroup.id == group.id)
        )
        if still_there is None:
            raise GroupNotFoundError(group.id) from None
        raise NotAnOrgMemberError(user_id) from None
    await extensions.on_group_change(session, group.organization_id, group.id, added=[user_id])
    return user


async def remove_member(
    session: AsyncSession,
    group: OrganizationGroup,
    user_id: uuid.UUID,
    *,
    via_scim: bool = False,
) -> User:
    """Take ``user_id`` out of the group.

    Refused for a SCIM-managed group unless ``via_scim``; leaving the SCIM
    admin group demotes an ``admin`` to ``member`` (owners keep their role).
    """
    _refuse_manual_edit(group, via_scim)
    row = (
        await session.execute(
            select(OrganizationGroupMember, User)
            .join(User, User.id == OrganizationGroupMember.user_id)
            .where(
                OrganizationGroupMember.group_id == group.id,
                OrganizationGroupMember.user_id == user_id,
            )
        )
    ).first()
    if row is None:
        raise NotInGroupError(user_id)
    membership, user = cast(tuple[OrganizationGroupMember, User], tuple(row))
    await session.delete(membership)
    await session.flush()
    await extensions.on_group_change(session, group.organization_id, group.id, removed=[user_id])
    return user


async def drop_user_from_org_groups(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> int:
    """Remove ``user_id`` from every group of ``org_id``; returns how many. No commit.

    Called by ``org_service.remove_member``: leaving the organization leaves its
    groups too. Groups of the user's other organizations are untouched.
    """
    group_ids = select(OrganizationGroup.id).where(OrganizationGroup.organization_id == org_id)
    count = await session.scalar(
        select(func.count()).where(
            OrganizationGroupMember.user_id == user_id,
            OrganizationGroupMember.group_id.in_(group_ids),
        )
    )
    if count:
        await session.execute(
            delete(OrganizationGroupMember).where(
                OrganizationGroupMember.user_id == user_id,
                OrganizationGroupMember.group_id.in_(group_ids),
            )
        )
    return int(count or 0)


async def delete_org_groups(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Every group of ``org_id`` and its memberships (the organization purge). No commit."""
    group_ids = select(OrganizationGroup.id).where(OrganizationGroup.organization_id == org_id)
    await session.execute(
        delete(OrganizationGroupMember).where(OrganizationGroupMember.group_id.in_(group_ids))
    )
    await docs_folders.drop_group_shares(session, group_ids)
    await session.execute(
        delete(OrganizationGroup).where(OrganizationGroup.organization_id == org_id)
    )


async def group_member_ids(
    session: AsyncSession, org_id: uuid.UUID, group_ids: Collection[uuid.UUID]
) -> set[uuid.UUID]:
    """The users in any of ``group_ids``, for consumers that grant or route by group.

    Only groups of ``org_id`` count — an id of another organization's group, or
    one that does not exist, contributes nobody — and only users who are still
    members of ``org_id``. For note sharing (F24), event-type owners and alert
    routing; none of them reads it yet.
    """
    if not group_ids:
        return set()
    rows = await session.scalars(
        select(OrganizationGroupMember.user_id)
        .join(OrganizationGroup, OrganizationGroup.id == OrganizationGroupMember.group_id)
        .join(
            OrganizationMember,
            (OrganizationMember.user_id == OrganizationGroupMember.user_id)
            & (OrganizationMember.organization_id == OrganizationGroup.organization_id),
        )
        .where(
            OrganizationGroup.organization_id == org_id,
            OrganizationGroupMember.group_id.in_(list(group_ids)),
        )
        .distinct()
    )
    return set(rows.all())
