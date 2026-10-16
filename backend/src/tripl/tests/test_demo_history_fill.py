"""A demo opens on its last two days and fills the rest of its history after.

The first build (``POST /projects/demo``) replays and scores
``pipeline.FIRST_BUILD_HISTORY`` and promotes the demo; the history fill
(``demo_history_service.finish_demo_history``, a task of its own) replays the
rest of ``DEMO_HISTORY_DAYS`` back to the history's start, re-scores the last
three weeks, files the alerts two days of baseline could not, and runs the
steps that read the whole history. Here the fill is held when the build queues it and run by the
test, so each phase is observed on its own; so is each slice the fill queues the rest in.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from tripl.config import settings
from tripl.core.bucketing import to_utc
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.project import Project
from tripl.models.signal_triage import SignalTriage
from tripl.services import demo_history_service, demo_service
from tripl.services.demo import noise, pipeline
from tripl.tests.conftest import TestSessionLocal


@pytest.fixture
def held_fills(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """The demos whose history fill the build queued (held, not run)."""
    held: list[uuid.UUID] = []

    async def _hold(_session: object, project_id: uuid.UUID, _request_id: str | None) -> None:
        held.append(project_id)

    monkeypatch.setattr(settings, "demo_warehouse_mode", "memory")
    monkeypatch.setattr(settings, "demo_history_days", noise.DEMO_HISTORY_DAYS)
    monkeypatch.setattr(demo_service, "enqueue_demo_history_fill", _hold)
    return held


async def test_a_demo_opens_on_two_days_and_the_fill_completes_it(
    client: AsyncClient, held_fills: list[uuid.UUID], demo_enqueued_deliveries: list[str]
) -> None:
    started = time.perf_counter()
    resp = await client.post("/api/v1/projects/demo")
    first_build = time.perf_counter() - started
    assert resp.status_code == 202, resp.text
    demo = resp.json()
    project_id = uuid.UUID(demo["id"])
    assert demo["generation_status"] == "ready"
    assert demo["demo_history_status"] == "filling"
    assert demo["demo_history_progress"] == 0
    assert held_fills == [project_id]

    async with TestSessionLocal() as session:
        seeded_at = to_utc(
            await session.scalar(select(Project.demo_seeded_at).where(Project.id == project_id))
        )
        config_id = await pipeline.demo_scan_config_id(session, project_id)
        assert config_id is not None
        replays = await pipeline.replay_summaries(session, config_id)
        deliveries = await session.scalar(select(func.count()).select_from(AlertDelivery))
    # The first build replayed its two days only.
    assert replays
    assert min(_starts(replays)) >= pipeline.first_build_start(seeded_at)

    # A manual run would interleave with the fill's replays.
    scans = (await client.get(f"/api/v1/projects/{demo['slug']}/scans")).json()
    run = await client.post(f"/api/v1/projects/{demo['slug']}/scans/{scans[0]['id']}/run")
    assert run.status_code == 409, run.text

    # Sliced as on the worker, at its finest: every replay job spends the
    # slice, queues the rest and returns, and the next task resumes where it
    # stopped (a task of its own, so a session of its own).
    started = time.perf_counter()
    slices = 0
    while True:
        async with TestSessionLocal() as session:
            outcome = await demo_history_service.finish_demo_history(
                session, project_id, budget=timedelta(0)
            )
        if outcome != "continued":
            break
        slices += 1
        assert slices <= len(pipeline.fill_windows(seeded_at)), "a slice replayed a window again"
    fill = time.perf_counter() - started
    assert outcome == "complete"
    assert slices == len(pipeline.fill_windows(seeded_at))
    assert held_fills == [project_id] * (1 + slices)
    print(f"\nfirst build {first_build:.1f}s, history fill {fill:.1f}s in {slices + 1} slices")

    filled = (await client.get(f"/api/v1/projects/{demo['slug']}")).json()
    assert filled["demo_history_status"] == "complete"
    assert filled["demo_history_progress"] is None
    async with TestSessionLocal() as session:
        replays = await pipeline.replay_summaries(session, config_id)
        refiled = await session.scalar(select(func.count()).select_from(AlertDelivery))
        verdicts = await session.scalar(
            select(func.count())
            .select_from(SignalTriage)
            .where(SignalTriage.note == pipeline.DEMO_WEEKLY_PROMO_NOTE)
        )
        # A redelivered task after the fill ended finds nothing to do.
        assert await demo_history_service.finish_demo_history(session, project_id) == "skipped"
    # The whole history is stored now, the full baseline found the volume
    # incident the two days could not and its alert is filed and queued, and
    # every weekly send in the scored weeks holds its expected verdict.
    assert min(_starts(replays)) == noise.history_start(seeded_at)
    assert refiled is not None and deliveries is not None and refiled > deliveries
    assert len(demo_enqueued_deliveries) == refiled
    assert verdicts and verdicts >= 3


def test_the_fill_meets_the_first_build() -> None:
    """Whatever the hour, the fill and the first build tile the history exactly."""
    for hour in range(0, 24 * 7, 5):
        now = datetime(2026, 10, 12, tzinfo=UTC) + timedelta(hours=hour)
        windows = pipeline.fill_windows(now) + pipeline.history_windows(
            now, pipeline.first_build_start(now)
        )
        assert windows[0][0] == noise.history_start(now)
        assert windows[-1][1] == now
        for (_, end), (start, _) in zip(windows, windows[1:], strict=False):
            assert end == start
        assert all(end - start <= pipeline.REPLAY_JOB_SPAN for start, end in windows)
        assert pipeline.fill_progress([], now) == 0


def _starts(summaries: list[object]) -> list[datetime]:
    return [
        to_utc(datetime.fromisoformat(str(summary["time_from"])))  # type: ignore[index]
        for summary in summaries
    ]
