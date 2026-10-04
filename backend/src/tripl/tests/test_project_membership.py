"""Per-project membership.

Projects used to be visible to every user on the instance. Now:

* a non-member does not see a project AT ALL: every ``/projects/{slug}/...``
  route (and ``/activity/projects/{slug}``, ``/projects/demo/{slug}/...``) answers
  404 "Project not found", and the project is absent from every list and feed;
* an owner or admin of the project's organization sees and manages every
  project of it without a membership row (F20 PR4);
* the creator of a project is an ``editor`` member from the first commit;
* members are managed at ``/projects/{slug}/members`` by an org owner/admin or
  the project's creator.

The route audit at the top is structural (the membership dependency is mounted
on every slug route) and behavioural (a non-member gets 404 from every one).
"""

import json
import re
import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from tripl import cache
from tripl.api.deps import require_project_membership
from tripl.main import app
from tripl.models.audit_log import AuditLog
from tripl.models.data_source import DataSource
from tripl.models.organization import DEFAULT_ORG_ID
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob
from tripl.models.user import User
from tripl.services import project_access
from tripl.tests._members import add_member_by_slug, remove_member_by_slug
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_rbac import MIN_API_ROUTES

PASSWORD = "Password123!"
NOT_FOUND = "Project not found"

# ── route audit ─────────────────────────────────────────────────────────────


def _routes_with_effective_dependencies() -> list[tuple[str, APIRoute, set[Callable[..., Any]]]]:
    """Every ``(full_path, route, dependency callables)`` including inherited ones.

    ``test_rbac.iter_api_routes`` walks FastAPI's ``_IncludedRouter`` nodes but
    reads only ``route.dependant``, which does not carry the dependencies passed
    to ``include_router`` — exactly where the membership gate is mounted. So this
    walk also accumulates each include's ``include_context.dependencies`` and the
    routers' own ``dependencies`` on the way down.
    """
    found: list[tuple[str, APIRoute, set[Callable[..., Any]]]] = []

    def calls_of(depends: object) -> set[Callable[..., Any]]:
        return {
            call
            for call in (getattr(d, "dependency", None) for d in (depends or []))  # type: ignore[attr-defined]
            if call is not None
        }

    def walk(router: object, prefix: str, inherited: set[Callable[..., Any]]) -> None:
        child_prefix = prefix + str(getattr(router, "prefix", ""))
        own = inherited | calls_of(getattr(router, "dependencies", None))
        for route in getattr(router, "routes", []):
            if isinstance(route, APIRoute):
                calls = (
                    own
                    | calls_of(route.dependencies)
                    | {dependency.call for dependency in route.dependant.dependencies}
                )
                found.append((prefix + route.path, route, calls))
                continue
            included = getattr(route, "original_router", None)
            if included is None:
                continue
            context = getattr(route, "include_context", None)
            walk(included, child_prefix, own | calls_of(getattr(context, "dependencies", None)))

    walk(app.router, "", set())
    return found


def _slug_routes() -> list[tuple[str, APIRoute, set[Callable[..., Any]]]]:
    return [
        (path, route, calls)
        for path, route, calls in _routes_with_effective_dependencies()
        if path.startswith("/api/v1") and "{slug}" in path
    ]


def test_route_walk_reaches_the_whole_api() -> None:
    """Guard the guard: a stalled walk would make the audit below vacuous."""
    routes = _routes_with_effective_dependencies()
    assert len(routes) > MIN_API_ROUTES
    paths = {path for path, _route, _calls in _slug_routes()}
    assert "/api/v1/projects/{slug}/event-types" in paths
    assert "/api/v1/projects/{slug}/members" in paths
    assert "/api/v1/activity/projects/{slug}" in paths
    assert any(path.startswith("/api/v1/projects/demo/{slug}") for path in paths)


def test_every_slug_route_carries_the_membership_gate() -> None:
    """A new ``/projects/{slug}/...`` router mounted without the gate fails here.

    The gate is mounted through ``protected_dependencies`` in ``api.v1.router``;
    a router included without it (or a slug route on the auth router) would serve
    a project to a non-member.
    """
    slug_routes = _slug_routes()
    assert len(slug_routes) > 50, f"only {len(slug_routes)} slug routes found"
    offenders = [
        f"{','.join(sorted(route.methods or set()))} {path}"
        for path, route, calls in slug_routes
        if require_project_membership not in calls
    ]
    assert offenders == []


