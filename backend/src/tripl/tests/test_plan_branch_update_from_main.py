"""``/branches/{id}/update-from-main``: a three-way merge of main INTO a branch (PL-8).

Main is edited through the ORM, the way the merge tests edit a branch, so each
test states exactly which side changed what. SQLite enforces no foreign keys,
so every cleanup the update owes a deleted row is asserted explicitly.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.api.deps import get_current_user
from tripl.main import app
from tripl.models.audit_log import AuditLog
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_meta_value import EventMetaValue
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.meta_field_definition import MetaFieldDefinition
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.plan_branch_merge_resolution import PlanBranchMergeResolution
from tripl.models.plan_revision import PlanRevision
from tripl.models.project import Project
from tripl.models.user import User
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.services import plan_branch_update_service
from tripl.services.plan_revision_service import build_plan_snapshot
from tripl.services.variable_service import rewrite_variable_token_references
from tripl.tests._members import persisted_member_user
from tripl.tests.conftest import TestSessionLocal

EVENT = "purchase:success"


def _url(slug: str, branch_id: str | uuid.UUID, tail: str = "update-from-main") -> str:
    return f"/api/v1/projects/{slug}/branches/{branch_id}/{tail}"


async def _ids(slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == slug))
        main_id = await session.scalar(
            select(PlanBranch.id).where(
                PlanBranch.project_id == project_id, PlanBranch.kind == BranchKind.main.value
            )
        )
    assert project_id is not None and main_id is not None
    return project_id, main_id


MainHook = Callable[[AsyncSession, uuid.UUID, uuid.UUID], Awaitable[None]]


async def _seed(client: AsyncClient, slug: str, before_branch: MainHook | None = None) -> str:
    """Main with a type, a field, an event, two variables and a meta field.

    ``before_branch(session, project_id, main_id)`` adds more to main before the
    cut, inside the seeding transaction. Returns the id of a branch cut from it.
    """
    assert (
        await client.post("/api/v1/projects", json={"name": slug, "slug": slug, "description": ""})
    ).status_code == 201
    et = await client.post(
        f"/api/v1/projects/{slug}/event-types", json={"name": "track", "display_name": "Track"}
    )
    assert et.status_code == 201
    field = await client.post(
        f"/api/v1/projects/{slug}/event-types/{et.json()['id']}/fields",
        json={"name": "name", "display_name": "Name", "field_type": "string"},
    )
    assert field.status_code == 201
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et.json()["id"], "name": EVENT},
    )
    assert event.status_code == 201

    project_id, main_id = await _ids(slug)
    async with TestSessionLocal() as session:
        main_event = await session.scalar(
            select(Event).where(Event.branch_id == main_id, Event.name == EVENT)
        )
        assert main_event is not None
        # The API may already have stored an empty value for the field; the
        # pair is unique, so write through whichever row is there.
        existing = await session.scalar(
            select(EventFieldValue).where(
                EventFieldValue.event_id == main_event.id,
                EventFieldValue.field_definition_id == uuid.UUID(field.json()["id"]),
            )
        )
        if existing is not None:
            existing.value = "${currency}"
        else:
            session.add(
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=main_event.id,
                    field_definition_id=uuid.UUID(field.json()["id"]),
                    value="${currency}",
                    is_authored=True,
                )
            )
        for name, source in (("currency", "S_CURRENCY"), ("country", "S_COUNTRY")):
            session.add(
                Variable(
                    project_id=project_id,
                    branch_id=main_id,
                    name=name,
                    source_name=source,
                    description="",
                )
            )
        session.add(
            MetaFieldDefinition(
                project_id=project_id,
                branch_id=main_id,
                name="team",
                display_name="Team",
                field_type="string",
                sensitivity="none",
            )
        )
        await session.flush()
        if before_branch is not None:
            await before_branch(session, project_id, main_id)
        await session.commit()

    branch = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    assert branch.status_code == 201
    return str(branch.json()["id"])


async def _one(model: Any, branch_id: uuid.UUID | str, **where: Any) -> Any:
    async with TestSessionLocal() as session:
        stmt = select(model).where(model.branch_id == uuid.UUID(str(branch_id)))
        for column, value in where.items():
            stmt = stmt.where(getattr(model, column) == value)
        return (await session.execute(stmt)).scalars().first()


async def _edit(
    model: Any, branch_id: uuid.UUID | str, where: dict[str, Any], **values: Any
) -> None:
    async with TestSessionLocal() as session:
        stmt = select(model).where(model.branch_id == uuid.UUID(str(branch_id)))
        for column, value in where.items():
            stmt = stmt.where(getattr(model, column) == value)
        row = (await session.execute(stmt)).scalars().one()
        for column, value in values.items():
            setattr(row, column, value)
        await session.commit()


async def _branch_row(branch_id: str) -> PlanBranch:
    async with TestSessionLocal() as session:
        branch = await session.get(PlanBranch, uuid.UUID(branch_id))
        assert branch is not None
        return branch


async def _count(model: Any, *where: Any) -> int:
    async with TestSessionLocal() as session:
        return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def _rename_variable(
    project_id: uuid.UUID, branch_id: uuid.UUID | str, old: str, new: str
) -> None:
    """Rename a variable the way the variable update does: its ``${token}``s go along."""
    await _edit(Variable, branch_id, {"name": old}, name=new)
    async with TestSessionLocal() as session:
        await rewrite_variable_token_references(
            session,
            project_id=project_id,
            branch_id=uuid.UUID(str(branch_id)),
            old_name=old,
            new_name=new,
        )
        await session.commit()


async def _approve_and_merge(client: AsyncClient, slug: str, branch_id: str) -> Any:
    for action in ("submit", "approve"):
        await client.post(_url(slug, branch_id, "transition"), json={"action": action})
    return await client.post(_url(slug, branch_id, "merge"))


# --- the clean path ------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_update_brings_main_in_and_keeps_the_branch_work(client: AsyncClient) -> None:
    slug = "ufm-clean"
    branch_id = await _seed(client, slug)
    project_id, main_id = await _ids(slug)
    main_track = await _one(EventType, main_id, name="track")
    old_base = (await _branch_row(branch_id)).base_revision_id

    await _edit(EventType, main_id, {"name": "track"}, color="#abcdef")
    await _edit(MetaFieldDefinition, main_id, {"name": "team"}, display_name="Team (main)")
    async with TestSessionLocal() as session:
        session.add(
            Event(
                project_id=project_id, branch_id=main_id, event_type_id=main_track.id, name="signup"
            )
        )
        country = await session.scalar(
            select(Variable).where(Variable.branch_id == main_id, Variable.name == "country")
        )
        await session.delete(country)
        await session.commit()
    await _edit(Event, branch_id, {"name": EVENT}, description="branch desc")

    preview = await client.get(_url(slug, branch_id))
    assert preview.status_code == 200, preview.text
    assert preview.json()["behind"] is True
    assert preview.json()["conflicts"]["overlap_count"] == 0
    brought = {row["entity_type"]: row for row in preview.json()["main_changes"]}
    assert brought["event"]["added"] == 1
    assert brought["variable"]["removed"] == 1

    resp = await client.post(
        _url(slug, branch_id), json={"expected_main_hash": preview.json()["main_hash"]}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] is True
    assert body["previous_base_revision_id"] == str(old_base)
    assert body["base_revision_id"] != str(old_base)

    assert (await _one(EventType, branch_id, name="track")).color == "#abcdef"
    assert (await _one(MetaFieldDefinition, branch_id, name="team")).display_name == "Team (main)"
    assert await _one(Variable, branch_id, name="country") is None
    assert (await _one(Event, branch_id, name=EVENT)).description == "branch desc"
    main_signup = await _one(Event, main_id, name="signup")
    branch_signup = await _one(Event, branch_id, name="signup")
    assert branch_signup is not None and branch_signup.origin_id == main_signup.id

    async with TestSessionLocal() as session:
        branch = await session.get(PlanBranch, uuid.UUID(branch_id))
        assert branch is not None
        assert branch.base_revision_id == uuid.UUID(body["base_revision_id"])
        revision = await session.get(PlanRevision, branch.base_revision_id)
        assert revision is not None and revision.kind == "branch_base"
        assert revision.branch_id == branch.id
        assert revision.payload == await build_plan_snapshot(session, project_id, branch_id=main_id)
        # The old base stays in history.
        assert await session.get(PlanRevision, old_base) is not None

    listed = await client.get(f"/api/v1/projects/{slug}/branches?include_diff_counts=true")
    row = next(item for item in listed.json()["items"] if item["id"] == branch_id)
    assert row["behind_base"] is False
    diff = (await client.get(_url(slug, branch_id, "diff"))).json()
    assert diff["behind_base"] is False
    assert [(e["entity_type"], e["kind"], e["name"]) for e in diff["entries"]] == [
        ("event", "changed", EVENT)
    ]

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text
    assert (await _one(Event, main_id, name=EVENT)).description == "branch desc"
    # Updated in place on main: the branch's copy of signup paired by origin.
    assert await _count(Event, Event.branch_id == main_id, Event.name == "signup") == 1
    assert (await _one(Event, main_id, name="signup")).id == main_signup.id
    assert (await _one(EventType, main_id, name="track")).color == "#abcdef"


@pytest.mark.asyncio
async def test_update_is_idempotent_and_audited_once(client: AsyncClient) -> None:
    slug = "ufm-idem"
    branch_id = await _seed(client, slug)
    _project_id, main_id = await _ids(slug)
    await _edit(EventType, main_id, {"name": "track"}, description="main text")

    first = await client.post(_url(slug, branch_id), json={})
    assert first.status_code == 200, first.text
    assert first.json()["updated"] is True
    revisions = await _count(PlanRevision)

    second = await client.post(_url(slug, branch_id), json={})
    assert second.status_code == 200, second.text
    assert second.json()["updated"] is False
    assert second.json()["base_revision_id"] == first.json()["base_revision_id"]
    assert await _count(PlanRevision) == revisions

    async with TestSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "plan_branch.update_from_main")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].target_id == uuid.UUID(branch_id)
    assert set(rows[0].payload) == {
        "previous_base_revision_id",
        "base_revision_id",
        "applied",
        "resolutions",
    }
    assert rows[0].payload["applied"] == [
        {"entity_type": "event_type", "added": 0, "changed": 1, "removed": 0, "renamed": 0}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("broker_up", [True, False])
async def test_search_rebuild_goes_to_the_worker_unless_the_broker_refuses(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, broker_up: bool
) -> None:
    """Rebuilt inline, the branch's search index was two thirds of the update."""
    from tripl.services import search_service

    slug = f"ufm-reindex-{str(broker_up).lower()}"
    branch_id = await _seed(client, slug)
    _project_id, main_id = await _ids(slug)
    await _edit(EventType, main_id, {"name": "track"}, description="main text")

    queued: list[uuid.UUID] = []
    inline: list[uuid.UUID] = []

    async def fake_queue(_project_id: uuid.UUID, branch: uuid.UUID) -> bool:
        queued.append(branch)
        return broker_up

    async def fake_reindex(_session: AsyncSession, *, branch_id: uuid.UUID, **_: Any) -> None:
        inline.append(branch_id)

    monkeypatch.setattr(plan_branch_update_service, "_worker_reindexes", lambda _session: True)
    monkeypatch.setattr(search_service, "_queue_branch_reindex", fake_queue)
    monkeypatch.setattr(search_service, "reindex_project_branch", fake_reindex)

    resp = await client.post(_url(slug, branch_id), json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] is True
    assert queued == [uuid.UUID(branch_id)]
    assert inline == ([] if broker_up else [uuid.UUID(branch_id)])


