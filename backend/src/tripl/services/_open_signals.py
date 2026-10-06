"""Open, significant, counted scan-anomaly signals: the one implementation.

The sidebar / ProjectsPage badge (``project_service._populate_monitoring_signals``)
and the F15 health score's "signals" component
(``_open_event_signals.open_unverdicted_event_signals``) both count "the signals
the Anomalies page shows by default" for scan-backed scopes. They used to carry
two copies of that rule; both now call :func:`open_counted_scan_signals`, which
applies, in order:

* the latest ``MetricAnomaly`` per ``(scan_config, scope_type, scope_ref)`` of
  the given projects and scopes (optionally restricted to some events);
* the latest ``EventMetric`` bucket per scope — ``project_total`` keyed by the
  scan config id and ``event_type`` by the type id, both read off the per-type
  rows (``event_id IS NULL AND event_type_id IS NOT NULL``); ``event`` keyed by
  the event id (``event_id IS NOT NULL``);
* scan liveness (``latest_bucket_by_scan``): each scan's newest bucket over the
  rows those three scopes read, i.e. ``event_id IS NOT NULL OR event_type_id IS
  NOT NULL``. This is the badge's historical rule; a scan's rows with neither id
  set do not keep an outage anchor open;
* ``classify_signal_state`` with the project's recent-signal window;
* the "Significant" magnitude gate (``is_significant_signal``);
* ``signal_triage_service.uncounted_signal_keys``: F01 verdicts, incident
  status, mutes and ``expected`` hide a signal.

Catalog-metric signals (``scope_type = metric``, no scan config) are not handled
here; the badge folds them in separately.

Open property drifts (F23, #306) are the second population both surfaces read,
and they have one implementation here too: :func:`open_property_drift_counts`
(per project, the ``open_property_drift_count`` the sidebar and the bell show)
and :func:`open_property_drift_counts_by_event` (the health score's drifts
component). Both apply the same rule as property-drift alert candidates
(``alerting_property_drift.active_property_drift_filters``) inside the
value-drift retention window, so a count, a score and an alert cannot disagree
about which drifts are open. They are NOT folded into the scan-signal count:
that number badges the Anomalies page and must equal what it lists.

Queries are batched: one for the anomalies, one for the metric buckets, one for
scan liveness (skipped on the unrestricted three-scope badge path, where the
metric-bucket rows already cover it), one for the recent-signal windows and the
triage lookups, whatever the number of projects. An event restriction is split into bounded
``IN`` chunks (``_id_chunks.chunked``), one anomaly and one bucket query per
chunk.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.alerting_property_drift import active_property_drift_filters
from tripl.core.analyzers.anomaly_detector import (
    SCOPE_EVENT,
    SCOPE_EVENT_TYPE,
    SCOPE_PROJECT_TOTAL,
)
from tripl.models.event_metric import EventMetric
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.property_drift import PropertyDrift
from tripl.models.scan_config import ScanConfig
from tripl.models.variable import Variable
from tripl.services import signal_triage_service
from tripl.services._id_chunks import chunked
from tripl.services.metrics_insights_service import is_significant_signal
from tripl.services.metrics_service import _get_project_recent_signal_windows
from tripl.services.monitoring_utils import (
    classify_signal_state,
    latest_bucket_by_scan,
    scan_interval_to_timedelta,
)
from tripl.services.variable_value_drift_service import retention_cutoff

SCAN_SCOPES: tuple[str, ...] = (SCOPE_PROJECT_TOTAL, SCOPE_EVENT_TYPE, SCOPE_EVENT)

# (scan_config_id, scope_type, scope_ref) -> newest metric bucket of that scope.
_BucketKey = tuple[uuid.UUID, str, str]
# (project_id, scan name, scan interval, latest anomaly of one scope).
_AnomalyRow = tuple[uuid.UUID, str, str | None, MetricAnomaly]


@dataclass(frozen=True, slots=True)
class OpenSignal:
    """One open, significant signal no triage verdict hides."""

    project_id: uuid.UUID
    scan_name: str
    state: str
    anomaly: MetricAnomaly


def signal_key(anomaly: MetricAnomaly) -> signal_triage_service.SignalKey:
    return signal_triage_service.signal_key(
        anomaly.scan_config_id, anomaly.scope_type, anomaly.scope_ref, anomaly.bucket
    )


def _latest_anomalies_stmt(
    project_ids: Sequence[uuid.UUID],
    scope_types: Sequence[str],
    scope_refs: Sequence[str] | None,
) -> Select[*_AnomalyRow]:
    filters = [
        ScanConfig.project_id.in_(list(project_ids)),
        MetricAnomaly.scope_type.in_(list(scope_types)),
        # A planned event expected these (F18): drawn on the chart, never open.
        MetricAnomaly.planned_event_id.is_(None),
    ]
    if scope_refs is not None:
        filters.append(MetricAnomaly.scope_ref.in_(list(scope_refs)))
    latest = (
        select(
            ScanConfig.project_id.label("project_id"),
            MetricAnomaly.scan_config_id.label("scan_config_id"),
            MetricAnomaly.scope_type.label("scope_type"),
            MetricAnomaly.scope_ref.label("scope_ref"),
            func.max(MetricAnomaly.bucket).label("bucket"),
        )
        .join(ScanConfig, ScanConfig.id == MetricAnomaly.scan_config_id)
        .where(*filters)
        .group_by(
            ScanConfig.project_id,
            MetricAnomaly.scan_config_id,
            MetricAnomaly.scope_type,
            MetricAnomaly.scope_ref,
        )
        .subquery()
    )
    return (
        select(ScanConfig.project_id, ScanConfig.name, ScanConfig.interval, MetricAnomaly)
        .join(ScanConfig, ScanConfig.id == MetricAnomaly.scan_config_id)
        .join(
            latest,
            (ScanConfig.project_id == latest.c.project_id)
            & (MetricAnomaly.scan_config_id == latest.c.scan_config_id)
            & (MetricAnomaly.scope_type == latest.c.scope_type)
            & (MetricAnomaly.scope_ref == latest.c.scope_ref)
            & (MetricAnomaly.bucket == latest.c.bucket),
        )
        .order_by(ScanConfig.project_id, MetricAnomaly.bucket.desc())
    )


async def _latest_anomalies(
    session: AsyncSession,
    project_ids: Sequence[uuid.UUID],
    scope_types: Sequence[str],
    event_refs: Sequence[str] | None,
) -> list[_AnomalyRow]:
    """Latest anomaly per scope, newest bucket first within each project."""
    statements: list[Select[*_AnomalyRow]] = []
    if event_refs is None:
        statements.append(_latest_anomalies_stmt(project_ids, scope_types, None))
    else:
        other_scopes = [scope for scope in scope_types if scope != SCOPE_EVENT]
        if other_scopes:
            statements.append(_latest_anomalies_stmt(project_ids, other_scopes, None))
        if SCOPE_EVENT in scope_types:
            statements.extend(
                _latest_anomalies_stmt(project_ids, [SCOPE_EVENT], chunk)
                for chunk in chunked(event_refs)
            )
    rows: list[_AnomalyRow] = []
    for statement in statements:
        result = await session.execute(statement)
        rows.extend(
            (project_id, scan_name, interval, anomaly)
            for project_id, scan_name, interval, anomaly in result.all()
        )
    if len(statements) > 1:
        # One statement arrives ordered by the database; several are merged here
        # into the same newest-first order (stable, so ties keep query order).
        rows.sort(key=lambda row: row[3].bucket, reverse=True)
    return rows


def _type_rows_branch(scan_ids: Sequence[uuid.UUID], scope_type: str) -> Select[*tuple[Any, ...]]:
    """Newest per-type row per scope: keyed by scan (project_total) or by type."""
    project_total = scope_type == SCOPE_PROJECT_TOTAL
    ref = EventMetric.scan_config_id if project_total else EventMetric.event_type_id
    group_by = (
        [EventMetric.scan_config_id]
        if project_total
        else [EventMetric.scan_config_id, EventMetric.event_type_id]
    )
    return (
        select(
            EventMetric.scan_config_id.label("scan_config_id"),
            literal(scope_type).label("scope_type"),
            ref.label("scope_ref_uuid"),
            func.max(EventMetric.bucket).label("latest_metric_bucket"),
        )
        .where(
            EventMetric.scan_config_id.in_(list(scan_ids)),
            EventMetric.event_id.is_(None),
            EventMetric.event_type_id.is_not(None),
        )
        .group_by(*group_by)
    )


def _event_rows_branch(
    scan_ids: Sequence[uuid.UUID], event_ids: Sequence[uuid.UUID] | None
) -> Select[*tuple[Any, ...]]:
    filters = [EventMetric.scan_config_id.in_(list(scan_ids)), EventMetric.event_id.is_not(None)]
    if event_ids is not None:
        filters.append(EventMetric.event_id.in_(list(event_ids)))
    return (
        select(
            EventMetric.scan_config_id.label("scan_config_id"),
            literal(SCOPE_EVENT).label("scope_type"),
            EventMetric.event_id.label("scope_ref_uuid"),
            func.max(EventMetric.bucket).label("latest_metric_bucket"),
        )
        .where(*filters)
        .group_by(EventMetric.scan_config_id, EventMetric.event_id)
    )


async def _latest_metric_buckets(
    session: AsyncSession,
    scan_ids: Sequence[uuid.UUID],
    scope_types: Sequence[str],
    event_ids: Sequence[uuid.UUID] | None,
) -> dict[_BucketKey, datetime]:
    """Newest stored bucket per scope the anomalies live on."""
    type_branches = [
        _type_rows_branch(scan_ids, scope)
        for scope in (SCOPE_PROJECT_TOTAL, SCOPE_EVENT_TYPE)
        if scope in scope_types
    ]
    event_filters: list[Sequence[uuid.UUID] | None] = []
    if SCOPE_EVENT in scope_types:
        event_filters = [None] if event_ids is None else list(chunked(event_ids))
    statements: list[list[Select[*tuple[Any, ...]]]] = [
        (type_branches if index == 0 else []) + [_event_rows_branch(scan_ids, chunk)]
        for index, chunk in enumerate(event_filters)
    ] or ([type_branches] if type_branches else [])

    buckets: dict[_BucketKey, datetime] = {}
    for branches in statements:
        union = union_all(*branches).subquery()
        result = await session.execute(
            select(
                union.c.scan_config_id,
                union.c.scope_type,
                union.c.scope_ref_uuid,
                union.c.latest_metric_bucket,
            )
        )
        for scan_config_id, scope_type, scope_ref_uuid, bucket in result.all():
            if scope_ref_uuid is not None and bucket is not None:
                buckets[(scan_config_id, scope_type, str(scope_ref_uuid))] = bucket
    return buckets


async def _scan_liveness(
    session: AsyncSession, scan_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, datetime]:
    """Newest bucket each scan collected over its per-type and per-event rows."""
    result = await session.execute(
        select(EventMetric.scan_config_id, func.max(EventMetric.bucket))
        .where(
            EventMetric.scan_config_id.in_(list(scan_ids)),
            or_(EventMetric.event_id.is_not(None), EventMetric.event_type_id.is_not(None)),
        )
        .group_by(EventMetric.scan_config_id)
    )
    return latest_bucket_by_scan(
        (scan_config_id, bucket) for scan_config_id, bucket in result.all()
    )


async def open_counted_scan_signals(
    session: AsyncSession,
    project_ids: Sequence[uuid.UUID],
    scope_types: Sequence[str] = SCAN_SCOPES,
    *,
    event_ids: Sequence[uuid.UUID] | None = None,
    now: datetime | None = None,
) -> list[OpenSignal]:
    """Every open, significant, counted scan signal of ``project_ids``.

    ``scope_types`` picks among ``project_total``, ``event_type`` and ``event``.
    ``event_ids``, when given, restricts the ``event`` scope to those events
    (other scopes are unaffected). Rows come newest bucket first per project.
    """
    if not project_ids:
        return []
    scopes = [scope for scope in SCAN_SCOPES if scope in scope_types]
    event_refs: list[str] | None = None
    if event_ids is not None:
        event_refs = [str(event_id) for event_id in event_ids]
        if not event_refs:
            scopes = [scope for scope in scopes if scope != SCOPE_EVENT]
    if not scopes:
        return []

    anomaly_rows = await _latest_anomalies(session, project_ids, scopes, event_refs)
    if not anomaly_rows:
        return []

    scan_ids = sorted(
        {
            anomaly.scan_config_id
            for *_row, anomaly in anomaly_rows
            if anomaly.scan_config_id is not None
        },
        key=str,
    )
    signal_event_ids: list[uuid.UUID] | None = None
    if event_ids is not None:
        wanted = {str(event_id): event_id for event_id in event_ids}
        signal_event_ids = sorted(
            {
                wanted[anomaly.scope_ref]
                for *_row, anomaly in anomaly_rows
                if anomaly.scope_type == SCOPE_EVENT and anomaly.scope_ref in wanted
            },
            key=str,
        )
    latest_buckets = await _latest_metric_buckets(session, scan_ids, scopes, signal_event_ids)
    if event_ids is None and set(scopes) == set(SCAN_SCOPES):
        # The unrestricted three-scope read already covers every row the
        # liveness rule looks at (per-type rows plus per-event rows), so the
        # per-scan max of those buckets is the liveness; no extra query.
        scan_latest = latest_bucket_by_scan(
            (scan_config_id, bucket) for (scan_config_id, _, _), bucket in latest_buckets.items()
        )
    else:
        scan_latest = await _scan_liveness(session, scan_ids)
    recent_windows = await _get_project_recent_signal_windows(session, list(project_ids))
    now = now or datetime.now(UTC)

    open_rows: list[OpenSignal] = []
    for project_id, scan_name, interval, anomaly in anomaly_rows:
        if anomaly.scan_config_id is None:  # the ScanConfig join rules this out
            continue
        state = classify_signal_state(
            anomaly_bucket=anomaly.bucket,
            latest_metric_bucket=latest_buckets.get(
                (anomaly.scan_config_id, anomaly.scope_type, anomaly.scope_ref)
            ),
            now=now,
            interval=scan_interval_to_timedelta(interval),
            recent_window=recent_windows.get(project_id),
            # An outage announced once and never re-emitted is re-checked against
            # the current series rather than its own age, and only
            # while the anchor had volume to lose. The magnitude
            # gate below already hides a zero-versus-zero row, so the expectation
            # changes no number here today; it is passed because every surface
            # must reach ``classify_signal_state`` with the same inputs.
            anomaly_actual_count=anomaly.actual_count,
            anomaly_expected_count=anomaly.expected_count,
            scan_latest_bucket=scan_latest.get(anomaly.scan_config_id),
        )
        if state is None:
            continue
        # The AnomaliesPage's default "Significant" view: relative effect >= 0.5,
        # incident children included, no incident dedup.
        if not is_significant_signal(anomaly.actual_count, anomaly.expected_count):
            continue
        open_rows.append(OpenSignal(project_id, scan_name, state, anomaly))
    if not open_rows:
        return []

    # Triage runs over the open, significant rows only: one verdict query for
    # every project, and one incident lookup per project with open rows.
    candidates: dict[uuid.UUID, list[signal_triage_service.SignalKey]] = {}
    for row in open_rows:
        candidates.setdefault(row.project_id, []).append(signal_key(row.anomaly))
    hidden = await signal_triage_service.uncounted_signal_keys(session, candidates)
    return [
        row for row in open_rows if signal_key(row.anomaly) not in hidden.get(row.project_id, set())
    ]


def _open_property_drifts_stmt(now: datetime) -> Select[*tuple[Any, ...]]:
    return (
        select(PropertyDrift.project_id, PropertyDrift.event_id, func.count(PropertyDrift.id))
        .join(Variable, Variable.id == PropertyDrift.variable_id)
        .where(
            PropertyDrift.detected_at >= retention_cutoff(now),
            *active_property_drift_filters(now),
        )
    )


async def open_property_drift_counts(
    session: AsyncSession,
    project_ids: Sequence[uuid.UUID],
    *,
    now: datetime | None = None,
) -> dict[uuid.UUID, int]:
    """Open property drifts per project, type changes included. One query."""
    if not project_ids:
        return {}
    now = now or datetime.now(UTC)
    result = await session.execute(
        _open_property_drifts_stmt(now)
        .where(PropertyDrift.project_id.in_(list(project_ids)))
        .group_by(PropertyDrift.project_id, PropertyDrift.event_id)
    )
    counts: dict[uuid.UUID, int] = {}
    for project_id, _event_id, count in result.all():
        counts[project_id] = counts.get(project_id, 0) + int(count)
    return counts


async def open_property_drift_counts_by_event(
    session: AsyncSession,
    project_id: uuid.UUID,
    event_ids: Sequence[uuid.UUID],
    *,
    now: datetime | None = None,
) -> dict[uuid.UUID, int]:
    """Open per-event property drifts (new property, missing required) per event.

    A type change is per property (``event_id`` NULL) and is not charged to
    any event: the sampler cannot say which event carried the value.
    """
    now = now or datetime.now(UTC)
    counts: dict[uuid.UUID, int] = {}
    for chunk in chunked(event_ids):
        result = await session.execute(
            _open_property_drifts_stmt(now)
            .where(
                PropertyDrift.project_id == project_id,
                PropertyDrift.event_id.in_(list(chunk)),
            )
            .group_by(PropertyDrift.project_id, PropertyDrift.event_id)
        )
        for _project_id, event_id, count in result.all():
            if event_id is not None:
                counts[event_id] = int(count)
    return counts
