"""Project search — the recommended entry point for any catalog question."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from tripl_cli.api import page_items, page_total, search, send

from tripl_mcp.enums import SearchEntityType, as_strings
from tripl_mcp.runtime import client_for
from tripl_mcp.tools._common import READ_ONLY, SEARCH_RESULT_FIELDS, trim


async def search_plan(
    slug: str,
    q: str,
    ctx: Context,  # type: ignore[type-arg]
    types: list[SearchEntityType] | None = None,
    limit: int | None = None,
    branch_id: str | None = None,
) -> dict[str, Any]:
    client = client_for(ctx)
    # A list of Literals so the tool schema enumerates the twelve kinds and a
    # misspelt one is refused before the request, widened for the shared
    # builder's invariant `list[str]` exactly as list_events does.
    data = await send(
        client,
        search.search_plan(slug, q, types=as_strings(types), limit=limit, branch=branch_id),
    )
    # Same split as list_events: the trim is an agent context budget and stays,
    # the envelope belongs to the route. `semantic_used` moved with it — both
    # surfaces publish it and both had their own `.get` for it, and it now reads
    # False rather than null on a body that omits the key, which is what the
    # backend's `bool = False` default actually means.
    return {
        "items": [trim(item, SEARCH_RESULT_FIELDS) for item in page_items(data)],
        "total": page_total(data),
        # `total` on THIS route is the size of the response, not a pre-paging
        # count, so an agent could not tell a complete answer from a clipped one.
        # The route answers it outright, and both surfaces read
        # that one key through this reader — `tripl plan search` falls back to
        # its page-fullness guess only against an instance predating the field.
        # Published as a bool, never null: an envelope key an agent has to test
        # for existence on is a key it will eventually forget to test for.
        "truncated": search.truncated(data),
        "semantic_used": search.semantic_used(data),
    }


def register(mcp: FastMCP) -> None:
    mcp.tool(
        name="search_plan",
        annotations=READ_ONLY,
        description=(
            "Search the tripl tracking plan with a natural-language phrase or partial "
            "event name. Use it to find an entity when you do not know its id; hits "
            "are ranked summaries, so read the entity itself (get_event etc.) before "
            "relying on its details or editing it. 'types' restricts to "
            "entity kinds, spelled the way a hit's entity_type is spelled — plan "
            "content (event, event_type, field, ...) and project configuration "
            "(scan_config, alert_rule) alike; an unfiltered search shows which kinds "
            "this plan holds. Each hit carries a confidence in [0,1]. "
            "Results are the top 'limit' of a ranked list, not a page: 'total' is "
            "how many came back, 'truncated' says ranked hits were dropped, and "
            "there is no offset — raise 'limit', or threshold on confidence. "
            "Requires a tk_r_ or tk_w_ tripl API key."
        ),
    )(search_plan)
