from collections.abc import AsyncGenerator

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from tripl.api.deps import WRITE_GATES
from tripl.main import app
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.tests._members import add_member_by_slug


@pytest.fixture
async def fresh_anon_client() -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _register(ac: AsyncClient, email: str) -> AsyncClient:
    resp = await ac.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Password123!", "name": email},
    )
    assert resp.status_code == 201, resp.text
    return ac


# The instance-era roles, as the tests that predate organization roles name
# them: ``editor`` / ``viewer`` were an instance-wide ceiling. They are now an
# organization ``member`` whose project rows carry that role.
_LEGACY_PROJECT_ROLES = {"editor", "viewer"}


async def _set_role(owner_client: AsyncClient, target_email: str, role: str) -> None:
    """Set ``target_email``'s organization role through ``PATCH /users/{id}``.

    ``role`` is an organization role (owner | admin | member). The legacy
    ``editor`` / ``viewer`` spell "member, and every project row of theirs at
    that role": the API has no instance-wide viewer any more, so the rows are
    rewritten directly, exactly what migration ``c9e1a3b5d7f9`` does once.
    """
    users = await owner_client.get("/api/v1/users")
    assert users.status_code == 200, users.text
    target = next(u for u in users.json() if u["email"] == target_email)
    org_role = "member" if role in _LEGACY_PROJECT_ROLES else role
    resp = await owner_client.patch(f"/api/v1/users/{target['id']}", json={"role": org_role})
    assert resp.status_code == 200, resp.text
    if role in _LEGACY_PROJECT_ROLES:
        from tripl.tests.conftest import TestSessionLocal

        async with TestSessionLocal() as session:
            user_id = await session.scalar(select(User.id).where(User.email == target_email))
            await session.execute(
                update(ProjectMember).where(ProjectMember.user_id == user_id).values(role=role)
            )
            await session.commit()


def iter_api_routes() -> list[tuple[str, APIRoute]]:
    """Every ``(full_path, APIRoute)`` in the app, including nested routers.

    FastAPI 0.140 wraps each ``include_router`` result in a private
    ``_IncludedRouter`` node instead of copying the child's routes up, so
    ``app.routes`` yields only the handful declared directly on the app
    (``/health``, ``/docs``, ...) and each route's ``path`` is missing the
    prefixes of the routers it was included *into*. Walking it naively made this
    module's route audits inspect exactly one route and report "no offenders" for
    the entire API, so they must recurse through ``original_router`` and rebuild
    the path (a route already carries its own router's prefix; what is missing is
    every ancestor's).
    """
    found: list[tuple[str, APIRoute]] = []

    def walk(router: object, prefix: str) -> None:
        child_prefix = prefix + str(getattr(router, "prefix", ""))
        for route in getattr(router, "routes", []):
            if isinstance(route, APIRoute):
                found.append((prefix + route.path, route))
            else:
                included = getattr(route, "original_router", None)
                if included is not None:
                    walk(included, child_prefix)

    walk(app.router, "")
    return found


# Floor for "the walk reached the real API and not just the app-level handlers".
# Every route audit that filters on a gate is vacuous if the walk stalls, so each
# one asserts this first — from here, not from its own literal.
MIN_API_ROUTES = 150


def test_route_audit_sees_the_whole_api() -> None:
    """Guard the guard: if the walker silently stops finding routes, say so here."""
    routes = iter_api_routes()
    assert len(routes) > MIN_API_ROUTES, f"route audit only reached {len(routes)} routes"
    paths = {path for path, _ in routes}
    # Paths must be reconstructed in full, or every ``/api/v1`` filter below
    # silently matches nothing.
    assert "/api/v1/auth/login" in paths
    assert "/api/v1/projects/{slug}/event-types" in paths


