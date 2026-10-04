"""Batch 3 lane A2: the metric-scope purge, signed volume floors, event_type filters.

Three defects that share nothing but the files they live in:

* ``tripl-0zpq.8`` — unticking the project's **Metrics** detection scope deleted
  the project's WHOLE metric-scope anomaly history instead of the window an
  enabled pass would have rewritten, so a temporary toggle-off destroyed months
  of history nothing else ever ages out;
* ``tripl-0zpq.102`` — every volume gate compared a SIGNED expectation against a
  non-negative floor, so a catalog metric whose level legitimately sits below
  zero could never be scored and, if it had been, could never have matched a
  rule;
* ``tripl-0zpq.7`` — event-anchored signals carry a NULL ``event_type_id``, and
  the matcher read a missing type as "this signal has no such field", so an
  alert rule's ``event_type`` filter was silently inert for all of them.

Sync sqlite fixtures mirror ``test_metric_anomaly_scope.py`` /
``test_alert_digest_delivery.py``; the async simulator test uses the shared
in-memory fixtures from ``conftest``.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.alert_templates import (
    NO_BASELINE_LABEL,
    format_percent_delta,
    has_baseline,
    percent_delta_of,
    percent_delta_or_none,
)
from tripl.alerting_matching import (
    SCOPE_METRIC,
    SCOPE_RELEASE_REGRESSION,
    SCOPE_VARIABLE_VALUE_DRIFT,
    rule_matches_anomaly,
)
from tripl.core.analyzers.anomaly_detector import (
    SCOPE_EVENT,
    SCOPE_PROJECT_TOTAL,
    AnomalyDetectionSettings,
    DetectedAnomaly,
    SeriesPoint,
    _detect_trend_shift,
    detect_anomalies,
)
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_filter import AlertRuleFilter
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import (
    MetricAggregation,
    MetricComposition,
    MetricKind,
    MetricStatus,
)
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.services._alerting_deliveries import _build_inbox_group_response
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks.alerts_messages import _digest_groups, _digest_headline
from tripl.worker.tasks.metrics import detect as metrics_detect
from tripl.worker.tasks.metrics import dispatch as metrics_dispatch

# Recent, hour-aligned UTC buckets matching persisted values.
_BASE = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=12)
_EVAL_FROM = _BASE + timedelta(hours=8)
_EVAL_TO = _BASE + timedelta(hours=10)
_SPIKE_HOUR = 9
_DAY = timedelta(days=1)
_DAY_END = _BASE.replace(hour=0, minute=0, second=0, microsecond=0)
# The scan-grid window ``collect_metrics`` computes for a 1h config: 30 buckets
# of the SCAN's grid, which is 30 hours however coarse the metric is.
_SCAN_WINDOW = timedelta(hours=30)


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch3_a2.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _seed_project(
    session: Session,
    *,
    detect_metrics: bool = True,
    anomaly_detection_enabled: bool = True,
) -> ScanConfig:
    project = Project(
        id=uuid.uuid4(),
        name="Batch3 A2",
        slug=f"batch3-a2-{uuid.uuid4().hex[:8]}",
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
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Scan",
        base_query="SELECT time, event_name FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval="1h",
    )
    settings = ProjectAnomalySettings(
        project_id=project.id,
        anomaly_detection_enabled=anomaly_detection_enabled,
        detect_metrics=detect_metrics,
        sigma_threshold=3.0,
        min_expected_count=10,
    )
    session.add_all([project, data_source, config, settings])
    session.commit()
    return config


def _add_metric(
    session: Session,
    config: ScanConfig,
    *,
    kind: MetricKind,
    name: str,
    aggregation: MetricAggregation | None = None,
    composition: MetricComposition | None = None,
    interval: str | None = "1h",
    status: MetricStatus = MetricStatus.active,
    anomaly_detection_enabled: bool = True,
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
        status=status.value,
        anomaly_detection_enabled=anomaly_detection_enabled,
    )
    session.add(metric)
    session.commit()
    return metric


def _add_metric_anomaly(session: Session, metric: MetricDefinition, bucket: datetime) -> None:
    """One stored metric-scope marker, shaped exactly as detection writes it."""
    session.add(
        MetricAnomaly(
            id=uuid.uuid4(),
            scan_config_id=None,
            scope_type=SCOPE_METRIC,
            scope_ref=str(metric.id),
            event_id=None,
            event_type_id=None,
            bucket=bucket,
            direction="drop",
            actual_count=1.0,
            expected_count=10.0,
            stddev=1.0,
            z_score=-9.0,
        )
    )
    session.commit()


def _metric_anomaly_buckets(session: Session, metric_id: uuid.UUID) -> set[datetime]:
    return {
        anomaly.bucket
        for anomaly in session.execute(
            select(MetricAnomaly).where(
                MetricAnomaly.scope_type == SCOPE_METRIC,
                MetricAnomaly.scope_ref == str(metric_id),
            )
        ).scalars()
    }


def _seed_values_at(
    session: Session,
    metric: MetricDefinition,
    values: dict[datetime, float],
) -> None:
    for bucket, value in values.items():
        session.add(
            MetricValue(
                id=uuid.uuid4(),
                metric_definition_id=metric.id,
                scan_config_id=None,
                bucket=bucket,
                value=value,
            )
        )
    session.commit()


# ---------------------------------------------------------------------------
# the detect_metrics purge
# ---------------------------------------------------------------------------


def test_detect_metrics_disabled_keeps_history_older_than_the_reeval_window(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Unticking the Metrics scope box clears its window, not the project's past.

    The three event scopes have always deleted exactly
    ``[evaluation_start, evaluation_end)`` when switched off. The metric scope
    deleted every row below ``evaluation_end`` with no start bound at all, so one
    toggle-off erased history no later run can re-derive: re-enabling only
    re-scores the trailing 30 buckets.
    """
    with sync_session_factory() as session:
        config = _seed_project(session, detect_metrics=False)
        metric = _add_metric(
            session,
            config,
            kind=MetricKind.fact,
            aggregation=MetricAggregation.count,
            composition=MetricComposition.single,
            name="signups",
        )
        # The metric's own re-evaluation window is 30 hourly buckets back from
        # the end, so `recent` is inside it and `old` is 170 hours outside.
        old = _EVAL_TO - timedelta(hours=200)
        recent = _EVAL_TO - timedelta(hours=2)
        _add_metric_anomaly(session, metric, old)
        _add_metric_anomaly(session, metric, recent)

        metrics_detect._recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=_EVAL_FROM,
            evaluation_end=_EVAL_TO,
        )
        session.commit()

        assert _metric_anomaly_buckets(session, metric.id) == {old}


