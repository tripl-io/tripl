"""Docs catalog wire schemas (F22, GH #299).

Mirrored by hand in ``frontend/src/types/docs.ts``; keep the two in sync.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from tripl.services.docs_paths import (
    MAX_BUNDLE_FILES,
    MAX_FILE_BYTES,
    DocScope,
)

DocAudience = Literal["human", "agent", "both"]
DocLinkKind = Literal["event", "event_type", "field"]
DocLinkStatus = Literal["resolved", "ambiguous", "broken"]
DocRevisionAction = Literal["create", "update", "move", "restore", "import"]
DocImportMode = Literal["merge", "mirror"]
DocBundleFormat = Literal["tripl-docs/v1"]
#: Who may read a note (F24, GH #308): its author only, its author plus the
#: people and groups it is shared with, or everyone at its level (the default).
DocVisibility = Literal["private", "restricted", "level"]
#: What a share grants, and what the caller may do with a note.
DocPermission = Literal["view", "edit"]
DocSharePrincipalType = Literal["user", "group"]

__all__ = ["DocScope"]  # re-exported: the scope literal lives next to the path rules


class DocSummary(BaseModel):
    scope: DocScope
    path: str
    title: str
    description: str = ""
    tags: list[str] = []
    audience: DocAudience = "both"
    revision: int
    size_bytes: int
    updated_at: datetime
    updated_by_name: str | None = None
    # F24: the note's effective visibility (its own, or its folder's while it
    # inherits), what the caller may do with it, and whether it is shared with
    # anyone. A note the caller cannot see is never listed.
    visibility: DocVisibility = "level"
    my_permission: DocPermission = "view"
    shared: bool = False


class DocTreeProject(BaseModel):
    slug: str
    name: str


class DocTreeOrganization(BaseModel):
    id: uuid.UUID
    slug: str
    name: str


class DocLimits(BaseModel):
    max_file_bytes: int
    max_files_per_scope: int
    max_bundle_files: int
    max_bundle_bytes: int


class DocTreeResponse(BaseModel):
    project: DocTreeProject
    organization: DocTreeOrganization
    project_docs: list[DocSummary]
    organization_docs: list[DocSummary]
    limits: DocLimits


class DocLinkRef(BaseModel):
    kind: DocLinkKind
    target: str
    qualifier: str | None = None
    label: str | None = None
    raw: str


class DocLinkResolution(BaseModel):
    kind: DocLinkKind
    target: str
    qualifier: str | None = None
    raw: str
    status: DocLinkStatus
    route_path: str | None = None
    entity_id: uuid.UUID | None = None
    candidates: int = 0


class DocFileResponse(DocSummary):
    id: uuid.UUID
    content: str
    body: str
    extra_frontmatter: dict[str, Any] = {}
    links: list[DocLinkResolution] = []
    created_at: datetime
    created_by_name: str | None = None
    # F24: true when an organization owner or admin opened a note hidden from
    # them (an audited break-glass read); such a read never grants editing.
    break_glass: bool = False


class DocWriteRequest(BaseModel):
    # The byte limit (MAX_FILE_BYTES of UTF-8) is enforced by the service with
    # a 413. This character cap only stops a body far beyond it before anything
    # else runs; set well above the limit so an oversized note gets the 413 and
    # its precise message rather than a generic 422.
    content: str = Field(max_length=4 * MAX_FILE_BYTES)
    base_revision: int | None = Field(default=None, ge=1)
    create_only: bool = False
    message: str = Field(default="", max_length=500)


class DocWriteResponse(DocFileResponse):
    created: bool
    changed: bool
    warnings: list[str] = []


class DocMoveRequest(BaseModel):
    scope: DocScope
    from_path: str = Field(min_length=1, max_length=1024)
    to_path: str = Field(min_length=1, max_length=1024)
    folder: bool = False


class DocMovedPath(BaseModel):
    from_path: str
    to_path: str


class DocMoveResponse(BaseModel):
    moved: list[DocMovedPath]


class DocFolderDeleteResponse(BaseModel):
    deleted: list[str]


class DocRestoreRequest(BaseModel):
    message: str = Field(default="", max_length=500)


class DocRevisionSummary(BaseModel):
    id: uuid.UUID
    number: int
    action: DocRevisionAction
    path: str
    message: str
    author_name: str | None = None
    created_at: datetime
    content_sha256: str
    size_bytes: int
    restored_from_number: int | None = None


class DocRevisionListResponse(BaseModel):
    scope: DocScope
    path: str
    current_revision: int
    items: list[DocRevisionSummary]


class DocRevisionDetail(DocRevisionSummary):
    content: str
    diff: str
    diff_truncated: bool = False


class DocSearchHit(BaseModel):
    scope: DocScope
    path: str
    title: str
    description: str = ""
    tags: list[str] = []
    audience: DocAudience = "both"
    snippet: str = ""
    score: float
    confidence: float = 0.0


class DocSearchResponse(BaseModel):
    items: list[DocSearchHit]
    total: int
    truncated: bool = False
    semantic_used: bool = False


class DocBacklinkItem(BaseModel):
    scope: DocScope
    path: str
    title: str
    description: str = ""
    audience: DocAudience = "both"
    link_raw: str


class DocBacklinksResponse(BaseModel):
    kind: DocLinkKind
    name: str
    qualifier: str | None = None
    items: list[DocBacklinkItem]


class DocBundleFile(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    content: str
    sha256: str | None = None


class DocBundle(BaseModel):
    format: DocBundleFormat = "tripl-docs/v1"
    scope: DocScope
    project_slug: str
    organization_slug: str | None = None
    exported_at: datetime
    files: list[DocBundleFile]


class DocImportRequest(BaseModel):
    format: DocBundleFormat = "tripl-docs/v1"
    files: list[DocBundleFile] = Field(max_length=MAX_BUNDLE_FILES)


class DocImportSkipped(BaseModel):
    path: str
    reason: str


class DocImportError(BaseModel):
    path: str
    detail: str


class DocImportResult(BaseModel):
    scope: DocScope
    mode: DocImportMode
    dry_run: bool
    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    deleted: list[str] = []
    skipped: list[DocImportSkipped] = []
    errors: list[DocImportError] = []


# ── Sharing (F24, GH #308) ────────────────────────────────────────────────────


class DocShareItem(BaseModel):
    principal_type: DocSharePrincipalType
    principal_id: uuid.UUID
    #: The user's name (or email) or the group's name.
    name: str = ""
    permission: DocPermission = "view"


class DocShareInput(BaseModel):
    principal_type: DocSharePrincipalType
    principal_id: uuid.UUID
    permission: DocPermission = "view"


class DocSharingResponse(BaseModel):
    """A note's or a folder's sharing.

    ``inherited`` is true when it follows the nearest folder setting above it,
    named by ``inherited_from`` (``None`` when no folder has one and the default,
    ``level``, applies). ``visibility`` and ``shares`` are then the folder's.
    """

    scope: DocScope
    path: str
    visibility: DocVisibility
    inherited: bool
    inherited_from: str | None = None
    shares: list[DocShareItem] = []
    can_manage: bool = False


class DocSharingUpdate(BaseModel):
    """``inherited: true`` drops the note's (or folder's) own setting and shares."""

    visibility: DocVisibility = "level"
    inherited: bool = False
    shares: list[DocShareInput] = Field(default=[], max_length=200)
