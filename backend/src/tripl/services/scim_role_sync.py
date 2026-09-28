"""The SCIM admin-group mapping: membership of one group is organization role ``admin`` (F20).

An organization's owner names a group (``org_scim_configs.admin_group_id``).
Whoever joins that group is promoted ``member`` -> ``admin``; whoever leaves it
is demoted ``admin`` -> ``member``. Owners are never touched (owner mapping is
not supported, by design), and nobody OUTSIDE the change is either: a manual
admin who is in no way affected by a membership change of the mapped group
keeps their role.

Called by :mod:`tripl.services.org_group_service` on every member add/remove
and group deletion, whoever makes it (SCIM or the groups API), and by the
config route when the mapping moves. Holds the organization's owner-set lock,
the one every role change takes. Nothing here commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.domain_enums import OrganizationRole
from tripl.models.org_scim import OrgScimConfig
from tripl.models.organization import OrganizationMember
from tripl.services import audit_service, auth_service


@dataclass(frozen=True)
class RoleChange:
    user_id: uuid.UUID
    old_role: str
    new_role: str


async def admin_group_id(session: AsyncSession, org_id: uuid.UUID) -> uuid.UUID | None:
    found: uuid.UUID | None = await session.scalar(
        select(OrgScimConfig.admin_group_id).where(OrgScimConfig.organization_id == org_id)
    )
    return found


async def _set_roles(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_ids: Collection[uuid.UUID],
    *,
    from_role: OrganizationRole,
    to_role: OrganizationRole,
) -> list[RoleChange]:
    if not user_ids:
        return []
    await auth_service.acquire_owner_set_xact_lock(session, org_id)
    rows = list(
        (
            await session.scalars(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == org_id,
                    OrganizationMember.user_id.in_(list(user_ids)),
                    OrganizationMember.role == from_role.value,
                )
            )
        ).all()
    )
    changes: list[RoleChange] = []
    for row in rows:
        row.role = to_role.value
        changes.append(RoleChange(row.user_id, from_role.value, to_role.value))
    if changes:
        await session.flush()
    return changes


async def promote(
    session: AsyncSession, org_id: uuid.UUID, user_ids: Collection[uuid.UUID]
) -> list[RoleChange]:
    """``member`` -> ``admin`` for those of ``user_ids`` who are plain members."""
    return await _set_roles(
        session,
        org_id,
        user_ids,
        from_role=OrganizationRole.member,
        to_role=OrganizationRole.admin,
    )


async def demote(
    session: AsyncSession, org_id: uuid.UUID, user_ids: Collection[uuid.UUID]
) -> list[RoleChange]:
    """``admin`` -> ``member`` for those of ``user_ids`` who are admins (owners untouched)."""
    return await _set_roles(
        session,
        org_id,
        user_ids,
        from_role=OrganizationRole.admin,
        to_role=OrganizationRole.member,
    )


async def on_group_change(
    session: AsyncSession,
    org_id: uuid.UUID,
    group_id: uuid.UUID,
    *,
    added: Collection[uuid.UUID] = (),
    removed: Collection[uuid.UUID] = (),
) -> list[RoleChange]:
    """Apply the mapping to a membership change of ``group_id``; a no-op for other groups."""
    if await admin_group_id(session, org_id) != group_id:
        return []
    changes = [
        *await promote(session, org_id, added),
        *await demote(session, org_id, removed),
    ]
    await record_changes(session, org_id, group_id, changes)
    return changes


async def record_changes(
    session: AsyncSession,
    org_id: uuid.UUID,
    group_id: uuid.UUID | None,
    changes: Collection[RoleChange],
) -> None:
    """One ``org.member_role_update`` row per change, by no user. No commit."""
    for change in changes:
        await audit_service.record(
            session,
            user=None,
            action="org.member_role_update",
            target_type="user",
            target_id=change.user_id,
            payload={
                "via": "scim_admin_group",
                "group_id": str(group_id) if group_id is not None else None,
                "old_role": change.old_role,
                "new_role": change.new_role,
            },
            commit=False,
            organization_id=org_id,
        )
