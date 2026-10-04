"""End-to-end through FastMCP: in-memory MCP session + respx-mocked tripl API."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from mcp.shared.memory import create_connected_server_and_client_session

from tests.conftest import API_BASE
from tripl_mcp import server as server_module
from tripl_mcp.runtime import ALLOW_MAIN_ENV, Runtime
from tripl_mcp.server import build_server


async def call_tool(name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    """Call one tool over an in-memory client/server session pair."""
    mcp = build_server()
    async with create_connected_server_and_client_session(mcp._mcp_server) as client_session:
        result = await client_session.call_tool(name, arguments)
    text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
    return bool(result.isError), text


@respx.mock
async def test_stdio_lifespan_reuses_one_http_client(
    stdio_runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One stdio server session owns one pool and closes it on shutdown."""
    clients: list[httpx.AsyncClient] = []

    def create_tracking_client(
        base_url: str, api_key: str, timeout: float, user_agent: str
    ) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/api/v1",
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
        )
        clients.append(client)
        return client

    monkeypatch.setattr(server_module, "create_http_client", create_tracking_client)
    route = respx.get(f"{API_BASE}/projects").mock(
        return_value=httpx.Response(200, json=[{"slug": "demo"}])
    )

    mcp = build_server()
    async with create_connected_server_and_client_session(mcp._mcp_server) as client_session:
        first = await client_session.call_tool("list_projects", {})
        second = await client_session.call_tool("list_projects", {})

        assert not first.isError
        assert not second.isError
        assert len(clients) == 1
        assert not clients[0].is_closed
        assert route.call_count == 2

    assert clients[0].is_closed

    second_server = build_server()
    async with create_connected_server_and_client_session(
        second_server._mcp_server
    ) as client_session:
        result = await client_session.call_tool("list_projects", {})

        assert not result.isError
        assert len(clients) == 2
        assert clients[1] is not clients[0]
        assert not clients[1].is_closed

    assert all(client.is_closed for client in clients)


@respx.mock
async def test_read_tool_search_plan_end_to_end(stdio_runtime: Runtime) -> None:
    # Arrange
    route = respx.get(f"{API_BASE}/projects/demo/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "entity_type": "event",
                        "entity_id": "e1",
                        "title": "purchase:success",
                        "score": 9.5,
                        "confidence": 1.0,
                        "highlights": ["huge blob that should be trimmed"],
                    }
                ],
                "total": 1,
                "semantic_used": False,
            },
        )
    )

    # Act
    is_error, text = await call_tool("search_plan", {"slug": "demo", "q": "purchase", "limit": 5})

    # Assert
    assert not is_error
    assert "purchase:success" in text
    assert "huge blob" not in text  # trimmed field
    url = str(route.calls.last.request.url)
    assert "q=purchase" in url
    assert "limit=5" in url
    assert route.calls.last.request.headers["Authorization"].startswith("Bearer tk_w_")
    # Covers the STDIO lifespan's User-Agent: server.py builds that pool, and it
    # is the second of the two places the header is set. The
    # streamable-http path is covered in test_runtime.
    assert route.calls.last.request.headers["User-Agent"].startswith("tripl-mcp/")


@respx.mock
async def test_search_reports_a_missing_semantic_flag_as_false_rather_than_null(
    stdio_runtime: Runtime,
) -> None:
    """A body without the key IS the route saying the index did not answer.

    The backend declares ``SearchResponse.semantic_used: bool = False``, so null
    was never a state this route could be in — and an agent handed null there has
    no way to read the confidences it was handed alongside, which is the whole
    reason the flag is published. Both surfaces now ask
    ``tripl_cli.api.search.semantic_used`` instead of keeping a ``.get`` each.
    """
    respx.get(f"{API_BASE}/projects/demo/search").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0})
    )

    is_error, text = await call_tool("search_plan", {"slug": "demo", "q": "purchase"})

    assert not is_error
    envelope = json.loads(text)
    assert envelope["semantic_used"] is False
    # Same rule, same reason, for the truncation flag the route added later:
    # ``SearchResponse.truncated: bool = False``.
    assert envelope["truncated"] is False


