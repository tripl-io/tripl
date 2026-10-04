/**
 * Catalog-metric adapters for MonitoringDetailPage.
 *
 * Catalog metric series mirror the event-volume shapes almost exactly (the only
 * real difference is a float `value` instead of an integer `count`), so the
 * `metric` scope reuses every drilldown consumer by mapping its catalog
 * responses onto the event-shaped types the tabs already render.
 */
import type {
  AppVersionSeriesResponse,
  EventMetricBreakdownsResponse,
  EventMetricPoint,
  EventMetricsResponse,
  MetricBreakdownsResponse,
  MetricSeriesPoint,
  MetricSeriesResponse,
  MetricSignalResponse,
  MetricVersionSeriesResponse,
  MonitoringSignal,
  SignalIncidentRef,
  SignalVerdict,
} from '@/types'

/**
 * The verdict fields (#254) the metric payloads carry on a flagged point and
 * on the latest signal. Read structurally: the generated catalog types gain
 * them only once the OpenAPI schema is regenerated, and until then an older
 * payload simply has neither.
 */
interface VerdictFields {
  verdict?: SignalVerdict | null
  incident?: SignalIncidentRef | null
}

// Left undefined, not null, when the payload has none: an absent verdict is
// "unknown", and the points stay the shape they always were.
function verdictFieldsOf(source: object): VerdictFields {
  const { verdict, incident } = source as VerdictFields
  return { verdict, incident }
}
import {
  coarserGranularity,
  defaultGranularityForRange,
  type MetricRollupMode,
  type MetricsGranularity,
} from '@/lib/metrics'

/**
 * The chart granularity matching each backend collection interval
 * (backend/src/tripl/core/intervals.py), so the axis describes the buckets the
 * data actually has. A table rather than a chain of ternaries: that chain once
 * covered only `1d` and `1w`, so a `15m` or `6h` metric fell through to "Hours".
 */
const GRANULARITY_FOR_INTERVAL: Record<string, MetricsGranularity> = {
  '15m': '15min',
  '1h': 'hour',
  '6h': '6h',
  '1d': 'day',
  '1w': 'week',
}

/** The granularity a series was collected at, or null for an unknown interval. */
export function granularityForInterval(interval: string | null | undefined): MetricsGranularity | null {
  return GRANULARITY_FOR_INTERVAL[interval ?? ''] ?? null
}

/**
 * The one default-granularity rule every drilldown scope shares (MON-43): the
 * range's readable default, but never finer than the collection interval — a
 * daily metric at 7d charts days, an hourly one hours. It used to be two rules
 * (interval for metrics, range for everything else), so moving between a
 * metric and its event changed the bucket size for no visible reason.
 */
export function defaultDrilldownGranularity(
  rangeDays: number,
  interval: string | null | undefined,
): MetricsGranularity {
  const byRange = defaultGranularityForRange(rangeDays)
  const native = granularityForInterval(interval)
  return native ? coarserGranularity(native, byRange) : byRange
}

type MetricShape = {
  kind?: string
  aggregation?: string | null
  composition?: string | null
}

/**
 * How a catalog metric rolls up to a coarser bucket (MON-2 / MET-12).
 *
 * Only additive metrics sum: a single-event composition (a count) and a fact
 * `count` or `sum`. Ratios, averages, min/max, distinct counts and free SQL are
 * not additive, so they average — summing 24 hourly values of an 8 % rate plots
 * 192 %, and summing hourly distinct users counts the same user 24 times.
 *
 * `undefined` (a non-metric scope) is an event volume, which sums.
 */
export function metricRollupMode(metric: MetricShape | undefined): MetricRollupMode {
  if (!metric) return 'sum'
  if (metric.kind === 'sql') return 'mean'
  if (metric.kind === 'event_composition') return metric.composition === 'single' ? 'sum' : 'mean'
  if (metric.kind === 'fact') {
    if (metric.composition === 'ratio') return 'mean'
    return metric.aggregation === 'count' || metric.aggregation === 'sum' ? 'sum' : 'mean'
  }
  return 'sum'
}

