"""Per-metric anomaly coverage, and the cost of computing it.

Two defects on the same seam:

* every monitored catalog metric was handed the RUNNING scan's
  ``covered_buckets`` while being scored on its OWN grid, so a metric on a
  different interval had its series decimated by ``expand_series`` and a metric
  sourced from a different scan config was judged by someone else's outages.
  ``_replace_scope_anomalies`` then rewrote the metric's whole trailing window
  from whatever survived.
* ``covered_buckets_from_scan_jobs`` read every completed
  ``ScanJob`` and every distinct ``EventMetric.bucket`` for a config's entire
  lifetime on every scheduled collection, to build a set the detector only ever
  consults back to its own ``history_from``.

Sync sqlite fixtures mirror ``test_metric_anomaly_scope.py``.
"""

import contextlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.analyzers.anomaly_detector import required_history_buckets
from tripl.core.bucketing import to_utc
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import (
    MetricAggregation,
    MetricComposition,
    MetricKind,
    MetricStatus,
)
from tripl.models.event_metric import EventMetric
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.worker.tasks.metrics import detect as metrics_detect
from tripl.worker.tasks.metrics.coverage import covered_buckets_from_scan_jobs

# Hourly UTC buckets, anchored inside the freshness horizon.
_HOUR = timedelta(hours=1)
_BASE = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=12)
_SPIKE_HOUR = 9
_EVAL_FROM = _BASE + _HOUR * 8
_EVAL_TO = _BASE + _HOUR * 10

# Fixed, tz-AWARE anchor for the direct ``covered_buckets_from_scan_jobs`` tests.
# EVERY bucket that function returns is aware UTC — recorded job windows, the
# caller's own window and the stored-bucket read alike — so an assertion on the
# returned set has to be aware to compare at all.
_JOB_BASE = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
# The same instant as ``_JOB_BASE``, spelled the way a naive producer spells it:
# a bucket column read on a backend without timezone support, or a window a
# caller minted off a naive clock. It is NOT equal to ``_JOB_BASE`` — that
# inequality is the whole defect — so it exists to be passed IN, never to appear
# in an expectation.
_JOB_BASE_NAIVE = _JOB_BASE.replace(tzinfo=None)
# The third spelling a caller can arrive in: aware, but not UTC. It is equal
# to its UTC twin and hashes alike (Python keys aware datetimes by INSTANT), so
# it is harmless at a comparison — and NOT harmless at a boundary, because a
# bound handed to a backend without timezone support is stored and compared by
# its PRINTED FIELDS, which read two hours off.
_PLUS_TWO = timezone(timedelta(hours=2))


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch3_a1.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


@contextlib.contextmanager
def _captured_reads(engine: Engine) -> Iterator[list[tuple[str, Any]]]:
    """Every statement ``engine`` executes inside the block, with its parameters.

    Keeping the bound parameters alongside the SQL is the point: it makes a
    captured SELECT RE-RUNNABLE, so a test can count the rows the module's own
    read returned instead of asserting on the shape of its SQL. Same
    ``before_cursor_execute`` hook ``test_batch3_e1`` uses to pin statement
    counts.
    """
    executed: list[tuple[str, Any]] = []

    def _record(
        _conn: object,
        _cursor: object,
        statement: str,
        parameters: Any,
        _context: object,
        _executemany: bool,
    ) -> None:
        executed.append((statement, parameters))

    sa_event.listen(engine, "before_cursor_execute", _record)
    try:
        yield executed
    finally:
        sa_event.remove(engine, "before_cursor_execute", _record)


