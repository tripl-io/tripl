"""Pre-launch fixes to the demo runtime tick and the demo recipe.

* The conversion metric is composed from its two event series, by the recipe
  and afterwards by the real collector; the tick writes no value of its own.
* The tick re-scores the volume scopes through the scheduled collection's own
  pass, so the project's anomaly settings, scope toggles, per-scope overrides
  and the archived filter hold, and chart baselines are written.
* A tick with no new hour stamps the demo's collection and stops (no
  re-detection, prune or broadcast), and overlapping sweeps skip.
* The demo's scan runs report the metric points they stored, without posing as
  the dispatcher's own collections.
* The feature branch's audit rows, its base revision and its comment share one
  timeline.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.analyzers.anomaly_detector import (
    SCOPE_EVENT,
    SCOPE_EVENT_TYPE,
    SCOPE_PROJECT_TOTAL,
)
from tripl.core.bucketing import to_utc
from tripl.models import Base
from tripl.models.anomaly_scope_override import AnomalyScopeOverride
from tripl.models.audit_log import AuditLog
from tripl.models.event import Event, EventStatus
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_baseline import MetricBaseline
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.plan_branch_comment import PlanBranchComment
from tripl.models.plan_revision import PlanRevision
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.tests import test_demo_runtime as demo_runtime_tests
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import demo_runtime
from tripl.worker.tasks.alert_flush import _ALERT_FLUSH_ADVISORY_LOCK_KEY
from tripl.worker.tasks.metrics.freshness_sweep import _FRESHNESS_SWEEP_ADVISORY_LOCK_KEY
from tripl.worker.tasks.metrics.schedule import (
    _DISPATCH_ADVISORY_LOCK_KEY,
    _METRIC_DEFINITION_DISPATCH_ADVISORY_LOCK_KEY,
    _event_composition_due,
    _hours_since_last_scheduled_collection,
)
from tripl.worker.tasks.metrics.tasks import (
    _is_dispatcher_collection_job,
    _last_collected_window_to,
)

_HOUR = timedelta(hours=1)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'demo_tick.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _seed(factory: sessionmaker[Session]) -> tuple[demo_runtime_tests._Seeded, datetime]:
    seed_now = demo_runtime_tests._floor(datetime.now(UTC)) - timedelta(days=1)
    with factory() as session:
        seeded = demo_runtime_tests._seed_demo(session, seed_now=seed_now, history_hours=72)
    return seeded, seed_now


def _keep_active(factory: sessionmaker[Session], project_id: uuid.UUID, at: datetime) -> None:
    """A viewer is still on the demo, so the idle pause does not stop the tick."""
    with factory() as session:
        project = session.get(Project, project_id)
        assert project is not None
        project.demo_last_accessed_at = at
        session.commit()


# ── A tick with nothing new; overlapping sweeps ─────────────────────────────


def test_a_tick_with_no_new_hour_only_stamps_the_collection(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The beat fires every five minutes and a bucket is due once an hour. The
    ticks in between used to prune, commit and broadcast a refresh to every open
    tab of the demo for nothing; now they only record the collection."""
    seeded, seed_now = _seed(factory)
    first = seed_now + timedelta(hours=3)
    assert demo_runtime_tests._run_tick(factory, monkeypatch, first)["advanced"] == 1
    _keep_active(factory, seeded.project_id, first)

    calls: list[str] = []
    monkeypatch.setattr(demo_runtime, "_emit_status", lambda *_args: calls.append("emit"))
    monkeypatch.setattr(demo_runtime, "_prune_retention", lambda *_args: calls.append("prune"))
    monkeypatch.setattr(
        demo_runtime, "_recompute_anomalies", lambda *_args, **_kwargs: calls.append("detect")
    )

    second = first + timedelta(minutes=5)
    assert demo_runtime_tests._run_tick(factory, monkeypatch, second)["advanced"] == 1

    assert calls == []
    with factory() as session:
        config = session.get(ScanConfig, seeded.scan_config_id)
        project = session.get(Project, seeded.project_id)
        assert config is not None and project is not None
        assert to_utc(config.last_collection_at) == second
        assert project.demo_last_tick_at is not None
        assert to_utc(project.demo_last_tick_at) == second
        # No second run row for a tick that stored nothing.
        assert demo_runtime_tests._scan_job_count(session, seeded.scan_config_id) == 1


