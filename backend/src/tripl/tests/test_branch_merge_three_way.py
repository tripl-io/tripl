"""Branch merge: photos and their discussion merge three-way against the base.

Each test here pins one side's change after the cut surviving the other side's
unrelated change — the shape the photos arm and the comment merge used to get
wrong by treating the branch's current state as the whole answer.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl import cache
from tripl.models.event import Event
from tripl.models.event_photo import EventPhoto
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.services import search_service
from tripl.services.plan_branch_conflicts import comparable_field
from tripl.services.plan_branch_merge_service import (
    _split_identity_rows,
    _three_way_count,
    _touched_event_names,
)
from tripl.tests._project_ids import project_id_by_slug
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_plan_branches import (
    _approve_and_merge,
    _attach_main_figma,
    _create_branch,
    _seed_plan,
    _transition,
)

_P = "https://www.figma.com/file/p/P"
_Q = "https://www.figma.com/file/q/Q"
_R = "https://www.figma.com/file/r/R"


async def _main_event_id(client: AsyncClient, slug: str) -> str:
    events = await client.get(f"/api/v1/projects/{slug}/events")
    return str(events.json()["items"][0]["id"])


async def _branch_photo(branch_id: str, url: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with TestSessionLocal() as session:
        branch_event = (
            (await session.execute(select(Event).where(Event.branch_id == uuid.UUID(branch_id))))
            .scalars()
            .one()
        )
        photo = (
            (
                await session.execute(
                    select(EventPhoto).where(
                        EventPhoto.event_id == branch_event.id, EventPhoto.external_url == url
                    )
                )
            )
            .scalars()
            .one()
        )
        return branch_event.id, photo.id


async def _comments(photo_id: str | uuid.UUID) -> list[EventPhotoComment]:
    async with TestSessionLocal() as session:
        rows = await session.execute(
            select(EventPhotoComment).where(EventPhotoComment.photo_id == uuid.UUID(str(photo_id)))
        )
        return list(rows.scalars().all())


@pytest.mark.asyncio
async def test_a_comment_only_branch_leaves_main_photo_changes_alone(client: AsyncClient) -> None:
    """tripl-0zpq.132: main adds Q and deletes R after the cut; the branch only
    comments on P. The photos arm used to replace main's set with the branch's,
    deleting Q and putting R back."""
    slug = "merge3w-set"
    await _seed_plan(client, slug)
    main_event_id = await _main_event_id(client, slug)
    kept = await _attach_main_figma(client, slug, main_event_id, _P, "P")
    gone = await _attach_main_figma(client, slug, main_event_id, _R, "R")
    branch_id = await _create_branch(client, slug, "feature-talk")

    added = await _attach_main_figma(client, slug, main_event_id, _Q, "Q")
    deleted = await client.delete(f"/api/v1/projects/{slug}/events/{main_event_id}/photos/{gone}")
    assert deleted.status_code == 204

    branch_event_id, branch_photo_id = await _branch_photo(branch_id, _P)
    talk = await client.post(
        f"/api/v1/projects/{slug}/events/{branch_event_id}/photos/{branch_photo_id}/comments",
        json={"body": "only talk"},
    )
    assert talk.status_code == 201

    resp = await _approve_and_merge(client, slug, branch_id)
    assert resp.status_code == 200, resp.text

    listed = await client.get(f"/api/v1/projects/{slug}/events/{main_event_id}/photos")
    assert sorted(row["id"] for row in listed.json()) == sorted([kept, added])
    assert [c.body for c in await _comments(kept)] == ["only talk"]


@pytest.mark.asyncio
async def test_photo_comments_merge_three_way_and_keep_their_time(client: AsyncClient) -> None:
    """tripl-0zpq.135 + .143: a comment main deleted after the cut stays
    deleted, one the branch deleted leaves main too, and one written on the
    branch arrives with the time it was written, not the merge's."""
    slug = "merge3w-comments"
    await _seed_plan(client, slug)
    main_event_id = await _main_event_id(client, slug)
    photo_id = await _attach_main_figma(client, slug, main_event_id, _P, "P")
    main_url = f"/api/v1/projects/{slug}/events/{main_event_id}/photos/{photo_id}/comments"
    deleted_on_main = (await client.post(main_url, json={"body": "deleted on main"})).json()
    await client.post(main_url, json={"body": "deleted on branch"})
    branch_id = await _create_branch(client, slug, "feature-talk")

    gone = await client.delete(f"{main_url}/{deleted_on_main['id']}")
    assert gone.status_code == 204

    branch_event_id, branch_photo_id = await _branch_photo(branch_id, _P)
    branch_url = (
        f"/api/v1/projects/{slug}/events/{branch_event_id}/photos/{branch_photo_id}/comments"
    )
    branch_copy = next(c for c in await _comments(branch_photo_id) if c.body == "deleted on branch")
    assert (await client.delete(f"{branch_url}/{branch_copy.id}")).status_code == 204
    new_id = (await client.post(branch_url, json={"body": "new on branch"})).json()["id"]
    written_at = datetime(2026, 1, 5, 9, 30, tzinfo=UTC)
    async with TestSessionLocal() as session:
        row = await session.get(EventPhotoComment, uuid.UUID(new_id))
        assert row is not None
        row.created_at = written_at
        await session.commit()

    resp = await _approve_and_merge(client, slug, branch_id)
    assert resp.status_code == 200, resp.text

    on_main = await _comments(photo_id)
    assert [c.body for c in on_main] == ["new on branch"]
    assert on_main[0].created_at.replace(tzinfo=UTC) == written_at


