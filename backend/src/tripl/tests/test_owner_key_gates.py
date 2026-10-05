"""Which owner-only routes an API key may pass, and which stay browser-only.

"Owner" means owner or admin of the request's organization (F20 PR4); the
classification of every owner-gated route is pinned below. Owner-only means
security and organization administration, so ``get_owner_user`` refuses a
Bearer token at any scope. That also blocked the bounded metrics
replay, which is why tripl-mcp ships no replay tool and the CLI dropped
``tripl scans replay``. The replay — and only the replay — now takes
``get_key_reachable_owner_user``, so this module pins all four corners of that
gate plus the exact route list that carries it: the value of a per-route
exception is that it stays short and reviewed.
"""

from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from tripl.api.deps import (
    ORG_ADMIN_REQUIRED,
    get_key_reachable_owner_user,
    get_owner_user,
    get_path_org_admin_user,
    get_path_org_owner_user,
    get_settings_admin_user,
    require_org_creator,
    require_platform_admin,
)
from tripl.main import app
from tripl.tests._members import add_member_by_slug
from tripl.tests.test_rbac import MIN_API_ROUTES, iter_api_routes
from tripl.worker.tasks import metrics

PASSWORD = "Password123!"
REPLAY_WINDOW = {"time_from": "2026-04-01T00:00:00Z", "time_to": "2026-04-02T00:00:00Z"}

# The complete set of owner-gated routes an API key may reach. Enumerated from
# the live app below rather than asserted route by route, so a new route that
# copies the widened gate off this one fails the build instead of shipping.
KEY_REACHABLE_OWNER_ROUTES = {
    "POST /api/v1/projects/{slug}/scans/{scan_id}/metrics/replay",
}

# The stored policy that was explicitly NOT widened. Named individually
# because these are the routes whose reach a leaked ``tk_w_`` would extend from
# "re-run SQL an owner approved" to "own the warehouse credential".
SESSION_ONLY_OWNER_ROUTES = {
    "DELETE /api/v1/projects/{slug}",
    "POST /api/v1/data-sources",
    "PATCH /api/v1/data-sources/{ds_id}",
    "DELETE /api/v1/data-sources/{ds_id}",
    "POST /api/v1/data-sources/{ds_id}/test",
    "POST /api/v1/data-sources/test",
}


# F20 PR4: every route gated on organization owner/admin, session only
# (``get_owner_user``). Org-owned business resources: projects, their danger
# zone and settings, scan SQL authored against org-owned credentials, the
# org-owned data sources, a project's audit history, and org membership administration.
# A route moving in or out of this set is a security decision, so it is listed
# rather than derived.
ORG_ADMIN_SESSION_ONLY_ROUTES = {
    "DELETE /api/v1/projects/{slug}",
    "POST /api/v1/projects/{slug}/danger/reset-anomalies",
    "POST /api/v1/projects/{slug}/danger/reset-drifts",
    "POST /api/v1/projects/{slug}/danger/retire-unused-variables",
    "PATCH /api/v1/projects/{slug}/tracker-config",
    "PATCH /api/v1/projects/{slug}/branch-settings",
    "POST /api/v1/projects/{slug}/scans",
    "POST /api/v1/projects/{slug}/scans/preview",
    "GET /api/v1/projects/{slug}/scans/preview-jobs/{job_id}",
    "POST /api/v1/projects/{slug}/scans/dry-run",
    "GET /api/v1/projects/{slug}/scans/dry-run-jobs/{job_id}",
    "PATCH /api/v1/projects/{slug}/scans/{scan_id}",
    "DELETE /api/v1/projects/{slug}/scans/{scan_id}",
    "POST /api/v1/projects/{slug}/scans/{scan_id}/event-groups/apply",
    "POST /api/v1/data-sources",
    "POST /api/v1/data-sources/test",
    "GET /api/v1/data-sources/{ds_id}/stats",
    "PATCH /api/v1/data-sources/{ds_id}",
    "DELETE /api/v1/data-sources/{ds_id}",
    "POST /api/v1/data-sources/{ds_id}/test",
    "GET /api/v1/projects/{slug}/audit",
    "GET /api/v1/projects/{slug}/audit/actions",
    "GET /api/v1/projects/{slug}/audit/{entry_id}",
    "POST /api/v1/users/invitations",
    "GET /api/v1/users/invitations",
    "DELETE /api/v1/users/invitations/{invitation_id}",
    "PATCH /api/v1/users/{user_id}",
}

