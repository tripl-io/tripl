"""SCIM ``/Groups`` of one organization, mapped onto organization groups (F20, GH #273).

A SCIM group is an ``organization_groups`` row: ``displayName`` is its name,
``members`` the organization members in it (by ``users.id``), ``externalId``
lives in ``scim_group_links``. Every group of the organization is listed, so
an identity provider can adopt an existing group by name; the first SCIM write
to a group marks it ``managed_by_scim``, after which only SCIM renames it or
changes its members (the groups API answers 409).

Members are written through :func:`org_group_service.add_member` /
:func:`org_group_service.remove_member`, so the SCIM admin-group mapping
(:mod:`tripl.services.scim_role_sync`) runs on every change. A member id that
is not a user this organization's SCIM can see is 400 ``invalidValue``; a
visible user who is deprovisioned (no longer a member) is skipped: they come
back into the group when the provider re-activates and re-adds them.

Nothing here commits; the route files the audit row and commits once.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import ColumnElement, delete, false, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.org_scim import SCIM_EXTERNAL_ID_MAX_LENGTH, ScimGroupLink, ScimUserLink
from tripl.models.organization import OrganizationMember
from tripl.models.organization_group import (
    GROUP_NAME_MAX_LENGTH,
    OrganizationGroup,
    OrganizationGroupMember,
)
from tripl.models.user import User
from tripl.services import org_group_service
from tripl.services.scim_errors import INVALID_PATH, bad_request, conflict, not_found
from tripl.services.scim_resources import (
    GROUP_FILTER_ATTRIBUTES,
    GROUP_SCHEMA,
    PatchOperation,
    excluded_attributes,
    list_response,
    normalize_path,
    optional_string,
    paging,
    parse_filter,
    patch_operations,
    timestamp,
)
from tripl.services.scim_token_service import ScimCaller
from tripl.services.scim_user_service import loaded

GROUP_NOT_FOUND = "Group not found"

_MEMBER_FILTER_PREFIX = "members[value eq "


class _Unset:
    """Marks an attribute the request did not mention."""


UNSET = _Unset()


@dataclass
class GroupChanges:
    display_name: str | _Unset = UNSET
    external_id: str | None | _Unset = UNSET
    #: ``("add" | "remove" | "set", ids)`` in request order; ``("clear", [])`` empties it.
    member_ops: list[tuple[str, list[uuid.UUID]]] = field(default_factory=list)


@dataclass(frozen=True)
class GroupWrite:
    resource: dict[str, Any]
    group_id: uuid.UUID
    name: str
    action: str
    payload: dict[str, object]


# ── reading ─────────────────────────────────────────────────────────────────


def _parse_id(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


async def get_group(session: AsyncSession, org_id: uuid.UUID, raw_id: str) -> OrganizationGroup:
    group_id = _parse_id(raw_id)
    if group_id is None:
        raise not_found(GROUP_NOT_FOUND)
    try:
        return await org_group_service.get_group(session, org_id, group_id)
    except org_group_service.GroupNotFoundError:
        raise not_found(GROUP_NOT_FOUND) from None


async def _external_ids(
    session: AsyncSession, org_id: uuid.UUID, group_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str | None]:
    if not group_ids:
        return {}
    rows = (
        await session.execute(
            select(ScimGroupLink.group_id, ScimGroupLink.external_id).where(
                ScimGroupLink.organization_id == org_id,
                ScimGroupLink.group_id.in_(list(group_ids)),
            )
        )
    ).all()
    return {group_id: external_id for group_id, external_id in rows}


async def _members(
    session: AsyncSession, org_id: uuid.UUID, group_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[uuid.UUID, str]]]:
    """Each group's members who are still members of the organization."""
    if not group_ids:
        return {}
    rows = (
        await session.execute(
            select(OrganizationGroupMember.group_id, User.id, User.email)
            .join(User, User.id == OrganizationGroupMember.user_id)
            .join(
                OrganizationMember,
                (OrganizationMember.user_id == OrganizationGroupMember.user_id)
                & (OrganizationMember.organization_id == org_id),
            )
            .where(OrganizationGroupMember.group_id.in_(list(group_ids)))
            .order_by(User.email)
        )
    ).all()
    out: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = {}
    for group_id, user_id, email in rows:
        out.setdefault(group_id, []).append((user_id, email))
    return out