@respx.mock
async def test_search_reports_route_truncation_to_the_agent(
    stdio_runtime: Runtime,
) -> None:
    """``total`` here is the size of the response, not a count to page against.

    The CLI worked around that with a page-fullness guess; this surface had no
    workaround at all and republished ``total`` verbatim, so an agent could not
    tell a complete answer from a clipped one. ``/search`` takes
    no offset, so the flag's answer is a bigger ``limit``.
    """
    respx.get(f"{API_BASE}/projects/demo/search").mock(
        return_value=httpx.Response(
            200, json={"items": [], "total": 0, "truncated": True, "semantic_used": True}
        )
    )

    is_error, text = await call_tool("search_plan", {"slug": "demo", "q": "purchase", "limit": 1})

    assert not is_error
    assert json.loads(text)["truncated"] is True


@respx.mock
async def test_list_events_reads_the_page_through_the_shared_layer(
    stdio_runtime: Runtime,
) -> None:
    """``total`` is the API's pre-paging count and must survive the trim.

    The trim is this consumer's context budget and stays; the envelope around it
    is what the route answers, and the CLI reads the same one from the same
    route. A non-object row is dropped rather than passed through to ``trim``,
    which is what ``tripl events list`` has always done with it.
    """
    respx.get(f"{API_BASE}/projects/demo/events").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"id": "e1", "name": "purchase:success", "status": "live", "field_values": []},
                    "not an object",
                ],
                "total": 137,
            },
        )
    )

    is_error, text = await call_tool("list_events", {"slug": "demo"})

    assert not is_error
    payload = json.loads(text)
    assert payload["total"] == 137
    assert payload["items"] == [{"id": "e1", "name": "purchase:success", "status": "live"}]


@respx.mock
async def test_list_events_sends_reviewed_only_when_the_agent_asks(
    stdio_runtime: Runtime,
) -> None:
    """The filter has three states and one of them is an absent parameter.

    ``reviewed`` is in EVENT_LIST_FIELDS, so an agent could already SEE the flag
    on every row and had no way to filter on it. The false case is the one worth
    a request of its own: "show me what nobody has reviewed" is the question the
    tool exists for, and it is the value a truthiness filter anywhere between
    here and httpx would quietly drop.
    """
    route = respx.get(f"{API_BASE}/projects/demo/events").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0})
    )

    is_error, _ = await call_tool("list_events", {"slug": "demo"})
    assert not is_error
    assert "reviewed" not in route.calls.last.request.url.params

    is_error, _ = await call_tool("list_events", {"slug": "demo", "reviewed": False})
    assert not is_error
    assert route.calls.last.request.url.params["reviewed"] == "false"

    is_error, _ = await call_tool("list_events", {"slug": "demo", "reviewed": True})
    assert not is_error
    assert route.calls.last.request.url.params["reviewed"] == "true"


@respx.mock
async def test_list_events_sends_field_value_and_order_by_only_when_asked(
    stdio_runtime: Runtime,
) -> None:
    """The two filters the route declared and no client could reach.

    ``field_value`` (PR #78) and ``order_by`` (PR #29) both predate the shared
    builder and were left out of it, so an agent could neither ask "which events
    carry this value" - it can already SEE field values through get_event - nor
    rank a large catalog by traffic, only page through the authored order.

    ``order_by`` is checked absent as well as present: the route owns the
    default, and a tool that always spelled ``catalog`` would pin today's answer
    for every agent, the same rule get_scan_status follows for ``limit``.
    """
    route = respx.get(f"{API_BASE}/projects/demo/events").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0})
    )

    is_error, _ = await call_tool("list_events", {"slug": "demo"})
    assert not is_error
    assert "field_value" not in route.calls.last.request.url.params
    assert "order_by" not in route.calls.last.request.url.params

    is_error, _ = await call_tool(
        "list_events", {"slug": "demo", "field_value": "checkout", "order_by": "volume"}
    )
    assert not is_error
    params = route.calls.last.request.url.params
    assert params["field_value"] == "checkout"
    assert params["order_by"] == "volume"


@respx.mock
async def test_write_tool_update_event_end_to_end(stdio_runtime: Runtime) -> None:
    # Arrange
    respx.get(f"{API_BASE}/projects/demo/branches/b-42").mock(
        return_value=httpx.Response(
            200, json={"id": "b-42", "name": "wip", "kind": "working", "status": "open"}
        )
    )
    route = respx.patch(f"{API_BASE}/projects/demo/events/e1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "e1",
                "name": "purchase_success",
                "warnings": ["client-supplied name ignored: scan naming rule applies"],
            },
        )
    )

    # Act
    is_error, text = await call_tool(
        "update_event",
        {
            "slug": "demo",
            "event_id": "e1",
            "branch_id": "b-42",
            "patch": {"description": "Fired after checkout succeeds."},
        },
    )

    # Assert
    assert not is_error
    assert "IMPORTANT_warnings" in text
    assert "scan naming rule" in text
    request = route.calls.last.request
    assert "branch=b-42" in str(request.url)
    assert json.loads(request.content) == {"description": "Fired after checkout succeeds."}