# POSTs that only carry a request body for a read: no state changes, so they are
# deliberately ungated. Shared with the project-scope audit in
# test_project_mutation_authorization.py so both lists cannot drift apart.
READ_LIKE_MUTATING_PATHS = {
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
    # Unauthenticated by definition — a logged-out user has no role to gate on.
    # Both shipped while this audit was silently inspecting a single route, so
    # they were never added to the list; they are correct as written.
    "/api/v1/auth/password-reset/request",
    "/api/v1/auth/password-reset/confirm",
    # Redeeming an invitation: an auth endpoint by nature, since the caller has
    # no account yet and therefore no role to gate on. Its authorization is the
    # single-use, expiring, owner-issued token itself, and MINTING one is
    # owner-only with API keys rejected (POST /users/invitations). Rate-limited
    # on the register bucket.
    "/api/v1/auth/invitations/{token}/accept",
    "/api/v1/projects/{slug}/events/window-metrics",
    "/api/v1/projects/{slug}/anomalies/signals/query",
    # Read-like: row sparklines for a batch of signals; POST only to carry it.
    "/api/v1/projects/{slug}/anomalies/signals/series",
    "/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules/{rule_id}/simulate",
    # Read-like: NL question over the plan; POST only to carry the body.
    "/api/v1/projects/{slug}/ai/ask",
    # Read-like: dependents of a planned change set (GH #257); POST only to
    # carry the change list, and it refuses nothing.
    "/api/v1/projects/{slug}/impact",
    # Read-like: validates a batch of tracking calls against the plan (GH #261,
    # ``tripl check``); POST only to carry up to 5000 items, and it writes
    # nothing. Ungated so a CI job's read-scope key and viewers can call it.
    "/api/v1/projects/{slug}/plan/validate",
    # Read-like: duplicate and naming-lint check for would-be events (GH #265);
    # POST only to carry up to 500 candidates, and it writes nothing. Ungated so
    # a viewer sees the same warning an editor does.
    "/api/v1/projects/{slug}/events/duplicate-check",
}


def test_mutating_routes_require_write_gate() -> None:
    """Every mutating API route needs a write-scope dependency unless it is
    intentionally read-like or an auth endpoint.

    The gate list comes from ``deps.WRITE_GATES`` rather than a literal here: when
    this file spelled its own set, adding a gate to deps.py left every route that
    used it looking ungated to this audit, which passes silently.
    """
    allowed_without_write_gate = READ_LIKE_MUTATING_PATHS
    offenders: list[str] = []

    for path, route in iter_api_routes():
        mutating_methods = (route.methods or set()) & {"POST", "PATCH", "PUT", "DELETE"}
        if not mutating_methods or not path.startswith("/api/v1"):
            continue
        if path in allowed_without_write_gate:
            continue
        dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
        if dependency_calls.isdisjoint(WRITE_GATES):
            offenders.append(f"{','.join(sorted(mutating_methods))} {path}")

    assert offenders == []


@pytest.mark.asyncio
async def test_first_user_becomes_owner_subsequent_users_are_members(
    fresh_anon_client: AsyncClient,
) -> None:
    """Self-hosted bootstrap: the first account owns the default org AND operates the instance."""
    await _register(fresh_anon_client, "first@example.com")
    me = (await fresh_anon_client.get("/api/v1/auth/me")).json()
    assert me["role"] == "owner"
    assert me["is_platform_admin"] is True
    assert me["orgs"] == [{"slug": "default", "name": "Default organization", "role": "owner"}]

    # Clear the owner session and register a second user → an org member.
    await fresh_anon_client.post("/api/v1/auth/logout")
    await _register(fresh_anon_client, "second@example.com")
    me2 = (await fresh_anon_client.get("/api/v1/auth/me")).json()
    assert me2["role"] == "member"
    assert me2["is_platform_admin"] is False
    assert me2["orgs"] == [{"slug": "default", "name": "Default organization", "role": "member"}]


@pytest.mark.asyncio
async def test_viewer_cannot_mutate_but_can_read(fresh_anon_client: AsyncClient) -> None:
    # Owner registers and creates a project + viewer user.
    await _register(fresh_anon_client, "owner@example.com")
    await fresh_anon_client.post("/api/v1/projects", json={"name": "RBAC", "slug": "rbac-proj"})

    await fresh_anon_client.post("/api/v1/auth/logout")
    await _register(fresh_anon_client, "viewer@example.com")

    # A non-member does not see the project at all, so make the member a
    # project viewer. The project row is the whole story now: there is no
    # instance-wide viewer role left to cap it.
    await add_member_by_slug("rbac-proj", "viewer@example.com", "viewer")

    # Reads work.
    listing = await fresh_anon_client.get("/api/v1/projects")
    assert listing.status_code == 200

    detail = await fresh_anon_client.get("/api/v1/projects/rbac-proj")
    assert detail.status_code == 200, detail.text
    assert detail.json()["my_role"] == "viewer"
    assert detail.json()["can_mutate"] is False
    listed = {item["slug"]: item for item in listing.json()}
    assert listed["rbac-proj"]["my_role"] == "viewer"
    assert listed["rbac-proj"]["can_mutate"] is False

    # Mutations inside the project are rejected with 403.
    create_et = await fresh_anon_client.post(
        "/api/v1/projects/rbac-proj/event-types",
        json={"name": "x", "display_name": "X"},
    )
    assert create_et.status_code == 403