async def render(
    session: AsyncSession,
    org_id: uuid.UUID,
    groups: Sequence[OrganizationGroup],
    base_url: str,
    excluded: set[str] | None = None,
) -> list[dict[str, Any]]:
    excluded = excluded or set()
    ids = [group.id for group in groups]
    for group in groups:
        await loaded(session, group)
    external = await _external_ids(session, org_id, ids)
    members = {} if "members" in excluded else await _members(session, org_id, ids)
    out: list[dict[str, Any]] = []
    for group in groups:
        resource: dict[str, Any] = {
            "schemas": [GROUP_SCHEMA],
            "id": str(group.id),
            "displayName": group.name,
            "meta": {
                "resourceType": "Group",
                "created": timestamp(group.created_at),
                "lastModified": timestamp(group.updated_at),
                "location": f"{base_url}/Groups/{group.id}",
            },
        }
        if external.get(group.id):
            resource["externalId"] = external[group.id]
        if "members" not in excluded:
            resource["members"] = [
                {"value": str(user_id), "display": email, "$ref": f"{base_url}/Users/{user_id}"}
                for user_id, email in members.get(group.id, [])
            ]
        out.append(resource)
    return out


async def list_groups(
    session: AsyncSession, org_id: uuid.UUID, params: Mapping[str, str], base_url: str
) -> dict[str, Any]:
    flt = parse_filter(params.get("filter"), GROUP_FILTER_ATTRIBUTES)
    start, count = paging(params)
    where: list[ColumnElement[bool]] = [OrganizationGroup.organization_id == org_id]
    if flt is not None:
        if flt.attribute == "displayName":
            where.append(func.lower(OrganizationGroup.name) == flt.value.lower())
        elif flt.attribute == "externalId":
            where.append(
                OrganizationGroup.id.in_(
                    select(ScimGroupLink.group_id).where(
                        ScimGroupLink.organization_id == org_id,
                        ScimGroupLink.external_id == flt.value,
                    )
                )
            )
        else:
            group_id = _parse_id(flt.value)
            where.append(OrganizationGroup.id == group_id if group_id is not None else false())
    total = int(
        await session.scalar(select(func.count()).select_from(OrganizationGroup).where(*where)) or 0
    )
    groups: list[OrganizationGroup] = []
    if count > 0:
        groups = list(
            (
                await session.scalars(
                    select(OrganizationGroup)
                    .where(*where)
                    .order_by(OrganizationGroup.created_at, OrganizationGroup.id)
                    .offset(start - 1)
                    .limit(count)
                )
            ).all()
        )
    resources = await render(session, org_id, groups, base_url, excluded_attributes(params))
    return list_response(resources, total=total, start_index=start)


async def get_group_resource(
    session: AsyncSession, org_id: uuid.UUID, raw_id: str, params: Mapping[str, str], base_url: str
) -> dict[str, Any]:
    group = await get_group(session, org_id, raw_id)
    [resource] = await render(session, org_id, [group], base_url, excluded_attributes(params))
    return resource


# ── request bodies ──────────────────────────────────────────────────────────


def _display_name(value: object) -> str:
    name = optional_string(value, "displayName", max_length=GROUP_NAME_MAX_LENGTH)
    if name is None:
        raise bad_request("'displayName' is required")
    return name


