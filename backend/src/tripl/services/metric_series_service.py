"""Read service for catalog-metric (MetricDefinition) series.

Loads ``MetricValue`` rows for a definition, densifies them onto the interval
grid, joins anomaly flags, and attaches a one-step forecast — returning a
payload shaped like the event-metric series but with float values. Breakdown
and app-version variants mirror the event equivalents.

Reuse, not duplication: the densify-to-grid primitive (``expand_series``), the
forecast (``forecast_next_buckets``), the anomaly→signal mapping
(``_signal_from_anomaly``) and the signal-state classifier
(``classify_signal_state``) are all imported, not reimplemented. Only the
float/no-event-scope shaping is specialised here.

ANOMALY-SCOPE (ticket tripl-dxhp.6): catalog-metric anomalies are stored in
``MetricAnomaly`` under ``scope_type='metric'`` /
``scope_ref=str(metric_definition_id)`` with a NULL ``scan_config_id``. The read
filters on BOTH ``scope_type == MetricScopeType.metric`` and the scope_ref so it
can never pick up an unrelated row whose scope_ref happens to equal a metric
definition UUID.

GRID-POPULATION (ticket tripl-0zpq.115): one chart must describe ONE population.
An anomaly row carries no ``scan_config_id``, so the band it draws is whatever
the detector scored; the value line therefore has to be read the way the
detector reads it — SUMMED per bucket over every source config on the metric's
resolved grid interval (:func:`tripl.metric_grid.grid_population_filter`, shared with
``detect._metric_grid_population``). Reading a single config plots one addend of
the band around it; reading every config mixes intervals that are not addable.
"""

from __future__ import annotations

import asyncio
import math
import re
import uuid
from datetime import datetime, timedelta
from typing import cast

from fastapi import HTTPException
from sqlalchemy import ColumnExpressionArgument, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.analyzers.anomaly_detector import (
    SCOPE_METRIC,
    SeriesPoint,
    expand_series,
    forecast_next_buckets,
)
from tripl.core.intervals import get_interval
from tripl.metric_grid import grid_population_filter, metric_grid_stmt, metric_grids
from tripl.metric_monitoring import is_metric_monitored
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.fact_table import FactTable
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.metric_value_breakdown import MetricValueBreakdown
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.schemas.event_metric import AppVersionInfo, ForecastPoint, MetricSignalResponse
from tripl.schemas.metric_series import (
    MetricBreakdownSeries,
    MetricBreakdownsResponse,
    MetricSeriesPoint,
    MetricSeriesResponse,
    MetricVersionSeries,
    MetricVersionSeriesResponse,
)
from tripl.semver import (
    APP_VERSION_OTHER_LABEL,
    order_versions,
)
from tripl.services import anomaly_attribution_service, signal_verdict_service
from tripl.services.metrics_service import (
    _FORECAST_MAX_POINTS,
    _apply_scope_sigma_override,
    _get_project_recent_signal_window,
    _get_project_sigma_threshold,
    _resolve_project,
    _retained_versions,
    _served_stddev,
    _signal_from_anomaly,
)
from tripl.services.monitoring_utils import classify_signal_state, scan_interval_to_timedelta
from tripl.services.version_activation import (
    DEFAULT_ACTIVE_SHARE_MIN,
    active_release_versions,
    compile_prerelease_pattern,
    released_versions,
    resolve_share_min,
)
from tripl.worker.analyzers.metric_value_kind import is_count_shaped


async def _resolve_metric(
    session: AsyncSession,
    project: Project,
    metric_id: uuid.UUID,
) -> MetricDefinition:
    metric = await session.get(MetricDefinition, metric_id)
    if metric is None or metric.project_id != project.id:
        raise HTTPException(404, "Metric definition not found")
    return metric


async def _resolve_metric_interval(
    session: AsyncSession,
    metric: MetricDefinition,
) -> tuple[str | None, uuid.UUID | None]:
    """Interval + source scan_config for the metric's grid.

    Thin wrapper over the shared rule in :mod:`tripl.metric_grid` (own interval,
    else the inherited source-scan grid), kept for the tuple shape this module's
    callers read.
    """
    grid = metric_grids(
        (await session.execute(metric_grid_stmt(MetricDefinition.id == metric.id))).all()
    ).get(metric.id)
    if grid is None:
        return None, None
    return grid.interval, grid.scan_config_id


def _grid_population_filter(
    *,
    interval: str | None,
    scan_config_id: uuid.UUID | None,
) -> ColumnExpressionArgument[bool]:
    """``MetricValue`` rows on the metric's grid population.

    The rule and its reasoning live in :func:`tripl.metric_grid.grid_population_filter`,
    shared with the detector (tripl-67he).
    """
    return grid_population_filter(
        MetricValue.scan_config_id, interval=interval, scan_config_id=scan_config_id
    )


