"""The docs catalog: Markdown notes for people and agents (F22).

Two scopes, one route family. A project's own notes and the notes of its
ORGANIZATION are both reached under ``/projects/{slug}/docs...``, and ``scope``
picks the root. That keeps every read and write inside the project-membership
gate and the API-key project fence the rest of the API already applies, so a
key fenced to one project reads that project's organization notes and nothing
beyond them.

There is one version of each file: the catalog is NOT branch-aware, so no
builder here takes ``branch``. Links inside a note (``[[event:NAME]]``) resolve
against the main plan.

The bundle builders carry ``tripl-docs/v1``, the one format both ``tripl docs
pull``/``push`` and the app's import/export dialog speak: a flat list of
``{path, content}`` where ``content`` is the raw file, frontmatter included.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from tripl_cli.api.request import ApiRequest
from tripl_cli.model import JsonDict, JsonList, as_dict, as_list

TREE = "/projects/{slug}/docs"
FILE = "/projects/{slug}/docs/file"
SEARCH = "/projects/{slug}/docs/search"
EXPORT = "/projects/{slug}/docs/export"
IMPORT = "/projects/{slug}/docs/import"

ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("get", TREE),
    ("get", FILE),
    ("put", FILE),
    ("get", SEARCH),
    ("get", EXPORT),
    ("post", IMPORT),
)

# `DocScope`, verbatim: the `scope` query parameter every file route takes.
# Pinned to backend/openapi.json by the contract test, and mirrored by
# tripl_mcp.enums.DocScope.
SCOPES: tuple[str, ...] = ("project", "organization")
# `DocAudience`, verbatim: who a note is written for, from its frontmatter.
AUDIENCES: tuple[str, ...] = ("human", "agent", "both")
# The import route's `mode`. "mirror" also DELETES every note of the scope that
# the bundle does not carry.
IMPORT_MODES: tuple[str, ...] = ("merge", "mirror")
MODE_MERGE = "merge"
MODE_MIRROR = "mirror"

BUNDLE_FORMAT = "tripl-docs/v1"
# The export route's `format`. The CLI only ever asks for JSON: a zip is for a
# browser download, and JSON is what the import route takes back.
EXPORT_FORMAT_JSON = "json"

# The service's own limits, checked locally by `tripl docs push` so an oversize
# folder fails at exit 2 naming the file instead of as a 413/422 after the upload.
MAX_FILE_BYTES = 256 * 1024
MAX_BUNDLE_FILES = 2000
MAX_BUNDLE_BYTES = 20 * 1024 * 1024

# GET /docs/search's own bounds: `q` is 1..500 and `limit` 1..50, default 20.
SEARCH_QUERY_MAX_LENGTH = 500
SEARCH_LIMIT_DEFAULT = 20
SEARCH_LIMIT_MAX = 50


def list_docs(slug: str) -> ApiRequest:
    """``DocTreeResponse``: both roots at once, ``project_docs`` and ``organization_docs``.

    Flat lists of ``DocSummary`` (no content). Folders are implicit in the paths.
    """
    return ApiRequest("GET", TREE.format(slug=slug))


def read_doc(slug: str, scope: str, path: str) -> ApiRequest:
    """``DocFileResponse``: the raw content, the parsed frontmatter, the links with status."""
    return ApiRequest("GET", FILE.format(slug=slug), params={"scope": scope, "path": path})


def write_doc(
    slug: str,
    scope: str,
    path: str,
    content: str,
    *,
    base_revision: int | None = None,
    message: str | None = None,
    create_only: bool | None = None,
) -> ApiRequest:
    """Create or replace one note. Editor-gated: a ``tk_w_`` key backed by an editor or owner.

    ``base_revision`` is the optimistic lock: the API answers 409 when the note
    moved past it. Unset members are omitted so the server's defaults apply.
    """
    body: JsonDict = {"content": content}
    if base_revision is not None:
        body["base_revision"] = base_revision
    if message is not None:
        body["message"] = message
    if create_only is not None:
        body["create_only"] = create_only
    return ApiRequest(
        "PUT", FILE.format(slug=slug), params={"scope": scope, "path": path}, json_body=body
    )


def search_docs(
    slug: str, query: str, *, scope: str | None = None, limit: int | None = None
) -> ApiRequest:
    """``DocSearchResponse``: ranked hits over both scopes unless ``scope`` narrows it.

    The same hybrid index ``/search`` reads, restricted to notes. Not paged.
    """
    return ApiRequest(
        "GET",
        SEARCH.format(slug=slug),
        params={"q": query, "scope": scope, "limit": limit},
    )


def export_docs(slug: str, scope: str) -> ApiRequest:
    """``DocBundle`` as JSON: every note of one scope, raw content included."""
    return ApiRequest(
        "GET",
        EXPORT.format(slug=slug),
        params={"scope": scope, "format": EXPORT_FORMAT_JSON},
    )


def import_docs(
    slug: str,
    scope: str,
    files: Sequence[tuple[str, str]],
    *,
    mode: str = MODE_MERGE,
    dry_run: bool = False,
) -> ApiRequest:
    """``DocImportResult``. Editor-gated. The organization scope refuses a
    project-bound key, and ``mirror`` on it refuses every key (it takes the
    owner's browser session).

    ``files`` is ``(path, content)`` pairs. A ``dry_run`` answers the same result
    and changes nothing, which is how ``tripl docs push`` previews before it asks.
    """
    body: JsonDict = {
        "format": BUNDLE_FORMAT,
        "files": [{"path": path, "content": content} for path, content in files],
    }
    return ApiRequest(
        "POST",
        IMPORT.format(slug=slug),
        params={"scope": scope, "mode": mode, "dry_run": dry_run},
        json_body=body,
    )


def tree_docs(payload: Any, scope: str | None = None) -> JsonList:
    """The ``DocSummary`` rows of a tree, project notes first, optionally one scope only.

    One reader for both surfaces: ``tripl docs ls`` and the MCP's ``list_docs``
    read the same two keys of the same route.
    """
    tree = as_dict(payload)
    rows: JsonList = []
    if scope in (None, "project"):
        rows.extend(as_list(tree.get("project_docs")))
    if scope in (None, "organization"):
        rows.extend(as_list(tree.get("organization_docs")))
    return rows


def bundle_files(payload: Any) -> JsonList:
    """The ``files`` of a ``DocBundle``, dropping anything that is not an object."""
    return as_list(as_dict(payload).get("files"))
