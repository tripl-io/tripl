"""The branch merge, batch 2 of the backend review sweep.

* .146 — a screenshot the merge deletes from main releases its blob once no
  row holds the key any more — only after the merge has committed — and a
  failure there never fails the merge.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tripl.config import settings
from tripl.models.event import Event
from tripl.models.event_photo import EventPhoto
from tripl.models.plan_branch import PlanBranch
from tripl.models.plan_revision import PlanRevision
from tripl.storage.photo_storage import LocalPhotoStorage, reset_photo_storage
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_plan_branches import _approve_and_merge, _create_branch, _seed_plan

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def local_photos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The local backend, rooted in a directory of this test's own."""
    monkeypatch.setattr(settings, "photo_storage_backend", "local")
    monkeypatch.setattr(settings, "photo_local_dir", str(tmp_path))
    reset_photo_storage()
    yield tmp_path
    reset_photo_storage()


# --- the blob of a photo the merge deletes ------------------


def _photos_url(slug: str, event_id: str) -> str:
    return f"/api/v1/projects/{slug}/events/{event_id}/photos"


async def _screenshot_on_main(client: AsyncClient, slug: str) -> str:
    """Upload one screenshot onto main's only event; returns its storage key."""
    events = await client.get(f"/api/v1/projects/{slug}/events")
    main_event_id = str(events.json()["items"][0]["id"])
    uploaded = await client.post(
        _photos_url(slug, main_event_id), files={"file": ("shot.png", _PNG, "image/png")}
    )
    assert uploaded.status_code == 201, uploaded.text
    async with TestSessionLocal() as session:
        photo = await session.get(EventPhoto, uuid.UUID(uploaded.json()["id"]))
        assert photo is not None
        assert photo.storage_key
        return photo.storage_key


async def _copy_on(branch_id: str, storage_key: str) -> tuple[str, str]:
    """``(event id, photo id)`` of the branch's copy of the screenshot."""
    async with TestSessionLocal() as session:
        row = (
            await session.execute(
                select(EventPhoto.event_id, EventPhoto.id)
                .join(Event, Event.id == EventPhoto.event_id)
                .where(
                    Event.branch_id == uuid.UUID(branch_id),
                    EventPhoto.storage_key == storage_key,
                )
            )
        ).one()
        return str(row.event_id), str(row.id)


async def _rows_holding(storage_key: str) -> int:
    async with TestSessionLocal() as session:
        rows = await session.execute(
            select(EventPhoto.id).where(EventPhoto.storage_key == storage_key)
        )
        return len(rows.all())


async def _delete_copy_on_branch(
    client: AsyncClient, slug: str, branch_id: str, storage_key: str
) -> None:
    event_id, photo_id = await _copy_on(branch_id, storage_key)
    deleted = await client.delete(f"{_photos_url(slug, event_id)}/{photo_id}")
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_the_merge_releases_the_blob_of_a_screenshot_the_branch_deleted(
    client: AsyncClient, local_photos: Path
) -> None:
    """Replacing a screenshot through a branch. Deleting the branch's copy
    keeps the blob, since main's row still holds it; the merge then deletes
    main's row with a bulk delete that never touched storage, and the object
    stayed behind for good with nothing pointing at it."""
    slug = "merge-releases-blob"
    await _seed_plan(client, slug)
    storage_key = await _screenshot_on_main(client, slug)
    blob = local_photos / storage_key
    branch_id = await _create_branch(client, slug)
    await _delete_copy_on_branch(client, slug, branch_id, storage_key)
    assert blob.read_bytes() == _PNG

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text
    assert await _rows_holding(storage_key) == 0
    assert not blob.exists()


@pytest.mark.asyncio
async def test_the_merge_releases_the_blobs_of_an_event_the_branch_deleted(
    client: AsyncClient, local_photos: Path
) -> None:
    """Copilot on PR #164: deleting the whole EVENT goes down another path.
    ``doomed_main_events`` are deleted before the photo reconciliation runs, and
    their attachments go with them through the FK cascade, so their keys never
    reached the release set and the screenshot stayed in storage for good."""
    slug = "merge-releases-deleted-event-blob"
    await _seed_plan(client, slug)
    storage_key = await _screenshot_on_main(client, slug)
    blob = local_photos / storage_key
    branch_id = await _create_branch(client, slug)

    branch_event_id, _photo_id = await _copy_on(branch_id, storage_key)
    # The event routes take their branch from ``?branch=``, unlike the photo
    # routes, which address a branch's event by its own id.
    dropped = await client.delete(
        f"/api/v1/projects/{slug}/events/{branch_event_id}?branch={branch_id}"
    )
    assert dropped.status_code == 204, dropped.text
    assert blob.read_bytes() == _PNG

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text
    assert await _rows_holding(storage_key) == 0
    assert not blob.exists()