# ── actors ──────────────────────────────────────────────────────────────────


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _register(client: AsyncClient, email: str, name: str) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD, "name": name}
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def _create_project(client: AsyncClient, slug: str) -> dict[str, Any]:
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


class Actors:
    """The org owner and three plain org members, each with a jar.

    ``viewer`` is an org member like the others; tests give them viewer rows.
    """

    def __init__(self) -> None:
        self.owner = _new_client()
        self.editor = _new_client()
        self.stranger = _new_client()
        self.viewer = _new_client()
        self.ids: dict[str, str] = {}

    async def aclose(self) -> None:
        for client in (self.owner, self.editor, self.stranger, self.viewer):
            await client.aclose()


@pytest_asyncio.fixture
async def actors() -> AsyncGenerator[Actors]:
    people = Actors()
    people.ids["owner"] = (await _register(people.owner, "owner@example.com", "Owner"))["id"]
    people.ids["editor"] = (await _register(people.editor, "editor@example.com", "Editor"))["id"]
    people.ids["stranger"] = (await _register(people.stranger, "stranger@example.com", "Stranger"))[
        "id"
    ]
    people.ids["viewer"] = (await _register(people.viewer, "viewer@example.com", "Viewer"))["id"]
    yield people
    await people.aclose()


# ── 404 for non-members on every slug route ─────────────────────────────────

_PATH_PARAM = re.compile(r"\{([^}:]+)(?::[^}]*)?\}")


def _concrete(path: str, slug: str) -> str:
    return _PATH_PARAM.sub(
        lambda m: slug if m.group(1) == "slug" else str(uuid.uuid4()),
        path,
    )