async def _load_metric_values(
    session: AsyncSession,
    metric_id: uuid.UUID,
    *,
    time_from: datetime | None,
    time_to: datetime | None,
    interval: str | None,
    scan_config_id: uuid.UUID | None,
) -> list[tuple[datetime, float]]:
    """The metric's value series: SUMMED per bucket over its grid population.

    ``func.sum`` with the grid population, not a bare read of one config's rows,
    for the reason :func:`_grid_population_filter` spells out — this is the
    reduction the anomalies overlaid on the result were scored from.
    """
    query = (
        select(MetricValue.bucket, func.sum(MetricValue.value))
        .where(
            MetricValue.metric_definition_id == metric_id,
            _grid_population_filter(interval=interval, scan_config_id=scan_config_id),
        )
        .group_by(MetricValue.bucket)
        .order_by(MetricValue.bucket)
    )
    if time_from is not None:
        query = query.where(MetricValue.bucket >= time_from)
    if time_to is not None:
        query = query.where(MetricValue.bucket < time_to)
    result = await session.execute(query)
    return [(bucket, float(value)) for bucket, value in result.all()]


async def _load_metric_anomalies(
    session: AsyncSession,
    metric_id: uuid.UUID,
    *,
    time_from: datetime | None,
    time_to: datetime | None,
) -> list[MetricAnomaly]:
    """Anomalies for a metric, matched on (scope_type='metric', scope_ref)."""
    query = (
        select(MetricAnomaly)
        .where(
            MetricAnomaly.scope_type == SCOPE_METRIC,
            MetricAnomaly.scope_ref == str(metric_id),
        )
        .order_by(MetricAnomaly.bucket)
    )
    if time_from is not None:
        query = query.where(MetricAnomaly.bucket >= time_from)
    if time_to is not None:
        query = query.where(MetricAnomaly.bucket < time_to)
    result = await session.execute(query)
    return list(result.scalars().all())


async def _latest_metric_value_bucket(
    session: AsyncSession,
    metric_id: uuid.UUID,
    *,
    since: datetime,
    time_to: datetime | None,
    interval: str | None,
    scan_config_id: uuid.UUID | None,
) -> datetime | None:
    """Newest stored value bucket at or after ``since``, on the grid population.

    ``since`` bounds the read and costs no accuracy for its one caller, which
    only asks whether the metric stored anything NEWER than a candidate anchor —
    rows older than that anchor cannot change the answer.

    Population-filtered for the same reason as :func:`_load_metric_values`: this
    probe decides whether an anomaly still classifies open, so it must measure
    against the same series the chart will plot, not against a retired grid's
    leftovers. ``max`` needs no per-bucket sum — the newest bucket of a sum over
    a set is the newest bucket in the set.
    """
    query = select(func.max(MetricValue.bucket)).where(
        MetricValue.metric_definition_id == metric_id,
        _grid_population_filter(interval=interval, scan_config_id=scan_config_id),
        MetricValue.bucket >= since,
    )
    if time_to is not None:
        query = query.where(MetricValue.bucket < time_to)
    return (await session.execute(query)).scalar_one_or_none()


