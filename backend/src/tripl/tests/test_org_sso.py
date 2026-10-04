"""Organization OIDC single sign-on (F20, GH #273).

* the settings are an OWNER's (an admin and a member get 403, a stranger 404);
  the client secret is write-only and encrypted; every change is audited
  without it;
* a domain is proven by DNS TXT (the resolver is patched); a domain verified
  by one organization is 409 for another; enabling needs a verified domain;
* the issuer must be https, and on a hosted instance public — at save and at
  use (a name that re-resolves to a private address is refused);
* sign-in against a fake provider (``_fake_idp``): the first sign-in creates
  the account (JIT), a linked identity signs straight in, an existing account
  is linked only after the person confirms FROM A SESSION OF THAT ACCOUNT (the
  takeover of unverified accounts and removed members:
  ``test_org_sso_accounts.py``); an unverified or foreign-domain
  email, a bad nonce / audience / issuer / algorithm and an expired token are
  refused; the state is single use and bound to the browser; ``next`` never
  leaves the origin;
* "SSO required" refuses a member's password session (403 naming where to
  sign in), exempts owners' sessions, revokes the organization's non-SSO API
  keys when turned on (owners' too) and refuses such keys; removing a member
  drops their identity and keeps them out.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select

from tripl import crypto
from tripl.config import settings
from tripl.main import app
from tripl.models.api_key import ApiKey
from tripl.models.audit_log import AuditLog
from tripl.models.org_sso import OrgSsoConfig, UserSsoIdentity
from tripl.models.user import User
from tripl.models.user_session import UserSession
from tripl.services import org_sso_service
from tripl.services.oidc import idp_http
from tripl.services.oidc.flow import safe_next
from tripl.tests._fake_idp import CLIENT_ID, CLIENT_SECRET, ISSUER, KID, FakeIdp
from tripl.tests._members import add_org_member
from tripl.tests.conftest import TestSessionLocal

PASSWORD = "Password123!"
API = "/api/v1"
ACME = "acme"
DOMAIN = "acme.example.com"
SSO_URL = f"{API}/orgs/{ACME}/sso"
DOMAINS_URL = f"{SSO_URL}/domains"
START_URL = f"{API}/auth/sso/{ACME}/start"
CALLBACK_URL = f"{API}/auth/sso/{ACME}/callback"


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class People:
    """``root`` registers first: owner of the default organization, platform admin."""

    def __init__(self) -> None:
        self.clients: dict[str, AsyncClient] = {}
        self.ids: dict[str, uuid.UUID] = {}

    async def register(self, name: str, email: str) -> None:
        client = _new_client()
        resp = await client.post(
            f"{API}/auth/register", json={"email": email, "password": PASSWORD, "name": name}
        )
        assert resp.status_code == 201, resp.text
        self.clients[name] = client
        self.ids[name] = uuid.UUID(resp.json()["id"])

    def __getitem__(self, name: str) -> AsyncClient:
        return self.clients[name]

    async def aclose(self) -> None:
        for client in self.clients.values():
            await client.aclose()


@pytest.fixture
async def people() -> AsyncIterator[People]:
    crowd = People()
    for name, email in (
        ("root", "root@example.com"),
        ("bob", "bob@example.com"),
        ("mia", f"mia@{DOMAIN}"),
        ("carol", f"carol@{DOMAIN}"),
        ("stranger", "stranger@example.com"),
    ):
        await crowd.register(name, email)
    try:
        yield crowd
    finally:
        await crowd.aclose()


@pytest.fixture
async def acme(people: People) -> uuid.UUID:
    """``acme``: root owns it, bob is an admin, mia a member. carol is not in it."""
    created = await people["root"].post(f"{API}/orgs", json={"slug": ACME, "name": "Acme"})
    assert created.status_code == 201, created.text
    org_id = uuid.UUID(created.json()["id"])
    async with TestSessionLocal() as session:
        await add_org_member(session, people.ids["bob"], "admin", org_id=org_id)
        await add_org_member(session, people.ids["mia"], "member", org_id=org_id)
    return org_id


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> FakeIdp:
    fake = FakeIdp()
    monkeypatch.setattr(idp_http, "_send", fake.send)
    return fake


@pytest.fixture
def encryption_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "encryption_key", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


def _txt(monkeypatch: pytest.MonkeyPatch, records: dict[str, list[str]]) -> list[str]:
    """Patch the DNS lookup to answer ``records``; returns the names looked up."""
    asked: list[str] = []

    def lookup(name: str) -> list[str]:
        asked.append(name)
        return records.get(name, [])

    monkeypatch.setattr(org_sso_service, "_lookup_txt", lookup)
    return asked


async def _verified_domain(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, domain: str = DOMAIN, org: str = ACME
) -> dict[str, Any]:
    url = f"{API}/orgs/{org}/sso/domains"
    added = await client.post(url, json={"domain": domain})
    assert added.status_code == 201, added.text
    body = added.json()
    _txt(monkeypatch, {body["txt_record_name"]: [body["txt_record_value"]]})
    verified = await client.post(f"{url}/{body['id']}/verify")
    assert verified.status_code == 200, verified.text
    assert verified.json()["verified"] is True
    result: dict[str, Any] = verified.json()
    return result


def _config(**extra: Any) -> dict[str, Any]:
    return {
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        **extra,
    }


async def _enable(
    people: People, monkeypatch: pytest.MonkeyPatch, *, required: bool = False
) -> dict[str, Any]:
    await _verified_domain(people["root"], monkeypatch)
    saved = await people["root"].put(SSO_URL, json=_config(enabled=True, sso_required=required))
    assert saved.status_code == 200, saved.text
    body: dict[str, Any] = saved.json()
    return body


async def _sign_in(
    client: AsyncClient, idp: FakeIdp, *, next_path: str | None = None, **claims: Any
) -> Response:
    """Start, "log in" at the provider with ``claims``, and call back. The callback's answer."""
    params = {} if next_path is None else {"next": next_path}
    started = await client.get(START_URL, params=params)
    assert started.status_code == 302, started.text
    code, state = idp.authorize(started.headers["location"], **claims)
    return await client.get(CALLBACK_URL, params={"code": code, "state": state})


