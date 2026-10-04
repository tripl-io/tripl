"""Reverting a branch change against an older base or an awkward name.

- The revert reads the base snapshot the way the diff reads it,
  through ``with_snapshot_defaults``. A base taken before a key joined v2 has no
  such key; the diff shows the default as the base value, so the revert has to
  put back that default — not ``None``.
- The successor pointer is stored as ``"<type>.<name>"`` and is
  resolved by spelling that key back whole. Dotted names resolve, and a key two
  branch events answer to is a 409 rather than a guess or a 500 — as is a key
  two BASE events answered to. The event being reverted is never its own
  successor, though it can spell the key itself.
- Rebuilding a deleted event puts its successor back, resolved the
  way the field revert resolves it — including both 409s.
- Namesakes (cut back): a branch copy does not record which base
  row it came from, so when several base rows answer to the key a change or a
  removal names, the revert is a 409 rather than a restore from an arbitrary
  one of them.
- An event's type is part of its diff key, never a changed
  field, so there is no ``event_type_name`` for the revert to restore.
- A merged branch cannot be reopened, so its refusal does not say to reopen it.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.plan_branch import BranchStatus, PlanBranch
from tripl.models.plan_revision import PlanRevision
from tripl.services.plan_revision_service import PLAN_SNAPSHOT_VERSION
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_plan_branches import _create_branch, _seed_plan


async def _project(client: AsyncClient, slug: str) -> None:
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text


async def _event_type(
    client: AsyncClient, slug: str, name: str, branch_id: str | None = None
) -> str:
    where = f"?branch={branch_id}" if branch_id else ""
    resp = await client.post(
        f"/api/v1/projects/{slug}/event-types{where}", json={"name": name, "display_name": name}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _event(
    client: AsyncClient,
    slug: str,
    event_type_id: str,
    name: str,
    branch_id: str | None = None,
    description: str = "",
) -> str:
    where = f"?branch={branch_id}" if branch_id else ""
    resp = await client.post(
        f"/api/v1/projects/{slug}/events{where}",
        json={"event_type_id": event_type_id, "name": name, "description": description},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _point_at(
    client: AsyncClient,
    slug: str,
    event_id: str,
    successor_id: str | None,
    branch_id: str | None = None,
) -> None:
    where = f"?branch={branch_id}" if branch_id else ""
    resp = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}{where}",
        json={"superseded_by_event_id": successor_id},
    )
    assert resp.status_code == 200, resp.text


async def _delete_event(client: AsyncClient, slug: str, branch_id: str, event_id: str) -> None:
    gone = await client.delete(f"/api/v1/projects/{slug}/events/{event_id}?branch={branch_id}")
    assert gone.status_code == 204, gone.text


async def _branch_type_id(client: AsyncClient, slug: str, branch_id: str, name: str) -> str:
    resp = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    assert resp.status_code == 200, resp.text
    return next(et["id"] for et in resp.json() if et["name"] == name)


async def _branch_event_id(
    client: AsyncClient, slug: str, branch_id: str, type_name: str, name: str
) -> str:
    """The one branch event keyed (type_name, name) — the diff's own key."""
    type_id = await _branch_type_id(client, slug, branch_id, type_name)
    resp = await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")
    assert resp.status_code == 200, resp.text
    [event_id] = [
        e["id"] for e in resp.json()["items"] if e["event_type_id"] == type_id and e["name"] == name
    ]
    return event_id