def test_a_tick_that_writes_an_hour_prunes_and_broadcasts_once(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seeded, seed_now = _seed(factory)
    calls: list[str] = []
    real_prune = demo_runtime._prune_retention

    def _prune(*args: object) -> None:
        calls.append("prune")
        real_prune(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(demo_runtime, "_emit_status", lambda *_args: calls.append("emit"))
    monkeypatch.setattr(demo_runtime, "_prune_retention", _prune)

    demo_runtime_tests._run_tick(factory, monkeypatch, seed_now + timedelta(hours=3))

    # Pruned inside the transaction, broadcast after its commit.
    assert calls == ["prune", "emit"]


def test_a_sweep_skips_while_another_holds_the_lock(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded, seed_now = _seed(factory)
    with factory() as session:
        before = demo_runtime_tests._bucket_set(session, seeded.scan_config_id)
    monkeypatch.setattr(
        demo_runtime, "try_acquire_advisory_lock", lambda _session, _key: (None, False)
    )

    result = demo_runtime_tests._run_tick(factory, monkeypatch, seed_now + timedelta(hours=3))

    assert result == {"enabled": True, "running": True, "advanced": 0, "skipped": 0}
    with factory() as session:
        assert demo_runtime_tests._bucket_set(session, seeded.scan_config_id) == before
        project = session.get(Project, seeded.project_id)
        assert project is not None and project.demo_last_tick_at is None


def test_a_sweep_releases_its_lock(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seeded, seed_now = _seed(factory)
    lock = object()
    released: list[tuple[object, int]] = []
    monkeypatch.setattr(
        demo_runtime, "try_acquire_advisory_lock", lambda _session, _key: (lock, True)
    )
    monkeypatch.setattr(
        demo_runtime,
        "release_advisory_lock",
        lambda conn, key, *, name: released.append((conn, key)),
    )

    demo_runtime_tests._run_tick(factory, monkeypatch, seed_now + timedelta(hours=3))

    assert released == [(lock, demo_runtime._ADVANCE_DEMOS_ADVISORY_LOCK_KEY)]


def test_the_sweep_lock_key_is_its_own() -> None:
    keys = {
        _DISPATCH_ADVISORY_LOCK_KEY,
        _METRIC_DEFINITION_DISPATCH_ADVISORY_LOCK_KEY,
        _ALERT_FLUSH_ADVISORY_LOCK_KEY,
        _FRESHNESS_SWEEP_ADVISORY_LOCK_KEY,
        demo_runtime._ADVANCE_DEMOS_ADVISORY_LOCK_KEY,
    }
    assert len(keys) == 5


# ── The tick's run row ───────────────────────────────────────────────────────


def test_a_tick_run_reports_the_points_it_stored_without_posing_as_a_collection(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan page counts a run with points as a metrics run; the dispatcher
    trusts only its own ``metrics_collection`` runs. So the tick reports its
    points and no ``mode``, and neither the collection watermark nor the demo's
    collection cooldown moves."""
    seeded, seed_now = _seed(factory)
    tick = seed_now + timedelta(hours=3)
    demo_runtime_tests._run_tick(factory, monkeypatch, tick)

    first_appended = demo_runtime_tests._floor(seed_now)
    with factory() as session:
        job = session.execute(
            select(ScanJob).where(ScanJob.scan_config_id == seeded.scan_config_id)
        ).scalar_one()
        summary = job.result_summary or {}
        event_rows, all_rows = session.execute(
            select(func.count(EventMetric.event_id), func.count()).where(
                EventMetric.scan_config_id == seeded.scan_config_id,
                EventMetric.bucket >= first_appended,
            )
        ).one()
        breakdown_event_rows, breakdown_rows = session.execute(
            select(func.count(EventMetricBreakdown.event_id), func.count()).where(
                EventMetricBreakdown.scan_config_id == seeded.scan_config_id,
                EventMetricBreakdown.bucket >= first_appended,
            )
        ).one()
        volume = session.execute(
            select(func.coalesce(func.sum(EventMetric.count), 0)).where(
                EventMetric.scan_config_id == seeded.scan_config_id,
                EventMetric.event_id.is_(None),
                EventMetric.bucket >= first_appended,
            )
        ).scalar_one()

        assert summary["buckets_appended"] == 3
        assert summary["event_metrics"] == event_rows > 0
        assert summary["type_metrics"] == all_rows - event_rows > 0
        assert summary["breakdown_event_metrics"] == breakdown_event_rows
        assert summary["breakdown_type_metrics"] == breakdown_rows - breakdown_event_rows
        assert summary["catalog_rows_scanned"] == volume
        assert "mode" not in summary
        assert not _is_dispatcher_collection_job(summary)
        assert _last_collected_window_to(session, seeded.scan_config_id) is None
        assert (
            _hours_since_last_scheduled_collection(session, seeded.scan_config_id, now=tick) is None
        )


# ── Re-detection through the collection's own pass ──────────────────────────


class _Scored(NamedTuple):
    # ``(scope_type, scope_ref)`` of every anomaly row the scan holds.
    anomalies: set[tuple[str, str]]
    baselines: int
    home: str
    buy: str


_Change = Callable[[Session, demo_runtime_tests._Seeded, dict[str, uuid.UUID]], None]


def _rescore(factory: sessionmaker[Session], change: _Change | None = None) -> _Scored:
    """Seed a demo whose newest six hours never landed, apply ``change``, re-score."""
    seed_now = demo_runtime_tests._floor(datetime.now(UTC))
    with factory() as session:
        seeded = demo_runtime_tests._seed_demo(session, seed_now=seed_now, history_hours=72)
        session.execute(
            update(EventMetric)
            .where(
                EventMetric.scan_config_id == seeded.scan_config_id,
                EventMetric.bucket >= seed_now - 6 * _HOUR,
            )
            .values(count=1)
        )
        events = {
            name: event_id
            for name, event_id in session.execute(
                select(Event.name, Event.id).where(Event.project_id == seeded.project_id)
            ).tuples()
        }
        if change is not None:
            change(session, seeded, events)
        session.commit()

        config = session.get(ScanConfig, seeded.scan_config_id)
        assert config is not None
        demo_runtime._recompute_anomalies(session, config, seed_now)
        session.flush()
        anomalies = {
            (str(scope_type), scope_ref)
            for scope_type, scope_ref in session.execute(
                select(MetricAnomaly.scope_type, MetricAnomaly.scope_ref).where(
                    MetricAnomaly.scan_config_id == seeded.scan_config_id
                )
            ).tuples()
        }
        baselines = session.execute(
            select(func.count())
            .select_from(MetricBaseline)
            .where(MetricBaseline.scan_config_id == seeded.scan_config_id)
        ).scalar_one()
    return _Scored(
        anomalies=anomalies,
        baselines=baselines,
        home=str(events["Home Screen View"]),
        buy=str(events["Buy Button Click"]),
    )


def _settings(session: Session, seeded: demo_runtime_tests._Seeded) -> ProjectAnomalySettings:
    return session.execute(
        select(ProjectAnomalySettings).where(ProjectAnomalySettings.project_id == seeded.project_id)
    ).scalar_one()


def test_the_tick_scores_every_volume_scope_and_writes_baselines(
    factory: sessionmaker[Session],
) -> None:
    scored = _rescore(factory)

    assert (SCOPE_EVENT, scored.home) in scored.anomalies
    assert (SCOPE_EVENT, scored.buy) in scored.anomalies
    scope_types = {scope_type for scope_type, _ref in scored.anomalies}
    assert {SCOPE_PROJECT_TOTAL, SCOPE_EVENT_TYPE, SCOPE_EVENT} <= scope_types
    # The chart band every scored bucket gets; the tick's own copy wrote none.
    assert scored.baselines > 0


def test_the_tick_honours_the_project_thresholds(factory: sessionmaker[Session]) -> None:
    def _insensitive(session: Session, seeded: demo_runtime_tests._Seeded, _events: object) -> None:
        _settings(session, seeded).min_expected_count = 10**9

    assert _rescore(factory, _insensitive).anomalies == set()


def test_the_tick_honours_a_switched_off_scope(factory: sessionmaker[Session]) -> None:
    def _no_events(session: Session, seeded: demo_runtime_tests._Seeded, _events: object) -> None:
        _settings(session, seeded).detect_events = False

    scored = _rescore(factory, _no_events)

    scope_types = {scope_type for scope_type, _ref in scored.anomalies}
    assert SCOPE_EVENT not in scope_types
    assert SCOPE_EVENT_TYPE in scope_types


def test_the_tick_honours_a_scope_override(factory: sessionmaker[Session]) -> None:
    """The false-positive ratchet tunes one scope through an override; the tick
    used to score every scope at the seeded settings and undo it within the hour."""

    def _override(
        session: Session, seeded: demo_runtime_tests._Seeded, events: dict[str, uuid.UUID]
    ) -> None:
        session.add(
            AnomalyScopeOverride(
                project_id=seeded.project_id,
                scan_config_id=seeded.scan_config_id,
                scope_type=SCOPE_EVENT,
                scope_ref=str(events["Buy Button Click"]),
                sigma_threshold=3.0,
                min_expected_count=10**9,
            )
        )

    scored = _rescore(factory, _override)

    assert (SCOPE_EVENT, scored.buy) not in scored.anomalies
    assert (SCOPE_EVENT, scored.home) in scored.anomalies


def test_the_tick_skips_an_archived_event(factory: sessionmaker[Session]) -> None:
    def _archive(
        session: Session, _seeded: demo_runtime_tests._Seeded, events: dict[str, uuid.UUID]
    ) -> None:
        event = session.get(Event, events["Buy Button Click"])
        assert event is not None
        event.status = EventStatus.archived

    scored = _rescore(factory, _archive)

    assert (SCOPE_EVENT, scored.buy) not in scored.anomalies
    assert (SCOPE_EVENT, scored.home) in scored.anomalies


def test_the_tick_clears_the_scan_when_detection_is_off(factory: sessionmaker[Session]) -> None:
    def _off(session: Session, seeded: demo_runtime_tests._Seeded, events: object) -> None:
        _settings(session, seeded).anomaly_detection_enabled = False
        session.add(
            MetricAnomaly(
                scan_config_id=seeded.scan_config_id,
                scope_type=SCOPE_PROJECT_TOTAL,
                scope_ref=str(seeded.scan_config_id),
                bucket=demo_runtime_tests._floor(datetime.now(UTC)) - 2 * _HOUR,
                actual_count=1,
                expected_count=2000,
                stddev=40,
                z_score=-50,
                direction="drop",
            )
        )

    scored = _rescore(factory, _off)

    assert scored.anomalies == set()
    assert scored.baselines == 0


# ── The demo recipe ──────────────────────────────────────────────────────────


async def _demo(client: AsyncClient, session: AsyncSession) -> uuid.UUID:
    slug = (await client.post("/api/v1/projects/demo")).json()["slug"]
    return (await session.execute(select(Project.id).where(Project.slug == slug))).scalar_one()


@pytest.mark.asyncio
async def test_the_seeded_conversion_is_the_ratio_of_its_two_event_series(
    client: AsyncClient,
) -> None:
    """Purchase conversion is a ratio of two series the demo charts beside it; a
    hand-drawn line divided to nothing a reader could reproduce. Composed over
    the whole window, it also leaves the collector nothing to backfill."""
    async with TestSessionLocal() as session:
        project_id = await _demo(client, session)
        metric = (
            await session.execute(
                select(MetricDefinition).where(
                    MetricDefinition.project_id == project_id,
                    MetricDefinition.name == "purchase_conversion",
                )
            )
        ).scalar_one()
        values = {
            to_utc(bucket): value
            for bucket, value in (
                await session.execute(
                    select(MetricValue.bucket, MetricValue.value).where(
                        MetricValue.metric_definition_id == metric.id
                    )
                )
            ).tuples()
        }
        series: dict[uuid.UUID | None, dict[datetime, int]] = {}
        for event_id in (metric.numerator_event_id, metric.denominator_event_id):
            series[event_id] = {
                to_utc(bucket): count
                for bucket, count in (
                    await session.execute(
                        select(EventMetric.bucket, EventMetric.count).where(
                            EventMetric.event_id == event_id
                        )
                    )
                ).tuples()
            }
        due = await session.run_sync(lambda sync: _event_composition_due(sync, metric))

    numerator = series[metric.numerator_event_id]
    denominator = series[metric.denominator_event_id]
    assert set(values) == set(numerator) | set(denominator)
    for bucket, value in values.items():
        assert value == pytest.approx(numerator.get(bucket, 0) / denominator[bucket])
    assert not due


@pytest.mark.asyncio
async def test_the_seeded_scan_runs_report_the_points_they_stored(client: AsyncClient) -> None:
    async with TestSessionLocal() as session:
        project_id = await _demo(client, session)
        scan_config_id = (
            await session.execute(select(ScanConfig.id).where(ScanConfig.project_id == project_id))
        ).scalar_one()
        jobs = (
            (
                await session.execute(
                    select(ScanJob).where(
                        ScanJob.scan_config_id == scan_config_id,
                        ScanJob.status == ScanJobStatus.completed.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert jobs
        for job in jobs:
            summary = job.result_summary or {}
            window_from = to_utc(datetime.fromisoformat(str(summary["scan_window_from"])))
            for model, event_key, type_key in (
                (EventMetric, "event_metrics", "type_metrics"),
                (EventMetricBreakdown, "breakdown_event_metrics", "breakdown_type_metrics"),
            ):
                event_rows, all_rows = (
                    await session.execute(
                        select(func.count(model.event_id), func.count()).where(
                            model.scan_config_id == scan_config_id,
                            model.bucket == window_from,
                        )
                    )
                ).one()
                assert summary[event_key] == event_rows > 0, (event_key, window_from)
                assert summary[type_key] == all_rows - event_rows > 0, (type_key, window_from)
            assert "mode" not in summary
            assert not _is_dispatcher_collection_job(summary)


@pytest.mark.asyncio
async def test_the_feature_branch_reads_one_timeline(client: AsyncClient) -> None:
    """The audit log dated the branch from the seed clock's spread while the
    branch, its base revision and its comment took the database's own clock, so
    the log, Plan history and the branch list named different times."""
    async with TestSessionLocal() as session:
        project_id = await _demo(client, session)
        branch = (
            await session.execute(
                select(PlanBranch).where(
                    PlanBranch.project_id == project_id,
                    PlanBranch.kind == BranchKind.working.value,
                )
            )
        ).scalar_one()
        assert branch.base_revision_id is not None
        revision = await session.get(PlanRevision, branch.base_revision_id)
        assert revision is not None
        comment = (
            await session.execute(
                select(PlanBranchComment).where(PlanBranchComment.branch_id == branch.id)
            )
        ).scalar_one()
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.project_id == project_id)))
            .scalars()
            .all()
        )

    created = next(row for row in rows if row.action == "plan_branch.create")
    edit = next(row for row in rows if row.action == "event.update" and row.branch_id == branch.id)
    opened = to_utc(branch.created_at)
    assert to_utc(created.created_at) == opened == to_utc(revision.created_at)
    assert opened < to_utc(edit.created_at) < to_utc(comment.created_at)
    assert to_utc(comment.created_at) <= datetime.now(UTC)
    # Connecting the warehouse and setting up the catalog and alerting came first.
    set_up = [
        row
        for row in rows
        if row.action
        in {
            "scan_config.create",
            "fact_table.create",
            "metric_definition.create",
            "alert_destination.create",
            "alert_rule.create",
        }
    ]
    assert set_up
    assert all(to_utc(row.created_at) < opened for row in set_up)
