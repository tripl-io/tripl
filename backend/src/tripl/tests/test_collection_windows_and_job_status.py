"""Collection and replay windows: alignment, bounds, job-status guards, cooldown.

Five defects on the same seam — the window a collection run is given, and who is
allowed to close the job that ran it:

* ``_floor_to_interval`` anchored at 2000-01-01, a *Saturday*, so
  every ``1w`` window bound sat five days off the Monday grid the warehouse
  adapters, ``core.bucketing`` and the frontend all bin weeks on.
* a replay period reaching into the interval still filling was
  refused by the worker with a bare ``ValueError``, which the error sanitiser
  flattens to "Scan failed due to an internal error.", and only AFTER the API had
  created the job and dispatched it.
* ``collect_metrics`` skipped only ``cancelled`` jobs before
  start, so a job the stale reaper had already stamped ``failed`` was flipped
  back to ``running`` and re-run, and a run that finished after being reaped
  overwrote that ``failed`` with ``completed``.
* the demo collection cooldown identified "the last scheduled
  collection" by excluding the demo tick, so a manual scan or a replay on the
  same scan config deferred the real collection for up to six hours.
* the scheduled window started from ``max(EventMetric.bucket)``
  alone and ignored the job watermark, so a source that had gone silent widened
  its window by one interval per tick without bound.

Sync sqlite fixtures mirror ``test_metrics_tasks.py``; the API cases use the
shared async ``client`` fixture.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch
from httpx import AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.core.bucketing import floor_to_bucket
from tripl.core.intervals import INTERVALS, get_interval
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.worker.tasks import metrics
from tripl.worker.tasks._errors import ScanError, user_facing_error
from tripl.worker.tasks.metrics import schedule as metrics_schedule
from tripl.worker.tasks.metrics import tasks as metrics_tasks
from tripl.worker.tasks.metrics._helpers import _ceil_to_interval, _floor_to_interval
from tripl.worker.tasks.metrics.generation import _iter_window_chunks
from tripl.worker.utils import job_status

GENERIC_FAILURE = "Scan failed due to an internal error."


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch3_a3.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _create_scan_config(
    session: Session,
    *,
    interval: str = "1h",
    with_event_type: bool = False,
    is_demo: bool = False,
) -> ScanConfig:
    # An ACTIVE demo when is_demo: the dispatcher skips a PAUSED one outright,
    # and a demo with neither stamp set reads as paused, so the
    # cooldown's job-identity rule below would never be reached.
    now = datetime.now(UTC)
    project = Project(
        id=uuid.uuid4(),
        name="Batch3 A3",
        slug=f"batch3-a3-{uuid.uuid4().hex[:8]}",
        description="",
        is_demo=is_demo,
        demo_seeded_at=now - timedelta(days=3) if is_demo else None,
        demo_last_accessed_at=now if is_demo else None,
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"Batch3 DS {uuid.uuid4().hex[:8]}",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    session.add_all([project, data_source])

    event_type_id = None
    if with_event_type:
        event_type = EventType(
            id=uuid.uuid4(),
            project_id=project.id,
            name="structured",
            display_name="Structured",
            description="",
        )
        session.add(event_type)
        event_type_id = event_type.id

    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        event_type_id=event_type_id,
        name="Structured Events",
        base_query="SELECT time, event_name FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval=interval,
    )
    session.add(config)
    session.commit()
    return config


# ── the worker's window bounds sit on the bucket grid ──────────

# A whole week plus an instant already on the weekly grid: the Saturday anchor
# and the Monday one agree on exactly one day in seven, so a single sample date
# proves nothing.
_INSTANTS = [
    *(datetime(2026, 9, day, 13, 47, 31, tzinfo=UTC) for day in range(7, 14)),
    datetime(2026, 9, 7, tzinfo=UTC),
]


@pytest.mark.parametrize("code", sorted(INTERVALS))
def test_worker_window_bounds_agree_with_the_bucket_contract(code: str) -> None:
    """``_floor_to_interval`` must be ``floor_to_bucket`` on every interval code.

    These bounds ARE the window the warehouse's own ``GROUP BY`` is evaluated
    over. Off the bucket grid, a chunked replay splits a bucket across two chunks
    — and the upsert is last-chunk-wins, so a full weekly count is overwritten by
    the two days of the tail chunk — while a scheduled run writes a 5/7-complete
    week as if it were complete.
    """
    delta = get_interval(code).delta
    for moment in _INSTANTS:
        assert _floor_to_interval(moment, delta) == floor_to_bucket(moment, code)
        ceiled = _ceil_to_interval(moment, delta)
        # A ceiling is itself a boundary, and never earlier than the floor.
        assert floor_to_bucket(ceiled, code) == ceiled
        assert ceiled >= floor_to_bucket(moment, code)


def test_weekly_replay_chunks_never_split_a_monday_bucket() -> None:
    """``_iter_window_chunks`` promises no bucket spans two chunks; for 1w it lied."""
    delta = get_interval("1w").delta
    time_from = _floor_to_interval(datetime(2026, 8, 1, 9, tzinfo=UTC), delta)
    time_to = _ceil_to_interval(datetime(2026, 8, 29, 9, tzinfo=UTC), delta)
    assert time_from.weekday() == 0
    assert time_to.weekday() == 0

    chunks = _iter_window_chunks(time_from, time_to, interval_delta=delta, chunk_interval_code="1w")

    assert len(chunks) > 1
    for chunk_from, chunk_to in chunks:
        assert floor_to_bucket(chunk_from, "1w") == chunk_from
        assert floor_to_bucket(chunk_to, "1w") == chunk_to
    # No bucket is written by two chunks, so no upsert overwrites a full week.
    assert len({chunk_from for chunk_from, _ in chunks}) == len(chunks)


# ── a replay period that cannot run is refused, and says why ────


def test_replay_into_the_incomplete_interval_is_a_curated_scan_error(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The refusal must survive the error sanitiser and name the boundary.

    A bare ``ValueError`` is not in ``_CURATED_ERRORS``, so the run report showed
    "Scan failed due to an internal error." for a period the user chose — and one
    the dialog's own defaults used to seed on every daily or weekly scan.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session, interval="1d")
        boundary = floor_to_bucket(datetime.now(UTC), "1d")
        requested_to = boundary + timedelta(hours=1)

        with pytest.raises(ScanError) as excinfo:
            metrics_tasks._resolve_collection_window(
                session,
                config=config,
                delta=timedelta(days=1),
                manual_time_from=(requested_to - timedelta(days=1)).isoformat(),
                manual_time_to=requested_to.isoformat(),
            )

    message = user_facing_error(excinfo.value)
    assert message.startswith("Scan failed")
    assert message != GENERIC_FAILURE
    assert f"{boundary:%Y-%m-%d %H:%M}" in message


def test_replay_ending_on_the_last_complete_boundary_is_accepted(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Only the interval still filling is refused — the rule was not tightened."""
    with sync_session_factory() as session:
        config = _create_scan_config(session, interval="1d")
        boundary = floor_to_bucket(datetime.now(UTC), "1d")
        start = boundary - timedelta(days=1)

        resolved = metrics_tasks._resolve_collection_window(
            session,
            config=config,
            delta=timedelta(days=1),
            manual_time_from=start.isoformat(),
            manual_time_to=boundary.isoformat(),
        )

    assert resolved == (start, boundary, True)


