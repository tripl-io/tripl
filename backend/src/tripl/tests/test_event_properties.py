"""An event's property list (F23.3, #306).

A ``variable_event_value_overrides`` row is the entry: a ``required`` flag and
an optional per-event override of the allowed values (NULL: the global list).
"""

import uuid

import pytest
from httpx import AsyncClient

from tripl.models.variable import Variable
from tripl.models.variable_value_drift import VariableValueDrift
from tripl.services.plan_revision_service import with_snapshot_defaults
from tripl.tests.conftest import TestSessionLocal

EVENT = "purchase:success"


async def _seed(client: AsyncClient, slug: str) -> tuple[str, str]:
    """A project with one event and a variable documenting ["USD", "EUR"]."""
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text
    et = await client.post(
        f"/api/v1/projects/{slug}/event-types", json={"name": "track", "display_name": "Track"}
    )
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et.json()["id"], "name": EVENT},
    )
    assert event.status_code == 201, event.text
    variable = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={
            "name": "currency",
            "allowed_values": ["USD", "EUR"],
            "json_schema": {"type": "string", "pattern": "^[A-Z]{3}$"},
        },
    )
    assert variable.status_code == 201, variable.text
    return variable.json()["id"], event.json()["id"]


def _q(branch: str | None) -> str:
    return f"?branch={branch}" if branch else ""


async def _put(
    client: AsyncClient,
    slug: str,
    var_id: str,
    event_id: str,
    body: dict,
    branch: str | None = None,
):
    return await client.put(
        f"/api/v1/projects/{slug}/variables/{var_id}/event-overrides/{event_id}{_q(branch)}",
        json=body,
    )


async def _properties(
    client: AsyncClient, slug: str, event_id: str, branch: str | None = None
) -> list[dict]:
    resp = await client.get(f"/api/v1/projects/{slug}/events/{event_id}/properties{_q(branch)}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _event_id(client: AsyncClient, slug: str, branch: str | None = None) -> str:
    items = (await client.get(f"/api/v1/projects/{slug}/events{_q(branch)}")).json()["items"]
    return next(e["id"] for e in items if e["name"] == EVENT)


async def _var_id(client: AsyncClient, slug: str, branch: str | None = None) -> str:
    items = (await client.get(f"/api/v1/projects/{slug}/variables{_q(branch)}")).json()["items"]
    return next(v["id"] for v in items if v["name"] == "currency")


@pytest.mark.asyncio
async def test_listing_a_property_without_an_override_keeps_the_global_values(
    client: AsyncClient,
) -> None:
    slug = "props-list"
    var_id, event_id = await _seed(client, slug)
    assert await _properties(client, slug, event_id) == []

    resp = await _put(client, slug, var_id, event_id, {"required": True})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["required"], resp.json()["values"]) == (True, None)

    [entry] = await _properties(client, slug, event_id)
    assert entry["name"] == "currency"
    assert entry["variable_type"] == "string"
    assert entry["json_schema"] == {"type": "string", "pattern": "^[A-Z]{3}$"}
    assert entry["required"] is True
    assert entry["values"] is None
    assert entry["effective_values"] == ["USD", "EUR"]


@pytest.mark.asyncio
async def test_an_upsert_is_a_patch_of_the_entry(client: AsyncClient) -> None:
    slug = "props-patch"
    var_id, event_id = await _seed(client, slug)
    await _put(client, slug, var_id, event_id, {"required": True})

    # The pre-F23 call shape: values only. The requiredness survives it.
    resp = await _put(client, slug, var_id, event_id, {"values": ["USD"]})
    assert resp.status_code == 200, resp.text
    [entry] = await _properties(client, slug, event_id)
    assert (entry["required"], entry["values"], entry["effective_values"]) == (
        True,
        ["USD"],
        ["USD"],
    )

    # values: null drops the override and keeps the property.
    await _put(client, slug, var_id, event_id, {"values": None})
    [entry] = await _properties(client, slug, event_id)
    assert (entry["required"], entry["values"], entry["effective_values"]) == (
        True,
        None,
        ["USD", "EUR"],
    )

    refused = await _put(client, slug, var_id, event_id, {"required": None})
    assert refused.status_code == 422

    deleted = await client.delete(
        f"/api/v1/projects/{slug}/variables/{var_id}/event-overrides/{event_id}"
    )
    assert deleted.status_code == 204
    assert await _properties(client, slug, event_id) == []


@pytest.mark.asyncio
async def test_the_event_must_be_on_the_branch(client: AsyncClient) -> None:
    slug = "props-404"
    await _seed(client, slug)
    resp = await client.get(f"/api/v1/projects/{slug}/events/{uuid.uuid4()}/properties")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_accepting_drift_for_the_event_seeds_an_entry_without_override(
    client: AsyncClient,
) -> None:
    """An entry with no override starts from the global list, like a new row —
    not from an empty list, which would drop USD and EUR."""
    slug = "props-drift"
    var_id, event_id = await _seed(client, slug)
    await _put(client, slug, var_id, event_id, {"required": True})
    async with TestSessionLocal() as session, session.begin():
        variable = await session.get(Variable, uuid.UUID(var_id))
        assert variable is not None
        drift = VariableValueDrift(
            project_id=variable.project_id,
            variable_id=variable.id,
            event_id=uuid.UUID(event_id),
            observed_values=["GBP"],
        )
        session.add(drift)
    resp = await client.post(
        f"/api/v1/projects/{slug}/variables/drifts/{drift.id}/action",
        json={"action": "accept", "scope": "event"},
    )
    assert resp.status_code == 200, resp.text
    [entry] = await _properties(client, slug, event_id)
    assert (entry["required"], entry["values"]) == (True, ["USD", "EUR", "GBP"])


