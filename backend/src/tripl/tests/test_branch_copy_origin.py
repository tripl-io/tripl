"""Branch copies record their origin, so namesakes are told apart.

Two events may share (type, name) and two relations may link the same two
fields — nothing forbids it, and production holds such pairs. Every branch path
paired rows by that natural key, one row per key, so the pair collapsed to
whichever row a dict kept: an earlier review found 21 defects of that one root. A branch
copy now records the main row it was made from (``origin_id``) and the diff,
the conflict scan, the merge, a revert and the discussion twin pair by it.

One scenario per defect class that review listed, each built on two namesakes, each
arranged so the natural-key pairing gets it WRONG — acting on the namesake a
key-keyed dict does not keep (the last one listed), or on the one the old twin
rule did not pick (the lowest id) — so reverting to that pairing fails it.
The remaining half of that fix is the first two tests.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy import select, update

from tripl.models.event import Event
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.models.event_type import EventType
from tripl.models.event_type_relation import EventTypeRelation
from tripl.models.field_definition import FieldDefinition
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.project import Project
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.services._origin_pairing import pair_rows
from tripl.services.plan_revision_service import build_plan_snapshot, plan_snapshot_hash
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_plan_branches import _approve_and_merge, _create_branch, _seed_plan

# --- helpers --------------------------------------------------------------------


async def _post_event(
    client: AsyncClient,
    slug: str,
    event_type_id: str,
    name: str,
    *,
    description: str = "",
    branch_id: str | None = None,
) -> str:
    where = f"?branch={branch_id}" if branch_id else ""
    resp = await client.post(
        f"/api/v1/projects/{slug}/events{where}",
        json={"event_type_id": event_type_id, "name": name, "description": description},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _namesakes(client: AsyncClient, slug: str) -> tuple[str, str, str]:
    """Main holds two ``track/dup`` rows, "first" inserted before "second".

    ``track`` has no scan name template, so neither carries a scan identity and
    nothing refuses the second name. Returns (type id, first id, second id).
    """
    et_id = await _seed_plan(client, slug)
    first = await _post_event(client, slug, et_id, "dup", description="first")
    second = await _post_event(client, slug, et_id, "dup", description="second")
    return et_id, first, second


async def _copies(branch_id: str) -> dict[str, str]:
    """Main row id -> the branch copy made from it."""
    async with TestSessionLocal() as session:
        rows = (
            await session.execute(
                select(Event.origin_id, Event.id).where(
                    Event.branch_id == uuid.UUID(branch_id), Event.origin_id.is_not(None)
                )
            )
        ).all()
    return {str(origin): str(row_id) for origin, row_id in rows}


async def _event(event_id: str) -> Event | None:
    async with TestSessionLocal() as session:
        return await session.get(Event, uuid.UUID(event_id))


async def _patch(
    client: AsyncClient, slug: str, event_id: str, branch_id: str, **body: Any
) -> None:
    resp = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}?branch={branch_id}", json=body
    )
    assert resp.status_code == 200, resp.text


async def _diff_entries(client: AsyncClient, slug: str, branch_id: str) -> list[dict[str, Any]]:
    resp = await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")
    assert resp.status_code == 200, resp.text
    return [e for e in resp.json()["entries"] if e["entity_type"] == "event"]


async def _merged(client: AsyncClient, slug: str, branch_id: str) -> None:
    resp = await _approve_and_merge(client, slug, branch_id)
    assert resp.status_code == 200, resp.text


async def _main_branch_of(slug: str) -> uuid.UUID:
    async with TestSessionLocal() as session:
        branch_id = await session.scalar(
            select(PlanBranch.id)
            .join(Project, Project.id == PlanBranch.project_id)
            .where(Project.slug == slug, PlanBranch.kind == BranchKind.main.value)
        )
    assert branch_id is not None
    return branch_id


# --- the merge's own half ----------------------------------------


@pytest.mark.asyncio
async def test_merge_deletes_exactly_the_namesake_whose_copy_was_deleted(
    client: AsyncClient,
) -> None:
    """Main kept BOTH namesakes after a merge that deleted one: a row was
    deleted only when its NAME vanished from the branch."""
    slug = "origin-merge-delete"
    _, first, second = await _namesakes(client, slug)
    branch_id = await _create_branch(client, slug)
    copies = await _copies(branch_id)
    deleted = await client.delete(
        f"/api/v1/projects/{slug}/events/{copies[first]}?branch={branch_id}"
    )
    assert deleted.status_code == 204

    await _merged(client, slug, branch_id)
    assert await _event(first) is None
    kept = await _event(second)
    assert kept is not None and kept.description == "second"


@pytest.mark.asyncio
async def test_merge_writes_each_copys_edits_onto_its_own_origin(client: AsyncClient) -> None:
    """Whichever copy a dict kept used to be written onto whichever main row
    it kept — one edit lost, and it could land on the other namesake."""
    slug = "origin-merge-edits"
    _, first, second = await _namesakes(client, slug)
    branch_id = await _create_branch(client, slug)
    copies = await _copies(branch_id)
    await _patch(client, slug, copies[first], branch_id, description="first, edited")

    await _merged(client, slug, branch_id)
    first_row, second_row = await _event(first), await _event(second)
    assert first_row is not None and first_row.description == "first, edited"
    assert second_row is not None and second_row.description == "second"


# --- the rest of the defect list -----------------------------------------


@pytest.mark.asyncio
async def test_merge_moves_an_override_onto_the_namesake_its_event_came_from(
    client: AsyncClient,
) -> None:
    """Override mapping: the override arm mapped each override's event through
    a one-row-per-key map and put it on the namesake that map kept."""
    slug = "origin-merge-override"
    _, first, second = await _namesakes(client, slug)
    created = await client.post(
        f"/api/v1/projects/{slug}/variables", json={"name": "plan", "variable_type": "string"}
    )
    assert created.status_code == 201, created.text
    branch_id = await _create_branch(client, slug)
    copies = await _copies(branch_id)
    listed = await client.get(f"/api/v1/projects/{slug}/variables?branch={branch_id}")
    items = listed.json()["items"] if isinstance(listed.json(), dict) else listed.json()
    branch_variable = next(v["id"] for v in items if v["name"] == "plan")
    put = await client.put(
        f"/api/v1/projects/{slug}/variables/{branch_variable}/event-overrides/"
        f"{copies[first]}?branch={branch_id}",
        json={"values": ["gold"]},
    )
    assert put.status_code == 200, put.text

    await _merged(client, slug, branch_id)
    main_branch_id = await _main_branch_of(slug)
    async with TestSessionLocal() as session:
        overrides = (
            await session.execute(
                select(
                    VariableEventValueOverride.event_id, VariableEventValueOverride.values
                ).where(VariableEventValueOverride.branch_id == main_branch_id)
            )
        ).all()
    assert [(str(event_id), values) for event_id, values in overrides] == [(first, ["gold"])]
    assert second not in {str(event_id) for event_id, _ in overrides}


@pytest.mark.asyncio
async def test_merge_resolves_a_successor_to_the_namesake_the_copy_points_at(
    client: AsyncClient,
) -> None:
    """Successor pointers: a successor with a namesake resolved last-wins."""
    slug = "origin-merge-successor"
    et_id, first, _second = await _namesakes(client, slug)
    legacy = await _post_event(client, slug, et_id, "legacy")
    branch_id = await _create_branch(client, slug)
    copies = await _copies(branch_id)
    await _patch(client, slug, copies[legacy], branch_id, superseded_by_event_id=copies[first])

    await _merged(client, slug, branch_id)
    legacy_row = await _event(legacy)
    assert legacy_row is not None
    assert str(legacy_row.superseded_by_event_id) == first


@pytest.mark.asyncio
async def test_merge_carries_an_order_change_to_its_own_namesake(client: AsyncClient) -> None:
    """Order changes on namesakes were lost or landed on the other row."""
    slug = "origin-merge-order"
    _, first, second = await _namesakes(client, slug)
    second_order = (await _event(second)).order  # type: ignore[union-attr]
    branch_id = await _create_branch(client, slug)
    copies = await _copies(branch_id)
    async with TestSessionLocal() as session:
        await session.execute(
            update(Event).where(Event.id == uuid.UUID(copies[first])).values(order=42)
        )
        await session.commit()

    await _merged(client, slug, branch_id)
    assert (await _event(first)).order == 42  # type: ignore[union-attr]
    assert (await _event(second)).order == second_order  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_merge_renames_the_namesake_the_branch_renamed(client: AsyncClient) -> None:
    """Rename pairing: ``pair_renames`` sees one row per key and needs a scan
    identity, so renaming one namesake merged as a third row beside both."""
    slug = "origin-merge-rename"
    _, first, second = await _namesakes(client, slug)
    branch_id = await _create_branch(client, slug)
    copies = await _copies(branch_id)
    await _patch(client, slug, copies[first], branch_id, name="dup_retired")

    await _merged(client, slug, branch_id)
    main_branch_id = await _main_branch_of(slug)
    async with TestSessionLocal() as session:
        rows = (
            await session.execute(
                select(Event.id, Event.name).where(
                    Event.branch_id == main_branch_id, Event.name.in_(["dup", "dup_retired"])
                )
            )
        ).all()
    assert sorted((str(row_id), name) for row_id, name in rows) == sorted(
        [(first, "dup_retired"), (second, "dup")]
    )


@pytest.mark.asyncio
async def test_snapshot_orders_namesakes_by_id_and_the_hash_ignores_origin_ids(
    client: AsyncClient,
) -> None:
    """Approval-hash tie order: namesakes serialized in database order, so an
    approval hash could move with no edit. And ``origin_id`` is bookkeeping the
    migration backfilled onto approved branches — hashed, it would void them."""
    slug = "origin-snapshot-order"
    et_id = await _seed_plan(client, slug)
    async with TestSessionLocal() as session:
        event_type = await session.get(EventType, uuid.UUID(et_id))
        assert event_type is not None
        # Inserted highest id first, so database order and id order disagree.
        for row_id in (
            "ffffffff-0000-4000-8000-000000000000",
            "00000000-0000-4000-8000-000000000000",
        ):
            session.add(
                Event(
                    id=uuid.UUID(row_id),
                    project_id=event_type.project_id,
                    branch_id=event_type.branch_id,
                    event_type_id=event_type.id,
                    name="dup",
                )
            )
        await session.commit()
        project_id = event_type.project_id
    branch_id = await _create_branch(client, slug)

    async with TestSessionLocal() as session:
        main_snapshot = await build_plan_snapshot(session, project_id)
        branch_snapshot = await build_plan_snapshot(session, project_id, uuid.UUID(branch_id))
    main_dups = [e["id"] for e in main_snapshot["events"] if e["name"] == "dup"]
    assert main_dups == sorted(main_dups)
    branch_dups = [e for e in branch_snapshot["events"] if e["name"] == "dup"]
    assert [e["id"] for e in branch_dups] == sorted(e["id"] for e in branch_dups)
    assert {e["origin_id"] for e in branch_dups} == set(main_dups)
    # Main snapshots, and so every stored base, carry no such key.
    assert all("origin_id" not in e for e in main_snapshot["events"])
    stripped = {
        **branch_snapshot,
        "events": [
            {k: v for k, v in e.items() if k != "origin_id"} for e in branch_snapshot["events"]
        ],
    }
    assert plan_snapshot_hash(branch_snapshot) == plan_snapshot_hash(stripped)


@pytest.mark.asyncio
async def test_diff_enters_a_deleted_and_an_edited_namesake_separately(
    client: AsyncClient,
) -> None:
    """Keyed one row per key, the diff read one namesake's deletion as an edit
    to the other, or showed a stand-in that could not say which was which."""
    slug = "origin-diff"
    _, first, second = await _namesakes(client, slug)
    branch_id = await _create_branch(client, slug)
    copies = await _copies(branch_id)
    await client.delete(f"/api/v1/projects/{slug}/events/{copies[first]}?branch={branch_id}")
    await _patch(client, slug, copies[second], branch_id, description="second, edited")

    entries = {e["kind"]: e for e in await _diff_entries(client, slug, branch_id)}
    assert set(entries) == {"removed", "changed"}
    assert entries["removed"]["entity_id"] == first
    assert entries["changed"]["entity_id"] == copies[second]
    assert [c["before"] for c in entries["changed"]["field_changes"]] == ["second"]
    assert all(entry["warnings"] == [] for entry in entries.values())


@pytest.mark.asyncio
async def test_revert_of_an_added_namesake_deletes_exactly_that_row(
    client: AsyncClient,
) -> None:
    """An added namesake could not be reverted: the name named several rows."""
    slug = "origin-revert-added"
    _, first, _second = await _namesakes(client, slug)
    branch_id = await _create_branch(client, slug)
    async with TestSessionLocal() as session:
        branch_type = await session.scalar(
            select(EventType.id).where(
                EventType.branch_id == uuid.UUID(branch_id), EventType.name == "track"
            )
        )
    added = await _post_event(
        client, slug, str(branch_type), "dup", description="third", branch_id=branch_id
    )
    [entry] = await _diff_entries(client, slug, branch_id)
    assert (entry["kind"], entry["entity_id"]) == ("added", added)

    resp = await client.post(
        f"/api/v1/projects/{slug}/branches/{branch_id}/revert",
        json={"entity_type": "event", "name": "dup", "parent": "track", "entity_id": added},
    )
    assert resp.status_code == 200, resp.text
    assert await _event(added) is None
    assert set(await _copies(branch_id)) >= {first}


@pytest.mark.asyncio
async def test_revert_restores_an_override_onto_the_copy_of_its_base_event(
    client: AsyncClient,
) -> None:
    """Override restore: with a namesake authored on the branch the restore
    refused as ambiguous — it asked the branch side by name only. The base
    names one event, and its copy is found by origin."""
    slug = "origin-revert-override"
    et_id = await _seed_plan(client, slug)
    tap = await _post_event(client, slug, et_id, "tap")
    created = await client.post(
        f"/api/v1/projects/{slug}/variables", json={"name": "plan", "variable_type": "string"}
    )
    assert created.status_code == 201, created.text
    put = await client.put(
        f"/api/v1/projects/{slug}/variables/{created.json()['id']}/event-overrides/{tap}",
        json={"values": ["gold"]},
    )
    assert put.status_code == 200, put.text
    branch_id = await _create_branch(client, slug)
    copies = await _copies(branch_id)
    async with TestSessionLocal() as session:
        branch_type = await session.scalar(
            select(EventType.id).where(
                EventType.branch_id == uuid.UUID(branch_id), EventType.name == "track"
            )
        )
        branch_variable = await session.scalar(
            select(VariableEventValueOverride.variable_id).where(
                VariableEventValueOverride.branch_id == uuid.UUID(branch_id)
            )
        )
    await _post_event(client, slug, str(branch_type), "tap", branch_id=branch_id)
    changed = await client.put(
        f"/api/v1/projects/{slug}/variables/{branch_variable}/event-overrides/"
        f"{copies[tap]}?branch={branch_id}",
        json={"values": ["silver"]},
    )
    assert changed.status_code == 200, changed.text

    resp = await client.post(
        f"/api/v1/projects/{slug}/branches/{branch_id}/revert",
        json={"entity_type": "variable", "name": "plan", "field": "event_value_overrides"},
    )
    assert resp.status_code == 200, resp.text
    async with TestSessionLocal() as session:
        restored = (
            await session.execute(
                select(
                    VariableEventValueOverride.event_id, VariableEventValueOverride.values
                ).where(VariableEventValueOverride.branch_id == uuid.UUID(branch_id))
            )
        ).all()
    assert [(str(event_id), values) for event_id, values in restored] == [(copies[tap], ["gold"])]


@pytest.mark.asyncio
async def test_open_questions_filter_matches_only_the_copy_of_the_asked_namesake(
    client: AsyncClient,
) -> None:
    """The open-questions filter matched a branch row through ANY main row
    sharing its identity, so both copies listed for one question."""
    slug = "origin-open-questions"
    _, first, _second = await _namesakes(client, slug)
    asked = await client.post(
        f"/api/v1/projects/{slug}/events/{first}/comments", json={"body": "still sent?"}
    )
    assert asked.status_code == 201, asked.text
    branch_id = await _create_branch(client, slug)
    copies = await _copies(branch_id)

    listed = await client.get(
        f"/api/v1/projects/{slug}/events",
        params={"branch": branch_id, "has_open_questions": "true"},
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [copies[first]]


@pytest.mark.asyncio
async def test_a_copy_reads_and_merges_the_discussion_of_its_own_namesake(
    client: AsyncClient,
) -> None:
    """Thread moves: ``main_counterparts`` gave every copy the lowest-id main
    namesake as its twin, so one copy read — and the merge moved its thread
    onto — the other namesake's discussion."""
    slug = "origin-thread"
    _, first, second = await _namesakes(client, slug)
    higher = max(first, second, key=uuid.UUID)
    asked = await client.post(
        f"/api/v1/projects/{slug}/events/{higher}/comments", json={"body": "about this one"}
    )
    assert asked.status_code == 201, asked.text
    branch_id = await _create_branch(client, slug)
    copies = await _copies(branch_id)

    through_copy = await client.get(
        f"/api/v1/projects/{slug}/events/{copies[higher]}/comments?branch={branch_id}"
    )
    assert through_copy.status_code == 200, through_copy.text
    assert [row["id"] for row in through_copy.json()] == [asked.json()["id"]]

    # A thread hanging on the copy's own row (started before any twin existed)
    # is moved at merge onto the namesake the copy came from.
    async with TestSessionLocal() as session:
        own = EventPhotoComment(
            id=uuid.uuid4(), event_id=uuid.UUID(copies[higher]), user_id=None, body="own row"
        )
        session.add(own)
        await session.commit()
        own_id = own.id
    await _merged(client, slug, branch_id)
    async with TestSessionLocal() as session:
        moved = await session.get(EventPhotoComment, own_id)
    assert moved is not None and str(moved.event_id) == higher