def _scan_job_reads(executed: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """The captured statements that read ``scan_jobs``."""
    return [
        (statement, parameters)
        for statement, parameters in executed
        if statement.lstrip().upper().startswith("SELECT") and "scan_jobs" in statement
    ]


def _seed_project(
    session: Session,
    *,
    interval: str = "1h",
    with_settings: bool = True,
) -> ScanConfig:
    project = Project(
        id=uuid.uuid4(),
        name="Batch3 A1",
        slug=f"batch3-a1-{uuid.uuid4().hex[:8]}",
        description="",
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"DS {uuid.uuid4().hex[:8]}",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    config = _make_config(project.id, data_source.id, interval=interval)
    session.add_all([project, data_source, config])
    if with_settings:
        session.add(
            ProjectAnomalySettings(
                project_id=project.id,
                anomaly_detection_enabled=True,
                detect_metrics=True,
                # Pinned so the small crafted series (baseline ~10) stay
                # eligible; these tests exercise which coverage set reaches a
                # metric, not the product defaults.
                sigma_threshold=3.0,
                min_expected_count=10,
            )
        )
    session.commit()
    return config


def _make_config(
    project_id: uuid.UUID,
    data_source_id: uuid.UUID,
    *,
    interval: str = "1h",
) -> ScanConfig:
    return ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source_id,
        project_id=project_id,
        name=f"Scan {uuid.uuid4().hex[:6]}",
        base_query="SELECT time, event_name FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval=interval,
    )


def _add_sibling_config(
    session: Session, config: ScanConfig, *, interval: str = "1h"
) -> ScanConfig:
    """A second scan config in the SAME project, on the same data source."""
    sibling = _make_config(config.project_id, config.data_source_id, interval=interval)
    session.add(sibling)
    session.commit()
    return sibling


def _add_metric(
    session: Session,
    config: ScanConfig,
    *,
    kind: MetricKind,
    name: str,
    aggregation: MetricAggregation | None = None,
    composition: MetricComposition | None = None,
    interval: str | None = "1h",
) -> MetricDefinition:
    metric = MetricDefinition(
        id=uuid.uuid4(),
        project_id=config.project_id,
        name=name,
        display_name=name.upper(),
        kind=kind.value,
        aggregation=aggregation.value if aggregation else None,
        composition=composition.value if composition else None,
        config={},
        data_source_id=config.data_source_id,
        interval=interval,
        status=MetricStatus.active.value,
        anomaly_detection_enabled=True,
    )
    session.add(metric)
    session.commit()
    return metric


def _seed_values(
    session: Session,
    metric: MetricDefinition,
    values: dict[int, float],
    *,
    scan_config_id: uuid.UUID | None = None,
) -> None:
    for hour, value in values.items():
        session.add(
            MetricValue(
                id=uuid.uuid4(),
                metric_definition_id=metric.id,
                scan_config_id=scan_config_id,
                bucket=_BASE + _HOUR * hour,
                value=value,
            )
        )
    session.commit()


def _seed_event_metric_buckets(
    session: Session, scan_config_id: uuid.UUID, buckets: list[datetime]
) -> None:
    """Stored buckets for a config — the presence half of its coverage."""
    for bucket in buckets:
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=scan_config_id,
                event_id=None,
                event_type_id=None,
                bucket=bucket,
                count=10,
            )
        )
    session.commit()


def _seed_event_metrics(session: Session, scan_config_id: uuid.UUID, hours: list[int]) -> None:
    """Stored buckets on the ``_BASE`` grid the detection fixtures share."""
    _seed_event_metric_buckets(session, scan_config_id, [_BASE + _HOUR * hour for hour in hours])


def _add_completed_job(
    session: Session,
    scan_config_id: uuid.UUID,
    *,
    created_at: datetime,
    window: tuple[datetime, datetime],
) -> None:
    session.add(
        ScanJob(
            id=uuid.uuid4(),
            scan_config_id=scan_config_id,
            status=ScanJobStatus.completed.value,
            created_at=created_at,
            result_summary={
                "time_from": window[0].isoformat(),
                "time_to": window[1].isoformat(),
            },
        )
    )
    session.commit()


def _metric_anomalies(session: Session, metric_id: uuid.UUID) -> list[MetricAnomaly]:
    return list(
        session.execute(
            select(MetricAnomaly)
            .where(
                MetricAnomaly.scope_type == "metric",
                MetricAnomaly.scope_ref == str(metric_id),
            )
            .order_by(MetricAnomaly.bucket)
        ).scalars()
    )


# --------------------------------------------------------------------------
# coverage is resolved per metric, on the metric's own grid
# --------------------------------------------------------------------------