async def _branch(client: AsyncClient, slug: str) -> str:
    resp = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_a_branch_copies_the_list_and_a_merge_brings_it_back(client: AsyncClient) -> None:
    slug = "props-branch"
    var_id, event_id = await _seed(client, slug)
    await _put(client, slug, var_id, event_id, {"required": True})
    branch_id = await _branch(client, slug)

    b_event, b_var = (
        await _event_id(client, slug, branch_id),
        await _var_id(client, slug, branch_id),
    )
    [copied] = await _properties(client, slug, b_event, branch_id)
    assert (copied["required"], copied["values"]) == (True, None)

    await _put(client, slug, b_var, b_event, {"required": False, "values": ["EUR"]}, branch_id)
    diff = (await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")).json()
    assert "required" in str(diff)
    [on_main] = await _properties(client, slug, event_id)
    assert (on_main["required"], on_main["values"]) == (True, None)

    for action in ("submit", "approve"):
        await client.post(
            f"/api/v1/projects/{slug}/branches/{branch_id}/transition", json={"action": action}
        )
    merged = await client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge")
    assert merged.status_code == 200, merged.text
    [on_main] = await _properties(client, slug, event_id)
    assert (on_main["required"], on_main["values"]) == (False, ["EUR"])


@pytest.mark.asyncio
async def test_revert_restores_an_entry_without_override(client: AsyncClient) -> None:
    slug = "props-revert"
    var_id, event_id = await _seed(client, slug)
    await _put(client, slug, var_id, event_id, {"required": True})
    branch_id = await _branch(client, slug)
    b_event, b_var = (
        await _event_id(client, slug, branch_id),
        await _var_id(client, slug, branch_id),
    )
    await _put(client, slug, b_var, b_event, {"required": False, "values": ["EUR"]}, branch_id)

    resp = await client.post(
        f"/api/v1/projects/{slug}/branches/{branch_id}/revert",
        json={"entity_type": "variable", "name": "currency", "field": "event_value_overrides"},
    )
    assert resp.status_code == 200, resp.text
    [entry] = await _properties(client, slug, b_event, branch_id)
    assert (entry["required"], entry["values"]) == (True, None)


@pytest.mark.asyncio
async def test_update_from_main_brings_mains_entry(client: AsyncClient) -> None:
    slug = "props-ufm"
    var_id, event_id = await _seed(client, slug)
    branch_id = await _branch(client, slug)
    await _put(client, slug, var_id, event_id, {"required": True})

    updated = await client.post(
        f"/api/v1/projects/{slug}/branches/{branch_id}/update-from-main", json={}
    )
    assert updated.status_code == 200, updated.text
    b_event = await _event_id(client, slug, branch_id)
    [entry] = await _properties(client, slug, b_event, branch_id)
    assert (entry["required"], entry["values"]) == (True, None)


def test_a_stored_revision_from_before_f23_reads_as_unchanged() -> None:
    """A base serialised before F23 has no ``json_schema`` and no ``required``.
    Read raw, every variable with an override would diff as changed and
    conflict as soon as main touched it; upgraded on read, it compares equal."""
    old = {
        "variables": [
            {
                "name": "currency",
                "event_value_overrides": [
                    {"event_type_name": "track", "event_name": EVENT, "values": ["USD"]}
                ],
            }
        ]
    }
    upgraded = with_snapshot_defaults(old)
    [variable] = upgraded["variables"]
    assert variable["json_schema"] is None
    assert variable["event_value_overrides"][0]["required"] is False
    assert "required" not in old["variables"][0]["event_value_overrides"][0]
    assert with_snapshot_defaults(upgraded) is upgraded


@pytest.mark.asyncio
async def test_a_branch_copies_the_measured_presence(client: AsyncClient) -> None:
    from tripl.models.variable_value import VariableValue

    slug = "props-presence-branch"
    var_id, event_id = await _seed(client, slug)
    await _put(client, slug, var_id, event_id, {})
    event = (await client.get(f"/api/v1/projects/{slug}/events/{event_id}")).json()
    field = await client.post(
        f"/api/v1/projects/{slug}/event-types/{event['event_type_id']}/fields",
        json={"name": "payload", "display_name": "Payload", "field_type": "json"},
    )
    assert field.status_code == 201, field.text
    async with TestSessionLocal() as session, session.begin():
        variable = await session.get(Variable, uuid.UUID(var_id))
        assert variable is not None
        session.add(
            VariableValue(
                project_id=variable.project_id,
                branch_id=variable.branch_id,
                variable_id=variable.id,
                event_id=uuid.UUID(event_id),
                field_definition_id=uuid.UUID(field.json()["id"]),
                source_column="payload.currency",
                value_kind="high",
                observed_count=1,
                values=["USD"],
                presence_rate=0.9,
            )
        )
    [on_main] = await _properties(client, slug, event_id)
    assert on_main["presence_rate"] == 0.9

    branch_id = await _branch(client, slug)
    b_event = await _event_id(client, slug, branch_id)
    [copied] = await _properties(client, slug, b_event, branch_id)
    assert copied["presence_rate"] == 0.9
