"""Who may read and write which docs catalog notes (F22, GH #299; F24, GH #308).

Two layers, checked in this order.

**The level** (F22, F20 PR4 org roles). Every docs route is under
``/projects/{slug}/docs`` behind the project membership gate, so only members
reach a note at all. Project notes are then written by an editing project role
(``EditorUserDep``). Organization notes reach further than the path's project —
they are shown in, and indexed into, every project of the organization — so:

* only an owner or admin of the note's organization (``organization_members``,
  the organization of the path's project) may create, edit, move, restore or
  delete them;
* a project-bound API key is fenced into one project (``_enforce_project_scope``),
  so it may not change notes that other projects read;
* deleting organization notes in bulk (a folder delete, a ``mirror`` import)
  follows the strict owner gate: organization owner/admin **and** a browser
  session, never an API key (``deps.get_owner_user``).

The organization id is always taken from the resolved project row, never from
the caller, so an admin of another organization holds nothing here.

**The note** (F24). ``doc_files.visibility`` — or, while
``visibility_inherited`` is true, the setting of the nearest folder above the
note (``doc_folder_settings``) — narrows who may see it:

* ``level``: everyone the level admits (the F22 behaviour); anyone who may write
  at the level may edit.
* ``restricted``: the author, plus the users and organization groups it is
  shared with (``doc_shares``, or the folder's ``doc_folder_shares``). A
  ``view`` share reads, an ``edit`` share also edits.
* ``private``: the author only.

The author (``created_by``) always reads and edits. Sharing never grants the
level: an ``edit`` share lets a caller edit only if the level lets them write
too, and a share to someone outside the project never reaches them (they never
pass the membership gate). Group membership is resolved at read time.

Organization owners and admins keep an audited **break-glass read**: they may
open a note they cannot see directly, by path or revision id, and every such
read writes ``doc.break_glass_read``. It is never LISTED to them — the tree,
search, back-links, exports and counts all go through
:func:`visible_docs_clause` — and it does not let them edit.

Every rule above is expressed ONCE, as SQL over ``doc_files``
(:func:`visible_docs_clause`, :func:`editable_docs_clause`), so a list, a search,
a back-link query and a single read cannot disagree. :func:`access_of` runs the
same expressions for single notes.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, cast

from fastapi import HTTPException, status
from sqlalchemy import (
    ColumnElement,
    ScalarSelect,
    and_,
    case,
    exists,
    false,
    func,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from tripl.models.doc_file import DocFile
from tripl.models.doc_share import DocFolderSetting, DocFolderShare, DocShare
from tripl.models.organization_group import OrganizationGroupMember
from tripl.models.project import Project
from tripl.models.user import User
from tripl.services import audit_service, project_access
from tripl.services.docs_paths import DocScope

ORG_NOTES_ADMIN_REQUIRED = "Organization owner or admin role required to edit organization notes"
DOC_NOT_FOUND = "Doc not found"
NOTE_NOT_EDITABLE = "This note is shared with you for viewing only"
BREAK_GLASS_NOT_EDITABLE = (
    "An organization owner or admin can read this note, but only its author "
    "or someone it is shared with for editing can change it"
)

DocVisibility = Literal["private", "restricted", "level"]
DocPermission = Literal["view", "edit"]
_READ: tuple[str, ...] = ("view", "edit")
_EDIT: tuple[str, ...] = ("edit",)


@dataclass(frozen=True)
class DocCaller:
    """The request facts the rules need, beside the user."""

    user: User
    via_api_key: bool = False
    key_project_bound: bool = False
    read_only_key: bool = False
    #: The caller's role in the path's project, as the membership gate stashed
    #: it; ``None`` when unknown (a service called directly), then looked up.
    project_role: str | None = None

    @classmethod
    def from_request_state(cls, user: User, state: Any) -> DocCaller:
        """Read what ``deps`` stashed on ``request.state``."""
        scope = getattr(state, "api_key_scope", None)
        return cls(
            user=user,
            via_api_key=scope is not None,
            key_project_bound=getattr(state, "api_key_project_id", None) is not None,
            read_only_key=scope == "read",
            project_role=getattr(state, "project_role", None),
        )


# ── The rule, as SQL ──────────────────────────────────────────────────────────


def _in_groups_of(user_id: uuid.UUID) -> Any:
    return select(OrganizationGroupMember.group_id).where(
        OrganizationGroupMember.user_id == user_id
    )


def nearest_folder_id() -> ScalarSelect[uuid.UUID]:
    """The id of the deepest folder setting above the ``DocFile`` row, or NULL.

    Correlated to :class:`DocFile`; a folder ``a/b`` governs ``a/b/c.md`` and
    ``a/b/x/y.md``, never ``a/bc.md``.
    """
    folder = aliased(DocFolderSetting)
    return (
        select(folder.id)
        .where(
            or_(
                folder.project_id == DocFile.project_id,
                folder.organization_id == DocFile.organization_id,
            ),
            func.substr(DocFile.path_key, 1, func.length(folder.path_key) + 1)
            == folder.path_key + "/",
        )
        .order_by(func.length(folder.path_key).desc())
        .limit(1)
        .correlate(DocFile)
        .scalar_subquery()
    )


def effective_folder_id() -> ColumnElement[uuid.UUID | None]:
    """The folder setting the note follows: the nearest one, while it inherits."""
    return case((DocFile.visibility_inherited.is_(True), nearest_folder_id()), else_=None)


def effective_visibility() -> ColumnElement[str]:
    """The note's visibility: its folder's while it inherits one, else its own."""
    folder = aliased(DocFolderSetting)
    folder_visibility = (
        select(folder.visibility)
        .where(folder.id == effective_folder_id())
        .correlate(DocFile)
        .scalar_subquery()
    )
    return func.coalesce(folder_visibility, DocFile.visibility)


def _shared_clause(user_id: uuid.UUID | None, permissions: tuple[str, ...]) -> ColumnElement[bool]:
    """The note is shared with ``user_id`` (or with anyone, for ``None``) at ``permissions``."""
    share = aliased(DocShare)
    folder_share = aliased(DocFolderShare)
    folder_id = effective_folder_id()
    own = [share.doc_file_id == DocFile.id, share.permission.in_(permissions)]
    via_folder = [folder_share.folder_id == folder_id, folder_share.permission.in_(permissions)]
    if user_id is not None:
        own.append(or_(share.user_id == user_id, share.group_id.in_(_in_groups_of(user_id))))
        via_folder.append(
            or_(
                folder_share.user_id == user_id,
                folder_share.group_id.in_(_in_groups_of(user_id)),
            )
        )
    return or_(
        and_(folder_id.is_(None), exists().where(*own).correlate(DocFile)),
        and_(
            folder_id.is_not(None),
            exists().where(*via_folder).correlate(DocFile),
        ),
    )


def _access_clause(user_id: uuid.UUID | None, permissions: tuple[str, ...]) -> ColumnElement[bool]:
    visibility = effective_visibility()
    if user_id is None:
        return visibility == "level"
    return or_(
        DocFile.created_by == user_id,
        visibility == "level",
        and_(visibility == "restricted", _shared_clause(user_id, permissions)),
    )


def visible_docs_clause(user_id: uuid.UUID | None) -> ColumnElement[bool]:
    """SQL: ``user_id`` may SEE the ``DocFile`` row (list it, search it, read it).

    The one filter of every list, search, back-link, export and count. ``None``
    (no caller: a worker, an internal search) sees ``level`` notes only. The
    level itself (project membership) is the route's gate, not this clause's.
    """
    return _access_clause(user_id, _READ)


def editable_docs_clause(user_id: uuid.UUID | None) -> ColumnElement[bool]:
    """SQL: the note's own rule lets ``user_id`` edit it (the level is checked apart)."""
    if user_id is None:
        return false()
    return _access_clause(user_id, _EDIT)


