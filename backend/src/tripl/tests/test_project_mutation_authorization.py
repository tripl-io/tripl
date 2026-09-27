"""Project-scoped authorization for the per-project mutation surface.

Roles are instance-wide, so before tripl-jfm3.19 the ``editor`` role meant "may
rewrite the tracking plan of every project on the instance". That was first
fenced by provenance (creator / owner-created shared projects) and is now
explicit project membership (tripl-vefw):

* a non-member does not see the project at all — 404 on every slug route;
* a member may mutate when their project role is ``editor`` and their instance
  role is not ``viewer`` (the instance role caps the membership role);
* the instance owner sees and manages everything without a row.

These tests pin the DENIED cases route by route, plus the allowed cases.
"""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tripl.api.deps import PROJECT_SCOPED_GATES
from tripl.main import app
from tripl.models.project import Project
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_rbac import READ_LIKE_MUTATING_PATHS, iter_api_routes

PASSWORD = "Password123!"

# Slug-scoped writes that change only the CALLER's own state, never the plan, so
# any project member may make them — viewers included. Membership is still
# enforced by the router-level ``require_project_membership`` gate (a non-member
# gets 404), and ``test_rbac``'s write-gate audit still requires a write gate on
# them, so a ``read``-scope API key is refused.
MEMBER_PERSONAL_WRITE_PATHS = {
    # Watch / Unwatch / Mute an entity (GH #259): a personal notification
    # preference, and a viewer must be able to watch what they read.
    "/api/v1/projects/{slug}/subscriptions/{entity_type}/{entity_id}",
    # Build the incident summary for the current facts (GH #267): a viewer who
    # opens an incident gets its summary generated lazily. Membership is still
    # required router-wide; a read API key is refused by the write gate, and
    # forcing a fresh generation (/summary/regenerate) stays editor-only.
    "/api/v1/projects/{slug}/alert-inbox/{correlation_group_id}/summary",
}


