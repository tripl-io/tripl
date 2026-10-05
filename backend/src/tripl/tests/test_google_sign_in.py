"""Sign in with Google, the instance-wide OAuth client.

Google itself is never reached: discovery is answered locally and the code
exchange plus id_token checks (``tripl.services.oidc``) are
replaced by the claims Google would vouch for.
"""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select

from tripl.config import REGISTRATION_DISABLED, settings
from tripl.models.user import User
from tripl.services import google_login_service
from tripl.services.oidc import idp_http
from tripl.services.oidc.id_tokens import IdTokenClaims
from tripl.tests._tenancy import use_public_demo
from tripl.tests.conftest import TestSessionLocal

START = "/api/v1/auth/google/start"
CALLBACK = "/api/v1/auth/google/callback"
DISCOVERY = idp_http.Discovery(
    issuer=google_login_service.GOOGLE_ISSUER,
    authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
    token_endpoint="https://oauth2.googleapis.com/token",
    jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
    token_endpoint_auth_methods=("client_secret_post",),
    id_token_signing_algs=("RS256",),
)
VISITOR = IdTokenClaims(
    issuer=google_login_service.GOOGLE_ISSUER,
    subject="g-1",
    email="visitor@gmail.com",
    email_verified=True,
    name="Visitor",
)


class Google:
    """Who Google vouches for in the next callback."""

    def __init__(self) -> None:
        self.claims = VISITOR

    def authenticate(self, **_kwargs: object) -> IdTokenClaims:
        return self.claims


@pytest.fixture(autouse=True)
def google(monkeypatch: pytest.MonkeyPatch) -> Google:
    monkeypatch.setattr(settings, "google_client_id", "client-1")
    monkeypatch.setattr(settings, "google_client_secret", "secret-1")
    monkeypatch.setattr(idp_http, "fetch_discovery", lambda _issuer: DISCOVERY)
    fake = Google()
    monkeypatch.setattr(google_login_service, "_authenticate", fake.authenticate)
    return fake


async def _sign_in(client: AsyncClient, *, next_path: str = "/projects") -> Response:
    started = await client.get(START, params={"next": next_path})
    assert started.status_code == 302, started.text
    location = urlparse(started.headers["location"])
    assert location.netloc == "accounts.google.com"
    query = parse_qs(location.query)
    assert query["client_id"] == ["client-1"]
    assert query["redirect_uri"] == ["http://test/api/v1/auth/google/callback"]
    assert query["code_challenge_method"] == ["S256"]
    return await client.get(CALLBACK, params={"code": "c-1", "state": query["state"][0]})


def _landed(resp: Response) -> str:
    assert resp.status_code == 302, resp.text
    return str(resp.headers["location"])


async def _user(email: str) -> User | None:
    async with TestSessionLocal() as session:
        user: User | None = await session.scalar(select(User).where(User.email == email))
        return user


async def _user_count() -> int:
    async with TestSessionLocal() as session:
        return int(await session.scalar(select(func.count()).select_from(User)) or 0)


async def test_a_first_sign_in_creates_the_account_and_signs_it_in(
    anon_client: AsyncClient,
) -> None:
    resp = await _sign_in(anon_client)

    assert _landed(resp) == "http://test/projects"
    me = await anon_client.get("/api/v1/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["email"] == "visitor@gmail.com"
    user = await _user("visitor@gmail.com")
    assert user is not None and user.email_verified_at is not None
    # The first account of an empty self-hosted instance owns it, as by password.
    assert user.is_platform_admin


async def test_an_existing_account_signs_in_without_a_new_one(
    client: AsyncClient, google: Google
) -> None:
    """``client`` registered test@example.com with a password."""
    google.claims = replace(VISITOR, email="Test@Example.com", name=None)
    before = await _user_count()
    client.cookies.clear()

    resp = await _sign_in(client, next_path="/")

    assert _landed(resp) == "http://test/"
    assert await _user_count() == before
    me = await client.get("/api/v1/auth/me")
    assert me.json()["email"] == "test@example.com"


@pytest.mark.parametrize(
    ("email", "verified", "code"),
    [
        ("visitor@gmail.com", False, "email_not_verified"),
        ("someone@other.org", True, "email_domain_not_allowed"),
    ],
)
async def test_what_google_does_not_vouch_for_is_refused(
    anon_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    google: Google,
    email: str,
    verified: bool,
    code: str,
) -> None:
    monkeypatch.setattr(settings, "google_allowed_domains", ["gmail.com"])
    google.claims = replace(VISITOR, email=email, email_verified=verified)

    resp = await _sign_in(anon_client)

    assert _landed(resp) == f"http://test/auth?sso_error={code}"
    assert await _user(email) is None


async def test_closed_sign_up_signs_nobody_new_up(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "registration_mode", REGISTRATION_DISABLED)
    client.cookies.clear()

    resp = await _sign_in(client)

    assert _landed(resp) == "http://test/auth?sso_error=signup_closed"
    assert await _user("visitor@gmail.com") is None


async def test_a_forged_or_missing_state_is_refused(anon_client: AsyncClient) -> None:
    started = await anon_client.get(START)
    assert started.status_code == 302
    resp = await anon_client.get(CALLBACK, params={"code": "c-1", "state": "not-the-state"})
    assert _landed(resp) == "http://test/auth?sso_error=invalid_state"
    anon_client.cookies.clear()
    resp = await anon_client.get(CALLBACK, params={"code": "c-1", "state": "anything"})
    assert _landed(resp) == "http://test/auth?sso_error=invalid_state"


async def test_without_a_client_there_is_no_google(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "google_client_secret", "")
    assert _landed(await anon_client.get(START)) == "http://test/auth?sso_error=sso_unavailable"
    status = await anon_client.get("/api/v1/auth/status")
    assert status.json()["google_sign_in"] is False


async def test_a_public_demo_signs_up_with_google_only(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_public_demo(monkeypatch)
    status = (await anon_client.get("/api/v1/auth/status")).json()
    assert status["google_sign_in"] is True
    assert status["public_demo"] is True

    resp = await anon_client.post(
        "/api/v1/auth/register",
        json={"email": "typed@example.com", "password": "Password123!", "name": "T"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == (
        "This public demo does not take password sign-ups; sign in with Google."
    )
    _landed(await _sign_in(anon_client))
    assert await _user("visitor@gmail.com") is not None
