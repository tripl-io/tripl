"""One property across its events: the catalog listing, the events-list filter
and the bulk edit (F23.8, #306)."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.audit_log import AuditLog
from tripl.models.variable import Variable
from tripl.models.variable_value import VariableValue
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal

PASSWORD = "Password123!"
EVENTS = ("checkout:start", "checkout:success", "home:view")


async def _seed(client: AsyncClient, slug: str) -> tuple[str, dict[str, str]]:
    """A project with three events and a property documenting ["USD", "EUR"]."""
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text
    et = await client.post(
        f"/api/v1/projects/{slug}/event-types", json={"name": "track", "display_name": "Track"}
    )
    events: dict[str, str] = {}
    for name in EVENTS:
        event = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={"event_type_id": et.json()["id"], "name": name},
        )
        assert event.status_code == 201, event.text
        events[name] = event.json()["id"]
    variable = await client.post(
        f"/api/v1/projects/{slug}/properties",
        json={"name": "currency", "allowed_values": ["USD", "EUR"]},
    )
    assert variable.status_code == 201, variable.text
    return variable.json()["id"], events


def _q(branch: str | None) -> str:
    return f"?branch={branch}" if branch else ""


async def _bulk(client: AsyncClient, slug: str, var_id: str, body: dict, branch: str | None = None):
    return await client.post(
        f"/api/v1/projects/{slug}/properties/{var_id}/event-overrides/bulk{_q(branch)}",
        json=body,
    )


async def _events_of(
    client: AsyncClient, slug: str, var_id: str, branch: str | None = None
) -> list[dict]:
    resp = await client.get(f"/api/v1/projects/{slug}/properties/{var_id}/events{_q(branch)}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _filtered(client: AsyncClient, slug: str, ref: str, branch: str | None = None) -> set:
    sep = "&" if branch else "?"
    resp = await client.get(f"/api/v1/projects/{slug}/events{_q(branch)}{sep}property={ref}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == len(body["items"])
    return {item["name"] for item in body["items"]}


async def _audit(action: str) -> list[AuditLog]:
    async with TestSessionLocal() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.action == action))
        return list(result.scalars().all())


@pytest.mark.asyncio
async def test_bulk_adds_the_property_and_patches_existing_entries(client: AsyncClient) -> None:
    slug = "prop-bulk"
    var_id, events = await _seed(client, slug)
    start, success = events["checkout:start"], events["checkout:success"]
    one = await client.put(
        f"/api/v1/projects/{slug}/properties/{var_id}/event-overrides/{start}",
        json={"values": ["USD"]},
    )
    assert one.status_code == 200, one.text

    resp = await _bulk(
        client, slug, var_id, {"event_ids": [start, success, start], "required": True}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"created": 1, "updated": 1, "removed": 0}

    rows = await _events_of(client, slug, var_id)
    assert [(r["event_name"], r["required"], r["values"], r["effective_values"]) for r in rows] == [
        ("checkout:start", True, ["USD"], ["USD"]),
        ("checkout:success", True, None, ["USD", "EUR"]),
    ]

    # values: null drops every override and keeps the entries and their flag.
    resp = await _bulk(client, slug, var_id, {"event_ids": [start, success], "values": None})
    assert resp.status_code == 200, resp.text
    rows = await _events_of(client, slug, var_id)
    assert [(r["required"], r["values"]) for r in rows] == [(True, None), (True, None)]

    rows_audit = await _audit("variable.override_bulk_set")
    assert len(rows_audit) == 2
    first = next(r for r in rows_audit if r.payload.get("required") is True)
    assert first.target_name == "currency"
    assert first.payload["count"] == 2
    assert first.payload["created"] == 1
    assert sorted(first.payload["event_names"]) == ["checkout:start", "checkout:success"]


@pytest.mark.asyncio
async def test_bulk_is_all_or_nothing_and_refuses_null_required(client: AsyncClient) -> None:
    slug = "prop-bulk-404"
    var_id, events = await _seed(client, slug)
    resp = await _bulk(
        client,
        slug,
        var_id,
        {"event_ids": [events["home:view"], str(uuid.uuid4())], "required": True},
    )
    assert resp.status_code == 404, resp.text
    assert await _events_of(client, slug, var_id) == []

    refused = await _bulk(
        client, slug, var_id, {"event_ids": [events["home:view"]], "required": None}
    )
    assert refused.status_code == 422
    empty = await _bulk(client, slug, var_id, {"event_ids": []})
    assert empty.status_code == 422
    missing = await _bulk(client, slug, str(uuid.uuid4()), {"event_ids": [events["home:view"]]})
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_bulk_delete_takes_the_property_off_and_names_only_what_it_removed(
    client: AsyncClient,
) -> None:
    slug = "prop-bulk-del"
    var_id, events = await _seed(client, slug)
    await _bulk(client, slug, var_id, {"event_ids": [events["checkout:start"]]})
    resp = await client.post(
        f"/api/v1/projects/{slug}/properties/{var_id}/event-overrides/bulk-delete",
        json={"event_ids": [events["checkout:start"], events["home:view"]]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["removed"] == 1
    assert await _events_of(client, slug, var_id) == []
    [row] = await _audit("variable.override_bulk_delete")
    assert row.payload["event_names"] == ["checkout:start"]
    assert row.payload["count"] == 1


@pytest.mark.asyncio
async def test_listing_reports_presence_against_each_events_threshold(
    client: AsyncClient,
) -> None:
    slug = "prop-presence"
    var_id, events = await _seed(client, slug)
    start, success = events["checkout:start"], events["checkout:success"]
    await _bulk(client, slug, var_id, {"event_ids": [start, success]})
    patched = await client.patch(
        f"/api/v1/projects/{slug}/events/{success}", json={"required_presence_threshold": 0.5}
    )
    assert patched.status_code == 200, patched.text
    [et] = (await client.get(f"/api/v1/projects/{slug}/event-types")).json()
    field = await client.post(
        f"/api/v1/projects/{slug}/event-types/{et['id']}/fields",
        json={"name": "props", "display_name": "Props", "field_type": "json"},
    )
    assert field.status_code == 201, field.text
    async with TestSessionLocal() as session, session.begin():
        variable = await session.get(Variable, uuid.UUID(var_id))
        assert variable is not None
        for event_id in (start, success):
            session.add(
                VariableValue(
                    project_id=variable.project_id,
                    branch_id=variable.branch_id,
                    variable_id=variable.id,
                    event_id=uuid.UUID(event_id),
                    field_definition_id=uuid.UUID(field.json()["id"]),
                    source_column="props.currency",
                    values=["USD"],
                    presence_rate=0.8,
                )
            )

    rows = {r["event_name"]: r for r in await _events_of(client, slug, var_id)}
    assert rows["checkout:start"]["presence_rate"] == 0.8
    assert rows["checkout:start"]["required_presence_threshold"] is None
    assert rows["checkout:start"]["suggested_required"] is False
    assert rows["checkout:success"]["required_presence_threshold"] == 0.5
    assert rows["checkout:success"]["suggested_required"] is True


@pytest.mark.asyncio
async def test_the_events_list_filters_by_property_id_or_name(client: AsyncClient) -> None:
    slug = "prop-filter"
    var_id, events = await _seed(client, slug)
    await _bulk(
        client, slug, var_id, {"event_ids": [events["checkout:start"], events["home:view"]]}
    )
    assert await _filtered(client, slug, var_id) == {"checkout:start", "home:view"}
    assert await _filtered(client, slug, "currency") == {"checkout:start", "home:view"}
    assert await _filtered(client, slug, "nothing") == set()
    assert await _filtered(client, slug, str(uuid.uuid4())) == set()


@pytest.mark.asyncio
async def test_bulk_and_filter_work_on_a_branch_and_leave_main_alone(client: AsyncClient) -> None:
    slug = "prop-branch"
    var_id, _ = await _seed(client, slug)
    branch = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    assert branch.status_code == 201, branch.text
    branch_id = branch.json()["id"]
    b_items = (await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")).json()
    b_event = next(e["id"] for e in b_items["items"] if e["name"] == "home:view")
    b_vars = (await client.get(f"/api/v1/projects/{slug}/properties?branch={branch_id}")).json()
    b_var = next(v["id"] for v in b_vars["items"] if v["name"] == "currency")

    # The main row's ids are not on the branch.
    wrong = await _bulk(client, slug, var_id, {"event_ids": [b_event]}, branch_id)
    assert wrong.status_code == 404

    resp = await _bulk(client, slug, b_var, {"event_ids": [b_event], "required": True}, branch_id)
    assert resp.status_code == 200, resp.text
    assert await _filtered(client, slug, "currency", branch_id) == {"home:view"}
    assert await _filtered(client, slug, "currency") == set()
    assert await _events_of(client, slug, var_id) == []
    diff = (await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")).json()
    assert "home:view" in str(diff)


@pytest.mark.asyncio
async def test_a_viewer_can_read_but_not_bulk_edit(client: AsyncClient) -> None:
    slug = "prop-viewer"
    var_id, events = await _seed(client, slug)
    await client.post("/api/v1/auth/logout")
    registered = await client.post(
        "/api/v1/auth/register",
        json={"email": "prop-viewer@example.com", "password": PASSWORD, "name": "Viewer"},
    )
    assert registered.status_code == 201, registered.text
    await add_member_by_slug(slug, "prop-viewer@example.com", "viewer")

    assert await _events_of(client, slug, var_id) == []
    denied = await _bulk(client, slug, var_id, {"event_ids": [events["home:view"]]})
    assert denied.status_code == 403, denied.text
    denied = await client.post(
        f"/api/v1/projects/{slug}/properties/{var_id}/event-overrides/bulk-delete",
        json={"event_ids": [events["home:view"]]},
    )
    assert denied.status_code == 403, denied.text


@pytest.mark.asyncio
async def test_list_counts_the_events_that_list_the_property(client: AsyncClient) -> None:
    slug = "prop-list-counts"
    var_id, events = await _seed(client, slug)
    resp = await _bulk(
        client, slug, var_id, {"event_ids": [events["checkout:start"]], "required": True}
    )
    assert resp.status_code == 200, resp.text
    resp = await _bulk(client, slug, var_id, {"event_ids": [events["home:view"]]})
    assert resp.status_code == 200, resp.text

    listing = await client.get(f"/api/v1/projects/{slug}/properties")
    assert listing.status_code == 200, listing.text
    (row,) = [item for item in listing.json()["items"] if item["id"] == var_id]
    assert (row["listed_event_count"], row["required_event_count"]) == (2, 1)
    # Listing is not observing: no scan saw the property anywhere yet.
    assert row["event_count"] == 0
