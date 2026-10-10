"""What the trimmed list tools keep, and the one warning-hoist shape.

The trims are a context budget, but a trim that drops what a tool description
tells the agent to rely on makes the agent pay a second call for it: the
`title` create_event says to set, the naming rule that may override the `name`
it sends, the columns the list filters act on, and the counts a reconciliation
envelope carries beside its rows.
"""

from __future__ import annotations

import json

import httpx
import respx

from tests.conftest import API_BASE
from tests.test_tools_e2e import call_tool
from tripl_mcp.runtime import Runtime
from tripl_mcp.tools._common import hoist_warnings, summarize_collection, with_mutation_warnings
from tripl_mcp.tools.docs import with_link_warnings


@respx.mock
async def test_list_events_keeps_the_title_and_the_filtered_columns(
    stdio_runtime: Runtime,
) -> None:
    respx.get(f"{API_BASE}/projects/demo/events").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "e1",
                        "name": "purchase:success",
                        "title": "Purchase succeeded",
                        "status": "live",
                        "open_question_count": 2,
                        "last_seen_at": "2026-10-01T00:00:00Z",
                        "drift_count": 4,
                        "field_values": [],
                    }
                ],
                "total": 1,
            },
        )
    )

    is_error, text = await call_tool("list_events", {"slug": "demo"})

    assert not is_error
    (row,) = json.loads(text)["items"]
    assert row["title"] == "Purchase succeeded"
    assert row["open_question_count"] == 2
    assert row["last_seen_at"] == "2026-10-01T00:00:00Z"
    assert "drift_count" not in row
    assert "field_values" not in row


@respx.mock
async def test_event_types_carry_the_naming_rule_that_may_override_a_name(
    stdio_runtime: Runtime,
) -> None:
    event_type = {
        "id": "t1",
        "name": "pageview",
        "display_name": "Page view",
        "event_name_format": "{screen}:{action}",
        "field_definitions": [],
    }
    respx.get(f"{API_BASE}/projects/demo/event-types").mock(
        return_value=httpx.Response(200, json=[event_type])
    )
    respx.get(f"{API_BASE}/projects/demo/event-types/t1").mock(
        return_value=httpx.Response(200, json=event_type)
    )
    respx.get(f"{API_BASE}/projects/demo/event-types/t1/fields").mock(
        return_value=httpx.Response(200, json=[])
    )

    is_error, listed = await call_tool("list_event_types", {"slug": "demo"})
    assert not is_error
    assert json.loads(listed)["event_name_format"] == "{screen}:{action}"

    is_error, merged = await call_tool(
        "get_event_type_fields", {"slug": "demo", "event_type_id": "t1"}
    )
    assert not is_error
    assert json.loads(merged)["event_name_format"] == "{screen}:{action}"


@respx.mock
async def test_reconciliation_status_keeps_the_envelope_counts(stdio_runtime: Runtime) -> None:
    respx.get(f"{API_BASE}/projects/demo/reconciliation/coverage").mock(
        return_value=httpx.Response(200, json={"documented": 10})
    )
    respx.get(f"{API_BASE}/projects/demo/reconciliation/dead-events").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "d1"}], "total": 1, "days": 30})
    )
    respx.get(f"{API_BASE}/projects/demo/reconciliation/shadow-events").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": "s1"}, {"id": "s2"}], "total": 2, "new_count": 1}
        )
    )

    is_error, text = await call_tool("reconciliation_status", {"slug": "demo"})

    assert not is_error
    payload = json.loads(text)
    assert payload["dead_events"] == {"days": 30, "total": 1, "sample": [{"id": "d1"}]}
    assert payload["shadow_events"]["new_count"] == 1
    assert payload["shadow_events"]["total"] == 2


def test_a_summary_keeps_scalars_but_never_a_second_collection() -> None:
    summary = summarize_collection(
        {"items": [{"id": "a"}], "total": 5, "new_count": 3, "extra": [1, 2], "meta": {"k": 1}}
    )

    assert summary == {"new_count": 3, "total": 5, "sample": [{"id": "a"}]}


def test_both_write_hoists_share_one_shape() -> None:
    event = {"id": "e1", "warnings": ["name was derived"]}
    note = {"path": "a.md", "revision": 2, "warnings": ["[[event:x]] is broken"]}

    hoisted_event = with_mutation_warnings(event)
    hoisted_note = with_link_warnings(note)

    for hoisted, source in ((hoisted_event, event), (hoisted_note, note)):
        assert list(hoisted) == ["IMPORTANT_warnings", "note", "result"]
        assert hoisted["IMPORTANT_warnings"] == source["warnings"]
        assert "warnings" not in hoisted["result"]
    assert "Adopt the server-canonical name/id" in hoisted_event["note"]
    assert "base_revision" in hoisted_note["note"]


def test_a_write_without_warnings_passes_through() -> None:
    clean = {"id": "e1", "warnings": []}

    assert hoist_warnings(clean, "unused") is clean
    assert hoist_warnings(["not", "an", "object"], "unused") == ["not", "an", "object"]
