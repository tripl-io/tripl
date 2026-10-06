"""Catalog order is main's without asking, and conflict choices go in one batch.

Two halves of one complaint: an event authored on a branch while a scan made
its twin on main came back as eight rows to click through one at a time, one
of them its catalog position. ``order`` is now a main-wins field
(``MAIN_WINS_FIELDS``) everywhere a both-sides change would have become a
conflict — the merge's detection, the three-way plan, the merge's apply — and
``POST /resolutions/batch`` stores a "for all" action's choices in one call.

The pure half reuses the hand-built snapshots of
``test_plan_branch_conflicts_all_entities``; the endpoint half the seeding of
``test_plan_branch_update_from_main``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select

from tripl.api.deps import get_current_user
from tripl.main import app
from tripl.models.audit_log import AuditLog
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.meta_field_definition import MetaFieldDefinition
from tripl.models.plan_branch_merge_resolution import PlanBranchMergeResolution
from tripl.models.user import User
from tripl.services._plan_branch_three_way import plan_three_way
from tripl.services._plan_branch_three_way_model import PRESENCE_FIELD, ThreeWay
from tripl.services.plan_branch_conflicts import (
    MAIN_WINS_FIELDS,
    _detect_merge_conflicts,
    conflicts_response,
    detect_field_conflicts,
    main_keeps,
    merge_blocked_by,
)
from tripl.tests._members import persisted_member_user
from tripl.tests.conftest import TestSessionLocal, engine
from tripl.tests.test_plan_branch_conflicts_all_entities import _event, _sides
from tripl.tests.test_plan_branch_update_from_main import (
    EVENT,
    _approve_and_merge,
    _count,
    _edit,
    _ids,
    _one,
    _seed,
    _url,
)

# --- the rule ----------------------------------------------------------------


def test_only_order_is_main_wins() -> None:
    assert frozenset({"order"}) == MAIN_WINS_FIELDS


@pytest.mark.parametrize(
    ("field", "base_item", "main_value", "kept"),
    [
        # Main moved it too: main's stands.
        ("order", {"order": 1}, 2, True),
        # Main left it where the base had it: the branch's move lands.
        ("order", {"order": 1}, 1, False),
        # Added on both sides: main's copy keeps its place.
        ("order", None, 5, True),
        # Every other field is the conflict check's to decide.
        ("description", {"description": "a"}, "b", False),
        ("description", None, "b", False),
    ],
)
def test_main_keeps(
    field: str, base_item: dict[str, Any] | None, main_value: Any, kept: bool
) -> None:
    assert main_keeps(field, base_item, main_value) is kept


# --- detection and the three-way plan (pure) -----------------------------------


def _reorder(payload: dict[str, Any], entity_type: str, value: int) -> None:
    if entity_type == "event":
        payload["events"][0]["order"] = value
    elif entity_type == "event_type":
        payload["event_types"][0]["order"] = value
    elif entity_type == "field_definition":
        payload["event_types"][0]["field_definitions"][0]["order"] = value
    else:
        payload["meta_fields"][0]["order"] = value


def _order_writes(plan: ThreeWay, entity_type: str) -> list[Any]:
    return [
        (op.main or {}).get("order")
        for op in plan.ops
        if op.kind == "write" and op.entity_type == entity_type and "order" in op.fields
    ]


@pytest.mark.parametrize("entity_type", ["event", "event_type", "field_definition", "meta_field"])
def test_both_sides_reordering_is_no_conflict_and_main_wins(entity_type: str) -> None:
    base, main, branch = _sides()
    _reorder(base, entity_type, 1)
    _reorder(main, entity_type, 2)
    _reorder(branch, entity_type, 3)

    rows = detect_field_conflicts(base, main, branch, origins_complete=True)

    assert [row for row in rows if row["field"] == "order"] == []
    assert merge_blocked_by(base, main, branch, origins_complete=True) is False
    assert _detect_merge_conflicts(base, main, branch, theirs_origins_complete=True) == []
    # "Update from main" copies main's position onto the branch.
    plan = plan_three_way(base, main, branch, origins_complete=True)
    assert plan.unresolved == []
    assert _order_writes(plan, entity_type) == [2]


def _production_twins() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """pv.onboarding/starting_place: authored on the branch, scanned on main."""
    base, main, branch = _sides()
    main["events"].append(
        _event(
            "ev-scan",
            "starting_place",
            description="Seen in the onboarding flow",
            status="active",
            order=3186,
            field_values=[{"field_name": "name", "value": "scan"}],
        )
    )
    branch["events"].append(
        _event(
            "b-ev-new",
            "starting_place",
            title="Starting place",
            status="draft",
            order=3257,
            owner_id="user-1",
            field_values=[{"field_name": "name", "value": "authored"}],
            meta_values=[{"meta_field_name": "team", "value": "growth"}],
            tags=["onboarding"],
        )
    )
    return base, main, branch


def test_production_twin_lists_seven_rows_and_no_order() -> None:
    base, main, branch = _production_twins()

    rows = [
        row
        for row in detect_field_conflicts(base, main, branch, origins_complete=True)
        if row["name"] == "track.starting_place"
    ]

    assert sorted(row["field"] for row in rows) == sorted(
        [
            "title",
            "description",
            "status",
            "owner_id",
            "field_values",
            "meta_values",
            "tags",
        ]
    )
    assert all(row["base"] is None for row in rows)

    resolutions = {("event", "track.starting_place", row["field"]): "theirs" for row in rows}
    resolutions[("event", "track.starting_place", "description")] = "ours"
    plan = plan_three_way(base, main, branch, origins_complete=True, resolutions=resolutions)
    assert plan.unresolved == []
    [write] = [
        op
        for op in plan.ops
        if op.kind == "write"
        and op.entity_type == "event"
        and (op.branch or {}).get("id") == "b-ev-new"
    ]
    assert set(write.fields) == {"description", "order"}
    assert write.main is not None and write.main["order"] == 3186


def test_added_on_both_marks_the_twin_and_not_an_empty_base_value() -> None:
    base, main, branch = _production_twins()
    # The base event had no owner: this row's base is None too.
    main["events"][0]["owner_id"] = "user-main"
    branch["events"][0]["owner_id"] = "user-branch"

    response = conflicts_response(
        detect_field_conflicts(base, main, branch, origins_complete=True),
        {},
        behind=True,
        merge_blocked=True,
    )

    flags = {entity.name: entity.added_on_both for entity in response.entities}
    assert flags == {"track.starting_place": True, "track.purchase": False}


def test_twins_differing_only_in_order_are_one_event_in_main_s_place() -> None:
    base, main, branch = _sides()
    main["events"].append(_event("ev-2", "signup", order=20))
    branch["events"].append(_event("b-ev-2", "signup", order=10))

    assert detect_field_conflicts(base, main, branch, origins_complete=True) == []
    assert merge_blocked_by(base, main, branch, origins_complete=True) is False
    plan = plan_three_way(base, main, branch, origins_complete=True)
    assert _order_writes(plan, "event") == [20]


def test_a_one_sided_move_still_lands() -> None:
    base, main, branch = _sides()
    branch["events"][0]["order"] = 7

    # Branch only: the update leaves the branch's position alone, the merge
    # has nothing to refuse.
    plan = plan_three_way(base, main, branch, origins_complete=True)
    assert _order_writes(plan, "event") == []
    assert _detect_merge_conflicts(base, main, branch, theirs_origins_complete=True) == []

    # Main only: the update brings main's, and it counts as main's change.
    base, main, branch = _sides()
    main["events"][0]["order"] = 9
    plan = plan_three_way(base, main, branch, origins_complete=True)
    assert _order_writes(plan, "event") == [9]
    assert plan.main_changes["event"]["changed"] == 1


def test_a_reorder_against_a_deletion_still_asks() -> None:
    """Deliberately unchanged: main deleted what the branch only moved."""
    base, main, branch = _sides()
    main["events"] = []
    branch["events"][0]["order"] = 5

    rows = detect_field_conflicts(base, main, branch, origins_complete=True)

    assert [(row["name"], row["field"]) for row in rows] == [("track.purchase", PRESENCE_FIELD)]
    assert merge_blocked_by(base, main, branch, origins_complete=True) is True


# --- the merge and the update (database) ---------------------------------------


async def _field_definition(branch_id: uuid.UUID | str, name: str = "name") -> FieldDefinition:
    async with TestSessionLocal() as session:
        row = await session.scalar(
            select(FieldDefinition)
            .join(EventType, EventType.id == FieldDefinition.event_type_id)
            .where(EventType.branch_id == uuid.UUID(str(branch_id)), FieldDefinition.name == name)
        )
        assert row is not None
        return row


async def _set_field_order(branch_id: uuid.UUID | str, order: int) -> None:
    fd = await _field_definition(branch_id)
    async with TestSessionLocal() as session:
        row = await session.get(FieldDefinition, fd.id)
        assert row is not None
        row.order = order
        await session.commit()


@pytest.mark.asyncio
async def test_merge_keeps_main_s_order_where_both_moved_it(client: AsyncClient) -> None:
    slug = "afb-order-both"
    branch_id = await _seed(client, slug)
    _project_id, main_id = await _ids(slug)
    for model, where in (
        (Event, {"name": EVENT}),
        (EventType, {"name": "track"}),
        (MetaFieldDefinition, {"name": "team"}),
    ):
        await _edit(model, main_id, where, order=20)
        await _edit(model, branch_id, where, order=30)
    await _set_field_order(main_id, 20)
    await _set_field_order(branch_id, 30)
    await _edit(Event, branch_id, {"name": EVENT}, description="branch desc")

    conflicts = (await client.get(_url(slug, branch_id, "conflicts"))).json()
    assert conflicts["entities"] == []
    assert conflicts["merge_blocked"] is False

    merged = await _approve_and_merge(client, slug, branch_id)

    assert merged.status_code == 200, merged.text
    main_event = await _one(Event, main_id, name=EVENT)
    assert (main_event.order, main_event.description) == (20, "branch desc")
    assert (await _one(EventType, main_id, name="track")).order == 20
    assert (await _one(MetaFieldDefinition, main_id, name="team")).order == 20
    assert (await _field_definition(main_id)).order == 20


@pytest.mark.asyncio
async def test_merge_lands_a_branch_only_move(client: AsyncClient) -> None:
    slug = "afb-order-branch"
    branch_id = await _seed(client, slug)
    _project_id, main_id = await _ids(slug)
    await _edit(Event, branch_id, {"name": EVENT}, order=30)
    await _set_field_order(branch_id, 30)

    merged = await _approve_and_merge(client, slug, branch_id)

    assert merged.status_code == 200, merged.text
    assert (await _one(Event, main_id, name=EVENT)).order == 30
    assert (await _field_definition(main_id)).order == 30


@pytest.mark.asyncio
async def test_merge_of_twins_differing_only_in_order_keeps_main_s(client: AsyncClient) -> None:
    slug = "afb-order-twins"
    branch_id = await _seed(client, slug)
    project_id, main_id = await _ids(slug)
    async with TestSessionLocal() as session:
        for side, order in ((main_id, 20), (uuid.UUID(branch_id), 10)):
            event_type = await session.scalar(
                select(EventType).where(EventType.branch_id == side, EventType.name == "track")
            )
            assert event_type is not None
            session.add(
                Event(
                    project_id=project_id,
                    branch_id=side,
                    event_type_id=event_type.id,
                    name="starting_place",
                    order=order,
                )
            )
        await session.commit()

    merged = await _approve_and_merge(client, slug, branch_id)

    assert merged.status_code == 200, merged.text
    assert await _count(Event, Event.branch_id == main_id, Event.name == "starting_place") == 1
    assert (await _one(Event, main_id, name="starting_place")).order == 20


@pytest.mark.asyncio
async def test_a_stale_order_choice_does_not_override_main(client: AsyncClient) -> None:
    """A stored ``order`` choice from before is still accepted, and never read."""
    slug = "afb-order-stale"
    branch_id = await _seed(client, slug)
    _project_id, main_id = await _ids(slug)
    await _edit(EventType, main_id, {"name": "track"}, order=5)
    await _edit(EventType, branch_id, {"name": "track"}, order=7, description="branch")
    saved = await client.post(
        _url(slug, branch_id, "resolutions"),
        json={
            "entity_type": "event_type",
            "entity_name": "track",
            "field_name": "order",
            "choice": "theirs",
        },
    )
    assert saved.status_code == 201, saved.text

    merged = await _approve_and_merge(client, slug, branch_id)

    assert merged.status_code == 200, merged.text
    main_track = await _one(EventType, main_id, name="track")
    assert (main_track.order, main_track.description) == (5, "branch")


@pytest.mark.asyncio
async def test_update_brings_main_s_order_without_asking(client: AsyncClient) -> None:
    slug = "afb-order-update"
    branch_id = await _seed(client, slug)
    _project_id, main_id = await _ids(slug)
    await _edit(Event, main_id, {"name": EVENT}, order=20)
    await _edit(Event, branch_id, {"name": EVENT}, order=30, description="branch desc")

    preview = (await client.get(_url(slug, branch_id))).json()
    assert preview["conflicts"]["overlap_count"] == 0
    updated = await client.post(_url(slug, branch_id), json={})

    assert updated.status_code == 200, updated.text
    branch_event = await _one(Event, branch_id, name=EVENT)
    assert (branch_event.order, branch_event.description) == (20, "branch desc")
    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text
    assert (await _one(Event, main_id, name=EVENT)).order == 20


# --- POST /resolutions/batch ---------------------------------------------------


def _pick(field: str, choice: str = "theirs", name: str = f"track.{EVENT}") -> dict[str, str]:
    return {"entity_type": "event", "entity_name": name, "field_name": field, "choice": choice}


async def _stored(branch_id: str) -> dict[str, str]:
    async with TestSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(PlanBranchMergeResolution).where(
                        PlanBranchMergeResolution.branch_id == uuid.UUID(branch_id)
                    )
                )
            )
            .scalars()
            .all()
        )
    return {row.field_name: row.choice for row in rows}


@pytest.mark.asyncio
async def test_batch_saves_every_choice_in_one_call_and_one_audit_row(
    client: AsyncClient,
) -> None:
    slug = "afb-batch"
    branch_id = await _seed(client, slug)
    single = await client.post(_url(slug, branch_id, "resolutions"), json=_pick("title", "ours"))
    assert single.status_code == 201

    resp = await client.post(
        _url(slug, branch_id, "resolutions/batch"),
        json={
            "resolutions": [
                _pick("title"),
                _pick("description", "ours"),
                _pick("tags", "ours"),
                # Named twice: the last choice is the one kept.
                _pick("tags"),
                _pick("title", name="track.other"),
            ]
        },
    )

    assert resp.status_code == 201, resp.text
    saved = resp.json()["resolutions"]
    assert len(saved) == 4
    # The single route's earlier choice was overwritten, not duplicated.
    assert await _stored(branch_id) == {"title": "theirs", "description": "ours", "tags": "theirs"}
    assert await _count(PlanBranchMergeResolution) == 4
    async with TestSessionLocal() as session:
        audit = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "plan_branch.resolution_batch_save")
                )
            )
            .scalars()
            .all()
        )
    assert len(audit) == 1
    assert audit[0].payload == {"count": 4, "entity_count": 2}


@pytest.mark.asyncio
async def test_batch_costs_the_same_statements_whatever_its_size(client: AsyncClient) -> None:
    """One SELECT for the stored rows, one flush, one read back: not one per item."""
    slug = "afb-batch-cost"
    branch_id = await _seed(client, slug)
    url = _url(slug, branch_id, "resolutions/batch")
    statements = 0

    def _on_statement(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        nonlocal statements
        statements += 1

    async def _cost(body: dict[str, Any]) -> int:
        nonlocal statements
        statements = 0
        event.listen(engine.sync_engine, "before_cursor_execute", _on_statement)
        try:
            resp = await client.post(url, json=body)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _on_statement)
        assert resp.status_code == 201, resp.text
        return statements

    small = await _cost({"resolutions": [_pick("title", name="track.s0")]})
    # Half of these overwrite rows the first call or each other stored.
    picks = [_pick("title", name=f"track.e{i}") for i in range(200)]
    picks += [_pick("title", "ours", name=f"track.e{i}") for i in range(100)]
    picks.append(_pick("title", "ours", name="track.s0"))
    large = await _cost({"resolutions": picks})

    assert large <= small + 3, (small, large)
    stored = await _count(PlanBranchMergeResolution)
    assert stored == 201
    async with TestSessionLocal() as session:
        rows = (
            await session.scalars(
                select(PlanBranchMergeResolution).where(
                    PlanBranchMergeResolution.branch_id == uuid.UUID(branch_id)
                )
            )
        ).all()
    choices = {row.entity_name: row.choice for row in rows}
    assert choices["track.e0"] == "ours"
    assert choices["track.e150"] == "theirs"
    assert choices["track.s0"] == "ours"


@pytest.mark.asyncio
async def test_batch_is_all_or_none(client: AsyncClient) -> None:
    slug = "afb-batch-422"
    branch_id = await _seed(client, slug)

    resp = await client.post(
        _url(slug, branch_id, "resolutions/batch"),
        json={"resolutions": [_pick("title"), _pick("not_a_field")]},
    )

    assert resp.status_code == 422, resp.text
    assert await _count(PlanBranchMergeResolution) == 0


@pytest.mark.asyncio
async def test_batch_bounds_and_targets(client: AsyncClient) -> None:
    slug = "afb-batch-bounds"
    branch_id = await _seed(client, slug)
    project_id, main_id = await _ids(slug)
    url = _url(slug, branch_id, "resolutions/batch")

    assert (await client.post(url, json={"resolutions": []})).status_code == 422
    too_many = {"resolutions": [_pick("title", name=f"track.e{i}") for i in range(5001)]}
    assert (await client.post(url, json=too_many)).status_code == 422
    body = {"resolutions": [_pick("title")]}
    assert (
        await client.post(_url(slug, main_id, "resolutions/batch"), json=body)
    ).status_code == 400
    assert (
        await client.post(_url(slug, uuid.uuid4(), "resolutions/batch"), json=body)
    ).status_code == 404

    viewer = await persisted_member_user(project_id, role="viewer")

    async def _viewer() -> User:
        return viewer

    app.dependency_overrides[get_current_user] = _viewer
    try:
        forbidden = await client.post(url, json=body)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert forbidden.status_code == 403
    assert await _count(PlanBranchMergeResolution) == 0
