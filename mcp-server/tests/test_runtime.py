"""Credential resolution: env keys, HTTP header pass-through, branch gate."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
import pytest
import respx
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.context import RequestContext
from starlette.requests import Request
from tripl_cli.client import TriplClient

from tests.conftest import API_BASE, BASE_URL, STDIO_KEY
from tripl_mcp import __version__
from tripl_mcp import runtime as runtime_module
from tripl_mcp.runtime import (
    ALLOW_MAIN_ENV,
    TRANSPORT_STREAMABLE_HTTP,
    Runtime,
    client_for,
    configure,
    ensure_branch_not_main,
    extract_bearer_token,
    require_branch_id,
    resolve_api_key,
)


@dataclass
class _StubRequest:
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class _StubRequestContext:
    request: Any = None


@dataclass
class _StubContext:
    request_context: Any = None


@pytest.fixture
def http_runtime() -> Iterator[Runtime]:
    previous = runtime_module._runtime
    rt = Runtime(base_url=BASE_URL, transport=TRANSPORT_STREAMABLE_HTTP, api_key=None)
    configure(rt)
    yield rt
    runtime_module._runtime = previous


class TestExtractBearerToken:
    def test_extracts_token_case_insensitive_header(self) -> None:
        assert extract_bearer_token({"authorization": "Bearer tk_r_x"}) == "tk_r_x"
        assert extract_bearer_token({"Authorization": "Bearer tk_w_y"}) == "tk_w_y"

    def test_rejects_non_bearer_and_empty(self) -> None:
        assert extract_bearer_token({}) is None
        assert extract_bearer_token({"authorization": "Basic abc"}) is None
        assert extract_bearer_token({"authorization": "Bearer "}) is None


class TestResolveApiKey:
    def test_stdio_uses_env_configured_key(self, stdio_runtime: Runtime) -> None:
        assert resolve_api_key(_StubContext()) == STDIO_KEY

    def test_stdio_without_key_errors(self) -> None:
        previous = runtime_module._runtime
        configure(Runtime(base_url=BASE_URL, transport="stdio", api_key=None))
        try:
            with pytest.raises(ToolError, match="TRIPL_API_KEY"):
                resolve_api_key(_StubContext())
        finally:
            runtime_module._runtime = previous

    def test_http_forwards_bearer_from_request(self, http_runtime: Runtime) -> None:
        ctx = _StubContext(
            request_context=_StubRequestContext(
                request=_StubRequest(headers={"authorization": "Bearer tk_r_fwd"})
            )
        )

        assert resolve_api_key(ctx) == "tk_r_fwd"

    def test_http_without_header_gets_clear_error(self, http_runtime: Runtime) -> None:
        ctx = _StubContext(request_context=_StubRequestContext(request=_StubRequest()))

        with pytest.raises(ToolError, match="Authorization: Bearer"):
            resolve_api_key(ctx)

    def test_http_without_request_object_gets_clear_error(self, http_runtime: Runtime) -> None:
        with pytest.raises(ToolError, match="Authorization: Bearer"):
            resolve_api_key(_StubContext())

    def test_http_forwards_bearer_from_real_fastmcp_context(self, http_runtime: Runtime) -> None:
        """Drift guard: exercise the ctx.request_context.request.headers path
        against the REAL FastMCP/mcp/starlette classes instead of stubs, so an
        mcp dependency bump that moves the request attribute fails this test
        instead of silently breaking http-mode credential forwarding."""
        # Arrange: the exact object shape the streamable-http transport builds —
        # a starlette Request placed at RequestContext.request.
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/mcp",
                "query_string": b"",
                "headers": [(b"authorization", b"Bearer tk_r_real")],
            }
        )
        request_context: RequestContext[Any, None, Request] = RequestContext(
            request_id=1,
            meta=None,
            session=cast(Any, None),
            lifespan_context=None,
            request=request,
        )
        ctx: Context[Any, None, Request] = Context(request_context=request_context)

        # Act / Assert
        assert resolve_api_key(ctx) == "tk_r_real"


class TestClientFor:
    def test_http_mode_keeps_per_invocation_clients(self, http_runtime: Runtime) -> None:
        first = client_for(
            _StubContext(
                request_context=_StubRequestContext(
                    request=_StubRequest(headers={"authorization": "Bearer tk_r_first"})
                )
            )
        )
        second = client_for(
            _StubContext(
                request_context=_StubRequestContext(
                    request=_StubRequest(headers={"authorization": "Bearer tk_r_second"})
                )
            )
        )

        assert first is not second
        assert first._http_client is None
        assert second._http_client is None
        # Covers the STREAMABLE-HTTP transport's User-Agent. http mode leaves
        # http_client=None, so TriplClient builds the transport itself from this
        # attribute — the path that would silently regress to the CLI's default
        # `tripl/...` if client_for stopped passing it. The stdio
        # lifespan's UA is covered in test_tools_e2e.
        assert first._user_agent == f"tripl-mcp/{__version__}"

    def test_stdio_mode_requires_lifespan_client(self, stdio_runtime: Runtime) -> None:
        with pytest.raises(ToolError, match="server lifespan"):
            client_for(_StubContext())


class TestEnsureBranchNotMain:
    """The gate must also refuse the main branch's *explicit* id — the backend
    accepts any project branch UUID, main included."""

    @respx.mock
    async def test_working_branch_passes(self, stdio_runtime: Runtime) -> None:
        respx.get(f"{API_BASE}/projects/demo/branches/b-1").mock(
            return_value=httpx.Response(200, json={"id": "b-1", "kind": "working"})
        )
        client = TriplClient(base_url=BASE_URL, api_key=STDIO_KEY)

        await ensure_branch_not_main(client, "demo", "b-1")

    @respx.mock
    async def test_main_branch_id_is_blocked(
        self, stdio_runtime: Runtime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ALLOW_MAIN_ENV, raising=False)
        respx.get(f"{API_BASE}/projects/demo/branches/b-main").mock(
            return_value=httpx.Response(200, json={"id": "b-main", "kind": "main"})
        )
        client = TriplClient(base_url=BASE_URL, api_key=STDIO_KEY)

        with pytest.raises(ToolError, match="MAIN branch"):
            await ensure_branch_not_main(client, "demo", "b-main")

    @respx.mock
    async def test_allow_main_env_skips_lookup(
        self, stdio_runtime: Runtime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ALLOW_MAIN_ENV, "1")
        client = TriplClient(base_url=BASE_URL, api_key=STDIO_KEY)

        await ensure_branch_not_main(client, "demo", "b-main")

        assert not respx.calls  # opt-in mode adds no per-write round trip

    @respx.mock
    async def test_missing_branch_id_is_a_noop(self, stdio_runtime: Runtime) -> None:
        client = TriplClient(base_url=BASE_URL, api_key=STDIO_KEY)

        await ensure_branch_not_main(client, "demo", None)

        assert not respx.calls  # require_branch_id owns the missing-id case


class TestRequireBranchId:
    def test_branch_id_present_passes(self) -> None:
        require_branch_id("b-123")

    def test_missing_branch_id_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ALLOW_MAIN_ENV, raising=False)

        with pytest.raises(ToolError, match="branch_id is required"):
            require_branch_id(None)

    def test_allow_main_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ALLOW_MAIN_ENV, "1")

        require_branch_id(None)

    def test_allow_main_falsy_values_still_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ALLOW_MAIN_ENV, "0")

        with pytest.raises(ToolError, match="branch_id is required"):
            require_branch_id(None)
