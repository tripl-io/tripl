from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, ColumnExpressionArgument, delete, select
from sqlalchemy import and_ as sa_and
from sqlalchemy import func as sa_func
from sqlalchemy import not_ as sa_not
from sqlalchemy import or_ as sa_or
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import InstrumentedAttribute, Session

from tripl.core.analyzers.anomaly_detector import (
    SCOPE_EVENT,
    SCOPE_EVENT_TYPE,
    SCOPE_METRIC,
    SCOPE_PROJECT_TOTAL,
    AnomalyDetectionSettings,
    BaselinePoint,
    DetectedAnomaly,
    SeriesPoint,
    SuppressedRange,
    detect_anomalies,
    is_provably_silent,
    required_history_buckets,
    settling_buckets_for,
)
from tripl.core.bucketing import to_utc
from tripl.core.intervals import get_interval
from tripl.metric_grid import MetricGrid, grid_population_filter, metric_grid_stmt, metric_grids
from tripl.metric_monitoring import monitored_metric_criteria
from tripl.models.anomaly_scope_override import AnomalyScopeOverride
from tripl.models.domain_enums import MetricBreakdownAnomalyKind, MetricKind
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_baseline import MetricBaseline
from tripl.models.metric_breakdown_anomaly import MetricBreakdownAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.observability.metrics import anomalies_detected_total
from tripl.services.planned_event_service import retag_planned_anomalies
from tripl.worker.analyzers.metric_value_kind import is_count_shaped
from tripl.worker.tasks.metrics.coverage import covered_buckets_from_scan_jobs

# Fractional (ratio/average/sql) catalog metrics drop the count-shaped
# ``min_expected_count`` gate so sub-unit ratio movements survive.
# We keep a tiny POSITIVE floor rather than a blanket 0 so a genuinely
# empty/flatlined-at-zero fractional series can't manufacture multi-sigma
# anomalies from pure noise — the detector lane widens the stddev floor, this
# preserves the volume guard.
#
# On a series that actually carries negative values the detector reads this as a
# floor on |expected| (``anomaly_detector._clears_volume_gate``): a ratio
# flatlined at 0 is still gated, while a level sitting at -100 is scored exactly
# like one at +100 instead of being rejected for its sign.
_FRACTIONAL_MIN_EXPECTED_COUNT = 1e-6
# Age-out horizon for config-scoped anomaly markers. Rows older than this are
# deleted during each recompute so stale historical dots stop being served as
# active markers. Kept generous: the serving/classify layer (Wave 1 wall-clock
# freshness horizon in ``monitoring_utils.classify_signal_state``) already
# ages markers out for rendering, so this only trims long-dead rows and never
# touches ``metric``-scope rows (NULL scan_config_id, shared across configs).
ANOMALY_RETENTION_DAYS = 180
# Anomaly re-evaluation always sweeps at least this many trailing buckets, even
# on an incremental run that only collected the newest one or two. A backfilled
# or re-collected bucket inside this window then gets its flag cleared/updated on
# the next run instead of being frozen at whatever the first pass decided.
# Replays over a wider explicit window keep that wider window.
#
# The count is in buckets OF THE SERIES BEING SCORED, which is why it lives here
# rather than in the scan orchestrator: catalog metrics carry their own grid.
# Multiplying it by the scan config's 1h delta gave a 1d metric a 30-HOUR
# window — one candidate bucket, which the settling allowance then withheld, so
# the metric could never emit. A 1d series with a 25-day collapse scored 264
# anomalies on its own grid and 0 on the 1h scan grid.
ANOMALY_TRAILING_REEVAL_BUCKETS = 30
# Default ingestion-settling allowance for the recalculation entrypoints: none.
# The scan orchestrator owns the policy and passes its own allowance (see
# ``tasks.ANOMALY_INGESTION_SETTLING``); callers that hand in an explicit,
# already-settled window (replays, tests, conformance harnesses) get the
# historical behavior of scoring every bucket they asked for.
NO_INGESTION_SETTLING = timedelta(0)
# ``ScanJob.created_at`` is only an approximation of the window the job went on
# to record: a job that sat queued stamps a created_at earlier than the window it
# eventually wrote. One day of slack absorbs that, so a coverage horizon can be
# used as a ``created_at`` floor without dropping a job whose window reaches back
# past it. BOTH callers of ``covered_buckets_from_scan_jobs`` apply it — the
# running scan's horizon in ``coverage_history_start`` and the per-metric horizon
# in ``_metric_covered_buckets`` — because a job dropped from either read costs
# the same thing: every genuinely-zero bucket it covered is EXCLUDED from the
# series rather than zero-filled. It lives up here with the other module
# constants because the per-metric caller is defined well above the horizon
# helper.
COVERAGE_HORIZON_SLACK = timedelta(days=1)


# ── the subsystem's ONE bucket convention ────────────────────────────────────
# Every bucket compared, hashed or set-tested inside detection is TZ-AWARE UTC.
# It is a real decision, not an accident of the first value that arrived:
#
#  * ``core.bucketing`` already declares it for the whole pipeline ("a naive
#    datetime is ASSUMED to be UTC"), and ``metric_composition.normalize_series``
#    already enforces it at the other junction where two series meet;
#  * PostgreSQL — what production runs on — hands back aware values from every
#    ``timestamptz`` bucket column, so aware is the majority convention already;
#  * the alternative (strip to naive) would have to UNDO a correct annotation on
#    the production backend and would still leave ``datetime.now(UTC)`` and every
#    parsed ``result_summary`` bound to convert at each use.
#
# Naive values still ENTER, from two places and two only: a bucket column read on
# a backend without timezone support (SQLite, in tests), and a window handed in by
# a caller. Both are stamped at the boundary — the loaders below, the entrypoints
# below that, and ``coverage.covered_buckets_from_scan_jobs`` — so nothing
# downstream converts. In particular ``anomaly_detector.expand_series`` tests
# ``bucket not in covered_buckets``: a set lookup CANNOT normalize, a mismatch
# there raises nothing, and the silent result is that coverage under-reports and
# genuinely-zero buckets are dropped from the baseline instead of zero-filled.
#
# A new producer conforms by calling ``to_utc`` where its value enters, never at
# the comparison.
def _canonical_window(
    evaluation_start: datetime, evaluation_end: datetime
) -> tuple[datetime, datetime]:
    """An evaluation window as the detection passes compare it: aware UTC."""
    return to_utc(evaluation_start), to_utc(evaluation_end)


def _canonical_covered(covered_buckets: set[datetime] | None) -> set[datetime] | None:
    """A caller-supplied coverage set re-keyed onto the comparison convention.

    ``None`` (no coverage gating) is preserved; it is not the same as an empty
    set, which excludes every bucket.
    """
    if covered_buckets is None:
        return None
    return {to_utc(bucket) for bucket in covered_buckets}


def _build_anomaly_settings(
    settings: ProjectAnomalySettings,
) -> AnomalyDetectionSettings:
    return AnomalyDetectionSettings(
        baseline_window_buckets=settings.baseline_window_buckets,
        min_history_buckets=settings.min_history_buckets,
        sigma_threshold=settings.sigma_threshold,
        min_expected_count=settings.min_expected_count,
    )


def _get_project_anomaly_settings(
    session: Session,
    project_id: uuid.UUID,
) -> ProjectAnomalySettings | None:
    return session.execute(
        select(ProjectAnomalySettings).where(ProjectAnomalySettings.project_id == project_id)
    ).scalar_one_or_none()


# Per-scope sensitivity written by the false-positive ratchet, keyed the way an
# anomaly keys itself. Loaded ONCE per recalculation and applied per scope — a
# per-scope override nobody reads would be worse than the project-wide ratchet
# it replaced, since the operator would be told the scope was tuned and nothing
# would change.
ScopeOverrides = dict[tuple[str, str], tuple[float, int]]


def _load_scope_overrides(
    session: Session,
    *,
    project_id: uuid.UUID,
    scan_config_id: uuid.UUID,
) -> ScopeOverrides:
    """Overrides that can apply to this config's recalculation.

    Both this config's rows AND the project's NULL-config rows: ``metric`` scopes
    are project-global (their ``MetricAnomaly`` rows carry a NULL scan_config_id)
    yet are recomputed from inside a scan's run. The two sets cannot collide —
    ``metric`` is the only scope type stored with a NULL config.
    """
    rows = session.execute(
        select(AnomalyScopeOverride).where(
            AnomalyScopeOverride.project_id == project_id,
            sa_or(
                AnomalyScopeOverride.scan_config_id == scan_config_id,
                AnomalyScopeOverride.scan_config_id.is_(None),
            ),
        )
    ).scalars()
    return {
        (str(row.scope_type), row.scope_ref): (
            float(row.sigma_threshold),
            int(row.min_expected_count),
        )
        for row in rows
    }


def _scope_settings(
    settings: AnomalyDetectionSettings,
    overrides: ScopeOverrides,
    scope_type: str,
    scope_ref: str,
) -> AnomalyDetectionSettings:
    override = overrides.get((scope_type, scope_ref))
    if override is None:
        return settings
    sigma_threshold, min_expected_count = override
    return replace(
        settings,
        sigma_threshold=sigma_threshold,
        min_expected_count=min_expected_count,
    )


def _scan_has_event_level_breakdown_columns(session: Session, scan_config_id: uuid.UUID) -> bool:
    event_ids = (
        select(EventMetric.event_id)
        .where(
            EventMetric.scan_config_id == scan_config_id,
            EventMetric.event_id.is_not(None),
        )
        .distinct()
    )
    rows = session.execute(
        select(Event.metric_breakdown_columns).where(Event.id.in_(event_ids))
    ).scalars()
    return any(columns for columns in rows)


def _load_scope_points(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    scope_type: str,
    scope_ref: str,
    history_from: datetime,
    time_to: datetime,
) -> list[SeriesPoint]:
    if scope_type == SCOPE_PROJECT_TOTAL:
        rows = session.execute(
            select(EventMetric.bucket, sa_func.sum(EventMetric.count))
            .where(
                EventMetric.scan_config_id == scan_config_id,
                EventMetric.event_id.is_(None),
                EventMetric.event_type_id.is_not(None),
                EventMetric.bucket >= history_from,
                EventMetric.bucket < time_to,
            )
            .group_by(EventMetric.bucket)
            .order_by(EventMetric.bucket)
        ).all()
        return [SeriesPoint(bucket=to_utc(bucket), count=int(count)) for bucket, count in rows]

    if scope_type == SCOPE_EVENT_TYPE:
        event_type_id = uuid.UUID(scope_ref)
        rows = session.execute(
            select(EventMetric.bucket, EventMetric.count)
            .where(
                EventMetric.scan_config_id == scan_config_id,
                EventMetric.event_id.is_(None),
                EventMetric.event_type_id == event_type_id,
                EventMetric.bucket >= history_from,
                EventMetric.bucket < time_to,
            )
            .order_by(EventMetric.bucket)
        ).all()
        return [SeriesPoint(bucket=to_utc(bucket), count=count) for bucket, count in rows]

    event_id = uuid.UUID(scope_ref)
    rows = session.execute(
        select(EventMetric.bucket, EventMetric.count)
        .where(
            EventMetric.scan_config_id == scan_config_id,
            EventMetric.event_id == event_id,
            EventMetric.bucket >= history_from,
            EventMetric.bucket < time_to,
        )
        .order_by(EventMetric.bucket)
    ).all()
    return [SeriesPoint(bucket=to_utc(bucket), count=count) for bucket, count in rows]


