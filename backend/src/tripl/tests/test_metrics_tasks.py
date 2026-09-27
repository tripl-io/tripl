import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import ColumnInfo, FieldContractViolation
from tripl.core.analyzers._event_generator_variables import (
    SCAN_PROVENANCE_DESCRIPTION,
    VariableIndex,
)
from tripl.core.analyzers.event_generator import GenerationResult
from tripl.core.bucketing import to_utc
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.data_source import DataSource
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.event import Event, EventStatus
from tripl.models.event_change import EventChange
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_breakdown_anomaly import MetricBreakdownAnomaly
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.release_regression import ReleaseComparability, ReleaseRegression
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.models.schema_drift import SchemaDrift
from tripl.models.variable import Variable
from tripl.models.variable_value import VariableValue, VariableValueKind
from tripl.worker import variable_sweep
from tripl.worker.tasks._errors import ScanError, user_facing_error
from tripl.worker.tasks.metrics import catalog_sync as metrics_catalog_sync
from tripl.worker.tasks.metrics import collect as metrics_collect
from tripl.worker.tasks.metrics import dispatch as metrics_dispatch
from tripl.worker.tasks.metrics import generation as metrics_generation
from tripl.worker.tasks.metrics import schedule as metrics_schedule
from tripl.worker.tasks.metrics import schema_drift as metrics_schema_drift
from tripl.worker.tasks.metrics import signals as metrics_signals
from tripl.worker.tasks.metrics import tasks as metrics
from tripl.worker.tasks.metrics._helpers import STALE_ACTIVE_SCAN_JOB_TIMEOUT


@pytest.fixture(autouse=True)
def _source_never_late(monkeypatch: MonkeyPatch) -> None:
    """Keep the source-freshness hold (#269) out of these tests.

    They pin the collection window with ``_floor_to_interval`` but freshness is
    judged against the wall clock, and ``_ANOMALY_BASE`` is fixed at import
    time. A suite that crosses an hour boundary between import and the test
    reads the source as late and holds the very drops these tests assert on.
    The hold has its own tests in ``test_source_freshness.py``.
    """
    monkeypatch.setattr(metrics, "is_holding", lambda freshness: False)


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'metrics_tasks.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _create_scan_config(session: Session, *, with_event_type: bool = False) -> ScanConfig:
    project = Project(
        id=uuid.uuid4(),
        name="Metrics Project",
        slug=f"metrics-{uuid.uuid4().hex[:8]}",
        description="",
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"Metrics DS {uuid.uuid4().hex[:8]}",
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
        interval="1h",
    )
    session.add(config)
    session.commit()
    return config


# Fresh, hour-aligned base for the anomaly-history fixture so the detected drop's
# bucket stays inside the wall-clock signal freshness horizon. Callers that assert
# an OPEN signal / queued alert pass base=_ANOMALY_BASE; the other callers keep the
# historical 2026-01-01 anchor (behaviourally identical for them — they do not
# assert on signal freshness). Persisted buckets are UTC aware.
_ANOMALY_BASE = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=11)


def _seed_anomaly_scan_state(
    session: Session, *, base: datetime = datetime(2026, 1, 1, 0, 0)
) -> tuple[ScanConfig, EventType, Event]:
    config = _create_scan_config(session, with_event_type=True)
    assert config.event_type_id is not None

    session.add(
        ProjectAnomalySettings(
            project_id=config.project_id,
            anomaly_detection_enabled=True,
            # Pinned so the small crafted series stay eligible; these tests
            # exercise recompute/dispatch mechanics, not the product defaults.
            sigma_threshold=3.0,
            min_expected_count=10,
        )
    )
    event_type = session.get(EventType, config.event_type_id)
    assert event_type is not None
    event = Event(
        id=uuid.uuid4(),
        project_id=config.project_id,
        event_type_id=event_type.id,
        name="event_name=Login",
        description="",
        status="implemented",
    )
    session.add(event)

    for hour in range(10):
        bucket = base + timedelta(hours=hour)
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                event_id=event.id,
                event_type_id=None,
                bucket=bucket,
                count=10,
            )
        )
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                event_id=None,
                event_type_id=event_type.id,
                bucket=bucket,
                count=10,
            )
        )

    session.commit()
    return config, event_type, event


def test_check_metrics_due_skips_dispatch_when_active_job_exists(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        job = ScanJob(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            status=ScanJobStatus.pending.value,
        )
        session.add(job)
        session.commit()
        config_id = config.id
        job_id = job.id

    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(metrics_schedule, "_get_sync_session", sync_session_factory)

    def fake_delay(scan_config_id: str, scan_job_id: str) -> None:
        dispatched.append((scan_config_id, scan_job_id))

    monkeypatch.setattr(
        metrics_schedule.collect_metrics,
        "delay",
        fake_delay,
    )

    result = metrics_schedule.check_metrics_due.run()

    assert result == {"checked": 1, "dispatched": 0}
    assert dispatched == []

    with sync_session_factory() as session:
        jobs = (
            session.execute(select(ScanJob).where(ScanJob.scan_config_id == config_id))
            .scalars()
            .all()
        )
    assert len(jobs) == 1
    assert jobs[0].id == job_id
    assert jobs[0].status == ScanJobStatus.pending.value


def _prepare_demo_dispatch(
    session: Session, *, recent_scheduled_job: bool, tick_job: bool
) -> uuid.UUID:
    """A demo scan config that is due, with an optional recent job history."""
    config = _create_scan_config(session)
    project = session.get(Project, config.project_id)
    assert project is not None
    project.is_demo = True
    now = datetime.now(UTC)
    # An ACTIVE demo. The dispatcher skips a PAUSED one outright (tripl-0zpq.72),
    # and these tests are about the cooldown, so the pause gate must not be what
    # holds them back. A demo with both stamps NULL reads as paused, which would
    # leave `test_demo_collection_is_deferred_by_the_cooldown` passing for the
    # wrong reason.
    project.demo_seeded_at = now - timedelta(days=3)
    project.demo_last_accessed_at = now
    if recent_scheduled_job:
        session.add(
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                status=ScanJobStatus.completed.value,
                created_at=now - timedelta(hours=1),
                completed_at=now - timedelta(hours=1),
                # What the dispatcher writes: the positive ``mode`` stamp is what
                # marks a job as a scheduled collection (tripl-0zpq.24).
                result_summary={"mode": metrics.METRICS_COLLECTION_MODE},
            )
        )
    if tick_job:
        # What advance_demos writes every hour.
        session.add(
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                status=ScanJobStatus.completed.value,
                created_at=now - timedelta(minutes=5),
                completed_at=now - timedelta(minutes=5),
                result_summary={"demo_runtime_tick": True, "buckets_appended": 1},
            )
        )
    session.commit()
    return config.id


def test_demo_collection_is_deferred_by_the_cooldown(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A demo that collected an hour ago must not collect again (tripl-jfm3.73)."""
    with sync_session_factory() as session:
        _prepare_demo_dispatch(session, recent_scheduled_job=True, tick_job=False)

    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(metrics_schedule, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(
        metrics_schedule.collect_metrics,
        "delay",
        lambda scan_config_id, scan_job_id: dispatched.append((scan_config_id, scan_job_id)),
    )

    result = metrics_schedule.check_metrics_due.run()

    assert result["dispatched"] == 0
    assert dispatched == []


def test_demo_cooldown_ignores_the_hourly_runtime_tick_jobs(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The demo tick writes a ScanJob every hour; it must not hold off collection.

    Without this, the newest job would always be a tick job minutes old and the
    scheduled collection — the only producer of breakdown anomalies and
    distribution drift — would be deferred forever.
    """
    with sync_session_factory() as session:
        _prepare_demo_dispatch(session, recent_scheduled_job=False, tick_job=True)

    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(metrics_schedule, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(
        metrics_schedule.collect_metrics,
        "delay",
        lambda scan_config_id, scan_job_id: dispatched.append((scan_config_id, scan_job_id)),
    )

    result = metrics_schedule.check_metrics_due.run()

    assert result["dispatched"] == 1
    assert len(dispatched) == 1


def test_non_demo_projects_are_not_subject_to_the_cooldown(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The cooldown is demo-only — a real project keeps its configured cadence."""
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        now = datetime.now(UTC)
        session.add(
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                status=ScanJobStatus.completed.value,
                created_at=now - timedelta(minutes=10),
                completed_at=now - timedelta(minutes=10),
                result_summary={"events_created": 0},
            )
        )
        session.commit()

    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(metrics_schedule, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(
        metrics_schedule.collect_metrics,
        "delay",
        lambda scan_config_id, scan_job_id: dispatched.append((scan_config_id, scan_job_id)),
    )

    result = metrics_schedule.check_metrics_due.run()

    assert result["dispatched"] == 1


def test_check_metrics_due_creates_pending_job_before_dispatch(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        config_id = config.id

    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(metrics_schedule, "_get_sync_session", sync_session_factory)

    def fake_delay(scan_config_id: str, scan_job_id: str) -> None:
        dispatched.append((scan_config_id, scan_job_id))

    monkeypatch.setattr(
        metrics_schedule.collect_metrics,
        "delay",
        fake_delay,
    )

    result = metrics_schedule.check_metrics_due.run()

    assert result == {"checked": 1, "dispatched": 1}
    assert dispatched[0][0] == str(config_id)

    with sync_session_factory() as session:
        jobs = (
            session.execute(select(ScanJob).where(ScanJob.scan_config_id == config_id))
            .scalars()
            .all()
        )

    assert len(jobs) == 1
    assert str(jobs[0].id) == dispatched[0][1]
    assert jobs[0].status == ScanJobStatus.pending.value
    assert jobs[0].started_at is None


def test_check_metrics_due_reaps_stale_active_job_and_dispatches_replacement(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    stale_at = datetime.now(UTC) - STALE_ACTIVE_SCAN_JOB_TIMEOUT - timedelta(minutes=5)
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        stale_job = ScanJob(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            status=ScanJobStatus.running.value,
            created_at=stale_at,
            started_at=stale_at,
            updated_at=stale_at,
        )
        session.add(stale_job)
        session.commit()
        config_id = config.id
        stale_job_id = stale_job.id

    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(metrics_schedule, "_get_sync_session", sync_session_factory)

    def fake_delay(scan_config_id: str, scan_job_id: str) -> None:
        dispatched.append((scan_config_id, scan_job_id))

    monkeypatch.setattr(metrics_schedule.collect_metrics, "delay", fake_delay)

    result = metrics_schedule.check_metrics_due.run()

    assert result == {"checked": 1, "dispatched": 1}
    assert dispatched[0][0] == str(config_id)

    with sync_session_factory() as session:
        jobs = (
            session.execute(
                select(ScanJob)
                .where(ScanJob.scan_config_id == config_id)
                .order_by(ScanJob.created_at.asc())
            )
            .scalars()
            .all()
        )

    assert len(jobs) == 2
    assert jobs[0].id == stale_job_id
    assert jobs[0].status == ScanJobStatus.failed.value
    assert jobs[0].completed_at is not None
    assert jobs[0].error_message is not None
    assert "Marked failed by scheduler" in jobs[0].error_message
    assert str(jobs[1].id) == dispatched[0][1]
    assert jobs[1].status == ScanJobStatus.pending.value


def test_check_metrics_due_uses_updated_at_as_running_job_activity(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        running_job = ScanJob(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            status=ScanJobStatus.running.value,
            created_at=now - STALE_ACTIVE_SCAN_JOB_TIMEOUT - timedelta(minutes=10),
            started_at=now - STALE_ACTIVE_SCAN_JOB_TIMEOUT - timedelta(minutes=10),
            updated_at=now - timedelta(minutes=1),
        )
        session.add(running_job)
        session.commit()
        job_id = running_job.id

    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(metrics_schedule, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(
        metrics_schedule.collect_metrics,
        "delay",
        lambda scan_config_id, scan_job_id: dispatched.append((scan_config_id, scan_job_id)),
    )

    result = metrics_schedule.check_metrics_due.run()

    assert result == {"checked": 1, "dispatched": 0}
    assert dispatched == []

    with sync_session_factory() as session:
        reloaded = session.get(ScanJob, job_id)

    assert reloaded is not None
    assert reloaded.status == ScanJobStatus.running.value
    assert reloaded.completed_at is None
    assert reloaded.error_message is None


def test_check_metrics_due_reaps_shadowed_stale_running_job(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A stale RUNNING job is reaped even when a fresher, non-stale PENDING job
    exists for the same config.

    Regression: the dispatcher used to inspect only the *newest* active job, so
    a stuck old running job could be permanently shadowed by a fresher pending
    row and never reaped — it stayed `running` forever.
    """
    now = datetime.now(UTC)
    stale_at = now - STALE_ACTIVE_SCAN_JOB_TIMEOUT - timedelta(minutes=10)
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        stale_running = ScanJob(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            status=ScanJobStatus.running.value,
            created_at=stale_at,
            started_at=stale_at,
            updated_at=stale_at,
        )
        fresh_pending = ScanJob(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            status=ScanJobStatus.pending.value,
        )
        session.add_all([stale_running, fresh_pending])
        session.commit()
        config_id = config.id
        stale_id = stale_running.id
        fresh_id = fresh_pending.id

    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(metrics_schedule, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(
        metrics_schedule.collect_metrics,
        "delay",
        lambda scan_config_id, scan_job_id: dispatched.append((scan_config_id, scan_job_id)),
    )

    result = metrics_schedule.check_metrics_due.run()

    # The fresh pending job is still live, so no replacement is dispatched...
    assert result == {"checked": 1, "dispatched": 0}
    assert dispatched == []

    with sync_session_factory() as session:
        jobs = {
            job.id: job
            for job in session.execute(
                select(ScanJob).where(ScanJob.scan_config_id == config_id)
            ).scalars()
        }

    # ...but the shadowed stale running job is reaped instead of left stuck.
    assert jobs[stale_id].status == ScanJobStatus.failed.value
    assert jobs[stale_id].completed_at is not None
    assert jobs[fresh_id].status == ScanJobStatus.pending.value


def _seed_job_history(
    session: Session,
    scan_config_id: uuid.UUID,
    *,
    statuses: list[str],
    newest_age: timedelta,
    mode: str | None = metrics.METRICS_COLLECTION_MODE,
) -> None:
    """Write one terminal ScanJob per status, newest first, spaced an hour apart.

    ``mode`` defaults to what the dispatcher stamps on its own jobs, because the
    failure streak only counts those. Pass ``None`` to simulate the rows another
    producer writes against the same scan_config_id — a manual scan, a replay, an
    event-group apply — which must neither feed the streak nor clear it.
    """
    now = datetime.now(UTC)
    for offset, status in enumerate(statuses):
        stamped = now - newest_age - timedelta(hours=offset)
        session.add(
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=scan_config_id,
                status=status,
                created_at=stamped,
                completed_at=stamped,
                result_summary=None if mode is None else {"mode": mode},
            )
        )
    session.commit()


def _run_dispatcher(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> tuple[dict[str, int], list[tuple[str, str]]]:
    """Run check_metrics_due against the test DB, capturing what it dispatched."""
    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(metrics_schedule, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(
        metrics_schedule.collect_metrics,
        "delay",
        lambda scan_config_id, scan_job_id: dispatched.append((scan_config_id, scan_job_id)),
    )
    return metrics_schedule.check_metrics_due.run(), dispatched


def test_check_metrics_due_backs_off_after_consecutive_failures(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A config that keeps failing waits instead of retrying on every beat tick.

    "Due" is derived from max(EventMetric.bucket), which a collection that dies
    before writing a row never advances — so before tripl-n9ee this config was
    re-dispatched every 300 s forever (prod: 200 failed jobs in 17 h, each a ~30 s
    warehouse query).
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        config_id = config.id
        _seed_job_history(
            session,
            config_id,
            statuses=[ScanJobStatus.failed.value] * metrics_schedule.FAILURE_BACKOFF_AFTER,
            # Well inside the one-interval (1h) wait the third failure earns.
            newest_age=timedelta(minutes=10),
        )

    result, dispatched = _run_dispatcher(sync_session_factory, monkeypatch)

    assert result == {"checked": 1, "dispatched": 0}
    assert dispatched == []

    # No junk pending row either — the whole point is to stop filling scan_jobs.
    with sync_session_factory() as session:
        jobs = (
            session.execute(select(ScanJob).where(ScanJob.scan_config_id == config_id))
            .scalars()
            .all()
        )
    assert len(jobs) == metrics_schedule.FAILURE_BACKOFF_AFTER
    assert {job.status for job in jobs} == {ScanJobStatus.failed.value}


def test_failure_streak_ignores_jobs_this_dispatcher_did_not_create(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """Manual scans share scan_jobs with collection and must not move the streak.

    ``scan_jobs`` is keyed on scan_config_id and written by run_scan, metrics
    replay, event-group apply and the demo tick as well as by this dispatcher.
    Counting them by exclusion broke both ways: a user's failed "Run now" would
    trigger a backoff on collection, and one successful "Run now" would clear a
    real streak and let the 5-minute storm resume.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        config_id = config.id
        # Enough foreign failures to trip the threshold if they were counted.
        _seed_job_history(
            session,
            config_id,
            statuses=[ScanJobStatus.failed.value] * (metrics_schedule.FAILURE_BACKOFF_AFTER + 2),
            newest_age=timedelta(minutes=5),
            mode=None,
        )

    result, dispatched = _run_dispatcher(sync_session_factory, monkeypatch)
    assert result == {"checked": 1, "dispatched": 1}, "foreign failures must not defer collection"
    assert len(dispatched) == 1

    # And the mirror case: a foreign SUCCESS on top of a real streak must not
    # look like a recovery.
    with sync_session_factory() as session:
        session.execute(delete(ScanJob).where(ScanJob.scan_config_id == config_id))
        session.commit()
        _seed_job_history(
            session,
            config_id,
            statuses=[ScanJobStatus.failed.value] * metrics_schedule.FAILURE_BACKOFF_AFTER,
            newest_age=timedelta(minutes=10),
        )
        _seed_job_history(
            session,
            config_id,
            statuses=[ScanJobStatus.completed.value],
            newest_age=timedelta(minutes=1),
            mode=None,
        )

    result, dispatched = _run_dispatcher(sync_session_factory, monkeypatch)
    assert result == {"checked": 1, "dispatched": 0}, "a foreign success must not clear the streak"
    assert dispatched == []


def test_check_metrics_due_retries_once_the_backoff_window_has_elapsed(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """Backoff defers the retry; it never abandons the config."""
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        _seed_job_history(
            session,
            config.id,
            statuses=[ScanJobStatus.failed.value] * metrics_schedule.FAILURE_BACKOFF_AFTER,
            # Past the 1h (one interval) wait for the first backed-off attempt.
            newest_age=timedelta(minutes=90),
        )

    result, dispatched = _run_dispatcher(sync_session_factory, monkeypatch)

    assert result == {"checked": 1, "dispatched": 1}
    assert len(dispatched) == 1


def test_check_metrics_due_does_not_back_off_below_the_failure_threshold(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A short run of failures still retries immediately.

    This is what keeps the stale-job reaper above honest: it marks a hung job
    failed moments before the streak is measured, and that single fresh failure
    must not defer the replacement dispatch it exists to trigger.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        _seed_job_history(
            session,
            config.id,
            statuses=[ScanJobStatus.failed.value] * (metrics_schedule.FAILURE_BACKOFF_AFTER - 1),
            newest_age=timedelta(minutes=1),
        )

    result, dispatched = _run_dispatcher(sync_session_factory, monkeypatch)

    assert result == {"checked": 1, "dispatched": 1}
    assert len(dispatched) == 1


def test_check_metrics_due_dispatches_healthy_config_despite_older_failures(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A healthy config keeps its cadence — the streak breaks at the first success.

    A config that failed for a while and then recovered must dispatch the instant
    its bucket check says due, with no residual penalty from the old failures.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        _seed_job_history(
            session,
            config.id,
            statuses=[
                ScanJobStatus.completed.value,
                *[ScanJobStatus.failed.value] * (metrics_schedule.FAILURE_BACKOFF_AFTER + 2),
            ],
            newest_age=timedelta(minutes=1),
        )

    result, dispatched = _run_dispatcher(sync_session_factory, monkeypatch)

    assert result == {"checked": 1, "dispatched": 1}
    assert len(dispatched) == 1


def test_failure_backoff_delay_grows_then_holds_at_a_bounded_ceiling() -> None:
    """The delay doubles per failure and is clamped by both ceilings (tripl-n9ee)."""
    hour = timedelta(hours=1)

    # Below the threshold there is no wait at all — nothing changes for a config
    # that merely blipped.
    for streak in range(metrics_schedule.FAILURE_BACKOFF_AFTER):
        assert metrics_schedule._failure_backoff_delay(streak, hour) is None

    # 1x, 2x, 4x, 8x the interval, then held at FAILURE_BACKOFF_MAX_INTERVALS.
    assert metrics_schedule._failure_backoff_delay(3, hour) == hour
    assert metrics_schedule._failure_backoff_delay(4, hour) == 2 * hour
    assert metrics_schedule._failure_backoff_delay(5, hour) == 4 * hour
    assert metrics_schedule._failure_backoff_delay(6, hour) == 8 * hour
    assert metrics_schedule._failure_backoff_delay(9, hour) == 8 * hour

    # A short interval hits the 8x multiple long before the absolute ceiling...
    quarter = timedelta(minutes=15)
    assert metrics_schedule._failure_backoff_delay(20, quarter) == 2 * hour

    # ...while a coarse one is held at its own interval rather than being pushed
    # out to 8 weeks, so a fixed weekly config is not left looking dead.
    week = timedelta(weeks=1)
    assert metrics_schedule._failure_backoff_delay(20, week) == week


def test_replace_scope_anomalies_upserts_on_conflict(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The anomaly write upserts on (config, scope, bucket) instead of failing.

    Regression: two concurrent collect_metrics runs for the same config both
    delete + re-insert the same window; a plain INSERT then tripped
    uq_metric_anomaly_scope_bucket and failed the whole job.
    """
    from tripl.core.analyzers.anomaly_detector import SCOPE_EVENT, DetectedAnomaly
    from tripl.worker.tasks.metrics.detect import _replace_scope_anomalies

    bucket = datetime(2026, 6, 15, 9, 0, tzinfo=UTC)
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        event_id = uuid.uuid4()
        # A row a racing run already committed. The delete window below is set
        # AFTER this bucket on purpose, so the delete cannot clear it first and
        # the insert is forced to hit the unique constraint.
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                scope_type=SCOPE_EVENT,
                scope_ref=str(event_id),
                event_id=event_id,
                event_type_id=None,
                bucket=bucket,
                actual_count=10,
                expected_count=5.0,
                stddev=1.0,
                z_score=5.0,
                direction="spike",
            )
        )
        session.commit()

        written = _replace_scope_anomalies(
            session,
            scan_config_id=config.id,
            scope_type=SCOPE_EVENT,
            scope_ref=str(event_id),
            evaluation_start=bucket + timedelta(hours=1),
            evaluation_end=bucket + timedelta(hours=2),
            event_id=event_id,
            event_type_id=None,
            anomalies=[
                DetectedAnomaly(
                    bucket=bucket,
                    actual_count=39,
                    expected_count=33.0,
                    stddev=0.15,
                    z_score=4.76,
                    direction="spike",
                )
            ],
        )
        session.commit()

        assert written == 1
        rows = (
            session.execute(
                select(MetricAnomaly).where(
                    MetricAnomaly.scan_config_id == config.id,
                    MetricAnomaly.scope_ref == str(event_id),
                    MetricAnomaly.bucket == bucket,
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1  # upserted, not duplicated and not crashed
        assert rows[0].actual_count == 39  # row updated to the second run's value


def test_collect_metrics_skips_job_cancelled_before_start(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A job cancelled while queued must not run or be flipped back to running."""
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        job = ScanJob(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            status=ScanJobStatus.cancelled.value,
        )
        session.add(job)
        session.commit()
        config_id = str(config.id)
        job_id = str(job.id)

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)

    def _no_adapter(*args: object, **kwargs: object) -> object:
        raise AssertionError("adapter must not be built for a cancelled job")

    monkeypatch.setattr(metrics, "_build_adapter", _no_adapter)

    result = metrics.collect_metrics.run(config_id, job_id)

    assert result == {"cancelled": True, "scan_config_id": config_id}
    with sync_session_factory() as session:
        reloaded = session.get(ScanJob, uuid.UUID(job_id))
        assert reloaded is not None
        assert reloaded.status == ScanJobStatus.cancelled.value
        assert reloaded.started_at is None


def test_collect_metrics_reuses_existing_pending_job(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        job = ScanJob(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            status=ScanJobStatus.pending.value,
        )
        session.add(job)
        session.commit()
        config_id = str(config.id)
        job_id = str(job.id)

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
            return (["event_name"], [], [])

        def close(self) -> None:
            return None

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)

    def build_fake_adapter(ds: DataSource) -> FakeAdapter:
        return FakeAdapter()

    def fake_analyze_cardinality(*args: object, **kwargs: object) -> object:
        return object()

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        return GenerationResult(columns_analyzed=1)

    monkeypatch.setattr(metrics, "_build_adapter", build_fake_adapter)
    monkeypatch.setattr(metrics, "analyze_cardinality", fake_analyze_cardinality)
    monkeypatch.setattr(
        metrics,
        "generate_events",
        fake_generate_events,
    )

    result = metrics.collect_metrics.run(config_id, job_id)

    assert result["event_metrics"] == 0
    assert result["type_metrics"] == 0
    assert result["signals_added"] == 0

    with sync_session_factory() as session:
        jobs = (
            session.execute(select(ScanJob).where(ScanJob.scan_config_id == uuid.UUID(config_id)))
            .scalars()
            .all()
        )

    assert len(jobs) == 1
    assert str(jobs[0].id) == job_id
    assert jobs[0].status == ScanJobStatus.completed.value
    assert jobs[0].started_at is not None
    assert jobs[0].completed_at is not None
    assert jobs[0].result_summary is not None
    assert jobs[0].result_summary["columns_analyzed"] == 1


def test_collect_metrics_persists_sanitized_error_on_failure(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A driver failure during collection must not leak host/port/library text
    into the job's user-facing error_message (full detail stays in the logs)."""
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        job = ScanJob(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            status=ScanJobStatus.pending.value,
        )
        session.add(job)
        session.commit()
        config_id = str(config.id)
        job_id = str(job.id)

    class TimingOutAdapter:
        def test_connection(self) -> bool:
            raise TimeoutError(
                "clickhouse-connect: HTTPConnectionPool("
                "host='warehouse.internal', port=8123): Read timed out. "
                "(read timeout=30)"
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: TimingOutAdapter())

    with pytest.raises(TimeoutError):
        metrics.collect_metrics.run(config_id, job_id)

    with sync_session_factory() as session:
        reloaded = session.get(ScanJob, uuid.UUID(job_id))
        assert reloaded is not None
        assert reloaded.status == ScanJobStatus.failed.value
        assert reloaded.completed_at is not None
        assert reloaded.error_message == "Scan failed: the data source did not respond in time."
        assert "warehouse.internal" not in reloaded.error_message
        assert "8123" not in reloaded.error_message
        assert "clickhouse" not in reloaded.error_message.lower()
        assert "read timed out" not in reloaded.error_message.lower()


def test_collect_metrics_replaces_metric_rows_in_window(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None
        login_event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login",
            description="",
            status="implemented",
        )
        stale_event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Old",
            description="",
            status="implemented",
        )
        bucket = datetime(2026, 1, 1, 10)
        session.add_all(
            [
                login_event,
                stale_event,
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=login_event.id,
                    event_type_id=None,
                    bucket=bucket,
                    count=1,
                ),
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=stale_event.id,
                    event_type_id=None,
                    bucket=bucket,
                    count=99,
                ),
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=None,
                    event_type_id=config.event_type_id,
                    bucket=bucket,
                    count=100,
                ),
            ]
        )
        session.commit()
        config_id = str(config.id)
        login_event_id = login_event.id
        stale_event_id = stale_event.id
        event_type_id = config.event_type_id

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
            return (["event_name"], [], [(datetime(2026, 1, 1, 10), "Login", 12)])

        def close(self) -> None:
            return None

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: FakeAdapter())
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11), False),
    )
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        with sync_session_factory() as session:
            persisted_event = session.get(Event, login_event_id)
            assert persisted_event is not None
            return GenerationResult(
                columns_analyzed=1,
                col_meta={"event_name": {"is_json": False, "is_low": True}},
                events_by_name={"event_name=Login": persisted_event},
            )

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)

    result = metrics.collect_metrics.run(config_id)

    assert result["metrics_deleted"] == 3
    assert result["event_metrics"] == 1
    assert result["type_metrics"] == 1

    with sync_session_factory() as session:
        stale_metric = session.execute(
            select(EventMetric).where(EventMetric.event_id == stale_event_id)
        ).scalar_one_or_none()
        assert stale_metric is None
        login_metric = session.execute(
            select(EventMetric).where(EventMetric.event_id == login_event_id)
        ).scalar_one()
        assert login_metric.count == 12
        type_metric = session.execute(
            select(EventMetric).where(EventMetric.event_type_id == event_type_id)
        ).scalar_one()
        assert type_metric.count == 12


def test_collect_metrics_uses_database_grouped_breakdown_rows(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None
        config.metric_breakdown_columns = ["country", "device"]
        config.metric_breakdown_values_limit = 2
        login_event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login",
            description="",
            status="implemented",
        )
        stale_metric = EventMetricBreakdown(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            event_id=login_event.id,
            event_type_id=None,
            bucket=datetime(2026, 1, 1, 10),
            breakdown_column="country",
            breakdown_value="Old",
            is_other=False,
            count=99,
        )
        session.add_all([login_event, stale_metric])
        session.commit()
        config_id = str(config.id)
        login_event_id = login_event.id
        event_type_id = config.event_type_id

    class FakeAdapter:
        def __init__(self) -> None:
            self.breakdown_calls: list[tuple[list[str], int | None]] = []

        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
                ColumnInfo(name="country", type_name="String"),
                ColumnInfo(name="device", type_name="String"),
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
            return (
                ["event_name", "country", "device"],
                [],
                [(datetime(2026, 1, 1, 10), "Login", "US", "mobile", 30)],
            )

        def get_time_bucketed_breakdown_counts_multi(
            self,
            base_query: str,
            time_column: str,
            interval: str,
            breakdown_columns: list[str],
            regular_columns: list[str],
            json_columns: list[str],
            json_value_paths: dict[str, list[str]] | None,
            time_from: datetime,
            time_to: datetime,
            values_limit: int | None = None,
            limit: int = 100000,
        ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
            self.breakdown_calls.append((breakdown_columns, values_limit))
            assert regular_columns == ["event_name", "country", "device"]
            return (
                ["event_name", "country", "device"],
                [],
                [
                    (
                        datetime(2026, 1, 1, 10),
                        "country",
                        "US",
                        False,
                        "Login",
                        "US",
                        "mobile",
                        10,
                    ),
                    (
                        datetime(2026, 1, 1, 10),
                        "country",
                        "Other",
                        True,
                        "Login",
                        "FR",
                        "desktop",
                        20,
                    ),
                    (
                        datetime(2026, 1, 1, 10),
                        "device",
                        "mobile",
                        False,
                        "Login",
                        "US",
                        "mobile",
                        15,
                    ),
                    (
                        datetime(2026, 1, 1, 10),
                        "device",
                        "Other",
                        True,
                        "Login",
                        "FR",
                        "desktop",
                        15,
                    ),
                ],
            )

        def close(self) -> None:
            return None

    adapter = FakeAdapter()
    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11), False),
    )
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        with sync_session_factory() as session:
            persisted_event = session.get(Event, login_event_id)
            assert persisted_event is not None
            return GenerationResult(
                columns_analyzed=2,
                col_meta={"event_name": {"is_json": False, "is_low": True}},
                events_by_name={"event_name=Login": persisted_event},
            )

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)

    result = metrics.collect_metrics.run(config_id)

    assert adapter.breakdown_calls == [(["country", "device"], 2)]
    assert result["breakdown_event_metrics"] == 4
    assert result["breakdown_type_metrics"] == 4
    assert result["breakdown_metrics_deleted"] == 1

    with sync_session_factory() as session:
        event_breakdowns = (
            session.execute(
                select(EventMetricBreakdown).where(EventMetricBreakdown.event_id == login_event_id)
            )
            .scalars()
            .all()
        )
        assert {
            (row.breakdown_column, row.breakdown_value, row.is_other, row.count)
            for row in event_breakdowns
        } == {
            ("country", "US", False, 10),
            ("country", "Other", True, 20),
            ("device", "mobile", False, 15),
            ("device", "Other", True, 15),
        }
        type_breakdowns = (
            session.execute(
                select(EventMetricBreakdown).where(
                    EventMetricBreakdown.event_type_id == event_type_id
                )
            )
            .scalars()
            .all()
        )
        assert {
            (row.breakdown_column, row.breakdown_value, row.is_other, row.count)
            for row in type_breakdowns
        } == {
            ("country", "US", False, 10),
            ("country", "Other", True, 20),
            ("device", "mobile", False, 15),
            ("device", "Other", True, 15),
        }