def test_detect_metrics_disabled_clears_a_daily_metric_on_its_own_grid(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The purge window is the METRIC's grid, not the scan config's.

    Guards the naive fix as well as the defect. A 1d metric under a 1h scan is
    scored over 30 DAYS, so bounding the disabled-scope delete by the scan's
    30-hour window would strand its in-window rows as markers no later run
    re-evaluates — the failure the old whole-history wipe was reaching for.
    """
    with sync_session_factory() as session:
        config = _seed_project(session, detect_metrics=False)
        metric = _add_metric(
            session,
            config,
            kind=MetricKind.fact,
            aggregation=MetricAggregation.count,
            composition=MetricComposition.single,
            name="daily_signups",
            interval="1d",
        )
        inside = _DAY_END - _DAY * 20
        ancient = _DAY_END - _DAY * 200
        _add_metric_anomaly(session, metric, inside)
        _add_metric_anomaly(session, metric, ancient)

        metrics_detect._recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=_DAY_END - _SCAN_WINDOW,
            evaluation_end=_DAY_END,
        )
        session.commit()

        assert _metric_anomaly_buckets(session, metric.id) == {ancient}


def test_master_switch_off_still_wipes_metric_history(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The branch that must NOT change.

    ``anomaly_detection_enabled = False`` is the documented project master
    switch and already drops every config-scoped row this scan owns, so it keeps
    taking the metric-scope history with it. Narrowing the old purge's signature
    must leave that behaviour alone.
    """
    with sync_session_factory() as session:
        config = _seed_project(session, anomaly_detection_enabled=False)
        metric = _add_metric(
            session,
            config,
            kind=MetricKind.fact,
            aggregation=MetricAggregation.count,
            composition=MetricComposition.single,
            name="signups",
        )
        _add_metric_anomaly(session, metric, _EVAL_TO - timedelta(hours=200))
        _add_metric_anomaly(session, metric, _EVAL_TO - timedelta(hours=2))

        metrics_detect._recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=_EVAL_FROM,
            evaluation_end=_EVAL_TO,
        )
        session.commit()

        assert _metric_anomaly_buckets(session, metric.id) == set()


def test_detect_metrics_disabled_leaves_another_projects_history_alone(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Metric-scope rows share one global ``scan_config_id IS NULL`` space.

    The old purge was scoped by the project's metric ids for exactly this
    reason; the per-grid replacement has to keep that scoping.
    """
    with sync_session_factory() as session:
        config = _seed_project(session, detect_metrics=False)
        mine = _add_metric(
            session,
            config,
            kind=MetricKind.fact,
            aggregation=MetricAggregation.count,
            composition=MetricComposition.single,
            name="signups",
        )
        other_config = _seed_project(session)
        theirs = _add_metric(
            session,
            other_config,
            kind=MetricKind.fact,
            aggregation=MetricAggregation.count,
            composition=MetricComposition.single,
            name="their_signups",
        )
        in_window = _EVAL_TO - timedelta(hours=2)
        _add_metric_anomaly(session, mine, in_window)
        _add_metric_anomaly(session, theirs, in_window)

        metrics_detect._recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=_EVAL_FROM,
            evaluation_end=_EVAL_TO,
        )
        session.commit()

        assert _metric_anomaly_buckets(session, mine.id) == set()
        assert _metric_anomaly_buckets(session, theirs.id) == {in_window}


def test_detect_metrics_disabled_spares_metrics_the_enabled_pass_never_scores(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The purge population is the ENABLED pass's population, nothing wider.

    ``_recalculate_project_metric_anomalies`` selects on
    ``monitored_metric_criteria()`` — ``active`` AND ``anomaly_detection_enabled``
    — so a metric that has left monitoring is never scored and never rewritten
    with the box ticked. Sweeping it when the box is UNticked would erase rows
    no pass ever re-derives (re-ticking does not bring them back: the metric is
    still unmonitored), which is the promise ``tripl.metric_monitoring`` makes
    for an archived metric's recorded history.
    """
    with sync_session_factory() as session:
        config = _seed_project(session, detect_metrics=False)
        monitored = _add_metric(
            session,
            config,
            kind=MetricKind.fact,
            aggregation=MetricAggregation.count,
            composition=MetricComposition.single,
            name="signups",
        )
        archived = _add_metric(
            session,
            config,
            kind=MetricKind.fact,
            aggregation=MetricAggregation.count,
            composition=MetricComposition.single,
            name="archived_signups",
            status=MetricStatus.archived,
        )
        detection_off = _add_metric(
            session,
            config,
            kind=MetricKind.fact,
            aggregation=MetricAggregation.count,
            composition=MetricComposition.single,
            name="unwatched_signups",
            anomaly_detection_enabled=False,
        )
        in_window = _EVAL_TO - timedelta(hours=2)
        for metric in (monitored, archived, detection_off):
            _add_metric_anomaly(session, metric, in_window)

        metrics_detect._recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=_EVAL_FROM,
            evaluation_end=_EVAL_TO,
        )
        session.commit()

        # The monitored metric still loses its window — the purge must keep
        # doing its job, or this test would pass on a purge that does nothing.
        assert _metric_anomaly_buckets(session, monitored.id) == set()
        assert _metric_anomaly_buckets(session, archived.id) == {in_window}
        assert _metric_anomaly_buckets(session, detection_off.id) == {in_window}


# ---------------------------------------------------------------------------
# the signed volume floor
# ---------------------------------------------------------------------------

_FRACTIONAL_SETTINGS = AnomalyDetectionSettings(
    baseline_window_buckets=14,
    min_history_buckets=7,
    sigma_threshold=3.0,
    # What ``detect._recalculate_project_metric_anomalies`` pins every fractional
    # catalog metric to.
    min_expected_count=1e-6,
)
_SERIES_START = datetime(2026, 7, 1, tzinfo=UTC)
_SERIES_BUCKETS = 40
_HOUR = timedelta(hours=1)


def _bucket(hour: int) -> datetime:
    return _SERIES_START + _HOUR * hour


def _level_series(sign: float) -> list[SeriesPoint]:
    """A flat level of ``sign * ~100`` for 39 buckets, then ``sign * 300``.

    The jitter is deterministic and never strictly monotonic, so the fractional
    path's sustained-ramp exemption does not defer the last bucket.
    """
    counts = [sign * (100.0 - (hour % 3) * 0.5) for hour in range(_SERIES_BUCKETS)]
    counts[-1] = sign * 300.0
    return [SeriesPoint(bucket=_bucket(hour), count=count) for hour, count in enumerate(counts)]


def _score_last_bucket(points: list[SeriesPoint]) -> list[DetectedAnomaly]:
    return detect_anomalies(
        points,
        interval=_HOUR,
        evaluation_start=_bucket(_SERIES_BUCKETS - 1),
        evaluation_end=_bucket(_SERIES_BUCKETS),
        settings=_FRACTIONAL_SETTINGS,
        fill_gaps=False,
    ).anomalies


def test_negative_level_fractional_series_is_scored() -> None:
    """A fractional series living below zero must be scored, not silently dropped.

    A ``fact`` sum/avg/min/max over a signed column, or any ``sql`` metric, can
    sit at -100 as naturally as at +100. Every volume gate compared that signed
    expectation against a non-negative floor, so the whole class was rejected on
    all three scoring paths.
    """
    anomalies = _score_last_bucket(_level_series(-1.0))

    assert [(a.direction, a.actual_count) for a in anomalies] == [("drop", -300.0)]
    assert anomalies[0].expected_count < 0
    assert anomalies[0].expected_count == pytest.approx(-99.46, abs=0.1)


