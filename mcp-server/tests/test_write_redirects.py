"""A write tool behind a redirect fails, naming the setting to fix.

Before, the shared client followed every redirect, and httpx re-sends a POST as
a GET after a 301. `create_event` against an http:// TRIPL_BASE_URL behind an
"always use HTTPS" proxy therefore read the event list and handed it back as
the created event, with no error.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.conftest import API_BASE
from tests.test_tools_e2e import call_tool
from tripl_mcp.runtime import ALLOW_MAIN_ENV, Runtime


@pytest.mark.respx(assert_all_called=False)
async def test_create_event_behind_a_301_is_an_error_not_a_read(
    stdio_runtime: Runtime, monkeypatch: pytest.MonkeyPatch, respx_mock: respx.MockRouter
) -> None:
    monkeypatch.setenv(ALLOW_MAIN_ENV, "1")
    events_url = f"{API_BASE}/projects/demo/events"
    location = events_url.replace("http://", "https://", 1)
    respx_mock.post(events_url).mock(
        return_value=httpx.Response(301, headers={"Location": location})
    )
    # Registered so a followed redirect would land somewhere observable.
    target_get = respx_mock.get(location).mock(return_value=httpx.Response(200, json={"items": []}))

    is_error, text = await call_tool(
        "create_event",
        {"slug": "demo", "branch_id": None, "event_type_id": "t1", "name": "checkout:completed"},
    )

    assert is_error
    assert location in text
    assert "TRIPL_BASE_URL" in text
    assert not target_get.called
