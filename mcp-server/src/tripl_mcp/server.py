"""tripl MCP server entry point.

Runs the MCP server over stdio (env-configured credentials) or streamable-http
(per-request ``Authorization: Bearer`` pass-through, never stored).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from tripl_cli.client import DEFAULT_TIMEOUT_SECONDS, create_http_client
from tripl_cli.config import normalize_base_url
from tripl_cli.errors import TriplConfigError

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


def runtime_from_env(transport: str) -> Runtime:
    """Read TRIPL_BASE_URL and TRIPL_API_KEY for ``transport``, or exit saying why.

    The URL goes through the CLI's own ``normalize_base_url``, so the two read it
    identically and ``tripl doctor`` proves what this server will use: a value
    with no scheme is refused here, with the fix, rather than as a connection
    error on every tool call, and a pasted ``.../api/v1`` is dropped rather than
    turning every route into a 404 under /api/v1/api/v1.
    """
    raw_url = os.environ.get("TRIPL_BASE_URL", "").strip()
    if not raw_url:
        sys.exit("tripl-mcp: TRIPL_BASE_URL environment variable is required")
    try:
        base_url = normalize_base_url(raw_url, "TRIPL_BASE_URL")
    except TriplConfigError as exc:
        sys.exit(f"tripl-mcp: {exc}")

    api_key = os.environ.get("TRIPL_API_KEY", "").strip() or None
    if transport == TRANSPORT_STDIO and api_key is None:
        sys.exit("tripl-mcp: TRIPL_API_KEY is required for stdio transport")

    return Runtime(
        base_url=base_url,
        transport=transport,
        # http mode never holds a key server-side; each request brings its own.
        api_key=api_key if transport == TRANSPORT_STDIO else None,
    )


# What the MCP SDK accepts on its own when bound to loopback (mcp 2,
# lowlevel/server.py streamable_http_app). Spelled out because passing ANY
# TransportSecuritySettings replaces that default instead of extending it, and a
# local health check or client on 127.0.0.1 must keep working when an operator
# adds the public name a reverse proxy forwards.
LOOPBACK_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")
LOOPBACK_ORIGINS = ("http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*")


def transport_security(
    allowed_hosts: Sequence[str], allowed_origins: Sequence[str]
) -> TransportSecuritySettings | None:
    """Host and Origin checking for streamable-http, or None for the SDK's default.

    The SDK's default checks both only on a loopback bind, and accepts only the
    loopback names there - so a reverse proxy that forwards the public Host
    (Caddy and Traefik do by default; nginx does with ``proxy_set_header Host
    $host``) gets ``421 Invalid Host header`` on every request. On any other
    bind it checks nothing. Either flag turns checking on whatever the bind
    address, with the operator's values added to the loopback ones, so on
    ``--host 0.0.0.0`` every public name has to be listed. The checks guard
    against DNS rebinding: a browser page that resolves its own name to this
    server cannot use it, because its Host and Origin are not on the list.
    """
    if not allowed_hosts and not allowed_origins:
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*LOOPBACK_HOSTS, *allowed_hosts],
        allowed_origins=[*LOOPBACK_ORIGINS, *allowed_origins],
    )


def build_parser() -> argparse.ArgumentParser:
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
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "streamable-http bind address (default: 127.0.0.1, reachable from this machine "
            "only). 0.0.0.0 listens on every interface"
        ),
    )
    parser.add_argument("--port", type=int, default=8765, help="streamable-http bind port")
    parser.add_argument(
        "--allowed-host",
        dest="allowed_hosts",
        metavar="HOST",
        action="append",
        default=[],
        help=(
            "streamable-http: accept requests whose Host header is HOST, e.g. "
            "mcp.example.com or mcp.example.com:* for any port. Repeatable. Turns Host and "
            "Origin checking on whatever --host is; the loopback names stay accepted"
        ),
    )
    parser.add_argument(
        "--allowed-origin",
        dest="allowed_origins",
        metavar="ORIGIN",
        action="append",
        default=[],
        help=(
            "streamable-http: accept requests whose Origin header is ORIGIN, e.g. "
            "https://agents.example.com. Repeatable. Only browser clients send one; turns "
            "checking on like --allowed-host"
        ),
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    configure(runtime_from_env(args.transport))

    mcp = build_server()
    if args.transport == TRANSPORT_STREAMABLE_HTTP:
        # mcp 2 takes the bind address at run(), not on settings.
        mcp.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            transport_security=transport_security(args.allowed_hosts, args.allowed_origins),
        )
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
