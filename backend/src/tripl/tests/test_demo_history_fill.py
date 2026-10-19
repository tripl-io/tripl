"""A demo opens on its last two days and fills the rest of its history after.

The first build (``POST /projects/demo``) replays and scores
``pipeline.FIRST_BUILD_HISTORY`` and promotes the demo; the history fill
(``demo_history_service.finish_demo_history``, a task of its own) replays the
rest of ``DEMO_HISTORY_DAYS`` back to the history's start, re-scores the whole
history, files the alerts two days of baseline could not, and runs the
steps that read the whole history. Here the fill is held when the build queues it and run by the
test, so each phase is observed on its own; so is each slice the fill queues the rest in.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.config import settings
from tripl.core.bucketing import to_utc
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.project import Project
from tripl.models.schema_drift import SchemaDrift
from tripl.models.signal_triage import SignalTriage
from tripl.services import demo_history_service, demo_service
from tripl.services.demo import noise, pipeline
from tripl.services.demo.scenario import DemoContext
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
        contract_drifts = (
            await session.execute(
                select(SchemaDrift.field_name, SchemaDrift.drift_type).where(
                    SchemaDrift.scan_config_id == config_id
                )
            )
        ).all()
    # The first build replayed its two days only.
    assert replays
    assert min(_starts(replays)) >= pipeline.first_build_start(seeded_at)
    # Its live collection waits for the fill, but not its field-contract check:
    # the tour's schema-drift step is there when the demo opens.
    assert ("currency", "enum_violation") in {(name, str(kind)) for name, kind in contract_drifts}

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


def test_the_newest_fill_window_is_a_whole_span(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fill runs newest first, so the window next to the first build is a whole week.

    The short remainder (the history starts at midnight, the first build at the
    build hour) is the oldest window, the one replayed last.
    """
    monkeypatch.setattr(settings, "demo_history_days", noise.DEMO_HISTORY_DAYS)
    for hour in range(0, 24 * 7, 5):
        now = datetime(2026, 10, 12, tzinfo=UTC) + timedelta(hours=hour)
        start, end = noise.history_start(now), pipeline.first_build_start(now)
        windows = pipeline.fill_windows(now)
        assert end - start >= pipeline.REPLAY_JOB_SPAN
        assert windows[-1] == (end - pipeline.REPLAY_JOB_SPAN, end)
        assert windows[0][0] == start
        for (_, prev_end), (next_start, _) in zip(windows, windows[1:], strict=False):
            assert prev_end == next_start
        assert all(b - a == pipeline.REPLAY_JOB_SPAN for a, b in windows[1:])
        assert windows[0][1] - windows[0][0] <= pipeline.REPLAY_JOB_SPAN


class _FillLoopDone(Exception):
    """Raised where the fill leaves its replay loop, so a test sees only the loop."""


class _RecordingSession:
    """The one session call the fill's replay loop makes: the collection job."""

    def __init__(self) -> None:
        self.windows: list[tuple[str, str]] = []

    async def run_sync(
        self, _fn: object, _config_id: str, _job_id: str, time_from: str, time_to: str, **_: object
    ) -> None:
        self.windows.append((time_from, time_to))


async def _replayed_windows(
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    *,
    done: Sequence[Mapping[str, object]] = (),
    horizon: datetime | None = None,
) -> list[tuple[str, str]]:
    """The ``(time_from, time_to)`` the fill's replay loop runs, in order."""
    from tripl.worker.tasks.metrics import demo_window

    async def _job(*_args: object) -> uuid.UUID:
        return uuid.uuid4()

    async def _completed(*_args: object) -> None:
        return None

    async def _summaries(*_args: object, **_kwargs: object) -> list[Mapping[str, object]]:
        return list(done)

    def _leave_loop(*_args: object, **_kwargs: object) -> None:
        raise _FillLoopDone

    monkeypatch.setattr(pipeline, "_new_job", _job)
    monkeypatch.setattr(pipeline, "_require_completed", _completed)
    monkeypatch.setattr(pipeline, "replay_summaries", _summaries)
    monkeypatch.setattr(pipeline, "scoring_window", _leave_loop)
    monkeypatch.setattr(
        demo_window,
        "demo_warehouse_horizon",
        lambda _now: horizon if horizon is not None else noise.history_start(now),
    )
    ctx = DemoContext(project_id=uuid.uuid4(), branch_id=uuid.uuid4(), slug="fill-order", now=now)
    ctx.scan_config_id = uuid.uuid4()
    session = _RecordingSession()
    with pytest.raises(_FillLoopDone):
        await pipeline.fill_history(cast(AsyncSession, session), ctx, deliveries=[])
    return session.windows


_FILL_NOW = datetime(2026, 10, 12, 13, tzinfo=UTC)


def _iso(windows: Sequence[tuple[datetime, datetime]]) -> list[tuple[str, str]]:
    return [(start.isoformat(), end.isoformat()) for start, end in windows]


async def test_the_fill_replays_newest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "demo_history_days", noise.DEMO_HISTORY_DAYS)
    windows = pipeline.fill_windows(_FILL_NOW)
    assert len(windows) > 1
    assert await _replayed_windows(monkeypatch, _FILL_NOW) == _iso(windows[::-1])


async def test_a_fill_window_past_the_horizon_is_skipped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The oldest window, run last, may be gone from the warehouse by then.

    Its job would fail on the start clamp (nothing of the window left) and the
    whole fill with it; skipped, the fill goes on to its scoring pass, and the
    skip is logged, since that window holds the history's first promo send.
    """
    monkeypatch.setattr(settings, "demo_history_days", noise.DEMO_HISTORY_DAYS)
    oldest, *newer = pipeline.fill_windows(_FILL_NOW)
    with caplog.at_level(logging.WARNING, logger=pipeline.__name__):
        replayed = await _replayed_windows(monkeypatch, _FILL_NOW, horizon=oldest[1])
    assert replayed == _iso(newer[::-1])
    dropped = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("demo.history_fill.window_dropped")
    ]
    assert len(dropped) == 1
    assert f"time_from={oldest[0].isoformat()}" in dropped[0]
    assert f"time_to={oldest[1].isoformat()}" in dropped[0]


async def test_a_clamped_completed_window_counts_as_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """A completed job whose start the horizon clamped records a later ``time_from``."""
    monkeypatch.setattr(settings, "demo_history_days", noise.DEMO_HISTORY_DAYS)
    oldest, *newer = pipeline.fill_windows(_FILL_NOW)
    clamped = {
        "time_from": (oldest[0] + timedelta(hours=3)).isoformat(),
        "time_to": oldest[1].isoformat(),
        "replay_chunks_completed": 1,
        "replay_chunks_total": 1,
    }
    replayed = await _replayed_windows(monkeypatch, _FILL_NOW, done=[clamped])
    assert replayed == _iso(newer[::-1])


def _starts(summaries: Sequence[Mapping[str, object]]) -> list[datetime]:
    return [to_utc(datetime.fromisoformat(str(summary["time_from"]))) for summary in summaries]
