"""Monitoring tools: monitors summary, anomaly signals, reconciliation health.

Both bodies here are two- and three-call merges, and both stay.
The CLI reaches three of these five routes and asks a different question of each:
``doctor`` reads ``coverage`` and ``signals`` to reach a VERDICT, through the
``Fetched`` regime where a 403 is data rather than an exception, and ``watch``
polls ``signals`` expanded because a collapsed list drops the event-scope
anomalies a follow mode exists for. Neither wants a count-plus-sample digest,
and no CLI command touches ``dead-events`` or ``shadow-events`` at all. These
shapes have one consumer, and it is the one that pays for every token.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from tripl_cli.api import monitoring, send

from tripl_mcp.runtime import client_for
from tripl_mcp.tools._common import READ_ONLY, summarize_collection


async def monitors_summary(
    slug: str,
    ctx: Context,
) -> dict[str, Any]:
    client = client_for(ctx)
    summary = await send(client, monitoring.get_monitors_summary(slug))
    signals = await send(client, monitoring.list_signals(slug))
    return {
        "summary": summary,
        "top_anomaly_signals": summarize_collection(signals),
    }


async def reconciliation_status(
    slug: str,
    ctx: Context,
) -> dict[str, Any]:
    client = client_for(ctx)
    coverage = await send(client, monitoring.get_coverage(slug))
    dead = await send(client, monitoring.list_dead_events(slug))
    shadow = await send(client, monitoring.list_shadow_events(slug))
    return {
        "coverage": coverage,
        "dead_events": summarize_collection(dead),
        "shadow_events": summarize_collection(shadow),
    }


def register(mcp: MCPServer) -> None:
    mcp.tool(
        name="monitors_summary",
        annotations=READ_ONLY,
        description=(
            "Health overview of the project's monitors plus the top open anomaly "
            "signals (scan-produced; read-only here — resolution actions are not "
            "exposed). Requires a tk_r_ or tk_w_ key."
        ),
    )(monitors_summary)
    mcp.tool(
        name="reconciliation_status",
        annotations=READ_ONLY,
        description=(
            "Plan-vs-warehouse reconciliation health: coverage, dead events "
            "(documented but silent for 'days' days) and shadow events (observed but "
            "undocumented; 'new_count' of them still untriaged), each as count + "
            "sample. Use to ground 'is the plan accurate?' answers. "
            "Requires a tk_r_ or tk_w_ key."
        ),
    )(reconciliation_status)
