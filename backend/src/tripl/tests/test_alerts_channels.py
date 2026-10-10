"""Outbound transport guards for alert/webhook/tracker deliveries.

The SSRF check on a destination only covers the URL an operator saved. urllib's
default opener then follows a 3xx to any host, so these tests drive ``_post_json``
/ ``_get_json`` through a stub transport and assert that a redirect to an
internal address is refused *before* the second hop is issued, and that a hop to
a *public* host that changes origin does not carry the request's credentials.
"""

from __future__ import annotations

import io
import urllib.request
from http.client import HTTPMessage

import pytest

from tripl.worker.tasks import alerts_channels

_HOOK_URL = "https://webhook.example.test/hook"
_JIRA_URL = "https://jira.example.test/rest/api/3/issue/AB-1?fields=status"
# Literal IPs keep the private-host guard off DNS, so the tests stay offline.
# That applies to every *redirect target* below — a `.test` hostname there would
# fail the guard's closed DNS lookup instead of exercising what is under test.
_METADATA_URL = "https://169.254.169.254/latest/meta-data/"
_PRIVATE_URL = "https://10.0.0.5/internal"
_PUBLIC_URL = "https://93.184.216.34/hook"
_PUBLIC_MOVED_URL = "https://93.184.216.34/moved"
_PUBLIC_ALT_PORT_URL = "https://93.184.216.34:8443/hook"
_PUBLIC_HTTP_URL = "http://93.184.216.34/hook"
_SECRET_HEADERS = {
    "Authorization": "Basic b3BzOnRva2Vu",
    "X-Tripl-Signature": "s3cret",
    "Accept": "application/json",
}


class _StubResponse(io.BytesIO):
    def __init__(self, url: str, code: int, headers: HTTPMessage, body: bytes) -> None:
        super().__init__(body)
        self.url = url
        self.code = code
        self.status = code
        self.msg = "stub"
        self.reason = "stub"
        self.headers = headers

    def info(self) -> HTTPMessage:
        return self.headers

    def geturl(self) -> str:
        return self.url


class _StubTransport(urllib.request.HTTPSHandler, urllib.request.HTTPHandler):
    """Answers each hop from a scripted map and records what was requested.

    Subclassing *both* handlers matters: ``build_opener`` only drops a default
    handler when a supplied one is an instance of it, so an https-only stub
    would leave the real ``HTTPHandler`` registered ahead of ours and an http
    hop would leave the machine.
    """

    def __init__(self, responses: dict[str, tuple[int, dict[str, str], bytes]]) -> None:
        super().__init__()
        self.responses = responses
        self.requested: list[str] = []
        self.sent_headers: list[dict[str, str]] = []

    def https_open(self, req: urllib.request.Request) -> _StubResponse:
        self.requested.append(req.full_url)
        # header_items() is what urllib would put on the wire: the request's own
        # headers merged over the unredirected ones.
        self.sent_headers.append({name.lower(): value for name, value in req.header_items()})
        code, headers, body = self.responses[req.full_url]
        message = HTTPMessage()
        for name, value in headers.items():
            message[name] = value
        return _StubResponse(req.full_url, code, message, body)

    # Registered for http:// too, so a hop to http would be recorded, not sent.
    def http_open(self, req: urllib.request.Request) -> _StubResponse:
        return self.https_open(req)


def _install_stub_transport(
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[str, tuple[int, dict[str, str], bytes]],
) -> _StubTransport:
    handler = _StubTransport(responses)
    opener = urllib.request.build_opener(
        # An empty proxy map keeps a proxied CI environment from rewriting the hop.
        urllib.request.ProxyHandler({}),
        alerts_channels._ValidatingRedirectHandler,
        handler,
    )
    monkeypatch.setattr(alerts_channels, "_REDIRECT_SAFE_OPENER", opener)
    return handler


