import { Fragment, useCallback, useId, useMemo, useRef, useState } from 'react'
import { Rocket, Tag } from 'lucide-react'
import {
  Area,
  Bar,
  ComposedChart,
  CartesianGrid,
  ErrorBar,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { cn } from '@/lib/utils'
import { COARSE_POINTER_QUERY, useMediaQuery } from '@/hooks/useMediaQuery'
import {
  axisWidthForValues,
  CHART_SURFACE_TAB_INDEX,
  dayBoundaryTicks,
  EVENTS_NOUN,
  formatAnomalyCount,
  formatCount,
  formatDayTick,
  formatSeriesValue,
  formatTick,
  formatTooltipLabel,
  isSubDayGranularity,
  SERIES_COLORS,
  seriesNounPlural,
  summarizeBuckets,
  summarizeForecastRange,
  type SeriesNoun,
} from '@/components/ui/chart-format'
import { formatDateTime } from '@/lib/datetime'
import { APP_LOCALE, formatNumber } from '@/lib/format'
import type { MetricsGranularity } from '@/lib/metrics'
import { ratioDelta } from '@/lib/percentDelta'
import { useTheme, type ChartStyle } from '@/components/theme-provider'
import type { ChartAnnotation, ChartAnnotationSource, EventMetricPoint, ForecastPoint } from '@/types'
import {
  annotationMarkerColor,
  annotationSourceLabel,
  isAutomaticAnnotation,
  safeAnnotationUrl,
  truncateAnnotationLabel,
} from '@/lib/chartAnnotations'
import { Checkbox } from '@/components/ui/checkbox'
import { useShowReleases } from '@/hooks/useShowReleases'
import { signalDirectionColor } from '@/lib/statusLexicon'
import { windowPaddingBuckets } from '@/components/ui/chart-window'

/**
 * The default colour of a single-series chart (DS-27): the first categorical
 * slot, a fixed hue that does not follow the user's accent. "Volume over time"
 * used to be drawn in the accent on one page, teal on another and violet on a
 * third. Anomalies are marked with danger dots and bands, never by recolouring
 * the line, so pass `color` only for a series that means something else.
 */
export const SINGLE_SERIES_COLOR = SERIES_COLORS[0]

interface MetricsChartProps {
  data: EventMetricPoint[]
  forecast?: ForecastPoint[]
  annotations?: ChartAnnotation[]
  className?: string
  color?: string
  height?: number
  granularity?: MetricsGranularity
  /** What the values count; the pair form pluralizes ("1 event"). */
  seriesLabel?: SeriesNoun
  /**
   * Optional formatter for the numeric values on the Y axis and in the
   * tooltip (e.g. percent-unit catalog metrics render stored fractions ×100).
   * The formatted string carries its own unit, so the tooltip skips the
   * `seriesLabel` suffix. When omitted, the chart keeps its default
   * compact-count ticks and `value seriesLabel` tooltip lines.
   */
  valueFormatter?: (value: number) => string
  /**
   * Formats the tooltip values when they need more than the axis does: a tick
   * leaves a trailing unit off ('0.0045'), the tooltip spells the value out
   * ('0.0045 s', '$1,234'). Falls back to `valueFormatter`. Like it, the
   * string carries its own unit, so the `seriesLabel` suffix is skipped.
   */
  tooltipFormatter?: (value: number) => string
  /**
   * The sigma threshold the detector actually scored this scope with, served on
   * the metrics response as `sigma_threshold`: the PROJECT setting, narrowed by
   * any false-positive scope override (`metrics_service._apply_scope_sigma_override`).
   * The confidence band is drawn as `expected ± sigmaThreshold * stddev` using
   * the STORED effective stddev, so a flagged point sits outside the band.
   * Falls back to `DEFAULT_SIGMA_THRESHOLD` when the payload carries none.
   */
  sigmaThreshold?: number
  /**
   * Clamp the confidence and forecast bands at zero (MON-21). A count cannot be
   * negative, but `expected - k·σ` can, and the band then dragged the axis
   * below zero on every quiet scope. Defaults to on for count series — no
   * `valueFormatter` — and off for catalog metrics, which may be signed.
   */
  nonNegative?: boolean
  /**
   * The window the reader asked for. The x-axis is padded with empty buckets
   * out to it, so a series that starts late in a 30-day range is drawn at its
   * real position rather than stretched edge-to-edge (MON-22).
   */
  from?: string
  to?: string
  /**
   * Rolled-up buckets the data does not fully cover (MO-5). A 30-day chart in
   * days starts mid-day and ends at "now", so its first and last buckets hold
   * a fraction of a day and drew as cliffs. They are drawn dashed with a
   * hollow point and named in the tooltip instead. `first` is the instant the
   * data starts inside the first bucket, `last` the instant it runs through in
   * the last one.
   */
  partial?: PartialWindow
  /** Draw the legend under the plot (MO-1): only the marks the chart has. */
  legend?: boolean
}

export interface PartialWindow {
  first?: string
  last?: string
}

interface MiniMetricsChartProps {
  data: EventMetricPoint[]
  className?: string
  color?: string
  height?: number
  label?: string
}

interface MetricsMultiSeriesChartProps {
  series: Array<{
    label: string
    data: EventMetricPoint[]
    color?: string
    /**
     * SVG dash pattern. The palette has eight hues, so a ninth series reuses
     * the first one's colour and needs a second cue to stay distinguishable
     * (MON-29).
     */
    dash?: string
    isHighlighted?: boolean
  }>
  className?: string
  height?: number
  granularity?: MetricsGranularity
  /** See MetricsChartProps.seriesLabel. */
  seriesLabel?: SeriesNoun
  emptyLabel?: string
  /**
   * Optional formatter for Y-axis ticks and tooltip values, mirroring
   * `MetricsChartProps.valueFormatter` (percent-unit catalog metrics render
   * stored fractions ×100). The formatted string carries its own unit, so the
   * tooltip skips the `seriesLabel` suffix. When omitted, the chart keeps its
   * default compact-count ticks and `value seriesLabel` tooltip lines.
   */
  valueFormatter?: (value: number) => string
  /** See MetricsChartProps.tooltipFormatter. */
  tooltipFormatter?: (value: number) => string
  /** See MetricsChartProps.from / .to (MON-22). */
  from?: string
  to?: string
}

function collectChartYValues(data: ChartDataPoint[]): number[] {
  const values: number[] = []
  for (const point of data) {
    if (point.count != null) values.push(point.count)
    if (point.expected_count != null) values.push(point.expected_count)
    if (point.band) values.push(point.band[0], point.band[1])
    if (point.forecast_expected != null) values.push(point.forecast_expected)
    if (point.forecast_band) values.push(point.forecast_band[0], point.forecast_band[1])
  }
  return values
}

function collectMultiSeriesYValues(
  rows: Array<Record<string, string | number | boolean>>,
): number[] {
  const values: number[] = []
  for (const row of rows) {
    for (const value of Object.values(row)) {
      if (typeof value === 'number') values.push(value)
    }
  }
  return values
}

/**
 * X-axis tick props for a bucket axis. A sub-day series spanning two days or
 * more gets one tick per local day, labelled with the date alone; anything
 * shorter keeps recharts' spacing and the full bucket label (LIVE-27).
 * `preserveStartEnd` keeps the first day when the axis is too narrow for every
 * tick — the default `preserveEnd` dropped it at 768px.
 */
function useTimeAxisTicks(
  rows: Array<{ bucket: string }>,
  granularity: MetricsGranularity,
) {
  const ticks = useMemo(
    () => dayBoundaryTicks(rows.map(row => row.bucket), granularity),
    [rows, granularity],
  )
  return ticks
    ? {
        ticks,
        interval: 'preserveStartEnd' as const,
        tickFormatter: (value: unknown) => formatDayTick(String(value)),
      }
    : { tickFormatter: (value: unknown) => formatTick(String(value), granularity) }
}

/**
 * Track whether the chart's container has a positive on-screen size. Recharts'
 * ResponsiveContainer logs "The width(-1) and height(-1) of chart should be
 * greater than 0" when it mounts inside a zero-size parent (a collapsed tab, or
 * a collapsible mid-open animation). Gate the ResponsiveContainer on a real
 * measured size so recharts never receives -1. The wrapper keeps its fixed
 * height and `w-full` regardless, so nothing shifts while we wait for a size.
 *
 * A callback ref, not a mount-time effect: MiniMetricsChart attaches the ref
 * only once it has data, so an effect that ran once on mount found no element
 * and left a chart that later received points blank; and after data -> [] ->
 * data it kept observing the detached old node. The callback re-runs on every
 * attach and detach.
 */
function useChartContainerReady() {
  const [ready, setReady] = useState(false)
  const observerRef = useRef<ResizeObserver | null>(null)

  const ref = useCallback((element: HTMLDivElement | null) => {
    observerRef.current?.disconnect()
    observerRef.current = null
    if (!element) {
      setReady(false)
      return
    }
    const measure = () => {
      const { width, height } = element.getBoundingClientRect()
      setReady(width > 0 && height > 0)
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    observerRef.current = observer
  }, [])

  return { ref, ready }
}

// Fallback confidence-band multiplier when the series response does not carry a
// `sigma_threshold` (e.g. older payloads). The band is drawn as
// `expected ± sigma_threshold * effective_stddev` using the STORED effective
// stddev the backend serves in `stddev`, so "outside the band" == "flagged".
// 4.0 is the detector's own default, `ProjectAnomalySettings.sigma_threshold`
// (models/project_anomaly_settings.py). It is a PROJECT setting, not a
// scan-config one — the scan-config copy has no reader left in the backend.
// This was 3.0 with a comment claiming the scan-config default, wrong on both
// counts, so a payload without a threshold drew a band a quarter too narrow and
// made unflagged buckets look flagged (tripl-0zpq.299).
//
// Every scope now serves a real per-scope sigma (event, event-type and
// project-total since tripl-0zpq.299; events-total since tripl-e443; the catalog
// metric since tripl-4cgl, threaded through `adaptMetricSeries`), so this
// constant only covers a payload that predates the field.
const DEFAULT_SIGMA_THRESHOLD = 4

interface ChartDataPoint {
  bucket: string
  count: number | null
  expected_count: number | null
  stddev: number | null
  is_anomaly?: boolean
  anomaly_direction?: 'spike' | 'drop' | null
  z_score?: number | null
  band?: [number, number]
  /** `band` as offsets from `expected_count`, the shape ErrorBar reads. */
  expected_error?: [number, number]
  forecast_expected?: number
  forecast_band?: [number, number]
  forecast_error?: [number, number]
  is_forecast?: boolean
  /** The value drawn solid; null on a partial bucket (MO-5). */
  solid_count?: number | null
  /** The dashed stub into a partial bucket: the partial point and its neighbour. */
  partial_count?: number | null
  partial_from?: string
  partial_through?: string
}

/**
 * Below this many points a line is drawn straight between the measurements,
 * with a small dot on each: monotone smoothing over a handful of daily or
 * weekly buckets invented a trend between them and hid where the real points
 * were (MO-6).
 */
const SMOOTH_MIN_POINTS = 60
const POINT_DOTS_MAX_POINTS = 30

type CurveType = 'linear' | 'monotone'

function curveFor(pointCount: number): CurveType {
  return pointCount > SMOOTH_MIN_POINTS ? 'monotone' : 'linear'
}

/** Flag the partial first/last buckets and split the series around them (MO-5). */
function markPartialBuckets(points: ChartDataPoint[], partial: PartialWindow) {
  for (const point of points) point.solid_count = point.count
  const first = points[0]
  const last = points[points.length - 1]
  if (partial.first && first) {
    first.partial_from = partial.first
    first.solid_count = null
    first.partial_count = first.count
    const next = points[1]
    if (next) next.partial_count = next.count
  }
  if (partial.last && last) {
    last.partial_through = partial.last
    last.solid_count = null
    last.partial_count = last.count
    const previous = points[points.length - 2]
    if (previous) previous.partial_count = previous.count
  }
}

/**
 * The expected value and stddev a bucket's band is drawn from (tripl-i9mt.25).
 * A flagged bucket uses its anomaly row's (`expected_count`/`stddev`): a trend
 * row's expectation is not the per-bucket one. Every other scored bucket uses
 * the baseline the detector stored for it; an unscored bucket has none.
 */
function bandSource(point: EventMetricPoint): [number, number] | null {
  if (point.expected_count != null && point.stddev != null) {
    return [point.expected_count, point.stddev]
  }
  if (point.baseline_expected != null && point.baseline_stddev != null) {
    return [point.baseline_expected, point.baseline_stddev]
  }
  return null
}

/**
 * Whether the expected-value line paints a hollow point on this row: exactly
 * where it carries the normal-range whisker (a flagged bucket, or one whose
 * neighbours have no band), so the whisker is never a bare error bar and a run
 * of scored buckets stays a plain dashed line.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function hasExpectedDot(point: ChartDataPoint | undefined): boolean {
  return point?.expected_count != null && point.expected_error != null
}

// Exported for unit tests only — recharts never paints in jsdom, so the band
// geometry is verified on the pure builder.
// eslint-disable-next-line react-refresh/only-export-components
export function buildChartData(
  data: EventMetricPoint[],
  forecast: ForecastPoint[] = [],
  sigmaThreshold: number = DEFAULT_SIGMA_THRESHOLD,
  nonNegative = false,
  partial?: PartialWindow,
): ChartDataPoint[] {
  // Robust to a missing/invalid served threshold: fall back to the default.
  const k = Number.isFinite(sigmaThreshold) && sigmaThreshold > 0
    ? sigmaThreshold
    : DEFAULT_SIGMA_THRESHOLD
  // A count's band stops at zero (MON-21); a signed metric's does not.
  const floor = (value: number) => (nonNegative ? Math.max(0, value) : value)
  const points: ChartDataPoint[] = data.map(point => {
    const baseline = bandSource(point)
    if (baseline == null) {
      return { ...point }
    }
    const [expected, stddev] = baseline
    const offset = k * stddev
    const band: [number, number] = [floor(expected - offset), expected + offset]
    return {
      ...point,
      expected_count: expected,
      stddev,
      band,
    }
  })
  // The normal range as a whisker on the expected point wherever the area
  // cannot paint it: a bucket whose neighbours carry no band (MO-1). With
  // stored baselines most scored buckets form a run the area fills; a flagged
  // bucket keeps its whisker regardless, so its range reads at a glance.
  points.forEach((point, index) => {
    if (!point.band || point.expected_count == null) return
    const isolated = !points[index - 1]?.band && !points[index + 1]?.band
    if (!point.is_anomaly && !isolated) return
    point.expected_error = [point.expected_count - point.band[0], point.band[1] - point.expected_count]
  })

  if (partial?.first || partial?.last) markPartialBuckets(points, partial)

  // The forecast is its own hollow point with a whisker for its range, not a
  // dashed line from the last actual: drawn from a spike, that line fell
  // steeply at the right edge and read as "then it crashed" (MO-7).
  if (points.length > 0) {
    for (const point of forecast) {
      const offset = k * point.stddev
      const expected = nonNegative ? Math.max(0, point.expected_count) : point.expected_count
      const band: [number, number] = [floor(point.expected_count - offset), point.expected_count + offset]
      points.push({
        bucket: point.bucket,
        count: null,
        expected_count: null,
        stddev: null,
        forecast_expected: expected,
        forecast_band: band,
        forecast_error: [Math.max(0, expected - band[0]), Math.max(0, band[1] - expected)],
        is_forecast: true,
      })
    }
  }

  return points
}

/** Empty padding rows, typed for whichever row shape the chart uses (MON-22). */
function padRows<Row extends { bucket: string }>(
  rows: Row[],
  window: { from?: string; to?: string },
  granularity: MetricsGranularity,
  empty: (bucket: string) => Row,
): Row[] {
  const first = rows[0]
  const last = rows[rows.length - 1]
  if (!first || !last || (!window.from && !window.to)) return rows
  const { before, after } = windowPaddingBuckets(first.bucket, last.bucket, window, granularity)
  if (!before.length && !after.length) return rows
  return [...before.map(empty), ...rows, ...after.map(empty)]
}

// Exported for unit tests only — recharts never paints in jsdom.
// eslint-disable-next-line react-refresh/only-export-components
export function padChartData(
  rows: ChartDataPoint[],
  window: { from?: string; to?: string },
  granularity: MetricsGranularity,
): ChartDataPoint[] {
  return padRows(rows, window, granularity, bucket => ({
    bucket,
    count: null,
    expected_count: null,
    stddev: null,
  }))
}

/**
 * The anomaly line of a tooltip: which way it moved and how far (MON-17). The
 * dot was the only mark, in one red for spikes and drops alike, and hovering it
 * said nothing the plain line did not.
 */
function AnomalyTooltipLine({
  direction,
  zScore,
  prefix = 'Anomaly',
}: {
  direction?: 'spike' | 'drop' | null
  zScore?: number | null
  prefix?: string
}) {
  const z = zScore != null && Number.isFinite(zScore) ? ` (z=${zScore.toFixed(1)})` : ''
  return (
    <p
      className="text-body-sm font-medium"
      style={{ color: direction ? signalDirectionColor(direction) : 'var(--danger)' }}
    >
      {prefix}: {direction ?? 'flagged'}{z}
    </p>
  )
}

// Exported for unit tests only — recharts never paints its tooltip in jsdom.
export function CustomTooltip({
  active,
  payload,
  label,
  granularity,
  seriesLabel,
  valueFormatter: axisFormatter,
  tooltipFormatter,
}: {
  active?: boolean
  payload?: Array<{ value: number; dataKey?: string; payload: ChartDataPoint }>
  label?: string | number
  granularity: MetricsGranularity
  seriesLabel: SeriesNoun
  valueFormatter?: (value: number) => string
  tooltipFormatter?: (value: number) => string
  /**
   * Accepted for callers; the tooltip no longer prints "±Nσ band" (MO-38).
   * The legend names the band's width instead.
   */
  sigmaThreshold?: number
}) {
  const point = payload?.[0]?.payload
  if (!active || !point) return null
  const valueFormatter = tooltipFormatter ?? axisFormatter
  const heading = formatTooltipHeading(String(label ?? ''), granularity)

  if (point.is_forecast && point.forecast_expected != null) {
    return (
      <div className="rounded-card border border-dashed bg-popover text-popover-foreground px-3 py-2 shadow-md">
        <p className="text-body-sm text-fg-tertiary">{heading} · forecast</p>
        <p className="text-body font-semibold">
          ~{valueFormatter
            ? valueFormatter(point.forecast_expected)
            : formatSeriesValue(Math.round(point.forecast_expected), seriesLabel)}
        </p>
        {point.forecast_band && (
          <p className="text-body-sm text-fg-tertiary">
            Likely {formatValueRange(point.forecast_band, valueFormatter)}
          </p>
        )}
      </div>
    )
  }

  // A padding bucket out at the window's edge (MON-22): no value, not zero.
  if (point.count == null && point.expected_count == null) {
    return (
      <div className="rounded-card border bg-popover text-popover-foreground px-3 py-2 shadow-md">
        <p className="text-body-sm text-fg-tertiary">{heading}</p>
        <p className="text-body-sm text-fg-tertiary">No data for this bucket</p>
      </div>
    )
  }

  const expectedCount = point.expected_count
  const count = point.count ?? 0
  // A flagged value that rounds onto its own band edge ("37, normal 27–37")
  // read as inside the band; one more decimal separates them (MO-38).
  const extraDigit = !valueFormatter && point.band != null
    && point.band.some(edge => Math.round(edge) === Math.round(count) && edge !== count)
  const formatPlain = (value: number) =>
    extraDigit
      ? formatNumber(Math.round(value * 10) / 10)
      : formatNumber(Math.round(value))
  const formatSecondary = (value: number) => (valueFormatter ? valueFormatter(value) : formatPlain(value))
  const partialNote = partialBucketNote(point, granularity)

  // Three lines at most (MO-38): the bucket, the value, what was expected with
  // its normal range, and for a flagged bucket which way and how far.
  return (
    <div className="rounded-card border bg-popover text-popover-foreground px-3 py-2 shadow-md">
      <p className="text-body-sm text-fg-tertiary">{heading}</p>
      <p className="text-body font-semibold">
        {valueFormatter ? valueFormatter(count) : formatSeriesValue(count, seriesLabel)}
      </p>
      {expectedCount !== null && (
        <p className="text-body-sm text-fg-tertiary">
          Expected {formatSecondary(expectedCount)}
          {point.band && ` (normal ${formatValueRange(point.band, valueFormatter ?? formatPlain)})`}
        </p>
      )}
      {point.is_anomaly && (
        <AnomalyEffectLine
          direction={point.anomaly_direction}
          actual={count}
          expected={expectedCount}
          zScore={point.z_score}
        />
      )}
      {partialNote && <p className="text-body-sm text-fg-tertiary">{partialNote}</p>}
    </div>
  )
}

/**
 * The tooltip's bucket label. A sub-day bucket is an instant in the viewer's
 * zone, so it names the zone ("Sep 22, 06:00 PM GMT+3", MO-38); calendar
 * buckets are UTC days and keep the plain label.
 */
function formatTooltipHeading(bucket: string, granularity: MetricsGranularity): string {
  const text = formatTooltipLabel(bucket, granularity)
  if (!isSubDayGranularity(granularity)) return text
  const date = new Date(bucket)
  if (Number.isNaN(date.getTime())) return text
  const zone = new Intl.DateTimeFormat(APP_LOCALE, { timeZoneName: 'short' })
    .formatToParts(date)
    .find(part => part.type === 'timeZoneName')?.value
  return zone ? `${text} ${zone}` : text
}

/** "27–37" with the unit once, at the end: "3–7%", not "3%–7%" (MO-38). */
function formatValueRange(
  [low, high]: [number, number],
  format: ((value: number) => string) | undefined,
): string {
  const fmt = format ?? ((value: number) => formatNumber(Math.round(value)))
  const lowText = fmt(low)
  const highText = fmt(high)
  const suffix = /[^\d]*$/.exec(highText)?.[0] ?? ''
  const trimmedLow = suffix && lowText.endsWith(suffix) ? lowText.slice(0, -suffix.length) : lowText
  return `${trimmedLow}–${highText}`
}

const PARTIAL_BUCKET_NOUN: Record<MetricsGranularity, string> = {
  '15min': '15 minutes',
  hour: 'hour',
  '6h': '6 hours',
  day: 'day',
  week: 'week',
  month: 'month',
}

function partialBucketNote(point: ChartDataPoint, granularity: MetricsGranularity): string | null {
  const noun = PARTIAL_BUCKET_NOUN[granularity]
  if (point.partial_through) return `Partial ${noun}: data through ${formatDateTime(point.partial_through)}`
  if (point.partial_from) return `Partial ${noun}: data from ${formatDateTime(point.partial_from)}`
  return null
}

/**
 * A flagged bucket in words: which way it moved and by how much against the
 * expectation ("▲ Spike, +16% above expected"), not a z-score (MO-38 / MO-2).
 * The z-score stays only when there is no expectation to compare against.
 */
function AnomalyEffectLine({
  direction,
  actual,
  expected,
  zScore,
}: {
  direction?: 'spike' | 'drop' | null
  actual: number
  expected: number | null
  zScore?: number | null
}) {
  const glyph = direction === 'drop' ? '▼' : direction === 'spike' ? '▲' : '●'
  const word = direction === 'drop' ? 'Drop' : direction === 'spike' ? 'Spike' : 'Anomaly'
  const delta = expected === null ? null : ratioDelta(actual, expected)
  let detail = ''
  if (direction === 'drop' && actual === 0) {
    detail = ' to zero'
  } else if (delta !== null) {
    detail = delta >= 0
      ? `, +${Math.round(delta)}% above expected`
      : `, ${Math.abs(Math.round(delta))}% below expected`
  } else if (zScore != null && Number.isFinite(zScore)) {
    detail = ` (z=${zScore.toFixed(1)})`
  }
  return (
    <p
      className="text-body-sm font-medium"
      style={{ color: direction ? signalDirectionColor(direction) : 'var(--danger)' }}
    >
      <span aria-hidden="true">{glyph} </span>
      {word}
      {detail}
    </p>
  )
}

// Exported for unit tests only — recharts never paints its tooltip in jsdom
// (mirrors CustomTooltip above).
export function MultiSeriesTooltip({
  active,
  payload,
  label,
  granularity,
  seriesLabel,
  valueFormatter: axisFormatter,
  tooltipFormatter,
}: {
  active?: boolean
  payload?: Array<{
    value: number
    dataKey?: string
    color?: string
    name?: string
    payload?: Record<string, unknown>
  }>
  label?: string | number
  granularity: MetricsGranularity
  seriesLabel: SeriesNoun
  valueFormatter?: (value: number) => string
  tooltipFormatter?: (value: number) => string
}) {
  const valueFormatter = tooltipFormatter ?? axisFormatter
  if (!active || !payload?.length) return null
  const visiblePayload = payload.filter(item => typeof item.value === 'number')
  if (!visiblePayload.length) return null

  return (
    <div className="max-w-xs rounded-card border bg-popover text-popover-foreground px-3 py-2 shadow-md">
      <p className="text-body-sm text-fg-tertiary">{formatTooltipLabel(String(label ?? ''), granularity)}</p>
      <div className="mt-1 space-y-1">
        {visiblePayload.map(item => (
          <div key={item.dataKey} className="flex items-center justify-between gap-4 text-body-sm">
            <span className="flex min-w-0 items-center gap-1">
              <span className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: item.color }} />
              <span className="truncate">{item.name}</span>
            </span>
            <span className="font-medium">
              {valueFormatter
                ? valueFormatter(Number(item.value))
                : formatSeriesValue(Number(item.value), seriesLabel)}
            </span>
          </div>
        ))}
      </div>
      {visiblePayload.map(item => {
        const row = item.payload
        if (!item.dataKey || !row?.[`${item.dataKey}__anomaly`]) return null
        const direction = row[`${item.dataKey}__direction`]
        const zScore = row[`${item.dataKey}__z`]
        return (
          <AnomalyTooltipLine
            key={`${item.dataKey}-anomaly`}
            prefix={`${item.name ?? 'Series'} anomaly`}
            direction={direction === 'spike' || direction === 'drop' ? direction : null}
            zScore={typeof zScore === 'number' ? zScore : null}
          />
        )
      })}
    </div>
  )
}