async def _create_replayable_scan(client: AsyncClient, *, interval: str) -> tuple[str, str]:
    """A project plus a scan config the replay route will accept, by slug and id."""
    slug = f"replay-{uuid.uuid4().hex[:8]}"
    project = await client.post(
        "/api/v1/projects", json={"name": "Replay", "slug": slug, "description": ""}
    )
    assert project.status_code == 201, project.text
    ds = await client.post(
        "/api/v1/data-sources",
        json={
            "name": f"Warehouse {uuid.uuid4().hex[:6]}",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
        },
    )
    assert ds.status_code == 201, ds.text
    scan = await client.post(
        f"/api/v1/projects/{slug}/scans",
        json={
            "data_source_id": ds.json()["id"],
            "name": "Scheduled metrics",
            "base_query": "SELECT * FROM events",
            "time_column": "created_at",
            "interval": interval,
        },
    )
    assert scan.status_code == 201, scan.text
    return slug, scan.json()["id"]


async def test_replay_rejects_a_period_reaching_into_the_incomplete_interval(
    client: AsyncClient,
    monkeypatch: MonkeyPatch,
) -> None:
    """No job, no dispatch: the API answers what the worker would only fail on.

    The dialog's seeded period on a daily scan produced a 201 followed by a run
    that could never succeed, and a ScanJob row recording it.
    """
    slug, scan_id = await _create_replayable_scan(client, interval="1d")
    dispatched: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        metrics.collect_metrics,
        "delay",
        lambda *args: dispatched.append(args),
    )

    now = datetime.now(UTC)
    resp = await client.post(
        f"/api/v1/projects/{slug}/scans/{scan_id}/metrics/replay",
        json={
            "time_from": (now - timedelta(days=1)).isoformat(),
            "time_to": now.isoformat(),
        },
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "complete bucket" in detail
    assert f"{floor_to_bucket(now, '1d'):%Y-%m-%d %H:%M}" in detail
    assert dispatched == []
    jobs = await client.get(f"/api/v1/projects/{slug}/scans/{scan_id}/jobs")
    assert jobs.status_code == 200
    assert jobs.json() == []


async def test_replay_accepts_a_period_ending_on_the_last_complete_boundary(
    client: AsyncClient,
    monkeypatch: MonkeyPatch,
) -> None:
    """Guards the new check against being off by a whole interval."""
    slug, scan_id = await _create_replayable_scan(client, interval="1d")
    dispatched: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        metrics.collect_metrics,
        "delay",
        lambda *args: dispatched.append(args),
    )

    boundary = floor_to_bucket(datetime.now(UTC), "1d")
    resp = await client.post(
        f"/api/v1/projects/{slug}/scans/{scan_id}/metrics/replay",
        json={
            "time_from": (boundary - timedelta(days=1)).isoformat(),
            "time_to": boundary.isoformat(),
        },
    )

    assert resp.status_code == 201, resp.text
    assert len(dispatched) == 1
    assert dispatched[0][3] == boundary.isoformat()


