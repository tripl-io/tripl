"""An event's discussion when the event only gets a row on main later.

A branch copy of a main event reads and writes the discussion on its main twin,
so nothing hangs on the branch row. An event CREATED on a branch has no twin, so
its thread — the note typed in the New-event box included (PR #162) — hangs on
the branch row itself. The merge used to give the event a fresh main row and
leave the thread behind: main's row opened with an empty discussion, the branch
row then read through to that empty twin, and deleting the merged branch took
the rows with it through the cascade. A twin appearing without any merge — a
scan, or the same event authored on main — hid the thread just the same.
"""

import uuid
from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from tripl.models.event import Event
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.models.event_type import EventType
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_plan_branches import _approve_and_merge, _create_branch, _seed_plan

# A moment nothing in these tests runs at. SQLite's CURRENT_TIMESTAMP has
# one-second resolution, so comparing against a timestamp the test itself
# produced could not tell "kept" from "re-stamped in the same second".
_LONG_AGO = datetime(2020, 1, 2, 3, 4, 5)


async def _create_on_branch(client: AsyncClient, slug: str, branch_id: str, name: str) -> str:
    # A branch deep-copies its event types under new ids, so the event is
    # authored against the branch's own "track".
    branch_ets = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    branch_et_id = next(et["id"] for et in branch_ets.json() if et["name"] == "track")
    created = await client.post(
        f"/api/v1/projects/{slug}/events?branch={branch_id}",
        json={"event_type_id": branch_et_id, "name": name},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _event_id(client: AsyncClient, slug: str, name: str, *, branch: str = "") -> str:
    params = {"branch": branch} if branch else {}
    listed = await client.get(f"/api/v1/projects/{slug}/events", params=params)
    assert listed.status_code == 200
    return str(next(item["id"] for item in listed.json()["items"] if item["name"] == name))


async def _post(
    client: AsyncClient, slug: str, event_id: str, body: str, *, parent_id: str | None = None
) -> dict:
    payload: dict[str, str] = {"body": body}
    if parent_id is not None:
        payload["parent_id"] = parent_id
    resp = await client.post(f"/api/v1/projects/{slug}/events/{event_id}/comments", json=payload)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def _thread(client: AsyncClient, slug: str, event_id: str) -> dict[str, dict]:
    """The discussion as the API shows it, keyed by comment id.

    Keyed rather than ordered: SQLite's CURRENT_TIMESTAMP has one-second
    resolution, so comments posted in one test tie on ``created_at``.
    """
    resp = await client.get(f"/api/v1/projects/{slug}/events/{event_id}/comments")
    assert resp.status_code == 200, resp.text
    return {row["id"]: row for row in resp.json()}


async def _question_counts(client: AsyncClient, slug: str, **params: str) -> dict[str, int]:
    resp = await client.get(f"/api/v1/projects/{slug}/events", params=params)
    assert resp.status_code == 200, resp.text
    return {item["name"]: item["open_question_count"] for item in resp.json()["items"]}


async def _backdate(comment_id: str) -> None:
    async with TestSessionLocal() as session:
        await session.execute(
            update(EventPhotoComment)
            .where(EventPhotoComment.id == uuid.UUID(comment_id))
            .values(created_at=_LONG_AGO, updated_at=_LONG_AGO)
        )
        await session.commit()


async def _stored_comment(comment_id: str) -> EventPhotoComment:
    async with TestSessionLocal() as session:
        comment = await session.get(EventPhotoComment, uuid.UUID(comment_id))
        assert comment is not None
        return comment


async def _insert_main_event(event_type_id: str, *, name: str, source_name: str) -> str:
    """A main row carrying a display name beside its scan identity — what
    ``reconciliation_service.accept_shadow_event`` writes when an operator
    labels the candidate while accepting it, and how scans and legacy data left
    several events under one type and name."""
    async with TestSessionLocal() as session:
        event_type = await session.get(EventType, uuid.UUID(event_type_id))
        assert event_type is not None
        event = Event(
            project_id=event_type.project_id,
            branch_id=event_type.branch_id,
            event_type_id=event_type.id,
            name=name,
            source_name=source_name,
        )
        session.add(event)
        await session.commit()
        return str(event.id)


async def _rows_on(branch_id: str, **columns: str) -> list[str]:
    """Ids of the branch's events whose columns equal ``columns``."""
    async with TestSessionLocal() as session:
        ids = await session.execute(
            select(Event.id).where(
                Event.branch_id == uuid.UUID(branch_id),
                *(getattr(Event, column) == value for column, value in columns.items()),
            )
        )
        return [str(row_id) for row_id in ids.scalars()]


async def _comments_anchored_on_branch_rows(branch_id: str) -> list[EventPhotoComment]:
    async with TestSessionLocal() as session:
        return list(
            (
                await session.execute(
                    select(EventPhotoComment)
                    .join(Event, Event.id == EventPhotoComment.event_id)
                    .where(Event.branch_id == uuid.UUID(branch_id))
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_a_branch_only_events_discussion_moves_to_the_row_the_merge_creates(
    client: AsyncClient,
) -> None:
    """Two events created on the branch, so each thread has to reach its OWN
    new main row — and arrive whole: ids, the reply's parent, a resolved
    thread's state and note, and when each comment was written."""
    slug = "evc-merge-new"
    await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)
    tap_branch_id = await _create_on_branch(client, slug, branch_id, "checkout:new_tap")
    swipe_branch_id = await _create_on_branch(client, slug, branch_id, "checkout:new_swipe")

    question = await _post(client, slug, tap_branch_id, "should this fire on a double tap?")
    # No twin on main yet, so the branch row is the thread's home.
    assert question["event_id"] == tap_branch_id
    reply = await _post(client, slug, tap_branch_id, "once per gesture", parent_id=question["id"])
    answered = await _post(client, slug, tap_branch_id, "which screen?")
    resolved = await client.post(
        f"/api/v1/projects/{slug}/events/{tap_branch_id}/comments/{answered['id']}/actions",
        json={"action": "resolve", "note": "the cart"},
    )
    assert resolved.status_code == 200, resolved.text
    swipe_question = await _post(client, slug, swipe_branch_id, "left or right?")
    await _backdate(question["id"])

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text

    tap_main_id = await _event_id(client, slug, "checkout:new_tap")
    swipe_main_id = await _event_id(client, slug, "checkout:new_swipe")
    assert tap_main_id != tap_branch_id

    tap_thread = await _thread(client, slug, tap_main_id)
    assert set(tap_thread) == {question["id"], reply["id"], answered["id"]}
    assert {row["event_id"] for row in tap_thread.values()} == {tap_main_id}
    assert tap_thread[reply["id"]]["parent_id"] == question["id"]
    assert tap_thread[answered["id"]]["status"] == "resolved"
    assert tap_thread[answered["id"]]["resolution_note"] == "the cart"
    # Moved, not re-written: main orders the thread by when it was said, and
    # the move is no edit — the column's onupdate must not stamp the merge.
    moved = await _stored_comment(question["id"])
    assert moved.created_at.replace(tzinfo=None) == _LONG_AGO
    assert moved.updated_at.replace(tzinfo=None) == _LONG_AGO

    swipe_thread = await _thread(client, slug, swipe_main_id)
    assert set(swipe_thread) == {swipe_question["id"]}
    assert swipe_thread[swipe_question["id"]]["event_id"] == swipe_main_id

    # The branch row reads through to its new twin and finds the same thread.
    assert set(await _thread(client, slug, tap_branch_id)) == set(tap_thread)

    # Main's catalog sees the open questions: one per event, since a reply and
    # a resolved thread are not open questions.
    open_on_main = await _question_counts(client, slug, has_open_questions="true")
    assert open_on_main == {"checkout:new_swipe": 1, "checkout:new_tap": 1}

    # Nothing is left on the branch for its deletion to take.
    assert await _comments_anchored_on_branch_rows(branch_id) == []
    deleted = await client.delete(f"/api/v1/projects/{slug}/branches/{branch_id}")
    assert deleted.status_code == 204
    assert set(await _thread(client, slug, tap_main_id)) == set(tap_thread)
    assert set(await _thread(client, slug, swipe_main_id)) == {swipe_question["id"]}


async def _thread_started_before_main_had_the_event(
    client: AsyncClient, slug: str
) -> tuple[str, str, str, dict, dict]:
    """A branch-only event with a thread, then main grows the same event.

    Returns ``(branch_id, branch_row_id, main_row_id, question, reply)``.
    """
    main_et_id = await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)
    branch_row_id = await _create_on_branch(client, slug, branch_id, "checkout:new_tap")
    question = await _post(client, slug, branch_row_id, "asked on the branch before main had it")
    reply = await _post(
        client, slug, branch_row_id, "and on every screen?", parent_id=question["id"]
    )
    assert question["event_id"] == reply["event_id"] == branch_row_id
    # Main grows the same event: what a scan does the moment the warehouse
    # reports it, or an analyst authoring it on main. Identical to the branch's,
    # so the merge below matches the two rather than reporting a conflict.
    twin = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": main_et_id, "name": "checkout:new_tap"},
    )
    assert twin.status_code == 201, twin.text
    return branch_id, branch_row_id, str(twin.json()["id"]), question, reply


@pytest.mark.asyncio
async def test_a_twin_appearing_on_main_does_not_hide_the_branch_rows_thread(
    client: AsyncClient,
) -> None:
    """No merge at all: the moment main has a row for the identity, the branch
    row resolved to it and read only ITS thread — the question already asked on
    the branch vanished from the page, its count read ?0, and it could no longer
    be answered, resolved or deleted from there."""
    slug = "evc-twin-appears"
    (
        branch_id,
        branch_row_id,
        main_row_id,
        question,
        reply,
    ) = await _thread_started_before_main_had_the_event(client, slug)
    on_main = await _post(client, slug, main_row_id, "asked on main")

    assert set(await _thread(client, slug, branch_row_id)) == {
        question["id"],
        reply["id"],
        on_main["id"],
    }

    # A NEW thread started from the branch still goes to the twin, as for any
    # branch copy: the twin is where the discussion lives from now on.
    later = await _post(client, slug, branch_row_id, "asked from the branch after main had it")
    assert later["event_id"] == main_row_id

    # The older thread stays answerable from the branch, and the answer joins
    # ITS thread rather than straddling two anchors.
    answer = await _post(client, slug, branch_row_id, "yes, every screen", parent_id=question["id"])
    assert answer["event_id"] == branch_row_id
    base = f"/api/v1/projects/{slug}/events/{branch_row_id}/comments"
    assert (await client.delete(f"{base}/{reply['id']}")).status_code == 204

    counts = await _question_counts(client, slug, branch=branch_id)
    # question (branch row) + on_main + later (twin); replies never count.
    assert counts["checkout:new_tap"] == 3
    resolved = await client.post(f"{base}/{question['id']}/actions", json={"action": "resolve"})
    assert resolved.status_code == 200, resolved.text
    counts = await _question_counts(client, slug, branch=branch_id)
    assert counts["checkout:new_tap"] == 2
    # The filter agrees with the count it sits beside.
    open_on_branch = await _question_counts(
        client, slug, branch=branch_id, has_open_questions="true"
    )
    assert open_on_branch == {"checkout:new_tap": 2}

    # Main's own row reads main's thread only: a question drafted on a branch
    # main has not merged reaches main with the merge, like the rest of it.
    assert set(await _thread(client, slug, main_row_id)) == {on_main["id"], later["id"]}


@pytest.mark.asyncio
async def test_at_merge_a_branch_rows_thread_joins_the_twins_thread(
    client: AsyncClient,
) -> None:
    """The merge MATCHES the branch row to main's existing one (its id is
    kept), and the thread the branch row held joins main's beside it — neither
    lost with the branch nor duplicated."""
    slug = "evc-twin-merge"
    (
        branch_id,
        branch_row_id,
        main_row_id,
        question,
        reply,
    ) = await _thread_started_before_main_had_the_event(client, slug)
    on_main = await _post(client, slug, main_row_id, "asked on main")

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text
    assert await _event_id(client, slug, "checkout:new_tap") == main_row_id

    main_thread = await _thread(client, slug, main_row_id)
    assert set(main_thread) == {question["id"], reply["id"], on_main["id"]}
    assert {row["event_id"] for row in main_thread.values()} == {main_row_id}
    assert main_thread[reply["id"]]["parent_id"] == question["id"]
    assert await _comments_anchored_on_branch_rows(branch_id) == []

    counts = await _question_counts(client, slug)
    assert counts["checkout:new_tap"] == 2
    # The branch row reads the same one thread through its twin.
    assert set(await _thread(client, slug, branch_row_id)) == set(main_thread)

    deleted = await client.delete(f"/api/v1/projects/{slug}/branches/{branch_id}")
    assert deleted.status_code == 204
    assert set(await _thread(client, slug, main_row_id)) == set(main_thread)


@pytest.mark.asyncio
async def test_an_event_that_lands_nowhere_on_main_keeps_or_drops_its_thread(
    client: AsyncClient,
) -> None:
    """Pins the two cases the move deliberately leaves alone, with a main row
    beside them that a move guessing its target would pick.

    * Main DELETED the event after the cut. The branch row lands nowhere and its
      thread stays with the branch — main's change stands, the rule the photo
      threads follow. (This used to be a main-side RENAME, which no scan
      identity tied to the copy; since origin ids the copy reads through to
      the row main renamed, which is the row it was copied from, so a rename
      no longer lands nowhere — test_batch18_origin_id pins that.)
    * The branch deleted the event: the merge deletes main's row, and main's
      thread goes with it through the same cascade as a delete on main.
    """
    slug = "evc-lands-nowhere"
    main_et_id = await _seed_plan(client, slug)
    # "purchase:success": main deletes it after the cut, the branch keeps it.
    success_main_id = await _event_id(client, slug, "purchase:success")
    # "purchase:failed": the branch deletes it, so the merge deletes main's row.
    failed = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": main_et_id, "name": "purchase:failed"},
    )
    assert failed.status_code == 201, failed.text
    on_failed = await _post(client, slug, str(failed.json()["id"]), "why do we still send this?")

    branch_id = await _create_branch(client, slug)
    success_branch_id = await _event_id(client, slug, "purchase:success", branch=branch_id)
    failed_branch_id = await _event_id(client, slug, "purchase:failed", branch=branch_id)

    deleted_on_main = await client.delete(f"/api/v1/projects/{slug}/events/{success_main_id}")
    assert deleted_on_main.status_code == 204, deleted_on_main.text
    deleted_on_branch = await client.delete(
        f"/api/v1/projects/{slug}/events/{failed_branch_id}?branch={branch_id}"
    )
    assert deleted_on_branch.status_code == 204
    # Nothing on main answers to the branch row's identity any more, so it has
    # no twin and anchors its own thread.
    orphan = await _post(client, slug, success_branch_id, "still needed?")
    assert orphan["event_id"] == success_branch_id

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text
    # Main's deletion and the branch's deletion both stand.
    assert await _question_counts(client, slug) == {}

    assert set(await _thread(client, slug, success_branch_id)) == {orphan["id"]}
    async with TestSessionLocal() as session:
        assert await session.get(EventPhotoComment, uuid.UUID(on_failed["id"])) is None
        stayed = await session.get(EventPhotoComment, uuid.UUID(orphan["id"]))
        assert stayed is not None
        assert stayed.event_id == uuid.UUID(success_branch_id)


