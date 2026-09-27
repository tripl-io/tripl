"""Watch / subscribe, @mentions and the notification center (#259)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from tripl.main import app
from tripl.models.notification import Notification
from tripl.models.project import Project
from tripl.models.subscription import Subscription
from tripl.services import notification_service
from tripl.services.mentions import excerpt, mentioned_user_ids
from tripl.tests._members import add_member_by_slug, remove_member_by_slug
from tripl.tests.conftest import TestSessionLocal

PASSWORD = "Password123!"


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _register(client: AsyncClient, email: str, name: str) -> str:
    resp = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD, "name": name}
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


class People:
    """The instance owner (Ann, the event author), an editor (Bob), an outsider (Cid)."""

    def __init__(self) -> None:
        self.ann = _new_client()
        self.bob = _new_client()
        self.cid = _new_client()
        self.ids: dict[str, str] = {}

    async def aclose(self) -> None:
        for client in (self.ann, self.bob, self.cid):
            await client.aclose()


@pytest_asyncio.fixture
async def people() -> AsyncGenerator[People]:
    crowd = People()
    crowd.ids["ann"] = await _register(crowd.ann, "ann@example.com", "Ann")
    crowd.ids["bob"] = await _register(crowd.bob, "bob@example.com", "Bob")
    crowd.ids["cid"] = await _register(crowd.cid, "cid@example.com", "Cid")
    yield crowd
    await crowd.aclose()


async def _project_with_event(people: People, slug: str) -> str:
    """Ann's project with Bob as an editor member and one event Ann authored."""
    project = await people.ann.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert project.status_code == 201, project.text
    await add_member_by_slug(slug, "bob@example.com", "editor")
    et = await people.ann.post(
        f"/api/v1/projects/{slug}/event-types", json={"name": "track", "display_name": "Track"}
    )
    assert et.status_code == 201, et.text
    event = await people.ann.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et.json()["id"], "name": "purchase:success"},
    )
    assert event.status_code == 201, event.text
    return str(event.json()["id"])


def _comments(slug: str, event_id: str) -> str:
    return f"/api/v1/projects/{slug}/events/{event_id}/comments"


def _subscription(slug: str, event_id: str) -> str:
    return f"/api/v1/projects/{slug}/subscriptions/event/{event_id}"


async def _inbox(client: AsyncClient, **params: Any) -> list[dict[str, Any]]:
    resp = await client.get("/api/v1/me/notifications", params=params)
    assert resp.status_code == 200, resp.text
    return list(resp.json()["items"])


async def _unread(client: AsyncClient) -> int:
    resp = await client.get("/api/v1/me/notifications/unread-count")
    assert resp.status_code == 200, resp.text
    return int(resp.json()["unread"])


# ── mentions (pure) ─────────────────────────────────────────────────────────


def test_only_the_composer_token_is_a_mention() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    body = f"hey @[Bob]({first}) and @bob and @[Cid]({second}) again @[Bob]({first})"
    assert mentioned_user_ids(body) == [first, second]
    assert excerpt(body).startswith("hey @Bob and @bob and @Cid")


# ── end to end ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_watching_an_event_delivers_a_comment_notification(people: People) -> None:
    slug = "notif-watch"
    event_id = await _project_with_event(people, slug)

    state = await people.bob.get(_subscription(slug, event_id))
    assert state.status_code == 200, state.text
    assert state.json()["watching"] is False

    watched = await people.bob.put(_subscription(slug, event_id))
    assert watched.status_code == 200, watched.text
    assert watched.json() == {
        "entity_type": "event",
        "entity_id": event_id,
        "watching": True,
        "muted": False,
        "reasons": ["manual"],
    }

    posted = await people.ann.post(_comments(slug, event_id), json={"body": "is this live?"})
    assert posted.status_code == 201, posted.text

    items = await _inbox(people.bob)
    assert [(n["kind"], n["entity_id"], n["project_slug"]) for n in items] == [
        ("comment", event_id, slug)
    ]
    assert items[0]["url"] == f"/p/{slug}/events/detail/{event_id}"
    assert items[0]["body"] == "is this live?"
    assert items[0]["actor"]["name"] == "Ann"
    assert await _unread(people.bob) == 1

    # The actor never hears about their own comment.
    assert await _inbox(people.ann) == []