# --- overlaps ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolved_overlap_refuses_and_writes_nothing(client: AsyncClient) -> None:
    slug = "ufm-unresolved"
    branch_id = await _seed(client, slug)
    _project_id, main_id = await _ids(slug)
    await _edit(Event, main_id, {"name": EVENT}, description="main")
    await _edit(Event, branch_id, {"name": EVENT}, description="branch")
    await _edit(EventType, main_id, {"name": "track"}, color="#010101")
    before = await _branch_row(branch_id)
    revisions = await _count(PlanRevision)

    resp = await client.post(
        _url(slug, branch_id),
        json={
            "resolutions": [
                {
                    "entity_type": "variable",
                    "entity_name": "currency",
                    "field_name": "description",
                    "choice": "ours",
                }
            ]
        },
    )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["unresolved_conflicts"] == [
        {"entity_type": "event", "name": f"track.{EVENT}", "field": "description"}
    ]
    assert detail["conflicts"]["overlap_count"] == 1
    entity = detail["conflicts"]["entities"][0]
    assert (entity["parent"], entity["label"]) == ("track", EVENT)
    after = await _branch_row(branch_id)
    assert after.base_revision_id == before.base_revision_id
    assert await _count(PlanRevision) == revisions
    # The inline choice went with the refusal.
    assert await _count(PlanBranchMergeResolution) == 0
    # And main's one-sided change did not land either.
    assert (await _one(EventType, branch_id, name="track")).color != "#010101"


@pytest.mark.asyncio
async def test_keep_branch_choice_inline_survives_as_the_branch_change(
    client: AsyncClient,
) -> None:
    slug = "ufm-theirs"
    branch_id = await _seed(client, slug)
    _project_id, main_id = await _ids(slug)
    await _edit(Event, main_id, {"name": EVENT}, description="main")
    await _edit(Event, branch_id, {"name": EVENT}, description="branch")

    resp = await client.post(
        _url(slug, branch_id),
        json={
            "resolutions": [
                {
                    "entity_type": "event",
                    "entity_name": f"track.{EVENT}",
                    "field_name": "description",
                    "choice": "theirs",
                }
            ]
        },
    )

    assert resp.status_code == 200, resp.text
    assert (await _one(Event, branch_id, name=EVENT)).description == "branch"
    diff = (await client.get(_url(slug, branch_id, "diff"))).json()
    [entry] = diff["entries"]
    assert [change["field"] for change in entry["field_changes"]] == ["description"]
    assert entry["field_changes"][0]["before"] == "main"
    # Resolutions were against the old base, and it is gone.
    assert await _count(PlanBranchMergeResolution) == 0


@pytest.mark.asyncio
async def test_take_main_choice_stored_beforehand(client: AsyncClient) -> None:
    slug = "ufm-ours"
    branch_id = await _seed(client, slug)
    _project_id, main_id = await _ids(slug)
    await _edit(Variable, main_id, {"name": "currency"}, description="main")
    await _edit(Variable, branch_id, {"name": "currency"}, description="branch")

    saved = await client.post(
        _url(slug, branch_id, "resolutions"),
        json={
            "entity_type": "variable",
            "entity_name": "currency",
            "field_name": "description",
            "choice": "ours",
        },
    )
    assert saved.status_code == 201, saved.text
    # Without the preview's hash a stored choice does not count: it may have
    # been made before main changed that field again.
    blind = await client.post(_url(slug, branch_id), json={})
    assert blind.status_code == 409
    assert blind.json()["detail"]["unresolved_conflicts"] == [
        {"entity_type": "variable", "name": "currency", "field": "description"}
    ]

    preview = (await client.get(_url(slug, branch_id))).json()
    assert preview["conflicts"]["unresolved_count"] == 0
    resp = await client.post(
        _url(slug, branch_id), json={"expected_main_hash": preview["main_hash"]}
    )

    assert resp.status_code == 200, resp.text
    assert (await _one(Variable, branch_id, name="currency")).description == "main"
    diff = (await client.get(_url(slug, branch_id, "diff"))).json()
    assert diff["entries"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["ours", "theirs"])