# Direction of the anomalies a late source withholds (#269).
_HELD_DIRECTION = "drop"


def _apply_drop_hold(
    anomalies: list[DetectedAnomaly],
    direction_column: InstrumentedAttribute[str],
    *,
    hold_drops: bool,
    held: list[int] | None,
) -> tuple[list[DetectedAnomaly], ColumnElement[bool] | None]:
    """Withhold drop-direction anomalies while the scan's source is late (#269).

    A late warehouse load reads as a volume drop on every scope. While the
    source is late or overdue the detector still scores the whole series, but
    no NEW drop is written: the drops found are counted into ``held`` and
    dropped from the insert, and the window's replace spares drop rows already
    stored (they were written while the data was on time, so they stand).
    Spikes are unaffected. Nothing is lost — the next run after the data lands
    re-collects every bucket since the previous newest event
    (``tasks._widen_for_held_buckets``), so the buckets that were empty during
    the delay are scored against the late rows, not as drops.

    Returns the anomalies to write and an extra delete filter (``None`` when
    nothing is held).
    """
    if not hold_drops:
        return anomalies, None
    kept = [anomaly for anomaly in anomalies if anomaly.direction != _HELD_DIRECTION]
    if held is not None:
        held.append(len(anomalies) - len(kept))
    return kept, direction_column != _HELD_DIRECTION


def _replace_scope_anomalies(
    session: Session,
    *,
    scan_config_id: uuid.UUID | None,
    scope_type: str,
    scope_ref: str,
    evaluation_start: datetime,
    evaluation_end: datetime,
    event_id: uuid.UUID | None,
    event_type_id: uuid.UUID | None,
    anomalies: list[DetectedAnomaly],
    suppressed_ranges: Sequence[SuppressedRange] = (),
    baselines: Sequence[BaselinePoint] | None = None,
    hold_drops: bool = False,
    held: list[int] | None = None,
) -> int:
    # The chart band of every scored bucket. ``None`` leaves the
    # stored baselines alone; a sequence, even an empty one, replaces the window.
    # Scan scopes only — see ``MetricBaseline``.
    if baselines is not None and scan_config_id is not None:
        _replace_scope_baselines(
            session,
            scan_config_id=scan_config_id,
            scope_type=scope_type,
            scope_ref=scope_ref,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            baselines=baselines,
        )

    # ``metric``-scope rows carry a NULL scan_config_id and are keyed purely by
    # (scope_type, scope_ref); event scopes additionally partition by config.
    delete_filters = [
        MetricAnomaly.scope_type == scope_type,
        MetricAnomaly.scope_ref == scope_ref,
        MetricAnomaly.bucket >= evaluation_start,
        MetricAnomaly.bucket < evaluation_end,
    ]
    # Ranges the detector declined to score rather than found clean.
    # An outage is announced ONCE, at the first flagged bucket at or after its
    # anchor, and every later pass whose window starts past that anchor emits
    # nothing for the run — while the announced row may still sit inside this
    # window. Clearing it here would destroy the outage's only row, and no later
    # pass would ever write another: the event would vanish from the page, the
    # badge, the bell and the drilldown, and its alert state would close.
    for suppressed in suppressed_ranges:
        delete_filters.append(
            sa_not(
                sa_and(
                    MetricAnomaly.bucket >= suppressed.start,
                    MetricAnomaly.bucket < suppressed.end,
                )
            )
        )
    if scan_config_id is None:
        delete_filters.append(MetricAnomaly.scan_config_id.is_(None))
    else:
        delete_filters.append(MetricAnomaly.scan_config_id == scan_config_id)
    anomalies, hold_filter = _apply_drop_hold(
        anomalies, MetricAnomaly.direction, hold_drops=hold_drops, held=held
    )
    if hold_filter is not None:
        delete_filters.append(hold_filter)
    session.execute(delete(MetricAnomaly).where(*delete_filters))

    rows: list[dict[str, object]] = []
    for anomaly in anomalies:
        rows.append(
            {
                "id": uuid.uuid4(),
                "scan_config_id": scan_config_id,
                "scope_type": scope_type,
                "scope_ref": scope_ref,
                "event_id": event_id,
                "event_type_id": event_type_id,
                "bucket": anomaly.bucket,
                "actual_count": anomaly.actual_count,
                "expected_count": anomaly.expected_count,
                "stddev": anomaly.stddev,
                "z_score": anomaly.z_score,
                "direction": anomaly.direction,
                # C3 columns: the floored stddev actually used in the z
                # denominator and which detector path produced the row, sourced
                # from the DetectedAnomaly (C1) so the chart band and marker
                # provenance stay consistent with what was flagged.
                "effective_stddev": anomaly.effective_stddev,
                "detector_kind": anomaly.kind,
            }
        )
        anomalies_detected_total.labels(scope=scope_type, direction=anomaly.direction).inc()

    # Idempotent insert: a concurrent collect_metrics run over the same window
    # (e.g. a manual replay overlapping a scheduled collection) deletes and
    # re-inserts the same (scope, bucket) rows; a plain INSERT trips the unique
    # index and fails the whole job. Upsert is safe.
    #
    # The conflict target depends on scan_config_id. Event scopes set it, so the
    # composite ``uq_metric_anomaly_scope_bucket`` (which includes it) dedupes
    # them. ``metric`` scopes carry a NULL scan_config_id; SQL treats NULLs as
    # DISTINCT, so that composite constraint NEVER fires for them — two
    # ``(NULL, 'metric', ref, bucket)`` rows from concurrent runs would both
    # insert. We instead target the partial unique index
    # ``uq_metric_anomaly_metric_scope`` (scope_type, scope_ref, bucket) WHERE
    # scan_config_id IS NULL, which excludes the NULL column and so does conflict.
    _updatable = [
        "event_id",
        "event_type_id",
        "actual_count",
        "expected_count",
        "stddev",
        "z_score",
        "direction",
        "effective_stddev",
        "detector_kind",
    ]
    null_scope = scan_config_id is None
    if rows:
        if session.bind is not None and session.bind.dialect.name == "sqlite":
            sqlite_stmt = sqlite_insert(MetricAnomaly).values(rows)
            if null_scope:
                sqlite_stmt = sqlite_stmt.on_conflict_do_update(
                    index_elements=["scope_type", "scope_ref", "bucket"],
                    index_where=MetricAnomaly.scan_config_id.is_(None),
                    set_={col: getattr(sqlite_stmt.excluded, col) for col in _updatable},
                )
            else:
                sqlite_stmt = sqlite_stmt.on_conflict_do_update(
                    index_elements=["scan_config_id", "scope_type", "scope_ref", "bucket"],
                    set_={col: getattr(sqlite_stmt.excluded, col) for col in _updatable},
                )
            session.execute(sqlite_stmt)
        else:
            pg_stmt = pg_insert(MetricAnomaly).values(rows)
            if null_scope:
                pg_stmt = pg_stmt.on_conflict_do_update(
                    index_elements=["scope_type", "scope_ref", "bucket"],
                    index_where=MetricAnomaly.scan_config_id.is_(None),
                    set_={col: getattr(pg_stmt.excluded, col) for col in _updatable},
                )
            else:
                pg_stmt = pg_stmt.on_conflict_do_update(
                    constraint="uq_metric_anomaly_scope_bucket",
                    set_={col: getattr(pg_stmt.excluded, col) for col in _updatable},
                )
            session.execute(pg_stmt)

    return len(anomalies)


def _replace_scope_baselines(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    scope_type: str,
    scope_ref: str,
    evaluation_start: datetime,
    evaluation_end: datetime,
    baselines: Sequence[BaselinePoint],
) -> None:
    """Rewrite one scope's per-bucket baselines over the evaluation window.

    Unlike the anomaly rows there is no suppressed range to spare: the detector
    re-scores every bucket of the window it can, so what it did not return now
    it has no band for. Upserted for the same reason the anomaly rows are — a
    manual replay can overlap a scheduled collection over the same window.
    """
    session.execute(
        delete(MetricBaseline).where(
            MetricBaseline.scan_config_id == scan_config_id,
            MetricBaseline.scope_type == scope_type,
            MetricBaseline.scope_ref == scope_ref,
            MetricBaseline.bucket >= evaluation_start,
            MetricBaseline.bucket < evaluation_end,
        )
    )
    rows = [
        {
            "id": uuid.uuid4(),
            "scan_config_id": scan_config_id,
            "scope_type": scope_type,
            "scope_ref": scope_ref,
            "bucket": baseline.bucket,
            "expected_count": baseline.expected_count,
            "effective_stddev": baseline.effective_stddev,
        }
        for baseline in baselines
        if evaluation_start <= baseline.bucket < evaluation_end
    ]
    if not rows:
        return
    updatable = ["expected_count", "effective_stddev"]
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        sqlite_stmt = sqlite_insert(MetricBaseline).values(rows)
        sqlite_stmt = sqlite_stmt.on_conflict_do_update(
            index_elements=["scan_config_id", "scope_type", "scope_ref", "bucket"],
            set_={col: getattr(sqlite_stmt.excluded, col) for col in updatable},
        )
        session.execute(sqlite_stmt)
    else:
        pg_stmt = pg_insert(MetricBaseline).values(rows)
        pg_stmt = pg_stmt.on_conflict_do_update(
            constraint="uq_metric_baseline_scope_bucket",
            set_={col: getattr(pg_stmt.excluded, col) for col in updatable},
        )
        session.execute(pg_stmt)


