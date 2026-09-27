"""Docs catalog tools: team notes in Markdown, for people and for agents (F22).

A project's notes and its organization's notes both hang off the project, so
every tool takes ``slug`` and a ``scope``. Reads work with a ``tk_r_`` key.
``write_doc`` needs a ``tk_w_`` key backed by an editor or owner, and the server
refuses anything less with a 403.

Deliberately NOT exposed: move, rename, delete, restore and bulk import. A note
deleted by an agent takes its revision history with it, and an import can
replace a whole folder at once. Both stay with a human, in the app or in
``tripl docs push``, which asks first. ``write_doc`` is a single-note create or
replace, with an optimistic lock (``base_revision``) so an agent cannot silently
overwrite an edit it never read.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from tripl_cli.api import docs, page_items, page_total, search, send

from tripl_mcp.enums import DocAudience, DocScope
from tripl_mcp.runtime import client_for
from tripl_mcp.tools._common import (
    DOC_LIST_FIELDS,
    DOC_READ_FIELDS,
    DOC_WRITE_FIELDS,
    READ_ONLY,
    WRITE_REPLACE,
    trim,
)

# What a `both` note matches: it is written for either reader.
_AUDIENCE_MATCHES: dict[str, tuple[str, ...]] = {
    "human": ("human", "both"),
    "agent": ("agent", "both"),
    "both": ("both",),
}


async def list_docs(
    slug: str,
    ctx: Context,  # type: ignore[type-arg]
    scope: DocScope | None = None,
    audience: DocAudience | None = None,
) -> dict[str, Any]:
    client = client_for(ctx)
    data = await send(client, docs.list_docs(slug))
    rows = docs.tree_docs(data, scope)
    if audience is not None:
        rows = [row for row in rows if row.get("audience") in _AUDIENCE_MATCHES[audience]]
    return {
        "items": [trim(row, DOC_LIST_FIELDS) for row in rows],
        "total": len(rows),
        "note": "Items carry no content; use read_doc with the item's scope and path.",
    }


async def read_doc(
    slug: str,
    scope: DocScope,
    path: str,
    ctx: Context,  # type: ignore[type-arg]
) -> Any:
    client = client_for(ctx)
    return trim(await send(client, docs.read_doc(slug, scope, path)), DOC_READ_FIELDS)


async def search_docs(
    slug: str,
    q: str,
    ctx: Context,  # type: ignore[type-arg]
    scope: DocScope | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    client = client_for(ctx)
    data = await send(client, docs.search_docs(slug, q, scope=scope, limit=limit))
    # The same envelope search_plan answers, read through the same shared
    # readers: `truncated` and `semantic_used` mean what they mean there.
    return {
        "items": page_items(data),
        "total": page_total(data),
        "truncated": search.truncated(data),
        "semantic_used": search.semantic_used(data),
    }


async def write_doc(
    slug: str,
    scope: DocScope,
    path: str,
    content: str,
    ctx: Context,  # type: ignore[type-arg]
    base_revision: int | None = None,
    message: str | None = None,
) -> Any:
    client = client_for(ctx)
    data = await send(
        client,
        docs.write_doc(slug, scope, path, content, base_revision=base_revision, message=message),
    )
    return with_link_warnings(trim(data, DOC_WRITE_FIELDS))


def with_link_warnings(data: Any) -> Any:
    """Hoist a saved note's broken-link warnings to the front of the payload.

    The note WAS saved. A link to an event, field or event type the main plan
    does not have is kept as written and shows as broken in the app, so the
    agent should fix the name now rather than leave a dead link for a reader.
    """
    if isinstance(data, dict) and data.get("warnings"):
        return {
            "IMPORTANT_warnings": data["warnings"],
            "note": (
                "The note was saved, but these [[links]] do not resolve to exactly one "
                "entity on the main plan. Check the names with search_plan and write the "
                "note again with base_revision set to the revision below."
            ),
            "result": {k: v for k, v in data.items() if k != "warnings"},
        }
    return data


def register(mcp: FastMCP) -> None:
    mcp.tool(
        name="list_docs",
        annotations=READ_ONLY,
        description=(
            "List the team's Markdown notes (the docs catalog) for a project: its own "
            "notes (scope 'project') and its organization's notes (scope 'organization'), "
            "both unless 'scope' narrows it. Each item has scope, path, title, "
            "description, tags, audience and revision, but no content. 'audience' keeps "
            "notes written for that reader; a note marked 'both' matches 'human' and "
            "'agent'. Notes hold context the plan does not: warehouse gotchas, query "
            "recipes, conventions. Requires a tk_r_ or tk_w_ tripl API key."
        ),
    )(list_docs)
    mcp.tool(
        name="read_doc",
        annotations=READ_ONLY,
        description=(
            "Read one note of the docs catalog by scope and path (e.g. "
            "'guides/warehouse.md'). Returns the raw Markdown 'content' with its "
            "frontmatter, the parsed title/description/tags/audience, "
            "'extra_frontmatter' for any other keys, the current 'revision', and "
            "'links': every [[event:NAME]], [[event-type:NAME]] and [[field:NAME]] in "
            "the note with its status on the main plan (resolved, ambiguous or broken). "
            "Requires a tk_r_ or tk_w_ tripl API key."
        ),
    )(read_doc)
    mcp.tool(
        name="search_docs",
        annotations=READ_ONLY,
        description=(
            "Search the docs catalog's notes with a phrase, over both scopes unless "
            "'scope' narrows it. Hits carry scope, path, title, a snippet and a "
            "confidence in [0,1]; read the note with read_doc before relying on it. "
            "Not paged: 'truncated' says ranked hits were dropped, so raise 'limit' "
            "(at most 50). search_plan with types=['doc'] finds the same notes "
            "alongside plan entities. Requires a tk_r_ or tk_w_ tripl API key."
        ),
    )(search_docs)
    mcp.tool(
        name="write_doc",
        annotations=WRITE_REPLACE,
        description=(
            "Create a note, or replace one note's whole content, at a scope and path "
            "ending in '.md'. 'content' is the full raw Markdown, optionally starting "
            "with YAML frontmatter (title, description, tags, audience: "
            "human|agent|both). To edit an existing note, read_doc it first and pass its "
            "'revision' as 'base_revision': if someone changed it since, the server "
            "answers 409 instead of overwriting their edit. Link plan entities as "
            "[[event:NAME]], [[event-type:NAME]] or [[field:EVENT_TYPE/NAME]]; links "
            "that do not resolve come back as warnings, and the note is saved anyway. "
            "Notes are not branch-aware: the write is live at once. Requires a tk_w_ "
            "tripl API key backed by an editor or owner user."
        ),
    )(write_doc)
