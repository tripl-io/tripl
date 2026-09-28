"""The docs catalog: Markdown notes for people and AI agents (F22, GH #299).

Two scopes, both reached through a project: its own notes (``project``) and its
organization's (``organization``). Every public function takes the path's
project slug and resolves it in :func:`_resolve_project`, the one place a
project is looked up here.

Every write does the same five things, in order: re-parse the frontmatter,
rebuild the note's links, append a revision (``_docs_store.apply_content``),
write the audit row without committing, then reindex search, which commits
(``_docs_store.reindex_after_write``). So the note, its history, its audit row
and its index entry land in one transaction.

Who may see and change each note (F24) is decided in ``docs_access``: every list
here filters through ``visible_docs_clause``, a direct read goes through
``require_readable`` (with the audited break-glass read of an organization
owner/admin), and a write through ``require_editable``. Folder operations only
ever touch the notes the caller can see; a note hidden from them stays where it
is, uncounted.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.doc_file import DocFile
from tripl.models.organization import Organization
from tripl.models.project import Project
from tripl.schemas.docs import (
    DocBacklinksResponse,
    DocFileResponse,
    DocLimits,
    DocLinkKind,
    DocLinkResolution,
    DocMovedPath,
    DocMoveRequest,
    DocMoveResponse,
    DocTreeOrganization,
    DocTreeProject,
    DocTreeResponse,
    DocWriteRequest,
    DocWriteResponse,
)
from tripl.services import _docs_store as store
from tripl.services import audit_service, docs_folders, docs_links, project_lookup
from tripl.services.docs_access import (
    NOTE_NOT_EDITABLE,
    DocAccess,
    DocCaller,
    DocPermission,
    access_of,
    can_manage_sharing,
    docs_with_access,
    level_rights,
    my_permission,
    require_doc_writer,
    require_editable,
    require_org_bulk_delete,
    require_readable,
)
from tripl.services.docs_frontmatter import DocContentError, parse_frontmatter
from tripl.services.docs_paths import (
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_FILES,
    MAX_FILE_BYTES,
    MAX_FILES_PER_SCOPE,
    DocPathError,
    DocScope,
    content_bytes,
    normalize_doc_path,
    normalize_prefix,
    path_key,
)


async def _resolve_project(session: AsyncSession, slug: str) -> Project:
    """The ONE project lookup of the docs catalog, scoped to the request's organization."""
    return await project_lookup.resolve_project(session, slug)


def _path(raw: str) -> str:
    try:
        return normalize_doc_path(raw)
    except DocPathError as exc:
        raise store.unprocessable(exc) from exc


def _prefix(raw: str) -> str:
    try:
        return normalize_prefix(raw)
    except DocPathError as exc:
        raise store.unprocessable(exc) from exc


async def list_tree(session: AsyncSession, slug: str, caller: DocCaller) -> DocTreeResponse:
    """The notes of the project and its organization that the caller can see."""
    project = await _resolve_project(session, slug)
    organization = await session.get(Organization, project.organization_id)
    pairs = await docs_with_access(session, caller.user.id, store.any_scope_filter(project))
    rights = await level_rights(session, caller, project)
    names = await store.user_names(session, [doc.updated_by for doc, _ in pairs])
    summaries = [
        store.summary(doc, names, access, my_permission(access, rights, store.scope_of(doc)))
        for doc, access in pairs
    ]
    return DocTreeResponse(
        project=DocTreeProject(slug=project.slug, name=project.name),
        organization=DocTreeOrganization(
            id=project.organization_id,
            slug=organization.slug if organization else "",
            name=organization.name if organization else "",
        ),
        project_docs=[item for item in summaries if item.scope == "project"],
        organization_docs=[item for item in summaries if item.scope == "organization"],
        limits=DocLimits(
            max_file_bytes=MAX_FILE_BYTES,
            max_files_per_scope=MAX_FILES_PER_SCOPE,
            max_bundle_files=MAX_BUNDLE_FILES,
            max_bundle_bytes=MAX_BUNDLE_BYTES,
        ),
    )


