"""Store "why did it change?" with every volume anomaly (F02, #255).

Runs in the metrics worker right after the anomaly passes have rewritten the
evaluation window. For each ``project_total`` / ``event_type`` / ``event``
anomaly of the scan in that window it splits the anomaly's delta (actual -
expected) across every breakdown column the scope has series for
(``tripl.core.analyzers.attribution``) and adds the release that crossed the
activation gate shortly before, then upserts one ``MetricAnomalyAttribution``
row per anomaly.

Numbers are fixed at detection time on purpose: the alert this run queues, the
AI explanation and the drilldown then all quote the same split. A replay
re-scores its window, which replaces the anomaly rows (the old attributions go
with them, ON DELETE CASCADE), and this pass recomputes them.

The app-version column never enters the dimension ranking — its series describe
rollout adoption, not a stable cohort, which is also why the anomaly detector
leaves it alone — it only feeds the release context.

The baseline a value's share is read from is ALWAYS the trailing
``baseline_window_buckets`` buckets before the flagged one (the project's
``ProjectAnomalySettings.baseline_window_buckets``, default 14) — whatever
detector flagged the bucket and whatever expectation it used; only the scope's
``expected_count`` comes from the detector. A value present in fewer than half
of the column's baseline buckets has no stable share to compare against: it is
left out of the named values and folds into ``Other``.

Reads are batched: one breakdown query and one scope-total query per scope
type over the union of the anomalies' windows, and the release activations are
computed once per run (``core.analyzers.attribution.release_activations``) —
never one query per anomaly.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy import func as sa_func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from tripl.core.analyzers.anomaly_detector import (
    SCOPE_EVENT,
    SCOPE_EVENT_TYPE,
    SCOPE_PROJECT_TOTAL,
)
from tripl.core.analyzers.attribution import (
    ColumnContribution,
    ReleaseContext,
    attribution_payload,
    column_contributions,
    rank_columns,
    release_activations,
    release_context_at,
)
from tripl.core.bucketing import to_utc
from tripl.core.intervals import get_interval
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_anomaly_attribution import MetricAnomalyAttribution
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.services.version_activation import compile_prerelease_pattern, resolve_share_min
from tripl.worker.tasks.metrics.release_annotations import RELEASE_ANNOTATION_LOOKBACK

logger = logging.getLogger(__name__)

ATTRIBUTED_SCOPES: tuple[str, ...] = (SCOPE_PROJECT_TOTAL, SCOPE_EVENT_TYPE, SCOPE_EVENT)
# Trailing baseline depth when the project has no settings row to read it from;
# mirrors ``ProjectAnomalySettings.baseline_window_buckets``' default.
_FALLBACK_BASELINE_BUCKETS = 14
# A release is context for an anomaly when it activated within the baseline
# window before it — and never less than a day, so an hourly scan with a short
# baseline still sees yesterday's rollout.
_MIN_RELEASE_WINDOW = timedelta(hours=24)

# A value must carry volume in at least this fraction of the column's baseline
# buckets to be named; rarer values fold into ``Other``.
MIN_VALUE_PRESENCE = 0.5

# {column: {bucket: {value: count}}}; ``None`` as the value key is the
# breakdown's own "Other" row, kept only to mark the bucket as collected.
_BreakdownSlice = dict[str, dict[datetime, dict[str | None, float]]]
# {scope_ref: {bucket: scope total}}.
_ScopeTotals = dict[str, dict[datetime, float]]


def scan_breakdown_columns(session: Session, config: ScanConfig) -> set[str]:
    """Every breakdown column that can feed the ranking for this scan.

    Scan-level ``metric_breakdown_columns`` and ``platform_column``, plus the
    event-level ``metric_breakdown_columns`` of the scan's events — minus the
    app-version column, which only feeds the release context.
    """
    app_version = config.app_version_column
    columns = {column for column in (config.metric_breakdown_columns or []) if column}
    if config.platform_column:
        columns.add(config.platform_column)
    event_ids = (
        select(EventMetric.event_id)
        .where(EventMetric.scan_config_id == config.id, EventMetric.event_id.is_not(None))
        .distinct()
    )
    for event_columns in session.execute(
        select(Event.metric_breakdown_columns).where(Event.id.in_(event_ids))
    ).scalars():
        columns.update(column for column in (event_columns or []) if column)
    columns.discard(app_version or "")
    return columns


def _baseline_buckets(session: Session, project_id: uuid.UUID) -> int:
    value = session.execute(
        select(ProjectAnomalySettings.baseline_window_buckets).where(
            ProjectAnomalySettings.project_id == project_id
        )
    ).scalar_one_or_none()
    return max(int(value), 1) if value else _FALLBACK_BASELINE_BUCKETS


def _scope_refs(scope_type: str, refs: set[str]) -> dict[str, uuid.UUID]:
    """Parse the scope refs of one scope type; a malformed ref is skipped."""
    parsed: dict[str, uuid.UUID] = {}
    for ref in refs:
        try:
            parsed[ref] = uuid.UUID(ref)
        except ValueError:
            logger.warning("attribution: malformed %s scope_ref %r", scope_type, ref)
    return parsed


def _load_breakdown_slices(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    scope_type: str,
    scope_refs: set[str],
    time_from: datetime,
    time_to: datetime,
    exclude_column: str | None,
) -> dict[str, _BreakdownSlice]:
    """Every scope of ONE type: breakdown rows over ``[time_from, time_to)``.

    One query per scope type. Scope filters mirror
    ``detect._load_breakdown_scope_points``: the project total and event types
    read the per-type rollups, an event its own rows.
    """
    owner: Any
    ref_of: dict[uuid.UUID, str] = {}
    base = [
        EventMetricBreakdown.scan_config_id == scan_config_id,
        EventMetricBreakdown.bucket >= time_from,
        EventMetricBreakdown.bucket < time_to,
    ]
    if exclude_column:
        base.append(EventMetricBreakdown.breakdown_column != exclude_column)
    if scope_type == SCOPE_PROJECT_TOTAL:
        owner = None
        base += [
            EventMetricBreakdown.event_id.is_(None),
            EventMetricBreakdown.event_type_id.is_not(None),
        ]
    else:
        parsed = _scope_refs(scope_type, scope_refs)
        if not parsed:
            return {}
        ref_of = {value: ref for ref, value in parsed.items()}
        if scope_type == SCOPE_EVENT_TYPE:
            owner = EventMetricBreakdown.event_type_id
            base += [EventMetricBreakdown.event_id.is_(None), owner.in_(list(ref_of))]
        else:
            owner = EventMetricBreakdown.event_id
            base.append(owner.in_(list(ref_of)))

    group = [
        EventMetricBreakdown.breakdown_column,
        EventMetricBreakdown.breakdown_value,
        EventMetricBreakdown.is_other,
        EventMetricBreakdown.bucket,
    ]
    if owner is not None:
        group.append(owner)
    query = select(*group, sa_func.sum(EventMetricBreakdown.count)).where(*base).group_by(*group)

    result: dict[str, _BreakdownSlice] = {}
    for row in session.execute(query).all():
        column, value, is_other, bucket = row[0], row[1], row[2], row[3]
        count = row[-1]
        if owner is None:
            targets = list(scope_refs)
        else:
            ref = ref_of.get(row[4])
            if ref is None:
                continue
            targets = [ref]
        key = None if is_other else value
        for ref in targets:
            by_value = (
                result.setdefault(ref, {}).setdefault(column, {}).setdefault(to_utc(bucket), {})
            )
            by_value[key] = by_value.get(key, 0.0) + float(count or 0)
    return result


def _load_scope_totals(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    scope_type: str,
    scope_refs: set[str],
    time_from: datetime,
    time_to: datetime,
) -> _ScopeTotals:
    """Every scope of ONE type: its total per bucket, in one query.

    The same series ``detect._load_scope_points`` reads for one scope.
    """
    base = [
        EventMetric.scan_config_id == scan_config_id,
        EventMetric.bucket >= time_from,
        EventMetric.bucket < time_to,
    ]
    if scope_type == SCOPE_PROJECT_TOTAL:
        rows = session.execute(
            select(EventMetric.bucket, sa_func.sum(EventMetric.count))
            .where(
                *base,
                EventMetric.event_id.is_(None),
                EventMetric.event_type_id.is_not(None),
            )
            .group_by(EventMetric.bucket)
        ).all()
        series = {to_utc(bucket): float(count or 0) for bucket, count in rows}
        return {ref: dict(series) for ref in scope_refs}

    parsed = _scope_refs(scope_type, scope_refs)
    if not parsed:
        return {}
    ref_of = {value: ref for ref, value in parsed.items()}
    if scope_type == SCOPE_EVENT_TYPE:
        owner = EventMetric.event_type_id
        filters = [EventMetric.event_id.is_(None), owner.in_(list(ref_of))]
    else:
        owner = EventMetric.event_id
        filters = [owner.in_(list(ref_of))]
    result: _ScopeTotals = {}
    for owner_id, bucket, count in session.execute(
        select(owner, EventMetric.bucket, EventMetric.count).where(*base, *filters)
    ).all():
        ref = ref_of.get(owner_id)
        if ref is None:
            continue
        series = result.setdefault(ref, {})
        key = to_utc(bucket)
        series[key] = series.get(key, 0.0) + float(count or 0)
    return result


def _attribute_columns(
    anomaly: MetricAnomaly,
    *,
    breakdown: _BreakdownSlice,
    totals: dict[datetime, float],
    baseline_from: datetime,
) -> list[ColumnContribution]:
    """Split one anomaly's delta across every column of its scope.

    The baseline is the trailing window ``[baseline_from, flagged bucket)`` —
    the slice may reach further back for other anomalies of the run. Values
    present in fewer than ``MIN_VALUE_PRESENCE`` of the column's baseline
    buckets are not named; they fold into ``Other``.
    """
    bucket = to_utc(anomaly.bucket)
    contributions: list[ColumnContribution | None] = []
    for column, by_bucket in breakdown.items():
        flagged = by_bucket.get(bucket)
        if flagged is None and anomaly.actual_count > 0:
            # No series stored for the flagged bucket while the scope had
            # volume: the column was not collected then, not zero.
            continue
        baseline_buckets = [
            candidate for candidate in by_bucket if baseline_from <= candidate < bucket
        ]
        baseline_by_value: dict[str, float] = defaultdict(float)
        presence: dict[str, int] = defaultdict(int)
        for candidate in baseline_buckets:
            for value, count in by_bucket[candidate].items():
                if value is None:
                    continue
                baseline_by_value[value] += count
                if count > 0:
                    presence[value] += 1
        needed = MIN_VALUE_PRESENCE * len(baseline_buckets)
        named = {value for value, seen in presence.items() if seen >= needed}
        baseline_total = sum(totals.get(candidate, 0.0) for candidate in baseline_buckets)
        actual_by_value = {
            value: count
            for value, count in (flagged or {}).items()
            if value is not None and value in named
        }
        contributions.append(
            column_contributions(
                column,
                actual_total=float(anomaly.actual_count),
                expected_total=float(anomaly.expected_count),
                actual_by_value=actual_by_value,
                baseline_by_value={value: baseline_by_value[value] for value in named},
                baseline_total=baseline_total,
            )
        )
    return rank_columns(contributions)


def _load_version_traffic(
    session: Session,
    config: ScanConfig,
    *,
    time_from: datetime,
    time_to: datetime,
) -> tuple[dict[str, dict[datetime, float]], dict[datetime, float]]:
    """Per-version and total event-level traffic — the release markers' series."""
    rows = session.execute(
        select(
            EventMetricBreakdown.breakdown_value,
            EventMetricBreakdown.is_other,
            EventMetricBreakdown.bucket,
            sa_func.sum(EventMetricBreakdown.count),
        )
        .where(
            EventMetricBreakdown.scan_config_id == config.id,
            EventMetricBreakdown.breakdown_column == config.app_version_column,
            EventMetricBreakdown.event_id.is_not(None),
            EventMetricBreakdown.bucket >= time_from,
            EventMetricBreakdown.bucket < time_to,
        )
        .group_by(
            EventMetricBreakdown.breakdown_value,
            EventMetricBreakdown.is_other,
            EventMetricBreakdown.bucket,
        )
    ).all()
    by_version: dict[str, dict[datetime, float]] = {}
    traffic: dict[datetime, float] = {}
    for version, is_other, bucket, count in rows:
        bucket = to_utc(bucket)
        amount = float(count or 0)
        traffic[bucket] = traffic.get(bucket, 0.0) + amount
        if is_other or not version:
            continue
        series = by_version.setdefault(version, {})
        series[bucket] = series.get(bucket, 0.0) + amount
    return by_version, traffic


