"""Service for querying event metrics and anomaly signals from PostgreSQL."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from tripl.core.analyzers.anomaly_detector import (
    SCOPE_EVENT,
    SCOPE_EVENT_TYPE,
    SCOPE_PROJECT_TOTAL,
    SeriesPoint,
    expand_series,
    forecast_next_buckets,
)
from tripl.core.intervals import get_interval
from tripl.models.anomaly_scope_override import AnomalyScopeOverride
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import MetricBreakdownAnomalyKind
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.event_tag import EventTag
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_baseline import MetricBaseline
from tripl.models.metric_breakdown_anomaly import MetricBreakdownAnomaly
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.project_anomaly_settings import (
    DEFAULT_SIGMA_THRESHOLD,
    ProjectAnomalySettings,
)
from tripl.models.release_regression import ReleaseComparability, ReleaseRegression
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.schemas.data_source import DataSourceStatsResponse, DataSourceThroughputPoint
from tripl.schemas.event_metric import (
    AppVersionAdoptionResponse,
    AppVersionInfo,
    AppVersionMetricSeries,
    AppVersionSeriesResponse,
    BreakdownTimelinePoint,
    EventMetricBreakdownSeries,
    EventMetricBreakdownsResponse,
    EventMetricPoint,
    EventMetricsResponse,
    EventWindowMetricsResponse,
    ForecastPoint,
    MetricSignalResponse,
    OverviewKpiSeriesResponse,
    PlatformParityAnomaly,
    PlatformPresenceResponse,
    PlatformPresenceRow,
    ReleaseComparabilityItem,
    ReleaseRegressionItem,
    ReleaseRegressionsResponse,
    TopEventResponse,
)
from tripl.semver import (
    APP_VERSION_OTHER_LABEL,
    order_versions,
)
from tripl.services import anomaly_attribution_service, signal_verdict_service
from tripl.services._branch_counterparts import main_counterparts, metrics_row_for
from tripl.services.monitoring_utils import (
    classify_signal_state,
    latest_bucket_by_scan,
    recent_signal_window_from_hours,
    scan_interval_to_timedelta,
    scan_liveness_cutoff,
)
from tripl.services.plan_branch_service import ensure_main_branch_id, resolve_branch_id
from tripl.services.project_lookup import owning_org_id, resolve_project
from tripl.services.version_activation import (
    DEFAULT_ACTIVE_SHARE_MIN,
    active_release_versions,
    compile_prerelease_pattern,
    released_versions,
    resolve_share_min,
)


async def _resolve_event(
    session: AsyncSession,
    project_id: uuid.UUID,
    event_id: uuid.UUID,
) -> Event:
    event = await session.get(Event, event_id)
    if event is None or event.project_id != project_id:
        raise HTTPException(404, "Event not found")
    # A branch copy has no metrics of its own; its main twin has them all.
    return await metrics_row_for(session, project_id=project_id, event=event)


async def _resolve_event_type(
    session: AsyncSession,
    project_id: uuid.UUID,
    event_type_id: uuid.UUID,
) -> EventType:
    event_type = await session.get(EventType, event_type_id)
    if event_type is None or event_type.project_id != project_id:
        raise HTTPException(404, "Event type not found")
    return event_type


async def _resolve_scan_config(
    session: AsyncSession,
    project_id: uuid.UUID,
    scan_config_id: uuid.UUID,
) -> ScanConfig:
    config = await session.get(ScanConfig, scan_config_id)
    if config is None or config.project_id != project_id:
        raise HTTPException(404, "Scan config not found")
    return config


async def _get_scan_config_interval(
    session: AsyncSession,
    scan_config_id: uuid.UUID | None,
) -> str | None:
    if scan_config_id is None:
        return None
    result = await session.execute(
        select(ScanConfig.interval).where(ScanConfig.id == scan_config_id)
    )
    return result.scalar_one_or_none()


async def _get_project_sigma_threshold(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> float:
    """The project's anomaly sigma threshold — the band multiplier the UI serves.

    ``ProjectAnomalySettings.sigma_threshold``, because that is the column the
    DETECTOR scores with (``worker.tasks.metrics.detect._build_anomaly_settings``)
    and the one the operator edits in Settings → Monitoring. This used to read
    ``ScanConfig.sigma_threshold``, which no API has ever written: after an
    operator moved the project sigma to 5.0 the charts kept drawing the band at
    the scan row's stale 4.0, so buckets sat outside a band that had not flagged
    them — the exact opposite of the "outside the band = flagged" contract
    ``_apply_scope_sigma_override`` states below. ``alerting_service`` already
    reads the project row for the same reason.

    Falls back to the system default when the project has no settings row yet,
    which is the same value the detector would use.
    """
    result = await session.execute(
        select(ProjectAnomalySettings.sigma_threshold).where(
            ProjectAnomalySettings.project_id == project_id
        )
    )
    return result.scalar_one_or_none() or DEFAULT_SIGMA_THRESHOLD


async def _apply_scope_sigma_override(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    scan_config_id: uuid.UUID | None,
    scope_type: str,
    scope_ref: str,
    fallback: float,
) -> float:
    """The band multiplier for ONE scope, honouring its false-positive override.

    The chart band is ``expected ± sigma_threshold × effective_stddev`` and is
    meant to read as "outside the band = flagged". A scope the ratchet has
    tightened is scored at its own sigma, so serving the project-wide value
    would draw a band narrower than the rule that produced the dots and show
    buckets outside it that were deliberately not flagged.

    ``fallback`` is the project sigma (``_get_project_sigma_threshold``) — the
    same two-step the detector does, override first and project setting behind
    it (``worker.tasks.metrics.detect``).
    """
    override = await session.scalar(
        select(AnomalyScopeOverride.sigma_threshold).where(
            AnomalyScopeOverride.project_id == project_id,
            AnomalyScopeOverride.scan_config_id.is_(None)
            if scan_config_id is None
            else AnomalyScopeOverride.scan_config_id == scan_config_id,
            AnomalyScopeOverride.scope_type == scope_type,
            AnomalyScopeOverride.scope_ref == scope_ref,
        )
    )
    return float(override) if override is not None else fallback


async def _get_project_recent_signal_window(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> timedelta | None:
    """The project's configured open-signal freshness window, or ``None``.

    ``None`` (no monitoring-settings row yet) leaves ``classify_signal_state`` on
    its default 24h window. Signal classification runs over every anomaly in a
    project, so callers must load this ONCE per request and pass it down — never
    per signal.
    """
    hours = (
        await session.execute(
            select(ProjectAnomalySettings.recent_signal_window_hours).where(
                ProjectAnomalySettings.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    return recent_signal_window_from_hours(hours)


async def _get_project_recent_signal_windows(
    session: AsyncSession,
    project_ids: list[uuid.UUID],
) -> dict[uuid.UUID, timedelta]:
    """Batched sibling of :func:`_get_project_recent_signal_window` for N projects.

    Projects without a settings row are simply absent from the mapping, so a
    ``.get(project_id)`` lookup yields ``None`` and the default window.
    """
    if not project_ids:
        return {}
    rows = (
        await session.execute(
            select(
                ProjectAnomalySettings.project_id,
                ProjectAnomalySettings.recent_signal_window_hours,
            ).where(ProjectAnomalySettings.project_id.in_(project_ids))
        )
    ).all()
    windows: dict[uuid.UUID, timedelta] = {}
    for project_id, hours in rows:
        window = recent_signal_window_from_hours(hours)
        if window is not None:
            windows[project_id] = window
    return windows


async def _get_default_scan_config(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> ScanConfig | None:
    """The one scan config the project-scoped volume series is charted from.

    Ordering is ``created_at DESC, id DESC`` — deliberately NOT ``updated_at``.
    ``updated_at`` carries ``onupdate=func.now()``, so merely
    renaming an unrelated scan config silently re-pointed the Overview volume
    card and the Events dynamics chart at a different scan — a 32x swing on
    acme-ios with no change in the underlying data. ``created_at`` never moves,
    and the ``id`` tiebreak makes the pick deterministic even when two configs
    were created in the same microsecond (two acme-ios configs shared a
    byte-identical timestamp, so the winner was whatever order Postgres
    happened to return).
    """
    result = await session.execute(
        select(ScanConfig)
        .where(
            ScanConfig.project_id == project_id,
            ScanConfig.interval.isnot(None),
            ScanConfig.time_column.isnot(None),
        )
        .order_by(ScanConfig.created_at.desc(), ScanConfig.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _get_default_scan_config_id(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> uuid.UUID | None:
    config = await _get_default_scan_config(session, project_id)
    return config.id if config is not None else None


async def _resolve_scope_scan_config_id(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    event_id: uuid.UUID | None = None,
    event_type_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    metric_query = (
        select(EventMetric.scan_config_id, EventMetric.bucket)
        .join(ScanConfig, ScanConfig.id == EventMetric.scan_config_id)
        .where(ScanConfig.project_id == project_id)
    )
    anomaly_query = (
        select(MetricAnomaly.scan_config_id, MetricAnomaly.bucket)
        .join(ScanConfig, ScanConfig.id == MetricAnomaly.scan_config_id)
        .where(ScanConfig.project_id == project_id)
    )

    if event_id is not None:
        metric_query = metric_query.where(EventMetric.event_id == event_id)
        anomaly_query = anomaly_query.where(MetricAnomaly.event_id == event_id)
    elif event_type_id is not None:
        metric_query = metric_query.where(
            EventMetric.event_id.is_(None),
            EventMetric.event_type_id == event_type_id,
        )
        anomaly_query = anomaly_query.where(MetricAnomaly.event_type_id == event_type_id)
    else:
        return await _get_default_scan_config_id(session, project_id)

    metric_row = (
        await session.execute(metric_query.order_by(EventMetric.bucket.desc()).limit(1))
    ).first()
    anomaly_row = (
        await session.execute(anomaly_query.order_by(MetricAnomaly.bucket.desc()).limit(1))
    ).first()

    candidates: list[tuple[uuid.UUID, datetime]] = []
    if metric_row is not None:
        candidates.append((metric_row[0], metric_row[1]))
    if anomaly_row is not None:
        # The inner join on ``ScanConfig`` drops anomalies without a scan config.
        assert anomaly_row[0] is not None
        candidates.append((anomaly_row[0], anomaly_row[1]))
    if not candidates:
        return await _get_default_scan_config_id(session, project_id)

    scan_config_id, _bucket = max(candidates, key=lambda row: row[1])
    return scan_config_id


async def _resolve_events_metrics_scan_config(
    session: AsyncSession,
    project_id: uuid.UUID,
    conditions: list[ColumnElement[bool]],
) -> ScanConfig | None:
    """The single scan the "<Tab> Dynamics" series is charted from.

    Scoping to ONE scan is deliberate: summing EventMetric rows across every
    scan_config double-counts events that a legacy/backfill scan (an "Old events"
    config) also collected, and one inflated bucket then dominated the chart's
    y-axis.

    Picking WHICH one cannot be ``_get_default_scan_config`` alone, though. That
    answers "newest configured scan in the project" — right for the project-wide
    sparkline, wrong for a tab. acme-ios collects its ``se`` event type with
    "Snowplow Events (iOS)" while the newest config is "Snowplow Pageviews (iOS)",
    so the tab queried a scan that had never written a row for it and charted
    nothing at all, under 366 live events.

    So: keep the default whenever it has rows for these filters — the tab and the
    project total agreeing is the point — and only fall back to
    whichever scan does have them, most recent data first. The created_at/id
    tiebreak mirrors the default resolver, so the pick can never hang on Postgres
    row order.
    """
    default = await _get_default_scan_config(session, project_id)

    if default is not None:
        default_row = (
            await session.execute(
                select(EventMetric.bucket)
                .join(Event, EventMetric.event_id == Event.id)
                .where(*conditions, EventMetric.scan_config_id == default.id)
                .limit(1)
            )
        ).first()
        if default_row is not None:
            return default

    owners = (
        select(
            EventMetric.scan_config_id.label("scan_config_id"),
            func.max(EventMetric.bucket).label("last_bucket"),
        )
        .join(Event, EventMetric.event_id == Event.id)
        .where(*conditions)
        .group_by(EventMetric.scan_config_id)
        .subquery()
    )
    result = await session.execute(
        select(ScanConfig)
        .join(owners, ScanConfig.id == owners.c.scan_config_id)
        .order_by(
            owners.c.last_bucket.desc(),
            ScanConfig.created_at.desc(),
            ScanConfig.id.desc(),
        )
        .limit(1)
    )
    # Falling back to the default when nothing matched keeps the empty state
    # honest: "this scan has no volume here", not "no scan is configured".
    return result.scalar_one_or_none() or default


def _scope_metric_filters(
    *,
    scope: str,
    scan_config_id: uuid.UUID,
    scope_ref: str,
) -> list[ColumnElement[bool]]:
    """Which ``event_metrics`` rows belong to one drilldown scope.

    Shared by the series pull and the open-anchor probe below, so the probe can
    never decide "the scope has nothing newer than its anchor" off a different
    set of rows than the chart is drawn from.
    """
    if scope == SCOPE_PROJECT_TOTAL:
        return [
            EventMetric.scan_config_id == scan_config_id,
            EventMetric.event_id.is_(None),
            EventMetric.event_type_id.is_not(None),
        ]
    if scope == SCOPE_EVENT_TYPE:
        return [
            EventMetric.scan_config_id == scan_config_id,
            EventMetric.event_id.is_(None),
            EventMetric.event_type_id == uuid.UUID(scope_ref),
        ]
    return [
        EventMetric.scan_config_id == scan_config_id,
        EventMetric.event_id == uuid.UUID(scope_ref),
    ]


async def _get_metric_rows(
    session: AsyncSession,
    *,
    scope: str,
    scan_config_id: uuid.UUID,
    scope_ref: str,
    time_from: datetime | None,
    time_to: datetime | None,
) -> list[tuple[datetime, int]]:
    scope_filters = _scope_metric_filters(
        scope=scope,
        scan_config_id=scan_config_id,
        scope_ref=scope_ref,
    )
    if scope == SCOPE_PROJECT_TOTAL:
        query = (
            select(EventMetric.bucket, func.sum(EventMetric.count))
            .where(*scope_filters)
            .group_by(EventMetric.bucket)
            .order_by(EventMetric.bucket)
        )
    else:
        query = (
            select(EventMetric.bucket, EventMetric.count)
            .where(*scope_filters)
            .order_by(EventMetric.bucket)
        )

    if time_from is not None:
        query = query.where(EventMetric.bucket >= time_from)
    if time_to is not None:
        query = query.where(EventMetric.bucket < time_to)

    result = await session.execute(query)
    return [(bucket, int(count)) for bucket, count in result.all()]


async def _get_anomaly_rows(
    session: AsyncSession,
    *,
    scan_config_id: uuid.UUID,
    scope: str,
    scope_ref: str,
    time_from: datetime | None,
    time_to: datetime | None,
) -> list[MetricAnomaly]:
    query = (
        select(MetricAnomaly)
        .where(
            MetricAnomaly.scan_config_id == scan_config_id,
            MetricAnomaly.scope_type == scope,
            MetricAnomaly.scope_ref == scope_ref,
        )
        .order_by(MetricAnomaly.bucket)
    )
    if time_from is not None:
        query = query.where(MetricAnomaly.bucket >= time_from)
    if time_to is not None:
        query = query.where(MetricAnomaly.bucket < time_to)

    result = await session.execute(query)
    return list(result.scalars().all())


# One scored bucket's band: (expected value, floored effective stddev).
_BucketBaseline = tuple[float, float]


async def _get_baseline_rows(
    session: AsyncSession,
    *,
    scan_config_id: uuid.UUID,
    scope: str,
    scope_ref: str,
    time_from: datetime | None,
    time_to: datetime | None,
) -> dict[datetime, _BucketBaseline]:
    """The per-bucket baselines the detector stored for one scope.

    Keyed by bucket so ``_build_metric_points`` can attach one to every point.
    Buckets scored before baselines were persisted simply have no entry.
    """
    query = select(
        MetricBaseline.bucket,
        MetricBaseline.expected_count,
        MetricBaseline.effective_stddev,
    ).where(
        MetricBaseline.scan_config_id == scan_config_id,
        MetricBaseline.scope_type == scope,
        MetricBaseline.scope_ref == scope_ref,
    )
    if time_from is not None:
        query = query.where(MetricBaseline.bucket >= time_from)
    if time_to is not None:
        query = query.where(MetricBaseline.bucket < time_to)

    result = await session.execute(query)
    return {bucket: (expected, stddev) for bucket, expected, stddev in result.all()}


async def _get_scope_latest_metric_bucket(
    session: AsyncSession,
    *,
    scope: str,
    scan_config_id: uuid.UUID,
    scope_ref: str,
    since: datetime,
    time_to: datetime | None,
) -> datetime | None:
    """Newest bucket the scope stored at or after ``since``.

    ``since`` bounds the read — ``event_metrics`` is the largest table here and
    a guard test pins every query against it to a LIMIT or a bucket floor. It
    costs no accuracy for the one caller: it asks whether anything is newer than
    a candidate anchor, so rows older than that anchor cannot change the answer.
    """
    filters = _scope_metric_filters(
        scope=scope,
        scan_config_id=scan_config_id,
        scope_ref=scope_ref,
    )
    query = select(func.max(EventMetric.bucket)).where(*filters, EventMetric.bucket >= since)
    if time_to is not None:
        query = query.where(EventMetric.bucket < time_to)
    return (await session.execute(query)).scalar_one_or_none()


async def _load_scope_anomalies(
    session: AsyncSession,
    *,
    scope: str,
    scan_config_id: uuid.UUID,
    scope_ref: str,
    time_from: datetime | None,
    time_to: datetime | None,
    interval: str | None,
    recent_window: timedelta | None,
    scan_latest_bucket: datetime | None,
) -> tuple[list[MetricAnomaly], datetime | None]:
    """The scope's anomalies for the requested range, reaching back to an open anchor.

    Returns ``(anomalies, effective_time_from)``; the caller pulls its metric
    rows with the returned floor so chart and signal describe one range.

    An outage is announced ONCE, at onset, and deliberately never re-emitted, so
    a scope that has been silent for weeks is described by a single row that old.
    The Anomalies list, the sidebar badge and the bell all keep reporting it —
    ``_outage_is_still_running`` re-checks the anchor against the series instead
    of ageing it out — but the drilldown they link to loads only its selected
    range (7 days by default), which left the page for the incident the user had
    just clicked showing no signal at all.

    Widening is CONDITIONAL: only an anchor that still classifies open earns it.
    Reaching back to any older anomaly would quietly serve a different range than
    the user picked on every scope that has ever fired. The check runs the same
    ``classify_signal_state`` call, on the same inputs, that
    ``_build_metrics_response`` will run once the rows are loaded, so the probe
    and the response cannot disagree about what "open" means.
    """
    anomalies = await _get_anomaly_rows(
        session,
        scan_config_id=scan_config_id,
        scope=scope,
        scope_ref=scope_ref,
        time_from=time_from,
        time_to=time_to,
    )
    # A row inside the range is already the scope's newest (the range ends at
    # "now"), so it is the row every other surface classifies: nothing to reach
    # back for, and no extra query is run.
    if anomalies or time_from is None:
        return anomalies, time_from

    anchor = (
        await session.execute(
            select(MetricAnomaly)
            .where(
                MetricAnomaly.scan_config_id == scan_config_id,
                MetricAnomaly.scope_type == scope,
                MetricAnomaly.scope_ref == scope_ref,
                MetricAnomaly.bucket < time_from,
            )
            .order_by(MetricAnomaly.bucket.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if anchor is None:
        return anomalies, time_from

    # Mirrors what ``_build_metric_points`` will produce once the range starts at
    # the anchor: the densified series ends at the newest of the scope's metric
    # buckets and its anomaly buckets, and the anchor is one of the latter.
    stored_latest = await _get_scope_latest_metric_bucket(
        session,
        scope=scope,
        scan_config_id=scan_config_id,
        scope_ref=scope_ref,
        since=anchor.bucket,
        time_to=time_to,
    )
    latest_metric_bucket = (
        max(anchor.bucket, stored_latest) if stored_latest is not None else anchor.bucket
    )
    state = classify_signal_state(
        anomaly_bucket=anchor.bucket,
        latest_metric_bucket=latest_metric_bucket,
        interval=scan_interval_to_timedelta(interval),
        recent_window=recent_window,
        anomaly_actual_count=anchor.actual_count,
        anomaly_expected_count=anchor.expected_count,
        scan_latest_bucket=scan_latest_bucket,
    )
    if state is None:
        return anomalies, time_from

    return (
        await _get_anomaly_rows(
            session,
            scan_config_id=scan_config_id,
            scope=scope,
            scope_ref=scope_ref,
            time_from=anchor.bucket,
            time_to=time_to,
        ),
        anchor.bucket,
    )


def _signal_from_anomaly(
    anomaly: MetricAnomaly,
    *,
    state: str,
) -> MetricSignalResponse:
    """One stored anomaly as the signal every surface renders.

    ``stddev`` is the served (floored *effective*) one, not the raw column — the
    signal card sits next to the chart whose band is drawn from
    ``_served_stddev``, and the two reporting different widths for the same
    bucket is the disagreement ``_served_stddev`` exists to prevent.
    """
    return MetricSignalResponse(
        scan_config_id=anomaly.scan_config_id,
        scope_type=anomaly.scope_type,
        scope_ref=anomaly.scope_ref,
        state=state,
        event_id=anomaly.event_id,
        event_type_id=anomaly.event_type_id,
        bucket=anomaly.bucket,
        actual_count=anomaly.actual_count,
        expected_count=anomaly.expected_count,
        stddev=_served_stddev(anomaly),
        z_score=anomaly.z_score,
        direction=anomaly.direction,
        detected_at=anomaly.created_at,
        anomaly_id=getattr(anomaly, "id", None),
    )


def _served_stddev(anomaly: MetricAnomaly | MetricBreakdownAnomaly) -> float:
    """Stddev the UI band is drawn from: the FLOORED *effective* stddev actually
    used in the z denominator so ``expected ± k*stddev``
    matches the detector's flag decision. Falls back to the raw stored stddev
    when the ``effective_stddev`` column is absent (pre-migration rows / hand-
    built test objects), keeping existing behavior unchanged in that case.
    """
    effective = getattr(anomaly, "effective_stddev", None)
    if effective is None or effective == 0.0:
        return anomaly.stddev
    return float(effective)


def _served_detector_kind(anomaly: MetricAnomaly | MetricBreakdownAnomaly) -> str | None:
    """Which detector path flagged the bucket, or ``None`` when the column is
    absent (pre-migration rows / hand-built test objects)."""
    return getattr(anomaly, "detector_kind", None)


def _served_planned_event_id(
    anomaly: MetricAnomaly | MetricBreakdownAnomaly,
) -> uuid.UUID | None:
    """The planned event that expected the bucket (F18); breakdown rows carry none."""
    return getattr(anomaly, "planned_event_id", None)


def _baseline_fields(baseline: _BucketBaseline | None) -> dict[str, float | None]:
    """``EventMetricPoint`` baseline fields for one bucket, NULL when unscored."""
    if baseline is None:
        return {"baseline_expected": None, "baseline_stddev": None}
    expected, stddev = baseline
    return {"baseline_expected": expected, "baseline_stddev": stddev}


def _build_metric_points(
    *,
    interval: str | None,
    metric_rows: list[tuple[datetime, int]],
    anomalies: list[MetricAnomaly] | list[MetricBreakdownAnomaly],
    baselines: dict[datetime, _BucketBaseline] | None = None,
) -> list[EventMetricPoint]:
    counts_by_bucket = {bucket: count for bucket, count in metric_rows}
    anomalies_by_bucket = {anomaly.bucket: anomaly for anomaly in anomalies}
    # Attached to the points the series already has; a baseline never adds a
    # bucket of its own.
    baselines_by_bucket = baselines or {}

    for anomaly in anomalies:
        # Event-scope actuals are whole counts stored in a float column; round
        # keeps the int-typed volume series exact (catalog-metric series render
        # through metric_series_service, not this builder).
        counts_by_bucket.setdefault(anomaly.bucket, round(anomaly.actual_count))

    if interval and counts_by_bucket:
        delta = get_interval(interval).delta
        expanded = expand_series(
            [
                SeriesPoint(bucket=bucket, count=count)
                for bucket, count in sorted(counts_by_bucket.items())
            ],
            interval=delta,
            end_exclusive=max(counts_by_bucket) + delta,
        )
        data = [
            EventMetricPoint(
                bucket=point.bucket,
                count=point.count,
                expected_count=(
                    anomalies_by_bucket[point.bucket].expected_count
                    if point.bucket in anomalies_by_bucket
                    else None
                ),
                stddev=(
                    _served_stddev(anomalies_by_bucket[point.bucket])
                    if point.bucket in anomalies_by_bucket
                    else None
                ),
                detector_kind=(
                    _served_detector_kind(anomalies_by_bucket[point.bucket])
                    if point.bucket in anomalies_by_bucket
                    else None
                ),
                is_anomaly=point.bucket in anomalies_by_bucket,
                anomaly_direction=(
                    anomalies_by_bucket[point.bucket].direction
                    if point.bucket in anomalies_by_bucket
                    else None
                ),
                z_score=(
                    anomalies_by_bucket[point.bucket].z_score
                    if point.bucket in anomalies_by_bucket
                    else None
                ),
                planned_event_id=(
                    _served_planned_event_id(anomalies_by_bucket[point.bucket])
                    if point.bucket in anomalies_by_bucket
                    else None
                ),
                **_baseline_fields(baselines_by_bucket.get(point.bucket)),
            )
            for point in expanded
        ]
    else:
        data = [
            EventMetricPoint(
                bucket=bucket,
                count=count,
                expected_count=(
                    anomalies_by_bucket[bucket].expected_count
                    if bucket in anomalies_by_bucket
                    else None
                ),
                stddev=(
                    _served_stddev(anomalies_by_bucket[bucket])
                    if bucket in anomalies_by_bucket
                    else None
                ),
                detector_kind=(
                    _served_detector_kind(anomalies_by_bucket[bucket])
                    if bucket in anomalies_by_bucket
                    else None
                ),
                is_anomaly=bucket in anomalies_by_bucket,
                anomaly_direction=(
                    anomalies_by_bucket[bucket].direction if bucket in anomalies_by_bucket else None
                ),
                z_score=(
                    anomalies_by_bucket[bucket].z_score if bucket in anomalies_by_bucket else None
                ),
                planned_event_id=(
                    _served_planned_event_id(anomalies_by_bucket[bucket])
                    if bucket in anomalies_by_bucket
                    else None
                ),
                **_baseline_fields(baselines_by_bucket.get(bucket)),
            )
            for bucket, count in sorted(counts_by_bucket.items())
        ]

    return data


# Longest densified series a request will fit a forecast on. Shared with
# ``metric_series_service``, which imports it.
#
# ``forecast_next_buckets`` runs its OWN robust STL/MSTL fit, and on this
# hardware that costs ~8.5 ms for a 7d hourly series (STL, period 24) but 1.75 s
# at 720 points and 5.3 s at 2160 — an MSTL over periods 24 and 168. The cap is
# therefore a budget on the FIT, and 400 is where the fit is still cheap.
#
# What it covers, stated against the ranges the UI actually offers (7d / 30d /
# 90d, ``RANGE_OPTIONS`` in frontend/src/lib/metrics.ts). ``chartForecast``
# (MonitoringDetailPage) draws the dashed tail only when the display granularity
# equals the collection interval, and ``defaultGranularityForRange`` picks hour
# at 7d, day at 30d, week at 90d. So at the DEFAULT granularity the tail is drawn
# on 168 points (7d at 1h), 30 (30d at 1d) or ~13 (90d at 1w) — every one under
# the cap. A 6h scan is under it at every range too (360 points at 90d).
#
# What it COSTS, because it is not nothing: a manual granularity pick wins over
# the default and stays sticky across range changes, so a reader who pins the
# native granularity keeps the tail rendered at any range. Those series are 672
# points (7d at 15m), 720 (30d at 1h), 2160 (90d at 1h) and 8640 (90d at 15m) —
# all over the cap, so the forecast comes back empty and the tail they would
# have seen is not drawn. That is deliberate: the smallest bound that buys back
# the two cheapest of those is ~750, which costs 1.75 s of request CPU per
# drilldown and still leaves the other two empty. Raising the ceiling belongs
# with a cached or precomputed fit, not with a bigger number here.
_FORECAST_MAX_POINTS = 400


def _forecast_from_points(
    *,
    data: list[EventMetricPoint],
    interval: str | None,
) -> list[ForecastPoint]:
    """One-step-ahead forecast off the densified series the chart already shows.

    Returns an empty list when the series is too short to fit STL/MSTL or longer
    than ``_FORECAST_MAX_POINTS`` — the UI draws no dashed preview rather than a
    flat line. On the long side that is a real, deliberate loss for a reader who
    has pinned the native granularity; the cap's own comment says which views.

    CPU-bound (numpy/statsmodels), so async callers reach it through
    ``_forecast_off_event_loop`` rather than calling it directly.
    """
    if not data or not interval or len(data) > _FORECAST_MAX_POINTS:
        return []
    delta = get_interval(interval).delta
    series_points = [SeriesPoint(bucket=point.bucket, count=point.count) for point in data]
    forecasted = forecast_next_buckets(series_points, interval=delta, horizon=1)
    return [
        ForecastPoint(
            bucket=point.bucket,
            expected_count=point.expected_count,
            stddev=point.stddev,
        )
        for point in forecasted
    ]


async def _forecast_off_event_loop(
    *,
    data: list[EventMetricPoint],
    interval: str | None,
) -> list[ForecastPoint]:
    """``_forecast_from_points``, kept off the event loop.

    The STL/MSTL fit is pure CPU. Run inline on the async request path it parks
    the whole uvicorn worker for the length of the fit, so every unrelated
    request multiplexed onto that worker waits behind one chart's dashed tail.
    The cheap exits are taken here so the common case never pays for a thread
    hop; only an actual fit is handed to the executor.
    """
    if not data or not interval or len(data) > _FORECAST_MAX_POINTS:
        return []
    return await asyncio.to_thread(_forecast_from_points, data=data, interval=interval)


async def _get_scan_latest_buckets(
    session: AsyncSession,
    scan_config_ids: set[uuid.UUID],
    *,
    since: datetime,
) -> dict[uuid.UUID, datetime]:
    """Newest collected bucket per scan config — the "is this collector alive" probe.

    Feeds ``classify_signal_state``'s ``scan_latest_bucket`` on the drilldown
    path. Taken across every scope of the scan (not just the one being drawn),
    because a scope that has gone silent stops producing rows entirely: asking
    its own series whether the collector is alive would always answer "no".
    Shares ``latest_bucket_by_scan`` with the list path so the two surfaces
    cannot derive scan liveness by different rules.

    ``since`` bounds the read — ``event_metrics`` is the largest table here and
    a guard test pins every query against it to a LIMIT or a bucket floor. It
    costs no accuracy: anything older than ``scan_liveness_cutoff`` fails the
    freshness check downstream, so an empty result and a stale maximum lead to
    the same verdict.
    """
    if not scan_config_ids:
        return {}
    rows = await session.execute(
        select(EventMetric.scan_config_id, func.max(EventMetric.bucket))
        .where(
            EventMetric.scan_config_id.in_(scan_config_ids),
            EventMetric.bucket >= since,
        )
        .group_by(EventMetric.scan_config_id)
    )
    return latest_bucket_by_scan((scan_config_id, bucket) for scan_config_id, bucket in rows.all())


async def _get_scan_latest_bucket(
    session: AsyncSession,
    scan_config_id: uuid.UUID | None,
    *,
    interval: str | None,
    recent_window: timedelta | None,
) -> datetime | None:
    if scan_config_id is None:
        return None
    buckets = await _get_scan_latest_buckets(
        session,
        {scan_config_id},
        since=scan_liveness_cutoff(interval=interval, recent_window=recent_window),
    )
    return buckets.get(scan_config_id)


async def _get_collection_timing(
    session: AsyncSession,
    scan_config_id: uuid.UUID,
    *,
    interval: str | None,
    scan_latest_bucket: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    """``(last_collected_at, next_collection_at)`` for a drilldown's scan config (L4).

    Answered by the scheduler's own pure functions rather than a port of its
    rule, so the header can never say "next run 14:00" for a config the
    dispatcher considers due now. ``scan_latest_bucket`` is the liveness probe
    the drilldown already read: it is bounded to ``scan_liveness_cutoff``, at
    least three intervals back, and a newest bucket older than that leaves the
    config due whatever its exact value — so no second ``event_metrics`` read.
    The job read is the one ``_last_collected_window_to`` makes, capped the same
    way. Imported lazily, like ``metric_definition_service``'s scheduler read,
    to keep the request path out of the Celery import graph until it is used.
    """
    from tripl.worker.tasks.metrics.schedule import (
        scan_config_collection_progress,
        scan_config_collection_schedule,
    )
    from tripl.worker.tasks.metrics.tasks import _RECENT_JOB_SCAN_LIMIT

    rows = await session.execute(
        select(ScanJob.result_summary, ScanJob.completed_at)
        .where(
            ScanJob.scan_config_id == scan_config_id,
            ScanJob.status == ScanJobStatus.completed.value,
        )
        .order_by(ScanJob.created_at.desc())
        .limit(_RECENT_JOB_SCAN_LIMIT)
    )
    last_collected_at, watermark = scan_config_collection_progress(rows.tuples().all())
    delta = scan_interval_to_timedelta(interval)
    if delta is None:
        return last_collected_at, None
    # The dispatcher collects only a config with an interval AND a time column,
    # as ``scan_config_service._metrics_schedule`` says on the scan page — so a
    # config without one has no next collection here either.
    time_column = await session.scalar(
        select(ScanConfig.time_column).where(ScanConfig.id == scan_config_id)
    )
    if not time_column:
        return last_collected_at, None
    next_collection_at, _due = scan_config_collection_schedule(
        last_bucket=scan_latest_bucket,
        watermark=watermark,
        delta=delta,
        now=datetime.now(UTC),
    )
    return last_collected_at, next_collection_at


async def _build_metrics_response(
    *,
    scope: str,
    scan_config_id: uuid.UUID | None,
    scope_ref: str | None,
    interval: str | None,
    metric_rows: list[tuple[datetime, int]],
    anomalies: list[MetricAnomaly],
    baselines: dict[datetime, _BucketBaseline] | None = None,
    event_id: uuid.UUID | None = None,
    event_type_id: uuid.UUID | None = None,
    scan_config_name: str | None = None,
    sigma_threshold: float = DEFAULT_SIGMA_THRESHOLD,
    recent_window: timedelta | None = None,
    scan_latest_bucket: datetime | None = None,
    with_forecast: bool = True,
    collection_timing: tuple[datetime | None, datetime | None] = (None, None),
) -> EventMetricsResponse:
    """Shape one drilldown response. Async ONLY because of the forecast fit,
    which ``_forecast_off_event_loop`` hands to a worker thread; callers that
    throw the forecast away should pass ``with_forecast=False`` instead of
    paying for it."""
    data = _build_metric_points(
        interval=interval,
        metric_rows=metric_rows,
        anomalies=anomalies,
        baselines=baselines,
    )
    latest_signal = None
    latest_metric_bucket = data[-1].bucket if data else None
    if anomalies:
        latest_anomaly = anomalies[-1]
        state = classify_signal_state(
            anomaly_bucket=latest_anomaly.bucket,
            latest_metric_bucket=latest_metric_bucket,
            # Without the grid, a coarse series is judged against a bare 24h
            # window narrower than one of its own buckets, and closes.
            interval=scan_interval_to_timedelta(interval),
            recent_window=recent_window,
            # This response IS the drilldown the Anomalies page links to, so it
            # has to answer the ongoing-outage re-check the same way the list
            # does. Omitting these two left the list reporting a five-day-old
            # outage as open while the page it opened showed no signal at all.
            # Every scope served here is count-shaped and scan-backed, which is
            # exactly the population the re-check is defined for.
            anomaly_actual_count=latest_anomaly.actual_count,
            anomaly_expected_count=latest_anomaly.expected_count,
            scan_latest_bucket=scan_latest_bucket,
        )
        if state is not None:
            latest_signal = _signal_from_anomaly(latest_anomaly, state=state)

    forecast: list[ForecastPoint] = []
    if with_forecast:
        forecast = await _forecast_off_event_loop(data=data, interval=interval)

    return EventMetricsResponse(
        scope=scope,
        scan_config_id=scan_config_id,
        scan_config_name=scan_config_name,
        event_id=event_id,
        event_type_id=event_type_id,
        interval=interval,
        latest_signal=latest_signal,
        sigma_threshold=sigma_threshold,
        last_collected_at=collection_timing[0],
        next_collection_at=collection_timing[1],
        data=data,
        forecast=forecast,
    )


async def get_event_metrics(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> EventMetricsResponse:
    project = await resolve_project(session, slug)
    event = await _resolve_event(session, project.id, event_id)
    scan_config_id = await _resolve_scope_scan_config_id(
        session,
        project.id,
        event_id=event.id,
    )
    interval = await _get_scan_config_interval(session, scan_config_id)
    if scan_config_id is None:
        return EventMetricsResponse(
            scope=SCOPE_EVENT,
            event_id=event.id,
            interval=interval,
            data=[],
        )

    sigma_threshold = await _apply_scope_sigma_override(
        session,
        project_id=project.id,
        scan_config_id=scan_config_id,
        scope_type=SCOPE_EVENT,
        scope_ref=str(event.id),
        fallback=await _get_project_sigma_threshold(session, project.id),
    )
    recent_window = await _get_project_recent_signal_window(session, project.id)
    scan_latest_bucket = await _get_scan_latest_bucket(
        session, scan_config_id, interval=interval, recent_window=recent_window
    )
    anomalies, effective_time_from = await _load_scope_anomalies(
        session,
        scope=SCOPE_EVENT,
        scan_config_id=scan_config_id,
        scope_ref=str(event.id),
        time_from=time_from,
        time_to=time_to,
        interval=interval,
        recent_window=recent_window,
        scan_latest_bucket=scan_latest_bucket,
    )
    metric_rows = await _get_metric_rows(
        session,
        scope=SCOPE_EVENT,
        scan_config_id=scan_config_id,
        scope_ref=str(event.id),
        time_from=effective_time_from,
        time_to=time_to,
    )
    baselines = await _get_baseline_rows(
        session,
        scan_config_id=scan_config_id,
        scope=SCOPE_EVENT,
        scope_ref=str(event.id),
        time_from=effective_time_from,
        time_to=time_to,
    )
    collection_timing = await _get_collection_timing(
        session, scan_config_id, interval=interval, scan_latest_bucket=scan_latest_bucket
    )
    response = await _build_metrics_response(
        scope=SCOPE_EVENT,
        scan_config_id=scan_config_id,
        scope_ref=str(event.id),
        interval=interval,
        metric_rows=metric_rows,
        anomalies=anomalies,
        baselines=baselines,
        event_id=event.id,
        sigma_threshold=sigma_threshold,
        recent_window=recent_window,
        scan_latest_bucket=scan_latest_bucket,
        collection_timing=collection_timing,
    )
    response = await signal_verdict_service.with_chart_verdicts(
        session, project.id, response, scope_type=SCOPE_EVENT, scope_ref=str(event.id)
    )
    return await anomaly_attribution_service.with_latest_signal_attribution(
        session, response, project_id=project.id
    )


async def get_event_metric_breakdowns(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    column: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> EventMetricBreakdownsResponse:
    project = await resolve_project(session, slug)
    event = await _resolve_event(session, project.id, event_id)
    scan_config_id = await _resolve_scope_scan_config_id(
        session,
        project.id,
        event_id=event.id,
    )
    event_columns = list(dict.fromkeys(event.metric_breakdown_columns or []))
    if scan_config_id is None:
        if column is not None and column not in event_columns:
            raise HTTPException(400, "Breakdown column is not configured for this event")
        return EventMetricBreakdownsResponse(
            event_id=event.id,
            columns=event_columns,
            selected_column=column if column in event_columns else None,
            series=[],
        )

    config = await session.get(ScanConfig, scan_config_id)
    if config is None or config.project_id != project.id:
        if column is not None and column not in event_columns:
            raise HTTPException(400, "Breakdown column is not configured for this event")
        return EventMetricBreakdownsResponse(
            event_id=event.id,
            columns=event_columns,
            selected_column=column if column in event_columns else None,
            series=[],
        )

    # The scan's platform_column belongs in this list even though nobody listed
    # it: the collector gathers it as a scan-level breakdown on its own
    # (worker/tasks/metrics/metric_rows.py) and the scan validator REFUSES to
    # let it appear in metric_breakdown_columns, so without this line the stored
    # per-platform series and its parity anomalies were unreachable — columns=[]
    # and a 400 on ?column=<platform>. Appended last so an
    # explicitly configured column still wins the default selection below.
    platform_columns = [config.platform_column] if config.platform_column else []
    columns = list(
        dict.fromkeys([*(config.metric_breakdown_columns or []), *event_columns, *platform_columns])
    )
    if not columns:
        return EventMetricBreakdownsResponse(
            event_id=event.id,
            scan_config_id=scan_config_id,
            interval=config.interval,
            columns=[],
            series=[],
        )

    if column is not None and column not in columns:
        raise HTTPException(400, "Breakdown column is not configured for this event")

    selected_column = column
    if selected_column is None:
        data_column_query = select(EventMetricBreakdown.breakdown_column).where(
            EventMetricBreakdown.scan_config_id == scan_config_id,
            EventMetricBreakdown.event_id == event.id,
        )
        if time_from is not None:
            data_column_query = data_column_query.where(EventMetricBreakdown.bucket >= time_from)
        if time_to is not None:
            data_column_query = data_column_query.where(EventMetricBreakdown.bucket < time_to)
        data_columns = set((await session.execute(data_column_query.distinct())).scalars())
        selected_column = next((item for item in columns if item in data_columns), columns[0])

    metric_query = (
        select(
            EventMetricBreakdown.breakdown_value,
            EventMetricBreakdown.is_other,
            EventMetricBreakdown.bucket,
            func.sum(EventMetricBreakdown.count),
        )
        .where(
            EventMetricBreakdown.scan_config_id == scan_config_id,
            EventMetricBreakdown.event_id == event.id,
            EventMetricBreakdown.breakdown_column == selected_column,
        )
        .group_by(
            EventMetricBreakdown.breakdown_value,
            EventMetricBreakdown.is_other,
            EventMetricBreakdown.bucket,
        )
        .order_by(EventMetricBreakdown.breakdown_value, EventMetricBreakdown.bucket)
    )
    if time_from is not None:
        metric_query = metric_query.where(EventMetricBreakdown.bucket >= time_from)
    if time_to is not None:
        metric_query = metric_query.where(EventMetricBreakdown.bucket < time_to)

    metric_rows_by_series: dict[tuple[str, bool], list[tuple[datetime, int]]] = {}
    for value, is_other, bucket, count in (await session.execute(metric_query)).all():
        key = (value, bool(is_other))
        metric_rows_by_series.setdefault(key, []).append((bucket, int(count)))

    anomaly_query = (
        select(MetricBreakdownAnomaly)
        .where(
            MetricBreakdownAnomaly.scan_config_id == scan_config_id,
            MetricBreakdownAnomaly.scope_type == SCOPE_EVENT,
            MetricBreakdownAnomaly.event_id == event.id,
            MetricBreakdownAnomaly.breakdown_column == selected_column,
        )
        .order_by(MetricBreakdownAnomaly.breakdown_value, MetricBreakdownAnomaly.bucket)
    )
    if time_from is not None:
        anomaly_query = anomaly_query.where(MetricBreakdownAnomaly.bucket >= time_from)
    if time_to is not None:
        anomaly_query = anomaly_query.where(MetricBreakdownAnomaly.bucket < time_to)

    anomalies_by_series: dict[tuple[str, bool], list[MetricBreakdownAnomaly]] = {}
    parity_by_series: dict[tuple[str, bool], list[MetricBreakdownAnomaly]] = {}
    for anomaly in (await session.execute(anomaly_query)).scalars():
        key = (anomaly.breakdown_value, anomaly.is_other)
        target = (
            parity_by_series
            if anomaly.kind == MetricBreakdownAnomalyKind.parity
            else anomalies_by_series
        )
        target.setdefault(key, []).append(anomaly)

    series: list[EventMetricBreakdownSeries] = []
    for key in set(metric_rows_by_series) | set(anomalies_by_series) | set(parity_by_series):
        value, is_other = key
        data = _build_metric_points(
            interval=config.interval,
            metric_rows=metric_rows_by_series.get(key, []),
            anomalies=anomalies_by_series.get(key, []),
        )
        series.append(
            EventMetricBreakdownSeries(
                breakdown_value=value,
                is_other=is_other,
                total_count=sum(point.count for point in data),
                data=data,
                parity_anomalies=[
                    PlatformParityAnomaly(
                        bucket=anomaly.bucket,
                        actual_share=anomaly.actual_count,
                        expected_share=anomaly.expected_count,
                        stddev=anomaly.stddev,
                        z_score=anomaly.z_score,
                        direction=anomaly.direction,
                    )
                    for anomaly in parity_by_series.get(key, [])
                ],
            )
        )

    series.sort(key=lambda item: (item.is_other, -item.total_count, item.breakdown_value))
    return EventMetricBreakdownsResponse(
        event_id=event.id,
        scan_config_id=scan_config_id,
        interval=config.interval,
        columns=columns,
        selected_column=selected_column,
        series=series,
    )


def _validate_app_version_scope_ref(
    *,
    scope_type: str,
    scope_ref: str | None,
    scan_config_id: uuid.UUID,
) -> str:
    if scope_type == SCOPE_PROJECT_TOTAL:
        return str(scan_config_id)
    if scope_type in {SCOPE_EVENT_TYPE, SCOPE_EVENT}:
        if not scope_ref:
            raise HTTPException(400, "scope_ref is required for event and event_type scopes")
        try:
            return str(uuid.UUID(scope_ref))
        except ValueError as exc:
            raise HTTPException(400, "scope_ref must be a UUID") from exc
    raise HTTPException(400, "scope_type must be project_total, event_type, or event")


async def _resolve_app_version_scope(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    scope_type: str,
    scope_ref: str,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    if scope_type == SCOPE_EVENT:
        event = await _resolve_event(session, project_id, uuid.UUID(scope_ref))
        return event.id, None
    if scope_type == SCOPE_EVENT_TYPE:
        event_type = await _resolve_event_type(session, project_id, uuid.UUID(scope_ref))
        return None, event_type.id
    return None, None


def _order_app_version_keys(keys: set[tuple[str, bool]]) -> list[tuple[str, bool]]:
    explicit_versions = {version for version, is_other in keys if not is_other}
    ordered = [(version, False) for version in order_versions(explicit_versions, reverse=True)]
    other_versions = sorted((version, is_other) for version, is_other in keys if is_other)
    return ordered + other_versions


def _latest_app_version(
    keys: set[tuple[str, bool]], released: set[str] | None = None
) -> str | None:
    explicit_versions = {version for version, is_other in keys if not is_other}
    if released is not None:
        explicit_versions &= released
    ordered = order_versions(explicit_versions, reverse=True)
    return ordered[0] if ordered else None


def _retained_versions(
    explicit_versions: set[str],
    active_versions: set[str],
    keep_releases: int,
    *,
    released: set[str] | None = None,
) -> set[str]:
    """Latest ``keep_releases`` versions, prioritizing released, active versions.

    Retention slots are filled in priority order: released activated releases
    (newest first), then released-but-inactive, then prerelease/dev builds. This
    stops a higher-SemVer dev/tester build that never took real traffic — or a
    SemVer-prerelease build — from occupying a retention slot ahead of a shipped
    release (which would otherwise fold that release into "Other"). ``released``
    defaults to "everything is released", degrading to the prior active-first
    SemVer top-N and matching the previous behavior.
    """
    if keep_releases <= 0:
        return set()
    ordered = order_versions(explicit_versions, reverse=True)
    released_set = released if released is not None else set(explicit_versions)
    released_active = [v for v in ordered if v in released_set and v in active_versions]
    released_inactive = [v for v in ordered if v in released_set and v not in active_versions]
    prerelease = [v for v in ordered if v not in released_set]
    return set((released_active + released_inactive + prerelease)[:keep_releases])


async def _load_app_version_metric_rows(
    session: AsyncSession,
    *,
    config: ScanConfig,
    scope_type: str,
    event_id: uuid.UUID | None,
    event_type_id: uuid.UUID | None,
    time_from: datetime | None,
    time_to: datetime | None,
) -> dict[tuple[str, bool], list[tuple[datetime, int]]]:
    if config.app_version_column is None:
        return {}

    metric_query = (
        select(
            EventMetricBreakdown.breakdown_value,
            EventMetricBreakdown.is_other,
            EventMetricBreakdown.bucket,
            func.sum(EventMetricBreakdown.count),
        )
        .where(
            EventMetricBreakdown.scan_config_id == config.id,
            EventMetricBreakdown.breakdown_column == config.app_version_column,
        )
        .group_by(
            EventMetricBreakdown.breakdown_value,
            EventMetricBreakdown.is_other,
            EventMetricBreakdown.bucket,
        )
        .order_by(EventMetricBreakdown.bucket)
    )

    if scope_type == SCOPE_PROJECT_TOTAL:
        metric_query = metric_query.where(
            EventMetricBreakdown.event_id.is_(None),
            EventMetricBreakdown.event_type_id.is_not(None),
        )
    elif scope_type == SCOPE_EVENT_TYPE:
        metric_query = metric_query.where(
            EventMetricBreakdown.event_id.is_(None),
            EventMetricBreakdown.event_type_id == event_type_id,
        )
    else:
        metric_query = metric_query.where(EventMetricBreakdown.event_id == event_id)

    if time_from is not None:
        metric_query = metric_query.where(EventMetricBreakdown.bucket >= time_from)
    if time_to is not None:
        metric_query = metric_query.where(EventMetricBreakdown.bucket < time_to)

    metric_rows_by_series: dict[tuple[str, bool], list[tuple[datetime, int]]] = {}
    for version, is_other, bucket, count in (await session.execute(metric_query)).all():
        key = (str(version), bool(is_other))
        metric_rows_by_series.setdefault(key, []).append((bucket, int(count)))
    return metric_rows_by_series


def _build_app_version_series(
    *,
    interval: str | None,
    metric_rows_by_series: dict[tuple[str, bool], list[tuple[datetime, int]]],
    maturity_metric_rows_by_series: (
        dict[tuple[str, bool], list[tuple[datetime, int]]] | None
    ) = None,
    keep_releases: int,
    prerelease_pattern: re.Pattern[str] | None = None,
    share_min: float = DEFAULT_ACTIVE_SHARE_MIN,
) -> tuple[str | None, list[AppVersionInfo], list[AppVersionMetricSeries]]:
    # App-version curves are observational rollout/adoption views. They never
    # consume generic breakdown anomaly rows; release-specific failures are
    # represented by the dedicated ReleaseRegression model instead.
    # Retention is applied here, at read time, over every version present in the
    # requested window: keep the latest ``keep_releases`` versions — activated
    # releases first (see below) — as explicit series and roll the rest, plus any
    # legacy stored ``is_other`` rows, into a single "Other" series. Deciding the
    # kept set once over the whole window (rather than per collection chunk at
    # write time) stops a version from flipping in and out of "Other" between
    # chunks.
    explicit_versions = {version for version, is_other in metric_rows_by_series if not is_other}
    # Activation gate: a release can only be the "latest" — and can only hold a
    # retention slot ahead of an untried build — once it takes a real share of
    # project traffic. Event and event-type views supply their project's total
    # rows separately, so a rare event cannot make an otherwise mature release
    # look like a pre-release. Falling back keeps historical views usable until
    # a project-total row has been collected.
    maturity_rows_by_series = maturity_metric_rows_by_series or metric_rows_by_series
    release_totals_by_version: dict[str, dict[datetime, int]] = {}
    all_traffic_by_bucket: dict[datetime, int] = {}
    for (version, is_other), rows in maturity_rows_by_series.items():
        for bucket, count in rows:
            all_traffic_by_bucket[bucket] = all_traffic_by_bucket.get(bucket, 0) + count
            if not is_other:
                per_bucket = release_totals_by_version.setdefault(version, {})
                per_bucket[bucket] = per_bucket.get(bucket, 0) + count

    # Prerelease/dev builds (SemVer -tag by default, plus any per-scan pattern
    # match) are never eligible to be latest/active and never take a retention
    # slot ahead of a released version — but their series stays visible. Computing
    # ``active`` over only released versions keeps the denominator (all traffic)
    # intact while guaranteeing a prerelease is never marked active.
    released = released_versions(explicit_versions, prerelease_pattern=prerelease_pattern)
    maturity_released = released_versions(
        {version for version, is_other in maturity_rows_by_series if not is_other},
        prerelease_pattern=prerelease_pattern,
    )
    released_totals = {
        version: by_bucket
        for version, by_bucket in release_totals_by_version.items()
        if version in maturity_released
    }
    active_versions = (
        active_release_versions(released_totals, all_traffic_by_bucket, share_min=share_min)
        & explicit_versions
    )
    # Retain the latest ``keep_releases`` versions, released+active first, so a
    # higher-SemVer dev/prerelease build cannot push a shipped release into "Other".
    kept_versions = _retained_versions(
        explicit_versions, active_versions, keep_releases, released=released
    )

    def _display_key(version: str, is_other: bool) -> tuple[str, bool]:
        if not is_other and version in kept_versions:
            return (version, False)
        return (APP_VERSION_OTHER_LABEL, True)

    folded_metric_rows: dict[tuple[str, bool], dict[datetime, int]] = {}
    for (version, is_other), rows in metric_rows_by_series.items():
        bucket_counts = folded_metric_rows.setdefault(_display_key(version, is_other), {})
        for bucket, count in rows:
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + count
    metric_rows_by_display: dict[tuple[str, bool], list[tuple[datetime, int]]] = {
        key: sorted(counts.items()) for key, counts in folded_metric_rows.items()
    }

    keys = set(metric_rows_by_display)
    ordered_keys = _order_app_version_keys(keys)

    # Prefer the SemVer-max ACTIVE released version; fall back to the SemVer-max
    # released version only when nothing has activated yet (a young project still
    # ramping). Prereleases are excluded from both, so they are never latest.
    latest_version = (
        order_versions(active_versions, reverse=True)[0]
        if active_versions
        else _latest_app_version(keys, released)
    )

    def _is_active(version: str, is_other: bool) -> bool:
        return not is_other and version in active_versions

    versions = [
        AppVersionInfo(
            version=version,
            is_other=is_other,
            is_latest=not is_other and version == latest_version,
            is_active=_is_active(version, is_other),
        )
        for version, is_other in ordered_keys
    ]

    series: list[AppVersionMetricSeries] = []
    for version, is_other in ordered_keys:
        key = (version, is_other)
        data = _build_metric_points(
            interval=interval,
            metric_rows=metric_rows_by_display.get(key, []),
            anomalies=[],
        )
        series.append(
            AppVersionMetricSeries(
                version=version,
                is_other=is_other,
                is_latest=not is_other and version == latest_version,
                is_active=_is_active(version, is_other),
                total_count=sum(point.count for point in data),
                data=data,
            )
        )
    return latest_version, versions, series


def _build_app_version_totals(
    metric_rows_by_series: dict[tuple[str, bool], list[tuple[datetime, int]]],
) -> list[BreakdownTimelinePoint]:
    counts_by_bucket: dict[datetime, int] = {}
    for rows in metric_rows_by_series.values():
        for bucket, count in rows:
            counts_by_bucket[bucket] = counts_by_bucket.get(bucket, 0) + count
    return [
        BreakdownTimelinePoint(bucket=bucket, count=count)
        for bucket, count in sorted(counts_by_bucket.items())
    ]


async def get_app_version_series(
    session: AsyncSession,
    slug: str,
    *,
    scan_config_id: uuid.UUID,
    scope_type: str = SCOPE_PROJECT_TOTAL,
    scope_ref: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> AppVersionSeriesResponse:
    project = await resolve_project(session, slug)
    config = await _resolve_scan_config(session, project.id, scan_config_id)
    resolved_scope_ref = _validate_app_version_scope_ref(
        scope_type=scope_type,
        scope_ref=scope_ref,
        scan_config_id=config.id,
    )
    if not config.app_version_column:
        return AppVersionSeriesResponse(
            scan_config_id=config.id,
            scope_type=scope_type,
            scope_ref=resolved_scope_ref,
            app_version_column=None,
            interval=config.interval,
            versions=[],
            series=[],
        )

    event_id, event_type_id = await _resolve_app_version_scope(
        session,
        project_id=project.id,
        scope_type=scope_type,
        scope_ref=resolved_scope_ref,
    )
    metric_rows_by_series = await _load_app_version_metric_rows(
        session,
        config=config,
        scope_type=scope_type,
        event_id=event_id,
        event_type_id=event_type_id,
        time_from=time_from,
        time_to=time_to,
    )
    maturity_metric_rows_by_series = (
        metric_rows_by_series
        if scope_type == SCOPE_PROJECT_TOTAL
        else await _load_app_version_metric_rows(
            session,
            config=config,
            scope_type=SCOPE_PROJECT_TOTAL,
            event_id=None,
            event_type_id=None,
            time_from=time_from,
            time_to=time_to,
        )
    )
    latest_version, versions, series = _build_app_version_series(
        interval=config.interval,
        metric_rows_by_series=metric_rows_by_series,
        maturity_metric_rows_by_series=maturity_metric_rows_by_series,
        keep_releases=project.app_version_keep_releases,
        prerelease_pattern=compile_prerelease_pattern(config.app_version_prerelease_pattern),
        share_min=resolve_share_min(config.app_version_active_share_min),
    )
    return AppVersionSeriesResponse(
        scan_config_id=config.id,
        scope_type=scope_type,
        scope_ref=resolved_scope_ref,
        event_id=event_id,
        event_type_id=event_type_id,
        app_version_column=config.app_version_column,
        interval=config.interval,
        latest_version=latest_version,
        # The project setting, not ``config.sigma_threshold``: see
        # ``_get_project_sigma_threshold``. No scope override is applied — these
        # per-version series are not a scored scope, so there is no override row
        # keyed to them.
        sigma_threshold=await _get_project_sigma_threshold(session, project.id),
        versions=versions,
        series=series,
    )


async def get_app_version_adoption(
    session: AsyncSession,
    slug: str,
    *,
    scan_config_id: uuid.UUID,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> AppVersionAdoptionResponse:
    project = await resolve_project(session, slug)
    config = await _resolve_scan_config(session, project.id, scan_config_id)
    scope_ref = str(config.id)
    if not config.app_version_column:
        return AppVersionAdoptionResponse(
            scan_config_id=config.id,
            scope_type=SCOPE_PROJECT_TOTAL,
            scope_ref=scope_ref,
            app_version_column=None,
            interval=config.interval,
            versions=[],
            series=[],
            totals=[],
        )

    metric_rows_by_series = await _load_app_version_metric_rows(
        session,
        config=config,
        scope_type=SCOPE_PROJECT_TOTAL,
        event_id=None,
        event_type_id=None,
        time_from=time_from,
        time_to=time_to,
    )
    latest_version, versions, series = _build_app_version_series(
        interval=config.interval,
        metric_rows_by_series=metric_rows_by_series,
        keep_releases=project.app_version_keep_releases,
        prerelease_pattern=compile_prerelease_pattern(config.app_version_prerelease_pattern),
        share_min=resolve_share_min(config.app_version_active_share_min),
    )
    return AppVersionAdoptionResponse(
        scan_config_id=config.id,
        scope_type=SCOPE_PROJECT_TOTAL,
        scope_ref=scope_ref,
        app_version_column=config.app_version_column,
        interval=config.interval,
        latest_version=latest_version,
        # As in ``get_app_version_series``: the project setting is the sigma the
        # detector actually scored these buckets with.
        sigma_threshold=await _get_project_sigma_threshold(session, project.id),
        versions=versions,
        series=series,
        totals=_build_app_version_totals(metric_rows_by_series),
    )


async def get_release_regressions(
    session: AsyncSession,
    slug: str,
    *,
    scan_config_id: uuid.UUID,
    scope_type: str | None = None,
) -> ReleaseRegressionsResponse:
    """Events that changed in the latest active release, per scope.

    Backed by the ReleaseRegression rows the scan recomputes each run (they
    always describe the current latest release). Empty when the scan has no
    version column.

    ``comparability`` carries the verdict of the same pass, so an empty
    ``items`` can be read as either "nothing regressed" or "the release cannot
    be judged yet"; without it the two payloads are identical.
    """
    project = await resolve_project(session, slug)
    config = await _resolve_scan_config(session, project.id, scan_config_id)
    if not config.app_version_column:
        return ReleaseRegressionsResponse(
            scan_config_id=config.id,
            app_version_column=None,
            latest_version=None,
            comparability=[],
            items=[],
        )

    stmt = select(ReleaseRegression).where(ReleaseRegression.scan_config_id == config.id)
    verdict_stmt = select(ReleaseComparability).where(
        ReleaseComparability.scan_config_id == config.id
    )
    if scope_type is not None:
        stmt = stmt.where(ReleaseRegression.scope_type == scope_type)
        verdict_stmt = verdict_stmt.where(ReleaseComparability.scope_type == scope_type)
    regressions = list((await session.execute(stmt)).scalars())
    verdicts = sorted(
        (await session.execute(verdict_stmt)).scalars(),
        key=lambda v: str(v.scope_type),
    )
    comparability = [
        ReleaseComparabilityItem(
            scope_type=verdict.scope_type,
            comparable=verdict.comparable,
            reason=verdict.reason,
            version=verdict.version,
            previous_version=verdict.previous_version,
            emerging_share=verdict.emerging_share,
            max_emerging_share=verdict.max_emerging_share,
        )
        for verdict in verdicts
    ]
    # The verdict knows the release pair even when no row was emitted, which is
    # exactly the case a suppressed comparison has to name.
    judged_versions = {v.version for v in verdicts if v.version is not None}
    if not regressions:
        candidates = judged_versions
        return ReleaseRegressionsResponse(
            scan_config_id=config.id,
            app_version_column=config.app_version_column,
            latest_version=order_versions(candidates)[-1] if candidates else None,
            comparability=comparability,
            items=[],
        )

    event_ids = {r.event_id for r in regressions if r.event_id is not None}
    type_ids = {r.event_type_id for r in regressions if r.event_type_id is not None}
    event_names: dict[uuid.UUID, str] = {}
    if event_ids:
        for event_id, name in (
            await session.execute(select(Event.id, Event.name).where(Event.id.in_(event_ids)))
        ).all():
            event_names[event_id] = name
    type_names: dict[uuid.UUID, str] = {}
    if type_ids:
        for type_id, display_name, name in (
            await session.execute(
                select(EventType.id, EventType.display_name, EventType.name).where(
                    EventType.id.in_(type_ids)
                )
            )
        ).all():
            type_names[type_id] = display_name or name

    def _scope_name(regression: ReleaseRegression) -> str:
        if regression.event_id is not None:
            return event_names.get(regression.event_id, regression.scope_ref)
        if regression.event_type_id is not None:
            return type_names.get(regression.event_type_id, regression.scope_ref)
        return regression.scope_ref

    latest_version = order_versions({r.version for r in regressions} | judged_versions)[-1]
    # Missing events first, then by severity (lowest ratio = worst).
    ordered = sorted(regressions, key=lambda r: (0 if r.kind == "missing" else 1, r.ratio))
    items = [
        ReleaseRegressionItem(
            scope_type=regression.scope_type,
            scope_ref=regression.scope_ref,
            scope_name=_scope_name(regression),
            event_id=regression.event_id,
            event_type_id=regression.event_type_id,
            kind=regression.kind,
            version=regression.version,
            previous_version=regression.previous_version,
            observed_count=regression.observed_count,
            expected_count=regression.expected_count,
            ratio=regression.ratio,
            share_prev=regression.share_prev,
            share_new=regression.share_new,
            release_share=regression.release_share,
            window_from=regression.window_from,
            window_to=regression.window_to,
        )
        for regression in ordered
    ]
    return ReleaseRegressionsResponse(
        scan_config_id=config.id,
        app_version_column=config.app_version_column,
        latest_version=latest_version,
        comparability=comparability,
        items=items,
    )


async def get_platform_presence(
    session: AsyncSession,
    slug: str,
    scan_config_id: uuid.UUID,
) -> PlatformPresenceResponse:
    """Per-event platform presence matrix for a scan's designated platform column.

    Presence is derived from the generic breakdown rows the metrics worker stores
    under ``platform_column`` (event-scope, real values only, count > 0): an event
    is "present" on a platform if it has any such stored row. Empty when the scan
    has no ``platform_column`` configured.

    Read-only, and DISTINCT on purpose: the breakdown table is keyed per bucket
    and these rows have no retention outside demo projects, so the undeduplicated
    select hydrated one row per (event, platform, bucket) since the scan began —
    cost growing with scan age for an answer that is at most events x platforms.
    The database folds them now; ``Event.name`` rides along
    functionally dependent on ``event_id``, so it adds no rows of its own.
    """
    project = await resolve_project(session, slug)
    config = await _resolve_scan_config(session, project.id, scan_config_id)
    if not config.platform_column:
        return PlatformPresenceResponse(
            scan_config_id=config.id, platform_column=None, platforms=[], items=[]
        )

    rows = await session.execute(
        select(
            EventMetricBreakdown.event_id,
            EventMetricBreakdown.breakdown_value,
            Event.name,
        )
        .join(Event, Event.id == EventMetricBreakdown.event_id)
        .where(
            EventMetricBreakdown.scan_config_id == config.id,
            EventMetricBreakdown.breakdown_column == config.platform_column,
            EventMetricBreakdown.event_id.is_not(None),
            EventMetricBreakdown.is_other.is_(False),
            EventMetricBreakdown.count > 0,
        )
        .distinct()
    )
    platforms: set[str] = set()
    by_event: dict[uuid.UUID, tuple[str, set[str]]] = {}
    for event_id, platform_value, event_name in rows.all():
        # The query filters ``event_id IS NOT NULL``.
        assert event_id is not None
        platforms.add(platform_value)
        _name, present = by_event.setdefault(event_id, (event_name, set()))
        present.add(platform_value)

    items = [
        PlatformPresenceRow(
            event_id=event_id,
            event_name=name,
            present_platforms=sorted(present),
        )
        for event_id, (name, present) in sorted(by_event.items(), key=lambda kv: kv[1][0])
    ]
    return PlatformPresenceResponse(
        scan_config_id=config.id,
        platform_column=config.platform_column,
        platforms=sorted(platforms),
        items=items,
    )


async def _main_branch_event_type_id(
    session: AsyncSession,
    project_id: uuid.UUID,
    event_type_id: uuid.UUID,
) -> uuid.UUID:
    """The MAIN-branch ``EventType`` a possibly branch-copied type id stands for.

    ``event_metrics`` rows only ever reference main events — a scan never sees a
    working branch (see ``services/_branch_counterparts``) — while the Events
    page on a branch filters by the branch copy's type, which the deep copy gave
    a fresh uuid. Filtering the series by that id matched no row at all, so every
    event-type tab's Dynamics card read "no recent volume" while the sparklines
    beside it (which read through to the twins) showed traffic.

    Paired on the type NAME, the pairing ``main_counterparts`` already uses.
    Returns the id unchanged when it is already main's, or when the branch
    introduced a type main has no copy of: that one genuinely has no metrics.
    """
    # Scoped to THIS project, like every sibling query in this module: the id is
    # a raw query parameter on ``GET /events-metrics`` and nothing on that route
    # checks it belongs here. Unscoped, another project's type id resolved to
    # its name and then to a local twin of the same name, so the caller got a
    # series for a type they never asked for. Scoped, it falls through the
    # ``name is None`` branch below and is returned unchanged — which is what
    # the docstring already promises for a type main has no copy of.
    name = await session.scalar(
        select(EventType.name).where(
            EventType.id == event_type_id,
            EventType.project_id == project_id,
        )
    )
    if name is None:
        return event_type_id
    main_branch_ids = select(PlanBranch.id).where(
        PlanBranch.project_id == project_id,
        PlanBranch.kind == BranchKind.main.value,
    )
    twin_id = await session.scalar(
        select(EventType.id).where(
            EventType.project_id == project_id,
            EventType.branch_id.in_(main_branch_ids),
            EventType.name == name,
        )
    )
    return twin_id or event_type_id


async def _branch_filtered_metric_event_ids(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    event_type_id: uuid.UUID | None,
    search: str | None,
    status: list[str] | None,
    tag: str | None,
) -> list[uuid.UUID] | None:
    """Main-twin ids of the WORKING-BRANCH events the filters select, or None on main.

    ``event_metrics`` rows only ever reference main events, so the filters used
    to be evaluated against main's rows — and on a branch an analyst who had
    re-tagged or re-statused an event there got a chart for a different set of
    events than the table beside it lists. The filters are
    evaluated on the branch's own rows instead, and each selected copy reads its
    metrics through its main twin (``_branch_counterparts.main_counterparts``,
    the pairing every other branch read uses). A copy with no twin was added on
    the branch and has no metrics yet.

    ``None`` when no branch was named or it IS main: the caller's main-row
    filters already describe the page there.
    """
    if branch_id is None:
        return None
    resolved = await resolve_branch_id(session, project_id, branch_id)
    if resolved == await ensure_main_branch_id(session, project_id):
        return None
    criteria = [Event.project_id == project_id, Event.branch_id == resolved]
    if event_type_id:
        criteria.append(Event.event_type_id == event_type_id)
    if search:
        criteria.append(Event.name.ilike(f"%{search}%"))
    if status:
        criteria.append(Event.status.in_(status))
    if tag:
        criteria.append(Event.id.in_(select(EventTag.event_id).where(EventTag.name == tag)))
    branch_events = list((await session.execute(select(Event).where(*criteria))).scalars())
    twins = await main_counterparts(session, project_id=project_id, events=branch_events)
    return sorted({twin.id for twin in twins.values()})


async def get_events_metrics(
    session: AsyncSession,
    slug: str,
    event_type_id: uuid.UUID | None = None,
    search: str | None = None,
    tag: str | None = None,
    status: list[str] | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    branch_id: uuid.UUID | None = None,
) -> EventMetricsResponse:
    project = await resolve_project(session, slug)
    # Filled on every return, like every sibling metrics endpoint:
    # the points below carry no band today, but a served field left at the
    # schema default claims a threshold the project may not use.
    sigma_threshold = await _get_project_sigma_threshold(session, project.id)

    # Every filter the series is built from, collected once: the scan resolution
    # below has to see EXACTLY the rows the sum will see, or it can pick a scan
    # that turns out to have nothing for this tab.
    conditions = [
        Event.project_id == project.id,
        EventMetric.event_id.is_not(None),
    ]
    branch_filter_ids = await _branch_filtered_metric_event_ids(
        session,
        project_id=project.id,
        branch_id=branch_id,
        event_type_id=event_type_id,
        search=search,
        status=status,
        tag=tag,
    )
    if branch_filter_ids is not None:
        conditions.append(EventMetric.event_id.in_(branch_filter_ids))
    else:
        if event_type_id:
            # A caller that names no branch may still send a branch copy's type
            # id, which no metric row carries; resolve it to main's twin first.
            metrics_type_id = await _main_branch_event_type_id(session, project.id, event_type_id)
            conditions.append(Event.event_type_id == metrics_type_id)
        if search:
            conditions.append(Event.name.ilike(f"%{search}%"))
        if status:
            conditions.append(Event.status.in_(status))
        if tag:
            tagged_event_ids = select(EventTag.event_id).where(EventTag.name == tag).correlate(None)
            conditions.append(Event.id.in_(tagged_event_ids))
    # The rolling-week totals below read their own window, not the chart's.
    scope_conditions = list(conditions)
    if time_from:
        conditions.append(EventMetric.bucket >= time_from)
    if time_to:
        conditions.append(EventMetric.bucket < time_to)

    config = await _resolve_events_metrics_scan_config(session, project.id, conditions)
    if config is None:
        return EventMetricsResponse(scope="events_total", data=[], sigma_threshold=sigma_threshold)
    scan_config_id = config.id

    query = (
        select(EventMetric.bucket, func.sum(EventMetric.count))
        .join(Event, EventMetric.event_id == Event.id)
        .where(*conditions, EventMetric.scan_config_id == scan_config_id)
    )

    result = await session.execute(query.group_by(EventMetric.bucket).order_by(EventMetric.bucket))
    rows = [(bucket, int(count)) for bucket, count in result.all()]

    interval = await _get_scan_config_interval(session, scan_config_id) if rows else None
    week_total, prior_week_total = await _events_week_totals(
        session,
        scope_conditions=scope_conditions,
        scan_config_id=scan_config_id,
        week_end=time_to or datetime.now(UTC),
    )

    return EventMetricsResponse(
        scope="events_total",
        scan_config_id=scan_config_id,
        scan_config_name=config.name,
        interval=interval,
        sigma_threshold=sigma_threshold,
        week_total=week_total,
        prior_week_total=prior_week_total,
        data=[EventMetricPoint(bucket=bucket, count=count) for bucket, count in rows],
    )


_WEEK = timedelta(days=7)


async def _events_week_totals(
    session: AsyncSession,
    *,
    scope_conditions: list[ColumnElement[bool]],
    scan_config_id: uuid.UUID,
    week_end: datetime,
) -> tuple[int, int]:
    """``(last 7 days, the 7 before)`` of the Events tab's series, one query.

    The collapsed Dynamics strip reads "612K in 7d · +4% vs prior week" whatever
    range the chart is set to, so these sum their own fixed windows ending at the
    chart's upper bound rather than the chart's points — a 7d chart has no prior
    week to compare against. Same rows, joins and scan as the series; the newest
    bucket may still be filling, which only ever understates the current week.
    """
    week_start = week_end - _WEEK
    row = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(case((EventMetric.bucket >= week_start, EventMetric.count), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(case((EventMetric.bucket < week_start, EventMetric.count), else_=0)),
                    0,
                ),
            )
            .join(Event, EventMetric.event_id == Event.id)
            .where(
                *scope_conditions,
                EventMetric.scan_config_id == scan_config_id,
                EventMetric.bucket >= week_start - _WEEK,
                EventMetric.bucket < week_end,
            )
        )
    ).one()
    return int(row[0]), int(row[1])


async def get_overview_kpi_series(
    session: AsyncSession,
    slug: str,
    *,
    days: int = 14,
) -> OverviewKpiSeriesResponse:
    """Daily NEW-event counts (from Event.created_at) for the Overview
    'new events' KPI sparkline — the one KPI with genuine history.

    Restricted to the project's main branch: Event is branch-scoped and a
    working branch deep-copies every plan entity, so counting by project_id
    alone multiplied the series by (1 + open branches) and made it sum past the
    'Active events' stat beside it, which counts the main branch only.
    Mirrors project_service._get_project_summaries' scoping so
    the sparkline and the stat describe the same plan.
    """
    project = await resolve_project(session, slug)
    start_day = (datetime.now(UTC) - timedelta(days=days - 1)).date()
    time_from = datetime(start_day.year, start_day.month, start_day.day, tzinfo=UTC)
    main_branch_ids = select(PlanBranch.id).where(
        PlanBranch.project_id == project.id,
        PlanBranch.kind == BranchKind.main.value,
    )
    created_ats = (
        (
            await session.execute(
                select(Event.created_at).where(
                    Event.project_id == project.id,
                    Event.branch_id.in_(main_branch_ids),
                    Event.created_at >= time_from,
                )
            )
        )
        .scalars()
        .all()
    )
    counts: dict[date, int] = {}
    for created in created_ats:
        day = created.date()
        counts[day] = counts.get(day, 0) + 1
    series = [counts.get(start_day + timedelta(days=i), 0) for i in range(days)]
    return OverviewKpiSeriesResponse(days=days, new_events=series)


async def get_top_events_by_volume(
    session: AsyncSession,
    slug: str,
    *,
    window_hours: int = 48,
    limit: int = 6,
) -> list[TopEventResponse]:
    """Rank a project's events by total volume over a recent window.

    Sums EventMetric.count per event (scoped to the project via ScanConfig) and
    returns the top ``limit`` by volume — the data behind the Overview "Top
    events by volume" widget, computed server-side so the client need not fetch
    window metrics for every event just to show a handful.
    """
    project = await resolve_project(session, slug)
    time_from = datetime.now(UTC) - timedelta(hours=window_hours)
    total = func.coalesce(func.sum(EventMetric.count), 0)
    rows = (
        await session.execute(
            select(Event.id, Event.name, Event.event_type_id, total.label("total"))
            .join(EventMetric, EventMetric.event_id == Event.id)
            .join(ScanConfig, ScanConfig.id == EventMetric.scan_config_id)
            .where(
                ScanConfig.project_id == project.id,
                EventMetric.bucket >= time_from,
            )
            .group_by(Event.id, Event.name, Event.event_type_id)
            .order_by(total.desc())
            .limit(limit)
        )
    ).all()
    if not rows:
        return []
    # The share denominator: type-level rows only, the project-total
    # definition ``get_data_source_stats`` documents — every collection chunk
    # also writes event-level rows re-counting the same warehouse rows, so a
    # flat sum would count matched volume twice.
    window_total = (
        await session.execute(
            select(func.coalesce(func.sum(EventMetric.count), 0))
            .join(ScanConfig, ScanConfig.id == EventMetric.scan_config_id)
            .where(
                ScanConfig.project_id == project.id,
                EventMetric.bucket >= time_from,
                EventMetric.event_id.is_(None),
                EventMetric.event_type_id.is_not(None),
            )
        )
    ).scalar_one()
    return [
        TopEventResponse(
            event_id=row.id,
            name=row.name,
            event_type_id=row.event_type_id,
            total_count=int(row.total or 0),
            window_total_count=int(window_total or 0),
        )
        for row in rows
    ]


async def get_data_source_stats(
    session: AsyncSession,
    data_source_id: uuid.UUID,
    *,
    window_hours: int = 48,
) -> DataSourceStatsResponse:
    """Aggregate runtime activity for a data source from EventMetric rollups.

    Volume + events-tracked + a bucketed throughput series over the recent
    window, joined to the data source via ScanConfig. Owner-only diagnostics:
    nothing in the product reads this (see the note on the route in
    ``api/v1/data_sources.py``).

    Volume counts TYPE-level rows only — ``event_id IS NULL AND event_type_id IS
    NOT NULL``, the project-total definition ``_scope_metric_filters`` uses.
    Every collection chunk writes both an event-level row for each matched plan
    event and a type-level row re-counting the same warehouse rows, so summing
    the table flat reported matched volume twice and unmatched volume once —
    close to 2x. ``events_tracked`` still counts distinct
    ``event_id`` across all rows, because only the event-level rows carry one.

    Another organization's source is "not found" (F20 PR4), the answer every
    other data-source route gives it.
    """
    in_org = await session.scalar(
        select(DataSource.id).where(
            DataSource.id == data_source_id, DataSource.organization_id == owning_org_id()
        )
    )
    if in_org is None:
        raise HTTPException(404, "Data source not found")
    time_from = datetime.now(UTC) - timedelta(hours=window_hours)
    type_level = and_(EventMetric.event_id.is_(None), EventMetric.event_type_id.is_not(None))
    volume = func.coalesce(func.sum(case((type_level, EventMetric.count), else_=0)), 0)
    totals = (
        await session.execute(
            select(
                volume,
                func.count(func.distinct(EventMetric.event_id)),
            )
            .join(ScanConfig, ScanConfig.id == EventMetric.scan_config_id)
            .where(ScanConfig.data_source_id == data_source_id, EventMetric.bucket >= time_from)
        )
    ).one()
    series_rows = (
        await session.execute(
            select(EventMetric.bucket, func.coalesce(func.sum(EventMetric.count), 0))
            .join(ScanConfig, ScanConfig.id == EventMetric.scan_config_id)
            .where(
                ScanConfig.data_source_id == data_source_id,
                EventMetric.bucket >= time_from,
                type_level,
            )
            .group_by(EventMetric.bucket)
            .order_by(EventMetric.bucket)
        )
    ).all()
    return DataSourceStatsResponse(
        events_tracked=int(totals[1] or 0),
        volume_window=int(totals[0] or 0),
        window_hours=window_hours,
        throughput=[
            DataSourceThroughputPoint(bucket=bucket, count=int(count or 0))
            for bucket, count in series_rows
        ],
    )


async def get_events_window_metrics(
    session: AsyncSession,
    slug: str,
    *,
    event_ids: list[uuid.UUID],
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> list[EventWindowMetricsResponse]:
    if not event_ids:
        return []

    project = await resolve_project(session, slug)

    # Branch copies read through to their main twins: the batch
    # runs on the twin ids and each response is stamped with the id asked for.
    requested_rows = (
        (
            await session.execute(
                select(Event).where(Event.project_id == project.id, Event.id.in_(event_ids))
            )
        )
        .scalars()
        .all()
    )
    twins = await main_counterparts(session, project_id=project.id, events=requested_rows)
    twin_id_for = {event_id: twin.id for event_id, twin in twins.items()}
    requested_ids = list(dict.fromkeys(event_ids))
    event_ids = list(dict.fromkeys(twin_id_for.get(event_id, event_id) for event_id in event_ids))

    # Which scan config last reported each requested event, resolved as a
    # correlated "newest bucket wins" lookup per event.
    #
    # This used to SELECT (event_id, scan_config_id, bucket) for every metric row
    # the project had ever recorded for these events and keep the first row per
    # event in Python. On a real project that is hundreds of thousands of rows
    # decoded and thrown away for a 100-id bucket — measured at 5.4 s of the
    # endpoint's 6.1 s. The correlated form probes
    # ``ix_event_metric_event_bucket`` once per event (ORDER BY bucket DESC
    # LIMIT 1) and returns exactly one row per event; it is plain SQL, so it
    # behaves the same on SQLite as on Postgres.
    latest_scan_for_event = (
        select(EventMetric.scan_config_id)
        .where(
            EventMetric.event_id == Event.id,
            EventMetric.scan_config_id.in_(
                select(ScanConfig.id).where(ScanConfig.project_id == project.id)
            ),
        )
        .order_by(EventMetric.bucket.desc())
        .limit(1)
        .correlate(Event)
        .scalar_subquery()
    )
    event_rows = (
        await session.execute(
            select(Event.id, latest_scan_for_event).where(
                Event.project_id == project.id,
                Event.id.in_(event_ids),
            )
        )
    ).all()

    valid_event_ids = [event_id for event_id, _scan_config_id in event_rows]
    if not valid_event_ids:
        return []

    latest_scan_by_event: dict[uuid.UUID, uuid.UUID] = {
        event_id: scan_config_id
        for event_id, scan_config_id in event_rows
        if scan_config_id is not None
    }

    interval_by_scan: dict[uuid.UUID, str | None] = {}
    if latest_scan_by_event:
        interval_rows = (
            await session.execute(
                select(ScanConfig.id, ScanConfig.interval).where(
                    ScanConfig.id.in_(set(latest_scan_by_event.values()))
                )
            )
        ).all()
        interval_by_scan = {scan_config_id: interval for scan_config_id, interval in interval_rows}

    metric_rows_by_event: dict[uuid.UUID, list[tuple[datetime, int]]] = {
        event_id: [] for event_id in valid_event_ids
    }
    if latest_scan_by_event:
        metric_query = (
            select(
                EventMetric.event_id,
                EventMetric.scan_config_id,
                EventMetric.bucket,
                EventMetric.count,
            )
            .where(
                EventMetric.event_id.in_(valid_event_ids),
                EventMetric.scan_config_id.in_(set(latest_scan_by_event.values())),
            )
            .order_by(EventMetric.event_id, EventMetric.bucket)
        )
        if time_from is not None:
            metric_query = metric_query.where(EventMetric.bucket >= time_from)
        if time_to is not None:
            metric_query = metric_query.where(EventMetric.bucket < time_to)

        for event_id, metric_scan_id, bucket, count in (await session.execute(metric_query)).all():
            if event_id is None:
                continue
            if latest_scan_by_event.get(event_id) != metric_scan_id:
                continue
            metric_rows_by_event.setdefault(event_id, []).append((bucket, int(count)))

    anomaly_rows_by_event: dict[uuid.UUID, list[MetricAnomaly]] = {
        event_id: [] for event_id in valid_event_ids
    }
    if latest_scan_by_event:
        anomaly_query = (
            select(MetricAnomaly)
            .where(
                MetricAnomaly.scope_type == SCOPE_EVENT,
                MetricAnomaly.event_id.in_(valid_event_ids),
                MetricAnomaly.scan_config_id.in_(set(latest_scan_by_event.values())),
            )
            .order_by(MetricAnomaly.event_id, MetricAnomaly.bucket)
        )
        if time_from is not None:
            anomaly_query = anomaly_query.where(MetricAnomaly.bucket >= time_from)
        if time_to is not None:
            anomaly_query = anomaly_query.where(MetricAnomaly.bucket < time_to)

        for anomaly in (await session.execute(anomaly_query)).scalars():
            if anomaly.event_id is None:
                continue
            if latest_scan_by_event.get(anomaly.event_id) != anomaly.scan_config_id:
                continue
            anomaly_rows_by_event.setdefault(anomaly.event_id, []).append(anomaly)

    valid_event_ids_set = set(valid_event_ids)
    # Loaded once for the whole batch — the loop below runs per requested event.
    recent_window = await _get_project_recent_signal_window(session, project.id)
    # One query for the whole batch, for the same reason: the ongoing-outage
    # re-check needs scan liveness per event, and resolving it inside the loop
    # would be a round-trip per requested event. The batch can mix grids, so the
    # floor is the WIDEST horizon among them — a narrower one would hide a
    # coarse scan's newest bucket, and per-scan precision is restored downstream
    # where each response re-derives the horizon from its own interval.
    batch_scan_ids = {
        scan_config_id for scan_config_id in latest_scan_by_event.values() if scan_config_id
    }
    scan_latest_buckets = await _get_scan_latest_buckets(
        session,
        batch_scan_ids,
        since=min(
            (
                scan_liveness_cutoff(
                    interval=interval_by_scan.get(scan_config_id),
                    recent_window=recent_window,
                )
                for scan_config_id in batch_scan_ids
            ),
            default=datetime.now(UTC),
        ),
    )
    responses: list[EventWindowMetricsResponse] = []
    for requested_id in requested_ids:
        event_id = twin_id_for.get(requested_id, requested_id)
        if event_id not in valid_event_ids_set:
            continue

        scan_config_id = latest_scan_by_event.get(event_id)
        interval = interval_by_scan.get(scan_config_id) if scan_config_id is not None else None
        metric_rows = metric_rows_by_event.get(event_id, [])
        # Deliberately NOT widened to an open anchor the way the per-scope
        # drilldowns are (``_load_scope_anomalies``): this batch draws row
        # sparklines and reports ``total_count`` for the window the caller asked
        # for, so reaching outside it would silently change the number in the
        # column next to the chart. Nothing here reads ``latest_signal``.
        #
        # ``with_forecast=False`` because nothing here reads the forecast either:
        # only ``.data`` is copied out below. Computing one cost a fresh robust
        # STL/MSTL fit PER REQUESTED EVENT — up to 100 of them in one request —
        # and every result was dropped on the next line.
        metrics_response = await _build_metrics_response(
            scope=SCOPE_EVENT,
            scan_config_id=scan_config_id,
            scope_ref=str(event_id),
            interval=interval,
            metric_rows=metric_rows,
            anomalies=anomaly_rows_by_event.get(event_id, []),
            event_id=event_id,
            recent_window=recent_window,
            scan_latest_bucket=(
                scan_latest_buckets.get(scan_config_id) if scan_config_id is not None else None
            ),
            with_forecast=False,
        )
        responses.append(
            EventWindowMetricsResponse(
                event_id=requested_id,
                scan_config_id=scan_config_id,
                interval=interval,
                total_count=sum(count for _bucket, count in metric_rows),
                data=metrics_response.data,
            )
        )

    return responses


async def get_event_type_metrics(
    session: AsyncSession,
    slug: str,
    event_type_id: uuid.UUID,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> EventMetricsResponse:
    project = await resolve_project(session, slug)
    event_type = await _resolve_event_type(session, project.id, event_type_id)
    scan_config_id = await _resolve_scope_scan_config_id(
        session,
        project.id,
        event_type_id=event_type.id,
    )
    interval = await _get_scan_config_interval(session, scan_config_id)
    if scan_config_id is None:
        return EventMetricsResponse(
            scope=SCOPE_EVENT_TYPE,
            event_type_id=event_type.id,
            interval=interval,
            data=[],
        )

    sigma_threshold = await _apply_scope_sigma_override(
        session,
        project_id=project.id,
        scan_config_id=scan_config_id,
        scope_type=SCOPE_EVENT_TYPE,
        scope_ref=str(event_type.id),
        fallback=await _get_project_sigma_threshold(session, project.id),
    )
    recent_window = await _get_project_recent_signal_window(session, project.id)
    scan_latest_bucket = await _get_scan_latest_bucket(
        session, scan_config_id, interval=interval, recent_window=recent_window
    )
    anomalies, effective_time_from = await _load_scope_anomalies(
        session,
        scope=SCOPE_EVENT_TYPE,
        scan_config_id=scan_config_id,
        scope_ref=str(event_type.id),
        time_from=time_from,
        time_to=time_to,
        interval=interval,
        recent_window=recent_window,
        scan_latest_bucket=scan_latest_bucket,
    )
    metric_rows = await _get_metric_rows(
        session,
        scope=SCOPE_EVENT_TYPE,
        scan_config_id=scan_config_id,
        scope_ref=str(event_type.id),
        time_from=effective_time_from,
        time_to=time_to,
    )
    baselines = await _get_baseline_rows(
        session,
        scan_config_id=scan_config_id,
        scope=SCOPE_EVENT_TYPE,
        scope_ref=str(event_type.id),
        time_from=effective_time_from,
        time_to=time_to,
    )
    collection_timing = await _get_collection_timing(
        session, scan_config_id, interval=interval, scan_latest_bucket=scan_latest_bucket
    )
    response = await _build_metrics_response(
        scope=SCOPE_EVENT_TYPE,
        scan_config_id=scan_config_id,
        scope_ref=str(event_type.id),
        interval=interval,
        metric_rows=metric_rows,
        anomalies=anomalies,
        baselines=baselines,
        event_type_id=event_type.id,
        sigma_threshold=sigma_threshold,
        recent_window=recent_window,
        scan_latest_bucket=scan_latest_bucket,
        collection_timing=collection_timing,
    )
    response = await signal_verdict_service.with_chart_verdicts(
        session, project.id, response, scope_type=SCOPE_EVENT_TYPE, scope_ref=str(event_type.id)
    )
    return await anomaly_attribution_service.with_latest_signal_attribution(
        session, response, project_id=project.id
    )


async def get_project_total_metrics(
    session: AsyncSession,
    slug: str,
    scan_config_id: uuid.UUID | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> EventMetricsResponse:
    project = await resolve_project(session, slug)
    resolved_scan_config_id = scan_config_id or await _get_default_scan_config_id(
        session, project.id
    )
    if resolved_scan_config_id is None:
        return EventMetricsResponse(scope=SCOPE_PROJECT_TOTAL, data=[])

    config = await session.get(ScanConfig, resolved_scan_config_id)
    if config is None or config.project_id != project.id:
        raise HTTPException(404, "Scan config not found")

    recent_window = await _get_project_recent_signal_window(session, project.id)
    scan_latest_bucket = await _get_scan_latest_bucket(
        session,
        resolved_scan_config_id,
        interval=config.interval,
        recent_window=recent_window,
    )
    anomalies, effective_time_from = await _load_scope_anomalies(
        session,
        scope=SCOPE_PROJECT_TOTAL,
        scan_config_id=resolved_scan_config_id,
        scope_ref=str(resolved_scan_config_id),
        time_from=time_from,
        time_to=time_to,
        interval=config.interval,
        recent_window=recent_window,
        scan_latest_bucket=scan_latest_bucket,
    )
    metric_rows = await _get_metric_rows(
        session,
        scope=SCOPE_PROJECT_TOTAL,
        scan_config_id=resolved_scan_config_id,
        scope_ref=str(resolved_scan_config_id),
        time_from=effective_time_from,
        time_to=time_to,
    )
    baselines = await _get_baseline_rows(
        session,
        scan_config_id=resolved_scan_config_id,
        scope=SCOPE_PROJECT_TOTAL,
        scope_ref=str(resolved_scan_config_id),
        time_from=effective_time_from,
        time_to=time_to,
    )
    collection_timing = await _get_collection_timing(
        session,
        resolved_scan_config_id,
        interval=config.interval,
        scan_latest_bucket=scan_latest_bucket,
    )
    response = await _build_metrics_response(
        scope=SCOPE_PROJECT_TOTAL,
        scan_config_id=resolved_scan_config_id,
        scan_config_name=config.name,
        scope_ref=str(resolved_scan_config_id),
        interval=config.interval,
        metric_rows=metric_rows,
        anomalies=anomalies,
        baselines=baselines,
        sigma_threshold=await _apply_scope_sigma_override(
            session,
            project_id=project.id,
            scan_config_id=resolved_scan_config_id,
            scope_type=SCOPE_PROJECT_TOTAL,
            scope_ref=str(resolved_scan_config_id),
            fallback=await _get_project_sigma_threshold(session, project.id),
        ),
        recent_window=recent_window,
        scan_latest_bucket=scan_latest_bucket,
        collection_timing=collection_timing,
    )
    response = await signal_verdict_service.with_chart_verdicts(
        session,
        project.id,
        response,
        scope_type=SCOPE_PROJECT_TOTAL,
        scope_ref=str(resolved_scan_config_id),
    )
    return await anomaly_attribution_service.with_latest_signal_attribution(
        session, response, project_id=project.id
    )