async def read_file(
    session: AsyncSession,
    slug: str,
    scope: DocScope,
    path: str,
    caller: DocCaller,
    *,
    resolve: bool = True,
) -> DocFileResponse:
    """One note; 404 when hidden, unless an org owner/admin reads it (audited)."""
    project = await _resolve_project(session, slug)
    doc = await store.get_doc(session, project, scope, _path(path))
    access, break_glass = await require_readable(session, caller, project, doc, what="file")
    return await file_response_for(
        session, project, doc, caller, access, resolve=resolve, break_glass=break_glass
    )


async def file_response_for(
    session: AsyncSession,
    project: Project,
    doc: DocFile,
    caller: DocCaller,
    access: DocAccess | None = None,
    *,
    resolve: bool = True,
    break_glass: bool = False,
) -> DocFileResponse:
    """:func:`store.file_response` with the caller's access filled in."""
    if access is None:
        access = (await access_of(session, caller.user.id, [doc.id]))[doc.id]
    rights = await level_rights(session, caller, project)
    permission: DocPermission = (
        "view" if break_glass else my_permission(access, rights, store.scope_of(doc))
    )
    return await store.file_response(
        session,
        project,
        doc,
        resolve=resolve,
        access=access,
        permission=permission,
        break_glass=break_glass,
    )


async def resolve_refs(
    session: AsyncSession, slug: str, refs: list[str]
) -> list[DocLinkResolution]:
    """``GET /docs/links``: resolve ``kind:name`` refs for the editor's live preview."""
    project = await _resolve_project(session, slug)
    parsed: list[docs_links.LinkRef] = []
    for ref in refs:
        try:
            kind, target, qualifier = docs_links.parse_ref(ref)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        parsed.append(docs_links.LinkRef(kind, target, qualifier))
    return await docs_links.resolve_links(session, project, parsed)


async def backlinks(
    session: AsyncSession,
    slug: str,
    kind: DocLinkKind,
    name: str,
    qualifier: str | None,
    caller: DocCaller,
) -> DocBacklinksResponse:
    project = await _resolve_project(session, slug)
    items = await docs_links.backlinks(
        session, project, kind, name, qualifier, user_id=caller.user.id
    )
    return DocBacklinksResponse(kind=kind, name=name, qualifier=qualifier, items=items)


def _warnings(response: DocFileResponse) -> list[str]:
    return [
        warning
        for warning in (docs_links.link_warning(link) for link in response.links)
        if warning is not None
    ]


async def _write_response(
    session: AsyncSession,
    project: Project,
    doc: DocFile,
    caller: DocCaller,
    *,
    created: bool,
    changed: bool,
) -> DocWriteResponse:
    response = await file_response_for(session, project, doc, caller)
    return DocWriteResponse(
        **response.model_dump(), created=created, changed=changed, warnings=_warnings(response)
    )


async def _count_in_scope(session: AsyncSession, project: Project, scope: DocScope) -> int:
    count: int | None = await session.scalar(
        select(func.count()).select_from(DocFile).where(store.scope_filter(project, scope))
    )
    return count or 0


