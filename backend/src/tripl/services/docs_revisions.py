"""Docs catalog history: revisions, diffs and restore (F22, GH #299).

Every revision holds the note's full content; the diff against the previous
revision is computed on read. A deleted note's revisions go with it (CASCADE),
so history is only ever browsed, and restored, within a live note.
"""

from __future__ import annotations

import difflib
import uuid
from typing import cast

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.doc_file import DocFile, DocRevision
from tripl.models.project import Project
from tripl.schemas.docs import (
    DocRevisionAction,
    DocRevisionDetail,
    DocRevisionListResponse,
    DocRevisionSummary,
    DocWriteResponse,
)
from tripl.services import _docs_store as store
from tripl.services import audit_service
from tripl.services.docs_access import DocCaller, require_doc_writer
from tripl.services.docs_frontmatter import DocContentError, parse_frontmatter
from tripl.services.docs_paths import DocScope, content_bytes
from tripl.services.docs_service import (
    _path,
    _resolve_project,
    _write_response,
)

MAX_DIFF_BYTES = 200 * 1024


def _revision_summary(revision: DocRevision, names: dict[uuid.UUID, str]) -> DocRevisionSummary:
    return DocRevisionSummary(
        id=revision.id,
        number=revision.number,
        action=cast(DocRevisionAction, revision.action),
        path=revision.path,
        message=revision.message,
        author_name=names.get(revision.author_id) if revision.author_id else None,
        created_at=revision.created_at,
        content_sha256=revision.content_sha256,
        size_bytes=content_bytes(revision.content),
        restored_from_number=revision.restored_from_number,
    )


async def list_revisions(
    session: AsyncSession, slug: str, scope: DocScope, path: str
) -> DocRevisionListResponse:
    project = await _resolve_project(session, slug)
    doc = await store.get_doc(session, project, scope, _path(path))
    revisions = list(
        await session.scalars(
            select(DocRevision)
            .where(DocRevision.doc_file_id == doc.id)
            .order_by(DocRevision.number.desc())
        )
    )
    names = await store.user_names(session, [revision.author_id for revision in revisions])
    return DocRevisionListResponse(
        scope=scope,
        path=doc.path,
        current_revision=doc.revision,
        items=[_revision_summary(revision, names) for revision in revisions],
    )


async def _visible_revision(
    session: AsyncSession, project: Project, revision_id: uuid.UUID
) -> tuple[DocRevision, DocFile]:
    row = (
        await session.execute(
            select(DocRevision, DocFile)
            .join(DocFile, DocFile.id == DocRevision.doc_file_id)
            .where(DocRevision.id == revision_id, store.any_scope_filter(project))
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Revision not found")
    return row[0], row[1]


async def get_revision(
    session: AsyncSession, slug: str, revision_id: uuid.UUID
) -> DocRevisionDetail:
    project = await _resolve_project(session, slug)
    revision, _doc = await _visible_revision(session, project, revision_id)
    previous: DocRevision | None = await session.scalar(
        select(DocRevision)
        .where(
            DocRevision.doc_file_id == revision.doc_file_id,
            DocRevision.number < revision.number,
        )
        .order_by(DocRevision.number.desc())
        .limit(1)
    )
    diff = ""
    if previous is not None:
        diff = "".join(
            difflib.unified_diff(
                previous.content.splitlines(keepends=True),
                revision.content.splitlines(keepends=True),
                fromfile=f"a/{previous.path}",
                tofile=f"b/{revision.path}",
                n=3,
            )
        )
    encoded = diff.encode("utf-8")
    truncated = len(encoded) > MAX_DIFF_BYTES
    if truncated:
        diff = encoded[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
    names = await store.user_names(session, [revision.author_id])
    return DocRevisionDetail(
        **_revision_summary(revision, names).model_dump(),
        content=revision.content,
        diff=diff,
        diff_truncated=truncated,
    )


async def restore_revision(
    session: AsyncSession, slug: str, revision_id: uuid.UUID, message: str, caller: DocCaller
) -> DocWriteResponse:
    """Make an old revision's content current again, as a new ``restore`` revision.

    The note keeps its current path: a restore brings back words, not a location.
    """
    project = await _resolve_project(session, slug)
    revision, doc = await _visible_revision(session, project, revision_id)
    scope = store.scope_of(doc)
    await require_doc_writer(session, caller, scope, project.organization_id)
    user = caller.user
    if doc.content == revision.content:
        return await _write_response(session, project, doc, created=False, changed=False)
    try:
        parsed = parse_frontmatter(revision.content, doc.path)
    except DocContentError as exc:
        raise store.unprocessable(exc) from exc
    await store.apply_content(
        session,
        doc,
        content=revision.content,
        parsed=parsed,
        action="restore",
        user=user,
        message=message or f"Restored revision {revision.number}",
        restored_from=revision.number,
    )
    await audit_service.record(
        session,
        user=user,
        action="doc.restore",
        target_type="doc",
        target_id=doc.id,
        target_name=doc.path,
        project=project,
        payload={
            "scope": scope,
            "path": doc.path,
            "revision": doc.revision,
            "restored_from": revision.number,
        },
        commit=False,
    )
    try:
        await session.flush()
        await store.reindex_after_write(session, project, scope, [doc.id])
    except IntegrityError as exc:
        await session.rollback()
        raise store.concurrent_write() from exc
    return await _write_response(session, project, doc, created=False, changed=True)
