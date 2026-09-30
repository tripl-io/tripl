"""An organization's SCIM settings: the admin-group mapping (F20, GH #273).

Moving the mapping applies it at once: the new group's plain members become
``admin``, the old group's admins who are not in the new one become
``member``; owners and everyone else keep their role
(:mod:`tripl.services.scim_role_sync`). Nothing here commits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.org_scim import OrgScimConfig
from tripl.models.organization_group import OrganizationGroup, OrganizationGroupMember
from tripl.schemas.org_scim import OrgScimConfigResponse
from tripl.services import org_group_service, scim_role_sync, scim_token_service


@dataclass(frozen=True)
class ConfigSaved:
    old_group_id: uuid.UUID | None
    new_group_id: uuid.UUID | None
    changes: list[scim_role_sync.RoleChange]


def scim_base_url(app_base_url: str, org_slug: str) -> str:
    return f"{app_base_url.rstrip('/')}/scim/v2/{org_slug}"


async def get_config(session: AsyncSession, org_id: uuid.UUID) -> OrgScimConfig | None:
    config: OrgScimConfig | None = await session.scalar(
        select(OrgScimConfig).where(OrgScimConfig.organization_id == org_id)
    )
    return config


async def config_response(
    session: AsyncSession, org_id: uuid.UUID, *, base_url: str
) -> OrgScimConfigResponse:
    config = await get_config(session, org_id)
    group_id = config.admin_group_id if config is not None else None
    group_name: str | None = None
    if group_id is not None:
        group_name = await session.scalar(
            select(OrganizationGroup.name).where(OrganizationGroup.id == group_id)
        )
    return OrgScimConfigResponse(
        base_url=base_url,
        admin_group_id=group_id,
        admin_group_name=group_name,
        active_tokens=await scim_token_service.count_live_tokens(session, org_id),
    )


async def _group_members(session: AsyncSession, group_id: uuid.UUID | None) -> set[uuid.UUID]:
    if group_id is None:
        return set()
    rows = await session.scalars(
        select(OrganizationGroupMember.user_id).where(OrganizationGroupMember.group_id == group_id)
    )
    return set(rows.all())


async def set_admin_group(
    session: AsyncSession, org_id: uuid.UUID, group_id: uuid.UUID | None
) -> ConfigSaved:
    """Map ``group_id`` (a group of this organization; ``None`` unmaps) to ``admin``.

    Raises :class:`org_group_service.GroupNotFoundError` for a group of
    another organization or none.
    """
    if group_id is not None:
        await org_group_service.get_group(session, org_id, group_id)
    config = await get_config(session, org_id)
    old_group_id = config.admin_group_id if config is not None else None
    if config is None:
        config = OrgScimConfig(organization_id=org_id, admin_group_id=group_id)
        session.add(config)
    else:
        config.admin_group_id = group_id
    await session.flush()
    if old_group_id == group_id:
        return ConfigSaved(old_group_id, group_id, [])
    new_members = await _group_members(session, group_id)
    old_members = await _group_members(session, old_group_id)
    changes = [
        *await scim_role_sync.promote(session, org_id, new_members),
        *await scim_role_sync.demote(session, org_id, old_members - new_members),
    ]
    await scim_role_sync.record_changes(session, org_id, group_id, changes)
    return ConfigSaved(old_group_id, group_id, changes)
