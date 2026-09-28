"""The organization default project role and the per-project opt-out (F20, GH #273).

``organizations.default_project_role`` is the project role an organization
``member`` gets on a project they hold no ``project_members`` row in: ``none``
(the default: the project is a 404), ``viewer`` or ``editor``. A row always
wins, below the default as well as above it, and a ``none`` row opts one member
out of one project. Owners and admins of the organization are ``owner``
everywhere, whatever the default or their rows.

Covered on every surface that answers "who reaches this project":
the route gate and write gate over HTTP, ``member_project_ids``,
``members_among``, the sync notification fan-out, and the SSE ``still_member``
check; then ``PATCH /orgs/{org}`` (validation, audit), the project-member API's
``none`` role, and the migration's place in the chain.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.models import Base
from tripl.models.audit_log import AuditLog
from tripl.models.domain_enums import OrganizationRole
from tripl.models.organization import Organization, OrganizationMember
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.services import notification_service, project_access
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_alembic_revisions import _load_migration
from tripl.tests.test_org_roles_matrix import (
    People,
    _create_project,
    _project_id,
    _set_org_role,
    _user,
)
from tripl.tests.test_organizations_migration_pg import LATER_MIGRATIONS

API = "/api/v1"
ORG_URL = f"{API}/orgs/default"
SLUG = "roleplay"
MIGRATION = "d2f4a6c8e0b1_default_project_role.py"


@pytest.fixture
async def people() -> AsyncIterator[People]:
    crowd = People()
    # The first account owns the default organization.
    await crowd.register("boss")
    yield crowd
    await crowd.aclose()


async def _member(people: People, name: str, org_role: str = "member") -> None:
    await people.register(name)
    await _set_org_role(people.ids[name], org_role)


async def _set_default(people: People, role: str) -> None:
    resp = await people["boss"].patch(ORG_URL, json={"default_project_role": role})
    assert resp.status_code == 200, resp.text
    assert resp.json()["default_project_role"] == role


async def _write_status(people: People, name: str) -> int:
    resp = await people[name].post(
        f"{API}/projects/{SLUG}/event-types",
        json={"name": f"pv_{uuid.uuid4().hex[:6]}", "display_name": "PV"},
    )
    return resp.status_code


async def _read(people: People, name: str) -> tuple[int, str | None, bool]:
    detail = await people[name].get(f"{API}/projects/{SLUG}")
    listed = {p["slug"] for p in (await people[name].get(f"{API}/projects")).json()}
    role = detail.json().get("my_role") if detail.status_code == 200 else None
    return detail.status_code, role, SLUG in listed


# ── the route gate and the write gate ───────────────────────────────────────


async def test_default_none_keeps_projects_invisible(people: People) -> None:
    await _create_project(people["boss"], SLUG)
    await _member(people, "mia")

    assert (await people["boss"].get(ORG_URL)).json()["default_project_role"] == "none"
    assert await _read(people, "mia") == (404, None, False)
    assert await _write_status(people, "mia") == 404


async def test_default_viewer_reads_but_cannot_write(people: People) -> None:
    await _create_project(people["boss"], SLUG)
    await _member(people, "mia")
    await _set_default(people, "viewer")

    assert await _read(people, "mia") == (200, "viewer", True)
    assert await _write_status(people, "mia") == 403


async def test_default_editor_writes(people: People) -> None:
    await _create_project(people["boss"], SLUG)
    await _member(people, "mia")
    await _set_default(people, "editor")

    assert await _read(people, "mia") == (200, "editor", True)
    assert await _write_status(people, "mia") == 201


async def test_an_explicit_viewer_row_wins_over_an_editor_default(people: People) -> None:
    await _create_project(people["boss"], SLUG)
    await _member(people, "mia")
    await add_member_by_slug(SLUG, "mia@example.com", "viewer")
    await _set_default(people, "editor")

    assert await _read(people, "mia") == (200, "viewer", True)
    assert await _write_status(people, "mia") == 403


async def test_a_none_row_opts_out_under_a_viewer_default(people: People) -> None:
    await _create_project(people["boss"], SLUG)
    await _member(people, "mia")
    await _member(people, "noah")
    await add_member_by_slug(SLUG, "noah@example.com", "none")
    await _set_default(people, "viewer")

    assert await _read(people, "mia") == (200, "viewer", True)
    assert await _read(people, "noah") == (404, None, False)
    assert await _write_status(people, "noah") == 404


@pytest.mark.parametrize("org_role", ["owner", "admin"])
async def test_org_owners_and_admins_are_unaffected(people: People, org_role: str) -> None:
    await _create_project(people["boss"], SLUG)
    await _member(people, "ada", org_role)
    # A stale ``none`` row (the API refuses to write one) changes nothing either.
    await add_member_by_slug(SLUG, "ada@example.com", "none")
    for default in ("none", "viewer", "editor"):
        await _set_default(people, default)
        assert await _read(people, "ada") == (200, "owner", True)
        assert await _write_status(people, "ada") == 201


# ── the service helpers and fan-outs ────────────────────────────────────────


async def test_member_project_ids_members_among_and_still_member_follow_the_default(
    people: People,
) -> None:
    await _create_project(people["boss"], SLUG)
    await _member(people, "mia")  # no row
    await _member(people, "noah")  # ``none`` row
    await _member(people, "vic")  # ``viewer`` row
    await add_member_by_slug(SLUG, "noah@example.com", "none")
    await add_member_by_slug(SLUG, "vic@example.com", "viewer")
    project_id = await _project_id(SLUG)
    mia, noah, vic = [await _user(people.ids[name]) for name in ("mia", "noah", "vic")]
    everyone = {mia.id, noah.id, vic.id}

    async def _answers() -> tuple[set[uuid.UUID], set[uuid.UUID], set[uuid.UUID]]:
        async with TestSessionLocal() as session:
            visible = {
                user.id
                for user in (mia, noah, vic)
                if project_id in await project_access.member_project_ids(session, user)
            }
            among = await project_access.members_among(session, project_id, everyone)
        streaming = {
            user.id
            for user in (mia, noah, vic)
            if await project_access.still_member(
                TestSessionLocal, user_id=user.id, project_id=project_id
            )
        }
        return visible, among, streaming

    assert await _answers() == ({vic.id},) * 3

    await _set_default(people, "viewer")
    assert await _answers() == ({mia.id, vic.id},) * 3

    # Lowered again: the member who had it from the default loses the stream.
    await _set_default(people, "none")
    assert await _answers() == ({vic.id},) * 3


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'default_project_role.db'}")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _sync_user(
    session: Session, org_id: uuid.UUID, email: str, role: OrganizationRole
) -> uuid.UUID:
    user = User(id=uuid.uuid4(), email=email, name=email.split("@")[0], password_hash="x")
    session.add(user)
    session.flush()
    session.add(OrganizationMember(organization_id=org_id, user_id=user.id, role=role.value))
    session.flush()
    return user.id


@pytest.mark.parametrize(
    ("default", "reached"),
    [("none", {"admin", "vic"}), ("viewer", {"admin", "vic", "mia"})],
)
def test_notification_recipients_follow_the_default(
    factory: sessionmaker[Session], default: str, reached: set[str]
) -> None:
    with factory() as session:
        org = Organization(
            id=uuid.uuid4(), slug="org-a", name="Org A", default_project_role=default
        )
        other = Organization(id=uuid.uuid4(), slug="org-b", name="Org B")
        session.add_all([org, other])
        session.flush()
        project = Project(id=uuid.uuid4(), name="Shop", slug="shop", organization_id=org.id)
        session.add(project)
        session.flush()
        ids = {
            "admin": _sync_user(session, org.id, "admin@example.com", OrganizationRole.admin),
            "mia": _sync_user(session, org.id, "mia@example.com", OrganizationRole.member),
            "noah": _sync_user(session, org.id, "noah@example.com", OrganizationRole.member),
            "vic": _sync_user(session, org.id, "vic@example.com", OrganizationRole.member),
            "bea": _sync_user(session, other.id, "bea@example.com", OrganizationRole.member),
        }
        session.add_all(
            [
                ProjectMember(project_id=project.id, user_id=ids["noah"], role="none"),
                ProjectMember(project_id=project.id, user_id=ids["vic"], role="viewer"),
            ]
        )
        session.commit()

        among = notification_service._members_among_sync(session, project.id, set(ids.values()))
        assert among == {ids[name] for name in reached}


# ── PATCH /orgs/{org} ───────────────────────────────────────────────────────


async def test_patch_default_project_role_validates_and_audits(people: People) -> None:
    await _member(people, "mia")
    await _member(people, "ada", "admin")

    for bad in ("owner", "admin", "", None):
        refused = await people["boss"].patch(ORG_URL, json={"default_project_role": bad})
        assert refused.status_code == 422, refused.text
    assert (await people["boss"].patch(ORG_URL, json={})).status_code == 422
    # A plain member cannot change it.
    forbidden = await people["mia"].patch(ORG_URL, json={"default_project_role": "editor"})
    assert forbidden.status_code == 403, forbidden.text

    # An admin can; GET shows it to any member.
    changed = await people["ada"].patch(ORG_URL, json={"default_project_role": "viewer"})
    assert changed.status_code == 200, changed.text
    assert (await people["mia"].get(ORG_URL)).json()["default_project_role"] == "viewer"
    listed = (await people["mia"].get(f"{API}/orgs")).json()
    assert {org["slug"]: org["default_project_role"] for org in listed}["default"] == "viewer"

    # The same value again is no change and files nothing.
    again = await people["ada"].patch(ORG_URL, json={"default_project_role": "viewer"})
    assert again.status_code == 200, again.text

    async with TestSessionLocal() as session:
        entries = list(
            (await session.scalars(select(AuditLog).where(AuditLog.action == "org.update"))).all()
        )
    assert [entry.payload for entry in entries] == [
        {"before": {"default_project_role": "none"}, "after": {"default_project_role": "viewer"}}
    ]
    assert entries[0].user_id == people.ids["ada"]


# ── the project-member API's ``none`` ───────────────────────────────────────


async def test_members_api_sets_none_for_a_member_and_refuses_it_for_an_admin(
    people: People,
) -> None:
    await _create_project(people["boss"], SLUG)
    await _member(people, "mia")
    await _member(people, "vic")
    await _member(people, "ada", "admin")
    await _set_default(people, "viewer")
    members_url = f"{API}/projects/{SLUG}/members"

    added = await people["boss"].post(
        members_url, json={"user_id": str(people.ids["mia"]), "role": "none"}
    )
    assert added.status_code == 201, added.text
    assert added.json()["role"] == "none"
    listed = {row["email"]: row["role"] for row in (await people["boss"].get(members_url)).json()}
    assert listed["mia@example.com"] == "none"
    assert await _read(people, "mia") == (404, None, False)

    # Re-roling an existing row to ``none`` works the same way, and back again.
    await add_member_by_slug(SLUG, "vic@example.com", "editor")
    demoted = await people["boss"].patch(
        f"{members_url}/{people.ids['vic']}", json={"role": "none"}
    )
    assert demoted.status_code == 200, demoted.text
    assert await _read(people, "vic") == (404, None, False)
    # Removing the row puts the member back on the default.
    removed = await people["boss"].delete(f"{members_url}/{people.ids['vic']}")
    assert removed.status_code == 204, removed.text
    assert await _read(people, "vic") == (200, "viewer", True)

    # An org owner/admin always has access: ``none`` for them is refused.
    for body_user in (people.ids["ada"], people.ids["boss"]):
        refused = await people["boss"].post(
            members_url, json={"user_id": str(body_user), "role": "none"}
        )
        assert refused.status_code == 422, refused.text
    await add_member_by_slug(SLUG, "ada@example.com", "editor")
    refused_update = await people["boss"].patch(
        f"{members_url}/{people.ids['ada']}", json={"role": "none"}
    )
    assert refused_update.status_code == 422, refused_update.text
    # ``owner`` is no project member role.
    owner = await people["boss"].patch(f"{members_url}/{people.ids['ada']}", json={"role": "owner"})
    assert owner.status_code == 422, owner.text


# ── the migration ───────────────────────────────────────────────────────────


def test_migration_is_the_newest_later_migration() -> None:
    assert LATER_MIGRATIONS[0] == MIGRATION
    migration = _load_migration("default_project_role_migration", MIGRATION)
    assert migration.revision == "d2f4a6c8e0b1"
    assert migration.down_revision == LATER_MIGRATIONS[1].split("_", 1)[0]