def _load_breakdown_scope_points(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    scope_type: str,
    scope_ref: str,
    breakdown_column: str,
    breakdown_value: str,
    is_other: bool,
    history_from: datetime,
    time_to: datetime,
) -> list[SeriesPoint]:
    query = (
        select(EventMetricBreakdown.bucket, sa_func.sum(EventMetricBreakdown.count))
        .where(
            EventMetricBreakdown.scan_config_id == scan_config_id,
            EventMetricBreakdown.breakdown_column == breakdown_column,
            EventMetricBreakdown.breakdown_value == breakdown_value,
            EventMetricBreakdown.is_other.is_(is_other),
            EventMetricBreakdown.bucket >= history_from,
            EventMetricBreakdown.bucket < time_to,
        )
        .group_by(EventMetricBreakdown.bucket)
        .order_by(EventMetricBreakdown.bucket)
    )

    if scope_type == SCOPE_PROJECT_TOTAL:
        query = query.where(
            EventMetricBreakdown.event_id.is_(None),
            EventMetricBreakdown.event_type_id.is_not(None),
        )
    elif scope_type == SCOPE_EVENT_TYPE:
        query = query.where(
            EventMetricBreakdown.event_id.is_(None),
            EventMetricBreakdown.event_type_id == uuid.UUID(scope_ref),
        )
    else:
        query = query.where(EventMetricBreakdown.event_id == uuid.UUID(scope_ref))

    rows = session.execute(query).all()
    return [SeriesPoint(bucket=to_utc(bucket), count=int(count)) for bucket, count in rows]


def _load_platform_ratio_points(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    scope_type: str,
    scope_ref: str,
    breakdown_column: str,
    breakdown_value: str,
    is_other: bool,
    history_from: datetime,
    time_to: datetime,
    covered_buckets: set[datetime] | None = None,
    total_points: list[SeriesPoint] | None = None,
) -> list[SeriesPoint]:
    breakdown_points = _load_breakdown_scope_points(
        session,
        scan_config_id=scan_config_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        breakdown_column=breakdown_column,
        breakdown_value=breakdown_value,
        is_other=is_other,
        history_from=history_from,
        time_to=time_to,
    )
    breakdown_by_bucket = {point.bucket: point.count for point in breakdown_points}
    # The scope TOTAL is the same series for every breakdown value of that scope,
    # so the caller may hand in a cached copy instead of re-reading ~500 rows once
    # per platform value.
    if total_points is None:
        total_points = _load_scope_points(
            session,
            scan_config_id=scan_config_id,
            scope_type=scope_type,
            scope_ref=scope_ref,
            history_from=history_from,
            time_to=time_to,
        )
    return [
        SeriesPoint(
            bucket=point.bucket,
            count=breakdown_by_bucket.get(point.bucket, 0) / point.count,
        )
        for point in total_points
        if point.count > 0 and (covered_buckets is None or point.bucket in covered_buckets)
    ]


def _replace_scope_breakdown_anomalies(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    scope_type: str,
    scope_ref: str,
    breakdown_column: str,
    breakdown_value: str,
    is_other: bool,
    evaluation_start: datetime,
    evaluation_end: datetime,
    event_id: uuid.UUID | None,
    event_type_id: uuid.UUID | None,
    anomalies: list[DetectedAnomaly],
    kind: MetricBreakdownAnomalyKind = MetricBreakdownAnomalyKind.volume,
    suppressed_ranges: Sequence[SuppressedRange] = (),
    hold_drops: bool = False,
    held: list[int] | None = None,
) -> int:
    delete_filters = [
        MetricBreakdownAnomaly.scan_config_id == scan_config_id,
        MetricBreakdownAnomaly.scope_type == scope_type,
        MetricBreakdownAnomaly.scope_ref == scope_ref,
        MetricBreakdownAnomaly.breakdown_column == breakdown_column,
        MetricBreakdownAnomaly.breakdown_value == breakdown_value,
        MetricBreakdownAnomaly.is_other.is_(is_other),
        MetricBreakdownAnomaly.kind == kind,
        MetricBreakdownAnomaly.bucket >= evaluation_start,
        MetricBreakdownAnomaly.bucket < evaluation_end,
    ]
    # Same exclusion, same reason as ``_replace_scope_anomalies``:
    # a count-shaped breakdown series goes through the identical outage collapse,
    # so a ``platform=ios`` slice that dies announces ONCE, downstream of its
    # anchor, and every later pass declines to re-announce it while the announced
    # row still sits inside this window. Clearing it here destroys the slice's
    # only marker with nothing to replace it.
    for suppressed in suppressed_ranges:
        delete_filters.append(
            sa_not(
                sa_and(
                    MetricBreakdownAnomaly.bucket >= suppressed.start,
                    MetricBreakdownAnomaly.bucket < suppressed.end,
                )
            )
        )
    anomalies, hold_filter = _apply_drop_hold(
        anomalies, MetricBreakdownAnomaly.direction, hold_drops=hold_drops, held=held
    )
    if hold_filter is not None:
        delete_filters.append(hold_filter)
    session.execute(delete(MetricBreakdownAnomaly).where(*delete_filters))

    rows: list[dict[str, object]] = []
    for anomaly in anomalies:
        anomalies_detected_total.labels(
            scope=f"{scope_type}_breakdown", direction=anomaly.direction
        ).inc()
        rows.append(
            {
                "id": uuid.uuid4(),
                "scan_config_id": scan_config_id,
                "scope_type": scope_type,
                "scope_ref": scope_ref,
                "event_id": event_id,
                "event_type_id": event_type_id,
                "bucket": anomaly.bucket,
                "breakdown_column": breakdown_column,
                "breakdown_value": breakdown_value,
                "is_other": is_other,
                "kind": kind,
                "actual_count": anomaly.actual_count,
                "expected_count": anomaly.expected_count,
                "stddev": anomaly.stddev,
                "z_score": anomaly.z_score,
                "direction": anomaly.direction,
                # C3 columns sourced from the DetectedAnomaly (C1); see
                # _replace_scope_anomalies.
                "effective_stddev": anomaly.effective_stddev,
                "detector_kind": anomaly.kind,
            }
        )

    # Idempotent insert — see _replace_scope_anomalies: concurrent runs over the
    # same window must not crash on uq_metric_breakdown_anomaly_scope_bucket_value.
    _updatable = [
        "event_id",
        "event_type_id",
        "actual_count",
        "expected_count",
        "stddev",
        "z_score",
        "direction",
        "effective_stddev",
        "detector_kind",
    ]
    if rows:
        if session.bind is not None and session.bind.dialect.name == "sqlite":
            sqlite_stmt = sqlite_insert(MetricBreakdownAnomaly).values(rows)
            sqlite_stmt = sqlite_stmt.on_conflict_do_update(
                index_elements=[
                    "scan_config_id",
                    "scope_type",
                    "scope_ref",
                    "breakdown_column",
                    "breakdown_value",
                    "is_other",
                    "bucket",
                    "kind",
                ],
                set_={col: getattr(sqlite_stmt.excluded, col) for col in _updatable},
            )
            session.execute(sqlite_stmt)
        else:
            pg_stmt = pg_insert(MetricBreakdownAnomaly).values(rows)
            pg_stmt = pg_stmt.on_conflict_do_update(
                constraint="uq_metric_breakdown_anomaly_scope_bucket_value",
                set_={col: getattr(pg_stmt.excluded, col) for col in _updatable},
            )
            session.execute(pg_stmt)

    return len(anomalies)


def _scope_max_counts(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    scope_type: str,
    history_from: datetime,
    time_to: datetime,
) -> dict[uuid.UUID, float]:
    """MAX(count) per scope over the detection history window, in one query.

    The prefilter for the silent-series early exit: a scope whose
    max count satisfies ``is_provably_silent`` against ``min_expected_count``
    cannot emit, so the caller skips loading its ~500-row history and the
    detector entirely. The filters mirror ``_load_scope_points`` exactly:
    event-type rows are the ``event_id IS NULL`` rollups, event rows are keyed
    by ``event_id``. Zero-fill only ever adds zeros, so this stored-row MAX is
    an upper bound on the max the detector itself would see.
    """
    metric_column = (
        EventMetric.event_type_id if scope_type == SCOPE_EVENT_TYPE else EventMetric.event_id
    )
    filters = [
        EventMetric.scan_config_id == scan_config_id,
        metric_column.is_not(None),
        EventMetric.bucket >= history_from,
        EventMetric.bucket < time_to,
    ]
    if scope_type == SCOPE_EVENT_TYPE:
        filters.append(EventMetric.event_id.is_(None))
    rows = session.execute(
        select(metric_column, sa_func.max(EventMetric.count))
        .where(*filters)
        .group_by(metric_column)
    ).all()
    return {scope_id: float(max_count) for scope_id, max_count in rows}


def _breakdown_scope_max_counts(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    scope_type: str,
    history_from: datetime,
    time_to: datetime,
) -> dict[tuple[uuid.UUID, str, str, bool], float]:
    """MAX(count) per breakdown series over the detection history window, in one query.

    The breakdown twin of ``_scope_max_counts``. ``detect_anomalies``
    already early-exits on a provably-silent count series, but only AFTER the
    caller has loaded that series' ~500-bucket history — one query per silent
    (scope, column, value) triple, on every run, on a project that may have
    hundreds of them. Event and event-type breakdown series carry exactly one
    stored row per bucket (``uq_event_metric_breakdown_config_event_bucket_value``
    / ``..._type_bucket_value``), so this stored-row MAX is precisely the max the
    detector sees once the grid is zero-filled — the skip is exact, not a
    heuristic. ``project_total`` is excluded on purpose: its series SUMS across
    event types, so no per-row MAX bounds it.
    """
    id_column = (
        EventMetricBreakdown.event_type_id
        if scope_type == SCOPE_EVENT_TYPE
        else EventMetricBreakdown.event_id
    )
    filters = [
        EventMetricBreakdown.scan_config_id == scan_config_id,
        id_column.is_not(None),
        EventMetricBreakdown.bucket >= history_from,
        EventMetricBreakdown.bucket < time_to,
    ]
    if scope_type == SCOPE_EVENT_TYPE:
        filters.append(EventMetricBreakdown.event_id.is_(None))
    rows = session.execute(
        select(
            id_column,
            EventMetricBreakdown.breakdown_column,
            EventMetricBreakdown.breakdown_value,
            EventMetricBreakdown.is_other,
            sa_func.max(EventMetricBreakdown.count),
        )
        .where(*filters)
        .group_by(
            id_column,
            EventMetricBreakdown.breakdown_column,
            EventMetricBreakdown.breakdown_value,
            EventMetricBreakdown.is_other,
        )
    ).all()
    return {
        (scope_id, column, value, bool(is_other)): float(max_count)
        for scope_id, column, value, is_other, max_count in rows
    }


