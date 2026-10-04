"""Process-wide runtime configuration and per-request credential resolution.

The server runs in one of two modes:

- ``stdio`` (default): one operator, credentials come from the environment
  (``TRIPL_BASE_URL`` + ``TRIPL_API_KEY``) at startup.
- ``streamable-http``: multi-client; every incoming MCP request must carry its
  own ``Authorization: Bearer tk_...`` header, which is forwarded verbatim to
  the tripl API and never stored server-side.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server.fastmcp.exceptions import ToolError
from tripl_cli.api import branches, send
from tripl_cli.client import TriplClient
from tripl_cli.errors import TriplError

from tripl_mcp import USER_AGENT
from tripl_mcp.errors import to_tool_error

ALLOW_MAIN_ENV = "TRIPL_MCP_ALLOW_MAIN"

TRANSPORT_STDIO = "stdio"
TRANSPORT_STREAMABLE_HTTP = "streamable-http"


@dataclass(frozen=True)
class Runtime:
    """Immutable server-level configuration resolved at startup."""

    base_url: str
    transport: str
    api_key: str | None = None


@dataclass(frozen=True)
class RuntimeLifespan:
    """Resources owned by one FastMCP server lifespan."""

    stdio_http_client: httpx.AsyncClient | None = None


_runtime: Runtime | None = None


def configure(runtime: Runtime) -> None:
    global _runtime
    _runtime = runtime


def get_runtime() -> Runtime:
    if _runtime is None:
        raise ToolError(
            "tripl-mcp is not configured. Set TRIPL_BASE_URL (and TRIPL_API_KEY "
            "for stdio transport) and restart the server."
        )
    return _runtime


def extract_bearer_token(headers: Mapping[str, str]) -> str | None:
    """Pull the raw token out of an ``Authorization: Bearer ...`` header."""
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    token = auth.removeprefix("Bearer ").strip()
    return token or None


def resolve_api_key(ctx: Any) -> str:
    """Resolve the tripl API key for the current tool invocation.

    stdio mode reads the key configured from the environment at startup.
    streamable-http mode extracts ``Authorization: Bearer`` from the incoming
    MCP HTTP request and forwards it verbatim; it is never persisted.
    """
    runtime = get_runtime()
    if runtime.transport == TRANSPORT_STREAMABLE_HTTP:
        request = getattr(getattr(ctx, "request_context", None), "request", None)
        headers = getattr(request, "headers", None)
        token = extract_bearer_token(headers) if headers is not None else None
        if token is None:
            raise ToolError(
                "Missing credentials: this tripl-mcp server runs over streamable-http "
                "and requires each MCP request to carry an 'Authorization: Bearer "
                "tk_...' header with a tripl API key. Configure your MCP client to "
                "send it."
            )
        return token
    if not runtime.api_key:
        raise ToolError(
            "Missing credentials: set the TRIPL_API_KEY environment variable "
            "to a tripl API key (tk_r_... for read-only, tk_w_... for write)."
        )
    return runtime.api_key


class _McpTriplClient(TriplClient):
    """TriplClient whose failures reach the agent as ToolError, MCP-worded.

    The one place the shared client's neutral errors are translated. ``get``,
    ``post`` and ``patch`` all funnel through ``request``, so overriding it
    covers every call — including ``ensure_branch_not_main``'s own lookup below,
    which is not routed through any tools/ module.
    """

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> Any:
        try:
            return await super().request(method, path, params=params, json_body=json_body)
        except TriplError as exc:
            raise to_tool_error(exc) from exc


def client_for(ctx: Any) -> TriplClient:
    """Build a REST wrapper around the transport-appropriate HTTP client."""
    runtime = get_runtime()
    api_key = resolve_api_key(ctx)
    http_client: httpx.AsyncClient | None = None
    if runtime.transport == TRANSPORT_STDIO:
        try:
            lifespan = ctx.request_context.lifespan_context
        except (AttributeError, ValueError) as exc:
            raise ToolError(
                "The stdio HTTP client is unavailable outside the FastMCP server lifespan."
            ) from exc
        http_client = getattr(lifespan, "stdio_http_client", None)
        if not isinstance(http_client, httpx.AsyncClient):
            raise ToolError("The stdio HTTP client was not initialized by the server lifespan.")
    return _McpTriplClient(
        base_url=runtime.base_url,
        api_key=api_key,
        http_client=http_client,
        # http mode leaves http_client=None, so this is the value TriplClient
        # puts on the per-request transport it builds itself.
        user_agent=USER_AGENT,
    )


def is_main_write_allowed() -> bool:
    return os.environ.get(ALLOW_MAIN_ENV, "").strip() in {"1", "true", "yes"}


def require_branch_id(branch_id: str | None) -> None:
    """Refuse branchless plan mutations unless the operator opted in.

    Encodes the agent-guide rule: never mutate the main plan by accident.
    """
    if branch_id:
        return
    if is_main_write_allowed():
        return
    raise ToolError(
        "branch_id is required for plan mutations: without it the write would land "
        "on the project's LIVE main plan. List branches with list_branches and pass "
        "a working branch id. If editing main is truly intended, the server operator "
        f"must set {ALLOW_MAIN_ENV}=1 in the tripl-mcp environment."
    )


async def ensure_branch_not_main(client: TriplClient, slug: str, branch_id: str | None) -> None:
    """Refuse plan mutations that target the main branch via its explicit id.

    ``require_branch_id`` catches the *missing* branch_id path; this closes the
    second path onto the live plan: the backend accepts the id of the
    ``kind="main"`` branch that ``list_branches`` returns and treats it exactly
    as no branch at all, so passing main's id would silently write to the live
    main plan. That holds although main is stored with ``status="merged"``:
    the backend refuses writes naming a merged or closed WORKING branch with a
    409, which reaches the agent as a ToolError, but splits main off before
    that check. Costs one branch lookup per write; skipped entirely when the
    operator opted in via the env override.
    """
    if not branch_id or is_main_write_allowed():
        return
    branch = await send(client, branches.get_branch(slug, branch_id))
    if isinstance(branch, dict) and branch.get("kind") == "main":
        raise ToolError(
            "branch_id resolves to the project's MAIN branch: this write would land "
            "on the LIVE main plan. Pass a working branch id instead (see "
            "list_branches). If editing main is truly intended, the server operator "
            f"must set {ALLOW_MAIN_ENV}=1 in the tripl-mcp environment."
        )