interface SnappedAnnotation {
  id: string
  bucket: string
  label: string
  color: string
  source: ChartAnnotationSource
  description: string | null
  url: string | null
}

function snapAnnotationsToBuckets(
  annotations: ChartAnnotation[] | undefined,
  data: ChartDataPoint[],
  windowEnd?: string,
): SnappedAnnotation[] {
  if (!annotations?.length || !data.length) return []
  // Categorical x-axis only renders ReferenceLine for x values that exist
  // on rendered points, so snap each annotation to the bucket that CONTAINS
  // it: the latest bucket whose start is <= the annotation. Nearest-start
  // snapping put an 18:00 signal on the next day at day granularity (F25).
  // Annotations before the window drop. One past the newest point but still
  // inside the requested window (`windowEnd`, i.e. "now" for live ranges)
  // lands on the newest bucket: the form defaults to "now", and with
  // collection lag that instant is hours past the last bucket, so "mark the
  // deploy I just did" silently drew nothing (MO-8).
  const buckets = data.map(point => ({
    bucket: point.bucket,
    time: new Date(point.bucket).getTime(),
  }))
  const first = buckets[0]
  const last = buckets[buckets.length - 1]
  if (!first || !last) return []
  const previous = buckets[buckets.length - 2]
  const lastSpan = previous ? Math.max(0, last.time - previous.time) : 0
  const windowEndTime = windowEnd ? new Date(windowEnd).getTime() : Number.NaN
  const upperBound = Number.isNaN(windowEndTime)
    ? last.time + lastSpan
    : Math.max(last.time + lastSpan, windowEndTime)
  return annotations
    .map(annotation => {
      const annotationTime = new Date(annotation.bucket).getTime()
      if (Number.isNaN(annotationTime)) return null
      if (annotationTime < first.time || annotationTime > upperBound) {
        return null
      }
      let containing = first
      for (const candidate of buckets) {
        if (candidate.time <= annotationTime && candidate.time >= containing.time) {
          containing = candidate
        }
      }
      return {
        id: annotation.id,
        bucket: containing.bucket,
        label: annotation.label,
        color: annotationMarkerColor(annotation),
        // Rows cached before the column existed carry no source: manual.
        source: annotation.source ?? 'manual',
        description: annotation.description,
        url: safeAnnotationUrl(annotation.url),
      }
    })
    .filter((value): value is SnappedAnnotation => value !== null)
}