def _collect_scope_ids(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    history_from: datetime,
    evaluation_start: datetime,
    evaluation_end: datetime,
    scope_type: str,
) -> set[uuid.UUID]:
    metric_column = (
        EventMetric.event_type_id if scope_type == SCOPE_EVENT_TYPE else EventMetric.event_id
    )
    anomaly_column = (
        MetricAnomaly.event_type_id if scope_type == SCOPE_EVENT_TYPE else MetricAnomaly.event_id
    )

    if scope_type == SCOPE_EVENT:
        ids = {
            value
            for value in session.execute(
                select(EventMetric.event_id)
                .join(Event, EventMetric.event_id == Event.id)
                .where(
                    EventMetric.scan_config_id == scan_config_id,
                    EventMetric.event_id.is_not(None),
                    EventMetric.bucket >= history_from,
                    EventMetric.bucket < evaluation_end,
                    Event.status != "archived",
                )
                .distinct()
            ).scalars()
            if value is not None
        }
        ids.update(
            value
            for value in session.execute(
                select(MetricAnomaly.event_id)
                .join(Event, MetricAnomaly.event_id == Event.id)
                .where(
                    MetricAnomaly.scan_config_id == scan_config_id,
                    MetricAnomaly.scope_type == scope_type,
                    MetricAnomaly.event_id.is_not(None),
                    MetricAnomaly.bucket >= evaluation_start,
                    MetricAnomaly.bucket < evaluation_end,
                    Event.status != "archived",
                )
                .distinct()
            ).scalars()
            if value is not None
        )
    else:
        ids = {
            value
            for value in session.execute(
                select(metric_column)
                .where(
                    EventMetric.scan_config_id == scan_config_id,
                    metric_column.is_not(None),
                    EventMetric.bucket >= history_from,
                    EventMetric.bucket < evaluation_end,
                )
                .distinct()
            ).scalars()
            if value is not None
        }
        ids.update(
            value
            for value in session.execute(
                select(anomaly_column)
                .where(
                    MetricAnomaly.scan_config_id == scan_config_id,
                    MetricAnomaly.scope_type == scope_type,
                    anomaly_column.is_not(None),
                    MetricAnomaly.bucket >= evaluation_start,
                    MetricAnomaly.bucket < evaluation_end,
                )
                .distinct()
            ).scalars()
            if value is not None
        )
    return ids


def _collect_breakdown_scope_keys(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    history_from: datetime,
    evaluation_start: datetime,
    evaluation_end: datetime,
    scope_type: str,
    app_version_column: str | None = None,
    kind: MetricBreakdownAnomalyKind = MetricBreakdownAnomalyKind.volume,
    breakdown_column: str | None = None,
) -> set[tuple[uuid.UUID | None, uuid.UUID | None, str, str, bool]]:
    """Every breakdown scope with history or a stored anomaly in the window.

    DISTINCT in SQL and narrowed in SQL (``breakdown_column`` keeps only that
    column, ``app_version_column`` drops that one), so a scan with a few thousand
    events no longer materialises one row per stored BUCKET — up to 534 per key
    — just to build a set.
    """
    metric_id_column = (
        EventMetricBreakdown.event_type_id
        if scope_type == SCOPE_EVENT_TYPE
        else EventMetricBreakdown.event_id
    )
    anomaly_id_column = (
        MetricBreakdownAnomaly.event_type_id
        if scope_type == SCOPE_EVENT_TYPE
        else MetricBreakdownAnomaly.event_id
    )

    metric_query = select(
        EventMetricBreakdown.event_id,
        EventMetricBreakdown.event_type_id,
        EventMetricBreakdown.breakdown_column,
        EventMetricBreakdown.breakdown_value,
        EventMetricBreakdown.is_other,
    ).where(
        EventMetricBreakdown.scan_config_id == scan_config_id,
        EventMetricBreakdown.bucket >= history_from,
        EventMetricBreakdown.bucket < evaluation_end,
    )
    anomaly_query = select(
        MetricBreakdownAnomaly.event_id,
        MetricBreakdownAnomaly.event_type_id,
        MetricBreakdownAnomaly.breakdown_column,
        MetricBreakdownAnomaly.breakdown_value,
        MetricBreakdownAnomaly.is_other,
    ).where(
        MetricBreakdownAnomaly.scan_config_id == scan_config_id,
        MetricBreakdownAnomaly.scope_type == scope_type,
        MetricBreakdownAnomaly.kind == kind,
        MetricBreakdownAnomaly.bucket >= evaluation_start,
        MetricBreakdownAnomaly.bucket < evaluation_end,
    )

    if breakdown_column is not None:
        metric_query = metric_query.where(EventMetricBreakdown.breakdown_column == breakdown_column)
        anomaly_query = anomaly_query.where(
            MetricBreakdownAnomaly.breakdown_column == breakdown_column
        )
    if app_version_column:
        # App-version series describe rollout adoption rather than a stable
        # cohort: each release naturally ramps up and then declines as the next
        # one ships, and running the generic per-breakdown detector on those
        # lifecycle curves creates noise. Dedicated release-regression detection
        # handles version correctness; every other breakdown column stays
        # monitored here.
        metric_query = metric_query.where(
            EventMetricBreakdown.breakdown_column != app_version_column
        )
        anomaly_query = anomaly_query.where(
            MetricBreakdownAnomaly.breakdown_column != app_version_column
        )

    if scope_type == SCOPE_PROJECT_TOTAL:
        metric_query = metric_query.where(
            EventMetricBreakdown.event_id.is_(None),
            EventMetricBreakdown.event_type_id.is_not(None),
        )
    elif scope_type == SCOPE_EVENT:
        metric_query = metric_query.join(Event, EventMetricBreakdown.event_id == Event.id).where(
            EventMetricBreakdown.event_id.is_not(None),
            Event.status != "archived",
        )
        anomaly_query = anomaly_query.join(
            Event,
            MetricBreakdownAnomaly.event_id == Event.id,
        ).where(
            MetricBreakdownAnomaly.event_id.is_not(None),
            Event.status != "archived",
        )
    else:
        metric_query = metric_query.where(metric_id_column.is_not(None))
        anomaly_query = anomaly_query.where(anomaly_id_column.is_not(None))

    keys: set[tuple[uuid.UUID | None, uuid.UUID | None, str, str, bool]] = set()
    for query in (metric_query.distinct(), anomaly_query.distinct()):
        for event_id, event_type_id, column, value, is_other in session.execute(query).all():
            if scope_type == SCOPE_PROJECT_TOTAL:
                keys.add((None, None, column, value, bool(is_other)))
            else:
                keys.add((event_id, event_type_id, column, value, bool(is_other)))
    return keys


def _metric_grid_population(grid: MetricGrid | None) -> ColumnExpressionArgument[bool]:
    """The ``MetricValue`` rows that ARE this metric's series.

    What :func:`_load_metric_value_points` sums, what
    :func:`_metric_source_config_ids` reads coverage back for, and what the
    series read plots. The rule lives in
    :func:`tripl.metric_grid.grid_population_filter`, shared with the read path.
    A ``None`` grid (the metric row vanished mid-run) takes the
    IS NULL branch and matches nothing, which is the safe answer.
    """
    return grid_population_filter(
        MetricValue.scan_config_id,
        interval=None if grid is None else grid.interval,
        scan_config_id=None if grid is None else grid.scan_config_id,
    )


def _load_metric_value_points(
    session: Session,
    *,
    metric_definition_id: uuid.UUID,
    history_from: datetime,
    time_to: datetime,
    grid: MetricGrid | None = None,
) -> list[SeriesPoint]:
    """Load a catalog metric's stored value series as ``SeriesPoint``s.

    Values are summed per bucket over the metric's grid population — see
    :func:`_metric_grid_population` for which configs that is and why — and kept
    as floats, because the detector is scale-aware, so sub-unit ratio/average
    movements survive instead of rounding toward 0.

    ``grid`` is optional only to save the resolving query for the detection loop,
    which has already resolved it; omitting it resolves the same grid here rather
    than widening the population, so no caller can accidentally score a series
    the series read would not draw.
    """
    if grid is None:
        grid = _metric_grid_by_id(session, metric_definition_id)
    rows = session.execute(
        select(MetricValue.bucket, sa_func.sum(MetricValue.value))
        .where(
            MetricValue.metric_definition_id == metric_definition_id,
            _metric_grid_population(grid),
            MetricValue.bucket >= history_from,
            MetricValue.bucket < time_to,
        )
        .group_by(MetricValue.bucket)
        .order_by(MetricValue.bucket)
    ).all()
    return [SeriesPoint(bucket=to_utc(bucket), count=float(value)) for bucket, value in rows]


def _metric_grid_by_id(session: Session, metric_definition_id: uuid.UUID) -> MetricGrid | None:
    """:func:`_resolve_metric_grid` for callers holding only the id."""
    return metric_grids(
        session.execute(metric_grid_stmt(MetricDefinition.id == metric_definition_id)).all()
    ).get(metric_definition_id)


def _resolve_metric_grid(session: Session, metric: MetricDefinition) -> MetricGrid | None:
    """Grid of a metric — the shared rule in :mod:`tripl.metric_grid`.

    ``sql`` / ``fact`` carry their own ``interval``;
    ``event_composition`` leaves it NULL and inherits the grid of the
    most-recent value's ``scan_config_id``.

    The whole entry is returned rather than just the interval because the
    interval SELECTS the series (:func:`_metric_grid_population`) and
    :func:`_metric_covered_buckets` also has to know whether the RUNNING scan's
    grid is one of the metric's grids, which is an (interval, config) pair. The
    config id alone does NOT decide whose coverage describes the series: that is
    the union over every source config the values were summed from, read
    separately.
    """
    return _metric_grid_by_id(session, metric.id)


def _metric_source_config_ids(
    session: Session,
    *,
    metric_definition_id: uuid.UUID,
    grid: MetricGrid | None,
    history_from: datetime,
    time_to: datetime,
) -> list[uuid.UUID]:
    """Every scan config that contributed a value to the series being scored.

    The population :func:`_load_metric_value_points` SUMS over — the same
    ``grid``, the same predicate, the same window and the same metric, so the two
    cannot describe different things. Ordered so the coverage union below is
    byte-stable run to run.
    """
    rows = session.execute(
        select(MetricValue.scan_config_id)
        .where(
            MetricValue.metric_definition_id == metric_definition_id,
            MetricValue.scan_config_id.is_not(None),
            _metric_grid_population(grid),
            MetricValue.bucket >= history_from,
            MetricValue.bucket < time_to,
        )
        .distinct()
    ).scalars()
    return sorted(row for row in rows if row is not None)


