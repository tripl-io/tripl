"""tripl-mcp reads TRIPL_BASE_URL the way the CLI does.

The docs send an operator to `tripl doctor` to prove the URL an MCP client is
about to use. That proof only holds if both read the variable identically:
before, `https://host/api/v1` passed doctor and sent every tool to
/api/v1/api/v1, and a bare host started the server and failed on every call.
"""

from __future__ import annotations

import pytest

from tripl_mcp.runtime import TRANSPORT_STDIO, TRANSPORT_STREAMABLE_HTTP
from tripl_mcp.server import runtime_from_env


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRIPL_BASE_URL", raising=False)
    monkeypatch.delenv("TRIPL_API_KEY", raising=False)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://tripl.example.com/api/v1", "https://tripl.example.com"),
        ("https://tripl.example.com/api/v1/", "https://tripl.example.com"),
        ("  https://tripl.example.com/  ", "https://tripl.example.com"),
        ("http://localhost:8000", "http://localhost:8000"),
    ],
)
def test_the_base_url_is_normalised_like_the_cli_s(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setenv("TRIPL_BASE_URL", raw)
    monkeypatch.setenv("TRIPL_API_KEY", "tk_r_abc")

    runtime = runtime_from_env(TRANSPORT_STDIO)

    assert runtime.base_url == expected


def test_a_url_without_a_scheme_stops_startup_with_the_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRIPL_BASE_URL", "tripl.example.com")
    monkeypatch.setenv("TRIPL_API_KEY", "tk_r_abc")

    with pytest.raises(SystemExit) as excinfo:
        runtime_from_env(TRANSPORT_STDIO)

    message = str(excinfo.value.code)
    assert message.startswith("tripl-mcp: TRIPL_BASE_URL is not a valid tripl URL")
    assert "try https://tripl.example.com" in message


def test_a_control_character_stops_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIPL_BASE_URL", "https://tripl.example.com\nX=1")

    with pytest.raises(SystemExit, match="control character"):
        runtime_from_env(TRANSPORT_STREAMABLE_HTTP)


def test_a_missing_base_url_stops_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIPL_API_KEY", "tk_r_abc")

    with pytest.raises(SystemExit, match="TRIPL_BASE_URL environment variable is required"):
        runtime_from_env(TRANSPORT_STDIO)


def test_stdio_needs_a_key_and_http_never_holds_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIPL_BASE_URL", "https://tripl.example.com")
    with pytest.raises(SystemExit, match="TRIPL_API_KEY is required for stdio"):
        runtime_from_env(TRANSPORT_STDIO)

    monkeypatch.setenv("TRIPL_API_KEY", "tk_r_abc")
    assert runtime_from_env(TRANSPORT_STREAMABLE_HTTP).api_key is None
    assert runtime_from_env(TRANSPORT_STDIO).api_key == "tk_r_abc"