async def _event_detail(client: AsyncClient, slug: str, branch_id: str, event_id: str) -> dict:
    """The DETAIL response: the list variant drops ``superseded_by_event_id``."""
    resp = await client.get(f"/api/v1/projects/{slug}/events/{event_id}?branch={branch_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _revert(client: AsyncClient, slug: str, branch_id: str, **body: str):
    return await client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/revert", json=body)


async def _diff(client: AsyncClient, slug: str, branch_id: str) -> dict:
    resp = await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _event_entries(client: AsyncClient, slug: str, branch_id: str) -> list[dict]:
    return [
        e for e in (await _diff(client, slug, branch_id))["entries"] if e["entity_type"] == "event"
    ]


async def _age_base(
    branch_id: str,
    *,
    events: tuple[str, ...] = (),
    meta_fields: tuple[str, ...] = (),
) -> None:
    """Strip keys from the branch's base snapshot, as a base older than them lacks them.

    The version stays current. These keys joined v2 without a bump, which is
    the whole reason ``with_snapshot_defaults`` exists, so a real pre-2026-09-08
    base is a v2 payload that simply has no ``title`` on its events.
    """
    async with TestSessionLocal() as session:
        branch = await session.get(PlanBranch, uuid.UUID(branch_id))
        assert branch is not None and branch.base_revision_id is not None
        revision = await session.get(PlanRevision, branch.base_revision_id)
        assert revision is not None
        assert revision.payload["snapshot_version"] == PLAN_SNAPSHOT_VERSION
        for collection, keys in (("events", events), ("meta_fields", meta_fields)):
            for item in revision.payload.get(collection, []):
                for key in keys:
                    item.pop(key, None)
        flag_modified(revision, "payload")
        await session.commit()


# --- the base is read through with_snapshot_defaults ----------


async def _titled_event_on_an_untitled_base(client: AsyncClient, slug: str) -> tuple[str, str, str]:
    """A branch whose base predates ``title`` and ``superseded_by``, with both set on it.

    Returns (branch_id, event_id, successor_id), the ids being the branch's.
    """
    et_id = await _seed_plan(client, slug)
    await _event(client, slug, et_id, "purchase:v2")
    branch_id = await _create_branch(client, slug)
    await _age_base(branch_id, events=("title", "superseded_by"))

    event_id = await _branch_event_id(client, slug, branch_id, "track", "purchase:success")
    successor_id = await _branch_event_id(client, slug, branch_id, "track", "purchase:v2")
    edit = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}?branch={branch_id}",
        json={"title": "Tap on a model card", "superseded_by_event_id": successor_id},
    )
    assert edit.status_code == 200, edit.text

    # The diff reads the absent keys as their defaults: that '' is the base
    # value the reviewer is shown, and so the value a revert must restore.
    entry = next(
        e
        for e in (await _diff(client, slug, branch_id))["entries"]
        if e["name"] == "purchase:success"
    )
    changes = {fc["field"]: (fc["before"], fc["after"]) for fc in entry["field_changes"]}
    assert changes["title"] == ("", "Tap on a model card")
    assert changes["superseded_by"] == (None, "track.purchase:v2")
    return branch_id, event_id, successor_id


@pytest.mark.asyncio
async def test_reverting_a_title_puts_back_the_empty_default_of_an_older_base(
    client: AsyncClient,
) -> None:
    """The reported case: the base value is absent, not NULL, and the column is NOT NULL.

    Reading the raw payload wrote ``title = NULL``, which the database refused
    and the handler reported as a name clash — so the revert failed every time.
    """
    slug = "revert-untitled-base-field"
    branch_id, event_id, successor_id = await _titled_event_on_an_untitled_base(client, slug)

    resp = await _revert(
        client,
        slug,
        branch_id,
        entity_type="event",
        name="purchase:success",
        parent="track",
        field="title",
    )
    assert resp.status_code == 200, resp.text
    [entry] = resp.json()["entries"]
    assert [fc["field"] for fc in entry["field_changes"]] == ["superseded_by"]
    restored = await _event_detail(client, slug, branch_id, event_id)
    assert restored["title"] == ""
    # One field was asked for, and only that one moved.
    assert restored["superseded_by_event_id"] == successor_id