def test_negative_and_positive_fractional_levels_score_alike() -> None:
    """The symmetry the defect broke: -100 is as substantial a level as +100.

    Same series, mirrored through zero. The floors are magnitude-derived, so the
    two must produce the same |z| and mirrored expectations — and the positive
    half doubles as the no-regression anchor for an over-broad fix.
    """
    negative = _score_last_bucket(_level_series(-1.0))
    positive = _score_last_bucket(_level_series(1.0))

    assert len(negative) == 1
    assert len(positive) == 1
    assert negative[0].direction == "drop"
    assert positive[0].direction == "spike"
    assert negative[0].expected_count == pytest.approx(-positive[0].expected_count)
    assert abs(negative[0].z_score) == pytest.approx(abs(positive[0].z_score))


def test_fractional_series_flatlined_at_zero_is_still_gated() -> None:
    """The magnitude gate must not undo the deliberate zero-flatline guard.

    ``_FRACTIONAL_MIN_EXPECTED_COUNT`` is a tiny POSITIVE floor precisely so an
    empty/flatlined-at-zero fractional series cannot manufacture multi-sigma
    anomalies out of noise. A single negative wobble puts the
    series on the signed lane; the expectation is still 0, so nothing emits.
    """
    counts = [0.0] * _SERIES_BUCKETS
    counts[-1] = -0.5
    points = [SeriesPoint(bucket=_bucket(hour), count=count) for hour, count in enumerate(counts)]

    assert _score_last_bucket(points) == []


def test_trend_shift_reports_a_signed_expectation() -> None:
    """The trend path must surface the real negative level, not a clamped 0.0.

    Hand-built components so the arithmetic is exact and no MSTL fit is needed,
    mirroring the harness in ``test_anomaly_detector``. With ``signed=False`` —
    the default every existing direct-call test gets — the deseasonalized trend
    of -160 fails the volume gate and nothing is emitted at all, which is the
    defect; with ``signed=True`` the reconstruction is reported as it stands.
    """
    hours = 24 * 22  # three full hour-of-week cycles, so period 168 is selectable
    anchor = hours - 1
    points = [SeriesPoint(bucket=_bucket(hour), count=-100.0) for hour in range(hours)]
    points[anchor] = SeriesPoint(bucket=_bucket(anchor), count=-400.0)
    trend = [-100.0] * hours
    trend[anchor] = -160.0
    components = (tuple(trend), tuple([0.0] * hours), tuple([0.0] * hours))

    def trend_rows(*, signed: bool) -> list[tuple[str, float]]:
        result = _detect_trend_shift(
            points,
            components,
            evaluation_start=_bucket(anchor - 1),
            settings=_FRACTIONAL_SETTINGS,
            interval=_HOUR,
            signed=signed,
        )
        return [(row.direction, row.expected_count) for row in result.anomalies]

    assert trend_rows(signed=True) == [("drop", -100.0)]
    # The count lane is untouched: a negative deseasonalized trend is still
    # rejected outright there, which is what keeps the tripl-wkwv.8 rows gone.
    assert trend_rows(signed=False) == []


def test_trend_shift_emits_an_empty_bucket_against_a_negative_expectation() -> None:
    """The degenerate-pair guard is ``expected_count == 0.0``, not ``<= 0.0``.

    The guard exists to stop "spike, 0 actual vs 0 expected" rows.
    Once the trend reconstruction stopped being clamped to 0.0 for a signed
    series, ``<=`` also swallowed a REAL move: an empty bucket
    against an expectation of -100 is a drop to nothing, not an absence of
    movement. Nothing else in the suite reaches that combination — every other
    signed fixture has a non-zero anchor — so reverting the spelling would leave
    the suite green.
    """
    hours = 24 * 22  # three full hour-of-week cycles, so period 168 is selectable
    anchor = hours - 1
    points = [SeriesPoint(bucket=_bucket(hour), count=-100.0) for hour in range(hours)]
    points[anchor] = SeriesPoint(bucket=_bucket(anchor), count=0.0)
    trend = [-100.0] * hours
    trend[anchor] = -160.0
    components = (tuple(trend), tuple([0.0] * hours), tuple([0.0] * hours))

    def trend_rows(*, signed: bool) -> list[tuple[str, float, float]]:
        result = _detect_trend_shift(
            points,
            components,
            evaluation_start=_bucket(anchor - 1),
            settings=_FRACTIONAL_SETTINGS,
            interval=_HOUR,
            signed=signed,
        )
        return [(row.direction, row.actual_count, row.expected_count) for row in result.anomalies]

    assert trend_rows(signed=True) == [("spike", 0.0, -100.0)]
    # The count lane never reaches the guard at all — the volume gate rejects a
    # negative deseasonalized trend first — so the two spellings still agree
    # everywhere a non-negative series can go.
    assert trend_rows(signed=False) == []


def test_signed_phase_baseline_never_normalizes_by_a_near_zero_level() -> None:
    """A signed series takes the raw same-phase median, not a level-normalized one.

    ``_seasonal_factors`` divides each same-phase count by its own trailing mean.
    On a series that STRADDLES zero that divisor is positive-but-tiny — it passes
    the per-cycle ``level > 0`` test while sitting near zero — so the factors
    explode and the expectation lands far outside anything the series has ever
    reached. The magnitude gate added for signed series then
    admits the product instead of rejecting it for its sign.

    A quiet level of +10/+11 with a deep settlement dip at 03:00 emitted 21 rows,
    the loudest reading "spike, actual -220 vs expected -286" on a bucket
    identical to every prior 03:00. The divisor has to be far from zero, not
    merely above it, so the signed lane keeps the degenerate fallback.
    """
    hours = 24 * 6
    dip_hour = 24 * 5 + 3

    def rows(dip: float) -> list[tuple[int, str, float, float]]:
        counts = [
            -220.0 if hour % 24 == 3 else (11.0 if hour >= 24 * 5 else 10.0)
            for hour in range(hours)
        ]
        counts[dip_hour] = dip
        points = [
            SeriesPoint(bucket=_bucket(hour), count=count) for hour, count in enumerate(counts)
        ]
        result = detect_anomalies(
            points,
            interval=_HOUR,
            evaluation_start=_bucket(hours - 24),
            evaluation_end=_bucket(hours),
            settings=_FRACTIONAL_SETTINGS,
            fill_gaps=False,
        )
        return [
            (row.bucket.hour, row.direction, row.actual_count, row.expected_count)
            for row in result.anomalies
        ]

    # The settlement hour repeats exactly as it always has: nothing moved.
    assert rows(-220.0) == []
    # ...and the fallback is a real baseline, not silence: a settlement hour that
    # comes back at a tenth of its usual depth is still caught, against an
    # expectation inside the series' own observed range.
    assert rows(-20.0) == [(3, "spike", -20.0, -220.0)]