@dataclass(frozen=True)
class DocAccess:
    """What one caller may do with one note, by the note's rule alone."""

    visibility: DocVisibility
    inherited: bool
    folder_id: uuid.UUID | None
    readable: bool
    editable: bool
    shared: bool


def access_columns(user_id: uuid.UUID | None) -> tuple[Any, ...]:
    """The :class:`DocAccess` of the ``DocFile`` row, as selectable columns."""
    return (
        effective_visibility().label("eff_visibility"),
        effective_folder_id().label("eff_folder_id"),
        case((visible_docs_clause(user_id), True), else_=False).label("readable"),
        case((editable_docs_clause(user_id), True), else_=False).label("editable"),
        case((_shared_clause(None, _READ), True), else_=False).label("shared"),
    )


def _access_from_row(doc: DocFile, row: Any) -> DocAccess:
    visibility = str(row.eff_visibility or "level")
    return DocAccess(
        visibility=cast(
            DocVisibility, visibility if visibility in ("private", "restricted") else "level"
        ),
        inherited=bool(doc.visibility_inherited),
        folder_id=row.eff_folder_id,
        readable=bool(row.readable),
        editable=bool(row.editable),
        shared=bool(row.shared) and visibility == "restricted",
    )


async def docs_with_access(
    session: AsyncSession,
    user_id: uuid.UUID | None,
    *where: ColumnElement[bool],
    visible_only: bool = True,
    order_by: Any = DocFile.path_key,
) -> list[tuple[DocFile, DocAccess]]:
    """The notes matching ``where`` with their access; only visible ones by default."""
    conditions = list(where)
    if visible_only:
        conditions.append(visible_docs_clause(user_id))
    rows = await session.execute(
        select(DocFile, *access_columns(user_id)).where(*conditions).order_by(order_by)
    )
    return [(row[0], _access_from_row(row[0], row)) for row in rows]