/**
 * The label of a release or API marker (#256): a small source icon next to
 * the line, the muted label beside it, and a native tooltip naming the source.
 * With a URL the whole label is a link that opens in a new tab.
 */
// Exported for unit tests only.
export function AutomaticAnnotationLabel({
  x,
  y,
  anchor,
  annotation,
}: {
  x?: number
  y?: number
  /** `start` draws the label right of the line, `end` left of it. */
  anchor: 'start' | 'end'
  annotation: SnappedAnnotation
}) {
  if (typeof x !== 'number' || typeof y !== 'number') return null
  const Icon = annotation.source === 'release' ? Tag : Rocket
  const iconSize = 10
  const gap = 3
  // The icon always sits against the line; the text runs away from it.
  const iconX = anchor === 'start' ? x : x - iconSize
  const textX = anchor === 'start' ? x + iconSize + gap : x - iconSize - gap
  const title = [
    `${annotationSourceLabel(annotation.source)}: ${annotation.label}`,
    annotation.description,
    annotation.url ? 'Opens in a new tab' : null,
  ].filter(Boolean).join(' — ')
  const content = (
    <>
      <title>{title}</title>
      <Icon
        x={iconX}
        y={y}
        width={iconSize}
        height={iconSize}
        color={annotation.color}
        aria-hidden="true"
        data-testid={`annotation-icon-${annotation.source}`}
      />
      <text
        x={textX}
        y={y}
        textAnchor={anchor}
        dominantBaseline="hanging"
        fill={annotation.color}
        fontSize="var(--text-micro)"
      >
        {truncateAnnotationLabel(annotation.label)}
      </text>
    </>
  )
  if (!annotation.url) {
    return <g data-testid="automatic-annotation-label">{content}</g>
  }
  return (
    <a
      href={annotation.url}
      target="_blank"
      rel="noopener noreferrer"
      className="cursor-pointer"
      data-testid="automatic-annotation-label"
    >
      {content}
    </a>
  )
}

