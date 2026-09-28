"""Row-level helpers shared by the docs catalog services (F22, GH #299).

``docs_service`` (one note at a time) and ``docs_bundle`` (a whole folder) both
write through :func:`apply_content`, so every write — create, update, move,
restore, import — does the same things: re-parse the frontmatter into columns,
rebuild the note's ``DocLink`` rows, and append a ``DocRevision`` holding the
full content. The audit row and the search reindex belong to the caller, which
knows whether it wrote one note or two thousand.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import cast

from fastapi import HTTPException
from sqlalchemy import ColumnElement, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.doc_file import DocFile, DocLink, DocRevision
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.project import Project
from tripl.models.search_document import SearchDocument
from tripl.models.user import User
from tripl.schemas.docs import (
    DocAudience,
    DocBacklinkItem,
    DocFileResponse,
    DocLinkResolution,
    DocSummary,
)
from tripl.services import docs_links
from tripl.services._celery_dispatch import dispatch
from tripl.services.docs_access import DocAccess, DocPermission
from tripl.services.docs_frontmatter import DocContentError, ParsedDoc, parse_frontmatter
from tripl.services.docs_paths import (
    DocPathError,
    DocScope,
    content_bytes,
    path_key,
)
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.search_service import reindex_project_branch

logger = logging.getLogger(__name__)

DOC_NOT_FOUND = "Doc not found"


def scope_filter(project: Project, scope: DocScope) -> ColumnElement[bool]:
    """The rows of one scope as seen from ``project``: its own, or its organization's."""
    if scope == "project":
        return DocFile.project_id == project.id
    return DocFile.organization_id == project.organization_id


def any_scope_filter(project: Project) -> ColumnElement[bool]:
    return or_(scope_filter(project, "project"), scope_filter(project, "organization"))


def scope_of(doc: DocFile) -> DocScope:
    return "project" if doc.project_id is not None else "organization"


def as_audience(value: str) -> DocAudience:
    return cast(DocAudience, value if value in ("human", "agent", "both") else "both")


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def deleted_record(doc: DocFile) -> dict[str, object]:
    """What an audit row keeps of a deleted note: enough to say what it held."""
    return {"path": doc.path, "revision": doc.revision, "content_sha256": doc.content_sha256}


def concurrent_write() -> HTTPException:
    """Two writers raced to the same note or path; the loser gets the documented 409."""
    return HTTPException(status_code=409, detail="Doc changed concurrently; reload and retry")


