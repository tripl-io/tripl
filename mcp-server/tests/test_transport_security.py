"""streamable-http behind a reverse proxy: ``--allowed-host`` instead of a 421.

On a loopback bind the MCP SDK checks the Host header and accepts only the
loopback names, so a proxy that forwards the public Host (Caddy and Traefik by
default) got ``421 Invalid Host header`` on every request, and the only way out
was ``--host 0.0.0.0``, which turns the check off. ``--allowed-host`` and
``--allowed-origin`` add names to the SDK's list instead of replacing it.

The checks themselves are the SDK's: these tests feed its own middleware the
settings this server builds, rather than re-implementing what it accepts.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.requests import Request

from tripl_mcp import runtime as runtime_module
from tripl_mcp.server import (
    LOOPBACK_HOSTS,
    LOOPBACK_ORIGINS,
    build_server,
    main,
    transport_security,
)

PUBLIC_HOST = "mcp.example.com"


def _verdict(
    settings: TransportSecuritySettings, host: str, origin: str | None = None
) -> int | None:
    """The status the SDK answers a GET with, or None when it lets it through."""
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    request = Request({"type": "http", "method": "GET", "path": "/mcp", "headers": headers})
    response = asyncio.run(TransportSecurityMiddleware(settings).validate_request(request))
    return None if response is None else response.status_code


def _sdk_default_on_loopback() -> TransportSecuritySettings:
    """What the SDK builds for itself on a loopback bind when given nothing."""
    mcp = build_server()
    mcp.streamable_http_app(host="127.0.0.1")
    settings = mcp.session_manager.security_settings
    assert settings is not None, "the SDK no longer protects a loopback bind by default"
    return settings


def test_no_flag_leaves_the_sdk_default_in_place() -> None:
    assert transport_security([], []) is None


def test_the_loopback_names_kept_are_the_sdks_own() -> None:
    """Passing any settings REPLACES the SDK's default, so ours must repeat it."""
    default = _sdk_default_on_loopback()
    assert tuple(default.allowed_hosts) == LOOPBACK_HOSTS
    assert tuple(default.allowed_origins) == LOOPBACK_ORIGINS


def test_the_default_refuses_a_forwarded_public_host() -> None:
    """The failure the flag exists for: 421, and only a loopback Host gets through."""
    default = _sdk_default_on_loopback()
    assert _verdict(default, PUBLIC_HOST) == 421
    assert _verdict(default, "127.0.0.1:8765") is None


def test_an_allowed_host_passes_and_loopback_still_does() -> None:
    settings = transport_security([PUBLIC_HOST], [])
    assert settings is not None
    assert _verdict(settings, PUBLIC_HOST) is None
    assert _verdict(settings, "127.0.0.1:8765") is None
    # Still a DNS-rebinding guard: a name nobody listed is refused.
    assert _verdict(settings, "attacker.example:8765") == 421


def test_a_port_wildcard_matches_any_port_of_that_host() -> None:
    settings = transport_security([f"{PUBLIC_HOST}:*"], [])
    assert settings is not None
    assert _verdict(settings, f"{PUBLIC_HOST}:8443") is None


def test_an_allowed_origin_passes_and_another_is_refused() -> None:
    settings = transport_security([PUBLIC_HOST], ["https://agents.example.com"])
    assert settings is not None
    assert _verdict(settings, PUBLIC_HOST, "https://agents.example.com") is None
    assert _verdict(settings, PUBLIC_HOST, "https://attacker.example") == 403


def test_main_hands_the_settings_to_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(self: MCPServer[Any], transport: str = "stdio", **kwargs: Any) -> None:
        captured.update(kwargs, transport=transport)

    monkeypatch.setattr(MCPServer, "run", fake_run)
    # main() configures the process runtime; let monkeypatch put it back.
    monkeypatch.setattr(runtime_module, "_runtime", None)
    monkeypatch.setenv("TRIPL_BASE_URL", "https://tripl.example.com")
    monkeypatch.setattr(
        sys,
        "argv",
        ["tripl-mcp", "--transport", "streamable-http", "--allowed-host", PUBLIC_HOST],
    )

    main()

    assert captured["transport"] == "streamable-http"
    assert captured["host"] == "127.0.0.1"
    settings = captured["transport_security"]
    assert isinstance(settings, TransportSecuritySettings)
    assert settings.allowed_hosts == [*LOOPBACK_HOSTS, PUBLIC_HOST]


def test_main_without_the_flag_passes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(self: MCPServer[Any], transport: str = "stdio", **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(MCPServer, "run", fake_run)
    monkeypatch.setattr(runtime_module, "_runtime", None)
    monkeypatch.setenv("TRIPL_BASE_URL", "https://tripl.example.com")
    monkeypatch.setattr(sys, "argv", ["tripl-mcp", "--transport", "streamable-http"])

    main()

    assert captured["transport_security"] is None