async def test_main_deleted_what_the_branch_edited(client: AsyncClient, choice: str) -> None:
    slug = f"ufm-del-{choice}"
    branch_id = await _seed(client, slug)
    _project_id, main_id = await _ids(slug)
    async with TestSessionLocal() as session:
        main_event = await session.scalar(
            select(Event).where(Event.branch_id == main_id, Event.name == EVENT)
        )
        await session.delete(main_event)
        await session.commit()
    await _edit(Event, branch_id, {"name": EVENT}, description="kept")

    refused = await client.post(_url(slug, branch_id), json={})
    assert refused.status_code == 409
    assert refused.json()["detail"]["unresolved_conflicts"] == [
        {"entity_type": "event", "name": f"track.{EVENT}", "field": "@presence"}
    ]

    resp = await client.post(
        _url(slug, branch_id),
        json={
            "resolutions": [
                {
                    "entity_type": "event",
                    "entity_name": f"track.{EVENT}",
                    "field_name": "@presence",
                    "choice": choice,
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    kept = await _one(Event, branch_id, name=EVENT)
    if choice == "ours":
        assert kept is None
        return
    assert kept is not None and kept.origin_id is None
    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text
    recreated = await _one(Event, main_id, name=EVENT)
    assert recreated is not None and recreated.description == "kept"


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["ours", "theirs"])
async def test_parent_deleted_on_main_while_branch_added_a_child(
    client: AsyncClient, choice: str
) -> None:
    slug = f"ufm-parent-{choice}"
    assert (
        await client.post("/api/v1/projects", json={"name": slug, "slug": slug, "description": ""})
    ).status_code == 201
    project_id, main_id = await _ids(slug)
    async with TestSessionLocal() as session:
        session.add(
            EventType(project_id=project_id, branch_id=main_id, name="screen", display_name="S")
        )
        await session.commit()
    branch_id = (
        await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    ).json()["id"]
    branch_screen = await _one(EventType, branch_id, name="screen")
    async with TestSessionLocal() as session:
        session.add(
            Event(
                project_id=project_id,
                branch_id=uuid.UUID(branch_id),
                event_type_id=branch_screen.id,
                name="opened",
            )
        )
        main_screen = await session.scalar(
            select(EventType).where(EventType.branch_id == main_id, EventType.name == "screen")
        )
        await session.delete(main_screen)
        await session.commit()

    refused = await client.post(_url(slug, branch_id), json={})
    assert refused.status_code == 409
    [entity] = refused.json()["detail"]["conflicts"]["entities"]
    assert (entity["entity_type"], entity["name"]) == ("event_type", "screen")
    assert entity["fields"][0]["field"] == "@presence"
    assert entity["fields"][0]["dependents"] == 1

    resp = await client.post(
        _url(slug, branch_id),
        json={
            "resolutions": [
                {
                    "entity_type": "event_type",
                    "entity_name": "screen",
                    "field_name": "@presence",
                    "choice": choice,
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    screen_left = await _one(EventType, branch_id, name="screen")
    opened_left = await _one(Event, branch_id, name="opened")
    if choice == "ours":
        assert screen_left is None and opened_left is None
    else:
        assert screen_left is not None and opened_left is not None


# --- renames and identity ------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_on_main_lands_on_the_branch_row_and_carries_tokens(
    client: AsyncClient,
) -> None:
    slug = "ufm-rename"
    branch_id = await _seed(client, slug)
    _project_id, main_id = await _ids(slug)
    branch_currency = await _one(Variable, branch_id, name="currency")
    await _edit(Variable, main_id, {"name": "currency"}, name="currency_code")
    await _edit(Variable, branch_id, {"name": "currency"}, description="branch note")

    preview = (await client.get(_url(slug, branch_id))).json()
    assert preview["conflicts"]["overlap_count"] == 0
    resp = await client.post(_url(slug, branch_id), json={})

    assert resp.status_code == 200, resp.text
    renamed = await _one(Variable, branch_id, name="currency_code")
    assert renamed is not None and renamed.id == branch_currency.id
    assert renamed.description == "branch note"
    assert await _one(Variable, branch_id, name="currency") is None
    async with TestSessionLocal() as session:
        values = (
            (
                await session.execute(
                    select(EventFieldValue.value)
                    .join(Event, Event.id == EventFieldValue.event_id)
                    .where(Event.branch_id == uuid.UUID(branch_id))
                )
            )
            .scalars()
            .all()
        )
    assert values == ["${currency_code}"]


@pytest.mark.asyncio
async def test_renames_to_different_names_on_both_sides_ask_for_a_name(
    client: AsyncClient,
) -> None:
    slug = "ufm-rename-both"
    branch_id = await _seed(client, slug)
    _project_id, main_id = await _ids(slug)
    await _edit(Variable, main_id, {"name": "currency"}, name="currency_code")
    await _edit(Variable, branch_id, {"name": "currency"}, name="currency_iso")

    refused = await client.post(_url(slug, branch_id), json={})

    assert refused.status_code == 409
    assert refused.json()["detail"]["unresolved_conflicts"] == [
        {"entity_type": "variable", "name": "currency_iso", "field": "name"}
    ]


@pytest.mark.asyncio
async def test_main_swapping_two_variables_goes_through_the_parking_pass(
    client: AsyncClient,
) -> None:
    slug = "ufm-swap"
    branch_id = await _seed(client, slug)
    _project_id, main_id = await _ids(slug)
    currency_id = (await _one(Variable, branch_id, name="currency")).id
    country_id = (await _one(Variable, branch_id, name="country")).id
    async with TestSessionLocal() as session:
        rows = {
            v.name: v
            for v in (
                await session.execute(select(Variable).where(Variable.branch_id == main_id))
            ).scalars()
        }
        rows["currency"].name = "tmp_swap"
        await session.flush()
        rows["country"].name = "currency"
        await session.flush()
        rows["currency"].name = "country"
        await session.commit()

    resp = await client.post(_url(slug, branch_id), json={})

    assert resp.status_code == 200, resp.text
    assert (await _one(Variable, branch_id, id=currency_id)).name == "country"
    assert (await _one(Variable, branch_id, id=country_id)).name == "currency"


@pytest.mark.asyncio
async def test_main_moving_a_scan_identity_to_a_new_row_frees_it_first(
    client: AsyncClient,
) -> None:
    slug = "ufm-identity"
    branch_id = await _seed(client, slug)
    project_id, main_id = await _ids(slug)
    async with TestSessionLocal() as session:
        country = await session.scalar(
            select(Variable).where(Variable.branch_id == main_id, Variable.name == "country")
        )
        await session.delete(country)
        await session.flush()
        session.add(
            Variable(
                project_id=project_id,
                branch_id=main_id,
                name="region",
                source_name="S_COUNTRY",
                description="",
            )
        )
        await session.commit()

    resp = await client.post(_url(slug, branch_id), json={})

    assert resp.status_code == 200, resp.text
    region = await _one(Variable, branch_id, source_name="S_COUNTRY")
    assert region is not None and region.name == "region"
    assert await _count(Variable, Variable.branch_id == uuid.UUID(branch_id)) == 2


@pytest.mark.asyncio
async def test_main_re_using_a_renamed_name_keeps_its_own_tokens(client: AsyncClient) -> None:
    """Main renames ``currency`` and adds a new ``currency``: its values stay on it."""
    slug = "ufm-reuse"
    branch_id = await _seed(client, slug)
    project_id, main_id = await _ids(slug)
    # A rename on main carries main's own ``${currency}`` values with it, as
    # the variable update does.
    await _rename_variable(project_id, main_id, "currency", "currency_code")
    async with TestSessionLocal() as session:
        session.add(
            Variable(
                project_id=project_id,
                branch_id=main_id,
                name="currency",
                source_name="S_NEW",
                description="",
            )
        )
        main_track = await session.scalar(
            select(EventType).where(EventType.branch_id == main_id, EventType.name == "track")
        )
        assert main_track is not None
        session.add(
            Event(
                project_id=project_id,
                branch_id=main_id,
                event_type_id=main_track.id,
                name="refund",
                description="${currency}",
            )
        )
        await session.commit()

    resp = await client.post(_url(slug, branch_id), json={})

    assert resp.status_code == 200, resp.text
    # Main's new event names main's new variable, and keeps naming it.
    assert (await _one(Event, branch_id, name="refund")).description == "${currency}"
    async with TestSessionLocal() as session:
        values = (
            (
                await session.execute(
                    select(EventFieldValue.value)
                    .join(Event, Event.id == EventFieldValue.event_id)
                    .where(Event.branch_id == uuid.UUID(branch_id), Event.name == EVENT)
                )
            )
            .scalars()
            .all()
        )
    assert values == ["${currency_code}"]


@pytest.mark.asyncio
async def test_main_swapping_two_source_names_goes_through_parking(
    client: AsyncClient,
) -> None:
    slug = "ufm-swap-source"
    branch_id = await _seed(client, slug)
    _project_id, main_id = await _ids(slug)
    currency_id = (await _one(Variable, branch_id, name="currency")).id
    async with TestSessionLocal() as session:
        rows = {
            v.name: v
            for v in (
                await session.execute(select(Variable).where(Variable.branch_id == main_id))
            ).scalars()
        }
        rows["currency"].source_name = None
        await session.flush()
        rows["country"].source_name = "S_CURRENCY"
        await session.flush()
        rows["currency"].source_name = "S_COUNTRY"
        await session.commit()

    resp = await client.post(_url(slug, branch_id), json={})

    assert resp.status_code == 200, resp.text
    assert (await _one(Variable, branch_id, id=currency_id)).source_name == "S_COUNTRY"
    assert (await _one(Variable, branch_id, name="country")).source_name == "S_CURRENCY"


@pytest.mark.asyncio
async def test_a_name_clash_with_the_branch_own_row_is_named_before_the_update(
    client: AsyncClient,
) -> None:
    slug = "ufm-clash"
    branch_id = await _seed(client, slug)
    project_id, main_id = await _ids(slug)
    await _edit(Variable, main_id, {"name": "currency"}, name="money")
    async with TestSessionLocal() as session:
        session.add(
            Variable(
                project_id=project_id,
                branch_id=uuid.UUID(branch_id),
                name="money",
                source_name="S_MONEY",
                description="",
            )
        )
        await session.commit()

    preview = (await client.get(_url(slug, branch_id))).json()
    assert preview["updatable"] is False
    assert [(b["kind"], b["name"]) for b in preview["blockers"]] == [("identity_clash", "money")]
    resp = await client.post(
        _url(slug, branch_id), json={"expected_main_hash": preview["main_hash"]}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["update_blocked"][0]["kind"] == "identity_clash"


# --- gates ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_preview_is_refused(client: AsyncClient) -> None:
    slug = "ufm-stale"
    branch_id = await _seed(client, slug)
    _project_id, main_id = await _ids(slug)
    preview = (await client.get(_url(slug, branch_id))).json()
    await _edit(EventType, main_id, {"name": "track"}, color="#999999")
    before = await _branch_row(branch_id)

    resp = await client.post(
        _url(slug, branch_id), json={"expected_main_hash": preview["main_hash"]}
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["main_moved"] is True
    assert (await _branch_row(branch_id)).base_revision_id == before.base_revision_id


@pytest.mark.asyncio
async def test_gates(client: AsyncClient) -> None:
    slug = "ufm-gates"
    branch_id = await _seed(client, slug)
    project_id, main_id = await _ids(slug)
    await _edit(EventType, main_id, {"name": "track"}, color="#999999")

    assert (await client.post(_url(slug, main_id), json={})).status_code == 400
    assert (await client.post(_url(slug, uuid.uuid4()), json={})).status_code == 404

    # A persisted viewer MEMBER: a non-member would get 404, not 403.
    viewer = await persisted_member_user(project_id, role="viewer")

    async def _viewer() -> User:
        return viewer

    app.dependency_overrides[get_current_user] = _viewer
    try:
        forbidden = await client.post(_url(slug, branch_id), json={})
        readable = await client.get(_url(slug, branch_id))
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert forbidden.status_code == 403
    assert readable.status_code == 200

    async with TestSessionLocal() as session:
        branch = await session.get(PlanBranch, uuid.UUID(branch_id))
        assert branch is not None
        revision = await session.get(PlanRevision, branch.base_revision_id)
        assert revision is not None
        revision.payload = {**revision.payload, "snapshot_version": 1}
        await session.commit()
    legacy = await client.post(_url(slug, branch_id), json={})
    assert legacy.status_code == 409
    assert legacy.json()["detail"]["incomplete_base_snapshot"] is True
    # The preview and the header say so up front, instead of offering choices.
    legacy_preview = (await client.get(_url(slug, branch_id))).json()
    assert legacy_preview["updatable"] is False
    assert [b["kind"] for b in legacy_preview["blockers"]] == ["incomplete_base_snapshot"]
    conflicts = (await client.get(_url(slug, branch_id, "conflicts"))).json()
    assert conflicts["updatable"] is False

    async with TestSessionLocal() as session:
        branch = await session.get(PlanBranch, uuid.UUID(branch_id))
        assert branch is not None
        branch.status = "closed"
        await session.commit()
    closed = await client.post(_url(slug, branch_id), json={})
    assert closed.status_code == 409
    assert closed.json()["detail"] == "Branch is merged/closed; there is nothing to update."


@pytest.mark.asyncio
async def test_constraint_violation_is_a_409_and_leaves_the_session_usable(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    slug = "ufm-integrity"
    branch_id = await _seed(client, slug)
    _project_id, main_id = await _ids(slug)
    await _edit(EventType, main_id, {"name": "track"}, color="#999999")
    before = await _branch_row(branch_id)

    async def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise IntegrityError("UPDATE variables", {}, Exception("duplicate key"))

    monkeypatch.setattr(plan_branch_update_service, "_apply", _explode)
    resp = await client.post(_url(slug, branch_id), json={})

    assert resp.status_code == 409
    assert resp.json()["detail"]["update_constraint_violation"] is True
    assert (await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}")).status_code == 200
    assert (await _branch_row(branch_id)).base_revision_id == before.base_revision_id


# --- the conflicts endpoint -----------------------------------------------------------


@pytest.mark.asyncio
async def test_conflicts_endpoint_reports_every_type_and_the_merge_gate(
    client: AsyncClient,
) -> None:
    slug = "ufm-conflicts"
    branch_id = await _seed(client, slug)
    _project_id, main_id = await _ids(slug)

    level = (await client.get(_url(slug, branch_id, "conflicts"))).json()
    assert (level["behind"], level["overlap_count"], level["merge_blocked"]) == (False, 0, False)

    await _edit(Event, main_id, {"name": EVENT}, description="main")
    await _edit(Event, branch_id, {"name": EVENT}, description="branch")
    await _edit(MetaFieldDefinition, main_id, {"name": "team"}, display_name="Main team")
    await _edit(MetaFieldDefinition, branch_id, {"name": "team"}, display_name="Branch team")

    body = (await client.get(_url(slug, branch_id, "conflicts"))).json()

    assert body["behind"] is True
    assert body["overlap_count"] == 2
    assert body["unresolved_count"] == 2
    # A non-event-type conflict is one the merge refuses outright today.
    assert body["merge_blocked"] is True
    assert [(e["entity_type"], e["name"]) for e in body["entities"]] == [
        ("meta_field", "team"),
        ("event", f"track.{EVENT}"),
    ]

    bad_type = await client.post(
        _url(slug, branch_id, "resolutions"),
        json={"entity_type": "widget", "entity_name": "x", "field_name": "name", "choice": "ours"},
    )
    assert bad_type.status_code == 422
    bad_field = await client.post(
        _url(slug, branch_id, "resolutions"),
        json={
            "entity_type": "event",
            "entity_name": f"track.{EVENT}",
            "field_name": "nonsense",
            "choice": "ours",
        },
    )
    assert bad_field.status_code == 422


# --- references and tokens across renames (review round 2) ------------------------------


async def _event_on(session: AsyncSession, branch_id: uuid.UUID | str, name: str) -> Event:
    event = await session.scalar(
        select(Event).where(Event.branch_id == uuid.UUID(str(branch_id)), Event.name == name)
    )
    assert event is not None, name
    return event


async def _add_main_event(
    session: AsyncSession, project_id: uuid.UUID, main_id: uuid.UUID, name: str, **values: Any
) -> Event:
    track = await session.scalar(
        select(EventType).where(EventType.branch_id == main_id, EventType.name == "track")
    )
    assert track is not None
    event = Event(
        id=uuid.uuid4(),
        project_id=project_id,
        branch_id=main_id,
        event_type_id=track.id,
        name=name,
        **values,
    )
    session.add(event)
    await session.flush()
    return event


async def _field_values(branch_id: uuid.UUID | str, event_name: str) -> dict[str, str]:
    async with TestSessionLocal() as session:
        rows = await session.execute(
            select(FieldDefinition.name, EventFieldValue.value)
            .join(FieldDefinition, FieldDefinition.id == EventFieldValue.field_definition_id)
            .join(Event, Event.id == EventFieldValue.event_id)
            .where(Event.branch_id == uuid.UUID(str(branch_id)), Event.name == event_name)
        )
        return {name: value for name, value in rows.all()}


async def _meta_values(branch_id: uuid.UUID | str, event_name: str) -> dict[str, str]:
    async with TestSessionLocal() as session:
        rows = await session.execute(
            select(MetaFieldDefinition.name, EventMetaValue.value)
            .join(
                MetaFieldDefinition,
                MetaFieldDefinition.id == EventMetaValue.meta_field_definition_id,
            )
            .join(Event, Event.id == EventMetaValue.event_id)
            .where(Event.branch_id == uuid.UUID(str(branch_id)), Event.name == event_name)
        )
        return {name: value for name, value in rows.all()}


async def _overrides(branch_id: uuid.UUID | str, variable: str) -> dict[str, list[Any]]:
    """The branch variable's overrides, keyed by the name of the event they sit on."""
    async with TestSessionLocal() as session:
        rows = await session.execute(
            select(Event.name, VariableEventValueOverride.values)
            .join(Event, Event.id == VariableEventValueOverride.event_id)
            .join(Variable, Variable.id == VariableEventValueOverride.variable_id)
            .where(
                VariableEventValueOverride.branch_id == uuid.UUID(str(branch_id)),
                Variable.name == variable,
            )
        )
        return {name: list(values) for name, values in rows.all()}


async def _delete_branch_event(branch_id: str, name: str) -> None:
    """Delete an event on the branch with what the event delete takes along."""
    async with TestSessionLocal() as session:
        event = await _event_on(session, branch_id, name)
        for model in (EventFieldValue, EventMetaValue, VariableEventValueOverride):
            for row in (
                (await session.execute(select(model).where(model.event_id == event.id)))
                .scalars()
                .all()
            ):
                await session.delete(row)
        for other in (
            (await session.execute(select(Event).where(Event.superseded_by_event_id == event.id)))
            .scalars()
            .all()
        ):
            other.superseded_by_event_id = None
        await session.flush()
        await session.delete(event)
        await session.commit()


@pytest.mark.asyncio
async def test_patched_values_keep_the_tokens_the_rename_pass_rewrote(
    client: AsyncClient,
) -> None:
    """G1/G8: values rebuilt from the branch snapshot name the renamed variable's new name.

    Main renames ``currency`` and adds a field and a meta field with values on
    the event, so both value collections are rebuilt from the snapshots. The
    branch's own ``${currency}`` values were rewritten by the rename pass and
    must not be written back with the old token.
    """
    slug = "ufm-retoken"
    branch_id = await _seed(client, slug)
    project_id, main_id = await _ids(slug)
    async with TestSessionLocal() as session:
        branch_event = await _event_on(session, branch_id, EVENT)
        team = await session.scalar(
            select(MetaFieldDefinition).where(
                MetaFieldDefinition.branch_id == uuid.UUID(branch_id),
                MetaFieldDefinition.name == "team",
            )
        )
        assert team is not None
        session.add(
            EventMetaValue(
                event_id=branch_event.id, meta_field_definition_id=team.id, value="${currency}"
            )
        )
        main_event = await _event_on(session, main_id, EVENT)
        channel = MetaFieldDefinition(
            id=uuid.uuid4(),
            project_id=project_id,
            branch_id=main_id,
            name="channel",
            display_name="Channel",
            field_type="string",
            sensitivity="none",
        )
        amount = FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=main_event.event_type_id,
            name="amount",
            display_name="Amount",
            field_type="string",
        )
        session.add_all([channel, amount])
        await session.flush()
        session.add(
            EventMetaValue(event_id=main_event.id, meta_field_definition_id=channel.id, value="web")
        )
        session.add(
            EventFieldValue(
                event_id=main_event.id, field_definition_id=amount.id, value="42", is_authored=True
            )
        )
        await session.commit()
    await _edit(Variable, main_id, {"name": "currency"}, name="currency_code")

    resp = await client.post(_url(slug, branch_id), json={})

    assert resp.status_code == 200, resp.text
    assert await _field_values(branch_id, EVENT) == {"name": "${currency_code}", "amount": "42"}
    assert await _meta_values(branch_id, EVENT) == {"team": "${currency_code}", "channel": "web"}


@pytest.mark.asyncio
async def test_kept_branch_name_is_the_token_main_values_arrive_with(
    client: AsyncClient,
) -> None:
    """G9: main's new event names ``${main_name}``; the branch kept its own name."""
    slug = "ufm-retoken-main"
    branch_id = await _seed(client, slug)
    project_id, main_id = await _ids(slug)
    await _edit(Variable, main_id, {"name": "currency"}, name="currency_code")
    await _edit(Variable, branch_id, {"name": "currency"}, name="currency_iso")
    async with TestSessionLocal() as session:
        refund = await _add_main_event(session, project_id, main_id, "refund")
        name_field = await session.scalar(
            select(FieldDefinition).where(
                FieldDefinition.event_type_id == refund.event_type_id,
                FieldDefinition.name == "name",
            )
        )
        assert name_field is not None
        session.add(
            EventFieldValue(
                event_id=refund.id,
                field_definition_id=name_field.id,
                value="${currency_code}",
                is_authored=True,
            )
        )
        await session.commit()

    resp = await client.post(
        _url(slug, branch_id),
        json={
            "resolutions": [
                {
                    "entity_type": "variable",
                    "entity_name": "currency_iso",
                    "field_name": "name",
                    "choice": "theirs",
                }
            ]
        },
    )

    assert resp.status_code == 200, resp.text
    assert (await _one(Variable, branch_id, name="currency_iso")) is not None
    assert await _field_values(branch_id, "refund") == {"name": "${currency_iso}"}


@pytest.mark.asyncio
async def test_main_successor_on_an_event_the_branch_renamed_follows_the_origin(
    client: AsyncClient,
) -> None:
    """G2/G5: the successor is found by main's id, not by the name the branch changed."""

    async def extra(session: AsyncSession, project_id: uuid.UUID, main_id: uuid.UUID) -> None:
        await _add_main_event(session, project_id, main_id, "checkout_v2")

    slug = "ufm-successor-renamed"
    branch_id = await _seed(client, slug, extra)
    _project_id, main_id = await _ids(slug)
    await _edit(Event, branch_id, {"name": "checkout_v2"}, name="checkout_v3")
    async with TestSessionLocal() as session:
        successor = await _event_on(session, main_id, "checkout_v2")
        (await _event_on(session, main_id, EVENT)).superseded_by_event_id = successor.id
        await session.commit()

    resp = await client.post(_url(slug, branch_id), json={})

    assert resp.status_code == 200, resp.text
    renamed = await _one(Event, branch_id, name="checkout_v3")
    assert (await _one(Event, branch_id, name=EVENT)).superseded_by_event_id == renamed.id


@pytest.mark.asyncio
async def test_main_successor_among_namesakes_is_the_one_main_named(
    client: AsyncClient,
) -> None:
    """G7: namesakes on main do not refuse the update with the revert's 409."""

    async def extra(session: AsyncSession, project_id: uuid.UUID, main_id: uuid.UUID) -> None:
        await _add_main_event(session, project_id, main_id, "checkout_v2", description="one")
        await _add_main_event(session, project_id, main_id, "checkout_v2", description="two")

    slug = "ufm-successor-namesakes"
    branch_id = await _seed(client, slug, extra)
    _project_id, main_id = await _ids(slug)
    async with TestSessionLocal() as session:
        second = await session.scalar(
            select(Event).where(Event.branch_id == main_id, Event.description == "two")
        )
        assert second is not None
        (await _event_on(session, main_id, EVENT)).superseded_by_event_id = second.id
        await session.commit()
        second_id = second.id

    resp = await client.post(_url(slug, branch_id), json={})

    assert resp.status_code == 200, resp.text
    copy = await _one(Event, branch_id, origin_id=second_id)
    assert copy is not None and copy.description == "two"
    assert (await _one(Event, branch_id, name=EVENT)).superseded_by_event_id == copy.id


@pytest.mark.asyncio
@pytest.mark.parametrize("branch_move", ["rename", "delete"])
async def test_main_override_on_an_event_the_branch_moved_does_not_refuse(
    client: AsyncClient, branch_move: str
) -> None:
    """G3/G4: main's override follows a renamed event and is dropped with a deleted one."""
    slug = f"ufm-override-{branch_move}"
    branch_id = await _seed(client, slug)
    project_id, main_id = await _ids(slug)
    if branch_move == "rename":
        await _edit(Event, branch_id, {"name": EVENT}, name="purchase:renamed")
    else:
        await _delete_branch_event(branch_id, EVENT)
    async with TestSessionLocal() as session:
        currency = await session.scalar(
            select(Variable).where(Variable.branch_id == main_id, Variable.name == "currency")
        )
        assert currency is not None
        session.add(
            VariableEventValueOverride(
                id=uuid.uuid4(),
                project_id=project_id,
                branch_id=main_id,
                variable_id=currency.id,
                event_id=(await _event_on(session, main_id, EVENT)).id,
                values=["EUR"],
            )
        )
        await session.commit()

    resp = await client.post(_url(slug, branch_id), json={})

    assert resp.status_code == 200, resp.text
    expected = {"purchase:renamed": ["EUR"]} if branch_move == "rename" else {}
    assert await _overrides(branch_id, "currency") == expected


@pytest.mark.asyncio
async def test_recreating_a_deleted_event_from_main_restores_references_into_it(
    client: AsyncClient,
) -> None:
    """G6: the override and the successor the branch's deletion dropped come back."""

    async def extra(session: AsyncSession, project_id: uuid.UUID, main_id: uuid.UUID) -> None:
        purchase = await _event_on(session, main_id, EVENT)
        await _add_main_event(
            session, project_id, main_id, "signup", superseded_by_event_id=purchase.id
        )
        currency = await session.scalar(
            select(Variable).where(Variable.branch_id == main_id, Variable.name == "currency")
        )
        assert currency is not None
        session.add(
            VariableEventValueOverride(
                id=uuid.uuid4(),
                project_id=project_id,
                branch_id=main_id,
                variable_id=currency.id,
                event_id=purchase.id,
                values=["USD"],
            )
        )

    slug = "ufm-recreate-refs"
    branch_id = await _seed(client, slug, extra)
    _project_id, main_id = await _ids(slug)
    await _delete_branch_event(branch_id, EVENT)
    assert await _overrides(branch_id, "currency") == {}
    await _edit(Event, main_id, {"name": EVENT}, description="main")

    resp = await client.post(
        _url(slug, branch_id),
        json={
            "resolutions": [
                {
                    "entity_type": "event",
                    "entity_name": f"track.{EVENT}",
                    "field_name": "@presence",
                    "choice": "ours",
                }
            ]
        },
    )

    assert resp.status_code == 200, resp.text
    recreated = await _one(Event, branch_id, name=EVENT)
    assert recreated is not None
    assert await _overrides(branch_id, "currency") == {EVENT: ["USD"]}
    assert (await _one(Event, branch_id, name="signup")).superseded_by_event_id == recreated.id
    diff = (await client.get(_url(slug, branch_id, "diff"))).json()
    assert not [
        entry
        for entry in diff["entries"]
        if (entry["entity_type"], entry["name"]) in {("variable", "currency"), ("event", "signup")}
    ]


@pytest.mark.asyncio
async def test_conflicts_header_is_behind_for_a_cosmetic_overlap(client: AsyncClient) -> None:
    """G11: an overlap on a display name blocks the merge, so the header must offer the update."""
    slug = "ufm-behind-cosmetic"
    branch_id = await _seed(client, slug)
    _project_id, main_id = await _ids(slug)
    async with TestSessionLocal() as session:
        for branch, label in ((main_id, "Main name"), (uuid.UUID(branch_id), "Branch name")):
            field = await session.scalar(
                select(FieldDefinition)
                .join(EventType, EventType.id == FieldDefinition.event_type_id)
                .where(EventType.branch_id == branch, FieldDefinition.name == "name")
            )
            assert field is not None
            field.display_name = label
        await session.commit()

    body = (await client.get(_url(slug, branch_id, "conflicts"))).json()

    assert body["overlap_count"] == 1
    assert body["merge_blocked"] is True
    assert body["behind"] is True
    preview = (await client.get(_url(slug, branch_id))).json()
    assert preview["behind"] is True
    assert preview["conflicts"]["behind"] is True


# --- references across kept rows and renames (review round 3) -----------------------


def _pick(entity_type: str, name: str, field: str, choice: str) -> dict[str, Any]:
    return {"entity_type": entity_type, "entity_name": name, "field_name": field, "choice": choice}


async def _add_override(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | str,
    variable: str,
    event: str,
    values: list[Any],
) -> None:
    owner = uuid.UUID(str(branch_id))
    found = await session.scalar(
        select(Variable).where(Variable.branch_id == owner, Variable.name == variable)
    )
    assert found is not None
    session.add(
        VariableEventValueOverride(
            id=uuid.uuid4(),
            project_id=project_id,
            branch_id=owner,
            variable_id=found.id,
            event_id=(await _event_on(session, owner, event)).id,
            values=values,
        )
    )


async def _delete_main_event(main_id: uuid.UUID, name: str) -> None:
    """Delete an event on main with what the database cascade takes along."""
    await _delete_branch_event(str(main_id), name)


async def _add_label_field(
    session: AsyncSession, project_id: uuid.UUID, main_id: uuid.UUID
) -> None:
    """A second field on ``track``, valued ``x`` on the seeded event."""
    purchase = await _event_on(session, main_id, EVENT)
    label = FieldDefinition(
        id=uuid.uuid4(),
        event_type_id=purchase.event_type_id,
        name="label",
        display_name="Label",
        field_type="string",
    )
    session.add(label)
    await session.flush()
    session.add(
        EventFieldValue(
            event_id=purchase.id, field_definition_id=label.id, value="x", is_authored=True
        )
    )


async def _set_field_value(branch_id: uuid.UUID | str, event: str, field: str, value: str) -> None:
    async with TestSessionLocal() as session:
        row = (
            await session.execute(
                select(EventFieldValue)
                .join(FieldDefinition, FieldDefinition.id == EventFieldValue.field_definition_id)
                .join(Event, Event.id == EventFieldValue.event_id)
                .where(
                    Event.branch_id == uuid.UUID(str(branch_id)),
                    Event.name == event,
                    FieldDefinition.name == field,
                )
            )
        ).scalar_one()
        row.value = value
        await session.commit()


@pytest.mark.asyncio
async def test_keeping_an_event_main_deleted_keeps_the_references_into_it(
    client: AsyncClient,
) -> None:
    """H1/H8/H13: the override on the kept event and the successor into it survive.

    Main also adds an override of its own elsewhere, so the variable's
    overrides are written from main — without wiping the kept one.
    """

    async def extra(session: AsyncSession, project_id: uuid.UUID, main_id: uuid.UUID) -> None:
        purchase = await _event_on(session, main_id, EVENT)
        await _add_main_event(
            session, project_id, main_id, "signup", superseded_by_event_id=purchase.id
        )
        await _add_override(session, project_id, main_id, "currency", EVENT, ["USD"])

    slug = "ufm-kept-refs"
    branch_id = await _seed(client, slug, extra)
    project_id, main_id = await _ids(slug)
    await _delete_main_event(main_id, EVENT)
    async with TestSessionLocal() as session:
        await _add_override(session, project_id, main_id, "currency", "signup", ["EUR"])
        await session.commit()
    await _edit(Event, branch_id, {"name": EVENT}, description="kept")

    resp = await client.post(
        _url(slug, branch_id),
        json={"resolutions": [_pick("event", f"track.{EVENT}", "@presence", "theirs")]},
    )

    assert resp.status_code == 200, resp.text
    kept = await _one(Event, branch_id, name=EVENT)
    assert kept is not None
    assert await _overrides(branch_id, "currency") == {EVENT: ["USD"], "signup": ["EUR"]}
    assert (await _one(Event, branch_id, name="signup")).superseded_by_event_id == kept.id


@pytest.mark.asyncio
async def test_main_deleting_an_event_the_branch_pointed_at_asks_first(
    client: AsyncClient,
) -> None:
    """H15: an override the branch added on the event makes main's deletion a question."""
    slug = "ufm-del-pointed"
    branch_id = await _seed(client, slug)
    project_id, main_id = await _ids(slug)
    await _delete_main_event(main_id, EVENT)
    async with TestSessionLocal() as session:
        await _add_override(session, project_id, branch_id, "currency", EVENT, ["GBP"])
        await session.commit()

    refused = await client.post(_url(slug, branch_id), json={})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["unresolved_conflicts"] == [
        {"entity_type": "event", "name": f"track.{EVENT}", "field": "@presence"}
    ]

    resp = await client.post(
        _url(slug, branch_id),
        json={"resolutions": [_pick("event", f"track.{EVENT}", "@presence", "theirs")]},
    )
    assert resp.status_code == 200, resp.text
    assert await _overrides(branch_id, "currency") == {EVENT: ["GBP"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("renamer", ["branch", "main"])
async def test_a_variable_rename_beside_a_value_edit_is_no_conflict(
    client: AsyncClient, renamer: str
) -> None:
    """H2/H10: tokens are compared by variable, so only the label edit counts."""
    slug = f"ufm-token-{renamer}"
    branch_id = await _seed(client, slug, _add_label_field)
    project_id, main_id = await _ids(slug)
    if renamer == "branch":
        await _rename_variable(project_id, branch_id, "currency", "currency_iso")
        await _set_field_value(main_id, EVENT, "label", "main")
        expected = {"name": "${currency_iso}", "label": "main"}
    else:
        await _rename_variable(project_id, main_id, "currency", "currency_code")
        await _set_field_value(branch_id, EVENT, "label", "branch")
        expected = {"name": "${currency_code}", "label": "branch"}

    preview = (await client.get(_url(slug, branch_id))).json()
    assert preview["conflicts"]["overlap_count"] == 0, preview["conflicts"]
    resp = await client.post(_url(slug, branch_id), json={})

    assert resp.status_code == 200, resp.text
    assert await _field_values(branch_id, EVENT) == expected


@pytest.mark.asyncio
async def test_main_type_deletion_with_only_a_branch_variable_rename_asks_nothing(
    client: AsyncClient,
) -> None:
    """H2: the rewritten ``${token}`` is not the branch's edit of the type's events."""
    slug = "ufm-token-presence"
    branch_id = await _seed(client, slug)
    project_id, main_id = await _ids(slug)
    await _rename_variable(project_id, branch_id, "currency", "currency_iso")
    await _delete_type(main_id, "track")

    preview = (await client.get(_url(slug, branch_id))).json()
    assert preview["conflicts"]["overlap_count"] == 0, preview["conflicts"]
    resp = await client.post(_url(slug, branch_id), json={})
    assert resp.status_code == 200, resp.text
    assert await _one(EventType, branch_id, name="track") is None


@pytest.mark.asyncio
async def test_a_branch_event_rename_does_not_hide_main_reference_edits(
    client: AsyncClient,
) -> None:
    """H3: references compared by event, so main's override and successor edits land."""

    async def extra(session: AsyncSession, project_id: uuid.UUID, main_id: uuid.UUID) -> None:
        await _add_main_event(session, project_id, main_id, "s_old")
        await _add_main_event(session, project_id, main_id, "s_two")
        purchase = await _event_on(session, main_id, EVENT)
        purchase.superseded_by_event_id = (await _event_on(session, main_id, "s_old")).id
        await _add_override(session, project_id, main_id, "currency", "s_old", ["v1"])

    slug = "ufm-event-rename-refs"
    branch_id = await _seed(client, slug, extra)
    _project_id, main_id = await _ids(slug)
    await _edit(Event, branch_id, {"name": "s_old"}, name="s_new")
    async with TestSessionLocal() as session:
        override = await session.scalar(
            select(VariableEventValueOverride).where(
                VariableEventValueOverride.branch_id == main_id
            )
        )
        assert override is not None
        override.values = ["v2"]
        (await _event_on(session, main_id, EVENT)).superseded_by_event_id = (
            await _event_on(session, main_id, "s_two")
        ).id
        await session.commit()

    preview = (await client.get(_url(slug, branch_id))).json()
    assert preview["conflicts"]["overlap_count"] == 0, preview["conflicts"]
    resp = await client.post(_url(slug, branch_id), json={})

    assert resp.status_code == 200, resp.text
    assert await _overrides(branch_id, "currency") == {"s_new": ["v2"]}
    s_two = await _one(Event, branch_id, name="s_two")
    assert (await _one(Event, branch_id, name=EVENT)).superseded_by_event_id == s_two.id


@pytest.mark.asyncio
async def test_main_re_adding_an_event_under_its_name_repoints_the_copy(
    client: AsyncClient,
) -> None:
    """H4: main's new row at the key becomes the copy's origin; its override lands."""
    slug = "ufm-readd"
    branch_id = await _seed(client, slug)
    project_id, main_id = await _ids(slug)
    await _delete_main_event(main_id, EVENT)
    async with TestSessionLocal() as session:
        readded = await _add_main_event(session, project_id, main_id, EVENT)
        await _add_override(session, project_id, main_id, "currency", EVENT, ["EUR"])
        await session.commit()
        readded_id = readded.id

    resp = await client.post(_url(slug, branch_id), json={})

    assert resp.status_code == 200, resp.text
    copy = await _one(Event, branch_id, name=EVENT)
    assert copy.origin_id == readded_id
    assert await _overrides(branch_id, "currency") == {EVENT: ["EUR"]}


async def _delete_type(branch_id: uuid.UUID | str, name: str) -> None:
    """Delete an event type with its events and fields, as the cascade would."""
    owner = uuid.UUID(str(branch_id))
    async with TestSessionLocal() as session:
        event_type = await session.scalar(
            select(EventType).where(EventType.branch_id == owner, EventType.name == name)
        )
        assert event_type is not None
        names = list(
            (await session.execute(select(Event.name).where(Event.event_type_id == event_type.id)))
            .scalars()
            .all()
        )
    for event_name in names:
        await _delete_branch_event(str(owner), event_name)
    async with TestSessionLocal() as session:
        event_type = await session.scalar(
            select(EventType).where(EventType.branch_id == owner, EventType.name == name)
        )
        assert event_type is not None
        for fd in (
            (
                await session.execute(
                    select(FieldDefinition).where(FieldDefinition.event_type_id == event_type.id)
                )
            )
            .scalars()
            .all()
        ):
            await session.delete(fd)
        await session.flush()
        await session.delete(event_type)
        await session.commit()


@pytest.mark.asyncio
async def test_events_rebuilt_with_their_type_take_the_branch_variable_names(
    client: AsyncClient,
) -> None:
    """H5/H7/H14: children created while types are visited are re-tokened too."""
    slug = "ufm-type-retoken"
    branch_id = await _seed(client, slug)
    project_id, main_id = await _ids(slug)
    await _rename_variable(project_id, branch_id, "currency", "currency_iso")
    await _delete_type(branch_id, "track")
    await _edit(EventType, main_id, {"name": "track"}, color="#123456")

    resp = await client.post(
        _url(slug, branch_id),
        json={"resolutions": [_pick("event_type", "track", "@presence", "ours")]},
    )

    assert resp.status_code == 200, resp.text
    assert await _field_values(branch_id, EVENT) == {"name": "${currency_iso}"}


@pytest.mark.asyncio
async def test_rows_created_from_main_bring_its_observed_value_contexts(
    client: AsyncClient,
) -> None:
    """H6: a created event carries main's ``VariableValue`` contexts, as a new branch would."""
    from tripl.models.variable_value import VariableValue

    slug = "ufm-contexts"
    branch_id = await _seed(client, slug)
    project_id, main_id = await _ids(slug)
    async with TestSessionLocal() as session:
        refund = await _add_main_event(session, project_id, main_id, "refund")
        name_field = await session.scalar(
            select(FieldDefinition).where(
                FieldDefinition.event_type_id == refund.event_type_id,
                FieldDefinition.name == "name",
            )
        )
        currency = await session.scalar(
            select(Variable).where(Variable.branch_id == main_id, Variable.name == "currency")
        )
        assert name_field is not None and currency is not None
        session.add(
            VariableValue(
                id=uuid.uuid4(),
                project_id=project_id,
                branch_id=main_id,
                variable_id=currency.id,
                event_id=refund.id,
                field_definition_id=name_field.id,
                source_column="currency",
                observed_count=3,
                values=["USD", "EUR"],
            )
        )
        await session.commit()

    resp = await client.post(_url(slug, branch_id), json={})

    assert resp.status_code == 200, resp.text
    copy = await _one(Event, branch_id, name="refund")
    context = await _one(VariableValue, branch_id, event_id=copy.id)
    branch_currency = await _one(Variable, branch_id, name="currency")
    assert context is not None
    assert (context.variable_id, context.observed_count, context.values) == (
        branch_currency.id,
        3,
        ["USD", "EUR"],
    )


@pytest.mark.asyncio
async def test_a_reference_into_legacy_namesakes_is_a_blocker_in_the_preview(
    client: AsyncClient,
) -> None:
    """H9: the preview names what the apply would refuse, instead of offering the update."""

    async def extra(session: AsyncSession, project_id: uuid.UUID, main_id: uuid.UUID) -> None:
        await _add_main_event(session, project_id, main_id, "dup")

    slug = "ufm-legacy-ref"
    branch_id = await _seed(client, slug, extra)
    project_id, main_id = await _ids(slug)
    async with TestSessionLocal() as session:
        branch = await session.get(PlanBranch, uuid.UUID(branch_id))
        assert branch is not None
        branch.origin_ids_complete = False
        copy = await _event_on(session, branch_id, "dup")
        copy.origin_id = None
        session.add(
            Event(
                id=uuid.uuid4(),
                project_id=project_id,
                branch_id=uuid.UUID(branch_id),
                event_type_id=copy.event_type_id,
                name="dup",
                description="branch namesake",
            )
        )
        await session.flush()
        await _add_override(session, project_id, main_id, "currency", "dup", ["X"])
        await session.commit()

    preview = (await client.get(_url(slug, branch_id))).json()
    assert preview["updatable"] is False
    assert [(b["kind"], b["name"]) for b in preview["blockers"]] == [("ambiguous", "track.dup")]
    resp = await client.post(_url(slug, branch_id), json={})
    assert resp.status_code == 409
    assert "update_blocked" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_taking_main_photos_takes_the_uploader_of_a_matched_photo(
    client: AsyncClient,
) -> None:
    """H16: the kept branch row takes main's uploader, so no phantom photo change is left."""
    from tripl.models.event_photo import EventPhoto

    slug = "ufm-photo-uploader"
    branch_id = await _seed(client, slug)
    project_id, main_id = await _ids(slug)
    async with TestSessionLocal() as session:
        user_id = await session.scalar(select(User.id).limit(1))
        assert user_id is not None
        for owner, uploader in ((main_id, user_id), (uuid.UUID(branch_id), None)):
            session.add(
                EventPhoto(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    event_id=(await _event_on(session, owner, EVENT)).id,
                    uploaded_by_user_id=uploader,
                    kind="figma",
                    external_url="https://figma.example/one",
                )
            )
        await session.commit()

    resp = await client.post(
        _url(slug, branch_id),
        json={"resolutions": [_pick("event", f"track.{EVENT}", "photos", "ours")]},
    )

    assert resp.status_code == 200, resp.text
    async with TestSessionLocal() as session:
        branch_event = await _event_on(session, branch_id, EVENT)
        uploaders = (
            (
                await session.execute(
                    select(EventPhoto.uploaded_by_user_id).where(
                        EventPhoto.event_id == branch_event.id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert uploaders == [user_id]