async def access_of(
    session: AsyncSession, user_id: uuid.UUID | None, doc_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, DocAccess]:
    ids = list(set(doc_ids))
    if not ids:
        return {}
    pairs = await docs_with_access(
        session, user_id, DocFile.id.in_(ids), visible_only=False, order_by=DocFile.id
    )
    return {doc.id: access for doc, access in pairs}


async def readers_among(
    session: AsyncSession, doc_id: uuid.UUID, user_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    """The users among ``user_ids`` the note's own rule lets read ``doc_id``.

    :func:`visible_docs_clause` once per user (the clause is per reader); the
    level (project membership) is the caller's to check. Used to decide who an
    @mention in a note may notify (F24 part 2).
    """
    readers: set[uuid.UUID] = set()
    for user_id in set(user_ids):
        found = await session.scalar(
            select(DocFile.id).where(DocFile.id == doc_id, visible_docs_clause(user_id))
        )
        if found is not None:
            readers.add(user_id)
    return readers


async def hidden_doc_ids(
    session: AsyncSession, user_id: uuid.UUID | None, project_id: uuid.UUID
) -> list[uuid.UUID]:
    """Ids of the notes a project shows that ``user_id`` may NOT see (the search filter)."""
    organization_id = (
        select(Project.organization_id).where(Project.id == project_id).scalar_subquery()
    )
    rows = await session.scalars(
        select(DocFile.id).where(
            or_(
                DocFile.project_id == project_id,
                DocFile.organization_id == organization_id,
            ),
            ~visible_docs_clause(user_id),
        )
    )
    return list(rows.all())


# ── The level ─────────────────────────────────────────────────────────────────


async def require_doc_writer(
    session: AsyncSession, caller: DocCaller, scope: DocScope, org_id: uuid.UUID
) -> None:
    """Who may create, edit, move, restore or delete a note of ``scope``, by level.

    ``org_id`` is the organization of the path's project, which owns the
    organization notes.
    """
    if scope != "organization":
        return
    if not await project_access.is_org_admin(session, caller.user, org_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ORG_NOTES_ADMIN_REQUIRED)
    if caller.key_project_bound:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A project-scoped API key cannot edit organization notes",
        )