async def write_file(
    session: AsyncSession,
    slug: str,
    scope: DocScope,
    path: str,
    body: DocWriteRequest,
    caller: DocCaller,
) -> DocWriteResponse:
    """Create or update the note at ``path``.

    The note is read ``FOR UPDATE``, and a unique-index clash from a racing
    create (or a writer on a database without row locks) answers 409, never 500.
    """
    project = await _resolve_project(session, slug)
    await require_doc_writer(session, caller, scope, project.organization_id)
    user = caller.user
    normalized = _path(path)
    if content_bytes(body.content) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413, detail=f"Doc is larger than {MAX_FILE_BYTES // 1024} KiB"
        )
    try:
        parsed = parse_frontmatter(body.content, normalized)
    except DocContentError as exc:
        raise store.unprocessable(exc) from exc

    doc = await store.find_doc(session, project, scope, normalized, for_update=True)
    if doc is not None:
        await _require_editable_at_path(session, caller, project, doc, normalized)
    if doc is not None and body.create_only:
        raise HTTPException(status_code=409, detail=f"A doc already exists at {doc.path}")
    if body.base_revision is not None and (doc is None or doc.revision != body.base_revision):
        raise HTTPException(
            status_code=409, detail=f"Doc changed since revision {body.base_revision}"
        )
    if doc is not None and doc.path != normalized:
        raise HTTPException(
            status_code=409,
            detail=f"A doc named {doc.path} already exists (paths are case-insensitive)",
        )

    created = doc is None
    if doc is None:
        if await _count_in_scope(session, project, scope) >= MAX_FILES_PER_SCOPE:
            raise HTTPException(
                status_code=422,
                detail=f"This scope already holds {MAX_FILES_PER_SCOPE} docs, the maximum",
            )
        doc = store.new_doc(project, scope, normalized)
    elif doc.content == body.content:
        return await _write_response(session, project, doc, caller, created=False, changed=False)

    try:
        await store.apply_content(
            session,
            doc,
            content=body.content,
            parsed=parsed,
            action="create" if created else "update",
            user=user,
            message=body.message,
        )
        await audit_service.record(
            session,
            user=user,
            action="doc.create" if created else "doc.update",
            target_type="doc",
            target_id=doc.id,
            target_name=doc.path,
            project=project,
            payload={"scope": scope, "path": doc.path, "revision": doc.revision},
            commit=False,
        )
        await session.flush()
        await store.reindex_after_write(session, project, scope, [doc.id])
    except IntegrityError as exc:
        await session.rollback()
        raise store.concurrent_write() from exc
    return await _write_response(session, project, doc, caller, created=created, changed=True)


async def _require_editable_at_path(
    session: AsyncSession, caller: DocCaller, project: Project, doc: DocFile, path: str
) -> None:
    """A write to an existing note's path: the note's own rule.

    A note hidden from the caller answers 409 on a write (the path is taken)
    rather than 404: a PUT to a free path creates, so a 404 would be a lie. The
    reply names only the path the caller sent, never the note.
    """
    try:
        await require_editable(session, caller, project, doc)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=409, detail=f"A doc already exists at {path}") from None
        raise


async def folder_docs_for_write(
    session: AsyncSession,
    caller: DocCaller,
    project: Project,
    scope: DocScope,
    folder: str,
) -> list[DocFile]:
    """The notes under ``folder`` a folder-wide write touches: those the caller sees.

    404 when the caller sees none (the folder does not exist for them), 403 when
    they see one they may not edit. Notes hidden from them are left alone.
    """
    pairs = await docs_with_access(
        session,
        caller.user.id,
        store.scope_filter(project, scope),
        DocFile.path_key.startswith(path_key(folder) + "/", autoescape=True),
    )
    if not pairs:
        raise HTTPException(status_code=404, detail="Folder not found")
    if any(not access.editable for _, access in pairs):
        raise HTTPException(
            status_code=403,
            detail=f"{NOTE_NOT_EDITABLE}: the folder holds notes you cannot edit",
        )
    return [doc for doc, _ in pairs]


async def _move_plan(
    session: AsyncSession, project: Project, body: DocMoveRequest, caller: DocCaller
) -> list[tuple[DocFile, str]]:
    """``(doc, new path)`` for every note the move touches, validated, before any change."""
    scope = body.scope
    if body.folder:
        source, target = _prefix(body.from_path), _prefix(body.to_path)
        if path_key(target) == path_key(source) or path_key(target).startswith(
            path_key(source) + "/"
        ):
            raise HTTPException(status_code=422, detail="A folder cannot be moved into itself")
        docs = await folder_docs_for_write(session, caller, project, scope, source)
        return [(doc, _path(target + doc.path[len(source) :])) for doc in docs]
    source, target = _path(body.from_path), _path(body.to_path)
    doc = await store.get_doc(session, project, scope, source)
    await require_editable(session, caller, project, doc)
    return [(doc, target)]