@pytest.mark.asyncio
async def test_at_merge_a_thread_follows_the_twin_it_reads_through(
    client: AsyncClient,
) -> None:
    """Main holds the identity under another display name — a shadow event an
    operator labelled while accepting it — so the branch row pairs with that
    twin by scan identity while the merge, pairing by NAME, creates a second
    main row. Every thread started after the twin appeared already hangs on the
    twin; the one started before it has to join them there, not the new row."""
    slug = "evc-twin-display-name"
    main_et_id = await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)
    branch_row_id = await _create_on_branch(client, slug, branch_id, "checkout:new_tap")
    question = await _post(client, slug, branch_row_id, "asked before main had it")
    assert question["event_id"] == branch_row_id
    twin_id = await _insert_main_event(
        main_et_id, name="Checkout new tap", source_name="checkout:new_tap"
    )
    later = await _post(client, slug, branch_row_id, "asked after main had it")
    assert later["event_id"] == twin_id

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text
    # The premise: the merge did create a second main row under the name.
    by_name = await _event_id(client, slug, "checkout:new_tap")
    assert by_name != twin_id

    assert (await _stored_comment(question["id"])).event_id == uuid.UUID(twin_id)
    assert (await _stored_comment(later["id"])).event_id == uuid.UUID(twin_id)
    assert await _thread(client, slug, by_name) == {}
    assert await _comments_anchored_on_branch_rows(branch_id) == []


