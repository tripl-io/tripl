"""Client error mapping and User-Agent handling.

Moved wholesale from mcp-server/tests/test_client.py when the client became
shared. Every ``match=`` string is preserved: the extraction was
supposed to change the exception TYPE and nothing an operator or agent reads.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.conftest import API_BASE, BASE_URL
from tripl_cli import __version__
from tripl_cli.client import TriplClient
from tripl_cli.errors import TriplAPIError, TriplConnectionError


def make_client() -> TriplClient:
    return TriplClient(base_url=BASE_URL, api_key="tk_r_abc")


@respx.mock
async def test_sends_bearer_and_user_agent() -> None:
    # Arrange
    route = respx.get(f"{API_BASE}/projects").mock(
        return_value=httpx.Response(200, json=[{"slug": "demo"}])
    )

    # Act
    data = await make_client().get("/projects")

    # Assert
    assert data == [{"slug": "demo"}]
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer tk_r_abc"
    assert request.headers["User-Agent"] == f"tripl/{__version__}"


@respx.mock
async def test_user_agent_is_forwarded_verbatim_when_supplied() -> None:
    """The seam tripl-mcp uses to stay distinguishable in server access logs.

    A careless move of this module would have flattened every consumer's UA to
    one string; this pins that a caller-supplied value survives the per-request
    transport built inside ``TriplClient._send`` (the http_client=None path,
    which is the one streamable-http tripl-mcp always takes).
    """
    route = respx.get(f"{API_BASE}/projects").mock(return_value=httpx.Response(200, json=[]))
    client = TriplClient(base_url=BASE_URL, api_key="tk_r_abc", user_agent="tripl-mcp/9.9.9")

    await client.get("/projects")

    assert route.calls.last.request.headers["User-Agent"] == "tripl-mcp/9.9.9"


@respx.mock
async def test_drops_none_params_and_maps_branch() -> None:
    route = respx.get(f"{API_BASE}/projects/demo/search").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0})
    )

    await make_client().get(
        "/projects/demo/search", params={"q": "x", "limit": None, "branch": None}
    )

    assert str(route.calls.last.request.url).endswith("/search?q=x")


@respx.mock
async def test_401_maps_to_invalid_key_error() -> None:
    respx.get(f"{API_BASE}/projects").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid or expired API key"})
    )

    with pytest.raises(TriplAPIError, match="Invalid or expired API key \\(401\\)") as excinfo:
        await make_client().get("/projects")

    # The status travels with the exception so each consumer can add its own
    # guidance — that is the whole reason it is not baked into the message.
    assert excinfo.value.status_code == 401


@respx.mock
async def test_403_maps_to_scope_error_with_api_detail() -> None:
    respx.get(f"{API_BASE}/projects").mock(
        return_value=httpx.Response(403, json={"detail": "API key is scoped to a single project"})
    )

    with pytest.raises(TriplAPIError, match="scoped to a single project"):
        await make_client().get("/projects")


@respx.mock
async def test_404_carries_api_detail() -> None:
    respx.get(f"{API_BASE}/projects/demo/events/nope").mock(
        return_value=httpx.Response(404, json={"detail": "Event not found"})
    )

    with pytest.raises(TriplAPIError, match="Not found \\(404\\): Event not found"):
        await make_client().get("/projects/demo/events/nope")


@respx.mock
async def test_422_carries_validation_detail_verbatim() -> None:
    detail = [{"loc": ["body", "name"], "msg": "field required", "type": "value_error.missing"}]
    respx.post(f"{API_BASE}/projects/demo/events").mock(
        return_value=httpx.Response(422, json={"detail": detail})
    )

    with pytest.raises(TriplAPIError, match="422") as excinfo:
        await make_client().post("/projects/demo/events", json_body={})

    assert "field required" in str(excinfo.value)
    assert "value_error.missing" in str(excinfo.value)
    assert excinfo.value.detail == detail


@respx.mock
async def test_409_carries_conflict_detail() -> None:
    respx.post(f"{API_BASE}/projects/demo/events").mock(
        return_value=httpx.Response(409, json={"detail": "Event name already exists"})
    )

    with pytest.raises(TriplAPIError, match="409.*Event name already exists"):
        await make_client().post("/projects/demo/events", json_body={"name": "dup"})


@respx.mock
async def test_connection_error_names_base_url() -> None:
    respx.get(f"{API_BASE}/projects").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(TriplConnectionError, match="Could not reach the tripl API") as excinfo:
        await make_client().get("/projects")

    # Structured, not just interpolated: the CLI renders the URL alongside where
    # it came from, and tripl-mcp names TRIPL_BASE_URL.
    assert excinfo.value.base_url == API_BASE


@respx.mock
async def test_204_becomes_a_status_payload() -> None:
    respx.post(f"{API_BASE}/projects/demo/scans/s1/run").mock(return_value=httpx.Response(204))

    assert await make_client().post("/projects/demo/scans/s1/run") == {"status": "ok"}
