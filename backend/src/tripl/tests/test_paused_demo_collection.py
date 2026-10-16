"""A PAUSED demo must not be collected.

A demo nobody has opened for ``DEMO_IDLE_PAUSE_MINUTES`` is paused: the metrics
dispatcher skips its scan configs, and the source-freshness sweep does not chase
it. Nothing is lost by skipping — the shared demo warehouse keeps every hour of
its retention, so the first collection after an access catches the gap up from
real rows — and a demo's collection is the most expensive thing a deployment
runs per project.

These tests pin the gate itself (skip while paused, dispatch on access), that it
is demo-only, that it is measured before the cooldown's job-history read, and
that every caller asks ONE predicate rather than copies that can drift.
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

# Imported first and for its side effect, exactly as ``test_rule_replay_sources``
# documents: the worker task package is import-order sensitive and celery_app's
# bottom-of-file registration is what pulls the task modules in an order they all
# survive. This file reaches into ``metrics.schedule`` and ``metrics.freshness_sweep``,
# so it must enter the same way rather than rely on an alphabetically earlier test file
# having done it.
import tripl.worker.celery_app  # noqa: F401
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.worker.tasks import _demo_pause
from tripl.worker.tasks.metrics import freshness_sweep
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
    which is what happens once nobody looks at the demo: the config is due
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
        # The demo warehouse, as a recipe-5 demo reads it. (A demo still on an
        # older recipe's ``synthetic`` source is frozen and never dispatched at
        # all, paused or not.)
        name=f"Batch5 demopause DS {tag}",
        db_type="demo",
        host="demo-warehouse",
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
        session.commit()

    _active_result, active_dispatched = _run_dispatcher(sync_session_factory, monkeypatch)
    assert len(active_dispatched) == 1
    assert measured == [seeded.scan_config_id], "an active demo is still cooldown-checked"


# ── One definition of "paused" ───────────────────────────────────────────────


def test_the_dispatcher_and_the_freshness_sweep_share_one_pause_definition() -> None:
    """Both callers must resolve to the same function object, not a copy of it.

    Two idleness rules that drift apart would have the freshness sweep report a
    paused demo's source as late while the dispatcher, by design, leaves it be.
    """
    assert metrics_schedule.is_demo_paused is _demo_pause.is_demo_paused
    assert freshness_sweep.is_demo_paused is _demo_pause.is_demo_paused


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
def test_is_demo_paused_reads_the_demos_activity(
    seeded_at: datetime | None,
    last_accessed: datetime | None,
    expected: bool,
    why: str,
) -> None:
    assert _demo_pause.is_demo_paused(seeded_at, last_accessed, _NOW) is expected, why
