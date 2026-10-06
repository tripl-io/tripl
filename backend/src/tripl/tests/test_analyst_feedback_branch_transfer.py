"""``POST /branches/{id}/transfer``: move or copy diff rows onto another branch.

Main is seeded by ``test_plan_branch_update_from_main._seed`` (a ``track``
type with a ``name`` field, the event ``purchase:success`` valued
``${currency}``, the variables ``currency`` and ``country``, the meta field
``team``) and cut into the branch ``feature``. Branch work is written through
the ORM, the way the update tests write it, so each test states exactly what
each branch changed.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tripl.models.audit_log import AuditLog
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_meta_value import EventMetaValue
from tripl.models.event_photo import EventPhoto
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.models.event_tag import EventTag
from tripl.models.event_type import EventType
from tripl.models.event_type_relation import EventTypeRelation
from tripl.models.field_definition import FieldDefinition
from tripl.models.meta_field_definition import MetaFieldDefinition
from tripl.models.plan_branch import PlanBranch
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.services import plan_branch_transfer_service
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_plan_branch_update_from_main import (
    EVENT,
    _approve_and_merge,
    _count,
    _edit,
    _ids,
    _one,
    _rename_variable,
    _seed,
    _url,
)


def _ref(entity_type: str, name: str, parent: str | None = None) -> dict[str, Any]:
    return {"entity_type": entity_type, "name": name, "parent": parent}


async def _branch(client: AsyncClient, slug: str, name: str) -> str:
    resp = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": name})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _transfer(
    client: AsyncClient,
    slug: str,
    source: str,
    target: str | None,
    mode: str,
    *entries: dict[str, Any],
    dry_run: bool = False,
) -> Any:
    return await client.post(
        _url(slug, source, "transfer"),
        json={
            "target_branch_id": target,
            "mode": mode,
            "dry_run": dry_run,
            "entries": list(entries),
        },
    )


async def _rows(client: AsyncClient, slug: str, branch_id: str) -> set[tuple[str, str, str]]:
    diff = (await client.get(_url(slug, branch_id, "diff"))).json()
    return {(e["entity_type"], e["kind"], e["name"]) for e in diff["entries"]}


async def _add_field(branch_id: str, name: str, *, event_type: str = "track") -> uuid.UUID:
    async with TestSessionLocal() as session:
        track = await session.scalar(
            select(EventType).where(
                EventType.branch_id == uuid.UUID(branch_id), EventType.name == event_type
            )
        )
        assert track is not None
        field = FieldDefinition(
            event_type_id=track.id,
            name=name,
            display_name=name.title(),
            field_type="string",
            sensitivity="none",
        )
        session.add(field)
        await session.commit()
        return field.id


async def _add_event(
    project_id: uuid.UUID,
    branch_id: str,
    name: str,
    *,
    values: dict[str, str] | None = None,
    meta: dict[str, str] | None = None,
    tags: tuple[str, ...] = (),
    superseded_by: uuid.UUID | None = None,
) -> uuid.UUID:
    branch = uuid.UUID(branch_id)
    async with TestSessionLocal() as session:
        track = await session.scalar(
            select(EventType).where(EventType.branch_id == branch, EventType.name == "track")
        )
        assert track is not None
        event = Event(
            project_id=project_id,
            branch_id=branch,
            event_type_id=track.id,
            name=name,
            superseded_by_event_id=superseded_by,
        )
        session.add(event)
        await session.flush()
        for field_name, value in (values or {}).items():
            field = await session.scalar(
                select(FieldDefinition).where(
                    FieldDefinition.event_type_id == track.id, FieldDefinition.name == field_name
                )
            )
            assert field is not None
            session.add(
                EventFieldValue(
                    event_id=event.id, field_definition_id=field.id, value=value, is_authored=True
                )
            )
        for meta_name, value in (meta or {}).items():
            meta_field = await session.scalar(
                select(MetaFieldDefinition).where(
                    MetaFieldDefinition.branch_id == branch, MetaFieldDefinition.name == meta_name
                )
            )
            assert meta_field is not None
            session.add(
                EventMetaValue(
                    event_id=event.id, meta_field_definition_id=meta_field.id, value=value
                )
            )
        for tag in tags:
            session.add(EventTag(event_id=event.id, name=tag))
        await session.commit()
        return event.id


async def _values(event_id: uuid.UUID) -> list[str]:
    async with TestSessionLocal() as session:
        return sorted(
            (
                await session.execute(
                    select(EventFieldValue.value).where(EventFieldValue.event_id == event_id)
                )
            )
            .scalars()
            .all()
        )


# --- copy and move ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy_puts_an_added_event_on_the_target_and_leaves_the_source(
    client: AsyncClient,
) -> None:
    slug = "xfer-copy"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    project_id, _main_id = await _ids(slug)
    await _add_event(
        project_id, source, "signup", values={"name": "x"}, meta={"team": "growth"}, tags=("t1",)
    )

    resp = await _transfer(client, slug, source, target, "copy", _ref("event", "signup", "track"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [(i["name"], i["kind"]) for i in body["applied"]] == [("signup", "added")]
    assert body["target_counts"] == [
        {"entity_type": "event", "added": 1, "changed": 0, "removed": 0, "renamed": 0}
    ]

    copied = await _one(Event, target, name="signup")
    assert copied is not None and copied.origin_id is None
    assert await _values(copied.id) == ["x"]
    async with TestSessionLocal() as session:
        meta = (
            (
                await session.execute(
                    select(EventMetaValue.value).where(EventMetaValue.event_id == copied.id)
                )
            )
            .scalars()
            .all()
        )
        tags = (
            (await session.execute(select(EventTag.name).where(EventTag.event_id == copied.id)))
            .scalars()
            .all()
        )
    assert list(meta) == ["growth"] and list(tags) == ["t1"]
    assert ("event", "added", "signup") in await _rows(client, slug, target)
    assert ("event", "added", "signup") in await _rows(client, slug, source)


@pytest.mark.asyncio
async def test_created_rows_take_main_origins_and_land_on_main_as_additions(
    client: AsyncClient,
) -> None:
    slug = "xfer-origin"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    project_id, main_id = await _ids(slug)
    plan_id = await _add_field(source, "plan")
    await _add_event(project_id, source, "signup", values={"plan": "pro"})
    async with TestSessionLocal() as session:
        track = await session.scalar(
            select(EventType).where(
                EventType.branch_id == uuid.UUID(source), EventType.name == "track"
            )
        )
        name_field = await session.scalar(
            select(FieldDefinition).where(
                FieldDefinition.event_type_id == track.id, FieldDefinition.name == "name"
            )
        )
        session.add(
            EventTypeRelation(
                project_id=project_id,
                branch_id=uuid.UUID(source),
                source_event_type_id=track.id,
                target_event_type_id=track.id,
                source_field_id=name_field.id,
                target_field_id=plan_id,
            )
        )
        await session.commit()
    await _edit(Event, source, {"name": EVENT}, description="from the source")

    resp = await _transfer(
        client,
        slug,
        source,
        target,
        "copy",
        _ref("event", "signup", "track"),
        _ref("relation", "track.name → track.plan"),
        _ref("event", EVENT, "track"),
    )
    assert resp.status_code == 200, resp.text
    assert [(i["name"], i["needed_by"]) for i in resp.json()["carried"]] == [("plan", "signup")]

    main_event = await _one(Event, main_id, name=EVENT)
    assert (await _one(Event, target, name=EVENT)).origin_id == main_event.id
    assert (await _one(Event, target, name="signup")).origin_id is None
    assert (await _one(EventTypeRelation, target)).origin_id is None

    merged = await _approve_and_merge(client, slug, target)
    assert merged.status_code == 200, merged.text
    assert await _count(Event, Event.branch_id == main_id, Event.name == "signup") == 1
    assert await _count(EventTypeRelation, EventTypeRelation.branch_id == main_id) == 1
    assert (await _one(Event, main_id, name=EVENT)).description == "from the source"
    assert (await _one(Event, main_id, name=EVENT)).id == main_event.id


@pytest.mark.asyncio
async def test_move_takes_the_rows_off_the_source(client: AsyncClient) -> None:
    slug = "xfer-move"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    project_id, _main_id = await _ids(slug)
    await _add_field(source, "plan")
    await _add_event(project_id, source, "signup", values={"plan": "pro"})

    resp = await _transfer(client, slug, source, target, "move", _ref("event", "signup", "track"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["source_diff"]["entries"] == []
    assert await _rows(client, slug, source) == set()
    assert await _rows(client, slug, target) == {
        ("event", "added", "signup"),
        ("field_definition", "added", "plan"),
    }
    assert await _one(Event, source, name="signup") is None


@pytest.mark.asyncio
async def test_a_changed_variable_moves_only_its_changed_fields(client: AsyncClient) -> None:
    slug = "xfer-var"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    await _edit(Variable, source, {"name": "currency"}, description="A note")
    await _edit(Variable, target, {"name": "currency"}, allowed_values=["EUR"])

    resp = await _transfer(client, slug, source, target, "move", _ref("variable", "currency"))
    assert resp.status_code == 200, resp.text
    moved = await _one(Variable, target, name="currency")
    assert moved.description == "A note" and moved.allowed_values == ["EUR"]
    assert (await _one(Variable, source, name="currency")).description == ""
    assert await _rows(client, slug, source) == set()


# --- refusals -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_field_the_target_edited_too_is_refused_and_nothing_is_written(
    client: AsyncClient,
) -> None:
    slug = "xfer-both"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    await _edit(Event, source, {"name": EVENT}, description="mine")
    await _edit(Event, target, {"name": EVENT}, description="theirs")

    resp = await _transfer(client, slug, source, target, "move", _ref("event", EVENT, "track"))
    assert resp.status_code == 409, resp.text
    [conflict] = resp.json()["detail"]["transfer_conflicts"]
    assert (conflict["reason"], conflict["field"]) == ("target_changed", "description")
    assert '"theirs"' in conflict["message"] and "the base had" in conflict["message"]
    assert (await _one(Event, target, name=EVENT)).description == "theirs"
    assert (await _one(Event, source, name=EVENT)).description == "mine"


@pytest.mark.asyncio
async def test_an_added_name_the_target_holds_is_refused_unless_it_is_the_same(
    client: AsyncClient,
) -> None:
    slug = "xfer-exists"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    project_id, _main_id = await _ids(slug)
    async with TestSessionLocal() as session:
        for branch_id, text in ((source, "x"), (target, "y")):
            session.add(
                Variable(
                    project_id=project_id,
                    branch_id=uuid.UUID(branch_id),
                    name="plan_tier",
                    description=text,
                )
            )
        await session.commit()

    refused = await _transfer(client, slug, source, target, "copy", _ref("variable", "plan_tier"))
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["transfer_conflicts"][0]["reason"] == "target_exists"

    await _edit(Variable, target, {"name": "plan_tier"}, description="x")
    same = await _transfer(client, slug, source, target, "copy", _ref("variable", "plan_tier"))
    assert same.status_code == 200, same.text
    assert [i["name"] for i in same.json()["skipped"]] == ["plan_tier"]
    assert same.json()["applied"] == []


@pytest.mark.asyncio
async def test_a_parent_the_target_deleted_is_named(client: AsyncClient) -> None:
    slug = "xfer-parent"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    project_id, _main_id = await _ids(slug)
    await _add_event(project_id, source, "signup")
    async with TestSessionLocal() as session:
        for event in (
            await session.execute(select(Event).where(Event.branch_id == uuid.UUID(target)))
        ).scalars():
            await session.delete(event)
        await session.flush()
        track = await session.scalar(
            select(EventType).where(EventType.branch_id == uuid.UUID(target))
        )
        await session.delete(track)
        await session.commit()

    resp = await _transfer(client, slug, source, target, "copy", _ref("event", "signup", "track"))
    assert resp.status_code == 409, resp.text
    [conflict] = resp.json()["detail"]["transfer_conflicts"]
    assert conflict["reason"] == "target_missing"
    assert "event type 'track'" in conflict["message"]


# --- removals ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_moved_removal_deletes_the_target_copy_by_origin_beside_a_namesake(
    client: AsyncClient,
) -> None:
    slug = "xfer-removed"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    project_id, _main_id = await _ids(slug)
    async with TestSessionLocal() as session:
        event = await session.scalar(
            select(Event).where(Event.branch_id == uuid.UUID(source), Event.name == EVENT)
        )
        await session.delete(event)
        await session.commit()
    namesake = await _add_event(project_id, target, EVENT)

    resp = await _transfer(client, slug, source, target, "move", _ref("event", EVENT, "track"))
    assert resp.status_code == 200, resp.text
    left = await _count(Event, Event.branch_id == uuid.UUID(target), Event.name == EVENT)
    assert left == 1
    assert (await _one(Event, target, name=EVENT)).id == namesake
    restored = await _one(Event, source, name=EVENT)
    assert restored is not None and await _values(restored.id) == ["${currency}"]


@pytest.mark.asyncio
async def test_a_removed_event_type_is_not_transferable(client: AsyncClient) -> None:
    slug = "xfer-removed-type"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    async with TestSessionLocal() as session:
        for event in (
            await session.execute(select(Event).where(Event.branch_id == uuid.UUID(source)))
        ).scalars():
            await session.delete(event)
        await session.flush()
        track = await session.scalar(
            select(EventType).where(EventType.branch_id == uuid.UUID(source))
        )
        await session.delete(track)
        await session.commit()

    resp = await _transfer(client, slug, source, target, "move", _ref("event_type", "track"))
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["removed_not_transferable"] is True
    assert "directly" in resp.json()["detail"]["message"]
    assert await _one(EventType, target, name="track") is not None


# --- renames ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_moved_variable_rename_lands_with_its_tokens_and_the_source_goes_back(
    client: AsyncClient,
) -> None:
    slug = "xfer-rename"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    project_id, _main_id = await _ids(slug)
    await _rename_variable(project_id, source, "currency", "currency_code")
    await _edit(Variable, source, {"name": "currency_code"}, description="ISO 4217")

    # Only the removed half is named; the addition comes with it.
    resp = await _transfer(client, slug, source, target, "move", _ref("variable", "currency"))
    assert resp.status_code == 200, resp.text
    assert {(i["name"], i["kind"]) for i in resp.json()["applied"]} == {
        ("currency", "removed"),
        ("currency_code", "added"),
    }

    landed = await _one(Variable, target, name="currency_code")
    assert landed is not None and landed.description == "ISO 4217"
    assert await _values((await _one(Event, target, name=EVENT)).id) == ["${currency_code}"]

    back = await _one(Variable, source, name="currency")
    assert back is not None and back.description == ""
    assert await _one(Variable, source, name="currency_code") is None
    assert await _values((await _one(Event, source, name=EVENT)).id) == ["${currency}"]
    assert await _rows(client, slug, source) == set()


@pytest.mark.asyncio
async def test_an_event_rename_named_by_its_added_half_expands_to_both(
    client: AsyncClient,
) -> None:
    slug = "xfer-rename-event"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    await _edit(Event, source, {"name": EVENT}, name="purchase:done")

    resp = await _transfer(
        client, slug, source, target, "copy", _ref("event", "purchase:done", "track")
    )
    assert resp.status_code == 200, resp.text
    assert {(i["name"], i["kind"]) for i in resp.json()["applied"]} == {
        (EVENT, "removed"),
        ("purchase:done", "added"),
    }
    assert await _one(Event, target, name=EVENT) is None
    assert await _one(Event, target, name="purchase:done") is not None


# --- references -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_moved_event_keeps_its_successor_and_overrides_on_the_target(
    client: AsyncClient,
) -> None:
    slug = "xfer-refs"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    project_id, _main_id = await _ids(slug)
    v2 = await _add_event(project_id, source, "v2")
    v1 = await _add_event(project_id, source, "v1", superseded_by=v2)
    country = await _one(Variable, source, name="country")
    async with TestSessionLocal() as session:
        session.add(
            VariableEventValueOverride(
                project_id=project_id,
                branch_id=uuid.UUID(source),
                variable_id=country.id,
                event_id=v1,
                values=["DE"],
                required=False,
            )
        )
        await session.commit()

    resp = await _transfer(client, slug, source, target, "move", _ref("event", "v1", "track"))
    assert resp.status_code == 200, resp.text
    assert {i["name"] for i in resp.json()["carried"]} == {"v2", "country"}

    target_v1 = await _one(Event, target, name="v1")
    target_v2 = await _one(Event, target, name="v2")
    assert target_v1.superseded_by_event_id == target_v2.id
    target_country = await _one(Variable, target, name="country")
    override = await _one(VariableEventValueOverride, target, variable_id=target_country.id)
    assert override is not None and override.event_id == target_v1.id
    assert await _one(Event, source, name="v1") is None
    assert await _rows(client, slug, source) == set()


@pytest.mark.asyncio
async def test_photos_move_to_the_target_and_stay_on_the_source(client: AsyncClient) -> None:
    slug = "xfer-photo"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    project_id, _main_id = await _ids(slug)
    source_event = await _one(Event, source, name=EVENT)
    async with TestSessionLocal() as session:
        session.add(
            EventPhoto(
                project_id=project_id,
                event_id=source_event.id,
                kind="figma",
                external_url="https://www.figma.com/file/shot",
                original_filename="shot",
            )
        )
        await session.commit()

    resp = await _transfer(client, slug, source, target, "move", _ref("event", EVENT, "track"))
    assert resp.status_code == 200, resp.text
    assert any("keeps its photo changes" in w for w in resp.json()["warnings"])
    target_event = await _one(Event, target, name=EVENT)
    assert await _count(EventPhoto, EventPhoto.event_id == target_event.id) == 1
    assert await _count(EventPhoto, EventPhoto.event_id == source_event.id) == 1


# --- discussion -------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("anchor", ["event", "photo"])
async def test_a_move_refuses_an_added_event_with_comments_and_a_copy_keeps_them(
    client: AsyncClient, anchor: str
) -> None:
    slug = f"xfer-talk-{anchor}"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    project_id, _main_id = await _ids(slug)
    signup = await _add_event(project_id, source, "signup")
    async with TestSessionLocal() as session:
        if anchor == "event":
            session.add(EventPhotoComment(event_id=signup, body="Why this name?"))
        else:
            photo = EventPhoto(
                project_id=project_id,
                event_id=signup,
                kind="figma",
                external_url="https://www.figma.com/file/x",
            )
            session.add(photo)
            await session.flush()
            session.add(EventPhotoComment(photo_id=photo.id, body="Wrong screen"))
        await session.commit()

    moved = await _transfer(client, slug, source, target, "move", _ref("event", "signup", "track"))
    assert moved.status_code == 409, moved.text
    [conflict] = moved.json()["detail"]["transfer_conflicts"]
    assert conflict["reason"] == "has_discussion" and "Copy it instead" in conflict["message"]

    copied = await _transfer(client, slug, source, target, "copy", _ref("event", "signup", "track"))
    assert copied.status_code == 200, copied.text
    assert await _count(EventPhotoComment) == 1
    assert await _one(Event, source, name="signup") is not None


# --- the same main --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_branches_cut_from_different_mains_name_the_one_to_update(
    client: AsyncClient,
) -> None:
    slug = "xfer-base"
    older = await _seed(client, slug)
    project_id, main_id = await _ids(slug)
    await _edit(EventType, main_id, {"name": "track"}, color="#abcdef")
    newer = await _branch(client, slug, "task-2")
    await _add_event(project_id, newer, "signup")
    await _add_event(project_id, older, "login")

    resp = await _transfer(client, slug, newer, older, "copy", _ref("event", "signup", "track"))
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["transfer_base_mismatch"] is True
    assert "Run Update from main on 'feature'" in detail["message"]
    assert detail["behind_branch_ids"] == [older]

    preview = await _transfer(
        client, slug, older, None, "copy", _ref("event", "login", "track"), dry_run=True
    )
    assert preview.status_code == 409, preview.text
    assert "Run Update from main on 'feature'" in preview.json()["detail"]["message"]


# --- statuses -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_statuses_gate_the_transfer_and_are_left_as_they_were(client: AsyncClient) -> None:
    slug = "xfer-status"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    other = await _branch(client, slug, "task-3")
    landed = await _branch(client, slug, "task-4")
    project_id, _main_id = await _ids(slug)
    await _add_event(project_id, source, "signup")
    signup = _ref("event", "signup", "track")
    await client.post(_url(slug, source, "transition"), json={"action": "submit"})

    assert (await _transfer(client, slug, source, source, "copy", signup)).status_code == 400

    copied = await _transfer(client, slug, source, target, "copy", signup)
    assert copied.status_code == 200, copied.text
    statuses = {
        b["id"]: b["status"]
        for b in (await client.get(f"/api/v1/projects/{slug}/branches")).json()["items"]
    }
    assert statuses[source] == "ready_for_review" and statuses[target] == "draft"

    await client.post(_url(slug, target, "transition"), json={"action": "close"})
    assert (await _transfer(client, slug, source, target, "copy", signup)).status_code == 409

    await client.post(_url(slug, source, "transition"), json={"action": "close"})
    assert (await _transfer(client, slug, source, other, "move", signup)).status_code == 409
    from_closed = await _transfer(client, slug, source, other, "copy", signup)
    assert from_closed.status_code == 200, from_closed.text

    await _edit(EventType, landed, {"name": "track"}, color="#123456")
    assert (await _approve_and_merge(client, slug, landed)).status_code == 200
    into_merged = await _transfer(client, slug, other, landed, "copy", signup)
    assert into_merged.status_code == 409, into_merged.text
    out_of_merged = await _transfer(
        client, slug, landed, other, "copy", _ref("event_type", "track")
    )
    assert out_of_merged.status_code == 409, out_of_merged.text
    assert "merged" in out_of_merged.json()["detail"]


# --- dry run --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dry_run_reports_and_writes_nothing(client: AsyncClient) -> None:
    slug = "xfer-dry"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    project_id, _main_id = await _ids(slug)
    await _add_field(source, "plan")
    await _add_event(project_id, source, "signup", values={"plan": "pro"})
    audits = await _count(AuditLog)

    resp = await _transfer(
        client, slug, source, target, "move", _ref("event", "signup", "track"), dry_run=True
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True and body["source_diff"] is None
    assert [(i["name"], i["needed_by"]) for i in body["carried"]] == [("plan", "signup")]
    assert await _one(Event, target, name="signup") is None
    assert await _one(Event, source, name="signup") is not None
    assert await _count(AuditLog) == audits


@pytest.mark.asyncio
async def test_a_refusal_the_revert_raises_shows_in_the_dry_run(client: AsyncClient) -> None:
    slug = "xfer-dry-revert"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    async with TestSessionLocal() as session:
        event = await session.scalar(
            select(Event).where(Event.branch_id == uuid.UUID(source), Event.name == EVENT)
        )
        await session.delete(event)
        await session.flush()
        field = await session.scalar(
            select(FieldDefinition)
            .join(EventType, EventType.id == FieldDefinition.event_type_id)
            .where(EventType.branch_id == uuid.UUID(source), FieldDefinition.name == "name")
        )
        await session.delete(field)
        await session.commit()

    # Undoing the removal on the source would restore a value for a field the
    # source deleted: the revert refuses, and the preview says so.
    resp = await _transfer(
        client, slug, source, target, "move", _ref("event", EVENT, "track"), dry_run=True
    )
    assert resp.status_code == 409, resp.text
    assert "no longer exist" in str(resp.json()["detail"])
    assert await _one(Event, target, name=EVENT) is not None


@pytest.mark.asyncio
async def test_a_preview_against_a_new_branch_writes_no_branch(client: AsyncClient) -> None:
    slug = "xfer-new"
    source = await _seed(client, slug)
    await _edit(Event, source, {"name": EVENT}, description="mine")
    async with TestSessionLocal() as session:
        country = await session.scalar(
            select(Variable).where(
                Variable.branch_id == uuid.UUID(source), Variable.name == "country"
            )
        )
        await session.delete(country)
        await session.commit()
    branches = await _count(PlanBranch)

    resp = await _transfer(
        client,
        slug,
        source,
        None,
        "move",
        _ref("event", EVENT, "track"),
        _ref("variable", "country"),
        dry_run=True,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["target_branch_id"] is None
    assert {(i["name"], i["kind"]) for i in body["applied"]} == {
        (EVENT, "changed"),
        ("country", "removed"),
    }
    assert await _count(PlanBranch) == branches
    assert (await _one(Event, source, name=EVENT)).description == "mine"

    real = await _transfer(client, slug, source, None, "copy", _ref("event", EVENT, "track"))
    assert real.status_code == 422


# --- audit and errors ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_real_transfer_is_audited_on_both_branches(client: AsyncClient) -> None:
    slug = "xfer-audit"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    project_id, _main_id = await _ids(slug)
    await _add_event(project_id, source, "signup")

    resp = await _transfer(client, slug, source, target, "copy", _ref("event", "signup", "track"))
    assert resp.status_code == 200, resp.text
    async with TestSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action.like("plan_branch.transfer_%"))
                )
            )
            .scalars()
            .all()
        )
    by_action = {row.action: row for row in rows}
    assert set(by_action) == {"plan_branch.transfer_out", "plan_branch.transfer_in"}
    assert by_action["plan_branch.transfer_out"].target_id == uuid.UUID(source)
    assert by_action["plan_branch.transfer_in"].target_id == uuid.UUID(target)
    payload = by_action["plan_branch.transfer_in"].payload
    assert payload["mode"] == "copy" and payload["other_branch_name"] == "feature"
    assert payload["entries"] == [
        {"entity_type": "event", "name": "signup", "parent": "track", "kind": "added"}
    ]


@pytest.mark.asyncio
async def test_a_constraint_violation_is_a_409_and_rolls_both_branches_back(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    slug = "xfer-integrity"
    source = await _seed(client, slug)
    target = await _branch(client, slug, "task-2")
    project_id, _main_id = await _ids(slug)
    await _add_event(project_id, source, "signup")
    real_apply = plan_branch_transfer_service._apply

    async def failing(*args: Any, **kwargs: Any) -> Any:
        await real_apply(*args, **kwargs)
        raise IntegrityError("INSERT", {}, Exception("forced"))

    monkeypatch.setattr(plan_branch_transfer_service, "_apply", failing)
    resp = await _transfer(client, slug, source, target, "move", _ref("event", "signup", "track"))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["transfer_constraint_violation"] is True
    assert await _one(Event, target, name="signup") is None
    assert await _one(Event, source, name="signup") is not None