def unprocessable(exc: DocPathError | DocContentError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


async def find_doc(
    session: AsyncSession,
    project: Project,
    scope: DocScope,
    path: str,
    *,
    for_update: bool = False,
) -> DocFile | None:
    query = select(DocFile).where(scope_filter(project, scope), DocFile.path_key == path_key(path))
    if for_update:
        # Serialises two writers of the same note, so the base_revision check
        # below the caller sees is the one its write lands on.
        query = query.with_for_update()
    doc: DocFile | None = await session.scalar(query)
    return doc


async def get_doc(session: AsyncSession, project: Project, scope: DocScope, path: str) -> DocFile:
    doc = await find_doc(session, project, scope, path)
    if doc is None:
        raise HTTPException(status_code=404, detail=DOC_NOT_FOUND)
    return doc


async def user_names(
    session: AsyncSession, ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    wanted = {user_id for user_id in ids if user_id is not None}
    if not wanted:
        return {}
    rows = await session.execute(select(User.id, User.name, User.email).where(User.id.in_(wanted)))
    return {user_id: (name or email) for user_id, name, email in rows}


def new_doc(project: Project, scope: DocScope, path: str) -> DocFile:
    """An unsaved note in ``scope``; :func:`apply_content` fills it and adds it."""
    return DocFile(
        id=uuid.uuid4(),
        project_id=project.id if scope == "project" else None,
        organization_id=project.organization_id if scope == "organization" else None,
        path=path,
        path_key=path_key(path),
        revision=0,
        visibility="level",
        visibility_inherited=True,
    )


async def apply_content(
    session: AsyncSession,
    doc: DocFile,
    *,
    content: str,
    parsed: ParsedDoc,
    action: str,
    user: User | None,
    message: str = "",
    restored_from: int | None = None,
) -> DocRevision:
    """Write ``content`` into ``doc`` as a new revision, rebuilding its links."""
    is_new = doc.revision == 0
    # Stamped here rather than left to the server defaults: a server-side value
    # is expired after the flush, and reading it back on an async session
    # would need another round trip (or fail outside the greenlet).
    now = datetime.now(UTC)
    doc.updated_at = now
    doc.content = content
    doc.content_sha256 = sha256(content)
    doc.size_bytes = content_bytes(content)
    doc.title = parsed.title
    doc.description = parsed.description
    doc.tags = list(parsed.tags)
    doc.audience = parsed.audience
    doc.revision = doc.revision + 1
    doc.updated_by = user.id if user else None
    if is_new:
        doc.created_at = now
        doc.created_by = user.id if user else None
        session.add(doc)
        await session.flush()
    await rebuild_links(session, doc, parsed.body)
    return add_revision(
        session, doc, action=action, user=user, message=message, restored_from=restored_from
    )


def add_revision(
    session: AsyncSession,
    doc: DocFile,
    *,
    action: str,
    user: User | None,
    message: str = "",
    restored_from: int | None = None,
) -> DocRevision:
    revision = DocRevision(
        id=uuid.uuid4(),
        created_at=datetime.now(UTC),
        doc_file_id=doc.id,
        number=doc.revision,
        action=action,
        path=doc.path,
        content=doc.content,
        content_sha256=doc.content_sha256,
        message=message[:500],
        author_id=user.id if user else None,
        restored_from_number=restored_from,
    )
    session.add(revision)
    return revision


async def rebuild_links(session: AsyncSession, doc: DocFile, body: str) -> None:
    await session.execute(delete(DocLink).where(DocLink.doc_file_id == doc.id))
    seen: set[tuple[str, str, str | None]] = set()
    for link in docs_links.extract_links(body):
        key = (link.kind, link.target, link.qualifier)
        if key in seen:
            continue
        seen.add(key)
        session.add(
            DocLink(
                id=uuid.uuid4(),
                doc_file_id=doc.id,
                kind=link.kind,
                target=link.target[:500],
                qualifier=link.qualifier[:500] if link.qualifier else None,
            )
        )


def safe_parse(doc: DocFile) -> ParsedDoc:
    """Parse a STORED note; it was valid when written, so fall back rather than fail."""
    try:
        return parse_frontmatter(doc.content, doc.path)
    except DocContentError:
        return ParsedDoc(
            title=doc.title,
            description=doc.description,
            tags=list(doc.tags or []),
            audience=doc.audience,
            body=doc.content,
        )


def summary(
    doc: DocFile,
    names: dict[uuid.UUID, str],
    access: DocAccess | None = None,
    permission: DocPermission = "view",
) -> DocSummary:
    """A note as the tree lists it; ``access`` is the caller's (F24)."""
    return DocSummary(
        scope=scope_of(doc),
        path=doc.path,
        title=doc.title,
        description=doc.description,
        tags=list(doc.tags or []),
        audience=as_audience(doc.audience),
        revision=doc.revision,
        size_bytes=doc.size_bytes,
        updated_at=doc.updated_at,
        updated_by_name=names.get(doc.updated_by) if doc.updated_by else None,
        visibility=access.visibility if access is not None else "level",
        my_permission=permission,
        shared=access.shared if access is not None else False,
    )


async def file_response(
    session: AsyncSession,
    project: Project,
    doc: DocFile,
    *,
    resolve: bool = True,
    access: DocAccess | None = None,
    permission: DocPermission = "view",
    break_glass: bool = False,
    viewer_id: uuid.UUID | None = None,
) -> DocFileResponse:
    """The note as ``viewer_id`` reads it: links to notes they cannot see say so."""
    parsed = safe_parse(doc)
    names = await user_names(session, [doc.created_by, doc.updated_by])
    links: list[DocLinkResolution] = []
    linked_from: list[DocBacklinkItem] = []
    if resolve:
        refs: list[docs_links.LinkRef] = []
        seen: set[tuple[str, str, str | None]] = set()
        for link in docs_links.extract_links(parsed.body):
            key = (link.kind, link.target, link.qualifier)
            if key not in seen:
                seen.add(key)
                refs.append(docs_links.LinkRef(link.kind, link.target, link.qualifier))
        links = await docs_links.resolve_links(session, project, refs, user_id=viewer_id)
        # "Linked from": the notes linking here by id, as this reader may see them.
        linked_from = await docs_links.backlinks(
            session, project, "doc", str(doc.id), user_id=viewer_id
        )
    return DocFileResponse(
        **summary(doc, names, access, permission).model_dump(),
        id=doc.id,
        content=doc.content,
        body=parsed.body,
        extra_frontmatter=parsed.extra,
        links=links,
        linked_from=linked_from,
        created_at=doc.created_at,
        created_by_name=names.get(doc.created_by) if doc.created_by else None,
        break_glass=break_glass,
    )


async def purge_doc_search_rows(
    session: AsyncSession, project: Project, scope: DocScope, doc_ids: Iterable[uuid.UUID]
) -> None:
    """Drop the index rows of ``doc_ids`` from every branch that can show them.

    Doc rows are branch-independent copies of the note, so a delete, a move or
    an edit must not leave the old title and body searchable in a feature
    branch (never rebuilt here) or in another project whose rebuild is only
    queued. They come back with each branch's next rebuild.
    """
    ids = list(doc_ids)
    if not ids:
        return
    if scope == "project":
        where_project: ColumnElement[bool] = SearchDocument.project_id == project.id
    else:
        where_project = SearchDocument.project_id.in_(
            select(Project.id).where(Project.organization_id == project.organization_id)
        )
    for start in range(0, len(ids), 1000):
        await session.execute(
            delete(SearchDocument).where(
                where_project,
                SearchDocument.entity_type == "doc",
                SearchDocument.entity_id.in_(ids[start : start + 1000]),
            )
        )


async def reindex_after_write(
    session: AsyncSession,
    project: Project,
    scope: DocScope,
    doc_ids: Iterable[uuid.UUID] = (),
) -> None:
    """Refresh the search index after a docs write, committing the transaction.

    The written notes' rows are first purged everywhere
    (:func:`purge_doc_search_rows`). The current project's main index is then
    rebuilt inline, the way fact tables do it
    (``fact_table_service._refresh_main_search_index``). An organization note is
    indexed into EVERY project of the organization, so the other projects' main
    branches are handed to the worker (fire-and-forget: a broker outage costs
    freshness until their next rebuild, never the write, and never leaves stale
    content behind). The worker cannot index a non-PostgreSQL database, so there
    they are rebuilt inline instead. Feature branches pick the note up again on
    their next rebuild, as with metrics.
    """
    await purge_doc_search_rows(session, project, scope, doc_ids)
    main_branch_id = await resolve_branch_id(session, project.id, None)
    await reindex_project_branch(
        session, project_id=project.id, branch_id=main_branch_id, slug=project.slug
    )
    if scope != "organization":
        return
    rows = (
        await session.execute(
            select(Project.id, Project.slug, PlanBranch.id)
            .join(PlanBranch, PlanBranch.project_id == Project.id)
            .where(
                Project.organization_id == project.organization_id,
                Project.id != project.id,
                PlanBranch.kind == BranchKind.main.value,
            )
        )
    ).all()
    is_postgres = session.bind.dialect.name == "postgresql"
    for other_id, other_slug, other_branch_id in rows:
        if not is_postgres:
            await reindex_project_branch(
                session, project_id=other_id, branch_id=other_branch_id, slug=other_slug
            )
            continue
        try:
            from tripl.worker.celery_app import celery_app

            await dispatch(
                celery_app.send_task,
                "tripl.worker.tasks.search.reindex_search_branch",
                args=[str(other_id), str(other_branch_id)],
            )
        except Exception:
            logger.warning(
                "Failed to queue the docs reindex of project %s branch %s",
                other_id,
                other_branch_id,
                exc_info=True,
            )