async def require_org_bulk_delete(
    session: AsyncSession, caller: DocCaller, scope: DocScope, org_id: uuid.UUID, what: str
) -> None:
    """Deleting many organization notes at once: org owner/admin, browser session only."""
    await require_doc_writer(session, caller, scope, org_id)
    if scope != "organization":
        return
    if caller.via_api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Owner session required to {what} organization notes",
        )


@dataclass(frozen=True)
class LevelRights:
    """Whether the level lets the caller write notes of each scope."""

    project: bool
    organization: bool
    org_admin: bool

    def writes(self, scope: DocScope) -> bool:
        return self.project if scope == "project" else self.organization


async def level_rights(session: AsyncSession, caller: DocCaller, project: Project) -> LevelRights:
    """The non-raising form of the write gates, for ``my_permission``."""
    org_admin = await project_access.is_org_admin(session, caller.user, project.organization_id)
    if caller.read_only_key:
        return LevelRights(project=False, organization=False, org_admin=org_admin)
    role = caller.project_role
    if role is None:
        role = await project_access.member_role(session, caller.user, project.id)
    return LevelRights(
        project=project_access.can_edit(role),
        organization=org_admin and not caller.key_project_bound,
        org_admin=org_admin,
    )


def my_permission(access: DocAccess, rights: LevelRights, scope: DocScope) -> DocPermission:
    return "edit" if access.editable and rights.writes(scope) else "view"


def can_manage_sharing(
    caller: DocCaller, rights: LevelRights, doc: DocFile, scope: DocScope
) -> bool:
    """Who may change one note's sharing: an org owner/admin, or its author with level write.

    A single-note move that would change the note's access (it starts or stops
    following a folder setting) is a sharing change too, so ``docs_service.move``
    asks the same question.
    """
    if caller.read_only_key:
        return False
    if scope == "organization" and caller.key_project_bound:
        return False
    return rights.org_admin or (doc.created_by == caller.user.id and rights.writes(scope))


# ── One note ──────────────────────────────────────────────────────────────────


async def record_break_glass(
    session: AsyncSession, caller: DocCaller, project: Project, doc: DocFile, what: str
) -> None:
    """Record an organization owner/admin reading a note they cannot see."""
    await audit_service.record(
        session,
        user=caller.user,
        action="doc.break_glass_read",
        target_type="doc",
        target_id=doc.id,
        target_name=doc.path,
        project=project,
        payload={
            "scope": "project" if doc.project_id is not None else "organization",
            "path": doc.path,
            "read": what,
            "via_api_key": caller.via_api_key,
        },
    )


async def require_readable(
    session: AsyncSession, caller: DocCaller, project: Project, doc: DocFile, *, what: str
) -> tuple[DocAccess, bool]:
    """``(access, break_glass)`` for a DIRECT read of ``doc``; 404 when it is hidden.

    An organization owner or admin reads a hidden note anyway, and the read is
    audited as ``doc.break_glass_read`` (``what`` says which read it was).
    """
    access = (await access_of(session, caller.user.id, [doc.id]))[doc.id]
    if access.readable:
        return access, False
    if await project_access.is_org_admin(session, caller.user, project.organization_id):
        await record_break_glass(session, caller, project, doc, what)
        return access, True
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=DOC_NOT_FOUND)


async def require_editable(
    session: AsyncSession, caller: DocCaller, project: Project, doc: DocFile
) -> DocAccess:
    """The note's own rule for a write: 404 when hidden, 403 when view-only."""
    access = (await access_of(session, caller.user.id, [doc.id]))[doc.id]
    if access.editable:
        return access
    if access.readable:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=NOTE_NOT_EDITABLE)
    if await project_access.is_org_admin(session, caller.user, project.organization_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=BREAK_GLASS_NOT_EDITABLE)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=DOC_NOT_FOUND)