@pytest.mark.asyncio
async def test_a_non_member_gets_404_from_every_slug_route(actors: Actors) -> None:
    """The behavioural half of the audit: every method on every slug route.

    Router-level dependencies are solved before the route's own dependencies,
    query/path validation and body validation, so the membership 404 is the
    answer whatever the route would otherwise say (403 owner-only, 422 bad body,
    404 unknown child id). The SSE stream is skipped only because a regression
    there would hang rather than fail; the structural audit covers it.
    """
    await _create_project(actors.editor, "hidden-everywhere")

    checked = 0
    wrong: list[str] = []
    for path, route, _calls in _slug_routes():
        if path.endswith("/stream"):
            continue
        url = _concrete(path, "hidden-everywhere")
        for method in sorted(route.methods or set()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            kwargs: dict[str, Any] = {}
            if method in {"POST", "PATCH", "PUT"}:
                kwargs["json"] = {}
            resp = await actors.stranger.request(method, url, **kwargs)
            checked += 1
            if resp.status_code != 404 or resp.json().get("detail") != NOT_FOUND:
                wrong.append(f"{method} {path} -> {resp.status_code} {resp.text[:120]}")
    assert checked > 50
    assert wrong == []


@pytest.mark.asyncio
async def test_non_member_and_unknown_slug_are_indistinguishable(actors: Actors) -> None:
    await _create_project(actors.editor, "real-but-hidden")
    for slug in ("real-but-hidden", "never-existed"):
        for url in (
            f"/api/v1/projects/{slug}",
            f"/api/v1/projects/{slug}/event-types",
            f"/api/v1/activity/projects/{slug}",
        ):
            resp = await actors.stranger.get(url)
            assert resp.status_code == 404, (url, resp.text)
            assert resp.json()["detail"] == NOT_FOUND


@pytest.mark.asyncio
async def test_non_member_cannot_reach_another_users_demo_routes(actors: Actors) -> None:
    await _create_project(actors.owner, "owner-demo-like")
    async with TestSessionLocal() as session:
        project = await session.scalar(select(Project).where(Project.slug == "owner-demo-like"))
        assert project is not None
        project.is_demo = True
        await session.commit()

    for method, url in (
        ("POST", "/api/v1/projects/demo/owner-demo-like/reset"),
        ("DELETE", "/api/v1/projects/demo/owner-demo-like"),
    ):
        resp = await actors.editor.request(method, url)
        assert resp.status_code == 404, (method, url, resp.text)


# ── lists, feeds and the shared cache ───────────────────────────────────────


async def _slugs(client: AsyncClient) -> set[str]:
    resp = await client.get("/api/v1/projects")
    assert resp.status_code == 200, resp.text
    return {item["slug"] for item in resp.json()}


@pytest.mark.asyncio
async def test_project_list_is_filtered_by_membership(actors: Actors) -> None:
    await _create_project(actors.owner, "alpha")
    await _create_project(actors.editor, "beta")

    assert await _slugs(actors.owner) == {"alpha", "beta"}
    assert await _slugs(actors.editor) == {"beta"}
    # A new user sees nothing until added.
    assert await _slugs(actors.stranger) == set()
    assert await _slugs(actors.viewer) == set()

    await add_member_by_slug("alpha", "viewer@example.com", "viewer")
    assert await _slugs(actors.viewer) == {"alpha"}


class _InMemoryCache:
    """A dict standing in for Redis behind ``tripl.cache``'s JSON helpers.

    Tests run with ``redis_url`` empty, so without this every ``get_json`` is a
    miss and a cache test would pass whatever the service did with the entry.
    Values go through a JSON round trip, exactly as they would through Redis.
    """

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.hits: list[str] = []

    async def get_json(self, key: str) -> Any | None:
        if key not in self.store:
            return None
        self.hits.append(key)
        return json.loads(self.store[key])

    async def set_json(self, key: str, value: Any, ttl_seconds: int) -> None:
        self.store[key] = json.dumps(value, default=str)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.store.pop(key, None)

    async def delete_prefix(self, prefix: str) -> None:
        for key in [key for key in self.store if key.startswith(prefix)]:
            del self.store[key]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("get_json", "set_json", "delete", "delete_prefix"):
            monkeypatch.setattr(cache, name, getattr(self, name))

    def hits_on(self, key: str) -> int:
        return self.hits.count(key)

    def cached_slugs(self, key: str) -> set[str]:
        return {item["slug"] for item in json.loads(self.store[key])}


@pytest.mark.asyncio
async def test_the_shared_list_cache_never_leaks_across_users(
    actors: Actors, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``list_projects`` caches instance-wide; membership is applied after the read.

    Warm the cache as the owner (every project), then read as a member of one:
    a filter applied before the cache write would serve the owner's list, and
    one applied only on a miss would serve it from the warm entry. The cache is
    a real (in-memory) store here, so the member's read is provably served from
    the owner's warm entry, and that entry provably holds every project.
    """
    memory = _InMemoryCache()
    memory.install(monkeypatch)
    key = cache.key_projects_list(DEFAULT_ORG_ID)

    await _create_project(actors.owner, "cache-a")
    await _create_project(actors.owner, "cache-b")
    await add_member_by_slug("cache-b", "editor@example.com", "editor")
    assert key not in memory.store  # creation invalidated the list

    assert await _slugs(actors.owner) == {"cache-a", "cache-b"}  # warms the cache
    assert memory.cached_slugs(key) == {"cache-a", "cache-b"}
    assert memory.hits_on(key) == 0

    assert await _slugs(actors.editor) == {"cache-b"}
    assert memory.hits_on(key) == 1, "the member's read must be served from the warm entry"
    # The member's filtered read did not narrow the shared entry.
    assert memory.cached_slugs(key) == {"cache-a", "cache-b"}

    assert await _slugs(actors.stranger) == set()
    # And the other way round: a narrow caller first must not narrow the owner.
    assert await _slugs(actors.owner) == {"cache-a", "cache-b"}
    assert memory.hits_on(key) == 3
    assert memory.cached_slugs(key) == {"cache-a", "cache-b"}

    # A membership change needs no cache invalidation to show up.
    await add_member_by_slug("cache-a", "editor@example.com", "viewer")
    listed = await actors.editor.get("/api/v1/projects")
    assert memory.hits_on(key) == 4
    by_slug = {item["slug"]: item for item in listed.json()}
    assert set(by_slug) == {"cache-a", "cache-b"}
    assert by_slug["cache-a"]["my_role"] == "viewer"
    assert by_slug["cache-a"]["can_mutate"] is False
    assert by_slug["cache-b"]["my_role"] == "editor"
    assert by_slug["cache-b"]["can_mutate"] is True


@pytest.mark.asyncio
async def test_my_role_per_caller(actors: Actors) -> None:
    await _create_project(actors.editor, "roles")
    await add_member_by_slug("roles", "stranger@example.com", "viewer")
    # The row is authoritative: an editor row makes an editor.
    await add_member_by_slug("roles", "viewer@example.com", "editor")

    expected = {
        "owner": ("owner", True),
        "editor": ("editor", True),
        "stranger": ("viewer", False),
        "viewer": ("editor", True),
    }
    for who, (role, can_mutate) in expected.items():
        client = getattr(actors, who)
        detail = await client.get("/api/v1/projects/roles")
        assert detail.status_code == 200, (who, detail.text)
        assert detail.json()["my_role"] == role, who
        assert detail.json()["can_mutate"] is can_mutate, who


async def _seed_scan_run(project_id: uuid.UUID, source_id: uuid.UUID, name: str) -> None:
    now = datetime.now(UTC) - timedelta(minutes=5)
    async with TestSessionLocal() as session:
        config = ScanConfig(
            project_id=project_id, data_source_id=source_id, name=name, base_query="SELECT 1"
        )
        session.add(config)
        await session.flush()
        session.add(
            ScanJob(
                scan_config_id=config.id,
                status="completed",
                created_at=now,
                updated_at=now,
                started_at=now,
                completed_at=now,
                result_summary={"events_created": 1},
            )
        )
        await session.commit()


async def _global_source(name: str = "shared-wh") -> uuid.UUID:
    source_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(
            DataSource(
                id=source_id,
                project_id=None,
                name=name,
                db_type="clickhouse",
                host="localhost",
                port=8123,
                database_name="analytics",
                username="",
                password_encrypted="",
            )
        )
        await session.commit()
    return source_id


@pytest.mark.asyncio
async def test_workspace_activity_is_filtered_by_membership(actors: Actors) -> None:
    alpha = await _create_project(actors.owner, "feed-alpha")
    beta = await _create_project(actors.editor, "feed-beta")
    source_id = await _global_source()
    await _seed_scan_run(uuid.UUID(alpha["id"]), source_id, "alpha scan")
    await _seed_scan_run(uuid.UUID(beta["id"]), source_id, "beta scan")

    async def feed_slugs(client: AsyncClient) -> set[str]:
        resp = await client.get("/api/v1/activity", params={"limit": 100})
        assert resp.status_code == 200, resp.text
        return {item["project_slug"] for item in resp.json()}

    owner_view = await feed_slugs(actors.owner)
    assert {"feed-alpha", "feed-beta"} <= owner_view
    assert await feed_slugs(actors.editor) <= {"feed-beta"}
    assert "feed-beta" in await feed_slugs(actors.editor)
    assert await feed_slugs(actors.stranger) == set()

    hidden = await actors.editor.get("/api/v1/activity/projects/feed-alpha")
    assert hidden.status_code == 404
    assert hidden.json()["detail"] == NOT_FOUND


# ── data sources ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sources_bound_to_a_hidden_project_are_hidden(actors: Actors) -> None:
    private = await _create_project(actors.owner, "ds-private")
    await _create_project(actors.editor, "ds-mine")
    bound_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(
            DataSource(
                id=bound_id,
                project_id=uuid.UUID(private["id"]),
                name="private-synthetic",
                db_type="synthetic",
                host="",
                port=0,
                database_name="",
                username="",
                password_encrypted="",
            )
        )
        await session.commit()
    global_id = await _global_source("global-wh")

    listing = await actors.editor.get("/api/v1/data-sources")
    assert listing.status_code == 200, listing.text
    ids = {row["id"] for row in listing.json()}
    assert str(global_id) in ids
    assert str(bound_id) not in ids

    for url in (f"/api/v1/data-sources/{bound_id}", f"/api/v1/data-sources/{bound_id}/schema"):
        resp = await actors.editor.get(url)
        assert resp.status_code == 404, (url, resp.text)

    # The owner sees both.
    owner_ids = {row["id"] for row in (await actors.owner.get("/api/v1/data-sources")).json()}
    assert {str(bound_id), str(global_id)} <= owner_ids

    # Membership opens it.
    await add_member_by_slug("ds-private", "editor@example.com", "viewer")
    listing = await actors.editor.get("/api/v1/data-sources")
    assert str(bound_id) in {row["id"] for row in listing.json()}
    assert (await actors.editor.get(f"/api/v1/data-sources/{bound_id}")).status_code == 200


@pytest.mark.asyncio
async def test_scan_refs_and_counts_cover_member_projects_only(actors: Actors) -> None:
    """A shared warehouse must not reveal a hidden project's name, slug or volume."""
    hidden = await _create_project(actors.owner, "refs-hidden")
    mine = await _create_project(actors.editor, "refs-mine")
    source_id = await _global_source("refs-wh")
    await _seed_scan_run(uuid.UUID(hidden["id"]), source_id, "hidden scan")
    await _seed_scan_run(uuid.UUID(mine["id"]), source_id, "my scan")

    def row_for(payload: list[dict[str, Any]]) -> dict[str, Any]:
        return next(row for row in payload if row["id"] == str(source_id))

    as_editor = row_for((await actors.editor.get("/api/v1/data-sources")).json())
    assert as_editor["scan_count"] == 1
    assert {ref["project_slug"] for ref in as_editor["scans"]} == {"refs-mine"}
    assert "refs-hidden" not in str(as_editor)

    single = (await actors.editor.get(f"/api/v1/data-sources/{source_id}")).json()
    assert single["scan_count"] == 1

    as_owner = row_for((await actors.owner.get("/api/v1/data-sources")).json())
    assert as_owner["scan_count"] == 2
    assert {ref["project_slug"] for ref in as_owner["scans"]} == {"refs-hidden", "refs-mine"}


# ── creation paths ──────────────────────────────────────────────────────────


async def _membership(slug: str, user_id: str) -> ProjectMember | None:
    async with TestSessionLocal() as session:
        return await session.scalar(
            select(ProjectMember)
            .join(Project, Project.id == ProjectMember.project_id)
            .where(Project.slug == slug, ProjectMember.user_id == uuid.UUID(user_id))
        )


@pytest.mark.asyncio
async def test_the_creator_is_an_editor_member(actors: Actors) -> None:
    created = await _create_project(actors.editor, "made-by-editor")
    assert created["my_role"] == "editor"
    assert created["can_mutate"] is True

    row = await _membership("made-by-editor", actors.ids["editor"])
    assert row is not None
    assert str(row.role) == "editor"

    listed = await actors.editor.get("/api/v1/projects/made-by-editor/members")
    assert listed.status_code == 200, listed.text
    assert [(m["email"], m["role"]) for m in listed.json()] == [("editor@example.com", "editor")]


@pytest.mark.asyncio
async def test_member_role_service_rules(actors: Actors) -> None:
    """``project_access`` in one place: org owner, members by row, non-member."""
    project = await _create_project(actors.editor, "svc-rules")
    project_id = uuid.UUID(project["id"])
    await add_member_by_slug("svc-rules", "viewer@example.com", "viewer")

    async with TestSessionLocal() as session:
        users = {user.email: user for user in (await session.scalars(select(User))).all()}
        assert (
            await project_access.member_role(session, users["owner@example.com"], project_id)
            == "owner"
        )
        assert (
            await project_access.member_role(session, users["editor@example.com"], project_id)
            == "editor"
        )
        assert (
            await project_access.member_role(session, users["viewer@example.com"], project_id)
            == "viewer"
        )
        assert (
            await project_access.member_role(session, users["stranger@example.com"], project_id)
            is None
        )
        # Every project of the org, never "everything on the instance" (None).
        assert await project_access.member_project_ids(session, users["owner@example.com"]) == {
            project_id
        }
        # A project that does not exist has no role, even for the org owner (critique #4).
        assert (
            await project_access.member_role(session, users["owner@example.com"], uuid.uuid4())
            is None
        )
        assert (
            await project_access.member_project_ids(session, users["stranger@example.com"]) == set()
        )
        assert await project_access.member_project_ids(session, users["editor@example.com"]) == {
            project_id
        }
    assert project_access.can_edit("owner")
    assert project_access.can_edit("editor")
    assert not project_access.can_edit("viewer")
    assert not project_access.can_edit(None)


# ── member management API ───────────────────────────────────────────────────


def _members_url(slug: str, user_id: str | None = None) -> str:
    base = f"/api/v1/projects/{slug}/members"
    return base if user_id is None else f"{base}/{user_id}"


async def _audit_actions(slug: str) -> list[str]:
    async with TestSessionLocal() as session:
        rows = await session.scalars(
            select(AuditLog.action).where(
                AuditLog.project_slug == slug, AuditLog.action.like("project.member_%")
            )
        )
        # Sorted, not ordered by created_at: SQLite stamps whole seconds, so
        # three writes in one test tie.
        return sorted(rows.all())


@pytest.mark.asyncio
async def test_owner_manages_members_end_to_end(actors: Actors) -> None:
    await _create_project(actors.owner, "team")

    added = await actors.owner.post(
        _members_url("team"), json={"user_id": actors.ids["editor"], "role": "viewer"}
    )
    assert added.status_code == 201, added.text
    body = added.json()
    assert body["user_id"] == actors.ids["editor"]
    assert body["email"] == "editor@example.com"
    assert body["name"] == "Editor"
    assert body["role"] == "viewer"
    assert body["added_at"]

    # The new member sees the project, and the member list.
    assert (await actors.editor.get("/api/v1/projects/team")).status_code == 200
    listed = await actors.editor.get(_members_url("team"))
    assert listed.status_code == 200, listed.text
    # The owner created the project, so they hold a creator row too.
    assert {m["user_id"] for m in listed.json()} == {actors.ids["owner"], actors.ids["editor"]}

    duplicate = await actors.owner.post(
        _members_url("team"), json={"user_id": actors.ids["editor"], "role": "editor"}
    )
    assert duplicate.status_code == 409, duplicate.text

    unknown = await actors.owner.post(
        _members_url("team"), json={"user_id": str(uuid.uuid4()), "role": "viewer"}
    )
    assert unknown.status_code == 404, unknown.text

    promoted = await actors.owner.patch(
        _members_url("team", actors.ids["editor"]), json={"role": "editor"}
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["role"] == "editor"
    created = await actors.editor.post(
        "/api/v1/projects/team/event-types", json={"name": "pv", "display_name": "PV"}
    )
    assert created.status_code == 201, created.text

    missing = await actors.owner.patch(
        _members_url("team", actors.ids["stranger"]), json={"role": "editor"}
    )
    assert missing.status_code == 404, missing.text

    removed = await actors.owner.delete(_members_url("team", actors.ids["editor"]))
    assert removed.status_code == 204, removed.text
    gone = await actors.editor.get("/api/v1/projects/team")
    assert gone.status_code == 404
    assert "team" not in await _slugs(actors.editor)

    assert await _audit_actions("team") == [
        "project.member_add",
        "project.member_remove",
        "project.member_update",
    ]


@pytest.mark.asyncio
async def test_the_creator_manages_their_own_projects_members(actors: Actors) -> None:
    await _create_project(actors.editor, "editors-team")
    added = await actors.editor.post(
        _members_url("editors-team"), json={"user_id": actors.ids["stranger"], "role": "editor"}
    )
    assert added.status_code == 201, added.text
    assert (await actors.stranger.get("/api/v1/projects/editors-team")).status_code == 200


@pytest.mark.asyncio
async def test_only_the_creator_or_the_owner_manages_members(actors: Actors) -> None:
    await _create_project(actors.owner, "managed")
    await add_member_by_slug("managed", "editor@example.com", "editor")

    # An editor MEMBER edits the plan but does not manage who is in the project.
    refused = await actors.editor.post(
        _members_url("managed"), json={"user_id": actors.ids["stranger"], "role": "viewer"}
    )
    assert refused.status_code == 403, refused.text
    re_roled = await actors.editor.patch(
        _members_url("managed", actors.ids["editor"]), json={"role": "viewer"}
    )
    assert re_roled.status_code == 403, re_roled.text
    self_removed = await actors.editor.delete(_members_url("managed", actors.ids["editor"]))
    assert self_removed.status_code == 403, self_removed.text

    # A viewer member is refused by the write gate first.
    await add_member_by_slug("managed", "stranger@example.com", "viewer")
    viewer_refused = await actors.stranger.post(
        _members_url("managed"), json={"user_id": actors.ids["viewer"], "role": "viewer"}
    )
    assert viewer_refused.status_code == 403, viewer_refused.text

    # A non-member cannot even list them.
    assert (await actors.viewer.get(_members_url("managed"))).status_code == 404

    assert await _audit_actions("managed") == []


@pytest.mark.asyncio
async def test_member_management_needs_a_browser_session(actors: Actors) -> None:
    """Membership is access control: even the owner's write key cannot grant it."""
    await _create_project(actors.owner, "keyless")
    minted = await actors.owner.post(
        "/api/v1/me/api-keys", json={"name": "agent", "scope": "write"}
    )
    assert minted.status_code == 201, minted.text
    headers = {"Authorization": f"Bearer {minted.json()['token']}"}

    async with _new_client() as bearer:
        listed = await bearer.get(_members_url("keyless"), headers=headers)
        assert listed.status_code == 200, listed.text
        refused = await bearer.post(
            _members_url("keyless"),
            json={"user_id": actors.ids["editor"], "role": "viewer"},
            headers=headers,
        )
        assert refused.status_code == 403, refused.text
    assert await _membership("keyless", actors.ids["editor"]) is None


# ── collaborators must be members ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_branch_reviewer_must_be_a_member(actors: Actors) -> None:
    await _create_project(actors.owner, "review-me")
    branch = await actors.owner.post("/api/v1/projects/review-me/branches", json={"name": "wip"})
    assert branch.status_code == 201, branch.text
    url = f"/api/v1/projects/review-me/branches/{branch.json()['id']}/reviewers"

    refused = await actors.owner.post(url, json={"user_id": actors.ids["editor"]})
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == "User is not a member of this project"

    await add_member_by_slug("review-me", "editor@example.com", "viewer")
    added = await actors.owner.post(url, json={"user_id": actors.ids["editor"]})
    assert added.status_code == 201, added.text


# ── demo reset keeps the audience ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_demo_create_and_reset_keep_the_members(actors: Actors) -> None:
    """The reset purges the row (and its memberships by cascade) and seeds a new
    one under the same slug; whoever could see the demo before can see it after."""
    demo = await actors.owner.post("/api/v1/projects/demo")
    assert demo.status_code == 202, demo.text
    slug = demo.json()["slug"]
    assert await _membership(slug, actors.ids["owner"]) is not None

    # A demo is not shared with anyone by default.
    assert (await actors.editor.get(f"/api/v1/projects/{slug}")).status_code == 404
    await add_member_by_slug(slug, "editor@example.com", "viewer")
    before = await actors.editor.get(f"/api/v1/projects/{slug}")
    assert before.status_code == 200, before.text

    reset = await actors.owner.post(f"/api/v1/projects/demo/{slug}/reset")
    assert reset.status_code == 200, reset.text
    assert reset.json()["id"] != before.json()["id"]

    after = await actors.editor.get(f"/api/v1/projects/{slug}")
    assert after.status_code == 200, after.text
    assert after.json()["my_role"] == "viewer"
    regranted = await _membership(slug, actors.ids["editor"])
    assert regranted is not None
    assert str(regranted.role) == "viewer"
    assert await _membership(slug, actors.ids["owner"]) is not None
    assert (await actors.stranger.get(f"/api/v1/projects/{slug}")).status_code == 404


@pytest.mark.asyncio
async def test_an_editors_demo_is_theirs_alone(actors: Actors) -> None:
    demo = await actors.editor.post("/api/v1/projects/demo")
    assert demo.status_code == 202, demo.text
    slug = demo.json()["slug"]
    assert slug in await _slugs(actors.editor)
    assert slug not in await _slugs(actors.stranger)
    assert slug in await _slugs(actors.owner)


# ── cascades ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deleting_a_project_drops_its_memberships(actors: Actors) -> None:
    await _create_project(actors.owner, "doomed")
    await add_member_by_slug("doomed", "editor@example.com", "editor")
    deleted = await actors.owner.delete("/api/v1/projects/doomed")
    assert deleted.status_code == 204, deleted.text
    async with TestSessionLocal() as session:
        remaining = await session.scalar(select(func.count(ProjectMember.id)))
    assert remaining == 0


@pytest.mark.asyncio
async def test_deleting_or_resetting_a_project_drops_its_realtime_keys(
    actors: Actors, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A purged id is never reused, so its stream state must not linger in Redis."""
    from tripl import realtime

    dropped: list[uuid.UUID] = []

    async def spy(project_id: uuid.UUID) -> None:
        dropped.append(project_id)

    monkeypatch.setattr(realtime, "async_drop_project_keys", spy)

    project = await _create_project(actors.owner, "doomed-rt")
    deleted = await actors.owner.delete("/api/v1/projects/doomed-rt")
    assert deleted.status_code == 204, deleted.text
    assert dropped == [uuid.UUID(project["id"])]

    demo = await actors.owner.post("/api/v1/projects/demo")
    assert demo.status_code == 202, demo.text
    reset = await actors.owner.post(f"/api/v1/projects/demo/{demo.json()['slug']}/reset")
    assert reset.status_code == 200, reset.text
    assert dropped[-1] == uuid.UUID(demo.json()["id"])


# ── an open SSE stream ends when its caller loses the project ───────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _connected() -> bool:
    return False


@pytest.mark.asyncio
async def test_the_event_stream_guard_ends_a_removed_members_stream(actors: Actors) -> None:
    """The guard re-reads membership once per interval and stops a revoked stream."""
    from tripl.api.v1.events_stream import membership_guard

    project = await _create_project(actors.owner, "sse-revoke")
    await add_member_by_slug("sse-revoke", "editor@example.com", "viewer")
    clock = _Clock()
    should_stop = membership_guard(
        user_id=uuid.UUID(actors.ids["editor"]),
        project_id=uuid.UUID(project["id"]),
        is_disconnected=_connected,
        session_factory=TestSessionLocal,
        interval_seconds=15.0,
        clock=clock,
    )

    assert await should_stop() is False
    clock.now += 15.0
    assert await should_stop() is False  # re-checked: still a member

    await remove_member_by_slug("sse-revoke", "editor@example.com")
    clock.now += 5.0
    assert await should_stop() is False  # inside the interval: no re-read yet
    clock.now += 10.0
    assert await should_stop() is True  # re-read: no longer a member
    # Revocation sticks, even if the membership came back mid-stream.
    await add_member_by_slug("sse-revoke", "editor@example.com", "viewer")
    clock.now += 15.0
    assert await should_stop() is True


@pytest.mark.asyncio
async def test_the_event_stream_guard_keeps_the_owner_and_fails_closed(actors: Actors) -> None:
    from tripl.api.v1.events_stream import membership_guard

    project = await _create_project(actors.owner, "sse-owner")
    clock = _Clock()
    owner_guard = membership_guard(
        user_id=uuid.UUID(actors.ids["owner"]),
        project_id=uuid.UUID(project["id"]),
        is_disconnected=_connected,
        session_factory=TestSessionLocal,
        interval_seconds=1.0,
        clock=clock,
    )
    for _ in range(3):
        clock.now += 1.0
        assert await owner_guard() is False  # no row needed

    # A client that went away stops the stream regardless of membership.
    async def gone() -> bool:
        return True

    gone_guard = membership_guard(
        user_id=uuid.UUID(actors.ids["owner"]),
        project_id=uuid.UUID(project["id"]),
        is_disconnected=gone,
        session_factory=TestSessionLocal,
        clock=clock,
    )
    assert await gone_guard() is True

    # A failed re-check ends the stream; the reconnect meets the 404 gate.
    def broken_factory() -> Any:
        raise RuntimeError("database unavailable")

    broken_guard = membership_guard(
        user_id=uuid.UUID(actors.ids["owner"]),
        project_id=uuid.UUID(project["id"]),
        is_disconnected=_connected,
        session_factory=broken_factory,  # type: ignore[arg-type]
        interval_seconds=1.0,
        clock=clock,
    )
    assert await broken_guard() is False
    clock.now += 1.0
    assert await broken_guard() is True


@pytest.mark.asyncio
async def test_the_event_stream_guard_ends_the_owners_stream_after_a_demo_reset(
    actors: Actors,
) -> None:
    """A reset re-creates the demo under a new id; the owner's old-id stream must end.

    ``member_role`` reads the project row for everyone, the org owner included,
    so a stream left on the dead id's channel ends.
    """
    from tripl.api.v1.events_stream import membership_guard

    demo = await actors.owner.post("/api/v1/projects/demo")
    assert demo.status_code == 202, demo.text
    slug = demo.json()["slug"]
    old_id = uuid.UUID(demo.json()["id"])
    clock = _Clock()
    owner_guard = membership_guard(
        user_id=uuid.UUID(actors.ids["owner"]),
        project_id=old_id,
        is_disconnected=_connected,
        session_factory=TestSessionLocal,
        interval_seconds=1.0,
        clock=clock,
    )
    clock.now += 1.0
    assert await owner_guard() is False

    reset = await actors.owner.post(f"/api/v1/projects/demo/{slug}/reset")
    assert reset.status_code == 200, reset.text
    assert uuid.UUID(reset.json()["id"]) != old_id

    clock.now += 1.0
    assert await owner_guard() is True


@pytest.mark.asyncio
async def test_the_event_stream_is_opened_with_the_membership_guard(
    actors: Actors, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint hands the generator the guard, bound to the caller and project."""
    from tripl.api.v1 import events_stream

    project = await _create_project(actors.owner, "sse-wired")
    await add_member_by_slug("sse-wired", "editor@example.com", "viewer")
    seen: dict[str, Any] = {}
    real_guard = events_stream.membership_guard

    def spy(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return real_guard(**kwargs)

    monkeypatch.setattr(events_stream, "membership_guard", spy)
    resp = await actors.editor.get("/api/v1/projects/sse-wired/events/stream?max_events=0")
    assert resp.status_code == 200, resp.text
    assert seen["user_id"] == uuid.UUID(actors.ids["editor"])
    assert seen["project_id"] == uuid.UUID(project["id"])
