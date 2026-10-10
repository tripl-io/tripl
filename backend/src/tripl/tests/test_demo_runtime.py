"""Demo runtime tick — fake-clock + multi-worker at the task level.

Exercises ``advance_demos`` against an isolated SQLite engine with an explicit
(fake) clock, mirroring the sync fixtures in ``test_metric_anomaly_scope.py`` /
``test_metrics_tasks.py``. SQLite has no ``pg_advisory_lock`` and no real broker,
so these tests prove correctness comes from the DB unique constraints + idempotent
insert-if-absent, with the advisory lock a production-only optimisation.

Covers: two complete ticks (fresh beyond the 3h horizon), the platform and
app-version split each appended hour carries, concurrency/idempotency (no
duplicate buckets; savepoint-safe after a simulated partial write), retention
caps, pause/resume, and the feature flag off (no-op; never touches non-demo).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.synthetic_traffic import DemoTraffic, stored_traffic
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import (
    MetricComposition,
    MetricKind,
    MetricStatus,
)
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.event_type import EventType
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.plan_branch import PlanBranch
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.services.demo import noise
from tripl.services.demo.breakdowns import (
    APP_VERSION_COLUMN,
    PLATFORM_COLUMN,
    EventVolume,
    breakdown_rows,
)
from tripl.services.demo.scenario import DEMO_SEED
from tripl.worker.tasks import demo_runtime

# Imported from the leaf module that now owns it, not off ``demo_runtime``: the
# constant moved there with the predicate that reads it and is
# deliberately not re-exported, because ruff selects "F" — an unused import there
# would be F401, and an ``import X as X`` alias kept alive for one test line is a
# second name for one truth.
from tripl.worker.tasks._demo_pause import DEMO_IDLE_PAUSE_MINUTES
from tripl.worker.tasks.metrics import detect as metrics_detect

# Two events from the demo roster (names must match ``event_specs``): the spike
# event (base 1800) and a click event (base 300).
_HOME = ("Home Screen View", "screen_view", 1800)
_BUY = ("Buy Button Click", "click", 300)

_HOUR = timedelta(hours=1)
_TOTAL_BUCKETS = noise.DEMO_HISTORY_DAYS * 24


def _floor(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'demo_runtime.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


class _Seeded:
    def __init__(self, project_id: uuid.UUID, scan_config_id: uuid.UUID, slug: str) -> None:
        self.project_id = project_id
        self.scan_config_id = scan_config_id
        self.slug = slug


def _seed_demo(
    session: Session,
    *,
    seed_now: datetime,
    history_hours: int = 72,
    slug: str | None = None,
    last_accessed: datetime | None = None,
    with_conversion_metric: bool = False,
) -> _Seeded:
    """Seed a minimal but faithful demo: project, scan config, two events, and an
    hourly ``EventMetric`` history whose newest bucket sits ~1h before ``seed_now``.

    Volumes come from the same deterministic ``hourly_volume`` the tick uses, so
    appended buckets are indistinguishable in shape from the seeded ones.
    """
    slug = slug or f"demo-{uuid.uuid4().hex[:6]}"
    project = Project(
        id=uuid.uuid4(),
        name="Demo Project",
        slug=slug,
        description="",
        is_demo=True,
        generation_status="ready",
        demo_recipe_version="3",
        demo_seeded_at=seed_now,
        demo_last_accessed_at=last_accessed if last_accessed is not None else seed_now,
    )
    branch = PlanBranch(id=uuid.uuid4(), project_id=project.id, name="main", kind="main")
    data_source = DataSource(
        id=uuid.uuid4(),
        project_id=project.id,
        name=f"Demo warehouse {slug}",
        db_type="synthetic",
        host="synthetic",
        port=0,
        database_name="synthetic",
        username="",
        password_encrypted="",
    )
    scan_config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Demo scan",
        base_query="SELECT * FROM events",
        time_column="event_time",
        interval="1h",
    )
    # The row the monitoring builder seeds: the tick re-scores through the
    # collection's own pass, which reads its thresholds from here.
    anomaly_settings = ProjectAnomalySettings(
        project_id=project.id,
        anomaly_detection_enabled=True,
        detect_project_total=True,
        detect_event_types=True,
        detect_events=True,
        baseline_window_buckets=noise.DEMO_ANOMALY_SETTINGS.baseline_window_buckets,
        min_history_buckets=noise.DEMO_ANOMALY_SETTINGS.min_history_buckets,
        sigma_threshold=noise.DEMO_ANOMALY_SETTINGS.sigma_threshold,
        min_expected_count=noise.DEMO_ANOMALY_SETTINGS.min_expected_count,
        anomaly_ingestion_settling_minutes=0,
    )
    session.add_all([project, branch, data_source, scan_config, anomaly_settings])

    roster: list[tuple[uuid.UUID, uuid.UUID, int, str]] = []
    for name, type_name, base in (_HOME, _BUY):
        event_type = EventType(
            id=uuid.uuid4(),
            project_id=project.id,
            branch_id=branch.id,
            name=type_name,
            display_name=type_name.title(),
        )
        event = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            branch_id=branch.id,
            event_type_id=event_type.id,
            name=name,
        )
        session.add_all([event_type, event])
        roster.append((event.id, event_type.id, base, name))

    conversion_id: uuid.UUID | None = None
    if with_conversion_metric:
        # Buy Button Click per Home Screen View, composed per seeded bucket
        # below as the catalog builder composes the demo's conversion.
        conversion = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project.id,
            name="purchase_conversion",
            display_name="Purchase conversion",
            kind=MetricKind.event_composition.value,
            composition=MetricComposition.ratio.value,
            status=MetricStatus.active.value,
            numerator_event_id=roster[1][0],
            denominator_event_id=roster[0][0],
        )
        session.add(conversion)
        conversion_id = conversion.id

    session.flush()

    grid_start = _floor(seed_now) - timedelta(days=noise.DEMO_HISTORY_DAYS)
    last_bucket = _floor(seed_now) - _HOUR
    bucket = last_bucket - (history_hours - 1) * _HOUR
    while bucket <= last_bucket:
        _seed_bucket(session, scan_config.id, roster, bucket, grid_start, conversion_id)
        bucket += _HOUR
    session.commit()
    return _Seeded(project.id, scan_config.id, slug)


def _seed_bucket(
    session: Session,
    scan_config_id: uuid.UUID,
    roster: list[tuple[uuid.UUID, uuid.UUID, int, str]],
    bucket: datetime,
    grid_start: datetime,
    conversion_id: uuid.UUID | None,
) -> None:
    idx = round((bucket - grid_start).total_seconds() / 3600.0)
    per_type: dict[uuid.UUID, int] = {}
    counts: list[int] = []
    for event_id, event_type_id, base, name in roster:
        seed = noise.derive_seed(DEMO_SEED, name) % 997
        count = noise.hourly_volume(base, bucket, idx, seed, _TOTAL_BUCKETS)
        counts.append(count)
        session.add(
            EventMetric(
                scan_config_id=scan_config_id,
                event_id=event_id,
                event_type_id=None,
                bucket=bucket,
                count=count,
            )
        )
        per_type[event_type_id] = per_type.get(event_type_id, 0) + count
    for event_type_id, total in per_type.items():
        session.add(
            EventMetric(
                scan_config_id=scan_config_id,
                event_id=None,
                event_type_id=event_type_id,
                bucket=bucket,
                count=total,
            )
        )
    if conversion_id is not None:
        session.add(
            MetricValue(
                metric_definition_id=conversion_id,
                scan_config_id=scan_config_id,
                bucket=bucket,
                value=counts[1] / counts[0],
            )
        )


def _run_tick(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, now: datetime
) -> dict[str, object]:
    monkeypatch.setattr(demo_runtime, "_get_sync_session", factory)
    return demo_runtime.advance_demos(now=now)


def _max_bucket(session: Session, scan_config_id: uuid.UUID) -> datetime | None:
    return session.execute(
        select(func.max(EventMetric.bucket)).where(EventMetric.scan_config_id == scan_config_id)
    ).scalar()


def _bucket_set(session: Session, scan_config_id: uuid.UUID) -> set[datetime]:
    return {
        _naive(b)
        for b in session.execute(
            select(EventMetric.bucket)
            .where(
                EventMetric.scan_config_id == scan_config_id,
                EventMetric.event_type_id.isnot(None),
            )
            .distinct()
        ).scalars()
    }


# ── Two complete ticks ───────────────────────────────────────────────────────


def test_two_ticks_append_new_buckets_and_stay_fresh(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_now = _floor(datetime.now(UTC)) - timedelta(days=1)
    with factory() as session:
        seeded = _seed_demo(session, seed_now=seed_now, history_hours=72)
        before = _bucket_set(session, seeded.scan_config_id)

    # First tick 3h later.
    tick1 = seed_now + timedelta(hours=3)
    result1 = _run_tick(factory, monkeypatch, tick1)
    assert result1["advanced"] == 1

    with factory() as session:
        after1 = _bucket_set(session, seeded.scan_config_id)
        assert after1 - before, "first tick added new buckets"
        newest = _naive(_max_bucket(session, seeded.scan_config_id))
        assert newest == _naive(_floor(tick1) - _HOUR)
        # Fresh beyond the 3h signal horizon.
        assert _naive(tick1) - newest <= timedelta(hours=3)
        jobs1 = _scan_job_count(session, seeded.scan_config_id)
        assert jobs1 == 1
        project = session.get(Project, seeded.project_id)
        assert project is not None and project.demo_last_tick_at is not None

    # A viewer is still looking at the demo (the API GET bumps access); model that
    # so the demo stays active across the gap to the second tick.
    with factory() as session:
        project = session.get(Project, seeded.project_id)
        assert project is not None
        project.demo_last_accessed_at = tick1
        session.commit()

    # Second tick 4h after the first.
    tick2 = tick1 + timedelta(hours=4)
    result2 = _run_tick(factory, monkeypatch, tick2)
    assert result2["advanced"] == 1

    with factory() as session:
        after2 = _bucket_set(session, seeded.scan_config_id)
        assert after2 - after1, "second tick added more new buckets"
        newest2 = _naive(_max_bucket(session, seeded.scan_config_id))
        assert newest2 == _naive(_floor(tick2) - _HOUR)
        assert _naive(tick2) - newest2 <= timedelta(hours=3)
        # A NEW completed scan job each tick (real execution, not a rewrite).
        assert _scan_job_count(session, seeded.scan_config_id) == 2
        jobs = (
            session.execute(select(ScanJob).where(ScanJob.scan_config_id == seeded.scan_config_id))
            .scalars()
            .all()
        )
        assert all(j.status == ScanJobStatus.completed.value for j in jobs)
        assert all((j.result_summary or {}).get("demo_runtime_tick") for j in jobs)


def _scan_job_count(session: Session, scan_config_id: uuid.UUID) -> int:
    return int(
        session.execute(
            select(func.count())
            .select_from(ScanJob)
            .where(ScanJob.scan_config_id == scan_config_id)
        ).scalar()
        or 0
    )


def test_tick_appends_coverage_and_leaves_the_conversion_to_its_collector(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tick appends coverage with the volume, but writes no catalog-metric
    value: the conversion re-derives from the event series, so appending a newer
    numerator bucket is what makes the real collector due for it. A value the
    tick wrote itself would hide that from ``_event_composition_due`` and show a
    number the two event charts beside it do not divide to."""
    from tripl.models.coverage_metric import CoverageMetric
    from tripl.worker.tasks.metrics.schedule import _event_composition_due

    seed_now = _floor(datetime.now(UTC)) - timedelta(days=1)
    with factory() as session:
        seeded = _seed_demo(
            session, seed_now=seed_now, history_hours=48, with_conversion_metric=True
        )
        conversion = session.execute(
            select(MetricDefinition).where(MetricDefinition.project_id == seeded.project_id)
        ).scalar_one()
        assert not _event_composition_due(session, conversion)

    tick = seed_now + timedelta(hours=5)
    _run_tick(factory, monkeypatch, tick)

    with factory() as session:
        coverage = session.execute(
            select(func.count())
            .select_from(CoverageMetric)
            .where(CoverageMetric.scan_config_id == seeded.scan_config_id)
        ).scalar()
        assert coverage and coverage >= 1, "coverage rows appended for new buckets"
        conv_values = session.execute(select(func.count()).select_from(MetricValue)).scalar()
        assert conv_values == 48, "the tick wrote a conversion value of its own"
        conversion = session.execute(
            select(MetricDefinition).where(MetricDefinition.project_id == seeded.project_id)
        ).scalar_one()
        assert _event_composition_due(session, conversion)