@pytest.mark.asyncio
async def test_conflict_scan_pairs_namesakes_by_origin(client: AsyncClient) -> None:
    """Keyed one row per key, main's edit to one namesake and the branch's edit
    to the SAME one compared two different rows and passed, and the merge then
    wrote the branch's value over main's."""
    slug = "origin-conflicts"
    _, first, _second = await _namesakes(client, slug)
    branch_id = await _create_branch(client, slug)
    copies = await _copies(branch_id)
    main_edit = await client.patch(
        f"/api/v1/projects/{slug}/events/{first}", json={"description": "main's"}
    )
    assert main_edit.status_code == 200, main_edit.text
    await _patch(client, slug, copies[first], branch_id, description="branch's")

    resp = await _approve_and_merge(client, slug, branch_id)
    assert resp.status_code == 409, resp.text
    assert "conflicts" in resp.json()["detail"]
    assert (await _event(first)).description == "main's"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_merge_deletes_exactly_the_relation_namesake_whose_copy_was_deleted(
    client: AsyncClient,
) -> None:
    """Relations share the same root: nothing makes their four names unique."""
    slug = "origin-relations"
    et_id = await _seed_plan(client, slug)
    async with TestSessionLocal() as session:
        event_type = await session.get(EventType, uuid.UUID(et_id))
        assert event_type is not None
        field_id = await session.scalar(
            select(FieldDefinition.id).where(FieldDefinition.event_type_id == event_type.id)
        )
        relation_ids = [uuid.uuid4(), uuid.uuid4()]
        for relation_id, description in zip(relation_ids, ("first", "second"), strict=True):
            session.add(
                EventTypeRelation(
                    id=relation_id,
                    project_id=event_type.project_id,
                    branch_id=event_type.branch_id,
                    source_event_type_id=event_type.id,
                    target_event_type_id=event_type.id,
                    source_field_id=field_id,
                    target_field_id=field_id,
                    description=description,
                )
            )
        await session.commit()
    branch_id = await _create_branch(client, slug)
    async with TestSessionLocal() as session:
        await session.execute(
            sa.delete(EventTypeRelation).where(EventTypeRelation.origin_id == relation_ids[0])
        )
        await session.commit()

    await _merged(client, slug, branch_id)
    async with TestSessionLocal() as session:
        left = (
            (
                await session.execute(
                    select(EventTypeRelation.id).where(EventTypeRelation.id.in_(relation_ids))
                )
            )
            .scalars()
            .all()
        )
    assert left == [relation_ids[1]]


