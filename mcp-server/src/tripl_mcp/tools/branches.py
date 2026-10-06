"""Plan branch tools: discovery and diff review (merge/revert are not exposed)."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from tripl_cli.api import branches, send

from tripl_mcp.runtime import client_for
from tripl_mcp.tools._common import READ_ONLY


async def list_branches(
    slug: str,
    ctx: Context,
) -> Any:
    client = client_for(ctx)
    return await send(client, branches.list_branches(slug))


async def get_branch_diff(
    slug: str,
    branch_id: str,
    ctx: Context,
) -> Any:
    client = client_for(ctx)
    return await send(client, branches.get_diff(slug, branch_id))


def register(mcp: MCPServer) -> None:
    mcp.tool(
        name="list_branches",
        annotations=READ_ONLY,
        description=(
            "List the project's plan branches: id, name, kind, status. Use the id of "
            "a working branch whose status is draft, ready_for_review, "
            "changes_requested or approved as branch_id on write tools so edits never "
            "land on the live main plan by accident. A merged branch is read-only, "
            "and a closed one is until a human reopens it: a write naming either "
            "answers 409. Requires a tk_r_ or tk_w_ key."
        ),
    )(list_branches)
    mcp.tool(
        name="get_branch_diff",
        annotations=READ_ONLY,
        description=(
            "Review what a working branch changed versus its base: one entry per "
            "changed entity with entity_type, kind (added/changed/removed), name, "
            "parent, entity_id and field_changes. Entities are keyed by NAME, so a "
            "rename arrives as a removal beside an addition; the response's "
            "'renames' list (entity_type, parent, removed_name, added_name) names "
            "the pairs the merge will treat as ONE renamed row, so read it before "
            "reporting a deletion. Names are not always unique: two events can share "
            "a type and name, and two relations can link the same two fields. The "
            "diff, the merge and a revert still match such rows by that key, one row "
            "per key, so the diff shows at most one entry for the name and a change "
            "to one of them can show on the other. That entry carries a warning: "
            "rename one of the events, or remove one of the relations, before "
            "changing either. Use to verify your edits before a human merges. "
            "Merging, reverting and branch transitions are NOT exposed "
            "through this MCP server — hand off to a human in the tripl UI. Requires "
            "a tk_r_ or tk_w_ key."
        ),
    )(get_branch_diff)