def _sso_error(resp: Response) -> str | None:
    assert resp.status_code == 302, resp.text
    query = parse_qs(urlparse(resp.headers["location"]).query)
    return query.get("sso_error", [None])[0]


async def _audit(action: str) -> list[AuditLog]:
    async with TestSessionLocal() as session:
        return list(
            (await session.scalars(select(AuditLog).where(AuditLog.action == action))).all()
        )


async def _user_id(email: str) -> uuid.UUID | None:
    async with TestSessionLocal() as session:
        found: uuid.UUID | None = await session.scalar(select(User.id).where(User.email == email))
        return found


# ── the settings ────────────────────────────────────────────────────────────


async def test_only_an_owner_reads_or_changes_sso(people: People, acme: uuid.UUID) -> None:
    own = await people["root"].get(SSO_URL)
    assert own.status_code == 200, own.text
    assert own.json()["configured"] is False
    assert own.json()["redirect_uri"].endswith(f"/api/v1/auth/sso/{ACME}/callback")
    assert own.json()["login_url"] == START_URL

    for name in ("bob", "mia"):
        for method, url, body in (
            ("GET", SSO_URL, None),
            ("PUT", SSO_URL, _config()),
            ("GET", DOMAINS_URL, None),
            ("POST", DOMAINS_URL, {"domain": DOMAIN}),
        ):
            resp = await people[name].request(method, url, json=body)
            assert resp.status_code == 403, f"{name} {method} {url}: {resp.text}"
    stranger = await people["stranger"].get(SSO_URL)
    assert stranger.status_code == 404, stranger.text