@respx.mock
async def test_update_event_without_branch_id_is_blocked(
    stdio_runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ALLOW_MAIN_ENV, raising=False)

    is_error, text = await call_tool(
        "update_event",
        {"slug": "demo", "event_id": "e1", "branch_id": None, "patch": {"reviewed": True}},
    )

    assert is_error
    assert "branch_id is required" in text
    assert not respx.calls  # never reached the API


@respx.mock
async def test_create_event_without_branch_id_is_blocked(
    stdio_runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ALLOW_MAIN_ENV, raising=False)

    is_error, text = await call_tool(
        "create_event",
        {
            "slug": "demo",
            "branch_id": None,
            "event_type_id": "t1",
            "name": "checkout:completed",
        },
    )

    assert is_error
    assert "branch_id is required" in text
    assert not respx.calls


@respx.mock
async def test_update_event_with_main_branch_id_is_blocked(
    stdio_runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: the main branch's own UUID passes the truthy check but must
    # still be refused — otherwise the write lands on the live main plan.
    monkeypatch.delenv(ALLOW_MAIN_ENV, raising=False)
    respx.get(f"{API_BASE}/projects/demo/branches/b-main").mock(
        return_value=httpx.Response(
            200, json={"id": "b-main", "name": "main", "kind": "main", "status": "merged"}
        )
    )
    patch_route = respx.patch(f"{API_BASE}/projects/demo/events/e1").mock(
        return_value=httpx.Response(200, json={"id": "e1", "warnings": []})
    )

    # Act
    is_error, text = await call_tool(
        "update_event",
        {
            "slug": "demo",
            "event_id": "e1",
            "branch_id": "b-main",
            "patch": {"reviewed": True},
        },
    )

    # Assert
    assert is_error
    assert "MAIN branch" in text
    assert not patch_route.called  # the mutation never reached the API


@respx.mock
async def test_create_event_with_main_branch_id_is_blocked(
    stdio_runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ALLOW_MAIN_ENV, raising=False)
    respx.get(f"{API_BASE}/projects/demo/branches/b-main").mock(
        return_value=httpx.Response(
            200, json={"id": "b-main", "name": "main", "kind": "main", "status": "merged"}
        )
    )
    post_route = respx.post(f"{API_BASE}/projects/demo/events").mock(
        return_value=httpx.Response(201, json={"id": "e9", "warnings": []})
    )

    is_error, text = await call_tool(
        "create_event",
        {
            "slug": "demo",
            "branch_id": "b-main",
            "event_type_id": "t1",
            "name": "checkout:completed",
        },
    )

    assert is_error
    assert "MAIN branch" in text
    assert not post_route.called


@respx.mock
async def test_allow_main_env_permits_branchless_write(
    stdio_runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOW_MAIN_ENV, "1")
    route = respx.post(f"{API_BASE}/projects/demo/events").mock(
        return_value=httpx.Response(
            201, json={"id": "e9", "name": "checkout:completed", "warnings": []}
        )
    )

    is_error, text = await call_tool(
        "create_event",
        {
            "slug": "demo",
            "branch_id": None,
            "event_type_id": "t1",
            "name": "checkout:completed",
        },
    )

    assert not is_error
    assert "checkout:completed" in text
    assert "branch" not in str(route.calls.last.request.url)


@respx.mock
async def test_403_surfaces_scope_guidance_through_mcp(stdio_runtime: Runtime) -> None:
    respx.get(f"{API_BASE}/projects").mock(
        return_value=httpx.Response(403, json={"detail": "API key is scoped to a single project"})
    )

    is_error, text = await call_tool("list_projects", {})

    assert is_error
    assert "scoped to a single project" in text


@respx.mock
async def test_401_regains_mcp_credential_guidance_through_mcp(stdio_runtime: Runtime) -> None:
    """The shared client's neutral 401 gets tripl-mcp's wording put back on.

    ``tripl_cli.client`` cannot name TRIPL_API_KEY or a Bearer header — the same
    module serves the CLI, which reads --api-key and a config file — so
    ``runtime._McpTriplClient`` re-attaches the hint via ``errors.to_tool_error``.
    This asserts the agent still gets both, i.e. that the
    extraction cost nothing an agent reads.
    """
    respx.get(f"{API_BASE}/projects").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid or expired API key"})
    )

    is_error, text = await call_tool("list_projects", {})

    assert is_error
    assert "Invalid or expired API key (401)" in text
    assert "TRIPL_API_KEY" in text
    assert "Bearer token sent to this server" in text