def test_collect_metrics_stores_every_app_version_without_collapse(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    # Write path stores every version verbatim; SemVer latest-N retention and the
    # "Other" rollup happen at read time (metrics_service.get_app_version_series),
    # so the kept set stays stable across the whole window regardless of chunking.
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None
        config.app_version_column = "app_version"
        config.app_version_keep_releases = 2
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
        login_event_id = login_event.id
        event_type_id = config.event_type_id

    class FakeAdapter:
        def __init__(self) -> None:
            self.breakdown_calls: list[tuple[list[str], int | None]] = []

        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
                ColumnInfo(name="app_version", type_name="String"),
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
            return (
                ["event_name", "app_version"],
                [],
                [
                    (datetime(2026, 1, 1, 10), "Login", "2.2.0", 10),
                    (datetime(2026, 1, 1, 10), "Login", "2.1.0", 8),
                    (datetime(2026, 1, 1, 10), "Login", "2.0.0", 5),
                    (datetime(2026, 1, 1, 10), "Login", "1.9.0", 3),
                ],
            )

        def get_time_bucketed_breakdown_counts_multi(
            self,
            base_query: str,
            time_column: str,
            interval: str,
            breakdown_columns: list[str],
            regular_columns: list[str],
            json_columns: list[str],
            json_value_paths: dict[str, list[str]] | None,
            time_from: datetime,
            time_to: datetime,
            values_limit: int | None = None,
            limit: int = 100000,
        ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
            self.breakdown_calls.append((breakdown_columns, values_limit))
            return (
                ["event_name", "app_version"],
                [],
                [],
            )

        def close(self) -> None:
            return None

    adapter = FakeAdapter()
    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11), False),
    )
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        with sync_session_factory() as session:
            persisted_event = session.get(Event, login_event_id)
            assert persisted_event is not None
            return GenerationResult(
                columns_analyzed=2,
                col_meta={"event_name": {"is_json": False, "is_low": True}},
                events_by_name={"event_name=Login": persisted_event},
            )

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)

    result = metrics.collect_metrics.run(config_id)

    # Version breakdown is derived from the primary bucketed metric rows, so no
    # extra warehouse breakdown query is needed when generic breakdown columns
    # are absent.
    assert adapter.breakdown_calls == []
    # Every version is stored verbatim (is_other=False); nothing is collapsed at
    # write time regardless of app_version_keep_releases — retention is read-time.
    assert result["breakdown_event_metrics"] == 4
    assert result["breakdown_type_metrics"] == 4

    expected = {
        ("app_version", "2.2.0", False, 10),
        ("app_version", "2.1.0", False, 8),
        ("app_version", "2.0.0", False, 5),
        ("app_version", "1.9.0", False, 3),
    }
    with sync_session_factory() as session:
        event_breakdowns = (
            session.execute(
                select(EventMetricBreakdown).where(EventMetricBreakdown.event_id == login_event_id)
            )
            .scalars()
            .all()
        )
        assert {
            (row.breakdown_column, row.breakdown_value, row.is_other, row.count)
            for row in event_breakdowns
        } == expected
        type_breakdowns = (
            session.execute(
                select(EventMetricBreakdown).where(
                    EventMetricBreakdown.event_type_id == event_type_id
                )
            )
            .scalars()
            .all()
        )
        assert {
            (row.breakdown_column, row.breakdown_value, row.is_other, row.count)
            for row in type_breakdowns
        } == expected


def test_collect_metrics_never_collects_the_version_column_as_a_generic_breakdown(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """An event listing the scan's app_version column takes the version path only.

    The version column already has its own series
    (``_collect_app_version_breakdown_rows``, the test above). Collecting it a
    second time as a generic breakdown wrote the SAME (scope, bucket, column,
    value, is_other) key twice inside one multi-row ON CONFLICT DO UPDATE — a
    cardinality violation on Postgres — and added a generic
    ``('app_version', 'Other', True)`` row the version path neither writes nor
    deletes, which then double-counts in the version series (tripl-0zpq.15).
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None
        config.app_version_column = "app_version"
        config.app_version_keep_releases = 2
        login_event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login",
            description="",
            status="implemented",
            # The stored value this fix has to tolerate: saved back when the
            # event form still offered the column.
            metric_breakdown_columns=["app_version"],
        )
        session.add(login_event)
        session.commit()
        config_id = str(config.id)
        login_event_id = login_event.id
        event_type_id = config.event_type_id

    class FakeAdapter:
        def __init__(self) -> None:
            self.breakdown_calls: list[tuple[list[str], int | None]] = []

        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
                ColumnInfo(name="app_version", type_name="String"),
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
            return (
                ["event_name", "app_version"],
                [],
                [
                    (datetime(2026, 1, 1, 10), "Login", "2.2.0", 10),
                    (datetime(2026, 1, 1, 10), "Login", "2.1.0", 8),
                ],
            )

        def get_time_bucketed_breakdown_counts_multi(
            self,
            base_query: str,
            time_column: str,
            interval: str,
            breakdown_columns: list[str],
            regular_columns: list[str],
            json_columns: list[str],
            json_value_paths: dict[str, list[str]] | None,
            time_from: datetime,
            time_to: datetime,
            values_limit: int | None = None,
            limit: int = 100000,
        ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
            self.breakdown_calls.append((breakdown_columns, values_limit))
            return (
                ["event_name", "app_version"],
                [],
                [
                    (
                        datetime(2026, 1, 1, 10),
                        "app_version",
                        "Other",
                        True,
                        "Login",
                        "1.0.0",
                        18,
                    ),
                ],
            )

        def close(self) -> None:
            return None

    adapter = FakeAdapter()
    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11), False),
    )
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        with sync_session_factory() as session:
            persisted_event = session.get(Event, login_event_id)
            assert persisted_event is not None
            return GenerationResult(
                columns_analyzed=2,
                col_meta={"event_name": {"is_json": False, "is_low": True}},
                events_by_name={"event_name=Login": persisted_event},
            )

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)

    result = metrics.collect_metrics.run(config_id)

    # The configured column is refused before any query is built, so the generic
    # breakdown pass never runs at all -- the "Other" row the fake stands ready
    # to return is unreachable.
    assert adapter.breakdown_calls == []
    assert result["breakdown_event_metrics"] == 2
    assert result["breakdown_type_metrics"] == 2

    expected = {
        ("app_version", "2.2.0", False, 10),
        ("app_version", "2.1.0", False, 8),
    }
    with sync_session_factory() as session:
        event_breakdowns = (
            session.execute(
                select(EventMetricBreakdown).where(EventMetricBreakdown.event_id == login_event_id)
            )
            .scalars()
            .all()
        )
        # The version series is intact and carries no generic rollup row.
        assert {
            (row.breakdown_column, row.breakdown_value, row.is_other, row.count)
            for row in event_breakdowns
        } == expected
        type_breakdowns = (
            session.execute(
                select(EventMetricBreakdown).where(
                    EventMetricBreakdown.event_type_id == event_type_id
                )
            )
            .scalars()
            .all()
        )
        assert {
            (row.breakdown_column, row.breakdown_value, row.is_other, row.count)
            for row in type_breakdowns
        } == expected


def test_reserved_catalog_columns_includes_version_and_platform() -> None:
    from tripl.worker.tasks.metrics.tasks import reserved_catalog_columns

    full = ScanConfig(
        event_type_column="event_name",
        time_column="time",
        app_version_column="app_version",
        platform_column="platform",
    )
    assert reserved_catalog_columns(full) == {"event_name", "time", "app_version", "platform"}

    # Only the columns that are actually set are reserved.
    partial = ScanConfig(time_column="time", app_version_column="app_version")
    assert reserved_catalog_columns(partial) == {"time", "app_version"}

    assert reserved_catalog_columns(ScanConfig()) == set()


def test_reserved_catalog_columns_includes_event_group_rule_columns() -> None:
    """The column group rules match on is identity, so it must stay out of the catalog.

    It is the scan's SECOND grouping column and was the one the catalog sync did
    not know about: it auto-created a FieldDefinition for it and the scan filled
    that field with the rule's own regex (tripl-jfm3.57). The demo shows it
    plainly — event_type_column is "event_type" while the rules key on
    "event_name", so reserving only the former left the latter exposed.
    """
    from tripl.worker.tasks.metrics.tasks import reserved_catalog_columns

    config = ScanConfig(
        event_type_column="event_type",
        time_column="event_time",
        event_group_rules=[
            {
                "name": "Home Screen View",
                "condition_logic": "all",
                "conditions": [{"field": "event_name", "pattern": r"^Home\ Screen\ View$"}],
            },
            {
                "name": "Checkout",
                "condition_logic": "any",
                "conditions": [
                    {"field": "event_name", "pattern": "^Purchase"},
                    {"field": "screen_name", "pattern": "^Checkout"},
                ],
            },
        ],
    )
    assert reserved_catalog_columns(config) == {
        "event_type",
        "event_time",
        "event_name",
        "screen_name",
    }


def test_reserved_catalog_columns_never_reserves_the_event_name_source() -> None:
    """A column the event name is built from outranks every reservation rule.

    Reserving it makes catalog_sync skip its FieldDefinition, and generate_events
    assembles format arguments only from columns that have one — so the name
    format is evaluated with its placeholder missing and collection dies with
    "the event name format references unknown keys".

    This is production's 'Old events (iOS)' config: group rules keyed on
    ``action`` plus ``event_name_format='{action}'``. tripl-jfm3.90 reserved
    ``action`` and took the scan down for 200 consecutive runs (tripl-lpin).
    """
    from tripl.worker.tasks.metrics.tasks import reserved_catalog_columns

    config = ScanConfig(
        time_column="time",
        event_name_format="{action}",
        event_group_rules=[
            {
                "name": "wind_alert_regularity_select_*",
                "condition_logic": "all",
                "conditions": [{"field": "action", "pattern": "^wind_alert_regularity_select_.*"}],
            }
        ],
    )
    assert reserved_catalog_columns(config) == {"time"}

    # A multi-key format is covered the same way, and a group-rule column the
    # name does NOT use stays reserved — the tripl-jfm3.57 fix is intact.
    multi = ScanConfig(
        event_type_column="event_type",
        time_column="time",
        app_version_column="app_version",
        event_name_format="{category}:{action}",
        event_group_rules=[
            {
                "conditions": [
                    {"field": "action", "pattern": "^x"},
                    {"field": "screen_name", "pattern": "^y"},
                ]
            }
        ],
    )
    assert reserved_catalog_columns(multi) == {"event_type", "time", "app_version", "screen_name"}


def test_reserved_catalog_columns_never_reserves_a_dotted_placeholders_base_column() -> None:
    """tripl-lpin reached from the other direction, through a DOTTED placeholder.

    ``{event.category}`` is walked out of the ``event`` column's JSON, and
    ``generate_events`` assembles ``col.path`` keys only for columns that reached
    ``col_meta`` — i.e. that have a FieldDefinition. Subtracting the full key
    ``event.category`` from a set of TOP-LEVEL column names removes nothing, so
    ``event`` stayed reserved, catalog_sync skipped its FieldDefinition, and the
    scan died on the very message this function's docstring exists to prevent.
    Reverting to ``event_name_format_columns`` here turns this red.
    """
    from tripl.worker.tasks.metrics.tasks import reserved_catalog_columns

    config = ScanConfig(
        time_column="time",
        platform_column="event",
        event_name_format="{event.category}",
    )
    assert reserved_catalog_columns(config) == {"time"}

    # The base column, never a path segment: a group-rule column that merely
    # shares a name with a path SEGMENT stays reserved.
    segment = ScanConfig(
        time_column="time",
        event_name_format="{event.category}",
        event_group_rules=[{"conditions": [{"field": "category", "pattern": "^x"}]}],
    )
    assert reserved_catalog_columns(segment) == {"time", "category"}


def test_reserved_catalog_columns_survives_malformed_group_rules() -> None:
    """event_group_rules is a JSON column, so an older or hand-edited row must not crash a scan."""
    from tripl.worker.tasks.metrics.tasks import reserved_catalog_columns

    config = ScanConfig(
        time_column="event_time",
        event_group_rules=[
            "not-a-mapping",
            {"name": "no conditions key"},
            {"name": "conditions not a list", "conditions": {"field": "x"}},
            {"name": "condition not a mapping", "conditions": ["nope"]},
            {"name": "blank field", "conditions": [{"field": "   ", "pattern": "^x"}]},
            # A non-string field used to be coerced with str(), so null became the
            # literal reserved column "None" — which would then exempt a real
            # column of that name from plan-gap reporting (Copilot review, #72).
            {"name": "null field", "conditions": [{"field": None, "pattern": "^x"}]},
            {"name": "numeric field", "conditions": [{"field": 7, "pattern": "^x"}]},
            {"name": "no field key", "conditions": [{"pattern": "^x"}]},
            {"name": "good", "conditions": [{"field": "event_name", "pattern": "^x"}]},
        ],
    )
    assert reserved_catalog_columns(config) == {"event_time", "event_name"}


def test_correlation_group_id_is_the_same_across_buckets() -> None:
    """The group is the INCIDENT, not the hour it was last seen.

    With the bucket in the key, every collection minted a group the user had
    never acted on, so acknowledging/resolving/muting in the inbox silenced
    exactly the delivery already in hand and the next hour alerted again
    (tripl-jfm3.91).
    """
    from tripl.worker.tasks.metrics.dispatch import _correlation_group_id

    scan_config_id = uuid.uuid4()
    rule_id = uuid.uuid4()
    scope = {"scope_type": "event", "scope_ref": str(uuid.uuid4())}

    first = _correlation_group_id(
        scan_config_id=scan_config_id, rule_id=rule_id, direction="drop", **scope
    )
    second = _correlation_group_id(
        scan_config_id=scan_config_id, rule_id=rule_id, direction="drop", **scope
    )
    assert first == second

    # Direction still separates incidents: a spike is not the drop you acked.
    spike = _correlation_group_id(
        scan_config_id=scan_config_id, rule_id=rule_id, direction="spike", **scope
    )
    assert spike != first
    # And so does the rule.
    other_rule = _correlation_group_id(
        scan_config_id=scan_config_id, rule_id=uuid.uuid4(), direction="drop", **scope
    )
    assert other_rule != first


def test_correlation_group_id_separates_scopes() -> None:
    """One inbox action must not silence every other scope of the rule.

    Keyed on scan_config:rule:direction alone, ``_SUPPRESSING_INBOX_STATUSES``
    gated all of them together and ``_reopen_closed_incidents`` could not release
    them, because it waited for a scope that suppression was keeping alive. On
    prod one operator note covered 7 unrelated scopes under group bd6c96f5.
    """
    from tripl.worker.tasks.metrics.dispatch import _correlation_group_id

    scan_config_id = uuid.uuid4()
    rule_id = uuid.uuid4()

    def group(scope_type: str, scope_ref: str) -> uuid.UUID:
        return _correlation_group_id(
            scan_config_id=scan_config_id,
            rule_id=rule_id,
            scope_type=scope_type,
            scope_ref=scope_ref,
            direction="drop",
        )

    first_event = str(uuid.uuid4())
    second_event = str(uuid.uuid4())
    assert group("event", first_event) != group("event", second_event)
    # The scope_type is part of it too: an event and an event_type can share a ref.
    assert group("event", first_event) != group("event_type", first_event)


def test_acknowledged_groups_are_suppressed(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Acknowledge must stop re-delivery, like resolve/mute already did.

    It was the one inbox action with no effect on delivery at all, so an
    operator who acked an incident kept being paged for it every hour.
    """
    from tripl.models.alert_correlation_state import AlertCorrelationState
    from tripl.worker.tasks.metrics.dispatch import _suppressed_correlation_group_ids

    with sync_session_factory() as session:
        project = Project(name="Ack", slug="ack-suppression", description="")
        session.add(project)
        session.flush()
        acknowledged = uuid.uuid4()
        still_open = uuid.uuid4()
        session.add_all(
            [
                AlertCorrelationState(
                    project_id=project.id,
                    correlation_group_id=acknowledged,
                    status="acknowledged",
                ),
                AlertCorrelationState(
                    project_id=project.id,
                    correlation_group_id=still_open,
                    status="open",
                ),
            ]
        )
        session.flush()

        suppressed = _suppressed_correlation_group_ids(session, project_id=project.id)

    assert acknowledged in suppressed
    assert still_open not in suppressed


def test_closing_an_incident_reopens_its_inbox_decision(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Suppression must not outlive the incident, or it would be permanent.

    Acknowledging a drop silences that scope's drops — but only until the scope
    stops firing. The next drop is a new incident and has to alert. A scope that
    is STILL firing keeps its decision: it is the same incident.
    """
    from tripl.models.alert_correlation_state import AlertCorrelationState
    from tripl.worker.tasks.metrics.dispatch import (
        _correlation_group_id,
        _reopen_closed_incidents,
    )

    with sync_session_factory() as session:
        project = Project(name="Reopen", slug="reopen-incident", description="")
        session.add(project)
        session.flush()
        scan_config_id = uuid.uuid4()
        rule_id = uuid.uuid4()
        closed_scope = ("event", str(uuid.uuid4()))
        firing_scope = ("event", str(uuid.uuid4()))
        group_id = _correlation_group_id(
            scan_config_id=scan_config_id,
            rule_id=rule_id,
            scope_type=closed_scope[0],
            scope_ref=closed_scope[1],
            direction="drop",
        )
        still_firing_group_id = _correlation_group_id(
            scan_config_id=scan_config_id,
            rule_id=rule_id,
            scope_type=firing_scope[0],
            scope_ref=firing_scope[1],
            direction="drop",
        )
        unrelated = uuid.uuid4()
        session.add_all(
            [
                AlertCorrelationState(
                    project_id=project.id,
                    correlation_group_id=group_id,
                    status="acknowledged",
                ),
                AlertCorrelationState(
                    project_id=project.id,
                    correlation_group_id=still_firing_group_id,
                    status="acknowledged",
                ),
                AlertCorrelationState(
                    project_id=project.id,
                    correlation_group_id=unrelated,
                    status="acknowledged",
                ),
            ]
        )
        session.flush()

        _reopen_closed_incidents(
            session,
            project_id=project.id,
            scan_config_id=scan_config_id,
            rule_id=rule_id,
            scope_keys=[closed_scope],
        )
        session.flush()

        states = {
            state.correlation_group_id: state.status
            for state in session.execute(select(AlertCorrelationState)).scalars()
        }

    assert states[group_id] == "open"
    # The rule's other scope is still firing; its decision stands.
    assert states[still_firing_group_id] == "acknowledged"
    # Another rule's decision is untouched.
    assert states[unrelated] == "acknowledged"


def test_closing_an_incident_does_not_cancel_a_timed_mute(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """ "Muted until T" outlives the incident; "acknowledged" does not.

    Reopening on quiet killed a seven-day mute the moment the signal paused for
    one collection, and the incident paged the user again hours later
    (tripl-jfm3.98). A LAPSED mute still reopens — that path lives in
    _suppressed_correlation_group_ids.
    """
    from tripl.models.alert_correlation_state import AlertCorrelationState
    from tripl.worker.tasks.metrics.dispatch import (
        _correlation_group_id,
        _reopen_closed_incidents,
    )

    with sync_session_factory() as session:
        project = Project(name="Mute", slug="mute-survives", description="")
        session.add(project)
        session.flush()
        scan_config_id = uuid.uuid4()
        rule_id = uuid.uuid4()
        scope = ("event", str(uuid.uuid4()))
        group_id = _correlation_group_id(
            scan_config_id=scan_config_id,
            rule_id=rule_id,
            scope_type=scope[0],
            scope_ref=scope[1],
            direction="drop",
        )
        lapsed_id = _correlation_group_id(
            scan_config_id=scan_config_id,
            rule_id=rule_id,
            scope_type=scope[0],
            scope_ref=scope[1],
            direction="spike",
        )
        now = datetime.now(UTC)
        session.add_all(
            [
                AlertCorrelationState(
                    project_id=project.id,
                    correlation_group_id=group_id,
                    status="muted",
                    muted_until=now + timedelta(days=7),
                ),
                AlertCorrelationState(
                    project_id=project.id,
                    correlation_group_id=lapsed_id,
                    status="muted",
                    muted_until=now - timedelta(minutes=1),
                ),
            ]
        )
        session.flush()

        _reopen_closed_incidents(
            session,
            project_id=project.id,
            scan_config_id=scan_config_id,
            rule_id=rule_id,
            scope_keys=[scope],
        )
        session.flush()

        states = {
            state.correlation_group_id: (state.status, state.muted_until)
            for state in session.execute(select(AlertCorrelationState)).scalars()
        }

    assert states[group_id][0] == "muted"
    assert states[group_id][1] is not None
    assert states[lapsed_id] == ("open", None)


def test_closing_an_incident_does_not_cancel_an_indefinite_mute(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A NULL ``muted_until`` is "muted until I unmute", not "expired long ago".

    The timed mute above survives a quiet collection; the INDEFINITE one is the
    case a fall-through hurts most, because its release is a deliberate human
    act and nothing downstream would ever restore the row. The old check read
    ``muted_until is not None and muted_until > now``, which answers False for a
    NULL and silently reopened the strongest mute in the product on the first
    quiet scan (tripl-a50u).

    The other two rows pin that the fix did not over-correct: a LAPSED mute and
    an ACKNOWLEDGE must still reset, or "do not tell me before T" and "I am on
    this incident" would both become permanent by accident.
    """
    from tripl.models.alert_correlation_state import AlertCorrelationState
    from tripl.worker.tasks.metrics.dispatch import (
        _correlation_group_id,
        _reopen_closed_incidents,
    )

    with sync_session_factory() as session:
        project = Project(name="Forever", slug="mute-forever", description="")
        session.add(project)
        session.flush()
        scan_config_id = uuid.uuid4()
        rule_id = uuid.uuid4()
        scope = ("event", str(uuid.uuid4()))
        other_scope = ("event", str(uuid.uuid4()))

        def group(scope_key: tuple[str, str], direction: str) -> uuid.UUID:
            return _correlation_group_id(
                scan_config_id=scan_config_id,
                rule_id=rule_id,
                scope_type=scope_key[0],
                scope_ref=scope_key[1],
                direction=direction,
            )

        # AnomalyDirection is spike|drop, so one scope carries exactly two
        # groups — enough to sit the indefinite and the lapsed mute side by side
        # under a single set of scope_keys.
        indefinite_id = group(scope, "drop")
        lapsed_id = group(scope, "spike")
        acknowledged_id = group(other_scope, "drop")
        now = datetime.now(UTC)
        session.add_all(
            [
                AlertCorrelationState(
                    project_id=project.id,
                    correlation_group_id=indefinite_id,
                    status="muted",
                    muted_until=None,
                ),
                AlertCorrelationState(
                    project_id=project.id,
                    correlation_group_id=lapsed_id,
                    status="muted",
                    muted_until=now - timedelta(minutes=1),
                ),
                AlertCorrelationState(
                    project_id=project.id,
                    correlation_group_id=acknowledged_id,
                    status="acknowledged",
                ),
            ]
        )
        session.flush()

        _reopen_closed_incidents(
            session,
            project_id=project.id,
            scan_config_id=scan_config_id,
            rule_id=rule_id,
            scope_keys=[scope, other_scope],
        )
        session.flush()

        states = {
            state.correlation_group_id: (state.status, state.muted_until)
            for state in session.execute(select(AlertCorrelationState)).scalars()
        }

    assert states[indefinite_id] == ("muted", None)
    assert states[lapsed_id] == ("open", None)
    assert states[acknowledged_id] == ("open", None)


def test_indefinitely_muted_groups_stay_suppressed_and_are_never_lapsed(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The mute lifecycle lives here, and a NULL expiry must never run out.

    ``_suppressed_correlation_group_ids`` is the one place that expires a mute,
    and it is ALREADY null-correct — so this test passes on the code it was
    written against. It is a regression lock, not a reproduction: the instinct
    when fixing the sibling check in ``_reopen_closed_incidents`` is to make this
    one "symmetric" by writing ``muted_until is None or muted_until <= now``,
    which would expire every indefinite mute on the very next collection while
    every other test still passed and the API still reported the mute as taken
    (tripl-a50u). Nothing else pins this line's NULL behaviour.

    The lapsed row is asserted alongside it so a fix in the other direction —
    never expiring anything — cannot pass either.
    """
    from tripl.models.alert_correlation_state import AlertCorrelationState
    from tripl.worker.tasks.metrics.dispatch import _suppressed_correlation_group_ids

    with sync_session_factory() as session:
        project = Project(name="Suppress", slug="mute-suppression", description="")
        session.add(project)
        session.flush()
        indefinite = uuid.uuid4()
        lapsed = uuid.uuid4()
        still_open = uuid.uuid4()
        now = datetime.now(UTC)
        session.add_all(
            [
                AlertCorrelationState(
                    project_id=project.id,
                    correlation_group_id=indefinite,
                    status="muted",
                    muted_until=None,
                ),
                AlertCorrelationState(
                    project_id=project.id,
                    correlation_group_id=lapsed,
                    status="muted",
                    muted_until=now - timedelta(minutes=1),
                ),
                AlertCorrelationState(
                    project_id=project.id,
                    correlation_group_id=still_open,
                    status="open",
                ),
            ]
        )
        session.flush()

        suppressed = _suppressed_correlation_group_ids(session, project_id=project.id)
        session.flush()

        states = {
            state.correlation_group_id: (state.status, state.muted_until)
            for state in session.execute(select(AlertCorrelationState)).scalars()
        }

    assert indefinite in suppressed
    assert states[indefinite] == ("muted", None)
    # A mute with an expiry still runs out here, and reopening it is this
    # function's job — not _reopen_closed_incidents'.
    assert lapsed not in suppressed
    assert states[lapsed] == ("open", None)
    assert still_open not in suppressed


def test_ensure_event_type_skips_reserved_columns(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Reserved columns (app_version/platform/time) must not become catalog
    fields; ordinary columns still do."""
    from tripl.worker.tasks.metrics.generation import _ensure_event_type_with_fields

    with sync_session_factory() as session:
        config = _create_scan_config(session)
        columns = [
            ColumnInfo(name="time", type_name="DateTime"),
            ColumnInfo(name="app_version", type_name="String"),
            ColumnInfo(name="platform", type_name="String"),
            ColumnInfo(name="screen", type_name="String"),
        ]
        et = _ensure_event_type_with_fields(
            session,
            config.project_id,
            "ReservedSkipPanel",
            columns,
            {"time", "app_version", "platform"},
        )
        assert {fd.name for fd in et.field_definitions} == {"screen"}


def _seed_app_version_breakdowns(
    session: Session,
    config: ScanConfig,
    *,
    login_id: uuid.UUID,
    filler_id: uuid.UUID,
) -> None:
    """Mature 2.0.0 across 10 daily buckets; 2.1.0 ships on day 7 (active) but
    Login disappears (no 2.1.0 rows for it)."""
    days = [datetime(2026, 1, d) for d in range(1, 11)]
    for day in days:
        for event_id, count in ((login_id, 100), (filler_id, 900)):
            session.add(
                EventMetricBreakdown(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=event_id,
                    event_type_id=None,
                    bucket=day,
                    breakdown_column="app_version",
                    breakdown_value="2.0.0",
                    is_other=False,
                    count=count,
                )
            )
    for day in days[6:]:
        session.add(
            EventMetricBreakdown(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                event_id=filler_id,
                event_type_id=None,
                bucket=day,
                breakdown_column="app_version",
                breakdown_value="2.1.0",
                is_other=False,
                count=500,
            )
        )


def test_recalculate_release_regressions_flags_missing_event_idempotently(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        config.app_version_column = "app_version"
        login = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login",
            description="",
            status="implemented",
        )
        filler = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Filler",
            description="",
            status="implemented",
        )
        session.add_all([login, filler])
        session.commit()
        login_id = login.id
        _seed_app_version_breakdowns(session, config, login_id=login_id, filler_id=filler.id)
        session.commit()

        detected = metrics._recalculate_release_regressions(session, config)
        session.commit()
        assert detected == 1
        rows = (
            session.execute(
                select(ReleaseRegression).where(ReleaseRegression.scan_config_id == config.id)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        regression = rows[0]
        assert regression.scope_type == "event"
        assert regression.event_id == login_id
        assert regression.version == "2.1.0"
        assert regression.previous_version == "2.0.0"
        assert regression.kind == "missing"
        assert regression.observed_count == 0
        assert regression.expected_count == 200.0

        # Re-run replaces, never accumulates.
        detected_again = metrics._recalculate_release_regressions(session, config)
        session.commit()
        assert detected_again == 1
        rows_again = (
            session.execute(
                select(ReleaseRegression).where(ReleaseRegression.scan_config_id == config.id)
            )
            .scalars()
            .all()
        )
        assert len(rows_again) == 1

        # The pass records that it could judge the release, not only what it
        # found. Zero rows and a comparable verdict is a clean bill of health;
        # zero rows and no verdict at all is not.
        verdicts = {
            v.scope_type: v
            for v in (
                session.execute(
                    select(ReleaseComparability).where(
                        ReleaseComparability.scan_config_id == config.id
                    )
                )
                .scalars()
                .all()
            )
        }
        assert set(verdicts) == {"event", "event_type"}
        assert verdicts["event"].comparable is True
        assert verdicts["event"].reason == "comparable"
        assert verdicts["event"].version == "2.1.0"
        assert verdicts["event"].previous_version == "2.0.0"
        assert verdicts["event"].app_version_column == "app_version"
        # The event-type scope has no breakdown rows of its own, but the release
        # pair and its populations come from the shared event-level totals, so
        # that scope is comparable too — it simply has nothing to report.
        assert verdicts["event_type"].comparable is True
        assert verdicts["event_type"].version == "2.1.0"


def test_recalculate_release_regressions_records_a_withheld_verdict(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A suppressed comparison and a healthy one used to be byte-identical: no
    rows either way, the verdict logged at INFO and dropped. The windy-ios 15.7.4
    mix (two thirds of the rollout's volume in onboarding screens the baseline
    barely visited) has to leave a readable trace instead."""
    days = [datetime(2026, 1, d) for d in range(1, 11)]
    steady = {"main": 700, "onboarding": 20, "purchase": 280}
    fresh = {"main": 60, "onboarding": 640, "purchase": 300}
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        config.app_version_column = "app_version"
        events = {
            name: Event(
                id=uuid.uuid4(),
                project_id=config.project_id,
                event_type_id=config.event_type_id,
                name=f"event_name={name}",
                description="",
                status="implemented",
            )
            for name in steady
        }
        session.add_all(list(events.values()))
        session.commit()

        for day in days:
            for name, count in steady.items():
                session.add(
                    EventMetricBreakdown(
                        id=uuid.uuid4(),
                        scan_config_id=config.id,
                        event_id=events[name].id,
                        event_type_id=None,
                        bucket=day,
                        breakdown_column="app_version",
                        breakdown_value="2.0.0",
                        is_other=False,
                        count=count,
                    )
                )
        for day in days[6:]:
            for name, count in fresh.items():
                session.add(
                    EventMetricBreakdown(
                        id=uuid.uuid4(),
                        scan_config_id=config.id,
                        event_id=events[name].id,
                        event_type_id=None,
                        bucket=day,
                        breakdown_column="app_version",
                        breakdown_value="2.1.0",
                        is_other=False,
                        count=count,
                    )
                )
        session.commit()

        metrics._recalculate_release_regressions(session, config)
        session.commit()

        verdict = (
            session.execute(
                select(ReleaseComparability).where(
                    ReleaseComparability.scan_config_id == config.id,
                    ReleaseComparability.scope_type == "event",
                )
            )
            .scalars()
            .one()
        )
        assert verdict.comparable is False
        assert verdict.reason == "population_mismatch"
        assert verdict.version == "2.1.0"
        assert verdict.previous_version == "2.0.0"
        assert verdict.emerging_share > verdict.max_emerging_share


def test_recalculate_release_regressions_writes_one_verdict_for_both_scopes(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The two persistence passes judge the same release and must not disagree.

    Event-scope sees the windy-ios mix and scores 0.54; the event-type rows here
    are a single type carrying all the traffic, which on its own scores 0.0 and
    comes back comparable. Comparability is a property of the release, so the
    partition that saw the population change decides for both — otherwise the
    type pass persists composition-normalized rows that the event pass had
    already ruled untrustworthy, with nothing downstream filtering by scope
    (tripl-phpy).
    """
    days = [datetime(2026, 1, d) for d in range(1, 11)]
    steady = {"main": 700, "onboarding": 20, "purchase": 280}
    fresh = {"main": 60, "onboarding": 640, "purchase": 300}
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        config.app_version_column = "app_version"
        events = {
            name: Event(
                id=uuid.uuid4(),
                project_id=config.project_id,
                event_type_id=config.event_type_id,
                name=f"event_name={name}",
                description="",
                status="implemented",
            )
            for name in steady
        }
        session.add_all(list(events.values()))
        session.commit()

        def _add(*, event_id, event_type_id, bucket, version, count):
            session.add(
                EventMetricBreakdown(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=event_id,
                    event_type_id=event_type_id,
                    bucket=bucket,
                    breakdown_column="app_version",
                    breakdown_value=version,
                    is_other=False,
                    count=count,
                )
            )

        for day in days:
            for name, count in steady.items():
                _add(
                    event_id=events[name].id,
                    event_type_id=None,
                    bucket=day,
                    version="2.0.0",
                    count=count,
                )
            # The type-scope partition: all of it in one type, so its own
            # composition never moves and it would be judged comparable alone.
            _add(
                event_id=None,
                event_type_id=config.event_type_id,
                bucket=day,
                version="2.0.0",
                count=sum(steady.values()),
            )
        for day in days[6:]:
            for name, count in fresh.items():
                _add(
                    event_id=events[name].id,
                    event_type_id=None,
                    bucket=day,
                    version="2.1.0",
                    count=count,
                )
            _add(
                event_id=None,
                event_type_id=config.event_type_id,
                bucket=day,
                version="2.1.0",
                count=sum(fresh.values()),
            )
        session.commit()

        metrics._recalculate_release_regressions(session, config)
        session.commit()

        verdicts = {
            v.scope_type: v
            for v in (
                session.execute(
                    select(ReleaseComparability).where(
                        ReleaseComparability.scan_config_id == config.id
                    )
                )
                .scalars()
                .all()
            )
        }
        assert set(verdicts) == {"event", "event_type"}
        assert {v.comparable for v in verdicts.values()} == {False}
        assert {v.reason for v in verdicts.values()} == {"population_mismatch"}
        assert {v.version for v in verdicts.values()} == {"2.1.0"}
        # Both rows carry the share that decided the verdict, not the score each
        # partition happened to contribute: a stored 0.0 next to
        # ``comparable=False`` would read as a broken gate.
        assert len({v.emerging_share for v in verdicts.values()}) == 1
        assert all(v.emerging_share > v.max_emerging_share for v in verdicts.values())


def test_recalculate_release_regressions_skips_prerelease_builds(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """``ScanConfig.app_version_prerelease_pattern`` reached the app-version
    chart but not this analyzer, so the two disagreed on the same scan: the chart
    named the shipped release while the regression judged the TestFlight build.
    """
    days = [datetime(2026, 1, d) for d in range(1, 11)]
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        config.app_version_column = "app_version"
        config.app_version_prerelease_pattern = r"\+internal$"
        login = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login",
            description="",
            status="implemented",
        )
        filler = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Filler",
            description="",
            status="implemented",
        )
        session.add_all([login, filler])
        session.commit()

        # Two shipped releases in lockstep, plus an internal build that took real
        # traffic and never sent Login.
        for day in days:
            for version, login_count, filler_count in (
                ("2.0.0", 100, 900),
                ("2.1.0", 100, 900),
                ("2.2.0+internal", 0, 1000),
            ):
                for event, count in ((login, login_count), (filler, filler_count)):
                    session.add(
                        EventMetricBreakdown(
                            id=uuid.uuid4(),
                            scan_config_id=config.id,
                            event_id=event.id,
                            event_type_id=None,
                            bucket=day,
                            breakdown_column="app_version",
                            breakdown_value=version,
                            is_other=False,
                            count=count,
                        )
                    )
        session.commit()

        detected = metrics._recalculate_release_regressions(session, config)
        session.commit()

        assert detected == 0
        verdict = (
            session.execute(
                select(ReleaseComparability).where(
                    ReleaseComparability.scan_config_id == config.id,
                    ReleaseComparability.scope_type == "event",
                )
            )
            .scalars()
            .one()
        )
        assert verdict.version == "2.1.0"
        assert verdict.previous_version == "2.0.0"


def test_recalculate_release_regressions_inert_without_version_column(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        # No app_version_column set: feature is inert and clears stale rows.
        session.add(
            ReleaseRegression(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                scope_type="event",
                scope_ref="stale",
                event_id=None,
                event_type_id=None,
                app_version_column="app_version",
                version="9.9.9",
                previous_version="9.8.0",
                kind="missing",
                observed_count=0,
                expected_count=10.0,
                ratio=0.0,
                share_prev=0.5,
                share_new=0.0,
                release_share=0.5,
                window_from=datetime(2026, 1, 1),
                window_to=datetime(2026, 1, 2),
            )
        )
        session.add(
            ReleaseComparability(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                scope_type="event",
                app_version_column="app_version",
                version="9.9.9",
                previous_version="9.8.0",
                comparable=False,
                reason="population_mismatch",
                emerging_share=0.9,
                max_emerging_share=0.25,
            )
        )
        session.commit()

        detected = metrics._recalculate_release_regressions(session, config)
        session.commit()
        assert detected == 0
        rows = (
            session.execute(
                select(ReleaseRegression).where(ReleaseRegression.scan_config_id == config.id)
            )
            .scalars()
            .all()
        )
        assert rows == []
        # A stale "cannot be judged yet" outliving the pass that produced it is
        # the same lie in the other direction.
        verdicts = (
            session.execute(
                select(ReleaseComparability).where(ReleaseComparability.scan_config_id == config.id)
            )
            .scalars()
            .all()
        )
        assert verdicts == []


def test_collect_metrics_uses_event_level_breakdown_columns(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None
        login_event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login",
            description="",
            status="implemented",
            metric_breakdown_columns=["country"],
        )
        signup_event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Signup",
            description="",
            status="implemented",
        )
        session.add_all([login_event, signup_event])
        session.commit()
        config_id = str(config.id)
        login_event_id = login_event.id
        signup_event_id = signup_event.id

    class FakeAdapter:
        def __init__(self) -> None:
            self.breakdown_calls: list[tuple[list[str], int | None]] = []

        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
                ColumnInfo(name="country", type_name="String"),
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
            return (
                ["event_name", "country"],
                [],
                [
                    (datetime(2026, 1, 1, 10), "Login", "US", 7),
                    (datetime(2026, 1, 1, 10), "Signup", "US", 11),
                ],
            )

        def get_time_bucketed_breakdown_counts_multi(
            self,
            base_query: str,
            time_column: str,
            interval: str,
            breakdown_columns: list[str],
            regular_columns: list[str],
            json_columns: list[str],
            json_value_paths: dict[str, list[str]] | None,
            time_from: datetime,
            time_to: datetime,
            values_limit: int | None = None,
            limit: int = 100000,
        ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
            self.breakdown_calls.append((breakdown_columns, values_limit))
            return (
                ["event_name", "country"],
                [],
                [
                    (datetime(2026, 1, 1, 10), "country", "US", False, "Login", "US", 7),
                    (datetime(2026, 1, 1, 10), "country", "US", False, "Signup", "US", 11),
                ],
            )

        def close(self) -> None:
            return None

    adapter = FakeAdapter()
    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11), False),
    )
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        with sync_session_factory() as session:
            login = session.get(Event, login_event_id)
            signup = session.get(Event, signup_event_id)
            assert login is not None
            assert signup is not None
            return GenerationResult(
                columns_analyzed=1,
                col_meta={"event_name": {"is_json": False, "is_low": True}},
                events_by_name={
                    "event_name=Login": login,
                    "event_name=Signup": signup,
                },
            )

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)

    result = metrics.collect_metrics.run(config_id)

    assert adapter.breakdown_calls == [(["country"], None)]
    assert result["breakdown_event_metrics"] == 1
    assert result["breakdown_type_metrics"] == 0

    with sync_session_factory() as session:
        event_breakdowns = session.execute(select(EventMetricBreakdown)).scalars().all()
        assert [
            (row.event_id, row.breakdown_column, row.breakdown_value, row.count)
            for row in event_breakdowns
        ] == [(login_event_id, "country", "US", 7)]


def test_collect_metrics_writes_distribution_drift_rows(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None
        config.distribution_drift_fields = ["platform"]
        config.baseline_window_buckets = 2
        config.min_history_buckets = 2
        login_event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login",
            description="",
            status="implemented",
        )
        stale_drift = DistributionDrift(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            event_type_id=None,
            field_name="platform",
            bucket=datetime(2026, 1, 1, 10),
            psi=0.0,
            band="stable",
            baseline_total=1,
            current_total=1,
            top_movers=[],
        )
        session.add_all([login_event, stale_drift])
        session.commit()
        config_id = str(config.id)
        login_event_id = login_event.id
        event_type_id = config.event_type_id

    class FakeAdapter:
        def __init__(self) -> None:
            self.breakdown_calls: list[list[str]] = []

        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
                ColumnInfo(name="platform", type_name="String"),
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
            return (
                ["event_name", "platform"],
                [],
                [
                    (datetime(2026, 1, 1, 10), "Login", "ios", 90),
                    (datetime(2026, 1, 1, 10), "Login", "android", 10),
                ],
            )

        def get_time_bucketed_breakdown_counts_multi(
            self,
            base_query: str,
            time_column: str,
            interval: str,
            breakdown_columns: list[str],
            regular_columns: list[str],
            json_columns: list[str],
            json_value_paths: dict[str, list[str]] | None,
            time_from: datetime,
            time_to: datetime,
            values_limit: int | None = None,
            limit: int = 100000,
        ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
            self.breakdown_calls.append(breakdown_columns)
            assert values_limit is None
            assert time_from == datetime(2026, 1, 1, 8)
            assert time_to == datetime(2026, 1, 1, 11)
            return (
                ["event_name", "platform"],
                [],
                [
                    (datetime(2026, 1, 1, 8), "platform", "ios", False, "Login", "ios", 50),
                    (
                        datetime(2026, 1, 1, 8),
                        "platform",
                        "android",
                        False,
                        "Login",
                        "android",
                        50,
                    ),
                    (datetime(2026, 1, 1, 9), "platform", "ios", False, "Login", "ios", 50),
                    (
                        datetime(2026, 1, 1, 9),
                        "platform",
                        "android",
                        False,
                        "Login",
                        "android",
                        50,
                    ),
                    (datetime(2026, 1, 1, 10), "platform", "ios", False, "Login", "ios", 90),
                    (
                        datetime(2026, 1, 1, 10),
                        "platform",
                        "android",
                        False,
                        "Login",
                        "android",
                        10,
                    ),
                ],
            )

        def close(self) -> None:
            return None

    adapter = FakeAdapter()
    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11), False),
    )
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        with sync_session_factory() as session:
            persisted_event = session.get(Event, login_event_id)
            assert persisted_event is not None
            return GenerationResult(
                columns_analyzed=2,
                col_meta={"event_name": {"is_json": False, "is_low": True}},
                events_by_name={"event_name=Login": persisted_event},
            )

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)

    result = metrics.collect_metrics.run(config_id)

    assert adapter.breakdown_calls == [["platform"]]
    assert result["distribution_drifts"] == 2
    assert result["significant_distribution_drifts"] == 2
    assert result["distribution_drifts_deleted"] == 1

    with sync_session_factory() as session:
        drifts = session.execute(select(DistributionDrift)).scalars().all()
        assert len(drifts) == 2
        assert {drift.event_type_id for drift in drifts} == {None, event_type_id}
        assert {drift.band for drift in drifts} == {"significant"}
        assert all(drift.field_name == "platform" for drift in drifts)
        assert all(drift.top_movers for drift in drifts)