/**
 * Hides release and API markers on every chart at once (#256); the choice is
 * remembered. Shown only when the window has such a marker to hide.
 */
function ShowReleasesToggle({
  checked,
  onCheckedChange,
}: {
  checked: boolean
  onCheckedChange: (checked: boolean) => void
}) {
  const id = useId()
  return (
    <div
      data-testid="show-releases-toggle"
      className="mt-2 flex items-center gap-1.5 text-caption text-fg-tertiary"
    >
      <Checkbox
        id={id}
        checked={checked}
        onCheckedChange={value => onCheckedChange(value === true)}
      />
      <label htmlFor={id} className="cursor-pointer select-none">
        Show releases
      </label>
    </div>
  )
}

export function MetricsChart({
  data,
  forecast,
  annotations,
  className,
  color,
  height = 300,
  granularity = 'hour',
  seriesLabel = EVENTS_NOUN,
  valueFormatter,
  tooltipFormatter,
  sigmaThreshold = DEFAULT_SIGMA_THRESHOLD,
  nonNegative,
  from,
  to,
  partial,
  legend = false,
}: MetricsChartProps) {
  const { chartStyle } = useTheme()
  const chartColor = color || SINGLE_SERIES_COLOR
  const gradientId = useId().replace(/:/g, '')
  const descId = useId()
  const clampAtZero = nonNegative ?? valueFormatter === undefined
  const chartData = useMemo(
    () =>
      padChartData(
        buildChartData(data, forecast, sigmaThreshold, clampAtZero, partial),
        { from, to: forecast?.length ? undefined : to },
        granularity,
      ),
    [data, forecast, sigmaThreshold, clampAtZero, partial, from, to, granularity],
  )
  const [showReleases, setShowReleases] = useShowReleases()
  const allSnappedAnnotations = useMemo(
    () => snapAnnotationsToBuckets(annotations, chartData, to),
    [annotations, chartData, to],
  )
  // The toggle hides release and API markers only; manual ones always draw.
  const hasAutomaticAnnotations = allSnappedAnnotations.some(isAutomaticAnnotation)
  const snappedAnnotations = useMemo(
    () => showReleases
      ? allSnappedAnnotations
      : allSnappedAnnotations.filter(annotation => !isAutomaticAnnotation(annotation)),
    [allSnappedAnnotations, showReleases],
  )
  const { ref: containerRef, ready: containerReady } = useChartContainerReady()
  const yAxisWidth = useMemo(
    () => axisWidthForValues(collectChartYValues(chartData), valueFormatter ?? formatCount),
    [chartData, valueFormatter],
  )
  const xAxisTicks = useTimeAxisTicks(chartData, granularity)
  const bucketIndex = useMemo(
    () => new Map(chartData.map((row, index) => [row.bucket, index])),
    [chartData],
  )

  if (!data.length) {
    return (
      <div className={cn('flex items-center justify-center text-fg-tertiary text-body', className)} style={{ height }}>
        No metrics data available
      </div>
    )
  }

  const anomalyBuckets = data.filter(point => point.is_anomaly).map(point => point.bucket)
  const anomalyCount = anomalyBuckets.length
  const forecastBuckets = (forecast ?? []).map(point => point.bucket)
  const hasPartial = Boolean(partial?.first || partial?.last)
  const curve = curveFor(data.length)
  const hasExpected = chartData.some(point => point.expected_count != null)
  const hasBand = chartData.some(point => point.band)
  const sigmaLabel = Number.isFinite(sigmaThreshold) && sigmaThreshold > 0
    ? sigmaThreshold
    : DEFAULT_SIGMA_THRESHOLD
  const hasFooter = legend || hasAutomaticAnnotations

  const chart = (
    <div
      ref={containerRef}
      role="img"
      aria-label={`${seriesNounPlural(seriesLabel)} over time`}
      aria-describedby={descId}
      className={cn('w-full', !hasFooter && className)}
      style={{ height }}
    >
      <div id={descId} className="sr-only">
        {data.length} data points.
        {/* The separating spaces sit OUTSIDE the spans: an accessible name or
            description is built from each element's trimmed text, so a space
            inside a span was dropped and the sentences ran together
            ("points.1 anomaly"). */}
        {anomalyCount > 0 && (
          <>
            {' '}
            <span data-testid="anomaly-dot">
              {formatAnomalyCount(anomalyCount)}: {summarizeBuckets(anomalyBuckets, granularity)}.
            </span>
          </>
        )}
        {forecastBuckets.length > 0 && (
          <>
            {' '}
            <span data-testid="forecast-point">
              {summarizeForecastRange(forecastBuckets, granularity)}.
            </span>
          </>
        )}
        {hasPartial && (
          <>
            {' '}
            <span data-testid="partial-buckets">
              {partial?.first && partial?.last
                ? 'The first and last buckets are partial.'
                : partial?.first
                  ? 'The first bucket is partial.'
                  : 'The last bucket is partial.'}
            </span>
          </>
        )}
        {/* Humanized like every other bucket in this summary, and separated:
            the raw ISO instants used to run together
            ("2026-09-24T10:00:00Z: Deploy2026-…", DS-25). */}
        {snappedAnnotations.length > 0 && (
          <>
            {' '}
            {snappedAnnotations.map((annotation, index) => (
              <Fragment key={annotation.id}>
                {index > 0 && '; '}
                <span data-testid="chart-annotation">
                  {formatTooltipLabel(annotation.bucket, granularity)}: {annotation.label}
                </span>
              </Fragment>
            ))}
            .
          </>
        )}
      </div>
      {containerReady ? (
        <ResponsiveContainer width="100%" height="100%">
        <ComposedChart
          data={chartData}
          margin={{ top: 8, right: 8, bottom: 0, left: 0 }}
          tabIndex={CHART_SURFACE_TAB_INDEX}
        >
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor={chartColor} stopOpacity={0.3} />
              <stop offset="95%" stopColor={chartColor} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" className="stroke-border" vertical={false} />
          <XAxis
            dataKey="bucket"
            {...xAxisTicks}
            className="text-body-sm fill-fg-tertiary"
            tickLine={false}
            axisLine={false}
            tickMargin={8}
          />
          <YAxis
            tickFormatter={valueFormatter ?? formatCount}
            className="text-body-sm fill-fg-tertiary"
            tickLine={false}
            axisLine={false}
            tickMargin={8}
            width={yAxisWidth}
          />
          <Tooltip
            content={
              <CustomTooltip
                granularity={granularity}
                seriesLabel={seriesLabel}
                valueFormatter={valueFormatter}
                tooltipFormatter={tooltipFormatter}
              />
            }
          />
          {/* Normal range — recharts renders a 2-tuple dataKey as a vertical
              range area, a soft fill where consecutive buckets carry one. */}
          <Area
            type={curve}
            dataKey="band"
            stroke="none"
            fill="var(--fg-faint)"
            fillOpacity={0.1}
            isAnimationActive={false}
            connectNulls={false}
            activeDot={false}
            legendType="none"
          />
          {/* Expected value: dashed where buckets run together (every bucket
              the detector scored stores its baseline, tripl-i9mt.25), and a
              hollow point with its normal-range whisker where a bucket carries
              one alone — a flagged bucket, or history scored before baselines
              were stored (MO-1). */}
          <Line
            type={curve}
            dataKey="expected_count"
            stroke="var(--fg-subtle)"
            strokeDasharray="4 4"
            strokeWidth={1.5}
            dot={(props: { cx?: number; cy?: number; payload?: ChartDataPoint }) => {
              // Per point, not per chart: a flagged or isolated expected value
              // keeps its hollow point however long the chart is, and a run of
              // scored buckets stays a plain dashed line (tripl-i9mt.25).
              if (!hasExpectedDot(props.payload) || props.cx === undefined || props.cy === undefined) {
                return <></>
              }
              return (
                <circle
                  cx={props.cx}
                  cy={props.cy}
                  r={3}
                  fill="var(--background)"
                  stroke="var(--fg-subtle)"
                  strokeWidth={1.5}
                  data-testid="expected-point"
                />
              )
            }}
            activeDot={false}
            isAnimationActive={false}
            connectNulls={false}
          >
            <ErrorBar dataKey="expected_error" width={6} stroke="var(--fg-subtle)" strokeWidth={1.5} direction="y" />
          </Line>
          {renderCountSeries({
            chartStyle,
            chartColor,
            gradientId,
            mini: false,
            curve,
            dataKey: hasPartial ? 'solid_count' : 'count',
            pointDots: data.length <= POINT_DOTS_MAX_POINTS,
          })}
          {/* A partial first/last bucket: dashed, with a hollow point, so a
              day that is only half over does not read as a drop (MO-5). */}
          {hasPartial && chartStyle !== 'bar' && (
            <Line
              type="linear"
              dataKey="partial_count"
              stroke={chartColor}
              strokeDasharray="4 3"
              strokeWidth={2}
              isAnimationActive={false}
              connectNulls={false}
              dot={(props: { cx?: number; cy?: number; payload?: ChartDataPoint }) => {
                const point = props.payload
                if (!point || (!point.partial_from && !point.partial_through)) return <></>
                if (point.is_anomaly) {
                  return (
                    <AnomalyMark cx={props.cx} cy={props.cy} direction={point.anomaly_direction} mini={false} />
                  )
                }
                if (props.cx === undefined || props.cy === undefined) return <></>
                return (
                  <circle
                    cx={props.cx}
                    cy={props.cy}
                    r={3.5}
                    fill="var(--background)"
                    stroke={chartColor}
                    strokeWidth={2}
                    data-testid="partial-point"
                  />
                )
              }}
              activeDot={{ r: 4, strokeWidth: 0 }}
              legendType="none"
            />
          )}
          {/* Forecast: a hollow point with a whisker for its likely range, in
              a neutral ink, not a line from the last actual (MO-7). */}
          <Line
            type="linear"
            dataKey="forecast_expected"
            stroke="var(--fg-subtle)"
            strokeDasharray="2 3"
            strokeWidth={1.25}
            dot={{ r: 3.5, fill: 'var(--background)', stroke: 'var(--fg-subtle)', strokeWidth: 1.5 }}
            activeDot={{ r: 4, fill: 'var(--fg-subtle)', strokeWidth: 0 }}
            isAnimationActive={false}
            connectNulls={false}
          >
            <ErrorBar dataKey="forecast_error" width={6} stroke="var(--fg-subtle)" strokeWidth={1.25} direction="y" />
          </Line>
          {snappedAnnotations.map(annotation => {
            // Inside the plot, on the side with room: `top` drew the label
            // above the plot area, where the right edge clipped a label on
            // the newest bucket ("injected dem…", LIVE-22). A line in the
            // right half puts its label to its left, and vice versa.
            const rightHalf = (bucketIndex.get(annotation.bucket) ?? 0) > (chartData.length - 1) / 2
            const position = rightHalf ? 'insideTopRight' : 'insideTopLeft'
            const automatic = isAutomaticAnnotation(annotation)
            return (
              <ReferenceLine
                key={annotation.id}
                x={annotation.bucket}
                stroke={annotation.color}
                strokeDasharray="2 3"
                // Release and API markers recede behind the ones people placed (#256).
                strokeWidth={automatic ? 1 : 1.5}
                strokeOpacity={automatic ? 0.7 : 1}
                label={
                  automatic
                    ? {
                        position,
                        content: (props: { x?: number | string; y?: number | string }) => (
                          <AutomaticAnnotationLabel
                            x={typeof props.x === 'number' ? props.x : undefined}
                            y={typeof props.y === 'number' ? props.y : undefined}
                            anchor={rightHalf ? 'end' : 'start'}
                            annotation={annotation}
                          />
                        ),
                      }
                    : {
                        value: truncateAnnotationLabel(annotation.label),
                        position,
                        fill: annotation.color,
                        fontSize: 'var(--text-micro)',
                      }
                }
                ifOverflow="extendDomain"
              />
            )
          })}
        </ComposedChart>
        </ResponsiveContainer>
      ) : null}
    </div>
  )

  if (!hasFooter) return chart
  return (
    <div className={cn('w-full', className)}>
      {chart}
      {legend && (
        <ChartLegend
          color={chartColor}
          expected={hasExpected}
          band={hasBand ? sigmaLabel : null}
          anomaly={anomalyCount > 0}
          partial={hasPartial}
          forecast={forecastBuckets.length > 0}
        />
      )}
      {hasAutomaticAnnotations && (
        <ShowReleasesToggle checked={showReleases} onCheckedChange={setShowReleases} />
      )}
    </div>
  )
}

