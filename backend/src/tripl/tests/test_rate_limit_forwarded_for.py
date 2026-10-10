"""With proxy trust on and no X-Real-IP, the limiter keys on the nearest proxy's entry.

A proxy that appends to X-Forwarded-For keeps whatever the client sent on the
left. Keying on the leftmost entry let a client write a new address there on
every request and get a fresh bucket each time; the rightmost entry is the one
the proxy itself appended.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from starlette.datastructures import Headers
from starlette.requests import Request

from tripl.config import settings
from tripl.middleware.rate_limit import _client_key


def _key(headers: dict[str, str]) -> str:
    request = SimpleNamespace(client=SimpleNamespace(host="10.0.0.1"), headers=Headers(headers))
    return _client_key(cast(Request, request), "login")


@pytest.fixture(autouse=True)
def _trust_proxy_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_trust_forwarded_for", True)


def test_an_invented_left_entry_does_not_open_a_new_bucket() -> None:
    keys = {
        _key({"x-forwarded-for": f"{spoofed}, 203.0.113.7"})
        for spoofed in ("1.1.1.1", "2.2.2.2", "3.3.3.3, 4.4.4.4")
    }
    assert keys == {"login:203.0.113.7"}


def test_a_single_entry_is_still_the_client() -> None:
    assert _key({"x-forwarded-for": "198.51.100.4"}) == "login:198.51.100.4"


def test_x_real_ip_still_wins() -> None:
    headers = {"x-real-ip": "192.0.2.9", "x-forwarded-for": "1.1.1.1, 203.0.113.7"}
    assert _key(headers) == "login:192.0.2.9"


def test_an_empty_last_entry_falls_back_to_the_socket_peer() -> None:
    assert _key({"x-forwarded-for": "1.1.1.1, "}) == "login:10.0.0.1"