def test_collect_metrics_rolls_back_metric_delete_when_job_fails(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None
        event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login",
            description="",
            status="implemented",
        )
        metric = EventMetric(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            event_id=event.id,
            event_type_id=None,
            bucket=datetime(2026, 1, 1, 10),
            count=9,
        )
        job = ScanJob(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            status=ScanJobStatus.pending.value,
        )
        session.add_all([event, metric, job])
        session.commit()
        config_id = str(config.id)
        event_id = event.id
        job_id = str(job.id)

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
            return (["event_name"], [], [(datetime(2026, 1, 1, 10), "Login", 12)])

        def close(self) -> None:
            return None

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: FakeAdapter())
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11), False),
    )
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        with sync_session_factory() as session:
            persisted_event = session.get(Event, event_id)
            assert persisted_event is not None
            return GenerationResult(
                columns_analyzed=1,
                col_meta={"event_name": {"is_json": False, "is_low": True}},
                events_by_name={"event_name=Login": persisted_event},
            )

    def fail_upsert(*args: object, **kwargs: object) -> None:
        raise RuntimeError("upsert failed")

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)
    monkeypatch.setattr(metrics, "_upsert_event_metrics_rows", fail_upsert)

    with pytest.raises(RuntimeError, match="upsert failed"):
        metrics.collect_metrics.run(config_id, job_id)

    with sync_session_factory() as session:
        persisted_metric = session.execute(
            select(EventMetric).where(EventMetric.event_id == event_id)
        ).scalar_one()
        assert persisted_metric.count == 9
        persisted_job = session.get(ScanJob, uuid.UUID(job_id))
        assert persisted_job is not None
        assert persisted_job.status == ScanJobStatus.failed.value


def test_collect_metrics_recalculates_and_clears_metric_anomalies(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config, event_type, event = _seed_anomaly_scan_state(session, base=_ANOMALY_BASE)
        config_id = str(config.id)

    class FakeAdapter:
        rows: list[tuple[object, ...]] = []

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
            return (["event_name"], [], self.rows)

        def close(self) -> None:
            return None

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: FakeAdapter())
    # The window head is held back by ANOMALY_INGESTION_SETTLING (tripl-jfm3.7),
    # so end it two buckets PAST the hour-10 drop for that drop to be settled and
    # scored on this run rather than deferred to the next one.
    monkeypatch.setattr(
        metrics, "_floor_to_interval", lambda dt, delta: _ANOMALY_BASE + timedelta(hours=13)
    )
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        with sync_session_factory() as session:
            persisted_event = session.get(Event, event.id)
            assert persisted_event is not None
            return GenerationResult(
                columns_analyzed=1,
                col_meta={"event_name": {"is_json": False, "is_low": True}},
                events_by_name={"event_name=Login": persisted_event},
            )

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)

    FakeAdapter.rows = [
        (_ANOMALY_BASE + timedelta(hours=8), "Login", 10),
        (_ANOMALY_BASE + timedelta(hours=9), "Login", 10),
    ]
    first_result = metrics.collect_metrics.run(config_id)
    assert first_result["anomalies_detected"] == 3
    assert first_result["signals_added"] == 3

    with sync_session_factory() as session:
        anomalies = session.execute(select(MetricAnomaly)).scalars().all()
        assert {(anomaly.scope_type, anomaly.direction) for anomaly in anomalies} == {
            ("project_total", "drop"),
            ("event_type", "drop"),
            ("event", "drop"),
        }
        assert {anomaly.bucket for anomaly in anomalies} == {_ANOMALY_BASE + timedelta(hours=10)}

    FakeAdapter.rows = [
        (_ANOMALY_BASE + timedelta(hours=8), "Login", 10),
        (_ANOMALY_BASE + timedelta(hours=9), "Login", 10),
    ]
    repeated_result = metrics.collect_metrics.run(config_id)
    assert repeated_result["anomalies_detected"] == 3
    assert repeated_result["signals_added"] == 0
    assert repeated_result["signals_removed"] == 0

    FakeAdapter.rows = [
        (_ANOMALY_BASE + timedelta(hours=8), "Login", 10),
        (_ANOMALY_BASE + timedelta(hours=9), "Login", 10),
        (_ANOMALY_BASE + timedelta(hours=10), "Login", 10),
    ]
    second_result = metrics.collect_metrics.run(config_id)
    assert second_result["anomalies_detected"] == 0
    assert second_result["signals_added"] == 0
    assert second_result["signals_removed"] == 3

    with sync_session_factory() as session:
        anomalies = session.execute(select(MetricAnomaly)).scalars().all()
        assert anomalies == []


def _arrange_alerting_collection(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> tuple[str, list[str]]:
    """A config whose next collection finds three anomalies one alert rule matches.

    Answers the config id and the list ``send_alert_delivery.delay`` appends to.
    """
    with sync_session_factory() as session:
        config, _event_type, _event = _seed_anomaly_scan_state(session, base=_ANOMALY_BASE)
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=config.project_id,
            type="slack",
            name="Main Slack",
            enabled=True,
            webhook_url_encrypted="secret",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Main Rule",
            enabled=True,
            include_project_total=True,
            include_event_types=True,
            include_events=True,
            notify_on_spike=True,
            notify_on_drop=True,
            min_percent_delta=0,
            min_absolute_delta=0,
            min_expected_count=0,
            cooldown_minutes=1440,
        )
        session.add_all([destination, rule])
        session.commit()
        config_id = str(config.id)

    queued_delivery_ids: list[str] = []

    class FakeAdapter:
        rows: list[tuple[object, ...]] = []

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
            return (["event_name"], [], self.rows)

        def close(self) -> None:
            return None

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: FakeAdapter())
    # Two buckets of head-room for the ingestion-settling allowance; see
    # test_collect_metrics_recalculates_and_clears_metric_anomalies.
    monkeypatch.setattr(
        metrics, "_floor_to_interval", lambda dt, delta: _ANOMALY_BASE + timedelta(hours=13)
    )
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        metrics.send_alert_delivery,
        "delay",
        lambda delivery_id: queued_delivery_ids.append(delivery_id),
    )

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        with sync_session_factory() as session:
            persisted_event = session.execute(select(Event)).scalar_one()
            return GenerationResult(
                columns_analyzed=1,
                col_meta={"event_name": {"is_json": False, "is_low": True}},
                events_by_name={"event_name=Login": persisted_event},
            )

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)

    FakeAdapter.rows = [
        (_ANOMALY_BASE + timedelta(hours=8), "Login", 10),
        (_ANOMALY_BASE + timedelta(hours=9), "Login", 10),
    ]
    return config_id, queued_delivery_ids


def test_collect_metrics_queues_alert_deliveries(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    config_id, queued_delivery_ids = _arrange_alerting_collection(sync_session_factory, monkeypatch)
    result = metrics.collect_metrics.run(config_id)

    assert result["alerts_queued"] == 1
    assert len(queued_delivery_ids) == 1

    with sync_session_factory() as session:
        deliveries = session.execute(select(AlertDelivery)).scalars().all()
        items = session.execute(select(AlertDeliveryItem)).scalars().all()
        assert len(deliveries) == 1
        assert deliveries[0].matched_count == 3
        assert len(items) == 3
        # Every item carries a group id — that id is the inbox handle, and an
        # item without one can never be acted on. One per SCOPE, so acting on
        # any of the three leaves the other two alerting.
        group_ids = {item.correlation_group_id for item in items}
        assert None not in group_ids
        assert len(group_ids) == 3


def test_a_failing_notification_producer_never_fails_the_collection(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """#259: the signal notification hook runs last and best-effort — when it
    raises, the job still completes and the alert deliveries are still queued."""
    config_id, queued_delivery_ids = _arrange_alerting_collection(sync_session_factory, monkeypatch)
    with sync_session_factory() as session:
        job = ScanJob(
            id=uuid.uuid4(),
            scan_config_id=uuid.UUID(config_id),
            status=ScanJobStatus.pending.value,
        )
        session.add(job)
        session.commit()
        job_id = str(job.id)
    produced: list[str] = []

    def _boom(_session: Session, source: str, _subject: object) -> int:
        produced.append(source)
        raise RuntimeError("notifications unavailable")

    monkeypatch.setattr(metrics, "produce_notifications", _boom)

    result = metrics.collect_metrics.run(config_id, job_id)

    assert produced == ["signals"]
    assert result["alerts_queued"] == 1
    assert len(queued_delivery_ids) == 1
    with sync_session_factory() as session:
        reloaded = session.get(ScanJob, uuid.UUID(job_id))
        assert reloaded is not None
        assert reloaded.status == ScanJobStatus.completed.value
        assert len(session.execute(select(AlertDelivery)).scalars().all()) == 1


def _seed_alert_rule(
    session: Session,
    config: ScanConfig,
    *,
    destination_type: str = "slack",
    include_schema_drifts: bool = False,
) -> AlertRule:
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=config.project_id,
        type=destination_type,
        name=f"Dest {destination_type}",
        enabled=True,
        webhook_url_encrypted="secret",
        bot_token_encrypted="secret",
        chat_id="-100",
    )
    rule = AlertRule(
        id=uuid.uuid4(),
        destination_id=destination.id,
        name="Rule",
        enabled=True,
        include_project_total=True,
        include_event_types=True,
        include_events=True,
        include_schema_drifts=include_schema_drifts,
        notify_on_spike=True,
        notify_on_drop=True,
        min_percent_delta=0,
        min_absolute_delta=0,
        min_expected_count=0,
        cooldown_minutes=1440,
    )
    session.add_all([destination, rule])
    return rule


def _seed_drop_anomaly(
    session: Session,
    config: ScanConfig,
    event: Event,
    *,
    bucket: datetime,
) -> MetricAnomaly:
    anomaly = MetricAnomaly(
        id=uuid.uuid4(),
        scan_config_id=config.id,
        scope_type="event",
        scope_ref=str(event.id),
        event_id=event.id,
        event_type_id=None,
        bucket=bucket,
        actual_count=0,
        expected_count=10,
        stddev=1,
        z_score=-10,
        direction="drop",
    )
    session.add(anomaly)
    return anomaly


def test_a_live_scope_alerts_on_the_newest_bucket_the_detector_may_emit(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A volume anomaly on a still-emitting scope has to reach dispatch.

    The detector withholds the newest ``settling_buckets`` of a series from
    emission (120 minutes / 1h grid = 2 buckets), so a scope that is still
    filling the freshest bucket can never carry an anomaly at or after its metric
    head. Classifying against the RAW head therefore only ever admitted scopes
    that had gone SILENT, whose head stops advancing: on prod all 16 event-scope
    alert items ever delivered carry actual_count 0.0 (x15) or 1.0.
    """
    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session, base=_ANOMALY_BASE)
        _seed_alert_rule(session, config)
        head = _ANOMALY_BASE + timedelta(hours=9)
        # The settled head: two buckets back, exactly where _emission_end put
        # the newest emittable row.
        _seed_drop_anomaly(session, config, event, bucket=head - timedelta(hours=2))
        session.commit()

        active = metrics_signals._get_latest_active_anomalies(session, config)
        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)

        assert ("event", str(event.id)) in active
        assert len(delivery_ids) == 1
        item = session.execute(select(AlertDeliveryItem)).scalar_one()
        assert item.scope_ref == str(event.id)


def test_an_anomaly_older_than_the_settled_head_closes_its_alert_state(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The settled head is a shift, not a removal, of the freshness bar.

    A scope whose newest anomaly sits further back than the withheld buckets is
    no longer firing on the latest scan, so its AlertRuleState must still close
    rather than alert forever.
    """
    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session, base=_ANOMALY_BASE)
        _seed_alert_rule(session, config)
        head = _ANOMALY_BASE + timedelta(hours=9)
        # One bucket behind the settled head.
        _seed_drop_anomaly(session, config, event, bucket=head - timedelta(hours=3))
        session.commit()

        active = metrics_signals._get_latest_active_anomalies(session, config)

        assert ("event", str(event.id)) not in active


# ---------------------------------------------------------------------------
# An outage that is still running keeps its alert state open (tripl-l429.26).
# ---------------------------------------------------------------------------

_OUTAGE_ANCHOR = _ANOMALY_BASE - timedelta(days=5)


def _seed_aged_outage(
    session: Session,
    *,
    scan_alive: bool,
    actual_count: float = 0,
    expected_count: float = 10,
) -> tuple[ScanConfig, Event]:
    """One scope that went silent five days ago, on a scan that may still be alive.

    ``_seed_anomaly_scan_state`` lays the event's metric rows down at the anchor
    and nothing after — the shape ``_collapse_outage_runs`` leaves behind once it
    has announced an outage once (one anchor row at ``actual_count`` 0, never
    re-emitted). With ``scan_alive`` the SAME scan keeps collecting for its
    event-type scope right up to the present, so the collector is demonstrably
    alive and the silence belongs to the scope rather than to a switched-off scan.

    ``expected_count`` is what the anchor says the scope normally emits; the
    default is an ordinary baseline. Seeding it 0 describes a scope that was
    never expected to emit at all, which is not an outage (tripl-wkwv.4).
    """
    config, event_type, event = _seed_anomaly_scan_state(session, base=_OUTAGE_ANCHOR)
    _seed_alert_rule(session, config)
    anchor = _OUTAGE_ANCHOR + timedelta(hours=9)

    if scan_alive:
        # A sibling scope of the same scan, still collecting now.
        for hour in range(3):
            session.add(
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=None,
                    event_type_id=event_type.id,
                    bucket=_ANOMALY_BASE + timedelta(hours=hour),
                    count=10,
                )
            )

    anomaly = _seed_drop_anomaly(session, config, event, bucket=anchor)
    anomaly.actual_count = actual_count
    anomaly.expected_count = expected_count
    session.commit()
    return config, event


def test_an_outage_that_is_still_running_keeps_its_alert_state_open(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A live incident must not have its alert closed by the age of its own report.

    An outage is announced ONCE, at onset, and deliberately never re-emitted, so
    a scope that has been down for five days is described by a single five-day-old
    row. Judging that row by its own age closed the alert state at
    ``max(24h, 3 x interval)`` into an incident that was still running — while the
    Anomalies page, the sidebar badge, the metrics list and the drilldown all kept
    rendering it open, because they re-check the anchor against the series
    (``monitoring_utils._outage_is_still_running``). The monitor therefore read
    healthy during a live outage and ``_reopen_closed_incidents`` cleared the
    operator's inbox acknowledgement mid-incident.

    The worker never carried that re-check. It was an unported gap, not a
    deliberate divergence: this is exactly the population the re-check is defined
    for (count-shaped, scan-backed), and both of its inputs were already in hand.
    """
    with sync_session_factory() as session:
        config, event = _seed_aged_outage(session, scan_alive=True)

        active = metrics_signals._get_latest_active_anomalies(session, config)

        assert ("event", str(event.id)) in active


def test_an_outage_on_a_scan_that_stopped_collecting_still_closes(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The cap the re-check must not remove.

    A switched-off collector and a dead event look identical from the stored
    data, so the re-check only holds a signal open while the SCAN is provably
    still collecting for something. Without this, the wall-clock cap that stops a
    decommissioned scan pinning its final anomaly red forever would be gone — and
    on the alert path that means a rule that never stops firing.
    """
    with sync_session_factory() as session:
        config, event = _seed_aged_outage(session, scan_alive=False)

        active = metrics_signals._get_latest_active_anomalies(session, config)

        assert ("event", str(event.id)) not in active


def test_an_aged_spike_is_not_held_open_by_the_outage_recheck(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Only silence earns the re-check — a burned-out spike still ages out.

    The anchor row has to SAY the scope was at zero. A scope that emitted
    something has a series that can age its own report out normally, and holding
    every stale anomaly open on a live scan would keep alert state open for every
    scope that ever fired.
    """
    with sync_session_factory() as session:
        config, event = _seed_aged_outage(session, scan_alive=True, actual_count=7)

        active = metrics_signals._get_latest_active_anomalies(session, config)

        assert ("event", str(event.id)) not in active


def test_a_zero_baseline_anchor_is_not_held_open_by_the_outage_recheck(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The anchor also has to say there was something to LOSE (tripl-wkwv.4).

    A scope expected to emit nothing and emitting nothing is not an incident, and
    it is the shape this re-check alone can end: an empty scope stores no metric
    rows, so its head never advances past the anchor and the latest-scan branch
    stays true for the life of the deployment.

    Pinned on the WORKER path as well as the display one because this is the same
    predicate reached through a different caller, and the two paths have already
    drifted twice (tripl-l429.14, tripl-l429.19) — each time by one side gaining
    an input the other did not.
    """
    with sync_session_factory() as session:
        config, event = _seed_aged_outage(session, scan_alive=True, expected_count=0)

        active = metrics_signals._get_latest_active_anomalies(session, config)

        assert ("event", str(event.id)) not in active


def test_a_zero_baseline_spike_that_is_still_live_keeps_alerting(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """And a real move off a zero baseline still reaches dispatch.

    The trap in tripl-wkwv.4: "expected 0" alone must not close anything. A scope
    that started emitting where it never had is a genuine observation, judged on
    freshness like any other signal — here on the newest bucket the detector may
    emit, which is what a live scope carries.
    """
    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session, base=_ANOMALY_BASE)
        _seed_alert_rule(session, config)
        head = _ANOMALY_BASE + timedelta(hours=9)
        anomaly = _seed_drop_anomaly(session, config, event, bucket=head - timedelta(hours=2))
        anomaly.actual_count = 7
        anomaly.expected_count = 0
        anomaly.direction = "spike"
        session.commit()

        active = metrics_signals._get_latest_active_anomalies(session, config)

        assert ("event", str(event.id)) in active


def test_an_aged_ongoing_outage_leaves_the_run_delta_but_not_alerting(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The one divergence between the two signal paths that is deliberate.

    ``_get_visible_signal_scope_keys`` answers "what did THIS RUN change", which
    is a different question from "what is open in the project". An outage
    announced in an earlier run is not new in this one, so it leaves the run
    delta at the freshness horizon while staying an open alert candidate and
    staying listed on the Anomalies page.

    Pinned because the divergence was previously only a comment. Giving this set
    the outage re-check too would make ``signals_removed`` stop naming a scope
    the page still lists — the opposite of what its docstring promises — so the
    next attempt to "unify" the two has to break this test to do it.
    """
    with sync_session_factory() as session:
        config, event = _seed_aged_outage(session, scan_alive=True)
        key = ("event", str(event.id))

        assert key in metrics_signals._get_latest_active_anomalies(session, config)
        assert key not in metrics_signals._get_visible_signal_scope_keys(session, config.id)


def _seed_closed_alert_state(
    session: Session,
    rule: AlertRule,
    config: ScanConfig,
    event: Event,
    *,
    last_notified_at: datetime,
    last_anomaly_bucket: datetime,
) -> AlertRuleState:
    state = AlertRuleState(
        id=uuid.uuid4(),
        rule_id=rule.id,
        scan_config_id=config.id,
        scope_type="event",
        scope_ref=str(event.id),
        is_active=False,
        opened_at=last_notified_at,
        closed_at=last_notified_at,
        last_anomaly_bucket=last_anomaly_bucket,
        last_notified_at=last_notified_at,
    )
    session.add(state)
    return state


def test_reactivation_inside_the_cooldown_reopens_the_state_without_alerting(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Reopening a closed scope is the normal path, so it has to honour the cooldown.

    A volume scope is a candidate for a bounded run of collections per anomaly
    bucket and then closes, so its next anomaly arrives at a CLOSED state — over
    a 24h replay of live data 406 of 436 sends (93%) came in through
    reactivation and none through the cooldown branch. While reactivation
    ignored ``last_notified_at``, cooldown_minutes changed nothing at any value.
    """
    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session, base=_ANOMALY_BASE)
        rule = _seed_alert_rule(session, config)
        session.flush()
        head = _ANOMALY_BASE + timedelta(hours=9)
        _seed_drop_anomaly(session, config, event, bucket=head - timedelta(hours=2))
        _seed_closed_alert_state(
            session,
            rule,
            config,
            event,
            # cooldown_minutes is 1440 on the seeded rule.
            last_notified_at=datetime.now(UTC) - timedelta(hours=1),
            last_anomaly_bucket=head - timedelta(hours=6),
        )
        session.commit()

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        state = session.execute(select(AlertRuleState)).scalar_one()

        assert delivery_ids == []
        assert session.execute(select(AlertDeliveryItem)).scalars().all() == []
        # The scope IS firing again — the state has to say so, or the monitor
        # reads as quiet while it is silenced.
        assert state.is_active is True
        assert state.closed_at is None
        assert to_utc(state.last_anomaly_bucket) == head - timedelta(hours=2)


def test_reactivation_after_the_cooldown_alerts_again(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The case the reactivation branch exists for still fires.

    A scope that genuinely closed and reopens long after has to alert; gating on
    elapsed time rather than the ``is_active`` flag is what keeps that working.
    """
    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session, base=_ANOMALY_BASE)
        rule = _seed_alert_rule(session, config)
        session.flush()
        head = _ANOMALY_BASE + timedelta(hours=9)
        _seed_drop_anomaly(session, config, event, bucket=head - timedelta(hours=2))
        _seed_closed_alert_state(
            session,
            rule,
            config,
            event,
            last_notified_at=datetime.now(UTC) - timedelta(days=3),
            last_anomaly_bucket=head - timedelta(days=3),
        )
        session.commit()

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        item = session.execute(select(AlertDeliveryItem)).scalar_one()

    assert len(delivery_ids) == 1
    assert item.scope_ref == str(event.id)


def _seed_acknowledged_group(
    session: Session,
    rule: AlertRule,
    config: ScanConfig,
    event: Event,
) -> uuid.UUID:
    """Acknowledge the inbox group this scope's drops belong to."""
    from tripl.models.alert_correlation_state import AlertCorrelationState

    group_id = metrics_dispatch._correlation_group_id(
        scan_config_id=config.id,
        rule_id=rule.id,
        scope_type="event",
        scope_ref=str(event.id),
        direction="drop",
    )
    session.add(
        AlertCorrelationState(
            project_id=config.project_id,
            correlation_group_id=group_id,
            status="acknowledged",
        )
    )
    return group_id


def test_ack_survives_a_reactivating_scope(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Acknowledging a scope that keeps firing must not un-acknowledge it.

    A scope's AlertRuleState closes as soon as its anomaly ages past the settled
    head and reopens on the next one, so at the point inbox decisions are
    released the state of a scope firing RIGHT NOW reads closed — on live data
    that is 93% of sends, not an edge case. Releasing on the flag alone would
    clear the acknowledgement the operator just made and page them again on the
    very next collection. Only a scope absent from this run's matches is over.

    Rule-wide release used to make this safe by accident: any other live scope
    of the rule vetoed the reset. Per-scope groups removed that veto.
    """
    from tripl.models.alert_correlation_state import AlertCorrelationState

    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session, base=_ANOMALY_BASE)
        rule = _seed_alert_rule(session, config)
        session.flush()
        head = _ANOMALY_BASE + timedelta(hours=9)
        _seed_drop_anomaly(session, config, event, bucket=head - timedelta(hours=2))
        # Closed state, cooldown long elapsed: without the acknowledgement this
        # run would alert (test_reactivation_after_the_cooldown_alerts_again).
        _seed_closed_alert_state(
            session,
            rule,
            config,
            event,
            last_notified_at=datetime.now(UTC) - timedelta(days=3),
            last_anomaly_bucket=head - timedelta(days=3),
        )
        group_id = _seed_acknowledged_group(session, rule, config, event)
        session.commit()

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.flush()
        state = session.execute(
            select(AlertCorrelationState).where(
                AlertCorrelationState.correlation_group_id == group_id
            )
        ).scalar_one()

        assert state.status == "acknowledged"
        assert delivery_ids == []
        assert session.execute(select(AlertDeliveryItem)).scalars().all() == []


def test_ack_is_released_once_the_scope_stops_firing(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The other half: suppression dies with the incident, or it is permanent.

    Holding the decision for a firing scope must not become never releasing it —
    a scope with nothing to match this run is over, and its next drop is a new
    incident that has to alert.
    """
    from tripl.models.alert_correlation_state import AlertCorrelationState

    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session, base=_ANOMALY_BASE)
        rule = _seed_alert_rule(session, config)
        session.flush()
        head = _ANOMALY_BASE + timedelta(hours=9)
        # No anomaly seeded at all: this scope is quiet on this run.
        _seed_closed_alert_state(
            session,
            rule,
            config,
            event,
            last_notified_at=datetime.now(UTC) - timedelta(days=3),
            last_anomaly_bucket=head - timedelta(days=3),
        )
        group_id = _seed_acknowledged_group(session, rule, config, event)
        session.commit()

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.flush()
        state = session.execute(
            select(AlertCorrelationState).where(
                AlertCorrelationState.correlation_group_id == group_id
            )
        ).scalar_one()

        assert state.status == "open"
        assert delivery_ids == []


def test_telegram_deliveries_are_chunked_below_the_message_ceiling(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Telegram 400s on a body over 4096 chars, and a failed send is retried forever.

    ``last_notified_at`` is stamped only on success while the re-send gate treats
    NULL as "never told them", so the 14-item / 4154-char delivery that surfaced
    this was rebuilt on every collection. Chunking keeps every item — a truncated
    delivery would drop scopes the operator never hears about.
    """
    item_count = 14
    with sync_session_factory() as session:
        config, event_type, _event = _seed_anomaly_scan_state(session)
        _seed_alert_rule(session, config, destination_type="telegram", include_schema_drifts=True)
        for index in range(item_count):
            session.add(
                SchemaDrift(
                    id=uuid.uuid4(),
                    event_type_id=event_type.id,
                    scan_config_id=config.id,
                    field_name=f"payload.f{index}",
                    drift_type="new_field",
                    observed_type="String",
                    declared_type=None,
                    sample_value="x",
                    detected_at=datetime.now(UTC),
                )
            )
        session.commit()

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)

        deliveries = session.execute(select(AlertDelivery)).scalars().all()
        items = session.execute(select(AlertDeliveryItem)).scalars().all()

    assert len(delivery_ids) == 2
    assert sorted(delivery.matched_count for delivery in deliveries) == [6, 8]
    # Nothing is dropped: every drift still reaches the operator, and each chunk
    # stamps last_notified_at for its own items when it lands.
    assert len(items) == item_count
    assert len({item.scope_ref for item in items}) == item_count


def test_non_telegram_channels_keep_one_delivery_per_rule(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Only Telegram has the 4096-char ceiling.

    Chunking jira/linear would file duplicate issues, and Slack/email/webhook
    have no comparable limit, so the ceiling is keyed on the channel.
    """
    with sync_session_factory() as session:
        config, event_type, _event = _seed_anomaly_scan_state(session)
        _seed_alert_rule(session, config, destination_type="slack", include_schema_drifts=True)
        for index in range(14):
            session.add(
                SchemaDrift(
                    id=uuid.uuid4(),
                    event_type_id=event_type.id,
                    scan_config_id=config.id,
                    field_name=f"payload.f{index}",
                    drift_type="new_field",
                    observed_type="String",
                    declared_type=None,
                    sample_value="x",
                    detected_at=datetime.now(UTC),
                )
            )
        session.commit()

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        delivery = session.execute(select(AlertDelivery)).scalar_one()

    assert len(delivery_ids) == 1
    assert delivery.matched_count == 14


def test_breakdown_anomalies_do_not_queue_alert_deliveries(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session)
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=config.project_id,
            type="slack",
            name="Main Slack",
            enabled=True,
            webhook_url_encrypted="secret",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Main Rule",
            enabled=True,
            include_project_total=True,
            include_event_types=True,
            include_events=True,
            notify_on_spike=True,
            notify_on_drop=True,
            min_percent_delta=0,
            min_absolute_delta=0,
            min_expected_count=0,
            cooldown_minutes=1440,
        )
        session.add_all([destination, rule])
        session.add(
            MetricBreakdownAnomaly(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                scope_type="event",
                scope_ref=str(event.id),
                event_id=event.id,
                event_type_id=None,
                bucket=datetime(2026, 1, 1, 11),
                breakdown_column="country",
                breakdown_value="US",
                is_other=False,
                actual_count=0,
                expected_count=10,
                stddev=1,
                z_score=-10,
                direction="drop",
            )
        )
        session.commit()

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)

        assert delivery_ids == []
        assert session.execute(select(AlertDelivery)).scalars().all() == []


def test_schema_drifts_queue_alert_deliveries(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, event_type, _event = _seed_anomaly_scan_state(session)
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=config.project_id,
            type="slack",
            name="Main Slack",
            enabled=True,
            webhook_url_encrypted="secret",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Schema Rule",
            enabled=True,
            include_project_total=False,
            include_event_types=False,
            include_events=False,
            include_schema_drifts=True,
            notify_on_spike=True,
            notify_on_drop=False,
            min_percent_delta=999,
            min_absolute_delta=999,
            min_expected_count=999,
            cooldown_minutes=1440,
        )
        drift = SchemaDrift(
            id=uuid.uuid4(),
            event_type_id=event_type.id,
            scan_config_id=config.id,
            field_name="payload.extra",
            drift_type="new_field",
            observed_type="String",
            declared_type=None,
            sample_value="TASK-123",
            detected_at=datetime.now(UTC),
        )
        session.add_all([destination, rule, drift])
        session.commit()

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)

        assert len(delivery_ids) == 1
        delivery = session.execute(select(AlertDelivery)).scalar_one()
        item = session.execute(select(AlertDeliveryItem)).scalar_one()
        assert delivery.matched_count == 1
        assert item.scope_type == "schema"
        assert item.scope_ref == str(drift.id)
        assert item.scope_name == f"{event_type.display_name}.payload.extra"
        assert item.drift_field == "payload.extra"
        assert item.drift_type == "new_field"
        assert item.sample_value == "TASK-123"


def test_distribution_drifts_queue_alert_deliveries(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, event_type, _event = _seed_anomaly_scan_state(session)
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=config.project_id,
            type="slack",
            name="Main Slack",
            enabled=True,
            webhook_url_encrypted="secret",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Distribution Rule",
            enabled=True,
            include_project_total=False,
            include_event_types=False,
            include_events=False,
            include_schema_drifts=False,
            include_distribution_drifts=True,
            notify_on_spike=True,
            notify_on_drop=False,
            min_percent_delta=999,
            min_absolute_delta=999,
            min_expected_count=999,
            cooldown_minutes=1440,
        )
        drift = DistributionDrift(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            event_type_id=event_type.id,
            field_name="platform",
            bucket=datetime(2026, 1, 1, 11),
            psi=0.42,
            band="significant",
            baseline_total=1000,
            current_total=1000,
            top_movers=[
                {
                    "value": "ios",
                    "baseline_share": 0.5,
                    "current_share": 0.9,
                    "contribution": 0.25,
                }
            ],
        )
        session.add_all([destination, rule, drift])
        session.commit()

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)

        assert len(delivery_ids) == 1
        delivery = session.execute(select(AlertDelivery)).scalar_one()
        item = session.execute(select(AlertDeliveryItem)).scalar_one()
        assert delivery.matched_count == 1
        assert item.scope_type == "distribution"
        assert item.scope_name == f"{event_type.display_name}.platform"
        assert item.drift_field == "platform"
        assert item.drift_type == "distribution_shift"
        assert item.sample_value is not None
        assert "psi=0.420" in item.sample_value