def test_negative_level_fractional_metric_is_scored_end_to_end(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A monitored catalog metric sitting below zero can finally signal.

    End-to-end through ``_recalculate_metric_anomalies``: before the fix the
    metric stayed listed as monitored and produced nothing, forever.
    """
    with sync_session_factory() as session:
        config = _seed_project(session)
        metric = _add_metric(session, config, kind=MetricKind.sql, name="net_margin")
        values = {
            _BASE + timedelta(hours=hour): (-9.0 if hour == _SPIKE_HOUR else -3.0)
            for hour in range(_SPIKE_HOUR + 1)
        }
        _seed_values_at(session, metric, values)

        metrics_detect._recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=_EVAL_FROM,
            evaluation_end=_EVAL_TO,
        )
        session.commit()

        anomalies = list(
            session.execute(
                select(MetricAnomaly).where(
                    MetricAnomaly.scope_type == SCOPE_METRIC,
                    MetricAnomaly.scope_ref == str(metric.id),
                )
            ).scalars()
        )

    assert len(anomalies) == 1
    assert anomalies[0].bucket == _BASE + timedelta(hours=_SPIKE_HOUR)
    assert anomalies[0].direction == "drop"
    assert anomalies[0].actual_count == -9.0
    assert anomalies[0].expected_count < 0


def test_delivery_records_a_measured_percent_delta_for_a_negative_baseline(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """``percent_delta`` is a placeholder only when there is NO baseline.

    The matcher reads a signed expectation as a magnitude (``abs(expected)``
    against ``min_expected_count``, ``absolute_delta / abs(expected)`` against
    ``min_percent_delta``), so a rule fires on -3 -> -9 precisely BECAUSE the
    move is 200%. The payload builder still asked ``expected_count > 0`` and
    stored the 0.0 placeholder for it — the tripl-l429.24 misreport, reproduced
    against a REAL baseline — and that column is frozen history: the renderers
    read it back, so nothing later can recover the number.
    """
    with sync_session_factory() as session:
        config = _seed_project(session)
        metric = _add_metric(session, config, kind=MetricKind.sql, name="net_margin")
        _seed_values_at(
            session,
            metric,
            {
                _BASE + timedelta(hours=hour): (-9.0 if hour == _SPIKE_HOUR else -3.0)
                for hour in range(_SPIKE_HOUR + 1)
            },
        )
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=config.project_id,
            type="slack",
            name="Main Slack",
            enabled=True,
            webhook_url_encrypted="secret",
            delivery_schedule_cron=None,
        )
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Metrics only",
            enabled=True,
            include_project_total=False,
            include_event_types=False,
            include_events=False,
            include_metrics=True,
            notify_on_spike=True,
            notify_on_drop=True,
            min_percent_delta=100.0,
            min_absolute_delta=0,
            min_expected_count=1.0,
            cooldown_minutes=1440,
        )
        destination.rules = [rule]
        session.add_all([destination, rule])
        session.commit()

        metrics_detect._recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=_EVAL_FROM,
            evaluation_end=_EVAL_TO,
        )
        session.commit()
        metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

        items = list(session.execute(select(AlertDeliveryItem)).scalars())
        deliveries = list(session.execute(select(AlertDelivery)).scalars())

    assert len(items) == 1
    assert items[0].scope_type == SCOPE_METRIC
    assert items[0].expected_count == pytest.approx(-3.0)
    assert items[0].actual_count == pytest.approx(-9.0)
    assert items[0].absolute_delta == pytest.approx(6.0)
    # The size of the move, not its sign: 6 against a baseline of magnitude 3.
    assert items[0].percent_delta == pytest.approx(200.0)

    # ...and the FROZEN SNAPSHOT of the same delivery says the same number.
    # ``payload_snapshot`` is built by ``alert_payload`` from its own read of the
    # anomaly, so it is a second answer to one question: the audit API and the
    # Inbox read this blob while the typed ``items[]`` beside it reads the rows
    # above. Computed with an ``expected > 0`` divisor it collapsed to 0.0 here
    # while the row kept 200.0 — one delivery contradicting itself, which is
    # strictly worse than both halves being wrong the same way.
    assert len(deliveries) == 1
    snapshot_items = deliveries[0].payload_snapshot["items"]
    assert len(snapshot_items) == 1
    assert snapshot_items[0]["expected_count"] == pytest.approx(-3.0)
    assert snapshot_items[0]["percent_delta"] == pytest.approx(items[0].percent_delta)
    assert snapshot_items[0]["percent_delta"] == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# Shared matcher fixtures for the two alerting defects
# ---------------------------------------------------------------------------

_CANDIDATE_BUCKET = datetime(2026, 7, 10, 12, tzinfo=UTC)


@dataclass
class _Candidate:
    """Minimal ``AlertMatchCandidate``: only the fields the matcher reads."""

    scope_type: str
    scope_ref: str = "scope"
    event_id: uuid.UUID | None = None
    event_type_id: uuid.UUID | None = None
    direction: str = "drop"
    actual_count: float = 0.0
    expected_count: float = 0.0
    scan_config_id: uuid.UUID | None = None
    bucket: datetime = _CANDIDATE_BUCKET
    id: uuid.UUID = field(default_factory=uuid.uuid4)


def _build_rule(*, filters: list[AlertRuleFilter] | None = None, **overrides: object) -> AlertRule:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "destination_id": uuid.uuid4(),
        "name": "Rule",
        "enabled": True,
        "scan_config_id": None,
        "include_project_total": True,
        "include_event_types": True,
        "include_events": True,
        "include_schema_drifts": False,
        "include_distribution_drifts": False,
        "include_variable_value_drifts": False,
        "include_release_regressions": False,
        "include_metrics": False,
        "notify_on_spike": True,
        "notify_on_drop": True,
        "min_percent_delta": 0.0,
        "min_absolute_delta": 0.0,
        "min_expected_count": 0.0,
        "cooldown_minutes": 60,
        "message_template": None,
        "items_template": None,
        "message_format": "plain",
    }
    defaults.update(overrides)
    rule = AlertRule(**defaults)
    rule.filters = filters or []
    return rule


def _event_type_filter(operator: str, *values: uuid.UUID) -> AlertRuleFilter:
    return AlertRuleFilter(
        id=uuid.uuid4(),
        rule_id=uuid.uuid4(),
        field="event_type",
        operator=operator,
        values=[str(value) for value in values],
        position=0,
    )


# ---------------------------------------------------------------------------
# the alert layer's half of the signed gate
# ---------------------------------------------------------------------------


def _metric_candidate(*, expected: float, actual: float) -> _Candidate:
    return _Candidate(
        scope_type=SCOPE_METRIC,
        scope_ref=str(uuid.uuid4()),
        direction="drop",
        actual_count=actual,
        expected_count=expected,
    )


def test_metric_rule_matches_a_negative_expectation() -> None:
    """``min_expected_count`` is a floor on volume, and volume has no sign.

    ``AlertRule.min_expected_count`` is ``ge=0`` by schema, so comparing a
    signed expectation against it dropped every negative-level metric anomaly
    even after the detector had emitted one.
    """
    rule = _build_rule(include_metrics=True, min_expected_count=50.0, min_percent_delta=100.0)

    assert rule_matches_anomaly(rule, _metric_candidate(expected=-100.0, actual=-300.0)) is True


def test_metric_rule_still_applies_the_percent_gate_to_a_negative_expectation() -> None:
    """Closing the floor must not open the percent gate.

    A 1% move against a -100 baseline has to fail a 100% threshold. Left in the
    ``expected_count > 0`` branch it would have fallen through to the
    "no baseline" case and matched regardless.
    """
    rule = _build_rule(include_metrics=True, min_expected_count=50.0, min_percent_delta=100.0)

    assert rule_matches_anomaly(rule, _metric_candidate(expected=-100.0, actual=-101.0)) is False


def test_metric_rule_keeps_matching_a_positive_expectation() -> None:
    """No-regression anchor: the ordinary positive case is untouched."""
    rule = _build_rule(include_metrics=True, min_expected_count=50.0, min_percent_delta=100.0)

    assert rule_matches_anomaly(rule, _metric_candidate(expected=100.0, actual=300.0)) is True
    assert rule_matches_anomaly(rule, _metric_candidate(expected=100.0, actual=101.0)) is False
    assert rule_matches_anomaly(rule, _metric_candidate(expected=10.0, actual=300.0)) is False


# ---------------------------------------------------------------------------
# every reader of a baseline has to answer the same way
#
# The gate moved to MAGNITUDE in the detector, the matcher and the payload
# builder. The readers did not: they kept asking ``expected_count > 0`` and so
# reported "no baseline" over the very number that made the rule fire. These pin
# the single definition every backend surface now routes through
# (``alert_templates.has_baseline`` / ``percent_delta_of``), so a fifth copy of
# the expression cannot drift back in unnoticed.
# ---------------------------------------------------------------------------

_BASELINE_GRID = (-1000.0, -100.0, -3.0, -0.5, 0.0, 0.5, 3.0, 100.0, 1000.0)


def _matcher_reads_a_baseline(expected: float) -> bool:
    """Whether the MATCHER divided by ``expected``, observed through its effect.

    Not asserted about directly, because the matcher exposes no predicate: a
    move of 1% of the magnitude sits far below the rule's 100% floor, so the
    percent gate rejects the candidate exactly when the matcher treats the
    expectation as a baseline. When it does not, the candidate falls into the
    no-baseline branch — which only rejects a candidate that did not move at all
    — and matches. A zero expectation moved by 1.0 therefore reads False here,
    which is the case the placeholder exists for.
    """
    rule = _build_rule(include_metrics=True, min_percent_delta=100.0, min_expected_count=0.0)
    move = abs(expected) * 0.01 or 1.0
    return not rule_matches_anomaly(
        rule, _metric_candidate(expected=expected, actual=expected + move)
    )


@pytest.mark.parametrize("expected", _BASELINE_GRID)
def test_every_baseline_reader_agrees_with_the_matcher(expected: float) -> None:
    """Matcher, both outbound encodings and the stored number, pinned equal.

    They are separate functions in separate modules and only a test can hold
    them together. Regress any one of them to ``expected_count > 0`` and it
    starts calling every negative row "no baseline" while the matcher keeps
    firing on it — the renderer contradicting the gate that admitted the signal
    — and this goes red on the first negative value in the grid.
    """
    matcher = _matcher_reads_a_baseline(expected)

    assert has_baseline(expected) is matcher
    # The two outbound encodings, both routed through ``has_baseline``: the
    # words for a human, ``null`` for a program.
    assert (format_percent_delta(200.0, expected) != NO_BASELINE_LABEL) is matcher
    assert (percent_delta_or_none(200.0, expected) is not None) is matcher
    # And the number live dispatch stores and the simulator replays: a measured
    # ratio where there is a baseline, the frozen 0.0 placeholder where not.
    assert (percent_delta_of(expected * 3.0, expected) != 0.0) is matcher


def test_percent_delta_of_measures_a_negative_baseline_as_a_size() -> None:
    """-3 -> -9 is a 200% move, exactly as 3 -> 9 is.

    Numerator and divisor are both magnitudes, so the ratio cannot flip sign
    with the level; direction is carried by ``direction``/``actual_count`` and
    never by this field.
    """
    assert percent_delta_of(-9.0, -3.0) == pytest.approx(200.0)
    assert percent_delta_of(9.0, 3.0) == pytest.approx(200.0)
    assert percent_delta_of(-1.0, -3.0) == pytest.approx(200.0 / 3.0)
    # A move THROUGH zero is still a size: -3 -> +3 is a 200% move.
    assert percent_delta_of(3.0, -3.0) == pytest.approx(200.0)
    # No baseline keeps the frozen placeholder — the column is NOT NULL.
    assert percent_delta_of(7.0, 0.0) == 0.0


def test_alert_renderers_report_a_measured_negative_baseline() -> None:
    """The label and the number may never disagree about whether there was one."""
    assert format_percent_delta(200.0, -3.0) == "200.0%"
    assert percent_delta_or_none(200.0, -3.0) == pytest.approx(200.0)
    # The genuine no-baseline class is unchanged: named, never printed as 0%.
    assert format_percent_delta(0.0, 0.0) == NO_BASELINE_LABEL
    assert percent_delta_or_none(0.0, 0.0) is None
    # And the ordinary positive case is untouched.
    assert format_percent_delta(200.0, 3.0) == "200.0%"
    assert percent_delta_or_none(200.0, 3.0) == pytest.approx(200.0)


# ── Every READER of a baseline, not just the writers ─────────────────────────
#
# The writers moved first (``dispatch._create_deliveries``, the matcher, the
# detector) because the column is frozen history and a row stored as 0.0 can
# never be recovered. The readers each carried their OWN copy of the test, and a
# half-applied semantic change is worse than an unapplied one: the same anomaly
# answered differently depending on which surface was looking at it. These pin
# the readers to the writers.

_SIGNED_BASELINE = -3.0
_SIGNED_ACTUAL = -9.0


def _delivery_item(
    *,
    expected_count: float,
    actual_count: float,
    percent_delta: float,
    direction: str = "drop",
    scope_name: str = "net_margin",
) -> AlertDeliveryItem:
    """An unsaved delivery row carrying only what the readers under test read."""
    return AlertDeliveryItem(
        id=uuid.uuid4(),
        delivery_id=uuid.uuid4(),
        scope_type=SCOPE_METRIC,
        scope_ref=str(uuid.uuid4()),
        scope_name=scope_name,
        bucket=_CANDIDATE_BUCKET,
        direction=direction,
        actual_count=actual_count,
        expected_count=expected_count,
        absolute_delta=abs(actual_count - expected_count),
        percent_delta=percent_delta,
    )


def test_digest_files_a_signed_metric_as_a_drop_not_as_unbaselined() -> None:
    """A negative baseline is a baseline, in the headline and in the groups.

    ``_digest_headline`` and ``_digest_groups`` bucket the same list and must
    bucket it the SAME way — the heading says "1 down" and the group under it
    holds the item. Asking ``expected_count > 0`` filed the signed row under
    "new", which is the group for a counter that fired from nothing: it is sorted
    by absolute delta because an undefined ratio has no magnitude to rank, so the
    one item here whose 200% move IS its story lost the ranking that would have
    surfaced it, and the headline lost the worst-drop clause entirely.
    """
    signed = _delivery_item(
        expected_count=_SIGNED_BASELINE,
        actual_count=_SIGNED_ACTUAL,
        percent_delta=200.0,
    )
    unbaselined = _delivery_item(
        expected_count=0.0,
        actual_count=12.0,
        percent_delta=0.0,
        direction="spike",
        scope_name="first_sighting",
    )

    headline = _digest_headline([signed, unbaselined], 2)
    assert "1 down" in headline
    assert "1 new" in headline
    # Named as the worst mover, which the "new" bucket is never eligible for.
    assert "worst net_margin down 200%" in headline

    labels = [label for label, _ in _digest_groups([signed, unbaselined])]
    assert labels == ["1 down", "1 new"]
    groups = dict(_digest_groups([signed, unbaselined]))
    assert groups["1 down"] == [signed]
    assert groups["1 new"] == [unbaselined]


def test_inbox_group_reports_a_measured_negative_baseline_as_its_worst_move() -> None:
    """One response may not answer the same question two ways.

    ``percent_delta`` (the newest item, through ``percent_delta_or_none``) and
    ``max_abs_percent_delta`` (the largest MEASURED deviation in the group)
    describe the same rows. The first already read a signed baseline correctly
    while the second filtered on ``expected_count > 0`` and dropped every signed
    row, so a group whose card said "200.0%" reported no worst move at all.
    """
    now = datetime.now(UTC)
    signed = _delivery_item(
        expected_count=_SIGNED_BASELINE,
        actual_count=_SIGNED_ACTUAL,
        percent_delta=200.0,
    )
    delivery = AlertDelivery(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
        destination_id=uuid.uuid4(),
        rule_id=uuid.uuid4(),
        status="sent",
        matched_count=1,
        created_at=now,
    )
    destination = AlertDestination(id=uuid.uuid4(), name="Main Slack", type="slack")
    rule = AlertRule(id=uuid.uuid4(), name="Metrics only")
    scan_config = ScanConfig(id=uuid.uuid4(), name="sc-signed")

    group = _build_inbox_group_response(
        correlation_group_id=uuid.uuid4(),
        state=None,
        rows=[(signed, delivery, destination, rule, scan_config)],
        now=now,
        acted_by_name=None,
    )

    assert group.expected_count == pytest.approx(_SIGNED_BASELINE)
    assert group.percent_delta == pytest.approx(200.0)
    assert group.max_abs_percent_delta == pytest.approx(200.0)


def test_inbox_group_still_reports_no_worst_move_without_a_baseline() -> None:
    """The class the filter exists for is untouched: 0.0 is a placeholder.

    Folding it in made a zero-baseline group — the loudest class there is — sort
    as the smallest deviation in the inbox, so widening the test
    to ``!= 0`` must not widen it to "everything".
    """
    now = datetime.now(UTC)
    item = _delivery_item(
        expected_count=0.0,
        actual_count=12.0,
        percent_delta=0.0,
        direction="spike",
        scope_name="first_sighting",
    )
    delivery = AlertDelivery(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
        destination_id=uuid.uuid4(),
        rule_id=uuid.uuid4(),
        status="sent",
        matched_count=1,
        created_at=now,
    )

    group = _build_inbox_group_response(
        correlation_group_id=uuid.uuid4(),
        state=None,
        rows=[
            (
                item,
                delivery,
                AlertDestination(id=uuid.uuid4(), name="Main Slack", type="slack"),
                AlertRule(id=uuid.uuid4(), name="Metrics only"),
                ScanConfig(id=uuid.uuid4(), name="sc-new"),
            )
        ],
        now=now,
        acted_by_name=None,
    )

    assert group.percent_delta is None
    assert group.max_abs_percent_delta is None


# ── The drift guard ──────────────────────────────────────────────────────────
#
# Six copies of one expression is how the change got half-applied. The copies are
# gone — every surface below calls ``alert_templates.has_baseline`` /
# ``percent_delta_of``, a leaf module importing only ``tripl.models.*`` — and
# this is what stops a seventh appearing. The behavioural tests above catch a
# regression in the readers they cover; this catches one ANYWHERE in the module,
# including a reader added tomorrow, and it names the line.

_BASELINE_MODULES = (
    "tripl.alert_templates",
    "tripl.schemas.alerting",
    "tripl.services._alerting_deliveries",
    "tripl.services.demo.builders.alerts",
    "tripl.worker.tasks.alerts_messages",
    "tripl.worker.tasks.metrics.alert_payload",
    "tripl.worker.tasks.metrics.dispatch",
)

_ORDERING_OPS = (ast.Gt, ast.GtE, ast.Lt, ast.LtE)
_BASELINE_OPERANDS = frozenset({"expected", "expected_count"})


def _operand_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _is_zero_literal(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
        and node.value == 0
    )


def _ordering_tests_against_zero(source_path: Path) -> list[str]:
    """Every ``expected [<|<=|>|>=] 0`` in one module, as ``file:line`` strings.

    Parsed, not grepped: these modules DISCUSS ``expected_count > 0`` at length in
    their comments and docstrings, because the history is the reason the helper
    exists. An AST walk sees the code and nothing else, so the explanation can
    stay.
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        # ``a < b <= c`` is ONE Compare node with two ops and three operands, so
        # the ops pair up with adjacent operands: ``operands[:-1]`` and
        # ``operands[1:]`` are both ``len(ops)`` long, which is what makes
        # ``strict`` meaningful here rather than an exception.
        for op, left, right in zip(node.ops, operands[:-1], operands[1:], strict=True):
            if not isinstance(op, _ORDERING_OPS):
                continue
            names = {_operand_name(left), _operand_name(right)}
            if not names & _BASELINE_OPERANDS:
                continue
            if _is_zero_literal(left) or _is_zero_literal(right):
                hits.append(f"{source_path.name}:{node.lineno}")
    return hits


@pytest.mark.parametrize("module_name", _BASELINE_MODULES)
def test_no_alerting_surface_re_derives_the_baseline_test(module_name: str) -> None:
    """Being ABOVE zero is not the same question as having a baseline.

    ZERO is the no-baseline condition and the only one. An ordering test against
    zero answers a DIFFERENT question — "is the level positive" — and a signed
    catalog metric, a signed ``fact`` sum/avg, or a ``sql`` level below zero is a
    real baseline the detector scores and the matcher fires on.
    Any surface asking the ordering question prints "no
    baseline" over the very number that made the rule fire.

    Fails with the offending ``file:line``. If a genuinely different question
    about the sign of an expectation ever belongs in one of these modules, say so
    with a named predicate rather than by loosening this list.
    """
    module = importlib.import_module(module_name)
    source = inspect.getsourcefile(module)
    assert source is not None
    assert _ordering_tests_against_zero(Path(source)) == []


# ---------------------------------------------------------------------------
# event_type filters on event-anchored signals
# ---------------------------------------------------------------------------

_TYPE_A = uuid.uuid4()
_TYPE_B = uuid.uuid4()
_EVENT_ID = uuid.uuid4()


def _event_anchored_candidate(scope_type: str) -> _Candidate:
    """An event-anchored candidate exactly as its producer writes it.

    Event-scope anomalies, variable-value drifts and event-scope release
    regressions all carry a real ``event_id`` and a NULL ``event_type_id``.
    """
    return _Candidate(
        scope_type=scope_type,
        scope_ref=str(_EVENT_ID),
        event_id=_EVENT_ID,
        event_type_id=None,
        direction="drop",
        actual_count=1.0,
        expected_count=10.0,
    )


_EVENT_ANCHORED_SCOPES = [
    (SCOPE_EVENT, {"include_events": True}),
    (SCOPE_VARIABLE_VALUE_DRIFT, {"include_variable_value_drifts": True}),
    (SCOPE_RELEASE_REGRESSION, {"include_release_regressions": True}),
]


@pytest.mark.parametrize(("scope_type", "scope_toggle"), _EVENT_ANCHORED_SCOPES)
def test_event_type_filter_excludes_a_mismatched_event_anchored_signal(
    scope_type: str,
    scope_toggle: dict[str, object],
) -> None:
    """The filed defect: an ``in {A}`` filter admitted a type-B signal.

    The candidate's own ``event_type_id`` is NULL by design, and the matcher
    read that as "this signal carries no such field, pass it through" — so the
    filter was silently a no-op for every event-anchored family at once.
    """
    rule = _build_rule(filters=[_event_type_filter("in", _TYPE_A)], **scope_toggle)
    candidate = _event_anchored_candidate(scope_type)

    assert (
        rule_matches_anomaly(rule, candidate, event_type_by_event_id={_EVENT_ID: _TYPE_B}) is False
    )


@pytest.mark.parametrize(("scope_type", "scope_toggle"), _EVENT_ANCHORED_SCOPES)
def test_event_type_filter_excludes_an_explicitly_excluded_type(
    scope_type: str,
    scope_toggle: dict[str, object],
) -> None:
    """The mirror half: ``not_in {B}`` admitted the very signal it excluded."""
    rule = _build_rule(filters=[_event_type_filter("not_in", _TYPE_B)], **scope_toggle)
    candidate = _event_anchored_candidate(scope_type)

    assert (
        rule_matches_anomaly(rule, candidate, event_type_by_event_id={_EVENT_ID: _TYPE_B}) is False
    )


@pytest.mark.parametrize(("scope_type", "scope_toggle"), _EVENT_ANCHORED_SCOPES)
def test_event_type_filter_admits_a_matching_event_anchored_signal(
    scope_type: str,
    scope_toggle: dict[str, object],
) -> None:
    """Positive control: the filter must still admit what it names."""
    rule = _build_rule(filters=[_event_type_filter("in", _TYPE_B)], **scope_toggle)
    candidate = _event_anchored_candidate(scope_type)

    assert (
        rule_matches_anomaly(rule, candidate, event_type_by_event_id={_EVENT_ID: _TYPE_B}) is True
    )


def test_event_type_filter_still_passes_a_genuinely_event_less_rollup() -> None:
    """The passthrough stays: a project-total rollup carries no event at all.

    Only a signal that is ANCHORED to an event gains a resolvable type. The
    ``actual is None`` passthrough is still the right answer for the rollups and
    for catalog metrics, and the docs promise it.
    """
    rule = _build_rule(filters=[_event_type_filter("in", _TYPE_A)])
    rollup = _Candidate(
        scope_type=SCOPE_PROJECT_TOTAL,
        scope_ref=str(uuid.uuid4()),
        direction="drop",
        actual_count=1.0,
        expected_count=10.0,
    )

    assert rule_matches_anomaly(rule, rollup, event_type_by_event_id={_EVENT_ID: _TYPE_B}) is True


def _seed_event_scope_alerting(
    session: Session,
    *,
    filtered_type: str,
) -> tuple[ScanConfig, EventType, EventType]:
    """A project whose only live signal is one event-scope anomaly on type B.

    ``filtered_type`` picks which type the rule's ``event_type`` filter names:
    ``"a"`` is the type the event does NOT have, ``"b"`` the one it does.
    """
    config = _seed_project(session)
    type_a = EventType(
        id=uuid.uuid4(),
        project_id=config.project_id,
        name="checkout",
        display_name="Checkout",
        description="",
    )
    type_b = EventType(
        id=uuid.uuid4(),
        project_id=config.project_id,
        name="session",
        display_name="Session",
        description="",
    )
    session.add_all([type_a, type_b])
    session.commit()
    event = Event(
        id=uuid.uuid4(),
        project_id=config.project_id,
        event_type_id=type_b.id,
        name="session_start",
        description="",
    )
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=config.project_id,
        type="slack",
        name="Main Slack",
        enabled=True,
        webhook_url_encrypted="secret",
        delivery_schedule_cron=None,
    )
    rule = AlertRule(
        id=uuid.uuid4(),
        destination_id=destination.id,
        name="Events only",
        enabled=True,
        include_project_total=False,
        include_event_types=False,
        include_events=True,
        notify_on_spike=True,
        notify_on_drop=True,
        min_percent_delta=0,
        min_absolute_delta=0,
        min_expected_count=0,
        cooldown_minutes=1440,
    )
    # Wired through the relationships, not by bare foreign keys, so the graph the
    # dispatch run walks in memory matches what is written.
    rule.filters = [
        AlertRuleFilter(
            id=uuid.uuid4(),
            rule_id=rule.id,
            field="event_type",
            operator="in",
            values=[str(type_a.id if filtered_type == "a" else type_b.id)],
            position=0,
        )
    ]
    destination.rules = [rule]
    bucket = datetime.now(UTC).replace(minute=0, second=0, microsecond=0, tzinfo=None) - timedelta(
        hours=2
    )
    # Dispatch only treats a scope as live while the scan has metrics for it.
    session.add_all([event, destination, rule])
    session.add(
        EventMetric(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            event_id=event.id,
            event_type_id=type_b.id,
            bucket=bucket,
            count=200,
        )
    )
    session.add(
        MetricAnomaly(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            scope_type=SCOPE_EVENT,
            scope_ref=str(event.id),
            event_id=event.id,
            # The producer's NULL, which is the whole defect.
            event_type_id=None,
            bucket=bucket,
            direction="spike",
            actual_count=200.0,
            expected_count=20.0,
            stddev=1.0,
            z_score=10.0,
        )
    )
    session.commit()
    return config, type_a, type_b


