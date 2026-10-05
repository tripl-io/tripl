"""A sample of routes answers identically under ``/api/v1/orgs/default`` (F20 PR2).

The rewrite is transparent: the same handler, the same response. Reads are
compared byte for byte; writes, which create or touch rows, are compared with
ids and timestamps masked and the one varying input mapped back.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

LEGACY = "/api/v1"
ORG = "/api/v1/orgs/default"
SLUG = "orgpfx"

_VOLATILE_KEYS = frozenset({"id", "created_at", "updated_at", "project_id", "event_type_id"})
# Stamped per computation, not per row: the only thing a read may differ on.
_READ_VOLATILE_KEYS = frozenset({"computed_at", "generated_at"})


async def _seed(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/projects", json={"name": "Org prefix", "slug": SLUG})
    assert resp.status_code == 201, resp.text
    et = await client.post(
        f"/api/v1/projects/{SLUG}/event-types",
        json={"name": "seeded", "display_name": "Seeded"},
    )
    assert et.status_code == 201, et.text


def _mask(value: Any, swap: dict[str, str], keys: frozenset[str] = _VOLATILE_KEYS) -> Any:
    if isinstance(value, dict):
        return {k: ("<volatile>" if k in keys else _mask(v, swap, keys)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v, swap, keys) for v in value]
    if isinstance(value, str):
        for src, dst in swap.items():
            value = value.replace(src, dst)
        return value
    return value


@pytest.mark.parametrize(
    "path",
    [
        "/projects",
        f"/projects/{SLUG}",
        f"/projects/{SLUG}/event-types",
        f"/projects/{SLUG}/events",
        f"/projects/{SLUG}/variables",
        f"/projects/{SLUG}/meta-fields",
        f"/projects/{SLUG}/health",
        f"/projects/{SLUG}/tracker-config",
        f"/activity/projects/{SLUG}",
        "/activity",
        f"/projects/{SLUG}/audit",
        "/data-sources",
        "/users",
        "/me/notifications/unread-count",
        "/me/api-keys",
    ],
)
async def test_reads_are_identical(client: AsyncClient, path: str) -> None:
    await _seed(client)
    legacy = await client.get(LEGACY + path)
    org = await client.get(ORG + path)
    assert legacy.status_code == 200, legacy.text
    assert org.status_code == legacy.status_code
    assert _mask(org.json(), {}, _READ_VOLATILE_KEYS) == _mask(
        legacy.json(), {}, _READ_VOLATILE_KEYS
    )


@pytest.mark.parametrize(
    ("method", "path", "legacy_body", "org_body", "swap"),
    [
        (
            "PATCH",
            f"/projects/{SLUG}",
            {"description": "same description"},
            {"description": "same description"},
            {},
        ),
        (
            "POST",
            f"/projects/{SLUG}/event-types",
            {"name": "legacy_et", "display_name": "Legacy ET"},
            {"name": "org_et", "display_name": "Org ET"},
            {"org_et": "legacy_et", "Org ET": "Legacy ET"},
        ),
        (
            "POST",
            f"/projects/{SLUG}/variables",
            {"name": "legacy_var"},
            {"name": "org_var"},
            {"org_var": "legacy_var"},
        ),
        (
            "PATCH",
            f"/projects/{SLUG}/tracker-config",
            {"enabled": False, "project_key": "ABC"},
            {"enabled": False, "project_key": "ABC"},
            {},
        ),
    ],
)
async def test_writes_are_identical(
    client: AsyncClient,
    method: str,
    path: str,
    legacy_body: dict[str, Any],
    org_body: dict[str, Any],
    swap: dict[str, str],
) -> None:
    await _seed(client)
    legacy = await client.request(method, LEGACY + path, json=legacy_body)
    org = await client.request(method, ORG + path, json=org_body)
    assert legacy.status_code in (200, 201), legacy.text
    assert org.status_code == legacy.status_code, org.text
    assert _mask(org.json(), swap) == _mask(legacy.json(), {})


async def test_a_write_through_the_org_path_is_visible_on_the_legacy_path(
    client: AsyncClient,
) -> None:
    await _seed(client)
    created = await client.post(
        f"{ORG}/projects/{SLUG}/event-types",
        json={"name": "via_org", "display_name": "Via org"},
    )
    assert created.status_code == 201, created.text
    names = {
        et["name"] for et in (await client.get(f"{LEGACY}/projects/{SLUG}/event-types")).json()
    }
    assert "via_org" in names


async def test_project_scoped_key_through_the_org_path(client: AsyncClient) -> None:
    await _seed(client)
    created = await client.post(
        "/api/v1/me/api-keys",
        json={"name": "fenced", "scope": "read", "project_slug": SLUG},
    )
    assert created.status_code == 201, created.text
    bearer = {"Authorization": f"Bearer {created.json()['token']}"}

    legacy = await client.get(f"{LEGACY}/projects/{SLUG}/event-types", headers=bearer)
    org = await client.get(f"{ORG}/projects/{SLUG}/event-types", headers=bearer)
    assert legacy.status_code == org.status_code == 200
    assert org.json() == legacy.json()
    # The key is still fenced off instance-wide routes under either spelling.
    for prefix in (LEGACY, ORG):
        resp = await client.get(f"{prefix}/projects", headers=bearer)
        assert resp.status_code == 403