def test_release_regressions_queue_alert_deliveries(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session)
        config.app_version_column = "app_version"
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=config.project_id,
            type="slack",
            name="Main Slack",
            enabled=True,
            webhook_url_encrypted="secret",
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Release Rule",
            enabled=True,
            include_project_total=False,
            include_event_types=False,
            # Generic event anomalies are off — only the dedicated regression
            # toggle should drive this delivery.
            include_events=False,
            include_schema_drifts=False,
            include_distribution_drifts=False,
            include_release_regressions=True,
            notify_on_spike=False,
            notify_on_drop=True,
            # High numeric thresholds prove they are skipped for regressions.
            min_percent_delta=999,
            min_absolute_delta=999,
            min_expected_count=999,
            cooldown_minutes=1440,
        )
        regression = ReleaseRegression(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            scope_type="event",
            scope_ref=str(event.id),
            event_id=event.id,
            event_type_id=None,
            app_version_column="app_version",
            version="2.1.0",
            previous_version="2.0.0",
            kind="missing",
            observed_count=0,
            expected_count=200.0,
            ratio=0.0,
            share_prev=0.1,
            share_new=0.0,
            release_share=0.33,
            window_from=datetime(2026, 1, 1, 10),
            window_to=datetime(2026, 1, 1, 11),
        )
        session.add_all([destination, rule, regression])
        session.commit()
        event_name = event.name

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)

        assert len(delivery_ids) == 1
        delivery = session.execute(select(AlertDelivery)).scalar_one()
        item = session.execute(select(AlertDeliveryItem)).scalar_one()
        assert delivery.matched_count == 1
        assert item.scope_type == "release_regression"
        assert item.scope_name == event_name  # resolved to the event, not a UUID
        assert item.direction == "drop"
        assert item.drift_field == "2.1.0"
        assert item.drift_type == "missing"
        assert item.sample_value == "2.0.0"
        assert item.actual_count == 0
        assert item.expected_count == 200


def test_release_regression_candidates_inert_without_version_column(
    sync_session_factory: sessionmaker[Session],
) -> None:
    from tripl.worker.tasks.metrics.signals import _get_active_release_regression_candidates

    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session)
        # No app_version_column: a stray row must never surface as a candidate.
        session.add(
            ReleaseRegression(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                scope_type="event",
                scope_ref=str(event.id),
                event_id=event.id,
                event_type_id=None,
                app_version_column="app_version",
                version="2.1.0",
                previous_version="2.0.0",
                kind="missing",
                observed_count=0,
                expected_count=200.0,
                ratio=0.0,
                share_prev=0.1,
                share_new=0.0,
                release_share=0.33,
                window_from=datetime(2026, 1, 1, 10),
                window_to=datetime(2026, 1, 1, 11),
            )
        )
        session.commit()
        assert _get_active_release_regression_candidates(session, config) == {}


def test_bump_event_last_seen_is_monotonic_and_ignores_zero(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session)
        event_id = event.id

        earlier = datetime(2026, 5, 1, 10, tzinfo=UTC)
        later = datetime(2026, 5, 1, 12, tzinfo=UTC)

        def _current_last_seen() -> datetime | None:
            session.expire_all()
            row = session.get(Event, event_id)
            assert row is not None
            value = row.last_seen_at
            # SQLite (test backend) drops tzinfo on read; normalize for compare.
            if value is not None and value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value

        # First bump moves NULL → later.
        metrics_collect._bump_event_last_seen(
            session,
            event_agg={(config.id, event_id, later): 5},
        )
        session.commit()
        assert _current_last_seen() == later

        # A second bump with an EARLIER bucket must NOT rewind the column.
        metrics_collect._bump_event_last_seen(
            session,
            event_agg={(config.id, event_id, earlier): 5},
        )
        session.commit()
        assert _current_last_seen() == later

        # Zero-count buckets are ignored even if the bucket is newer.
        even_later = datetime(2026, 5, 2, 0, tzinfo=UTC)
        metrics_collect._bump_event_last_seen(
            session,
            event_agg={(config.id, event_id, even_later): 0},
        )
        session.commit()
        assert _current_last_seen() == later


def test_bump_event_last_seen_promotes_implemented_to_live(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """An 'implemented' event that receives fresh data is promoted to 'live'
    and an EventChange row (user_id=None) is written for the transition."""
    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session)
        # _seed_anomaly_scan_state creates the event with status='implemented'
        assert event.status == "implemented"
        event_id = event.id
        bucket = datetime(2026, 5, 1, 12, tzinfo=UTC)

        metrics_collect._bump_event_last_seen(
            session,
            event_agg={(config.id, event_id, bucket): 7},
        )
        session.commit()

        session.expire_all()
        refreshed = session.get(Event, event_id)
        assert refreshed is not None
        assert refreshed.status == EventStatus.live

        changes = (
            session.execute(select(EventChange).where(EventChange.event_id == event_id))
            .scalars()
            .all()
        )
        assert len(changes) == 1
        assert changes[0].user_id is None
        assert changes[0].field == "status"
        assert changes[0].old_value == EventStatus.implemented
        assert changes[0].new_value == EventStatus.live


def test_bump_event_last_seen_does_not_duplicate_live_transition(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Bumping an already-'live' event must not write an extra EventChange row."""
    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session)
        event_id = event.id
        bucket1 = datetime(2026, 5, 1, 12, tzinfo=UTC)
        bucket2 = datetime(2026, 5, 1, 13, tzinfo=UTC)

        # First bump: implemented → live + one EventChange row.
        metrics_collect._bump_event_last_seen(
            session,
            event_agg={(config.id, event_id, bucket1): 5},
        )
        session.commit()

        # Second bump on the same already-live event: no extra row.
        metrics_collect._bump_event_last_seen(
            session,
            event_agg={(config.id, event_id, bucket2): 3},
        )
        session.commit()

        changes = (
            session.execute(select(EventChange).where(EventChange.event_id == event_id))
            .scalars()
            .all()
        )
        assert len(changes) == 1  # still exactly one row from the first bump


def _make_event_type_with_fields(
    session: Session,
    config: ScanConfig,
    *,
    fields: list[tuple[str, str]],
) -> EventType:
    from tripl.models.field_definition import FieldDefinition

    et = EventType(
        id=uuid.uuid4(),
        project_id=config.project_id,
        name="drift_subject",
        display_name="Drift Subject",
        description="",
    )
    session.add(et)
    session.flush()
    for name, field_type in fields:
        session.add(
            FieldDefinition(
                id=uuid.uuid4(),
                event_type_id=et.id,
                name=name,
                display_name=name,
                field_type=field_type,
                is_required=False,
                description="",
            )
        )
    session.flush()
    session.refresh(et)
    return et


def test_diff_event_type_schema_detects_three_drift_kinds(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        et = _make_event_type_with_fields(
            session,
            config,
            fields=[
                # Both auto-create types — type_changed only fires within {string,json}.
                ("payload", "string"),
                ("user_id", "string"),
                # User-curated type: must NOT trip type_changed even if observed mismatches.
                ("amount", "number"),
                # Declared but no longer observed → missing_field.
                ("legacy", "string"),
            ],
        )
        columns = [
            # Same column, but now CH reports JSON → type_changed (string → json).
            ColumnInfo(name="payload", type_name="JSON"),
            # Unchanged.
            ColumnInfo(name="user_id", type_name="String"),
            # User-curated amount: CH still says String — must NOT drift.
            ColumnInfo(name="amount", type_name="String"),
            # Not declared yet → new_field.
            ColumnInfo(name="device_id", type_name="String"),
            # In skip set — ignored entirely.
            ColumnInfo(name="time", type_name="DateTime"),
        ]

        drift_items = metrics_schema_drift._diff_event_type_schema(
            et,
            columns,
            skip_columns={"time"},
        )

        triples = sorted((item["field_name"], item["drift_type"]) for item in drift_items)
        assert triples == [
            ("device_id", "new_field"),
            ("legacy", "missing_field"),
            ("payload", "type_changed"),
        ]

        metrics_schema_drift._upsert_schema_drifts(
            session,
            event_type_id=et.id,
            scan_config_id=config.id,
            drift_items=drift_items,
        )
        session.commit()

        from tripl.models.schema_drift import SchemaDrift

        rows = (
            session.execute(select(SchemaDrift).where(SchemaDrift.event_type_id == et.id))
            .scalars()
            .all()
        )
        assert len(rows) == 3
        first_detected_at = {row.field_name: row.detected_at for row in rows}

        # Re-running the diff/upsert must be idempotent (unique constraint
        # collapses duplicates onto detected_at refresh).
        metrics_schema_drift._upsert_schema_drifts(
            session,
            event_type_id=et.id,
            scan_config_id=config.id,
            drift_items=drift_items,
        )
        session.commit()
        # The factory is expire_on_commit=False, so the identity map would hand
        # back the pre-conflict attribute values without this.
        session.expire_all()
        rows = (
            session.execute(select(SchemaDrift).where(SchemaDrift.event_type_id == et.id))
            .scalars()
            .all()
        )
        assert len(rows) == 3
        # What the ON CONFLICT branch actually assigns: the provenance every
        # consumer filters on survives the re-upsert, and detected_at moves on so
        # the drift reads as seen again rather than as stale.
        assert {row.scan_config_id for row in rows} == {config.id}
        assert all(row.detected_at > first_detected_at[row.field_name] for row in rows)


def test_diff_event_type_schema_ignores_columns_this_event_type_never_fills(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A grouped scan hands every event type the whole table's column list.

    The cardinality results it passes alongside are already scoped to that
    type's rows, so a column with count 0 held nothing here — reporting it as
    `new_field` says "your plan is missing a field" about a column this event
    does not use. The demo produced ~24 such rows per scan (tripl-jfm3.57).

    An undeclared column that DOES carry data is a genuine plan gap and must
    still drift, which is the other half of this test.
    """
    from tripl.core.analyzers.cardinality import CardinalityResult

    with sync_session_factory() as session:
        config = _create_scan_config(session)
        et = _make_event_type_with_fields(session, config, fields=[("screen_name", "string")])
        columns = [
            ColumnInfo(name="screen_name", type_name="String"),
            # Belongs to a different event type in the same flat table: always
            # NULL for these rows.
            ColumnInfo(name="amount", type_name="Float64"),
            # Undeclared AND populated — a real gap.
            ColumnInfo(name="device_id", type_name="String"),
        ]
        results = {
            "screen_name": CardinalityResult(
                column=columns[0], is_low=True, count=3, sample_values=["home"]
            ),
            # count excludes NULLs, so 0 == "no value in any row of this group".
            "amount": CardinalityResult(column=columns[1], is_low=True, count=0, sample_values=[]),
            "device_id": CardinalityResult(
                column=columns[2], is_low=True, count=2, sample_values=["ios-42"]
            ),
        }

        drift_items = metrics_schema_drift._diff_event_type_schema(
            et,
            columns,
            skip_columns=set(),
            cardinality_results=results,
        )

        reported = sorted((item["field_name"], item["drift_type"]) for item in drift_items)
        assert reported == [("device_id", "new_field")]


def test_diff_event_type_schema_still_reports_when_it_has_no_cardinality_evidence(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """No results passed means no evidence — report as before rather than guess.

    Pins that the emptiness filter never silences a caller that simply does not
    supply cardinality (the ungrouped path).
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        et = _make_event_type_with_fields(session, config, fields=[("screen_name", "string")])
        columns = [
            ColumnInfo(name="screen_name", type_name="String"),
            ColumnInfo(name="amount", type_name="Float64"),
        ]

        drift_items = metrics_schema_drift._diff_event_type_schema(et, columns, skip_columns=set())

        assert sorted(item["field_name"] for item in drift_items) == ["amount"]


def test_diff_event_type_schema_does_not_call_a_reserved_column_missing(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A declared field whose column is RESERVED must not read as "it vanished".

    Regression from tripl-jfm3.57. Reserving event-group-rule columns removed
    them from `observed`, and the missing_field branch then reported every
    declared field of the same name — production groups on `action` AND declares
    `action` on the same event type, so the first scan after deploy raised a
    false "missing_field action" alert. Reserved means "not catalog-managed",
    which is neither new nor missing.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        et = _make_event_type_with_fields(
            session,
            config,
            fields=[("action", "string"), ("really_gone", "string")],
        )
        columns = [ColumnInfo(name="screen_name", type_name="String")]

        drift_items = metrics_schema_drift._diff_event_type_schema(
            et,
            columns,
            # What reserved_catalog_columns now yields for a grouped scan.
            skip_columns={"action", "event_time"},
        )

        reported = sorted((item["field_name"], item["drift_type"]) for item in drift_items)
        # `screen_name` is genuinely undeclared, `really_gone` genuinely vanished,
        # and `action` is neither.
        assert reported == [("really_gone", "missing_field"), ("screen_name", "new_field")]


def test_diff_event_type_schema_attaches_sample_value(
    sync_session_factory: sessionmaker[Session],
) -> None:
    from tripl.core.analyzers.cardinality import CardinalityResult

    with sync_session_factory() as session:
        config = _create_scan_config(session)
        et = _make_event_type_with_fields(
            session,
            config,
            fields=[("payload", "string")],  # declared as string, observed as JSON
        )
        columns = [
            ColumnInfo(name="payload", type_name="JSON"),
            ColumnInfo(name="device_id", type_name="String"),
        ]
        results = {
            "payload": CardinalityResult(
                column=columns[0],
                is_low=True,
                count=1,
                sample_values=['{"k": 1}', '{"k": 2}'],
            ),
            "device_id": CardinalityResult(
                column=columns[1],
                is_low=False,
                count=100,
                sample_values=["", "ios-42", "android-7"],
            ),
        }

        drift_items = metrics_schema_drift._diff_event_type_schema(
            et,
            columns,
            skip_columns=set(),
            cardinality_results=results,
        )
        by_kind = {(item["field_name"], item["drift_type"]): item for item in drift_items}

        # new_field with a sample — the first empty string in sample_values is skipped.
        assert by_kind[("device_id", "new_field")]["sample_value"] == "ios-42"
        # type_changed pulls the first non-empty sample for the same column.
        assert by_kind[("payload", "type_changed")]["sample_value"] == '{"k": 1}'

        metrics_schema_drift._upsert_schema_drifts(
            session,
            event_type_id=et.id,
            scan_config_id=config.id,
            drift_items=drift_items,
        )
        session.commit()

        persisted = (
            session.execute(select(SchemaDrift).where(SchemaDrift.event_type_id == et.id))
            .scalars()
            .all()
        )
        by_persisted_kind = {(d.field_name, d.drift_type): d for d in persisted}
        assert by_persisted_kind[("device_id", "new_field")].sample_value == "ios-42"
        assert by_persisted_kind[("payload", "type_changed")].sample_value == '{"k": 1}'


def test_field_contract_violations_are_upserted_as_schema_drifts(
    sync_session_factory: sessionmaker[Session],
) -> None:
    class FakeContractAdapter:
        def __init__(self) -> None:
            self.expectation_types: list[str] = []
            self.group_value: str | None = None

        def validate_field_contracts(self, base_query, expectations, **kwargs):
            self.expectation_types = [item.drift_type for item in expectations]
            self.group_value = kwargs["group_value"]
            return [
                FieldContractViolation(
                    field_name="status",
                    drift_type="enum_violation",
                    bad_count=2,
                    total_count=10,
                    bad_rate=0.2,
                    threshold=0.0,
                    sample_value="beta",
                ),
                FieldContractViolation(
                    field_name="user_id",
                    drift_type="required_null_violation",
                    bad_count=3,
                    total_count=10,
                    bad_rate=0.3,
                    threshold=0.1,
                    sample_value="<NULL>",
                ),
                FieldContractViolation(
                    field_name="sku",
                    drift_type="regex_violation",
                    bad_count=1,
                    total_count=10,
                    bad_rate=0.1,
                    threshold=0.0,
                    sample_value="bad",
                ),
                FieldContractViolation(
                    field_name="amount",
                    drift_type="range_violation",
                    bad_count=1,
                    total_count=10,
                    bad_rate=0.1,
                    threshold=0.05,
                    sample_value="999",
                ),
            ]

    with sync_session_factory() as session:
        config = _create_scan_config(session)
        et = _make_event_type_with_fields(
            session,
            config,
            fields=[
                ("status", "enum"),
                ("user_id", "string"),
                ("sku", "string"),
                ("amount", "number"),
                ("ignored", "string"),
            ],
        )
        by_name = {field.name: field for field in et.field_definitions}
        by_name["status"].enum_options = ["active"]
        by_name["user_id"].is_required = True
        by_name["user_id"].contract_required_max_null_rate = 0.1
        by_name["sku"].contract_regex = r"^sku-\d+$"
        by_name["amount"].contract_min_value = 0
        by_name["amount"].contract_max_value = 100
        by_name["amount"].contract_max_bad_rate = 0.05
        session.commit()

        adapter = FakeContractAdapter()
        outcome = metrics_schema_drift._detect_field_contract_violations(
            session,
            adapter=adapter,
            event_type=et,
            base_query=config.base_query,
            columns=[
                ColumnInfo(name="status", type_name="String"),
                ColumnInfo(name="user_id", type_name="String"),
                ColumnInfo(name="sku", type_name="String"),
                ColumnInfo(name="amount", type_name="Float64"),
                ColumnInfo(name="time", type_name="DateTime"),
            ],
            skip_columns={"time"},
            scan_config_id=config.id,
            time_column="time",
            time_from=datetime(2026, 1, 1, tzinfo=UTC),
            time_to=datetime(2026, 1, 2, tzinfo=UTC),
            group_column="event_type",
            group_value="purchase",
        )
        session.commit()

        # Two numbers now, not one: a contract that could not be EVALUATED used
        # to report the same 0 as a contract that was evaluated and met, so the
        # count alone could not tell "nothing is wrong" from "nothing was
        # checked". Assert both, or this stops pinning the distinction.
        assert outcome.violations_detected == 4
        assert outcome.checks_failed == 0
        assert adapter.group_value == "purchase"
        assert sorted(adapter.expectation_types) == [
            "enum_violation",
            "range_violation",
            "regex_violation",
            "required_null_violation",
        ]

        rows = (
            session.execute(select(SchemaDrift).where(SchemaDrift.event_type_id == et.id))
            .scalars()
            .all()
        )
        by_kind = {(row.field_name, row.drift_type): row for row in rows}
        assert by_kind[("status", "enum_violation")].declared_type == "enum"
        assert "bad_rate=20.00%" in by_kind[("status", "enum_violation")].observed_type
        assert by_kind[("user_id", "required_null_violation")].sample_value == "<NULL>"
        assert by_kind[("sku", "regex_violation")].sample_value == "bad"
        assert by_kind[("amount", "range_violation")].sample_value == "999"


def test_cleanup_schema_drifts_prunes_only_expired_rows(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    from tripl.services.schema_drift_service import DRIFT_RETENTION_DAYS
    from tripl.worker.tasks import maintenance

    with sync_session_factory() as session:
        config = _create_scan_config(session)
        et = _make_event_type_with_fields(session, config, fields=[("x", "string")])

        now = datetime.now(UTC)
        fresh = SchemaDrift(
            id=uuid.uuid4(),
            event_type_id=et.id,
            scan_config_id=config.id,
            field_name="fresh",
            drift_type="new_field",
            observed_type="String",
            declared_type=None,
            sample_value="hi",
            detected_at=now - timedelta(days=1),
        )
        stale = SchemaDrift(
            id=uuid.uuid4(),
            event_type_id=et.id,
            scan_config_id=config.id,
            field_name="stale",
            drift_type="new_field",
            observed_type="String",
            declared_type=None,
            sample_value="bye",
            detected_at=now - timedelta(days=DRIFT_RETENTION_DAYS + 5),
        )
        session.add_all([fresh, stale])
        session.commit()

    monkeypatch.setattr(maintenance, "_get_sync_session", sync_session_factory)

    result = maintenance.cleanup_schema_drifts.run()
    assert result["deleted"] == 1

    with sync_session_factory() as session:
        rows = session.execute(select(SchemaDrift)).scalars().all()
        assert [r.field_name for r in rows] == ["fresh"]


def test_iter_window_chunks_splits_by_interval() -> None:
    start = datetime(2026, 1, 1, 8)
    end = datetime(2026, 1, 1, 11)
    hour = timedelta(hours=1)

    # No chunk code → single whole-window pass (legacy behavior).
    assert metrics._iter_window_chunks(
        start, end, interval_delta=hour, chunk_interval_code=None
    ) == [(start, end)]

    # chunk == interval → one bucket per chunk.
    assert metrics._iter_window_chunks(
        start, end, interval_delta=hour, chunk_interval_code="1h"
    ) == [
        (datetime(2026, 1, 1, 8), datetime(2026, 1, 1, 9)),
        (datetime(2026, 1, 1, 9), datetime(2026, 1, 1, 10)),
        (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11)),
    ]

    # chunk coarser than interval → many buckets per chunk, trailing chunk clipped.
    assert metrics._iter_window_chunks(
        datetime(2026, 1, 1, 0),
        datetime(2026, 1, 2, 5),
        interval_delta=hour,
        chunk_interval_code="1d",
    ) == [
        (datetime(2026, 1, 1, 0), datetime(2026, 1, 2, 0)),
        (datetime(2026, 1, 2, 0), datetime(2026, 1, 2, 5)),
    ]

    # chunk finer than interval is clamped to one bucket (never below a bucket).
    assert metrics._iter_window_chunks(
        start,
        datetime(2026, 1, 1, 10),
        interval_delta=hour,
        chunk_interval_code="15m",
    ) == [
        (datetime(2026, 1, 1, 8), datetime(2026, 1, 1, 9)),
        (datetime(2026, 1, 1, 9), datetime(2026, 1, 1, 10)),
    ]


def test_collect_metrics_uses_replay_safe_time_limit() -> None:
    assert (
        metrics.collect_metrics.soft_time_limit == metrics.COLLECT_METRICS_SOFT_TIME_LIMIT_SECONDS
    )
    assert metrics.collect_metrics.time_limit == metrics.COLLECT_METRICS_TIME_LIMIT_SECONDS


def test_rabbitmq_consumer_timeout_exceeds_collect_metrics_hard_limit() -> None:
    """The broker must not force-requeue a still-running replay.

    With task_acks_late=True a long metrics replay holds its delivery unacked for
    the whole run (hours). If RabbitMQ's consumer_timeout is shorter than the
    Celery hard task_time_limit, the broker force-closes the channel and requeues
    the live task, spawning overlapping duplicate executions that corrupt chunk
    progress (e.g. 19/24 -> 14/24) and never let the job finish. Keep the broker
    timeout strictly above Celery's hard limit so Celery governs termination.
    """
    repo_root = Path(__file__).resolve().parents[4]
    conf = repo_root / "infra" / "rabbitmq" / "rabbitmq.conf"
    assert conf.is_file(), f"missing broker config at {conf}"

    consumer_timeout_ms: int | None = None
    for raw_line in conf.read_text().splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "consumer_timeout":
            consumer_timeout_ms = int(value.strip())
            break

    assert consumer_timeout_ms is not None, "consumer_timeout not set in rabbitmq.conf"
    assert consumer_timeout_ms > metrics.COLLECT_METRICS_TIME_LIMIT_SECONDS * 1000


def test_collect_metrics_splits_replay_into_interval_chunks(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None
        # 1h chunk over a 1h interval → exactly one bucket per warehouse query.
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
        login_event_id = login_event.id
        event_type_id = config.event_type_id

    counts_by_bucket = {
        datetime(2026, 1, 1, 8): 8,
        datetime(2026, 1, 1, 9): 9,
        datetime(2026, 1, 1, 10): 10,
    }

    class FakeAdapter:
        def __init__(self) -> None:
            self.count_calls: list[tuple[datetime, datetime]] = []
            self.progress_summaries: list[dict[str, object] | None] = []

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
            self.count_calls.append((time_from, time_to))
            with sync_session_factory() as progress_session:
                progress_job = progress_session.get(ScanJob, uuid.UUID(job_id))
                assert progress_job is not None
                self.progress_summaries.append(progress_job.result_summary)
            rows: list[tuple[object, ...]] = [
                (bucket, "Login", count)
                for bucket, count in counts_by_bucket.items()
                if time_from <= bucket < time_to
            ]
            return (["event_name"], [], rows)

        def close(self) -> None:
            return None

    adapter = FakeAdapter()
    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 8), datetime(2026, 1, 1, 11), True),
    )
    monkeypatch.setattr(
        metrics,
        "analyze_cardinality",
        lambda *args, **kwargs: pytest.fail("replay must not run cardinality analysis"),
    )
    monkeypatch.setattr(
        metrics,
        "generate_events",
        lambda *args, **kwargs: pytest.fail("replay must not sync catalog events"),
    )

    result = metrics.collect_metrics.run(config_id, job_id)

    # One bounded warehouse query per 1-hour sub-window, not one giant query.
    assert adapter.count_calls == [
        (datetime(2026, 1, 1, 8), datetime(2026, 1, 1, 9)),
        (datetime(2026, 1, 1, 9), datetime(2026, 1, 1, 10)),
        (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11)),
    ]
    assert result["mode"] == "metrics_replay"
    assert result["catalog_sync_skipped"] is True
    assert result["event_metrics"] == 3
    assert result["type_metrics"] == 3
    assert result["replay_chunks_total"] == 3
    assert result["replay_chunks_completed"] == 3
    assert result["replay_progress_percent"] == 100.0
    assert result["replay_progress_phase"] == "completed"

    completed_chunks = [
        summary and summary["replay_chunks_completed"] for summary in adapter.progress_summaries
    ]
    current_chunks = [
        summary and summary["replay_current_chunk_index"] for summary in adapter.progress_summaries
    ]
    assert completed_chunks == [
        0,
        1,
        2,
    ]
    assert current_chunks == [
        1,
        2,
        3,
    ]

    with sync_session_factory() as session:
        completed_job = session.get(ScanJob, uuid.UUID(job_id))
        assert completed_job is not None
        assert completed_job.result_summary is not None
        assert completed_job.result_summary["replay_chunks_total"] == 3
        assert completed_job.result_summary["replay_chunks_completed"] == 3
        assert completed_job.result_summary["replay_progress_phase"] == "completed"

        login_metrics = (
            session.execute(
                select(EventMetric)
                .where(EventMetric.event_id == login_event_id)
                .order_by(EventMetric.bucket)
            )
            .scalars()
            .all()
        )
        assert [m.bucket for m in login_metrics] == [
            datetime(2026, 1, 1, 8, tzinfo=UTC),
            datetime(2026, 1, 1, 9, tzinfo=UTC),
            datetime(2026, 1, 1, 10, tzinfo=UTC),
        ]
        assert [m.count for m in login_metrics] == [8, 9, 10]
        type_metrics = (
            session.execute(select(EventMetric).where(EventMetric.event_type_id == event_type_id))
            .scalars()
            .all()
        )
        assert len(type_metrics) == 3


def test_collect_metrics_resumes_running_replay_from_completed_chunks(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None
        config.replay_chunk_interval = "1h"
        time_from = datetime(2026, 1, 1, 8)
        time_to = datetime(2026, 1, 1, 11)
        job = ScanJob(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            status=ScanJobStatus.running.value,
            result_summary=metrics._build_replay_progress_summary(
                time_from_dt=time_from,
                time_to_dt=time_to,
                replay_chunk_interval="1h",
                total_chunks=3,
                completed_chunks=1,
                phase="collecting",
            ),
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
        session.add_all(
            [
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=login_event.id,
                    event_type_id=None,
                    bucket=datetime(2026, 1, 1, 8),
                    count=8,
                ),
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=None,
                    event_type_id=config.event_type_id,
                    bucket=datetime(2026, 1, 1, 8),
                    count=8,
                ),
            ]
        )
        session.commit()
        config_id = str(config.id)
        job_id = str(job.id)
        login_event_id = login_event.id

    counts_by_bucket = {
        datetime(2026, 1, 1, 8): 8,
        datetime(2026, 1, 1, 9): 9,
        datetime(2026, 1, 1, 10): 10,
    }

    class FakeAdapter:
        def __init__(self) -> None:
            self.count_calls: list[tuple[datetime, datetime]] = []
            self.progress_summaries: list[dict[str, object] | None] = []

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
            self.count_calls.append((time_from, time_to))
            with sync_session_factory() as progress_session:
                progress_job = progress_session.get(ScanJob, uuid.UUID(job_id))
                assert progress_job is not None
                self.progress_summaries.append(progress_job.result_summary)
            rows: list[tuple[object, ...]] = [
                (bucket, "Login", count)
                for bucket, count in counts_by_bucket.items()
                if time_from <= bucket < time_to
            ]
            return (["event_name"], [], rows)

        def close(self) -> None:
            return None

    adapter = FakeAdapter()
    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (time_from, time_to, True),
    )
    monkeypatch.setattr(
        metrics,
        "analyze_cardinality",
        lambda *args, **kwargs: pytest.fail("replay must not run cardinality analysis"),
    )
    monkeypatch.setattr(
        metrics,
        "generate_events",
        lambda *args, **kwargs: pytest.fail("replay must not sync catalog events"),
    )

    result = metrics.collect_metrics.run(config_id, job_id)

    assert adapter.count_calls == [
        (datetime(2026, 1, 1, 9), datetime(2026, 1, 1, 10)),
        (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11)),
    ]
    completed_chunks = [
        summary and summary["replay_chunks_completed"] for summary in adapter.progress_summaries
    ]
    current_chunks = [
        summary and summary["replay_current_chunk_index"] for summary in adapter.progress_summaries
    ]
    assert completed_chunks == [1, 2]
    assert current_chunks == [2, 3]
    assert result["event_metrics"] == 2
    assert result["type_metrics"] == 2
    assert result["query_rows_scanned"] == 2
    assert result["replay_chunks_completed"] == 3
    assert result["replay_progress_phase"] == "completed"

    with sync_session_factory() as session:
        login_metrics = (
            session.execute(
                select(EventMetric)
                .where(EventMetric.event_id == login_event_id)
                .order_by(EventMetric.bucket)
            )
            .scalars()
            .all()
        )
        assert [(m.bucket, m.count) for m in login_metrics] == [
            (datetime(2026, 1, 1, 8, tzinfo=UTC), 8),
            (datetime(2026, 1, 1, 9, tzinfo=UTC), 9),
            (datetime(2026, 1, 1, 10, tzinfo=UTC), 10),
        ]


def test_replay_appends_low_cardinality_variable_values(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None

        fd_event_name = FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=config.event_type_id,
            name="event_name",
            display_name="Event name",
            field_type="string",
            is_required=False,
            description="",
        )
        fd_user_id = FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=config.event_type_id,
            name="user_id",
            display_name="User ID",
            field_type="string",
            is_required=False,
            description="",
        )
        session.add_all([fd_event_name, fd_user_id])

        event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login | user_id=${user_id}",
            source_name="event_name=Login | user_id=${user_id}",
            description="",
            status="implemented",
        )
        session.add(event)
        session.flush()
        session.add_all(
            [
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=event.id,
                    field_definition_id=fd_event_name.id,
                    value="Login",
                ),
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=event.id,
                    field_definition_id=fd_user_id.id,
                    value="${user_id}",
                ),
            ]
        )

        variable = Variable(
            id=uuid.uuid4(),
            project_id=config.project_id,
            name="user_id",
            source_name="user_id",
            variable_type="string",
            description="",
        )
        session.add(variable)
        session.flush()

        session.add(
            VariableValue(
                id=uuid.uuid4(),
                project_id=config.project_id,
                branch_id=event.branch_id,
                variable_id=variable.id,
                event_id=event.id,
                field_definition_id=fd_user_id.id,
                source_column="user_id",
                value_kind=VariableValueKind.low.value,
                observed_count=1,
                values=["u1"],
            )
        )
        session.commit()
        config_id = str(config.id)

    class FakeAdapter:
        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
                ColumnInfo(name="user_id", type_name="String"),
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
            return (
                ["event_name", "user_id"],
                [],
                [
                    (datetime(2026, 1, 1, 8), "Login", "u2", 3),
                    (datetime(2026, 1, 1, 9), "Login", "u3", 4),
                ],
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: FakeAdapter())
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 8), datetime(2026, 1, 1, 10), True),
    )

    metrics.collect_metrics.run(config_id)

    with sync_session_factory() as session:
        context = session.execute(select(VariableValue)).scalar_one()
        assert context.value_kind == VariableValueKind.low.value
        assert context.values == ["u1", "u2", "u3"]
        assert context.observed_count == 3


def test_replay_does_not_drop_unmatched_metric_rows(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None

        fd_event_name = FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=config.event_type_id,
            name="event_name",
            display_name="Event name",
            field_type="string",
            is_required=False,
            description="",
        )
        session.add(fd_event_name)

        login_event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login",
            source_name="event_name=Login",
            description="",
            status="implemented",
        )
        stale_event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Legacy",
            source_name="event_name=Legacy",
            description="",
            status="implemented",
        )
        session.add_all([login_event, stale_event])
        session.flush()
        session.add_all(
            [
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=login_event.id,
                    field_definition_id=fd_event_name.id,
                    value="Login",
                ),
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=stale_event.id,
                    event_type_id=None,
                    bucket=datetime(2026, 1, 1, 8),
                    count=99,
                ),
            ]
        )
        session.commit()
        config_id = str(config.id)
        login_event_id = login_event.id
        stale_event_id = stale_event.id

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
            return (
                ["event_name"],
                [],
                [(datetime(2026, 1, 1, 8), "Login", 3)],
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: FakeAdapter())
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 8), datetime(2026, 1, 1, 9), True),
    )
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        with sync_session_factory() as session:
            persisted_event = session.get(Event, login_event_id)
            assert persisted_event is not None
            return GenerationResult(
                columns_analyzed=1,
                col_meta={"event_name": {"is_json": False, "is_low": True}},
                events_by_name={"event_name=Login": persisted_event},
            )

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)

    result = metrics.collect_metrics.run(config_id)

    assert result["mode"] == "metrics_replay"
    assert result["event_metrics"] == 1

    with sync_session_factory() as session:
        login_metric = session.execute(
            select(EventMetric).where(EventMetric.event_id == login_event_id)
        ).scalar_one()
        assert login_metric.count == 3

        stale_metric = session.execute(
            select(EventMetric).where(EventMetric.event_id == stale_event_id)
        ).scalar_one_or_none()
        assert stale_metric is not None
        assert stale_metric.count == 99


def test_replay_uses_latest_scan_snapshot(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None

        fd_event_name = FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=config.event_type_id,
            name="event_name",
            display_name="Event name",
            field_type="string",
            is_required=False,
            description="",
        )
        fd_user_id = FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=config.event_type_id,
            name="user_id",
            display_name="User ID",
            field_type="string",
            is_required=False,
            description="",
        )
        session.add_all([fd_event_name, fd_user_id])

        event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login | user_id=${user_id}",
            source_name="event_name=Login | user_id=${user_id}",
            description="",
            status="implemented",
        )
        session.add(event)
        session.flush()
        session.add_all(
            [
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=event.id,
                    field_definition_id=fd_event_name.id,
                    value="Login",
                ),
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=event.id,
                    field_definition_id=fd_user_id.id,
                    value="${user_id}",
                ),
            ]
        )

        snapshot = {
            "version": 1,
            "single_result": {
                "columns_analyzed": 2,
                "details": [],
                "event_type_id": str(config.event_type_id),
                "branch_id": str(event.branch_id) if event.branch_id is not None else None,
                "col_meta": {
                    "event_name": {"is_json": False, "is_low": True},
                    "user_id": {
                        "is_json": False,
                        "is_low": False,
                        "template": "${user_id}",
                    },
                },
                "events": [
                    {
                        "identity": "event_name=Login | user_id=${user_id}",
                        "event_id": str(event.id),
                        "name": event.name,
                        "source_name": event.source_name,
                        "branch_id": str(event.branch_id) if event.branch_id is not None else None,
                        "archived": False,
                        "implemented": True,
                        "reviewed": True,
                        "metric_breakdown_columns": [],
                        "field_values": [
                            {
                                "field_definition_id": str(fd_event_name.id),
                                "value": "Login",
                            },
                            {
                                "field_definition_id": str(fd_user_id.id),
                                "value": "${user_id}",
                            },
                        ],
                    }
                ],
            },
        }

        session.add(
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                status=ScanJobStatus.completed.value,
                completed_at=datetime.now(UTC),
                result_summary={"generation_snapshot": snapshot},
            )
        )
        session.commit()
        config_id = str(config.id)
        event_id = event.id
        user_field_value_id = fd_user_id.id

    class FakeAdapter:
        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
                ColumnInfo(name="user_id", type_name="String"),
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
            return (
                ["event_name", "user_id"],
                [],
                [(datetime(2026, 1, 1, 8), "Login", "u2", 3)],
            )

        def close(self) -> None:
            return None

    with sync_session_factory() as session:
        current_event = session.get(Event, event_id)
        assert current_event is not None
        current_field = session.execute(
            select(EventFieldValue).where(
                EventFieldValue.field_definition_id == user_field_value_id
            )
        ).scalar_one_or_none()
        assert current_field is not None
        current_field.value = "u999"
        session.commit()

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: FakeAdapter())
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 8), datetime(2026, 1, 1, 9), True),
    )

    result = metrics.collect_metrics.run(config_id)
    assert result["mode"] == "metrics_replay"

    with sync_session_factory() as session:
        metric = session.execute(
            select(EventMetric).where(EventMetric.event_id == event_id)
        ).scalar_one()
        assert metric.count == 3


def test_replay_clears_empty_chunk(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None

        fd_event_name = FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=config.event_type_id,
            name="event_name",
            display_name="Event name",
            field_type="string",
            is_required=False,
            description="",
        )
        session.add(fd_event_name)

        stale_event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Legacy",
            source_name="event_name=Legacy",
            description="",
            status="implemented",
        )
        session.add(stale_event)
        session.flush()
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                event_id=stale_event.id,
                event_type_id=None,
                bucket=datetime(2026, 1, 1, 8),
                count=99,
            )
        )
        session.commit()
        config_id = str(config.id)
        stale_event_id = stale_event.id

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
            return (["event_name"], [], [])

        def close(self) -> None:
            return None

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: FakeAdapter())
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 8), datetime(2026, 1, 1, 9), True),
    )
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        return GenerationResult(
            columns_analyzed=1,
            col_meta={"event_name": {"is_json": False, "is_low": True}},
            events_by_name={},
        )

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)

    result = metrics.collect_metrics.run(config_id)

    assert result["mode"] == "metrics_replay"
    assert result["metrics_deleted"] == 1

    with sync_session_factory() as session:
        stale_metric = session.execute(
            select(EventMetric).where(EventMetric.event_id == stale_event_id)
        ).scalar_one_or_none()
        assert stale_metric is None


def test_replay_greedily_adds_json_paths_from_variable_tokens() -> None:
    event = Event(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        event_type_id=uuid.uuid4(),
        name="demo",
    )
    event.field_values = [
        EventFieldValue(
            id=uuid.uuid4(),
            event_id=event.id,
            field_definition_id=uuid.uuid4(),
            value="${payload.user.id} ${payload.user.name} ${flat_token} ${payload.bad-path}",
        )
    ]

    out = metrics._augment_json_value_paths_for_replay_tokens(
        json_value_path_map={"payload": ["existing.path"]},
        json_columns=["payload"],
        replay_events=[event],
        variable_index=VariableIndex(),
    )

    assert out["payload"] == ["existing.path", "user.id", "user.name"]


def _replay_event_with_value(value: str) -> tuple[Event, uuid.UUID]:
    """An unsaved replay event carrying one field value, plus that field's id."""
    event = Event(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        event_type_id=uuid.uuid4(),
        name="demo",
    )
    field_definition_id = uuid.uuid4()
    event.field_values = [
        EventFieldValue(
            id=uuid.uuid4(),
            event_id=event.id,
            field_definition_id=field_definition_id,
            value=value,
        )
    ]
    return event, field_definition_id


def _replay_variable(
    name: str,
    *,
    source_name: str | None = None,
    bindings: list[str] | None = None,
    excluded_from_scans: bool = False,
) -> Variable:
    return Variable(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        name=name,
        source_name=source_name,
        variable_type="string",
        description="",
        bindings=bindings or [],
        excluded_from_scans=excluded_from_scans,
    )


def _shortened_scan_variable(*, excluded_from_scans: bool = False) -> Variable:
    """The shape the scan writes: short display name, raw path on source_name."""
    return _replay_variable(
        "aalter",
        source_name="property.Aalter",
        bindings=["property.Aalter"],
        excluded_from_scans=excluded_from_scans,
    )


def test_replay_adds_json_paths_for_shortened_variable_names() -> None:
    variable = _shortened_scan_variable()
    event, _ = _replay_event_with_value("${aalter}")

    out = metrics._augment_json_value_paths_for_replay_tokens(
        json_value_path_map={},
        json_columns=["property"],
        replay_events=[event],
        variable_index=VariableIndex([variable]),
    )

    assert out["property"] == ["Aalter"]


def test_replay_json_path_admission_still_rejects_a_hostile_source_name() -> None:
    hostile = _replay_variable(
        "hostile",
        source_name="payload.user-id) OR 1=1",
        bindings=["payload.drop; --"],
    )
    event, _ = _replay_event_with_value("${hostile}")

    out = metrics._augment_json_value_paths_for_replay_tokens(
        json_value_path_map={"payload": ["existing.path"]},
        json_columns=["payload"],
        replay_events=[event],
        variable_index=VariableIndex([hostile]),
    )

    assert out["payload"] == ["existing.path"]


def test_replay_json_path_augmentation_caps_paths_per_column() -> None:
    over_cap = metrics_generation._MAX_REPLAY_JSON_PATHS_PER_COLUMN + 5
    event, _ = _replay_event_with_value(
        " ".join(f"${{payload.p{index}}}" for index in range(over_cap))
    )

    out = metrics._augment_json_value_paths_for_replay_tokens(
        json_value_path_map={},
        json_columns=["payload"],
        replay_events=[event],
        variable_index=VariableIndex(),
    )

    assert len(out["payload"]) == metrics_generation._MAX_REPLAY_JSON_PATHS_PER_COLUMN
    assert out["payload"][0] == "p0"
    assert f"p{over_cap - 1}" not in out["payload"]


def test_replay_samples_a_shortened_variable_from_its_json_source_path() -> None:
    variable = _shortened_scan_variable()
    event, field_definition_id = _replay_event_with_value("${aalter}")

    accum: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], dict[str, object]] = {}
    metrics_generation._accumulate_replay_variable_samples(
        accum,
        event=event,
        data_row=("Login", ["Aalter"], "42"),
        reg_index={"event_name": 0},
        n_reg=1,
        n_json=1,
        json_value_names=["property.Aalter"],
        variable_index=VariableIndex([variable]),
    )

    entry = accum[(variable.id, event.id, field_definition_id)]
    assert entry["values"] == ["42"]
    # The row's address, not the token that led to it.
    assert entry["source_column"] == "property.Aalter"