def _metric_covered_buckets(
    session: Session,
    config: ScanConfig,
    *,
    metric: MetricDefinition,
    grid: MetricGrid,
    delta: timedelta,
    history_from: datetime,
    evaluation_end: datetime,
    scan_covered_buckets: set[datetime] | None,
    memo: dict[tuple[uuid.UUID, timedelta], set[datetime]],
) -> set[datetime] | None:
    """Scan-job coverage for ONE catalog metric, on THAT metric's own grid.

    Only a count-shaped series ever consults it (``expand_series`` runs behind
    ``fill_gaps``), and the right answer depends on where the values came from:

    * ``fact`` / ``sql`` metrics collect on their own ``interval``, on their own
      schedule, and store ``scan_config_id = NULL``. No scan job ever recorded a
      window for them, so no scan's coverage describes them at all; ``None``
      keeps the documented unconditional zero-fill.
    * an ``event_composition`` metric gets the UNION of ITS GRID POPULATION's
      configs' coverage, each re-enumerated on the metric's own delta. The source
      reading THIS scan on THIS scan's grid contributes the running scan's set
      verbatim: it is already on the right delta, and it uniquely carries the
      window this run just wrote. Every other source is read from its own
      completed jobs and stored buckets.

    The union — rather than the newest value's single ``scan_config_id`` — is
    what keeps coverage describing the SAME population the series is summed
    from. ``_load_metric_value_points`` sums across every config on the metric's
    grid interval (:func:`_metric_grid_population`), because one event type can
    legitimately be collected by two live scans (``EventMetric`` is keyed on
    (scan_config_id, event_id, bucket) and ``_collect_event_composition`` writes
    one ``MetricValue`` row per source grid). Masking that summed series with ONE
    source's coverage drops every bucket only the other source contributed —
    ``expand_series`` EXCLUDES an uncovered bucket rather than zero-filling it,
    even when a real value is sitting there — and which source that was is an
    arbitrary ``ORDER BY bucket DESC`` tie-break between two equally-current
    configs (:func:`tripl.metric_grid.metric_grid_stmt`), so the truncation could
    also flap between runs. A union has no tie to break.

    ``grid`` bounds the union to the same configs the values came from, so a
    RETIRED grid on another interval — whose rows are no longer part of the
    series — cannot vouch for buckets of it either.

    ``None`` still means "no coverage gating, zero-fill unconditionally", and a
    source whose set is unknown forces it: the running scan contributing a
    ``None`` set would otherwise let the other sources' coverage silently
    exclude buckets nobody vouched against.

    ``memo`` keys on (source config, delta), which is enough: ``history_from``
    is a pure function of the delta and the run's evaluation window, so two
    metrics sharing both share a set.
    """
    if metric.kind != MetricKind.event_composition:
        return None
    source_ids = _metric_source_config_ids(
        session,
        metric_definition_id=metric.id,
        grid=grid,
        history_from=history_from,
        time_to=evaluation_end,
    )
    if not source_ids:
        # An event_composition metric with no stored value in this window: no
        # source scan is known, so there is no coverage to apply.
        return None
    covered: set[datetime] = set()
    for source_id in source_ids:
        if source_id == config.id and grid.interval == config.interval:
            if scan_covered_buckets is None:
                return None
            covered |= scan_covered_buckets
            continue
        key = (source_id, delta)
        cached = memo.get(key)
        if cached is None:
            cached = covered_buckets_from_scan_jobs(
                session,
                scan_config_id=source_id,
                delta=delta,
                # ``ScanJob.created_at`` is only an approximation of the window
                # the job recorded, so the floor needs the same day of slack the
                # running scan's horizon gets (``coverage_history_start``).
                # Without it a job that sat queued across the horizon is dropped
                # from the read and every genuinely-zero bucket it covered leaves
                # the baseline instead of being zero-filled.
                history_from=history_from - COVERAGE_HORIZON_SLACK,
                # This run wrote nothing for that config, so it has no current
                # window to vouch for; the stored-bucket read is bounded by the
                # metric's own evaluation end instead.
                presence_before=evaluation_end,
            )
            memo[key] = cached
        covered |= cached
    return covered


def _project_metric_scope_refs(session: Session, project_id: uuid.UUID) -> list[str]:
    return [
        str(metric_id)
        for metric_id in session.execute(
            select(MetricDefinition.id).where(MetricDefinition.project_id == project_id)
        ).scalars()
    ]


def _purge_project_metric_anomalies(
    session: Session,
    config: ScanConfig,
) -> None:
    """Delete EVERY ``metric``-scope anomaly for THIS project's metrics.

    Scoped to the project's metric ids so it never touches another project's
    metric-scope rows (which share the global ``scan_config_id IS NULL`` space).

    The whole-history wipe belongs to the MASTER switch and nothing else: with
    ``anomaly_detection_enabled`` off the same branch already drops every
    config-scoped row this scan owns, so "stop detecting for this project" takes
    the recorded markers with it. Unticking ONE scope box is a different act and
    must not reuse this function — that is :func:`_purge_disabled_metric_scope`,
    which clears only the window an enabled pass would have rewritten.
    """
    scope_refs = _project_metric_scope_refs(session, config.project_id)
    if not scope_refs:
        return
    session.execute(
        delete(MetricAnomaly).where(
            MetricAnomaly.scope_type == SCOPE_METRIC,
            MetricAnomaly.scope_ref.in_(scope_refs),
        )
    )


def _purge_disabled_metric_scope(
    session: Session,
    config: ScanConfig,
    *,
    evaluation_start: datetime,
    evaluation_end: datetime,
) -> None:
    """Clear the window a disabled ``metric`` scope would have rewritten.

    The invariant every scope in :func:`_recalculate_metric_anomalies` obeys:
    switching a scope OFF deletes precisely the window the enabled pass would
    have replaced and nothing older, so anomalies outside it stay on the chart
    as history — the promise the per-metric ``anomaly_detection_enabled`` toggle
    already makes. The three event scopes spell that as
    ``[evaluation_start, evaluation_end)`` because they are scored on the SCAN's
    grid.

    A catalog metric is not. It is scored from ``min(evaluation_start,
    evaluation_end - delta * ANOMALY_TRAILING_REEVAL_BUCKETS)`` on its OWN grid
    (see :func:`_recalculate_project_metric_anomalies`), so the scan window is
    the wrong bound: a daily metric's in-window rows sit far behind a 1h
    config's 30-hour sweep and bounding by that sweep would strand them as
    markers no later run re-evaluates. Take the enabled side's per-grid start
    instead, one DELETE per distinct start — the interval vocabulary is small,
    so a project with hundreds of metrics still issues a handful of statements.

    Grids resolve in ONE query rather than per metric, and the POPULATION is the
    enabled side's population — ``monitored_metric_criteria()`` on top of the
    project, exactly as :func:`_recalculate_project_metric_anomalies` selects,
    plus the same skip for a metric with no resolvable grid. A metric that has
    left monitoring (archived, or its own **Anomaly detection** switch off) is
    never scored by the enabled pass and never rewritten by it, so this must not
    delete its rows either; ``tripl.metric_monitoring`` states that promise, and
    without the predicate one untick of the project-level **Metrics** box would
    erase up to ``ANOMALY_TRAILING_REEVAL_BUCKETS`` grid intervals of history no
    later run can re-derive.
    """
    grids = metric_grids(
        session.execute(
            metric_grid_stmt(
                MetricDefinition.project_id == config.project_id,
                *monitored_metric_criteria(),
            )
        ).all()
    )
    refs_by_start: dict[datetime, list[str]] = {}
    for metric_id, grid in grids.items():
        if grid.interval is None:
            continue
        delta = get_interval(grid.interval).delta
        start = min(evaluation_start, evaluation_end - delta * ANOMALY_TRAILING_REEVAL_BUCKETS)
        refs_by_start.setdefault(start, []).append(str(metric_id))
    for start, scope_refs in refs_by_start.items():
        session.execute(
            delete(MetricAnomaly).where(
                # The enabled branch's own predicate (``_replace_scope_anomalies``
                # with ``scan_config_id=None``) minus the suppressed-range
                # carve-outs. Metric-scope rows always carry NULL here, so the
                # first clause is a no-op today; it keeps the two sides literally
                # comparable.
                MetricAnomaly.scan_config_id.is_(None),
                MetricAnomaly.scope_type == SCOPE_METRIC,
                MetricAnomaly.scope_ref.in_(scope_refs),
                MetricAnomaly.bucket >= start,
                MetricAnomaly.bucket < evaluation_end,
            )
        )


