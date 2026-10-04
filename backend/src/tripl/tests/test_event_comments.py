"""The discussion anchored on an event rather than on one of its attachments.

Feedback item 4: "somewhere to write notes and comments — not a
Title, and not a Description, because it does not describe the event, it raises
something for discussion." The object already existed; only its anchor and its
surface were wrong.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.event import Event
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.services.plan_revision_service import build_plan_snapshot
from tripl.tests.conftest import TestSessionLocal


async def _setup_event(client: AsyncClient, slug: str) -> str:
    project = await client.post(
        "/api/v1/projects", json={"name": slug, "slug": slug, "description": ""}
    )
    assert project.status_code == 201
    et = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "track", "display_name": "Track"},
    )
    assert et.status_code == 201
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et.json()["id"], "name": "purchase:success"},
    )
    assert event.status_code == 201
    return str(event.json()["id"])


@pytest.mark.asyncio
async def test_an_event_carries_a_thread_without_any_attachment(client: AsyncClient) -> None:
    """The old anchor was photo_id and nothing else, so there was no comment
    surface at all until somebody uploaded an image."""
    slug = "ev-discussion"
    event_id = await _setup_event(client, slug)
    base = f"/api/v1/projects/{slug}/events/{event_id}/comments"

    assert (await client.get(base)).json() == []

    top = await client.post(base, json={"body": "should this fire on cancel too?"})
    assert top.status_code == 201
    assert top.json()["photo_id"] is None
    assert top.json()["event_id"] == event_id

    reply = await client.post(
        base, json={"body": "no — cancel has its own event", "parent_id": top.json()["id"]}
    )
    assert reply.status_code == 201
    assert reply.json()["parent_id"] == top.json()["id"]

    listed = await client.get(base)
    assert [row["body"] for row in listed.json()] == [
        "should this fire on cancel too?",
        "no — cancel has its own event",
    ]

    gone = await client.delete(f"{base}/{reply.json()['id']}")
    assert gone.status_code == 204
    assert [row["body"] for row in (await client.get(base)).json()] == [
        "should this fire on cancel too?"
    ]


@pytest.mark.asyncio
async def test_a_reply_must_belong_to_this_events_thread(client: AsyncClient) -> None:
    slug = "ev-discussion-parent"
    first_event_id = await _setup_event(client, slug)
    event_types = await client.get(f"/api/v1/projects/{slug}/event-types")
    other = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": event_types.json()[0]["id"], "name": "purchase:failed"},
    )
    assert other.status_code == 201
    other_event_id = other.json()["id"]

    posted = await client.post(
        f"/api/v1/projects/{slug}/events/{first_event_id}/comments",
        json={"body": "here"},
    )
    assert posted.status_code == 201

    crossed = await client.post(
        f"/api/v1/projects/{slug}/events/{other_event_id}/comments",
        json={"body": "over there", "parent_id": posted.json()["id"]},
    )
    assert crossed.status_code == 400


@pytest.mark.asyncio
async def test_the_discussion_is_not_plan_content(client: AsyncClient) -> None:
    """The reason for a separate box in the first place: Description ships to
    whoever implements the event. A question must not.

    Concretely, the thread stays out of ``build_plan_snapshot`` — which is what
    keeps it out of the plan hash, the branch diff and the approval gate, since
    all three read that payload.
    """
    slug = "ev-discussion-snapshot"
    event_id = await _setup_event(client, slug)

    async with TestSessionLocal() as session:
        event = await session.get(Event, uuid.UUID(event_id))
        assert event is not None
        before = await build_plan_snapshot(session, event.project_id, branch_id=event.branch_id)

    posted = await client.post(
        f"/api/v1/projects/{slug}/events/{event_id}/comments",
        json={"body": "raising this for discussion"},
    )
    assert posted.status_code == 201

    async with TestSessionLocal() as session:
        event = await session.get(Event, uuid.UUID(event_id))
        assert event is not None
        after = await build_plan_snapshot(session, event.project_id, branch_id=event.branch_id)
    assert after == before


@pytest.mark.asyncio
async def test_a_branch_copy_reads_and_writes_the_events_one_discussion(
    client: AsyncClient,
) -> None:
    """One event, one conversation. A branch copy resolves to its twin on main
    rather than forking the thread — the fork is what let a comment block a
    merge."""
    slug = "ev-discussion-branch"
    main_event_id = await _setup_event(client, slug)
    on_main = await client.post(
        f"/api/v1/projects/{slug}/events/{main_event_id}/comments",
        json={"body": "asked on main"},
    )
    assert on_main.status_code == 201

    branch = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    assert branch.status_code == 201
    branch_id = branch.json()["id"]

    async with TestSessionLocal() as session:
        branch_event = (
            (await session.execute(select(Event).where(Event.branch_id == uuid.UUID(branch_id))))
            .scalars()
            .first()
        )
        assert branch_event is not None
        branch_event_id = branch_event.id
    assert str(branch_event_id) != main_event_id

    branch_base = f"/api/v1/projects/{slug}/events/{branch_event_id}/comments"
    # The branch reads main's thread…
    assert [row["body"] for row in (await client.get(branch_base)).json()] == ["asked on main"]
    # …and answering from the branch lands on the same thread, not a copy.
    answered = await client.post(branch_base, json={"body": "answered from the branch"})
    assert answered.status_code == 201
    assert answered.json()["event_id"] == main_event_id

    main_base = f"/api/v1/projects/{slug}/events/{main_event_id}/comments"
    assert [row["body"] for row in (await client.get(main_base)).json()] == [
        "asked on main",
        "answered from the branch",
    ]

    async with TestSessionLocal() as session:
        anchored_on_the_copy = (
            (
                await session.execute(
                    select(EventPhotoComment).where(EventPhotoComment.event_id == branch_event_id)
                )
            )
            .scalars()
            .all()
        )
    assert list(anchored_on_the_copy) == []


async def _open_thread(client: AsyncClient, slug: str, event_id: str, body: str) -> str:
    resp = await client.post(
        f"/api/v1/projects/{slug}/events/{event_id}/comments", json={"body": body}
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "open"
    return str(resp.json()["id"])


@pytest.mark.asyncio
async def test_a_thread_resolves_reopens_and_keeps_its_note(client: AsyncClient) -> None:
    """A question typed today never closed: neither comment table had a status
    column, so the discussion had no way to end."""
    slug = "ev-resolve"
    event_id = await _setup_event(client, slug)
    comment_id = await _open_thread(client, slug, event_id, "does this fire on cancel?")
    actions = f"/api/v1/projects/{slug}/events/{event_id}/comments/{comment_id}/actions"

    resolved = await client.post(actions, json={"action": "resolve", "note": "cancel has its own"})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["resolution_note"] == "cancel has its own"
    assert resolved.json()["resolved_at"] is not None
    assert resolved.json()["resolved_by"] is not None

    # Omitting the note must not erase one an earlier action stored — the defect
    # schema_drift_service had to learn.
    again = await client.post(actions, json={"action": "resolve"})
    assert again.json()["resolution_note"] == "cancel has its own"

    reopened = await client.post(actions, json={"action": "reopen"})
    assert reopened.json()["status"] == "open"
    # A reopened thread has no resolution any more.
    assert reopened.json()["resolution_note"] is None
    assert reopened.json()["resolved_at"] is None
    assert reopened.json()["resolved_by"] is None


@pytest.mark.asyncio
async def test_only_a_top_level_comment_carries_resolution(client: AsyncClient) -> None:
    """The thread is the unit that gets answered. Letting a reply carry its own
    state would make "is this answered" a question with several contradictory
    answers."""
    slug = "ev-resolve-reply"
    event_id = await _setup_event(client, slug)
    base = f"/api/v1/projects/{slug}/events/{event_id}/comments"
    top_id = await _open_thread(client, slug, event_id, "why two events here?")
    reply = await client.post(base, json={"body": "history", "parent_id": top_id})

    resp = await client.post(f"{base}/{reply.json()['id']}/actions", json={"action": "resolve"})
    assert resp.status_code == 400
    assert "top-level" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_a_snooze_needs_a_date_and_a_resolve_refuses_one(client: AsyncClient) -> None:
    slug = "ev-snooze-validation"
    event_id = await _setup_event(client, slug)
    comment_id = await _open_thread(client, slug, event_id, "still true?")
    actions = f"/api/v1/projects/{slug}/events/{event_id}/comments/{comment_id}/actions"

    assert (await client.post(actions, json={"action": "snooze"})).status_code == 422
    # A snooze date on a resolve is a client that meant something else;
    # accepting and discarding it would hide the mistake.
    late = await client.post(
        actions, json={"action": "resolve", "snoozed_until": "2030-01-01T00:00:00Z"}
    )
    assert late.status_code == 422


@pytest.mark.asyncio
async def test_the_catalog_filters_on_unanswered_questions(client: AsyncClient) -> None:
    slug = "ev-open-questions"
    event_id = await _setup_event(client, slug)
    et_id = (await client.get(f"/api/v1/projects/{slug}/event-types")).json()[0]["id"]
    quiet = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et_id, "name": "purchase:cancel"},
    )
    assert quiet.status_code == 201

    async def names(**params: str) -> list[str]:
        resp = await client.get(f"/api/v1/projects/{slug}/events", params=params)
        assert resp.status_code == 200
        return sorted(item["name"] for item in resp.json()["items"])

    # Nothing asked yet.
    assert await names(has_open_questions="true") == []
    assert await names(has_open_questions="false") == ["purchase:cancel", "purchase:success"]

    comment_id = await _open_thread(client, slug, event_id, "which currency?")
    assert await names(has_open_questions="true") == ["purchase:success"]
    assert await names(has_open_questions="false") == ["purchase:cancel"]

    listed = await client.get(f"/api/v1/projects/{slug}/events")
    counts = {item["name"]: item["open_question_count"] for item in listed.json()["items"]}
    # The filter needs something visible to agree with; a filter whose result the
    # list cannot explain reads as a bug.
    assert counts == {"purchase:success": 1, "purchase:cancel": 0}

    actions = f"/api/v1/projects/{slug}/events/{event_id}/comments/{comment_id}/actions"
    assert (await client.post(actions, json={"action": "resolve"})).status_code == 200
    assert await names(has_open_questions="true") == []


@pytest.mark.asyncio
async def test_a_lapsed_snooze_counts_as_unanswered_again(client: AsyncClient) -> None:
    """Decided at read time, not written back by a sweeper: a stored flag would
    be wrong for exactly as long as it took the next job to run, and the point of
    a snooze is that nobody is watching the thread in the meantime."""
    slug = "ev-snooze-lapse"
    event_id = await _setup_event(client, slug)
    comment_id = await _open_thread(client, slug, event_id, "ask again later")
    actions = f"/api/v1/projects/{slug}/events/{event_id}/comments/{comment_id}/actions"

    ahead = await client.post(
        actions, json={"action": "snooze", "snoozed_until": "2999-01-01T00:00:00Z"}
    )
    assert ahead.json()["status"] == "snoozed"
    resp = await client.get(
        f"/api/v1/projects/{slug}/events", params={"has_open_questions": "true"}
    )
    assert resp.json()["items"] == []

    behind = await client.post(
        actions, json={"action": "snooze", "snoozed_until": "2000-01-01T00:00:00Z"}
    )
    assert behind.json()["status"] == "snoozed"
    resp = await client.get(
        f"/api/v1/projects/{slug}/events", params={"has_open_questions": "true"}
    )
    assert [item["name"] for item in resp.json()["items"]] == ["purchase:success"]


@pytest.mark.asyncio
async def test_a_branch_listing_sees_the_question_asked_on_main(client: AsyncClient) -> None:
    """An event has ONE discussion, hanging on its main twin. Matching comment
    rows against branch ids would answer "no open questions" on every branch —
    a wrong answer dressed as a real one."""
    slug = "ev-questions-branch"
    event_id = await _setup_event(client, slug)
    await _open_thread(client, slug, event_id, "which currency?")

    branch = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    assert branch.status_code == 201
    branch_id = branch.json()["id"]

    resp = await client.get(
        f"/api/v1/projects/{slug}/events",
        params={"branch": branch_id, "has_open_questions": "true"},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [item["name"] for item in items] == ["purchase:success"]
    # The BRANCH's own row, not main's.
    assert items[0]["id"] != event_id
    assert items[0]["open_question_count"] == 1