def _upsert_attributions(session: Session, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        return
    updatable = ["delta", "columns", "release", "computed_at"]
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        sqlite_stmt = sqlite_insert(MetricAnomalyAttribution).values(list(rows))
        sqlite_stmt = sqlite_stmt.on_conflict_do_update(
            index_elements=["anomaly_id"],
            set_={column: getattr(sqlite_stmt.excluded, column) for column in updatable},
        )
        session.execute(sqlite_stmt)
        return
    pg_stmt = pg_insert(MetricAnomalyAttribution).values(list(rows))
    pg_stmt = pg_stmt.on_conflict_do_update(
        constraint="uq_metric_anomaly_attribution_anomaly",
        set_={column: getattr(pg_stmt.excluded, column) for column in updatable},
    )
    session.execute(pg_stmt)


def recompute_anomaly_attributions(
    session: Session,
    config: ScanConfig,
    *,
    evaluation_start: datetime,
    evaluation_end: datetime,
    now: datetime | None = None,
) -> int:
    """(Re)compute the attribution of every volume anomaly in the window.

    Returns how many attribution rows were written. Never commits. An anomaly
    whose scope has no breakdown series (and no release context) gets no row,
    and any row it had is removed, so the read side reports ``not_computed``
    rather than a stale split.
    """
    if not config.interval:
        return 0
    evaluation_start, evaluation_end = to_utc(evaluation_start), to_utc(evaluation_end)
    anomalies = list(
        session.execute(
            select(MetricAnomaly)
            .where(
                MetricAnomaly.scan_config_id == config.id,
                MetricAnomaly.scope_type.in_(ATTRIBUTED_SCOPES),
                MetricAnomaly.bucket >= evaluation_start,
                MetricAnomaly.bucket < evaluation_end,
            )
            .order_by(MetricAnomaly.bucket)
        ).scalars()
    )
    if not anomalies:
        return 0
    anomaly_ids = [anomaly.id for anomaly in anomalies]
    if not scan_breakdown_columns(session, config):
        session.execute(
            delete(MetricAnomalyAttribution).where(
                MetricAnomalyAttribution.anomaly_id.in_(anomaly_ids)
            )
        )
        return 0

    delta = get_interval(config.interval).delta
    baseline_buckets = _baseline_buckets(session, config.project_id)
    baseline_span = delta * baseline_buckets
    release_window = max(_MIN_RELEASE_WINDOW, baseline_span)
    first_bucket = to_utc(anomalies[0].bucket)
    last_bucket = to_utc(anomalies[-1].bucket)

    # Release context is scan-wide: the activations are computed once per run
    # over the whole slice, then read per flagged bucket.
    by_version: dict[str, dict[datetime, float]] = {}
    version_traffic: dict[datetime, float] = {}
    prerelease_pattern = compile_prerelease_pattern(config.app_version_prerelease_pattern)
    if config.app_version_column:
        by_version, version_traffic = _load_version_traffic(
            session,
            config,
            time_from=first_bucket - RELEASE_ANNOTATION_LOOKBACK,
            time_to=last_bucket + delta,
        )
    activations = (
        release_activations(
            by_version,
            version_traffic,
            share_min=resolve_share_min(config.app_version_active_share_min),
            prerelease_pattern=prerelease_pattern,
        )
        if by_version
        else {}
    )
    releases: dict[datetime, ReleaseContext | None] = {}

    # One breakdown read and one scope-total read per scope type, over the
    # union of that type's anomaly windows.
    breakdowns: dict[tuple[str, str], _BreakdownSlice] = {}
    totals: dict[tuple[str, str], dict[datetime, float]] = {}
    for scope_type in ATTRIBUTED_SCOPES:
        typed = [anomaly for anomaly in anomalies if anomaly.scope_type == scope_type]
        if not typed:
            continue
        refs = {anomaly.scope_ref for anomaly in typed}
        time_from = min(to_utc(anomaly.bucket) for anomaly in typed) - baseline_span
        time_to = max(to_utc(anomaly.bucket) for anomaly in typed) + delta
        slices = _load_breakdown_slices(
            session,
            scan_config_id=config.id,
            scope_type=scope_type,
            scope_refs=refs,
            time_from=time_from,
            time_to=time_to,
            exclude_column=config.app_version_column,
        )
        if not slices:
            continue
        scope_totals = _load_scope_totals(
            session,
            scan_config_id=config.id,
            scope_type=scope_type,
            scope_refs=set(slices),
            time_from=time_from,
            time_to=time_to,
        )
        for ref, ref_breakdown in slices.items():
            breakdowns[(scope_type, ref)] = ref_breakdown
            totals[(scope_type, ref)] = scope_totals.get(ref, {})

    computed_at = now or datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for anomaly in anomalies:
        bucket = to_utc(anomaly.bucket)
        key = (anomaly.scope_type, anomaly.scope_ref)
        breakdown = breakdowns.get(key)
        columns: list[ColumnContribution] = []
        if breakdown:
            columns = _attribute_columns(
                anomaly,
                breakdown=breakdown,
                totals=totals.get(key, {}),
                baseline_from=bucket - baseline_span,
            )
        if bucket not in releases:
            releases[bucket] = (
                release_context_at(
                    activations,
                    by_version,
                    version_traffic,
                    anomaly_bucket=bucket,
                    window=release_window,
                    prerelease_pattern=prerelease_pattern,
                )
                if activations
                else None
            )
        release = releases[bucket]
        if not columns and release is None:
            continue
        payload = attribution_payload(
            delta=float(anomaly.actual_count) - float(anomaly.expected_count),
            columns=columns,
            release=release,
        )
        rows.append(
            {
                "id": uuid.uuid4(),
                "anomaly_id": anomaly.id,
                "delta": payload["delta"],
                "columns": payload["columns"],
                "release": payload["release"],
                "computed_at": computed_at,
            }
        )

    written = {row["anomaly_id"] for row in rows}
    stale = [anomaly_id for anomaly_id in anomaly_ids if anomaly_id not in written]
    if stale:
        session.execute(
            delete(MetricAnomalyAttribution).where(MetricAnomalyAttribution.anomaly_id.in_(stale))
        )
    _upsert_attributions(session, rows)
    return len(rows)


def stored_attribution_payload(
    session: Session,
    *,
    scan_config_id: uuid.UUID | None,
    scope_type: str,
    scope_ref: str,
    bucket: datetime,
) -> dict[str, Any] | None:
    """The stored ``{delta, columns, release}`` of one anomaly, keyed like a signal.

    For the alert side (delivery item / message / AI prompt): pair it with
    ``core.analyzers.attribution.attribution_headline`` and ``release_line`` so
    the alert quotes the same one-liner the drilldown shows.
    """
    if scan_config_id is None:
        return None
    row = session.execute(
        select(MetricAnomalyAttribution)
        .join(MetricAnomaly, MetricAnomaly.id == MetricAnomalyAttribution.anomaly_id)
        .where(
            MetricAnomaly.scan_config_id == scan_config_id,
            MetricAnomaly.scope_type == scope_type,
            MetricAnomaly.scope_ref == scope_ref,
            MetricAnomaly.bucket == bucket,
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return {"delta": row.delta, "columns": row.columns or [], "release": row.release}