@respx.mock
async def test_unreachable_instance_regains_mcp_base_url_guidance(stdio_runtime: Runtime) -> None:
    """Same seam, connection side: TRIPL_BASE_URL is this server's variable."""
    respx.get(f"{API_BASE}/projects").mock(side_effect=httpx.ConnectError("refused"))

    is_error, text = await call_tool("list_projects", {})

    assert is_error
    assert "Could not reach the tripl API" in text
    assert "Check TRIPL_BASE_URL" in text


@respx.mock
async def test_list_event_types_is_branch_scoped_and_trimmed(stdio_runtime: Runtime) -> None:
    """Plan reads follow the branch, and do not ship the whole field catalogue.

    Without ``branch`` the API resolves main, so an agent working on a branch was
    quietly answered with main's event types. The response also came back
    verbatim — every event type carrying all of its field definitions.
    """
    route = respx.get(f"{API_BASE}/projects/demo/event-types").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "t1",
                    "project_id": "p1",
                    "name": "pageview",
                    "display_name": "Page view",
                    "description": "",
                    "color": "#fff",
                    "order": 0,
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "field_definitions": [{"id": "f1"}, {"id": "f2"}],
                }
            ],
        )
    )

    is_error, text = await call_tool("list_event_types", {"slug": "demo", "branch_id": "br-7"})

    assert not is_error
    assert "branch=br-7" in str(route.calls.last.request.url)
    # FastMCP emits one content block per list element, so a single-item list
    # arrives as that item's JSON.
    event_type = json.loads(text)
    assert event_type["field_count"] == 2
    # Bulk dropped: the embedded definitions and the row bookkeeping.
    assert "field_definitions" not in event_type
    assert "created_at" not in event_type
    assert "project_id" not in event_type


@respx.mock
async def test_get_event_type_fields_forwards_branch_and_trims_fields(
    stdio_runtime: Runtime,
) -> None:
    type_route = respx.get(f"{API_BASE}/projects/demo/event-types/t1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "t1",
                "project_id": "p1",
                "name": "pageview",
                "display_name": "Page view",
                "description": "",
                "color": "#fff",
                "order": 0,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )
    )
    fields_route = respx.get(f"{API_BASE}/projects/demo/event-types/t1/fields").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "f1",
                    "event_type_id": "t1",
                    "name": "url",
                    "display_name": "URL",
                    "field_type": "string",
                    "is_required": True,
                    "enum_options": None,
                    "description": "",
                    "order": 0,
                    "sensitivity": "none",
                    "contract_max_bad_rate": 0.1,
                    "contract_regex": None,
                },
            ],
        )
    )

    is_error, text = await call_tool(
        "get_event_type_fields",
        {"slug": "demo", "event_type_id": "t1", "branch_id": "br-7"},
    )

    assert not is_error
    assert "branch=br-7" in str(type_route.calls.last.request.url)
    assert "branch=br-7" in str(fields_route.calls.last.request.url)
    payload = json.loads(text)
    field = payload["fields"][0]
    # Everything needed to compose a valid value survives...
    assert field["name"] == "url"
    assert field["is_required"] is True
    assert field["field_type"] == "string"
    # ...and the scanner-side contract thresholds do not.
    assert "contract_max_bad_rate" not in field
    assert "event_type_id" not in field


@respx.mock
async def test_list_scans_is_trimmed(stdio_runtime: Runtime) -> None:
    """The one field projection the CLI and the MCP share.

    ``ScanConfigResponse`` carries ``base_query`` — free-text SQL that can run to
    kilobytes — plus ~20 tuning knobs. The tool's own description already
    promised "(id, name, schedule, governed event types)", so trimming aligns the
    payload with the description rather than changing a contract anybody stated.
    ``dispatchable`` comes with it, and is computed in exactly one place.
    """
    respx.get(f"{API_BASE}/projects/demo/scans").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "s1",
                    "project_id": "p1",
                    "name": "prod events",
                    "interval": "1h",
                    "time_column": "event_time",
                    "data_source_id": "ds1",
                    "base_query": "select * from analytics.events",
                    "cardinality_threshold": 500,
                    "json_value_paths": [],
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            ],
        )
    )

    is_error, text = await call_tool("list_scans", {"slug": "demo"})

    assert not is_error
    scan = json.loads(text)
    assert scan["name"] == "prod events"
    assert scan["dispatchable"] is True
    assert "base_query" not in scan
    assert "cardinality_threshold" not in scan
    assert "json_value_paths" not in scan


