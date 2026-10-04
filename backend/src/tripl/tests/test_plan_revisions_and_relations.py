"""Plan revisions and relations.

* a key two rows share no longer swallows a change whole. The
  deletion of a namesake that is not the one listed last used to produce no
  diff entry at all, and two namesakes listed in a different order on the two
  sides used to read as an edit of one into the other.
* the revision list counts a snapshot's entities in the
  database instead of dragging every whole-plan payload into the event loop.
* a relation can only name event types and fields that live in
  the project branch it is created on, so no row is left that makes every later
  branch creation for the project raise KeyError.
"""

import json
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tripl.models import Base
from tripl.models.plan_revision import PlanRevision
from tripl.models.project import Project
from tripl.services.plan_revision_service import (
    _UNATTRIBUTABLE_CHANGE_WARNING,
    compute_plan_diff_entries,
    list_revisions,
)
from tripl.tests.test_plan_branches import _create_branch
from tripl.tests.test_plan_diff_and_snapshots import _copy, _event_row, _payload, _relation_row
from tripl.tests.test_relations import _setup_relation

# --- a shared key must not swallow the change ------------------

#: The two entity kinds nothing makes unique — an event's (type, name) and the
#: two fields a relation links — each built as a row told from its namesake by
#: description alone.
_SHARED_KEY_ROWS = [
    pytest.param("events", lambda tag: _event_row(description=tag), id="event"),
    pytest.param("relations", lambda tag: _relation_row(description=tag), id="relation"),
]


@pytest.mark.parametrize(("collection", "row"), _SHARED_KEY_ROWS)
def test_deleting_the_namesake_that_is_not_listed_last_still_shows_on_the_diff(
    collection: str, row: Callable[[str], dict[str, Any]]
) -> None:
    """The silent half of the collapse, and the worst half.

    Each side keeps one row per key — the last one listed — so when the row
    that went is NOT the one listed last, the survivor sits in the slot the
    pair shared and the two representatives compare equal. The deletion then
    produced no entry, the branch read as ahead by nothing, and the
    shared-key warning went with it, since it rides on an entry.

    Reverting the fix drops the stand-in entry and this diff is empty again, so
    the unpacking below raises.
    """
    doomed, survivor = row("doomed"), row("survivor")

    (entry,) = compute_plan_diff_entries(
        _payload(**{collection: [doomed, survivor]}),
        _payload(**{collection: [_copy(survivor)]}),
    )

    assert entry.kind == "changed"
    # It stands in for the change; it does not claim to be it. The row that was
    # matched is not the row that went, so there is no field change to name.
    assert entry.changes == []
    assert entry.field_changes == []
    assert entry.before == entry.after
    assert _UNATTRIBUTABLE_CHANGE_WARNING in entry.warnings
    assert any(warning.startswith("More than one") for warning in entry.warnings)


@pytest.mark.parametrize(("collection", "row"), _SHARED_KEY_ROWS)
def test_namesakes_neither_side_touched_are_not_a_change_however_they_are_ordered(
    collection: str, row: Callable[[str], dict[str, Any]]
) -> None:
    """The other half: which namesake is the representative is not stable.

    ``build_plan_snapshot`` orders events by name alone, so two snapshots of
    the SAME two rows can list them in either order — and comparing the
    representatives then reads one namesake as an edit of the other. A project
    that merely HAS namesakes was ahead of, and behind, everything for ever.

    Reverting the fix compares 'second' against 'first' again and this diff
    carries a phantom ``changed`` entry.
    """
    first, second = row("first"), row("second")

    assert (
        compute_plan_diff_entries(
            _payload(**{collection: [first, second]}),
            _payload(**{collection: [_copy(second), _copy(first)]}),
        )
        == []
    )


# --- the list counts in the database ---------------------------

#: Sits inside a stored snapshot and nowhere else, so finding it in what the
#: engine deserialized means a whole payload crossed into Python.
_PAYLOAD_MARKER = "only-inside-the-stored-snapshot"

_FULL_PAYLOAD: dict[str, Any] = {
    "snapshot_version": 2,
    "event_types": [
        {"name": "track", "field_definitions": [{"name": "a"}, {"name": "b"}]},
        {"name": "screen", "field_definitions": [{"name": "c"}]},
    ],
    "events": [{"name": _PAYLOAD_MARKER}],
    "variables": [{"name": "city"}],
    "meta_fields": [],
    "relations": [{"relation_type": "belongs_to"}, {"relation_type": "belongs_to"}],
}

#: A revision written before those keys existed: every count but the first is
#: read off a key that is simply absent.
_LEGACY_PAYLOAD: dict[str, Any] = {"event_types": [{"name": "track"}]}