/**
 * What each mark on a volume chart means (MO-1), listing only the marks this
 * chart draws: "— Actual · - - Expected · ┃ Normal range (±4σ) · ▲ Anomaly".
 */
// Exported for unit tests only.
export function ChartLegend({
  color,
  expected,
  band,
  anomaly,
  partial,
  forecast,
}: {
  color: string
  expected: boolean
  /** The sigma multiplier of the normal range, or null when none is drawn. */
  band: number | null
  anomaly: boolean
  partial: boolean
  forecast: boolean
}) {
  return (
    <ul
      aria-label="Chart legend"
      data-testid="chart-legend"
      className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-caption text-fg-tertiary"
    >
      <li className="inline-flex items-center gap-1.5">
        <LegendSwatch stroke={color} />
        Actual
      </li>
      {expected && (
        <li className="inline-flex items-center gap-1.5">
          <LegendSwatch stroke="var(--fg-subtle)" dash="3 3" />
          Expected
        </li>
      )}
      {band !== null && (
        <li className="inline-flex items-center gap-1.5">
          <svg aria-hidden="true" width="14" height="10" className="shrink-0">
            <line x1="7" y1="1" x2="7" y2="9" stroke="var(--fg-subtle)" strokeWidth="1.5" />
            <line x1="4" y1="1" x2="10" y2="1" stroke="var(--fg-subtle)" strokeWidth="1.5" />
            <line x1="4" y1="9" x2="10" y2="9" stroke="var(--fg-subtle)" strokeWidth="1.5" />
          </svg>
          Normal range (±{band}σ)
        </li>
      )}
      {anomaly && (
        <li className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" className="text-danger">▲</span>
          Anomaly
        </li>
      )}
      {partial && (
        <li className="inline-flex items-center gap-1.5">
          <LegendSwatch stroke={color} dash="4 3" />
          Partial bucket
        </li>
      )}
      {forecast && (
        <li className="inline-flex items-center gap-1.5">
          <svg aria-hidden="true" width="14" height="10" className="shrink-0">
            <circle cx="7" cy="5" r="3" fill="var(--background)" stroke="var(--fg-subtle)" strokeWidth="1.5" />
          </svg>
          Forecast (next bucket)
        </li>
      )}
    </ul>
  )
}