def _new_client() -> AsyncClient:
    """A client with its own cookie jar, so several roles can act interleaved."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _register(client: AsyncClient, email: str, name: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "name": name},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_project(client: AsyncClient, slug: str) -> dict:
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _mark_demo(slug: str) -> None:
    """Turn a real project into a demo without paying for demo seeding."""
    async with TestSessionLocal() as session:
        project = await session.scalar(select(Project).where(Project.slug == slug))
        assert project is not None
        project.is_demo = True
        await session.commit()


async def _clear_creator(slug: str) -> None:
    """Simulate a project that predates creator tracking."""
    async with TestSessionLocal() as session:
        project = await session.scalar(select(Project).where(Project.slug == slug))
        assert project is not None
        project.created_by_user_id = None
        await session.commit()


class Actors:
    """owner + two unrelated editors + a viewer, each with its own session."""

    def __init__(self) -> None:
        self.owner = _new_client()
        self.editor = _new_client()
        self.stranger = _new_client()
        self.viewer = _new_client()

    async def aclose(self) -> None:
        for client in (self.owner, self.editor, self.stranger, self.viewer):
            await client.aclose()


@pytest_asyncio.fixture
async def actors() -> AsyncGenerator[Actors]:
    people = Actors()
    # First registered user is the instance owner; everyone after defaults to editor.
    await _register(people.owner, "owner@example.com", "Owner")
    await _register(people.editor, "editor@example.com", "Editor")
    await _register(people.stranger, "stranger@example.com", "Stranger")
    viewer = await _register(people.viewer, "viewer@example.com", "Viewer")

    demote = await people.owner.patch(
        f"/api/v1/users/{viewer['id']}",
        json={"role": "viewer"},
    )
    assert demote.status_code == 200, demote.text
    # A role change invalidates the user's sessions, so log the viewer back in.
    relogin = await people.viewer.post(
        "/api/v1/auth/login",
        json={"email": "viewer@example.com", "password": PASSWORD},
    )
    assert relogin.status_code == 200, relogin.text

    yield people
    await people.aclose()


# One representative mutation per per-project router that carries the editor gate.
# (method, path suffix, json body)
MUTATION_ROUTES = [
    ("POST", "event-types", {"name": "pv", "display_name": "Page View"}),
    ("POST", "variables", {"name": "plan", "display_name": "Plan", "value_type": "string"}),
    ("POST", "meta-fields", {"name": "team", "label": "Team", "field_type": "string"}),
    ("POST", "annotations", {"bucket": "2026-01-01T00:00:00Z", "label": "deploy"}),
    ("POST", "branches", {"name": "feature-x"}),
    ("POST", "revisions", {"message": "snapshot"}),
    ("POST", "search/reindex", None),
    ("POST", "alert-destinations", {"name": "ops", "kind": "webhook", "config": {}}),
    ("PATCH", "anomaly-settings", {"enabled": False}),
]


async def _call(client: AsyncClient, method: str, slug: str, suffix: str, body: dict | None):
    url = f"/api/v1/projects/{slug}/{suffix}"
    if method == "POST":
        return await client.post(url, json=body if body is not None else {})
    return await client.patch(url, json=body or {})


def test_every_project_scoped_mutation_carries_a_project_gate() -> None:
    """A new ``/projects/{slug}/...`` mutation must not ship with an unscoped gate.

    ``PROJECT_SCOPED_GATES`` holds every dependency that resolves the path's
    project: ``get_editor_user`` runs :func:`require_project_mutation_access`,
    and the two owner gates are instance-owner-only and therefore pass it by
    definition. A slug-scoped mutation wired to bare ``get_write_user`` (or to no
    gate at all) would reopen tripl-jfm3.19, so fail the build instead of waiting
    for the next audit.

    Read from ``deps`` rather than spelled here: this audit went stale the moment
    tripl-cj5z added a third gate, and a literal set means the audit silently
    reclassifies the new gate's routes as ungated — an audit that fails open is
    worse than none.
    """
    project_gates = PROJECT_SCOPED_GATES
    offenders: list[str] = []

    for path, route in iter_api_routes():
        methods = (route.methods or set()) & {"POST", "PATCH", "PUT", "DELETE"}
        if not methods or "{slug}" not in path:
            continue
        if path in READ_LIKE_MUTATING_PATHS or path in MEMBER_PERSONAL_WRITE_PATHS:
            continue
        calls = {dependency.call for dependency in route.dependant.dependencies}
        if calls.isdisjoint(project_gates):
            offenders.append(f"{','.join(sorted(methods))} {path}")

    assert offenders == []


@pytest.mark.asyncio
async def test_non_member_editor_gets_404_on_every_mutation(actors: Actors) -> None:
    """The headline vector: a self-registered editor reaching someone else's project."""
    await _create_project(actors.editor, "editors-own")

    for method, suffix, body in MUTATION_ROUTES:
        hidden = await _call(actors.stranger, method, "editors-own", suffix, body)
        assert hidden.status_code == 404, f"{method} {suffix} -> {hidden.status_code}"
        assert hidden.json()["detail"] == "Project not found"

    # The creator (an editor member by construction) and the instance owner are allowed.
    allowed = await _call(
        actors.editor, "POST", "editors-own", "event-types", MUTATION_ROUTES[0][2]
    )
    assert allowed.status_code == 201, allowed.text
    owner_allowed = await actors.owner.post(
        "/api/v1/projects/editors-own/event-types",
        json={"name": "se", "display_name": "Session"},
    )
    assert owner_allowed.status_code == 201, owner_allowed.text


