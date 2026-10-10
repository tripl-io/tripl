"""Sharing a docs catalog note or folder (F24, GH #308).

``GET``/``PUT .../docs/file/sharing`` and ``.../docs/folder/sharing``. The rules
of who then sees what live in ``docs_access``; this module reads and replaces a
note's (or a folder's) visibility and shares.

* A note's sharing is changed by its author, or by an owner or admin of the
  organization; every change is audited as ``doc.share_update`` with the
  before and after (never the content).
* A folder's sharing is changed by an organization owner or admin, or — for
  project notes — by a project editor when every note that follows the folder
  is either their own or still open to the whole project (``level``) and
  editable by them. Holding an ``edit`` share on a restricted note does not
  qualify: it lets a member edit a note, never widen who sees it.
* Shares name members only: a user must be a member of the note's project (a
  project note) or organization (an organization note), and a group must be
  one of the organization's. Sharing never grants project access.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.doc_file import DocFile
from tripl.models.doc_share import DocFolderSetting, DocFolderShare, DocShare
from tripl.models.organization import OrganizationMember
from tripl.models.organization_group import OrganizationGroup
from tripl.models.project import Project
from tripl.schemas.docs import (
    DocShareInput,
    DocShareItem,
    DocSharingResponse,
    DocSharingUpdate,
    DocVisibility,
)
from tripl.services import _docs_store as store
from tripl.services import audit_service, docs_folders, project_access
from tripl.services.docs_access import (
    DOC_NOT_FOUND,
    DocAccess,
    DocCaller,
    LevelRights,
    access_of,
    can_manage_sharing,
    docs_with_access,
    level_rights,
    record_break_glass,
    scope_filter,
)
from tripl.services.docs_paths import DocPathError, DocScope, normalize_doc_path, normalize_prefix
from tripl.services.docs_paths import path_key as key_of
from tripl.services.docs_service import _resolve_project

NOT_A_SHARING_MANAGER = (
    "Only the note's author or an organization owner or admin can change its sharing"
)
NOT_A_FOLDER_MANAGER = (
    "Only an organization owner or admin, or a project editor when every note that "
    "follows the folder is theirs or open to the whole project, can change its sharing"
)

_ShareRow = DocShare | DocFolderShare


def _path(raw: str, *, folder: bool) -> str:
    try:
        return normalize_prefix(raw) if folder else normalize_doc_path(raw)
    except DocPathError as exc:
        raise store.unprocessable(exc) from exc


async def _items(session: AsyncSession, rows: Sequence[_ShareRow]) -> list[DocShareItem]:
    """Shares with display names: users first, then groups, each by name."""
    users = await store.user_names(session, [row.user_id for row in rows])
    group_ids = [row.group_id for row in rows if row.group_id is not None]
    groups: dict[uuid.UUID, str] = {}
    if group_ids:
        result = await session.execute(
            select(OrganizationGroup.id, OrganizationGroup.name).where(
                OrganizationGroup.id.in_(group_ids)
            )
        )
        groups = {group_id: name for group_id, name in result.all()}
    items: list[DocShareItem] = []
    for row in rows:
        if row.user_id is not None:
            items.append(
                DocShareItem(
                    principal_type="user",
                    principal_id=row.user_id,
                    name=users.get(row.user_id, ""),
                    permission=cast(Any, row.permission),
                )
            )
        elif row.group_id is not None:
            items.append(
                DocShareItem(
                    principal_type="group",
                    principal_id=row.group_id,
                    name=groups.get(row.group_id, ""),
                    permission=cast(Any, row.permission),
                )
            )
    items.sort(key=lambda item: (item.principal_type != "user", item.name.lower()))
    return items


async def _doc_shares(session: AsyncSession, doc_id: uuid.UUID) -> list[DocShare]:
    return list(await session.scalars(select(DocShare).where(DocShare.doc_file_id == doc_id)))


async def _folder_shares(session: AsyncSession, folder_id: uuid.UUID) -> list[DocFolderShare]:
    return list(
        await session.scalars(select(DocFolderShare).where(DocFolderShare.folder_id == folder_id))
    )


def _snapshot(visibility: str, inherited: bool, items: list[DocShareItem]) -> dict[str, Any]:
    """What an audit row keeps of a sharing state: no names, no content."""
    return {
        "visibility": visibility,
        "inherited": inherited,
        "shares": [
            {
                "principal_type": item.principal_type,
                "principal_id": str(item.principal_id),
                "permission": item.permission,
            }
            for item in items
        ],
    }


async def _validate_principals(
    session: AsyncSession, project: Project, scope: DocScope, shares: list[DocShareInput]
) -> None:
    """422 unless every user is a member at the note's level and every group the org's."""
    user_ids = {share.principal_id for share in shares if share.principal_type == "user"}
    group_ids = {share.principal_id for share in shares if share.principal_type == "group"}
    if len(user_ids) + len(group_ids) != len(shares):
        raise HTTPException(status_code=422, detail="A person or group is listed twice")
    if user_ids:
        if scope == "project":
            members = await project_access.members_among(session, project.id, user_ids)
        else:
            members = set(
                await session.scalars(
                    select(OrganizationMember.user_id).where(
                        OrganizationMember.organization_id == project.organization_id,
                        OrganizationMember.user_id.in_(user_ids),
                    )
                )
            )
        strangers = user_ids - members
        if strangers:
            where = "project" if scope == "project" else "organization"
            raise HTTPException(
                status_code=422,
                detail=f"Notes can only be shared with members of the {where}",
            )
    if group_ids:
        known = set(
            await session.scalars(
                select(OrganizationGroup.id).where(
                    OrganizationGroup.organization_id == project.organization_id,
                    OrganizationGroup.id.in_(group_ids),
                )
            )
        )
        if group_ids - known:
            raise HTTPException(
                status_code=422, detail="Notes can only be shared with the organization's groups"
            )


def _principal(share: DocShareInput) -> dict[str, uuid.UUID | None]:
    if share.principal_type == "user":
        return {"user_id": share.principal_id, "group_id": None}
    return {"user_id": None, "group_id": share.principal_id}


# ── A note ────────────────────────────────────────────────────────────────────


async def _file_response(
    session: AsyncSession,
    scope: DocScope,
    doc: DocFile,
    access: DocAccess,
    *,
    can_manage: bool,
) -> DocSharingResponse:
    if access.inherited and access.folder_id is not None:
        folder = await session.get(DocFolderSetting, access.folder_id)
        if folder is not None:
            return DocSharingResponse(
                scope=scope,
                path=doc.path,
                visibility=cast(DocVisibility, folder.visibility),
                inherited=True,
                inherited_from=folder.path,
                shares=await _items(session, await _folder_shares(session, folder.id)),
                can_manage=can_manage,
            )
    if doc.visibility_inherited:
        return DocSharingResponse(
            scope=scope, path=doc.path, visibility="level", inherited=True, can_manage=can_manage
        )
    shares: list[DocShareItem] = []
    if doc.visibility == "restricted":
        shares = await _items(session, await _doc_shares(session, doc.id))
    return DocSharingResponse(
        scope=scope,
        path=doc.path,
        visibility=cast(DocVisibility, doc.visibility),
        inherited=False,
        shares=shares,
        can_manage=can_manage,
    )


async def _load_file(
    session: AsyncSession,
    slug: str,
    scope: DocScope,
    path: str,
    caller: DocCaller,
    *,
    audit_read: bool,
) -> tuple[Project, DocFile, DocAccess, LevelRights]:
    """The note, if the caller sees it; an org owner/admin also reaches a hidden one.

    With ``audit_read`` (the GET), an org owner/admin reaching a note hidden
    from them is a break-glass read of its sharing — who it is shared with —
    and is audited as ``doc.break_glass_read`` like any other direct read. The
    PUT is audited as ``doc.share_update`` instead.
    """
    project = await _resolve_project(session, slug)
    doc = await store.get_doc(session, project, scope, _path(path, folder=False))
    access = (await access_of(session, caller.user.id, [doc.id]))[doc.id]
    rights = await level_rights(session, caller, project)
    if not access.readable and not rights.org_admin:
        raise HTTPException(status_code=404, detail=DOC_NOT_FOUND)
    if not access.readable and audit_read:
        await record_break_glass(session, caller, project, doc, "sharing")
    return project, doc, access, rights


async def get_file_sharing(
    session: AsyncSession, slug: str, scope: DocScope, path: str, caller: DocCaller
) -> DocSharingResponse:
    _project, doc, access, rights = await _load_file(
        session, slug, scope, path, caller, audit_read=True
    )
    return await _file_response(
        session, scope, doc, access, can_manage=can_manage_sharing(caller, rights, doc, scope)
    )


async def update_file_sharing(
    session: AsyncSession,
    slug: str,
    scope: DocScope,
    path: str,
    body: DocSharingUpdate,
    caller: DocCaller,
) -> DocSharingResponse:
    """Replace a note's visibility and shares; audited as ``doc.share_update``."""
    project, doc, access, rights = await _load_file(
        session, slug, scope, path, caller, audit_read=False
    )
    if not can_manage_sharing(caller, rights, doc, scope):
        raise HTTPException(status_code=403, detail=NOT_A_SHARING_MANAGER)
    shares = body.shares if not body.inherited and body.visibility == "restricted" else []
    await _validate_principals(session, project, scope, shares)
    before = await _file_response(session, scope, doc, access, can_manage=True)

    await session.execute(delete(DocShare).where(DocShare.doc_file_id == doc.id))
    if body.inherited:
        doc.visibility = "level"
        doc.visibility_inherited = True
    else:
        doc.visibility = body.visibility
        doc.visibility_inherited = False
    now = datetime.now(UTC)
    for share in shares:
        session.add(
            DocShare(
                id=uuid.uuid4(),
                doc_file_id=doc.id,
                permission=share.permission,
                created_at=now,
                **_principal(share),
            )
        )
    await session.flush()
    after_access = (await access_of(session, caller.user.id, [doc.id]))[doc.id]
    after = await _file_response(session, scope, doc, after_access, can_manage=True)
    await audit_service.record(
        session,
        user=caller.user,
        action="doc.share_update",
        target_type="doc",
        target_id=doc.id,
        target_name=doc.path,
        project=project,
        payload={
            "scope": scope,
            "path": doc.path,
            "before": _snapshot(before.visibility, before.inherited, before.shares),
            "after": _snapshot(after.visibility, after.inherited, after.shares),
            "as_author": doc.created_by == caller.user.id,
        },
    )
    after.can_manage = can_manage_sharing(caller, rights, doc, scope)
    return after


