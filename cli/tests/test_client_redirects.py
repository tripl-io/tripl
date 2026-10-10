"""A redirect is followed for a read and refused for a write.

httpx re-sends a POST as a GET after a 301, and any method as a GET after a 302
or 303. Every write route the CLI and tripl-mcp call has a GET sibling on the
same path, so a followed redirect turned the write into a read that answered
200: `tripl annotate` reported "already exists" and exited 0 with nothing
written, and tripl-mcp's create_event handed back an event list as the result.
The usual trigger is an http:// base URL behind an "always use HTTPS" 301.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.conftest import API_BASE, BASE_URL, FakeInstance
from tripl_cli.cli import main
from tripl_cli.client import TriplClient, create_http_client
from tripl_cli.errors import TriplAPIError

HTTPS_API_BASE = API_BASE.replace("http://", "https://", 1)
PATH = "/projects/demo/annotations"

# The redirect TARGET is registered so a followed redirect would land somewhere
# observable; it must stay uncalled, hence assert_all_called=False.


def make_client(http_client: httpx.AsyncClient | None = None) -> TriplClient:
    return TriplClient(base_url=BASE_URL, api_key="tk_w_abc", http_client=http_client)


@pytest.mark.parametrize(
    ("method", "status"),
    [
        ("POST", 301),
        ("POST", 302),
        ("POST", 303),
        ("POST", 307),
        ("PUT", 302),
        ("PATCH", 302),
        ("DELETE", 302),
    ],
)
@respx.mock(assert_all_called=False)
async def test_a_redirected_write_raises_and_never_reaches_the_target(
    method: str, status: int
) -> None:
    location = f"{HTTPS_API_BASE}{PATH}"
    respx.request(method, f"{API_BASE}{PATH}").mock(
        return_value=httpx.Response(status, headers={"Location": location})
    )
    target_get = respx.get(location).mock(return_value=httpx.Response(200, json=[]))
    target_same_method = respx.request(method, location).mock(
        return_value=httpx.Response(201, json={"id": "ann-1"})
    )

    with pytest.raises(TriplAPIError) as excinfo:
        await make_client().request(method, PATH, json_body={"label": "x"})

    assert excinfo.value.status_code == status
    assert location in str(excinfo.value)
    assert f"redirected {method} {PATH}" in str(excinfo.value)
    assert not target_get.called
    assert not target_same_method.called


@respx.mock(assert_all_called=False)
async def test_a_borrowed_pool_refuses_a_redirected_write_too() -> None:
    """The CLI runner and stdio tripl-mcp lend one pool; that branch must hold the rule.

    The pool here follows redirects by default, so the test proves the per-request
    choice wins over whatever the lender configured.
    """
    location = f"{HTTPS_API_BASE}{PATH}"
    respx.post(f"{API_BASE}{PATH}").mock(
        return_value=httpx.Response(301, headers={"Location": location})
    )
    target_get = respx.get(location).mock(return_value=httpx.Response(200, json=[]))

    async with httpx.AsyncClient(base_url=API_BASE, follow_redirects=True) as pool:
        with pytest.raises(TriplAPIError, match="redirected POST"):
            await make_client(pool).post(PATH, json_body={"label": "x"})

    assert not target_get.called


@respx.mock
async def test_a_read_on_the_shared_pool_still_follows_a_redirect() -> None:
    """``create_http_client`` no longer follows at the pool level; a GET must still."""
    respx.get(f"{API_BASE}/projects").mock(
        return_value=httpx.Response(301, headers={"Location": f"{HTTPS_API_BASE}/projects"})
    )
    respx.get(f"{HTTPS_API_BASE}/projects").mock(
        return_value=httpx.Response(200, json=[{"slug": "demo"}])
    )

    async with create_http_client(BASE_URL, "tk_r_abc") as pool:
        assert await make_client(pool).get("/projects") == [{"slug": "demo"}]


@respx.mock
async def test_a_bodiless_redirect_is_not_mistaken_for_an_empty_success() -> None:
    """Without the 3xx check an empty 301 body came back as {"status": "ok"}."""
    respx.post(f"{API_BASE}/projects/demo/scans/s1/run").mock(
        return_value=httpx.Response(301, headers={"Location": "https://tripl.test/x"})
    )

    with pytest.raises(TriplAPIError, match="301"):
        await make_client().post("/projects/demo/scans/s1/run")


@respx.mock
async def test_a_redirect_without_location_still_fails() -> None:
    respx.post(f"{API_BASE}{PATH}").mock(return_value=httpx.Response(302))

    with pytest.raises(TriplAPIError, match="no Location header"):
        await make_client().post(PATH, json_body={})


@respx.mock
async def test_a_read_still_follows_http_to_https() -> None:
    respx.get(f"{API_BASE}/projects").mock(
        return_value=httpx.Response(301, headers={"Location": f"{HTTPS_API_BASE}/projects"})
    )
    target = respx.get(f"{HTTPS_API_BASE}/projects").mock(
        return_value=httpx.Response(200, json=[{"slug": "demo"}])
    )

    assert await make_client().get("/projects") == [{"slug": "demo"}]
    # httpx keeps the credential on an http -> https hop to the same host.
    assert target.calls.last.request.headers["Authorization"] == "Bearer tk_w_abc"


def test_annotate_behind_a_301_exits_non_zero_and_names_the_target(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The deploy-step case: before, this printed "already exists" and exited 0."""
    url = tripl_api.annotations_url("prod")
    location = url.replace("http://", "https://", 1)
    tripl_api.handler(
        url,
        lambda _request: httpx.Response(301, headers={"Location": location}),
        method="POST",
    )
    target = tripl_api.router.get(location).mock(return_value=httpx.Response(200, json=[]))

    assert main(["annotate", "Deployed", "--project", "prod"]) != 0

    captured = capsys.readouterr()
    assert location in captured.err
    assert "already exists" not in captured.out
    assert not target.called
