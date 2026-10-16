"""A demo's history fill always ends: an error fails it, a dead worker is swept.

The fill (``demo_history_service.finish_demo_history``) marks itself
``failed`` on any error, wherever it is raised. A worker that dies outright
leaves it ``filling``, and while it fills the demo's scheduled collection and
catalog metrics are held; the stall sweep in ``check_metrics_due`` re-queues
it, and fails it once the re-queues run out.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch
from httpx import AsyncClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from tripl.config import settings
from tripl.models import Base
from tripl.models.domain_enums import DemoHistoryStatus
from tripl.models.project import Project
from tripl.models.scan_job import ScanJob
from tripl.services import demo_history_service
from tripl.services.demo import pipeline
from tripl.services.demo_history_service import (
    HISTORY_FILL_MAX_REQUEUES,
    HISTORY_FILL_STALL,
    stalled_fill_action,
)
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_paused_demo_collection import _run_dispatcher, _seed_scan_config
from tripl.worker.tasks import demo_provision
from tripl.worker.tasks.metrics import schedule as metrics_schedule


@pytest.fixture
def sync_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'fill_stall.db'}")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


@pytest.fixture
def requeued(monkeypatch: MonkeyPatch) -> list[str]:
    queued: list[str] = []
    monkeypatch.setattr(
        demo_provision.fill_demo_history,
        "delay",
        lambda project_id, _request_id: queued.append(project_id),
    )
    return queued


def _filling_demo(
    factory: sessionmaker[Session], *, last_alive: timedelta, attempts: int = 0
) -> uuid.UUID:
    """An active demo whose fill and scan last showed life ``last_alive`` ago."""
    now = datetime.now(UTC)
    with factory() as session:
        seeded = _seed_scan_config(
            session,
            now=now,
            is_demo=True,
            seeded_at=now - last_alive,
            last_accessed=now,
        )
        session.execute(
            update(Project)
            .where(Project.id == seeded.project_id)
            .values(
                demo_history_status=DemoHistoryStatus.filling.value,
                demo_history_attempts=attempts,
            )
        )
        session.execute(
            update(ScanJob)
            .where(ScanJob.scan_config_id == seeded.scan_config_id)
            .values(updated_at=now - last_alive)
        )
        session.commit()
    return seeded.project_id


def _fill_state(factory: sessionmaker[Session], project_id: uuid.UUID) -> tuple[str | None, int]:
    with factory() as session:
        row = session.execute(
            select(Project.demo_history_status, Project.demo_history_attempts).where(
                Project.id == project_id
            )
        ).one()
    return row[0], row[1]


@pytest.mark.parametrize(
    ("stalled", "attempts", "action"),
    [
        (HISTORY_FILL_STALL - timedelta(minutes=1), 0, "wait"),
        (HISTORY_FILL_STALL, 0, "requeue"),
        # Each re-queue waits one stall period longer than the one before.
        (2 * HISTORY_FILL_STALL - timedelta(minutes=1), 1, "wait"),
        (2 * HISTORY_FILL_STALL, 1, "requeue"),
        ((HISTORY_FILL_MAX_REQUEUES + 1) * HISTORY_FILL_STALL, HISTORY_FILL_MAX_REQUEUES, "fail"),
    ],
)
def test_the_stall_policy(stalled: timedelta, attempts: int, action: str) -> None:
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    assert stalled_fill_action(now - stalled, attempts, now) == action


def test_a_filling_demo_is_not_collected_on_schedule(
    sync_factory: sessionmaker[Session], monkeypatch: MonkeyPatch, requeued: list[str]
) -> None:
    """The fill runs the demo's live collection itself; a scheduled one would interleave."""
    project_id = _filling_demo(sync_factory, last_alive=timedelta(minutes=5))

    result, dispatched = _run_dispatcher(sync_factory, monkeypatch)

    assert (result, dispatched, requeued) == ({"checked": 1, "dispatched": 0}, [], [])
    assert _fill_state(sync_factory, project_id) == ("filling", 0)


