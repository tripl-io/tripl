"""Tool registration for the tripl MCP server."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from tripl_mcp.tools import branches, docs, events, monitoring, plan, scans, search


def register_all(mcp: FastMCP) -> None:
    search.register(mcp)
    events.register(mcp)
    plan.register(mcp)
    branches.register(mcp)
    scans.register(mcp)
    monitoring.register(mcp)
    docs.register(mcp)