@pytest.mark.asyncio
async def test_listing_revisions_counts_entities_without_reading_a_payload() -> None:
    """The History tab's page must not drag whole plan snapshots into Python.

    ``payload`` is a plain JSON column with no deferral, a revision carrying a
    full snapshot is written at every branch creation, every merge and every
    manual snapshot, and the list shows nothing from it but six lengths.
    Selecting the ORM entity fetched all of them and json-decoded them on the
    event loop, a page at a time.

    Proved through the engine's ``json_deserializer``: every JSON column that
    reaches Python passes through it. Revert the fix — select ``PlanRevision``
    again — and the marker inside the stored snapshot turns up in ``decoded``.
    """
    decoded: list[str] = []

    def _spy(value: str) -> Any:
        decoded.append(value)
        return json.loads(value)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", json_deserializer=_spy)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_local = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_local() as session:
            project = Project(name="History", slug="rev-counts")
            session.add(project)
            await session.flush()
            session.add_all(
                [
                    PlanRevision(
                        project_id=project.id,
                        summary="full",
                        payload=_FULL_PAYLOAD,
                        created_at=datetime(2026, 9, 20, 12, 0, 0),
                    ),
                    PlanRevision(
                        project_id=project.id,
                        summary="legacy",
                        payload=_LEGACY_PAYLOAD,
                        created_at=datetime(2026, 9, 19, 12, 0, 0),
                    ),
                ]
            )
            await session.commit()
            decoded.clear()

            listing = await list_revisions(session, "rev-counts")
            counted = list(decoded)

            # The instrument itself: loading a revision the ordinary way DOES
            # put its payload through the spy, so an empty ``counted`` is a
            # fact about the list query and not about a spy that never fires.
            async with session_local() as reader:
                assert await reader.scalar(
                    select(PlanRevision).where(PlanRevision.summary == "full")
                )
            control = list(decoded)
    finally:
        await engine.dispose()

    assert listing.total == 2
    assert [item.summary for item in listing.items] == ["full", "legacy"]
    assert listing.items[0].entity_counts == {
        "event_types": 2,
        "fields": 3,
        "events": 1,
        "variables": 1,
        "meta_fields": 0,
        "relations": 2,
    }
    # A snapshot older than the keys counts 0 for them, exactly as taking
    # ``len()`` of a missing list in Python did.
    assert listing.items[1].entity_counts == {
        "event_types": 1,
        "fields": 0,
        "events": 0,
        "variables": 0,
        "meta_fields": 0,
        "relations": 0,
    }
    assert [text for text in counted if _PAYLOAD_MARKER in text] == []
    assert [text for text in control if _PAYLOAD_MARKER in text] != []


# --- a relation's ends must live in the branch it is made on ---


@pytest.mark.asyncio
async def test_a_relation_whose_field_belongs_to_another_event_type_is_refused(
    client: AsyncClient,
) -> None:
    """Nothing tied ``source_field_id`` to ``source_event_type_id``.

    Both rows exist, so both foreign keys are satisfied, and the row is stored.
    ``deep_copy_plan_to_branch`` then maps the field through the type it was
    filed under and gets a stranger back; the merge's relation key reads the
    same pair. Reverting the fix answers 201 and stores the row.
    """
    slug = "rel-wrong-field"
    pv_id, se_id, pv_field_id, _se_field_id = await _setup_relation(client, slug)

    resp = await client.post(
        f"/api/v1/projects/{slug}/relations",
        json={
            "source_event_type_id": se_id,
            "target_event_type_id": pv_id,
            # pv's field, filed as se's.
            "source_field_id": pv_field_id,
            "target_field_id": pv_field_id,
        },
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "source_field_id is not a field of source_event_type_id"
    assert (await client.get(f"/api/v1/projects/{slug}/relations")).json() == []


@pytest.mark.asyncio
async def test_a_relation_naming_another_projects_event_type_is_refused(
    client: AsyncClient,
) -> None:
    """The four ids are whatever the client sent; only the FKs read them, and
    they are satisfied by a row in ANY project. One such row makes every later
    ``create_branch`` for this project raise KeyError and 500 until it is
    deleted. Reverting the fix answers 201 and stores the row."""
    slug = "rel-own"
    pv_id, _se_id, pv_field_id, _se_field_id = await _setup_relation(client, slug)
    _other_pv, other_se, _other_pv_field, other_se_field = await _setup_relation(
        client, "rel-foreign"
    )

    resp = await client.post(
        f"/api/v1/projects/{slug}/relations",
        json={
            "source_event_type_id": other_se,
            "target_event_type_id": pv_id,
            "source_field_id": other_se_field,
            "target_field_id": pv_field_id,
        },
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == (
        "source_event_type_id is not an event type in this project branch"
    )
    assert (await client.get(f"/api/v1/projects/{slug}/relations")).json() == []


@pytest.mark.asyncio
async def test_a_relation_made_on_a_branch_must_name_the_branchs_own_copies(
    client: AsyncClient,
) -> None:
    """A branch has its own copies of every type and field, under new ids.

    A branch relation pointing at main's ids is the merge's half of the same
    hole: ``branch_relation_by_key`` looks the ids up among the branch's rows
    and finds nothing. The second half of this test is the control — the same
    request built from the branch's own copies is still accepted — so the
    refusal is about the mismatch and not about branches. Reverting the fix
    answers 201 to the first request.
    """
    slug = "rel-branch"
    main_pv, main_se, main_pv_field, main_se_field = await _setup_relation(client, slug)
    branch_id = await _create_branch(client, slug)

    naming_main = await client.post(
        f"/api/v1/projects/{slug}/relations?branch={branch_id}",
        json={
            "source_event_type_id": main_se,
            "target_event_type_id": main_pv,
            "source_field_id": main_se_field,
            "target_field_id": main_pv_field,
        },
    )
    assert naming_main.status_code == 422, naming_main.text
    assert naming_main.json()["detail"] == (
        "source_event_type_id is not an event type in this project branch"
    )

    copies = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    by_name = {et["name"]: et for et in copies.json()}
    assert set(by_name) == {"pv", "se"}
    assert uuid.UUID(by_name["se"]["id"]) != uuid.UUID(main_se)

    accepted = await client.post(
        f"/api/v1/projects/{slug}/relations?branch={branch_id}",
        json={
            "source_event_type_id": by_name["se"]["id"],
            "target_event_type_id": by_name["pv"]["id"],
            "source_field_id": by_name["se"]["field_definitions"][0]["id"],
            "target_field_id": by_name["pv"]["field_definitions"][0]["id"],
        },
    )
    assert accepted.status_code == 201, accepted.text
