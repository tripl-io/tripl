"""Organization roles are the source of truth (F20 PR4, GH #273).

* the role matrix: org ``owner`` / ``admin`` / ``member`` x project row
  (none / ``viewer`` / ``editor``);
* a platform admin gets the operator settings and nothing inside any org;
* an org admin gets the org-scoped settings but not the operator fields;
* a role in org A buys nothing in org B, including through an id taken from a
  resource (critique #4);
* ``add_member`` refuses a user from another organization;
* the owner set, its lock and the last-owner rule are per organization;
* ``/users`` and ``/me/api-keys`` are scoped to the request's organization.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from starlette.requests import Request

from tripl.api import deps
from tripl.main import app
from tripl.middleware.org_context import OrgRef, bound_org
from tripl.models.domain_enums import OrganizationRole
from tripl.models.organization import (
    DEFAULT_ORG_ID,
    DEFAULT_ORG_SLUG,
    Organization,
    OrganizationMember,
)
from tripl.models.project import Project
from tripl.models.user import User
from tripl.services import project_access, user_service
from tripl.tests._members import add_member_by_slug, add_org_member
from tripl.tests.conftest import TestSessionLocal

PASSWORD = "Password123!"
ACME_ID = uuid.UUID("00000000-0000-0000-0000-0000000ac3e2")
ACME_SLUG = "acme"
ACME = OrgRef(id=ACME_ID, slug=ACME_SLUG)
DEFAULT = OrgRef(id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG)


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class People:
    """Clients with their own cookie jars, created on demand."""

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

    async def aclose(self) -> None:
        for client in self.clients.values():
            await client.aclose()


@pytest.fixture
async def people() -> AsyncIterator[People]:
    crowd = People()
    # The first account owns the default organization and operates the instance.
    await crowd.register("boss")
    yield crowd
    await crowd.aclose()


async def _set_org_role(user_id: uuid.UUID, role: str, org_id: uuid.UUID = DEFAULT_ORG_ID) -> None:
    async with TestSessionLocal() as session:
        await add_org_member(session, user_id, role, org_id=org_id)


async def _leave_org(user_id: uuid.UUID, org_id: uuid.UUID = DEFAULT_ORG_ID) -> None:
    async with TestSessionLocal() as session:
        await session.execute(
            delete(OrganizationMember).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_id == user_id,
            )
        )
        await session.commit()


async def _add_acme() -> None:
    async with TestSessionLocal() as session:
        session.add(Organization(id=ACME_ID, slug=ACME_SLUG, name="Acme"))
        await session.commit()


async def _project_id(slug: str, org_id: uuid.UUID = DEFAULT_ORG_ID) -> uuid.UUID:
    async with TestSessionLocal() as session:
        project_id = await session.scalar(
            select(Project.id).where(Project.slug == slug, Project.organization_id == org_id)
        )
    assert project_id is not None
    return project_id


async def _user(user_id: uuid.UUID) -> User:
    async with TestSessionLocal() as session:
        user = await session.get(User, user_id)
    assert user is not None
    return user


async def _create_project(client: AsyncClient, slug: str, prefix: str = "/api/v1") -> None:
    resp = await client.post(f"{prefix}/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text


# ── the role matrix ─────────────────────────────────────────────────────────

_MATRIX = [
    (org_role, row)
    for org_role in ("owner", "admin", "member")
    for row in (None, "viewer", "editor")
]


def _expected_project_role(org_role: str, row: str | None) -> str | None:
    if org_role in ("owner", "admin"):
        return "owner"
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize(("org_role", "row"), _MATRIX)
async def test_org_role_by_project_row_matrix(
    people: People, org_role: str, row: str | None
) -> None:
    await _create_project(people["boss"], "matrix")
    subject = await people.register("subject")
    await _set_org_role(people.ids["subject"], org_role)
    if row is not None:
        await add_member_by_slug("matrix", "subject@example.com", row)
    expected = _expected_project_role(org_role, row)

    detail = await subject.get("/api/v1/projects/matrix")
    listed = {p["slug"] for p in (await subject.get("/api/v1/projects")).json()}
    if expected is None:
        assert detail.status_code == 404, detail.text
        assert "matrix" not in listed
    else:
        assert detail.status_code == 200, detail.text
        assert detail.json()["my_role"] == expected
        assert detail.json()["can_mutate"] is (expected in ("owner", "editor"))
        assert "matrix" in listed

    write = await subject.post(
        "/api/v1/projects/matrix/event-types", json={"name": "pv", "display_name": "PV"}
    )
    assert write.status_code == {None: 404, "viewer": 403, "editor": 201, "owner": 201}[expected], (
        write.text
    )

    # Org administration follows the org role alone.
    audit = await subject.get("/api/v1/audit")
    assert audit.status_code == (200 if org_role in ("owner", "admin") else 403), audit.text

    # Project deletion: org owner/admin only; a member with a row is refused,
    # one without a row does not see the project at all.
    deleted = await subject.delete("/api/v1/projects/matrix")
    assert (
        deleted.status_code == {None: 404, "viewer": 403, "editor": 403, "owner": 204}[expected]
    ), deleted.text


@pytest.mark.asyncio
async def test_effective_role_is_pure_and_ignores_rows_for_org_admins() -> None:
    assert project_access.effective_role("owner", None) == "owner"
    assert project_access.effective_role("admin", "viewer") == "owner"
    assert project_access.effective_role("member", "editor") == "editor"
    assert project_access.effective_role("member", "viewer") == "viewer"
    assert project_access.effective_role("member", None) is None
    assert project_access.effective_role(None, "editor") == "editor"
    assert project_access.effective_role(None, None) is None
    assert project_access.is_org_admin_role("admin")
    assert not project_access.is_org_admin_role("member")
    assert not project_access.is_org_admin_role(None)


# ── platform admin vs org admin ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_platform_admin_without_membership_sees_no_project_but_operates(
    people: People,
) -> None:
    boss = people["boss"]
    await _create_project(boss, "private")
    me = (await boss.get("/api/v1/auth/me")).json()
    assert me["is_platform_admin"] is True
    # The platform admin leaves the organization (its creator row stays: drop
    # it too, so nothing but the flag is left).
    await _leave_org(people.ids["boss"])
    async with TestSessionLocal() as session:
        from tripl.models.project_member import ProjectMember

        await session.execute(
            delete(ProjectMember).where(ProjectMember.user_id == people.ids["boss"])
        )
        await session.commit()

    assert (await boss.get("/api/v1/projects/private")).status_code == 404
    assert "private" not in {p["slug"] for p in (await boss.get("/api/v1/projects")).json()}
    assert (await boss.get("/api/v1/audit")).status_code == 403
    assert (await boss.get("/api/v1/users")).status_code == 403
    assert (
        await boss.post("/api/v1/projects", json={"name": "No", "slug": "no-org"})
    ).status_code == 403

    settings = await boss.get("/api/v1/settings")
    assert settings.status_code == 200, settings.text
    assert settings.json()["system"] is not None
    operator = await boss.patch(
        "/api/v1/settings", json={"security": {"registration_mode": "disabled"}}
    )
    assert operator.status_code == 200, operator.text


@pytest.mark.asyncio
async def test_org_admin_edits_org_settings_but_not_operator_settings(people: People) -> None:
    admin = await people.register("orgadmin")
    await _set_org_role(people.ids["orgadmin"], "admin")

    settings = await admin.get("/api/v1/settings")
    assert settings.status_code == 200, settings.text
    assert settings.json()["system"] is None

    org_field = await admin.patch(
        "/api/v1/settings", json={"runtime": {"scan_row_limit_default": 4321}}
    )
    assert org_field.status_code == 200, org_field.text

    operator = await admin.patch(
        "/api/v1/settings", json={"security": {"registration_mode": "disabled"}}
    )
    assert operator.status_code == 403, operator.text
    assert operator.json()["detail"] == deps.PLATFORM_ADMIN_REQUIRED

    member = await people.register("plainmember")
    assert (await member.get("/api/v1/settings")).status_code == 403


@pytest.mark.asyncio
async def test_admin_of_another_org_is_not_a_settings_admin(people: People) -> None:
    await _add_acme()
    outsider = await people.register("acmeadmin")
    await _set_org_role(people.ids["acmeadmin"], "admin", ACME_ID)
    assert (await outsider.get("/api/v1/settings")).status_code == 403


@pytest.mark.asyncio
async def test_the_platform_gate_refuses_api_keys_and_non_admins() -> None:
    request = Request({"type": "http", "method": "PATCH", "path": "/", "headers": []})
    admin = User(email="p@example.com", password_hash="x", is_platform_admin=True)
    assert await deps.require_platform_admin(request, admin) is admin

    plain = User(email="q@example.com", password_hash="x", is_platform_admin=False)
    with pytest.raises(deps.HTTPException) as refused:
        await deps.require_platform_admin(request, plain)
    assert refused.value.status_code == 403

    request.state.api_key_scope = "write"
    with pytest.raises(deps.HTTPException) as keyed:
        await deps.require_platform_admin(request, admin)
    assert keyed.value.detail == "Platform admin session required"


# ── org A buys nothing in org B ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_role_in_one_org_gives_no_rights_in_another(people: People) -> None:
    await _add_acme()
    await _create_project(people["boss"], "home")
    xavier = await people.register("xavier")
    await _set_org_role(people.ids["xavier"], "admin", ACME_ID)
    await _create_project(xavier, "abroad", prefix=f"/api/v1/orgs/{ACME_SLUG}")

    # Admin in acme, plain member in the default org: nothing there.
    assert (await xavier.get("/api/v1/projects/home")).status_code == 404
    assert (await xavier.get("/api/v1/audit")).status_code == 403
    assert (await xavier.get(f"/api/v1/orgs/{ACME_SLUG}/audit")).status_code == 200
    abroad = await xavier.get(f"/api/v1/orgs/{ACME_SLUG}/projects/abroad")
    assert abroad.status_code == 200, abroad.text
    assert abroad.json()["my_role"] == "owner"

    # The default org's owner is nobody in acme.
    assert (
        await people["boss"].get(f"/api/v1/orgs/{ACME_SLUG}/projects/abroad")
    ).status_code == 404

    # Service level, with an id taken from a resource (critique #4): the bound
    # org fences the answer; the stream check reads the project's own org.
    abroad_id = await _project_id("abroad", ACME_ID)
    x = await _user(people.ids["xavier"])
    boss = await _user(people.ids["boss"])
    async with TestSessionLocal() as session:
        with bound_org(DEFAULT):
            assert await project_access.member_role(session, x, abroad_id) is None
            assert await project_access.member_role(session, boss, abroad_id) is None
            assert abroad_id not in await project_access.member_project_ids(session, x)
            assert not await project_access.is_org_admin(session, x)
        with bound_org(ACME):
            assert await project_access.member_role(session, x, abroad_id) == "owner"
            assert await project_access.member_project_ids(session, x) == {abroad_id}
            assert await project_access.is_org_admin(session, x)
            assert not await project_access.is_org_owner(session, x)
        # Membership fan-out: org admins of the PROJECT's org, never another's.
        assert await project_access.members_among(session, abroad_id, {x.id, boss.id}) == {x.id}
    assert await project_access.still_member(TestSessionLocal, user_id=x.id, project_id=abroad_id)
    assert not await project_access.still_member(
        TestSessionLocal, user_id=boss.id, project_id=abroad_id
    )


@pytest.mark.asyncio
async def test_add_member_refuses_a_user_of_another_org(people: People) -> None:
    await _add_acme()
    await _create_project(people["boss"], "closed-shop")
    await people.register("foreigner")
    await _leave_org(people.ids["foreigner"])
    await _set_org_role(people.ids["foreigner"], "member", ACME_ID)

    refused = await people["boss"].post(
        "/api/v1/projects/closed-shop/members",
        json={"user_id": str(people.ids["foreigner"]), "role": "viewer"},
    )
    assert refused.status_code in (404, 422), refused.text
    members = await people["boss"].get("/api/v1/projects/closed-shop/members")
    assert str(people.ids["foreigner"]) not in {m["user_id"] for m in members.json()}


# ── owners, per organization ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_last_owner_rule_is_per_org(people: People) -> None:
    await _add_acme()
    olga = await people.register("olga")
    await _set_org_role(people.ids["olga"], "owner", ACME_ID)
    # The default org has another owner (boss) — that does not help acme.
    demote_self = await olga.patch(
        f"/api/v1/orgs/{ACME_SLUG}/users/{people.ids['olga']}", json={"role": "member"}
    )
    assert demote_self.status_code == 400, demote_self.text

    async with TestSessionLocal() as session:
        with pytest.raises(user_service.LastOwnerError):
            await user_service.update_org_role(
                session,
                ACME_ID,
                people.ids["olga"],
                OrganizationRole.admin,
                actor_id=people.ids["olga"],
            )
        await session.rollback()

    # In the default org a second owner lets the first step down.
    boss = people["boss"]
    promoted = await boss.patch(f"/api/v1/users/{people.ids['olga']}", json={"role": "owner"})
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["role"] == "owner"
    stepped_down = await olga.patch(f"/api/v1/users/{people.ids['boss']}", json={"role": "admin"})
    assert stepped_down.status_code == 200, stepped_down.text


@pytest.mark.asyncio
async def test_only_an_owner_manages_owners(people: People) -> None:
    ada = await people.register("ada")
    await people.register("bob")
    await _set_org_role(people.ids["ada"], "admin")

    # An admin manages members and admins...
    to_admin = await ada.patch(f"/api/v1/users/{people.ids['bob']}", json={"role": "admin"})
    assert to_admin.status_code == 200, to_admin.text
    # ...but neither makes nor unmakes an owner.
    make_owner = await ada.patch(f"/api/v1/users/{people.ids['bob']}", json={"role": "owner"})
    assert make_owner.status_code == 403, make_owner.text
    demote_owner = await ada.patch(f"/api/v1/users/{people.ids['boss']}", json={"role": "member"})
    assert demote_owner.status_code == 403, demote_owner.text

    async with TestSessionLocal() as session:
        with pytest.raises(user_service.OwnerManagementError):
            await user_service.update_org_role(
                session,
                DEFAULT_ORG_ID,
                people.ids["bob"],
                OrganizationRole.owner,
                actor_id=people.ids["ada"],
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_owner_set_lock_keys_differ_per_org() -> None:
    from tripl.services import auth_service

    assert auth_service.owner_set_lock_key(DEFAULT_ORG_ID) == auth_service.owner_set_lock_key(
        DEFAULT_ORG_ID
    )
    assert auth_service.owner_set_lock_key(DEFAULT_ORG_ID) != auth_service.owner_set_lock_key(
        ACME_ID
    )


# ── org-scoped lists ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_users_lists_the_members_of_the_request_org(people: People) -> None:
    await _add_acme()
    await people.register("carol")
    await _set_org_role(people.ids["carol"], "owner", ACME_ID)
    await _set_org_role(people.ids["boss"], "member", ACME_ID)

    default_roster = (await people["boss"].get("/api/v1/users")).json()
    assert {u["email"]: u["role"] for u in default_roster} == {
        "boss@example.com": "owner",
        "carol@example.com": "member",
    }
    acme_roster = (await people["carol"].get(f"/api/v1/orgs/{ACME_SLUG}/users")).json()
    assert {u["email"]: u["role"] for u in acme_roster} == {
        "carol@example.com": "owner",
        "boss@example.com": "member",
    }

    async with TestSessionLocal() as session:
        listed = await user_service.list_org_users(session, ACME_ID, limit=10, offset=0)
    assert {item.email for item in listed} == {"carol@example.com", "boss@example.com"}


@pytest.mark.asyncio
async def test_me_api_keys_lists_the_keys_of_the_request_org(people: People) -> None:
    await _add_acme()
    boss = people["boss"]
    await _set_org_role(people.ids["boss"], "member", ACME_ID)
    home = await boss.post("/api/v1/me/api-keys", json={"name": "home", "scope": "read"})
    assert home.status_code == 201, home.text
    away = await boss.post(
        f"/api/v1/orgs/{ACME_SLUG}/me/api-keys", json={"name": "away", "scope": "read"}
    )
    assert away.status_code == 201, away.text

    assert [k["name"] for k in (await boss.get("/api/v1/me/api-keys")).json()] == ["home"]
    assert [
        k["name"] for k in (await boss.get(f"/api/v1/orgs/{ACME_SLUG}/me/api-keys")).json()
    ] == ["away"]


@pytest.mark.asyncio
async def test_auth_me_carries_every_membership(people: People) -> None:
    await _add_acme()
    await _set_org_role(people.ids["boss"], "admin", ACME_ID)
    me = (await people["boss"].get("/api/v1/auth/me")).json()
    assert me["role"] == "owner"  # the default org, on this org-free route
    assert me["is_platform_admin"] is True
    assert sorted(me["orgs"], key=lambda org: org["slug"]) == [
        {"slug": ACME_SLUG, "name": "Acme", "role": "admin", "status": "active"},
        {
            "slug": DEFAULT_ORG_SLUG,
            "name": "Default organization",
            "role": "owner",
            "status": "active",
        },
    ]