# --- the pure pairing and the migration's backfill -------------------------------


def test_pair_rows_places_by_id_then_by_the_only_row_under_a_key() -> None:
    old = [{"id": "a", "k": "dup"}, {"id": "b", "k": "dup"}, {"id": "c", "k": "solo"}]
    new = [
        {"id": "x", "origin_id": "b", "k": "dup"},
        {"id": "y", "k": "solo"},
        {"id": "z", "k": "fresh"},
    ]
    pairing = pair_rows(
        old,
        new,
        key_of_old=lambda item: item["k"],
        key_of_new=lambda item: item["k"],
        id_of_old=lambda item: item["id"],
        ref_of_new=lambda item: item.get("origin_id") or item["id"],
    )
    assert [(o["id"], n["id"]) for o, n in pairing.pairs] == [("b", "x"), ("c", "y")]
    assert [o["id"] for o in pairing.removed] == ["a"]
    assert [n["id"] for n in pairing.added] == ["z"]


@pytest.mark.parametrize("complete", [False, True])
def test_pair_rows_leaves_several_unplaced_namesakes_ambiguous_unless_origins_are_complete(
    complete: bool,
) -> None:
    old = [{"id": "a", "k": "dup"}, {"id": "b", "k": "dup"}]
    new = [{"id": "x", "k": "dup"}]
    pairing = pair_rows(
        old,
        new,
        key_of_old=lambda item: item["k"],
        key_of_new=lambda item: item["k"],
        id_of_old=lambda item: item["id"],
        ref_of_new=lambda item: item.get("origin_id") or item["id"],
        unplaced_are_new=complete,
    )
    if complete:
        assert ([o["id"] for o in pairing.removed], [n["id"] for n in pairing.added]) == (
            ["a", "b"],
            ["x"],
        )
        assert pairing.ambiguous == {}
    else:
        assert list(pairing.ambiguous) == ["dup"]