def test_fact_metric_ignores_the_running_scans_coverage(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A ``fact`` metric collects on its own schedule with ``scan_config_id``
    NULL, so no scan job ever recorded a window for it and the running scan's
    coverage says nothing about it.

    Handed that set anyway, ``expand_series`` kept only the buckets the SCAN
    happened to cover — one, for a scan on a coarser grid — and the metric's
    whole series collapsed to a single point that can never score.
    """
    with sync_session_factory() as session:
        config = _seed_project(session)
        metric = _add_metric(
            session,
            config,
            kind=MetricKind.fact,
            aggregation=MetricAggregation.count,
            composition=MetricComposition.single,
            name="signups",
        )
        # Flat baseline (10) for 9 buckets, a clear spike (100) at the last.
        series = {hour: 10.0 for hour in range(_SPIKE_HOUR)}
        series[_SPIKE_HOUR] = 100.0
        _seed_values(session, metric, series)

        metrics_detect._recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=_EVAL_FROM,
            evaluation_end=_EVAL_TO,
            # What a coarser scan grid produces: one bucket in the metric's
            # whole trailing window.
            covered_buckets={_BASE},
        )
        session.commit()

        anomalies = _metric_anomalies(session, metric.id)

    assert len(anomalies) == 1
    assert anomalies[0].direction == "spike"
    assert anomalies[0].bucket == _BASE + _HOUR * _SPIKE_HOUR


def test_event_composition_metric_uses_its_source_scans_coverage(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """An ``event_composition`` metric inherits the grid of the scan its values
    were collected under, so that scan's coverage is the one that describes its
    gaps — not the coverage of whichever config happens to be running.

    Source scan A was down for hours 7 and 8. Running scan B covered them
    honestly, and lending B's set to the metric zero-filled A's outage into a
    drop to zero.
    """
    with sync_session_factory() as session:
        running = _seed_project(session)
        source = _add_sibling_config(session, running)
        metric = _add_metric(
            session,
            running,
            kind=MetricKind.event_composition,
            composition=MetricComposition.single,
            name="checkouts",
            interval=None,
        )
        collected = [0, 1, 2, 3, 4, 5, 6, _SPIKE_HOUR]
        values = {hour: 10.0 for hour in collected}
        values[_SPIKE_HOUR] = 100.0
        _seed_values(session, metric, values, scan_config_id=source.id)
        # A's own stored buckets and recorded window: hours 7 and 8 are in
        # neither, because A never collected them.
        _seed_event_metrics(session, source.id, collected)
        _add_completed_job(
            session,
            source.id,
            created_at=_BASE,
            window=(_BASE, _BASE + _HOUR * 7),
        )

        metrics_detect._recalculate_metric_anomalies(
            session,
            running,
            evaluation_start=_EVAL_FROM,
            evaluation_end=_EVAL_TO,
            # B's honest coverage of its OWN grid — every hour of the window.
            covered_buckets={_BASE + _HOUR * hour for hour in range(10)},
        )
        session.commit()

        anomalies = _metric_anomalies(session, metric.id)

    # The spike is still scored (the pass ran), and A's uncollected hours are
    # excluded from the series instead of read as a collapse to zero.
    assert [(a.bucket, a.direction) for a in anomalies] == [(_BASE + _HOUR * _SPIKE_HOUR, "spike")]


def test_metric_on_the_running_scans_grid_still_inherits_its_coverage(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Guard on the over-correction: a metric whose resolved grid IS the running
    scan's grid must keep using that scan's set, which uniquely carries the
    window this run just wrote. Dropping it would zero-fill the scan's own
    outage back into a fake drop.
    """
    with sync_session_factory() as session:
        config = _seed_project(session)
        metric = _add_metric(
            session,
            config,
            kind=MetricKind.event_composition,
            composition=MetricComposition.single,
            name="checkouts",
            interval=None,
        )
        collected = [0, 1, 2, 3, 4, 5, 6, _SPIKE_HOUR]
        values = {hour: 10.0 for hour in collected}
        values[_SPIKE_HOUR] = 100.0
        _seed_values(session, metric, values, scan_config_id=config.id)

        metrics_detect._recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=_EVAL_FROM,
            evaluation_end=_EVAL_TO,
            covered_buckets={_BASE + _HOUR * hour for hour in collected},
        )
        session.commit()

        anomalies = _metric_anomalies(session, metric.id)

    assert [(a.bucket, a.direction) for a in anomalies] == [(_BASE + _HOUR * _SPIKE_HOUR, "spike")]


# --------------------------------------------------------------------------
# the coverage read is bounded by the detector's own horizon
# --------------------------------------------------------------------------


