"""The organization management API (F20 PR6, GH #273).

* ``POST /orgs`` is a platform admin's on a self-hosted instance (any verified
  session's on a hosted one: ``test_hosted_signup.py``); the creator owns the
  new organization and ``/auth/me`` lists it at once;
* ``PATCH /orgs/{org}`` renames (owner/admin), never re-slugs (422);
* ``DELETE /orgs/{org}`` is an owner's, needs the typed slug, never takes the
  default organization, marks the row ``deleting`` (404 from then on) and queues
  the purge, which removes everything the organization owned;
* removing a member takes their project rows and revokes their keys there;
* ownership transfers; owners are managed by owners; the last owner stays;
* an owner of another organization cannot see, let alone manage, this one.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from tripl.config import settings
from tripl.main import app
from tripl.models.api_key import ApiKey
from tripl.models.app_setting import AppSetting
from tripl.models.audit_log import AuditLog
from tripl.models.data_source import DataSource
from tripl.models.doc_file import DocFile
from tripl.models.domain_enums import OrganizationRole
from tripl.models.event_photo import EventPhoto
from tripl.models.invitation import Invitation
from tripl.models.organization import Organization, OrganizationMember
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.services import org_deletion_service, org_service, user_service
from tripl.tests._members import add_org_member
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import org_delete

PASSWORD = "Password123!"
API = "/api/v1"
ACME = "acme"
ACME_URL = f"{API}/orgs/{ACME}"


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
    for name in ("root", "alice", "bob", "carol"):
        await crowd.register(name)
    try:
        yield crowd
    finally:
        await crowd.aclose()


async def _org_id(slug: str) -> uuid.UUID:
    async with TestSessionLocal() as session:
        org_id: uuid.UUID | None = await session.scalar(
            select(Organization.id).where(Organization.slug == slug)
        )
    assert org_id is not None
    return org_id


@pytest.fixture
async def acme(people: People) -> uuid.UUID:
    """``acme``: root owns it, alice is a member, bob an admin."""
    created = await people["root"].post(f"{API}/orgs", json={"slug": ACME, "name": "Acme"})
    assert created.status_code == 201, created.text
    org_id = uuid.UUID(created.json()["id"])
    async with TestSessionLocal() as session:
        await add_org_member(session, people.ids["alice"], "member", org_id=org_id)
        await add_org_member(session, people.ids["bob"], "admin", org_id=org_id)
    return org_id


async def _mint_key(client: AsyncClient, prefix: str, name: str = "key") -> str:
    resp = await client.post(f"{prefix}/me/api-keys", json={"name": name, "scope": "write"})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["token"])


async def _as_key(token: str, method: str, url: str, **kwargs: Any) -> Any:
    async with _new_client() as client:
        return await client.request(
            method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )


async def _audit(action: str) -> list[AuditLog]:
    async with TestSessionLocal() as session:
        return list(
            (await session.scalars(select(AuditLog).where(AuditLog.action == action))).all()
        )


# ── create ───────────────────────────────────────────────────────────────────


async def test_only_a_platform_admin_creates_an_organization(people: People) -> None:
    assert settings.deployment_mode == "self_hosted"
    refused = await people["alice"].post(f"{API}/orgs", json={"slug": ACME, "name": "Acme"})
    assert refused.status_code == 403, refused.text

    created = await people["root"].post(f"{API}/orgs", json={"slug": ACME, "name": " Acme "})
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["slug"], body["name"], body["role"], body["status"]) == (
        ACME,
        "Acme",
        "owner",
        "active",
    )
    assert body["is_default"] is False

    # The creator's /auth/me and /orgs list it at once.
    me = (await people["root"].get(f"{API}/auth/me")).json()
    assert {"slug": ACME, "name": "Acme", "role": "owner", "status": "active"} in me["orgs"]
    listed = (await people["root"].get(f"{API}/orgs")).json()
    assert ACME in {org["slug"] for org in listed}
    # Nobody else is in it.
    alice_orgs = (await people["alice"].get(f"{API}/orgs")).json()
    assert ACME not in {org["slug"] for org in alice_orgs}

    [entry] = await _audit("org.create")
    assert entry.organization_id == uuid.UUID(body["id"])


async def test_create_validates_the_slug(people: People) -> None:
    root = people["root"]
    for slug in ("Bad Slug", "settings", "orgs", "default", "-x", ""):
        resp = await root.post(f"{API}/orgs", json={"slug": slug, "name": "X"})
        assert resp.status_code == 422, (slug, resp.text)
    assert (await root.post(f"{API}/orgs", json={"slug": ACME, "name": "A"})).status_code == 201
    taken = await root.post(f"{API}/orgs", json={"slug": ACME, "name": "Again"})
    assert taken.status_code == 409, taken.text


async def test_a_platform_admin_key_cannot_create_an_organization(people: People) -> None:
    token = await _mint_key(people["root"], API)
    resp = await _as_key(token, "POST", f"{API}/orgs", json={"slug": ACME, "name": "Acme"})
    assert resp.status_code == 403, resp.text


# ── read and rename ─────────────────────────────────────────────────────────


async def test_members_read_admins_rename_and_the_slug_is_immutable(
    people: People, acme: uuid.UUID
) -> None:
    got = await people["alice"].get(ACME_URL)
    assert got.status_code == 200, got.text
    assert got.json()["role"] == "member"

    refused = await people["alice"].patch(ACME_URL, json={"name": "Alice Corp"})
    assert refused.status_code == 403, refused.text

    renamed = await people["bob"].patch(ACME_URL, json={"name": "Acme Inc"})
    assert renamed.status_code == 200, renamed.text
    assert (renamed.json()["slug"], renamed.json()["name"]) == (ACME, "Acme Inc")

    for body in ({"slug": "acme-2"}, {"name": "Acme", "slug": "acme-2"}):
        immutable = await people["root"].patch(ACME_URL, json=body)
        assert immutable.status_code == 422, immutable.text
    assert (await people["root"].get(ACME_URL)).json()["slug"] == ACME

    # A non-member: the organization does not exist for them.
    assert (await people["carol"].get(ACME_URL)).status_code == 404
    assert (await people["carol"].patch(ACME_URL, json={"name": "x"})).status_code == 404

    [entry] = await _audit("org.update")
    assert entry.organization_id == acme
    assert entry.payload == {"before": {"name": "Acme"}, "after": {"name": "Acme Inc"}}


async def test_members_list_is_the_organization_roster(people: People, acme: uuid.UUID) -> None:
    del acme
    resp = await people["alice"].get(f"{ACME_URL}/members")
    assert resp.status_code == 200, resp.text
    roles = {row["email"]: row["role"] for row in resp.json()}
    assert roles == {
        "root@example.com": "owner",
        "alice@example.com": "member",
        "bob@example.com": "admin",
    }


async def test_an_api_key_sees_only_its_own_organization(people: People, acme: uuid.UUID) -> None:
    del acme
    acme_key = await _mint_key(people["alice"], ACME_URL)
    default_key = await _mint_key(people["alice"], API)

    listed = await _as_key(acme_key, "GET", f"{API}/orgs")
    assert [org["slug"] for org in listed.json()] == [ACME]
    assert (await _as_key(acme_key, "GET", ACME_URL)).status_code == 200
    # A key of the default organization does not reach acme, member or not.
    assert (await _as_key(default_key, "GET", ACME_URL)).status_code == 404

    me = (await _as_key(acme_key, "GET", f"{API}/auth/me")).json()
    assert (me["org"], me["api_key_scope"], me["role"]) == (ACME, "write", "member")
    session_me = (await people["alice"].get(f"{API}/auth/me")).json()
    assert (session_me["org"], session_me["api_key_scope"]) == (None, None)

    # Keys never manage an organization, even an owner's.
    root_key = await _mint_key(people["root"], ACME_URL)
    renamed = await _as_key(root_key, "PATCH", ACME_URL, json={"name": "x"})
    assert renamed.status_code == 403, renamed.text


# ── members ─────────────────────────────────────────────────────────────────


async def test_role_changes_keep_owners_with_owners(people: People, acme: uuid.UUID) -> None:
    alice, bob = people.ids["alice"], people.ids["bob"]
    by_admin = await people["bob"].patch(f"{ACME_URL}/members/{alice}", json={"role": "owner"})
    assert by_admin.status_code == 403, by_admin.text
    by_member = await people["alice"].patch(f"{ACME_URL}/members/{bob}", json={"role": "member"})
    assert by_member.status_code == 403, by_member.text

    last = await people["root"].patch(
        f"{ACME_URL}/members/{people.ids['root']}", json={"role": "admin"}
    )
    assert last.status_code == 400, last.text
    unknown = await people["root"].patch(
        f"{ACME_URL}/members/{people.ids['carol']}", json={"role": "admin"}
    )
    assert unknown.status_code == 404, unknown.text

    promoted = await people["bob"].patch(f"{ACME_URL}/members/{alice}", json={"role": "admin"})
    assert promoted.status_code == 200, promoted.text
    # The member's own /auth/me says so at once.
    me = (await people["alice"].get(f"{API}/auth/me")).json()
    assert {"slug": ACME, "name": "Acme", "role": "admin", "status": "active"} in me["orgs"]
    [entry] = await _audit("org.member_role_update")
    assert entry.organization_id == acme


async def test_removing_a_member_takes_their_project_rows_and_keys(
    people: People, acme: uuid.UUID
) -> None:
    root, alice_id = people["root"], people.ids["alice"]
    project = await root.post(f"{ACME_URL}/projects", json={"name": "Web", "slug": "web"})
    assert project.status_code == 201, project.text
    project_id = uuid.UUID(project.json()["id"])
    added = await root.post(
        f"{ACME_URL}/projects/web/members", json={"user_id": str(alice_id), "role": "editor"}
    )
    assert added.status_code in (200, 201), added.text
    acme_key = await _mint_key(people["alice"], ACME_URL, "acme")
    default_key = await _mint_key(people["alice"], API, "default")
    assert (await _as_key(acme_key, "GET", f"{ACME_URL}/projects")).status_code == 200

    refused = await people["alice"].delete(f"{ACME_URL}/members/{people.ids['bob']}")
    assert refused.status_code == 403, refused.text

    removed = await people["bob"].delete(f"{ACME_URL}/members/{alice_id}")
    assert removed.status_code == 200, removed.text
    assert removed.json() == {
        "user_id": str(alice_id),
        "project_memberships_removed": 1,
        "api_keys_revoked": 1,
        "invitations_revoked": 0,
        "group_memberships_removed": 0,
    }

    async with TestSessionLocal() as session:
        rows = await session.scalar(
            select(func.count())
            .select_from(ProjectMember)
            .where(ProjectMember.project_id == project_id, ProjectMember.user_id == alice_id)
        )
        membership = await session.scalar(
            select(OrganizationMember.id).where(
                OrganizationMember.organization_id == acme,
                OrganizationMember.user_id == alice_id,
            )
        )
    assert rows == 0
    assert membership is None
    assert (await _as_key(acme_key, "GET", f"{ACME_URL}/projects")).status_code == 401
    # Their other organization is untouched.
    assert (await _as_key(default_key, "GET", f"{API}/auth/me")).status_code == 200
    assert (await people["alice"].get(ACME_URL)).status_code == 404
    me = (await people["alice"].get(f"{API}/auth/me")).json()
    assert ACME not in {org["slug"] for org in me["orgs"]}
    [entry] = await _audit("org.member_remove")
    assert entry.organization_id == acme


async def test_owners_are_removed_by_owners_and_the_last_one_stays(
    people: People, acme: uuid.UUID
) -> None:
    del acme
    root_id = people.ids["root"]
    by_admin = await people["bob"].delete(f"{ACME_URL}/members/{root_id}")
    assert by_admin.status_code == 403, by_admin.text
    last = await people["root"].delete(f"{ACME_URL}/members/{root_id}")
    assert last.status_code == 400, last.text
    unknown = await people["root"].delete(f"{ACME_URL}/members/{people.ids['carol']}")
    assert unknown.status_code == 404, unknown.text


async def test_ownership_transfers_to_another_member(people: People, acme: uuid.UUID) -> None:
    alice_id = people.ids["alice"]
    by_admin = await people["bob"].post(
        f"{ACME_URL}/transfer-ownership", json={"user_id": str(alice_id)}
    )
    assert by_admin.status_code == 403, by_admin.text
    to_self = await people["root"].post(
        f"{ACME_URL}/transfer-ownership", json={"user_id": str(people.ids["root"])}
    )
    assert to_self.status_code == 400, to_self.text
    to_stranger = await people["root"].post(
        f"{ACME_URL}/transfer-ownership", json={"user_id": str(people.ids["carol"])}
    )
    assert to_stranger.status_code == 404, to_stranger.text

    moved = await people["root"].post(
        f"{ACME_URL}/transfer-ownership", json={"user_id": str(alice_id)}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["role"] == "owner"
    roles = {
        row["email"]: row["role"]
        for row in (await people["root"].get(f"{ACME_URL}/members")).json()
    }
    assert roles["alice@example.com"] == "owner"
    assert roles["root@example.com"] == "admin"
    # The former owner is an admin now: owner-only routes refuse them.
    again = await people["root"].post(
        f"{ACME_URL}/transfer-ownership", json={"user_id": str(people.ids["bob"])}
    )
    assert again.status_code == 403, again.text
    [entry] = await _audit("org.transfer_ownership")
    assert entry.organization_id == acme


# ── another organization's owner ────────────────────────────────────────────


async def test_an_owner_of_another_organization_cannot_manage_this_one(
    people: People, acme: uuid.UUID
) -> None:
    del acme
    beta = await people["root"].post(f"{API}/orgs", json={"slug": "beta", "name": "Beta"})
    assert beta.status_code == 201, beta.text
    async with TestSessionLocal() as session:
        await add_org_member(session, people.ids["carol"], "owner", org_id=await _org_id("beta"))
    carol, alice_id = people["carol"], people.ids["alice"]
    carol_key = await _mint_key(carol, f"{API}/orgs/beta")

    probes: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", ACME_URL, None),
        ("PATCH", ACME_URL, {"name": "Mine"}),
        ("DELETE", ACME_URL, {"confirm_slug": ACME}),
        ("GET", f"{ACME_URL}/members", None),
        ("PATCH", f"{ACME_URL}/members/{alice_id}", {"role": "admin"}),
        ("DELETE", f"{ACME_URL}/members/{alice_id}", None),
        ("POST", f"{ACME_URL}/transfer-ownership", {"user_id": str(alice_id)}),
    ]
    for method, url, body in probes:
        resp = await carol.request(method, url, json=body)
        assert resp.status_code == 404, (method, url, resp.text)
        assert resp.json()["detail"] == "Organization not found"
        keyed = await _as_key(carol_key, method, url, json=body)
        assert keyed.status_code == 404, (method, url, keyed.text)
    # The same answer as an organization nobody has.
    assert (await carol.get(f"{API}/orgs/no-such-org")).json()["detail"] == "Organization not found"
    # Carol's own organization is hers to manage.
    assert (await carol.patch(f"{API}/orgs/beta", json={"name": "Beta 2"})).status_code == 200


# ── delete ───────────────────────────────────────────────────────────────────


async def test_the_default_organization_cannot_be_deleted(people: People) -> None:
    resp = await people["root"].request(
        "DELETE", f"{API}/orgs/default", json={"confirm_slug": "default"}
    )
    assert resp.status_code == 400, resp.text
    assert (await people["root"].get(f"{API}/orgs/default")).json()["status"] == "active"


async def test_delete_needs_an_owner_and_the_typed_slug(
    people: People, acme: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued: list[str] = []
    monkeypatch.setattr(org_delete.purge_organization, "delay", queued.append)

    by_admin = await people["bob"].request("DELETE", ACME_URL, json={"confirm_slug": ACME})
    assert by_admin.status_code == 403, by_admin.text
    mistyped = await people["root"].request("DELETE", ACME_URL, json={"confirm_slug": "acm"})
    assert mistyped.status_code == 400, mistyped.text
    missing = await people["root"].request("DELETE", ACME_URL, json={})
    assert missing.status_code == 422, missing.text
    assert queued == []

    accepted = await people["root"].request("DELETE", ACME_URL, json={"confirm_slug": ACME})
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["status"] == "deleting"
    assert queued == [str(acme)]
    [entry] = await _audit("org.delete_request")
    assert entry.organization_id == acme


async def test_a_failed_queue_leaves_the_organization_alone(
    people: People, acme: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _broker_down(_org_id: str) -> None:
        raise ConnectionError("broker down")

    monkeypatch.setattr(org_delete.purge_organization, "delay", _broker_down)
    resp = await people["root"].request("DELETE", ACME_URL, json={"confirm_slug": ACME})
    assert resp.status_code == 503, resp.text
    assert (await people["root"].get(ACME_URL)).json()["status"] == "active"
    # The request is on record, and so is its undoing.
    [requested] = await _audit("org.delete_request")
    [cancelled] = await _audit("org.delete_cancel")
    assert requested.organization_id == cancelled.organization_id == acme
    assert cancelled.created_at >= requested.created_at


class _FakeStorage:
    def __init__(self, deleted: list[str]) -> None:
        self.deleted = deleted

    async def delete(self, key: str) -> None:
        self.deleted.append(key)

    def list_objects(self, prefix: str) -> Iterator[Any]:
        # The purge's last pass lists the organization's prefix: nothing left.
        del prefix
        return iter(())


async def _seed_acme(people: People, acme: uuid.UUID) -> dict[str, str]:
    """A project with an event, a photo, members, a source, keys, docs, an invitation."""
    root = people["root"]
    project = await root.post(f"{ACME_URL}/projects", json={"name": "Web", "slug": "web"})
    assert project.status_code == 201, project.text
    base = f"{ACME_URL}/projects/web"
    event_type = await root.post(
        f"{base}/event-types", json={"name": "checkout", "display_name": "Checkout"}
    )
    assert event_type.status_code == 201, event_type.text
    event = await root.post(
        f"{base}/events", json={"event_type_id": event_type.json()["id"], "name": "purchase"}
    )
    assert event.status_code == 201, event.text
    await root.post(f"{base}/members", json={"user_id": str(people.ids["alice"]), "role": "editor"})
    source = await root.post(
        f"{ACME_URL}/data-sources",
        json={
            "name": "warehouse",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 1,
            "database_name": "analytics",
        },
    )
    assert source.status_code == 201, source.text
    doc = await root.put(
        f"{base}/docs/file",
        params={"scope": "organization", "path": "handbook.md"},
        json={"content": "# Handbook\n\nOrg note"},
    )
    assert doc.status_code in (200, 201), doc.text
    invited = await root.post(f"{ACME_URL}/users/invitations", json={"email": "new@example.com"})
    assert invited.status_code == 201, invited.text
    token = await _mint_key(people["alice"], ACME_URL)
    async with TestSessionLocal() as session:
        session.add(
            EventPhoto(
                project_id=uuid.UUID(project.json()["id"]),
                event_id=uuid.UUID(event.json()["id"]),
                original_filename="shot.png",
                kind="photo",
                storage_backend="local",
                storage_key="photos/acme-shot.png",
            )
        )
        session.add(AppSetting(key="service", value={"x": 1}, organization_id=acme))
        await session.commit()
    return {"token": token}


async def _count(model: Any, org_id: uuid.UUID) -> int:
    async with TestSessionLocal() as session:
        value: int | None = await session.scalar(
            select(func.count()).select_from(model).where(model.organization_id == org_id)
        )
    return int(value or 0)


async def test_deleting_marks_the_organization_gone_and_the_job_purges_it(
    people: People, acme: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed_acme(people, acme)
    monkeypatch.setattr(org_delete.purge_organization, "delay", lambda _org_id: None)
    deleted: list[str] = []
    monkeypatch.setattr(org_deletion_service, "storage_for", lambda _backend: _FakeStorage(deleted))

    accepted = await people["root"].request("DELETE", ACME_URL, json={"confirm_slug": ACME})
    assert accepted.status_code == 202, accepted.text

    # From here on the organization answers like one that never existed.
    for url in (ACME_URL, f"{ACME_URL}/members", f"{ACME_URL}/projects/web/events"):
        assert (await people["root"].get(url)).status_code == 404, url
    assert (await _as_key(seeded["token"], "GET", f"{API}/auth/me")).status_code == 404
    me = (await people["alice"].get(f"{API}/auth/me")).json()
    assert ACME not in {org["slug"] for org in me["orgs"]}
    assert ACME not in {org["slug"] for org in (await people["root"].get(f"{API}/orgs")).json()}
    # Nothing is gone yet: the job does that.
    assert await _count(Project, acme) == 1

    async with TestSessionLocal() as session:
        result = await org_deletion_service.purge_organization(session, acme)
    assert result is not None
    assert (result.slug, result.projects, result.blobs_deleted) == (ACME, 1, 1)
    assert deleted == ["photos/acme-shot.png"]

    for model in (Project, DataSource, ApiKey, Invitation, DocFile, AppSetting, OrganizationMember):
        assert await _count(model, acme) == 0, model.__name__
    async with TestSessionLocal() as session:
        assert await session.get(Organization, acme) is None
        photos = await session.scalar(select(func.count()).select_from(EventPhoto))
        members = await session.scalar(select(func.count()).select_from(ProjectMember))
    assert photos == 0
    assert members == 0

    [done] = await _audit("org.delete_complete")
    assert done.organization_id is None
    assert done.payload["slug"] == ACME
    # The slug is free again, and a re-delivered job finds nothing to do.
    async with TestSessionLocal() as session:
        assert await org_deletion_service.purge_organization(session, acme) is None
    again = await people["root"].post(f"{API}/orgs", json={"slug": ACME, "name": "Acme II"})
    assert again.status_code == 201, again.text


async def test_the_purge_refuses_an_active_organization(people: People, acme: uuid.UUID) -> None:
    del people
    async with TestSessionLocal() as session:
        assert await org_deletion_service.purge_organization(session, acme) is None
        assert await session.get(Organization, acme) is not None


# ── pending invitations follow the member out ───────────────────────────────


async def _invite(client: AsyncClient, email: str, role: str) -> str:
    created = await client.post(
        f"{ACME_URL}/users/invitations", json={"email": email, "role": role}
    )
    assert created.status_code == 201, created.text
    return str(created.json()["accept_path"]).rsplit("/", 1)[-1]


async def _redeem(token: str) -> Any:
    async with _new_client() as anon:
        return await anon.post(
            f"{API}/auth/invitations/{token}/accept", json={"password": PASSWORD}
        )


async def test_a_removed_admin_cannot_come_back_through_a_link_they_minted(
    people: People, acme: uuid.UUID
) -> None:
    # Bob (admin) mints an admin invitation to an address he controls.
    backdoor = await _invite(people["bob"], "bob-alt@example.com", "admin")
    # An invitation someone else sent to a third party is not bob's to lose.
    unrelated = await _invite(people["root"], "dave@example.com", "member")

    removed = await people["root"].delete(f"{ACME_URL}/members/{people.ids['bob']}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["invitations_revoked"] == 1
    [entry] = await _audit("org.member_remove")
    assert entry.payload["invitations"] == 1

    assert (await _redeem(backdoor)).status_code == 400
    assert (await _redeem(unrelated)).status_code == 201


async def test_an_invitation_addressed_to_the_removed_member_dies_with_the_membership(
    people: People, acme: uuid.UUID
) -> None:
    # Carol is invited (not yet a member), then joins some other way.
    token = await _invite(people["root"], "carol@example.com", "admin")
    async with TestSessionLocal() as session:
        await add_org_member(session, people.ids["carol"], "member", org_id=acme)

    removed = await people["root"].delete(f"{ACME_URL}/members/{people.ids['carol']}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["invitations_revoked"] == 1
    back = await people["carol"].post(f"{API}/auth/invitations/{token}/accept", json={})
    assert back.status_code == 400, back.text


async def test_a_demotion_drops_the_invitations_the_new_role_cannot_grant(
    people: People, acme: uuid.UUID
) -> None:
    del acme
    as_admin = await _invite(people["bob"], "eve@example.com", "admin")
    as_member = await _invite(people["bob"], "frank@example.com", "member")

    demoted = await people["root"].patch(
        f"{ACME_URL}/members/{people.ids['bob']}", json={"role": "member"}
    )
    assert demoted.status_code == 200, demoted.text
    [entry] = await _audit("org.member_role_update")
    assert entry.payload["invitations_revoked"] == 1
    assert (await _redeem(as_admin)).status_code == 400
    assert (await _redeem(as_member)).status_code == 201


async def test_transferring_ownership_drops_the_old_owners_owner_invitations(
    people: People, acme: uuid.UUID
) -> None:
    del acme
    as_owner = await _invite(people["root"], "grace@example.com", "owner")
    as_admin = await _invite(people["root"], "heidi@example.com", "admin")
    moved = await people["root"].post(
        f"{ACME_URL}/transfer-ownership", json={"user_id": str(people.ids["bob"])}
    )
    assert moved.status_code == 200, moved.text
    assert (await _redeem(as_owner)).status_code == 400
    assert (await _redeem(as_admin)).status_code == 201


# ── the actor's role is read under the owner-set lock ──────────────────────


async def test_owner_set_changes_reread_the_actors_role(people: People, acme: uuid.UUID) -> None:
    """The gate's role is read before the lock; the services must not trust it.

    Simulates the race: the actor passed the gate as an owner (or admin), then
    lost the role before its own transaction took the lock.
    """
    root, alice, bob = people.ids["root"], people.ids["alice"], people.ids["bob"]
    async with TestSessionLocal() as session:
        # Bob (admin at the gate) has since been demoted to member.
        await session.execute(
            update(OrganizationMember)
            .where(OrganizationMember.organization_id == acme, OrganizationMember.user_id == bob)
            .values(role="member")
        )
        await session.commit()
        with pytest.raises(user_service.OwnerManagementError):
            await org_service.remove_member(session, acme, alice, actor_id=bob)
        await session.rollback()
        with pytest.raises(user_service.OwnerManagementError):
            await user_service.update_org_role(
                session, acme, alice, OrganizationRole.admin, actor_id=bob
            )
        await session.rollback()

        # Alice is made a second owner, then root is demoted to admin: root's
        # in-flight transfer must not go through.
        for user_id, role in ((alice, "owner"), (root, "admin")):
            await session.execute(
                update(OrganizationMember)
                .where(
                    OrganizationMember.organization_id == acme,
                    OrganizationMember.user_id == user_id,
                )
                .values(role=role)
            )
        await session.commit()
        with pytest.raises(user_service.OwnerManagementError):
            await org_service.transfer_ownership(session, acme, actor_id=root, target_id=bob)
        await session.rollback()
        # ...nor may the now-admin root demote or remove the remaining owner.
        with pytest.raises(user_service.OwnerManagementError):
            await user_service.update_org_role(
                session, acme, alice, OrganizationRole.member, actor_id=root
            )
        await session.rollback()
        with pytest.raises(user_service.OwnerManagementError):
            await org_service.remove_member(session, acme, alice, actor_id=root)
        await session.rollback()


# ── a purge that gave up is picked up again ─────────────────────────────────


async def test_a_stranded_deletion_is_claimed_once_per_grace_period(
    people: People, acme: uuid.UUID
) -> None:
    del people
    now = datetime.now(UTC)
    async with TestSessionLocal() as session:
        await session.execute(
            update(Organization)
            .where(Organization.id == acme)
            .values(status="deleting", updated_at=now - timedelta(minutes=30))
        )
        await session.commit()
        # A row a live job could still be working on is left alone.
        assert await org_deletion_service.requeue_stranded_deletions(session, now=now) == []

        later = now + org_deletion_service.STRANDED_DELETION_GRACE
        assert await org_deletion_service.requeue_stranded_deletions(session, now=later) == [acme]
        # Claimed: the next pass does not queue it a second time.
        assert await org_deletion_service.requeue_stranded_deletions(session, now=later) == []
        # An active organization is never picked up.
        far = later + 2 * org_deletion_service.STRANDED_DELETION_GRACE
        assert await org_deletion_service.requeue_stranded_deletions(session, now=far) == [acme]
        await session.execute(
            update(Organization).where(Organization.id == acme).values(status="active")
        )
        await session.commit()
        far += 2 * org_deletion_service.STRANDED_DELETION_GRACE
        assert await org_deletion_service.requeue_stranded_deletions(session, now=far) == []


def test_the_chaser_queues_a_purge_for_each_claimed_organization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stranded = [uuid.uuid4(), uuid.uuid4()]

    async def _claim(_session: Any) -> list[uuid.UUID]:
        return stranded

    async def _no_db(run: Any) -> None:
        await run(None)

    queued: list[str] = []
    monkeypatch.setattr(org_deletion_service, "requeue_stranded_deletions", _claim)
    monkeypatch.setitem(
        org_delete.requeue_stranded_org_deletions.run.__globals__,
        "run_with_async_worker_session",
        _no_db,
    )
    monkeypatch.setattr(org_delete.purge_organization, "delay", queued.append)

    result = org_delete.requeue_stranded_org_deletions.run()

    assert queued == [str(org_id) for org_id in stranded]
    assert result == {"requeued": queued}
    from tripl.worker.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["requeue-stranded-org-deletions"]
    assert entry["task"] == org_delete.requeue_stranded_org_deletions.name
