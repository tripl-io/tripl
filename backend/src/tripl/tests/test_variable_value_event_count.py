"""``value_event_count``: the events whose own values name a property's token.

A property can be in use only because a field or meta value templates
``${token}`` with it, with no scan context and no property-list entry. The
list reports that use apart from ``listed_event_count`` and ``event_count``.
"""

import pytest
from httpx import AsyncClient

SLUG = "var-value-events"


async def _setup(client: AsyncClient) -> tuple[str, str, str]:
    resp = await client.post("/api/v1/projects", json={"name": "V", "slug": SLUG})
    assert resp.status_code == 201, resp.text
    et = await client.post(
        f"/api/v1/projects/{SLUG}/event-types", json={"name": "track", "display_name": "Track"}
    )
    assert et.status_code == 201, et.text
    field = await client.post(
        f"/api/v1/projects/{SLUG}/event-types/{et.json()['id']}/fields",
        json={"name": "screen", "display_name": "Screen", "field_type": "string"},
    )
    assert field.status_code == 201, field.text
    meta = await client.post(
        f"/api/v1/projects/{SLUG}/meta-fields",
        json={"name": "jira", "display_name": "Jira", "field_type": "url"},
    )
    assert meta.status_code == 201, meta.text
    return et.json()["id"], field.json()["id"], meta.json()["id"]


async def _event(
    client: AsyncClient,
    et_id: str,
    name: str,
    *,
    field: tuple[str, str] | None = None,
    meta: tuple[str, str] | None = None,
) -> None:
    body: dict = {"event_type_id": et_id, "name": name}
    if field is not None:
        body["field_values"] = [{"field_definition_id": field[0], "value": field[1]}]
    if meta is not None:
        body["meta_values"] = [{"meta_field_definition_id": meta[0], "value": meta[1]}]
    resp = await client.post(f"/api/v1/projects/{SLUG}/events", json=body)
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_list_counts_events_whose_values_name_the_property(client: AsyncClient) -> None:
    et_id, field_id, meta_id = await _setup(client)
    for body in (
        {"name": "variant", "bindings": ["payload.variant"]},
        {"name": "plan"},
        {"name": "idle"},
    ):
        created = await client.post(f"/api/v1/projects/{SLUG}/properties", json=body)
        assert created.status_code == 201, created.text

    # A field value naming the property.
    await _event(client, et_id, "Event A", field=(field_id, "${variant}"))
    # A binding is one of the property's tokens too; the meta value counts.
    await _event(
        client, et_id, "Event B", field=(field_id, "${payload.variant}"), meta=(meta_id, "${plan}")
    )
    # Named in both tables: still one event.
    await _event(
        client, et_id, "Event C", field=(field_id, "x-${variant}"), meta=(meta_id, "${variant}")
    )
    # Literal values and an unclosed ``${`` name nothing.
    await _event(client, et_id, "Event D", field=(field_id, "home"), meta=(meta_id, "literal ${"))

    listing = await client.get(f"/api/v1/projects/{SLUG}/properties")
    assert listing.status_code == 200, listing.text
    counts = {item["name"]: item["value_event_count"] for item in listing.json()["items"]}
    assert counts == {"variant": 3, "plan": 1, "idle": 0}
    # Neither listed on an event nor seen by a scan: this count is the only use.
    for item in listing.json()["items"]:
        assert item["listed_event_count"] == 0
        assert item["event_count"] == 0
