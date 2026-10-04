# ruff: noqa: F811  (the fixtures imported from test_org_sso are redefined as parameters)
"""SSO sign-in against existing accounts, and the outbound hardening (F20, GH #273).

* an account nobody ever proved the address of (a hosted sign-up still
  unverified) is taken over clean by the provider-verified person: the
  squatter's password, sessions and keys stop working;
* a platform admin is never such an account: linking one needs its session;
* a member removed from the organization does not sign back in through SSO
  (neither the linked identity nor a new link) until they accept an invitation;
* a TXT value that is not ASCII is "not verified", not a 500;
* on a hosted instance an IdP request connects to the very address that was
  vetted (DNS rebinding), and a private one is refused before connecting;
* start and callback have their own rate-limit bucket and answer an empty one
  with ``/auth?sso_error=rate_limited``, leaving password sign-in alone.

Fixtures and helpers come from ``test_org_sso``.
"""

from __future__ import annotations

import socket
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from tripl.config import settings
from tripl.models.api_key import ApiKey
from tripl.models.org_sso import SsoMembershipBlock
from tripl.models.organization import DEFAULT_ORG_SLUG
from tripl.models.user import User
from tripl.services import org_sso_service
from tripl.services.oidc import idp_http
from tripl.tests._fake_idp import FakeIdp
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_org_sso import (  # noqa: F401 - fixtures
    ACME,
    API,
    CALLBACK_URL,
    DOMAIN,
    DOMAINS_URL,
    PASSWORD,
    SSO_URL,
    START_URL,
    People,
    _audit,
    _config,
    _enable,
    _new_client,
    _sign_in,
    _sso_error,
    _txt,
    _user_id,
    _verified_domain,
    _with_key,
    acme,
    idp,
    people,
)


def _ticket(location: str) -> str:
    parsed = urlparse(location)
    assert parsed.path == "/sso/link", location
    return parse_qs(parsed.query)["ticket"][0]


async def _unverify(email: str) -> None:
    async with TestSessionLocal() as session:
        user = await session.scalar(select(User).where(User.email == email))
        assert user is not None
        user.email_verified_at = None
        await session.commit()


# ── existing accounts ───────────────────────────────────────────────────────


