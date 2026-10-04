"""Batch 7, R3: the branch copy and the merge, repaired after review.

tripl-0zpq.123, second half — the batch closed the write door for an event
pointing outside its own branch (``event_service`` now scopes both the event
type and the meta-field definitions it accepts) and taught the RELATION copy in
``deep_copy_plan_to_branch`` to answer 409 for the rows stored before that
refusal. The event children in the same function kept their unqualified
subscripts, so one such row still made "create a branch" a bare 500 naming
nothing, and the meta-value replay in ``_apply_merge`` made the merge one too.
Both now refuse by name.

tripl-0zpq.149 is deliberately NOT covered here any more. This file used to hold
the base arm of an ambiguous-natural-key refusal — ``_ambiguous_keys``,
``_reject_ambiguous_keys`` and the call in ``merge_branch`` — and that whole
refusal was removed after review, so the test went with it. No coverage was
lost: a branch is how an analyst CLEANS UP a pair of namesakes (delete both
copies, author one row in their place), and refusing that merge took away the
only door out of the very state it complained about, which is the workflow two
tests in ``test_event_comment_merge_batch2`` already pin. The diff still warns
on the row — ``plan_revision_service._shared_key_warning``, held by
``test_plan_revision_batch2`` — and the merge's half of the ticket waits on
tripl-0zpq.292, an origin id on branch copies, which is the one thing that lets
rows sharing a key be paired instead of refused.

tripl-0zpq.128, the main side — the KeyError guard on ``relation_key`` was added
to the branch comprehension only, while ``main_relation_by_key`` still indexed
main's event-type names unguarded.

Each test names the production line whose revert reddens it.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_meta_value import EventMetaValue
from tripl.models.event_type import EventType
from tripl.models.event_type_relation import EventTypeRelation
from tripl.models.field_definition import FieldDefinition
from tripl.models.meta_field_definition import MetaFieldDefinition
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_plan_branches import _approve_and_merge, _create_branch, _transition

# --- helpers ------------------------------------------------------------------


async def _project(client: AsyncClient, slug: str) -> None:
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text


async def _event_type(client: AsyncClient, slug: str, name: str = "pv") -> str:
    resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": name, "display_name": name.title()},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _field(client: AsyncClient, slug: str, event_type_id: str, name: str) -> str:
    resp = await client.post(
        f"/api/v1/projects/{slug}/event-types/{event_type_id}/fields",
        json={"name": name, "display_name": name.title(), "field_type": "string"},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _event(
    client: AsyncClient,
    slug: str,
    event_type_id: str,
    name: str,
    **extra: object,
) -> str:
    resp = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": event_type_id, "name": name, **extra},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _meta_field(client: AsyncClient, slug: str, name: str) -> str:
    resp = await client.post(
        f"/api/v1/projects/{slug}/meta-fields",
        json={"name": name, "display_name": name.title(), "field_type": "string"},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _branch_event_type_id(branch_id: str, name: str) -> uuid.UUID:
    """The id the deep copy minted for event type ``name`` on ``branch_id``.

    Read from the database rather than a list endpoint: these are the ids a
    client is NOT supposed to be able to mix with another branch's, which is the
    whole point of the rows planted below.
    """
    async with TestSessionLocal() as session:
        found = await session.scalar(
            select(EventType.id).where(
                EventType.branch_id == uuid.UUID(branch_id),
                EventType.name == name,
            )
        )
    assert found is not None, f"event type '{name}' was not copied to the branch"
    return found


async def _branch_meta_field_id(branch_id: str, name: str) -> uuid.UUID:
    async with TestSessionLocal() as session:
        found = await session.scalar(
            select(MetaFieldDefinition.id).where(
                MetaFieldDefinition.branch_id == uuid.UUID(branch_id),
                MetaFieldDefinition.name == name,
            )
        )
    assert found is not None, f"meta field '{name}' was not copied to the branch"
    return found


async def _field_id_under(event_type_id: uuid.UUID, name: str) -> uuid.UUID:
    """A field definition carries no ``branch_id`` — its event type holds it."""
    async with TestSessionLocal() as session:
        found = await session.scalar(
            select(FieldDefinition.id).where(
                FieldDefinition.event_type_id == event_type_id,
                FieldDefinition.name == name,
            )
        )
    assert found is not None, f"field '{name}' was not copied to the branch"
    return found


# --- the branch copy refuses by name --------------------------


@pytest.mark.asyncio
async def test_a_main_event_holding_another_branchs_meta_field_names_itself(
    client: AsyncClient,
) -> None:
    """Dropping the ``dangling`` guard from ``deep_copy_plan_to_branch``'s event
    loop reddens this: ``mf_map[mv.meta_field_definition_id]`` is a KeyError, so
    creating a branch stops being a 409 that names the event and becomes a bare
    500 that names nothing — and the project can never take a branch again.

    The row is the one the batch's own ``_normalize_meta_values`` docstring
    describes: before that scoping, a main event could be posted with a meta
    field definition belonging to another branch, and no migration sweeps the
    ones already stored.
    """
    slug = "b7b-meta-copy"
    await _project(client, slug)
    event_type_id = await _event_type(client, slug)
    event_id = await _event(client, slug, event_type_id, "purchase")
    await _meta_field(client, slug, "jira")

    branch_id = await _create_branch(client, slug, name="first")
    other_branch_meta_id = await _branch_meta_field_id(branch_id, "jira")

    async with TestSessionLocal() as session:
        session.add(
            EventMetaValue(
                event_id=uuid.UUID(event_id),
                meta_field_definition_id=other_branch_meta_id,
                value="TRIP-1",
            )
        )
        await session.commit()

    refused = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "second"})
    assert refused.status_code == 409, refused.text
    detail = str(refused.json()["detail"])
    assert "purchase" in detail, detail
    assert str(other_branch_meta_id) in detail, detail
    # The remedy has to be one the operator can actually perform: the event type
    # is fine, so dropping the value from a PATCH is the whole repair.
    assert "Edit the event to drop those values" in detail, detail

    # Not vacuous: that row is the only thing standing in the way.
    async with TestSessionLocal() as session:
        planted = await session.scalar(
            select(EventMetaValue).where(
                EventMetaValue.meta_field_definition_id == other_branch_meta_id
            )
        )
        assert planted is not None
        await session.delete(planted)
        await session.commit()
    allowed = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "third"})
    assert allowed.status_code == 201, allowed.text


@pytest.mark.asyncio
async def test_a_main_event_parented_by_another_branchs_type_names_itself(
    client: AsyncClient,
) -> None:
    """The same guard, reached through its other two arms — the event's own
    ``event_type_id`` and a field value's ``field_definition_id``. Reverting it
    turns ``et_map[ev.event_type_id]`` back into the KeyError.

    ``EventUpdate`` carries no ``event_type_id``, so this arm gets the other
    remedy: there is no route that re-points a mis-parented event, and the
    message must not tell the operator to edit one.
    """
    slug = "b7b-orphan-copy"
    await _project(client, slug)
    event_type_id = await _event_type(client, slug)
    field_id = await _field(client, slug, event_type_id, "user_id")
    event_id = await _event(
        client,
        slug,
        event_type_id,
        "purchase",
        field_values=[{"field_definition_id": field_id, "value": "u-1"}],
    )

    branch_id = await _create_branch(client, slug, name="first")
    other_branch_type_id = await _branch_event_type_id(branch_id, "pv")
    other_branch_field_id = await _field_id_under(other_branch_type_id, "user_id")

    async with TestSessionLocal() as session:
        event = await session.get(Event, uuid.UUID(event_id))
        assert event is not None
        event.event_type_id = other_branch_type_id
        value = await session.scalar(
            select(EventFieldValue).where(EventFieldValue.event_id == event.id)
        )
        assert value is not None
        value.field_definition_id = other_branch_field_id
        await session.commit()

    refused = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "second"})
    assert refused.status_code == 409, refused.text
    detail = str(refused.json()["detail"])
    # Every dangling id, not just the first one the loop tripped over.
    assert str(other_branch_type_id) in detail, detail
    assert str(other_branch_field_id) in detail, detail
    assert "Delete the event, then create the branch." in detail, detail

    # Not vacuous, and the remedy the message names is the one that works.
    removed = await client.delete(f"/api/v1/projects/{slug}/events/{event_id}")
    assert removed.status_code == 204, removed.text
    allowed = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "third"})
    assert allowed.status_code == 201, allowed.text


@pytest.mark.asyncio
async def test_a_branch_event_holding_mains_meta_field_refuses_the_merge(
    client: AsyncClient,
) -> None:
    """Reverting ``main_meta_field_id`` to the bare
    ``branch_mf_id_to_name[mv.meta_field_definition_id]`` reddens this: the
    merge's meta-value replay is the second place a pre-refusal row surfaces,
    and it was the same bare 500.

    409 rather than skipping the value: both replay arms DELETE main's whole set
    for the event and rebuild it from the branch, so a skipped value is one that
    disappears from main with nothing said.
    """
    slug = "b7b-meta-merge"
    await _project(client, slug)
    event_type_id = await _event_type(client, slug)
    await _event(client, slug, event_type_id, "purchase")
    main_meta_id = await _meta_field(client, slug, "jira")

    branch_id = await _create_branch(client, slug)
    async with TestSessionLocal() as session:
        branch_event_id = await session.scalar(
            select(Event.id).where(
                Event.branch_id == uuid.UUID(branch_id),
                Event.name == "purchase",
            )
        )
        assert branch_event_id is not None
        session.add(
            EventMetaValue(
                event_id=branch_event_id,
                meta_field_definition_id=uuid.UUID(main_meta_id),
                value="TRIP-1",
            )
        )
        await session.commit()

    refused = await _approve_and_merge(client, slug, branch_id)
    assert refused.status_code == 409, refused.text
    detail = str(refused.json()["detail"])
    assert "purchase" in detail, detail
    assert main_meta_id in detail, detail
    assert "Edit the event to drop that value, then merge." in detail, detail

    # Not vacuous: the identical merge goes through once the value the message
    # names is gone. Dropping it EDITS the branch, so the approval pinned to the
    # old content hash reads stale and the merge would answer 409
    # insufficient_approvals instead — a different refusal from the one under
    # test. The reviewer approves again, which is legal from "approved" and
    # restamps the hash in place, exactly as ``_approve_and_merge`` stamps it.
    async with TestSessionLocal() as session:
        planted = await session.scalar(
            select(EventMetaValue).where(EventMetaValue.event_id == branch_event_id)
        )
        assert planted is not None
        await session.delete(planted)
        await session.commit()
    assert "_status" not in await _transition(client, slug, branch_id, "approve")
    merged = await client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge")
    assert merged.status_code == 200, merged.text


# --- the main side of the relation guard ----------------------


@pytest.mark.asyncio
async def test_a_main_relation_naming_another_branchs_type_does_not_500_the_merge(
    client: AsyncClient,
) -> None:
    """Dropping the two ``main_et_id_to_name_after`` terms from
    ``main_relation_by_key`` reddens this: ``relation_key`` indexes that map
    directly, so a main relation holding a branch copy's event-type id is a
    KeyError, i.e. every merge in the project is a bare 500.

    The four ids on a relation were only ever checked by their foreign keys,
    which any branch's row satisfies, so this row shape predates
    ``create_relation``'s refusal and no migration sweeps it.
    """
    slug = "b7b-relation-main"
    await _project(client, slug)
    source_type_id = await _event_type(client, slug, "se")
    source_field_id = await _field(client, slug, source_type_id, "user_id")
    target_type_id = await _event_type(client, slug, "pv")
    target_field_id = await _field(client, slug, target_type_id, "user_id")
    created = await client.post(
        f"/api/v1/projects/{slug}/relations",
        json={
            "source_event_type_id": source_type_id,
            "target_event_type_id": target_type_id,
            "source_field_id": source_field_id,
            "target_field_id": target_field_id,
        },
    )
    assert created.status_code == 201, created.text

    branch_id = await _create_branch(client, slug)
    other_branch_type_id = await _branch_event_type_id(branch_id, "se")

    async with TestSessionLocal() as session:
        # Only the event-type end is re-pointed: the field ids stay main's, so
        # the pre-existing field-only guard passes this row straight through to
        # ``relation_key``.
        relation = await session.get(EventTypeRelation, uuid.UUID(created.json()["id"]))
        assert relation is not None
        relation.source_event_type_id = other_branch_type_id
        await session.commit()

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text
    # Skipped, not re-added: the branch's copy of the relation is in the base
    # too, so an unresolvable main row must not turn the merge into an insert.
    relations = await client.get(f"/api/v1/projects/{slug}/relations")
    assert relations.status_code == 200, relations.text
    assert len(relations.json()) == 1, relations.text