def test_tick_continues_the_seeded_platform_and_version_split(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each appended hour carries every event's platform and app-version split from
    the traffic model the demo was seeded with — the rows the seeder writes for an
    hour — so the version charts run on past the seed clock instead of stopping
    there. A source seeded before it named that model is stamped with it, so the
    next scheduled collection serves the same split."""
    seed_now = _floor(datetime.now(UTC)) - timedelta(days=1)
    with factory() as session:
        seeded = _seed_demo(session, seed_now=seed_now, history_hours=48)
        scan_config = session.get(ScanConfig, seeded.scan_config_id)
        assert scan_config is not None
        scan_config.platform_column = PLATFORM_COLUMN
        scan_config.app_version_column = APP_VERSION_COLUMN
        data_source_id = scan_config.data_source_id
        session.commit()

    # Appends the three hours from the seed clock on.
    _run_tick(factory, monkeypatch, seed_now + timedelta(hours=3))

    traffic = DemoTraffic(anchor=seed_now, seed=DEMO_SEED)
    bases = {name: base for name, _type_name, base in (_HOME, _BUY)}
    with factory() as session:
        source = session.get(DataSource, data_source_id)
        assert source is not None
        assert stored_traffic(source.extra_params) == traffic

        type_names = dict(
            session.execute(
                select(EventType.id, EventType.name).where(
                    EventType.project_id == seeded.project_id
                )
            ).all()
        )
        events = session.execute(
            select(Event.id, Event.event_type_id, Event.name).where(
                Event.project_id == seeded.project_id
            )
        ).all()
        expected: dict[tuple[object, ...], object] = {}
        for hours in range(3):
            bucket = seed_now + hours * _HOUR
            volumes = [
                EventVolume(
                    event_id=event_id,
                    event_type_id=event_type_id,
                    name=name,
                    event_type=type_names[event_type_id],
                    base=bases[name],
                    count=traffic.volume(bases[name], name, bucket),
                )
                for event_id, event_type_id, name in events
            ]
            for row in breakdown_rows(
                traffic, bucket, volumes, scan_config_id=seeded.scan_config_id
            ):
                key = (
                    row["event_id"],
                    row["event_type_id"],
                    _naive(bucket),
                    row["breakdown_column"],
                    row["breakdown_value"],
                )
                expected[key] = row["count"]
        stored = {
            (event_id, event_type_id, _naive(bucket), column, value): count
            for event_id, event_type_id, bucket, column, value, count in session.execute(
                select(
                    EventMetricBreakdown.event_id,
                    EventMetricBreakdown.event_type_id,
                    EventMetricBreakdown.bucket,
                    EventMetricBreakdown.breakdown_column,
                    EventMetricBreakdown.breakdown_value,
                    EventMetricBreakdown.count,
                ).where(EventMetricBreakdown.scan_config_id == seeded.scan_config_id)
            ).all()
        }

    assert {key[3] for key in stored} == {PLATFORM_COLUMN, APP_VERSION_COLUMN}
    assert stored == expected


# ── Concurrency / idempotency ────────────────────────────────────────────────


def test_concurrent_ticks_same_clock_are_idempotent(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_now = _floor(datetime.now(UTC)) - timedelta(days=1)
    with factory() as session:
        seeded = _seed_demo(session, seed_now=seed_now, history_hours=48)

    tick = seed_now + timedelta(hours=4)
    # Two workers at the SAME fake clock (advisory lock is a no-op on SQLite, so
    # the unique constraints must carry idempotency).
    _run_tick(factory, monkeypatch, tick)
    with factory() as session:
        rows_after_first = session.execute(
            select(func.count())
            .select_from(EventMetric)
            .where(EventMetric.scan_config_id == seeded.scan_config_id)
        ).scalar()

    _run_tick(factory, monkeypatch, tick)
    with factory() as session:
        rows_after_second = session.execute(
            select(func.count())
            .select_from(EventMetric)
            .where(EventMetric.scan_config_id == seeded.scan_config_id)
        ).scalar()
        # ONE logical result: the second run added nothing.
        assert rows_after_second == rows_after_first
        # No duplicate per-type row for any bucket.
        dupes = session.execute(
            select(EventMetric.bucket, func.count())
            .where(
                EventMetric.scan_config_id == seeded.scan_config_id,
                EventMetric.event_type_id.isnot(None),
            )
            .group_by(EventMetric.bucket, EventMetric.event_type_id)
            .having(func.count() > 1)
        ).all()
        assert not dupes


def test_partial_write_is_savepoint_safe(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing per-event row for a would-be-new bucket forces an
    IntegrityError on that bucket; the savepoint rolls it back and the tick keeps
    going — no crash, no duplicate, the other new buckets still land."""
    seed_now = _floor(datetime.now(UTC)) - timedelta(days=1)
    with factory() as session:
        seeded = _seed_demo(session, seed_now=seed_now, history_hours=48)
        roster_event_id = (
            session.execute(
                select(EventMetric.event_id).where(
                    EventMetric.scan_config_id == seeded.scan_config_id,
                    EventMetric.event_id.isnot(None),
                )
            )
            .scalars()
            .first()
        )
        # A bucket that WILL be new at tick time (2h after seed), pre-populated
        # with only a per-EVENT row (the pre-check looks for per-TYPE rows, so it
        # won't skip this bucket — the per-event insert collides instead).
        collide_bucket = _floor(seed_now) + _HOUR
        session.add(
            EventMetric(
                scan_config_id=seeded.scan_config_id,
                event_id=roster_event_id,
                event_type_id=None,
                bucket=collide_bucket,
                count=123,
            )
        )
        session.commit()

    tick = seed_now + timedelta(hours=4)
    result = _run_tick(factory, monkeypatch, tick)
    assert result["advanced"] == 1  # did not crash

    with factory() as session:
        # The collided bucket was skipped: no per-type row was written for it.
        per_type = session.execute(
            select(func.count())
            .select_from(EventMetric)
            .where(
                EventMetric.scan_config_id == seeded.scan_config_id,
                EventMetric.event_type_id.isnot(None),
                EventMetric.bucket == collide_bucket,
            )
        ).scalar()
        assert per_type == 0
        # The pre-existing per-event row is intact and not duplicated.
        collided = session.execute(
            select(func.count())
            .select_from(EventMetric)
            .where(
                EventMetric.scan_config_id == seeded.scan_config_id,
                EventMetric.event_id == roster_event_id,
                EventMetric.bucket == collide_bucket,
            )
        ).scalar()
        assert collided == 1
        # Freshness still reached (later buckets landed fine).
        newest = _naive(_max_bucket(session, seeded.scan_config_id))
        assert newest == _naive(_floor(tick) - _HOUR)


# ── Retention ────────────────────────────────────────────────────────────────


def test_retention_caps_row_counts(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seed a demo whose history stretches well beyond the retention window.
    seed_now = _floor(datetime.now(UTC)) - timedelta(days=1)
    over_window_hours = (noise.DEMO_HISTORY_DAYS + 7) * 24
    with factory() as session:
        seeded = _seed_demo(session, seed_now=seed_now, history_hours=over_window_hours)
        oldest_before = session.execute(
            select(func.min(EventMetric.bucket)).where(
                EventMetric.scan_config_id == seeded.scan_config_id
            )
        ).scalar()

    tick = seed_now + timedelta(hours=3)
    _run_tick(factory, monkeypatch, tick)

    with factory() as session:
        oldest_after = _naive(
            session.execute(
                select(func.min(EventMetric.bucket)).where(
                    EventMetric.scan_config_id == seeded.scan_config_id
                )
            ).scalar()
        )
        cutoff = _naive(_floor(tick) - timedelta(days=demo_runtime.DEMO_RETENTION_DAYS))
        # Oldest surviving bucket is within the retention window; older ones pruned.
        assert oldest_after >= cutoff
        assert _naive(oldest_before) < cutoff
        # The rolling window is bounded: distinct buckets never exceed the window.
        distinct_buckets = session.execute(
            select(func.count(func.distinct(EventMetric.bucket))).where(
                EventMetric.scan_config_id == seeded.scan_config_id
            )
        ).scalar()
        assert distinct_buckets <= demo_runtime.DEMO_RETENTION_DAYS * 24 + 1


# ── Pause / resume ───────────────────────────────────────────────────────────


def test_paused_demo_is_skipped_and_resumes_on_access(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_now = _floor(datetime.now(UTC)) - timedelta(days=2)
    stale_access = seed_now - timedelta(minutes=DEMO_IDLE_PAUSE_MINUTES + 60)
    with factory() as session:
        seeded = _seed_demo(
            session, seed_now=seed_now, history_hours=48, last_accessed=stale_access
        )
        before = _bucket_set(session, seeded.scan_config_id)

    tick = seed_now + timedelta(hours=3)
    result = _run_tick(factory, monkeypatch, tick)
    assert result["advanced"] == 0
    assert result["skipped"] == 1

    with factory() as session:
        after = _bucket_set(session, seeded.scan_config_id)
        assert after == before, "paused demo was not advanced"

    # Resume: touch access to 'now', then a later tick catches it up.
    with factory() as session:
        project = session.get(Project, seeded.project_id)
        assert project is not None
        project.demo_last_accessed_at = tick
        session.commit()

    tick2 = tick + timedelta(hours=1)
    result2 = _run_tick(factory, monkeypatch, tick2)
    assert result2["advanced"] == 1

    with factory() as session:
        resumed = _bucket_set(session, seeded.scan_config_id)
        assert resumed - before, "resumed demo advanced after access"


# ── Deadlock resilience ───────────────────────────────────────


def test_persistent_deadlock_gives_up_and_skips_demo(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deadlock that recurs on EVERY attempt: ``advance_demos`` retries the whole
    per-demo advance a bounded number of times, then SKIPS this demo — never aborts
    the sweep, never leaks the exception, never spins unbounded.

    The loop is monkeypatched at ``_advance_demo`` (the whole per-demo unit) rather
    than at ``_recompute_anomalies`` so the retry contract is asserted on its own:
    injecting the failure mid-``_advance_demo`` would entangle it with the partial
    ``_append_buckets`` writes and SQLite savepoint semantics the loop does not
    depend on. The re-raise discrimination inside ``_recompute_anomalies`` is pinned
    separately by ``test_recompute_anomalies_reraises_dbapi_error``.
    """
    seed_now = _floor(datetime.now(UTC)) - timedelta(days=1)
    with factory() as session:
        seeded = _seed_demo(session, seed_now=seed_now, history_hours=72)
        before = _bucket_set(session, seeded.scan_config_id)

    attempts = 0

    def _always_deadlock(*_args: object, **_kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        raise OperationalError("UPDATE metric_anomalies", {}, Exception("deadlock detected"))

    monkeypatch.setattr(demo_runtime, "_advance_demo", _always_deadlock)

    tick = seed_now + timedelta(hours=3)
    result = _run_tick(factory, monkeypatch, tick)

    # Bounded retries, then skip; the sweep returned without raising.
    assert result["advanced"] == 0
    assert result["skipped"] == 1
    assert attempts == demo_runtime.DEMO_ADVANCE_MAX_RETRIES

    with factory() as session:
        after = _bucket_set(session, seeded.scan_config_id)
        assert after == before  # nothing committed for this demo
        project = session.get(Project, seeded.project_id)
        assert project is not None and project.demo_last_tick_at is None


def test_transient_deadlock_retries_then_advances(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deadlock that CLEARS on retry (Postgres kills one side of the row race): the
    per-demo advance is retried and the demo advances normally — not skipped. This is
    the recovery the P0 fix restores; the old code rolled the whole tick back and left
    the demo frozen.
    """
    seed_now = _floor(datetime.now(UTC)) - timedelta(days=1)
    with factory() as session:
        seeded = _seed_demo(session, seed_now=seed_now, history_hours=72)

    real_advance = demo_runtime._advance_demo
    calls = 0

    def _deadlock_once(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("UPDATE metric_anomalies", {}, Exception("deadlock detected"))
        real_advance(*args, **kwargs)

    monkeypatch.setattr(demo_runtime, "_advance_demo", _deadlock_once)

    tick = seed_now + timedelta(hours=3)
    result = _run_tick(factory, monkeypatch, tick)

    # Retried once, then the real advance ran and the demo advanced.
    assert result["advanced"] == 1
    assert result["skipped"] == 0
    assert calls == 2

    with factory() as session:
        project = session.get(Project, seeded.project_id)
        assert project is not None and project.demo_last_tick_at is not None


def test_non_db_error_in_recompute_is_swallowed_and_tick_commits(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detector/logic bug (a non-DBAPI exception) does not break the transaction.

    Every statement before it succeeded, so ``_recompute_anomalies`` logs and
    swallows it: a detector bug must not kill the tick. The appended series still
    commits and the demo advances normally.
    """
    seed_now = _floor(datetime.now(UTC)) - timedelta(days=1)
    with factory() as session:
        seeded = _seed_demo(session, seed_now=seed_now, history_hours=72)
        before = _bucket_set(session, seeded.scan_config_id)

    def _detector_bug(*_args: object, **_kwargs: object) -> None:
        raise ValueError("detector logic bug")

    # The tick scores through the collection's own pass, which calls the
    # detector off its module's globals.
    monkeypatch.setattr(metrics_detect, "detect_anomalies", _detector_bug)

    tick = seed_now + timedelta(hours=3)
    result = _run_tick(factory, monkeypatch, tick)

    # The tick still advanced: the detector bug was swallowed, not propagated.
    assert result["advanced"] == 1
    assert result["skipped"] == 0

    with factory() as session:
        after = _bucket_set(session, seeded.scan_config_id)
        assert after - before, "appended buckets committed despite the detector bug"
        project = session.get(Project, seeded.project_id)
        assert project is not None and project.demo_last_tick_at is not None


def test_recompute_anomalies_reraises_dbapi_error() -> None:
    """A DBAPI error from ``session.execute`` (a real deadlock) POISONS the tick's
    transaction, so ``_recompute_anomalies`` must RE-RAISE it rather than swallow it.

    Swallowing would let ``_advance_demo`` fall through to ``_prune_retention``, whose
    next execute raises ``InFailedSqlTransaction`` and rolls back the whole tick — the
    exact P0 that froze the demo. This pins the except-clause discrimination directly
    on ``_recompute_anomalies`` (the retry path in ``advance_demos`` is covered
    separately); a non-DBAPI detector bug is still swallowed, see
    ``test_non_db_error_in_recompute_is_swallowed_and_tick_commits``.
    """

    class _BoomSession:
        def execute(self, *_args: object, **_kwargs: object) -> None:
            raise OperationalError(
                "SELECT project_anomaly_settings", {}, Exception("deadlock detected")
            )

    config = ScanConfig(id=uuid.uuid4(), project_id=uuid.uuid4(), interval="1h")
    with pytest.raises(OperationalError):
        demo_runtime._recompute_anomalies(
            _BoomSession(),  # type: ignore[arg-type]
            config,
            datetime.now(UTC),
        )


# ── Feature flag off ─────────────────────────────────────────────────────────


def test_flag_off_is_noop(factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> None:
    seed_now = _floor(datetime.now(UTC)) - timedelta(days=1)
    with factory() as session:
        seeded = _seed_demo(session, seed_now=seed_now, history_hours=48)
        before = _bucket_set(session, seeded.scan_config_id)

    monkeypatch.setattr(demo_runtime.settings, "demo_runtime_enabled", False)
    result = _run_tick(factory, monkeypatch, seed_now + timedelta(hours=3))
    assert result == {"enabled": False, "advanced": 0, "skipped": 0}

    with factory() as session:
        after = _bucket_set(session, seeded.scan_config_id)
        assert after == before, "flag off: nothing appended"


def test_tick_never_touches_non_demo_projects(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_now = _floor(datetime.now(UTC)) - timedelta(days=1)
    with factory() as session:
        demo = _seed_demo(session, seed_now=seed_now, history_hours=48, slug="a-demo")
        real = _seed_demo(session, seed_now=seed_now, history_hours=48, slug="real-proj")
        # Flip the second project to a REAL project (not a demo).
        project = session.get(Project, real.project_id)
        assert project is not None
        project.is_demo = False
        session.commit()
        real_before = _bucket_set(session, real.scan_config_id)

    result = _run_tick(factory, monkeypatch, seed_now + timedelta(hours=3))
    # Only the demo advanced.
    assert result["advanced"] == 1

    with factory() as session:
        assert _bucket_set(session, real.scan_config_id) == real_before, (
            "real project's scan series untouched"
        )
        demo_after = _bucket_set(session, demo.scan_config_id)
        assert len(demo_after) > 48
        # The real project never gets a demo-runtime scan job.
        assert _scan_job_count(session, real.scan_config_id) == 0