# ── A folder ──────────────────────────────────────────────────────────────────


async def _folder_rights(
    session: AsyncSession, caller: DocCaller, project: Project, scope: DocScope, folder: str
) -> tuple[bool, bool]:
    """``(caller sees the folder, caller may manage its sharing)``."""
    rights = await level_rights(session, caller, project)
    pairs = await docs_with_access(
        session,
        caller.user.id,
        scope_filter(project, scope),
        DocFile.path_key.startswith(key_of(folder) + "/", autoescape=True),
        visible_only=False,
    )
    sees = rights.org_admin or any(access.readable for _, access in pairs)
    if caller.read_only_key or not sees:
        return sees, False
    if scope == "organization":
        return sees, rights.writes("organization")
    if rights.org_admin:
        return sees, True
    # A project editor manages a folder only while that could never widen a
    # note beyond what they could change on it alone: every note that follows
    # the folder is theirs, or is still open to the level (and editable). An
    # ``edit`` share on a restricted note is not enough — it lets them edit,
    # not re-share (the note-level rule in ``can_manage_sharing``).
    governed = [(doc, access) for doc, access in pairs if doc.visibility_inherited]
    return sees, rights.project and all(
        access.editable and (doc.created_by == caller.user.id or access.visibility == "level")
        for doc, access in governed
    )