@pytest.mark.asyncio
async def test_another_branchs_thread_on_the_same_name_does_not_match_this_branch(
    client: AsyncClient,
) -> None:
    """Two branches each draft "checkout:new_tap", and one asks about it. The
    filter matched every row sharing the key with ANY open anchor, so the other
    branch listed its own copy under "has open questions" beside a count of
    ?0 — and hid it under "no open questions"."""
    slug = "evc-two-branches"
    await _seed_plan(client, slug)
    drafting = await _create_branch(client, slug, "drafting")
    other = await _create_branch(client, slug, "other")
    drafting_row = await _create_on_branch(client, slug, drafting, "checkout:new_tap")
    await _create_on_branch(client, slug, other, "checkout:new_tap")
    question = await _post(client, slug, drafting_row, "which screen?")
    assert question["event_id"] == drafting_row

    assert await _question_counts(client, slug, branch=drafting, has_open_questions="true") == {
        "checkout:new_tap": 1
    }
    # The other branch's copy has no thread of its own and no twin on main.
    assert await _question_counts(client, slug, branch=other, has_open_questions="true") == {}
    unanswered_elsewhere = await _question_counts(
        client, slug, branch=other, has_open_questions="false"
    )
    assert unanswered_elsewhere["checkout:new_tap"] == 0