# The org-free legacy ``/settings`` surface (``get_settings_admin_user``): a
# platform admin, or an owner/admin of the organization the legacy route
# resolves (default org self-hosted, the user's only org hosted; F20 PR9).
# Operator fields in a write, and the ``system`` block, additionally need a
# platform admin.
SETTINGS_ADMIN_ROUTES = {
    "GET /api/v1/settings",
    "PATCH /api/v1/settings",
    "PUT /api/v1/settings",
    "GET /api/v1/settings/ai",
    "GET /api/v1/settings/ai/defaults",
    "POST /api/v1/settings/ai/test",
    "POST /api/v1/settings/email/test",
}


def _gate_calls(route: APIRoute) -> set[Any]:
    """Every dependency in a route's tree, not only the ones declared on it.

    ``route.dependant.dependencies`` is direct children only, so a gate reached
    through one wrapper dependency would be invisible — and the enumeration below
    would report the widened gate as unused while a route quietly used it.
    """
    calls: set[Any] = set()

    def walk(dependant: Any) -> None:
        for sub in dependant.dependencies:
            calls.add(sub.call)
            walk(sub)

    walk(route.dependant)
    return calls


def _routes_carrying(gate: Any) -> set[str]:
    routes = iter_api_routes()
    # ``iter_api_routes`` descends ``original_router`` because a naive walk of
    # ``app.routes`` reaches six of ~215 routes — and then every set comparison
    # below holds vacuously against an empty walk.
    assert len(routes) > MIN_API_ROUTES, f"route audit only reached {len(routes)} routes"
    return {
        f"{method} {path}"
        for path, route in routes
        for method in sorted(route.methods or set())
        if gate in _gate_calls(route)
    }


def test_key_reachable_owner_gate_is_confined_to_the_reviewed_routes() -> None:
    assert _routes_carrying(get_key_reachable_owner_user) == KEY_REACHABLE_OWNER_ROUTES


def test_session_only_owner_routes_did_not_move() -> None:
    """Widening the replay must not have widened anything else."""
    assert _routes_carrying(get_owner_user) >= SESSION_ONLY_OWNER_ROUTES
    assert SESSION_ONLY_OWNER_ROUTES.isdisjoint(_routes_carrying(get_key_reachable_owner_user))


def test_org_admin_routes_are_exactly_the_classified_set() -> None:
    """Every ``get_owner_user`` route is one the PR4 classification names, and vice versa."""
    assert _routes_carrying(get_owner_user) == ORG_ADMIN_SESSION_ONLY_ROUTES
    assert SESSION_ONLY_OWNER_ROUTES <= ORG_ADMIN_SESSION_ONLY_ROUTES


def test_settings_routes_take_the_settings_admin_gate() -> None:
    """``/settings`` is no longer behind the org gate, which binds no org there."""
    assert _routes_carrying(get_settings_admin_user) == SETTINGS_ADMIN_ROUTES
    assert SETTINGS_ADMIN_ROUTES.isdisjoint(_routes_carrying(get_owner_user))
    # The platform gate, wherever a route declares it, guards operator settings
    # only; creating an organization has its own mode-aware gate (critique #24).
    assert _routes_carrying(require_platform_admin) <= SETTINGS_ADMIN_ROUTES | PLATFORM_ADMIN_ROUTES


# The operator console (F20 PR9): the operator scope, whole, for platform admins.
PLATFORM_SETTINGS_ROUTES = {
    "GET /api/v1/platform/settings",
    "PATCH /api/v1/platform/settings",
    "POST /api/v1/platform/settings/ai/test",
    "POST /api/v1/platform/settings/email/test",
}

# The platform console's routes are the Enterprise package's.
PLATFORM_ADMIN_ROUTES = PLATFORM_SETTINGS_ROUTES
# Creating an organization: a platform admin self-hosted, any verified session
# hosted (``deps.require_org_creator``, F20 hosted sign-up).
ORG_CREATOR_ROUTES = {"POST /api/v1/orgs"}