def test_covered_buckets_drops_jobs_older_than_the_history_horizon(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A completed job from two years ago is below every pass's ``history_from``
    and must not be READ — so the claim is asserted on the READ.

    No assertion on the returned set can substantiate it. The enumeration clamp
    advances every recorded window up to ``history_from`` before the loop body
    runs, so a two-year-old window contributes no bucket whether its row was
    fetched or not: delete ``ScanJob.created_at >= history_from`` from
    ``covered_buckets_from_scan_jobs`` and the five output assertions below still
    hold against a byte-identical set (measured, not reasoned about). What that
    deletion costs is the job half of tripl-0zpq.25 — an hourly config a year old
    re-reads ~8,800 completed ``result_summary`` blobs on every scheduled
    collection — and it is invisible to output, because widening the row set can
    only ADD windows that the clamp then bounds from below.

    So the load-bearing assertion re-runs the statement the module actually
    emitted and counts its rows. That also pins the bound's VALUE, not just its
    presence: a filter weakened to any horizon below ``ancient`` fetches two
    rows and fails here.
    """
    with sync_session_factory() as session:
        config = _seed_project(session)
        ancient = _JOB_BASE - timedelta(days=730)
        _add_completed_job(
            session,
            config.id,
            created_at=ancient,
            window=(ancient, ancient + _HOUR * 3),
        )
        _add_completed_job(
            session,
            config.id,
            created_at=_JOB_BASE + _HOUR,
            window=(_JOB_BASE + _HOUR, _JOB_BASE + _HOUR * 4),
        )

        engine = session.get_bind()
        assert isinstance(engine, Engine)
        with _captured_reads(engine) as executed:
            covered = covered_buckets_from_scan_jobs(
                session,
                scan_config_id=config.id,
                delta=_HOUR,
                history_from=_JOB_BASE,
                current_window=(_JOB_BASE + _HOUR * 10, _JOB_BASE + _HOUR * 11),
            )

        reads = _scan_job_reads(executed)
        assert len(reads) == 1, reads
        statement, parameters = reads[0]
        # Re-running the emitted read is what makes "must not be read at all"
        # falsifiable: two completed jobs exist for this config and only the one
        # at or above the horizon may come back.
        rows = session.connection().exec_driver_sql(statement, parameters).fetchall()
        assert len(rows) == 1, rows

    # The output half, unchanged and still true — it pins the clamp, not the row
    # filter (see ``test_covered_buckets_does_not_enumerate_below_the_horizon``).
    assert _JOB_BASE + _HOUR in covered
    assert _JOB_BASE + _HOUR * 3 in covered
    assert _JOB_BASE + _HOUR * 10 in covered  # the current run's window
    assert ancient not in covered
    assert min(covered) >= _JOB_BASE


def test_covered_buckets_keeps_the_oldest_bucket_the_detector_reads(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The dangerous direction is under-coverage: a bucket missing from the set
    is EXCLUDED from the series rather than zero-filled, so a genuinely-zero
    bucket at the horizon would silently leave every baseline.

    A job whose window straddles the horizon keeps every bucket at or above it,
    still on the ``window_from + n * delta`` grid — the horizon itself need not
    sit on that grid.
    """
    with sync_session_factory() as session:
        config = _seed_project(session)
        # Deliberately off the horizon's grid, so a floor that merely truncates
        # would land BELOW ``history_from``.
        window_from = _JOB_BASE - timedelta(hours=5, minutes=30)
        _add_completed_job(
            session,
            config.id,
            created_at=_JOB_BASE,
            window=(window_from, _JOB_BASE + _HOUR * 3),
        )

        covered = covered_buckets_from_scan_jobs(
            session,
            scan_config_id=config.id,
            delta=_HOUR,
            history_from=_JOB_BASE,
            current_window=(_JOB_BASE + _HOUR * 10, _JOB_BASE + _HOUR * 11),
        )

    from_job = {bucket for bucket in covered if bucket < _JOB_BASE + _HOUR * 4}
    assert from_job == {window_from + _HOUR * step for step in range(6, 9)}  # 00:30, 01:30, 02:30
    assert min(from_job) >= _JOB_BASE
    assert all((bucket - window_from) % _HOUR == timedelta(0) for bucket in from_job)


def test_covered_buckets_does_not_enumerate_below_the_horizon(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A replay job has a RECENT ``created_at`` and a multi-year recorded window,
    so the ``created_at`` filter alone does not bound the enumeration."""
    with sync_session_factory() as session:
        config = _seed_project(session)
        _add_completed_job(
            session,
            config.id,
            created_at=_JOB_BASE + _HOUR,
            window=(_JOB_BASE - timedelta(days=1095), _JOB_BASE + _HOUR * 2),
        )

        covered = covered_buckets_from_scan_jobs(
            session,
            scan_config_id=config.id,
            delta=_HOUR,
            history_from=_JOB_BASE,
            current_window=(_JOB_BASE + _HOUR * 10, _JOB_BASE + _HOUR * 11),
        )

    assert min(covered) >= _JOB_BASE
    # Two buckets from the replay window plus the one the current run wrote,
    # not the ~26k the unbounded loop enumerated.
    assert covered == {_JOB_BASE, _JOB_BASE + _HOUR, _JOB_BASE + _HOUR * 10}


def test_covered_buckets_bounds_stored_buckets_by_the_horizon(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The stored-bucket DISTINCT is the larger of the two reads (one row per
    event per bucket) and was bounded above only.

    The seeded column IS naive here, because sqlite hands ``EventMetric.bucket``
    back without tzinfo — and the expectation is aware anyway, because
    ``covered_buckets_from_scan_jobs`` stamps that read onto the subsystem's one
    comparison convention (aware UTC) on the way in. That is the whole point of
    the convention: the caller does not have to know which backend answered.
    """
    with sync_session_factory() as session:
        config = _seed_project(session)
        _seed_event_metrics(session, config.id, [-100, 2])

        covered = covered_buckets_from_scan_jobs(
            session,
            scan_config_id=config.id,
            delta=_HOUR,
            history_from=_BASE,
            # A foreign source config claims no current window; presence alone
            # bounds the read from above.
            presence_before=_BASE + _HOUR * 10,
        )

    assert covered == {to_utc(_BASE + _HOUR * 2)}


def test_covered_buckets_requires_a_window_or_a_presence_bound(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config = _seed_project(session)
        with pytest.raises(ValueError, match="current_window or presence_before"):
            covered_buckets_from_scan_jobs(
                session,
                scan_config_id=config.id,
                delta=_HOUR,
                history_from=_BASE,
            )


def test_coverage_history_start_reaches_the_deepest_pass(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The horizon is derived from the detector's own history depth, so a change
    to ``required_history_buckets`` or ``ANOMALY_TRAILING_REEVAL_BUCKETS`` moves
    it instead of silently outrunning it.
    """
    evaluation_end = _JOB_BASE + timedelta(days=40)
    evaluation_start = evaluation_end - _HOUR * 30
    with sync_session_factory() as session:
        config = _seed_project(session)
        settings = session.execute(
            select(ProjectAnomalySettings).where(
                ProjectAnomalySettings.project_id == config.project_id
            )
        ).scalar_one()
        detector_settings = metrics_detect._build_anomaly_settings(settings)

        horizon = metrics_detect.coverage_history_start(
            session,
            config,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
        )

        # Exactly the window the hourly passes load, plus the created_at slack.
        expected = (
            min(
                evaluation_start,
                evaluation_end - _HOUR * metrics_detect.ANOMALY_TRAILING_REEVAL_BUCKETS,
            )
            - _HOUR * required_history_buckets(_HOUR, detector_settings)
            - metrics_detect.COVERAGE_HORIZON_SLACK
        )
        assert horizon == expected
        # Never shallower than the history a config-scope pass actually loads.
        assert horizon <= evaluation_start - _HOUR * required_history_buckets(
            _HOUR, detector_settings
        )

        # A coarser scan grid needs a proportionally deeper horizon.
        daily = _add_sibling_config(session, config, interval="1d")
        daily_horizon = metrics_detect.coverage_history_start(
            session,
            daily,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
        )
        assert daily_horizon < horizon


def test_coverage_history_start_survives_a_project_without_settings(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A never-configured project detects nothing, so the horizon is unused —
    but transient column defaults read back as ``None`` and must not reach the
    arithmetic."""
    evaluation_end = _JOB_BASE + timedelta(days=40)
    evaluation_start = evaluation_end - _HOUR * 30
    with sync_session_factory() as session:
        config = _seed_project(session, with_settings=False)

        horizon = metrics_detect.coverage_history_start(
            session,
            config,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
        )

    assert horizon < evaluation_start


# --------------------------------------------------------------------------
# tripl-0zpq.6 follow-up — coverage describes the WHOLE population the series
# is summed from, and the foreign read gets the same created_at slack the
# running scan's horizon gets
# --------------------------------------------------------------------------


def test_multi_grid_metric_unions_every_source_configs_coverage(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """``_load_metric_value_points`` SUMS a metric's values across every source
    grid ON THE RESOLVED GRID'S INTERVAL, with no filter down to one config, so
    coverage has to describe the same population. Resolving it from one source
    alone excluded every bucket only the other source contributed —
    ``expand_series`` drops an uncovered bucket even when a real value is sitting
    in it. Both sibling configs here are the default 1h, so the interval
    predicate keeps every seeded row and the population is the full union.

    Two live scans collect the same event type. The YOUNGER one holds the newest
    stored bucket, so ``metric_grid_stmt``'s ``ORDER BY bucket DESC`` resolves
    the metric to it, and its coverage spans only its own short lifetime. The
    older source's seven buckets are the metric's entire baseline; without them
    the series is three buckets long and the spike can never clear
    ``min_history_buckets``.
    """
    with sync_session_factory() as session:
        running = _seed_project(session)
        older = _add_sibling_config(session, running)
        younger = _add_sibling_config(session, running)
        metric = _add_metric(
            session,
            running,
            kind=MetricKind.event_composition,
            composition=MetricComposition.single,
            name="checkouts",
            interval=None,
        )
        old_hours = list(range(7))
        young_hours = [7, 8, _SPIKE_HOUR]
        baseline = {hour: 10.0 for hour in old_hours}
        recent = {hour: 10.0 for hour in young_hours}
        recent[_SPIKE_HOUR] = 100.0
        _seed_values(session, metric, baseline, scan_config_id=older.id)
        _seed_values(session, metric, recent, scan_config_id=younger.id)
        # Each source vouches only for the buckets it actually collected.
        _seed_event_metrics(session, older.id, old_hours)
        _seed_event_metrics(session, younger.id, young_hours)

        metrics_detect._recalculate_metric_anomalies(
            session,
            running,
            evaluation_start=_EVAL_FROM,
            evaluation_end=_EVAL_TO,
            # The running scan is neither source, so this set must never reach
            # the metric — it is here to prove the union is not inheriting it.
            covered_buckets={_BASE + _HOUR * hour for hour in range(10)},
        )
        session.commit()

        anomalies = _metric_anomalies(session, metric.id)

    assert [(a.bucket, a.direction) for a in anomalies] == [(_BASE + _HOUR * _SPIKE_HOUR, "spike")]


def test_metric_coverage_keeps_a_job_that_sat_queued_across_the_horizon(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """``ScanJob.created_at`` is only an approximation of the window the job
    recorded, which is why the running scan's horizon carries
    ``COVERAGE_HORIZON_SLACK``. The per-metric read needs the same day: a job
    queued before the metric's ``history_from`` and executed after it records a
    window that straddles the horizon, and dropping it costs every
    genuinely-zero bucket of that window — those are EXCLUDED from the series
    rather than zero-filled.
    """
    history_from = _JOB_BASE
    window_from = _JOB_BASE - timedelta(hours=6)
    with sync_session_factory() as session:
        running = _seed_project(session)
        source = _add_sibling_config(session, running)
        metric = _add_metric(
            session,
            running,
            kind=MetricKind.event_composition,
            composition=MetricComposition.single,
            name="checkouts",
            interval=None,
        )
        for step in range(4):
            session.add(
                MetricValue(
                    id=uuid.uuid4(),
                    metric_definition_id=metric.id,
                    scan_config_id=source.id,
                    bucket=_JOB_BASE + _HOUR * step,
                    value=10.0,
                )
            )
        session.commit()
        # Created a quarter-day before the horizon, executed after it.
        _add_completed_job(
            session,
            source.id,
            created_at=window_from,
            window=(window_from, _JOB_BASE + _HOUR * 4),
        )
        grid = metrics_detect._resolve_metric_grid(session, metric)
        assert grid is not None

        covered = metrics_detect._metric_covered_buckets(
            session,
            running,
            metric=metric,
            grid=grid,
            delta=_HOUR,
            history_from=history_from,
            evaluation_end=_JOB_BASE + _HOUR * 10,
            scan_covered_buckets=None,
            memo={},
        )

    assert covered is not None
    # Nothing else vouches for these: no EventMetric row was stored, so the
    # presence half cannot rescue them.
    assert {_JOB_BASE + _HOUR * step for step in range(4)} <= covered
    # The slack widens the created_at floor, not the recorded window, so the
    # whole window is still honoured and nothing below it is invented.
    assert min(covered) == window_from


# --------------------------------------------------------------------------
# The bucket convention — every datetime this seam compares is tz-aware UTC,
# stamped at the boundary where a value ENTERS rather than at a comparison.
#
# Naive and aware datetimes are neither equal nor equal-hashing, and the
# consumer (``anomaly_detector.expand_series``) does a ``bucket not in covered``
# set-membership test, which cannot convert. A half that skips the boundary
# therefore contributes entries that can never match, and NOTHING RAISES: the
# only symptom is coverage under-reporting, and an uncovered bucket is excluded
# from the series rather than zero-filled. That silence is why the convention
# needs assertions rather than a runtime guard.
# --------------------------------------------------------------------------


def _assert_uniformly_aware_utc(buckets: set[datetime]) -> None:
    """Every member is tz-aware and at zero offset — no mixed set, no naive."""
    assert buckets, "an empty set would satisfy the convention vacuously"
    assert all(bucket.tzinfo is not None for bucket in buckets)
    assert all(bucket.utcoffset() == timedelta(0) for bucket in buckets)


def _seed_both_coverage_halves(session: Session, scan_config_id: uuid.UUID) -> None:
    """Populate BOTH halves of the union that ``covered_buckets_from_scan_jobs``
    builds, because the defect was that the two disagreed — a test that seeds
    one half alone passes with the stamping removed.

    * the job-window half: a COMPLETED job whose ``result_summary`` records
      ``[_JOB_BASE, +3h)``, parsed back through ``_parse_task_datetime``;
    * the presence half: stored ``EventMetric`` rows at +3h and +4h, seeded in
      the NAIVE spelling because that is what sqlite hands back regardless of
      what was written (and what PostgreSQL never does).
    """
    _add_completed_job(
        session,
        scan_config_id,
        created_at=_JOB_BASE,
        window=(_JOB_BASE, _JOB_BASE + _HOUR * 3),
    )
    _seed_event_metric_buckets(
        session,
        scan_config_id,
        [_JOB_BASE_NAIVE + _HOUR * 3, _JOB_BASE_NAIVE + _HOUR * 4],
    )


def test_covered_buckets_are_uniformly_aware_across_both_halves(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The returned set is homogeneous even though it is unioned from sources
    whose awareness is decided in three different places: the recorded job
    window (aware by parse), the caller's own window (aware by stamp), and the
    stored-bucket read (decided by the BACKEND — naive here, ``timestamptz`` on
    PostgreSQL).

    The equality below is the load-bearing half of the assertion: a set holding
    a naive +3h next to an aware +3h has six members too, but it is not this
    set, and downstream it is a set in which half the buckets can never match.
    """
    with sync_session_factory() as session:
        config = _seed_project(session)
        _seed_both_coverage_halves(session, config.id)

        covered = covered_buckets_from_scan_jobs(
            session,
            scan_config_id=config.id,
            delta=_HOUR,
            history_from=_JOB_BASE,
            current_window=(_JOB_BASE + _HOUR * 5, _JOB_BASE + _HOUR * 6),
        )

    # 0,1,2 from the recorded window; 3,4 from the stored buckets; 5 from the
    # window this run just wrote. One flat run only because all three key alike.
    assert covered == {_JOB_BASE + _HOUR * step for step in range(6)}
    _assert_uniformly_aware_utc(covered)


def test_covered_buckets_stamp_a_naive_current_window(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The boundary-stamping contract at the caller's entry point: hand in a
    naive ``history_from`` and a naive ``current_window`` — what a caller off a
    naive clock mints — and the output is still uniformly aware, identical to
    the aware-input call above.

    A caller is not required to know the convention; the module is required to
    impose it. Nothing downstream converts, because the membership test cannot.
    """
    with sync_session_factory() as session:
        config = _seed_project(session)
        _seed_both_coverage_halves(session, config.id)

        covered = covered_buckets_from_scan_jobs(
            session,
            scan_config_id=config.id,
            delta=_HOUR,
            history_from=_JOB_BASE_NAIVE,
            current_window=(_JOB_BASE_NAIVE + _HOUR * 5, _JOB_BASE_NAIVE + _HOUR * 6),
        )

    assert covered == {_JOB_BASE + _HOUR * step for step in range(6)}
    _assert_uniformly_aware_utc(covered)


def test_covered_buckets_stamp_a_naive_presence_bound(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The other entry point, taken when the caller is asking about a FOREIGN
    config it wrote nothing for: no ``current_window``, a naive
    ``presence_before`` supplying the upper bound instead.

    Both halves are still seeded — the foreign-config path is exactly where the
    two disagreed, since the presence half is the only one the caller can see.
    """
    with sync_session_factory() as session:
        config = _seed_project(session)
        _seed_both_coverage_halves(session, config.id)

        covered = covered_buckets_from_scan_jobs(
            session,
            scan_config_id=config.id,
            delta=_HOUR,
            history_from=_JOB_BASE_NAIVE,
            presence_before=_JOB_BASE_NAIVE + _HOUR * 6,
        )

    # No current window to contribute +5h this time.
    assert covered == {_JOB_BASE + _HOUR * step for step in range(5)}
    _assert_uniformly_aware_utc(covered)


def test_covered_buckets_bound_a_non_utc_presence_bound_at_the_same_instant(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The upper bound is stamped too, and the stamp is what makes it an INSTANT
    rather than a wall-clock reading.

    ``presence_before`` reaches the database as a bind parameter, and a backend
    without timezone support stores and compares a datetime by its printed
    fields — so an unstamped ``08:00+02:00`` bounds the stored-bucket read at
    08:00 instead of at the 06:00 instant the caller meant. The decoy bucket at
    +7h sits in exactly that two-hour gap: it is outside the caller's window and
    must not be vouched for.
    """
    with sync_session_factory() as session:
        config = _seed_project(session)
        _seed_both_coverage_halves(session, config.id)
        # Above the real bound, inside the drift an unstamped bound would open.
        _seed_event_metric_buckets(session, config.id, [_JOB_BASE_NAIVE + _HOUR * 7])

        covered = covered_buckets_from_scan_jobs(
            session,
            scan_config_id=config.id,
            delta=_HOUR,
            history_from=_JOB_BASE,
            presence_before=(_JOB_BASE + _HOUR * 6).astimezone(_PLUS_TWO),
        )

    assert covered == {_JOB_BASE + _HOUR * step for step in range(5)}
    _assert_uniformly_aware_utc(covered)


def test_covered_buckets_stamp_a_non_utc_current_window(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The same, through the other entry point — and one step further, because
    ``current_window`` is not only a bound but a source of buckets.

    Unstamped, its ``+02:00`` buckets still EQUAL their UTC twins and still hash
    alike, so the union stays correct by instant; what breaks is the stated
    convention that every member is UTC, which is what lets a reader trust a
    ``min()``/``max()`` or a printed bucket without re-deriving the offset.
    """
    with sync_session_factory() as session:
        config = _seed_project(session)
        _seed_both_coverage_halves(session, config.id)
        _seed_event_metric_buckets(session, config.id, [_JOB_BASE_NAIVE + _HOUR * 7])

        covered = covered_buckets_from_scan_jobs(
            session,
            scan_config_id=config.id,
            delta=_HOUR,
            history_from=_JOB_BASE,
            current_window=(
                (_JOB_BASE + _HOUR * 5).astimezone(_PLUS_TWO),
                (_JOB_BASE + _HOUR * 6).astimezone(_PLUS_TWO),
            ),
        )

    assert covered == {_JOB_BASE + _HOUR * step for step in range(6)}
    _assert_uniformly_aware_utc(covered)


def test_canonical_covered_preserves_none_and_stamps_a_naive_set() -> None:
    """``None`` means NO coverage gating and must survive as ``None``.

    It is not interchangeable with the empty set: ``expand_series`` reads an
    empty set as 'nothing is covered' and drops every bucket of every series,
    which is silent — no anomaly is scored and no error is raised. Collapsing
    one into the other in either direction blanks or un-gates every pass, so
    both directions are pinned here alongside the stamping itself.
    """
    assert metrics_detect._canonical_covered(None) is None

    empty = metrics_detect._canonical_covered(set())
    assert empty is not None
    assert empty == set()

    stamped = metrics_detect._canonical_covered({_JOB_BASE_NAIVE, _JOB_BASE_NAIVE + _HOUR})
    assert stamped is not None
    assert stamped == {_JOB_BASE, _JOB_BASE + _HOUR}
    _assert_uniformly_aware_utc(stamped)


def test_canonical_window_stamps_a_naive_pair() -> None:
    """Every detection entrypoint runs its evaluation window through this, so a
    naive pair from a caller is converted once, here, and never compared against
    an aware bucket downstream."""
    start, end = metrics_detect._canonical_window(_JOB_BASE_NAIVE, _JOB_BASE_NAIVE + _HOUR)

    assert (start, end) == (_JOB_BASE, _JOB_BASE + _HOUR)
    _assert_uniformly_aware_utc({start, end})