def test_post_json_rejects_redirect_to_link_local(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _install_stub_transport(
        monkeypatch,
        {
            _HOOK_URL: (302, {"Location": _METADATA_URL}, b""),
            _METADATA_URL: (200, {}, b'{"token": "leaked"}'),
        },
    )

    with pytest.raises(ValueError, match="private or internal address"):
        alerts_channels._post_json(_HOOK_URL, {"text": "hi"})

    assert handler.requested == [_HOOK_URL]


def test_get_json_rejects_redirect_to_private_host(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _install_stub_transport(
        monkeypatch,
        {
            _JIRA_URL: (302, {"Location": _PRIVATE_URL}, b""),
            _PRIVATE_URL: (200, {}, b'{"fields": {}}'),
        },
    )

    with pytest.raises(ValueError, match="private or internal address"):
        alerts_channels._get_json(_JIRA_URL)

    assert handler.requested == [_JIRA_URL]


def test_post_json_rejects_non_http_redirect_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    # urllib's built-in redirect check allows ftp:// through; alerting does not.
    handler = _install_stub_transport(
        monkeypatch,
        {_HOOK_URL: (302, {"Location": "ftp://files.example.test/secrets"}, b"")},
    )

    with pytest.raises(ValueError, match="must be an https URL"):
        alerts_channels._post_json(_HOOK_URL, {"text": "hi"})

    assert handler.requested == [_HOOK_URL]


def test_post_json_follows_redirect_to_public_host(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _install_stub_transport(
        monkeypatch,
        {
            _HOOK_URL: (302, {"Location": _PUBLIC_URL}, b""),
            _PUBLIC_URL: (200, {}, b'{"ok": true}'),
        },
    )

    assert alerts_channels._post_json(_HOOK_URL, {"text": "hi"}) == {"ok": True}
    assert handler.requested == [_HOOK_URL, _PUBLIC_URL]


def test_post_json_strips_credentials_on_cross_origin_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The public-host allowance means a merely compromised destination can answer
    # 302 and harvest the operator's tracker credentials unless they are dropped.
    handler = _install_stub_transport(
        monkeypatch,
        {
            _HOOK_URL: (302, {"Location": _PUBLIC_URL}, b""),
            _PUBLIC_URL: (200, {}, b'{"ok": true}'),
        },
    )

    assert alerts_channels._post_json(_HOOK_URL, {"text": "hi"}, _SECRET_HEADERS) == {"ok": True}

    assert handler.requested == [_HOOK_URL, _PUBLIC_URL]
    first, second = handler.sent_headers
    assert first["authorization"] == _SECRET_HEADERS["Authorization"]
    assert first["x-tripl-signature"] == _SECRET_HEADERS["X-Tripl-Signature"]
    assert "authorization" not in second
    assert "x-tripl-signature" not in second
    assert second["accept"] == "application/json"


def test_get_json_refuses_a_scheme_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A downgrade used to be followed with the credentials stripped. It is now
    # not followed at all: over plain HTTP nothing proves the next hop is the
    # host that was checked, so a public https destination's 302 could reach a
    # name that re-resolves to an internal address (DNS rebinding).
    handler = _install_stub_transport(
        monkeypatch,
        {
            _PUBLIC_URL: (302, {"Location": _PUBLIC_HTTP_URL}, b""),
            _PUBLIC_HTTP_URL: (200, {}, b'{"fields": {}}'),
        },
    )

    with pytest.raises(ValueError, match="must be an https URL"):
        alerts_channels._get_json(_PUBLIC_URL, _SECRET_HEADERS)

    assert handler.requested == [_PUBLIC_URL]


def test_get_json_keeps_credentials_on_same_origin_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _install_stub_transport(
        monkeypatch,
        {
            _PUBLIC_URL: (302, {"Location": "/moved"}, b""),
            _PUBLIC_MOVED_URL: (200, {}, b'{"fields": {}}'),
        },
    )

    assert alerts_channels._get_json(_PUBLIC_URL, _SECRET_HEADERS) == {"fields": {}}

    assert handler.requested == [_PUBLIC_URL, _PUBLIC_MOVED_URL]
    assert handler.sent_headers[1]["authorization"] == _SECRET_HEADERS["Authorization"]


def test_get_json_refuses_an_http_url_before_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every destination is validated as https and the fixed endpoints are
    # https, so the http -> https upgrade this used to allow had no caller.
    handler = _install_stub_transport(
        monkeypatch,
        {
            _PUBLIC_HTTP_URL: (302, {"Location": _PUBLIC_URL}, b""),
            _PUBLIC_URL: (200, {}, b'{"fields": {}}'),
        },
    )

    with pytest.raises(ValueError, match="must be an https URL"):
        alerts_channels._get_json(_PUBLIC_HTTP_URL, _SECRET_HEADERS)

    assert handler.requested == []


def test_post_json_strips_credentials_on_port_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same host and scheme, different port — still a different origin.
    handler = _install_stub_transport(
        monkeypatch,
        {
            _PUBLIC_URL: (302, {"Location": _PUBLIC_ALT_PORT_URL}, b""),
            _PUBLIC_ALT_PORT_URL: (200, {}, b'{"ok": true}'),
        },
    )

    assert alerts_channels._post_json(_PUBLIC_URL, {"text": "hi"}, _SECRET_HEADERS) == {"ok": True}

    assert handler.requested == [_PUBLIC_URL, _PUBLIC_ALT_PORT_URL]
    assert "authorization" not in handler.sent_headers[1]