def _recalculate_project_metric_anomalies(
    session: Session,
    config: ScanConfig,
    *,
    settings: AnomalyDetectionSettings,
    overrides: ScopeOverrides | None = None,
    evaluation_start: datetime,
    evaluation_end: datetime,
    scan_covered_buckets: set[datetime] | None = None,
    settling_delay: timedelta = NO_INGESTION_SETTLING,
) -> int:
    """Detect anomalies over the project's MONITORED catalog metric series.

    Monitored is ``active`` AND ``anomaly_detection_enabled``
    (``tripl.metric_monitoring``) — the same predicate every consumer now applies,
    so alert candidacy and the four display surfaces work on exactly the
    population this pass scores.

    Metric anomalies are project-global: stored with ``scope_type='metric'``,
    ``scope_ref=str(metric_definition_id)`` and a NULL ``scan_config_id``.
    Count-shaped metrics keep the standard zero-fill + ``min_expected_count``
    behavior; fractional metrics (ratios/averages/sql) drop the zero-fill and
    swap the volume gate for a floor on |expected|, so sparse, sub-unit and
    signed series neither produce false anomalies nor get rejected for sitting
    below zero.

    ``evaluation_start`` arrives on the SCAN CONFIG's grid; each metric widens it
    onto its own grid (see ``ANOMALY_TRAILING_REEVAL_BUCKETS``). Widening only —
    a replay hands in a window wider than any metric's trailing sweep and keeps
    it, so a replayed range is still re-scored end to end.

    ``scan_covered_buckets`` is the RUNNING scan's coverage, enumerated on the
    RUNNING scan's grid from that config's own jobs and stored buckets. It is
    contributed only for a metric one of whose source grids IS that grid; every
    other source is read on the metric's own grid and horizon, and the metric
    gets the UNION (:func:`_metric_covered_buckets`), because the same set
    applied to a different grid silently decimates the series it is scored from
    and a single source's set does not describe a series summed over several.

    Both the window and ``scan_covered_buckets`` are stamped onto the aware-UTC
    comparison convention on entry; this pass is reachable directly, not only
    through :func:`_recalculate_metric_anomalies`.
    """
    evaluation_start, evaluation_end = _canonical_window(evaluation_start, evaluation_end)
    scan_covered_buckets = _canonical_covered(scan_covered_buckets)
    metrics = list(
        session.execute(
            select(MetricDefinition).where(
                MetricDefinition.project_id == config.project_id,
                *monitored_metric_criteria(),
            )
        ).scalars()
    )
    detected = 0
    # Coverage recomputed for a source config that is not the running scan,
    # memoized so a project whose metrics share a source pays one pair of
    # queries rather than one per metric.
    source_coverage: dict[tuple[uuid.UUID, timedelta], set[datetime]] = {}
    # Every monitored metric's grid in ONE window-function query — the batch
    # ``metric_grid_stmt`` exists for — rather than one per metric.
    # Same population as ``metrics`` above, so every metric
    # finds its entry; a metric that vanished between the two reads gets None,
    # exactly what the per-metric lookup answered.
    grids = metric_grids(
        session.execute(
            metric_grid_stmt(
                MetricDefinition.project_id == config.project_id,
                *monitored_metric_criteria(),
            )
        ).all()
    )
    for metric in metrics:
        grid = grids.get(metric.id)
        if grid is None or grid.interval is None:
            continue
        interval_spec = get_interval(grid.interval)
        count_shaped = is_count_shaped(metric)
        # The scope override lands FIRST, so a fractional metric still drops the
        # count gate afterwards: ratcheting a ratio's min_expected_count would
        # re-introduce exactly the volume gate tripl-68bc removed for it. Its
        # sigma ratchet still applies.
        scoped = _scope_settings(settings, overrides or {}, SCOPE_METRIC, str(metric.id))
        metric_settings = (
            scoped
            if count_shaped
            else replace(scoped, min_expected_count=_FRACTIONAL_MIN_EXPECTED_COUNT)
        )
        metric_evaluation_start = min(
            evaluation_start,
            evaluation_end - interval_spec.delta * ANOMALY_TRAILING_REEVAL_BUCKETS,
        )
        history_from = metric_evaluation_start - interval_spec.delta * required_history_buckets(
            interval_spec.delta, settings
        )
        points = _load_metric_value_points(
            session,
            metric_definition_id=metric.id,
            # The grid this loop already resolved, so the population predicate
            # costs no second query here.
            grid=grid,
            history_from=history_from,
            time_to=evaluation_end,
        )
        result = detect_anomalies(
            points,
            interval=interval_spec.delta,
            evaluation_start=metric_evaluation_start,
            evaluation_end=evaluation_end,
            settings=metric_settings,
            fill_gaps=count_shaped,
            covered_buckets=_metric_covered_buckets(
                session,
                config,
                metric=metric,
                grid=grid,
                delta=interval_spec.delta,
                history_from=history_from,
                evaluation_end=evaluation_end,
                scan_covered_buckets=scan_covered_buckets,
                memo=source_coverage,
            ),
            # Per-metric grid: a 1d ratio needs a whole bucket withheld for
            # the same allowance that withholds two hourly ones.
            settling_buckets=settling_buckets_for(interval_spec.delta, settling_delay),
        )
        detected += _replace_scope_anomalies(
            session,
            scan_config_id=None,
            scope_type=SCOPE_METRIC,
            scope_ref=str(metric.id),
            evaluation_start=metric_evaluation_start,
            evaluation_end=evaluation_end,
            event_id=None,
            event_type_id=None,
            anomalies=result.anomalies,
            suppressed_ranges=result.suppressed_ranges,
        )
    return detected


def _age_out_config_anomalies(
    session: Session,
    model: type[MetricAnomaly] | type[MetricBreakdownAnomaly] | type[MetricBaseline],
    scan_config_id: uuid.UUID,
) -> None:
    """Trim config-scoped anomaly markers older than the retention horizon.

    Conservative: only touches rows keyed to THIS scan_config (never the shared
    ``metric``-scope NULL-config rows) and only those far past the horizon, so a
    trailing re-eval never resurrects long-dead dots as active markers.
    """
    horizon = datetime.now(UTC) - timedelta(days=ANOMALY_RETENTION_DAYS)
    session.execute(
        delete(model).where(model.scan_config_id == scan_config_id, model.bucket < horizon)
    )


# Column defaults on ``ProjectAnomalySettings`` fire at INSERT, so a transient
# row reads them back as ``None`` and cannot be used to build settings for a
# project that has no row yet. Only the baseline width feeds history depth, so
# mirror that one default here. A project with no settings row detects nothing
# anyway — both recalculation entrypoints return 0 for it — so the horizon it
# produces is never consulted; it only has to be arithmetic, not ``None``.
_FALLBACK_COVERAGE_SETTINGS = AnomalyDetectionSettings(
    baseline_window_buckets=14,
    min_history_buckets=0,
    sigma_threshold=0.0,
    min_expected_count=0.0,
)


def coverage_history_start(
    session: Session,
    config: ScanConfig,
    *,
    evaluation_start: datetime,
    evaluation_end: datetime,
) -> datetime:
    """Oldest bucket this run can consult THIS scan's coverage set for.

    ``covered_buckets_from_scan_jobs`` reads a config's completed-job windows and
    its stored buckets; unbounded, that is the config's entire lifetime on every
    scheduled collection. This is the floor that makes the read a constant
    instead, derived from the detector rather than from a fixed number of days so
    the two cannot drift apart.

    The invariant: this scan's set is consulted only ON THIS SCAN'S GRID. The
    project-total / event-type / event passes and the breakdown + parity passes
    all load from ``evaluation_start - delta * required_history_buckets`` on
    ``config.interval``; a catalog metric inherits the set only when its resolved
    grid IS this scan's grid (same source config, same interval code), in which
    case its own widened window works out to the same depth, and
    ``_metric_covered_buckets`` resolves coverage separately — on that metric's
    grid and horizon — for every other metric. So a coarser catalog metric
    cannot outrun this bound.

    Reading SHALLOWER than this would be silent: ``expand_series`` EXCLUDES an
    uncovered bucket rather than zero-filling it, so a genuinely-zero bucket
    below the horizon would quietly leave every baseline. Hence the derivation,
    and hence ``COVERAGE_HORIZON_SLACK`` on top of it.

    Returned on the aware-UTC comparison convention, because it is fed straight
    back in as ``covered_buckets_from_scan_jobs(history_from=...)``.
    """
    evaluation_start, evaluation_end = _canonical_window(evaluation_start, evaluation_end)
    if not config.interval:
        return evaluation_start - COVERAGE_HORIZON_SLACK
    try:
        interval_spec = get_interval(config.interval)
    except ValueError:
        return evaluation_start - COVERAGE_HORIZON_SLACK

    project_settings = _get_project_anomaly_settings(session, config.project_id)
    settings = (
        _build_anomaly_settings(project_settings)
        if project_settings is not None
        else _FALLBACK_COVERAGE_SETTINGS
    )
    # Mirrors the two windows the passes compute: the trailing re-eval widening
    # in ``_recalculate_project_metric_anomalies`` and the history depth every
    # pass loads. ``evaluation_start`` already carries the widening for a
    # scheduled run, but a caller handing in its own window may not.
    trailing_start = min(
        evaluation_start,
        evaluation_end - interval_spec.delta * ANOMALY_TRAILING_REEVAL_BUCKETS,
    )
    history_from = trailing_start - interval_spec.delta * required_history_buckets(
        interval_spec.delta, settings
    )
    return history_from - COVERAGE_HORIZON_SLACK