export function metricPointToEventPoint(point: MetricSeriesPoint): EventMetricPoint {
  return {
    bucket: point.bucket,
    count: point.value,
    expected_count: point.expected_count ?? null,
    stddev: point.stddev ?? null,
    is_anomaly: point.is_anomaly,
    anomaly_direction: point.anomaly_direction ?? null,
    z_score: point.z_score ?? null,
    ...verdictFieldsOf(point),
    ...(point.planned_event_id ? { planned_event_id: point.planned_event_id } : {}),
  }
}

export function metricSignalToMonitoringSignal(signal: MetricSignalResponse): MonitoringSignal {
  return {
    scan_config_id: signal.scan_config_id ?? '',
    scope_type: signal.scope_type,
    scope_ref: signal.scope_ref,
    state: signal.state === 'recent' ? 'recent' : 'latest_scan',
    event_id: signal.event_id ?? null,
    event_type_id: signal.event_type_id ?? null,
    bucket: signal.bucket,
    actual_count: signal.actual_count,
    expected_count: signal.expected_count,
    stddev: signal.stddev,
    z_score: signal.z_score,
    direction: signal.direction,
    unit: signal.unit ?? null,
    detected_at: signal.detected_at ?? null,
    // Catalog metric-scope signals are never an incident rollup child.
    incident_child: false,
    ...verdictFieldsOf(signal),
  }
}

export function adaptMetricSeries(res: MetricSeriesResponse): EventMetricsResponse {
  return {
    scope: 'event',
    scan_config_id: res.scan_config_id ?? null,
    event_id: null,
    event_type_id: null,
    interval: res.interval ?? null,
    latest_signal: res.latest_signal ? metricSignalToMonitoringSignal(res.latest_signal) : null,
    data: res.data.map(metricPointToEventPoint),
    // Per-metric forecasting renders a dashed tail that trends toward 0, which
    // is misleading for fractional (ratio/avg) catalog metrics. Drop it for the
    // metric scope; event-scope forecasts come from their own endpoint and are
    // left untouched.
    forecast: [],
    // The project sigma narrowed by this metric's false-positive override,
    // the multiplier the detector scored it with.
    sigma_threshold: res.sigma_threshold,
  }
}

export function adaptMetricVersions(res: MetricVersionSeriesResponse): AppVersionSeriesResponse {
  // Both res.series and res.versions carry is_active, and the backend fills them
  // from the same gate. Read it off the versions catalog because that is the list
  // this adapter walks below, so the metric scope gets the same pre-release
  // treatment as the event scope.
  const activeByVersion = new Map(res.versions.map(info => [info.version, info.is_active]))
  return {
    scan_config_id: res.scan_config_id ?? '',
    scope_type: 'event',
    scope_ref: '',
    event_id: null,
    event_type_id: null,
    app_version_column: res.app_version_column ?? null,
    interval: res.interval ?? null,
    latest_version: res.latest_version ?? null,
    versions: res.versions,
    series: res.series.map(series => ({
      version: series.version,
      is_other: series.is_other,
      is_latest: series.is_latest,
      is_active: activeByVersion.get(series.version) ?? false,
      total_count: series.total_value,
      data: series.data.map(metricPointToEventPoint),
    })),
  }
}

export function adaptMetricBreakdowns(res: MetricBreakdownsResponse): EventMetricBreakdownsResponse {
  return {
    event_id: res.metric_id,
    scan_config_id: res.scan_config_id ?? null,
    interval: res.interval ?? null,
    columns: res.columns,
    selected_column: res.selected_column ?? null,
    series: res.series.map(series => ({
      breakdown_value: series.breakdown_value,
      is_other: series.is_other,
      total_count: series.total_value,
      data: series.data.map(metricPointToEventPoint),
      parity_anomalies: [],
    })),
  }
}