async def _keep_access(
    session: AsyncSession,
    project: Project,
    body: DocMoveRequest,
    caller: DocCaller,
    plan: list[tuple[DocFile, str]],
    before: dict[uuid.UUID, docs_folders.SharingState],
) -> list[str]:
    """A move changes no one's access unless the mover may change the note's sharing.

    A note that follows its folder's rule could gain or lose readers just by
    landing under another folder (or leaving one, or its folder's ancestor).
    That is a sharing change, and only the note's author or an org owner/admin
    may make one (:func:`docs_access.can_manage_sharing`):

    * a single-note move by such a caller lets the note follow its new folder,
      audited as ``doc.share_update`` (``via_move``);
    * anyone else's move — an ``edit`` share, a level editor moving a
      colleague's note — and every folder-wide move keep the note's old rule,
      copied onto it (:func:`docs_folders.pin_sharing`).

    Returns the new paths of the pinned notes (for the ``doc.move`` audit row).
    """
    after = await docs_folders.sharing_states(session, [doc.id for doc, _ in plan])
    changed = [
        (doc, before[doc.id], after[doc.id])
        for doc, _ in plan
        if doc.id in before and doc.id in after and before[doc.id] != after[doc.id]
    ]
    if not changed:
        return []
    rights = await level_rights(session, caller, project)
    pinned: list[str] = []
    for doc, old, new in changed:
        if not body.folder and can_manage_sharing(caller, rights, doc, body.scope):
            await audit_service.record(
                session,
                user=caller.user,
                action="doc.share_update",
                target_type="doc",
                target_id=doc.id,
                target_name=doc.path,
                project=project,
                payload={
                    "scope": body.scope,
                    "path": doc.path,
                    "before": {**old.snapshot(), "inherited": True},
                    "after": {**new.snapshot(), "inherited": True},
                    "as_author": doc.created_by == caller.user.id,
                    "via_move": True,
                },
                commit=False,
            )
            continue
        await docs_folders.pin_sharing(session, doc, old)
        pinned.append(doc.path)
    await session.flush()
    return sorted(pinned)