# The organization management gates (F20 PR6): a membership of the ORGANIZATION
# THE PATH NAMES, with the owner/admin and owner gates session only. Listed, not
# derived, for the same reason as the sets above.
PATH_ORG_ADMIN_ROUTES = {
    "PATCH /api/v1/orgs/{org}",
    "PATCH /api/v1/orgs/{org}/members/{user_id}",
    "DELETE /api/v1/orgs/{org}/members/{user_id}",
    # An organization's own settings (F20 PR9): owner/admin of THAT org.
    "GET /api/v1/orgs/{org}/settings",
    "PATCH /api/v1/orgs/{org}/settings",
    "PUT /api/v1/orgs/{org}/settings",
    "POST /api/v1/orgs/{org}/settings/ai/test",
    "POST /api/v1/orgs/{org}/settings/email/test",
    # The organization's Jira/Linear defaults (F20 PR12): owner/admin of THAT org.
    "GET /api/v1/orgs/{org}/settings/trackers",
    "PATCH /api/v1/orgs/{org}/settings/trackers",
    # Organization groups: owner/admin of THAT org manage them; any member reads.
    "POST /api/v1/orgs/{org}/groups",
    "PATCH /api/v1/orgs/{org}/groups/{group_id}",
    "DELETE /api/v1/orgs/{org}/groups/{group_id}",
    "POST /api/v1/orgs/{org}/groups/{group_id}/members",
    "DELETE /api/v1/orgs/{org}/groups/{group_id}/members/{user_id}",
}
PATH_ORG_OWNER_ROUTES = {
    "DELETE /api/v1/orgs/{org}",
    "POST /api/v1/orgs/{org}/transfer-ownership",
}


def test_org_management_routes_take_the_path_org_gates() -> None:
    assert _routes_carrying(require_platform_admin) >= PLATFORM_ADMIN_ROUTES
    assert _routes_carrying(require_org_creator) == ORG_CREATOR_ROUTES
    assert _routes_carrying(get_path_org_admin_user) == PATH_ORG_ADMIN_ROUTES
    assert _routes_carrying(get_path_org_owner_user) == PATH_ORG_OWNER_ROUTES
    # None of them rides the request-org gate, which binds a different organization.
    assert (PATH_ORG_ADMIN_ROUTES | PATH_ORG_OWNER_ROUTES).isdisjoint(
        _routes_carrying(get_owner_user)
    )


