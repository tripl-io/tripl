"""Pre-launch fixes: the API reference pages, the server's version, the NUL path
guard, the declared API key, and the alert-channel transport.

* ``/docs`` and ``/redoc`` rendered blank on every production install: the
  pages loaded their assets from a CDN and started from an inline script, both
  of which the SPA's policy refuses. They are now served from this instance
  under a policy of their own.
* The OpenAPI document said 0.1.0 on the 0.3.1 release; the usage ping and the
  System settings tile now read the same package version.
* An empty TELEMETRY_ENDPOINT was documented as turning the usage ping off; it
  has always meant the default endpoint.
* A NUL in a path parameter reached PostgreSQL and answered 500.
* The alert-channel client followed https -> http redirects, never pinned the
  vetted address, and read whole response bodies into stored error messages.
* A wrong sign-in password under 8 characters got a 422 naming the length rule
  instead of the neutral 401 every other wrong password gets.
* The member roster answers a page at a time; the web app read only the first
  page, so an organization's 201st member was missing everywhere.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
from datetime import UTC, datetime
from html.parser import HTMLParser
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from tripl import __version__, extensions
from tripl.api import deps
from tripl.api_docs import DOCS_CSP
from tripl.api_docs.pages import STATIC_DIR
from tripl.config import Settings, settings
from tripl.main import app
from tripl.middleware import SecurityHeadersMiddleware
from tripl.services import safe_http, telemetry_service, user_service
from tripl.services._alerting_test_send import classify_test_send_error
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_alerts_channels import _install_stub_transport
from tripl.worker.tasks import alerts_channels
from tripl.worker.tasks.implementation_tickets import _is_transient_tracker_error

# --------------------------------------------------------------------------- #
# /docs and /redoc
# --------------------------------------------------------------------------- #


class _PageAssets(HTMLParser):
    """Every script, stylesheet and spec URL a page loads, and any inline script."""

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.inline_scripts: list[str] = []
        self.openapi_urls: list[str] = []
        self._in_inline_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "script":
            if values.get("src"):
                self.urls.append(values["src"])
            else:
                self._in_inline_script = True
        elif tag == "link" and values.get("rel") == "stylesheet":
            self.urls.append(values["href"])
        if values.get("data-openapi-url"):
            self.openapi_urls.append(values["data-openapi-url"])
        if tag == "redoc":
            self.openapi_urls.append(values["spec-url"])

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_inline_script = False

    def handle_data(self, data: str) -> None:
        if self._in_inline_script and data.strip():
            self.inline_scripts.append(data)


def _directive(policy: str, name: str) -> list[str]:
    for part in policy.split(";"):
        tokens = part.split()
        if tokens and tokens[0] == name:
            return tokens[1:]
    return []


@pytest.mark.asyncio
@pytest.mark.parametrize("page", ["/docs", "/redoc"])
async def test_the_reference_pages_load_only_what_their_policy_allows(
    anon_client: AsyncClient, page: str
) -> None:
    """Every asset same-origin and served here, no inline script, a policy of its own."""
    response = await anon_client.get(page)

    assert response.status_code == 200
    assert response.headers["content-security-policy"] == DOCS_CSP
    assets = _PageAssets()
    assets.feed(response.text)
    assert assets.inline_scripts == []
    assert assets.urls, "the page loads nothing; the parser missed its assets"
    for url in assets.urls:
        assert url.startswith("/") and not url.startswith("//"), url
        served = await anon_client.get(url)
        assert served.status_code == 200, url
    assert assets.openapi_urls == [app.openapi_url]
    assert (await anon_client.get(assets.openapi_urls[0])).status_code == 200


@pytest.mark.asyncio
async def test_the_spa_policy_leaves_the_reference_pages_their_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The published image serves the SPA, whose policy blanked both pages.

    The suite's app is built without ``serve_frontend``, so the stack is wrapped
    in a header middleware built the way the image builds it.
    """
    monkeypatch.setattr(settings, "serve_frontend", True)
    monkeypatch.setattr(settings, "content_security_policy", "")
    production_like = SecurityHeadersMiddleware(app)

    transport = ASGITransport(app=production_like)
    async with AsyncClient(transport=transport, base_url="http://test") as served:
        spec = await served.get("/openapi.json")
        docs = await served.get("/docs")

    spa_policy = spec.headers["content-security-policy"]
    assert _directive(spa_policy, "script-src") == ["'self'"]
    assert spa_policy != DOCS_CSP
    assert docs.headers["content-security-policy"] == DOCS_CSP


