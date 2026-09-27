"""Open, significant, unverdicted EVENT-scope signals per event (F15, #268).

The health score's "signals" component counts what the sidebar badge counts,
restricted to ``scope_type = event`` and the requested events. This mirrors the
event half of ``project_service._populate_monitoring_signals``:

* the latest ``MetricAnomaly`` per ``(scan_config, scope_ref)``;
* the latest ``EventMetric`` bucket per ``(scan_config, event_id)`` and each
  scan's newest bucket overall (``latest_bucket_by_scan``: scan liveness);
* ``classify_signal_state`` with the project's recent-signal window;
* the "Significant" magnitude gate (``is_significant_signal``);
* ``signal_triage_service.uncounted_signal_keys``: F01 verdicts, incident
  status, mutes and ``expected`` hide a signal.

Follow-up (bd): have ``project_service`` consume this helper for its event
scope so the badge and the score share one implementation. It is kept separate
in v1 so the badge path stays untouched.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.analyzers.anomaly_detector import SCOPE_EVENT
from tripl.models.event_metric import EventMetric
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.scan_config import ScanConfig
from tripl.services import signal_triage_service
from tripl.services._id_chunks import chunked
from tripl.services.metrics_insights_service import is_significant_signal
from tripl.services.metrics_service import _get_project_recent_signal_windows
from tripl.services.monitoring_utils import (
    classify_signal_state,
    latest_bucket_by_scan,
    scan_interval_to_timedelta,
)


async def _latest_event_anomalies(
    session: AsyncSession, project_id: uuid.UUID, refs: Sequence[str]
) -> list[tuple[str | None, MetricAnomaly]]:
    """Latest anomaly per (scan_config, scope_ref) for the given event refs."""
    rows: list[tuple[str | None, MetricAnomaly]] = []
    for chunk in chunked(refs):
        latest = (
            select(
                MetricAnomaly.scan_config_id.label("scan_config_id"),
                MetricAnomaly.scope_ref.label("scope_ref"),
                func.max(MetricAnomaly.bucket).label("bucket"),
            )
            .join(ScanConfig, ScanConfig.id == MetricAnomaly.scan_config_id)
            .where(
                ScanConfig.project_id == project_id,
                MetricAnomaly.scope_type == SCOPE_EVENT,
                MetricAnomaly.scope_ref.in_(list(chunk)),
            )
            .group_by(MetricAnomaly.scan_config_id, MetricAnomaly.scope_ref)
            .subquery()
        )
        result = await session.execute(
            select(ScanConfig.interval, MetricAnomaly)
            .join(ScanConfig, ScanConfig.id == MetricAnomaly.scan_config_id)
            .join(
                latest,
                (MetricAnomaly.scan_config_id == latest.c.scan_config_id)
                & (MetricAnomaly.scope_ref == latest.c.scope_ref)
                & (MetricAnomaly.bucket == latest.c.bucket),
            )
            .where(MetricAnomaly.scope_type == SCOPE_EVENT)
        )
        rows.extend((interval, anomaly) for interval, anomaly in result.all())
    return rows


async def _latest_event_buckets(
    session: AsyncSession, scan_config_ids: Sequence[uuid.UUID], event_ids: Sequence[uuid.UUID]
) -> dict[tuple[uuid.UUID, str], datetime]:
    """Latest metric bucket per (scan_config, event_id), keyed by str(event_id)."""
    out: dict[tuple[uuid.UUID, str], datetime] = {}
    for chunk in chunked(event_ids):
        result = await session.execute(
            select(EventMetric.scan_config_id, EventMetric.event_id, func.max(EventMetric.bucket))
            .where(
                EventMetric.scan_config_id.in_(list(scan_config_ids)),
                EventMetric.event_id.in_(list(chunk)),
            )
            .group_by(EventMetric.scan_config_id, EventMetric.event_id)
        )
        for scan_config_id, event_id, bucket in result.all():
            if event_id is not None and bucket is not None:
                out[(scan_config_id, str(event_id))] = bucket
    return out


async def _scan_liveness(
    session: AsyncSession, scan_config_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, datetime]:
    """Newest bucket each scan collected, across every scope it writes."""
    result = await session.execute(
        select(EventMetric.scan_config_id, func.max(EventMetric.bucket))
        .where(EventMetric.scan_config_id.in_(list(scan_config_ids)))
        .group_by(EventMetric.scan_config_id)
    )
    return latest_bucket_by_scan(
        (scan_config_id, bucket) for scan_config_id, bucket in result.all()
    )


async def open_unverdicted_event_signals(
    session: AsyncSession,
    project_id: uuid.UUID,
    event_ids: Sequence[uuid.UUID],
    *,
    now: datetime | None = None,
) -> dict[uuid.UUID, int]:
    """Per event, how many of its event-scope signals the badge would count.

    Events with none are absent from the mapping.
    """
    if not event_ids:
        return {}
    wanted = {str(event_id): event_id for event_id in event_ids}
    anomaly_rows = await _latest_event_anomalies(session, project_id, list(wanted))
    if not anomaly_rows:
        return {}

    scan_ids = sorted(
        {anomaly.scan_config_id for _i, anomaly in anomaly_rows if anomaly.scan_config_id},
        key=str,
    )
    signal_event_ids = sorted(
        {wanted[anomaly.scope_ref] for _i, anomaly in anomaly_rows if anomaly.scope_ref in wanted},
        key=str,
    )
    latest_buckets = await _latest_event_buckets(session, scan_ids, signal_event_ids)
    scan_latest = await _scan_liveness(session, scan_ids)
    recent_window = (await _get_project_recent_signal_windows(session, [project_id])).get(
        project_id
    )
    now = now or datetime.now(UTC)

    open_rows: list[MetricAnomaly] = []
    for interval, anomaly in anomaly_rows:
        if anomaly.scan_config_id is None or anomaly.scope_ref not in wanted:
            continue
        state = classify_signal_state(
            anomaly_bucket=anomaly.bucket,
            latest_metric_bucket=latest_buckets.get((anomaly.scan_config_id, anomaly.scope_ref)),
            now=now,
            interval=scan_interval_to_timedelta(interval),
            recent_window=recent_window,
            anomaly_actual_count=anomaly.actual_count,
            anomaly_expected_count=anomaly.expected_count,
            scan_latest_bucket=scan_latest.get(anomaly.scan_config_id),
        )
        if state is None:
            continue
        if not is_significant_signal(anomaly.actual_count, anomaly.expected_count):
            continue
        open_rows.append(anomaly)
    if not open_rows:
        return {}

    def _key(anomaly: MetricAnomaly) -> signal_triage_service.SignalKey:
        return signal_triage_service.signal_key(
            anomaly.scan_config_id, anomaly.scope_type, anomaly.scope_ref, anomaly.bucket
        )

    hidden = (
        await signal_triage_service.uncounted_signal_keys(
            session, {project_id: [_key(anomaly) for anomaly in open_rows]}
        )
    ).get(project_id, set())
    counts: dict[uuid.UUID, int] = {}
    for anomaly in open_rows:
        if _key(anomaly) in hidden:
            continue
        event_id = wanted[anomaly.scope_ref]
        counts[event_id] = counts.get(event_id, 0) + 1
    return counts
