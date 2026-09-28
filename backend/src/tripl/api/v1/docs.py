"""Docs catalog routes (F22, GH #299): Markdown notes for people and AI agents.

Every route is under ``/projects/{slug}/docs`` and mounted behind the project
membership gate, so any member (viewers and read-scope keys included) reads, and
a non-member gets 404. ``?scope=project|organization`` picks the project's own
notes or its organization's. Writes carry ``EditorUserDep``; the services add the
organization rules for organization notes (``services.docs_access``).

Every read also honours each note's visibility (F24, GH #308): a note the caller
cannot see is absent from the tree, search, back-links and exports, and a direct
read answers 404 — except to an organization owner or admin, whose read is an
audited break-glass (``doc.break_glass_read``) and never lists anything. API keys
act as the user who minted them, so the CLI and the MCP server see exactly what
their user sees. ``.../file/sharing`` and ``.../folder/sharing`` read and change
who a note or folder is shared with.
"""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, File, Query, Request, Response, UploadFile, status

from tripl.api.deps import CurrentUserDep, EditorUserDep, SessionDep
from tripl.models.user import User
from tripl.schemas.docs import (
    DocBacklinksResponse,
    DocBundle,
    DocFileResponse,
    DocFolderDeleteResponse,
    DocImportMode,
    DocImportRequest,
    DocImportResult,
    DocLinkKind,
    DocLinkResolution,
    DocMoveRequest,
    DocMoveResponse,
    DocRestoreRequest,
    DocRevisionDetail,
    DocRevisionListResponse,
    DocSearchResponse,
    DocSharingResponse,
    DocSharingUpdate,
    DocTreeResponse,
    DocWriteRequest,
    DocWriteResponse,
)
from tripl.schemas.text_filters import FreeTextFilter
from tripl.services import docs_bundle, docs_revisions, docs_search, docs_service, docs_sharing
from tripl.services.docs_access import DocCaller
from tripl.services.docs_paths import MAX_ZIP_UPLOAD_BYTES, DocScope

router = APIRouter(prefix="/projects/{slug}/docs", tags=["docs"])

ScopeQuery = Annotated[
    DocScope, Query(description="Whose notes: the project's or its organization's.")
]
PathQuery = Annotated[
    str, Query(min_length=1, max_length=1024, description="The note's path, e.g. guides/setup.md")
]
FolderQuery = Annotated[str, Query(min_length=1, max_length=1024, description="A folder prefix.")]
MAX_LINK_REFS = 200


def _caller(request: Request, user: User) -> DocCaller:
    return DocCaller.from_request_state(user, request.state)


@router.get("", response_model=DocTreeResponse)
async def list_docs(
    request: Request, session: SessionDep, slug: str, current_user: CurrentUserDep
) -> DocTreeResponse:
    return await docs_service.list_tree(session, slug, _caller(request, current_user))


@router.get("/file", response_model=DocFileResponse)
async def read_doc(
    request: Request,
    session: SessionDep,
    slug: str,
    scope: ScopeQuery,
    path: PathQuery,
    current_user: CurrentUserDep,
) -> DocFileResponse:
    return await docs_service.read_file(session, slug, scope, path, _caller(request, current_user))


@router.get("/file/sharing", response_model=DocSharingResponse)
async def read_doc_sharing(
    request: Request,
    session: SessionDep,
    slug: str,
    scope: ScopeQuery,
    path: PathQuery,
    current_user: CurrentUserDep,
) -> DocSharingResponse:
    """Who the note is shared with. 404 for a note the caller cannot see."""
    return await docs_sharing.get_file_sharing(
        session, slug, scope, path, _caller(request, current_user)
    )


@router.put("/file/sharing", response_model=DocSharingResponse)
async def update_doc_sharing(
    request: Request,
    session: SessionDep,
    slug: str,
    scope: ScopeQuery,
    path: PathQuery,
    body: DocSharingUpdate,
    current_user: EditorUserDep,
) -> DocSharingResponse:
    """Change the note's visibility and shares: its author, or an org owner/admin."""
    return await docs_sharing.update_file_sharing(
        session, slug, scope, path, body, _caller(request, current_user)
    )


@router.get("/folder/sharing", response_model=DocSharingResponse)
async def read_folder_sharing(
    request: Request,
    session: SessionDep,
    slug: str,
    scope: ScopeQuery,
    path: FolderQuery,
    current_user: CurrentUserDep,
) -> DocSharingResponse:
    """A folder's setting, inherited by the notes under it that do not override it."""
    return await docs_sharing.get_folder_sharing(
        session, slug, scope, path, _caller(request, current_user)
    )


@router.put("/folder/sharing", response_model=DocSharingResponse)
async def update_folder_sharing(
    request: Request,
    session: SessionDep,
    slug: str,
    scope: ScopeQuery,
    path: FolderQuery,
    body: DocSharingUpdate,
    current_user: EditorUserDep,
) -> DocSharingResponse:
    """Set (or, with ``inherited: true``, clear) a folder's visibility and shares."""
    return await docs_sharing.update_folder_sharing(
        session, slug, scope, path, body, _caller(request, current_user)
    )


@router.put("/file", response_model=DocWriteResponse)
async def write_doc(
    request: Request,
    session: SessionDep,
    slug: str,
    scope: ScopeQuery,
    path: PathQuery,
    body: DocWriteRequest,
    current_user: EditorUserDep,
) -> DocWriteResponse:
    return await docs_service.write_file(
        session, slug, scope, path, body, _caller(request, current_user)
    )