@pytest.mark.asyncio
async def test_the_merge_keeps_a_blob_another_branch_still_holds(
    client: AsyncClient, local_photos: Path
) -> None:
    """The release goes by what the rows say AFTER the merge: another branch
    cut from the same main still shows the screenshot, so the blob stays."""
    slug = "merge-keeps-shared-blob"
    await _seed_plan(client, slug)
    storage_key = await _screenshot_on_main(client, slug)
    replacing = await _create_branch(client, slug, "replaces-the-shot")
    keeping = await _create_branch(client, slug, "keeps-the-shot")
    await _delete_copy_on_branch(client, slug, replacing, storage_key)

    merged = await _approve_and_merge(client, slug, replacing)
    assert merged.status_code == 200, merged.text
    assert await _rows_holding(storage_key) == 1
    event_id, photo_id = await _copy_on(keeping, storage_key)
    served = await client.get(f"{_photos_url(slug, event_id)}/{photo_id}/file")
    assert served.status_code == 200
    assert served.content == _PNG


@pytest.mark.asyncio
async def test_a_blob_that_cannot_be_deleted_never_fails_the_merge(
    client: AsyncClient, local_photos: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release runs after the commit: the merge has happened, and a storage
    outage then can only leave an orphaned object, never a failed merge."""
    slug = "merge-blob-delete-fails"
    await _seed_plan(client, slug)
    storage_key = await _screenshot_on_main(client, slug)
    branch_id = await _create_branch(client, slug)
    await _delete_copy_on_branch(client, slug, branch_id, storage_key)

    async def unreachable(self: LocalPhotoStorage, key: str) -> None:
        raise OSError(f"storage unreachable for {key}")

    monkeypatch.setattr(LocalPhotoStorage, "delete", unreachable)
    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text
    assert merged.json()["status"] == "merged"
    assert await _rows_holding(storage_key) == 0
    assert (local_photos / storage_key).exists()


@contextmanager
def _the_merge_commit_is_refused() -> Iterator[None]:
    """A constraint refusing the merge at its last step: the commit carrying the
    merged revision, when every write ``_apply_merge`` makes — the photos arm's
    bulk delete included — has already been sent."""

    def refuse(session: Session) -> None:
        if any(
            isinstance(row, PlanRevision) and (row.summary or "").startswith("Merged branch")
            for row in session.new
        ):
            raise IntegrityError("COMMIT", {}, Exception("refused by the test"))

    sa_event.listen(Session, "before_commit", refuse)
    try:
        yield
    finally:
        sa_event.remove(Session, "before_commit", refuse)


@pytest.mark.asyncio
async def test_a_merge_that_fails_after_deleting_a_screenshot_keeps_its_blob(
    client: AsyncClient, local_photos: Path
) -> None:
    """The blob goes only once the merge has committed. Released inside the
    transaction — right after the photos arm deletes main's row — a merge that
    failed later rolled that row back, and main showed a screenshot whose
    object was already gone."""
    slug = "merge-fails-keeps-blob"
    await _seed_plan(client, slug)
    storage_key = await _screenshot_on_main(client, slug)
    blob = local_photos / storage_key
    branch_id = await _create_branch(client, slug)
    await _delete_copy_on_branch(client, slug, branch_id, storage_key)

    with _the_merge_commit_is_refused():
        merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 409, merged.text
    assert merged.json()["detail"]["merge_constraint_violation"] is True

    # Main's row is back, and the object it points at is still there.
    async with TestSessionLocal() as session:
        [(event_id, photo_id)] = (
            await session.execute(
                select(EventPhoto.event_id, EventPhoto.id).where(
                    EventPhoto.storage_key == storage_key
                )
            )
        ).all()
        branch = await session.get(PlanBranch, uuid.UUID(branch_id))
        assert branch is not None
        assert branch.status == "approved"
    served = await client.get(f"{_photos_url(slug, str(event_id))}/{photo_id}/file")
    assert served.status_code == 200
    assert served.content == _PNG
    assert blob.read_bytes() == _PNG