@pytest.mark.asyncio
async def test_reverting_a_whole_event_lands_level_with_an_older_base(
    client: AsyncClient,
) -> None:
    """Every key the defaults fill for an event, reverted together: none may block the rest."""
    slug = "revert-untitled-base-entity"
    branch_id, event_id, _successor_id = await _titled_event_on_an_untitled_base(client, slug)

    resp = await _revert(
        client, slug, branch_id, entity_type="event", name="purchase:success", parent="track"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["entries"] == []
    restored = await _event_detail(client, slug, branch_id, event_id)
    assert restored["title"] == ""
    assert restored["superseded_by_event_id"] is None


@pytest.mark.asyncio
async def test_reverting_allow_multiple_puts_back_the_default_of_an_older_base(
    client: AsyncClient,
) -> None:
    """The defaults' other collection, reached through a field revert.

    ``allow_multiple`` is a diff change key, so reverting it
    writes the base value straight onto the column. A base taken before the
    key existed has none; the diff shows the default ``False`` as the base
    value, and reading the raw payload wrote ``NULL`` into a NOT NULL column
    instead — refused, and reported as a name clash. A rebuild of the whole
    meta field cannot tell the two apart (it falls back to the column default
    on its own), which is why this goes through the field.
    """
    slug = "revert-old-base-multi"
    await _seed_plan(client, slug)
    created = await client.post(
        f"/api/v1/projects/{slug}/meta-fields",
        json={"name": "jira", "display_name": "Jira ticket", "field_type": "string"},
    )
    assert created.status_code == 201, created.text
    branch_id = await _create_branch(client, slug)
    await _age_base(branch_id, meta_fields=("allow_multiple",))

    listed = await client.get(f"/api/v1/projects/{slug}/meta-fields?branch={branch_id}")
    branch_mf_id = next(mf["id"] for mf in listed.json() if mf["name"] == "jira")
    flipped = await client.patch(
        f"/api/v1/projects/{slug}/meta-fields/{branch_mf_id}?branch={branch_id}",
        json={"allow_multiple": True},
    )
    assert flipped.status_code == 200, flipped.text
    [entry] = (await _diff(client, slug, branch_id))["entries"]
    assert [(fc["field"], fc["before"], fc["after"]) for fc in entry["field_changes"]] == [
        ("allow_multiple", False, True)
    ]

    resp = await _revert(
        client, slug, branch_id, entity_type="meta_field", name="jira", field="allow_multiple"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["entries"] == []
    listed = await client.get(f"/api/v1/projects/{slug}/meta-fields?branch={branch_id}")
    restored = next(mf for mf in listed.json() if mf["name"] == "jira")
    assert restored["allow_multiple"] is False


# --- the successor key is spelled back whole -------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("type_name", "successor_name"),
    [
        pytest.param("track", "checkout_v2", id="one-dot-the-seam"),
        pytest.param("app.core", "checkout_v2", id="dot-in-the-type"),
        pytest.param("app", "core.checkout_v2", id="dot-in-the-event"),
        pytest.param("app.core", "screen.view_v2", id="dots-on-both-sides"),
    ],
)
async def test_revert_restores_a_successor_whatever_dots_its_names_carry(
    client: AsyncClient, type_name: str, successor_name: str
) -> None:
    """Neither an event type's name nor an event's forbids a dot.

    Cutting the stored key at its first dot read ``app.core.checkout_v2`` as
    type ``app``, found nothing, and cleared the pointer while the diff went on
    showing the change. Every placement of the dots has to come back.
    """
    slug = "revert-dotted-successor"
    await _project(client, slug)
    et_id = await _event_type(client, slug, type_name)
    legacy_id = await _event(client, slug, et_id, "checkout")
    successor_id = await _event(client, slug, et_id, successor_name)
    await _point_at(client, slug, legacy_id, successor_id)

    branch_id = await _create_branch(client, slug)
    branch_legacy = await _branch_event_id(client, slug, branch_id, type_name, "checkout")
    branch_successor = await _branch_event_id(client, slug, branch_id, type_name, successor_name)
    await _point_at(client, slug, branch_legacy, None, branch_id)

    resp = await _revert(
        client,
        slug,
        branch_id,
        entity_type="event",
        name="checkout",
        parent=type_name,
        field="superseded_by",
    )
    assert resp.status_code == 200, resp.text
    # Level with the base again, so the diff has nothing left to show.
    assert resp.json()["entries"] == []
    restored = await _event_detail(client, slug, branch_id, branch_legacy)
    assert restored["superseded_by_event_id"] == branch_successor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("type_name", "clash"),
    [
        # The reported 500: namesakes under an undotted type.
        pytest.param("track", "namesake", id="namesake"),
        pytest.param("app.core", "namesake", id="namesake-under-a-dotted-type"),
        # app + core.checkout_v2 spells app.core + checkout_v2's key exactly.
        pytest.param("app.core", "second-seam", id="two-seams-one-spelling"),
    ],
)
async def test_revert_refuses_a_successor_key_two_branch_events_answer_to(
    client: AsyncClient, type_name: str, clash: str
) -> None:
    """Event names carry no uniqueness, and neither does the spelling.

    Pointing the successor at an arbitrary one of two would name an event the
    reviewer never chose, so the revert refuses with a 409, as every other
    ambiguity in the revert does. Before, it was a bare 500, a silent clear or
    a wrong successor, depending on where the dots fell.

    The second answer is added on the BRANCH in every case: a key the base
    already spelled twice is refused before the branch is asked, by the check
    the next test pins.
    """
    slug = f"revert-ambiguous-successor-{clash}"
    await _project(client, slug)
    et_id = await _event_type(client, slug, type_name)
    legacy_id = await _event(client, slug, et_id, "checkout")
    successor_id = await _event(client, slug, et_id, "checkout_v2")
    await _point_at(client, slug, legacy_id, successor_id)

    branch_id = await _create_branch(client, slug)
    if clash == "namesake":
        branch_et_id = await _branch_type_id(client, slug, branch_id, type_name)
        await _event(client, slug, branch_et_id, "checkout_v2", branch_id)
    else:
        other_et_id = await _event_type(client, slug, "app", branch_id)
        await _event(client, slug, other_et_id, "core.checkout_v2", branch_id)
    branch_legacy = await _branch_event_id(client, slug, branch_id, type_name, "checkout")
    await _point_at(client, slug, branch_legacy, None, branch_id)

    resp = await _revert(
        client,
        slug,
        branch_id,
        entity_type="event",
        name="checkout",
        parent=type_name,
        field="superseded_by",
    )
    assert resp.status_code == 409, resp.text
    assert "More than one event on this branch" in resp.json()["detail"]
    assert f"'{type_name}.checkout_v2'" in resp.json()["detail"]
    # Nothing was guessed onto the row.
    restored = await _event_detail(client, slug, branch_id, branch_legacy)
    assert restored["superseded_by_event_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("type_name", "clash"),
    [
        pytest.param("track", "namesake", id="namesake"),
        # app + core.checkout_v2 spells app.core + checkout_v2's key exactly.
        pytest.param("app.core", "second-seam", id="two-seams-one-spelling"),
    ],
)
async def test_revert_refuses_a_successor_key_two_base_events_answered_to(
    client: AsyncClient, type_name: str, clash: str
) -> None:
    """The same ambiguity on the base side, where the branch cannot see it.

    The base key named one of two events, and the branch deleted the one it
    named. Only the other still answers on the branch, so the lookup found
    exactly one and pointed the event at it with a 200 — at a namesake, or at
    an event under another type — and the diff, spelling the base key again,
    read level over the swap. Clearing instead would leave
    the change in the diff after a 200, so it is a 409.
    """
    slug = f"revert-base-ambiguous-successor-{clash}"
    await _project(client, slug)
    et_id = await _event_type(client, slug, type_name)
    legacy_id = await _event(client, slug, et_id, "checkout")
    successor_id = await _event(client, slug, et_id, "checkout_v2")
    await _point_at(client, slug, legacy_id, successor_id)
    if clash == "namesake":
        await _event(client, slug, et_id, "checkout_v2")
    else:
        other_et_id = await _event_type(client, slug, "app")
        await _event(client, slug, other_et_id, "core.checkout_v2")

    branch_id = await _create_branch(client, slug)
    branch_legacy = await _branch_event_id(client, slug, branch_id, type_name, "checkout")
    # The deep copy repoints by id, so the copy's pointer names the branch copy
    # of exactly the event the base named — that is the one to delete.
    named = (await _event_detail(client, slug, branch_id, branch_legacy))["superseded_by_event_id"]
    await _delete_event(client, slug, branch_id, named)
    # The delete clears the pointer (SET NULL), so the diff shows it as a change.
    assert (await _event_detail(client, slug, branch_id, branch_legacy))[
        "superseded_by_event_id"
    ] is None

    resp = await _revert(
        client,
        slug,
        branch_id,
        entity_type="event",
        name="checkout",
        parent=type_name,
        field="superseded_by",
    )
    assert resp.status_code == 409, resp.text
    assert "base snapshot answers to" in resp.json()["detail"]
    assert f"'{type_name}.checkout_v2'" in resp.json()["detail"]
    # The surviving namesake was not named in its place.
    restored = await _event_detail(client, slug, branch_id, branch_legacy)
    assert restored["superseded_by_event_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("successor", ["kept", "deleted"])
async def test_revert_never_names_the_event_as_its_own_successor(
    client: AsyncClient, successor: str
) -> None:
    """``app`` / ``core.x`` points at ``app.core`` / ``x``: both spell ``app.core.x``.

    Counting the reverted event among the candidates, on either side, made the
    revert a spurious 409 while its successor was still there, and made it the
    only match once the successor was gone: a self-pointer, which
    ``_resolve_successor`` refuses from any client, written with a 200 while
    the diff, spelling the base key again, read level.
    """
    slug = f"revert-self-spelled-successor-{successor}"
    await _project(client, slug)
    app_id = await _event_type(client, slug, "app")
    app_core_id = await _event_type(client, slug, "app.core")
    legacy_id = await _event(client, slug, app_id, "core.x")
    successor_id = await _event(client, slug, app_core_id, "x")
    await _point_at(client, slug, legacy_id, successor_id)

    branch_id = await _create_branch(client, slug)
    branch_legacy = await _branch_event_id(client, slug, branch_id, "app", "core.x")
    branch_successor = await _branch_event_id(client, slug, branch_id, "app.core", "x")
    if successor == "kept":
        await _point_at(client, slug, branch_legacy, None, branch_id)
    else:
        await _delete_event(client, slug, branch_id, branch_successor)

    resp = await _revert(
        client,
        slug,
        branch_id,
        entity_type="event",
        name="core.x",
        parent="app",
        field="superseded_by",
    )
    assert resp.status_code == 200, resp.text
    restored = await _event_detail(client, slug, branch_id, branch_legacy)
    if successor == "kept":
        assert resp.json()["entries"] == []
        assert restored["superseded_by_event_id"] == branch_successor
    else:
        # A successor that is gone is cleared — the lookup's documented answer.
        assert restored["superseded_by_event_id"] is None


# --- a rebuilt event gets its successor back -----------------------


async def _checkout_pointing_at_its_successor(client: AsyncClient, slug: str) -> str:
    """Main holds ``track/checkout`` superseded by ``track/checkout_v2``.

    Returns the id of the event type on main, for callers that add to it.
    """
    await _project(client, slug)
    et_id = await _event_type(client, slug, "track")
    legacy_id = await _event(client, slug, et_id, "checkout")
    successor_id = await _event(client, slug, et_id, "checkout_v2")
    await _point_at(client, slug, legacy_id, successor_id)
    return et_id


async def _delete_checkout_on_a_branch(client: AsyncClient, slug: str, branch_id: str) -> None:
    legacy = await _branch_event_id(client, slug, branch_id, "track", "checkout")
    await _delete_event(client, slug, branch_id, legacy)
    [entry] = [e for e in await _event_entries(client, slug, branch_id) if e["name"] == "checkout"]
    assert (entry["kind"], entry["before"]["superseded_by"]) == ("removed", "track.checkout_v2")


async def _checkouts_on_branch(branch_id: str) -> list[uuid.UUID]:
    async with TestSessionLocal() as session:
        return list(
            (
                await session.execute(
                    select(Event.id).where(
                        Event.branch_id == uuid.UUID(branch_id), Event.name == "checkout"
                    )
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_restoring_a_deleted_event_puts_its_successor_back(client: AsyncClient) -> None:
    """The rebuild wrote every column but ``superseded_by_event_id``.

    The event came back with a 200 and no successor, and the diff traded the
    removal for a change, ``superseded_by: 'track.checkout_v2' → None``, which
    the reviewer then had to revert a second time.
    """
    slug = "revert-rebuild-successor"
    await _checkout_pointing_at_its_successor(client, slug)
    branch_id = await _create_branch(client, slug)
    branch_successor = await _branch_event_id(client, slug, branch_id, "track", "checkout_v2")
    await _delete_checkout_on_a_branch(client, slug, branch_id)

    resp = await _revert(
        client, slug, branch_id, entity_type="event", name="checkout", parent="track"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["entries"] == []
    restored = await _branch_event_id(client, slug, branch_id, "track", "checkout")
    assert (await _event_detail(client, slug, branch_id, restored))[
        "superseded_by_event_id"
    ] == branch_successor


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["base", "branch"])
async def test_restoring_a_deleted_event_refuses_a_successor_key_two_events_answer_to(
    client: AsyncClient, side: str
) -> None:
    """The rebuild asks the field revert's two questions, and refuses the same way.

    A second ``track/checkout_v2`` either sat in the base, so the key never
    named one event, or was added on the branch, so two answer to it now.
    Rebuilding the event with no pointer was a 200 that dropped the successor
    silently; pointing it at either one would be a guess. Nothing is rebuilt.
    """
    slug = f"revert-rebuild-ambiguous-successor-{side}"
    et_id = await _checkout_pointing_at_its_successor(client, slug)
    if side == "base":
        await _event(client, slug, et_id, "checkout_v2")
    branch_id = await _create_branch(client, slug)
    if side == "branch":
        branch_et_id = await _branch_type_id(client, slug, branch_id, "track")
        await _event(client, slug, branch_et_id, "checkout_v2", branch_id)
    await _delete_checkout_on_a_branch(client, slug, branch_id)

    resp = await _revert(
        client, slug, branch_id, entity_type="event", name="checkout", parent="track"
    )
    assert resp.status_code == 409, resp.text
    where = "base snapshot answers to" if side == "base" else "on this branch answers to"
    assert where in resp.json()["detail"]
    assert "'track.checkout_v2'" in resp.json()["detail"]
    assert await _checkouts_on_branch(branch_id) == []


# --- namesakes: several base rows under one key are refused ---------------------


async def _namesakes(client: AsyncClient, slug: str) -> str:
    """Two ``track/dup`` rows on main, told apart by description alone.

    ``track`` has no scan name template, so neither carries a ``source_name``
    and nothing refuses the second name. Returns a branch cut from main.
    """
    et_id = await _seed_plan(client, slug)
    for description in ("first", "second"):
        await _event(client, slug, et_id, "dup", description=description)
    return await _create_branch(client, slug)


async def _dups_on_branch(branch_id: str) -> list[tuple[str, str]]:
    """The branch's ``dup`` rows as sorted (description, id) pairs."""
    async with TestSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Event.description, Event.id).where(
                        Event.branch_id == uuid.UUID(branch_id), Event.name == "dup"
                    )
                )
            )
            .tuples()
            .all()
        )
    return sorted((description, str(event_id)) for description, event_id in rows)