@router.delete("/file", status_code=status.HTTP_204_NO_CONTENT)
async def delete_doc(
    request: Request,
    session: SessionDep,
    slug: str,
    scope: ScopeQuery,
    path: PathQuery,
    current_user: EditorUserDep,
) -> Response:
    await docs_service.delete_file(session, slug, scope, path, _caller(request, current_user))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/folder", response_model=DocFolderDeleteResponse)
async def delete_doc_folder(
    request: Request,
    session: SessionDep,
    slug: str,
    scope: ScopeQuery,
    path: FolderQuery,
    current_user: EditorUserDep,
) -> DocFolderDeleteResponse:
    deleted = await docs_service.delete_folder(
        session, slug, scope, path, _caller(request, current_user)
    )
    return DocFolderDeleteResponse(deleted=deleted)


@router.post("/move", response_model=DocMoveResponse)
async def move_doc(
    request: Request,
    session: SessionDep,
    slug: str,
    body: DocMoveRequest,
    current_user: EditorUserDep,
) -> DocMoveResponse:
    return await docs_service.move(session, slug, body, _caller(request, current_user))


@router.get("/revisions", response_model=DocRevisionListResponse)
async def list_doc_revisions(
    request: Request,
    session: SessionDep,
    slug: str,
    scope: ScopeQuery,
    path: PathQuery,
    current_user: CurrentUserDep,
) -> DocRevisionListResponse:
    return await docs_revisions.list_revisions(
        session, slug, scope, path, _caller(request, current_user)
    )


@router.get("/revisions/{revision_id}", response_model=DocRevisionDetail)
async def get_doc_revision(
    request: Request,
    session: SessionDep,
    slug: str,
    revision_id: uuid.UUID,
    current_user: CurrentUserDep,
) -> DocRevisionDetail:
    return await docs_revisions.get_revision(
        session, slug, revision_id, _caller(request, current_user)
    )


@router.post("/revisions/{revision_id}/restore", response_model=DocWriteResponse)
async def restore_doc_revision(
    request: Request,
    session: SessionDep,
    slug: str,
    revision_id: uuid.UUID,
    body: DocRestoreRequest,
    current_user: EditorUserDep,
) -> DocWriteResponse:
    return await docs_revisions.restore_revision(
        session, slug, revision_id, body.message, _caller(request, current_user)
    )


@router.get("/search", response_model=DocSearchResponse)
async def search_docs(
    request: Request,
    session: SessionDep,
    slug: str,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    current_user: CurrentUserDep,
    scope: Annotated[DocScope | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> DocSearchResponse:
    return await docs_search.search_docs(
        session, slug, q, _caller(request, current_user), scope=scope, limit=limit
    )


@router.get("/backlinks", response_model=DocBacklinksResponse)
async def doc_backlinks(
    request: Request,
    session: SessionDep,
    slug: str,
    current_user: CurrentUserDep,
    kind: DocLinkKind,
    # FreeTextFilter: both bind straight into a Postgres parameter, where a NUL
    # is a 500. The guard runs before min_length, so ?name=%00 answers 422.
    name: Annotated[FreeTextFilter, Query(min_length=1, max_length=500)],
    qualifier: Annotated[FreeTextFilter | None, Query(max_length=500)] = None,
) -> DocBacklinksResponse:
    return await docs_service.backlinks(
        session, slug, kind, name, qualifier or None, _caller(request, current_user)
    )


@router.get("/links", response_model=list[DocLinkResolution])
async def resolve_doc_links(
    session: SessionDep,
    slug: str,
    ref: Annotated[
        list[str],
        Query(
            max_length=MAX_LINK_REFS,
            description="kind:name, kind being event, event-type or field (repeatable).",
        ),
    ],
) -> list[DocLinkResolution]:
    return await docs_service.resolve_refs(session, slug, ref)


@router.get(
    "/export",
    response_model=DocBundle,
    responses={200: {"content": {"application/zip": {}}}},
)
async def export_docs(
    request: Request,
    session: SessionDep,
    slug: str,
    scope: ScopeQuery,
    current_user: CurrentUserDep,
    format: Annotated[Literal["json", "zip"], Query()] = "json",
) -> DocBundle | Response:
    caller = _caller(request, current_user)
    if format == "zip":
        data, filename = await docs_bundle.export_zip(session, slug, scope, caller)
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return await docs_bundle.export_bundle(session, slug, scope, caller)


@router.post("/import", response_model=DocImportResult)
async def import_docs(
    request: Request,
    session: SessionDep,
    slug: str,
    scope: ScopeQuery,
    body: DocImportRequest,
    current_user: EditorUserDep,
    mode: Annotated[DocImportMode, Query()] = "merge",
    dry_run: Annotated[bool, Query()] = False,
) -> DocImportResult:
    return await docs_bundle.import_bundle(
        session,
        slug,
        scope,
        body.files,
        mode=mode,
        dry_run=dry_run,
        caller=_caller(request, current_user),
    )


@router.post("/import/zip", response_model=DocImportResult)
async def import_docs_zip(
    request: Request,
    session: SessionDep,
    slug: str,
    scope: ScopeQuery,
    file: Annotated[UploadFile, File(description="A zip of .md files.")],
    current_user: EditorUserDep,
    mode: Annotated[DocImportMode, Query()] = "merge",
    dry_run: Annotated[bool, Query()] = False,
    keep_root: Annotated[bool, Query()] = False,
) -> DocImportResult:
    # One byte past the limit is enough for the parser to answer 413.
    data = await file.read(MAX_ZIP_UPLOAD_BYTES + 1)
    return await docs_bundle.import_zip(
        session,
        slug,
        scope,
        data,
        keep_root=keep_root,
        mode=mode,
        dry_run=dry_run,
        caller=_caller(request, current_user),
    )