@pytest.mark.asyncio
async def test_a_mention_reaches_a_member_who_is_not_subscribed(people: People) -> None:
    slug = "notif-mention"
    event_id = await _project_with_event(people, slug)

    body = f"@[Bob]({people.ids['bob']}) can you check this?"
    posted = await people.ann.post(_comments(slug, event_id), json={"body": body})
    assert posted.status_code == 201, posted.text

    items = await _inbox(people.bob)
    assert [n["kind"] for n in items] == ["mention"]
    assert items[0]["body"] == "@Bob can you check this?"


@pytest.mark.asyncio
async def test_non_members_are_never_notified(people: People) -> None:
    slug = "notif-outsider"
    event_id = await _project_with_event(people, slug)

    # Cid cannot watch through the API (the project does not exist for him)...
    assert (await people.cid.put(_subscription(slug, event_id))).status_code == 404
    # ...and a subscription row that outlived a membership sends nothing either.
    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == slug))
        assert project_id is not None
        session.add(
            Subscription(
                user_id=uuid.UUID(people.ids["cid"]),
                project_id=project_id,
                entity_type="event",
                entity_id=uuid.UUID(event_id),
                reasons=["manual"],
            )
        )
        await session.commit()

    body = f"@[Cid]({people.ids['cid']}) ping"
    assert (
        await people.ann.post(_comments(slug, event_id), json={"body": body})
    ).status_code == 201

    assert await _inbox(people.cid) == []
    async with TestSessionLocal() as session:
        count = await session.scalar(
            select(func.count(Notification.id)).where(
                Notification.user_id == uuid.UUID(people.ids["cid"])
            )
        )
    assert count == 0


@pytest.mark.asyncio
async def test_actor_is_excluded_and_the_author_gets_the_open_question(people: People) -> None:
    slug = "notif-actor"
    event_id = await _project_with_event(people, slug)
    assert (await people.bob.put(_subscription(slug, event_id))).status_code == 200

    top = await people.bob.post(_comments(slug, event_id), json={"body": "why two events?"})
    assert top.status_code == 201, top.text

    assert await _inbox(people.bob) == []
    ann_items = await _inbox(people.ann)
    assert [n["kind"] for n in ann_items] == ["open_question"]

    # A reply by the author reaches Bob (watcher and commenter), not Ann herself.
    reply = await people.ann.post(
        _comments(slug, event_id), json={"body": "legacy", "parent_id": top.json()["id"]}
    )
    assert reply.status_code == 201, reply.text
    assert [n["kind"] for n in await _inbox(people.bob)] == ["reply"]
    assert [n["kind"] for n in await _inbox(people.ann)] == ["open_question"]


@pytest.mark.asyncio
async def test_a_muted_thread_is_silent_except_for_mentions(people: People) -> None:
    slug = "notif-mute"
    event_id = await _project_with_event(people, slug)
    assert (await people.bob.put(_subscription(slug, event_id))).status_code == 200

    muted = await people.bob.patch(_subscription(slug, event_id), json={"muted": True})
    assert muted.status_code == 200, muted.text
    # Still watching (the reasons stand), but muted: the button reads "Muted".
    assert muted.json()["muted"] is True
    assert muted.json()["watching"] is True
    assert muted.json()["reasons"] == ["manual"]

    assert (
        await people.ann.post(_comments(slug, event_id), json={"body": "quiet"})
    ).status_code == 201
    assert await _inbox(people.bob) == []

    body = f"@[Bob]({people.ids['bob']}) you specifically"
    assert (
        await people.ann.post(_comments(slug, event_id), json={"body": body})
    ).status_code == 201
    assert [n["kind"] for n in await _inbox(people.bob)] == ["mention"]

    unmuted = await people.bob.patch(_subscription(slug, event_id), json={"muted": False})
    assert unmuted.json()["watching"] is True
    assert unmuted.json()["muted"] is False
    unwatched = await people.bob.delete(_subscription(slug, event_id))
    assert unwatched.status_code == 200
    assert unwatched.json()["watching"] is False
    assert unwatched.json()["muted"] is False


@pytest.mark.asyncio
async def test_muting_what_you_do_not_watch_is_muted_not_watching(people: People) -> None:
    slug = "notif-mute-only"
    event_id = await _project_with_event(people, slug)

    muted = await people.bob.patch(_subscription(slug, event_id), json={"muted": True})
    assert muted.status_code == 200, muted.text
    # A reason-less row only holds the mute: nothing is being watched.
    assert muted.json()["muted"] is True
    assert muted.json()["watching"] is False
    assert muted.json()["reasons"] == []

    unmuted = await people.bob.patch(_subscription(slug, event_id), json={"muted": False})
    assert unmuted.json() == {
        "entity_type": "event",
        "entity_id": event_id,
        "watching": False,
        "muted": False,
        "reasons": [],
    }