def _recalculate_metric_anomalies(
    session: Session,
    config: ScanConfig,
    *,
    evaluation_start: datetime,
    evaluation_end: datetime,
    covered_buckets: set[datetime] | None = None,
    settling_delay: timedelta = NO_INGESTION_SETTLING,
    hold_drops: bool = False,
    held: list[int] | None = None,
) -> int:
    # Entry boundary: a replay, a conformance harness or a test may hand in a
    # naive window and a hand-built naive coverage set. Stamp both once here.
    evaluation_start, evaluation_end = _canonical_window(evaluation_start, evaluation_end)
    covered_buckets = _canonical_covered(covered_buckets)
    project_settings = _get_project_anomaly_settings(session, config.project_id)
    if project_settings is None or not project_settings.anomaly_detection_enabled:
        session.execute(delete(MetricAnomaly).where(MetricAnomaly.scan_config_id == config.id))
        session.execute(delete(MetricBaseline).where(MetricBaseline.scan_config_id == config.id))
        _purge_project_metric_anomalies(session, config)
        session.flush()
        return 0

    if not config.interval:
        return 0

    _age_out_config_anomalies(session, MetricAnomaly, config.id)
    _age_out_config_anomalies(session, MetricBaseline, config.id)

    interval_spec = get_interval(config.interval)
    settings = _build_anomaly_settings(project_settings)
    overrides = _load_scope_overrides(
        session,
        project_id=config.project_id,
        scan_config_id=config.id,
    )
    # Buckets at the head of the window that the warehouse may still be filling.
    # They stay in the loaded series (so baselines are complete) but no anomaly
    # is emitted for them until a later scan re-evaluates them.
    settling_buckets = settling_buckets_for(interval_spec.delta, settling_delay)
    # History depth is driven by the baseline/min-history buckets, which no
    # override touches, so one window serves every scope.
    history_from = evaluation_start - interval_spec.delta * required_history_buckets(
        interval_spec.delta, settings
    )
    anomalies_detected = 0

    if project_settings.detect_project_total:
        total_settings = _scope_settings(settings, overrides, SCOPE_PROJECT_TOTAL, str(config.id))
        points = _load_scope_points(
            session,
            scan_config_id=config.id,
            scope_type=SCOPE_PROJECT_TOTAL,
            scope_ref=str(config.id),
            history_from=history_from,
            time_to=evaluation_end,
        )
        total_result = detect_anomalies(
            points,
            interval=interval_spec.delta,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            settings=total_settings,
            covered_buckets=covered_buckets,
            settling_buckets=settling_buckets,
        )
        anomalies_detected += _replace_scope_anomalies(
            session,
            scan_config_id=config.id,
            scope_type=SCOPE_PROJECT_TOTAL,
            scope_ref=str(config.id),
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            event_id=None,
            event_type_id=None,
            anomalies=total_result.anomalies,
            hold_drops=hold_drops,
            held=held,
            suppressed_ranges=total_result.suppressed_ranges,
            baselines=total_result.baselines,
        )
    else:
        session.execute(
            delete(MetricAnomaly).where(
                MetricAnomaly.scan_config_id == config.id,
                MetricAnomaly.scope_type == SCOPE_PROJECT_TOTAL,
                MetricAnomaly.bucket >= evaluation_start,
                MetricAnomaly.bucket < evaluation_end,
            )
        )
        session.execute(
            delete(MetricBaseline).where(
                MetricBaseline.scan_config_id == config.id,
                MetricBaseline.scope_type == SCOPE_PROJECT_TOTAL,
                MetricBaseline.bucket >= evaluation_start,
                MetricBaseline.bucket < evaluation_end,
            )
        )

    if project_settings.detect_event_types:
        type_max_counts = _scope_max_counts(
            session,
            scan_config_id=config.id,
            scope_type=SCOPE_EVENT_TYPE,
            history_from=history_from,
            time_to=evaluation_end,
        )
        for event_type_id in _collect_scope_ids(
            session,
            scan_config_id=config.id,
            history_from=history_from,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            scope_type=SCOPE_EVENT_TYPE,
        ):
            scope_ref = str(event_type_id)
            scope_settings = _scope_settings(settings, overrides, SCOPE_EVENT_TYPE, scope_ref)
            # Provably silent: the detector would early-exit on
            # this series anyway, so skip loading its history — but still run
            # the replace with no anomalies so stale window rows age out.
            if is_provably_silent(
                type_max_counts.get(event_type_id, 0.0), scope_settings.min_expected_count
            ):
                anomalies_detected += _replace_scope_anomalies(
                    session,
                    scan_config_id=config.id,
                    scope_type=SCOPE_EVENT_TYPE,
                    scope_ref=scope_ref,
                    evaluation_start=evaluation_start,
                    evaluation_end=evaluation_end,
                    event_id=None,
                    event_type_id=event_type_id,
                    anomalies=[],
                    hold_drops=hold_drops,
                    held=held,
                    baselines=(),
                )
                continue
            points = _load_scope_points(
                session,
                scan_config_id=config.id,
                scope_type=SCOPE_EVENT_TYPE,
                scope_ref=scope_ref,
                history_from=history_from,
                time_to=evaluation_end,
            )
            type_result = detect_anomalies(
                points,
                interval=interval_spec.delta,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                settings=scope_settings,
                covered_buckets=covered_buckets,
                settling_buckets=settling_buckets,
            )
            anomalies_detected += _replace_scope_anomalies(
                session,
                scan_config_id=config.id,
                scope_type=SCOPE_EVENT_TYPE,
                scope_ref=scope_ref,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                event_id=None,
                event_type_id=event_type_id,
                anomalies=type_result.anomalies,
                hold_drops=hold_drops,
                held=held,
                suppressed_ranges=type_result.suppressed_ranges,
                baselines=type_result.baselines,
            )
    else:
        session.execute(
            delete(MetricAnomaly).where(
                MetricAnomaly.scan_config_id == config.id,
                MetricAnomaly.scope_type == SCOPE_EVENT_TYPE,
                MetricAnomaly.bucket >= evaluation_start,
                MetricAnomaly.bucket < evaluation_end,
            )
        )
        session.execute(
            delete(MetricBaseline).where(
                MetricBaseline.scan_config_id == config.id,
                MetricBaseline.scope_type == SCOPE_EVENT_TYPE,
                MetricBaseline.bucket >= evaluation_start,
                MetricBaseline.bucket < evaluation_end,
            )
        )

    if project_settings.detect_events:
        event_max_counts = _scope_max_counts(
            session,
            scan_config_id=config.id,
            scope_type=SCOPE_EVENT,
            history_from=history_from,
            time_to=evaluation_end,
        )
        for event_id in _collect_scope_ids(
            session,
            scan_config_id=config.id,
            history_from=history_from,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            scope_type=SCOPE_EVENT,
        ):
            scope_ref = str(event_id)
            scope_settings = _scope_settings(settings, overrides, SCOPE_EVENT, scope_ref)
            # Provably silent: see the event-type loop above.
            if is_provably_silent(
                event_max_counts.get(event_id, 0.0), scope_settings.min_expected_count
            ):
                anomalies_detected += _replace_scope_anomalies(
                    session,
                    scan_config_id=config.id,
                    scope_type=SCOPE_EVENT,
                    scope_ref=scope_ref,
                    evaluation_start=evaluation_start,
                    evaluation_end=evaluation_end,
                    event_id=event_id,
                    event_type_id=None,
                    anomalies=[],
                    hold_drops=hold_drops,
                    held=held,
                    baselines=(),
                )
                continue
            points = _load_scope_points(
                session,
                scan_config_id=config.id,
                scope_type=SCOPE_EVENT,
                scope_ref=scope_ref,
                history_from=history_from,
                time_to=evaluation_end,
            )
            event_result = detect_anomalies(
                points,
                interval=interval_spec.delta,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                settings=scope_settings,
                covered_buckets=covered_buckets,
                settling_buckets=settling_buckets,
            )
            anomalies_detected += _replace_scope_anomalies(
                session,
                scan_config_id=config.id,
                scope_type=SCOPE_EVENT,
                scope_ref=scope_ref,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                event_id=event_id,
                event_type_id=None,
                anomalies=event_result.anomalies,
                hold_drops=hold_drops,
                held=held,
                suppressed_ranges=event_result.suppressed_ranges,
                baselines=event_result.baselines,
            )
    else:
        session.execute(
            delete(MetricAnomaly).where(
                MetricAnomaly.scan_config_id == config.id,
                MetricAnomaly.scope_type == SCOPE_EVENT,
                MetricAnomaly.bucket >= evaluation_start,
                MetricAnomaly.bucket < evaluation_end,
            )
        )
        session.execute(
            delete(MetricBaseline).where(
                MetricBaseline.scan_config_id == config.id,
                MetricBaseline.scope_type == SCOPE_EVENT,
                MetricBaseline.bucket >= evaluation_start,
                MetricBaseline.bucket < evaluation_end,
            )
        )

    if project_settings.detect_metrics:
        anomalies_detected += _recalculate_project_metric_anomalies(
            session,
            config,
            settings=settings,
            overrides=overrides,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            scan_covered_buckets=covered_buckets,
            settling_delay=settling_delay,
        )
    else:
        _purge_disabled_metric_scope(
            session,
            config,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
        )

    session.flush()
    # Rows just written inside a planned event's window are tagged before the
    # dispatch pass reads them, so a planned spike never reaches an alert (F18).
    retag_planned_anomalies(session, config.project_id)
    return anomalies_detected


# A platform-parity ratio needs a genuine two-platform history before the
# comparison says anything. Five non-zero buckets is deliberately low: it clears
# platform-exclusive events (whose ratio is zero almost everywhere) without
# muting a real platform that merely has quiet hours.
_PARITY_MIN_NONZERO_BUCKETS = 5


def _recalculate_platform_parity_anomalies(
    session: Session,
    config: ScanConfig,
    *,
    project_settings: ProjectAnomalySettings,
    settings: AnomalyDetectionSettings,
    evaluation_start: datetime,
    evaluation_end: datetime,
    covered_buckets: set[datetime] | None = None,
    settling_delay: timedelta = NO_INGESTION_SETTLING,
) -> int:
    # Entry boundary; see ``_recalculate_metric_anomalies``.
    evaluation_start, evaluation_end = _canonical_window(evaluation_start, evaluation_end)
    covered_buckets = _canonical_covered(covered_buckets)
    platform_column = config.platform_column
    if not platform_column or not config.interval:
        session.execute(
            delete(MetricBreakdownAnomaly).where(
                MetricBreakdownAnomaly.scan_config_id == config.id,
                MetricBreakdownAnomaly.kind == MetricBreakdownAnomalyKind.parity,
            )
        )
        return 0

    session.execute(
        delete(MetricBreakdownAnomaly).where(
            MetricBreakdownAnomaly.scan_config_id == config.id,
            MetricBreakdownAnomaly.kind == MetricBreakdownAnomalyKind.parity,
            MetricBreakdownAnomaly.breakdown_column != platform_column,
        )
    )

    interval_spec = get_interval(config.interval)
    ratio_settings = replace(settings, min_expected_count=0)
    settling_buckets = settling_buckets_for(interval_spec.delta, settling_delay)
    history_from = evaluation_start - interval_spec.delta * required_history_buckets(
        interval_spec.delta, ratio_settings
    )
    detected = 0

    scope_settings = (
        (SCOPE_PROJECT_TOTAL, project_settings.detect_project_total),
        (SCOPE_EVENT_TYPE, project_settings.detect_event_types),
        (SCOPE_EVENT, project_settings.detect_events),
    )
    for scope_type, enabled in scope_settings:
        if not enabled:
            session.execute(
                delete(MetricBreakdownAnomaly).where(
                    MetricBreakdownAnomaly.scan_config_id == config.id,
                    MetricBreakdownAnomaly.scope_type == scope_type,
                    MetricBreakdownAnomaly.kind == MetricBreakdownAnomalyKind.parity,
                    MetricBreakdownAnomaly.bucket >= evaluation_start,
                    MetricBreakdownAnomaly.bucket < evaluation_end,
                )
            )
            continue

        keys = _collect_breakdown_scope_keys(
            session,
            scan_config_id=config.id,
            history_from=history_from,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            scope_type=scope_type,
            kind=MetricBreakdownAnomalyKind.parity,
            breakdown_column=platform_column,
        )
        # One scope's total series is shared by all of its platform values.
        totals_by_scope: dict[str, list[SeriesPoint]] = {}
        for event_id, event_type_id, column, value, is_other in keys:
            if column != platform_column:
                continue
            if scope_type == SCOPE_PROJECT_TOTAL:
                scope_ref = str(config.id)
                stored_event_id = None
                stored_event_type_id = None
            elif scope_type == SCOPE_EVENT_TYPE:
                if event_type_id is None:
                    continue
                scope_ref = str(event_type_id)
                stored_event_id = None
                stored_event_type_id = event_type_id
            else:
                if event_id is None:
                    continue
                scope_ref = str(event_id)
                stored_event_id = event_id
                stored_event_type_id = None

            total_points = totals_by_scope.get(scope_ref)
            if total_points is None:
                total_points = _load_scope_points(
                    session,
                    scan_config_id=config.id,
                    scope_type=scope_type,
                    scope_ref=scope_ref,
                    history_from=history_from,
                    time_to=evaluation_end,
                )
                totals_by_scope[scope_ref] = total_points
            points = _load_platform_ratio_points(
                session,
                scan_config_id=config.id,
                scope_type=scope_type,
                scope_ref=scope_ref,
                breakdown_column=platform_column,
                breakdown_value=value,
                is_other=is_other,
                history_from=history_from,
                time_to=evaluation_end,
                covered_buckets=covered_buckets,
                total_points=total_points,
            )
            # A parity ratio that is zero in most buckets is a platform-EXCLUSIVE
            # event, not a platform imbalance. Scoring it flagged every bucket
            # where the other platform emitted even once: the ratio path runs at
            # min_expected_count=0 by design, so no volume gate stops it, and a
            # near-zero baseline made the deviation look enormous.
            # Require a real two-platform history before the
            # comparison means anything.
            if sum(1 for point in points if point.count) < _PARITY_MIN_NONZERO_BUCKETS:
                continue
            detected += _replace_scope_breakdown_anomalies(
                session,
                scan_config_id=config.id,
                scope_type=scope_type,
                scope_ref=scope_ref,
                breakdown_column=platform_column,
                breakdown_value=value,
                is_other=is_other,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                event_id=stored_event_id,
                event_type_id=stored_event_type_id,
                # No ``suppressed_ranges`` to thread: a parity ratio is
                # fractional, so ``fill_gaps=False`` and ``detect_anomalies``
                # returns before ``_collapse_outage_runs`` ever runs. This lane
                # can only ever hand back an empty tuple.
                anomalies=detect_anomalies(
                    points,
                    interval=interval_spec.delta,
                    evaluation_start=evaluation_start,
                    evaluation_end=evaluation_end,
                    settings=ratio_settings,
                    fill_gaps=False,
                    settling_buckets=settling_buckets,
                ).anomalies,
                kind=MetricBreakdownAnomalyKind.parity,
            )
    return detected