def test_dispatch_drops_an_event_signal_whose_type_the_filter_excludes(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Live dispatch: the rule filters on type A, the event is type B.

    Before the fix one delivery was minted for it, because the candidate's NULL
    ``event_type_id`` made the filter inert.
    """
    with sync_session_factory() as session:
        config, _type_a, _type_b = _seed_event_scope_alerting(session, filtered_type="a")

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

        assert delivery_ids == []
        assert session.execute(select(AlertDelivery)).scalars().all() == []
        assert session.execute(select(AlertDeliveryItem)).scalars().all() == []


def test_dispatch_keeps_an_event_signal_whose_type_the_filter_names(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Positive control in the same harness: filtering on type B still delivers."""
    with sync_session_factory() as session:
        config, _type_a, _type_b = _seed_event_scope_alerting(session, filtered_type="b")

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

        assert len(delivery_ids) == 1
        items = session.execute(select(AlertDeliveryItem)).scalars().all()
        assert [item.scope_type for item in items] == [SCOPE_EVENT]


async def _simulate_event_type_filter(
    client: AsyncClient, *, filtered_type: str
) -> dict[str, object]:
    """Seed a project with one event-scope anomaly and replay a filtered rule."""
    slug = f"a2-sim-{filtered_type}"
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": f"A2 Sim {filtered_type}", "slug": slug, "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    now = datetime.now(UTC)
    type_a_id = uuid.uuid4()
    type_b_id = uuid.uuid4()
    async with TestSessionLocal() as session, session.begin():
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"ds-{filtered_type}",
            db_type="clickhouse",
            host="h",
            port=8123,
            database_name="d",
            username="u",
            password_encrypted="",
        )
        session.add(data_source)
        await session.flush()
        scan = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project_id,
            name=f"sc-{filtered_type}",
            base_query="SELECT 1",
            cardinality_threshold=100,
            interval="1h",
        )
        type_a = EventType(
            id=type_a_id,
            project_id=project_id,
            name="checkout",
            display_name="Checkout",
            description="",
        )
        type_b = EventType(
            id=type_b_id,
            project_id=project_id,
            name="session",
            display_name="Session",
            description="",
        )
        session.add_all([scan, type_a, type_b])
        await session.flush()
        event = Event(
            id=uuid.uuid4(),
            project_id=project_id,
            event_type_id=type_b_id,
            name="session_start",
            description="",
        )
        session.add(event)
        await session.flush()
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=scan.id,
                scope_type=SCOPE_EVENT,
                scope_ref=str(event.id),
                event_id=event.id,
                event_type_id=None,
                bucket=now - timedelta(days=1),
                actual_count=200.0,
                expected_count=20.0,
                stddev=1.0,
                z_score=10.0,
                direction="spike",
            )
        )

    destination_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "slack",
            "name": "Sim Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T1/B1/a2sim",
        },
    )
    assert destination_resp.status_code == 201
    destination_id = destination_resp.json()["id"]

    filtered_id = type_a_id if filtered_type == "a" else type_b_id
    rule_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules",
        json={
            "name": "Events only",
            "enabled": True,
            "include_project_total": False,
            "include_event_types": False,
            "include_events": True,
            "notify_on_spike": True,
            "notify_on_drop": True,
            "min_percent_delta": 0,
            "min_absolute_delta": 0,
            "min_expected_count": 0,
            "cooldown_minutes": 60,
            "filters": [
                {"field": "event_type", "operator": "in", "values": [str(filtered_id)]},
            ],
        },
    )
    assert rule_resp.status_code == 201
    rule_id = rule_resp.json()["id"]

    resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules/{rule_id}/simulate"
        "?days=7"
    )
    assert resp.status_code == 200
    return resp.json()