async def _event_type_id(client: AsyncClient, slug: str, event_id: str) -> str:
    event = await client.get(f"/api/v1/projects/{slug}/events/{event_id}")
    assert event.status_code == 200, event.text
    return str(event.json()["event_type_id"])


@pytest.mark.asyncio
async def test_an_open_question_asks_type_owners_not_type_watchers(people: People) -> None:
    """Watching an event type brings its events' signals, lifecycle findings and
    open questions — but the open question only to the type's OWNERS, and never
    the ordinary comments on its events."""
    slug = "notif-type-watch"
    event_id = await _project_with_event(people, slug)
    type_id = await _event_type_id(people.ann, slug, event_id)
    type_sub = f"/api/v1/projects/{slug}/subscriptions/event_type/{type_id}"
    watched = await people.bob.put(type_sub)
    assert watched.status_code == 200, watched.text
    assert watched.json()["reasons"] == ["manual"]

    dan = _new_client()
    try:
        await _register(dan, "dan@example.com", "Dan")
        await add_member_by_slug(slug, "dan@example.com", "editor")
        top = await dan.post(_comments(slug, event_id), json={"body": "is this still sent?"})
        assert top.status_code == 201, top.text
        reply = await dan.post(
            _comments(slug, event_id), json={"body": "and where?", "parent_id": top.json()["id"]}
        )
        assert reply.status_code == 201, reply.text
    finally:
        await dan.aclose()

    # Bob watches the type but does not own it: no question, no comment, no reply.
    assert await _inbox(people.bob) == []
    # Ann authored the event: she is asked, and as a watcher of the event she
    # hears the reply.
    assert sorted(n["kind"] for n in await _inbox(people.ann)) == ["open_question", "reply"]

    # Made an owner of the type, Bob is asked the next question.
    async with TestSessionLocal() as session:
        row = await session.scalar(
            select(Subscription).where(
                Subscription.user_id == uuid.UUID(people.ids["bob"]),
                Subscription.entity_type == "event_type",
                Subscription.entity_id == uuid.UUID(type_id),
            )
        )
        assert row is not None
        row.reasons = ["manual", "owner"]
        await session.commit()
    second = await people.ann.post(_comments(slug, event_id), json={"body": "another one"})
    assert second.status_code == 201, second.text
    assert [n["kind"] for n in await _inbox(people.bob)] == ["open_question"]


@pytest.mark.asyncio
async def test_a_mention_in_a_screenshot_thread_reaches_the_member(people: People) -> None:
    slug = "notif-photo-mention"
    event_id = await _project_with_event(people, slug)
    photo = await people.ann.post(
        f"/api/v1/projects/{slug}/events/{event_id}/photos/figma",
        json={"url": "https://www.figma.com/design/abc123/Checkout", "title": "Checkout"},
    )
    assert photo.status_code == 201, photo.text

    body = f"@[Bob]({people.ids['bob']}) does the button match?"
    posted = await people.ann.post(
        f"/api/v1/projects/{slug}/events/{event_id}/photos/{photo.json()['id']}/comments",
        json={"body": body},
    )
    assert posted.status_code == 201, posted.text

    items = await _inbox(people.bob)
    assert [(n["kind"], n["entity_id"]) for n in items] == [("mention", event_id)]
    assert items[0]["body"] == "@Bob does the button match?"
    assert items[0]["url"] == f"/p/{slug}/events/detail/{event_id}"


@pytest.mark.asyncio
async def test_a_failing_notification_never_fails_the_comment(
    people: People, monkeypatch: pytest.MonkeyPatch
) -> None:
    slug = "notif-best-effort"
    event_id = await _project_with_event(people, slug)
    assert (await people.bob.put(_subscription(slug, event_id))).status_code == 200

    async def _boom(*_args: object, **_kwargs: object) -> set[uuid.UUID]:
        raise RuntimeError("notifications unavailable")

    monkeypatch.setattr(notification_service, "notify", _boom)

    posted = await people.ann.post(_comments(slug, event_id), json={"body": "still posted"})
    assert posted.status_code == 201, posted.text

    listed = await people.ann.get(_comments(slug, event_id))
    assert [row["body"] for row in listed.json()] == ["still posted"]
    assert await _inbox(people.bob) == []
    # The commenter's subscription is its own step and still landed.
    state = await people.ann.get(_subscription(slug, event_id))
    assert "commenter" in state.json()["reasons"]