def test_replay_samples_a_dotted_display_name_from_its_regular_source_column() -> None:
    # source_name has no dot, the display name does — the reverse of the scan's
    # own shape, and reachable by hand-editing a variable.
    variable = _replay_variable("payload.user.id", source_name="user_id")
    event, field_definition_id = _replay_event_with_value("${payload.user.id}")

    accum: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], dict[str, object]] = {}
    metrics_generation._accumulate_replay_variable_samples(
        accum,
        event=event,
        data_row=("Login", "u77"),
        reg_index={"event_name": 0, "user_id": 1},
        n_reg=2,
        n_json=0,
        json_value_names=[],
        variable_index=VariableIndex([variable]),
    )

    entry = accum[(variable.id, event.id, field_definition_id)]
    assert entry["values"] == ["u77"]
    assert entry["source_column"] == "user_id"


def test_replay_falls_back_to_a_binding_when_the_source_column_is_absent() -> None:
    variable = _replay_variable(
        "id",
        source_name="payload.retired_id",
        bindings=["payload.user.id"],
    )
    event, field_definition_id = _replay_event_with_value("${id}")

    accum: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], dict[str, object]] = {}
    metrics_generation._accumulate_replay_variable_samples(
        accum,
        event=event,
        data_row=("Login", ["user.id"], "u77"),
        reg_index={"event_name": 0},
        n_reg=1,
        n_json=1,
        json_value_names=["payload.user.id"],
        variable_index=VariableIndex([variable]),
    )

    entry = accum[(variable.id, event.id, field_definition_id)]
    assert entry["values"] == ["u77"]
    assert entry["source_column"] == "payload.user.id"


def test_replay_samples_both_variables_that_share_a_source_token() -> None:
    scanned = _replay_variable("id", source_name="payload.user.id", bindings=["payload.user.id"])
    hand_authored = _replay_variable("user", bindings=["payload.user.id"])
    event, field_definition_id = _replay_event_with_value("${id} and ${user}")

    accum: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], dict[str, object]] = {}
    metrics_generation._accumulate_replay_variable_samples(
        accum,
        event=event,
        data_row=("Login", ["user.id"], "u77"),
        reg_index={"event_name": 0},
        n_reg=1,
        n_json=1,
        json_value_names=["payload.user.id"],
        variable_index=VariableIndex([scanned, hand_authored]),
    )

    # One warehouse path, two variables, two contexts: the accumulator is keyed
    # by variable, so a shared source token is a fan-out, never a collision.
    assert accum[(scanned.id, event.id, field_definition_id)]["values"] == ["u77"]
    assert accum[(hand_authored.id, event.id, field_definition_id)]["values"] == ["u77"]


def test_replay_json_samples_attribute_to_a_shortened_variable() -> None:
    variable = _shortened_scan_variable()
    event, field_definition_id = _replay_event_with_value("${aalter}")

    accum: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], dict[str, object]] = {}
    metrics_generation._accumulate_replay_json_samples_from_events(
        accum,
        events=[event],
        json_path_samples={"property": {"Aalter": ["a", "b"]}},
        variable_index=VariableIndex([variable]),
    )

    entry = accum[(variable.id, event.id, field_definition_id)]
    assert entry["values"] == ["a", "b"]
    assert entry["source_column"] == "property.Aalter"


def test_replay_row_walk_skips_a_variable_excluded_from_scans() -> None:
    """Excluding purges a variable's observed values; replay must not refill them.

    The shortened shape is the whole point: resolving through ``source_name``
    is what put this variable within replay's reach in the first place, so it
    is also the shape that can resurrect what exclusion deleted.
    """
    variable = _shortened_scan_variable(excluded_from_scans=True)
    event, _ = _replay_event_with_value("${aalter}")

    accum: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], dict[str, object]] = {}
    metrics_generation._accumulate_replay_variable_samples(
        accum,
        event=event,
        data_row=("Login", ["Aalter"], "42"),
        reg_index={"event_name": 0},
        n_reg=1,
        n_json=1,
        json_value_names=["property.Aalter"],
        variable_index=VariableIndex([variable]),
    )

    # The accumulator's only effect is this dict, and the merge writes a row for
    # every key in it — so an empty one is "no context created, none updated".
    assert accum == {}


def test_replay_json_samples_skip_a_variable_excluded_from_scans() -> None:
    variable = _shortened_scan_variable(excluded_from_scans=True)
    event, _ = _replay_event_with_value("${aalter}")

    accum: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], dict[str, object]] = {}
    metrics_generation._accumulate_replay_json_samples_from_events(
        accum,
        events=[event],
        json_path_samples={"property": {"Aalter": ["a", "b"]}},
        variable_index=VariableIndex([variable]),
    )

    assert accum == {}


def test_replay_enriches_existing_high_context_values(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None

        fd_event_name = FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=config.event_type_id,
            name="event_name",
            display_name="Event name",
            field_type="string",
            is_required=False,
            description="",
        )
        fd_payload = FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=config.event_type_id,
            name="payload",
            display_name="Payload",
            field_type="json",
            is_required=False,
            description="",
        )
        session.add_all([fd_event_name, fd_payload])

        # The identity below is an arbitrary placeholder, NOT the shape the
        # planner mints: a default name carries one segment per COLUMN
        # (``payload={...}``), never a ``payload.user.id=`` segment per JSON
        # path. Nothing here depends on it — replay enrichment attributes
        # samples through the ``${token}`` in the field value below, never
        # through the event name.
        event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login | payload.user.id=${payload.user.id}",
            source_name="event_name=Login | payload.user.id=${payload.user.id}",
            description="",
            status="implemented",
        )
        session.add(event)
        session.flush()
        session.add_all(
            [
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=event.id,
                    field_definition_id=fd_event_name.id,
                    value="Login",
                ),
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=event.id,
                    field_definition_id=fd_payload.id,
                    value='{"user": {"id": "${payload.user.id}"}}',
                ),
            ]
        )

        variable = Variable(
            id=uuid.uuid4(),
            project_id=config.project_id,
            name="payload.user.id",
            source_name="payload.user.id",
            variable_type="string",
            description="",
        )
        session.add(variable)
        session.flush()

        session.add(
            VariableValue(
                id=uuid.uuid4(),
                project_id=config.project_id,
                branch_id=event.branch_id,
                variable_id=variable.id,
                event_id=event.id,
                field_definition_id=fd_payload.id,
                source_column="payload.user.id",
                value_kind=VariableValueKind.high.value,
                observed_count=0,
                values=[],
            )
        )
        session.commit()
        config_id = str(config.id)

    class FakeAdapter:
        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
                ColumnInfo(name="payload", type_name="JSON"),
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
            return (
                ["event_name", "payload"],
                ["payload.user.id"],
                [
                    (datetime(2026, 1, 1, 8), "Login", ["user.id"], '"u77"', 5),
                ],
            )

        def get_json_path_samples(
            self,
            base_query: str,
            json_columns: list[str],
            *,
            time_column: str | None = None,
            time_from: datetime | None = None,
            time_to: datetime | None = None,
            path_limit: int = 1000,
            sample_limit: int = 3,
            sample_row_limit: int = 1000,
        ) -> dict[str, dict[str, list[object]]]:
            return {"payload": {"user.id": ["u77", "u88"]}}

        def close(self) -> None:
            return None

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: FakeAdapter())
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 8), datetime(2026, 1, 1, 9), True),
    )

    result = metrics.collect_metrics.run(config_id)
    assert result["variable_values_touched"] == 1

    with sync_session_factory() as session:
        context = session.execute(select(VariableValue)).scalar_one()
        assert context.value_kind == VariableValueKind.high.value
        assert context.values == ["u77", "u88"]


def test_replay_enriches_high_context_values_for_a_shortened_variable_name(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The same replay as above, for the population the scan actually creates.

    ``test_replay_enriches_existing_high_context_values`` gives its variable a
    display name EQUAL to its source_name, which is the one shape that never
    needed resolving. Here the name is shortened and the raw path lives on
    ``source_name``/``bindings``, exactly as ``derive_display_name`` writes it —
    so the field value carries ``${id}`` and nothing in the row or the sample
    map is keyed by that (tripl-xv77.3).
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None

        fd_event_name = FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=config.event_type_id,
            name="event_name",
            display_name="Event name",
            field_type="string",
            is_required=False,
            description="",
        )
        fd_payload = FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=config.event_type_id,
            name="payload",
            display_name="Payload",
            field_type="json",
            is_required=False,
            description="",
        )
        session.add_all([fd_event_name, fd_payload])

        # The identity below is an arbitrary placeholder, NOT the shape the
        # planner mints: a default name carries one segment per COLUMN
        # (``payload={...}``), never a ``payload.user.id=`` segment per JSON
        # path. Nothing here depends on it — replay enrichment attributes
        # samples through the ``${token}`` in the field value below, never
        # through the event name.
        event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login | payload.user.id=${id}",
            source_name="event_name=Login | payload.user.id=${id}",
            description="",
            status="implemented",
        )
        session.add(event)
        session.flush()
        session.add_all(
            [
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=event.id,
                    field_definition_id=fd_event_name.id,
                    value="Login",
                ),
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=event.id,
                    field_definition_id=fd_payload.id,
                    value='{"user": {"id": "${id}"}}',
                ),
            ]
        )

        variable = Variable(
            id=uuid.uuid4(),
            project_id=config.project_id,
            name="id",
            source_name="payload.user.id",
            bindings=["payload.user.id"],
            variable_type="string",
            description="",
        )
        session.add(variable)
        session.flush()

        session.add(
            VariableValue(
                id=uuid.uuid4(),
                project_id=config.project_id,
                branch_id=event.branch_id,
                variable_id=variable.id,
                event_id=event.id,
                field_definition_id=fd_payload.id,
                source_column="payload.user.id",
                value_kind=VariableValueKind.high.value,
                observed_count=0,
                values=[],
            )
        )
        session.commit()
        config_id = str(config.id)

    class FakeAdapter:
        seen_json_value_paths: dict[str, list[str]] | None = None

        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
                ColumnInfo(name="payload", type_name="JSON"),
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
            self.seen_json_value_paths = json_value_paths
            return (
                ["event_name", "payload"],
                ["payload.user.id"],
                [
                    (datetime(2026, 1, 1, 8), "Login", ["user.id"], '"u77"', 5),
                ],
            )

        def get_json_path_samples(
            self,
            base_query: str,
            json_columns: list[str],
            *,
            time_column: str | None = None,
            time_from: datetime | None = None,
            time_to: datetime | None = None,
            path_limit: int = 1000,
            sample_limit: int = 3,
            sample_row_limit: int = 1000,
        ) -> dict[str, dict[str, list[object]]]:
            return {"payload": {"user.id": ["u77", "u88"]}}

        def close(self) -> None:
            return None

    adapter = FakeAdapter()
    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 8), datetime(2026, 1, 1, 9), True),
    )

    result = metrics.collect_metrics.run(config_id)
    assert result["variable_values_touched"] == 1

    # The greedy path map has to reach the warehouse keyed by the JSON path the
    # variable lives at; keyed by its display name the query asks for nothing.
    assert (adapter.seen_json_value_paths or {}).get("payload") == ["user.id"]

    with sync_session_factory() as session:
        context = session.execute(select(VariableValue)).scalar_one()
        assert context.value_kind == VariableValueKind.high.value
        assert context.values == ["u77", "u88"]


def _seed_replay_value_context(
    session: Session,
    *,
    value_kind: str,
    values: list[str],
    observed_count: int | None = None,
) -> VariableValue:
    """One stored variable-value context for a direct merge call to land on.

    The merge keys on four columns, but the rows behind them are seeded too so
    the context is addressable the way production addresses it — project and
    branch included, since the branch is part of the lookup.

    ``observed_count`` defaults to the length of ``values``, the consistent case;
    passing it explicitly is how a caller reproduces a row whose stored count and
    stored list disagree, which the scan path can write.
    """
    config = _create_scan_config(session, with_event_type=True)
    assert config.event_type_id is not None

    field_definition = FieldDefinition(
        id=uuid.uuid4(),
        event_type_id=config.event_type_id,
        name="user_id",
        display_name="User ID",
        field_type="string",
        is_required=False,
        description="",
    )
    event = Event(
        id=uuid.uuid4(),
        project_id=config.project_id,
        event_type_id=config.event_type_id,
        name="event_name=Login | user_id=${user_id}",
        description="",
        status="implemented",
    )
    variable = Variable(
        id=uuid.uuid4(),
        project_id=config.project_id,
        name="user_id",
        source_name="user_id",
        variable_type="string",
        description="",
    )
    session.add_all([field_definition, event, variable])
    session.flush()

    context = VariableValue(
        id=uuid.uuid4(),
        project_id=config.project_id,
        branch_id=event.branch_id,
        variable_id=variable.id,
        event_id=event.id,
        field_definition_id=field_definition.id,
        source_column="user_id",
        value_kind=value_kind,
        observed_count=len(values) if observed_count is None else observed_count,
        values=list(values),
    )
    session.add(context)
    session.commit()
    return context


def _merge_replay_values(
    session: Session,
    context: VariableValue,
    *,
    cardinality_threshold: int,
    values: list[str],
) -> int:
    """Merge ``values`` into ``context`` at a threshold the caller chooses.

    Called directly rather than through ``collect_metrics`` because the
    interesting boundary is the threshold, and a scan config's default one is
    far above any sample a fake adapter would hand back.
    """
    key = (context.variable_id, context.event_id, context.field_definition_id)
    return metrics_generation._merge_replay_variable_samples(
        session,
        project_id=context.project_id,
        branch_id=context.branch_id,
        cardinality_threshold=cardinality_threshold,
        accumulated={
            key: {
                "variable_id": context.variable_id,
                "event_id": context.event_id,
                "field_definition_id": context.field_definition_id,
                "source_column": context.source_column,
                "values": values,
            }
        },
    )


def _stored_value_context(factory: sessionmaker[Session]) -> VariableValue:
    """Re-read the merged row, so an in-place edit that never persisted fails."""
    with factory() as session:
        return session.execute(select(VariableValue)).scalar_one()


def test_replay_merge_keeps_a_low_context_whole_past_the_sample_cap(
    sync_session_factory: sessionmaker[Session],
) -> None:
    cap = metrics_generation.VARIABLE_VALUE_SAMPLE_LIMIT
    stored = [f"u{index}" for index in range(cap - 2)]
    arriving = [f"u{index}" for index in range(cap - 2, cap + 2)]

    with sync_session_factory() as session:
        context = _seed_replay_value_context(
            session,
            value_kind=VariableValueKind.low.value,
            values=stored,
        )
        touched = _merge_replay_values(
            session,
            context,
            # Above the sample cap, so a list that outgrows the cap is still
            # inside the threshold — the only arrangement where "low is not
            # sampled" is observable at all.
            cardinality_threshold=cap + 5,
            values=arriving,
        )
        session.commit()

    assert touched == 1
    merged = _stored_value_context(sync_session_factory)
    assert merged.value_kind == VariableValueKind.low.value
    assert merged.values == stored + arriving
    assert merged.observed_count == cap + 2


def test_replay_merge_demotes_a_low_context_that_outgrows_the_threshold(
    sync_session_factory: sessionmaker[Session],
) -> None:
    cap = metrics_generation.VARIABLE_VALUE_SAMPLE_LIMIT
    distinct_total = cap + 5
    arriving = [f"u{index}" for index in range(1, distinct_total)]

    with sync_session_factory() as session:
        context = _seed_replay_value_context(
            session,
            value_kind=VariableValueKind.low.value,
            values=["u0"],
        )
        touched = _merge_replay_values(
            session,
            context,
            cardinality_threshold=3,
            values=arriving,
        )
        session.commit()

    assert touched == 1
    merged = _stored_value_context(sync_session_factory)
    assert merged.value_kind == VariableValueKind.high.value
    assert merged.values == [f"u{index}" for index in range(cap)]
    # What was seen, not what can be shown: the sample cap bounds the list and
    # must not bound the measurement.
    assert merged.observed_count == distinct_total


def test_replay_merge_records_a_demotion_that_changes_no_visible_value(
    sync_session_factory: sessionmaker[Session],
) -> None:
    cap = metrics_generation.VARIABLE_VALUE_SAMPLE_LIMIT
    stored = [f"u{index}" for index in range(cap)]

    with sync_session_factory() as session:
        context = _seed_replay_value_context(
            session,
            value_kind=VariableValueKind.low.value,
            values=stored,
        )
        touched = _merge_replay_values(
            session,
            context,
            # At the threshold the stored list is legitimately low; one more
            # value crosses it, and truncating back to the cap hands over the
            # identical list.
            cardinality_threshold=cap,
            values=["u_late"],
        )
        session.commit()

    assert touched == 1
    merged = _stored_value_context(sync_session_factory)
    assert merged.values == stored
    assert merged.value_kind == VariableValueKind.high.value
    assert merged.observed_count == cap + 1


def test_replay_merge_counts_every_distinct_value_a_high_context_saw(
    sync_session_factory: sessionmaker[Session],
) -> None:
    cap = metrics_generation.VARIABLE_VALUE_SAMPLE_LIMIT
    distinct_total = cap + 5
    arriving = [f"u{index}" for index in range(distinct_total)]

    with sync_session_factory() as session:
        context = _seed_replay_value_context(
            session,
            value_kind=VariableValueKind.high.value,
            values=[],
        )
        touched = _merge_replay_values(
            session,
            context,
            cardinality_threshold=3,
            values=arriving,
        )
        session.commit()

    assert touched == 1
    merged = _stored_value_context(sync_session_factory)
    assert merged.value_kind == VariableValueKind.high.value
    assert merged.values == arriving[:cap]
    assert merged.observed_count == distinct_total