async def test_an_unverified_account_is_taken_over_clean(
    people: People, acme: uuid.UUID, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Someone registered the address first and never proved it (hosted sign-up)."""
    await people.register("squatter", f"sam@{DOMAIN}")
    squatter = people["squatter"]
    key = await squatter.post(
        f"{API}/orgs/{DEFAULT_ORG_SLUG}/me/api-keys", json={"name": "squat", "scope": "read"}
    )
    assert key.status_code == 201, key.text
    squat_key = key.json()["token"]
    await _unverify(f"sam@{DOMAIN}")
    await _enable(people, monkeypatch)

    async with _new_client() as browser:
        done = await _sign_in(browser, idp, email=f"sam@{DOMAIN}", sub="sam-sub")
        ticket = _ticket(done.headers["location"])
        preview = await browser.get(f"{API}/auth/sso/link", params={"ticket": ticket})
        # Nobody holds the account in a way that counts: no sign-in needed.
        assert preview.json()["sign_in_required"] is False
        confirmed = await browser.post(f"{API}/auth/sso/link", json={"ticket": ticket})
        assert confirmed.status_code == 200, confirmed.text
        me = await browser.get(f"{API}/auth/me")
        assert me.json()["email"] == f"sam@{DOMAIN}"
        assert me.json()["email_verified"] is True

    # The squatter's session, password and key are all gone.
    assert (await squatter.get(f"{API}/auth/me")).status_code == 401
    async with _new_client() as again:
        login = await again.post(
            f"{API}/auth/login", json={"email": f"sam@{DOMAIN}", "password": PASSWORD}
        )
        assert login.status_code == 401, login.text
    assert (await _with_key(squat_key, f"{API}/projects")).status_code == 401
    async with TestSessionLocal() as session:
        row = await session.scalar(select(ApiKey).where(ApiKey.name == "squat"))
        assert row is not None
        assert row.revoked_at is not None
    [linked] = await _audit("user.sso_link")
    assert linked.payload["reclaimed_unverified_account"] is True


async def test_a_platform_admin_is_never_taken_over(
    people: People, acme: uuid.UUID, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even unverified, a platform admin's account links only from its own session."""
    root = people["root"]
    await _verified_domain(root, monkeypatch, domain="example.com")
    saved = await root.put(SSO_URL, json=_config(enabled=True))
    assert saved.status_code == 200, saved.text
    await _unverify("root@example.com")

    async with _new_client() as browser:
        done = await _sign_in(browser, idp, email="root@example.com", sub="root-sub")
        ticket = _ticket(done.headers["location"])
        preview = await browser.get(f"{API}/auth/sso/link", params={"ticket": ticket})
        assert preview.json()["sign_in_required"] is True
        refused = await browser.post(f"{API}/auth/sso/link", json={"ticket": ticket})
        assert refused.status_code == 401, refused.text
    assert await _audit("user.sso_link") == []
    assert (await root.get(f"{API}/auth/me")).status_code == 200


# ── removed members ─────────────────────────────────────────────────────────


async def test_a_removed_member_does_not_sign_back_in(
    people: People, acme: uuid.UUID, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable(people, monkeypatch)
    async with _new_client() as browser:
        await _sign_in(browser, idp)
    alice = await _user_id(f"alice@{DOMAIN}")
    mia = await _user_id(f"mia@{DOMAIN}")
    for user_id in (alice, mia):
        removed = await people["root"].delete(f"{API}/orgs/{ACME}/members/{user_id}")
        assert removed.status_code == 200, removed.text

    # The JIT account: its identity went with the membership, and a new link
    # would bring it back; refused at the callback.
    async with _new_client() as browser:
        assert _sso_error(await _sign_in(browser, idp)) == "membership_removed"
    # An account that never used SSO: refused likewise, even from its session.
    done = await _sign_in(people["mia"], idp, email=f"mia@{DOMAIN}", sub="mia-sub")
    assert _sso_error(done) == "membership_removed"
    assert await _audit("user.sso_link") == []

    # An accepted invitation (an admin's decision) lifts the block.
    assert mia is not None
    async with TestSessionLocal() as session:
        assert await org_sso_service.membership_blocked(session, acme, mia)
        await org_sso_service.lift_membership_block(session, acme, mia)
        await session.commit()
        assert not await org_sso_service.membership_blocked(session, acme, mia)
    done = await _sign_in(people["mia"], idp, email=f"mia@{DOMAIN}", sub="mia-sub")
    ticket = _ticket(done.headers["location"])
    confirmed = await people["mia"].post(f"{API}/auth/sso/link", json={"ticket": ticket})
    assert confirmed.status_code == 200, confirmed.text


async def test_a_ticket_issued_before_the_removal_is_refused(
    people: People, acme: uuid.UUID, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable(people, monkeypatch)
    mia = people["mia"]
    done = await _sign_in(mia, idp, email=f"mia@{DOMAIN}", sub="mia-sub")
    ticket = _ticket(done.headers["location"])
    mia_id = await _user_id(f"mia@{DOMAIN}")
    removed = await people["root"].delete(f"{API}/orgs/{ACME}/members/{mia_id}")
    assert removed.status_code == 200, removed.text
    async with TestSessionLocal() as session:
        blocks = (await session.scalars(select(SsoMembershipBlock))).all()
        assert [(b.organization_id, b.user_id) for b in blocks] == [(acme, mia_id)]

    refused = await mia.post(f"{API}/auth/sso/link", json={"ticket": ticket})
    assert refused.status_code == 403, refused.text


# ── domains ─────────────────────────────────────────────────────────────────


async def test_a_non_ascii_txt_value_is_not_verified(
    people: People, acme: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = people["root"]
    added = await root.post(DOMAINS_URL, json={"domain": DOMAIN})
    body = added.json()
    _txt(monkeypatch, {body["txt_record_name"]: ["tripl-verification=�é"]})
    pending = await root.post(f"{DOMAINS_URL}/{body['id']}/verify")
    assert pending.status_code == 200, pending.text
    assert pending.json()["verified"] is False


# ── outbound requests, hosted ───────────────────────────────────────────────


def test_a_hosted_request_connects_to_the_vetted_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolved once: a second answer (rebinding to a private address) is never used."""
    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    answers = iter(["93.184.215.14", "10.0.0.5"])
    lookups: list[str] = []

    def getaddrinfo(host: str, *_a: Any, **_k: Any) -> list[Any]:
        lookups.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), 443))]

    connected: list[tuple[str, int]] = []

    def create_connection(address: tuple[str, int], *_a: Any, **_k: Any) -> socket.socket:
        connected.append(address)
        raise OSError("stop here")

    monkeypatch.setattr(idp_http.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(idp_http.socket, "create_connection", create_connection)
    with pytest.raises(OSError):
        idp_http._send("GET", "https://idp.example.com/.well-known/x", {}, None)
    assert lookups == ["idp.example.com"]
    assert connected == [("93.184.215.14", 443)]


def test_a_hosted_request_to_a_private_address_never_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    monkeypatch.setattr(
        idp_http.socket,
        "getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))],
    )
    connected: list[Any] = []
    monkeypatch.setattr(idp_http.socket, "create_connection", lambda *a, **_k: connected.append(a))
    with pytest.raises(idp_http.IdpError) as refused:
        idp_http._send("GET", "https://idp.example.com/jwks", {}, None)
    assert refused.value.code == "idp_private_host"
    assert connected == []


# ── rate limits ─────────────────────────────────────────────────────────────


async def test_sso_has_its_own_bucket_and_redirects_when_it_is_empty(
    people: People, acme: uuid.UUID, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tripl.middleware.rate_limit import SSO_RATE_LIMIT_PER_MINUTE

    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    async with _new_client() as browser:
        # Acme has no SSO yet: each start is a cheap redirect, and a token.
        for _ in range(SSO_RATE_LIMIT_PER_MINUTE):
            assert _sso_error(await browser.get(START_URL)) == "sso_unavailable"
        assert _sso_error(await browser.get(START_URL)) == "rate_limited"
        callback = await browser.get(CALLBACK_URL, params={"code": "c", "state": "s"})
        assert _sso_error(callback) == "rate_limited"
        # The password sign-in bucket is untouched.
        login = await browser.post(
            f"{API}/auth/login", json={"email": "bob@example.com", "password": PASSWORD}
        )
        assert login.status_code == 200, login.text
