"""Shared setup for the docs catalog tests (F22, GH #299). Not a test module."""

from typing import Any

from httpx import AsyncClient

PASSWORD = "Password123!"


async def create_project(client: AsyncClient, slug: str) -> dict[str, Any]:
    resp = await client.post("/api/v1/projects", json={"name": slug.title(), "slug": slug})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def put_doc(
    client: AsyncClient,
    slug: str,
    path: str,
    content: str,
    *,
    scope: str = "project",
    expect: int = 200,
    **extra: Any,
) -> dict[str, Any]:
    resp = await client.put(
        f"/api/v1/projects/{slug}/docs/file",
        params={"scope": scope, "path": path},
        json={"content": content, **extra},
    )
    assert resp.status_code == expect, resp.text
    return resp.json()


async def get_doc(
    client: AsyncClient, slug: str, path: str, *, scope: str = "project", expect: int = 200
) -> dict[str, Any]:
    resp = await client.get(
        f"/api/v1/projects/{slug}/docs/file", params={"scope": scope, "path": path}
    )
    assert resp.status_code == expect, resp.text
    return resp.json()


async def seed_plan(client: AsyncClient, slug: str) -> dict[str, str]:
    """An event type ``checkout`` with a field ``amount`` and one event ``purchase``."""
    event_type = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "checkout", "display_name": "Checkout"},
    )
    assert event_type.status_code == 201, event_type.text
    event_type_id = event_type.json()["id"]
    field = await client.post(
        f"/api/v1/projects/{slug}/event-types/{event_type_id}/fields",
        json={"name": "amount", "display_name": "Amount", "field_type": "string"},
    )
    assert field.status_code == 201, field.text
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": event_type_id, "name": "purchase", "field_values": []},
    )
    assert event.status_code == 201, event.text
    return {
        "event_type_id": event_type_id,
        "field_id": field.json()["id"],
        "event_id": event.json()["id"],
    }


async def register(client: AsyncClient, email: str, name: str) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD, "name": name}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