def test_the_reference_policy_stays_strict() -> None:
    """Only what the two viewers need: no CDN, no inline or eval'd script."""
    assert _directive(DOCS_CSP, "script-src") == ["'self'"]
    assert _directive(DOCS_CSP, "connect-src") == ["'self'"]
    # ReDoc builds its search index in a worker it starts from a blob: URL.
    assert _directive(DOCS_CSP, "worker-src") == ["'self'", "blob:"]
    assert "https:" not in DOCS_CSP


def test_swagger_ui_starts_without_the_third_party_validator() -> None:
    init = (STATIC_DIR / "swagger-init.js").read_text(encoding="utf-8")
    assert "validatorUrl: null" in init
    assert "data-openapi-url" in init


def test_every_vendored_asset_matches_its_recorded_checksum() -> None:
    """``third-party.json`` says where each file came from; this keeps it true."""
    manifest = json.loads((STATIC_DIR / "third-party.json").read_text(encoding="utf-8"))
    for package in manifest["packages"]:
        assert package["license"]
        for name in package["license_files"]:
            assert (STATIC_DIR / name).is_file(), name
        for name, digest in package["files"].items():
            assert hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest() == digest, name


# --------------------------------------------------------------------------- #
# One version
# --------------------------------------------------------------------------- #


def test_the_version_is_the_installed_package_version() -> None:
    try:
        expected = version("tripl-server")
    except PackageNotFoundError:
        expected = "0.0.0+unknown"
    assert __version__ == expected


def test_the_api_document_reports_the_package_version() -> None:
    assert app.openapi()["info"]["version"] == __version__


@pytest.mark.asyncio
async def test_the_usage_ping_reports_the_same_version() -> None:
    async with TestSessionLocal() as session:
        payload = await telemetry_service.build_payload(session, datetime.now(UTC))
    assert payload["version"] == __version__


@pytest.mark.asyncio
async def test_the_system_settings_carry_the_version_and_edition(client: AsyncClient) -> None:
    """GET and PATCH alike: the frontend writes the PATCH answer into its cache."""
    fetched = await client.get("/api/v1/settings")
    patched = await client.patch(
        "/api/v1/settings", json={"runtime": {"scan_row_limit_default": 7}}
    )

    for response in (fetched, patched):
        assert response.status_code == 200, response.text
        system = response.json()["system"]
        assert system["version"] == __version__
        assert system["edition"] == telemetry_service.edition()


# --------------------------------------------------------------------------- #
# Telemetry endpoint
# --------------------------------------------------------------------------- #


