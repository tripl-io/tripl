"""Event catalog tools: list, fetch, create, update."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from tripl_cli.api import events, page_items, page_total, send

from tripl_mcp.enums import EventOrderBy, EventStatus, as_strings
from tripl_mcp.runtime import client_for, ensure_branch_not_main, require_branch_id
from tripl_mcp.tools._common import (
    EVENT_LIST_FIELDS,
    READ_ONLY,
    WRITE,
    WRITE_UPDATE,
    trim,
    with_mutation_warnings,
)


async def list_events(
    slug: str,
    ctx: Context,  # type: ignore[type-arg]
    search: str | None = None,
    status: list[EventStatus] | None = None,
    tag: str | None = None,
    field_value: str | None = None,
    meta_value: str | None = None,
    event_type_id: str | None = None,
    silent_since_days: int | None = None,
    reviewed: bool | None = None,
    has_open_questions: bool | None = None,
    property: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
    order_by: EventOrderBy | None = None,
    branch_id: str | None = None,
) -> dict[str, Any]:
    client = client_for(ctx)
    data = await send(
        client,
        events.list_events(
            slug,
            search=search,
            # Widened because the shared builder takes an invariant `list[str]`;
            # the scalar `order_by` needs no such call, since a Literal member
            # already IS a str.
            status=as_strings(status),
            tag=tag,
            field_value=field_value,
            meta_value=meta_value,
            event_type_id=event_type_id,
            silent_since_days=silent_since_days,
            reviewed=reviewed,
            has_open_questions=has_open_questions,
            property=property,
            offset=offset,
            limit=limit,
            order_by=order_by,
            branch=branch_id,
        ),
    )
    # The TRIM is this consumer's context budget and stays here — `tripl events
    # list --json` carries the same rows verbatim, because a pipe pays no
    # per-token cost. The ENVELOPE is not: `{items, total}` is what the route
    # answers, so it is read through the shared layer both surfaces share.
    return {
        "items": [trim(item, EVENT_LIST_FIELDS) for item in page_items(data)],
        "total": page_total(data),
        "note": "Items are trimmed; use get_event for field/meta values and full detail.",
    }


async def get_event(
    slug: str,
    event_id: str,
    ctx: Context,  # type: ignore[type-arg]
    branch_id: str | None = None,
) -> Any:
    client = client_for(ctx)
    return await send(client, events.get_event(slug, event_id, branch=branch_id))


async def get_event_properties(
    slug: str,
    event_id: str,
    ctx: Context,  # type: ignore[type-arg]
    branch_id: str | None = None,
) -> Any:
    client = client_for(ctx)
    return await send(client, events.get_event_properties(slug, event_id, branch=branch_id))


async def create_event(
    slug: str,
    branch_id: str | None,
    event_type_id: str,
    name: str,
    ctx: Context,  # type: ignore[type-arg]
    description: str | None = None,
    title: str | None = None,
    status: EventStatus | None = None,
    tags: list[str] | None = None,
    field_values: list[dict[str, Any]] | None = None,
    meta_values: list[dict[str, Any]] | None = None,
) -> Any:
    require_branch_id(branch_id)
    client = client_for(ctx)
    await ensure_branch_not_main(client, slug, branch_id)
    body: dict[str, Any] = {"event_type_id": event_type_id, "name": name}
    for key, value in (
        ("description", description),
        ("title", title),
        ("status", status),
        ("tags", tags),
        ("field_values", field_values),
        ("meta_values", meta_values),
    ):
        if value is not None:
            body[key] = value
    data = await send(client, events.create_event(slug, body, branch=branch_id))
    return with_mutation_warnings(data)


async def update_event(
    slug: str,
    event_id: str,
    branch_id: str | None,
    patch: dict[str, Any],
    ctx: Context,  # type: ignore[type-arg]
) -> Any:
    require_branch_id(branch_id)
    client = client_for(ctx)
    await ensure_branch_not_main(client, slug, branch_id)
    data = await send(client, events.update_event(slug, event_id, patch, branch=branch_id))
    return with_mutation_warnings(data)


def register(mcp: FastMCP) -> None:
    mcp.tool(
        name="list_events",
        annotations=READ_ONLY,
        description=(
            "List catalog events with structured filters (substring 'search' over "
            "name/description, repeatable lifecycle 'status' values draft/in_review/"
            "ready_for_dev/implemented/live/deprecated/archived, case-insensitive "
            "'tag', "
            "substring 'field_value' e.g. a screen name, substring 'meta_value' "
            "e.g. a ticket key, 'event_type_id', 'silent_since_days', "
            "'reviewed' true/false, 'has_open_questions' true/false, 'property' "
            "- a property name or id, keeps events whose property list carries it). "
            "'reviewed' is an axis of its own, not a "
            "spelling of 'status' - an event can be reviewed and still sit at "
            "in_review - and omitting it means either. "
            "'has_open_questions' keeps events whose discussion still has an "
            "unanswered thread (a lapsed snooze counts as unanswered); false "
            "keeps those with nothing open, and omitting it means either. "
            "'order_by' is 'catalog' (the authored order, and what omitting it "
            "gets) or 'volume' (busiest-first by ingested volume over the last "
            "24h) or 'health' (least healthy first by the health score, main "
            "plan only) - use 'volume' to triage a large catalog by traffic. "
            "Returns trimmed items + total; follow up with get_event for full detail. "
            "Requires a tk_r_ or tk_w_ key."
        ),
    )(list_events)
    mcp.tool(
        name="get_event",
        annotations=READ_ONLY,
        description=(
            "Fetch one event's full canonical detail by id: field values, meta values, "
            "tags, lifecycle state, event type brief, and property value contexts. "
            "Use after search_plan/list_events; treat the returned name/id as "
            "canonical. Requires a tk_r_ or tk_w_ key."
        ),
    )(get_event)
    mcp.tool(
        name="get_event_properties",
        annotations=READ_ONLY,
        description=(
            "Fetch one event's typed property list: each property the event carries "
            "with its variable_type, json_schema, whether it is required, the "
            "effective allowed values for this event, the measured presence_rate and "
            "suggested_required. Use it to build or check an event's properties "
            "payload. Requires a tk_r_ or tk_w_ key."
        ),
    )(get_event_properties)
    mcp.tool(
        name="create_event",
        annotations=WRITE,
        description=(
            "WRITE: create an event (needs a tk_w_ key backed by an editor/owner). "
            "branch_id is REQUIRED so the draft lands on a working branch, never on "
            "the live main plan by accident (list_branches to find one whose status "
            "is draft, ready_for_review, changes_requested or approved; a merged or "
            "closed branch is read-only and answers 409). 'event_type_id' and every "
            "'meta_field_definition_id' must come from a listing read with the SAME "
            "branch_id you are posting to — a branch holds its own copies under new "
            "ids, and an id from another branch answers 422. When a scan "
            "naming rule governs the event type the server derives the name from field "
            "values and may ignore yours with a warning — adopt the returned name/id. "
            "Put the human-readable label in 'title', never in 'name': 'name' is the "
            "identity a scan matches on. "
            "Tags are stored lower-cased, trimmed and de-duplicated; a tag over 100 "
            "characters is a 422. Adopt the tags from the response before filtering "
            "on them. Each meta value is capped at 2000 bytes AS STORED (a "
            "link-templated field stores only the part the template wraps); over "
            "that is also a 422. "
            "A 409 names the event already holding that derived name; the identity is "
            "a database unique key, so the 409 is final even under concurrent creates "
            "(the REST batch form prefixes it 'Event N of M: ') — open that event, do "
            "not retry. Prefer updating an existing event over creating a near-duplicate."
        ),
    )(create_event)
    mcp.tool(
        name="update_event",
        annotations=WRITE_UPDATE,
        description=(
            "WRITE: partially update an event (needs a tk_w_ key backed by an "
            "editor/owner). branch_id is REQUIRED to avoid accidental edits to the "
            "live main plan, and must name an open working branch: a merged or closed "
            "one is read-only and answers 409. "
            "'patch' takes any subset of: name, title, description, status, "
            "sunset_at, owner_id, reviewed, metric_breakdown_columns, tags, "
            "field_values, meta_values. WARNING: 'field_values' and 'meta_values' "
            "are FULL-LIST REPLACEMENTS — sending a partial list deletes the missing "
            "entries. Every 'meta_field_definition_id' in a patch must come from a "
            "listing read with the SAME branch_id the event lives on — a branch holds "
            "its own copies under new ids, and an id from another branch answers 422. "
            "Because 'meta_values' is a full-list replacement you cannot work around "
            "that 422 by dropping the offending entry without losing the event's other "
            "meta values. Each meta value is capped at 2000 bytes AS STORED (a "
            "link-templated field stores only the part the template wraps); over that "
            "is also a 422. Tags are stored lower-cased, trimmed and de-duplicated; a "
            "tag over 100 characters is a 422. Adopt the tags from the response before "
            "filtering on them. "
            "For narrow edits patch only description/name/tags/state. Read "
            "response warnings and adopt the server-canonical name/id."
        ),
    )(update_event)