def test_replay_merge_counts_new_values_a_full_high_context_cannot_show(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A row already holding the cap still has to keep counting.

    Once the sample is full the truncated list is identical on every later
    merge, so a write gated only on the values changing never fires again and
    ``observed_count`` freezes at the moment the row filled up — the exact
    number the count-before-truncation split exists to keep honest.
    """
    cap = metrics_generation.VARIABLE_VALUE_SAMPLE_LIMIT
    stored = [f"u{index}" for index in range(cap)]
    arriving = [f"v{index}" for index in range(cap)]

    with sync_session_factory() as session:
        context = _seed_replay_value_context(
            session,
            value_kind=VariableValueKind.high.value,
            values=stored,
        )
        touched = _merge_replay_values(
            session,
            context,
            cardinality_threshold=3,
            values=arriving,
        )
        session.commit()

    assert touched == 1
    merged = _stored_value_context(sync_session_factory)
    # Nothing new can be shown — the sample was already full — and that is
    # precisely why the count is the only evidence the run happened.
    assert merged.values == stored
    assert merged.observed_count == cap * 2


def test_replay_merge_leaves_a_full_high_context_that_saw_nothing_new(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The other half of the pair: ``touched`` counts changes, not visits."""
    cap = metrics_generation.VARIABLE_VALUE_SAMPLE_LIMIT
    stored = [f"u{index}" for index in range(cap)]

    with sync_session_factory() as session:
        context = _seed_replay_value_context(
            session,
            value_kind=VariableValueKind.high.value,
            values=stored,
        )
        touched = _merge_replay_values(
            session,
            context,
            cardinality_threshold=3,
            values=stored[:3],
        )
        session.commit()

    assert touched == 0
    merged = _stored_value_context(sync_session_factory)
    assert merged.values == stored
    assert merged.observed_count == cap


def test_replay_merge_repairs_a_low_context_counting_fewer_than_it_holds(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The low branch's version of the same miss, and the same second reason.

    ``record_variable_contexts`` maxes two observations' counts while unioning
    their values, so a low row can arrive holding more distinct values than it
    claims to have seen. A merge that shows it nothing new changes neither the
    kind nor the list, so only the count can carry the correction.
    """
    stored = ["u0", "u1", "u2"]

    with sync_session_factory() as session:
        context = _seed_replay_value_context(
            session,
            value_kind=VariableValueKind.low.value,
            values=stored,
            observed_count=1,
        )
        touched = _merge_replay_values(
            session,
            context,
            cardinality_threshold=100,
            values=["u0"],
        )
        session.commit()

    assert touched == 1
    merged = _stored_value_context(sync_session_factory)
    assert merged.value_kind == VariableValueKind.low.value
    assert merged.values == stored
    assert merged.observed_count == len(stored)


def test_replay_merge_keeps_a_low_context_sitting_exactly_on_the_threshold(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        context = _seed_replay_value_context(
            session,
            value_kind=VariableValueKind.low.value,
            values=["u1"],
        )
        touched = _merge_replay_values(
            session,
            context,
            cardinality_threshold=3,
            values=["u2", "u3"],
        )
        session.commit()

    assert touched == 1
    merged = _stored_value_context(sync_session_factory)
    # Reaching the threshold is not passing it: a column with exactly this many
    # distinct values is the one the low badge was written for.
    assert merged.value_kind == VariableValueKind.low.value
    assert merged.values == ["u1", "u2", "u3"]
    assert merged.observed_count == 3


def test_collect_metrics_uses_configured_metrics_row_limit(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        config.scan_row_limit = 17
        config.metrics_row_limit = 123
        session.commit()
        config_id = str(config.id)

    class FakeAdapter:
        seen_limit: int | None = None

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
            self.seen_limit = limit
            return (["event_name"], [], [])

        def close(self) -> None:
            return None

    adapter = FakeAdapter()
    seen_scan_limits: list[object] = []
    seen_scan_windows: list[tuple[object, object, object]] = []
    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: adapter)

    def fake_analyze_cardinality(*args: object, **kwargs: object) -> object:
        seen_scan_limits.append(kwargs.get("row_limit"))
        seen_scan_windows.append(
            (kwargs.get("time_column"), kwargs.get("time_from"), kwargs.get("time_to"))
        )
        return object()

    monkeypatch.setattr(metrics, "analyze_cardinality", fake_analyze_cardinality)
    monkeypatch.setattr(metrics, "_prepare_alert_deliveries", lambda *args, **kwargs: [])

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        return GenerationResult(
            columns_analyzed=1,
            col_meta={"event_name": {"is_json": False, "is_low": True}},
            events_by_name={},
        )

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)

    result = metrics.collect_metrics.run(config_id)

    assert seen_scan_limits == [17]
    assert seen_scan_windows[0][0] == "time"
    assert seen_scan_windows[0][1] is not None
    assert seen_scan_windows[0][2] is not None
    assert adapter.seen_limit == 124
    assert result["scan_row_limit"] == 17
    assert result["metrics_row_limit"] == 123
    assert result["query_rows_scanned"] == 0


def test_collect_metrics_allows_query_at_exact_row_limit(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        config.metrics_row_limit = 1
        session.commit()
        config_id = str(config.id)

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
            assert limit == 2
            return (
                ["event_name"],
                [],
                [(datetime(2026, 1, 1, 10), "event_name=Login", 1)],
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: FakeAdapter())
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        return GenerationResult(
            columns_analyzed=1,
            col_meta={"event_name": {"is_json": False, "is_low": True}},
            events_by_name={},
        )

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)

    result = metrics.collect_metrics.run(config_id)

    assert result["query_rows_scanned"] == 1


def test_collect_metrics_fails_when_query_exceeds_row_limit(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A truncated read aborts the chunk AND tells the user what to change.

    The guard raises ``ScanError`` rather than ``ValueError`` so that
    ``user_facing_error`` surfaces the curated text verbatim. As a ``ValueError``
    it fell through to the generic "Scan failed due to an internal error.", which
    left the one actionable instruction — raise ``metrics_row_limit`` — visible
    only in the worker log (tripl-embs).
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        config.metrics_row_limit = 1
        job = ScanJob(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            status=ScanJobStatus.pending.value,
        )
        session.add(job)
        session.commit()
        config_id = str(config.id)
        job_id = str(job.id)

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
            assert limit == 2
            return (
                ["event_name"],
                [],
                [
                    (datetime(2026, 1, 1, 10), "event_name=Login", 1),
                    (datetime(2026, 1, 1, 10), "event_name=Logout", 1),
                ],
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: FakeAdapter())
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        return GenerationResult(
            columns_analyzed=1,
            col_meta={"event_name": {"is_json": False, "is_low": True}},
            events_by_name={},
        )

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)

    with pytest.raises(ScanError, match="Metrics query reached configured row limit") as excinfo:
        metrics.collect_metrics.run(config_id, job_id)

    # The curated message survives the sanitiser instead of being genericised —
    # carrying the prefix the UI matches on, which the sanitiser adds so this
    # raise site does not have to remember it (tripl-7bol). Without the prefix
    # the text reached the browser intact and was discarded there instead, which
    # is the same outcome tripl-embs fixed one layer further down.
    assert user_facing_error(excinfo.value) == f"Scan failed: {excinfo.value}"

    # ...and that is exactly what the user reads off the failed job.
    with sync_session_factory() as session:
        persisted_job = session.get(ScanJob, uuid.UUID(job_id))
    assert persisted_job is not None
    assert persisted_job.status == ScanJobStatus.failed.value
    assert persisted_job.error_message is not None
    assert "Metrics query reached configured row limit (1)" in persisted_job.error_message
    assert "increase metrics_row_limit" in persisted_job.error_message
    assert persisted_job.error_message != "Scan failed due to an internal error."


def test_replace_scope_anomalies_persists_effective_stddev_and_detector_kind(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """MetricAnomaly rows carry the C3 columns sourced from the DetectedAnomaly."""
    from tripl.core.analyzers.anomaly_detector import SCOPE_EVENT, DetectedAnomaly
    from tripl.worker.tasks.metrics.detect import _replace_scope_anomalies

    bucket = datetime(2026, 6, 15, 9, 0, tzinfo=UTC)
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        event_id = uuid.uuid4()
        _replace_scope_anomalies(
            session,
            scan_config_id=config.id,
            scope_type=SCOPE_EVENT,
            scope_ref=str(event_id),
            evaluation_start=bucket,
            evaluation_end=bucket + timedelta(hours=1),
            event_id=event_id,
            event_type_id=None,
            anomalies=[
                DetectedAnomaly(
                    bucket=bucket,
                    actual_count=0,
                    expected_count=10.0,
                    stddev=0.5,
                    z_score=-8.0,
                    direction="drop",
                    effective_stddev=2.5,
                    kind="rolling",
                )
            ],
        )
        session.commit()

        row = session.execute(select(MetricAnomaly)).scalar_one()
        assert row.effective_stddev == 2.5
        assert row.detector_kind == "rolling"


def test_replace_scope_breakdown_anomalies_persists_detector_fields(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """MetricBreakdownAnomaly rows carry the C3 columns from the DetectedAnomaly."""
    from tripl.core.analyzers.anomaly_detector import SCOPE_EVENT, DetectedAnomaly
    from tripl.worker.tasks.metrics.detect import _replace_scope_breakdown_anomalies

    bucket = datetime(2026, 6, 15, 9, 0, tzinfo=UTC)
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        event_id = uuid.uuid4()
        _replace_scope_breakdown_anomalies(
            session,
            scan_config_id=config.id,
            scope_type=SCOPE_EVENT,
            scope_ref=str(event_id),
            breakdown_column="platform",
            breakdown_value="ios",
            is_other=False,
            evaluation_start=bucket,
            evaluation_end=bucket + timedelta(hours=1),
            event_id=event_id,
            event_type_id=None,
            anomalies=[
                DetectedAnomaly(
                    bucket=bucket,
                    actual_count=99,
                    expected_count=10.0,
                    stddev=1.0,
                    z_score=9.0,
                    direction="spike",
                    effective_stddev=3.3,
                    kind="phase",
                )
            ],
        )
        session.commit()

        row = session.execute(select(MetricBreakdownAnomaly)).scalar_one()
        assert row.effective_stddev == 3.3
        assert row.detector_kind == "phase"


def test_platform_share_shift_creates_distinct_parity_anomaly(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A platform share drop is detectable even while total volume is flat."""
    from tripl.worker.tasks.metrics.detect import _recalculate_metric_breakdown_anomalies

    base = datetime(2026, 1, 1, 0, 0)
    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session, base=base)
        config.platform_column = "platform"
        settings = session.execute(
            select(ProjectAnomalySettings).where(
                ProjectAnomalySettings.project_id == config.project_id
            )
        ).scalar_one()
        settings.detect_project_total = False
        settings.detect_event_types = False
        settings.min_history_buckets = 7
        settings.baseline_window_buckets = 7
        settings.sigma_threshold = 3.0

        event_metrics = session.execute(
            select(EventMetric).where(EventMetric.event_id == event.id)
        ).scalars()
        for metric in event_metrics:
            metric.count = 100

        for hour in range(10):
            session.add(
                EventMetricBreakdown(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=event.id,
                    event_type_id=None,
                    bucket=base + timedelta(hours=hour),
                    breakdown_column="platform",
                    breakdown_value="ios",
                    is_other=False,
                    count=10 if hour == 9 else 50,
                )
            )
        session.flush()

        detected = _recalculate_metric_breakdown_anomalies(
            session,
            config,
            evaluation_start=base + timedelta(hours=9),
            evaluation_end=base + timedelta(hours=10),
        )

        rows = (
            session.execute(
                select(MetricBreakdownAnomaly).where(
                    MetricBreakdownAnomaly.breakdown_value == "ios"
                )
            )
            .scalars()
            .all()
        )

        assert detected == 2
        assert {row.kind for row in rows} == {"volume", "parity"}
        parity = next(row for row in rows if row.kind == "parity")
        assert parity.actual_count == pytest.approx(0.1)
        assert parity.expected_count == pytest.approx(0.5)


def test_platform_disappearance_creates_parity_drop_only_when_bucket_is_covered(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A missing numerator means 0% share when collection covered the bucket,
    but remains missing data when that bucket was never collected."""
    from tripl.worker.tasks.metrics.detect import _recalculate_metric_breakdown_anomalies

    base = datetime(2026, 1, 1, 0, 0)
    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session, base=base)
        config.platform_column = "platform"
        settings = session.execute(
            select(ProjectAnomalySettings).where(
                ProjectAnomalySettings.project_id == config.project_id
            )
        ).scalar_one()
        settings.detect_project_total = False
        settings.detect_event_types = False
        settings.min_history_buckets = 7
        settings.baseline_window_buckets = 7
        settings.sigma_threshold = 3.0

        for metric in session.execute(
            select(EventMetric).where(EventMetric.event_id == event.id)
        ).scalars():
            metric.count = 100

        # iOS is 50% for the baseline, then has no numerator row at hour 9.
        for hour in range(9):
            session.add(
                EventMetricBreakdown(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=event.id,
                    event_type_id=None,
                    bucket=base + timedelta(hours=hour),
                    breakdown_column="platform",
                    breakdown_value="ios",
                    is_other=False,
                    count=50,
                )
            )
        session.flush()

        all_covered = {base + timedelta(hours=hour) for hour in range(10)}
        detected = _recalculate_metric_breakdown_anomalies(
            session,
            config,
            evaluation_start=base + timedelta(hours=9),
            evaluation_end=base + timedelta(hours=10),
            covered_buckets=all_covered,
        )
        rows = (
            session.execute(
                select(MetricBreakdownAnomaly).where(
                    MetricBreakdownAnomaly.breakdown_value == "ios"
                )
            )
            .scalars()
            .all()
        )

        assert detected == 2
        parity = next(row for row in rows if row.kind == "parity")
        assert parity.actual_count == 0
        assert parity.expected_count == pytest.approx(0.5)
        session.commit()
        session.expunge_all()
        persisted_config = session.get(ScanConfig, config.id)
        assert persisted_config is not None

        # The same absent numerator must not become a synthetic zero when the
        # collection coverage contract says hour 9 was never observed.
        detected_with_gap = _recalculate_metric_breakdown_anomalies(
            session,
            persisted_config,
            evaluation_start=base + timedelta(hours=9),
            evaluation_end=base + timedelta(hours=10),
            covered_buckets=all_covered - {base + timedelta(hours=9)},
        )
        remaining = (
            session.execute(
                select(MetricBreakdownAnomaly).where(
                    MetricBreakdownAnomaly.breakdown_value == "ios"
                )
            )
            .scalars()
            .all()
        )

        assert detected_with_gap == 0
        assert remaining == []


def test_collect_breakdown_scope_keys_excludes_app_versions(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """App-version series are observational; regular breakdowns stay monitored."""
    from tripl.core.analyzers.anomaly_detector import SCOPE_EVENT
    from tripl.worker.tasks.metrics.detect import _collect_breakdown_scope_keys

    base = datetime(2026, 1, 1, 0, 0)
    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session, base=base)
        config.app_version_column = "app_version"
        for hour in range(10):
            bucket = base + timedelta(hours=hour)
            session.add_all(
                [
                    # Mature release: it must still stay out of the generic detector.
                    EventMetricBreakdown(
                        id=uuid.uuid4(),
                        scan_config_id=config.id,
                        event_id=event.id,
                        event_type_id=None,
                        bucket=bucket,
                        breakdown_column="app_version",
                        breakdown_value="2.0.0",
                        is_other=False,
                        count=100,
                    ),
                    # Immature dev build is excluded for the same semantic reason.
                    EventMetricBreakdown(
                        id=uuid.uuid4(),
                        scan_config_id=config.id,
                        event_id=event.id,
                        event_type_id=None,
                        bucket=bucket,
                        breakdown_column="app_version",
                        breakdown_value="9.9.9",
                        is_other=False,
                        count=1,
                    ),
                    # A different breakdown column must NOT be gated.
                    EventMetricBreakdown(
                        id=uuid.uuid4(),
                        scan_config_id=config.id,
                        event_id=event.id,
                        event_type_id=None,
                        bucket=bucket,
                        breakdown_column="platform",
                        breakdown_value="ios",
                        is_other=False,
                        count=1,
                    ),
                ]
            )
        session.commit()

        keys = _collect_breakdown_scope_keys(
            session,
            scan_config_id=config.id,
            history_from=base,
            evaluation_start=base,
            evaluation_end=base + timedelta(hours=10),
            scope_type=SCOPE_EVENT,
            app_version_column="app_version",
        )

    pairs = {(column, value) for _e, _t, column, value, _o in keys}
    assert ("app_version", "2.0.0") not in pairs
    assert ("app_version", "9.9.9") not in pairs
    assert ("platform", "ios") in pairs  # other columns untouched


def test_recalculate_breakdown_anomalies_purges_app_version_rows(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A recompute removes legacy version markers without disabling real breakdowns."""
    from tripl.core.analyzers.anomaly_detector import SCOPE_EVENT
    from tripl.worker.tasks.metrics.detect import _recalculate_metric_breakdown_anomalies

    base = datetime(2026, 1, 1, 0, 0)
    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session, base=base)
        config.app_version_column = "app_version"
        config.metric_breakdown_columns = ["country"]
        settings = session.execute(
            select(ProjectAnomalySettings).where(
                ProjectAnomalySettings.project_id == config.project_id
            )
        ).scalar_one()
        settings.detect_project_total = False
        settings.detect_event_types = False
        settings.min_history_buckets = 7
        settings.baseline_window_buckets = 7
        settings.sigma_threshold = 3.0

        for hour in range(10):
            count = 1 if hour == 9 else 50
            session.add_all(
                [
                    EventMetricBreakdown(
                        id=uuid.uuid4(),
                        scan_config_id=config.id,
                        event_id=event.id,
                        event_type_id=None,
                        bucket=base + timedelta(hours=hour),
                        breakdown_column="country",
                        breakdown_value="US",
                        is_other=False,
                        count=count,
                    ),
                    EventMetricBreakdown(
                        id=uuid.uuid4(),
                        scan_config_id=config.id,
                        event_id=event.id,
                        event_type_id=None,
                        bucket=base + timedelta(hours=hour),
                        breakdown_column="app_version",
                        breakdown_value="2.0.0",
                        is_other=False,
                        count=count,
                    ),
                ]
            )
        session.add(
            MetricBreakdownAnomaly(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                scope_type=SCOPE_EVENT,
                scope_ref=str(event.id),
                event_id=event.id,
                event_type_id=None,
                bucket=base + timedelta(hours=9),
                breakdown_column="app_version",
                breakdown_value="2.0.0",
                is_other=False,
                actual_count=1,
                expected_count=50,
                stddev=1,
                z_score=-49,
                direction="drop",
            )
        )
        session.flush()

        detected = _recalculate_metric_breakdown_anomalies(
            session,
            config,
            evaluation_start=base + timedelta(hours=9),
            evaluation_end=base + timedelta(hours=10),
        )
        rows = list(
            session.execute(
                select(MetricBreakdownAnomaly).where(
                    MetricBreakdownAnomaly.scan_config_id == config.id,
                    MetricBreakdownAnomaly.scope_type == SCOPE_EVENT,
                )
            ).scalars()
        )

    assert detected == 1
    assert {(row.breakdown_column, row.breakdown_value) for row in rows} == {("country", "US")}


def test_recalculate_metric_anomalies_excludes_uncovered_gap(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A missing bucket inside real coverage flags as a drop, but the same
    missing bucket left OUT of ``covered_buckets`` (a collection gap) is excluded
    instead of zero-filled into a fake drop (C2 / tripl-dmch.16)."""
    from tripl.worker.tasks.metrics.detect import _recalculate_metric_anomalies

    base = _ANOMALY_BASE  # recent so the age-out horizon never fires
    eval_start = base
    eval_end = base + timedelta(hours=11)  # includes the missing hour-10 bucket
    with sync_session_factory() as session:
        config, _event_type, _event = _seed_anomaly_scan_state(session, base=base)

        covered_full = {base + timedelta(hours=h) for h in range(11)}
        flagged = _recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=eval_start,
            evaluation_end=eval_end,
            covered_buckets=covered_full,
        )
        session.commit()
        assert flagged > 0  # the covered gap zero-fills and reads as a drop

        covered_gap = {base + timedelta(hours=h) for h in range(10)}  # hour 10 uncovered
        excluded = _recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=eval_start,
            evaluation_end=eval_end,
            covered_buckets=covered_gap,
        )
        session.commit()
        assert excluded == 0  # collection gap excluded, no fake drop


def test_recalculate_metric_anomalies_trailing_reeval_clears_backfilled_bucket(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A stale flag on a bucket that has since been re-collected with healthy
    data is cleared when the trailing re-eval window covers it (tripl-dmch.14)."""
    from tripl.core.analyzers.anomaly_detector import SCOPE_EVENT
    from tripl.worker.tasks.metrics.detect import _recalculate_metric_anomalies

    base = _ANOMALY_BASE  # recent so the age-out horizon never fires
    stale_bucket = base + timedelta(hours=9)  # has healthy data (count=10) now
    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session, base=base)
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                scope_type=SCOPE_EVENT,
                scope_ref=str(event.id),
                event_id=event.id,
                event_type_id=None,
                bucket=stale_bucket,
                actual_count=0,
                expected_count=10.0,
                stddev=1.0,
                z_score=-10.0,
                direction="drop",
            )
        )
        session.commit()

        _recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=base,
            evaluation_end=base + timedelta(hours=10),
            covered_buckets={base + timedelta(hours=h) for h in range(10)},
        )
        session.commit()

        remaining = (
            session.execute(
                select(MetricAnomaly).where(
                    MetricAnomaly.event_id == event.id,
                    MetricAnomaly.bucket == stale_bucket,
                )
            )
            .scalars()
            .all()
        )
        assert remaining == []  # backfilled bucket re-evaluated → stale flag gone


# --------------------------------------------------------------------------
# An outage announced DOWNSTREAM of its anchor survives the sliding window
# (tripl-l429.16)
# --------------------------------------------------------------------------

# Production geometry: hourly grid, sigma 4.0, min_expected_count 50. The scope
# below runs at ~20/h through the small hours, which is BELOW that gate, so the
# bucket where it stops behaving normally cannot itself be flagged and the
# announcement necessarily lands several buckets later.
_OUTAGE_SIGMA = 4.0
_OUTAGE_MIN_EXPECTED = 50
# 02:00 on day 28. Day 28 leaves a full 504-bucket history (three weekly cycles,
# ``required_history_buckets`` for a 1h grid) in front of the death.
_OUTAGE_DEATH_HOUR = 24 * 28 + 2
_OUTAGE_HORIZON_HOURS = _OUTAGE_DEATH_HOUR + 24
# A whole number of days back from midnight, so the hour offsets below really are
# clock hours; recent enough that the 180-day age-out never fires.
_OUTAGE_BASE = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
    days=30
)


def _quiet_night_count(hour: int) -> int:
    """Dead 00:00-02:00, a thin ~20/h trickle 02:00-06:00, then ~500/h all day."""
    hour_of_day = hour % 24
    if hour_of_day < 2:
        return 0
    if hour_of_day < 6:
        return 20
    return 500


def _seed_dying_quiet_night_event(session: Session, *, base: datetime) -> tuple[ScanConfig, Event]:
    config = _create_scan_config(session, with_event_type=True)
    session.add(
        ProjectAnomalySettings(
            project_id=config.project_id,
            anomaly_detection_enabled=True,
            sigma_threshold=_OUTAGE_SIGMA,
            min_expected_count=_OUTAGE_MIN_EXPECTED,
        )
    )
    assert config.event_type_id is not None
    event = Event(
        id=uuid.uuid4(),
        project_id=config.project_id,
        event_type_id=config.event_type_id,
        name="event_name=Checkout",
        description="",
        status="implemented",
    )
    session.add(event)

    # Only the non-empty buckets are stored; the detector zero-fills the grid,
    # exactly as a real collection leaves an empty hour unrecorded.
    for hour in range(_OUTAGE_DEATH_HOUR):
        count = _quiet_night_count(hour)
        if not count:
            continue
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                event_id=event.id,
                event_type_id=None,
                bucket=base + timedelta(hours=hour),
                count=count,
            )
        )
    session.commit()
    return config, event


def _stored_outage_buckets(session: Session, config: ScanConfig, event: Event) -> list[datetime]:
    from tripl.core.analyzers.anomaly_detector import SCOPE_EVENT

    return sorted(
        session.execute(
            select(MetricAnomaly.bucket).where(
                MetricAnomaly.scan_config_id == config.id,
                MetricAnomaly.scope_type == SCOPE_EVENT,
                MetricAnomaly.scope_ref == str(event.id),
            )
        ).scalars()
    )


def test_outage_row_survives_every_window_position_past_its_anchor(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The outage keeps its ONE row as the evaluation window slides past the anchor.

    ``_collapse_outage_runs`` gates on the anchor — the bucket where the scope
    stopped behaving normally — but announces at the first FLAGGED bucket at or
    after it, which for a scope whose anchor phase sits below
    ``min_expected_count`` is hours later. The evaluation window then advances one
    bucket per collection, so there is always a pass whose window starts AFTER the
    anchor and still CONTAINS the announced row: it declines to announce, and a
    caller that clears its whole window would take the outage's only row with it.
    Nothing would ever write it back — every later pass starts later still — so
    the event would go silent on the page, the badge, the bell and the drilldown.

    Deliberately steps one bucket at a time: the detector-level sibling in
    ``test_anomaly_detector`` jumps a full day past the anchor and skips every
    window position where the deletion actually happens.
    """
    from tripl.worker.tasks.metrics.detect import _recalculate_metric_anomalies

    base = _OUTAGE_BASE
    anchor = base + timedelta(hours=_OUTAGE_DEATH_HOUR)
    horizon = base + timedelta(hours=_OUTAGE_HORIZON_HOURS)

    with sync_session_factory() as session:
        config, event = _seed_dying_quiet_night_event(session, base=base)

        # The pass whose window contains the anchor announces the outage once.
        _recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=anchor,
            evaluation_end=horizon,
        )
        session.commit()
        announced = _stored_outage_buckets(session, config, event)
        assert len(announced) == 1, "the outage is announced exactly once"
        announced_bucket = announced[0]
        # The announcement is DOWNSTREAM of the anchor — the premise of the bug.
        # The anchor's own phase expects ~20/h, under the min_expected_count gate,
        # so no detector path is allowed to flag it.
        assert announced_bucket > anchor

        # Every subsequent collection advances the window by exactly one bucket.
        # Walk it across the anchor..announcement span and one bucket beyond.
        announced_hour = round((announced_bucket - base).total_seconds() / 3600)
        for hour in range(_OUTAGE_DEATH_HOUR + 1, announced_hour + 2):
            _recalculate_metric_anomalies(
                session,
                config,
                evaluation_start=base + timedelta(hours=hour),
                evaluation_end=horizon,
            )
            session.commit()
            assert _stored_outage_buckets(session, config, event) == [announced_bucket], (
                f"the outage lost its only row when the window started at hour {hour} "
                f"(announced at {announced_bucket}, anchor at {anchor})"
            )


def _seed_dying_quiet_night_breakdown(
    session: Session, *, base: datetime
) -> tuple[ScanConfig, EventType]:
    """The breakdown twin of ``_seed_dying_quiet_night_event``.

    Here it is a single ``platform=ios`` slice of an event type that dies, so the
    outage collapse runs over the BREAKDOWN series. Same quiet-night shape and
    same production geometry, so the announcement lands downstream of the anchor
    for the same reason.
    """
    config = _create_scan_config(session, with_event_type=True)
    config.metric_breakdown_columns = ["platform"]
    assert config.event_type_id is not None
    session.add(
        ProjectAnomalySettings(
            project_id=config.project_id,
            anomaly_detection_enabled=True,
            sigma_threshold=_OUTAGE_SIGMA,
            min_expected_count=_OUTAGE_MIN_EXPECTED,
            # Only the event-type breakdown lane is under test. The other two
            # would re-score the very same rows under different scope keys and
            # cost a full seasonal fit per pass for nothing.
            detect_project_total=False,
            detect_events=False,
        )
    )
    event_type = session.get(EventType, config.event_type_id)
    assert event_type is not None

    # Only the non-empty buckets are stored; the detector zero-fills the grid,
    # exactly as a real collection leaves an empty hour unrecorded.
    for hour in range(_OUTAGE_DEATH_HOUR):
        count = _quiet_night_count(hour)
        if not count:
            continue
        session.add(
            EventMetricBreakdown(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                event_id=None,
                event_type_id=event_type.id,
                bucket=base + timedelta(hours=hour),
                breakdown_column="platform",
                breakdown_value="ios",
                is_other=False,
                count=count,
            )
        )
    session.commit()
    return config, event_type


def _stored_breakdown_outage_buckets(
    session: Session, config: ScanConfig, event_type: EventType
) -> list[datetime]:
    from tripl.core.analyzers.anomaly_detector import SCOPE_EVENT_TYPE

    return sorted(
        session.execute(
            select(MetricBreakdownAnomaly.bucket).where(
                MetricBreakdownAnomaly.scan_config_id == config.id,
                MetricBreakdownAnomaly.scope_type == SCOPE_EVENT_TYPE,
                MetricBreakdownAnomaly.scope_ref == str(event_type.id),
                MetricBreakdownAnomaly.breakdown_column == "platform",
                MetricBreakdownAnomaly.breakdown_value == "ios",
            )
        ).scalars()
    )


def test_breakdown_outage_row_survives_every_window_position_past_its_anchor(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A breakdown outage keeps its ONE row as the window slides past the anchor.

    Identical defect to the scope-level twin above, on the second surface that
    persists by DELETE-then-INSERT: a count-shaped breakdown series goes through
    the same ``_collapse_outage_runs``, so its outage is announced once, at a
    bucket downstream of the anchor, and every later pass declines to re-announce
    it. ``_replace_scope_breakdown_anomalies`` used to clear its whole evaluation
    window unconditionally, so the pass whose window starts after the anchor but
    still contains the announced row deleted the platform slice's only marker and
    nothing ever wrote it back.
    """
    from tripl.worker.tasks.metrics.detect import _recalculate_metric_breakdown_anomalies

    base = _OUTAGE_BASE
    anchor = base + timedelta(hours=_OUTAGE_DEATH_HOUR)
    horizon = base + timedelta(hours=_OUTAGE_HORIZON_HOURS)

    with sync_session_factory() as session:
        config, event_type = _seed_dying_quiet_night_breakdown(session, base=base)

        # The pass whose window contains the anchor announces the outage once.
        _recalculate_metric_breakdown_anomalies(
            session,
            config,
            evaluation_start=anchor,
            evaluation_end=horizon,
        )
        session.commit()
        announced = _stored_breakdown_outage_buckets(session, config, event_type)
        assert len(announced) == 1, "the breakdown outage is announced exactly once"
        announced_bucket = announced[0]
        # The announcement is DOWNSTREAM of the anchor — the premise of the bug.
        assert announced_bucket > anchor

        # Every subsequent collection advances the window by exactly one bucket.
        announced_hour = round((announced_bucket - base).total_seconds() / 3600)
        for hour in range(_OUTAGE_DEATH_HOUR + 1, announced_hour + 2):
            _recalculate_metric_breakdown_anomalies(
                session,
                config,
                evaluation_start=base + timedelta(hours=hour),
                evaluation_end=horizon,
            )
            session.commit()
            assert _stored_breakdown_outage_buckets(session, config, event_type) == [
                announced_bucket
            ], (
                f"the breakdown outage lost its only row when the window started at "
                f"hour {hour} (announced at {announced_bucket}, anchor at {anchor})"
            )


def test_recalculate_skips_sub_threshold_scopes_but_ages_out_stale_rows(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """tripl-h353: a scope whose MAX(count) sits below ``min_expected_count`` is
    prefiltered — no history load, no detector call — yet a stale anomaly row
    inside the evaluation window is still cleared via the empty-replace path."""
    from tripl.core.analyzers.anomaly_detector import SCOPE_EVENT
    from tripl.worker.tasks.metrics import detect as metrics_detect

    base = _ANOMALY_BASE  # recent so the age-out horizon never fires
    stale_bucket = base + timedelta(hours=9)
    with sync_session_factory() as session:
        config, event_type, event = _seed_anomaly_scan_state(session, base=base)
        # Raise the floor far above the seeded count=10 series: every event and
        # event-type scope becomes provably silent (2*10=20 < 50).
        settings_row = session.execute(
            select(ProjectAnomalySettings).where(
                ProjectAnomalySettings.project_id == config.project_id
            )
        ).scalar_one()
        settings_row.min_expected_count = 50
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                scope_type=SCOPE_EVENT,
                scope_ref=str(event.id),
                event_id=event.id,
                event_type_id=None,
                bucket=stale_bucket,
                actual_count=0,
                expected_count=10.0,
                stddev=1.0,
                z_score=-10.0,
                direction="drop",
            )
        )
        # A second event with a stale anomaly row and NO EventMetric rows at
        # all: reachable only through the anomaly-side union of
        # _collect_scope_ids, so it exercises the .get(id, 0.0) default in the
        # prefilter (max-count map has no entry for it).
        quiet_event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=event_type.id,
            name="event_name=WentQuiet",
            description="",
            status="implemented",
        )
        session.add(quiet_event)
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                scope_type=SCOPE_EVENT,
                scope_ref=str(quiet_event.id),
                event_id=quiet_event.id,
                event_type_id=None,
                bucket=stale_bucket,
                actual_count=0,
                expected_count=10.0,
                stddev=1.0,
                z_score=-10.0,
                direction="drop",
            )
        )
        session.commit()

        detector_calls: list[int] = []
        real_detect = metrics_detect.detect_anomalies

        def counting_detect(*args: object, **kwargs: object) -> object:
            detector_calls.append(1)
            return real_detect(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(metrics_detect, "detect_anomalies", counting_detect)

        detected = metrics_detect._recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=base,
            evaluation_end=base + timedelta(hours=10),
            covered_buckets={base + timedelta(hours=h) for h in range(10)},
        )
        session.commit()

        assert detected == 0
        # Only the project-total rollup reached the detector (it has no
        # prefilter); the event-type and event loops skipped it entirely.
        assert len(detector_calls) == 1
        remaining = (
            session.execute(select(MetricAnomaly).where(MetricAnomaly.scan_config_id == config.id))
            .scalars()
            .all()
        )
        # Both stale flags aged out by the skip path: the sub-threshold scope
        # with live metric rows AND the anomaly-only scope with none.
        assert remaining == []