@pytest.mark.asyncio
async def test_reverting_a_change_to_one_namesake_restores_that_namesake(
    client: AsyncClient,
) -> None:
    """Two base rows, the branch deletes one and edits the other.

    Before origin ids no base row was THE before of that change, and the revert
    refused rather than copy the fields of whichever the payload listed first.
    The copy now names the base row it came from, so the diff shows a removal
    and an edit, and reverting the edit restores that row's own values.
    """
    slug = "revert-namesake-group-change"
    branch_id = await _namesakes(client, slug)
    ids = dict(await _dups_on_branch(branch_id))
    await _delete_event(client, slug, branch_id, ids["second"])
    edit = await client.patch(
        f"/api/v1/projects/{slug}/events/{ids['first']}?branch={branch_id}",
        json={"description": "edited on the branch"},
    )
    assert edit.status_code == 200, edit.text
    entries = {e["kind"]: e for e in await _event_entries(client, slug, branch_id)}
    assert set(entries) == {"changed", "removed"}
    assert entries["changed"]["entity_id"] == ids["first"]

    resp = await _revert(
        client,
        slug,
        branch_id,
        entity_type="event",
        name="dup",
        parent="track",
        entity_id=entries["changed"]["entity_id"],
    )
    assert resp.status_code == 200, resp.text
    assert [description for description, _ in await _dups_on_branch(branch_id)] == ["first"]