async def move(
    session: AsyncSession, slug: str, body: DocMoveRequest, caller: DocCaller
) -> DocMoveResponse:
    """Rename or move one note, or every note under a folder; all or nothing."""
    project = await _resolve_project(session, slug)
    await require_doc_writer(session, caller, body.scope, project.organization_id)
    user = caller.user
    plan = [
        (doc, new)
        for doc, new in await _move_plan(session, project, body, caller)
        if doc.path != new
    ]
    if not plan:
        return DocMoveResponse(moved=[])
    moving_ids = {doc.id for doc, _ in plan}
    targets = {path_key(new): new for _, new in plan}
    taken = await docs_with_access(
        session,
        caller.user.id,
        store.scope_filter(project, body.scope),
        DocFile.path_key.in_(list(targets)),
        DocFile.id.not_in(moving_ids),
        visible_only=False,
    )
    if taken:
        # Name only the notes the caller can see; a hidden one is "a path".
        shown = sorted(doc.path for doc, access in taken if access.readable)
        raise HTTPException(
            status_code=409,
            detail=(
                f"A doc already exists at: {', '.join(shown[:50])}"
                if shown
                else "A doc already exists at the target path"
            ),
        )
    # Every moved note's access BEFORE the move, by value (see _keep_access).
    before = await docs_folders.sharing_states(session, moving_ids)
    # Two passes, so the unique index never sees a transient clash between one
    # moving note's new path and another moving note's old one.
    for doc, _ in plan:
        doc.path_key = f"~moving/{doc.id}"
    await session.flush()
    moved: list[DocMovedPath] = []
    now = datetime.now(UTC)
    for doc, new in plan:
        old = doc.path
        doc.path = new
        doc.path_key = path_key(new)
        doc.revision += 1
        doc.updated_by = user.id
        doc.updated_at = now
        store.add_revision(session, doc, action="move", user=user, message=f"Moved from {old}")
        moved.append(DocMovedPath(from_path=old, to_path=new))
    try:
        await session.flush()
        if body.folder:
            # The moved notes keep the access their folder gave them: its
            # setting (and those below it) go with them where that changes no
            # other note, and stay behind only while notes hidden from the
            # caller still live under the old path.
            source, target = _prefix(body.from_path), _prefix(body.to_path)
            await docs_folders.carry_folder_settings(
                session, project, body.scope, source, target, user, moving_ids
            )
            await session.flush()
        pinned = await _keep_access(session, project, body, caller, plan, before)
        if body.folder:
            await docs_folders.drop_orphan_folder_settings(
                session, project, body.scope, _prefix(body.from_path)
            )
        await session.flush()
        await audit_service.record(
            session,
            user=user,
            action="doc.move",
            target_type="doc",
            target_id=plan[0][0].id if not body.folder else None,
            target_name=moved[0].to_path if not body.folder else body.to_path,
            project=project,
            payload={
                "scope": body.scope,
                "from_path": moved[0].from_path if not body.folder else body.from_path,
                "to_path": moved[0].to_path if not body.folder else body.to_path,
                "folder": body.folder,
                "count": len(moved),
                "access_kept": pinned,
            },
            commit=False,
        )
        await session.flush()
        await store.reindex_after_write(session, project, body.scope, moving_ids)
    except IntegrityError as exc:
        await session.rollback()
        raise store.concurrent_write() from exc
    return DocMoveResponse(moved=moved)


async def delete_file(
    session: AsyncSession, slug: str, scope: DocScope, path: str, caller: DocCaller
) -> None:
    project = await _resolve_project(session, slug)
    await require_doc_writer(session, caller, scope, project.organization_id)
    user = caller.user
    doc = await store.get_doc(session, project, scope, _path(path))
    await require_editable(session, caller, project, doc)
    await audit_service.record(
        session,
        user=user,
        action="doc.delete",
        target_type="doc",
        target_id=doc.id,
        target_name=doc.path,
        project=project,
        payload={
            "scope": scope,
            "path": doc.path,
            "revision": doc.revision,
            "content_sha256": doc.content_sha256,
        },
        commit=False,
    )
    doc_id = doc.id
    await session.delete(doc)
    await session.flush()
    await store.reindex_after_write(session, project, scope, [doc_id])


async def delete_folder(
    session: AsyncSession, slug: str, scope: DocScope, prefix: str, caller: DocCaller
) -> list[str]:
    """Delete every note under ``prefix``.

    Deleting organization notes in bulk takes the same rule as a mirror import
    (:func:`docs_access.require_org_bulk_delete`). The audit row names every
    deleted note with its revision and content hash.
    """
    project = await _resolve_project(session, slug)
    await require_org_bulk_delete(
        session, caller, scope, project.organization_id, "delete folders of"
    )
    user = caller.user
    folder = _prefix(prefix)
    docs = await folder_docs_for_write(session, caller, project, scope, folder)
    deleted = [doc.path for doc in docs]
    await audit_service.record(
        session,
        user=user,
        action="doc.folder_delete",
        target_type="doc",
        target_id=None,
        target_name=folder,
        project=project,
        payload={
            "scope": scope,
            "path": folder,
            "count": len(deleted),
            "deleted": [store.deleted_record(doc) for doc in docs],
        },
        commit=False,
    )
    doc_ids = [doc.id for doc in docs]
    for doc in docs:
        await session.delete(doc)
    await session.flush()
    await docs_folders.drop_orphan_folder_settings(session, project, scope, folder)
    await store.reindex_after_write(session, project, scope, doc_ids)
    return deleted