def test_covered_buckets_from_scan_jobs_unions_completed_windows(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Coverage is the union of every COMPLETED job window and the current run;
    failed/incomplete jobs never mark a bucket as covered."""
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    delta = timedelta(hours=1)
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        session.add(
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                status=ScanJobStatus.completed.value,
                result_summary={
                    "time_from": base.isoformat(),
                    "time_to": (base + delta * 3).isoformat(),
                },
            )
        )
        session.add(
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                status=ScanJobStatus.failed.value,
                result_summary={
                    "time_from": (base + delta * 5).isoformat(),
                    "time_to": (base + delta * 7).isoformat(),
                },
            )
        )
        session.commit()

        # ``history_from`` is required: it is the oldest bucket the caller's
        # detector pass can consult, and both reads are over the config's whole
        # lifetime without it. Both seeded jobs carry a server-defaulted
        # ``created_at`` of now, far above this horizon.
        covered = metrics._covered_buckets_from_scan_jobs(
            session,
            scan_config_id=config.id,
            delta=delta,
            history_from=base - timedelta(days=30),
            current_window=(base + delta * 10, base + delta * 11),
        )
        # The same config asked about as a FOREIGN one: no window was claimed,
        # so ``presence_before`` bounds the stored-bucket read on its own.
        foreign_covered = metrics._covered_buckets_from_scan_jobs(
            session,
            scan_config_id=config.id,
            delta=delta,
            history_from=base - timedelta(days=30),
            presence_before=base + delta * 11,
        )

    assert base in covered
    assert base + delta in covered
    assert base + delta * 2 in covered
    assert base + delta * 5 not in covered  # failed job window is not coverage
    assert base + delta * 10 in covered  # the current run's window is always covered
    # No current run to credit, so the bucket it would have written is not covered.
    assert base + delta * 10 not in foreign_covered


def test_recalculate_metric_anomalies_withholds_still_filling_head_of_window(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """tripl-jfm3.7: the collection window ends at the last COMPLETE clock hour,
    but the warehouse is still delivering that hour. With no allowance the
    zero-filled newest bucket is scored and reads as a drop; a 2h ingestion
    allowance holds the newest two buckets back for a later scan to score."""
    from tripl.worker.tasks.metrics.detect import _recalculate_metric_anomalies

    base = _ANOMALY_BASE
    eval_start = base
    eval_end = base + timedelta(hours=11)  # hour 10 has no rows yet
    covered = {base + timedelta(hours=hour) for hour in range(11)}
    with sync_session_factory() as session:
        config, _event_type, _event = _seed_anomaly_scan_state(session, base=base)

        scored_immediately = _recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=eval_start,
            evaluation_end=eval_end,
            covered_buckets=covered,
        )
        session.commit()

        withheld = _recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=eval_start,
            evaluation_end=eval_end,
            covered_buckets=covered,
            settling_delay=timedelta(hours=2),
        )
        session.commit()

        remaining = (
            session.execute(select(MetricAnomaly).where(MetricAnomaly.scan_config_id == config.id))
            .scalars()
            .all()
        )

    assert scored_immediately > 0  # today's behavior: the still-filling bucket flags
    assert withheld == 0
    assert remaining == []  # and the row the unsettled pass wrote is cleared


def test_collect_metrics_applies_the_ingestion_settling_allowance(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The scan orchestrator owns the settling policy: both anomaly recalculation
    entrypoints must receive it, or the newest bucket is scored while the
    warehouse is still filling it (tripl-jfm3.7)."""
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None
        event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login",
            description="",
            status="implemented",
        )
        session.add(event)
        session.commit()
        config_id = str(config.id)
        event_id = event.id

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
            return (["event_name"], [], [(datetime(2026, 1, 1, 10), "Login", 12)])

        def close(self) -> None:
            return None

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: FakeAdapter())
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11), False),
    )
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        with sync_session_factory() as session:
            persisted_event = session.get(Event, event_id)
            assert persisted_event is not None
            return GenerationResult(
                columns_analyzed=1,
                col_meta={"event_name": {"is_json": False, "is_low": True}},
                events_by_name={"event_name=Login": persisted_event},
            )

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)

    seen: dict[str, object] = {}

    def capture(name: str) -> object:
        def _recalculate(*args: object, **kwargs: object) -> int:
            seen[name] = kwargs.get("settling_delay")
            return 0

        return _recalculate

    monkeypatch.setattr(metrics, "_recalculate_metric_anomalies", capture("anomalies"))
    monkeypatch.setattr(metrics, "_recalculate_metric_breakdown_anomalies", capture("breakdown"))

    metrics.collect_metrics.run(config_id)

    assert metrics.ANOMALY_INGESTION_SETTLING.total_seconds() > 0
    assert seen == {
        "anomalies": metrics.ANOMALY_INGESTION_SETTLING,
        "breakdown": metrics.ANOMALY_INGESTION_SETTLING,
    }


def test_grouped_event_type_lookup_is_scoped_to_the_main_plan(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Grouped collection must attribute volume to the MAIN plan's event types.

    A working branch deep-copies event types under the same names. Before
    tripl-jfm3.72 the lookup was keyed by name with no branch filter, so the
    branch copy won the dict: the main-branch series stopped at the last write
    (the detector then read it as "dropped to zero") and buckets carrying rows
    from both branches double-counted.
    """
    from tripl.models.plan_branch import BranchKind, PlanBranch
    from tripl.worker.tasks.metrics.tasks import main_plan_event_types_by_name

    with sync_session_factory() as session:
        config = _create_scan_config(session)
        main = PlanBranch(
            id=uuid.uuid4(),
            project_id=config.project_id,
            name="main",
            kind=BranchKind.main.value,
        )
        feature = PlanBranch(
            id=uuid.uuid4(),
            project_id=config.project_id,
            name="feature/checkout-funnel",
            kind=BranchKind.working.value,
        )
        session.add_all([main, feature])
        session.flush()

        main_type = EventType(
            id=uuid.uuid4(),
            project_id=config.project_id,
            branch_id=main.id,
            name="Purchase",
            display_name="Purchase",
            description="",
        )
        # Same name, different branch — the deep copy a working branch makes.
        branch_type = EventType(
            id=uuid.uuid4(),
            project_id=config.project_id,
            branch_id=feature.id,
            name="Purchase",
            display_name="Purchase",
            description="",
        )
        session.add_all([main_type, branch_type])
        session.commit()

        by_name = main_plan_event_types_by_name(session, config.project_id)

        assert by_name["Purchase"].id == main_type.id
        assert branch_type.id not in {et.id for et in by_name.values()}


def test_ingestion_settling_delay_reads_the_project_setting(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """tripl-jfm3.79: the settling allowance is a per-project knob, not a constant.

    A project with no monitoring settings row keeps the historical two hours;
    once the row exists the scan honours whatever the operator configured,
    including 0 (score every collected bucket immediately).
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session)

        assert (
            metrics._ingestion_settling_delay(session, config.project_id)
            == metrics.ANOMALY_INGESTION_SETTLING
        )

        settings = ProjectAnomalySettings(
            project_id=config.project_id,
            anomaly_detection_enabled=True,
            anomaly_ingestion_settling_minutes=45,
        )
        session.add(settings)
        session.commit()

        assert metrics._ingestion_settling_delay(session, config.project_id) == timedelta(
            minutes=45
        )

        settings.anomaly_ingestion_settling_minutes = 0
        session.commit()

        assert metrics._ingestion_settling_delay(session, config.project_id) == timedelta(0)