async def test_the_client_secret_is_write_only_and_encrypted(
    people: People, acme: uuid.UUID, encryption_key: None
) -> None:
    root = people["root"]
    missing = await root.put(SSO_URL, json={"issuer": ISSUER, "client_id": CLIENT_ID})
    assert missing.status_code == 422, missing.text

    saved = await root.put(SSO_URL, json=_config())
    assert saved.status_code == 200, saved.text
    assert saved.json()["client_secret_configured"] is True
    assert saved.json()["enabled"] is False
    assert CLIENT_SECRET not in saved.text
    read = await root.get(SSO_URL)
    assert read.json()["client_secret_configured"] is True
    assert CLIENT_SECRET not in read.text

    # Omitted: the stored one stays.
    kept = await root.put(SSO_URL, json={"issuer": ISSUER, "client_id": "renamed"})
    assert kept.status_code == 200, kept.text
    async with TestSessionLocal() as session:
        config = await session.scalar(
            select(OrgSsoConfig).where(OrgSsoConfig.organization_id == acme)
        )
        assert config is not None
        assert config.client_secret_encrypted != CLIENT_SECRET
        assert org_sso_service.client_secret(config) == CLIENT_SECRET
        assert config.client_id == "renamed"

    rows = await _audit("org.sso.update")
    assert len(rows) == 2
    assert all(row.organization_id == acme for row in rows)
    assert all(CLIENT_SECRET not in str(row.payload) for row in rows)
    assert (
        "client_secret" in rows[0].payload["changed"]
        or "client_secret" in (rows[1].payload["changed"])
    )


