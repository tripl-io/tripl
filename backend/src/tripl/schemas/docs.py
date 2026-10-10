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
#: What a ``[[kind:...]]`` link points at (F22 plan links, F24 part 2 the rest).
#: ``doc``, ``alert_rule`` and ``user`` are stored by id; the others by name.
DocLinkKind = Literal[
    "event",
    "event_type",
    "field",
    "doc",
    "variable",
    "metric",
    "alert_rule",
    "branch",
    "scan",
    "data_source",
    "user",
]
#: ``unavailable``: a ``[[doc:<id>]]`` link to a note the reader may not see, or
#: to no note at all: the two read the same (no reason, title, path or route), so
#: a link never confirms that a hidden note exists.
DocLinkStatus = Literal["resolved", "ambiguous", "broken", "unavailable"]
#: Why a link does not resolve, for the reader and the relink picker.
DocLinkReason = Literal[
    "not_found",
    "invalid_id",
    "path_form",
    "not_a_member",
]
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


class DocLanguageDefaults(BaseModel):
    """The project's default languages (lowercase BCP 47); None: the original."""

    agent_lang: str | None = None
    human_lang: str | None = None


class DocTreeResponse(BaseModel):
    project: DocTreeProject
    organization: DocTreeOrganization
    project_docs: list[DocSummary]
    organization_docs: list[DocSummary]
    limits: DocLimits
    language_defaults: DocLanguageDefaults = Field(default_factory=DocLanguageDefaults)


class DocLinkResolution(BaseModel):
    kind: DocLinkKind
    target: str
    # The event type of a field link, or the heading anchor of a doc link.
    qualifier: str | None = None
    raw: str
    status: DocLinkStatus
    route_path: str | None = None
    entity_id: uuid.UUID | None = None
    candidates: int = 0
    # What the link shows now: a note's current title, ``@Name`` for a mention,
    # an alert rule's current name, a metric's display name. None when broken
    # or unavailable, except a hand-typed ``[[doc:path]]`` (reason
    # ``path_form``) with a suggestion: the title of that readable note.
    label: str | None = None
    # A second line: a note's current scope and path. None when not resolved.
    detail: str | None = None
    reason: DocLinkReason | None = None
    # Relink candidates for a broken link: up to 3 current targets closest to
    # the stored one (plan names by similarity; for a hand-typed
    # ``[[doc:path]]``, the id of the readable note now at that path). Each
    # replaces ``target`` in the reference.
    suggestions: list[str] = []


class DocBacklinkItem(BaseModel):
    scope: DocScope
    path: str
    title: str
    description: str = ""
    audience: DocAudience = "both"
    link_raw: str


#: ``pending`` while the AI run is queued or running, ``ready`` once it holds
#: text, ``failed`` when the last run produced none (``error`` says why).
DocTranslationStatus = Literal["pending", "ready", "failed"]
#: Why a read got the original rather than the language it asked for (or the
#: project's agent default): no translation in it, one still being made, one
#: whose last run failed with no text, or, for the default only, one that is
#: behind the original.
DocTranslationFallback = Literal["missing", "pending", "failed", "outdated"]


class DocTranslationSummary(BaseModel):
    lang: str
    status: DocTranslationStatus
    # Saves of the text; 0 while the first AI run has not landed.
    revision: int
    # The original's revision the text was made from; ``outdated`` when the
    # original has moved on since.
    source_revision: int
    outdated: bool
    # True while the text is the model's as it came; a person's edit clears it.
    machine: bool
    error: str = ""
    updated_at: datetime
    updated_by_name: str | None = None


class DocFileResponse(DocSummary):
    id: uuid.UUID
    content: str
    body: str
    extra_frontmatter: dict[str, Any] = {}
    links: list[DocLinkResolution] = []
    # "Linked from" (F24 part 2): the notes linking to this one by id
    # (``[[doc:<id>]]``) that the reader can see. Also ``GET /docs/backlinks``
    # with ``kind=doc&name=<id>``.
    linked_from: list[DocBacklinkItem] = []
    created_at: datetime
    created_by_name: str | None = None
    # F24: true when an organization owner or admin opened a note hidden from
    # them (an audited break-glass read); such a read never grants editing.
    break_glass: bool = False
    # Translations. ``lang`` is the language served (None: the original);
    # ``content``, ``body``, ``title``, ``description`` and ``links`` are then
    # the translation's, and ``revision`` stays the original's.
    # ``requested_lang`` is what the read asked for, or the project's agent
    # default when it asked for nothing; ``translation_fallback`` says why it
    # got the original instead.
    lang: str | None = None
    requested_lang: str | None = None
    translation_fallback: DocTranslationFallback | None = None
    translation_outdated: bool = False
    translations: list[DocTranslationSummary] = []
    language_defaults: DocLanguageDefaults = Field(default_factory=DocLanguageDefaults)


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


class DocLinkSuggestion(BaseModel):
    """One autocomplete candidate of ``GET /docs/link-suggestions``."""

    kind: DocLinkKind
    # The entity's id (a note, a user, a metric...). Plan entities are still
    # referenced by name in ``insert``; the id is for the picker's keys.
    id: uuid.UUID
    label: str
    detail: str = ""
    # The canonical reference text to insert, e.g. ``[[metric:revenue]]``,
    # ``[[doc:<id>]]`` or ``[[user:<id>]]``.
    insert: str


class DocLinkSuggestionsResponse(BaseModel):
    items: list[DocLinkSuggestion]


class DocBacklinksResponse(BaseModel):
    kind: DocLinkKind
    name: str
    qualifier: str | None = None
    items: list[DocBacklinkItem]


class DocBundleFile(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    content: str
    sha256: str | None = None
    # A stored translation: the note it translates and its language. An export
    # sets both and names the file ``<note>.<lang>.md``; an import also takes a
    # ``<note>.<lang>.md`` without them as one, when ``<note>.md`` is a note.
    translation_of: str | None = Field(default=None, max_length=1024)
    lang: str | None = Field(default=None, max_length=64)


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
    # Translation files written, and (``mirror``) the translations removed, as
    # ``<note>.<lang>.md``.
    translations: list[str] = []
    translations_deleted: list[str] = []


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


class DocTranslateRequest(BaseModel):
    scope: DocScope
    path: str = Field(min_length=1, max_length=1024)
    # What the person typed: a code (``en``, ``pt-BR``) or a name in any
    # language (``немецкий``); the model turns a name into a code.
    language: str = Field(min_length=1, max_length=64)
    # Replace a translation a person has edited (it stays in its history).
    overwrite: bool = False


class DocTranslationWrite(BaseModel):
    scope: DocScope
    path: str = Field(min_length=1, max_length=1024)
    lang: str = Field(min_length=1, max_length=64)
    content: str = Field(max_length=12 * MAX_FILE_BYTES)
    base_revision: int | None = Field(default=None, ge=0)
    # Say the text matches the original's current revision (clears "outdated").
    mark_current: bool = False


class DocTranslationRevisionSummary(BaseModel):
    id: uuid.UUID
    number: int
    action: Literal["translate", "edit", "restore"]
    source_revision: int
    author_name: str | None = None
    created_at: datetime


class DocTranslationRevisionDetail(DocTranslationRevisionSummary):
    content: str


class DocLanguageDefaultsUpdate(BaseModel):
    """Codes or names (``en``, ``English``); null or empty: the original."""

    agent_lang: str | None = Field(default=None, max_length=64)
    human_lang: str | None = Field(default=None, max_length=64)