def _bearer_client() -> AsyncClient:
    """A cookie-less client, so only the ``Authorization`` header can authenticate."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _mint_key(session_client: AsyncClient, name: str, scope: str, **extra: Any) -> str:
    resp = await session_client.post(
        "/api/v1/me/api-keys", json={"name": name, "scope": scope, **extra}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


async def _create_project(session_client: AsyncClient, slug: str) -> None:
    resp = await session_client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text


@pytest.fixture
async def replayable_scan(client: AsyncClient) -> dict[str, str]:
    """A scan config an OWNER authored through the session-only routes.

    That provenance is the whole argument for the exception: the replay re-runs
    this ``base_query``, it cannot introduce another one.
    """
    await _create_project(client, "replay-proj")
    ds = await client.post(
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
    cfg = await client.post(
        "/api/v1/projects/replay-proj/scans",
        json={
            "data_source_id": ds.json()["id"],
            "name": "Hourly metrics",
            "base_query": "SELECT * FROM events",
            "time_column": "created_at",
            "interval": "1h",
        },
    )
    assert cfg.status_code == 201, cfg.text
    scan_id = cfg.json()["id"]
    return {
        "scan_id": scan_id,
        "url": f"/api/v1/projects/replay-proj/scans/{scan_id}/metrics/replay",
    }


async def test_owner_write_key_can_replay_scan_metrics(
    client: AsyncClient,
    replayable_scan: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The corner this change exists for: before it, this was 403."""
    dispatched: list[tuple[str, str, str, str]] = []

    def fake_delay(scan_config_id: str, scan_job_id: str, time_from: str, time_to: str) -> None:
        dispatched.append((scan_config_id, scan_job_id, time_from, time_to))

    monkeypatch.setattr(metrics.collect_metrics, "delay", fake_delay)
    token = await _mint_key(client, "agent", "write")

    async with _bearer_client() as bearer:
        resp = await bearer.post(
            replayable_scan["url"],
            json=REPLAY_WINDOW,
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    # The window the caller named must reach the worker, not a default one.
    assert dispatched == [
        (
            replayable_scan["scan_id"],
            body["id"],
            "2026-04-01T00:00:00+00:00",
            "2026-04-02T00:00:00+00:00",
        )
    ]


async def test_owner_write_key_still_cannot_delete_a_project(
    client: AsyncClient, replayable_scan: dict[str, str]
) -> None:
    """The same key that just replayed metrics stays out of the destructive routes."""
    token = await _mint_key(client, "agent", "write")
    auth = {"Authorization": f"Bearer {token}"}

    async with _bearer_client() as bearer:
        deleted = await bearer.delete("/api/v1/projects/replay-proj", headers=auth)
        assert deleted.status_code == 403, deleted.text
        assert deleted.json()["detail"] == "Owner session required"

        created_ds = await bearer.post(
            "/api/v1/data-sources",
            json={
                "name": "Attacker DS",
                "db_type": "clickhouse",
                "host": "localhost",
                "port": 8123,
                "database_name": "analytics",
            },
            headers=auth,
        )
        assert created_ds.status_code == 403, created_ds.text
        assert created_ds.json()["detail"] == "Owner session required"

    survived = await client.get("/api/v1/projects/replay-proj")
    assert survived.status_code == 200, survived.text


async def test_editor_write_key_cannot_replay(
    client: AsyncClient, replayable_scan: dict[str, str]
) -> None:
    """Write scope is not the gate — the user behind the key must be an org owner/admin.

    A project ``editor`` who is a plain org member is refused.
    """
    async with _bearer_client() as editor_session:
        registered = await editor_session.post(
            "/api/v1/auth/register",
            json={"email": "editor@example.com", "password": PASSWORD, "name": "Editor"},
        )
        assert registered.status_code == 201, registered.text
        # A member, so the answer is the owner gate's 403 and not the
        # membership gate's 404.
        await add_member_by_slug("replay-proj", "editor@example.com", "editor")
        token = await _mint_key(editor_session, "editor-agent", "write")

    async with _bearer_client() as bearer:
        denied = await bearer.post(
            replayable_scan["url"],
            json=REPLAY_WINDOW,
            headers={"Authorization": f"Bearer {token}"},
        )

    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"] == ORG_ADMIN_REQUIRED


async def test_org_admin_write_key_can_replay(
    client: AsyncClient,
    replayable_scan: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``admin`` passes the owner gates exactly like ``owner`` does."""
    from tripl.tests.test_rbac import _set_role

    monkeypatch.setattr(metrics.collect_metrics, "delay", lambda *args: None)
    async with _bearer_client() as admin_session:
        registered = await admin_session.post(
            "/api/v1/auth/register",
            json={"email": "admin@example.com", "password": PASSWORD, "name": "Admin"},
        )
        assert registered.status_code == 201, registered.text
        await _set_role(client, "admin@example.com", "admin")
        token = await _mint_key(admin_session, "admin-agent", "write")

    async with _bearer_client() as bearer:
        resp = await bearer.post(
            replayable_scan["url"],
            json=REPLAY_WINDOW,
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 201, resp.text


async def test_read_scope_key_cannot_replay(
    client: AsyncClient, replayable_scan: dict[str, str]
) -> None:
    """A replay writes metric values and burns warehouse time — ``read`` is not enough."""
    token = await _mint_key(client, "reader", "read")

    async with _bearer_client() as bearer:
        denied = await bearer.post(
            replayable_scan["url"],
            json=REPLAY_WINDOW,
            headers={"Authorization": f"Bearer {token}"},
        )

    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"] == "API key has read-only scope"


async def test_project_bound_owner_key_cannot_replay_another_project(
    client: AsyncClient, replayable_scan: dict[str, str]
) -> None:
    """A key fenced into one project stays fenced now that it can reach an owner route."""
    await _create_project(client, "other-proj")
    token = await _mint_key(client, "fenced-agent", "write", project_slug="other-proj")

    async with _bearer_client() as bearer:
        denied = await bearer.post(
            replayable_scan["url"],
            json=REPLAY_WINDOW,
            headers={"Authorization": f"Bearer {token}"},
        )

    assert denied.status_code == 404, denied.text
    assert denied.json()["detail"] == "Project not found"