def _recalculate_metric_breakdown_anomalies(
    session: Session,
    config: ScanConfig,
    *,
    evaluation_start: datetime,
    evaluation_end: datetime,
    covered_buckets: set[datetime] | None = None,
    settling_delay: timedelta = NO_INGESTION_SETTLING,
    hold_drops: bool = False,
    held: list[int] | None = None,
) -> int:
    # Entry boundary; see ``_recalculate_metric_anomalies``.
    evaluation_start, evaluation_end = _canonical_window(evaluation_start, evaluation_end)
    covered_buckets = _canonical_covered(covered_buckets)
    project_settings = _get_project_anomaly_settings(session, config.project_id)
    if project_settings is None or not project_settings.anomaly_detection_enabled:
        session.execute(
            delete(MetricBreakdownAnomaly).where(MetricBreakdownAnomaly.scan_config_id == config.id)
        )
        session.flush()
        return 0

    if not config.interval or (
        not config.metric_breakdown_columns
        and not config.platform_column
        and not _scan_has_event_level_breakdown_columns(session, config.id)
    ):
        session.execute(
            delete(MetricBreakdownAnomaly).where(MetricBreakdownAnomaly.scan_config_id == config.id)
        )
        session.flush()
        return 0

    _age_out_config_anomalies(session, MetricBreakdownAnomaly, config.id)

    interval_spec = get_interval(config.interval)
    settings = _build_anomaly_settings(project_settings)
    settling_buckets = settling_buckets_for(interval_spec.delta, settling_delay)
    app_version_column = config.app_version_column
    if app_version_column:
        # Clear markers produced before app-version series became observational.
        # Keep parity rows (platform-only) and the raw breakdown metrics used by
        # adoption charts and the dedicated release-regression detector.
        session.execute(
            delete(MetricBreakdownAnomaly).where(
                MetricBreakdownAnomaly.scan_config_id == config.id,
                MetricBreakdownAnomaly.breakdown_column == app_version_column,
                MetricBreakdownAnomaly.kind == MetricBreakdownAnomalyKind.volume,
            )
        )
    history_from = evaluation_start - interval_spec.delta * required_history_buckets(
        interval_spec.delta, settings
    )
    anomalies_detected = 0

    if project_settings.detect_project_total:
        for _event_id, _event_type_id, column, value, is_other in _collect_breakdown_scope_keys(
            session,
            scan_config_id=config.id,
            history_from=history_from,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            scope_type=SCOPE_PROJECT_TOTAL,
            app_version_column=app_version_column,
        ):
            points = _load_breakdown_scope_points(
                session,
                scan_config_id=config.id,
                scope_type=SCOPE_PROJECT_TOTAL,
                scope_ref=str(config.id),
                breakdown_column=column,
                breakdown_value=value,
                is_other=is_other,
                history_from=history_from,
                time_to=evaluation_end,
            )
            total_result = detect_anomalies(
                points,
                interval=interval_spec.delta,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                settings=settings,
                covered_buckets=covered_buckets,
                settling_buckets=settling_buckets,
            )
            anomalies_detected += _replace_scope_breakdown_anomalies(
                session,
                scan_config_id=config.id,
                scope_type=SCOPE_PROJECT_TOTAL,
                scope_ref=str(config.id),
                breakdown_column=column,
                breakdown_value=value,
                is_other=is_other,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                event_id=None,
                event_type_id=None,
                anomalies=total_result.anomalies,
                hold_drops=hold_drops,
                held=held,
                suppressed_ranges=total_result.suppressed_ranges,
            )
    else:
        session.execute(
            delete(MetricBreakdownAnomaly).where(
                MetricBreakdownAnomaly.scan_config_id == config.id,
                MetricBreakdownAnomaly.scope_type == SCOPE_PROJECT_TOTAL,
                MetricBreakdownAnomaly.bucket >= evaluation_start,
                MetricBreakdownAnomaly.bucket < evaluation_end,
            )
        )

    if project_settings.detect_event_types:
        type_breakdown_max = _breakdown_scope_max_counts(
            session,
            scan_config_id=config.id,
            scope_type=SCOPE_EVENT_TYPE,
            history_from=history_from,
            time_to=evaluation_end,
        )
        for _event_id, event_type_id, column, value, is_other in _collect_breakdown_scope_keys(
            session,
            scan_config_id=config.id,
            history_from=history_from,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            scope_type=SCOPE_EVENT_TYPE,
            app_version_column=app_version_column,
        ):
            if event_type_id is None:
                continue
            scope_ref = str(event_type_id)
            # Provably silent (extended to breakdowns in tripl-jfm3.73):
            # the detector would early-exit on this series anyway, so skip loading
            # its history — but still run the replace with no anomalies so stale
            # window rows age out.
            if is_provably_silent(
                type_breakdown_max.get((event_type_id, column, value, is_other), 0.0),
                settings.min_expected_count,
            ):
                anomalies_detected += _replace_scope_breakdown_anomalies(
                    session,
                    scan_config_id=config.id,
                    scope_type=SCOPE_EVENT_TYPE,
                    scope_ref=scope_ref,
                    breakdown_column=column,
                    breakdown_value=value,
                    is_other=is_other,
                    evaluation_start=evaluation_start,
                    evaluation_end=evaluation_end,
                    event_id=None,
                    event_type_id=event_type_id,
                    anomalies=[],
                    hold_drops=hold_drops,
                    held=held,
                )
                continue
            points = _load_breakdown_scope_points(
                session,
                scan_config_id=config.id,
                scope_type=SCOPE_EVENT_TYPE,
                scope_ref=scope_ref,
                breakdown_column=column,
                breakdown_value=value,
                is_other=is_other,
                history_from=history_from,
                time_to=evaluation_end,
            )
            type_result = detect_anomalies(
                points,
                interval=interval_spec.delta,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                settings=settings,
                covered_buckets=covered_buckets,
                settling_buckets=settling_buckets,
            )
            anomalies_detected += _replace_scope_breakdown_anomalies(
                session,
                scan_config_id=config.id,
                scope_type=SCOPE_EVENT_TYPE,
                scope_ref=scope_ref,
                breakdown_column=column,
                breakdown_value=value,
                is_other=is_other,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                event_id=None,
                event_type_id=event_type_id,
                anomalies=type_result.anomalies,
                hold_drops=hold_drops,
                held=held,
                suppressed_ranges=type_result.suppressed_ranges,
            )
    else:
        session.execute(
            delete(MetricBreakdownAnomaly).where(
                MetricBreakdownAnomaly.scan_config_id == config.id,
                MetricBreakdownAnomaly.scope_type == SCOPE_EVENT_TYPE,
                MetricBreakdownAnomaly.bucket >= evaluation_start,
                MetricBreakdownAnomaly.bucket < evaluation_end,
            )
        )

    if project_settings.detect_events:
        event_breakdown_max = _breakdown_scope_max_counts(
            session,
            scan_config_id=config.id,
            scope_type=SCOPE_EVENT,
            history_from=history_from,
            time_to=evaluation_end,
        )
        for event_id, _event_type_id, column, value, is_other in _collect_breakdown_scope_keys(
            session,
            scan_config_id=config.id,
            history_from=history_from,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            scope_type=SCOPE_EVENT,
            app_version_column=app_version_column,
        ):
            if event_id is None:
                continue
            scope_ref = str(event_id)
            # Provably silent (see the event-type loop above).
            if is_provably_silent(
                event_breakdown_max.get((event_id, column, value, is_other), 0.0),
                settings.min_expected_count,
            ):
                anomalies_detected += _replace_scope_breakdown_anomalies(
                    session,
                    scan_config_id=config.id,
                    scope_type=SCOPE_EVENT,
                    scope_ref=scope_ref,
                    breakdown_column=column,
                    breakdown_value=value,
                    is_other=is_other,
                    evaluation_start=evaluation_start,
                    evaluation_end=evaluation_end,
                    event_id=event_id,
                    event_type_id=None,
                    anomalies=[],
                    hold_drops=hold_drops,
                    held=held,
                )
                continue
            points = _load_breakdown_scope_points(
                session,
                scan_config_id=config.id,
                scope_type=SCOPE_EVENT,
                scope_ref=scope_ref,
                breakdown_column=column,
                breakdown_value=value,
                is_other=is_other,
                history_from=history_from,
                time_to=evaluation_end,
            )
            event_result = detect_anomalies(
                points,
                interval=interval_spec.delta,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                settings=settings,
                covered_buckets=covered_buckets,
                settling_buckets=settling_buckets,
            )
            anomalies_detected += _replace_scope_breakdown_anomalies(
                session,
                scan_config_id=config.id,
                scope_type=SCOPE_EVENT,
                scope_ref=scope_ref,
                breakdown_column=column,
                breakdown_value=value,
                is_other=is_other,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                event_id=event_id,
                event_type_id=None,
                anomalies=event_result.anomalies,
                hold_drops=hold_drops,
                held=held,
                suppressed_ranges=event_result.suppressed_ranges,
            )
    else:
        session.execute(
            delete(MetricBreakdownAnomaly).where(
                MetricBreakdownAnomaly.scan_config_id == config.id,
                MetricBreakdownAnomaly.scope_type == SCOPE_EVENT,
                MetricBreakdownAnomaly.bucket >= evaluation_start,
                MetricBreakdownAnomaly.bucket < evaluation_end,
            )
        )

    anomalies_detected += _recalculate_platform_parity_anomalies(
        session,
        config,
        project_settings=project_settings,
        settings=settings,
        evaluation_start=evaluation_start,
        evaluation_end=evaluation_end,
        covered_buckets=covered_buckets,
        settling_delay=settling_delay,
    )

    session.flush()
    return anomalies_detected