def _member_ids(value: object) -> list[uuid.UUID]:
    """``[{"value": "<id>"}, ...]`` (a single object is accepted too) as user ids."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    ids: list[uuid.UUID] = []
    for item in items:
        raw = item.get("value") if isinstance(item, dict) else None
        member_id = _parse_id(raw) if isinstance(raw, str) else None
        if member_id is None:
            raise bad_request("Each member needs a 'value' holding a user id")
        ids.append(member_id)
    return ids


def _filtered_member(path: str) -> uuid.UUID:
    """The id in ``members[value eq "<id>"]`` (Azure AD's remove path)."""
    inner = path[len(_MEMBER_FILTER_PREFIX) :]
    if not inner.endswith("]"):
        raise bad_request(f"Unsupported path '{path}'", INVALID_PATH)
    raw = inner[:-1].strip().strip('"')
    member_id = _parse_id(raw)
    if member_id is None:
        raise bad_request(f"Unsupported path '{path}'", INVALID_PATH)
    return member_id


def _apply(changes: GroupChanges, path: str, value: object, *, op: str) -> None:
    removing = op == "remove"
    if path == "displayname":
        if removing:
            raise bad_request("'displayName' cannot be removed", INVALID_PATH)
        changes.display_name = _display_name(value)
    elif path == "externalid":
        changes.external_id = (
            None
            if removing
            else optional_string(value, "externalId", max_length=SCIM_EXTERNAL_ID_MAX_LENGTH)
        )
    elif path == "members":
        if removing:
            ids = _member_ids(value)
            changes.member_ops.append(("remove", ids) if ids else ("clear", []))
        else:
            changes.member_ops.append(("add" if op == "add" else "set", _member_ids(value)))
    elif path.startswith(_MEMBER_FILTER_PREFIX):
        if not removing:
            raise bad_request("Only 'remove' is supported on a filtered members path", INVALID_PATH)
        changes.member_ops.append(("remove", [_filtered_member(path)]))
    elif path in {"id", "meta", "schemas"}:
        return
    else:
        raise bad_request(f"Unsupported attribute '{path}'", INVALID_PATH)


def changes_from_resource(body: Mapping[str, Any]) -> GroupChanges:
    changes = GroupChanges(external_id=None)
    changes.display_name = _display_name(body.get("displayName"))
    for key, value in body.items():
        path = str(key).lower()
        if path == "externalid":
            _apply(changes, path, value, op="replace")
    changes.member_ops.append(("set", _member_ids(body.get("members"))))
    return changes


def changes_from_patch(operations: Sequence[PatchOperation]) -> GroupChanges:
    changes = GroupChanges()
    for operation in operations:
        if operation.path is None:
            if not isinstance(operation.value, dict):
                raise bad_request("A PATCH operation without a path needs an object value")
            for key, value in operation.value.items():
                _apply(changes, normalize_path(str(key)), value, op=operation.op)
        else:
            _apply(changes, operation.path, operation.value, op=operation.op)
    return changes


# ── writing ─────────────────────────────────────────────────────────────────


async def _check_members(
    session: AsyncSession, org_id: uuid.UUID, ids: Sequence[uuid.UUID]
) -> set[uuid.UUID]:
    """400 for an id SCIM cannot see; returns those who are organization members."""
    if not ids:
        return set()
    wanted = set(ids)
    members = set(
        (
            await session.scalars(
                select(OrganizationMember.user_id).where(
                    OrganizationMember.organization_id == org_id,
                    OrganizationMember.user_id.in_(wanted),
                )
            )
        ).all()
    )
    linked = set(
        (
            await session.scalars(
                select(ScimUserLink.user_id).where(
                    ScimUserLink.organization_id == org_id, ScimUserLink.user_id.in_(wanted)
                )
            )
        ).all()
    )
    unknown = wanted - members - linked
    if unknown:
        raise bad_request(f"Unknown member: {sorted(str(i) for i in unknown)[0]}")
    return members


async def _current_members(session: AsyncSession, group: OrganizationGroup) -> set[uuid.UUID]:
    rows = await session.scalars(
        select(OrganizationGroupMember.user_id).where(OrganizationGroupMember.group_id == group.id)
    )
    return set(rows.all())


async def _apply_members(
    session: AsyncSession,
    org_id: uuid.UUID,
    group: OrganizationGroup,
    ops: Sequence[tuple[str, list[uuid.UUID]]],
) -> tuple[list[str], list[str]]:
    """Run the member operations in order; the ids added and removed."""
    added: list[str] = []
    removed: list[str] = []
    for kind, ids in ops:
        to_add: set[uuid.UUID]
        to_remove: set[uuid.UUID]
        current = await _current_members(session, group)
        if kind == "clear":
            to_add, to_remove = set(), current
        elif kind == "remove":
            to_add, to_remove = set(), current & set(ids)
        else:
            joinable = await _check_members(session, org_id, ids)
            to_add = joinable - current
            to_remove = current - set(ids) if kind == "set" else set()
        for user_id in sorted(to_remove):
            await org_group_service.remove_member(session, group, user_id, via_scim=True)
            removed.append(str(user_id))
        for user_id in sorted(to_add):
            await org_group_service.add_member(session, group, user_id, via_scim=True)
            added.append(str(user_id))
    return added, removed


async def _set_external_id(
    session: AsyncSession, org_id: uuid.UUID, group_id: uuid.UUID, external_id: str | None
) -> bool:
    link: ScimGroupLink | None = await session.scalar(
        select(ScimGroupLink).where(ScimGroupLink.group_id == group_id)
    )
    if link is None:
        if external_id is None:
            return False
        session.add(
            ScimGroupLink(organization_id=org_id, group_id=group_id, external_id=external_id)
        )
        await session.flush()
        return True
    if link.external_id == external_id:
        return False
    link.external_id = external_id
    await session.flush()
    return True


async def _write(
    session: AsyncSession,
    caller: ScimCaller,
    group: OrganizationGroup,
    changes: GroupChanges,
    base_url: str,
) -> dict[str, object]:
    org_id = caller.organization_id
    changed: dict[str, object] = {}
    if not group.managed_by_scim:
        group.managed_by_scim = True
        changed["adopted"] = True
        await session.flush()
    if not isinstance(changes.display_name, _Unset) and changes.display_name != group.name:
        old = group.name
        try:
            await org_group_service.update_group(
                session, group, name=changes.display_name, description=None, via_scim=True
            )
        except org_group_service.GroupNameTakenError:
            raise conflict(f"A group named '{changes.display_name}' already exists") from None
        changed["name"] = {"old": old, "new": group.name}
    if not isinstance(changes.external_id, _Unset) and await _set_external_id(
        session, org_id, group.id, changes.external_id
    ):
        changed["external_id"] = changes.external_id
    added, removed = await _apply_members(session, org_id, group, changes.member_ops)
    if added:
        changed["members_added"] = added
    if removed:
        changed["members_removed"] = removed
    await session.flush()
    return changed


async def create_group(
    session: AsyncSession, caller: ScimCaller, body: Mapping[str, Any], base_url: str
) -> GroupWrite:
    changes = changes_from_resource(body)
    name = changes.display_name
    if not isinstance(name, str):  # pragma: no cover - changes_from_resource always sets it
        raise bad_request("'displayName' is required")
    try:
        group = await org_group_service.create_group(
            session, caller.organization_id, name=name, description=""
        )
    except org_group_service.GroupNameTakenError:
        raise conflict(f"A group named '{name}' already exists") from None
    changes.display_name = UNSET
    changed = await _write(session, caller, group, changes, base_url)
    changed.pop("adopted", None)
    [resource] = await render(session, caller.organization_id, [group], base_url)
    return GroupWrite(
        resource=resource,
        group_id=group.id,
        name=group.name,
        action="create",
        payload=caller.audit_payload(name=group.name, **changed),
    )


async def replace_group(
    session: AsyncSession,
    caller: ScimCaller,
    raw_id: str,
    body: Mapping[str, Any],
    base_url: str,
) -> GroupWrite:
    group = await get_group(session, caller.organization_id, raw_id)
    changed = await _write(session, caller, group, changes_from_resource(body), base_url)
    [resource] = await render(session, caller.organization_id, [group], base_url)
    return GroupWrite(
        resource=resource,
        group_id=group.id,
        name=group.name,
        action="update",
        payload=caller.audit_payload(changes=changed),
    )


async def patch_group(
    session: AsyncSession,
    caller: ScimCaller,
    raw_id: str,
    body: Mapping[str, Any],
    base_url: str,
) -> GroupWrite:
    group = await get_group(session, caller.organization_id, raw_id)
    changes = changes_from_patch(patch_operations(body))
    changed = await _write(session, caller, group, changes, base_url)
    [resource] = await render(session, caller.organization_id, [group], base_url)
    return GroupWrite(
        resource=resource,
        group_id=group.id,
        name=group.name,
        action="update",
        payload=caller.audit_payload(changes=changed),
    )


async def delete_group(session: AsyncSession, caller: ScimCaller, raw_id: str) -> GroupWrite:
    group = await get_group(session, caller.organization_id, raw_id)
    group_id, name = group.id, group.name
    await session.execute(delete(ScimGroupLink).where(ScimGroupLink.group_id == group_id))
    members = await org_group_service.delete_group(session, group, via_scim=True)
    return GroupWrite(
        resource={},
        group_id=group_id,
        name=name,
        action="delete",
        payload=caller.audit_payload(name=name, members=members),
    )


async def delete_org_scim_groups(session: AsyncSession, org_id: uuid.UUID) -> None:
    """The organization purge: its group links (they would cascade; spelled out)."""
    await session.execute(
        delete(ScimGroupLink)
        .where(ScimGroupLink.organization_id == org_id)
        .execution_options(synchronize_session=False)
    )
