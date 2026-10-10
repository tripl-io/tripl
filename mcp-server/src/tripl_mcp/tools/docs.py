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

Every tool sees exactly what the key's user sees (F24): a note that is private,
or shared only with other people or groups, is absent from lists and searches
and answers 404 when read, and ``my_permission`` says whether the user may edit
a note it does see. One exception: a key of an organization owner or admin may
still read such a note directly by path — an audited break-glass read, flagged
``break_glass: true`` in the answer, never listed and never editable. Sharing
is changed in the app, not here.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from tripl_cli.api import docs, page_items, page_total, search, send

from tripl_mcp.enums import DocAudience, DocScope
from tripl_mcp.runtime import client_for
from tripl_mcp.tools._common import (
    DOC_LIST_FIELDS,
    DOC_READ_FIELDS,
    DOC_WRITE_FIELDS,
    READ_ONLY,
    WRITE_REPLACE,
    hoist_warnings,
    trim,
)


async def list_docs(
    slug: str,
    ctx: Context,
    scope: DocScope | None = None,
    audience: DocAudience | None = None,
) -> dict[str, Any]:
    client = client_for(ctx)
    data = await send(client, docs.list_docs(slug))
    rows = docs.filter_by_audience(docs.tree_docs(data, scope), audience)
    return {
        "items": [trim(row, DOC_LIST_FIELDS) for row in rows],
        "total": len(rows),
        "note": "Items carry no content; use read_doc with the item's scope and path.",
    }


async def read_doc(
    slug: str,
    scope: DocScope,
    path: str,
    ctx: Context,
    lang: str | None = None,
) -> Any:
    client = client_for(ctx)
    return trim(await send(client, docs.read_doc(slug, scope, path, lang)), DOC_READ_FIELDS)


async def search_docs(
    slug: str,
    q: str,
    ctx: Context,
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
    ctx: Context,
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
    return hoist_warnings(
        data,
        "The note was saved, but these [[links]] do not resolve to exactly one "
        "entity. Check the names with search_plan (a broken link's 'suggestions' "
        "in 'links' are the closest current names) and write the note again with "
        "base_revision set to the revision below.",
    )


def register(mcp: MCPServer) -> None:
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
            "recipes, conventions. Only notes the key's user may see are listed "
            "(visibility: private, restricted or level); 'my_permission' says whether "
            "they may edit one. Requires a tk_r_ or tk_w_ tripl API key."
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
            "'links': every [[kind:target]] link in the note with its status "
            "(resolved, ambiguous, broken, or unavailable for a note the user cannot "
            "see), its current 'label' and, for a broken link, up to three relink "
            "'suggestions'. "
            "A note hidden from the key's user answers 404, except for an "
            "organization owner or admin: they may still read it, the read is "
            "recorded in the audit log, and the answer carries break_glass=true "
            "(do not treat such a note as shared with the user; it stays read-only). "
            "Notes may carry stored translations ('translations': lang, status, "
            "outdated). Without 'lang' you get the project's agent default language "
            "when that translation is up to date, else the original; 'lang' in the "
            "answer is what you got (null: the original) and 'translation_fallback' "
            "says why you got the original instead. Pass lang='en' (any listed code) "
            "for that translation even when it is behind the original "
            "('translation_outdated'), or lang='original'. Write the original with "
            "write_doc, never a translation's text. "
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
            "alongside plan entities. Notes hidden from the key's user are never "
            "matched or counted. Requires a tk_r_ or tk_w_ tripl API key."
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
            "answers 409 instead of overwriting their edit. Link by name as "
            "[[event:NAME]], [[event-type:NAME]], [[field:EVENT_TYPE/NAME]], "
            "[[variable:NAME]], [[metric:NAME]], [[branch:NAME]], [[scan:NAME]] or "
            "[[data-source:NAME]]; by id as [[doc:NOTE_ID]] (optionally "
            "[[doc:NOTE_ID#heading]]; a [[doc:path/to/note.md]] is saved as the id "
            "form when the note exists and you can read it), [[alert-rule:RULE_ID]] "
            "or [[user:USER_ID]] (an @mention: it notifies that person if they can "
            "read the note and are a member of the project). Any "
            "link takes a label: [[metric:revenue|Revenue]]. Links that do not "
            "resolve come back as warnings, and the note is saved anyway. "
            "Notes are not branch-aware: the write is live at once. Requires a tk_w_ "
            "tripl API key backed by an editor or owner user."
        ),
    )(write_doc)