@pytest.mark.asyncio
async def test_editor_member_may_mutate_another_editors_project(actors: Actors) -> None:
    """Membership, not provenance, decides: an added editor member edits the plan."""
    await _create_project(actors.editor, "shared-by-editor")
    await add_member_by_slug("shared-by-editor", "stranger@example.com", "editor")

    resp = await actors.stranger.post(
        "/api/v1/projects/shared-by-editor/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_viewer_member_cannot_mutate(actors: Actors) -> None:
    """A ``viewer`` project role reads but never writes, whatever the instance role."""
    await _create_project(actors.editor, "viewer-member")
    await add_member_by_slug("viewer-member", "stranger@example.com", "viewer")

    readable = await actors.stranger.get("/api/v1/projects/viewer-member/event-types")
    assert readable.status_code == 200, readable.text

    for method, suffix, body in MUTATION_ROUTES:
        denied = await _call(actors.stranger, method, "viewer-member", suffix, body)
        assert denied.status_code == 403, f"{method} {suffix} -> {denied.status_code}"


@pytest.mark.asyncio
async def test_non_member_cannot_see_another_users_demo(actors: Actors) -> None:
    """A demo is visible to its creator (a member) and the owner only."""
    await _create_project(actors.owner, "owner-demo")
    await _mark_demo("owner-demo")

    for method, suffix, body in MUTATION_ROUTES:
        hidden = await _call(actors.editor, method, "owner-demo", suffix, body)
        assert hidden.status_code == 404, f"{method} {suffix} -> {hidden.status_code}"

    allowed = await actors.owner.post(
        "/api/v1/projects/owner-demo/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    assert allowed.status_code == 201, allowed.text


@pytest.mark.asyncio
async def test_editor_demo_is_closed_to_other_editors(actors: Actors) -> None:
    """A demo an editor made is theirs; another editor is out, the owner is in."""
    await _create_project(actors.editor, "editor-demo")
    await _mark_demo("editor-demo")

    hidden = await actors.stranger.post(
        "/api/v1/projects/editor-demo/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    assert hidden.status_code == 404, hidden.text

    creator_ok = await actors.editor.post(
        "/api/v1/projects/editor-demo/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    assert creator_ok.status_code == 201, creator_ok.text

    owner_ok = await actors.owner.post(
        "/api/v1/projects/editor-demo/event-types",
        json={"name": "se", "display_name": "Session"},
    )
    assert owner_ok.status_code == 201, owner_ok.text


@pytest.mark.asyncio
async def test_owner_created_projects_are_no_longer_open_to_every_editor(
    actors: Actors,
) -> None:
    """The old "shared workspace project" rule is gone: owner-created means nothing special."""
    await _create_project(actors.owner, "team-plan")

    for client in (actors.editor, actors.stranger):
        hidden = await client.post(
            "/api/v1/projects/team-plan/event-types",
            json={"name": f"pv-{id(client)}", "display_name": "Page View"},
        )
        assert hidden.status_code == 404, hidden.text

    await add_member_by_slug("team-plan", "editor@example.com", "editor")
    added = await actors.editor.post(
        "/api/v1/projects/team-plan/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    assert added.status_code == 201, added.text
    # Adding one editor does not open the project to the others.
    still_hidden = await actors.stranger.get("/api/v1/projects/team-plan")
    assert still_hidden.status_code == 404, still_hidden.text


@pytest.mark.asyncio
async def test_projects_predating_creator_tracking_follow_membership(actors: Actors) -> None:
    """``created_by_user_id IS NULL`` no longer means "shared": membership still decides."""
    await _create_project(actors.editor, "legacy-plan")
    await _clear_creator("legacy-plan")

    hidden = await actors.stranger.post(
        "/api/v1/projects/legacy-plan/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    assert hidden.status_code == 404, hidden.text

    # The creator's membership row outlives the creator column.
    kept = await actors.editor.post(
        "/api/v1/projects/legacy-plan/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    assert kept.status_code == 201, kept.text


@pytest.mark.asyncio
async def test_viewer_is_denied_on_every_project_shape(actors: Actors) -> None:
    """An instance viewer is capped at viewer even with an editor membership row.

    The 403 on the write is answered by ``require_editor``'s instance-role check
    before the project role is consulted, so it does not show the cap by itself;
    the cap is asserted on ``my_role`` / ``can_mutate`` of the project response.
    """
    await _create_project(actors.owner, "team-plan-v")
    await _create_project(actors.editor, "editors-own-v")

    for slug in ("team-plan-v", "editors-own-v"):
        hidden = await actors.viewer.post(
            f"/api/v1/projects/{slug}/event-types",
            json={"name": "pv", "display_name": "Page View"},
        )
        assert hidden.status_code == 404, hidden.text

        await add_member_by_slug(slug, "viewer@example.com", "editor")
        detail = await actors.viewer.get(f"/api/v1/projects/{slug}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["my_role"] == "viewer"
        assert detail.json()["can_mutate"] is False

        denied = await actors.viewer.post(
            f"/api/v1/projects/{slug}/event-types",
            json={"name": "pv", "display_name": "Page View"},
        )
        assert denied.status_code == 403, denied.text


@pytest.mark.asyncio
async def test_reads_are_members_only(actors: Actors) -> None:
    """Reads follow membership too: a non-member gets 404, any member reads."""
    await _create_project(actors.editor, "readable")
    await actors.editor.post(
        "/api/v1/projects/readable/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )

    for client in (actors.stranger, actors.viewer):
        listing = await client.get("/api/v1/projects/readable/event-types")
        assert listing.status_code == 404, listing.text
        detail = await client.get("/api/v1/projects/readable")
        assert detail.status_code == 404, detail.text

    await add_member_by_slug("readable", "stranger@example.com", "viewer")
    await add_member_by_slug("readable", "viewer@example.com", "viewer")
    for client in (actors.stranger, actors.viewer):
        listing = await client.get("/api/v1/projects/readable/event-types")
        assert listing.status_code == 200, listing.text
        assert [et["name"] for et in listing.json()] == ["pv"]

        detail = await client.get("/api/v1/projects/readable")
        assert detail.status_code == 200, detail.text
        assert detail.json()["my_role"] == "viewer"


@pytest.mark.asyncio
async def test_project_identity_edits_stay_creator_or_owner(actors: Actors) -> None:
    """An editor member edits the plan, not PATCH /projects/{slug}."""
    await _create_project(actors.owner, "team-plan-p")
    await add_member_by_slug("team-plan-p", "editor@example.com", "editor")

    denied = await actors.editor.patch(
        "/api/v1/projects/team-plan-p", json={"name": "Renamed by a member"}
    )
    assert denied.status_code == 403, denied.text

    allowed = await actors.owner.patch("/api/v1/projects/team-plan-p", json={"name": "Renamed"})
    assert allowed.status_code == 200, allowed.text


@pytest.mark.asyncio
async def test_unknown_project_still_404s_for_a_denied_caller(actors: Actors) -> None:
    resp = await actors.stranger.post(
        "/api/v1/projects/no-such-project/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_api_key_inherits_its_owners_membership(actors: Actors) -> None:
    """An editor's API key is the editor — it cannot reach past the same fence."""
    await _create_project(actors.editor, "keyed-project")
    key_resp = await actors.stranger.post(
        "/api/v1/me/api-keys", json={"name": "agent", "scope": "write"}
    )
    assert key_resp.status_code == 201, key_resp.text
    token = key_resp.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    async with _new_client() as bearer:
        hidden = await bearer.post(
            "/api/v1/projects/keyed-project/event-types",
            json={"name": "pv", "display_name": "Page View"},
            headers=headers,
        )
        assert hidden.status_code == 404, hidden.text

        await add_member_by_slug("keyed-project", "stranger@example.com", "editor")
        allowed = await bearer.post(
            "/api/v1/projects/keyed-project/event-types",
            json={"name": "pv", "display_name": "Page View"},
            headers=headers,
        )
        assert allowed.status_code == 201, allowed.text
