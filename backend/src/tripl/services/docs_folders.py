"""Folder settings of the docs catalog (F24, GH #308): rows keyed by a path prefix.

Folders are implicit (the prefixes of note paths), so a folder's visibility is a
row of its own, :class:`DocFolderSetting`, looked up by ``path_key``. The
effective rule a note gets from it is computed in ``docs_access``; this module
only keeps the rows in step with folder-wide moves and deletes.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.doc_file import DocFile
from tripl.models.doc_share import DocFolderSetting, DocFolderShare, DocShare
from tripl.models.project import Project
from tripl.models.user import User
from tripl.services.docs_access import effective_folder_id, effective_visibility, scope_filter
from tripl.services.docs_paths import DocScope, path_key


def folder_scope_filter(project: Project, scope: DocScope) -> ColumnElement[bool]:
    """The folder settings of one scope as seen from ``project``."""
    if scope == "project":
        return DocFolderSetting.project_id == project.id
    return DocFolderSetting.organization_id == project.organization_id


def _at_or_under(folder: str) -> ColumnElement[bool]:
    key = path_key(folder)
    return or_(
        DocFolderSetting.path_key == key,
        DocFolderSetting.path_key.startswith(key + "/", autoescape=True),
    )


async def find_setting(
    session: AsyncSession, project: Project, scope: DocScope, folder: str
) -> DocFolderSetting | None:
    setting: DocFolderSetting | None = await session.scalar(
        select(DocFolderSetting).where(
            folder_scope_filter(project, scope), DocFolderSetting.path_key == path_key(folder)
        )
    )
    return setting


async def nearest_ancestor(
    session: AsyncSession, project: Project, scope: DocScope, folder: str
) -> DocFolderSetting | None:
    """The deepest setting strictly ABOVE ``folder`` (what it would inherit)."""
    key = path_key(folder)
    parts = key.split("/")
    ancestors = ["/".join(parts[:index]) for index in range(len(parts) - 1, 0, -1)]
    if not ancestors:
        return None
    rows = {
        setting.path_key: setting
        for setting in await session.scalars(
            select(DocFolderSetting).where(
                folder_scope_filter(project, scope), DocFolderSetting.path_key.in_(ancestors)
            )
        )
    }
    for ancestor in ancestors:
        if ancestor in rows:
            return rows[ancestor]
    return None


def new_setting(
    project: Project, scope: DocScope, folder: str, visibility: str, user: User
) -> DocFolderSetting:
    now = datetime.now(UTC)
    return DocFolderSetting(
        id=uuid.uuid4(),
        project_id=project.id if scope == "project" else None,
        organization_id=project.organization_id if scope == "organization" else None,
        path=folder,
        path_key=path_key(folder),
        visibility=visibility,
        created_by=user.id,
        updated_by=user.id,
        created_at=now,
        updated_at=now,
    )


async def carry_folder_settings(
    session: AsyncSession,
    project: Project,
    scope: DocScope,
    source: str,
    target: str,
    user: User,
    moving_ids: Iterable[uuid.UUID],
) -> None:
    """Copy the settings at and under ``source`` to the same places under ``target``.

    Runs after the moved notes have their new paths. A setting is carried only
    to a folder that holds nothing but moved notes: a folder that already has a
    setting, or already holds notes that were not moved, keeps its own rule —
    a carried setting would otherwise change the access of notes the move
    never touched. The moved notes that do not get their old rule back this
    way are pinned by :func:`pin_sharing` (``docs_service.move``). The source
    rows are left for :func:`drop_orphan_folder_settings`, which removes them
    only once no note lives under them any more.
    """
    settings = list(
        await session.scalars(
            select(DocFolderSetting).where(
                folder_scope_filter(project, scope), _at_or_under(source)
            )
        )
    )
    if not settings:
        return
    wanted = {path_key(target + setting.path[len(source) :]): setting for setting in settings}
    present = set(
        await session.scalars(
            select(DocFolderSetting.path_key).where(
                folder_scope_filter(project, scope), DocFolderSetting.path_key.in_(list(wanted))
            )
        )
    )
    moving = list(set(moving_ids))
    for key, setting in wanted.items():
        if key in present:
            continue
        bystander = await session.scalar(
            select(DocFile.id)
            .where(
                scope_filter(project, scope),
                DocFile.path_key.startswith(key + "/", autoescape=True),
                DocFile.id.not_in(moving),
            )
            .limit(1)
        )
        if bystander is not None:
            continue
        copy = new_setting(
            project, scope, target + setting.path[len(source) :], setting.visibility, user
        )
        session.add(copy)
        await session.flush()
        shares = await session.scalars(
            select(DocFolderShare).where(DocFolderShare.folder_id == setting.id)
        )
        for share in shares.all():
            session.add(
                DocFolderShare(
                    id=uuid.uuid4(),
                    folder_id=copy.id,
                    user_id=share.user_id,
                    group_id=share.group_id,
                    permission=share.permission,
                    created_at=datetime.now(UTC),
                )
            )
    await session.flush()


# ── A note's access across a move ─────────────────────────────────────────────


@dataclass(frozen=True)
class SharingState:
    """Who a note's rule lets in: its effective visibility and shares, by value."""

    visibility: str
    shares: frozenset[tuple[uuid.UUID | None, uuid.UUID | None, str]]

    def snapshot(self) -> dict[str, Any]:
        """The audit form (the same shape ``docs_sharing`` writes; no names)."""
        return {
            "visibility": self.visibility,
            "shares": [
                {
                    "principal_type": "user" if user_id is not None else "group",
                    "principal_id": str(user_id if user_id is not None else group_id),
                    "permission": permission,
                }
                for user_id, group_id, permission in sorted(self.shares, key=str)
            ],
        }


