"""Batch 5, lane W5-demopause: a PAUSED demo must not be collected.

``advance_demos`` already skips a demo nobody has opened for
``DEMO_IDLE_PAUSE_MINUTES``. The metrics dispatcher did not, and the two together
destroyed history: with the tick paused the demo's newest ``EventMetric`` bucket
stops moving, so ``collection_progress_to`` — the point the collector resumes
from — freezes with it and the next scheduled collection opens a window spanning
the whole demo cooldown plus the two-bucket resume overlap. The synthetic
warehouse behind every demo only materialises its newest
``SYNTHETIC_ONGOING_HOURS`` at full volume, and the collector deletes each chunk
window before rewriting it, so every run replaced the hours beyond that reach
with sampled near-zero counts.

These tests pin the gate itself (skip while paused, dispatch on access), that it
is demo-only, that it is measured before the cooldown's job-history read, and
that the tick and the dispatcher ask ONE predicate rather than two that can drift.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import pytest
from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

# Imported first and for its side effect, exactly as ``test_batch4_replay``
# documents: the worker task package is import-order sensitive and celery_app's
# bottom-of-file registration is what pulls the task modules in an order they all
# survive. This file reaches into ``demo_runtime`` AND ``metrics.schedule``, so it
# must enter the same way rather than rely on an alphabetically earlier test file
# having done it.
import tripl.worker.celery_app  # noqa: F401
from tripl.core.adapters.synthetic import SYNTHETIC_ONGOING_HOURS
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.worker.tasks import _demo_pause, demo_runtime
from tripl.worker.tasks.metrics import schedule as metrics_schedule
from tripl.worker.tasks.metrics import tasks as metrics_tasks

_IDLE = timedelta(minutes=_demo_pause.DEMO_IDLE_PAUSE_MINUTES)


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch5_demopause.db'}")
    # Production is Postgres, which always enforces foreign keys; without the
    # pragma a mis-ordered insert here would pass and hide a broken fixture.
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


class _Seeded(NamedTuple):
    project_id: uuid.UUID
    scan_config_id: uuid.UUID


def _seed_scan_config(
    session: Session,
    *,
    now: datetime,
    is_demo: bool,
    seeded_at: datetime | None = None,
    last_accessed: datetime | None = None,
) -> _Seeded:
    """A due, off-cooldown scan config — exactly what a paused demo looks like.

    The stored history stops ``DEMO_COLLECTION_COOLDOWN_HOURS + 2`` hours back,
    which is what happens once ``advance_demos`` stops appending: the config is due
    (its newest bucket's exclusive end is well below the current hour boundary) and
    nothing but the gate under test can change that. The one job on the config is a
    dispatcher collection older than the cooldown, so the cooldown is deliberately
    NOT what decides these tests' outcome.

    Every name is uuid-suffixed because ``projects.slug`` and ``data_sources.name``
    are globally unique and this helper is called more than once per engine.
    """
    tag = uuid.uuid4().hex[:8]
    project = Project(
        id=uuid.uuid4(),
        name=f"Batch5 demopause {tag}",
        slug=f"batch5-demopause-{tag}",
        description="",
        is_demo=is_demo,
        demo_seeded_at=seeded_at,
        demo_last_accessed_at=last_accessed,
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        # The demo's own source type. The dispatcher never reads it — it is the
        # ADAPTER behind this type whose ongoing-hours window makes an over-wide
        # collection window destructive, which is why the gate exists.
        name=f"Batch5 demopause DS {tag}",
        db_type="synthetic",
        host="synthetic",
        port=0,
        database_name="demo",
        username="",
        password_encrypted="",
    )
    # Parents first, flushed, so the children below cannot be sent ahead of the
    # rows their foreign keys point at.
    session.add_all([project, data_source])
    session.flush()

    event_type = EventType(
        id=uuid.uuid4(),
        project_id=project.id,
        name=f"structured-{tag}",
        display_name="Structured",
        description="",
    )
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name=f"Demo events {tag}",
        base_query="SELECT event_time, event_name FROM events",
        time_column="event_time",
        cardinality_threshold=100,
        interval="1h",
    )
    session.add_all([event_type, config])
    session.flush()

    stale_hours = metrics_schedule.DEMO_COLLECTION_COOLDOWN_HOURS + 2
    session.add_all(
        [
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                event_id=None,
                event_type_id=event_type.id,
                bucket=now.replace(minute=0, second=0, microsecond=0)
                - timedelta(hours=stale_hours),
                count=4200,
            ),
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                status=ScanJobStatus.completed.value,
                created_at=now - timedelta(hours=stale_hours),
                completed_at=now - timedelta(hours=stale_hours),
                # The positive ``mode`` stamp is what marks a job as this
                # dispatcher's own scheduled collection; no ``time_to``, so the
                # watermark contributes nothing and due-ness rests on the bucket.
                result_summary={"mode": metrics_tasks.METRICS_COLLECTION_MODE},
            ),
        ]
    )
    session.commit()
    return _Seeded(project_id=project.id, scan_config_id=config.id)


def _simulate_backfill_tick(session: Session, scan_config_id: uuid.UUID) -> None:
    """What ``advance_demos`` does on the first tick after a resume.

    It backfills the paused hours, so the newest stored bucket catches up to the
    present. Without it the dispatcher now declines a resumed demo whose window
    would still reach back to pause start; these tests are about
    the pause gate, so they let the backfill win the race. Two hours back keeps
    the config due (its progress end sits one interval below the boundary).
    """
    current_hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    event_type_id = session.execute(
        select(EventMetric.event_type_id).where(EventMetric.scan_config_id == scan_config_id)
    ).scalar_one()
    session.add(
        EventMetric(
            id=uuid.uuid4(),
            scan_config_id=scan_config_id,
            event_id=None,
            event_type_id=event_type_id,
            bucket=current_hour - timedelta(hours=2),
            count=4200,
        )
    )


def _run_dispatcher(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> tuple[dict[str, int], list[tuple[str, str]]]:
    """Run ``check_metrics_due`` against the test DB, capturing what it dispatched."""
    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(metrics_schedule, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(
        metrics_schedule.collect_metrics,
        "delay",
        lambda scan_config_id, scan_job_id: dispatched.append((scan_config_id, scan_job_id)),
    )
    return metrics_schedule.check_metrics_due.run(), dispatched


def _pending_job_count(session: Session, scan_config_id: uuid.UUID) -> int:
    return len(
        session.execute(
            select(ScanJob.id).where(
                ScanJob.scan_config_id == scan_config_id,
                ScanJob.status == ScanJobStatus.pending.value,
            )
        )
        .scalars()
        .all()
    )


# ── The gate ─────────────────────────────────────────────────────────────────


def test_check_metrics_due_skips_a_paused_demo_and_resumes_on_access(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The whole demo-pause behaviour in one run: silent while paused, live on access.

    Also asserts no ``ScanJob`` row is written while paused — a pending job the
    dispatcher never hands to a worker would be reaped as stale later and counted
    against the config's failure streak.
    """
    now = datetime.now(UTC)
    with sync_session_factory() as session:
        seeded = _seed_scan_config(
            session,
            now=now,
            is_demo=True,
            seeded_at=now - timedelta(days=3),
            last_accessed=now - _IDLE - timedelta(hours=1),
        )

    result, dispatched = _run_dispatcher(sync_session_factory, monkeypatch)

    assert result == {"checked": 1, "dispatched": 0}
    assert dispatched == []
    with sync_session_factory() as session:
        assert _pending_job_count(session, seeded.scan_config_id) == 0

    # Resume exactly as a viewer does: ``project_service.get_project`` touches
    # ``demo_last_accessed_at``. Nothing else about the config changes.
    with sync_session_factory() as session:
        project = session.get(Project, seeded.project_id)
        assert project is not None
        project.demo_last_accessed_at = datetime.now(UTC)
        _simulate_backfill_tick(session, seeded.scan_config_id)
        session.commit()

    result2, dispatched2 = _run_dispatcher(sync_session_factory, monkeypatch)

    assert result2 == {"checked": 1, "dispatched": 1}
    assert [config_id for config_id, _job_id in dispatched2] == [str(seeded.scan_config_id)]
    with sync_session_factory() as session:
        assert _pending_job_count(session, seeded.scan_config_id) == 1


def test_a_real_project_is_never_gated_by_demo_activity(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A non-demo project carries no demo stamps at all and must still collect.

    Guards the direction this fix could most easily go wrong in: both demo stamps
    are NULL on every real project, and ``is_demo_paused`` reads "no evidence of
    activity" as paused, so a gate that forgot to check ``is_demo`` would silence
    scheduled collection for the entire installation.
    """
    now = datetime.now(UTC)
    with sync_session_factory() as session:
        seeded = _seed_scan_config(session, now=now, is_demo=False)
        project = session.get(Project, seeded.project_id)
        assert project is not None
        assert project.demo_last_accessed_at is None and project.demo_seeded_at is None

    result, dispatched = _run_dispatcher(sync_session_factory, monkeypatch)

    assert result == {"checked": 1, "dispatched": 1}
    assert len(dispatched) == 1


def test_a_paused_demo_is_skipped_before_its_cooldown_history_is_read(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The pause check comes first, so a skipped demo pays no job-history read.

    Same ordering the failure backoff already uses — measure only once the cheap
    check has said "due". Fails both if the gate is removed (the cooldown read
    happens for a paused demo) and if it is moved after the cooldown.
    """
    measured: list[uuid.UUID] = []

    def _record(session: Session, scan_config_id: uuid.UUID, *, now: datetime) -> float | None:
        measured.append(scan_config_id)
        return None

    monkeypatch.setattr(metrics_schedule, "_hours_since_last_scheduled_collection", _record)

    now = datetime.now(UTC)
    with sync_session_factory() as session:
        seeded = _seed_scan_config(
            session,
            now=now,
            is_demo=True,
            seeded_at=now - timedelta(days=3),
            last_accessed=now - _IDLE - timedelta(hours=1),
        )

    _paused_result, paused_dispatched = _run_dispatcher(sync_session_factory, monkeypatch)
    assert paused_dispatched == []
    assert measured == [], "a paused demo must not pay for the cooldown's job read"

    with sync_session_factory() as session:
        project = session.get(Project, seeded.project_id)
        assert project is not None
        project.demo_last_accessed_at = datetime.now(UTC)
        _simulate_backfill_tick(session, seeded.scan_config_id)
        session.commit()

    _active_result, active_dispatched = _run_dispatcher(sync_session_factory, monkeypatch)
    assert len(active_dispatched) == 1
    assert measured == [seeded.scan_config_id], "an active demo is still cooldown-checked"


def test_a_paused_demo_never_lets_the_window_outrun_the_synthetic_ongoing_hours(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The arithmetic that makes the gate load-bearing, with the gate as its escape.

    While the tick is paused the collector's resume point is frozen, so the next
    scheduled collection's window starts ``DEMO_COLLECTION_COOLDOWN_HOURS`` plus
    ``SCHEDULED_RESUME_OVERLAP_BUCKETS`` behind the current hour (the demo's
    interval is ``1h``, so one overlap bucket is one hour and the three constants
    are directly comparable). The synthetic adapter only materialises its newest
    ``SYNTHETIC_ONGOING_HOURS`` at full volume, so anything beyond that reach is
    rewritten at sampled volume by a collection that deleted it first.

    Written as one disjunction on purpose: widening the adapter's ongoing window
    past the reach would make the gate unnecessary and is allowed to keep this
    green, but raising the cooldown, or shrinking the ongoing window, while the
    gate is gone turns it red.
    """
    now = datetime.now(UTC)
    with sync_session_factory() as session:
        _seed_scan_config(
            session,
            now=now,
            is_demo=True,
            seeded_at=now - timedelta(days=3),
            last_accessed=now - _IDLE - timedelta(hours=1),
        )

    _result, dispatched = _run_dispatcher(sync_session_factory, monkeypatch)

    reach_hours = (
        metrics_schedule.DEMO_COLLECTION_COOLDOWN_HOURS
        + metrics_tasks.SCHEDULED_RESUME_OVERLAP_BUCKETS
    )
    assert reach_hours <= SYNTHETIC_ONGOING_HOURS or dispatched == [], (
        f"a paused demo's collection window reaches {reach_hours}h back, past the "
        f"synthetic adapter's {SYNTHETIC_ONGOING_HOURS} full-volume hours, and the "
        "collector deletes a window before rewriting it — so the dispatcher must "
        "skip paused demos, or the adapter must materialise at least that many "
        "hours at full volume (which costs row budget: see SYNTHETIC_MAX_ROWS)."
    )


# ── One definition of "paused" ───────────────────────────────────────────────


def test_the_tick_and_the_dispatcher_share_one_pause_definition() -> None:
    """Both callers must resolve to the same function object, not a copy of it.

    Two idleness rules that drift apart re-open the gap the gate closes: the
    dispatcher would keep collecting for a demo the tick has already stopped
    advancing, which is exactly the state that destroys history.
    """
    assert demo_runtime.is_demo_paused is _demo_pause.is_demo_paused
    assert metrics_schedule.is_demo_paused is _demo_pause.is_demo_paused


_NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("seeded_at", "last_accessed", "expected", "why"),
    [
        (_NOW - timedelta(days=3), _NOW - timedelta(minutes=1), False, "just accessed"),
        (_NOW - timedelta(days=3), _NOW - _IDLE - timedelta(minutes=1), True, "idle"),
        # The comparison is strict, so activity landing exactly on the boundary is
        # still active. Preserved from the predicate's pre-extraction form.
        (_NOW - timedelta(days=3), _NOW - _IDLE, False, "exactly on the boundary"),
        # No access yet: a freshly seeded demo is active for one idle window.
        (_NOW - timedelta(minutes=1), None, False, "seeded, never opened"),
        (_NOW - _IDLE - timedelta(minutes=1), None, True, "seeded long ago, never opened"),
        # Access wins over seed time in both directions.
        (_NOW - timedelta(days=30), _NOW - timedelta(minutes=1), False, "old seed, fresh access"),
        # No evidence of activity at all — in production a demo still seeding.
        (None, None, True, "neither stamp"),
        # SQLite drops tzinfo on read where Postgres keeps it; both must compare.
        (None, (_NOW - timedelta(minutes=1)).replace(tzinfo=None), False, "naive access"),
    ],
)
def test_is_demo_paused_reads_activity_as_the_tick_always_has(
    seeded_at: datetime | None,
    last_accessed: datetime | None,
    expected: bool,
    why: str,
) -> None:
    assert _demo_pause.is_demo_paused(seeded_at, last_accessed, _NOW) is expected, why