@pytest.mark.asyncio
async def test_a_thread_about_a_namesake_main_deleted_stays_with_the_branch(
    client: AsyncClient,
) -> None:
    """Two ``checkout:tap`` share a type and name, and main deletes one after
    the cut. A question then asked about the branch's copy of it has no twin, so
    it hangs on the branch row, and the merge placed it by type and name — on
    the namesake main kept, an event it was never about. Nothing records which
    main row a branch copy came from, so the thread stays with the branch, as
    one whose event lands nowhere does."""
    slug = "evc-namesake-deleted-on-main"
    main_et_id = await _seed_plan(client, slug)
    kept_id = await _insert_main_event(main_et_id, name="checkout:tap", source_name="tap:kept")
    dropped_id = await _insert_main_event(
        main_et_id, name="checkout:tap", source_name="tap:dropped"
    )
    branch_id = await _create_branch(client, slug)
    [dropped_copy] = await _rows_on(branch_id, source_name="tap:dropped")
    deleted = await client.delete(f"/api/v1/projects/{slug}/events/{dropped_id}")
    assert deleted.status_code == 204
    question = await _post(client, slug, dropped_copy, "is this one still sent?")
    assert question["event_id"] == dropped_copy

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text
    assert (await _stored_comment(question["id"])).event_id == uuid.UUID(dropped_copy)
    assert await _thread(client, slug, kept_id) == {}


