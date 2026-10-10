"""One REST call, described rather than performed.

A VALUE and not a call, because the two consumers need different things from
the same request: the CLI wraps a doctor/watch read into ``Fetched`` so a 403 is
data, tripl-mcp wants the exception so ``errors.py`` can reword it for an agent,
and ``--dry-run`` has to print exactly what it would have sent without sending
it. A function that both called and executed could serve only one of the three.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tripl_cli.client import TriplClient


@dataclass(frozen=True)
class ApiRequest:
    """Method, path, query and body — everything except the credential.

    ``path`` is concrete (already formatted) and relative to ``/api/v1``, which
    is where ``TriplClient`` starts. Frozen but NOT hashable: ``params`` is a
    dict, so this must never be used as a mapping key.

    A ``None`` member of ``params`` is dropped by ``TriplClient.request``, which
    is how a builder expresses "leave this parameter off entirely" and let the
    server apply its own default.
    """

    method: str  # "GET" | "POST" | "PATCH" | "PUT"
    path: str
    params: dict[str, Any] | None = None
    json_body: Any | None = None


async def send(client: TriplClient, request: ApiRequest) -> Any:
    """Execute one ``ApiRequest``. Raises the client's typed errors.

    ``client.request`` rather than ``client.get``/``post``: tripl-mcp's
    ``runtime._McpTriplClient`` overrides exactly that method to translate
    failures into ``ToolError``, so routing through it is what keeps the MCP's
    error surface unchanged by this indirection.
    """
    return await client.request(
        request.method,
        request.path,
        params=request.params,
        json_body=request.json_body,
    )
