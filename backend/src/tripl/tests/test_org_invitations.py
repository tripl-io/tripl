"""Invitations into an organization (F20 PR6, GH #273).

* ``/orgs/{org}/users/invitations`` mints into, and lists for, the organization
  its path names; the legacy ``/users/invitations`` stays the default one's;
* an address that already has an account may be invited into an organization
  it is not in; accepting while signed in adds the membership, but only for the
  account the invitation was sent to (case-insensitive), else 403;
* the new-account path is unchanged;
* an invitation into an organization being deleted is dead;
* the link is mailed through the operator's SMTP when there is one.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from tripl.config import settings
from tripl.main import app
from tripl.models.audit_log import AuditLog
from tripl.models.invitation import Invitation
from tripl.models.organization import Organization, OrganizationMember
from tripl.tests._tenancy import use_multi_org
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import alerts_channels


@pytest.fixture(autouse=True)
def _more_organizations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Organizations are created over the API here: the Enterprise edition's (``_tenancy``)."""
    use_multi_org(monkeypatch)


PASSWORD = "Password123!"
API = "/api/v1"
ACME = "acme"
ACME_URL = f"{API}/orgs/{ACME}"


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _register(client: AsyncClient, name: str) -> uuid.UUID:
    resp = await client.post(
        f"{API}/auth/register",
        json={"email": f"{name}@example.com", "password": PASSWORD, "name": name},
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


class World:
    def __init__(self) -> None:
        self.root = _new_client()
        self.alice = _new_client()
        self.bob = _new_client()
        self.anon = _new_client()
        self.acme_id = uuid.uuid4()
        self.alice_id = uuid.uuid4()

    async def aclose(self) -> None:
        for client in (self.root, self.alice, self.bob, self.anon):
            await client.aclose()


@pytest.fixture
async def world() -> AsyncIterator[World]:
    w = World()
    try:
        await _register(w.root, "root")  # default owner, platform admin
        w.alice_id = await _register(w.alice, "alice")
        await _register(w.bob, "bob")
        created = await w.root.post(f"{API}/orgs", json={"slug": ACME, "name": "Acme"})
        assert created.status_code == 201, created.text
        w.acme_id = uuid.UUID(created.json()["id"])
        yield w
    finally:
        await w.aclose()


def _token(created: dict[str, Any]) -> str:
    return str(created["accept_path"]).rsplit("/", 1)[-1]


async def _invite(client: AsyncClient, email: str, role: str = "member", prefix: str = ACME_URL):
    return await client.post(f"{prefix}/users/invitations", json={"email": email, "role": role})


async def test_org_qualified_invitations_belong_to_the_path_organization(world: World) -> None:
    created = await _invite(world.root, "newbie@example.com")
    assert created.status_code == 201, created.text
    invitation_id = created.json()["invitation"]["id"]
    async with TestSessionLocal() as session:
        org_id = await session.scalar(
            select(Invitation.organization_id).where(Invitation.id == uuid.UUID(invitation_id))
        )
    assert org_id == world.acme_id

    in_acme = await world.root.get(f"{ACME_URL}/users/invitations")
    assert [row["id"] for row in in_acme.json()] == [invitation_id]
    in_default = await world.root.get(f"{API}/users/invitations")
    assert in_default.json() == []

    # A new account redeemed from it joins acme, not the default organization.
    joined = await world.anon.post(
        f"{API}/auth/invitations/{_token(created.json())}/accept", json={"password": PASSWORD}
    )
    assert joined.status_code == 201, joined.text
    assert [org["slug"] for org in joined.json()["orgs"]] == [ACME]

    revoked = await world.root.delete(f"{ACME_URL}/users/invitations/{invitation_id}")
    assert revoked.status_code in (204, 404)


async def test_a_signed_in_account_accepts_an_invitation_sent_to_it(world: World) -> None:
    created = await _invite(world.root, "Alice@Example.com", role="admin")
    assert created.status_code == 201, created.text
    token = _token(created.json())

    accepted = await world.alice.post(f"{API}/auth/invitations/{token}/accept", json={})
    assert accepted.status_code == 200, accepted.text
    assert {"slug": ACME, "name": "Acme", "role": "admin", "status": "active"} in accepted.json()[
        "orgs"
    ]
    # /auth/me says so at once, and the account keeps its default membership.
    me = (await world.alice.get(f"{API}/auth/me")).json()
    assert {org["slug"] for org in me["orgs"]} == {"default", ACME}
    assert (await world.alice.get(ACME_URL)).json()["role"] == "admin"

    # Single use.
    replay = await world.alice.post(f"{API}/auth/invitations/{token}/accept", json={})
    assert replay.status_code == 400, replay.text
    async with TestSessionLocal() as session:
        entries = (
            await session.scalars(select(AuditLog).where(AuditLog.action == "user.invite_accept"))
        ).all()
    assert [(e.organization_id, e.user_id) for e in entries] == [(world.acme_id, world.alice_id)]
    assert entries[0].payload["existing_account"] is True


async def test_another_account_cannot_accept_it(world: World) -> None:
    created = await _invite(world.root, "alice@example.com")
    token = _token(created.json())

    refused = await world.bob.post(f"{API}/auth/invitations/{token}/accept", json={})
    assert refused.status_code == 403, refused.text
    # Still redeemable by the right account.
    assert (await world.anon.get(f"{API}/auth/invitations/{token}")).status_code == 200
    async with TestSessionLocal() as session:
        bob_in_acme = await session.scalar(
            select(OrganizationMember.id)
            .where(OrganizationMember.organization_id == world.acme_id)
            .where(OrganizationMember.role != "owner")
        )
    assert bob_in_acme is None
    assert (
        await world.alice.post(f"{API}/auth/invitations/{token}/accept", json={})
    ).status_code == 200


async def test_an_existing_member_is_not_invited_again(world: World) -> None:
    clash = await _invite(world.root, "root@example.com")
    assert clash.status_code == 409, clash.text
    # Alice has an account, but not in acme: she may be invited.
    assert (await _invite(world.root, "alice@example.com")).status_code == 201


async def test_signed_out_redemption_still_needs_a_password(world: World) -> None:
    created = await _invite(world.root, "fresh@example.com")
    token = _token(created.json())
    missing = await world.anon.post(f"{API}/auth/invitations/{token}/accept", json={})
    assert missing.status_code == 422, missing.text
    weak = await world.anon.post(f"{API}/auth/invitations/{token}/accept", json={"password": "x"})
    assert weak.status_code == 422, weak.text

    # An existing account that is not signed in is sent to sign in, as before.
    existing = await _invite(world.root, "alice@example.com")
    signed_out = await world.anon.post(
        f"{API}/auth/invitations/{_token(existing.json())}/accept", json={"password": PASSWORD}
    )
    assert signed_out.status_code == 409, signed_out.text


async def test_an_invitation_into_a_deleting_organization_is_dead(world: World) -> None:
    created = await _invite(world.root, "late@example.com")
    token = _token(created.json())
    async with TestSessionLocal() as session:
        await session.execute(
            update(Organization).where(Organization.id == world.acme_id).values(status="deleting")
        )
        await session.commit()
    assert (await world.anon.get(f"{API}/auth/invitations/{token}")).status_code == 400
    redeemed = await world.anon.post(
        f"{API}/auth/invitations/{token}/accept", json={"password": PASSWORD}
    )
    assert redeemed.status_code == 400, redeemed.text


async def test_the_link_is_mailed_through_the_operator_smtp(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "smtp_host", "smtp.operator.example.com")
    monkeypatch.setattr(settings, "smtp_from_address", "tripl@operator.example.com")
    monkeypatch.setattr(settings, "app_base_url", "https://tripl.example.com")
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(alerts_channels, "_send_email_message", lambda **kw: sent.append(kw))

    created = await _invite(world.root, "mailed@example.com")
    assert created.status_code == 201, created.text

    [mail] = sent
    assert mail["smtp_host"] == "smtp.operator.example.com"
    assert mail["from_address"] == "tripl@operator.example.com"
    assert mail["recipients"] == ["mailed@example.com"]
    assert "Acme" in mail["subject"]
    assert f"https://tripl.example.com{created.json()['accept_path']}" in mail["body"]


async def test_no_smtp_means_no_mail_and_the_link_still_works(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "smtp_host", "")
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(alerts_channels, "_send_email_message", lambda **kw: sent.append(kw))
    created = await _invite(world.root, "quiet@example.com")
    assert created.status_code == 201, created.text
    assert created.json()["accept_path"].startswith("/invite/")
    assert sent == []