async def _folder_response(
    session: AsyncSession, project: Project, scope: DocScope, folder: str, *, can_manage: bool
) -> DocSharingResponse:
    setting = await docs_folders.find_setting(session, project, scope, folder)
    if setting is not None:
        shares: list[DocShareItem] = []
        if setting.visibility == "restricted":
            shares = await _items(session, await _folder_shares(session, setting.id))
        return DocSharingResponse(
            scope=scope,
            path=folder,
            visibility=cast(DocVisibility, setting.visibility),
            inherited=False,
            shares=shares,
            can_manage=can_manage,
        )
    parent = await docs_folders.nearest_ancestor(session, project, scope, folder)
    if parent is None:
        return DocSharingResponse(
            scope=scope, path=folder, visibility="level", inherited=True, can_manage=can_manage
        )
    return DocSharingResponse(
        scope=scope,
        path=folder,
        visibility=cast(DocVisibility, parent.visibility),
        inherited=True,
        inherited_from=parent.path,
        shares=await _items(session, await _folder_shares(session, parent.id)),
        can_manage=can_manage,
    )


async def get_folder_sharing(
    session: AsyncSession, slug: str, scope: DocScope, path: str, caller: DocCaller
) -> DocSharingResponse:
    project = await _resolve_project(session, slug)
    folder = _path(path, folder=True)
    sees, can_manage = await _folder_rights(session, caller, project, scope, folder)
    if not sees:
        raise HTTPException(status_code=404, detail="Folder not found")
    return await _folder_response(session, project, scope, folder, can_manage=can_manage)