async def _add_a_twin_of_the_copy(
    client: AsyncClient, slug: str, branch_id: str, branch_et_id: str, name: str
) -> None:
    """Add on the branch a namesake of its one copy named ``name``, equal to
    it in every field the conflict scan and the merge compare."""
    twin_id = await _post_event(client, slug, branch_et_id, name, branch_id=branch_id)
    async with TestSessionLocal() as session:
        copy = await session.scalar(
            select(Event).where(
                Event.branch_id == uuid.UUID(branch_id),
                Event.name == name,
                Event.id != uuid.UUID(twin_id),
            )
        )
        assert copy is not None
        await session.execute(
            update(Event)
            .where(Event.id == uuid.UUID(twin_id))
            .values(
                {
                    attr: getattr(copy, attr)
                    for attr in (
                        "source_name",
                        "title",
                        "description",
                        "status",
                        "sunset_at",
                        "order",
                        "owner_id",
                        "reviewed",
                        "metric_breakdown_columns",
                    )
                }
            )
        )
        await session.commit()


async def _branch_type_id(branch_id: str) -> str:
    async with TestSessionLocal() as session:
        type_id = await session.scalar(
            select(EventType.id).where(
                EventType.branch_id == uuid.UUID(branch_id), EventType.name == "track"
            )
        )
    assert type_id is not None
    return str(type_id)