@pytest.mark.asyncio
async def test_a_thread_on_the_row_that_replaces_two_namesakes_follows_it_to_main(
    client: AsyncClient,
) -> None:
    """The branch folds main's two ``checkout:tap`` into one it authors itself,
    and asks about it: no scan identity ties the new row to either of main's,
    so the thread hangs on the branch row.

    Main used to keep both namesakes after the merge — it deleted a main row
    only when its NAME vanished from the branch — so the thread had to stay
    with the branch rather than land on an arbitrary one of them. The copies
    now name the main rows they came from, so the merge deletes exactly those
    two and creates the authored row, and the thread moves onto it
    ."""
    slug = "evc-namesakes-on-main"
    main_et_id = await _seed_plan(client, slug)
    first_id = await _insert_main_event(main_et_id, name="checkout:tap", source_name="tap:one")
    second_id = await _insert_main_event(main_et_id, name="checkout:tap", source_name="tap:two")
    branch_id = await _create_branch(client, slug)
    for copy_id in await _rows_on(branch_id, name="checkout:tap"):
        deleted = await client.delete(
            f"/api/v1/projects/{slug}/events/{copy_id}?branch={branch_id}"
        )
        assert deleted.status_code == 204
    authored = await _create_on_branch(client, slug, branch_id, "checkout:tap")
    question = await _post(client, slug, authored, "one event for both taps?")
    assert question["event_id"] == authored

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text
    async with TestSessionLocal() as session:
        taps = (
            (
                await session.execute(
                    select(Event.id).where(
                        Event.event_type_id == uuid.UUID(main_et_id),
                        Event.name == "checkout:tap",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(taps) == 1
    assert taps[0] not in {uuid.UUID(first_id), uuid.UUID(second_id)}
    assert (await _stored_comment(question["id"])).event_id == taps[0]
