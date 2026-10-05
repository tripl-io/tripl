"""Sign in through the instance's own OpenID Connect provider (``OIDC_*``).

The provider is never reached: discovery is answered locally and the code
exchange plus id_token checks are replaced by the claims it would vouch for.
The flow is Google's (``test_google_sign_in``); these pin what is the
provider's own: its button, its domains (platform admins included), and
whether it creates accounts.
"""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import AsyncClient, Response
from pydantic import ValidationError
from sqlalchemy import select

from tripl.config import Settings, settings
from tripl.models.audit_log import AuditLog
from tripl.models.user import User
from tripl.services import oidc_login_service
from tripl.services.oidc import idp_http
from tripl.services.oidc.id_tokens import IdTokenClaims
from tripl.tests.conftest import TestSessionLocal

ISSUER = "https://idp.example.com"
START = "/api/v1/auth/oidc/start"
CALLBACK = "/api/v1/auth/oidc/callback"
DISCOVERY = idp_http.Discovery(
    issuer=ISSUER,
    authorization_endpoint=f"{ISSUER}/authorize",
    token_endpoint=f"{ISSUER}/token",
    jwks_uri=f"{ISSUER}/keys",
    token_endpoint_auth_methods=("client_secret_basic",),
    id_token_signing_algs=("RS256",),
)
EMPLOYEE = IdTokenClaims(
    issuer=ISSUER, subject="okta-1", email="ann@corp.example", email_verified=True, name="Ann"
)


class Provider:
    """Who the provider vouches for in the next callback."""

    def __init__(self) -> None:
        self.claims = EMPLOYEE

    def authenticate(self, **_kwargs: object) -> IdTokenClaims:
        return self.claims


@pytest.fixture(autouse=True)
def provider(monkeypatch: pytest.MonkeyPatch) -> Provider:
    monkeypatch.setattr(settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(settings, "oidc_client_id", "tripl")
    monkeypatch.setattr(settings, "oidc_client_secret", "s3cret")
    monkeypatch.setattr(settings, "oidc_button_label", "Sign in with Okta")
    monkeypatch.setattr(idp_http, "fetch_discovery", lambda _issuer: DISCOVERY)
    fake = Provider()
    monkeypatch.setattr(oidc_login_service, "_authenticate", fake.authenticate)
    return fake


async def _sign_in(client: AsyncClient, *, next_path: str = "/projects") -> Response:
    started = await client.get(START, params={"next": next_path})
    assert started.status_code == 302, started.text
    location = urlparse(started.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{ISSUER}/authorize"
    query = parse_qs(location.query)
    assert query["client_id"] == ["tripl"]
    assert query["redirect_uri"] == ["http://test/api/v1/auth/oidc/callback"]
    assert query["scope"] == ["openid email profile"]
    assert query["code_challenge_method"] == ["S256"]
    assert "prompt" not in query
    return await client.get(CALLBACK, params={"code": "c-1", "state": query["state"][0]})


def _landed(resp: Response) -> str:
    assert resp.status_code == 302, resp.text
    return str(resp.headers["location"])


async def _user(email: str) -> User | None:
    async with TestSessionLocal() as session:
        user: User | None = await session.scalar(select(User).where(User.email == email))
        return user


async def test_the_button_says_what_the_operator_named_it(anon_client: AsyncClient) -> None:
    status = (await anon_client.get("/api/v1/auth/status")).json()
    assert status["oidc_sign_in"] is True
    assert status["oidc_button_label"] == "Sign in with Okta"


async def test_without_a_client_there_is_no_button(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "oidc_client_secret", "")
    status = (await anon_client.get("/api/v1/auth/status")).json()
    assert status["oidc_sign_in"] is False
    assert status["oidc_button_label"] is None
    assert _landed(await anon_client.get(START)) == "http://test/auth?sso_error=sso_unavailable"


async def test_a_first_sign_in_creates_the_account_and_audits_it(
    anon_client: AsyncClient,
) -> None:
    assert _landed(await _sign_in(anon_client)) == "http://test/projects"

    me = (await anon_client.get("/api/v1/auth/me")).json()
    assert me["email"] == "ann@corp.example"
    async with TestSessionLocal() as session:
        actions = list(
            await session.scalars(
                select(AuditLog.payload).where(AuditLog.action == "user.oidc_sign_in")
            )
        )
    assert actions == [{"issuer": ISSUER}]


async def test_an_existing_account_signs_in(client: AsyncClient, provider: Provider) -> None:
    """``client`` registered test@example.com with a password."""
    provider.claims = replace(EMPLOYEE, email="TEST@example.com")
    client.cookies.clear()
    assert _landed(await _sign_in(client, next_path="/")) == "http://test/"
    assert (await client.get("/api/v1/auth/me")).json()["email"] == "test@example.com"


async def test_allowed_domains_hold_for_platform_admins_too(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> None:
    """Unlike Google's: the instance's own provider vouches for whoever it lets
    in, so no account — the first, platform admin, included — is let past the list."""
    monkeypatch.setattr(settings, "oidc_allowed_domains", ["corp.example"])
    provider.claims = replace(EMPLOYEE, email="test@example.com")
    admin = await _user("test@example.com")
    assert admin is not None and admin.is_platform_admin
    client.cookies.clear()
    resp = await _sign_in(client)
    assert _landed(resp) == "http://test/auth?sso_error=email_domain_not_allowed"

    provider.claims = EMPLOYEE
    assert _landed(await _sign_in(client)) == "http://test/projects"


async def test_without_auto_create_only_existing_accounts_sign_in(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> None:
    monkeypatch.setattr(settings, "oidc_auto_create_users", False)
    client.cookies.clear()
    resp = await _sign_in(client)
    assert _landed(resp) == "http://test/auth?sso_error=signup_closed"
    assert await _user("ann@corp.example") is None

    provider.claims = replace(EMPLOYEE, email="test@example.com")
    assert _landed(await _sign_in(client)) == "http://test/projects"


async def test_an_unverified_address_is_refused(
    anon_client: AsyncClient, provider: Provider
) -> None:
    provider.claims = replace(EMPLOYEE, email_verified=False)
    resp = await _sign_in(anon_client)
    assert _landed(resp) == "http://test/auth?sso_error=email_not_verified"
    assert await _user("ann@corp.example") is None


def test_the_issuer_must_be_https_and_the_scopes_ask_for_an_email() -> None:
    with pytest.raises(ValidationError, match="OIDC_ISSUER must be an https:// URL"):
        Settings(oidc_issuer="http://idp.internal")
    with pytest.raises(ValidationError, match="OIDC_SCOPES must include email"):
        Settings(oidc_scopes="openid profile")
    assert Settings(oidc_scopes=" openid   email groups ").oidc_scopes == "openid email groups"