async def _load_anomalies_reaching_open_anchor(
    session: AsyncSession,
    metric_id: uuid.UUID,
    *,
    time_from: datetime | None,
    time_to: datetime | None,
    interval: str | None,
    recent_window: timedelta | None,
    scan_config_id: uuid.UUID | None,
) -> tuple[list[MetricAnomaly], datetime | None]:
    """The metric's anomalies for the requested range, reaching back to an open anchor.

    Returns ``(anomalies, effective_time_from)``; the caller pulls its value rows
    with the returned floor so chart and signal describe one range.

    ``recent_signal_window_hours`` is project-wide and reaches 720 (30 days),
    while the range picker on this page is per-visit. A metric anomaly older than
    the selected range but still inside that window is therefore OPEN on the
    Anomalies page, the sidebar badge and the metrics list, while this read —
    which loaded anomalies only inside the requested window — reported no signal
    at all on the page those three link to. ``metrics_service._load_scope_anomalies``
    fixed the identical shape for the event scopes; this is the catalog-metric
    half of it.

    Widening is CONDITIONAL: only an anchor that still classifies open earns it.
    Reaching back to any older anomaly would quietly serve a different range than
    the user picked on every metric that has ever been flagged. The probe runs
    the same ``classify_signal_state`` call, on the same inputs, that
    :func:`_latest_signal` will run once the rows are loaded, so the two cannot
    disagree about what "open" means. Note the outage re-check is not consulted
    on either side — a catalog metric has no scan to ask whether the collector is
    alive (see ``monitoring_utils._outage_is_still_running``).
    """
    anomalies = await _load_metric_anomalies(
        session, metric_id, time_from=time_from, time_to=time_to
    )
    # A row inside the range is already the metric's newest (the range ends at
    # "now"), so it is the row every other surface classifies: nothing to reach
    # back for, and no extra query is run.
    if anomalies or time_from is None:
        return anomalies, time_from

    anchor = (
        await session.execute(
            select(MetricAnomaly)
            .where(
                MetricAnomaly.scope_type == SCOPE_METRIC,
                MetricAnomaly.scope_ref == str(metric_id),
                MetricAnomaly.bucket < time_from,
            )
            .order_by(MetricAnomaly.bucket.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if anchor is None:
        return anomalies, time_from

    # Mirrors what ``_build_metric_series_points`` will produce once the range
    # starts at the anchor: an anomaly bucket with no value row of its own is
    # backfilled from ``actual_count``, so the densified series ends at the
    # newest of the metric's value buckets and its anomaly buckets.
    stored_latest = await _latest_metric_value_bucket(
        session,
        metric_id,
        since=anchor.bucket,
        time_to=time_to,
        interval=interval,
        scan_config_id=scan_config_id,
    )
    latest_metric_bucket = (
        max(anchor.bucket, stored_latest) if stored_latest is not None else anchor.bucket
    )
    state = classify_signal_state(
        anomaly_bucket=anchor.bucket,
        latest_metric_bucket=latest_metric_bucket,
        interval=scan_interval_to_timedelta(interval),
        recent_window=recent_window,
    )
    if state is None:
        return anomalies, time_from

    return (
        await _load_metric_anomalies(session, metric_id, time_from=anchor.bucket, time_to=time_to),
        anchor.bucket,
    )


def _densify_value_rows(
    *,
    interval: str | None,
    value_rows: list[tuple[datetime, float]],
    anomalies: list[MetricAnomaly],
    count_shaped: bool = True,
) -> list[tuple[datetime, float]]:
    """Place values on the interval grid, preserving float precision.

    For COUNT-shaped metrics ``expand_series`` produces the densified bucket grid
    and gap buckets are filled as ``0.0`` — a missing count genuinely means zero.
    For FRACTIONAL metrics (ratios/averages/sql) a missing bucket means "no
    data", not zero, so gaps are NOT filled: only present buckets are returned
    and the chart renders the gaps as null breaks.

    NON-FINITE values are dropped here, at the single chokepoint every series
    read (plain, breakdown, version) passes through. ``MetricValue.value`` is a
    plain Float column, and the collector USED to coerce warehouse cells with a
    bare ``float()``, so a ClickHouse ``countIf(a)/countIf(b)`` that divided by
    zero stored a literal NaN/Infinity. The write side now refuses them
    (``metric_rows._drop_non_finite_values``, on both upsert helpers), so what
    this read-side drop still covers is rows stored BEFORE that guard plus any
    future write path that bypasses those helpers — which is why it stays even
    though no current collector can produce one. Carrying one further used to
    raise ``ValueError``/``OverflowError`` out of the forecast's ``round()`` — a 500 on
    the whole metric detail page until retention dropped the row — and would in
    any case serialize as a JSON literal no client can parse. A poisoned bucket
    is treated exactly like an absent one: zero-filled for counts, a null break
    for fractionals.
    """
    values_by_bucket: dict[datetime, float] = {
        bucket: value for bucket, value in value_rows if math.isfinite(value)
    }
    for anomaly in anomalies:
        actual = float(anomaly.actual_count)
        if math.isfinite(actual):
            values_by_bucket.setdefault(anomaly.bucket, actual)

    if interval and values_by_bucket and count_shaped:
        delta = get_interval(interval).delta
        # ``SeriesPoint.count`` is a float and ``expand_series`` only reads the
        # buckets off these points, so the values ride through unrounded.
        grid_points = [
            SeriesPoint(bucket=bucket, count=value) for bucket, value in values_by_bucket.items()
        ]
        expanded = expand_series(
            grid_points,
            interval=delta,
            end_exclusive=max(values_by_bucket) + delta,
        )
        return [(point.bucket, values_by_bucket.get(point.bucket, 0.0)) for point in expanded]
    return sorted(values_by_bucket.items())


def _build_metric_series_points(
    *,
    interval: str | None,
    value_rows: list[tuple[datetime, float]],
    anomalies: list[MetricAnomaly],
    count_shaped: bool = True,
) -> list[MetricSeriesPoint]:
    anomalies_by_bucket = {anomaly.bucket: anomaly for anomaly in anomalies}
    grid = _densify_value_rows(
        interval=interval,
        value_rows=value_rows,
        anomalies=anomalies,
        count_shaped=count_shaped,
    )
    points: list[MetricSeriesPoint] = []
    for bucket, value in grid:
        anomaly = anomalies_by_bucket.get(bucket)
        points.append(
            MetricSeriesPoint(
                bucket=bucket,
                value=value,
                expected_count=anomaly.expected_count if anomaly else None,
                # ``_served_stddev``, the SAME floored effective stddev the
                # event-scope points serve (``metrics_service._build_metric_points``):
                # the band is drawn as ``expected ± k × stddev`` and the detector
                # flagged with the floored denominator, so serving the raw column
                # here drew a narrower band than the rule that produced the dot.
                stddev=_served_stddev(anomaly) if anomaly else None,
                is_anomaly=anomaly is not None,
                anomaly_direction=anomaly.direction if anomaly else None,
                z_score=anomaly.z_score if anomaly else None,
                planned_event_id=anomaly.planned_event_id if anomaly else None,
            )
        )
    return points


def _forecast_from_series(
    *,
    data: list[MetricSeriesPoint],
    interval: str | None,
) -> list[ForecastPoint]:
    """One-step-ahead forecast off the densified catalog-metric series.

    Bounded by the shared ``_FORECAST_MAX_POINTS`` cap for the COST half of the
    reason spelled out where it is defined: ``forecast_next_buckets`` fits its
    own robust STL/MSTL, and an hourly metric at the default 30d range is 720
    points — a 1.75 s fit.

    The roll-up half of that rationale does NOT apply here. A catalog-metric
    drilldown never rolls up: ``MonitoringDetailPage`` picks the metric scope's
    granularity from the collection interval regardless of range. The tail is
    invisible for a blunter reason — ``adaptMetricSeries`` returns
    ``forecast: []`` unconditionally for this scope, because a dashed tail
    trending toward 0 is misleading on a fractional metric. So the cap bounds a
    fit nothing currently reads; a future frontend that starts showing the tail
    inherits the cost argument above and nothing else.

    CPU-bound, so callers on the request path go through
    ``_forecast_off_event_loop`` rather than calling this directly.
    """
    if not data or not interval or len(data) > _FORECAST_MAX_POINTS:
        return []
    delta = get_interval(interval).delta
    # Floats, not ``round()``: ``SeriesPoint.count`` is float and the forecast is
    # scale-aware (tripl-68bc), so a ratio series no longer collapses to all
    # zeros before it is fitted. The ``isfinite`` guard is belt-and-braces —
    # ``_densify_value_rows`` already drops non-finite buckets — because
    # ``round()`` on a NaN/inf raised out of this line and 500'd the page.
    series_points = [
        SeriesPoint(bucket=point.bucket, count=point.value)
        for point in data
        if math.isfinite(point.value)
    ]
    return [
        ForecastPoint(
            bucket=point.bucket,
            expected_count=point.expected_count,
            stddev=point.stddev,
        )
        for point in forecast_next_buckets(series_points, interval=delta, horizon=1)
    ]


async def _forecast_off_event_loop(
    *,
    data: list[MetricSeriesPoint],
    interval: str | None,
) -> list[ForecastPoint]:
    """``_forecast_from_series`` on a worker thread — see the event-scope twin
    ``metrics_service._forecast_off_event_loop``. The cheap exits are taken here
    so only a real fit pays for the hop."""
    if not data or not interval or len(data) > _FORECAST_MAX_POINTS:
        return []
    return await asyncio.to_thread(_forecast_from_series, data=data, interval=interval)


def _latest_signal(
    *,
    data: list[MetricSeriesPoint],
    anomalies: list[MetricAnomaly],
    interval: str | None = None,
    recent_window: timedelta | None = None,
) -> MetricSignalResponse | None:
    if not anomalies:
        return None
    latest_metric_bucket = data[-1].bucket if data else None
    latest_anomaly = anomalies[-1]
    state = classify_signal_state(
        anomaly_bucket=latest_anomaly.bucket,
        latest_metric_bucket=latest_metric_bucket,
        # A catalog metric carries its OWN grid, often daily. Judged against a
        # bare 24h window it closes on the very day it fires.
        interval=scan_interval_to_timedelta(interval),
        recent_window=recent_window,
    )
    if state is None:
        return None
    return _signal_from_anomaly(latest_anomaly, state=state)


async def get_metric_series(
    session: AsyncSession,
    slug: str,
    metric_id: uuid.UUID,
    *,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> MetricSeriesResponse:
    project = await _resolve_project(session, slug)
    metric = await _resolve_metric(session, project, metric_id)
    interval, scan_config_id = await _resolve_metric_interval(session, metric)

    # "Not monitored" is not monitored on EVERY surface. A metric stops being
    # scored either by its own detection switch or by leaving ``active``
    # (``tripl.metric_monitoring``), and both leave the already-stored rows in
    # place, so this page reporting an open signal off those leftovers made the
    # four surfaces disagree the moment the metric was archived or switched off.
    # Only the SIGNAL is retracted: the stored anomalies still render as chart
    # markers, because the history is real and neither switch speaks about what
    # happened — only about what is watched.
    monitored = is_metric_monitored(metric)
    recent_window: timedelta | None = None
    series_from = time_from
    if monitored:
        recent_window = await _get_project_recent_signal_window(session, project.id)
        anomalies, series_from = await _load_anomalies_reaching_open_anchor(
            session,
            metric.id,
            time_from=time_from,
            time_to=time_to,
            interval=interval,
            recent_window=recent_window,
            scan_config_id=scan_config_id,
        )
    else:
        anomalies = await _load_metric_anomalies(
            session, metric.id, time_from=time_from, time_to=time_to
        )

    # ``series_from``, not ``time_from``: the chart and the signal must describe
    # ONE range, so a reach-back that widened the anomaly load widens this too.
    # The grid keeps the read on ONE POPULATION — the configs on the interval
    # that densifies the series just below, summed per bucket exactly as the
    # detector summed them to produce the ``anomalies`` above (see
    # ``_grid_population_filter``).
    value_rows = await _load_metric_values(
        session,
        metric.id,
        time_from=series_from,
        time_to=time_to,
        interval=interval,
        scan_config_id=scan_config_id,
    )
    data = _build_metric_series_points(
        interval=interval,
        value_rows=value_rows,
        anomalies=anomalies,
        count_shaped=is_count_shaped(metric),
    )
    response = MetricSeriesResponse(
        metric_id=metric.id,
        scan_config_id=scan_config_id,
        interval=interval,
        # Same predicate as the reach-back above.
        latest_signal=(
            _latest_signal(
                data=data,
                anomalies=anomalies,
                interval=interval,
                recent_window=recent_window,
            )
            if monitored
            else None
        ),
        # Override first, project setting behind it — the detector's two-step
        # for a ``metric`` scope, whose overrides are stored with no
        # ``scan_config_id`` (tripl-4cgl).
        sigma_threshold=await _apply_scope_sigma_override(
            session,
            project_id=project.id,
            scan_config_id=None,
            scope_type=SCOPE_METRIC,
            scope_ref=str(metric.id),
            fallback=await _get_project_sigma_threshold(session, project.id),
        ),
        data=data,
        forecast=await _forecast_off_event_loop(data=data, interval=interval),
    )
    # Verdicts ride along for the chart markers and the signal card (F01, #254).
    response = await signal_verdict_service.with_metric_series_verdicts(
        session, project.id, response
    )
    # The metric's owner on the signal card (F07, #260).
    if response.latest_signal is not None:
        (latest,) = await anomaly_attribution_service.attach_owners(
            session, project.id, [response.latest_signal]
        )
        response = response.model_copy(update={"latest_signal": latest})
    return response


async def _load_breakdown_value_rows(
    session: AsyncSession,
    metric_id: uuid.UUID,
    *,
    breakdown_column: str,
    time_from: datetime | None,
    time_to: datetime | None,
    interval: str | None,
    scan_config_id: uuid.UUID | None,
) -> dict[tuple[str, bool], list[tuple[datetime, float]]]:
    """Per-segment value rows, SUMMED per bucket over the metric's grid population.

    The same population as the Series tab's line (tripl-kom5): without the grid
    filter a metric collected on two intervals summed the retired grid's
    segments in, so the Breakdowns and version tabs disagreed with the series
    line beside them. See :func:`tripl.metric_grid.grid_population_filter`.
    """
    query = (
        select(
            MetricValueBreakdown.breakdown_value,
            MetricValueBreakdown.is_other,
            MetricValueBreakdown.bucket,
            func.sum(MetricValueBreakdown.value),
        )
        .where(
            MetricValueBreakdown.metric_definition_id == metric_id,
            MetricValueBreakdown.breakdown_column == breakdown_column,
            grid_population_filter(
                MetricValueBreakdown.scan_config_id,
                interval=interval,
                scan_config_id=scan_config_id,
            ),
        )
        .group_by(
            MetricValueBreakdown.breakdown_value,
            MetricValueBreakdown.is_other,
            MetricValueBreakdown.bucket,
        )
        .order_by(MetricValueBreakdown.breakdown_value, MetricValueBreakdown.bucket)
    )
    if time_from is not None:
        query = query.where(MetricValueBreakdown.bucket >= time_from)
    if time_to is not None:
        query = query.where(MetricValueBreakdown.bucket < time_to)

    rows_by_series: dict[tuple[str, bool], list[tuple[datetime, float]]] = {}
    for value, is_other, bucket, total in (await session.execute(query)).all():
        rows_by_series.setdefault((value, bool(is_other)), []).append((bucket, float(total)))
    return rows_by_series


async def get_metric_breakdowns(
    session: AsyncSession,
    slug: str,
    metric_id: uuid.UUID,
    *,
    column: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> MetricBreakdownsResponse:
    project = await _resolve_project(session, slug)
    metric = await _resolve_metric(session, project, metric_id)
    interval, scan_config_id = await _resolve_metric_interval(session, metric)
    columns = list(dict.fromkeys(metric.breakdown_columns or []))

    if column is not None and column not in columns:
        raise HTTPException(400, "Breakdown column is not configured for this metric")
    if not columns:
        return MetricBreakdownsResponse(
            metric_id=metric.id,
            scan_config_id=scan_config_id,
            interval=interval,
            columns=[],
            series=[],
        )

    selected_column = column
    if selected_column is None:
        data_columns = set(
            (
                await session.execute(
                    select(MetricValueBreakdown.breakdown_column)
                    .where(MetricValueBreakdown.metric_definition_id == metric.id)
                    .distinct()
                )
            ).scalars()
        )
        selected_column = next((item for item in columns if item in data_columns), columns[0])

    rows_by_series = await _load_breakdown_value_rows(
        session,
        metric.id,
        breakdown_column=selected_column,
        time_from=time_from,
        time_to=time_to,
        interval=interval,
        scan_config_id=scan_config_id,
    )
    series: list[MetricBreakdownSeries] = []
    # ``count_shaped``, like the series and version reads: a fractional metric's
    # missing breakdown bucket means "no data", not zero. The ratio collector
    # skips zero-denominator buckets outright
    # (``metric_collect._append_ratio_breakdown_rows``), so zero-filling them
    # here plotted a conversion rate as a hard drop to 0% wherever a segment
    # simply had no traffic.
    count_shaped = is_count_shaped(metric)
    for (value, is_other), value_rows in rows_by_series.items():
        points = _build_metric_series_points(
            interval=interval,
            value_rows=value_rows,
            anomalies=[],
            count_shaped=count_shaped,
        )
        series.append(
            MetricBreakdownSeries(
                breakdown_value=value,
                is_other=is_other,
                total_value=sum(point.value for point in points),
                data=points,
            )
        )
    series.sort(key=lambda item: (item.is_other, -item.total_value, item.breakdown_value))
    return MetricBreakdownsResponse(
        metric_id=metric.id,
        scan_config_id=scan_config_id,
        interval=interval,
        columns=columns,
        selected_column=selected_column,
        series=series,
    )


def _order_version_keys(
    keys: set[tuple[str, bool]], released: set[str] | None = None
) -> tuple[list[tuple[str, bool]], str | None]:
    explicit = {version for version, is_other in keys if not is_other}
    ordered_versions = order_versions(explicit, reverse=True)
    ordered = [(version, False) for version in ordered_versions]
    other = sorted((version, is_other) for version, is_other in keys if is_other)
    # The SemVer-max fallback for "latest" must skip prerelease/dev builds; when
    # ``released`` is omitted every version is eligible (prior behavior).
    latest_candidates = (
        ordered_versions if released is None else [v for v in ordered_versions if v in released]
    )
    latest = latest_candidates[0] if latest_candidates else None
    return ordered + other, latest


def _version_bucket_totals(
    value_rows_by_series: dict[tuple[str, bool], list[tuple[datetime, float]]],
) -> tuple[dict[str, dict[datetime, float]], dict[datetime, float]]:
    """Per-version and total per-bucket VALUE totals from the raw (pre-fold) rows.

    Only explicit (non-``is_other``) versions get a per-version series, but every
    row — including stored "Other" rows — feeds the traffic denominator so the
    activation shares are not inflated.
    """
    per_version: dict[str, dict[datetime, float]] = {}
    all_by_bucket: dict[datetime, float] = {}
    for (version, is_other), rows in value_rows_by_series.items():
        for bucket, value in rows:
            all_by_bucket[bucket] = all_by_bucket.get(bucket, 0.0) + value
            if not is_other:
                by_bucket = per_version.setdefault(version, {})
                by_bucket[bucket] = by_bucket.get(bucket, 0.0) + value
    return per_version, all_by_bucket


def _build_metric_version_series(
    *,
    interval: str | None,
    value_rows_by_series: dict[tuple[str, bool], list[tuple[datetime, float]]],
    maturity_rows_by_series: dict[tuple[str, bool], list[tuple[datetime, float]]] | None = None,
    keep_releases: int,
    count_shaped: bool = True,
    prerelease_pattern: re.Pattern[str] | None = None,
    share_min: float = DEFAULT_ACTIVE_SHARE_MIN,
) -> tuple[str | None, list[AppVersionInfo], list[MetricVersionSeries]]:
    """Read-time retention with project-total release maturity for all metric shapes.

    Chart values remain metric-local, but rollout maturity comes from project
    version totals. This makes a release's latest/pre-release state consistent
    across counts, ratios, and every event view. If project totals are not yet
    available, the metric's own rows remain a backwards-compatible fallback.
    """
    explicit = {version for version, is_other in value_rows_by_series if not is_other}
    maturity_rows = maturity_rows_by_series or value_rows_by_series
    per_version_totals, all_by_bucket = _version_bucket_totals(maturity_rows)

    # Prerelease/dev builds are ineligible to be latest/active and are subordinate
    # to released versions for retention. Maturity is computed only over released
    # project-total rows, so a prerelease is never marked active.
    released = released_versions(explicit, prerelease_pattern=prerelease_pattern)
    maturity_released = released_versions(
        {version for version, is_other in maturity_rows if not is_other},
        prerelease_pattern=prerelease_pattern,
    )
    released_totals = {
        version: by_bucket
        for version, by_bucket in per_version_totals.items()
        if version in maturity_released
    }
    # ``is_active`` for a FRACTIONAL metric is NOT "always False" — a share gate
    # on a ratio is meaningless, so the rule is:
    #   * project-total maturity rows available -> gate on PROJECT traffic share,
    #     which is a count and so is meaningful for every metric shape;
    #   * none available -> every released version counts as active, because the
    #     metric's own ratio rows cannot answer "does this release carry real
    #     traffic" and refusing to answer would retire every version at once.
    # Count-shaped metrics always take the share gate: their own rows are a
    # volume and can stand in for the project total.
    active_versions = (
        released
        if not maturity_rows_by_series and not count_shaped
        else active_release_versions(released_totals, all_by_bucket, share_min=share_min) & explicit
    )
    kept = _retained_versions(explicit, active_versions, keep_releases, released=released)

    def _display_key(version: str, is_other: bool) -> tuple[str, bool]:
        if not is_other and version in kept:
            return (version, False)
        return (APP_VERSION_OTHER_LABEL, True)

    def _fold_bucket(values: list[float]) -> float:
        """Combine the versions folded into one display line for one bucket.

        Folding is a SUM only for count-shaped metrics. A ratio / average / sql
        metric stores a LEVEL per version and levels do not add: three versions
        sitting near 0.30, plus the warehouse's own stored "Other" tail row,
        used to plot "Other" at 0.85 on a metric that cannot exceed 1.0. The
        per-version numerator and denominator are not stored, so the pooled
        level cannot be recomputed here; the MEAN is used instead. It always
        lands inside ``[min, max]`` of the folded levels — and so does the true
        pooled level — which keeps "Other" on the same scale as the lines it
        summarises. A kept version is alone under its own display key, so this
        is the identity for every non-folded series either way.
        """
        if count_shaped:
            return sum(values)
        return sum(values) / len(values)

    folded: dict[tuple[str, bool], dict[datetime, list[float]]] = {}
    for (version, is_other), rows in value_rows_by_series.items():
        bucket_values = folded.setdefault(_display_key(version, is_other), {})
        for bucket, value in rows:
            bucket_values.setdefault(bucket, []).append(value)
    rows_by_display = {
        key: sorted((bucket, _fold_bucket(values)) for bucket, values in buckets.items())
        for key, buckets in folded.items()
    }

    ordered_keys, semver_latest = _order_version_keys(set(rows_by_display), released=released)
    # Prefer the SemVer-max mature release from the shared project total; fall
    # back to the visible SemVer-max released version before any total is mature.
    # Prereleases are excluded from both, so never latest.
    latest_version = (
        order_versions(active_versions, reverse=True)[0] if active_versions else semver_latest
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
    series: list[MetricVersionSeries] = []
    for version, is_other in ordered_keys:
        points = _build_metric_series_points(
            interval=interval,
            value_rows=rows_by_display.get((version, is_other), []),
            anomalies=[],
            count_shaped=count_shaped,
        )
        series.append(
            MetricVersionSeries(
                version=version,
                is_other=is_other,
                is_latest=not is_other and version == latest_version,
                is_active=_is_active(version, is_other),
                total_value=sum(point.value for point in points),
                data=points,
            )
        )
    return latest_version, versions, series


async def _resolve_version_gate(
    session: AsyncSession,
    scan_config_id: uuid.UUID | None,
    project_keep_releases: int,
) -> tuple[int, re.Pattern[str] | None, float]:
    """Resolve the shared retention plus source-specific version gates.

    Retention is project-wide. The prerelease pattern and the activation-share
    floor come from the MATURITY scan — the one
    ``_resolve_maturity_scan_config_id`` picked, which is the metric's own scan
    when it is aligned to one and otherwise the source's highest-volume
    project-total scan. So a standalone SQL/fact metric is gated by that scan's
    settings too; it falls back to the module defaults only when no maturity
    scan could be resolved at all (``scan_config_id is None``, or the row has
    since been deleted).
    """
    if scan_config_id is not None:
        row = (
            await session.execute(
                select(
                    ScanConfig.app_version_prerelease_pattern,
                    ScanConfig.app_version_active_share_min,
                ).where(ScanConfig.id == scan_config_id)
            )
        ).first()
        if row is not None:
            pattern, share = row
            return (
                project_keep_releases,
                compile_prerelease_pattern(pattern),
                resolve_share_min(share),
            )
    return project_keep_releases, None, DEFAULT_ACTIVE_SHARE_MIN


async def _metric_source_data_source_id(
    session: AsyncSession,
    project_id: uuid.UUID,
    metric: MetricDefinition,
    scan_config_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Resolve the data source whose project total defines metric release maturity."""
    if scan_config_id is not None:
        return cast(
            uuid.UUID | None,
            await session.scalar(
                select(ScanConfig.data_source_id).where(
                    ScanConfig.id == scan_config_id,
                    ScanConfig.project_id == project_id,
                )
            ),
        )
    if metric.data_source_id is not None:
        return metric.data_source_id
    if metric.fact_table_id is not None:
        return cast(
            uuid.UUID | None,
            await session.scalar(
                select(FactTable.data_source_id).where(
                    FactTable.id == metric.fact_table_id,
                    FactTable.project_id == project_id,
                )
            ),
        )
    return None


async def _load_project_version_maturity_rows(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    scan_config_id: uuid.UUID | None,
    time_from: datetime | None,
    time_to: datetime | None,
) -> dict[tuple[str, bool], list[tuple[datetime, float]]]:
    """Load project-total version traffic from one canonical scan."""
    if scan_config_id is None:
        return {}

    query = (
        select(
            EventMetricBreakdown.breakdown_value,
            EventMetricBreakdown.is_other,
            EventMetricBreakdown.bucket,
            func.sum(EventMetricBreakdown.count),
        )
        .join(ScanConfig, EventMetricBreakdown.scan_config_id == ScanConfig.id)
        .where(
            ScanConfig.id == scan_config_id,
            ScanConfig.project_id == project_id,
            ScanConfig.app_version_column.is_not(None),
            EventMetricBreakdown.breakdown_column == ScanConfig.app_version_column,
            EventMetricBreakdown.event_id.is_(None),
            EventMetricBreakdown.event_type_id.is_not(None),
        )
        .group_by(
            EventMetricBreakdown.breakdown_value,
            EventMetricBreakdown.is_other,
            EventMetricBreakdown.bucket,
        )
        .order_by(EventMetricBreakdown.bucket)
    )
    if time_from is not None:
        query = query.where(EventMetricBreakdown.bucket >= time_from)
    if time_to is not None:
        query = query.where(EventMetricBreakdown.bucket < time_to)

    rows_by_series: dict[tuple[str, bool], list[tuple[datetime, float]]] = {}
    for version, is_other, bucket, count in (await session.execute(query)).all():
        rows_by_series.setdefault((str(version), bool(is_other)), []).append((bucket, float(count)))
    return rows_by_series


async def _resolve_maturity_scan_config_id(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    data_source_id: uuid.UUID | None,
    metric_scan_config_id: uuid.UUID | None,
    time_from: datetime | None,
    time_to: datetime | None,
) -> uuid.UUID | None:
    """Resolve one scan whose project total defines a metric's rollout state.

    Metrics aligned to a scan always reuse that exact scan. Standalone SQL/fact
    metrics choose the source scan with the largest project-total volume in the
    requested window, which avoids mixing overlapping scans or bucket grids.
    """
    if metric_scan_config_id is not None:
        return cast(
            uuid.UUID | None,
            await session.scalar(
                select(ScanConfig.id).where(
                    ScanConfig.id == metric_scan_config_id,
                    ScanConfig.project_id == project_id,
                    ScanConfig.app_version_column.is_not(None),
                )
            ),
        )
    if data_source_id is None:
        return None

    query = (
        select(ScanConfig.id)
        .join(EventMetricBreakdown, EventMetricBreakdown.scan_config_id == ScanConfig.id)
        .where(
            ScanConfig.project_id == project_id,
            ScanConfig.data_source_id == data_source_id,
            ScanConfig.app_version_column.is_not(None),
            EventMetricBreakdown.breakdown_column == ScanConfig.app_version_column,
            EventMetricBreakdown.event_id.is_(None),
            EventMetricBreakdown.event_type_id.is_not(None),
        )
        .group_by(ScanConfig.id)
        .order_by(func.sum(EventMetricBreakdown.count).desc(), ScanConfig.id)
        .limit(1)
    )
    if time_from is not None:
        query = query.where(EventMetricBreakdown.bucket >= time_from)
    if time_to is not None:
        query = query.where(EventMetricBreakdown.bucket < time_to)
    return cast(uuid.UUID | None, await session.scalar(query))


async def get_metric_version_series(
    session: AsyncSession,
    slug: str,
    metric_id: uuid.UUID,
    *,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> MetricVersionSeriesResponse:
    project = await _resolve_project(session, slug)
    metric = await _resolve_metric(session, project, metric_id)
    interval, scan_config_id = await _resolve_metric_interval(session, metric)
    if not metric.app_version_column:
        return MetricVersionSeriesResponse(
            metric_id=metric.id,
            scan_config_id=scan_config_id,
            app_version_column=None,
            interval=interval,
            versions=[],
            series=[],
        )

    value_rows_by_series = await _load_breakdown_value_rows(
        session,
        metric.id,
        breakdown_column=metric.app_version_column,
        time_from=time_from,
        time_to=time_to,
        interval=interval,
        scan_config_id=scan_config_id,
    )
    maturity_scan_config_id = await _resolve_maturity_scan_config_id(
        session,
        project_id=project.id,
        data_source_id=await _metric_source_data_source_id(
            session,
            project.id,
            metric,
            scan_config_id,
        ),
        metric_scan_config_id=scan_config_id,
        time_from=time_from,
        time_to=time_to,
    )
    maturity_rows_by_series = await _load_project_version_maturity_rows(
        session,
        project_id=project.id,
        scan_config_id=maturity_scan_config_id,
        time_from=time_from,
        time_to=time_to,
    )
    keep_releases, prerelease_pattern, share_min = await _resolve_version_gate(
        session,
        maturity_scan_config_id,
        project.app_version_keep_releases,
    )
    latest_version, versions, series = _build_metric_version_series(
        interval=interval,
        value_rows_by_series=value_rows_by_series,
        maturity_rows_by_series=maturity_rows_by_series,
        keep_releases=keep_releases,
        count_shaped=is_count_shaped(metric),
        prerelease_pattern=prerelease_pattern,
        share_min=share_min,
    )
    return MetricVersionSeriesResponse(
        metric_id=metric.id,
        scan_config_id=scan_config_id,
        app_version_column=metric.app_version_column,
        interval=interval,
        latest_version=latest_version,
        versions=versions,
        series=series,
    )