function LegendSwatch({ stroke, dash }: { stroke: string; dash?: string }) {
  return (
    <svg aria-hidden="true" width="16" height="10" className="shrink-0">
      <line x1="1" y1="5" x2="15" y2="5" stroke={stroke} strokeWidth="2" strokeDasharray={dash} />
    </svg>
  )
}

export function MetricsMultiSeriesChart({
  series,
  className,
  height = 300,
  granularity = 'hour',
  seriesLabel = EVENTS_NOUN,
  emptyLabel = 'No breakdown metrics available',
  valueFormatter,
  tooltipFormatter,
  from,
  to,
}: MetricsMultiSeriesChartProps) {
  // On a phone the finger sits on the plot and the tooltip beside it covered
  // the y-axis: pin it to the top edge instead, free to leave the plot (MO-29).
  const coarsePointer = useMediaQuery(COARSE_POINTER_QUERY)
  const chartSeries = useMemo(
    () => series
      .filter(item => item.data.length > 0)
      .map((item, index) => ({
        ...item,
        key: `series_${index}`,
        color: item.color ?? SERIES_COLORS[index % SERIES_COLORS.length],
        hasAnomaly: item.data.some(point => point.is_anomaly),
      })),
    [series],
  )
  const chartData = useMemo(() => {
    const rows = new Map<string, Record<string, string | number | boolean>>()
    for (const item of chartSeries) {
      for (const point of item.data) {
        const row = rows.get(point.bucket) ?? { bucket: point.bucket }
        row[item.key] = point.count
        row[`${item.key}__anomaly`] = point.is_anomaly
        // Read back by the tooltip's anomaly line (MON-17).
        if (point.anomaly_direction) row[`${item.key}__direction`] = point.anomaly_direction
        if (point.z_score != null) row[`${item.key}__z`] = point.z_score
        rows.set(point.bucket, row)
      }
    }
    const sorted = Array.from(rows.values()).sort((left, right) =>
      String(left.bucket).localeCompare(String(right.bucket)),
    ) as Array<Record<string, string | number | boolean> & { bucket: string }>
    return padRows(sorted, { from, to }, granularity, bucket => ({ bucket }))
  }, [chartSeries, from, to, granularity])
  const { ref: containerRef, ready: containerReady } = useChartContainerReady()
  const yAxisWidth = useMemo(
    () => axisWidthForValues(collectMultiSeriesYValues(chartData), valueFormatter ?? formatCount),
    [chartData, valueFormatter],
  )
  const xAxisTicks = useTimeAxisTicks(chartData, granularity)
  const descId = useId()

  if (!chartSeries.length || !chartData.length) {
    return (
      <div className={cn('flex items-center justify-center text-fg-tertiary text-body', className)} style={{ height }}>
        {emptyLabel}
      </div>
    )
  }

  return (
    <div
      ref={containerRef}
      role="img"
      aria-label={`${seriesNounPlural(seriesLabel)} breakdown over time`}
      // role="img" makes its children presentational, so the summary is only
      // read through this reference (DS-25).
      aria-describedby={descId}
      className={cn('w-full', className)}
      style={{ height }}
    >
      <div id={descId} className="sr-only">
        <p>Chart with {chartSeries.length} series: {chartSeries.map(s => s.label).join(', ')}.</p>
        {chartSeries.map(item => {
          const anomalies = item.data.filter(point => point.is_anomaly)
          return anomalies.length > 0 ? (
            <p key={item.key}>{item.label}: {formatAnomalyCount(anomalies.length)}.</p>
          ) : null
        })}
      </div>
      {containerReady ? (
        <ResponsiveContainer width="100%" height="100%">
        <ComposedChart
          data={chartData}
          margin={{ top: 8, right: 8, bottom: 0, left: 0 }}
          tabIndex={CHART_SURFACE_TAB_INDEX}
        >
          <CartesianGrid strokeDasharray="3 3" className="stroke-border" vertical={false} />
          <XAxis
            dataKey="bucket"
            {...xAxisTicks}
            className="text-body-sm fill-fg-tertiary"
            tickLine={false}
            axisLine={false}
            tickMargin={8}
          />
          <YAxis
            tickFormatter={valueFormatter ?? formatCount}
            className="text-body-sm fill-fg-tertiary"
            tickLine={false}
            axisLine={false}
            tickMargin={8}
            width={yAxisWidth}
          />
          <Tooltip
            position={coarsePointer ? { y: 0 } : undefined}
            allowEscapeViewBox={coarsePointer ? { y: true } : undefined}
            content={
              <MultiSeriesTooltip
                granularity={granularity}
                seriesLabel={seriesLabel}
                valueFormatter={valueFormatter}
                tooltipFormatter={tooltipFormatter}
              />
            }
          />
          {chartSeries.map(item => (
            <Line
              key={item.key}
              type={curveFor(chartData.length)}
              dataKey={item.key}
              name={item.label}
              stroke={item.color}
              strokeWidth={item.isHighlighted ? 3 : 2}
              strokeOpacity={item.isHighlighted ? 1 : 0.82}
              strokeDasharray={item.dash}
              // Static, like the main volume series: animating up to eight
              // lines of a few hundred points each janked every range change
              // (MON-23).
              isAnimationActive={false}
              // The dot renderer runs once per point, so a series with nothing
              // flagged skips it entirely instead of drawing empty fragments.
              dot={!item.hasAnomaly ? false : (props: { cx?: number; cy?: number; payload?: Record<string, unknown> }) => {
                if (!props.payload?.[`${item.key}__anomaly`]) return <></>
                const direction = props.payload[`${item.key}__direction`]
                return (
                  <AnomalyMark
                    cx={props.cx}
                    cy={props.cy}
                    direction={direction === 'spike' || direction === 'drop' ? direction : null}
                    mini={false}
                  />
                )
              }}
              activeDot={{ r: item.isHighlighted ? 5 : 4, strokeWidth: 0 }}
              connectNulls={false}
            />
          ))}
        </ComposedChart>
        </ResponsiveContainer>
      ) : null}
    </div>
  )
}