@pytest.mark.asyncio
async def test_project_delete_is_owner_only(fresh_anon_client: AsyncClient) -> None:
    await _register(fresh_anon_client, "project-owner@example.com")
    create = await fresh_anon_client.post(
        "/api/v1/projects",
        json={"name": "Protected", "slug": "protected-project"},
    )
    assert create.status_code == 201

    await fresh_anon_client.post("/api/v1/auth/logout")
    await _register(fresh_anon_client, "project-editor@example.com")

    # A non-member cannot even see the project.
    hidden = await fresh_anon_client.delete("/api/v1/projects/protected-project")
    assert hidden.status_code == 404

    # An editor member is still refused: deleting a project is owner-only.
    await add_member_by_slug("protected-project", "project-editor@example.com", "editor")
    denied = await fresh_anon_client.delete("/api/v1/projects/protected-project")
    assert denied.status_code == 403
    assert "owner" in denied.json()["detail"].lower()

    await fresh_anon_client.post("/api/v1/auth/logout")
    await fresh_anon_client.post(
        "/api/v1/auth/login",
        json={"email": "project-owner@example.com", "password": "Password123!"},
    )
    deleted = await fresh_anon_client.delete("/api/v1/projects/protected-project")
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_data_source_management_is_owner_only(fresh_anon_client: AsyncClient) -> None:
    await _register(fresh_anon_client, "ds-owner@example.com")
    create = await fresh_anon_client.post(
        "/api/v1/data-sources",
        json={
            "name": "Warehouse",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
        },
    )
    assert create.status_code == 201, create.text
    ds_id = create.json()["id"]

    await fresh_anon_client.post("/api/v1/auth/logout")
    await _register(fresh_anon_client, "ds-editor@example.com")

    listing = await fresh_anon_client.get("/api/v1/data-sources")
    assert listing.status_code == 200

    create_denied = await fresh_anon_client.post(
        "/api/v1/data-sources",
        json={
            "name": "Editor DS",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
        },
    )
    assert create_denied.status_code == 403

    update_denied = await fresh_anon_client.patch(
        f"/api/v1/data-sources/{ds_id}",
        json={"name": "Renamed"},
    )
    assert update_denied.status_code == 403

    test_denied = await fresh_anon_client.post(f"/api/v1/data-sources/{ds_id}/test")
    assert test_denied.status_code == 403

    delete_denied = await fresh_anon_client.delete(f"/api/v1/data-sources/{ds_id}")
    assert delete_denied.status_code == 403


@pytest.mark.asyncio
async def test_audit_log_is_owner_only(fresh_anon_client: AsyncClient) -> None:
    """The audit feed was the back door around two owner-only gates (tripl-jfm3.110).

    Entries carry the payload that produced them, so a non-owner could read the
    warehouse connection details that data_sources.py:69 blanks on a direct
    read, and the ``base_query`` SQL that authoring a scan is owner-only to
    protect. ``project_slug`` is a filter, not a scope, so the reach was every
    project on the instance.
    """
    await _register(fresh_anon_client, "audit-owner@example.com")
    as_owner = await fresh_anon_client.get("/api/v1/audit")
    assert as_owner.status_code == 200, as_owner.text

    await fresh_anon_client.post("/api/v1/auth/logout")
    await _register(fresh_anon_client, "audit-editor@example.com")

    as_editor = await fresh_anon_client.get("/api/v1/audit")
    assert as_editor.status_code == 403

    # Filters do not buy their way past the gate.
    filtered = await fresh_anon_client.get("/api/v1/audit", params={"action": "scan_config.create"})
    assert filtered.status_code == 403