async def update_folder_sharing(
    session: AsyncSession,
    slug: str,
    scope: DocScope,
    path: str,
    body: DocSharingUpdate,
    caller: DocCaller,
) -> DocSharingResponse:
    """Set or clear a folder's visibility and shares; audited as ``doc.share_update``."""
    project = await _resolve_project(session, slug)
    folder = _path(path, folder=True)
    sees, can_manage = await _folder_rights(session, caller, project, scope, folder)
    if not sees:
        raise HTTPException(status_code=404, detail="Folder not found")
    if not can_manage:
        raise HTTPException(status_code=403, detail=NOT_A_FOLDER_MANAGER)
    shares = body.shares if not body.inherited and body.visibility == "restricted" else []
    await _validate_principals(session, project, scope, shares)
    before = await _folder_response(session, project, scope, folder, can_manage=True)

    setting = await docs_folders.find_setting(session, project, scope, folder)
    if body.inherited:
        if setting is not None:
            await docs_folders.delete_settings(session, [setting.id])
            setting = None
    else:
        if setting is None:
            setting = docs_folders.new_setting(project, scope, folder, body.visibility, caller.user)
            session.add(setting)
            await session.flush()
        else:
            setting.visibility = body.visibility
            setting.updated_by = caller.user.id
            setting.updated_at = datetime.now(UTC)
            await session.execute(
                delete(DocFolderShare).where(DocFolderShare.folder_id == setting.id)
            )
        now = datetime.now(UTC)
        for share in shares:
            session.add(
                DocFolderShare(
                    id=uuid.uuid4(),
                    folder_id=setting.id,
                    permission=share.permission,
                    created_at=now,
                    **_principal(share),
                )
            )
    await session.flush()
    after = await _folder_response(session, project, scope, folder, can_manage=can_manage)
    await audit_service.record(
        session,
        user=caller.user,
        action="doc.share_update",
        target_type="doc_folder",
        target_id=setting.id if setting is not None else None,
        target_name=folder,
        project=project,
        payload={
            "scope": scope,
            "path": folder,
            "folder": True,
            "before": _snapshot(before.visibility, before.inherited, before.shares),
            "after": _snapshot(after.visibility, after.inherited, after.shares),
        },
    )
    return after
