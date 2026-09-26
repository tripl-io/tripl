"""Thin async HTTP client for the tripl REST API (`/api/v1`).

Maps tripl's auth/validation failures to typed exceptions the caller can render
however it needs. Pure pass-through otherwise — no re-modeling of API responses.

Shared by two distributions: the ``tripl`` CLI it ships in, and ``tripl-mcp``,
which imports it from here rather than carrying a copy (tripl-ey6j.1). Nothing
consumer-specific belongs in this module — no MCP vocabulary, no CLI flag names,
no ``print``. Errors carry structure (``status_code``, ``base_url``) and each
consumer words its own guidance around them.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from tripl_cli import __version__
from tripl_cli.errors import TriplAPIError, TriplConnectionError

API_PREFIX = "/api/v1"
DEFAULT_TIMEOUT_SECONDS = 30.0
# What this distribution calls itself on the wire. The backend does not read
# User-Agent, but it is the only thing an operator reading access logs has to
# tell CLI traffic from agent traffic — so it is a PARAMETER, not a constant,
# and tripl-mcp passes its own (tripl-ey6j.1).
DEFAULT_USER_AGENT = f"tripl/{__version__}"


def create_http_client(
    base_url: str,
    api_key: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> httpx.AsyncClient:
    """Create one configured transport client.

    The caller may own one client for a whole session (a CLI command that fires
    many requests, an MCP server lifespan) or let ``TriplClient`` create one per
    request.
    """
    return httpx.AsyncClient(
        base_url=base_url.rstrip("/") + API_PREFIX,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": user_agent,
        },
        timeout=timeout,
        follow_redirects=True,
    )


def _detail_of(response: httpx.Response) -> Any:
    """Best-effort extraction of FastAPI's ``detail`` from an error body."""
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return response.text[:500]
    if isinstance(body, dict) and "detail" in body:
        return body["detail"]
    return body


def _as_text(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    return json.dumps(detail, ensure_ascii=False)


def raise_for_status(response: httpx.Response) -> None:
    """Translate tripl API error responses into ``TriplAPIError``."""
    status = response.status_code
    if status < 400:
        return
    detail = _detail_of(response)
    if status == 401:
        # No "check TRIPL_API_KEY" here, unlike the pre-extraction version: stdio
        # tripl-mcp reads that env var, streamable-http tripl-mcp forwards a
        # per-request Bearer header, and the CLI also takes --api-key. Only the
        # consumer knows which applies, so only the consumer names it.
        raise TriplAPIError(
            status,
            detail,
            f"Invalid or expired API key (401). API detail: {_as_text(detail)}",
        )
    if status == 403:
        raise TriplAPIError(
            status,
            detail,
            "Forbidden (403): the API key lacks the required scope (tk_r_ keys "
            "cannot write), is scoped to a different project, or the backing user "
            f"role is insufficient. API detail: {_as_text(detail)}",
        )
    if status == 404:
        raise TriplAPIError(status, detail, f"Not found (404): {_as_text(detail)}")
    if status in (409, 422):
        # Carry the API's JSON detail verbatim so the caller sees the API's own
        # validation output instead of a paraphrase of it.
        raise TriplAPIError(
            status, detail, f"tripl API rejected the request ({status}): {_as_text(detail)}"
        )
    raise TriplAPIError(status, detail, f"tripl API error ({status}): {_as_text(detail)}")


class TriplClient:
    """REST wrapper that can borrow a caller-owned HTTP connection pool."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        http_client: httpx.AsyncClient | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._root_url = base_url.rstrip("/")
        self._base_url = self._root_url + API_PREFIX
        self._api_key = api_key
        self._timeout = timeout
        self._http_client = http_client
        self._user_agent = user_agent
        # The status of the last response this client received, or None before
        # the first. Only `tripl annotate` reads it: the annotation route answers
        # 201 for a new row and 200 for a de-duplicated one, with the same body
        # shape, so the code is the whole difference. Every other caller keeps
        # reading the body alone.
        self.last_status_code: int | None = None

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any],
        json_body: Any | None,
    ) -> httpx.Response:
        if self._http_client is not None:
            return await self._http_client.request(method, path, params=params, json=json_body)
        async with create_http_client(
            base_url=self._root_url,
            api_key=self._api_key,
            timeout=self._timeout,
            # Drift trap: this is the SECOND place a transport gets built (the
            # first being the caller's own create_http_client). tripl-mcp's
            # streamable-http mode always lands here with http_client=None, so
            # not forwarding the UA would make half its traffic report the CLI's
            # default in the operator's access logs.
            user_agent=self._user_agent,
        ) as client:
            return await client.request(method, path, params=params, json=json_body)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> Any:
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            response = await self._send(method, path, params=clean_params, json_body=json_body)
        except httpx.HTTPError as exc:
            raise TriplConnectionError(self._base_url, exc) from exc
        self.last_status_code = response.status_code
        raise_for_status(response)
        if response.status_code == 204 or not response.content:
            return {"status": "ok"}
        try:
            return response.json()
        except ValueError as exc:
            # A 2xx whose body is not JSON. Reachable in the field: an SSO gateway
            # or captive portal that intercepts the request answers 200 with an
            # HTML login page. json.JSONDecodeError subclasses ValueError, NOT
            # httpx.HTTPError, so without this it escapes every consumer's error
            # handling — `tripl doctor` died with a traceback instead of emitting
            # the report it promises always to emit, and tripl-mcp would have
            # surfaced it as an unhandled tool crash.
            raise TriplAPIError(
                response.status_code,
                None,
                f"tripl API returned {response.status_code} but the body is not JSON "
                f"({exc}). Is {self._base_url} really a tripl instance, or is a proxy "
                "or login page answering for it?",
            ) from exc

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> Any:
        return await self.request("POST", path, params=params, json_body=json_body)

    async def patch(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> Any:
        return await self.request("PATCH", path, params=params, json_body=json_body)
