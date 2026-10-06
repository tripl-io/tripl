"""The Properties panel's "Used in field values" (tripl-4zzc.6).

The suggestions are computed in the browser from what the API already serves:
the event's field values, the project's properties, and the event's property
list. These tests pin the server half the panel leans on — a branch copy keeps
the tokens and the bindings that resolve them under ids of its own, "Add" and
"Add all" are the existing PUT run per property, and that PUT is idempotent for
the same body but DOES overwrite ``required``, which is why the panel offers
nothing until the list has loaded.
"""

import json

import pytest
from httpx import AsyncClient

EVENT = "map:close:point"
VALUE = '{"spot_id":"${property.spot_id}","type":"${type}","details_rank":"${details_rank}"}'
NAMES = ("spot_id", "type", "details_rank")


async def _seed(client: AsyncClient, slug: str) -> str:
    """A project with three properties and one event whose JSON value uses them."""
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text
    et = await client.post(
        f"/api/v1/projects/{slug}/event-types", json={"name": "track", "display_name": "Track"}
    )
    assert et.status_code == 201, et.text
    field = await client.post(
        f"/api/v1/projects/{slug}/event-types/{et.json()['id']}/fields",
        json={"name": "payload", "display_name": "Payload", "field_type": "json"},
    )
    assert field.status_code == 201, field.text
    for body in (
        {"name": "spot_id", "bindings": ["property.spot_id"]},
        {"name": "type"},
        {"name": "details_rank"},
    ):
        variable = await client.post(f"/api/v1/projects/{slug}/variables", json=body)
        assert variable.status_code == 201, variable.text
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": et.json()["id"],
            "name": EVENT,
            "field_values": [{"field_definition_id": field.json()["id"], "value": VALUE}],
        },
    )
    assert event.status_code == 201, event.text
    return event.json()["id"]


def _q(branch: str | None) -> str:
    return f"?branch={branch}" if branch else ""


async def _branch(client: AsyncClient, slug: str) -> str:
    resp = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "WND-2"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _event_on(client: AsyncClient, slug: str, branch: str | None) -> dict:
    items = (await client.get(f"/api/v1/projects/{slug}/events{_q(branch)}")).json()["items"]
    event_id = next(e["id"] for e in items if e["name"] == EVENT)
    resp = await client.get(f"/api/v1/projects/{slug}/events/{event_id}{_q(branch)}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _variables_on(client: AsyncClient, slug: str, branch: str | None) -> dict[str, dict]:
    items = (await client.get(f"/api/v1/projects/{slug}/variables{_q(branch)}")).json()["items"]
    return {v["name"]: v for v in items}


async def _properties(
    client: AsyncClient, slug: str, event_id: str, branch: str | None = None
) -> list[dict]:
    resp = await client.get(f"/api/v1/projects/{slug}/events/{event_id}/properties{_q(branch)}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _add(
    client: AsyncClient,
    slug: str,
    var_id: str,
    event_id: str,
    required: bool,
    branch: str | None = None,
):
    return await client.put(
        f"/api/v1/projects/{slug}/variables/{var_id}/event-overrides/{event_id}{_q(branch)}",
        json={"required": required},
    )


@pytest.mark.asyncio
async def test_a_branch_copy_carries_the_tokens_and_bindings_under_its_own_ids(
    client: AsyncClient,
) -> None:
    slug = "af-refprops-copy"
    await _seed(client, slug)
    branch_id = await _branch(client, slug)

    main_event = await _event_on(client, slug, None)
    branch_event = await _event_on(client, slug, branch_id)
    assert branch_event["id"] != main_event["id"]
    # A JSON value is stored normalised, so compare it as JSON.
    assert [json.loads(fv["value"]) for fv in branch_event["field_values"]] == [json.loads(VALUE)]
    assert await _properties(client, slug, branch_event["id"], branch_id) == []

    main_vars = await _variables_on(client, slug, None)
    branch_vars = await _variables_on(client, slug, branch_id)
    assert branch_vars["spot_id"]["bindings"] == ["property.spot_id"]
    assert {branch_vars[n]["id"] for n in NAMES}.isdisjoint({main_vars[n]["id"] for n in NAMES})


@pytest.mark.asyncio
async def test_adding_all_writes_the_branch_copy_only(client: AsyncClient) -> None:
    slug = "af-refprops-add-all"
    await _seed(client, slug)
    branch_id = await _branch(client, slug)
    main_event = await _event_on(client, slug, None)
    branch_event = await _event_on(client, slug, branch_id)
    branch_vars = await _variables_on(client, slug, branch_id)

    for name in NAMES:
        var_id = branch_vars[name]["id"]
        resp = await _add(client, slug, var_id, branch_event["id"], False, branch_id)
        assert resp.status_code == 200, resp.text

    entries = await _properties(client, slug, branch_event["id"], branch_id)
    assert sorted(e["name"] for e in entries) == sorted(NAMES)
    assert all(e["required"] is False for e in entries)
    assert await _properties(client, slug, main_event["id"]) == []

    # Main's event id is not the branch's: the write is refused, not misfiled.
    type_id = branch_vars["type"]["id"]
    refused = await _add(client, slug, type_id, main_event["id"], False, branch_id)
    assert refused.status_code == 404


@pytest.mark.asyncio
async def test_pressing_add_all_again_finishes_the_rest_without_duplicates(
    client: AsyncClient,
) -> None:
    slug = "af-refprops-retry"
    await _seed(client, slug)
    event = await _event_on(client, slug, None)
    variables = await _variables_on(client, slug, None)

    # A first run that stopped after one property, then the whole run again.
    await _add(client, slug, variables["spot_id"]["id"], event["id"], True)
    for name in NAMES:
        resp = await _add(client, slug, variables[name]["id"], event["id"], True)
        assert resp.status_code == 200, resp.text

    entries = await _properties(client, slug, event["id"])
    assert sorted(e["name"] for e in entries) == sorted(NAMES)
    assert all(e["required"] is True for e in entries)


@pytest.mark.asyncio
async def test_the_put_overwrites_required_so_listed_ones_must_not_be_offered(
    client: AsyncClient,
) -> None:
    """Why the panel waits for the list: offered while it is still loading, a
    listed, required property would be turned optional by an "Add"."""
    slug = "af-refprops-overwrite"
    await _seed(client, slug)
    event = await _event_on(client, slug, None)
    spot = (await _variables_on(client, slug, None))["spot_id"]["id"]

    await _add(client, slug, spot, event["id"], True)
    await _add(client, slug, spot, event["id"], False)
    [entry] = await _properties(client, slug, event["id"])
    assert entry["required"] is False