async def test_a_domain_is_verified_through_dns(
    people: People, acme: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = people["root"]
    added = await root.post(DOMAINS_URL, json={"domain": " ACME.Example.com. "})
    assert added.status_code == 201, added.text
    domain = added.json()
    assert domain["domain"] == DOMAIN
    assert domain["verified"] is False
    assert domain["txt_record_name"] == f"_tripl-verification.{DOMAIN}"
    assert domain["txt_record_value"].startswith("tripl-verification=")
    again = await root.post(DOMAINS_URL, json={"domain": DOMAIN})
    assert again.status_code == 409, again.text
    bad = await root.post(DOMAINS_URL, json={"domain": "not a domain"})
    assert bad.status_code == 422, bad.text

    verify_url = f"{DOMAINS_URL}/{domain['id']}/verify"
    asked = _txt(monkeypatch, {domain["txt_record_name"]: ["tripl-verification=wrong"]})
    pending = await root.post(verify_url)
    assert pending.status_code == 200, pending.text
    assert pending.json()["verified"] is False
    assert asked == [f"_tripl-verification.{DOMAIN}"]
    assert await _audit("org.sso.domain_verify") == []

    _txt(monkeypatch, {domain["txt_record_name"]: ["v=spf1 -all", domain["txt_record_value"]]})
    done = await root.post(verify_url)
    assert done.json()["verified"] is True
    assert done.json()["verified_at"] is not None
    assert len(await _audit("org.sso.domain_verify")) == 1
    listed = await root.get(DOMAINS_URL)
    assert [d["domain"] for d in listed.json()] == [DOMAIN]


async def test_a_domain_verified_by_another_organization_is_409(
    people: People, acme: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = people["root"]
    other = await root.post(f"{API}/orgs", json={"slug": "globex", "name": "Globex"})
    assert other.status_code == 201, other.text
    globex_domains = f"{API}/orgs/globex/sso/domains"
    # Globex claims it first but never proves it; Acme proves it.
    claimed = await root.post(globex_domains, json={"domain": DOMAIN})
    assert claimed.status_code == 201, claimed.text
    await _verified_domain(root, monkeypatch)

    refused = await root.post(f"{globex_domains}/{claimed.json()['id']}/verify")
    assert refused.status_code == 409, refused.text
    fresh = await root.post(globex_domains, json={"domain": DOMAIN})
    assert fresh.status_code == 409, fresh.text


async def test_enabling_needs_a_verified_domain(
    people: People, acme: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = people["root"]
    refused = await root.put(SSO_URL, json=_config(enabled=True))
    assert refused.status_code == 409, refused.text
    required_off = await root.put(SSO_URL, json=_config(enabled=False, sso_required=True))
    assert required_off.status_code == 422, required_off.text

    domain = await _verified_domain(root, monkeypatch)
    enabled = await root.put(SSO_URL, json=_config(enabled=True))
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["enabled"] is True
    # The last verified domain of an enabled SSO stays.
    last = await root.delete(f"{DOMAINS_URL}/{domain['id']}")
    assert last.status_code == 409, last.text


async def test_the_issuer_must_be_https_and_public_when_hosted(
    people: People, acme: uuid.UUID, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = people["root"]
    plain = await root.put(SSO_URL, json=_config(issuer="http://idp.example.com"))
    assert plain.status_code == 422, plain.text

    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    private = await root.put(SSO_URL, json=_config(issuer="https://10.0.0.5"))
    assert private.status_code == 422, private.text
    assert "private" in private.text
    monkeypatch.setattr(settings, "deployment_mode", "self_hosted")

    await _enable(people, monkeypatch)
    # Saved while public; now the name resolves inside. Refused at use, hosted.
    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_a, **_k: [(2, 1, 6, "", ("10.0.0.5", 0))],
    )
    async with _new_client() as browser:
        started = await browser.get(START_URL)
    assert _sso_error(started) == "idp_error"
    assert idp.requests == []

    probe = await root.post(f"{SSO_URL}/test")
    assert probe.status_code == 200, probe.text
    assert probe.json()["ok"] is False
    assert probe.json()["error_code"] == "idp_private_host"
    # A fixed text: nothing of the host or of what answered.
    assert "10.0.0.5" not in probe.text


async def test_the_connection_test_checks_the_discovery_document(
    people: People, acme: uuid.UUID, idp: FakeIdp
) -> None:
    root = people["root"]
    unconfigured = await root.post(f"{SSO_URL}/test")
    assert unconfigured.status_code == 409, unconfigured.text
    await root.put(SSO_URL, json=_config())
    ok = await root.post(f"{SSO_URL}/test")
    assert ok.json()["ok"] is True, ok.text
    assert ok.json()["token_endpoint"] == f"{ISSUER}/token"
    assert ok.json()["token_endpoint_auth_method"] == "client_secret_basic"

    idp.discovery_overrides["issuer"] = "https://other.example.com"
    mismatch = await root.post(f"{SSO_URL}/test")
    assert mismatch.json()["ok"] is False
    assert mismatch.json()["error_code"] == "idp_issuer_mismatch"


def test_idp_requests_refuse_redirects_oversize_and_plain_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(idp_http, "_send", lambda *_a: idp_http.HttpResponse(status=302, body=b""))
    with pytest.raises(idp_http.IdpError) as redirect:
        idp_http.fetch_discovery(ISSUER)
    assert redirect.value.code == "idp_redirect"

    big = b"{" + b" " * (idp_http.MAX_RESPONSE_BYTES + 1) + b"}"
    monkeypatch.setattr(idp_http, "_send", lambda *_a: idp_http.HttpResponse(status=200, body=big))
    with pytest.raises(idp_http.IdpError) as oversize:
        idp_http.fetch_discovery(ISSUER)
    assert oversize.value.code == "idp_response_too_large"

    with pytest.raises(idp_http.IdpError) as insecure:
        idp_http.fetch_discovery("http://idp.example.com")
    assert insecure.value.code == "idp_insecure_url"


# ── signing in ──────────────────────────────────────────────────────────────


async def test_discovery_names_the_organization_of_a_verified_domain(
    people: People, acme: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _new_client() as anon:
        before = await anon.get(f"{API}/auth/sso/discover", params={"email": f"x@{DOMAIN}"})
        assert before.json() == {"orgs": []}
        await _enable(people, monkeypatch)
        found = await anon.get(
            f"{API}/auth/sso/discover", params={"email": "Someone@ACME.example.com"}
        )
        assert found.json() == {"orgs": [{"slug": ACME, "name": "Acme", "login_url": START_URL}]}
        other = await anon.get(f"{API}/auth/sso/discover", params={"email": "x@example.org"})
        assert other.json() == {"orgs": []}


async def test_the_first_sign_in_creates_a_verified_member(
    people: People, acme: uuid.UUID, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable(people, monkeypatch)
    async with _new_client() as browser:
        done = await _sign_in(browser, idp, next_path="/projects?tab=1")
        assert done.status_code == 302, done.text
        assert done.headers["location"] == "http://test/projects?tab=1"
        me = await browser.get(f"{API}/auth/me")
        assert me.status_code == 200, me.text
        assert me.json()["email"] == f"alice@{DOMAIN}"
        assert me.json()["email_verified"] is True
        assert me.json()["is_platform_admin"] is False
        assert {o["slug"]: o["role"] for o in me.json()["orgs"]} == {ACME: "member"}

    user_id = await _user_id(f"alice@{DOMAIN}")
    async with TestSessionLocal() as session:
        identity = await session.scalar(
            select(UserSsoIdentity).where(UserSsoIdentity.user_id == user_id)
        )
        assert identity is not None
        assert (identity.issuer, identity.subject, identity.organization_id) == (
            ISSUER,
            "subject-1",
            acme,
        )
        user = await session.get(User, user_id)
        assert user is not None
        # A real hash of an unknown secret, not a marker: /auth/login spends
        # the same scrypt time on it as on any other account.
        assert user.password_hash.startswith("scrypt$")
        sso_session = await session.scalar(
            select(UserSession).where(UserSession.user_id == user_id)
        )
        assert sso_session is not None
        assert sso_session.auth_method == "sso"
        assert sso_session.sso_organization_id == acme
    provisioned = await _audit("user.sso_provision")
    assert [row.organization_id for row in provisioned] == [acme]


async def test_a_linked_identity_signs_straight_in(
    people: People, acme: uuid.UUID, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable(people, monkeypatch)
    async with _new_client() as first:
        await _sign_in(first, idp)
    async with TestSessionLocal() as session:
        users_before = await session.scalar(select(func.count()).select_from(User))
    async with _new_client() as second:
        # The provider's address changed; the identity is what counts.
        done = await _sign_in(second, idp, email=f"alice.new@{DOMAIN}")
        assert done.headers["location"] == "http://test/"
        me = await second.get(f"{API}/auth/me")
        assert me.json()["email"] == f"alice@{DOMAIN}"
    async with TestSessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == users_before
    assert len(await _audit("user.sso_login")) == 1


async def test_an_existing_account_is_linked_only_after_confirmation(
    people: People, acme: uuid.UUID, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable(people, monkeypatch)
    async with _new_client() as browser:
        done = await _sign_in(browser, idp, email=f"carol@{DOMAIN}", sub="carol-sub")
        location = urlparse(done.headers["location"])
        assert location.path == "/sso/link"
        ticket = parse_qs(location.query)["ticket"][0]
        # Nothing signed in yet.
        assert (await browser.get(f"{API}/auth/me")).status_code == 401

        preview = await browser.get(f"{API}/auth/sso/link", params={"ticket": ticket})
        assert preview.status_code == 200, preview.text
        assert preview.json()["email"] == f"carol@{DOMAIN}"
        assert preview.json()["org_slug"] == ACME
        assert preview.json()["org_name"] == "Acme"
        assert preview.json()["sign_in_required"] is True

        # The provider sign-in alone does not prove the account: whoever runs
        # the provider can name any address of the domain. The ticket stays.
        unproven = await browser.post(f"{API}/auth/sso/link", json={"ticket": ticket})
        assert unproven.status_code == 401, unproven.text
        # Nor does a session of ANOTHER account.
        other = await people["stranger"].post(f"{API}/auth/sso/link", json={"ticket": ticket})
        assert other.status_code == 401, other.text
        assert await _audit("user.sso_link") == []

        login = await browser.post(
            f"{API}/auth/login", json={"email": f"carol@{DOMAIN}", "password": PASSWORD}
        )
        assert login.status_code == 200, login.text
        password_cookie = browser.cookies.get(settings.session_cookie_name)
        proven = await browser.get(f"{API}/auth/sso/link", params={"ticket": ticket})
        assert proven.json()["sign_in_required"] is False

        confirmed = await browser.post(f"{API}/auth/sso/link", json={"ticket": ticket})
        assert confirmed.status_code == 200, confirmed.text
        # The password session that proved the account is replaced.
        assert browser.cookies.get(settings.session_cookie_name) != password_cookie
        assert confirmed.json()["next"] == "/"
        assert confirmed.json()["user"]["email"] == f"carol@{DOMAIN}"
        me = await browser.get(f"{API}/auth/me")
        assert {o["slug"]: o["role"] for o in me.json()["orgs"]}[ACME] == "member"

        reused = await browser.post(f"{API}/auth/sso/link", json={"ticket": ticket})
        assert reused.status_code == 400, reused.text

    linked = await _audit("user.sso_link")
    assert [
        (
            row.organization_id,
            row.payload["joined_organization"],
            row.payload["reclaimed_unverified_account"],
        )
        for row in linked
    ] == [(acme, True, False)]
    async with _new_client() as again:
        direct = await _sign_in(again, idp, email=f"carol@{DOMAIN}", sub="carol-sub")
        assert direct.headers["location"] == "http://test/"
        assert (await again.get(f"{API}/auth/me")).json()["email"] == f"carol@{DOMAIN}"


@pytest.mark.parametrize(
    ("claims", "code"),
    [
        ({"email_verified": False}, "email_not_verified"),
        ({"email_verified": "true"}, "email_not_verified"),
        ({"email": "alice@example.org"}, "email_domain_not_allowed"),
        ({"email": f"alice@sub.{DOMAIN}"}, "email_domain_not_allowed"),
        ({"email": None}, "email_missing"),
        ({"nonce": "not-the-nonce"}, "invalid_token"),
        ({"aud": "someone-else"}, "invalid_token"),
        ({"iss": "https://evil.example.com"}, "invalid_token"),
        ({"iat": int(time.time()) - 7200, "exp": int(time.time()) - 3600}, "invalid_token"),
        # OIDC Core 3.1.3.7: an azp that is present must be this client.
        ({"azp": "someone-else"}, "invalid_token"),
        ({"aud": [CLIENT_ID, "someone-else"]}, "invalid_token"),
        # A non-ASCII nonce is a refusal, not a 500.
        ({"nonce": "n\u00f6nce"}, "invalid_token"),
    ],
)
async def test_a_bad_sign_in_is_refused(
    people: People,
    acme: uuid.UUID,
    idp: FakeIdp,
    monkeypatch: pytest.MonkeyPatch,
    claims: dict[str, Any],
    code: str,
) -> None:
    await _enable(people, monkeypatch)
    async with _new_client() as browser:
        done = await _sign_in(browser, idp, **claims)
        assert _sso_error(done) == code
        assert "http://test/auth?" in done.headers["location"]
        assert (await browser.get(f"{API}/auth/me")).status_code == 401
    assert await _user_id(f"alice@{DOMAIN}") is None


@pytest.mark.parametrize(
    "factory",
    [
        lambda claims, _key: jwt.encode(claims, None, algorithm="none"),
        lambda claims, _key: jwt.encode(
            claims, CLIENT_SECRET, algorithm="HS256", headers={"kid": KID}
        ),
        lambda claims, key: jwt.encode(claims, key, algorithm="RS256", headers={"kid": "other"}),
    ],
    ids=["alg-none", "hs256-with-the-client-secret", "unknown-kid"],
)
async def test_an_id_token_not_signed_by_the_provider_is_refused(
    people: People, acme: uuid.UUID, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch, factory: Any
) -> None:
    await _enable(people, monkeypatch)
    idp.token_factory = factory
    async with _new_client() as browser:
        assert _sso_error(await _sign_in(browser, idp)) == "invalid_token"


async def test_the_state_is_single_use_and_bound_to_the_browser(
    people: People, acme: uuid.UUID, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable(people, monkeypatch)
    async with _new_client() as browser, _new_client() as elsewhere:
        started = await browser.get(START_URL)
        code, state = idp.authorize(started.headers["location"])
        # Carried to another browser: no state cookie there.
        stolen = await elsewhere.get(CALLBACK_URL, params={"code": code, "state": state})
        assert _sso_error(stolen) == "invalid_state"

        done = await browser.get(CALLBACK_URL, params={"code": code, "state": state})
        assert _sso_error(done) is None
        # Replayed with the state cookie: the state is spent.
        replay = await elsewhere.get(
            CALLBACK_URL,
            params={"code": code, "state": state},
            headers={"Cookie": f"tripl_sso_state={state}"},
        )
        assert _sso_error(replay) == "invalid_state"


@pytest.mark.parametrize(
    "value",
    ["//evil.example.com/x", "https://evil.example.com/", "/\\evil.example.com", "javascript:x"],
)
def test_next_never_leaves_the_origin(value: str) -> None:
    assert safe_next(value) == "/"


async def test_an_open_redirect_next_lands_on_the_root(
    people: People, acme: uuid.UUID, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable(people, monkeypatch)
    async with _new_client() as browser:
        done = await _sign_in(browser, idp, next_path="//evil.example.com/steal")
        assert done.headers["location"] == "http://test/"


async def test_sign_in_to_an_organization_without_sso_is_refused(
    people: People, acme: uuid.UUID, idp: FakeIdp
) -> None:
    async with _new_client() as browser:
        assert _sso_error(await browser.get(START_URL)) == "sso_unavailable"
        missing = await browser.get(f"{API}/auth/sso/nowhere/start")
        assert _sso_error(missing) == "sso_unavailable"
    assert idp.requests == []


# ── SSO required ────────────────────────────────────────────────────────────


async def test_sso_required_refuses_a_members_password_session(
    people: People, acme: uuid.UUID, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable(people, monkeypatch, required=True)
    mia = people["mia"]
    for url in (f"{API}/orgs/{ACME}/projects", f"{API}/orgs/{ACME}"):
        refused = await mia.get(url)
        assert refused.status_code == 403, refused.text
        assert refused.json() == {
            "detail": "This organization requires single sign-on",
            "sso_start": START_URL,
        }
    # An admin is not exempt; the default organization is untouched.
    assert (await people["bob"].get(f"{API}/orgs/{ACME}/projects")).status_code == 403
    assert (await mia.get(f"{API}/projects")).status_code == 200
    # Identity routes stay reachable.
    assert (await mia.get(f"{API}/auth/me")).status_code == 200

    # Signing in through the provider (mia's existing account: link, confirmed
    # from her own password session, which the SSO one then replaces).
    done = await _sign_in(mia, idp, email=f"mia@{DOMAIN}", sub="mia-sub")
    ticket = parse_qs(urlparse(done.headers["location"]).query)["ticket"][0]
    confirmed = await mia.post(f"{API}/auth/sso/link", json={"ticket": ticket})
    assert confirmed.status_code == 200, confirmed.text
    allowed = await mia.get(f"{API}/orgs/{ACME}/projects")
    assert allowed.status_code == 200, allowed.text
    linked = await _audit("user.sso_link")
    assert linked[0].payload["joined_organization"] is False


async def test_owners_keep_password_sign_in(
    people: People, acme: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable(people, monkeypatch, required=True)
    root = people["root"]
    assert (await root.get(f"{API}/orgs/{ACME}/projects")).status_code == 200
    assert (await root.get(SSO_URL)).status_code == 200


async def _mint_key(client: AsyncClient, name: str) -> str:
    resp = await client.post(f"{API}/orgs/{ACME}/me/api-keys", json={"name": name, "scope": "read"})
    assert resp.status_code == 201, resp.text
    token: str = resp.json()["token"]
    return token


async def _with_key(token: str, url: str) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        return await client.get(url)


async def test_requiring_sso_revokes_the_non_sso_keys(
    people: People, acme: uuid.UUID, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    member_key = await _mint_key(people["mia"], "mia-key")
    owner_key = await _mint_key(people["root"], "root-key")
    assert (await _with_key(member_key, f"{API}/projects")).status_code == 200

    # Owners' keys go too: the owner break-glass is a password SIGN-IN, not a
    # long-lived key that outlives the switch.
    saved = await _enable(people, monkeypatch, required=True)
    assert saved["revoked_api_keys"] == 2
    assert (await _with_key(member_key, f"{API}/projects")).status_code == 401
    assert (await _with_key(owner_key, f"{API}/projects")).status_code == 401
    [update] = await _audit("org.sso.update")
    assert update.payload["revoked_api_keys"] == 2

    # An owner's password session still works (break-glass) but mints no key.
    refused_mint = await people["root"].post(
        f"{API}/orgs/{ACME}/me/api-keys", json={"name": "root-new", "scope": "read"}
    )
    assert refused_mint.status_code == 403, refused_mint.text
    assert refused_mint.json()["sso_start"] == START_URL

    # A key minted from an SSO session of the organization works.
    mia = people["mia"]
    done = await _sign_in(mia, idp, email=f"mia@{DOMAIN}", sub="mia-sub")
    ticket = parse_qs(urlparse(done.headers["location"]).query)["ticket"][0]
    linked = await mia.post(f"{API}/auth/sso/link", json={"ticket": ticket})
    assert linked.status_code == 200, linked.text
    sso_key = await _mint_key(mia, "mia-sso-key")
    assert (await _with_key(sso_key, f"{API}/projects")).status_code == 200
    async with TestSessionLocal() as session:
        row = await session.scalar(select(ApiKey).where(ApiKey.name == "mia-sso-key"))
        assert row is not None
        assert row.created_with_sso_org_id == acme

    # A key of the member from before, never revoked (e.g. written behind the
    # application's back), is refused all the same.
    async with TestSessionLocal() as session:
        stale = await session.scalar(select(ApiKey).where(ApiKey.name == "mia-key"))
        assert stale is not None
        stale.revoked_at = None
        await session.commit()
    refused = await _with_key(member_key, f"{API}/projects")
    assert refused.status_code == 403, refused.text
    assert refused.json()["sso_start"] == START_URL
    # An owner's, likewise: no owner exception for keys.
    async with TestSessionLocal() as session:
        stale_owner = await session.scalar(select(ApiKey).where(ApiKey.name == "root-key"))
        assert stale_owner is not None
        stale_owner.revoked_at = None
        await session.commit()
    refused_owner = await _with_key(owner_key, f"{API}/projects")
    assert refused_owner.status_code == 403, refused_owner.text


async def test_removing_a_member_drops_their_identity(
    people: People, acme: uuid.UUID, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable(people, monkeypatch)
    async with _new_client() as browser:
        await _sign_in(browser, idp)
    user_id = await _user_id(f"alice@{DOMAIN}")
    removed = await people["root"].delete(f"{API}/orgs/{ACME}/members/{user_id}")
    assert removed.status_code == 200, removed.text
    async with TestSessionLocal() as session:
        left = await session.scalar(
            select(func.count())
            .select_from(UserSsoIdentity)
            .where(UserSsoIdentity.user_id == user_id)
        )
        assert left == 0
    [row] = await _audit("org.member_remove")
    assert row.payload["sso_identities"] == 1