// The main volume series keeps `isAnimationActive={false}`, matching the band /
// forecast series above. MetricsChart mounts its ResponsiveContainer only once
// `useChartContainerReady` reports a real size (and TabMetricsCard nests it in a
// Collapsible), so recharts sees a late/resizing mount — exactly the case where
// an *animated* Area/Line can settle into its empty enter-frame and never paint.
// When the series is count-only (the events-metrics `events_total` response has
// no expected/band series to fall back on) that left the whole plot blank while
// the axes still rendered from the count domain (tripl-yfsj.2).
// Exported for unit tests only — recharts never paints in jsdom, so the
// animation flag is asserted on the returned element rather than on pixels.
// eslint-disable-next-line react-refresh/only-export-components
export function renderCountSeries({
  chartStyle,
  chartColor,
  gradientId,
  mini,
  curve = 'monotone',
  dataKey = 'count',
  pointDots = false,
}: {
  chartStyle: ChartStyle
  chartColor: string
  gradientId: string
  mini: boolean
  /** `linear` for a coarse or sparse series (MO-6). */
  curve?: CurveType
  /** `solid_count` when a partial bucket is split off onto its own dashed line (MO-5). */
  dataKey?: 'count' | 'solid_count'
  /** A small dot on every measured point of a sparse series (MO-6). */
  pointDots?: boolean
}) {
  const anomalyDot = (props: { cx?: number | null; cy?: number | null; payload?: ChartDataPoint }) => {
    if (props.cx == null || props.cy == null) return <></>
    if (!props.payload?.is_anomaly) {
      if (!pointDots || props.payload?.count == null) return <></>
      return <circle cx={props.cx} cy={props.cy} r={2} fill={chartColor} data-testid="point-dot" />
    }
    return (
      <AnomalyMark
        cx={props.cx}
        cy={props.cy}
        direction={props.payload.anomaly_direction}
        mini={mini}
      />
    )
  }

  if (chartStyle === 'bar') {
    // Bars keep every bucket: a partial one is drawn faded instead (MO-5).
    return (
      <Bar
        dataKey="count"
        fill={chartColor}
        radius={[2, 2, 0, 0]}
        isAnimationActive={false}
        shape={(props: AnomalyBarProps) => <AnomalyBar {...props} chartColor={chartColor} />}
      />
    )
  }

  if (chartStyle === 'line-only') {
    return (
      <Line
        type={curve}
        dataKey={dataKey}
        stroke={chartColor}
        strokeWidth={2}
        isAnimationActive={false}
        dot={anomalyDot}
        activeDot={mini ? false : { r: 4, strokeWidth: 0 }}
      />
    )
  }

  return (
    <Area
      type={curve}
      dataKey={dataKey}
      stroke={chartColor}
      fill={`url(#${gradientId})`}
      strokeWidth={2}
      isAnimationActive={false}
      dot={anomalyDot}
      activeDot={mini ? false : { r: 4, strokeWidth: 0 }}
    />
  )
}