# ── a terminal job is never re-run, nor re-opened ──────────────


def _no_adapter(*args: object, **kwargs: object) -> object:
    raise AssertionError("a job in a terminal state must not be collected")


@pytest.mark.parametrize(
    ("status", "error_message"),
    [
        (
            ScanJobStatus.failed.value,
            "Marked failed by scheduler after 75 minutes without progress",
        ),
        (ScanJobStatus.completed.value, None),
    ],
)
def test_collect_metrics_skips_a_job_that_is_already_finished(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
    status: str,
    error_message: str | None,
) -> None:
    """A reaped job, or one whose ``completed`` ack was lost, must not re-run.

    Both are reachable on the shipped single-worker topology: the dispatcher
    reaps an active job after 75 minutes and dispatches a replacement, and
    ``task_acks_late`` redelivers a message whose ack never landed. Re-running
    redid the whole window and — the expensive half — flipped the row back to
    ``running`` then ``completed``, erasing the failure the dispatcher's backoff
    streak keys on.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        job = ScanJob(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            status=status,
            completed_at=datetime.now(UTC),
            error_message=error_message,
            result_summary={"mode": metrics_tasks.METRICS_COLLECTION_MODE},
        )
        session.add(job)
        session.commit()
        config_id = str(config.id)
        job_id = str(job.id)

    monkeypatch.setattr(metrics_tasks, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics_tasks, "_build_adapter", _no_adapter)

    result = metrics_tasks.collect_metrics.run(config_id, job_id)

    assert result == {"skipped": True, "job_status": status, "scan_config_id": config_id}
    with sync_session_factory() as session:
        reloaded = session.get(ScanJob, uuid.UUID(job_id))
        assert reloaded is not None
        assert reloaded.status == status
        assert reloaded.started_at is None
        assert reloaded.error_message == error_message


def test_running_is_not_a_terminal_job_status() -> None:
    """``running`` stays out: an ``acks_late`` redelivery re-enters its own running job.

    ``worker.utils.job_status`` holds the one tuple the scan tasks and
    ``collect_metrics`` both enforce.
    """
    assert ScanJobStatus.running.value not in job_status.TERMINAL_SCAN_JOB_STATUSES


def test_collect_metrics_does_not_unfail_a_job_reaped_mid_run(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A run that finishes after being reaped leaves the reaper's verdict alone.

    The task time limit is 25 h and the reaper's window is 75 min, so a long run
    is reaped while it is still alive. It used to blind-write ``completed`` over
    that ``failed`` at the end, which zeroed the dispatcher's failure streak and
    reported a success the replacement run, not this one, owned.
    """
    reaped_message = "Marked failed by scheduler after 75 minutes without progress"
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None
        config.replay_chunk_interval = "1h"
        job = ScanJob(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            status=ScanJobStatus.pending.value,
        )
        session.add(job)
        session.add(
            FieldDefinition(
                id=uuid.uuid4(),
                event_type_id=config.event_type_id,
                name="event_name",
                display_name="Event name",
                field_type="string",
                is_required=False,
                description="",
            )
        )
        login_event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login",
            description="",
            status="implemented",
        )
        session.add(login_event)
        session.commit()
        config_id = str(config.id)
        job_id = str(job.id)

    time_from = datetime(2026, 1, 1, 8)
    time_to = datetime(2026, 1, 1, 11)
    counts_by_bucket = {
        datetime(2026, 1, 1, 8): 8,
        datetime(2026, 1, 1, 9): 9,
        datetime(2026, 1, 1, 10): 10,
    }

    class FakeAdapter:
        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
            ]

        def get_time_bucketed_counts(
            self,
            base_query: str,
            time_column: str,
            interval: str,
            regular_columns: list[str],
            json_columns: list[str],
            json_value_paths: dict[str, list[str]] | None,
            time_from: datetime,
            time_to: datetime,
            limit: int = 100000,
        ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
            rows: list[tuple[object, ...]] = [
                (bucket, "Login", count)
                for bucket, count in counts_by_bucket.items()
                if time_from <= bucket < time_to
            ]
            return (["event_name"], [], rows)

        def close(self) -> None:
            return None

    real_process_chunk = metrics_tasks.process_chunk
    reaped: list[bool] = []

    def process_chunk_then_reap(*args: object, **kwargs: object) -> object:
        stats = real_process_chunk(*args, **kwargs)  # type: ignore[arg-type]
        if not reaped:
            # The beat process reaping a job it believes is stuck, in its own
            # session — exactly what ``_fail_stale_active_scan_job`` writes.
            with sync_session_factory() as reaper_session:
                row = reaper_session.get(ScanJob, uuid.UUID(job_id))
                assert row is not None
                row.status = ScanJobStatus.failed.value
                row.completed_at = datetime.now(UTC)
                row.error_message = reaped_message
                reaper_session.commit()
            reaped.append(True)
        return stats

    monkeypatch.setattr(metrics_tasks, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics_tasks, "_build_adapter", lambda ds: FakeAdapter())
    monkeypatch.setattr(metrics_tasks, "process_chunk", process_chunk_then_reap)
    monkeypatch.setattr(
        metrics_tasks,
        "_resolve_collection_window",
        lambda *args, **kwargs: (time_from, time_to, True),
    )

    result = metrics_tasks.collect_metrics.run(config_id, job_id)

    # The run still did its work and still recorded what it collected.
    assert reaped == [True]
    assert result["event_metrics"] > 0
    with sync_session_factory() as session:
        reloaded = session.get(ScanJob, uuid.UUID(job_id))
        assert reloaded is not None
        assert reloaded.status == ScanJobStatus.failed.value
        assert reloaded.error_message == reaped_message
        assert isinstance(reloaded.result_summary, dict)
        assert reloaded.result_summary["event_metrics"] == result["event_metrics"]


# ── the scheduled window is bounded by collection progress ─────


def _seed_completed_collection(
    session: Session,
    scan_config_id: uuid.UUID,
    *,
    window_to: datetime,
    mode: str = metrics_tasks.METRICS_COLLECTION_MODE,
) -> None:
    """The ScanJob a FINISHED collection leaves behind, and no EventMetric row."""
    stamped = datetime.now(UTC) - timedelta(minutes=1)
    session.add(
        ScanJob(
            id=uuid.uuid4(),
            scan_config_id=scan_config_id,
            status=ScanJobStatus.completed.value,
            created_at=stamped,
            completed_at=stamped,
            result_summary={
                "mode": mode,
                "time_from": (window_to - timedelta(hours=30)).isoformat(),
                "time_to": window_to.isoformat(),
                "event_metrics": 0,
            },
        )
    )
    session.commit()


def _scheduled_window(
    session: Session,
    config: ScanConfig,
    *,
    delta: timedelta = timedelta(hours=1),
) -> tuple[datetime, datetime, bool]:
    return metrics_tasks._resolve_collection_window(
        session,
        config=config,
        delta=delta,
        manual_time_from=None,
        manual_time_to=None,
    )


def _add_bucket(session: Session, config: ScanConfig, bucket: datetime) -> None:
    session.add(
        EventMetric(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            event_id=uuid.uuid4(),
            event_type_id=None,
            bucket=bucket,
            count=1,
        )
    )
    session.commit()


def test_silent_stream_resume_window_is_bounded_by_the_collection_watermark(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A source that stopped delivering must not widen its window every tick.

    Due-ness reads the job watermark, so the config stays scheduled; the window
    read ``max(EventMetric.bucket)`` alone, so it grew by one interval per tick —
    one unbounded warehouse query and one unbounded delete, forever.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        boundary = _floor_to_interval(datetime.now(UTC), timedelta(hours=1))
        _add_bucket(session, config, boundary - timedelta(days=30))
        _seed_completed_collection(session, config.id, window_to=boundary)

        time_from, time_to, is_replay = _scheduled_window(session, config)

    assert is_replay is False
    assert time_to == boundary
    assert time_from == boundary - timedelta(hours=2)


def test_a_collection_outage_still_resumes_from_the_last_stored_bucket(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The watermark cannot advance without a completed job, so catch-up survives.

    Guards against the fix over-narrowing into a fixed two-bucket window: a run
    of failures must still be made up on the next success.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        boundary = _floor_to_interval(datetime.now(UTC), timedelta(hours=1))
        _add_bucket(session, config, boundary - timedelta(hours=5))

        time_from, _time_to, _is_replay = _scheduled_window(session, config)

    assert time_from == boundary - timedelta(hours=6)


def test_a_replay_window_is_not_read_as_the_live_resume_point(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A historical replay must not push the live grid's resume point forward."""
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        boundary = _floor_to_interval(datetime.now(UTC), timedelta(hours=1))
        _add_bucket(session, config, boundary - timedelta(hours=5))
        _seed_completed_collection(
            session,
            config.id,
            window_to=boundary,
            mode=metrics_tasks.METRICS_REPLAY_MODE,
        )

        time_from, _time_to, _is_replay = _scheduled_window(session, config)

    assert time_from == boundary - timedelta(hours=6)


def test_a_never_collected_config_still_backfills_thirty_buckets(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The first-run fallback is unchanged: the detector needs a baseline."""
    with sync_session_factory() as session:
        config = _create_scan_config(session)

        time_from, time_to, _is_replay = _scheduled_window(session, config)

    assert time_to - time_from == timedelta(hours=metrics_tasks.SCHEDULED_BACKFILL_BUCKETS)


# ── .10 meets .26: the resume point is progress, so it has to be floored ─────
#
# Both inputs to ``collection_progress_to`` can sit OFF the config's current
# grid, and neither is rewritten when the grid moves under it:
#
# * a watermark recorded before .10 moved the weekly origin to Monday is a
#   Saturday (the old 2000-01-01 anchor), and
# * ``last_bucket + delta`` is any weekday once a config is edited from ``1d``
#   to ``1w``, because the edit purges no ``EventMetric`` row.
#
# An off-grid ``time_from`` makes the warehouse's own ``GROUP BY`` key the
# leading partial bucket BELOW the window: the window's delete
# (``bucket >= time_from``) misses it and the upsert then overwrites a complete
# bucket's count with the two-day tail of it. The bound must satisfy the
# invariant ``_floor_to_interval`` states in its own docstring.


_WEEK = timedelta(weeks=1)


def test_a_saturday_watermark_still_opens_the_window_on_a_monday(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A pre-.10 weekly watermark must not re-phase the window it resumes."""
    with sync_session_factory() as session:
        config = _create_scan_config(session, interval="1w")
        monday = _floor_to_interval(datetime.now(UTC), _WEEK)
        # The Saturday a pre-deploy run recorded as the grid it collected up to.
        saturday = monday - timedelta(days=9)
        assert saturday.weekday() == 5
        # Older than the watermark, so the watermark wins ``collection_progress_to``
        # — the lagging case, which is the only one that can be off grid.
        _add_bucket(session, config, monday - 3 * _WEEK)
        _seed_completed_collection(session, config.id, window_to=saturday)

        time_from, time_to, _is_replay = _scheduled_window(session, config, delta=_WEEK)

    assert time_to == monday
    # Floored onto the Monday grid first, THEN backed off two buckets.
    assert time_from == monday - 4 * _WEEK
    assert time_from.weekday() == 0
    assert _floor_to_interval(time_from, _WEEK) == time_from
    # The bound and the warehouse agree, which is the whole point of the floor:
    # nothing the ``GROUP BY`` returns can be keyed below the window.
    assert floor_to_bucket(time_from, "1w") == time_from
    # Flooring may only widen the window, never skip grid the old start covered.
    assert time_from <= saturday - 2 * _WEEK


def test_stale_daily_buckets_do_not_open_a_weekly_window_mid_week(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The same floor covers an interval edited from ``1d`` to ``1w``.

    ``update_scan_config`` purges no ``EventMetric`` row, so ``last_bucket`` is
    still a daily boundary and ``last_bucket + 1w`` lands on a Wednesday.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session, interval="1w")
        monday = _floor_to_interval(datetime.now(UTC), _WEEK)
        wednesday = monday - timedelta(days=12)
        assert wednesday.weekday() == 2
        _add_bucket(session, config, wednesday)

        time_from, time_to, _is_replay = _scheduled_window(session, config, delta=_WEEK)

    assert time_to == monday
    # ``last_bucket + 1w`` is ``monday - 5d`` (a Wednesday); its Monday is
    # ``monday - 1w``, and two buckets back from that is ``monday - 3w``.
    assert time_from == monday - 3 * _WEEK
    assert _floor_to_interval(time_from, _WEEK) == time_from
    assert floor_to_bucket(time_from, "1w") == time_from


# ── the demo cooldown counts only the dispatcher's own jobs ────


def _demo_config_with_job(
    session: Session,
    *,
    result_summary: object,
    age: timedelta,
    status: str = ScanJobStatus.completed.value,
) -> uuid.UUID:
    """A due demo scan config carrying one job of the given shape."""
    config = _create_scan_config(session, is_demo=True)
    stamped = datetime.now(UTC) - age
    session.add(
        ScanJob(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            status=status,
            created_at=stamped,
            completed_at=stamped,
            result_summary=result_summary,
        )
    )
    session.commit()
    return config.id


def _run_dispatcher(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> list[tuple[str, str]]:
    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(metrics_schedule, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(
        metrics_schedule.collect_metrics,
        "delay",
        lambda scan_config_id, scan_job_id: dispatched.append((scan_config_id, scan_job_id)),
    )
    metrics_schedule.check_metrics_due.run()
    return dispatched


@pytest.mark.parametrize(
    "result_summary",
    [
        # A manual catalog scan (worker/tasks/scan.py) — no ``mode`` key at all.
        {"events_created": 0, "variables_retired": 0, "columns_analyzed": 3},
        # A metrics replay: its own historical window, not a scheduled collection.
        {"mode": "metrics_replay", "time_from": "2026-01-01T00:00:00+00:00"},
        # An event-group apply.
        {"mode": "event_groups_apply", "events_merged": 0},
        # Any of the above while still pending: scan_service leaves it NULL.
        None,
    ],
)
def test_demo_cooldown_ignores_jobs_the_dispatcher_did_not_create(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
    result_summary: object,
) -> None:
    """Anyone pressing Run scan or Replay on a demo used to mute its collection.

    The cooldown identified the last scheduled collection by EXCLUDING the demo
    runtime tick, so every foreign job on the same ``scan_config_id`` read as one
    and deferred the only producer of breakdown anomalies and distribution drift
    for up to six hours.
    """
    with sync_session_factory() as session:
        _demo_config_with_job(session, result_summary=result_summary, age=timedelta(minutes=5))

    assert len(_run_dispatcher(sync_session_factory, monkeypatch)) == 1


def test_demo_cooldown_skips_past_a_manual_scan_to_the_real_collection(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A foreign job is skipped, not treated as the end of the search.

    Pins the ``continue``: returning ``None`` on the first foreign row would let a
    demo collect again an hour after its last collection.
    """
    with sync_session_factory() as session:
        config_id = _demo_config_with_job(
            session,
            result_summary={"mode": metrics_tasks.METRICS_COLLECTION_MODE},
            age=timedelta(hours=1),
        )
        stamped = datetime.now(UTC) - timedelta(minutes=5)
        session.add(
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=config_id,
                status=ScanJobStatus.completed.value,
                created_at=stamped,
                completed_at=stamped,
                result_summary={"events_created": 0},
            )
        )
        session.commit()

    assert _run_dispatcher(sync_session_factory, monkeypatch) == []


def test_demo_cooldown_counts_a_failed_dispatcher_job(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """Status is deliberately not filtered: a failed run still consumed the slot.

    A completed-only filter would let a demo whose collection fails re-dispatch on
    every 300 s beat tick until the failure backoff engages. One failure is below
    ``FAILURE_BACKOFF_AFTER``, so the backoff is not what holds this one back.
    """
    with sync_session_factory() as session:
        _demo_config_with_job(
            session,
            result_summary={"mode": metrics_tasks.METRICS_COLLECTION_MODE},
            age=timedelta(hours=1),
            status=ScanJobStatus.failed.value,
        )

    assert _run_dispatcher(sync_session_factory, monkeypatch) == []


def test_the_dispatcher_reads_the_collection_identity_from_one_place() -> None:
    """The cooldown, the failure streak and the watermark share one predicate.

    Three readers of the same job history used to disagree — two identified a
    dispatcher job by its ``mode`` stamp and the cooldown identified it by
    exclusion. Cheap structural guard against that splitting again.
    """
    assert (
        metrics_schedule._is_dispatcher_collection_job
        is metrics_tasks._is_dispatcher_collection_job
    )
    assert not metrics_tasks._is_dispatcher_collection_job({"events_created": 0})
    assert not metrics_tasks._is_dispatcher_collection_job(None)
    assert metrics_tasks._is_dispatcher_collection_job(
        {"mode": metrics_tasks.METRICS_COLLECTION_MODE}
    )