@pytest.mark.asyncio
async def test_only_owner_can_change_roles(fresh_anon_client: AsyncClient) -> None:
    await _register(fresh_anon_client, "owner2@example.com")
    await fresh_anon_client.post("/api/v1/auth/logout")
    await _register(fresh_anon_client, "editor2@example.com")
    # Editor tries to change owner's role.
    users = await fresh_anon_client.get("/api/v1/users")
    owner = next(u for u in users.json() if u["email"] == "owner2@example.com")
    resp = await fresh_anon_client.patch(f"/api/v1/users/{owner['id']}", json={"role": "member"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_role_change_keeps_sessions_and_applies_on_the_next_request(
    fresh_anon_client: AsyncClient,
) -> None:
    """A role change no longer signs the user out everywhere (critique #7).

    Organization roles are read from the database on every request, so the
    change bites on the target's very next request, on the session they
    already hold — and an org admin of one organization can no longer sign a
    user out of every other organization they belong to.
    """
    await _register(fresh_anon_client, "owner3@example.com")
    await fresh_anon_client.post("/api/v1/auth/logout")
    await _register(fresh_anon_client, "promoted@example.com")
    member_cookies = dict(fresh_anon_client.cookies)
    assert (await fresh_anon_client.get("/api/v1/audit")).status_code == 403

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as owner_client:
        await owner_client.post(
            "/api/v1/auth/login",
            json={"email": "owner3@example.com", "password": "Password123!"},
        )
        await _set_role(owner_client, "promoted@example.com", "admin")

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", cookies=member_cookies
        ) as same_session:
            me = await same_session.get("/api/v1/auth/me")
            assert me.status_code == 200, me.text
            assert me.json()["role"] == "admin"
            assert (await same_session.get("/api/v1/audit")).status_code == 200

            await _set_role(owner_client, "promoted@example.com", "member")
            assert (await same_session.get("/api/v1/auth/me")).status_code == 200
            assert (await same_session.get("/api/v1/audit")).status_code == 403


@pytest.mark.asyncio
async def test_cannot_demote_last_owner(fresh_anon_client: AsyncClient) -> None:
    await _register(fresh_anon_client, "lone-owner@example.com")
    me = (await fresh_anon_client.get("/api/v1/auth/me")).json()
    resp = await fresh_anon_client.patch(f"/api/v1/users/{me['id']}", json={"role": "member"})
    assert resp.status_code == 400
    assert "last" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_users_api_speaks_organization_roles(fresh_anon_client: AsyncClient) -> None:
    """``GET /users`` lists org members with their org role; instance roles are gone (422)."""
    await _register(fresh_anon_client, "vocab-owner@example.com")
    await fresh_anon_client.post("/api/v1/auth/logout")
    await _register(fresh_anon_client, "vocab-member@example.com")
    await fresh_anon_client.post("/api/v1/auth/logout")
    await fresh_anon_client.post(
        "/api/v1/auth/login",
        json={"email": "vocab-owner@example.com", "password": "Password123!"},
    )
    users = await fresh_anon_client.get("/api/v1/users")
    assert users.status_code == 200, users.text
    roles = {u["email"]: u["role"] for u in users.json()}
    assert roles == {"vocab-owner@example.com": "owner", "vocab-member@example.com": "member"}

    member_id = next(u["id"] for u in users.json() if u["email"] == "vocab-member@example.com")
    for legacy in ("editor", "viewer"):
        refused = await fresh_anon_client.patch(f"/api/v1/users/{member_id}", json={"role": legacy})
        assert refused.status_code == 422, refused.text


@pytest.mark.asyncio
async def test_editor_cannot_run_sql_against_a_warehouse(fresh_anon_client: AsyncClient) -> None:
    """Scan SQL carries the same role as the credential it runs on (tripl-jfm3.18).

    Data sources are owner-configured and workspace-global, and ``base_query``
    is executed verbatim, so an editor must not be able to author, preview, or
    run one — otherwise any account is a read-anything handle on the warehouse.
    """
    await _register(fresh_anon_client, "sql-owner@example.com")
    ds = await fresh_anon_client.post(
        "/api/v1/data-sources",
        json={
            "name": "Warehouse",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
        },
    )
    assert ds.status_code == 201, ds.text
    ds_id = ds.json()["id"]
    await fresh_anon_client.post("/api/v1/projects", json={"name": "SQL", "slug": "sql-proj"})

    await fresh_anon_client.post("/api/v1/auth/logout")
    await _register(fresh_anon_client, "sql-editor@example.com")
    await add_member_by_slug("sql-proj", "sql-editor@example.com", "editor")

    # Editors keep the read-only views of the scan surface.
    assert (await fresh_anon_client.get("/api/v1/projects/sql-proj/scans")).status_code == 200

    preview = await fresh_anon_client.post(
        "/api/v1/projects/sql-proj/scans/preview",
        json={"data_source_id": ds_id, "base_query": "SELECT currentUser() AS name", "limit": 1},
    )
    assert preview.status_code == 403
    assert "owner" in preview.json()["detail"].lower()

    # The dry-run is the same hazard by another name: it runs a draft's free-text
    # SQL against the same stored credential, so it carries the same role.
    dry_run = await fresh_anon_client.post(
        "/api/v1/projects/sql-proj/scans/dry-run",
        json={"data_source_id": ds_id, "base_query": "SELECT currentUser() AS name"},
    )
    assert dry_run.status_code == 403
    assert "owner" in dry_run.json()["detail"].lower()

    dry_run_poll = await fresh_anon_client.get(
        "/api/v1/projects/sql-proj/scans/dry-run-jobs/00000000-0000-0000-0000-000000000000",
    )
    assert dry_run_poll.status_code == 403

    create = await fresh_anon_client.post(
        "/api/v1/projects/sql-proj/scans",
        json={
            "data_source_id": ds_id,
            "name": "exfil",
            "base_query": "SELECT * FROM secrets",
        },
    )
    assert create.status_code == 403


@pytest.mark.asyncio
async def test_scan_base_query_must_be_a_read_only_select(fresh_anon_client: AsyncClient) -> None:
    """``base_query`` now carries the same SQL-safety gate as ``metric_sql``."""
    await _register(fresh_anon_client, "sql-safety@example.com")
    ds = await fresh_anon_client.post(
        "/api/v1/data-sources",
        json={
            "name": "Warehouse",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
        },
    )
    ds_id = ds.json()["id"]
    await fresh_anon_client.post("/api/v1/projects", json={"name": "Safe", "slug": "safe-proj"})

    stacked = await fresh_anon_client.post(
        "/api/v1/projects/safe-proj/scans/preview",
        json={"data_source_id": ds_id, "base_query": "SELECT 1; DROP TABLE events"},
    )
    assert stacked.status_code == 422

    ddl = await fresh_anon_client.post(
        "/api/v1/projects/safe-proj/scans",
        json={"data_source_id": ds_id, "name": "bad", "base_query": "DROP TABLE events"},
    )
    assert ddl.status_code == 422


@pytest.mark.asyncio
async def test_editor_cannot_edit_another_users_project(fresh_anon_client: AsyncClient) -> None:
    """Editing project identity needs the creator or an owner (tripl-jfm3.19)."""
    await _register(fresh_anon_client, "proj-owner@example.com")
    owned = await fresh_anon_client.post(
        "/api/v1/projects", json={"name": "Owned", "slug": "owner-proj"}
    )
    assert owned.status_code == 201, owned.text

    await fresh_anon_client.post("/api/v1/auth/logout")
    await _register(fresh_anon_client, "proj-editor@example.com")

    # A non-member does not see the owner's project at all.
    unseen = await fresh_anon_client.patch(
        "/api/v1/projects/owner-proj", json={"name": "Vandalised"}
    )
    assert unseen.status_code == 404

    # An editor MEMBER may edit the plan but not the project's identity.
    await add_member_by_slug("owner-proj", "proj-editor@example.com", "editor")
    hijack = await fresh_anon_client.patch(
        "/api/v1/projects/owner-proj", json={"name": "Vandalised"}
    )
    assert hijack.status_code == 403
    assert "creator" in hijack.json()["detail"].lower()

    # The editor still fully controls a project they created themselves.
    mine = await fresh_anon_client.post(
        "/api/v1/projects", json={"name": "Mine", "slug": "editor-proj"}
    )
    assert mine.status_code == 201, mine.text
    renamed = await fresh_anon_client.patch(
        "/api/v1/projects/editor-proj", json={"name": "Mine, renamed"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Mine, renamed"

    # And the org owner can still edit any project of the organization.
    await fresh_anon_client.post("/api/v1/auth/logout")
    await fresh_anon_client.post(
        "/api/v1/auth/login",
        json={"email": "proj-owner@example.com", "password": "Password123!"},
    )
    by_owner = await fresh_anon_client.patch(
        "/api/v1/projects/editor-proj", json={"name": "Owner touched"}
    )
    assert by_owner.status_code == 200, by_owner.text
