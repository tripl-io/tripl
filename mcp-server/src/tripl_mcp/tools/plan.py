"""Plan structure tools: event types, fields, properties (variables), projects."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from tripl_cli.api import event_types, projects, send, variables

from tripl_mcp.runtime import client_for
from tripl_mcp.tools._common import (
    EVENT_TYPE_LIST_FIELDS,
    FIELD_DEFINITION_FIELDS,
    READ_ONLY,
    trim,
)


async def list_event_types(
    slug: str,
    ctx: Context,  # type: ignore[type-arg]
    branch_id: str | None = None,
) -> Any:
    """List a project's event types on ``branch_id`` (default: main).

    Branch-scoped like every other plan read — without ``branch`` the API
    resolves main, so an agent working on a branch was silently answered with
    main's event types. Field definitions are NOT embedded
    here; call ``get_event_type_fields`` for one type's fields.
    """
    client = client_for(ctx)
    data = await send(client, event_types.list_event_types(slug, branch=branch_id))
    if not isinstance(data, list):
        return data
    return [_event_type_summary(item) for item in data]


def _event_type_summary(item: Any) -> Any:
    """Trim one event type, passing anything unexpected through untouched.

    The TRIM is this consumer's context budget and stays here; the COUNT is a
    derived fact about ``EventTypeResponse`` that ``tripl plan types`` puts in
    its table too, so it is asked of the shared layer rather than spelled again.
    Two spellings of "how many fields does this type have" is how
    the two surfaces come to disagree about a type whose ``field_definitions``
    arrives null.
    """
    if not isinstance(item, dict):
        return item
    return {
        **trim(item, EVENT_TYPE_LIST_FIELDS),
        "field_count": event_types.field_count(item),
    }


async def get_event_type_fields(
    slug: str,
    event_type_id: str,
    ctx: Context,  # type: ignore[type-arg]
    branch_id: str | None = None,
) -> dict[str, Any]:
    """DETAIL + FIELDS, merged into one object with the fields under ``fields``.

    Stays here rather than moving to ``tripl_cli.api``. ``tripl plan
    fields`` looks like a counterpart and is not: it reads the LIST route, to
    resolve a name the operator typed into an id, then the fields — and emits the
    fields alone under ``items``, the key its six sibling verbs use. Different
    second request, different result shape, so this merge still has exactly one
    caller. Moving it would freeze an agent-shaped envelope in a package whose
    tests cannot see it, which is the trade tripl-ey6j.5 declined.
    """
    client = client_for(ctx)
    event_type = await send(
        client, event_types.get_event_type(slug, event_type_id, branch=branch_id)
    )
    fields = await send(client, event_types.get_fields(slug, event_type_id, branch=branch_id))
    merged = (
        dict(trim(event_type, EVENT_TYPE_LIST_FIELDS))
        if isinstance(event_type, dict)
        else {"event_type": event_type}
    )
    merged["fields"] = (
        [trim(field, FIELD_DEFINITION_FIELDS) for field in fields]
        if isinstance(fields, list)
        else fields
    )
    return merged


async def list_variables(
    slug: str,
    ctx: Context,  # type: ignore[type-arg]
    branch_id: str | None = None,
    offset: int = 0,
    limit: int = 200,
) -> Any:
    """List a project's variables.

    The endpoint is paged and answers ``{"items": [...], "total": n}``; pass
    ``offset``/``limit`` to walk a catalog larger than one page (a real project
    can carry well over a thousand variables).
    """
    client = client_for(ctx)
    return await send(
        client, variables.list_variables(slug, branch=branch_id, offset=offset, limit=limit)
    )


async def get_variable_values(
    slug: str,
    variable_id: str,
    ctx: Context,  # type: ignore[type-arg]
    branch_id: str | None = None,
) -> dict[str, Any]:
    """VALUES + EVENT_OVERRIDES, because an override REPLACES the global list.

    Stays here. ``tripl plan variables`` lists the catalog and stops
    there — no CLI verb reads one variable's observed values — so this merge has
    the one caller it had when tripl-ey6j.5 left it behind. It moves the day a
    ``tripl plan values <variable>`` exists, not before.
    """
    client = client_for(ctx)
    values = await send(client, variables.get_values(slug, variable_id, branch=branch_id))
    overrides = await send(
        client, variables.get_event_overrides(slug, variable_id, branch=branch_id)
    )
    return {"values": values, "event_overrides": overrides}


async def list_projects(
    ctx: Context,  # type: ignore[type-arg]
) -> Any:
    client = client_for(ctx)
    return await send(client, projects.list_projects())


def register(mcp: FastMCP) -> None:
    mcp.tool(
        name="list_event_types",
        annotations=READ_ONLY,
        description=(
            "List the project's event types (schemas events belong to) with a "
            "field_count each — field definitions are NOT embedded; call "
            "get_event_type_fields for one type's fields. Use before creating events "
            "to pick the right event_type_id. Pass branch_id to read a plan branch "
            "instead of main. Requires a tk_r_ or tk_w_ key."
        ),
    )(list_event_types)
    mcp.tool(
        name="get_event_type_fields",
        annotations=READ_ONLY,
        description=(
            "Fetch one event type merged with its field definitions (name, "
            "display_name, field_type, is_required, enum_options, sensitivity) under "
            "a 'fields' key. Consult this before writing field_values so payloads "
            "validate. Pass branch_id to read a plan branch instead of main. "
            "Requires a tk_r_ or tk_w_ key."
        ),
    )(get_event_type_fields)
    mcp.tool(
        name="list_variables",
        annotations=READ_ONLY,
        description=(
            "List the project's event properties (documented ${name} placeholders; the "
            "API's former name for them is 'variables') with "
            "allowed_values, bindings, usage summaries and open drift counts. Paged: "
            "answers {items, total}; pass offset/limit to reach beyond the first "
            "page. Requires a tk_r_ or tk_w_ key."
        ),
    )(list_variables)
    mcp.tool(
        name="get_variable_values",
        annotations=READ_ONLY,
        description=(
            "Fetch one property's observed per-event value contexts plus its "
            "event-level overrides (overrides replace the global documented list for "
            "their event). High-cardinality contexts return bounded samples with an "
            "observed count. Requires a tk_r_ or tk_w_ key."
        ),
    )(get_variable_values)
    mcp.tool(
        name="list_projects",
        annotations=READ_ONLY,
        description=(
            "List projects visible to the API key: [{slug, name, ...}]. NOTE: a "
            "project-scoped key (bound to one project) gets 403 here BY DESIGN — "
            "that is expected, not an error; use the project slug you were given "
            "instead. Requires a tk_r_ or tk_w_ key without project scoping."
        ),
    )(list_projects)
