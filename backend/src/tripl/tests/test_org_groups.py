"""Organization groups: ``/api/v1/orgs/{org}/groups`` (F20, GH #273).

* any member lists and reads groups; owners and admins create, rename, delete
  and manage members, from a browser session only;
* a group member must be a member of the organization; leaving the
  organization leaves its groups; purging the organization removes them;
* names are unique per organization (case-insensitively) and refuse a NUL;
* every change is audited as ``org.group.*``;
* another organization cannot see or touch the groups, and a group id of one
  organization is unknown under another's path;
* ``group_member_ids`` resolves only this organization's groups and members.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from tripl.main import app
from tripl.models.audit_log import AuditLog
from tripl.models.organization_group import OrganizationGroup, OrganizationGroupMember
from tripl.services import org_deletion_service, org_group_service
from tripl.tests._members import add_org_member
from tripl.tests._tenancy import use_multi_org
from tripl.tests.conftest import TestSessionLocal


@pytest.fixture(autouse=True)
def _more_organizations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Organizations are created over the API here: the Enterprise edition's (``_tenancy``)."""
    use_multi_org(monkeypatch)


PASSWORD = "Password123!"
API = "/api/v1"
ACME = "acme"
GLOBEX = "globex"
ACME_GROUPS = f"{API}/orgs/{ACME}/groups"
GLOBEX_GROUPS = f"{API}/orgs/{GLOBEX}/groups"


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class People:
    """``root`` registers first: owner of the default org and platform admin."""

    def __init__(self) -> None:
        self.clients: dict[str, AsyncClient] = {}
        self.ids: dict[str, uuid.UUID] = {}

    async def register(self, name: str) -> None:
        client = _new_client()
        resp = await client.post(
            f"{API}/auth/register",
            json={"email": f"{name}@example.com", "password": PASSWORD, "name": name},
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
    for name in ("root", "alice", "bob", "carol", "dave"):
        await crowd.register(name)
    try:
        yield crowd
    finally:
        await crowd.aclose()


@pytest.fixture
async def orgs(people: People) -> dict[str, uuid.UUID]:
    """``acme``: root owns it, alice member, bob admin, carol member.

    ``globex``: root (its creator) and dave own it; carol is a member of both.
    """
    ids: dict[str, uuid.UUID] = {}
    for slug, name in ((ACME, "Acme"), (GLOBEX, "Globex")):
        created = await people["root"].post(f"{API}/orgs", json={"slug": slug, "name": name})
        assert created.status_code == 201, created.text
        ids[slug] = uuid.UUID(created.json()["id"])
    async with TestSessionLocal() as session:
        await add_org_member(session, people.ids["alice"], "member", org_id=ids[ACME])
        await add_org_member(session, people.ids["bob"], "admin", org_id=ids[ACME])
        await add_org_member(session, people.ids["carol"], "member", org_id=ids[ACME])
        await add_org_member(session, people.ids["carol"], "member", org_id=ids[GLOBEX])
        await add_org_member(session, people.ids["dave"], "owner", org_id=ids[GLOBEX])
    return ids


async def _create(client: AsyncClient, url: str, name: str, **extra: Any) -> dict[str, Any]:
    resp = await client.post(url, json={"name": name, **extra})
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _audit(action: str) -> list[AuditLog]:
    async with TestSessionLocal() as session:
        return list(
            (await session.scalars(select(AuditLog).where(AuditLog.action == action))).all()
        )


# ── lifecycle ────────────────────────────────────────────────────────────────


async def test_an_admin_manages_a_group_end_to_end(
    people: People, orgs: dict[str, uuid.UUID]
) -> None:
    bob = people["bob"]
    group = await _create(bob, ACME_GROUPS, "  Analysts  ", description="Data people")
    assert group["name"] == "Analysts"
    assert group["description"] == "Data people"
    assert group["member_count"] == 0
    assert group["members"] == []
    url = f"{ACME_GROUPS}/{group['id']}"

    added = await bob.post(f"{url}/members", json={"user_id": str(people.ids["alice"])})
    assert added.status_code == 201, added.text
    assert added.json()["email"] == "alice@example.com"
    again = await bob.post(f"{url}/members", json={"user_id": str(people.ids["alice"])})
    assert again.status_code == 409, again.text
    await bob.post(f"{url}/members", json={"user_id": str(people.ids["carol"])})

    renamed = await bob.patch(url, json={"name": "Analytics"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Analytics"
    assert renamed.json()["description"] == "Data people"
    assert [m["name"] for m in renamed.json()["members"]] == ["alice", "carol"]

    listed = await people["alice"].get(ACME_GROUPS)
    assert listed.status_code == 200, listed.text
    assert [(g["name"], g["member_count"]) for g in listed.json()] == [("Analytics", 2)]

    removed = await bob.delete(f"{url}/members/{people.ids['carol']}")
    assert removed.status_code == 204, removed.text
    gone = await bob.delete(f"{url}/members/{people.ids['carol']}")
    assert gone.status_code == 404, gone.text

    deleted = await bob.delete(url)
    assert deleted.status_code == 204, deleted.text
    assert (await bob.get(url)).status_code == 404
    assert (await bob.get(ACME_GROUPS)).json() == []

    for action in (
        "org.group.create",
        "org.group.update",
        "org.group.delete",
        "org.group.member_add",
        "org.group.member_remove",
    ):
        rows = await _audit(action)
        assert rows, action
        assert all(row.organization_id == orgs[ACME] for row in rows), action
    [update] = await _audit("org.group.update")
    assert update.payload["changes"] == {"name": {"old": "Analysts", "new": "Analytics"}}
    [delete] = await _audit("org.group.delete")
    assert delete.payload == {"name": "Analytics", "members": 1}


async def test_a_no_op_update_files_no_audit_row(
    people: People, orgs: dict[str, uuid.UUID]
) -> None:
    group = await _create(people["root"], ACME_GROUPS, "Ops")
    same = await people["root"].patch(f"{ACME_GROUPS}/{group['id']}", json={"name": "Ops"})
    assert same.status_code == 200, same.text
    assert await _audit("org.group.update") == []


# ── validation ───────────────────────────────────────────────────────────────


async def test_names_are_unique_per_organization_case_insensitively(
    people: People, orgs: dict[str, uuid.UUID]
) -> None:
    root = people["root"]
    first = await _create(root, ACME_GROUPS, "Ops")
    clash = await root.post(ACME_GROUPS, json={"name": "ops"})
    assert clash.status_code == 409, clash.text
    other = await _create(root, ACME_GROUPS, "Sales")
    renamed_into = await root.patch(f"{ACME_GROUPS}/{other['id']}", json={"name": "OPS"})
    assert renamed_into.status_code == 409, renamed_into.text
    # A case-only rename of the group itself is fine.
    recased = await root.patch(f"{ACME_GROUPS}/{first['id']}", json={"name": "OPS"})
    assert recased.status_code == 200, recased.text
    # Another organization may use the same name.
    await _create(root, GLOBEX_GROUPS, "Ops")


async def test_the_database_enforces_case_insensitive_names(
    people: People, orgs: dict[str, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two requests racing past the pre-check still get a 409, not a second group."""
    root = people["root"]
    first = await _create(root, ACME_GROUPS, "Ops")
    other = await _create(root, ACME_GROUPS, "Sales")

    async def never_taken(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(org_group_service, "_name_taken", never_taken)
    clash = await root.post(ACME_GROUPS, json={"name": "ops"})
    assert clash.status_code == 409, clash.text
    renamed_into = await root.patch(f"{ACME_GROUPS}/{other['id']}", json={"name": "OPS"})
    assert renamed_into.status_code == 409, renamed_into.text
    names = [g["name"] for g in (await root.get(ACME_GROUPS)).json()]
    assert names == ["Ops", "Sales"]
    assert first["name"] == "Ops"


@pytest.mark.parametrize(
    "body",
    [
        {"name": "bad\x00name"},
        {"name": "ok", "description": "bad\x00description"},
        {"name": "   "},
        {"name": ""},
        {"name": "x" * 256},
        {"name": "ok", "unknown": 1},
    ],
)
async def test_invalid_bodies_are_422_never_500(
    people: People, orgs: dict[str, uuid.UUID], body: dict[str, Any]
) -> None:
    resp = await people["root"].post(ACME_GROUPS, json=body)
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize(
    "body", [{"name": "bad\x00"}, {"description": "x\x00"}, {"name": None}, {"description": None}]
)
async def test_invalid_updates_are_422(
    people: People, orgs: dict[str, uuid.UUID], body: dict[str, Any]
) -> None:
    group = await _create(people["root"], ACME_GROUPS, "Ops")
    resp = await people["root"].patch(f"{ACME_GROUPS}/{group['id']}", json=body)
    assert resp.status_code == 422, resp.text


async def test_a_nul_in_the_org_segment_is_404(people: People, orgs: dict[str, uuid.UUID]) -> None:
    resp = await people["root"].get(f"{API}/orgs/ac%00me/groups")
    assert resp.status_code == 404, resp.text


async def test_only_organization_members_can_join(
    people: People, orgs: dict[str, uuid.UUID]
) -> None:
    group = await _create(people["root"], ACME_GROUPS, "Ops")
    members = f"{ACME_GROUPS}/{group['id']}/members"
    # dave belongs to globex only; a random id to nobody.
    for user_id in (people.ids["dave"], uuid.uuid4()):
        resp = await people["root"].post(members, json={"user_id": str(user_id)})
        assert resp.status_code == 404, resp.text
    assert await _audit("org.group.member_add") == []


async def test_adding_to_a_group_deleted_meanwhile_is_not_found(
    people: People, orgs: dict[str, uuid.UUID]
) -> None:
    """A foreign-key failure is not reported as "already a member"."""
    group = await _create(people["root"], ACME_GROUPS, "Ops")
    async with TestSessionLocal() as session:
        loaded = await org_group_service.get_group(session, orgs[ACME], uuid.UUID(group["id"]))
        # Another request deletes the group after this one loaded it.
        await session.execute(delete(OrganizationGroup).where(OrganizationGroup.id == loaded.id))
        with pytest.raises(org_group_service.GroupNotFoundError):
            await org_group_service.add_member(session, loaded, people.ids["alice"])
        await session.rollback()


async def test_a_leftover_row_for_a_non_member_is_never_listed_or_counted(
    people: People, orgs: dict[str, uuid.UUID]
) -> None:
    """Group reads join the organization membership, like ``group_member_ids``."""
    root = people["root"]
    group = await _create(root, ACME_GROUPS, "Ops")
    alice = str(people.ids["alice"])
    added = await root.post(f"{ACME_GROUPS}/{group['id']}/members", json={"user_id": alice})
    assert added.status_code == 201, added.text
    async with TestSessionLocal() as session:
        # dave is not a member of acme: a row a lost race could have left behind.
        session.add(
            OrganizationGroupMember(group_id=uuid.UUID(group["id"]), user_id=people.ids["dave"])
        )
        await session.commit()

    detail = (await root.get(f"{ACME_GROUPS}/{group['id']}")).json()
    assert [m["user_id"] for m in detail["members"]] == [alice]
    assert detail["member_count"] == 1
    [listed] = (await root.get(ACME_GROUPS)).json()
    assert listed["member_count"] == 1


# ── who may do what ─────────────────────────────────────────────────────────


async def test_members_read_but_do_not_manage(people: People, orgs: dict[str, uuid.UUID]) -> None:
    group = await _create(people["root"], ACME_GROUPS, "Ops")
    url = f"{ACME_GROUPS}/{group['id']}"
    alice = people["alice"]
    assert (await alice.get(ACME_GROUPS)).status_code == 200
    assert (await alice.get(url)).status_code == 200
    for method, target, body in (
        ("POST", ACME_GROUPS, {"name": "Mine"}),
        ("PATCH", url, {"name": "Mine"}),
        ("DELETE", url, None),
        ("POST", f"{url}/members", {"user_id": str(people.ids["alice"])}),
        ("DELETE", f"{url}/members/{people.ids['alice']}", None),
    ):
        resp = await alice.request(method, target, json=body)
        assert resp.status_code == 403, f"{method} {target}: {resp.text}"


async def test_an_api_key_reads_groups_but_never_writes(
    people: People, orgs: dict[str, uuid.UUID]
) -> None:
    group = await _create(people["root"], ACME_GROUPS, "Ops")
    minted = await people["root"].post(
        f"{API}/orgs/{ACME}/me/api-keys", json={"name": "k", "scope": "write"}
    )
    assert minted.status_code == 201, minted.text
    headers = {"Authorization": f"Bearer {minted.json()['token']}"}
    async with _new_client() as client:
        listed = await client.get(ACME_GROUPS, headers=headers)
        assert listed.status_code == 200, listed.text
        created = await client.post(ACME_GROUPS, json={"name": "Keyed"}, headers=headers)
        assert created.status_code == 403, created.text
        deleted = await client.delete(f"{ACME_GROUPS}/{group['id']}", headers=headers)
        assert deleted.status_code == 403, deleted.text
        # A key of acme does not reach globex's groups at all.
        other = await client.get(GLOBEX_GROUPS, headers=headers)
        assert other.status_code == 404, other.text


# ── isolation ────────────────────────────────────────────────────────────────


async def test_another_organization_cannot_see_or_touch_the_groups(
    people: People, orgs: dict[str, uuid.UUID]
) -> None:
    group = await _create(people["root"], ACME_GROUPS, "Ops")
    await people["root"].post(
        f"{ACME_GROUPS}/{group['id']}/members", json={"user_id": str(people.ids["alice"])}
    )
    url = f"{ACME_GROUPS}/{group['id']}"
    dave = people["dave"]  # owner of globex, not in acme
    for method, target, body in (
        ("GET", ACME_GROUPS, None),
        ("GET", url, None),
        ("POST", ACME_GROUPS, {"name": "Probe"}),
        ("PATCH", url, {"name": "Probe"}),
        ("DELETE", url, None),
        ("POST", f"{url}/members", {"user_id": str(people.ids["dave"])}),
        ("DELETE", f"{url}/members/{people.ids['alice']}", None),
    ):
        resp = await dave.request(method, target, json=body)
        assert resp.status_code == 404, f"{method} {target}: {resp.text}"

    # Under globex's own path, acme's group id is unknown, to globex's owner and
    # to carol, who is a member of both.
    foreign = f"{GLOBEX_GROUPS}/{group['id']}"
    for client in (dave, people["carol"]):
        assert (await client.get(foreign)).status_code == 404
    for method, body in (("PATCH", {"name": "Probe"}), ("DELETE", None)):
        resp = await dave.request(method, foreign, json=body)
        assert resp.status_code == 404, resp.text
    resp = await dave.post(f"{foreign}/members", json={"user_id": str(people.ids["carol"])})
    assert resp.status_code == 404, resp.text
    assert (await dave.get(GLOBEX_GROUPS)).json() == []

    after = await people["root"].get(url)
    assert after.json()["name"] == "Ops"
    assert [m["email"] for m in after.json()["members"]] == ["alice@example.com"]


# ── membership follows the organization ─────────────────────────────────────


async def test_leaving_the_organization_leaves_its_groups(
    people: People, orgs: dict[str, uuid.UUID]
) -> None:
    root = people["root"]
    acme_group = await _create(root, ACME_GROUPS, "Ops")
    globex_group = await _create(root, GLOBEX_GROUPS, "Ops")
    carol = str(people.ids["carol"])
    await root.post(f"{ACME_GROUPS}/{acme_group['id']}/members", json={"user_id": carol})
    await root.post(f"{GLOBEX_GROUPS}/{globex_group['id']}/members", json={"user_id": carol})

    removed = await root.delete(f"{API}/orgs/{ACME}/members/{carol}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["group_memberships_removed"] == 1
    [audit] = await _audit("org.member_remove")
    assert audit.payload["group_memberships"] == 1

    assert (await root.get(f"{ACME_GROUPS}/{acme_group['id']}")).json()["members"] == []
    # Her globex group is untouched.
    kept = (await root.get(f"{GLOBEX_GROUPS}/{globex_group['id']}")).json()
    assert [m["user_id"] for m in kept["members"]] == [carol]


async def test_purging_the_organization_removes_its_groups(
    people: People, orgs: dict[str, uuid.UUID]
) -> None:
    root = people["root"]
    group = await _create(root, ACME_GROUPS, "Ops")
    await root.post(
        f"{ACME_GROUPS}/{group['id']}/members", json={"user_id": str(people.ids["alice"])}
    )
    await _create(root, GLOBEX_GROUPS, "Ops")
    async with TestSessionLocal() as session:
        await org_deletion_service.request_deletion(
            session, org_id=orgs[ACME], slug=ACME, confirm_slug=ACME
        )
        await session.commit()
        await org_deletion_service.purge_organization(session, orgs[ACME])
    async with TestSessionLocal() as session:
        groups = await session.scalar(select(func.count()).select_from(OrganizationGroup))
        members = await session.scalar(select(func.count()).select_from(OrganizationGroupMember))
    assert groups == 1  # globex's
    assert members == 0


# ── the reuse hook ───────────────────────────────────────────────────────────


async def test_group_member_ids_is_scoped_to_the_organization(
    people: People, orgs: dict[str, uuid.UUID]
) -> None:
    root = people["root"]
    ops = await _create(root, ACME_GROUPS, "Ops")
    sales = await _create(root, ACME_GROUPS, "Sales")
    foreign = await _create(root, GLOBEX_GROUPS, "Ops")
    for group_id, name in (
        (ops["id"], "alice"),
        (ops["id"], "carol"),
        (sales["id"], "carol"),
        (sales["id"], "bob"),
    ):
        resp = await root.post(
            f"{ACME_GROUPS}/{group_id}/members", json={"user_id": str(people.ids[name])}
        )
        assert resp.status_code == 201, resp.text
    await root.post(
        f"{GLOBEX_GROUPS}/{foreign['id']}/members", json={"user_id": str(people.ids["dave"])}
    )

    async with TestSessionLocal() as session:
        ids = await org_group_service.group_member_ids(
            session,
            orgs[ACME],
            [uuid.UUID(ops["id"]), uuid.UUID(sales["id"]), uuid.UUID(foreign["id"]), uuid.uuid4()],
        )
        assert ids == {people.ids["alice"], people.ids["bob"], people.ids["carol"]}
        assert await org_group_service.group_member_ids(session, orgs[ACME], []) == set()
        # globex's group asked for under acme contributes nobody.
        foreign_only = [uuid.UUID(foreign["id"])]
        assert await org_group_service.group_member_ids(session, orgs[ACME], foreign_only) == set()
