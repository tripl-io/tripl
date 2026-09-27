"""``/api/v1/orgs/{org}/<rest>`` rewrite (F20 PR2).

Only the allow-listed prefixes are served by the legacy routes; everything else
under ``/api/v1/orgs`` is left for real routes (none yet) and gets the router's
own 404. The org is checked after authentication, so an anonymous caller cannot
tell a real org from an unknown one.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from tripl.middleware.org_context import (
    ORG_ORIGINAL_PATH_STATE_KEY,
    ORG_REWRITE_PREFIXES,
    ORG_SCOPE_STATE_KEY,
    OrgRef,
    bound_org,
    current_org,
)
from tripl.middleware.org_path_rewrite import OrgPathRewriteMiddleware, rewrite_org_path
from tripl.models.organization import DEFAULT_ORG_ID, DEFAULT_ORG_SLUG


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/v1/orgs/default/projects", ("default", "/api/v1/projects")),
        ("/api/v1/orgs/default/projects/p/events", ("default", "/api/v1/projects/p/events")),
        ("/api/v1/orgs/acme/activity", ("acme", "/api/v1/activity")),
        ("/api/v1/orgs/acme/activity/projects/p", ("acme", "/api/v1/activity/projects/p")),
        ("/api/v1/orgs/acme/audit", ("acme", "/api/v1/audit")),
        ("/api/v1/orgs/acme/data-sources/x", ("acme", "/api/v1/data-sources/x")),
        ("/api/v1/orgs/acme/users", ("acme", "/api/v1/users")),
        ("/api/v1/orgs/acme/me/notifications", ("acme", "/api/v1/me/notifications")),
        ("/api/v1/orgs/acme/projects/", ("acme", "/api/v1/projects/")),
    ],
)
def test_allow_listed_prefixes_are_rewritten(path: str, expected: tuple[str, str]) -> None:
    assert rewrite_org_path(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/orgs",
        "/api/v1/orgs/",
        "/api/v1/orgs/default",
        "/api/v1/orgs/default/",
        "/api/v1/orgs/default/settings",
        "/api/v1/orgs/default/settings/runtime",
        "/api/v1/orgs/default/project-templates",
        "/api/v1/orgs/default/auth/me",
        "/api/v1/orgs/default/orgs/default/projects",
        "/api/v1/orgs/default/projectsx",
        "/api/v1/orgs/default/meh",
        "/api/v1/projects",
        "/api/v1/settings",
    ],
)
def test_other_paths_are_not_rewritten(path: str) -> None:
    assert rewrite_org_path(path) is None


def test_the_allow_list_excludes_settings_and_identity() -> None:
    for excluded in ("settings", "project-templates", "auth", "orgs"):
        assert excluded not in ORG_REWRITE_PREFIXES


async def _run(scope: dict[str, Any]) -> tuple[dict[str, Any], object]:
    seen: dict[str, Any] = {}

    async def app(inner: dict[str, Any], _receive: Any, _send: Any) -> None:
        seen.update(inner)
        seen["__org__"] = current_org()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    async def send(_message: dict[str, Any]) -> None:
        return None

    await OrgPathRewriteMiddleware(app)(scope, receive, send)
    return seen, seen["__org__"]


async def test_middleware_rewrites_scope_and_records_the_org() -> None:
    scope = {
        "type": "http",
        "path": "/api/v1/orgs/acme/projects/p/events/stream",
        "raw_path": b"/api/v1/orgs/acme/projects/p/events/stream",
        "root_path": "",
    }
    seen, _ = await _run(scope)
    assert seen["path"] == "/api/v1/projects/p/events/stream"
    assert seen["raw_path"] == b"/api/v1/projects/p/events/stream"
    assert seen["state"][ORG_SCOPE_STATE_KEY] == "acme"
    assert seen["state"][ORG_ORIGINAL_PATH_STATE_KEY] == scope["path"]
    # The caller's scope is not mutated.
    assert scope["path"] == "/api/v1/orgs/acme/projects/p/events/stream"


async def test_middleware_passes_other_paths_through() -> None:
    scope = {
        "type": "http",
        "path": "/api/v1/orgs/acme/settings",
        "raw_path": b"/api/v1/orgs/acme/settings",
        "root_path": "",
    }
    seen, _ = await _run(scope)
    assert seen["path"] == "/api/v1/orgs/acme/settings"
    assert ORG_SCOPE_STATE_KEY not in (seen.get("state") or {})


async def test_middleware_fences_the_org_contextvar() -> None:
    outer = OrgRef(id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG)
    with bound_org(outer):
        _, inside = await _run(
            {"type": "http", "path": "/api/v1/projects", "raw_path": b"/api/v1/projects"}
        )
        assert inside is None
        assert current_org() == outer


async def test_unknown_org_is_404_for_an_authenticated_user(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/orgs/no-such-org/projects")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Organization not found"


async def test_unknown_org_is_401_for_an_anonymous_caller(anon_client: AsyncClient) -> None:
    unknown = await anon_client.get("/api/v1/orgs/no-such-org/projects")
    known = await anon_client.get("/api/v1/orgs/default/projects")
    assert unknown.status_code == known.status_code == 401
    assert unknown.json() == known.json()


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/orgs/default/settings",
        "/api/v1/orgs/default/project-templates",
        "/api/v1/orgs/default/auth/me",
        "/api/v1/orgs",
        "/api/v1/orgs/default",
    ],
)
async def test_non_allow_listed_org_paths_reach_no_route(client: AsyncClient, path: str) -> None:
    resp = await client.get(path)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not Found"