@respx.mock
async def test_get_scan_status_still_asks_for_the_server_default_job_window(
    stdio_runtime: Runtime,
) -> None:
    """No ``limit`` on the wire.

    doctor asks the same builder for 200 because it measures how long a streak
    has run; an agent polling status wants the newest few and should keep taking
    the server's own default. Guards against the shared layer silently adopting
    doctor's window for both.
    """
    route = respx.get(f"{API_BASE}/projects/demo/scans/s1/jobs").mock(
        return_value=httpx.Response(200, json=[{"id": "j1", "status": "completed"}])
    )

    is_error, _ = await call_tool("get_scan_status", {"slug": "demo", "scan_id": "s1"})

    assert not is_error
    assert "limit" not in str(route.calls.last.request.url)


@respx.mock
async def test_trigger_scan_posts_to_the_run_route(stdio_runtime: Runtime) -> None:
    """Behaviour-preserving through the shared builder: same method, same path."""
    route = respx.post(f"{API_BASE}/projects/demo/scans/s1/run").mock(
        return_value=httpx.Response(201, json={"id": "j9", "status": "pending"})
    )

    is_error, text = await call_tool("trigger_scan", {"slug": "demo", "scan_id": "s1"})

    assert not is_error
    assert route.calls.last.request.method == "POST"
    assert json.loads(text)["id"] == "j9"


# --- docs catalog (F22) -------------------------------------------------------


def _doc_row(path: str, *, scope: str = "project", audience: str = "both") -> dict[str, Any]:
    return {
        "scope": scope,
        "path": path,
        "title": path,
        "description": "",
        "tags": ["warehouse"],
        "audience": audience,
        "revision": 3,
        "size_bytes": 120,
        "updated_at": "2026-09-27T10:00:00Z",
        "updated_by_name": "Ada",
    }


@respx.mock
async def test_list_docs_merges_both_roots_and_trims_rows(stdio_runtime: Runtime) -> None:
    respx.get(f"{API_BASE}/projects/demo/docs").mock(
        return_value=httpx.Response(
            200,
            json={
                "project": {"slug": "demo", "name": "Demo"},
                "organization": {"id": "o1", "slug": "default", "name": "Default"},
                "project_docs": [
                    _doc_row("a.md", audience="agent"),
                    _doc_row("b.md", audience="human"),
                ],
                "organization_docs": [_doc_row("c.md", scope="organization")],
                "limits": {},
            },
        )
    )

    is_error, text = await call_tool("list_docs", {"slug": "demo", "audience": "agent"})

    assert not is_error
    payload = json.loads(text)
    # `both` is written for either reader, so it matches `agent` too.
    assert [(row["scope"], row["path"]) for row in payload["items"]] == [
        ("project", "a.md"),
        ("organization", "c.md"),
    ]
    assert payload["total"] == 2
    assert "size_bytes" not in payload["items"][0]


@respx.mock
async def test_list_docs_narrows_to_one_scope(stdio_runtime: Runtime) -> None:
    respx.get(f"{API_BASE}/projects/demo/docs").mock(
        return_value=httpx.Response(
            200,
            json={
                "project_docs": [_doc_row("a.md")],
                "organization_docs": [_doc_row("c.md", scope="organization")],
            },
        )
    )

    is_error, text = await call_tool("list_docs", {"slug": "demo", "scope": "organization"})

    assert not is_error
    assert [row["path"] for row in json.loads(text)["items"]] == ["c.md"]


@respx.mock
async def test_read_doc_sends_scope_and_path_and_drops_the_duplicate_body(
    stdio_runtime: Runtime,
) -> None:
    route = respx.get(f"{API_BASE}/projects/demo/docs/file").mock(
        return_value=httpx.Response(
            200,
            json={
                **_doc_row("guides/warehouse.md", scope="organization"),
                "id": "d1",
                "content": "---\ntitle: W\n---\nUse [[event:purchase]].\n",
                "body": "Use [[event:purchase]].\n",
                "extra_frontmatter": {"name": "warehouse"},
                "links": [{"kind": "event", "target": "purchase", "status": "resolved"}],
            },
        )
    )

    is_error, text = await call_tool(
        "read_doc", {"slug": "demo", "scope": "organization", "path": "guides/warehouse.md"}
    )

    assert not is_error
    params = route.calls.last.request.url.params
    assert params["scope"] == "organization"
    assert params["path"] == "guides/warehouse.md"
    payload = json.loads(text)
    assert payload["content"].startswith("---\n")
    assert "body" not in payload
    assert "id" not in payload
    assert payload["links"][0]["status"] == "resolved"
    # No lang: the server picks the project's agent default.
    assert "lang" not in params