@pytest.mark.asyncio
async def test_reverting_the_removal_of_namesakes_rebuilds_each_one(client: AsyncClient) -> None:
    """Both ``dup`` rows deleted: each removal names its own base row.

    The revert used to refuse, because the name could not say which namesake to
    rebuild. The removed entry now carries that base row's id, so each revert
    rebuilds exactly its row, linked back to it, and the diff comes out level.
    """
    slug = "revert-namesake-removals"
    branch_id = await _namesakes(client, slug)
    for _description, event_id in await _dups_on_branch(branch_id):
        await _delete_event(client, slug, branch_id, event_id)
    removals = await _event_entries(client, slug, branch_id)
    assert len(removals) == 2 and {e["kind"] for e in removals} == {"removed"}

    for removal in removals:
        resp = await _revert(
            client,
            slug,
            branch_id,
            entity_type="event",
            name="dup",
            parent="track",
            entity_id=removal["entity_id"],
        )
        assert resp.status_code == 200, resp.text
    assert [description for description, _ in await _dups_on_branch(branch_id)] == [
        "first",
        "second",
    ]
    assert await _event_entries(client, slug, branch_id) == []


# --- an event's type is its key, not a changed field ------------


@pytest.mark.asyncio
async def test_an_events_type_is_never_a_changed_field_for_the_revert_to_restore(
    client: AsyncClient,
) -> None:
    """The proof the removed ``event_type_name`` arm rested on, pinned.

    Even when an event really does land under another type, the diff keys it by
    (event_type_name, name) and so shows a removal plus an addition, never a
    change of type. No revert request can therefore reach a type restore, and
    asking for one is the ordinary "did not change" 404. This passes with the
    arm and without it; it guards the invariant, not the deletion.
    """
    slug = "revert-type-is-the-key"
    et_id = await _seed_plan(client, slug)
    await _event(client, slug, et_id, "purchase:refund")
    await _event_type(client, slug, "screen")
    branch_id = await _create_branch(client, slug)

    # No API moves an event between types (EventUpdate has no event_type_id),
    # so the move is written straight to the row.
    async with TestSessionLocal() as session:
        moved = (
            await session.execute(
                select(Event).where(
                    Event.branch_id == uuid.UUID(branch_id), Event.name == "purchase:success"
                )
            )
        ).scalar_one()
        screen = (
            await session.execute(
                select(EventType).where(
                    EventType.branch_id == uuid.UUID(branch_id), EventType.name == "screen"
                )
            )
        ).scalar_one()
        moved.event_type_id = screen.id
        await session.commit()
    # And one ordinary edit, so there is a changed event entry to ask about.
    refund_id = await _branch_event_id(client, slug, branch_id, "track", "purchase:refund")
    edit = await client.patch(
        f"/api/v1/projects/{slug}/events/{refund_id}?branch={branch_id}",
        json={"description": "edited on the branch"},
    )
    assert edit.status_code == 200, edit.text

    diff = await _diff(client, slug, branch_id)
    events = sorted(
        (e["kind"], e["parent"], e["name"]) for e in diff["entries"] if e["entity_type"] == "event"
    )
    assert events == [
        ("added", "screen", "purchase:success"),
        ("changed", "track", "purchase:refund"),
        ("removed", "track", "purchase:success"),
    ]
    assert all(
        fc["field"] != "event_type_name" for e in diff["entries"] for fc in e["field_changes"]
    )

    resp = await _revert(
        client,
        slug,
        branch_id,
        entity_type="event",
        name="purchase:refund",
        parent="track",
        field="event_type_name",
    )
    assert resp.status_code == 404, resp.text


# --- a merged branch is not told to reopen -------------------------------------


@pytest.mark.asyncio
async def test_a_revert_on_a_merged_branch_does_not_say_to_reopen_it(client: AsyncClient) -> None:
    """Reopen takes approved, changes_requested or closed, never merged.

    The refusal used to share the closed branch's advice, "reopen it before
    reverting changes", which sent the user to a door that stays shut. The
    status is written straight to the row: the merge itself is not under test.
    """
    slug = "revert-merged-branch"
    await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)
    async with TestSessionLocal() as session:
        branch = await session.get(PlanBranch, uuid.UUID(branch_id))
        assert branch is not None
        branch.status = BranchStatus.merged
        await session.commit()

    resp = await _revert(
        client, slug, branch_id, entity_type="event", name="purchase:success", parent="track"
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "Branch is merged, so its plan is read-only"