/**
 * An anomaly's mark on the line: a triangle pointing the way it moved, in the
 * direction's colour — so a drop reads as a drop without colour, and a spike
 * and a drop are told apart at a glance (MON-17). A plain dot, as before, when
 * the direction is unknown.
 */
// Exported for unit tests only — recharts never paints in jsdom.
export function AnomalyMark({
  cx,
  cy,
  direction,
  mini,
}: {
  cx?: number
  cy?: number
  direction?: 'spike' | 'drop' | null
  mini: boolean
}) {
  if (cx === undefined || cy === undefined) return <></>
  const r = mini ? 3.5 : 5
  const strokeWidth = mini ? 1.5 : 2
  if (!direction) {
    return (
      <circle
        cx={cx}
        cy={cy}
        r={mini ? 3 : 4}
        fill="var(--destructive)"
        stroke="var(--background)"
        strokeWidth={strokeWidth}
        data-testid="anomaly-dot"
      />
    )
  }
  const tip = direction === 'spike' ? cy - r : cy + r
  const base = direction === 'spike' ? cy + r * 0.7 : cy - r * 0.7
  return (
    <polygon
      points={`${cx},${tip} ${cx - r},${base} ${cx + r},${base}`}
      fill={signalDirectionColor(direction)}
      stroke="var(--background)"
      strokeWidth={strokeWidth}
      strokeLinejoin="round"
      data-testid="anomaly-dot"
      data-direction={direction}
    />
  )
}

type AnomalyBarProps = {
  x?: number
  y?: number
  width?: number
  height?: number
  payload?: ChartDataPoint
}

function AnomalyBar({
  x,
  y,
  width,
  height,
  payload,
  chartColor,
}: AnomalyBarProps & { chartColor: string }) {
  if (x === undefined || y === undefined || width === undefined || height === undefined) {
    return <g />
  }
  if (!payload?.is_anomaly) {
    const partial = Boolean(payload?.partial_from || payload?.partial_through)
    return (
      <rect
        x={x}
        y={y}
        width={width}
        height={height}
        fill={chartColor}
        fillOpacity={partial ? 0.35 : undefined}
        stroke={partial ? chartColor : undefined}
        strokeDasharray={partial ? '3 2' : undefined}
        rx={2}
        ry={2}
      />
    )
  }
  // Fill AND an outline in the direction's colour: a changed fill alone was the
  // only cue, and at a bar's width a red and an amber fill are hard to tell
  // apart from the series colour (MON-17).
  const tone = payload.anomaly_direction
    ? signalDirectionColor(payload.anomaly_direction)
    : 'var(--destructive)'
  return (
    <rect
      x={x}
      y={y}
      width={width}
      height={height}
      fill={tone}
      fillOpacity={0.55}
      stroke={tone}
      strokeWidth={2}
      rx={2}
      ry={2}
      data-testid="anomaly-bar"
      data-direction={payload.anomaly_direction ?? undefined}
    />
  )
}

export function MiniMetricsChart({
  data,
  className,
  color,
  height = 72,
  label,
}: MiniMetricsChartProps) {
  const { chartStyle } = useTheme()
  const chartColor = color || SINGLE_SERIES_COLOR
  const gradientId = useId().replace(/:/g, '')
  const descId = useId()
  // Same gate as the full-size charts: a mini chart inside a collapsed card
  // mounted recharts at -1×-1 and logged a warning on every render (DS-27).
  const { ref: containerRef, ready: containerReady } = useChartContainerReady()

  if (!data.length) {
    return (
      <div
        className={cn('flex items-center justify-center text-caption text-fg-tertiary', className)}
        style={{ height }}
      >
        No recent events
      </div>
    )
  }

  const anomalyCount = data.filter(point => point.is_anomaly).length
  const chartLabel = label ?? 'Metric trend chart'

  return (
    <div
      ref={containerRef}
      role="img"
      aria-label={chartLabel}
      aria-describedby={descId}
      className={cn('w-full', className)}
      style={{ height }}
    >
      <span id={descId} className="sr-only">
        {data.length} data points{anomalyCount > 0 ? `, ${formatAnomalyCount(anomalyCount)}` : ''}.
      </span>
      {containerReady ? (
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart
          data={data}
          margin={{ top: 4, right: 2, bottom: 2, left: 2 }}
          tabIndex={CHART_SURFACE_TAB_INDEX}
        >
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor={chartColor} stopOpacity={0.25} />
              <stop offset="95%" stopColor={chartColor} stopOpacity={0} />
            </linearGradient>
          </defs>
          {renderCountSeries({
            chartStyle,
            chartColor,
            gradientId,
            mini: true,
          })}
        </ComposedChart>
      </ResponsiveContainer>
      ) : null}
    </div>
  )
}