async def sharing_states(
    session: AsyncSession, doc_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, SharingState]:
    """Each note's effective rule at its CURRENT path (flush path changes first)."""
    ids = list(set(doc_ids))
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(
                DocFile.id.label("doc_id"),
                effective_visibility().label("eff_visibility"),
                effective_folder_id().label("eff_folder_id"),
            ).where(DocFile.id.in_(ids))
        )
    ).all()
    folder_ids = {row.eff_folder_id for row in rows if row.eff_folder_id is not None}
    own_ids = [row.doc_id for row in rows if row.eff_folder_id is None]
    by_folder: dict[uuid.UUID, set[tuple[uuid.UUID | None, uuid.UUID | None, str]]] = defaultdict(
        set
    )
    by_doc: dict[uuid.UUID, set[tuple[uuid.UUID | None, uuid.UUID | None, str]]] = defaultdict(set)
    if folder_ids:
        for folder_share in await session.scalars(
            select(DocFolderShare).where(DocFolderShare.folder_id.in_(folder_ids))
        ):
            by_folder[folder_share.folder_id].add(
                (folder_share.user_id, folder_share.group_id, folder_share.permission)
            )
    if own_ids:
        for share in await session.scalars(
            select(DocShare).where(DocShare.doc_file_id.in_(own_ids))
        ):
            by_doc[share.doc_file_id].add((share.user_id, share.group_id, share.permission))
    states: dict[uuid.UUID, SharingState] = {}
    for row in rows:
        visibility = str(row.eff_visibility or "level")
        if visibility not in ("private", "restricted"):
            visibility = "level"
        shares: frozenset[tuple[uuid.UUID | None, uuid.UUID | None, str]] = frozenset()
        if visibility == "restricted":
            source = by_folder[row.eff_folder_id] if row.eff_folder_id else by_doc[row.doc_id]
            shares = frozenset(source)
        states[row.doc_id] = SharingState(visibility=visibility, shares=shares)
    return states


async def pin_sharing(session: AsyncSession, doc: DocFile, state: SharingState) -> None:
    """Give ``doc`` ``state`` as its OWN rule (it stops following folders). No commit.

    A move must not change who sees a note behind its author's back: when the
    note's folder rule differs at the new path, the old rule is copied onto it.
    """
    await session.execute(delete(DocShare).where(DocShare.doc_file_id == doc.id))
    doc.visibility = state.visibility
    doc.visibility_inherited = False
    now = datetime.now(UTC)
    for user_id, group_id, permission in state.shares:
        session.add(
            DocShare(
                id=uuid.uuid4(),
                doc_file_id=doc.id,
                user_id=user_id,
                group_id=group_id,
                permission=permission,
                created_at=now,
            )
        )


async def delete_settings(session: AsyncSession, setting_ids: list[uuid.UUID]) -> None:
    """Delete folder settings and their shares (explicitly: SQLite may not cascade)."""
    if not setting_ids:
        return
    await session.execute(delete(DocFolderShare).where(DocFolderShare.folder_id.in_(setting_ids)))
    await session.execute(delete(DocFolderSetting).where(DocFolderSetting.id.in_(setting_ids)))


async def drop_orphan_folder_settings(
    session: AsyncSession, project: Project, scope: DocScope, folder: str
) -> None:
    """Delete the settings at and under ``folder`` that no note lives under any more."""
    orphans = list(
        await session.scalars(
            select(DocFolderSetting.id).where(
                folder_scope_filter(project, scope),
                _at_or_under(folder),
                ~exists().where(
                    scope_filter(project, scope),
                    # substr, not LIKE: a folder name may hold "_" or "%".
                    func.substr(DocFile.path_key, 1, func.length(DocFolderSetting.path_key) + 1)
                    == DocFolderSetting.path_key + "/",
                ),
            )
        )
    )
    await delete_settings(session, orphans)


# ── Membership changes ────────────────────────────────────────────────────────


async def drop_user_shares_in_org(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Delete ``user_id``'s shares on every note and folder of ``org_id``. No commit.

    Called when the user leaves the organization (``org_service``). Their shares
    would be unreachable anyway — they no longer pass the membership gate — but
    a share must not wait for them to come back.
    """
    projects = select(Project.id).where(Project.organization_id == org_id)
    notes = select(DocFile.id).where(
        (DocFile.organization_id == org_id) | DocFile.project_id.in_(projects)
    )
    folders = select(DocFolderSetting.id).where(
        (DocFolderSetting.organization_id == org_id) | DocFolderSetting.project_id.in_(projects)
    )
    await session.execute(
        delete(DocShare).where(DocShare.user_id == user_id, DocShare.doc_file_id.in_(notes))
    )
    await session.execute(
        delete(DocFolderShare).where(
            DocFolderShare.user_id == user_id, DocFolderShare.folder_id.in_(folders)
        )
    )


async def drop_group_shares(session: AsyncSession, group_ids: Any) -> None:
    """Delete the shares of deleted groups (the FK cascades too; SQLite may not). No commit."""
    await session.execute(delete(DocShare).where(DocShare.group_id.in_(group_ids)))
    await session.execute(delete(DocFolderShare).where(DocFolderShare.group_id.in_(group_ids)))