def test_collect_metrics_uses_the_projects_configured_settling_allowance(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The orchestrator must hand BOTH recalculation entrypoints the project's
    own allowance, not the module constant (tripl-jfm3.79)."""
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None
        event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login",
            description="",
            status="implemented",
        )
        session.add(event)
        session.add(
            ProjectAnomalySettings(
                project_id=config.project_id,
                anomaly_detection_enabled=True,
                anomaly_ingestion_settling_minutes=15,
            )
        )
        session.commit()
        config_id = str(config.id)
        event_id = event.id

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
            return (["event_name"], [], [(datetime(2026, 1, 1, 10), "Login", 12)])

        def close(self) -> None:
            return None

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: FakeAdapter())
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11), False),
    )
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        with sync_session_factory() as session:
            persisted_event = session.get(Event, event_id)
            assert persisted_event is not None
            return GenerationResult(
                columns_analyzed=1,
                col_meta={"event_name": {"is_json": False, "is_low": True}},
                events_by_name={"event_name=Login": persisted_event},
            )

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)

    seen: dict[str, object] = {}

    def capture(name: str) -> object:
        def _recalculate(*args: object, **kwargs: object) -> int:
            seen[name] = kwargs.get("settling_delay")
            return 0

        return _recalculate

    monkeypatch.setattr(metrics, "_recalculate_metric_anomalies", capture("anomalies"))
    monkeypatch.setattr(metrics, "_recalculate_metric_breakdown_anomalies", capture("breakdown"))

    metrics.collect_metrics.run(config_id)

    assert seen == {
        "anomalies": timedelta(minutes=15),
        "breakdown": timedelta(minutes=15),
    }


def test_metrics_replay_scores_its_window_without_the_settling_allowance(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A replay must be handed NO settling allowance.

    The allowance withholds the head of the evaluated window from emission, but
    ``_replace_scope_anomalies`` first DELETES all of [start, end) and then
    reinserts only up to the emission end — so on a replay of already-settled
    history the withheld buckets simply lose the anomalies they carried, and no
    later scheduled run restores them (its trailing window starts at the live
    clock, past the replayed range).
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        session.add(
            ProjectAnomalySettings(
                project_id=config.project_id,
                anomaly_detection_enabled=True,
                anomaly_ingestion_settling_minutes=15,
            )
        )
        session.commit()
        config_id = str(config.id)
        project_id = config.project_id

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
            return (["event_name"], [], [])

        def close(self) -> None:
            return None

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: FakeAdapter())
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 8, 1, 10), datetime(2026, 8, 1, 18), True),
    )
    monkeypatch.setattr(
        metrics,
        "analyze_cardinality",
        lambda *args, **kwargs: pytest.fail("replay must not run cardinality analysis"),
    )
    monkeypatch.setattr(
        metrics,
        "generate_events",
        lambda *args, **kwargs: pytest.fail("replay must not sync catalog events"),
    )

    seen: dict[str, object] = {}

    def capture(name: str) -> object:
        def _recalculate(*args: object, **kwargs: object) -> int:
            seen[name] = kwargs.get("settling_delay")
            return 0

        return _recalculate

    monkeypatch.setattr(metrics, "_recalculate_metric_anomalies", capture("anomalies"))
    monkeypatch.setattr(metrics, "_recalculate_metric_breakdown_anomalies", capture("breakdown"))

    metrics.collect_metrics.run(config_id)

    with sync_session_factory() as session:
        # The project's own allowance is non-zero, so nothing but the replay
        # branch can produce the zero below.
        assert metrics._ingestion_settling_delay(session, project_id) == timedelta(minutes=15)
    assert seen == {"anomalies": timedelta(0), "breakdown": timedelta(0)}
    assert seen["anomalies"] == metrics.NO_INGESTION_SETTLING


def test_breakdown_recalculate_skips_sub_threshold_scopes_but_ages_out_stale_rows(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """tripl-jfm3.73: the breakdown pass gets the same prefilter as the volume pass.

    ``detect_anomalies`` already early-exits on a provably-silent count series,
    but the breakdown loops paid for the ~500-bucket history load first — one
    query per silent (scope, column, value) triple, every run. The scopes are
    still replaced with an empty anomaly list, so stale window rows age out.
    """
    from tripl.core.analyzers.anomaly_detector import SCOPE_EVENT
    from tripl.worker.tasks.metrics import detect as metrics_detect

    base = _ANOMALY_BASE
    stale_bucket = base + timedelta(hours=9)
    with sync_session_factory() as session:
        config, event_type, event = _seed_anomaly_scan_state(session, base=base)
        config.metric_breakdown_columns = ["platform"]
        settings_row = session.execute(
            select(ProjectAnomalySettings).where(
                ProjectAnomalySettings.project_id == config.project_id
            )
        ).scalar_one()
        # 2 * 10 < 50: every breakdown series below is provably silent.
        settings_row.min_expected_count = 50
        for hour in range(10):
            bucket = base + timedelta(hours=hour)
            session.add(
                EventMetricBreakdown(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=event.id,
                    event_type_id=None,
                    bucket=bucket,
                    breakdown_column="platform",
                    breakdown_value="ios",
                    is_other=False,
                    count=10,
                )
            )
            session.add(
                EventMetricBreakdown(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=None,
                    event_type_id=event_type.id,
                    bucket=bucket,
                    breakdown_column="platform",
                    breakdown_value="ios",
                    is_other=False,
                    count=10,
                )
            )
        session.add(
            MetricBreakdownAnomaly(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                scope_type=SCOPE_EVENT,
                scope_ref=str(event.id),
                event_id=event.id,
                event_type_id=None,
                bucket=stale_bucket,
                breakdown_column="platform",
                breakdown_value="ios",
                is_other=False,
                actual_count=0,
                expected_count=10.0,
                stddev=1.0,
                z_score=-10.0,
                direction="drop",
            )
        )
        session.commit()

        history_loads: list[str] = []
        real_load = metrics_detect._load_breakdown_scope_points

        def counting_load(*args: object, **kwargs: object) -> object:
            history_loads.append(str(kwargs.get("scope_type")))
            return real_load(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(metrics_detect, "_load_breakdown_scope_points", counting_load)

        detected = metrics_detect._recalculate_metric_breakdown_anomalies(
            session,
            config,
            evaluation_start=base,
            evaluation_end=base + timedelta(hours=10),
            covered_buckets={base + timedelta(hours=h) for h in range(10)},
        )
        session.commit()

        assert detected == 0
        # Only the project-total rollup still loads history: its series SUMS
        # across event types, so no per-row MAX bounds it. The event-type and
        # event breakdown series were skipped before the load.
        assert history_loads == ["project_total"]

        remaining = (
            session.execute(
                select(MetricBreakdownAnomaly).where(
                    MetricBreakdownAnomaly.scan_config_id == config.id
                )
            )
            .scalars()
            .all()
        )
        assert remaining == []


# ── zero-row collection backoff (tripl-wopq) ───────────────────────────────────


def _seed_completed_collection(
    session: Session,
    scan_config_id: uuid.UUID,
    *,
    window_to: datetime,
    completed_ago: timedelta = timedelta(minutes=1),
    mode: str = metrics.METRICS_COLLECTION_MODE,
) -> None:
    """Write the ScanJob a FINISHED collection leaves behind, and nothing else.

    No EventMetric row is written on purpose: that is the case this fixture
    exists for — a collection that completed against an empty warehouse window
    (or a stream that has gone silent) and therefore advanced no bucket.
    """
    stamped = datetime.now(UTC) - completed_ago
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
                "type_metrics": 0,
            },
        )
    )
    session.commit()


def _current_hour_boundary() -> datetime:
    """The boundary the dispatcher will floor to for a 1h config (its own helper)."""
    return metrics_schedule._floor_to_interval(datetime.now(UTC), timedelta(hours=1))


def test_completed_collection_that_wrote_no_rows_is_not_redispatched_next_tick(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A successful but EMPTY collection still costs an interval of quiet.

    Due-ness used to read ``max(EventMetric.bucket)`` alone, so a fresh config
    whose warehouse window holds no data yet completed, wrote nothing, left that
    watermark at NULL and was re-dispatched on the very next 300 s tick — forever,
    without ever registering the failure the tripl-n9ee backoff keys on.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        config_id = config.id
        _seed_completed_collection(session, config_id, window_to=_current_hour_boundary())

    result, dispatched = _run_dispatcher(sync_session_factory, monkeypatch)

    assert result == {"checked": 1, "dispatched": 0}
    assert dispatched == []
    # And no junk pending row: the point is to stop filling scan_jobs, not to
    # create a row and then not run it.
    with sync_session_factory() as session:
        pending = (
            session.execute(
                select(ScanJob).where(
                    ScanJob.scan_config_id == config_id,
                    ScanJob.status == ScanJobStatus.pending.value,
                )
            )
            .scalars()
            .all()
        )
    assert pending == []


def test_silent_stream_is_not_recollected_before_its_next_bucket(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The same guard for a config that HAS collected and then went quiet.

    Its newest stored bucket is hours behind the latest complete one, so the
    bucket check alone says "due" on every tick even though the collection that
    just ran already covered that grid and found nothing.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        boundary = _current_hour_boundary()
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                event_type_id=config.event_type_id,
                bucket=boundary - timedelta(hours=5),
                count=12,
            )
        )
        session.commit()
        _seed_completed_collection(session, config.id, window_to=boundary)

    result, dispatched = _run_dispatcher(sync_session_factory, monkeypatch)

    assert result == {"checked": 1, "dispatched": 0}
    assert dispatched == []


def test_empty_collection_is_retried_once_its_own_interval_has_passed(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The watermark defers the retry by one interval; it never abandons the config."""
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        _seed_completed_collection(
            session,
            config.id,
            # Collected up to the PREVIOUS boundary: a new complete bucket exists.
            window_to=_current_hour_boundary() - timedelta(hours=1),
            completed_ago=timedelta(minutes=61),
        )

    result, dispatched = _run_dispatcher(sync_session_factory, monkeypatch)

    assert result == {"checked": 1, "dispatched": 1}
    assert len(dispatched) == 1


def test_replay_window_is_not_read_as_scheduled_collection_progress(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """Only a metrics_collection job carries the live grid's watermark.

    A replay's window is an explicit historical range the user picked, so its end
    says nothing about how far the live grid has been collected. Reading it as
    progress would silence scheduled collection for a whole interval on a config
    that has never run one. (The other producers of completed jobs on this
    scan_config_id — the demo tick, a catalog scan — carry no ``mode`` stamp at
    all and are excluded by the same predicate.)
    """
    boundary = _current_hour_boundary()
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        _seed_completed_collection(
            session, config.id, window_to=boundary, mode=metrics.METRICS_REPLAY_MODE
        )

    result, dispatched = _run_dispatcher(sync_session_factory, monkeypatch)

    assert result == {"checked": 1, "dispatched": 1}
    assert len(dispatched) == 1


# ── catalog-metric failure backoff (tripl-wopq) ────────────────────────────────


def _create_active_sql_metric(
    session: Session,
    config: ScanConfig,
    *,
    last_collection_status: str | None = None,
    updated_ago: timedelta = timedelta(minutes=10),
    stamp_failed_at: bool = True,
) -> uuid.UUID:
    """An active ``sql`` metric on a 1h interval that has never stored a value.

    ``stamp_failed_at=False`` produces a row that failed BEFORE
    ``last_collection_failed_at`` existed — the state every erroring metric is in
    the moment the migration lands, and what the cooldown's fallback is for.
    """
    from tripl.models.domain_enums import MetricKind, MetricStatus, ScanInterval
    from tripl.models.metric_definition import MetricDefinition

    definition = MetricDefinition(
        id=uuid.uuid4(),
        project_id=config.project_id,
        name=f"sql-metric-{uuid.uuid4().hex[:8]}",
        display_name="SQL Metric",
        kind=MetricKind.sql,
        data_source_id=config.data_source_id,
        config={"metric_sql": "SELECT t, 1 AS value FROM events", "time_column": "t"},
        interval=ScanInterval.h1,
        status=MetricStatus.active,
    )
    session.add(definition)
    session.commit()
    if last_collection_status is not None:
        definition.last_collection_status = last_collection_status
        # Explicit, so the cooldown is measured from a known point rather than
        # from whenever this fixture happened to commit. Both fields, because
        # the cooldown reads last_collection_failed_at and falls back to
        # updated_at — set only the latter and every test here would pass
        # through the fallback and prove nothing about the real path.
        stamped = datetime.now(UTC) - updated_ago
        definition.updated_at = stamped
        if stamp_failed_at:
            definition.last_collection_failed_at = stamped
        session.commit()
    return definition.id


def _run_definitions_dispatcher(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> tuple[dict[str, int], list[str]]:
    """Run check_metric_definitions_due against the test DB, capturing dispatches."""
    dispatched: list[str] = []
    monkeypatch.setattr(metrics_schedule, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(
        metrics_schedule.collect_metric_definitions,
        "delay",
        lambda metric_definition_id: dispatched.append(metric_definition_id),
    )
    return metrics_schedule.check_metric_definitions_due.run(), dispatched


def test_errored_catalog_metric_is_not_redispatched_on_the_next_tick(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A catalog metric that cannot collect waits its own interval, like a config.

    ``_metric_definition_due`` reads max(MetricValue.bucket) and the completed
    window watermark, and a collection that dies writes NEITHER — so before
    tripl-wopq a permanently broken metric was re-dispatched every 300 s, the same
    retry storm on the same beat that tripl-n9ee removed from scan configs.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        _create_active_sql_metric(
            session,
            config,
            last_collection_status=metrics_schedule.COLLECTION_STATUS_ERROR,
            # Well inside the one-interval (1h) cooldown the error earns.
            updated_ago=timedelta(minutes=10),
        )

    result, dispatched = _run_definitions_dispatcher(sync_session_factory, monkeypatch)

    assert result == {"checked": 1, "dispatched": 0}
    assert dispatched == []


def test_errored_catalog_metric_retries_once_its_interval_has_elapsed(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The cooldown defers the retry; a fixed metric recovers on its own cadence."""
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        metric_id = _create_active_sql_metric(
            session,
            config,
            last_collection_status=metrics_schedule.COLLECTION_STATUS_ERROR,
            updated_ago=timedelta(minutes=90),
        )

    result, dispatched = _run_definitions_dispatcher(sync_session_factory, monkeypatch)

    assert result == {"checked": 1, "dispatched": 1}
    assert dispatched == [str(metric_id)]


def test_successful_catalog_metric_dispatches_without_any_cooldown(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The cooldown keys on the ERROR marker only — a healthy metric is untouched."""
    from tripl.worker.tasks.metrics.metric_collect import COLLECTION_STATUS_SUCCESS

    with sync_session_factory() as session:
        config = _create_scan_config(session)
        metric_id = _create_active_sql_metric(
            session,
            config,
            last_collection_status=COLLECTION_STATUS_SUCCESS,
            updated_ago=timedelta(seconds=30),
        )

    result, dispatched = _run_definitions_dispatcher(sync_session_factory, monkeypatch)

    assert result == {"checked": 1, "dispatched": 1}
    assert dispatched == [str(metric_id)]


def test_errored_event_composition_metric_uses_the_no_interval_floor(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A composition metric has no interval of its own, so it falls back to 1h.

    Its due check compares the composed values against the SOURCE event-metric
    buckets, neither of which a failed run moves — so it too was re-dispatched on
    every tick. Nothing is lost by waiting: the collector recomputes the whole
    stored series, so the deferred run produces exactly what each skipped one
    would have.
    """
    from tripl.models.domain_enums import MetricComposition, MetricKind, MetricStatus
    from tripl.models.metric_definition import MetricDefinition

    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                event_type_id=config.event_type_id,
                bucket=_current_hour_boundary() - timedelta(hours=1),
                count=7,
            )
        )
        definition = MetricDefinition(
            id=uuid.uuid4(),
            project_id=config.project_id,
            name="composition-metric",
            display_name="Composition Metric",
            kind=MetricKind.event_composition,
            composition=MetricComposition.single,
            config={},
            interval=None,
            numerator_event_type_id=config.event_type_id,
            status=MetricStatus.active,
        )
        session.add(definition)
        session.commit()
        definition.last_collection_status = metrics_schedule.COLLECTION_STATUS_ERROR
        definition.updated_at = datetime.now(UTC) - timedelta(minutes=10)
        session.commit()
        metric_id = definition.id

    result, dispatched = _run_definitions_dispatcher(sync_session_factory, monkeypatch)

    assert result == {"checked": 1, "dispatched": 0}
    assert dispatched == []

    # Past the fallback floor it retries, so the metric is deferred, not dropped.
    with sync_session_factory() as session:
        reloaded = session.get(MetricDefinition, metric_id)
        assert reloaded is not None
        reloaded.updated_at = datetime.now(UTC) - metrics_schedule.NO_INTERVAL_ERROR_BACKOFF
        session.commit()

    result, dispatched = _run_definitions_dispatcher(sync_session_factory, monkeypatch)

    assert result == {"checked": 1, "dispatched": 1}
    assert dispatched == [str(metric_id)]


def test_the_cooldown_survives_an_unrelated_write_to_the_metric(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Editing a broken metric must not restart the cooldown you are waiting out.

    The cooldown used to measure from ``updated_at``, and ``TimestampMixin``
    carries ``onupdate=func.now()``, so ANY write moved it — including
    ``update_metric_definition``, which is what an operator runs to FIX the
    metric. Half an hour into a one-hour cooldown, changing the display name
    bought them another full hour (tripl-os3v).
    """
    from tripl.models.metric_definition import MetricDefinition

    with sync_session_factory() as session:
        config = _create_scan_config(session)
        metric_id = _create_active_sql_metric(
            session,
            config,
            last_collection_status=metrics_schedule.COLLECTION_STATUS_ERROR,
            updated_ago=timedelta(minutes=30),
        )

        definition = session.get(MetricDefinition, metric_id)
        assert definition is not None
        before = metrics_schedule._metric_definition_error_backoff(
            definition, now=datetime.now(UTC)
        )
        assert before is not None

        # The edit: any field, committed the ordinary way, which is what bumps
        # updated_at. Nothing here touches the collection state.
        definition.display_name = "Renamed while broken"
        session.commit()

        after = metrics_schedule._metric_definition_error_backoff(definition, now=datetime.now(UTC))

    assert after is not None
    waited_before, _ = before
    waited_after, _ = after
    # Same elapsed time either side of the edit, to the second. Under the old
    # reading waited_after collapsed to ~0 and the metric sat out a second hour.
    assert abs(waited_after - waited_before) < timedelta(seconds=5)
    assert waited_after >= timedelta(minutes=29)


def test_a_metric_that_failed_before_the_column_existed_still_cools_down(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The migration lands on rows already in the error state, carrying NULL.

    Reading NULL as "never failed" would release every one of them on the first
    tick after deploy — the retry storm this backoff exists to prevent, produced
    by the fix for it. They fall back to ``updated_at`` until their next real
    failure stamps the column.
    """
    from tripl.models.metric_definition import MetricDefinition

    with sync_session_factory() as session:
        config = _create_scan_config(session)
        metric_id = _create_active_sql_metric(
            session,
            config,
            last_collection_status=metrics_schedule.COLLECTION_STATUS_ERROR,
            updated_ago=timedelta(minutes=5),
            stamp_failed_at=False,
        )
        definition = session.get(MetricDefinition, metric_id)
        assert definition is not None
        assert definition.last_collection_failed_at is None
        backoff = metrics_schedule._metric_definition_error_backoff(
            definition, now=datetime.now(UTC)
        )

    assert backoff is not None, "a legacy error row must still be held back"
    waited, delay = backoff
    assert waited < delay


def test_no_error_path_can_set_the_status_without_the_timestamp() -> None:
    """The seventh call site, pinned before it is written.

    Six sites across two modules put a metric into the error state, and the
    cooldown is only correct if every one of them stamps the time too. This
    walks the source rather than trusting a convention: an assignment of
    ``COLLECTION_STATUS_ERROR`` anywhere but inside ``mark_collection_error`` is
    the bug (tripl-os3v).

    The seventh site arrived, and it was not a worker: a group-rule merge fails
    a metric whose two ratio operands have collapsed onto one event, and
    ``core`` may not import ``worker``. So ``mark_collection_error`` moved onto
    the model — and this walk had to move with it, or the one module now holding
    the real assignment would be the one module nobody checks. Adding
    ``metric_definition`` here is what keeps the guard honest; without it the
    test would have gone on passing while covering strictly less.
    """
    import ast
    from pathlib import Path

    from tripl.models import metric_definition as model_module
    from tripl.worker.tasks.metrics import metric_collect as collect_module
    from tripl.worker.tasks.metrics import schedule as schedule_module

    offenders: list[str] = []
    for module in (collect_module, schedule_module, model_module):
        path = Path(str(module.__file__))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # By line range, not by skipping the FunctionDef node: ``ast.walk``
        # yields every descendant independently, so passing over the definition
        # does not pass over the assignment inside it — which is how the first
        # version of this test flagged the one function allowed to do this.
        allowed: set[int] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "mark_collection_error"
                and node.end_lineno is not None
            ):
                allowed |= set(range(node.lineno, node.end_lineno + 1))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or node.lineno in allowed:
                continue
            value = node.value
            if not (isinstance(value, ast.Name) and value.id == "COLLECTION_STATUS_ERROR"):
                continue
            targets = [
                target
                for target in node.targets
                if isinstance(target, ast.Attribute) and target.attr == "last_collection_status"
            ]
            if targets:
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, (
        "these set the error status directly instead of calling "
        f"mark_collection_error, so they leave the cooldown unstamped: {offenders}"
    )


def test_metric_error_cooldown_is_the_shared_backoff_curves_first_step(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Pins the two dispatchers to ONE curve instead of two drifting constants.

    The catalog path cannot count a failure streak (there is no job table for
    metrics), so it applies ``_failure_backoff_delay`` at the threshold. If that
    curve's first step ever changes, this must move with it rather than leaving
    the catalog metrics retrying on a private schedule.
    """
    from tripl.models.metric_definition import MetricDefinition

    hour = timedelta(hours=1)
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        metric_id = _create_active_sql_metric(
            session,
            config,
            last_collection_status=metrics_schedule.COLLECTION_STATUS_ERROR,
            updated_ago=timedelta(minutes=1),
        )
        definition = session.get(MetricDefinition, metric_id)
        assert definition is not None
        backoff = metrics_schedule._metric_definition_error_backoff(
            definition, now=datetime.now(UTC)
        )

    assert backoff is not None
    _waited, delay = backoff
    assert delay == metrics_schedule._failure_backoff_delay(
        metrics_schedule.FAILURE_BACKOFF_AFTER, hour
    )
    assert delay == hour  # the metric's own interval, spelled out


def test_recalculate_metric_anomalies_honours_per_scope_override(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A false-positive ratchet is only worth storing if detection reads it.

    The ratchet writes an ``AnomalyScopeOverride`` keyed the way an anomaly keys
    itself — (scan_config_id, scope_type, scope_ref) — so the scope an operator
    dismissed gets stricter and every OTHER scope keeps its previous
    sensitivity. Before tripl-l429 the ratchet raised the project-wide setting
    instead, so one click silenced scopes nobody had complained about.
    """
    from tripl.core.analyzers.anomaly_detector import SCOPE_EVENT, SCOPE_EVENT_TYPE
    from tripl.models.anomaly_scope_override import AnomalyScopeOverride
    from tripl.worker.tasks.metrics.detect import _recalculate_metric_anomalies

    base = _ANOMALY_BASE
    eval_start = base
    eval_end = base + timedelta(hours=11)  # includes the missing hour-10 bucket
    covered = {base + timedelta(hours=h) for h in range(11)}

    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session, base=base)

        _recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=eval_start,
            evaluation_end=eval_end,
            covered_buckets=covered,
        )
        session.commit()
        flagged_scopes = {
            row.scope_type
            for row in session.execute(
                select(MetricAnomaly).where(MetricAnomaly.scan_config_id == config.id)
            ).scalars()
        }
        # Baseline: the zero-filled gap reads as a drop on both scopes.
        assert SCOPE_EVENT in flagged_scopes
        assert SCOPE_EVENT_TYPE in flagged_scopes

        session.add(
            AnomalyScopeOverride(
                id=uuid.uuid4(),
                project_id=config.project_id,
                scan_config_id=config.id,
                scope_type=SCOPE_EVENT,
                scope_ref=str(event.id),
                scope_name=event.name,
                sigma_threshold=3.0,
                min_expected_count=1000,
                false_positive_count=1,
            )
        )
        session.commit()

        _recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=eval_start,
            evaluation_end=eval_end,
            covered_buckets=covered,
        )
        session.commit()

        after = {
            row.scope_type
            for row in session.execute(
                select(MetricAnomaly).where(MetricAnomaly.scan_config_id == config.id)
            ).scalars()
        }

    # The overridden scope goes quiet; the event-type scope, which nobody
    # dismissed, still flags the very same gap.
    assert SCOPE_EVENT not in after
    assert SCOPE_EVENT_TYPE in after


def _unbound_composition(session: Session, config, *, composition, **refs):
    """A composition metric whose operand refs are whatever the caller passes.

    Written directly rather than through the API because the schemas REQUIRE a
    numerator (and a denominator for ``ratio``), so the state under test is
    unreachable by hand — it is always the footprint of an ``ondelete="SET
    NULL"`` after a deleted event or event type.
    """
    from tripl.models.domain_enums import MetricKind, MetricStatus
    from tripl.models.metric_definition import MetricDefinition

    definition = MetricDefinition(
        id=uuid.uuid4(),
        project_id=config.project_id,
        name=f"composition-{uuid.uuid4().hex[:8]}",
        display_name="Composition",
        kind=MetricKind.event_composition,
        composition=composition,
        config={},
        interval=None,
        status=MetricStatus.active,
        **refs,
    )
    session.add(definition)
    session.commit()
    return definition


def test_a_composition_metric_with_no_operand_at_all_reports_a_failure(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The silent flatline, made loud (tripl-nmn3).

    Both refs NULL means _read_event_metric_series returns {}, which the
    collector reported as {"values": 0, "grids": 0} — a SUCCESS. The status
    stayed green, _event_composition_due then read the same empty series and
    returned False forever, and the metric sat at zero with nothing anywhere
    saying why. Structurally unable to produce a value is not the same as
    "nothing to produce yet", and only the second may stay quiet.
    """
    from tripl.models.domain_enums import MetricComposition
    from tripl.worker.tasks.metrics.metric_collect import _collect_event_composition

    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        definition = _unbound_composition(
            session,
            config,
            composition=MetricComposition.single,
            numerator_event_id=None,
            numerator_event_type_id=None,
        )
        with pytest.raises(ScanError) as excinfo:
            _collect_event_composition(session, definition=definition)

    assert "numerator" in str(excinfo.value)


def test_a_ratio_whose_denominator_lost_its_binding_reports_a_failure(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """And it raises BEFORE writing, so the stored series is not erased.

    A ratio with a live numerator and a dead denominator divides every bucket by
    nothing, yielding a dict of Nones — which is not empty, so the collector ran
    on to delete the existing window and then wrote none of it back. The guard
    has to come first or "make the failure visible" costs the user their data.
    """
    from tripl.models.domain_enums import MetricComposition
    from tripl.worker.tasks.metrics.metric_collect import _collect_event_composition

    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        definition = _unbound_composition(
            session,
            config,
            composition=MetricComposition.ratio,
            numerator_event_type_id=config.event_type_id,
            denominator_event_id=None,
            denominator_event_type_id=None,
        )
        with pytest.raises(ScanError) as excinfo:
            _collect_event_composition(session, definition=definition)

    assert "denominator" in str(excinfo.value)


def test_a_bound_composition_metric_with_no_rows_yet_stays_quiet(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The boundary the guard must not cross.

    A metric pointed at a real event type that simply has not been collected
    yet is legitimately zero. Raising here would turn every newly created
    metric red before its first scan.
    """
    from tripl.models.domain_enums import MetricComposition
    from tripl.worker.tasks.metrics.metric_collect import _collect_event_composition

    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        definition = _unbound_composition(
            session,
            config,
            composition=MetricComposition.single,
            numerator_event_type_id=config.event_type_id,
        )
        assert _collect_event_composition(session, definition=definition) == {
            "values": 0,
            "grids": 0,
        }


def test_an_unbound_composition_metric_stays_due_so_it_can_report(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Without this the guard would never fire.

    Due is checked before anything else, and a metric with no operand has no
    source bucket — so the old due check returned False forever and the
    collector was never asked. That is the half of tripl-jtnv that made the
    flatline permanent rather than merely quiet.
    """
    from tripl.models.domain_enums import MetricComposition

    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        unbound = _unbound_composition(
            session,
            config,
            composition=MetricComposition.single,
            numerator_event_id=None,
            numerator_event_type_id=None,
        )
        bound = _unbound_composition(
            session,
            config,
            composition=MetricComposition.single,
            numerator_event_type_id=config.event_type_id,
        )

        assert metrics_schedule._event_composition_due(session, unbound) is True
        # ...and a properly bound metric with no newer source bucket is not.
        assert metrics_schedule._event_composition_due(session, bound) is False


# --- sampler ring + rotation pace (tripl-81p5), sampler observability (tripl-d1rd)


def _seed_json_path_variable(
    session: Session,
    config: ScanConfig,
    *,
    source_name: str = "payload.user.plan",
    excluded: bool = False,
) -> Variable:
    variable = Variable(
        id=uuid.uuid4(),
        project_id=config.project_id,
        name=source_name.rpartition(".")[2],
        source_name=source_name,
        variable_type="string",
        bindings=[source_name],
        excluded_from_scans=excluded,
    )
    session.add(variable)
    session.commit()
    return variable


def _seed_variable_context(
    session: Session,
    config: ScanConfig,
    variable: Variable,
    *,
    observed_count: int,
    values: list[str],
) -> VariableValue:
    """A stored context row; its observation count is what makes a candidate."""
    assert config.event_type_id is not None
    fd = FieldDefinition(
        id=uuid.uuid4(),
        event_type_id=config.event_type_id,
        name="payload",
        display_name="Payload",
        field_type="json",
        is_required=False,
        description="",
    )
    event = Event(
        id=uuid.uuid4(),
        project_id=config.project_id,
        event_type_id=config.event_type_id,
        name="Signup",
        description="",
        status="implemented",
    )
    row = VariableValue(
        id=uuid.uuid4(),
        project_id=variable.project_id,
        branch_id=variable.branch_id,
        variable_id=variable.id,
        event_id=event.id,
        field_definition_id=fd.id,
        source_column="payload",
        value_kind="high",
        observed_count=observed_count,
        values=list(values),
    )
    session.add_all([fd, event, row])
    session.commit()
    return row


def _candidates(session: Session, config: ScanConfig) -> list[tuple[str, str]]:
    return metrics_catalog_sync._unfilled_json_path_candidates(
        session,
        project_id=config.project_id,
        branch_id=None,
        json_columns={"payload"},
    )


def test_a_variable_with_no_context_rows_is_not_a_candidate(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The ring-bloat half of tripl-81p5.

    A contextless variable is an unused one — no event field references its
    token, so a sampled value would have no row to land in. Keeping these as
    permanent candidates ran the production ring at ~10x its fillable size
    (1545 of 1777 variables) and starved the paths a sample could actually fill.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        _seed_json_path_variable(session, config)

        assert _candidates(session, config) == []


def test_a_variable_with_an_unfilled_context_is_a_candidate(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        variable = _seed_json_path_variable(session, config)
        _seed_variable_context(session, config, variable, observed_count=0, values=[])

        assert _candidates(session, config) == [("payload", "user.plan")]


def test_a_fully_observed_variable_is_not_a_candidate(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        variable = _seed_json_path_variable(session, config)
        _seed_variable_context(session, config, variable, observed_count=2, values=["pro"])

        assert _candidates(session, config) == []


def test_an_excluded_variable_is_not_a_candidate_even_with_an_unfilled_context(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The tombstone must silence sampling too, or exclusion still costs queries."""
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        variable = _seed_json_path_variable(session, config, excluded=True)
        _seed_variable_context(session, config, variable, observed_count=0, values=[])

        assert _candidates(session, config) == []


_STRIDE_TICK = timedelta(hours=1)
_STRIDE_END = datetime(2026, 8, 30, 12, tzinfo=UTC)


def test_rotating_window_strides_by_its_own_size_between_ticks() -> None:
    """The rotation-pace half of tripl-81p5.

    Under the pre-fix one-candidate stride this test FAILS: consecutive slices
    would overlap on all but one element (``second[:-1] == first[1:]``), which
    on production meant 199/200 of each run's budget re-sampled the previous
    run's paths and the last candidates waited ~67 days for a first attempt.
    """
    candidates = [("payload", f"user.p{index}") for index in range(10)]

    first = metrics_catalog_sync._rotating_window(
        candidates, size=4, window_end=_STRIDE_END, tick=_STRIDE_TICK
    )
    second = metrics_catalog_sync._rotating_window(
        candidates, size=4, window_end=_STRIDE_END + _STRIDE_TICK, tick=_STRIDE_TICK
    )

    start = candidates.index(first[0])
    assert second == [candidates[(start + 4 + offset) % 10] for offset in range(4)], (
        "the next tick's slice must start exactly one slice-width further on"
    )
    assert set(first).isdisjoint(second), "with 2*size <= len, consecutive slices cannot overlap"


def test_rotating_window_repeats_the_slice_for_a_retry_of_the_same_window() -> None:
    """A retry re-samples what the failed attempt was doing, not the next slice."""
    candidates = [("payload", f"user.p{index}") for index in range(10)]

    attempt = metrics_catalog_sync._rotating_window(
        candidates, size=4, window_end=_STRIDE_END, tick=_STRIDE_TICK
    )
    retry = metrics_catalog_sync._rotating_window(
        candidates, size=4, window_end=_STRIDE_END, tick=_STRIDE_TICK
    )

    assert retry == attempt


def test_rotating_window_returns_everything_when_the_ring_fits() -> None:
    candidates = [("payload", f"user.p{index}") for index in range(3)]

    window = metrics_catalog_sync._rotating_window(
        candidates, size=200, window_end=_STRIDE_END, tick=_STRIDE_TICK
    )

    assert window == candidates


class _SamplingFakeAdapter:
    """The scheduled collector's warehouse surface, JSON column included."""

    def test_connection(self) -> bool:
        return True

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        return [
            ColumnInfo(name="time", type_name="DateTime"),
            ColumnInfo(name="event_name", type_name="String"),
            ColumnInfo(name="payload", type_name="JSON"),
        ]

    def get_json_path_samples(self, *args: object, **kwargs: object):
        return {"payload": {"user.plan": ['"pro"', '"free"']}}

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
        return (["event_name"], [], [])

    def close(self) -> None:
        return None


def test_scheduled_run_reports_sampler_progress_in_result_summary(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """tripl-d1rd: a scheduled run must say what its sampler did.

    ``variable_values_touched`` is bound to the replay path and reads 0 on every
    scheduled run, so before these keys the 2026-08-31 production stall (whole
    cycles moving zero contexts from empty to filled) was invisible in the job
    summaries an operator actually looks at.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        variable = _seed_json_path_variable(session, config)
        _seed_variable_context(session, config, variable, observed_count=0, values=[])
        config_id = str(config.id)

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: _SamplingFakeAdapter())
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        metrics,
        "generate_events",
        lambda *args, **kwargs: GenerationResult(columns_analyzed=1, variable_values_written=2),
    )

    result = metrics.collect_metrics.run(config_id)

    assert result["mode"] == "metrics_collection"
    # One candidate ring entry, sampled this run, and the adapter had values.
    assert result["json_path_ring_size"] == 1
    assert result["json_paths_sampled"] == 1
    assert result["json_paths_with_samples"] == 1
    # The generator's own write count, summed across this run's generate calls.
    assert result["variable_values_written"] == 2
    # The seeded context stays unfilled (generate_events is stubbed), so the
    # post-sync aggregate an operator watches for convergence reads exactly it.
    assert result["variable_contexts_unfilled"] == 1


def test_replay_run_summary_does_not_gain_the_scheduled_sampler_keys(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """Replay reports through ``variable_values_touched`` and its own sampler;
    the scheduled-run keys would read as zeros there and imply a stalled ring."""
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        job = ScanJob(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            status=ScanJobStatus.pending.value,
        )
        session.add(job)
        session.commit()
        config_id = str(config.id)
        job_id = str(job.id)

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: _SamplingFakeAdapter())
    monkeypatch.setattr(
        metrics,
        "analyze_cardinality",
        lambda *args, **kwargs: pytest.fail("replay must not run cardinality analysis"),
    )
    monkeypatch.setattr(
        metrics,
        "generate_events",
        lambda *args, **kwargs: pytest.fail("replay must not sync catalog events"),
    )

    result = metrics.collect_metrics.run(
        config_id,
        job_id,
        time_from="2026-01-01T08:00:00+00:00",
        time_to="2026-01-01T10:00:00+00:00",
    )

    assert result["mode"] == "metrics_replay"
    assert result["variable_values_touched"] == 0
    for key in (
        "json_path_ring_size",
        "json_paths_sampled",
        "json_paths_with_samples",
        "variable_values_written",
        "variable_contexts_unfilled",
    ):
        assert key not in result


def _seed_scan_created_variable(
    session: Session,
    config: ScanConfig,
    *,
    source_name: str,
    column_type: str | None = None,
) -> Variable:
    """A variable in exactly the state a scan leaves behind, and nothing else.

    The provenance description is the ONLY marker the model carries — there is
    no ``created_by`` column — so a row without it reads as ``user_edited`` to
    the retirement predicate and can never be swept. That is why the other
    variables this module seeds are untouched by the sweep below:
    ``_seed_json_path_variable`` leaves ``description`` at its ``""`` default.

    ``column_type`` seeds the column the variable was minted from: a
    FieldDefinition named for the base column (``payload`` for
    ``payload.user.adana``) with that ``field_type`` on the config's event
    type. That stored type is what ``retire_unused_variables`` reads to tell a
    JSON-derived variable from a scalar-derived one when a scheduled run asks
    it to defer the latter (tripl-bwo8); left ``None``, no FieldDefinition
    exists and the sweep's conservative default calls the variable
    scalar-derived. Seeding one column twice reuses the row — a FieldDefinition
    name is unique per event type.
    """
    if column_type is not None:
        assert config.event_type_id is not None
        column = source_name.partition(".")[0]
        already_seeded = session.scalar(
            select(FieldDefinition.id).where(
                FieldDefinition.event_type_id == config.event_type_id,
                FieldDefinition.name == column,
            )
        )
        if already_seeded is None:
            session.add(
                FieldDefinition(
                    id=uuid.uuid4(),
                    event_type_id=config.event_type_id,
                    name=column,
                    display_name=column,
                    field_type=column_type,
                    is_required=False,
                    description="",
                )
            )
    variable = Variable(
        id=uuid.uuid4(),
        project_id=config.project_id,
        name=source_name.rpartition(".")[2],
        source_name=source_name,
        description=SCAN_PROVENANCE_DESCRIPTION,
        variable_type="string",
        bindings=[source_name],
    )
    session.add(variable)
    session.commit()
    return variable


def _seed_event_value_naming(session: Session, config: ScanConfig, *, token: str) -> None:
    """One stored event field value holding ``${token}`` — the reference check's input."""
    assert config.event_type_id is not None
    field = FieldDefinition(
        id=uuid.uuid4(),
        event_type_id=config.event_type_id,
        name=f"field_{token}",
        display_name="Field",
        field_type="string",
        is_required=False,
        description="",
    )
    event = Event(
        id=uuid.uuid4(),
        project_id=config.project_id,
        event_type_id=config.event_type_id,
        name=f"Uses {token}",
        description="",
        status="implemented",
    )
    session.add_all([field, event])
    session.flush()
    session.add(
        EventFieldValue(
            id=uuid.uuid4(),
            event_id=event.id,
            field_definition_id=field.id,
            value=f"${{{token}}}",
        )
    )
    session.commit()


def test_scheduled_collection_retires_the_variables_nothing_refers_to(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """tripl-bh1q: the path that mints variables on a schedule must also sweep them.

    ``run_scan`` — the MANUALLY triggered path — was the sweep's only worker call
    site, while the production shape the sweep was written for (a JSON map column
    keyed by user-typed text) is collected hourly by this task and scanned by
    hand approximately never. So the catalog every user-facing doc promises
    self-heals only ever grew, one permanent row per key.

    Every variable here carries the scan's provenance description, so the only
    thing separating them is USE: ``plan`` still has its ``${token}`` in a stored
    event field value, ``adana`` is the fossil of a JSON key that stopped
    arriving, ``campaign`` the fossil of a plain column whose token is gone.

    ``scan_lookback_hours`` is set so BOTH fossils go: with a declared lookback
    the operator has said which window represents their tracking plan, and the
    run sweeps scalar-derived variables as well as JSON-derived ones. Without
    it only the JSON-derived fossil would be taken — see
    ``test_scheduled_run_defers_scalar_derived_variables_without_a_declared_lookback``
    for the half that waits and
    ``test_scheduled_run_sweeps_json_derived_variables_without_a_declared_lookback``
    for the half that does not (tripl-bwo8).
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        config.scan_lookback_hours = 168
        session.commit()
        fossil = _seed_scan_created_variable(
            session, config, source_name="payload.user.adana", column_type="json"
        )
        scalar_fossil = _seed_scan_created_variable(
            session, config, source_name="campaign", column_type="string"
        )
        live = _seed_scan_created_variable(
            session, config, source_name="payload.user.plan", column_type="json"
        )
        _seed_event_value_naming(session, config, token="plan")
        config_id = str(config.id)
        project_id = config.project_id
        fossil_id = fossil.id
        scalar_fossil_id = scalar_fossil.id
        live_id = live.id

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: _SamplingFakeAdapter())
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        metrics,
        "generate_events",
        lambda *args, **kwargs: GenerationResult(columns_analyzed=1),
    )

    result = metrics.collect_metrics.run(config_id)

    # DISABLE-THE-FIX: delete the ``retire_unused_variables`` call from
    # ``collect_metrics`` and this reads 0 and both rows below are still there —
    # which is the state production was found in. Pass
    # ``include_scalar_derived=False`` regardless of the lookback and it reads
    # 1, with ``campaign`` still there.
    assert result["variables_retired"] == 2
    with sync_session_factory() as session:
        surviving = set(
            session.execute(select(Variable.id).where(Variable.project_id == project_id)).scalars()
        )
    assert fossil_id not in surviving
    assert scalar_fossil_id not in surviving
    # The predicate, not a blanket delete: a live ``${token}`` keeps its row.
    assert live_id in surviving
    # And the run says so where an operator reads it, in ``run_scan``'s words.
    details = result["details"]
    assert isinstance(details, list)
    assert "Retired 2 unused variables no event refers to" in details


def test_replay_does_not_retire_variables(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A replay skips catalog sync, so it must never judge which variables are unused.

    ``sync_catalog`` does not run on a replay — no cardinality analysis, no
    generation — so the run holds no fresh evidence about which paths a row
    still carries, and its window is historical by definition. Sweeping there
    would delete on the strength of a scan that did not happen. The gate is the
    same ``is_replay`` that already guards the catalog sync and the reindex.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        job = ScanJob(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            status=ScanJobStatus.pending.value,
        )
        session.add(job)
        session.commit()
        fossil = _seed_scan_created_variable(session, config, source_name="payload.user.adana")
        config_id = str(config.id)
        job_id = str(job.id)
        fossil_id = fossil.id

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: _SamplingFakeAdapter())
    monkeypatch.setattr(
        metrics,
        "analyze_cardinality",
        lambda *args, **kwargs: pytest.fail("replay must not run cardinality analysis"),
    )
    monkeypatch.setattr(
        metrics,
        "generate_events",
        lambda *args, **kwargs: pytest.fail("replay must not sync catalog events"),
    )

    result = metrics.collect_metrics.run(
        config_id,
        job_id,
        time_from="2026-01-01T08:00:00+00:00",
        time_to="2026-01-01T10:00:00+00:00",
    )

    assert result["mode"] == "metrics_replay"
    # The unreferenced row a scheduled run would take survives a replay.
    with sync_session_factory() as session:
        assert session.get(Variable, fossil_id) is not None
    # ABSENT, not zero. A ``0`` here would read as "swept, found nothing" rather
    # than "did not sweep" — precisely how ``variable_values_touched`` reading 0
    # on every scheduled run hid the 2026-08-31 stall for weeks.
    assert "variables_retired" not in result


def test_the_sweep_reports_the_rows_it_deleted_not_the_rows_it_planned(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A run that lost the race deleted nothing and must not claim otherwise.

    The sweep is PROJECT-wide but ``collect_metrics`` is per-config, and
    ``check_metrics_due`` dispatches every due config as an independent Celery
    task: its advisory lock serialises the dispatch LOOP, and the "an active job
    is still in progress" check is ``_get_active_scan_jobs(session, config.id)``
    — per config. Two configs of one project on the same interval therefore run
    in parallel, both compute the same retirable set, and only one ``DELETE``
    matches anything. Returning ``len(plan.retirable)`` had both of them report
    the full count in the summary an operator reads and in the log line an
    operator greps.

    The rival's commit is modelled by removing the rows through the task's own
    session between the plan and the ``DELETE``. What is under test is that a
    statement matching nothing contributes nothing — not SQLite's locking — and
    driving a second connection into a write lock mid-transaction would be
    testing the harness instead.

    DISABLE-THE-FIX: restore ``return len(plan.retirable)`` and the first
    assertion reads 1.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        config.scan_lookback_hours = 24
        fossil = _seed_scan_created_variable(session, config, source_name="payload.user.adana")
        project_id = config.project_id
        fossil_id = fossil.id

    real_plan_retirement = variable_sweep.plan_retirement

    with sync_session_factory() as session:

        def plan_then_lose_the_race(*args: object, **kwargs: object):
            plan = real_plan_retirement(*args, **kwargs)
            session.execute(delete(Variable).where(Variable.id.in_(plan.retirable)))
            return plan

        monkeypatch.setattr(variable_sweep, "plan_retirement", plan_then_lose_the_race)

        retired = variable_sweep.retire_unused_variables(
            session, project_id=project_id, branch_id=None
        )

    assert retired == 0, "the count must come from the statements' rowcounts, not from the plan"
    # The row is gone all the same — this run simply is not the one that took it.
    with sync_session_factory() as session:
        assert session.get(Variable, fossil_id) is None


def test_scheduled_run_records_the_sweep_before_a_later_failure_can_hide_it(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A run that dies after the delete must still say what it destroyed.

    In ``run_scan`` the sweep is the last work before the summary commit. In
    ``collect_metrics`` it sits at the HEAD of the run: the ``DELETE`` commits,
    then the reindex, every chunk's warehouse query under a 24h soft limit,
    anomaly recalculation and alert preparation all follow before
    ``result_summary`` is assembled. Anything raising in that span used to leave
    the deletions durable while ``variables_retired`` and the details sentence
    were never written — the job reported failure and said nothing at all about
    the rows it had removed.

    The reindex stands in for "anything after the delete" because it is the very
    next statement; the point is the ORDER, not which later stage fails.

    DISABLE-THE-FIX: delete the stub block that stamps ``job.result_summary``
    right after ``retire_unused_variables`` and the summary assertions go red
    while the variable stays deleted — the exact state this repairs.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        config.scan_lookback_hours = 168
        session.commit()
        job = ScanJob(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            status=ScanJobStatus.pending.value,
            # Exactly what ``check_metrics_due`` stamps at creation.
            result_summary={"mode": "metrics_collection"},
        )
        session.add(job)
        session.commit()
        fossil = _seed_scan_created_variable(session, config, source_name="payload.user.adana")
        config_id = str(config.id)
        job_id = str(job.id)
        job_pk = job.id
        fossil_id = fossil.id

    def exploding_reindex(*args: object, **kwargs: object) -> None:
        raise RuntimeError("search cluster unreachable")

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: _SamplingFakeAdapter())
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        metrics,
        "generate_events",
        lambda *args, **kwargs: GenerationResult(columns_analyzed=1),
    )
    monkeypatch.setattr(metrics, "reindex_main_branch_from_worker", exploding_reindex)

    with pytest.raises(RuntimeError):
        metrics.collect_metrics.run(config_id, job_id)

    with sync_session_factory() as session:
        # The deletion is durable — it was committed by the sweep itself.
        assert session.get(Variable, fossil_id) is None
        failed = session.get(ScanJob, job_pk)
        assert failed is not None
        assert failed.status == ScanJobStatus.failed.value
        summary = failed.result_summary
        assert isinstance(summary, dict)
        # …and so is the record of it, on the failed row.
        assert summary["variables_retired"] == 1
        assert summary["details"] == ["Retired 1 unused variables no event refers to"]
        # The dispatcher's mode label is carried over, not clobbered: an
        # unlabelled failed row is indistinguishable from a failed manual scan,
        # which is why it is stamped at job creation in the first place.
        assert summary["mode"] == "metrics_collection"


def test_scheduled_run_defers_scalar_derived_variables_without_a_declared_lookback(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A SCALAR column's variable waits for the operator to declare the window.

    On this path the catalog is ALWAYS windowed — the task returns early without
    a ``time_column`` — and with ``scan_lookback_hours`` unset
    ``resolve_lookback_window`` returns ``None`` and ``collect_metrics`` falls
    back to the collection window, which ``_resolve_collection_window`` sets to
    ``last_bucket - delta`` — three intervals in steady state on an hourly
    schedule. Cardinality judged over that slice can flip a column that is high
    over the table to
    ``is_low`` (``plan_column_meta``, non-JSON branch only), ``plan_events``
    then writes a LITERAL where the ``${token}`` stood, and the rewrite drops
    the field's contexts — the whole column at once, on every event carrying
    it. The evidence is lost at that rewrite, sweep or no sweep; what the sweep
    would add is the ROW, re-minted under a new id the next hour the column
    reads high again. That churn waits for a declared lookback.

    ``variables_retired`` is PRESENT and 0: the run swept — every JSON-derived
    variable was judged — and found nothing. Absent is reserved for a replay,
    which never asks the question (``test_replay_does_not_retire_variables``).

    DISABLE-THE-FIX: pass ``include_scalar_derived=True`` regardless of
    ``catalog_window_declared`` and the fossil is deleted and the count reads 1.
    (Loosening ``is_json_derived`` does NOT redden this one — ``campaign``'s
    FieldDefinition is ``string``, so no reading of the dotted-ness rule
    classifies it as JSON-derived. The dotted-ness rule is pinned in
    ``test_variable_retirement`` instead, where a JSON column exists to be
    matched.)
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        # The default, and the state of any config whose operator never typed a
        # lookback: nullable column, ``None`` in every request schema, blank in
        # the form.
        assert config.scan_lookback_hours is None
        fossil = _seed_scan_created_variable(
            session, config, source_name="campaign", column_type="string"
        )
        config_id = str(config.id)
        fossil_id = fossil.id

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: _SamplingFakeAdapter())
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        metrics,
        "generate_events",
        lambda *args, **kwargs: GenerationResult(columns_analyzed=1),
    )

    result = metrics.collect_metrics.run(config_id)

    assert result["mode"] == "metrics_collection"
    with sync_session_factory() as session:
        assert session.get(Variable, fossil_id) is not None
    assert result["variables_retired"] == 0
    # A run that retired nothing does not say "Retired 0" to the operator.
    assert not any("Retired" in line for line in result["details"])


def test_scheduled_run_sweeps_json_derived_variables_without_a_declared_lookback(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A JSON key nothing refers to is recycled on every scheduled cycle.

    The ``is_low`` flip that keeps the scalar case waiting cannot reach a
    variable minted from a JSON column: a JSON column has no template and no
    literal fallback, its variables are the discovered paths and are always
    high. What a one-interval window does to one is drop a key absent this hour
    from the value rebuilt for that event — the "key that stopped arriving" the
    sweep exists for, one key on one event. Gating the whole call on the
    lookback left every one of production's 1929 JSON-derived variables
    unswept for the sake of the two that were not (measured 2026-09-03), and
    blank is the default.

    DISABLE-THE-FIX: revert ``collect_metrics`` to calling the sweep only under
    ``catalog_window_declared`` and the fossil survives with ``variables_retired``
    absent. Make ``_json_column_names`` return an empty set and it survives with
    the count reading 0.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.scan_lookback_hours is None
        fossil = _seed_scan_created_variable(
            session, config, source_name="payload.user.adana", column_type="json"
        )
        config_id = str(config.id)
        fossil_id = fossil.id

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: _SamplingFakeAdapter())
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        metrics,
        "generate_events",
        lambda *args, **kwargs: GenerationResult(columns_analyzed=1),
    )

    result = metrics.collect_metrics.run(config_id)

    assert result["mode"] == "metrics_collection"
    assert result["variables_retired"] == 1
    with sync_session_factory() as session:
        assert session.get(Variable, fossil_id) is None
    assert "Retired 1 unused variables no event refers to" in result["details"]


def test_the_sweep_defers_scalar_derived_rows_and_says_how_many(
    sync_session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One call, both derivations: the JSON key goes, the column stays, and the
    log line says the column was left unjudged rather than found clean.

    ``deferred`` sits beside ``retired``/``planned``/``scanned`` because a line
    reporting a few retired keys would otherwise read as a full pass over the
    catalog. The default — what ``run_scan`` and the danger-zone endpoint mean
    — defers nothing, so the second call takes the row the first one left.

    DISABLE-THE-FIX: drop ``deferred`` from the log ``extra`` and the record
    has no such attribute; filter with ``include_scalar_derived`` inverted and
    the wrong row is deleted.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        json_fossil = _seed_scan_created_variable(
            session, config, source_name="payload.user.adana", column_type="json"
        )
        scalar_fossil = _seed_scan_created_variable(
            session, config, source_name="campaign", column_type="string"
        )
        project_id = config.project_id
        json_fossil_id = json_fossil.id
        scalar_fossil_id = scalar_fossil.id

    with (
        caplog.at_level("INFO", logger="tripl.worker.variable_sweep"),
        sync_session_factory() as session,
    ):
        retired = variable_sweep.retire_unused_variables(
            session, project_id=project_id, branch_id=None, include_scalar_derived=False
        )

    assert retired == 1
    with sync_session_factory() as session:
        assert session.get(Variable, json_fossil_id) is None
        assert session.get(Variable, scalar_fossil_id) is not None

    records = [r for r in caplog.records if r.name == "tripl.worker.variable_sweep"]
    assert len(records) == 1
    record = records[0]
    assert (record.retired, record.planned, record.scanned, record.deferred) == (1, 1, 1, 1)

    # The project can defend scalar retirement only once every config declares
    # a representative window; another config's full-table run is insufficient.
    with sync_session_factory() as session:
        config = session.get(ScanConfig, config.id)
        assert config is not None
        config.scan_lookback_hours = 24
        session.commit()
        assert (
            variable_sweep.retire_unused_variables(session, project_id=project_id, branch_id=None)
            == 1
        )
        assert session.get(Variable, scalar_fossil_id) is None


def test_a_column_that_is_json_on_one_type_and_string_on_another_is_scalar(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A FieldDefinition is per event type, a variable is per branch.

    The same column name can be ``json`` on one type and ``string`` on another,
    and then the variable's token stands in a scalar column's stored value
    somewhere on the branch — the value a narrow window can flip to a literal.
    Conservatively scalar, so a scheduled run without a lookback leaves it.

    DISABLE-THE-FIX: in ``_json_column_names`` make a name JSON when ANY of its
    FieldDefinitions is (``or`` for ``and``) and the row is deleted.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        fossil = _seed_scan_created_variable(
            session, config, source_name="payload.user.adana", column_type="json"
        )
        other_type = EventType(
            id=uuid.uuid4(),
            project_id=config.project_id,
            name="legacy",
            display_name="Legacy",
            description="",
        )
        session.add(other_type)
        session.flush()
        session.add(
            FieldDefinition(
                id=uuid.uuid4(),
                event_type_id=other_type.id,
                name="payload",
                display_name="payload",
                field_type="string",
                is_required=False,
                description="",
            )
        )
        session.commit()
        project_id = config.project_id
        fossil_id = fossil.id

    with sync_session_factory() as session:
        retired = variable_sweep.retire_unused_variables(
            session, project_id=project_id, branch_id=None, include_scalar_derived=False
        )

    assert retired == 0
    with sync_session_factory() as session:
        assert session.get(Variable, fossil_id) is not None


def test_scheduled_run_reindexes_whatever_the_sweep_deferred(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The lookback narrows the sweep alone — the reindex has no such condition.

    Phase 1 syncs the catalog on every non-replay run whatever the window is, so
    the search index must be rebuilt on every non-replay run too. Only the
    DESTRUCTIVE half reads ``catalog_window_declared``, and even that only to
    decide how much it may take.

    DISABLE-THE-FIX: put the reindex under ``if catalog_window_declared:`` and
    ``reindexed`` stays empty.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.scan_lookback_hours is None
        config_id = str(config.id)
        project_id = config.project_id

    reindexed: list[uuid.UUID] = []

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: _SamplingFakeAdapter())
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        metrics,
        "generate_events",
        lambda *args, **kwargs: GenerationResult(columns_analyzed=1),
    )
    monkeypatch.setattr(
        metrics,
        "reindex_main_branch_from_worker",
        lambda session, project_id_arg: reindexed.append(project_id_arg),
    )

    metrics.collect_metrics.run(config_id)

    assert reindexed == [project_id]


def test_bump_event_last_seen_promotes_ready_for_dev_to_live_but_not_drafts(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """tripl-kjhi.6: the handoff goes analyst → developer → data, and nobody
    flips the row to 'implemented' by hand before the first rows land."""
    with sync_session_factory() as session:
        config, _event_type, event = _seed_anomaly_scan_state(session)
        event.status = EventStatus.ready_for_dev
        draft = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=event.event_type_id,
            name="event_name=Draft",
            description="",
            status=EventStatus.draft,
        )
        session.add(draft)
        session.commit()
        bucket = datetime(2026, 5, 1, 12, tzinfo=UTC)

        metrics_collect._bump_event_last_seen(
            session,
            event_agg={(config.id, event.id, bucket): 7, (config.id, draft.id, bucket): 3},
        )
        session.commit()

        session.expire_all()
        assert session.get(Event, event.id).status == EventStatus.live  # type: ignore[union-attr]
        assert session.get(Event, draft.id).status == EventStatus.draft  # type: ignore[union-attr]
        changes = (
            session.execute(select(EventChange).where(EventChange.event_id == event.id))
            .scalars()
            .all()
        )
        assert [(c.field, c.old_value, c.new_value) for c in changes] == [
            ("status", EventStatus.ready_for_dev, EventStatus.live)
        ]
        assert (
            session.execute(select(EventChange).where(EventChange.event_id == draft.id))
            .scalars()
            .all()
            == []
        )
