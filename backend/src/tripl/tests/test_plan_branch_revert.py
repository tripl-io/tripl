"""Reverting one change in a plan branch.

A revert restores the entity — or one field of it — to the branch's base
snapshot: the state the plan was in when the branch was opened. These tests
drive it through the API, since that is where the guards (branch status, missing
base, unsupported kinds) live.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from tripl.models.event import Event
from tripl.models.plan_branch import PlanBranch
from tripl.models.plan_revision import PlanRevision
from tripl.tests.conftest import TestSessionLocal


async def _seed(client: AsyncClient, slug: str) -> tuple[str, str, str]:
    """A project on main with one event type, one field and one event."""
    project = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert project.status_code == 201
    event_type = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "track", "display_name": "Track"},
    )
    et_id = event_type.json()["id"]
    field = await client.post(
        f"/api/v1/projects/{slug}/event-types/{et_id}/fields",
        json={"name": "currency", "display_name": "Currency", "field_type": "string"},
    )
    field_id = field.json()["id"]
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": et_id,
            "name": "purchase:success",
            "description": "on main",
            "field_values": [{"field_definition_id": field_id, "value": "USD"}],
        },
    )
    assert event.status_code == 201
    return et_id, field_id, event.json()["id"]


async def _branch(client: AsyncClient, slug: str, name: str = "feature") -> str:
    resp = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": name})
    assert resp.status_code == 201
    return resp.json()["id"]


async def _branch_event(client: AsyncClient, slug: str, branch_id: str, name: str) -> dict:
    events = await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")
    return next(e for e in events.json()["items"] if e["name"] == name)


async def _branch_event_detail(client: AsyncClient, slug: str, branch_id: str, name: str) -> dict:
    """The DETAIL response, which the list variant is not a substitute for: it
    deliberately drops ``superseded_by_event_id`` (no room on the row, and
    nothing on that surface asks the question)."""
    listed = await _branch_event(client, slug, branch_id, name)
    resp = await client.get(f"/api/v1/projects/{slug}/events/{listed['id']}?branch={branch_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _revert(client: AsyncClient, slug: str, branch_id: str, **body):
    return await client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/revert", json=body)


async def _diff(client: AsyncClient, slug: str, branch_id: str) -> dict:
    resp = await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.asyncio
async def test_revert_undoes_one_field_and_leaves_the_others(client: AsyncClient) -> None:
    slug = "revert-field"
    await _seed(client, slug)
    branch_id = await _branch(client, slug)

    event = await _branch_event(client, slug, branch_id, "purchase:success")
    edit = await client.patch(
        f"/api/v1/projects/{slug}/events/{event['id']}?branch={branch_id}",
        json={"description": "edited on branch", "tags": ["revenue"]},
    )
    assert edit.status_code == 200
    diff = await _diff(client, slug, branch_id)
    assert {fc["field"] for fc in diff["entries"][0]["field_changes"]} == {"description", "tags"}

    resp = await _revert(
        client,
        slug,
        branch_id,
        entity_type="event",
        name="purchase:success",
        parent="track",
        field="description",
    )
    assert resp.status_code == 200, resp.text

    # The response is the branch's diff after the revert: description is back to
    # its base value, the untouched edit survives.
    entries = resp.json()["entries"]
    assert [fc["field"] for fc in entries[0]["field_changes"]] == ["tags"]
    reverted = await _branch_event(client, slug, branch_id, "purchase:success")
    assert reverted["description"] == "on main"
    assert [tag["name"] for tag in reverted["tags"]] == ["revenue"]


@pytest.mark.asyncio
async def test_revert_without_a_field_restores_the_whole_entity(client: AsyncClient) -> None:
    slug = "revert-entity"
    await _seed(client, slug)
    branch_id = await _branch(client, slug)

    event = await _branch_event(client, slug, branch_id, "purchase:success")
    branch_field_id = event["field_values"][0]["field_definition_id"]
    await client.patch(
        f"/api/v1/projects/{slug}/events/{event['id']}?branch={branch_id}",
        json={
            "description": "edited",
            "tags": ["revenue"],
            "reviewed": True,
            "field_values": [{"field_definition_id": branch_field_id, "value": "EUR"}],
        },
    )

    resp = await _revert(
        client, slug, branch_id, entity_type="event", name="purchase:success", parent="track"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["summary"] == {"added": 0, "removed": 0, "changed": 0, "housekeeping": 0}
    assert resp.json()["entries"] == []


@pytest.mark.asyncio
async def test_revert_restores_a_changed_field_value(client: AsyncClient) -> None:
    """An event's field values are the change reviewers most often want to undo."""
    slug = "revert-values"
    await _seed(client, slug)
    branch_id = await _branch(client, slug)

    event = await _branch_event(client, slug, branch_id, "purchase:success")
    branch_field_id = event["field_values"][0]["field_definition_id"]
    edit = await client.patch(
        f"/api/v1/projects/{slug}/events/{event['id']}?branch={branch_id}",
        json={"field_values": [{"field_definition_id": branch_field_id, "value": "EUR"}]},
    )
    assert edit.status_code == 200

    diff = await _diff(client, slug, branch_id)
    change = next(fc for fc in diff["entries"][0]["field_changes"] if fc["field"] == "field_values")
    assert [(item["key"], item["kind"]) for item in change["items"]] == [("currency", "changed")]

    resp = await _revert(
        client,
        slug,
        branch_id,
        entity_type="event",
        name="purchase:success",
        parent="track",
        field="field_values",
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["entries"] == []
    restored = await _branch_event(client, slug, branch_id, "purchase:success")
    assert [fv["value"] for fv in restored["field_values"]] == ["USD"]


@pytest.mark.asyncio
async def test_revert_deletes_an_entity_the_branch_added(client: AsyncClient) -> None:
    slug = "revert-added"
    await _seed(client, slug)
    branch_id = await _branch(client, slug)

    event_types = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    branch_et_id = next(et["id"] for et in event_types.json() if et["name"] == "track")
    added = await client.post(
        f"/api/v1/projects/{slug}/events?branch={branch_id}",
        json={"event_type_id": branch_et_id, "name": "checkout:started"},
    )
    assert added.status_code == 201

    resp = await _revert(
        client, slug, branch_id, entity_type="event", name="checkout:started", parent="track"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["entries"] == []
    events = await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")
    assert [e["name"] for e in events.json()["items"]] == ["purchase:success"]


@pytest.mark.asyncio
async def test_revert_rejects_a_single_field_of_an_added_entity(client: AsyncClient) -> None:
    slug = "revert-added-field"
    await _seed(client, slug)
    branch_id = await _branch(client, slug)

    event_types = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    branch_et_id = next(et["id"] for et in event_types.json() if et["name"] == "track")
    await client.post(
        f"/api/v1/projects/{slug}/events?branch={branch_id}",
        json={"event_type_id": branch_et_id, "name": "checkout:started"},
    )

    # An added entity has no base state, so there is no value to put back.
    resp = await _revert(
        client,
        slug,
        branch_id,
        entity_type="event",
        name="checkout:started",
        parent="track",
        field="description",
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_revert_rebuilds_a_deleted_event_with_its_values(client: AsyncClient) -> None:
    slug = "revert-removed"
    await _seed(client, slug)
    branch_id = await _branch(client, slug)

    event = await _branch_event(client, slug, branch_id, "purchase:success")
    deleted = await client.delete(
        f"/api/v1/projects/{slug}/events/{event['id']}?branch={branch_id}"
    )
    assert deleted.status_code == 204
    assert (await _diff(client, slug, branch_id))["summary"]["removed"] == 1

    resp = await _revert(
        client, slug, branch_id, entity_type="event", name="purchase:success", parent="track"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["entries"] == []

    # The event is back, and so is the field value that hung off it.
    restored = await _branch_event(client, slug, branch_id, "purchase:success")
    assert restored["description"] == "on main"
    assert [fv["value"] for fv in restored["field_values"]] == ["USD"]
    # A fresh row on the branch — main's copy was never touched.
    assert restored["id"] != event["id"]


@pytest.mark.asyncio
async def test_revert_refuses_when_two_events_share_the_name(client: AsyncClient) -> None:
    """Event names are not unique, and the diff keys entries by name.

    With two namesakes on the branch, "which one does this change belong to" has
    no answer — writing base values onto an arbitrary one would clobber a
    sibling the reviewer never looked at.
    """
    slug = "revert-ambiguous"
    await _seed(client, slug)
    branch_id = await _branch(client, slug)

    event = await _branch_event(client, slug, branch_id, "purchase:success")
    await client.patch(
        f"/api/v1/projects/{slug}/events/{event['id']}?branch={branch_id}",
        json={"description": "edited on branch"},
    )

    # A namesake under the same event type — the API allows it (no uniqueness).
    async with TestSessionLocal() as session:
        branch_event = (
            await session.execute(select(Event).where(Event.id == uuid.UUID(event["id"])))
        ).scalar_one()
        session.add(
            Event(
                id=uuid.uuid4(),
                project_id=branch_event.project_id,
                branch_id=branch_event.branch_id,
                event_type_id=branch_event.event_type_id,
                name=branch_event.name,
                description="the namesake",
            )
        )
        await session.commit()

    resp = await _revert(
        client,
        slug,
        branch_id,
        entity_type="event",
        name="purchase:success",
        parent="track",
        field="description",
    )
    assert resp.status_code == 409
    # Since origin ids the diff tells the two apart — the copy's edit and the
    # added namesake are two entries — so the name alone now names two changes;
    # the entry's entity_id picks one.
    assert "More than one" in resp.json()["detail"]

    # Neither namesake was touched.
    async with TestSessionLocal() as session:
        descriptions = sorted(
            e.description
            for e in (
                await session.execute(select(Event).where(Event.branch_id == uuid.UUID(branch_id)))
            )
            .scalars()
            .all()
        )
    assert descriptions == ["edited on branch", "the namesake"]


@pytest.mark.asyncio
async def test_revert_refuses_a_base_snapshot_that_predates_a_required_field(
    client: AsyncClient,
) -> None:
    """Snapshots gain fields over time; an old base may not describe the entity.

    Rebuilding it anyway would invent state the base never recorded, so a base
    missing an identity field is refused with a message that says why — not a
    500 from a KeyError.
    """
    slug = "revert-old-base"
    await _seed(client, slug)
    created = await client.post(
        f"/api/v1/projects/{slug}/meta-fields",
        json={"name": "jira", "display_name": "Jira ticket", "field_type": "string"},
    )
    assert created.status_code == 201
    branch_id = await _branch(client, slug)

    meta_fields = await client.get(f"/api/v1/projects/{slug}/meta-fields?branch={branch_id}")
    branch_mf_id = next(mf["id"] for mf in meta_fields.json() if mf["name"] == "jira")
    deleted = await client.delete(
        f"/api/v1/projects/{slug}/meta-fields/{branch_mf_id}?branch={branch_id}"
    )
    assert deleted.status_code == 204

    # Age the base snapshot: drop a field the serializer only started emitting
    # after this branch would have been opened. (display_name is not part of the
    # diff's key, so the diff still lists the deletion — only the rebuild is
    # short of what it needs.)
    async with TestSessionLocal() as session:
        branch = (
            await session.execute(select(PlanBranch).where(PlanBranch.id == uuid.UUID(branch_id)))
        ).scalar_one()
        revision = (
            await session.execute(
                select(PlanRevision).where(PlanRevision.id == branch.base_revision_id)
            )
        ).scalar_one()
        for snapshot_meta_field in revision.payload["meta_fields"]:
            del snapshot_meta_field["display_name"]
        flag_modified(revision, "payload")
        await session.commit()

    resp = await _revert(client, slug, branch_id, entity_type="meta_field", name="jira")
    assert resp.status_code == 409
    assert "predates 'display_name'" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_revert_rebuilds_an_event_whose_base_predates_status(client: AsyncClient) -> None:
    """A field with a column default is filled from that default, not refused.

    That is the same answer the diff gives for an older base (it skips keys the
    base never had), so the branch lands level with its base instead of showing
    a phantom status change.
    """
    slug = "revert-old-base-status"
    await _seed(client, slug)
    branch_id = await _branch(client, slug)

    event = await _branch_event(client, slug, branch_id, "purchase:success")
    await client.delete(f"/api/v1/projects/{slug}/events/{event['id']}?branch={branch_id}")

    async with TestSessionLocal() as session:
        branch = (
            await session.execute(select(PlanBranch).where(PlanBranch.id == uuid.UUID(branch_id)))
        ).scalar_one()
        revision = (
            await session.execute(
                select(PlanRevision).where(PlanRevision.id == branch.base_revision_id)
            )
        ).scalar_one()
        revision.payload["snapshot_version"] = 1
        for snapshot_event in revision.payload["events"]:
            del snapshot_event["status"]
        flag_modified(revision, "payload")
        await session.commit()

    resp = await _revert(
        client, slug, branch_id, entity_type="event", name="purchase:success", parent="track"
    )
    assert resp.status_code == 200, resp.text
    restored = await _branch_event(client, slug, branch_id, "purchase:success")
    assert restored["status"] == "draft"


@pytest.mark.asyncio
async def test_revert_rebuilds_a_deleted_event_type(client: AsyncClient) -> None:
    slug = "revert-removed-type"
    await _seed(client, slug)
    empty = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "identify", "display_name": "Identify", "description": "who"},
    )
    assert empty.status_code == 201
    branch_id = await _branch(client, slug)

    event_types = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    branch_et_id = next(et["id"] for et in event_types.json() if et["name"] == "identify")
    deleted = await client.delete(
        f"/api/v1/projects/{slug}/event-types/{branch_et_id}?branch={branch_id}"
    )
    assert deleted.status_code == 204

    resp = await _revert(client, slug, branch_id, entity_type="event_type", name="identify")
    assert resp.status_code == 200, resp.text
    assert resp.json()["entries"] == []

    event_types = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    restored = next(et for et in event_types.json() if et["name"] == "identify")
    assert restored["display_name"] == "Identify"
    assert restored["description"] == "who"


@pytest.mark.asyncio
async def test_revert_rebuilds_a_deleted_field_definition(client: AsyncClient) -> None:
    slug = "revert-removed-field-def"
    et_id, _field_id, _event_id = await _seed(client, slug)
    # A field no event carries a value for: restoring it is a clean insert, with
    # no values to reconcile (those are the event's own diff entry).
    spare = await client.post(
        f"/api/v1/projects/{slug}/event-types/{et_id}/fields",
        json={"name": "coupon", "display_name": "Coupon", "field_type": "string"},
    )
    assert spare.status_code == 201
    branch_id = await _branch(client, slug)

    event_types = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    branch_et_id = next(et["id"] for et in event_types.json() if et["name"] == "track")
    fields = await client.get(
        f"/api/v1/projects/{slug}/event-types/{branch_et_id}/fields?branch={branch_id}"
    )
    field_id = next(f["id"] for f in fields.json() if f["name"] == "coupon")
    deleted = await client.delete(
        f"/api/v1/projects/{slug}/event-types/{branch_et_id}/fields/{field_id}?branch={branch_id}"
    )
    assert deleted.status_code == 204

    resp = await _revert(
        client, slug, branch_id, entity_type="field_definition", name="coupon", parent="track"
    )
    assert resp.status_code == 200, resp.text

    fields = await client.get(
        f"/api/v1/projects/{slug}/event-types/{branch_et_id}/fields?branch={branch_id}"
    )
    restored = next(f for f in fields.json() if f["name"] == "coupon")
    assert restored["field_type"] == "string"
    assert restored["display_name"] == "Coupon"


@pytest.mark.asyncio
async def test_revert_rejects_a_single_field_of_a_deleted_entity(client: AsyncClient) -> None:
    slug = "revert-removed-field"
    await _seed(client, slug)
    branch_id = await _branch(client, slug)

    event = await _branch_event(client, slug, branch_id, "purchase:success")
    await client.delete(f"/api/v1/projects/{slug}/events/{event['id']}?branch={branch_id}")

    resp = await _revert(
        client,
        slug,
        branch_id,
        entity_type="event",
        name="purchase:success",
        parent="track",
        field="description",
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_revert_rejects_a_change_that_is_not_in_the_diff(client: AsyncClient) -> None:
    slug = "revert-unknown"
    await _seed(client, slug)
    branch_id = await _branch(client, slug)

    resp = await _revert(
        client, slug, branch_id, entity_type="event", name="purchase:success", parent="track"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_revert_rejects_an_unchanged_field(client: AsyncClient) -> None:
    slug = "revert-unchanged-field"
    await _seed(client, slug)
    branch_id = await _branch(client, slug)

    event = await _branch_event(client, slug, branch_id, "purchase:success")
    await client.patch(
        f"/api/v1/projects/{slug}/events/{event['id']}?branch={branch_id}",
        json={"description": "edited on branch"},
    )

    resp = await _revert(
        client,
        slug,
        branch_id,
        entity_type="event",
        name="purchase:success",
        parent="track",
        field="source_name",
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_revert_rejects_a_merged_branch(client: AsyncClient) -> None:
    slug = "revert-merged"
    await _seed(client, slug)
    branch_id = await _branch(client, slug)

    event = await _branch_event(client, slug, branch_id, "purchase:success")
    await client.patch(
        f"/api/v1/projects/{slug}/events/{event['id']}?branch={branch_id}",
        json={"description": "edited on branch"},
    )
    await client.post(
        f"/api/v1/projects/{slug}/branches/{branch_id}/transition", json={"action": "submit"}
    )
    await client.post(
        f"/api/v1/projects/{slug}/branches/{branch_id}/transition", json={"action": "approve"}
    )
    merged = await client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge")
    assert merged.status_code == 200

    resp = await _revert(
        client,
        slug,
        branch_id,
        entity_type="event",
        name="purchase:success",
        parent="track",
        field="description",
    )
    assert resp.status_code == 409
    assert "merged" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_revert_rejects_the_main_branch(client: AsyncClient) -> None:
    slug = "revert-main"
    await _seed(client, slug)
    branches = await client.get(f"/api/v1/projects/{slug}/branches")
    main_id = next(b for b in branches.json()["items"] if b["kind"] == "main")["id"]

    resp = await _revert(
        client, slug, main_id, entity_type="event", name="purchase:success", parent="track"
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_revert_rejects_an_unknown_branch(client: AsyncClient) -> None:
    slug = "revert-404"
    await _seed(client, slug)

    resp = await _revert(
        client,
        slug,
        str(uuid.uuid4()),
        entity_type="event",
        name="purchase:success",
        parent="track",
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_revert_restores_a_variables_documented_values(client: AsyncClient) -> None:
    slug = "revert-variable"
    await _seed(client, slug)
    created = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={"name": "currency", "variable_type": "string", "allowed_values": ["USD"]},
    )
    assert created.status_code == 201
    branch_id = await _branch(client, slug)

    variables = await client.get(f"/api/v1/projects/{slug}/variables?branch={branch_id}")
    branch_var = next(v for v in variables.json()["items"] if v["name"] == "currency")
    await client.patch(
        f"/api/v1/projects/{slug}/variables/{branch_var['id']}?branch={branch_id}",
        json={"allowed_values": ["USD", "EUR"]},
    )

    resp = await _revert(
        client,
        slug,
        branch_id,
        entity_type="variable",
        name="currency",
        field="allowed_values",
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["entries"] == []
    variables = await client.get(f"/api/v1/projects/{slug}/variables?branch={branch_id}")
    restored = next(v for v in variables.json()["items"] if v["name"] == "currency")
    assert restored["allowed_values"] == ["USD"]


@pytest.mark.asyncio
async def test_revert_rebuilds_a_deleted_variable(client: AsyncClient) -> None:
    slug = "revert-removed-variable"
    await _seed(client, slug)
    created = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={
            "name": "currency",
            "variable_type": "string",
            "allowed_values": ["USD"],
            "bindings": ["page_data.currency"],
        },
    )
    assert created.status_code == 201
    branch_id = await _branch(client, slug)

    variables = await client.get(f"/api/v1/projects/{slug}/variables?branch={branch_id}")
    branch_var = next(v for v in variables.json()["items"] if v["name"] == "currency")
    deleted = await client.delete(
        f"/api/v1/projects/{slug}/variables/{branch_var['id']}?branch={branch_id}"
    )
    assert deleted.status_code == 204

    resp = await _revert(client, slug, branch_id, entity_type="variable", name="currency")
    assert resp.status_code == 200, resp.text
    assert resp.json()["entries"] == []

    variables = await client.get(f"/api/v1/projects/{slug}/variables?branch={branch_id}")
    restored = next(v for v in variables.json()["items"] if v["name"] == "currency")
    assert restored["allowed_values"] == ["USD"]
    assert restored["bindings"] == ["page_data.currency"]


@pytest.mark.asyncio
async def test_revert_restores_a_successor_named_on_main(client: AsyncClient) -> None:
    """``superseded_by`` is the one event field the snapshot stores as a dotted
    natural key rather than a value, because an id means nothing across branches.
    Restoring it has to resolve that key against THIS branch's own rows."""
    slug = "revert-successor"
    et_id, _field_id, _event_id = await _seed(client, slug)
    old = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et_id, "name": "purchase:legacy"},
    )
    successor = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et_id, "name": "purchase:v2"},
    )
    await client.patch(
        f"/api/v1/projects/{slug}/events/{old.json()['id']}",
        json={"superseded_by_event_id": successor.json()["id"]},
    )

    branch_id = await _branch(client, slug)
    branch_old = await _branch_event_detail(client, slug, branch_id, "purchase:legacy")
    branch_successor = await _branch_event(client, slug, branch_id, "purchase:v2")
    assert branch_old["superseded_by_event_id"] == branch_successor["id"]

    # Clear it on the branch, then put it back.
    cleared = await client.patch(
        f"/api/v1/projects/{slug}/events/{branch_old['id']}?branch={branch_id}",
        json={"superseded_by_event_id": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["superseded_by_event_id"] is None
    diff = await _diff(client, slug, branch_id)
    entry = next(e for e in diff["entries"] if e["name"] == "purchase:legacy")
    assert "superseded_by" in {fc["field"] for fc in entry["field_changes"]}

    resp = await _revert(
        client,
        slug,
        branch_id,
        entity_type="event",
        name="purchase:legacy",
        parent="track",
        field="superseded_by",
    )
    assert resp.status_code == 200, resp.text
    restored = await _branch_event_detail(client, slug, branch_id, "purchase:legacy")
    # The BRANCH's copy of the successor, resolved from the dotted key — not the
    # main id the base snapshot was taken from.
    assert restored["superseded_by_event_id"] == branch_successor["id"]
    assert restored["superseded_by_event_id"] != successor.json()["id"]


@pytest.mark.asyncio
async def test_revert_clears_a_successor_that_no_longer_exists(client: AsyncClient) -> None:
    """The successor can be deleted on the branch after the pointer was changed.
    Reverting then has nothing to resolve; it must clear the pointer rather than
    refuse the revert and strand every other field of the entity."""
    slug = "revert-successor-gone"
    et_id, _field_id, _event_id = await _seed(client, slug)
    old = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et_id, "name": "purchase:legacy"},
    )
    successor = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et_id, "name": "purchase:v2"},
    )
    await client.patch(
        f"/api/v1/projects/{slug}/events/{old.json()['id']}",
        json={"superseded_by_event_id": successor.json()["id"]},
    )

    branch_id = await _branch(client, slug)
    branch_successor = await _branch_event(client, slug, branch_id, "purchase:v2")
    # Deleting the successor SET NULLs the pointer, which is itself the change
    # being reverted.
    dropped = await client.delete(
        f"/api/v1/projects/{slug}/events/{branch_successor['id']}?branch={branch_id}"
    )
    assert dropped.status_code in (200, 204)

    resp = await _revert(
        client,
        slug,
        branch_id,
        entity_type="event",
        name="purchase:legacy",
        parent="track",
        field="superseded_by",
    )
    assert resp.status_code == 200, resp.text
    restored = await _branch_event_detail(client, slug, branch_id, "purchase:legacy")
    assert restored["superseded_by_event_id"] is None