def test_comparable_photos_ignore_the_order_a_comment_gives_them() -> None:
    """tripl-0zpq.136: the snapshot sorts photos by canonical JSON with the
    comments inside, so one comment can swap two photos. Stripping the comments
    without re-sorting left that swap in place and read as a photo change."""

    def snapshot(*photos: dict[str, Any]) -> dict[str, Any]:
        canonical = lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))  # noqa: E731
        return {"photos": sorted(photos, key=canonical)}

    a = {"original_filename": "a.png", "sort_order": 0, "comments": []}
    b = {"original_filename": "b.png", "sort_order": 1, "comments": []}
    a_discussed = {
        **a,
        "comments": [{"user_fingerprint": "u", "body_fingerprint": "x", "replies": []}],
    }
    base, main = snapshot(a, b), snapshot(a_discussed, b)
    assert base["photos"][0]["original_filename"] != main["photos"][0]["original_filename"]
    assert comparable_field(base, "photos") == comparable_field(main, "photos")


def test_touched_events_are_keyed_by_type_and_name() -> None:
    """tripl-0zpq.137: two types may each have a ``login``; keyed on the name
    alone, the branch's edit of app/login compared web/login with itself."""

    def event(event_type: str, description: str) -> dict[str, Any]:
        return {
            "event_type_name": event_type,
            "name": "login",
            "status": "live",
            "description": description,
        }

    base = {"events": [event("app", ""), event("web", "")]}
    branch = {"events": [event("app", "edited on the branch"), event("web", "")]}
    assert _touched_event_names(base, branch) == {("app", "login")}


@pytest.mark.asyncio
async def test_merge_drops_main_list_caches(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tripl-0zpq.133: the merge rewrites main's types and meta fields, so the
    Redis lists every other write invalidates must go too."""
    slug = "merge3w-cache"
    await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)
    await _transition(client, slug, branch_id, "submit")
    await _transition(client, slug, branch_id, "approve")
    dropped: list[str] = []

    async def record(prefix: str) -> None:
        dropped.append(prefix)

    monkeypatch.setattr(cache, "delete_prefix", record)
    resp = await client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge")
    assert resp.status_code == 200, resp.text
    assert {
        cache.prefix_event_types(await project_id_by_slug(slug)),
        cache.prefix_meta_fields(await project_id_by_slug(slug)),
        cache.prefix_projects(),
    } <= set(dropped)


@pytest.mark.asyncio
async def test_a_failed_search_reindex_does_not_fail_a_committed_merge(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tripl-0zpq.139: a reindex whose flush fails leaves the session waiting
    for a rollback, and the response read after it used to 500."""

    async def broken_reindex(session, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        session.add(EventPhotoComment(id=uuid.uuid4(), photo_id=uuid.uuid4(), body=None))
        await session.flush()

    monkeypatch.setattr(search_service, "reindex_project_branch", broken_reindex)
    slug = "merge3w-reindex"
    await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)
    resp = await _approve_and_merge(client, slug, branch_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "merged"


def test_both_sides_adding_one_attachment_keeps_every_copy() -> None:
    """PR #163 review: main added one copy and the branch two. Pairing main's
    copy with a branch copy spent one of the branch's additions, leaving two
    rows where the count said three."""
    keep = _three_way_count(base=0, ours=1, theirs=2)
    pairs, added, doomed = _split_identity_rows(["m1"], ["b1", "b2"], keep)
    assert (keep, pairs, added, doomed) == (3, [], ["b1", "b2"], [])
    # Unchanged on both sides: one pair, nothing added or removed.
    assert _split_identity_rows(["m1"], ["b1"], 1) == ([("m1", "b1")], [], [])


@pytest.mark.asyncio
async def test_a_branch_reply_under_a_comment_main_deleted_goes_with_it(
    client: AsyncClient,
) -> None:
    """PR #163 review: main deleted a comment after the cut and the branch
    answered its copy. The reply used to reach main as a top-level comment
    answering nothing."""
    slug = "merge3w-orphan"
    await _seed_plan(client, slug)
    main_event_id = await _main_event_id(client, slug)
    photo_id = await _attach_main_figma(client, slug, main_event_id, _P, "P")
    main_url = f"/api/v1/projects/{slug}/events/{main_event_id}/photos/{photo_id}/comments"
    parent = (await client.post(main_url, json={"body": "deleted on main"})).json()
    branch_id = await _create_branch(client, slug, "feature-talk")
    assert (await client.delete(f"{main_url}/{parent['id']}")).status_code == 204

    branch_event_id, branch_photo_id = await _branch_photo(branch_id, _P)
    branch_url = (
        f"/api/v1/projects/{slug}/events/{branch_event_id}/photos/{branch_photo_id}/comments"
    )
    branch_parent = next(iter(await _comments(branch_photo_id)))
    reply = await client.post(
        branch_url, json={"body": "reply on the branch", "parent_id": str(branch_parent.id)}
    )
    assert reply.status_code == 201
    await client.post(branch_url, json={"body": "new top-level on the branch"})

    resp = await _approve_and_merge(client, slug, branch_id)
    assert resp.status_code == 200, resp.text
    assert [c.body for c in await _comments(photo_id)] == ["new top-level on the branch"]
