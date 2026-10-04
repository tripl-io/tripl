"""The analyst-to-developer handoff on a plan branch.

A read-only walk of production on 2026-09-07 followed an analyst authoring
events for a feature on a working branch and a developer handed the branch to
instrument. Every test here pins one thing that walk found broken, in the
order the epic ranks them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy import update as sql_update

from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_meta_value import EventMetaValue
from tripl.services.plan_revision_service import (
    PLAN_SNAPSHOT_VERSION,
    compute_plan_diff_entries,
    with_snapshot_defaults,
)
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_events import _seed_scan_name_rule
from tripl.tests.test_metrics_api import _seed_event_metrics_at
from tripl.tests.test_plan_branches import _create_branch, _seed_plan


async def _main_type(client: AsyncClient, slug: str, name: str = "track") -> dict:
    types = await client.get(f"/api/v1/projects/{slug}/event-types")
    return next(et for et in types.json() if et["name"] == name)


async def _branch_type(client: AsyncClient, slug: str, branch_id: str, name: str = "track") -> dict:
    types = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    return next(et for et in types.json() if et["name"] == name)


def _field_id(event_type: dict, name: str = "name") -> str:
    return next(fd["id"] for fd in event_type["field_definitions"] if fd["name"] == name)


async def _forget_identity(event_id: str, *, name: str) -> None:
    """Turn a row into what every branch event looked like before the fix."""
    async with TestSessionLocal() as session, session.begin():
        await session.execute(
            sql_update(Event)
            .where(Event.id == uuid.UUID(event_id))
            .values(source_name=None, name=name, title="")
        )


# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_naming_rule_reaches_the_branch_copy_of_the_event_type(client: AsyncClient) -> None:
    """P1. A scan config binds MAIN's type id; the branch copy has a new one.

    Before: the branch form offered free text, ``create_event`` found no rule,
    and the row was saved with no identity. Now the type reports the rule on
    both branches and the create path derives the name on either.
    """
    slug = "handoff-rule"
    await _seed_plan(client, slug)
    main_type = await _main_type(client, slug)
    assert main_type["event_name_format"] is None

    await _seed_scan_name_rule(slug, main_type["id"], "track:{name}")
    branch_id = await _create_branch(client, slug)
    branch_type = await _branch_type(client, slug, branch_id)
    assert branch_type["id"] != main_type["id"]
    assert branch_type["event_name_format"] == "track:{name}"
    assert (await _main_type(client, slug))["event_name_format"] == "track:{name}"
    single = await client.get(
        f"/api/v1/projects/{slug}/event-types/{branch_type['id']}?branch={branch_id}"
    )
    assert single.json()["event_name_format"] == "track:{name}"

    created = await client.post(
        f"/api/v1/projects/{slug}/events?branch={branch_id}",
        json={
            "event_type_id": branch_type["id"],
            "name": "Tap on a model card",
            "field_values": [{"field_definition_id": _field_id(branch_type), "value": "tap"}],
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "track:tap"
    assert body["source_name"] == "track:tap"
    assert any("generated from the scan rule" in w for w in body["warnings"])

    bulk = await client.post(
        f"/api/v1/projects/{slug}/events/bulk?branch={branch_id}",
        json=[
            {
                "event_type_id": branch_type["id"],
                "name": "ignored",
                "field_values": [{"field_definition_id": _field_id(branch_type), "value": "swipe"}],
            }
        ],
    )
    assert bulk.status_code == 201, bulk.text
    assert [(e["name"], e["source_name"]) for e in bulk.json()] == [("track:swipe", "track:swipe")]
    # Bulk-created rows start their history the way single creates do.
    history = (
        await client.get(
            f"/api/v1/projects/{slug}/events/{bulk.json()[0]['id']}/history?branch={branch_id}"
        )
    ).json()
    assert [(row["field"], row["new_value"], row["user_email"]) for row in history] == [
        ("created", "track:swipe", "test@example.com")
    ]


@pytest.mark.asyncio
async def test_diff_warns_about_branch_events_with_no_scan_identity(client: AsyncClient) -> None:
    slug = "handoff-warn"
    await _seed_plan(client, slug)
    await _seed_scan_name_rule(slug, (await _main_type(client, slug))["id"], "track:{name}")
    branch_id = await _create_branch(client, slug)
    branch_type = await _branch_type(client, slug, branch_id)
    field_id = _field_id(branch_type)

    filled = await client.post(
        f"/api/v1/projects/{slug}/events?branch={branch_id}",
        json={
            "event_type_id": branch_type["id"],
            "name": "x",
            "field_values": [{"field_definition_id": field_id, "value": "tap"}],
        },
    )
    await _forget_identity(filled.json()["id"], name="Tap on a model card")
    # With ``filled`` stripped of its identity, the same values create again.
    twin = await client.post(
        f"/api/v1/projects/{slug}/events?branch={branch_id}",
        json={
            "event_type_id": branch_type["id"],
            "name": "y",
            "field_values": [{"field_definition_id": field_id, "value": "tap"}],
        },
    )
    assert twin.status_code == 201, twin.text
    await _forget_identity(twin.json()["id"], name="Tap on a model card (again)")
    # Cannot be created through the API any more (422), so it is written raw.
    async with TestSessionLocal() as session, session.begin():
        unfilled = Event(
            project_id=uuid.UUID(branch_type["project_id"]),
            branch_id=uuid.UUID(branch_id),
            event_type_id=uuid.UUID(branch_type["id"]),
            name="Swipe without a name value",
            description="",
        )
        session.add(unfilled)

    diff = await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")
    assert diff.status_code == 200
    warnings = {
        e["name"]: e["warnings"] for e in diff.json()["entries"] if e["entity_type"] == "event"
    }
    assert warnings["Tap on a model card"] == [
        "No scan identity: this event was authored without one; the naming rule "
        "'track:{name}' derives 'track:tap'. Recreate it so the rule stamps the identity."
    ]
    assert warnings["Tap on a model card (again)"] == [
        "No scan identity: 'track:tap' is already held by 'Tap on a model card' on this branch."
    ]
    assert warnings["Swipe without a name value"] == [
        "No scan identity: the naming rule 'track:{name}' needs name. "
        "Fill those fields so the event merges with its scanned counterpart."
    ]


# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_title_is_a_label_beside_the_identity(client: AsyncClient) -> None:
    slug = "handoff-title"
    await _seed_plan(client, slug)
    main_type = await _main_type(client, slug)
    created = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": main_type["id"], "name": "track:tap", "title": "  Tap on a card "},
    )
    assert created.status_code == 201, created.text
    assert created.json()["title"] == "Tap on a card"
    event_id = created.json()["id"]
    listed = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"]
    assert next(e for e in listed if e["id"] == event_id)["title"] == "Tap on a card"

    branch_id = await _create_branch(client, slug)
    branch_events = (await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")).json()[
        "items"
    ]
    copy = next(e for e in branch_events if e["name"] == "track:tap")
    assert copy["title"] == "Tap on a card"

    renamed = await client.patch(
        f"/api/v1/projects/{slug}/events/{copy['id']}?branch={branch_id}",
        json={"title": "Tap on a model card"},
    )
    assert renamed.status_code == 200, renamed.text
    history = (
        await client.get(f"/api/v1/projects/{slug}/events/{copy['id']}/history?branch={branch_id}")
    ).json()
    title_row = next(row for row in history if row["field"] == "title")
    assert (title_row["old_value"], title_row["new_value"]) == (
        "Tap on a card",
        "Tap on a model card",
    )

    diff = (await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")).json()
    entry = next(
        e for e in diff["entries"] if e["entity_type"] == "event" and e["name"] == "track:tap"
    )
    [title_change] = entry["field_changes"]
    assert (title_change["field"], title_change["before"], title_change["after"]) == (
        "title",
        "Tap on a card",
        "Tap on a model card",
    )


def test_a_base_snapshot_without_title_reads_as_an_empty_title() -> None:
    """No version bump: an older v2 base must not flag every event as changed.

    ``new`` has to spell out EVERY key in ``_V2_EVENT_DEFAULTS`` — hence
    ``superseded_by`` beside ``title`` — because the identity assertion below is
    the point of the test: a snapshot that already carries the defaults is
    handed back untouched rather than rebuilt. Adding a default makes this line
    fail, which is the intended tripwire: the new key has to be checked against
    both halves of the shim, not just added to it.
    """
    old = {
        "snapshot_version": PLAN_SNAPSHOT_VERSION,
        "events": [{"id": "1", "event_type_name": "track", "name": "a", "field_values": []}],
    }
    new = {
        "snapshot_version": PLAN_SNAPSHOT_VERSION,
        "events": [
            {
                "id": "1",
                "event_type_name": "track",
                "name": "a",
                "title": "",
                "superseded_by": None,
                "required_presence_threshold": None,
                "field_values": [],
            }
        ],
    }
    assert with_snapshot_defaults(old)["events"][0]["title"] == ""
    assert with_snapshot_defaults(old)["events"][0]["superseded_by"] is None
    assert with_snapshot_defaults(old)["events"][0]["required_presence_threshold"] is None
    assert with_snapshot_defaults(new) is new
    assert compute_plan_diff_entries(old, new) == []


# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resaving_an_unchanged_value_keeps_its_authored_flag(client: AsyncClient) -> None:
    slug = "handoff-authored"
    await _seed_plan(client, slug)
    main_type = await _main_type(client, slug)
    field_id = _field_id(main_type)
    event_id = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"][0]["id"]
    async with TestSessionLocal() as session, session.begin():
        session.add(
            EventFieldValue(
                event_id=uuid.UUID(event_id),
                field_definition_id=uuid.UUID(field_id),
                value="observed",
                is_authored=False,
            )
        )

    async def flag() -> bool:
        async with TestSessionLocal() as session:
            row = (
                await session.execute(
                    select(EventFieldValue).where(EventFieldValue.event_id == uuid.UUID(event_id))
                )
            ).scalar_one()
            return row.is_authored

    resave = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}",
        json={
            "description": "edited",
            "field_values": [{"field_definition_id": field_id, "value": "observed"}],
        },
    )
    assert resave.status_code == 200, resave.text
    assert await flag() is False

    edit = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}",
        json={"field_values": [{"field_definition_id": field_id, "value": "typed"}]},
    )
    assert edit.status_code == 200, edit.text
    assert await flag() is True


def test_diff_does_not_read_an_authored_flag_flip_as_a_change() -> None:
    base = {
        "snapshot_version": PLAN_SNAPSHOT_VERSION,
        "events": [
            {
                "id": "1",
                "event_type_name": "track",
                "name": "a",
                "title": "",
                "field_values": [{"field_name": "name", "value": "x", "is_authored": False}],
            }
        ],
    }
    flipped = {
        **base,
        "events": [
            {
                **base["events"][0],
                "field_values": [{"field_name": "name", "value": "x", "is_authored": True}],
            }
        ],
    }
    retyped = {
        **base,
        "events": [
            {
                **base["events"][0],
                "field_values": [{"field_name": "name", "value": "y", "is_authored": True}],
            }
        ],
    }
    assert compute_plan_diff_entries(base, flipped) == []
    [entry] = compute_plan_diff_entries(base, retyped)
    assert (
        entry.changes == ["field_values: name: x → y"]
        or entry.field_changes[0].field == "field_values"
    )


# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meta_value_pasted_as_a_full_link_is_stored_as_its_key(client: AsyncClient) -> None:
    slug = "handoff-link"
    await _seed_plan(client, slug)
    main_type = await _main_type(client, slug)
    meta = await client.post(
        f"/api/v1/projects/{slug}/meta-fields",
        json={
            "name": "jira",
            "display_name": "Jira",
            "field_type": "string",
            "link_template": "https://tracker.example.com/browse/${value}",
        },
    )
    assert meta.status_code == 201, meta.text
    meta_id = meta.json()["id"]

    created = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": main_type["id"],
            "name": "track:tap",
            "meta_values": [
                {
                    "meta_field_definition_id": meta_id,
                    "value": "https://tracker.example.com/browse/PROJ-4770",
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    assert [mv["value"] for mv in created.json()["meta_values"]] == ["PROJ-4770"]

    edited = await client.patch(
        f"/api/v1/projects/{slug}/events/{created.json()['id']}",
        json={
            "meta_values": [
                {"meta_field_definition_id": meta_id, "value": "https://elsewhere.example/PROJ-1"}
            ]
        },
    )
    assert edited.status_code == 200, edited.text
    assert [mv["value"] for mv in edited.json()["meta_values"]] == [
        "https://elsewhere.example/PROJ-1"
    ]
    async with TestSessionLocal() as session:
        stored = (
            await session.execute(
                select(EventMetaValue.value).where(
                    EventMetaValue.event_id == uuid.UUID(created.json()["id"])
                )
            )
        ).scalar_one()
    assert stored == "https://elsewhere.example/PROJ-1"


# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_reads_answer_across_branches_and_say_where_the_row_lives(
    client: AsyncClient,
) -> None:
    slug = "handoff-links"
    await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)
    branch_type = await _branch_type(client, slug, branch_id)
    created = await client.post(
        f"/api/v1/projects/{slug}/events?branch={branch_id}",
        json={"event_type_id": branch_type["id"], "name": "track:tap"},
    )
    event_id = created.json()["id"]
    assert created.json()["branch_id"] == branch_id

    without_branch = await client.get(f"/api/v1/projects/{slug}/events/{event_id}")
    assert without_branch.status_code == 200
    assert without_branch.json()["branch_id"] == branch_id
    assert (
        await client.get(f"/api/v1/projects/{slug}/events/{event_id}/history")
    ).status_code == 200

    # Writes stay strict: a PATCH aimed at main must not land on the branch row.
    patched = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}", json={"description": "x"}
    )
    assert patched.status_code == 404
    main_event = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"][0]
    branches = (await client.get(f"/api/v1/projects/{slug}/branches")).json()["items"]
    main_branch_id = next(b["id"] for b in branches if b["kind"] == "main")
    assert main_event["id"] != event_id
    seen_from_branch = await client.get(
        f"/api/v1/projects/{slug}/events/{main_event['id']}?branch={branch_id}"
    )
    assert seen_from_branch.status_code == 200
    assert seen_from_branch.json()["branch_id"] == main_branch_id


# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_branch_copy_reads_metrics_and_last_seen_from_its_main_twin(
    client: AsyncClient,
) -> None:
    slug = "handoff-twin"
    await _seed_plan(client, slug)
    main_event = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"][0]
    project_id = main_event["project_id"]
    # The branch is opened FIRST: the deep copy carries whatever main had seen
    # by then, and what matters is the traffic that lands on main afterwards.
    branch_id = await _create_branch(client, slug)
    seen = datetime(2026, 9, 1, 12, tzinfo=UTC)
    async with TestSessionLocal() as session, session.begin():
        await session.execute(
            sql_update(Event)
            .where(Event.id == uuid.UUID(main_event["id"]))
            .values(last_seen_at=seen)
        )
    await _seed_event_metrics_at(
        project_id,
        main_event["id"],
        name="twin scan",
        points=[
            (datetime(2026, 9, 1, 10, tzinfo=UTC), 4),
            (datetime(2026, 9, 1, 11, tzinfo=UTC), 6),
        ],
    )

    copies = (await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")).json()[
        "items"
    ]
    copy = next(e for e in copies if e["name"] == "purchase:success")
    assert copy["id"] != main_event["id"]
    assert copy["last_seen_at"] is not None
    assert datetime.fromisoformat(copy["last_seen_at"]).replace(tzinfo=UTC) == seen
    single = await client.get(f"/api/v1/projects/{slug}/events/{copy['id']}?branch={branch_id}")
    assert datetime.fromisoformat(single.json()["last_seen_at"]).replace(tzinfo=UTC) == seen
    async with TestSessionLocal() as session:
        stored = await session.get(Event, uuid.UUID(copy["id"]))
        assert stored is not None and stored.last_seen_at is None, "read-through must not write"

    window = await client.post(
        f"/api/v1/projects/{slug}/events/window-metrics",
        json={
            "event_ids": [copy["id"]],
            "time_from": "2026-09-01T09:00:00Z",
            "time_to": "2026-09-01T12:00:00Z",
        },
    )
    assert window.status_code == 200, window.text
    assert [(row["event_id"], row["total_count"]) for row in window.json()] == [(copy["id"], 10)]

    metrics = await client.get(
        f"/api/v1/projects/{slug}/events/{copy['id']}/metrics"
        "?time_from=2026-09-01T09:00:00Z&time_to=2026-09-01T12:00:00Z"
    )
    assert metrics.status_code == 200, metrics.text
    assert sum(point["count"] for point in metrics.json()["data"]) == 10


@pytest.mark.asyncio
async def test_history_records_creation_tags_fields_and_meta(client: AsyncClient) -> None:
    slug = "handoff-history"
    await _seed_plan(client, slug)
    main_type = await _main_type(client, slug)
    field_id = _field_id(main_type)
    meta_id = (
        await client.post(
            f"/api/v1/projects/{slug}/meta-fields",
            json={"name": "jira", "display_name": "Jira", "field_type": "string"},
        )
    ).json()["id"]
    created = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": main_type["id"],
            "name": "track:tap",
            "tags": ["mobile"],
            "field_values": [{"field_definition_id": field_id, "value": "tap"}],
            "meta_values": [{"meta_field_definition_id": meta_id, "value": "PROJ-1"}],
        },
    )
    event_id = created.json()["id"]
    edited = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}",
        json={
            "tags": ["mobile", "onboarding"],
            "field_values": [{"field_definition_id": field_id, "value": "swipe"}],
            "meta_values": [{"meta_field_definition_id": meta_id, "value": "PROJ-2"}],
        },
    )
    assert edited.status_code == 200, edited.text
    unchanged = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}",
        json={
            "tags": ["onboarding", "mobile"],
            "field_values": [{"field_definition_id": field_id, "value": "swipe"}],
        },
    )
    assert unchanged.status_code == 200

    history = (await client.get(f"/api/v1/projects/{slug}/events/{event_id}/history")).json()
    rows = {(row["field"], row["old_value"], row["new_value"]) for row in history}
    assert rows == {
        ("created", None, "track:tap"),
        ("tags", "mobile", "mobile, onboarding"),
        ("field:name", "tap", "swipe"),
        ("meta:jira", "PROJ-1", "PROJ-2"),
    }
    assert all(row["user_email"] == "test@example.com" for row in history)


@pytest.mark.asyncio
async def test_first_seen_is_the_oldest_bucket_with_traffic_read_through_the_twin(
    client: AsyncClient,
) -> None:
    """``first_seen_at`` is when traffic first arrived, not when the row was authored.

    The detail page labelled ``created_at`` "First seen", which for an event
    planned before it shipped named a day nothing was seen on.
    A branch copy reads its main twin's buckets, the same way ``last_seen_at``
    reaches it; an empty bucket does not count as being seen.
    """
    slug = "handoff-first-seen"
    await _seed_plan(client, slug)
    main_event = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"][0]
    branch_id = await _create_branch(client, slug)
    await _seed_event_metrics_at(
        main_event["project_id"],
        main_event["id"],
        name="first seen scan",
        points=[
            (datetime(2026, 9, 1, 9, tzinfo=UTC), 0),
            (datetime(2026, 9, 1, 10, tzinfo=UTC), 4),
            (datetime(2026, 9, 1, 11, tzinfo=UTC), 6),
        ],
    )
    first_traffic = datetime(2026, 9, 1, 10, tzinfo=UTC)

    on_main = await client.get(f"/api/v1/projects/{slug}/events/{main_event['id']}")
    assert on_main.status_code == 200, on_main.text
    assert datetime.fromisoformat(on_main.json()["first_seen_at"]).replace(tzinfo=UTC) == (
        first_traffic
    )

    copies = (await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")).json()[
        "items"
    ]
    copy = next(e for e in copies if e["name"] == "purchase:success")
    assert copy.get("first_seen_at") is None, "the list does not compute it"
    single = await client.get(f"/api/v1/projects/{slug}/events/{copy['id']}?branch={branch_id}")
    assert datetime.fromisoformat(single.json()["first_seen_at"]).replace(tzinfo=UTC) == (
        first_traffic
    )

    main_type = await _main_type(client, slug)
    unseen = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": main_type["id"], "name": "track:never_shipped"},
    )
    assert unseen.status_code == 201, unseen.text
    fresh = await client.get(f"/api/v1/projects/{slug}/events/{unseen.json()['id']}")
    assert fresh.json()["first_seen_at"] is None
    assert fresh.json()["created_at"] is not None