def test_an_empty_telemetry_endpoint_is_the_default_not_an_off_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The docs and the Runtime card promised "empty sends nothing"; it never did.

    ``env_ignore_empty`` reads "" as unset, and has to: Compose passes an unset
    TELEMETRY_ENDPOINT as "", so empty meaning off would silence every Compose
    install. The off switches are TELEMETRY_ENABLED=false and DO_NOT_TRACK=1.
    """
    monkeypatch.setenv("TELEMETRY_ENDPOINT", "")
    built = Settings(_env_file=None)
    assert built.telemetry_endpoint == Settings.model_fields["telemetry_endpoint"].default
    assert built.telemetry_endpoint

    monkeypatch.setattr(settings, "telemetry_endpoint", built.telemetry_endpoint)
    monkeypatch.setattr(settings, "telemetry_enabled", True)
    monkeypatch.setattr(settings, "do_not_track", False)
    assert telemetry_service.inactive_reason() is None


# --------------------------------------------------------------------------- #
# OpenAPI metadata
# --------------------------------------------------------------------------- #


def test_every_operation_tag_is_described_and_every_described_tag_is_used() -> None:
    if extensions.extensions():
        pytest.skip("an extension adds operations under tags of its own")
    schema = app.openapi()
    declared = [tag["name"] for tag in schema["tags"]]
    used = {
        tag
        for operations in schema["paths"].values()
        for operation in operations.values()
        if isinstance(operation, dict)
        for tag in operation.get("tags", ())
    }
    assert len(declared) == len(set(declared))
    assert used - set(declared) == set(), "operations under a tag main.py does not describe"
    assert set(declared) - used == set(), "a described tag no operation uses"


def test_the_api_key_is_declared_on_the_operations_that_take_it() -> None:
    schema = app.openapi()

    scheme = schema["components"]["securitySchemes"][deps.API_KEY_SCHEME.scheme_name]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"
    assert schema["paths"]["/api/v1/projects"]["get"]["security"] == [
        {deps.API_KEY_SCHEME.scheme_name: []}
    ]
    # Signing in takes no key.
    assert "security" not in schema["paths"]["/api/v1/auth/login"]["post"]


@pytest.mark.asyncio
async def test_a_declared_scheme_does_not_change_how_a_bad_key_is_answered(
    anon_client: AsyncClient,
) -> None:
    bad_key = await anon_client.get(
        "/api/v1/projects", headers={"Authorization": "Bearer tk_r_not-a-key"}
    )
    other_scheme = await anon_client.get(
        "/api/v1/projects", headers={"Authorization": "Basic b3BzOnRva2Vu"}
    )

    assert (bad_key.status_code, bad_key.json()) == (401, {"detail": "Invalid or expired API key"})
    assert (other_scheme.status_code, other_scheme.json()) == (
        401,
        {"detail": "Authentication required"},
    )


# --------------------------------------------------------------------------- #
# A NUL in the path
# --------------------------------------------------------------------------- #

_NUL = "a%00b"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        f"/api/v1/projects/{_NUL}/events",
        f"/api/v1/orgs/{_NUL}/projects",
        f"/api/v1/orgs/{_NUL}/settings",
        f"/{_NUL}",
    ],
)
async def test_a_nul_in_the_path_is_a_404_before_routing(
    anon_client: AsyncClient, path: str
) -> None:
    """Asserted as exactly 404, not "below 500": the suite runs on SQLite, where
    the NUL never fails, so only the status the guard answers proves it ran.

    Anonymous on purpose: a route behind authentication would otherwise answer
    401 first, which says nothing about whether the slug ever reaches a query.
    """
    response = await anon_client.get(path)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_no_documented_path_takes_a_nul_in_a_parameter(client: AsyncClient) -> None:
    templated = [path for path in app.openapi()["paths"] if "{" in path]
    assert len(templated) > 100, f"the walk found only {len(templated)} templated paths"

    answered = {}
    for template in templated:
        path = re.sub(r"\{[^}]+\}", _NUL, template)
        answered[template] = (await client.get(path)).status_code

    assert {template for template, status in answered.items() if status != 404} == set()


@pytest.mark.asyncio
async def test_a_path_without_a_nul_is_untouched(anon_client: AsyncClient) -> None:
    # /health probes the module-level engine, not the suite's database, so it
    # may answer 503 here; either way the route itself answered, which is all
    # the guard has to let through.
    response = await anon_client.get("/health")

    assert response.status_code in {200, 503}
    assert "status" in response.json()


# --------------------------------------------------------------------------- #
# Alert channels and trackers
# --------------------------------------------------------------------------- #

_HOOK = "https://hooks.example.test/in"


class _Pinned:
    """Stands in for ``safe_http.send_pinned``; records what it was asked to send."""

    def __init__(self, status: int, body: bytes = b"") -> None:
        self.answer = safe_http.HttpResponse(status=status, body=body)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        **kwargs: Any,
    ) -> safe_http.HttpResponse:
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "body": body, **kwargs}
        )
        return self.answer


class _NoOpener:
    def open(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("the urllib opener was used with public hosts required")


@pytest.fixture
def public_hosts_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "outbound_public_hosts_only", True)
    monkeypatch.setattr(alerts_channels, "_REDIRECT_SAFE_OPENER", _NoOpener())


def _pin(monkeypatch: pytest.MonkeyPatch, status: int, body: bytes = b"") -> _Pinned:
    fake = _Pinned(status, body)
    monkeypatch.setattr(safe_http, "send_pinned", fake)
    return fake


@pytest.mark.usefixtures("public_hosts_only")
def test_public_hosts_only_sends_through_the_pinned_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned = _pin(monkeypatch, 202, b'{"status": "success"}')

    status, answer = alerts_channels._post_json_with_status(
        _HOOK, {"text": "hi"}, {"X-Secret": "s3cret"}
    )

    assert (status, answer) == (202, {"status": "success"})
    (call,) = pinned.calls
    assert call["method"] == "POST"
    assert call["url"] == _HOOK
    assert json.loads(call["body"]) == {"text": "hi"}
    headers = {name.lower(): value for name, value in call["headers"].items()}
    assert headers["x-secret"] == "s3cret"
    assert headers["content-type"] == "application/json"
    assert headers["user-agent"] == f"tripl/{__version__}"
    assert call["max_response_bytes"] == alerts_channels._MAX_RESPONSE_BYTES


@pytest.mark.usefixtures("public_hosts_only")
def test_public_hosts_only_refuses_a_redirect_with_its_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin(monkeypatch, 302)

    with pytest.raises(ValueError, match="HTTP 302 from https://hooks.example.test") as caught:
        alerts_channels._get_json(_HOOK)

    assert "redirects are not followed" in str(caught.value)
    # The Test dialog reads the status off the cause chain, as on the urllib path.
    assert classify_test_send_error(caught.value) == ("http_status", 302)


@pytest.mark.usefixtures("public_hosts_only")
def test_public_hosts_only_keeps_the_error_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin(monkeypatch, 503, b"upstream down")

    with pytest.raises(ValueError) as caught:
        alerts_channels._post_json(_HOOK, {"text": "hi"})

    assert str(caught.value) == "HTTP 503 from https://hooks.example.test: upstream down"

    assert classify_test_send_error(caught.value) == ("http_status", 503)
    # The ticket worker retries a 5xx; it finds the status the same way.
    assert _is_transient_tracker_error(caught.value)


def test_an_http_destination_is_refused_in_either_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    for public_only in (False, True):
        monkeypatch.setattr(settings, "outbound_public_hosts_only", public_only)
        with pytest.raises(ValueError, match="must be an https URL"):
            alerts_channels._post_json("http://hooks.example.test/in", {"text": "hi"})


def test_an_error_body_is_bounded_in_the_stored_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """The message is stored on the delivery, audited and shown on Test."""
    url = "https://93.184.216.34/hook"
    _install_stub_transport(monkeypatch, {url: (500, {}, b"x" * (2 * 1024 * 1024))})

    with pytest.raises(ValueError) as caught:
        alerts_channels._post_json(url, {"text": "hi"})

    message = str(caught.value)
    prefix = "HTTP 500 from https://93.184.216.34: "
    assert message.startswith(prefix)
    assert len(message) - len(prefix) <= alerts_channels._MAX_DETAIL_CHARS
    assert message.endswith("...")


def test_telegrams_description_still_names_the_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://93.184.216.34/bot/sendMessage"
    body = json.dumps(
        {"ok": False, "error_code": 400, "description": "Bad Request: message is too long"}
    ).encode()
    _install_stub_transport(monkeypatch, {url: (400, {}, body)})

    with pytest.raises(ValueError) as caught:
        alerts_channels._post_json(url, {"text": "hi"})

    assert str(caught.value) == (
        "HTTP 400 from https://93.184.216.34: Bad Request: message is too long"
    )
    assert isinstance(caught.value.__cause__, urllib.error.HTTPError)


def test_an_oversized_answer_counts_as_no_json(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://93.184.216.34/hook"
    huge = json.dumps({"id": "x" * alerts_channels._MAX_RESPONSE_BYTES}).encode()
    _install_stub_transport(monkeypatch, {url: (200, {}, huge)})

    assert alerts_channels._post_json_with_status(url, {"text": "hi"}) == (200, None)


# --------------------------------------------------------------------------- #
# Sign-in answers every wrong password alike
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_short_wrong_password_gets_the_same_answer_as_any_wrong_one(
    anon_client: AsyncClient,
) -> None:
    registered = await anon_client.post(
        "/api/v1/auth/register",
        json={"email": "owner@example.com", "password": "Password123!"},
    )
    assert registered.status_code == 201, registered.text
    await anon_client.post("/api/v1/auth/logout")

    short = await anon_client.post(
        "/api/v1/auth/login", json={"email": "owner@example.com", "password": "short"}
    )
    long = await anon_client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "a-long-wrong-password-1"},
    )

    assert short.status_code == long.status_code == 401
    assert short.json() == long.json()


def test_the_login_request_documents_no_password_floor() -> None:
    login = app.openapi()["components"]["schemas"]["LoginRequest"]["properties"]["password"]
    assert "minLength" not in login


# --------------------------------------------------------------------------- #
# The roster, page by page
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_roster_pages_without_repeating_or_skipping_anyone(
    anon_client: AsyncClient,
) -> None:
    emails = [f"member{n}@example.com" for n in range(5)]
    for email in emails:
        response = await anon_client.post(
            "/api/v1/auth/register", json={"email": email, "password": "Password123!"}
        )
        assert response.status_code == 201, response.text

    seen: list[str] = []
    offset = 0
    while True:
        page = await anon_client.get("/api/v1/users", params={"limit": 2, "offset": offset})
        assert page.status_code == 200, page.text
        rows = page.json()
        seen.extend(row["email"] for row in rows)
        if len(rows) < 2:
            break
        offset += 2

    # Accounts made back to back can tie on created_at, and the id breaks the
    # tie: a stable order, but not necessarily the order of sign-up.
    assert len(seen) == len(set(seen))
    assert sorted(seen) == sorted(emails)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/v1/users", "/api/v1/orgs/{org}/members"])
async def test_the_roster_serves_the_page_size_the_app_reads_with(
    client: AsyncClient, path: str
) -> None:
    """The web app asks for ``ROSTER_PAGE_MAX`` rows at a time; past it is a 422, not a cut."""
    me = (await client.get("/api/v1/auth/me")).json()
    url = path.format(org=me["orgs"][0]["slug"])

    largest = await client.get(url, params={"limit": user_service.ROSTER_PAGE_MAX})
    too_large = await client.get(url, params={"limit": user_service.ROSTER_PAGE_MAX + 1})

    assert largest.status_code == 200, largest.text
    assert too_large.status_code == 422
