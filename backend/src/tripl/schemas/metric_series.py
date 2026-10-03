"""Read-side response models for catalog-metric (MetricDefinition) series.

These mirror the event-metric shapes in ``schemas.event_metric`` but carry a
float ``value`` instead of an integer ``count`` — a catalog metric can be a
ratio or average, not just a count — and reuse the shared ``ForecastPoint`` /
``MetricSignalResponse`` / ``AppVersionInfo`` models so the frontend renders a
metric series with the same chart primitives it already uses for events.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from tripl.models.domain_enums import AnomalyDirection, ScanInterval
from tripl.schemas.event_metric import (
    AppVersionInfo,
    ForecastPoint,
    MetricSignalResponse,
    SignalVerdictInfo,
)


class MetricSeriesPoint(BaseModel):
    """One densified point of a catalog-metric series.

    Mirrors ``EventMetricPoint`` but stores a float ``value``. ``expected_count``
    / ``stddev`` / ``z_score`` / ``anomaly_direction`` are only populated for
    buckets that carry an anomaly row; the rest leave them ``None``.
    """

    bucket: datetime
    value: float
    expected_count: float | None = None
    stddev: float | None = None
    is_anomaly: bool = False
    anomaly_direction: AnomalyDirection | None = None
    z_score: float | None = None
    # See ``EventMetricPoint.verdict`` (F01, #254); omitted when NULL.
    verdict: SignalVerdictInfo | None = Field(default=None, exclude_if=lambda value: value is None)
    # See ``EventMetricPoint.planned_event_id`` (F18); omitted when NULL.
    planned_event_id: uuid.UUID | None = Field(default=None, exclude_if=lambda value: value is None)


class MetricSeriesResponse(BaseModel):
    metric_id: uuid.UUID
    scope: str = "metric"
    scan_config_id: uuid.UUID | None = None
    interval: ScanInterval | None = None
    latest_signal: MetricSignalResponse | None = None
    # The confidence-band multiplier, resolved per scope the way the detector
    # scores it: the project setting narrowed by this metric's false-positive
    # override (``metrics_service._apply_scope_sigma_override``). Required, not
    # defaulted like ``EventMetricsResponse.sigma_threshold``, so an unfilled
    # value cannot ship silently at 4.0 (tripl-4cgl).
    sigma_threshold: float
    data: list[MetricSeriesPoint]
    forecast: list[ForecastPoint] = []


class MetricBreakdownSeries(BaseModel):
    breakdown_value: str
    is_other: bool = False
    total_value: float
    data: list[MetricSeriesPoint]


class MetricBreakdownsResponse(BaseModel):
    metric_id: uuid.UUID
    scan_config_id: uuid.UUID | None = None
    interval: ScanInterval | None = None
    columns: list[str]
    selected_column: str | None = None
    series: list[MetricBreakdownSeries]


class MetricVersionSeries(BaseModel):
    version: str
    is_other: bool = False
    is_latest: bool = False
    # True once the release takes a real share of traffic (activation gate),
    # mirroring ``AppVersionMetricSeries.is_active``. For a FRACTIONAL metric the
    # gate is on PROJECT traffic share when project-total maturity rows exist —
    # a count, so meaningful for any metric shape — and without them every
    # released version is active, because the metric's own ratio rows cannot
    # answer "does this release carry real traffic" and refusing to answer would
    # retire them all at once. Prereleases are never active. See
    # ``metric_series_service._build_metric_version_series``.
    is_active: bool = False
    total_value: float
    data: list[MetricSeriesPoint]


class MetricVersionSeriesResponse(BaseModel):
    metric_id: uuid.UUID
    scan_config_id: uuid.UUID | None = None
    app_version_column: str | None = None
    interval: ScanInterval | None = None
    latest_version: str | None = None
    versions: list[AppVersionInfo]
    series: list[MetricVersionSeries]