@respx.mock
async def test_read_doc_passes_lang_and_keeps_the_translation_fields(
    stdio_runtime: Runtime,
) -> None:
    route = respx.get(f"{API_BASE}/projects/demo/docs/file").mock(
        return_value=httpx.Response(
            200,
            json={
                **_doc_row("a.md"),
                "content": "# Hallo\n",
                "lang": "de",
                "requested_lang": "de",
                "translation_fallback": None,
                "translation_outdated": True,
                "translations": [{"lang": "de", "status": "ready", "outdated": True}],
            },
        )
    )

    is_error, text = await call_tool(
        "read_doc", {"slug": "demo", "scope": "project", "path": "a.md", "lang": "de"}
    )

    assert not is_error
    assert route.calls.last.request.url.params["lang"] == "de"
    payload = json.loads(text)
    assert (payload["lang"], payload["translation_outdated"]) == ("de", True)
    assert payload["translations"][0]["lang"] == "de"


@respx.mock
async def test_read_doc_refuses_an_unknown_scope_before_any_request(
    stdio_runtime: Runtime,
) -> None:
    route = respx.get(f"{API_BASE}/projects/demo/docs/file").mock(
        return_value=httpx.Response(200, json={})
    )

    is_error, _ = await call_tool("read_doc", {"slug": "demo", "scope": "team", "path": "a.md"})

    assert is_error
    assert route.call_count == 0


@respx.mock
async def test_search_docs_reports_the_route_envelope(stdio_runtime: Runtime) -> None:
    route = respx.get(f"{API_BASE}/projects/demo/docs/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [{"scope": "project", "path": "a.md", "title": "A", "confidence": 0.8}],
                "total": 1,
                "truncated": True,
                "semantic_used": True,
            },
        )
    )

    is_error, text = await call_tool(
        "search_docs", {"slug": "demo", "q": "late events", "scope": "project", "limit": 5}
    )

    assert not is_error
    params = route.calls.last.request.url.params
    assert (params["q"], params["scope"], params["limit"]) == ("late events", "project", "5")
    payload = json.loads(text)
    assert payload["truncated"] is True
    assert payload["semantic_used"] is True
    assert payload["items"][0]["path"] == "a.md"


@respx.mock
async def test_write_doc_puts_the_content_with_the_lock_and_hoists_link_warnings(
    stdio_runtime: Runtime,
) -> None:
    route = respx.put(f"{API_BASE}/projects/demo/docs/file").mock(
        return_value=httpx.Response(
            200,
            json={
                **_doc_row("a.md"),
                "revision": 4,
                "content": "big",
                "body": "big",
                "created": False,
                "changed": True,
                "warnings": ["Broken link [[event:gone]]: no event named 'gone' on the main plan"],
                "links": [],
            },
        )
    )

    is_error, text = await call_tool(
        "write_doc",
        {
            "slug": "demo",
            "scope": "project",
            "path": "a.md",
            "content": "See [[event:gone]].",
            "base_revision": 3,
            "message": "agent edit",
        },
    )

    assert not is_error
    request = route.calls.last.request
    assert request.method == "PUT"
    assert request.url.params["scope"] == "project"
    assert request.url.params["path"] == "a.md"
    assert json.loads(request.content) == {
        "content": "See [[event:gone]].",
        "base_revision": 3,
        "message": "agent edit",
    }
    payload = json.loads(text)
    assert "gone" in payload["IMPORTANT_warnings"][0]
    assert payload["result"]["revision"] == 4
    assert "content" not in payload["result"]


@respx.mock
async def test_write_doc_with_a_read_key_surfaces_the_server_403(stdio_runtime: Runtime) -> None:
    respx.put(f"{API_BASE}/projects/demo/docs/file").mock(
        return_value=httpx.Response(403, json={"detail": "API key has read-only scope"})
    )

    is_error, text = await call_tool(
        "write_doc", {"slug": "demo", "scope": "project", "path": "a.md", "content": "x"}
    )

    assert is_error
    assert "read-only scope" in text
