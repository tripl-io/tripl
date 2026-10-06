"""tripl MCP server entry point.

Runs the MCP server over stdio (env-configured credentials) or streamable-http
(per-request ``Authorization: Bearer`` pass-through, never stored).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from tripl_cli.client import DEFAULT_TIMEOUT_SECONDS, create_http_client

from tripl_mcp import USER_AGENT, __version__
from tripl_mcp.runtime import (
    TRANSPORT_STDIO,
    TRANSPORT_STREAMABLE_HTTP,
    Runtime,
    RuntimeLifespan,
    configure,
    get_runtime,
)
from tripl_mcp.tools import register_all

INSTRUCTIONS = (
    "Curated tools over a tripl tracking-plan instance. Safe workflow: search_plan "
    "first, then fetch canonical entities by id before deciding anything; draft "
    "writes mentally (dry-run mindset) and prefer updating an existing event over "
    "creating a near-duplicate; pass a working branch_id on every plan write so the "
    "live main plan is never mutated by accident, picking an open branch (status "
    "draft, ready_for_review, changes_requested or approved), since a merged or closed "
    "branch is read-only and answers 409; always read mutation warnings and "
    "adopt the server-canonical names/ids over your proposed ones. Use read_doc/"
    "search_docs for team notes (warehouse gotchas, query recipes, conventions) before "
    "answering a question the plan alone cannot; write_doc needs a tk_w_ key backed by "
    "an editor and is live at once, since notes are not branch-aware. Read tools work "
    "with a tk_r_ key; write tools need a tk_w_ key backed by an editor/owner user."
)


@asynccontextmanager
async def server_lifespan(
    _: MCPServer[RuntimeLifespan],
) -> AsyncIterator[RuntimeLifespan]:
    runtime = get_runtime()
    if runtime.transport != TRANSPORT_STDIO:
        yield RuntimeLifespan()
        return
    if runtime.api_key is None:
        raise RuntimeError("TRIPL_API_KEY is required for the stdio server lifespan")
    # Imported by name rather than called as `client_mod.create_http_client`:
    # tests/test_tools_e2e.py monkeypatches THIS module-level binding to count
    # pool creations, and an attribute call would silently defeat that.
    async with create_http_client(
        runtime.base_url, runtime.api_key, DEFAULT_TIMEOUT_SECONDS, USER_AGENT
    ) as http_client:
        yield RuntimeLifespan(stdio_http_client=http_client)


def build_server() -> MCPServer[RuntimeLifespan]:
    mcp: MCPServer[RuntimeLifespan] = MCPServer(
        name="tripl", instructions=INSTRUCTIONS, lifespan=server_lifespan
    )
    register_all(mcp)
    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tripl-mcp",
        description=f"tripl MCP server {__version__} — httpx client of a running tripl instance",
    )
    parser.add_argument(
        "--transport",
        choices=[TRANSPORT_STDIO, TRANSPORT_STREAMABLE_HTTP],
        default=TRANSPORT_STDIO,
        help="stdio (default, env-configured key) or streamable-http (per-request Bearer)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="streamable-http bind host")
    parser.add_argument("--port", type=int, default=8765, help="streamable-http bind port")
    args = parser.parse_args()

    base_url = os.environ.get("TRIPL_BASE_URL", "").strip()
    if not base_url:
        sys.exit("tripl-mcp: TRIPL_BASE_URL environment variable is required")

    api_key = os.environ.get("TRIPL_API_KEY", "").strip() or None
    if args.transport == TRANSPORT_STDIO and api_key is None:
        sys.exit("tripl-mcp: TRIPL_API_KEY is required for stdio transport")

    configure(
        Runtime(
            base_url=base_url,
            transport=args.transport,
            # http mode never holds a key server-side; each request brings its own.
            api_key=api_key if args.transport == TRANSPORT_STDIO else None,
        )
    )

    mcp = build_server()
    if args.transport == TRANSPORT_STREAMABLE_HTTP:
        # mcp 2 takes the bind address at run(), not on settings.
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
