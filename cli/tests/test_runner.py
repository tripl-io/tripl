"""The sync/async seam: one pool per invocation, bounded fan-out, errors intact."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from tests.conftest import API_BASE, API_KEY, BASE_URL
from tripl_cli import runner
from tripl_cli.client import TriplClient
from tripl_cli.config import Config
from tripl_cli.errors import TriplAPIError, TriplConfigError


def make_config(*, api_key: str | None = API_KEY, base_url: str | None = BASE_URL) -> Config:
    return Config(
        base_url=base_url,
        api_key=api_key,
        path=Path("/nonexistent/config.toml"),
        path_exists=False,
        sources={"base_url": "$TRIPL_BASE_URL", "api_key": "$TRIPL_API_KEY"},
    )


def test_run_async_opens_exactly_one_pool_and_closes_it_when_the_body_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The close-exactly-once guarantee, on the path where it is actually at risk."""
    created: list[httpx.AsyncClient] = []
    real = runner.create_http_client

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        client = real(*args, **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(runner, "create_http_client", factory)

    async def body(_client: httpx.AsyncClient) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        runner.run_async(make_config(), body)

    assert len(created) == 1
    assert created[0].is_closed


def test_run_async_requires_credentials_before_opening_a_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "You have no key" (exit 2, no network) stays distinct from "the key was refused"."""
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        runner,
        "create_http_client",
        lambda *args, **kwargs: calls.append(args),  # type: ignore[misc]
    )

    async def body(_client: httpx.AsyncClient) -> None:  # pragma: no cover - never reached
        raise AssertionError("the body must not run without credentials")

    with pytest.raises(TriplConfigError, match="no tripl API key configured"):
        runner.run_async(make_config(api_key=None), body)

    assert calls == []


@respx.mock
def test_run_unauthenticated_needs_no_credentials_and_sends_no_authorization() -> None:
    """The second bridge, and the reason it exists.

    ``tripl install`` polls an instance that BY DEFINITION has no credentials
    yet - the first account has not been created, so no API key can exist. It
    must therefore reach the wire with no Config at all, and it must never put
    an Authorization header on an endpoint the backend deliberately leaves
    unauthenticated.
    """
    route = respx.get(f"{BASE_URL}/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    async def body(timeout: float) -> Any:
        # The per-request timeout is handed IN rather than applied around the
        # body: install's five-minute poll deadline and its ten-second request
        # deadline are different numbers.
        assert timeout == 3.5
        from tripl_cli.diagnostics.collect import probe_health

        return await probe_health(BASE_URL, timeout)

    fetched = runner.run_unauthenticated(body, timeout=3.5)

    assert fetched.ok
    assert "authorization" not in route.calls[0].request.headers


@respx.mock
def test_tripl_api_error_crosses_asyncio_run_with_its_status_code_intact() -> None:
    """The bridge adds an event loop, not a new error regime."""
    respx.get(f"{API_BASE}/projects").mock(
        return_value=httpx.Response(403, json={"detail": "API key is scoped to a single project"})
    )

    async def body(client: httpx.AsyncClient) -> Any:
        return await TriplClient(BASE_URL, API_KEY, http_client=client).get("/projects")

    with pytest.raises(TriplAPIError) as excinfo:
        runner.run_async(make_config(), body)

    assert excinfo.value.status_code == 403
    # main()'s `except TriplError` still routes it to a message and exit 1.
    assert excinfo.value.exit_code == 1


async def test_gather_bounded_holds_concurrency_at_exactly_the_limit() -> None:
    """Peak in-flight must REACH the limit, not merely stay under it.

    The equality is the half that matters: a semaphore that never releases would
    pass a `<= limit` assertion by never saturating. Plain coroutines rather than
    respx — a mocked response resolves too fast to show overlap.
    """
    inflight = 0
    peak = 0

    async def work(index: int) -> int:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.01)
        inflight -= 1
        return index

    results = await runner.gather_bounded([work(i) for i in range(40)], limit=6)

    assert peak == 6
    assert len(results) == 40


async def test_gather_bounded_preserves_input_order() -> None:
    """Results map back onto targets; a reordering bug mislabels every finding."""

    async def work(index: int) -> int:
        # Reverse the completion order relative to the input order.
        await asyncio.sleep((10 - index) * 0.005)
        return index

    assert await runner.gather_bounded([work(i) for i in range(10)], limit=6) == list(range(10))