@pytest.mark.asyncio
async def test_read_and_unread(people: People) -> None:
    slug = "notif-read"
    event_id = await _project_with_event(people, slug)
    assert (await people.bob.put(_subscription(slug, event_id))).status_code == 200
    for text in ("one", "two", "three"):
        resp = await people.ann.post(_comments(slug, event_id), json={"body": text})
        assert resp.status_code == 201

    items = await _inbox(people.bob)
    assert [n["body"] for n in items] == ["three", "two", "one"]
    assert await _unread(people.bob) == 3

    one = await people.bob.post("/api/v1/me/notifications/read", json={"ids": [items[0]["id"]]})
    assert one.status_code == 200, one.text
    assert one.json() == {"updated": 1, "unread": 2}
    assert [n["body"] for n in await _inbox(people.bob, unread=True)] == ["two", "one"]

    # Someone else's ids are ignored, not an error.
    ann_try = await people.ann.post("/api/v1/me/notifications/read", json={"ids": [items[1]["id"]]})
    assert ann_try.json()["updated"] == 0
    assert await _unread(people.bob) == 2

    page = await people.bob.get("/api/v1/me/notifications", params={"limit": 2})
    assert len(page.json()["items"]) == 2
    rest = await people.bob.get(
        "/api/v1/me/notifications", params={"limit": 2, "cursor": page.json()["next_cursor"]}
    )
    assert [n["body"] for n in rest.json()["items"]] == ["one"]
    assert rest.json()["next_cursor"] is None

    everything = await people.bob.post("/api/v1/me/notifications/read", json={"all": True})
    assert everything.json() == {"updated": 2, "unread": 0}
    assert (await people.bob.post("/api/v1/me/notifications/read", json={})).status_code == 422


@pytest.mark.asyncio
async def test_the_list_spans_only_projects_the_user_is_a_member_of(people: People) -> None:
    kept, dropped = "notif-kept", "notif-dropped"
    kept_event = await _project_with_event(people, kept)
    dropped_event = await _project_with_event(people, dropped)
    for slug, event_id in ((kept, kept_event), (dropped, dropped_event)):
        assert (await people.bob.put(_subscription(slug, event_id))).status_code == 200
        assert (
            await people.ann.post(_comments(slug, event_id), json={"body": slug})
        ).status_code == 201
    assert sorted(n["project_slug"] for n in await _inbox(people.bob)) == [dropped, kept]

    await remove_member_by_slug(dropped, "bob@example.com")

    assert [n["project_slug"] for n in await _inbox(people.bob)] == [kept]
    assert await _unread(people.bob) == 1


@pytest.mark.asyncio
async def test_prefs_default_and_update(people: People) -> None:
    got = await people.bob.get("/api/v1/me/notification-prefs")
    assert got.status_code == 200, got.text
    assert got.json()["email_mode"] == "daily"
    assert got.json()["mentions_email"] is True

    patched = await people.bob.patch(
        "/api/v1/me/notification-prefs", json={"email_mode": "weekly", "mentions_email": False}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["email_mode"] == "weekly"
    assert patched.json()["mentions_email"] is False
    bad = await people.bob.patch("/api/v1/me/notification-prefs", json={"email_mode": "hourly"})
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_signal_throttle_sends_one_per_window(people: People) -> None:
    slug = "notif-throttle"
    event_id = await _project_with_event(people, slug)
    assert (await people.bob.put(_subscription(slug, event_id))).status_code == 200

    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == slug))
        assert project_id is not None
        for _ in range(2):
            await notification_service.notify(
                session,
                project_id=project_id,
                kind="signal",
                entity_type="event",
                entity_id=uuid.UUID(event_id),
                title="Volume dropped",
                url=f"/p/{slug}/events/detail/{event_id}",
                watchers_of=[("event", uuid.UUID(event_id))],
                throttle=timedelta(hours=6),
            )
            await session.commit()

    assert [n["kind"] for n in await _inbox(people.bob)] == ["signal"]
