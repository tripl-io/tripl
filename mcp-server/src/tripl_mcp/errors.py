"""Re-attach tripl-mcp's own guidance to the shared client's typed failures.

The HTTP client moved into the `tripl` distribution, which the
CLI also ships. It therefore cannot name this server's environment variables or
its Bearer-header convention — the CLI resolves --url/--api-key and a config
file, and a message telling a terminal user to "check the Bearer token sent to
this server" is nonsense. So the client raises structured, neutral errors and
this module puts the MCP-shaped wording back on, in the one place that knows it.

Applied at ``runtime._McpTriplClient``, i.e. once, rather than in each of the
six tools/ modules.
"""

from __future__ import annotations

from mcp.server.fastmcp.exceptions import ToolError
from tripl_cli.errors import TriplAPIError, TriplConnectionError, TriplError


def to_tool_error(exc: TriplError) -> ToolError:
    """Wrap a shared-client failure as a ToolError with tripl-mcp's guidance.

    FastMCP re-wraps whatever a tool raises into an ``isError`` result carrying
    ``str(exc)``, so the exception CLASS never reaches the agent — only this
    text does. That is what makes converting the type here cheap and adding the
    hint here worthwhile.
    """
    if isinstance(exc, TriplConnectionError):
        return ToolError(f"{exc}. Check TRIPL_BASE_URL and that the tripl instance is running.")
    if isinstance(exc, TriplAPIError) and exc.status_code == 401:
        # stdio reads TRIPL_API_KEY; streamable-http forwards the caller's
        # Authorization header. Both are live, so both are named.
        return ToolError(f"{exc} Check TRIPL_API_KEY / the Bearer token sent to this server.")
    return ToolError(str(exc))
