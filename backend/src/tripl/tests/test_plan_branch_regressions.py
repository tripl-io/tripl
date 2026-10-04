"""Plan-branch regressions."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.config import settings
from tripl.models import Base
from tripl.models.event import Event
from tripl.models.event_photo import EventPhoto
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.services._plan_branch_renames import pair_renames
from tripl.storage.photo_storage import reset_photo_storage
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_plan_branches import (
    _approve_and_merge,
    _create_branch,
    _main_branch_id,
    _seed_plan,
)
from tripl.worker.tasks import maintenance

# --- a main-side rename is not a branch rename ---------------


def test_pair_renames_ignores_a_base_key_main_handed_to_another_identity() -> None:
    """The pure repro: main deleted c (S2) and renamed a (S1) to c.

    The branch touched neither, so it still holds S1 under ``a``. Read as a
    rename, main's ``c`` would be moved back to ``a``.
    """
    base = {("T", "a"): "S1", ("T", "c"): "S2"}
    main = {("T", "c"): "S1"}
    assert pair_renames(base, main, dict(base)) == {}


def test_pair_renames_ignores_a_variable_name_main_handed_to_another_identity() -> None:
    """The same shape keyed the way the merge keys variables."""
    base = {("v1",): "raw_v1", ("v2",): "raw_v2"}
    main = {("v2",): "raw_v1"}
    assert pair_renames(base, main, dict(base)) == {}


def test_pair_renames_still_pairs_a_true_branch_rename() -> None:
    """The guard reads the base identity, so a real branch rename still pairs."""
    base = {("T", "a"): "S1", ("T", "c"): "S2"}
    branch = {("T", "b"): "S1", ("T", "c"): "S2"}
    assert pair_renames(base, dict(base), branch) == {("T", "a"): ("T", "b")}


async def _main_events(main_branch_id: uuid.UUID) -> dict[str, Event]:
    async with TestSessionLocal() as session:
        rows = (
            (await session.execute(select(Event).where(Event.branch_id == main_branch_id)))
            .scalars()
            .all()
        )
    return {row.name: row for row in rows}


@pytest.mark.asyncio
async def test_merging_an_unrelated_branch_keeps_mains_retire_and_promote(
    client: AsyncClient,
) -> None:
    """Main retires v1 and promotes v2 into its name while a branch is open.

    Before the fix the merge renamed main's promoted row back to its old name
    and re-created the event main had deleted.
    """
    slug = "b18-retire-promote"
    et_id = await _seed_plan(client, slug)
    for name in ("checkout:v1", "checkout:v2"):
        created = await client.post(
            f"/api/v1/projects/{slug}/events", json={"event_type_id": et_id, "name": name}
        )
        assert created.status_code == 201, created.text
    main_branch_id = await _main_branch_id()
    # The scan identity a warehouse scan stamps; the API leaves it empty on an
    # untemplated type, and ``pair_renames`` pairs on nothing else.
    async with TestSessionLocal() as session:
        for row in (
            (await session.execute(select(Event).where(Event.branch_id == main_branch_id)))
            .scalars()
            .all()
        ):
            row.source_name = f"{row.name}_raw"
        await session.commit()
    before = await _main_events(main_branch_id)
    v1, v2 = before["checkout:v1"], before["checkout:v2"]

    branch_id = await _create_branch(client, slug)
    # The branch's own, unrelated change.
    branch_ets = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    branch_et_id = next(et["id"] for et in branch_ets.json() if et["name"] == "track")
    added = await client.post(
        f"/api/v1/projects/{slug}/events?branch={branch_id}",
        json={"event_type_id": branch_et_id, "name": "signup:done"},
    )
    assert added.status_code == 201, added.text

    # On main: retire v1, then promote v2 into v1's name.
    assert (await client.delete(f"/api/v1/projects/{slug}/events/{v1.id}")).status_code == 204
    renamed = await client.patch(
        f"/api/v1/projects/{slug}/events/{v2.id}", json={"name": "checkout:v1"}
    )
    assert renamed.status_code == 200, renamed.text

    resp = await _approve_and_merge(client, slug, branch_id)
    assert resp.status_code == 200, resp.text

    after = await _main_events(main_branch_id)
    assert set(after) == {"purchase:success", "checkout:v1", "signup:done"}
    promoted = after["checkout:v1"]
    assert promoted.id == v2.id
    assert promoted.source_name == "checkout:v2_raw"


# --- deleting a branch row keeps the thread its twin shows ---


async def _branch_track_id(client: AsyncClient, slug: str, branch_id: str, name: str) -> str:
    listed = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    return str(next(et["id"] for et in listed.json() if et["name"] == name))


async def _create_event(
    client: AsyncClient, slug: str, et_id: str, name: str, *, branch_id: str | None = None
) -> str:
    query = f"?branch={branch_id}" if branch_id else ""
    created = await client.post(
        f"/api/v1/projects/{slug}/events{query}", json={"event_type_id": et_id, "name": name}
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _ask(client: AsyncClient, slug: str, event_id: str, body: str, **extra: str) -> str:
    resp = await client.post(
        f"/api/v1/projects/{slug}/events/{event_id}/comments", json={"body": body, **extra}
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _thread_ids(client: AsyncClient, slug: str, event_id: str) -> set[str]:
    resp = await client.get(f"/api/v1/projects/{slug}/events/{event_id}/comments")
    assert resp.status_code == 200, resp.text
    return {row["id"] for row in resp.json()}


async def _branch_question_then_twin(
    client: AsyncClient, slug: str, *, event_type: str = "track"
) -> tuple[str, str, str, set[str]]:
    """A question on a branch-only event, then main grows the same event.

    Returns ``(branch_id, branch_row_id, twin_id, comment_ids)``. With
    ``event_type`` other than ``track`` the type is created on the branch too,
    and on main afterwards, so the event type's own delete doors can be tested.
    """
    main_et_id = await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)
    if event_type != "track":
        created = await client.post(
            f"/api/v1/projects/{slug}/event-types?branch={branch_id}",
            json={"name": event_type, "display_name": event_type},
        )
        assert created.status_code == 201, created.text
    branch_et_id = await _branch_track_id(client, slug, branch_id, event_type)
    branch_row_id = await _create_event(
        client, slug, branch_et_id, "checkout:new_tap", branch_id=branch_id
    )
    question = await _ask(client, slug, branch_row_id, "asked before main had it")
    reply = await _ask(client, slug, branch_row_id, "every screen?", parent_id=question)

    if event_type != "track":
        main_type = await client.post(
            f"/api/v1/projects/{slug}/event-types",
            json={"name": event_type, "display_name": event_type},
        )
        assert main_type.status_code == 201, main_type.text
        main_et_id = str(main_type.json()["id"])
    twin_id = await _create_event(client, slug, main_et_id, "checkout:new_tap")
    comment_ids = {question, reply}
    # The twin already shows the branch row's thread through its anchor pair.
    assert comment_ids <= await _thread_ids(client, slug, branch_row_id)
    return branch_id, branch_row_id, twin_id, comment_ids


async def _anchored_on(event_id: str) -> set[str]:
    async with TestSessionLocal() as session:
        rows = await session.execute(
            select(EventPhotoComment.id).where(EventPhotoComment.event_id == uuid.UUID(event_id))
        )
        return {str(row_id) for row_id in rows.scalars()}


@pytest.mark.asyncio
async def test_deleting_a_branch_event_keeps_its_thread_on_the_twin(client: AsyncClient) -> None:
    slug = "b18-thread-delete-event"
    branch_id, row_id, twin_id, comments = await _branch_question_then_twin(client, slug)
    resp = await client.delete(f"/api/v1/projects/{slug}/events/{row_id}?branch={branch_id}")
    assert resp.status_code == 204, resp.text
    assert comments <= await _anchored_on(twin_id)
    assert comments <= await _thread_ids(client, slug, twin_id)


@pytest.mark.asyncio
async def test_bulk_deleting_branch_events_keeps_their_thread_on_the_twin(
    client: AsyncClient,
) -> None:
    slug = "b18-thread-bulk-delete"
    branch_id, row_id, twin_id, comments = await _branch_question_then_twin(client, slug)
    resp = await client.post(
        f"/api/v1/projects/{slug}/events/bulk-delete?branch={branch_id}",
        json={"event_ids": [row_id]},
    )
    assert resp.status_code == 204, resp.text
    assert comments <= await _anchored_on(twin_id)


@pytest.mark.asyncio
async def test_deleting_a_branch_event_type_keeps_its_events_threads_on_the_twin(
    client: AsyncClient,
) -> None:
    slug = "b18-thread-delete-type"
    branch_id, _row_id, twin_id, comments = await _branch_question_then_twin(
        client, slug, event_type="screen"
    )
    branch_et_id = await _branch_track_id(client, slug, branch_id, "screen")
    resp = await client.delete(
        f"/api/v1/projects/{slug}/event-types/{branch_et_id}?branch={branch_id}"
    )
    assert resp.status_code == 204, resp.text
    assert comments <= await _anchored_on(twin_id)


@pytest.mark.asyncio
async def test_reverting_an_added_branch_event_keeps_its_thread_on_the_twin(
    client: AsyncClient,
) -> None:
    slug = "b18-thread-revert-event"
    branch_id, _row_id, twin_id, comments = await _branch_question_then_twin(client, slug)
    resp = await client.post(
        f"/api/v1/projects/{slug}/branches/{branch_id}/revert",
        json={"entity_type": "event", "name": "checkout:new_tap", "parent": "track"},
    )
    assert resp.status_code == 200, resp.text
    assert comments <= await _anchored_on(twin_id)


@pytest.mark.asyncio
async def test_reverting_an_added_branch_event_type_keeps_its_events_threads(
    client: AsyncClient,
) -> None:
    slug = "b18-thread-revert-type"
    branch_id, _row_id, twin_id, comments = await _branch_question_then_twin(
        client, slug, event_type="screen"
    )
    resp = await client.post(
        f"/api/v1/projects/{slug}/branches/{branch_id}/revert",
        json={"entity_type": "event_type", "name": "screen"},
    )
    assert resp.status_code == 200, resp.text
    assert comments <= await _anchored_on(twin_id)


@pytest.mark.asyncio
async def test_deleting_the_branch_keeps_its_rows_threads_on_their_twins(
    client: AsyncClient,
) -> None:
    slug = "b18-thread-delete-branch"
    branch_id, _row_id, twin_id, comments = await _branch_question_then_twin(client, slug)
    resp = await client.delete(f"/api/v1/projects/{slug}/branches/{branch_id}")
    assert resp.status_code == 204, resp.text
    assert comments <= await _anchored_on(twin_id)
    assert comments <= await _thread_ids(client, slug, twin_id)


@pytest.mark.asyncio
async def test_deleting_a_branch_only_event_with_no_twin_still_drops_its_thread(
    client: AsyncClient,
) -> None:
    """No twin, nowhere to go: the thread goes with the row, as before."""
    slug = "b18-thread-no-twin"
    await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)
    branch_et_id = await _branch_track_id(client, slug, branch_id, "track")
    row_id = await _create_event(client, slug, branch_et_id, "only:here", branch_id=branch_id)
    question = await _ask(client, slug, row_id, "only on the branch")
    resp = await client.delete(f"/api/v1/projects/{slug}/events/{row_id}?branch={branch_id}")
    assert resp.status_code == 204, resp.text
    async with TestSessionLocal() as session:
        assert await session.get(EventPhotoComment, uuid.UUID(question)) is None


# --- the orphan photo sweep -----------------------------------


@pytest.fixture
def sweep_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, sessionmaker[Session]]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'sweep.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "photos"
    root.mkdir()
    monkeypatch.setattr(maintenance, "_get_sync_session", factory)
    monkeypatch.setattr(settings, "photo_local_dir", str(root))
    monkeypatch.setattr(settings, "gcs_photo_bucket", "")
    monkeypatch.setattr(settings, "photo_orphan_sweep_grace_hours", 24)
    reset_photo_storage()
    try:
        yield root, factory
    finally:
        reset_photo_storage()
        engine.dispose()


def _write(root: Path, key: str, *, age_hours: float) -> Path:
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"jpeg")
    stamp = time.time() - age_hours * 3600
    os.utime(path, (stamp, stamp))
    return path


def test_the_sweep_deletes_only_old_unreferenced_photo_blobs(
    sweep_env: tuple[Path, sessionmaker[Session]],
) -> None:
    root, factory = sweep_env
    event_id = uuid.uuid4()
    orphan = _write(root, f"events/{event_id}/orphan.jpg", age_hours=48)
    referenced = _write(root, f"events/{event_id}/kept.jpg", age_hours=48)
    fresh = _write(root, f"events/{event_id}/uploading.jpg", age_hours=1)
    # Not under the photo prefix: the directory may hold what tripl did not write.
    foreign = _write(root, "backups/readme.txt", age_hours=48)
    with factory() as session:
        session.add(
            EventPhoto(
                id=uuid.uuid4(),
                project_id=uuid.uuid4(),
                event_id=event_id,
                original_filename="kept.jpg",
                content_type="image/jpeg",
                size_bytes=4,
                storage_backend="local",
                storage_key=f"events/{event_id}/kept.jpg",
            )
        )
        session.commit()

    result = maintenance.sweep_orphan_photo_blobs()

    assert not orphan.exists()
    assert referenced.exists()
    assert fresh.exists()
    assert foreign.exists()
    assert result["deleted"] == [f"local:events/{event_id}/orphan.jpg"]
    assert result["skipped_backends"] == []


def test_the_sweep_is_on_the_beat_schedule() -> None:
    from celery.schedules import crontab

    from tripl.worker.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["sweep-orphan-photo-blobs"]
    assert entry["task"] == "tripl.worker.tasks.maintenance.sweep_orphan_photo_blobs"
    assert isinstance(entry["schedule"], crontab)


def test_orphan_sweep_refuses_when_no_row_references_the_backend(tmp_path, monkeypatch) -> None:
    """An empty or half-restored database must not read as 'every blob is an orphan'."""
    import os
    import time

    from tripl.config import settings
    from tripl.worker.tasks import maintenance

    monkeypatch.setattr(settings, "photo_local_dir", str(tmp_path))
    monkeypatch.setattr(settings, "gcs_photo_bucket", "")
    blob = tmp_path / "events" / "e" / "old.jpg"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"x")
    old = time.time() - 30 * 24 * 3600
    os.utime(blob, (old, old))

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    monkeypatch.setattr(maintenance, "_get_sync_session", lambda: session)

    result = maintenance.sweep_orphan_photo_blobs()

    assert blob.exists()
    assert result["deleted"] == []
    assert result["skipped_backends"] == ["local"]
