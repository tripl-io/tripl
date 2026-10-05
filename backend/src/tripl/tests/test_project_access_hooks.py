"""The project-access extension points: grants and the write-permission check.

``Extension.project_grants`` adds project roles as SQL rows that
``project_access`` merges with the membership rows; ``Extension.project_permission_check``
may take one write permission away from an editor. Pinned here:

* with no extension, every answer is today's (the role matrix of
  ``test_org_roles_matrix`` and the pure ``effective_role``);
* a grant lifts a member's role everywhere at once — the detail, the list, the
  write gate and the fan-outs (``members_among``) — and the higher role wins;
* a grant never reaches across organizations, never reaches a non-member,
  never makes an owner and never changes an organization role;
* the permission check is asked only for editors, refuses with 403 on
  ``False``, and can never let a viewer write;
* every editor-gated project route maps to a named permission.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import Select, Uuid, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import extensions
from tripl.api import deps
from tripl.extensions import Extension, override_extensions
from tripl.models.organization import DEFAULT_ORG_ID
from tripl.models.user import User
from tripl.services import project_access, project_permissions
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_org_roles_matrix import (
    People,
    _create_project,
    _leave_org,
    _project_id,
    _set_org_role,
    _user,
)
from tripl.tests.test_rbac import iter_api_routes

API = "/api/v1"
EVENT_TYPE = {"name": "pv", "display_name": "PV"}


class _Grants(Extension):
    """Grants from a list of ``(org id, project id, user id, role)``; refuses listed permissions."""

    name = "grants"

    def __init__(
        self,
        rows: Sequence[tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]] = (),
        *,
        refuse: frozenset[str] = frozenset(),
        verdict: bool | None = None,
        columns: int = 4,
    ) -> None:
        self.rows = list(rows)
        self.refuse = refuse
        self.verdict = verdict
        self.columns = columns
        self.asked: list[tuple[uuid.UUID, uuid.UUID, str]] = []

    def project_grants(self) -> Select[Any] | None:
        if not self.rows:
            return None
        selects = [
            select(
                *(
                    literal(org, Uuid()).label("org_id"),
                    literal(project, Uuid()).label("project_id"),
                    literal(user, Uuid()).label("user_id"),
                    literal(role).label("role"),
                )[: self.columns]
            )
            for org, project, user, role in self.rows
        ]
        if len(selects) == 1:
            return selects[0]
        combined = union_all(*selects).subquery()
        return select(*combined.c)

    async def project_permission_check(
        self, session: AsyncSession, user: User, project_id: uuid.UUID, permission: str
    ) -> bool | None:
        self.asked.append((user.id, project_id, permission))
        if permission in self.refuse:
            return False
        return self.verdict


@pytest.fixture
async def people() -> AsyncIterator[People]:
    crowd = People()
    await crowd.register("boss")
    yield crowd
    await crowd.aclose()


async def _subject(people: People, *, org_role: str = "member") -> tuple[AsyncClient, uuid.UUID]:
    await _create_project(people["boss"], "granted")
    client = await people.register("subject")
    await _set_org_role(people.ids["subject"], org_role)
    return client, await _project_id("granted")


async def _sees(client: AsyncClient) -> tuple[int, str | None, bool]:
    detail = await client.get(f"{API}/projects/granted")
    listed = {p["slug"] for p in (await client.get(f"{API}/projects")).json()}
    role = detail.json().get("my_role") if detail.status_code == 200 else None
    return detail.status_code, role, "granted" in listed


# ── no extension: today's behaviour ─────────────────────────────────────────


async def test_without_an_extension_there_are_no_grants_and_no_permission_check(
    people: People,
) -> None:
    with override_extensions([]):
        assert extensions.project_grants() == []
        assert project_access._extension_grants() is None
        client, project_id = await _subject(people)
        assert await _sees(client) == (404, None, False)
        await add_member_by_slug("granted", "subject@example.com", "editor")
        assert await _sees(client) == (200, "editor", True)
        created = await client.post(f"{API}/projects/granted/event-types", json=EVENT_TYPE)
        assert created.status_code == 201, created.text
        refused = await extensions.project_permission_refused(
            None,  # type: ignore[arg-type]
            await _user(people.ids["subject"]),
            project_id,
            project_permissions.ALERTS_MANAGE,
        )
        assert refused is False


def test_effective_role_without_a_grant_is_unchanged() -> None:
    for org_role in ("owner", "admin", "member", None):
        for row in (None, "none", "viewer", "editor"):
            for default in ("none", "viewer", "editor"):
                assert project_access.effective_role(
                    org_role, row, default
                ) == project_access.effective_role(org_role, row, default, None)


def test_effective_role_with_a_grant_takes_the_higher_role_for_members_only() -> None:
    effective = project_access.effective_role
    assert effective("member", None, "none", "viewer") == "viewer"
    assert effective("member", None, "none", "editor") == "editor"
    assert effective("member", "viewer", "none", "editor") == "editor"
    assert effective("member", "editor", "none", "viewer") == "editor"
    assert effective("member", "none", "editor", "viewer") == "viewer"
    assert effective("member", None, "viewer", "editor") == "editor"
    # Never an owner, whatever the grant says.
    assert effective("member", None, "none", "owner") is None
    assert effective("member", "viewer", "none", "owner") == "viewer"
    # A non-member gets nothing from a grant.
    assert effective(None, None, "editor", "editor") is None
    assert effective(None, "viewer", "none", "editor") == "viewer"
    # An org admin is owner either way.
    assert effective("admin", None, "none", "viewer") == "owner"


# ── grants ──────────────────────────────────────────────────────────────────


async def test_a_grant_lifts_a_member_everywhere_and_goes_with_the_extension(
    people: People,
) -> None:
    client, project_id = await _subject(people)
    subject_id = people.ids["subject"]
    viewer = _Grants([(DEFAULT_ORG_ID, project_id, subject_id, "viewer")])
    with override_extensions([viewer]):
        assert await _sees(client) == (200, "viewer", True)
        write = await client.post(f"{API}/projects/granted/event-types", json=EVENT_TYPE)
        assert write.status_code == 403, write.text
        async with TestSessionLocal() as session:
            assert await project_access.members_among(session, project_id, [subject_id]) == {
                subject_id
            }
            assert project_id in await project_access.member_project_ids(
                session, await _user(subject_id)
            )
            # The core's own role leaves the grant out.
            assert (
                await project_access.direct_member_role(
                    session, await _user(subject_id), project_id
                )
                is None
            )
    editor = _Grants([(DEFAULT_ORG_ID, project_id, subject_id, "editor")])
    with override_extensions([editor]):
        assert await _sees(client) == (200, "editor", True)
        write = await client.post(f"{API}/projects/granted/event-types", json=EVENT_TYPE)
        assert write.status_code == 201, write.text
    # The grant is gone with the extension.
    with override_extensions([]):
        assert await _sees(client) == (404, None, False)
        async with TestSessionLocal() as session:
            assert await project_access.members_among(session, project_id, [subject_id]) == set()


async def test_the_higher_of_row_and_grant_wins(people: People) -> None:
    client, project_id = await _subject(people)
    subject_id = people.ids["subject"]
    await add_member_by_slug("granted", "subject@example.com", "editor")
    with override_extensions([_Grants([(DEFAULT_ORG_ID, project_id, subject_id, "viewer")])]):
        assert await _sees(client) == (200, "editor", True)
    await add_member_by_slug("granted", "subject@example.com", "none")
    with override_extensions([_Grants([(DEFAULT_ORG_ID, project_id, subject_id, "viewer")])]):
        assert await _sees(client) == (200, "viewer", True)
    # Two extensions, two grants: the higher one.
    with override_extensions(
        [
            _Grants([(DEFAULT_ORG_ID, project_id, subject_id, "viewer")]),
            _Grants([(DEFAULT_ORG_ID, project_id, subject_id, "editor")]),
        ]
    ):
        assert await _sees(client) == (200, "editor", True)


async def test_a_grant_never_crosses_organizations_reaches_outsiders_or_makes_owners(
    people: People,
) -> None:
    client, project_id = await _subject(people)
    subject_id = people.ids["subject"]
    other_org = uuid.uuid4()
    for rows in (
        # A row of another organization naming this organization's project.
        [(other_org, project_id, subject_id, "editor")],
        # A role that is not grantable.
        [(DEFAULT_ORG_ID, project_id, subject_id, "owner")],
        [(DEFAULT_ORG_ID, project_id, subject_id, "admin")],
        # Another project, another user.
        [(DEFAULT_ORG_ID, uuid.uuid4(), subject_id, "editor")],
        [(DEFAULT_ORG_ID, project_id, uuid.uuid4(), "editor")],
    ):
        with override_extensions([_Grants(rows)]):
            assert await _sees(client) == (404, None, False), rows
            async with TestSessionLocal() as session:
                assert (
                    await project_access.members_among(session, project_id, [subject_id]) == set()
                )

    owner_grant = _Grants([(DEFAULT_ORG_ID, project_id, subject_id, "editor")])
    with override_extensions([owner_grant]):
        # An editor by grant, still a plain member of the organization.
        assert await _sees(client) == (200, "editor", True)
        assert (await client.delete(f"{API}/projects/granted")).status_code == 403
        assert (await client.get(f"{API}/projects/granted/audit")).status_code == 403
        async with TestSessionLocal() as session:
            assert await project_access.org_role_of(session, subject_id, DEFAULT_ORG_ID) == "member"

    # Out of the organization: the grant counts for nothing any more.
    await _leave_org(subject_id)
    with override_extensions([owner_grant]):
        async with TestSessionLocal() as session:
            user = await _user(subject_id)
            assert await project_access.member_role(session, user, project_id) is None
            assert await project_access.members_among(session, project_id, [subject_id]) == set()
            assert project_id not in await project_access.member_project_ids(
                session, user, DEFAULT_ORG_ID
            )


async def test_a_grant_select_of_the_wrong_shape_fails_the_request(people: People) -> None:
    client, project_id = await _subject(people)
    broken = _Grants([(DEFAULT_ORG_ID, project_id, people.ids["subject"], "editor")], columns=3)
    with override_extensions([broken]), pytest.raises(TypeError, match="four columns"):
        await client.get(f"{API}/projects/granted")


# ── the permission check ────────────────────────────────────────────────────


async def test_the_permission_check_refuses_one_area_to_an_editor(people: People) -> None:
    client, project_id = await _subject(people)
    await add_member_by_slug("granted", "subject@example.com", "editor")
    check = _Grants(refuse=frozenset({project_permissions.ALERTS_MANAGE}))
    with override_extensions([check]):
        refused = await client.post(f"{API}/projects/granted/alert-destinations", json={})
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"] == deps.PROJECT_PERMISSION_REFUSED
        allowed = await client.post(f"{API}/projects/granted/event-types", json=EVENT_TYPE)
        assert allowed.status_code == 201, allowed.text
        # Reads are never asked about.
        assert (await client.get(f"{API}/projects/granted/alert-destinations")).status_code == 200
    subject_id = people.ids["subject"]
    assert check.asked == [
        (subject_id, project_id, project_permissions.ALERTS_MANAGE),
        (subject_id, project_id, project_permissions.PLAN_EDIT),
    ]


async def test_the_permission_check_skips_owners_and_never_lets_a_viewer_write(
    people: People,
) -> None:
    await _create_project(people["boss"], "granted")
    check = _Grants(refuse=frozenset(project_permissions.PERMISSION_KEYS))
    with override_extensions([check]):
        # The organization's owner holds every permission.
        created = await people["boss"].post(f"{API}/projects/granted/event-types", json=EVENT_TYPE)
        assert created.status_code == 201, created.text
    assert check.asked == []

    viewer = await people.register("viewer")
    await _set_org_role(people.ids["viewer"], "member")
    await add_member_by_slug("granted", "viewer@example.com", "viewer")
    yes = _Grants(verdict=True)
    with override_extensions([yes]):
        write = await viewer.post(
            f"{API}/projects/granted/event-types", json={**EVENT_TYPE, "name": "x"}
        )
    assert write.status_code == 403, write.text
    assert write.json()["detail"] == "Editor access to this project is required"
    assert yes.asked == []


async def test_a_failing_permission_check_fails_the_write(people: People) -> None:
    client, _ = await _subject(people)
    await add_member_by_slug("granted", "subject@example.com", "editor")

    class _Broken(Extension):
        async def project_permission_check(self, session, user, project_id, permission):  # type: ignore[no-untyped-def]
            raise RuntimeError("down")

    with override_extensions([_Broken()]), pytest.raises(RuntimeError, match="down"):
        await client.post(f"{API}/projects/granted/event-types", json=EVENT_TYPE)


def _editor_gated(route: Any) -> bool:
    seen: list[Any] = []

    def walk(dependant: Any) -> None:
        for sub in dependant.dependencies:
            seen.append(sub.call)
            walk(sub)

    walk(route.dependant)
    return deps.get_editor_user in seen


def test_every_editor_gated_project_route_names_a_permission() -> None:
    routes = [
        (path, route)
        for path, route in iter_api_routes()
        if "{slug}" in path
        and (set(route.methods or ()) - {"GET", "HEAD", "OPTIONS"})
        and _editor_gated(route)
    ]
    assert len(routes) > 100, f"the walk reached only {len(routes)} routes"
    unnamed = [path for path, _ in routes if project_permissions.classify(path) is None]
    assert not unnamed, f"name the permission these need in project_permissions: {unnamed}"
    assert {project_permissions.permission_for(path) for path, _ in routes} == set(
        project_permissions.PERMISSION_KEYS
    )


def test_the_permission_map_on_known_paths() -> None:
    p = project_permissions
    base = "/api/v1/projects/{slug}"
    assert p.permission_for(f"{base}/branches/{{branch_id}}/merge") == p.PLAN_MERGE
    assert p.permission_for(f"{base}/branches/{{branch_id}}/comments") == p.COMMENTS_WRITE
    assert p.permission_for(f"{base}/branches") == p.PLAN_EDIT
    assert p.permission_for(f"{base}/events/{{event_id}}/comments/{{comment_id}}") == (
        p.COMMENTS_WRITE
    )
    assert p.permission_for(f"{base}/docs/file") == p.DOCS_EDIT
    assert p.permission_for(f"{base}/alert-inbox/bulk-actions") == p.ALERTS_MANAGE
    assert p.permission_for(f"{base}/scans/{{scan_id}}/run") == p.DATA_SOURCES_MANAGE
    assert p.permission_for(f"{base}/metrics") == p.METRICS_MANAGE
    assert p.permission_for(base) == p.SETTINGS_MANAGE
    # Unknown: the widest permission, never none.
    assert p.classify(f"{base}/something-new") is None
    assert p.permission_for(f"{base}/something-new") == p.SETTINGS_MANAGE
    assert p.classify("/api/v1/projects") is None
