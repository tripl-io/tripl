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
"""

from __future__ import annotations

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
from tripl.services import audit_service, docs_links, project_lookup
from tripl.services.docs_access import DocCaller, require_doc_writer, require_org_bulk_delete
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


async def list_tree(session: AsyncSession, slug: str) -> DocTreeResponse:
    project = await _resolve_project(session, slug)
    organization = await session.get(Organization, project.organization_id)
    docs = list(
        await session.scalars(
            select(DocFile).where(store.any_scope_filter(project)).order_by(DocFile.path_key)
        )
    )
    names = await store.user_names(session, [doc.updated_by for doc in docs])
    summaries = [store.summary(doc, names) for doc in docs]
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
    session: AsyncSession, slug: str, scope: DocScope, path: str, *, resolve: bool = True
) -> DocFileResponse:
    project = await _resolve_project(session, slug)
    doc = await store.get_doc(session, project, scope, _path(path))
    return await store.file_response(session, project, doc, resolve=resolve)


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
    session: AsyncSession, slug: str, kind: DocLinkKind, name: str, qualifier: str | None
) -> DocBacklinksResponse:
    project = await _resolve_project(session, slug)
    items = await docs_links.backlinks(session, project, kind, name, qualifier)
    return DocBacklinksResponse(kind=kind, name=name, qualifier=qualifier, items=items)


def _warnings(response: DocFileResponse) -> list[str]:
    return [
        warning
        for warning in (docs_links.link_warning(link) for link in response.links)
        if warning is not None
    ]


async def _write_response(
    session: AsyncSession, project: Project, doc: DocFile, *, created: bool, changed: bool
) -> DocWriteResponse:
    response = await store.file_response(session, project, doc)
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
        return await _write_response(session, project, doc, created=False, changed=False)

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
    return await _write_response(session, project, doc, created=created, changed=True)


async def _move_plan(
    session: AsyncSession, project: Project, body: DocMoveRequest
) -> list[tuple[DocFile, str]]:
    """``(doc, new path)`` for every note the move touches, validated, before any change."""
    scope = body.scope
    if body.folder:
        source, target = _prefix(body.from_path), _prefix(body.to_path)
        if path_key(target) == path_key(source) or path_key(target).startswith(
            path_key(source) + "/"
        ):
            raise HTTPException(status_code=422, detail="A folder cannot be moved into itself")
        docs = list(
            await session.scalars(
                select(DocFile)
                .where(
                    store.scope_filter(project, scope),
                    DocFile.path_key.startswith(path_key(source) + "/", autoescape=True),
                )
                .order_by(DocFile.path_key)
            )
        )
        if not docs:
            raise HTTPException(status_code=404, detail="Folder not found")
        return [(doc, _path(target + doc.path[len(source) :])) for doc in docs]
    source, target = _path(body.from_path), _path(body.to_path)
    doc = await store.get_doc(session, project, scope, source)
    return [(doc, target)]


async def move(
    session: AsyncSession, slug: str, body: DocMoveRequest, caller: DocCaller
) -> DocMoveResponse:
    """Rename or move one note, or every note under a folder; all or nothing."""
    project = await _resolve_project(session, slug)
    await require_doc_writer(session, caller, body.scope, project.organization_id)
    user = caller.user
    plan = [(doc, new) for doc, new in await _move_plan(session, project, body) if doc.path != new]
    if not plan:
        return DocMoveResponse(moved=[])
    moving_ids = {doc.id for doc, _ in plan}
    targets = {path_key(new): new for _, new in plan}
    taken = list(
        await session.scalars(
            select(DocFile.path).where(
                store.scope_filter(project, body.scope),
                DocFile.path_key.in_(list(targets)),
                DocFile.id.not_in(moving_ids),
            )
        )
    )
    if taken:
        raise HTTPException(
            status_code=409,
            detail=f"A doc already exists at: {', '.join(sorted(taken)[:50])}",
        )
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
        },
        commit=False,
    )
    try:
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
    docs = list(
        await session.scalars(
            select(DocFile)
            .where(
                store.scope_filter(project, scope),
                DocFile.path_key.startswith(path_key(folder) + "/", autoescape=True),
            )
            .order_by(DocFile.path_key)
        )
    )
    if not docs:
        raise HTTPException(status_code=404, detail="Folder not found")
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
    await store.reindex_after_write(session, project, scope, doc_ids)
    return deleted
