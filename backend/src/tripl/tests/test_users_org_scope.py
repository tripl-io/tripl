"""The users API is the request organization's member roster (F20 PR4, GH #273).

* ``GET /users`` lists the bound organization's members with their ORG role, and
  refuses (403) a signed-in account outside it;
* ``PATCH /users/{id}`` writes ``organization_members.role`` in the vocabulary
  owner | admin | member; the instance-era ``editor`` / ``viewer`` are 422; a
  user of another organization is 404; the member is NOT signed out;
* invitations are minted into, listed for and revoked within the bound
  organization, at an organization role; inviting an owner takes an owner;
* the role the API answers is the organization role (there is no other).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from tripl.main import app
from tripl.models.organization import DEFAULT_ORG_ID, Organization, OrganizationMember
from tripl.tests._members import add_org_member
from tripl.tests.conftest import TestSessionLocal

PASSWORD = "Password123!"
ACME_ID = uuid.UUID("00000000-0000-0000-0000-00000000ac4e")
ACME_SLUG = "acme-users"
ACME = f"/api/v1/orgs/{ACME_SLUG}"


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class People:
    def __init__(self) -> None:
        self.clients: dict[str, AsyncClient] = {}
        self.ids: dict[str, uuid.UUID] = {}

    async def register(self, name: str) -> AsyncClient:
        client = _new_client()
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": f"{name}@example.com", "password": PASSWORD, "name": name},
        )
        assert resp.status_code == 201, resp.text
        self.clients[name] = client
        self.ids[name] = uuid.UUID(resp.json()["id"])
        return client

    def __getitem__(self, name: str) -> AsyncClient:
        return self.clients[name]


@pytest.fixture
async def people() -> AsyncIterator[People]:
    crowd = People()
    await crowd.register("boss")  # owns the default organization
    yield crowd
    for client in crowd.clients.values():
        await client.aclose()


async def _add_acme() -> None:
    async with TestSessionLocal() as session:
        session.add(Organization(id=ACME_ID, slug=ACME_SLUG, name="Acme"))
        await session.commit()


async def _set_org_role(user_id: uuid.UUID, role: str, org_id: uuid.UUID = DEFAULT_ORG_ID) -> None:
    async with TestSessionLocal() as session:
        await add_org_member(session, user_id, role, org_id=org_id)


async def _leave(user_id: uuid.UUID, org_id: uuid.UUID = DEFAULT_ORG_ID) -> None:
    async with TestSessionLocal() as session:
        await session.execute(
            delete(OrganizationMember).where(
                OrganizationMember.user_id == user_id,
                OrganizationMember.organization_id == org_id,
            )
        )
        await session.commit()


# ── the roster ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_roster_is_the_bound_orgs_members_with_their_org_role(people: People) -> None:
    await _add_acme()
    await people.register("mia")
    stranger = await people.register("stranger")
    await _leave(people.ids["stranger"])
    await _set_org_role(people.ids["stranger"], "owner", ACME_ID)

    roster = await people["mia"].get("/api/v1/users")
    assert roster.status_code == 200, roster.text
    assert {u["email"]: u["role"] for u in roster.json()} == {
        "boss@example.com": "owner",
        "mia@example.com": "member",
    }
    # Signed in, but a member of no organization this request acts in.
    refused = await stranger.get("/api/v1/users")
    assert refused.status_code == 403, refused.text


@pytest.mark.asyncio
async def test_a_plain_member_cannot_change_roles_or_invite(people: People) -> None:
    mia = await people.register("mia")
    await people.register("ned")
    changed = await mia.patch(f"/api/v1/users/{people.ids['ned']}", json={"role": "admin"})
    assert changed.status_code == 403
    invited = await mia.post("/api/v1/users/invitations", json={"email": "x@example.com"})
    assert invited.status_code == 403
    assert (await mia.get("/api/v1/users/invitations")).status_code == 403


# ── PATCH /users/{id} ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", ["editor", "viewer"])
async def test_the_instance_vocabulary_is_refused(people: People, legacy: str) -> None:
    await people.register("mia")
    resp = await people["boss"].patch(f"/api/v1/users/{people.ids['mia']}", json={"role": legacy})
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_role_change_writes_the_org_role_and_keeps_the_session(people: People) -> None:
    mia = await people.register("mia")

    promoted = await people["boss"].patch(
        f"/api/v1/users/{people.ids['mia']}", json={"role": "admin"}
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["role"] == "admin"

    async with TestSessionLocal() as session:
        org_role = await session.scalar(
            select(OrganizationMember.role).where(
                OrganizationMember.user_id == people.ids["mia"],
                OrganizationMember.organization_id == DEFAULT_ORG_ID,
            )
        )
    assert org_role == "admin"

    # Critique #7: still signed in, and the new role applies at once.
    me = await mia.get("/api/v1/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["role"] == "admin"
    assert (await mia.get("/api/v1/users/invitations")).status_code == 200


@pytest.mark.asyncio
async def test_a_user_of_another_org_is_not_found(people: People) -> None:
    await _add_acme()
    await people.register("abroad")
    await _leave(people.ids["abroad"])
    await _set_org_role(people.ids["abroad"], "member", ACME_ID)
    resp = await people["boss"].patch(
        f"/api/v1/users/{people.ids['abroad']}", json={"role": "admin"}
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_the_last_owner_cannot_step_down(people: People) -> None:
    resp = await people["boss"].patch(f"/api/v1/users/{people.ids['boss']}", json={"role": "admin"})
    assert resp.status_code == 400, resp.text


# ── invitations ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invitations_are_minted_at_an_org_role(people: People) -> None:
    created = await people["boss"].post(
        "/api/v1/users/invitations", json={"email": "new@example.com", "role": "admin"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["invitation"]["role"] == "admin"
    token = created.json()["accept_path"].rsplit("/", 1)[-1]

    async with _new_client() as invitee:
        preview = await invitee.get(f"/api/v1/auth/invitations/{token}")
        assert preview.status_code == 200, preview.text
        assert preview.json()["role"] == "admin"
        accepted = await invitee.post(
            f"/api/v1/auth/invitations/{token}/accept",
            json={"password": PASSWORD, "name": "New"},
        )
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["role"] == "admin"
        assert accepted.json()["is_platform_admin"] is False
        roster = await invitee.get("/api/v1/users")
    assert {u["email"]: u["role"] for u in roster.json()}["new@example.com"] == "admin"


@pytest.mark.asyncio
async def test_the_legacy_invitation_roles_are_refused(people: People) -> None:
    for legacy in ("editor", "viewer"):
        resp = await people["boss"].post(
            "/api/v1/users/invitations", json={"email": "x@example.com", "role": legacy}
        )
        assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_only_an_owner_invites_an_owner(people: People) -> None:
    ada = await people.register("ada")
    await _set_org_role(people.ids["ada"], "admin")
    refused = await ada.post(
        "/api/v1/users/invitations", json={"email": "o@example.com", "role": "owner"}
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == "Only an owner can manage owners"
    member = await ada.post(
        "/api/v1/users/invitations", json={"email": "m@example.com", "role": "member"}
    )
    assert member.status_code == 201, member.text
    owner = await people["boss"].post(
        "/api/v1/users/invitations", json={"email": "o@example.com", "role": "owner"}
    )
    assert owner.status_code == 201, owner.text


@pytest.mark.asyncio
async def test_invitations_stay_inside_their_org(people: People) -> None:
    await _add_acme()
    carol = await people.register("carol")
    await _set_org_role(people.ids["carol"], "owner", ACME_ID)

    abroad = await carol.post(f"{ACME}/users/invitations", json={"email": "acme-new@example.com"})
    assert abroad.status_code == 201, abroad.text
    home = await people["boss"].post(
        "/api/v1/users/invitations", json={"email": "home-new@example.com"}
    )
    assert home.status_code == 201, home.text

    home_list = await people["boss"].get("/api/v1/users/invitations")
    assert [i["email"] for i in home_list.json()] == ["home-new@example.com"]
    acme_list = await carol.get(f"{ACME}/users/invitations")
    assert [i["email"] for i in acme_list.json()] == ["acme-new@example.com"]

    # The default org's owner cannot revoke acme's invitation.
    foreign = await people["boss"].delete(
        f"/api/v1/users/invitations/{abroad.json()['invitation']['id']}"
    )
    assert foreign.status_code == 404, foreign.text

    # The accepted invitation joins acme, not the default organization.
    token = abroad.json()["accept_path"].rsplit("/", 1)[-1]
    async with _new_client() as invitee:
        accepted = await invitee.post(
            f"/api/v1/auth/invitations/{token}/accept", json={"password": PASSWORD}
        )
        assert accepted.status_code == 201, accepted.text
        assert {o["slug"]: o["role"] for o in accepted.json()["orgs"]} == {ACME_SLUG: "member"}
