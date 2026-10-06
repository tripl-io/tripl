"""Duplicate an existing event (tripl-4zzc.7).

Duplicate is built in the browser: it reads the source with the ordinary
single-event GET and posts an ordinary create. No endpoint was added, so these
tests pin the server half it relies on — the create takes every field the copy
carries (the presence threshold included), a copied value is authored on the
new event, status is the caller's, the source is left untouched, and a copy
made on a branch stays on that branch.
"""

import pytest
from httpx import AsyncClient


async def _seed(client: AsyncClient, slug: str) -> dict:
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text
    et = await client.post(
        f"/api/v1/projects/{slug}/event-types", json={"name": "track", "display_name": "Track"}
    )
    assert et.status_code == 201, et.text
    field = await client.post(
        f"/api/v1/projects/{slug}/event-types/{et.json()['id']}/fields",
        json={"name": "screen", "display_name": "Screen", "field_type": "string"},
    )
    assert field.status_code == 201, field.text
    source = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": et.json()["id"],
            "name": "checkout_started",
            "title": "Checkout started",
            "description": "Fires when checkout opens",
            "status": "live",
            "tags": ["checkout"],
            "metric_breakdown_columns": ["screen"],
            "required_presence_threshold": 0.9,
            "field_values": [{"field_definition_id": field.json()["id"], "value": "${screen}"}],
        },
    )
    assert source.status_code == 201, source.text
    got = await client.get(f"/api/v1/projects/{slug}/events/{source.json()['id']}")
    assert got.status_code == 200, got.text
    return got.json()


def _copy_of(source: dict, **changes: object) -> dict:
    """The create body the form sends for a duplicate of ``source``."""
    body: dict[str, object] = {
        "event_type_id": source["event_type_id"],
        "name": source["name"],
        "title": source["title"],
        "description": source["description"],
        "status": "draft",
        "owner_id": source["owner_id"],
        "metric_breakdown_columns": source["metric_breakdown_columns"],
        "tags": [t["name"] for t in source["tags"]],
        "required_presence_threshold": source["required_presence_threshold"],
        "field_values": [
            {"field_definition_id": fv["field_definition_id"], "value": fv["value"]}
            for fv in source["field_values"]
        ],
        "meta_values": [
            {"meta_field_definition_id": mv["meta_field_definition_id"], "value": mv["value"]}
            for mv in source["meta_values"]
        ],
    }
    body.update(changes)
    return body


@pytest.mark.asyncio
async def test_the_create_takes_everything_a_duplicate_copies(client: AsyncClient) -> None:
    slug = "af-duplicate-copy"
    source = await _seed(client, slug)

    resp = await client.post(
        f"/api/v1/projects/{slug}/events", json=_copy_of(source, name="checkout_completed")
    )
    assert resp.status_code == 201, resp.text
    copy = (await client.get(f"/api/v1/projects/{slug}/events/{resp.json()['id']}")).json()

    assert copy["id"] != source["id"]
    assert copy["status"] == "draft"
    assert copy["title"] == "Checkout started"
    assert copy["description"] == "Fires when checkout opens"
    assert [t["name"] for t in copy["tags"]] == ["checkout"]
    assert copy["metric_breakdown_columns"] == ["screen"]
    assert copy["required_presence_threshold"] == pytest.approx(0.9)
    assert copy["sunset_at"] is None
    assert copy.get("superseded_by_event_id") is None
    # The token is stored as written, and the value is authored on the copy.
    assert [(fv["value"], fv["is_authored"]) for fv in copy["field_values"]] == [
        ("${screen}", True)
    ]
    # The source is untouched.
    after = (await client.get(f"/api/v1/projects/{slug}/events/{source['id']}")).json()
    assert after["status"] == "live"
    assert after["name"] == "checkout_started"


@pytest.mark.asyncio
async def test_a_copy_without_a_threshold_keeps_the_default(client: AsyncClient) -> None:
    slug = "af-duplicate-threshold"
    source = await _seed(client, slug)
    body = _copy_of(source, name="checkout_completed")
    # The form leaves the key out when the source has the default.
    del body["required_presence_threshold"]

    resp = await client.post(f"/api/v1/projects/{slug}/events", json=body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["required_presence_threshold"] is None


@pytest.mark.asyncio
async def test_a_duplicate_lands_on_the_branch_it_was_made_on(client: AsyncClient) -> None:
    slug = "af-duplicate-branch"
    await _seed(client, slug)
    branch = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "WND-7"})
    assert branch.status_code == 201, branch.text
    branch_id = branch.json()["id"]
    items = (await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")).json()["items"]
    branch_source_id = next(e["id"] for e in items if e["name"] == "checkout_started")
    branch_source = (
        await client.get(f"/api/v1/projects/{slug}/events/{branch_source_id}?branch={branch_id}")
    ).json()

    resp = await client.post(
        f"/api/v1/projects/{slug}/events?branch={branch_id}",
        json=_copy_of(branch_source, name="checkout_completed"),
    )
    assert resp.status_code == 201, resp.text

    on_main = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"]
    on_branch = (await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")).json()[
        "items"
    ]
    assert "checkout_completed" not in {e["name"] for e in on_main}
    assert "checkout_completed" in {e["name"] for e in on_branch}