def test_a_stalled_fill_is_requeued_once_per_stall(
    sync_factory: sessionmaker[Session], monkeypatch: MonkeyPatch, requeued: list[str]
) -> None:
    project_id = _filling_demo(sync_factory, last_alive=HISTORY_FILL_STALL + timedelta(minutes=5))

    _run_dispatcher(sync_factory, monkeypatch)
    _run_dispatcher(sync_factory, monkeypatch)

    # The second tick finds the attempt made: the queued task may just not
    # have started yet.
    assert requeued == [str(project_id)]
    assert _fill_state(sync_factory, project_id) == ("filling", 1)


def test_a_fill_that_keeps_dying_is_failed(
    sync_factory: sessionmaker[Session], monkeypatch: MonkeyPatch, requeued: list[str]
) -> None:
    project_id = _filling_demo(
        sync_factory,
        last_alive=(HISTORY_FILL_MAX_REQUEUES + 1) * HISTORY_FILL_STALL + timedelta(minutes=5),
        attempts=HISTORY_FILL_MAX_REQUEUES,
    )

    _run_dispatcher(sync_factory, monkeypatch)

    assert requeued == []
    assert _fill_state(sync_factory, project_id) == ("failed", HISTORY_FILL_MAX_REQUEUES)


def test_demo_runtime_off_holds_every_demos_catalog_metrics(
    sync_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """``DEMO_RUNTIME_ENABLED=false``: demos keep what provisioning built, metrics too."""
    now = datetime.now(UTC)
    with sync_factory() as session:
        seeded = _seed_scan_config(
            session, now=now, is_demo=True, seeded_at=now - timedelta(days=1), last_accessed=now
        )
        monkeypatch.setattr(settings, "demo_runtime_enabled", True)
        assert metrics_schedule._demo_projects_held(session, {seeded.project_id}, now=now) == set()
        monkeypatch.setattr(settings, "demo_runtime_enabled", False)
        assert metrics_schedule._demo_projects_held(session, {seeded.project_id}, now=now) == {
            seeded.project_id
        }


async def test_an_error_outside_the_replays_fails_the_fill(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    """Reading the build back, not only replaying, is inside the fill's error handling."""
    resp = await client.post(
        "/api/v1/projects", json={"name": "Fill error", "slug": "fill-error", "description": ""}
    )
    assert resp.status_code == 201, resp.text
    project_id = uuid.UUID(resp.json()["id"])
    async with TestSessionLocal() as session:
        await session.execute(
            update(Project)
            .where(Project.id == project_id)
            .values(
                is_demo=True,
                demo_seeded_at=datetime.now(UTC),
                demo_history_status=DemoHistoryStatus.filling.value,
            )
        )
        await session.commit()

    async def _broken(*_args: object) -> None:
        raise RuntimeError("the build could not be read back")

    monkeypatch.setattr(demo_history_service, "_fill_context", _broken)
    async with TestSessionLocal() as session:
        assert await demo_history_service.finish_demo_history(session, project_id) == "failed"
        status = await session.scalar(
            select(Project.demo_history_status).where(Project.id == project_id)
        )
    assert status == "failed"


def test_fill_progress_counts_a_window_once() -> None:
    """A job a dead worker left behind and the one that resumed it share a window."""
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    (first, second, *_rest) = pipeline.fill_windows(now)

    def summary(window: tuple[datetime, datetime], done: int) -> dict[str, object]:
        return {
            "time_from": window[0].isoformat(),
            "time_to": window[1].isoformat(),
            "replay_chunks_completed": done,
            "replay_chunks_total": 4,
        }

    once = pipeline.fill_progress([summary(first, 4), summary(second, 2)], now)
    assert once > 0
    assert (
        pipeline.fill_progress([summary(first, 4), summary(second, 1), summary(second, 2)], now)
        == once
    )