async def test_simulator_applies_the_event_type_filter_like_live_dispatch(
    client: AsyncClient,
) -> None:
    """The replay and the pipeline must answer the same question.

    ``alerting_matching`` exists so the in-UI simulator cannot drift from live
    dispatch; a fix applied only in the worker would have re-introduced exactly
    that drift.
    """
    excluded = await _simulate_event_type_filter(client, filtered_type="a")
    assert excluded["anomalies_considered"] == 1
    assert excluded["matched_before_cooldown"] == 0
    assert excluded["firings"] == []

    included = await _simulate_event_type_filter(client, filtered_type="b")
    assert included["anomalies_considered"] == 1
    assert included["matched_before_cooldown"] == 1
    assert [firing["scope_type"] for firing in included["firings"]] == [SCOPE_EVENT]


async def test_simulator_reports_the_percent_delta_live_dispatch_would_store(
    client: AsyncClient,
) -> None:
    """The rule simulator and the send path must agree on the same anomaly.

    ``test_delivery_records_a_measured_percent_delta_for_a_negative_baseline``
    pins what live dispatch stores for -3 -> -9 on a signed catalog metric:
    200.0, because ``abs(expected)`` is what the matcher divided by when it
    admitted the row. The replay held its OWN copy of that expression, still
    asking ``expected_count > 0``, so it reported 0.0% for the identical
    anomaly under the identical rule — a simulator disagreeing with the thing it
    simulates, which is worse than no simulator. Both now read the one
    ``alert_templates.percent_delta_of``.
    """
    slug = "a2-sim-signed"
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "A2 Sim Signed", "slug": slug, "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    now = datetime.now(UTC)
    async with TestSessionLocal() as session, session.begin():
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"ds-signed-{uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="h",
            port=8123,
            database_name="d",
            username="u",
            password_encrypted="",
        )
        session.add(data_source)
        await session.flush()
        # A scan exists but owns nothing here: a catalog metric anomaly is
        # project-global, so it reaches the replay through the project's metric
        # definitions rather than through the scan join.
        session.add(
            ScanConfig(
                id=uuid.uuid4(),
                data_source_id=data_source.id,
                project_id=project_id,
                name="sc-signed",
                base_query="SELECT 1",
                cardinality_threshold=100,
                interval="1h",
            )
        )
        metric = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project_id,
            name="net_margin",
            display_name="Net margin",
            kind=MetricKind.sql.value,
            aggregation=None,
            composition=None,
            config={},
            data_source_id=data_source.id,
            interval="1h",
            status=MetricStatus.active.value,
            anomaly_detection_enabled=True,
        )
        session.add(metric)
        await session.flush()
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                # Catalog metric anomalies are project-global: NULL scan config,
                # scope_ref is the metric definition id.
                scan_config_id=None,
                scope_type=SCOPE_METRIC,
                scope_ref=str(metric.id),
                event_id=None,
                event_type_id=None,
                bucket=now - timedelta(days=1),
                actual_count=-9.0,
                expected_count=-3.0,
                stddev=1.0,
                z_score=-6.0,
                direction="drop",
            )
        )

    destination_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "slack",
            "name": "Sim Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T1/B1/a2signed",
        },
    )
    assert destination_resp.status_code == 201
    destination_id = destination_resp.json()["id"]

    rule_resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules",
        json={
            "name": "Metrics only",
            "enabled": True,
            "include_project_total": False,
            "include_event_types": False,
            "include_events": False,
            "include_metrics": True,
            "notify_on_spike": True,
            "notify_on_drop": True,
            # The rule admits this row BECAUSE the move is 200% of a baseline of
            # magnitude 3. A reader that then calls it "no baseline" contradicts
            # the gate that let it through.
            "min_percent_delta": 100,
            "min_absolute_delta": 0,
            "min_expected_count": 1,
            "cooldown_minutes": 60,
        },
    )
    assert rule_resp.status_code == 201
    rule_id = rule_resp.json()["id"]

    resp = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations/{destination_id}/rules/{rule_id}/simulate"
        "?days=7"
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["anomalies_considered"] == 1
    assert body["matched_before_cooldown"] == 1
    assert len(body["firings"]) == 1
    firing = body["firings"][0]
    assert firing["expected_count"] == pytest.approx(-3.0)
    assert firing["actual_count"] == pytest.approx(-9.0)
    assert firing["absolute_delta"] == pytest.approx(6.0)
    # The exact number ``dispatch._create_deliveries`` stores for this anomaly.
    assert firing["percent_delta"] == pytest.approx(200.0)
    # ...and the rendered preview quotes it rather than the no-baseline label,
    # which is the half of the divergence an operator actually reads.
    assert "200.0%" in firing["rendered_item"]
    assert NO_BASELINE_LABEL not in firing["rendered_item"]
