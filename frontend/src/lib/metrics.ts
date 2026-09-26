import type { EventMetricPoint } from '@/types'

/**
 * Chart granularities. `15min` and `6h` exist so that every *collection* interval
 * the backend supports (`15m`, `1h`, `6h`, `1d`, `1w` — see
 * backend/src/tripl/core/intervals.py) has a chart granularity that matches it.
 * Without them a 15m or 6h metric charted under an "Hours" label, naming the axis
 * after a bucket width the data does not have (tripl-64n8.15).
 */
export type MetricsGranularity = '15min' | 'hour' | '6h' | 'day' | 'week' | 'month'

export const RANGE_OPTIONS = [
  { label: '7d', days: 7 },
  { label: '30d', days: 30 },
  { label: '90d', days: 90 },
] as const

export const GRANULARITY_OPTIONS: { value: MetricsGranularity; label: string }[] = [
  { value: '15min', label: '15 min' },
  { value: 'hour', label: 'Hours' },
  { value: '6h', label: '6 hours' },
  { value: 'day', label: 'Days' },
  { value: 'week', label: 'Weeks' },
  { value: 'month', label: 'Months' },
]

/**
 * Default chart granularity for a selected day-range, sized so the series stays
 * readable instead of collapsing into an unreadable comb. Hourly buckets over a
 * month are ~720 points; following the range keeps the point count in the tens.
 *
 * Used as the *default* only — a manual granularity pick overrides it and stays
 * sticky across range changes. A series collected coarser than this default
 * (a daily metric at 7d) keeps its collection interval instead: the monitoring
 * page charts the coarser of the two (MON-43).
 */
export function defaultGranularityForRange(rangeDays: number): MetricsGranularity {
  if (rangeDays <= 7) return 'hour'
  if (rangeDays <= 30) return 'day'
  return 'week'
}

/** Coarsest-last order of every granularity, for "at least this coarse" rules. */
export const GRANULARITY_ORDER: readonly MetricsGranularity[] = [
  '15min', 'hour', '6h', 'day', 'week', 'month',
]

/** Nominal width of one bucket (a month counts as 30 days). */
const GRANULARITY_SPAN_MS: Record<MetricsGranularity, number> = {
  '15min': 15 * 60 * 1000,
  hour: 60 * 60 * 1000,
  '6h': 6 * 60 * 60 * 1000,
  day: 24 * 60 * 60 * 1000,
  week: 7 * 24 * 60 * 60 * 1000,
  month: 30 * 24 * 60 * 60 * 1000,
}

/**
 * The most buckets one series may draw. A 15 min pick over 90 days is 8,640
 * SVG points per series, times up to eight series, and the page janks on every
 * range change and hover (MON-23). 500 keeps each preset's finest readable
 * option: hours for 7d, 6 hours for 30d and 90d. A series' own collection
 * granularity is exempt (see {@link granularityFitsRange}).
 */
export const MAX_POINTS_PER_SERIES = 500

/** The coarser of two granularities. */
export function coarserGranularity(
  left: MetricsGranularity,
  right: MetricsGranularity,
): MetricsGranularity {
  return GRANULARITY_ORDER.indexOf(left) >= GRANULARITY_ORDER.indexOf(right) ? left : right
}

/**
 * Whether `granularity` may be picked over `rangeDays`: it stays within
 * {@link MAX_POINTS_PER_SERIES}, or it is the series' `native` collection
 * granularity. The native one is always allowed — the smallest preset is 7d, so
 * the cap alone ruled out a 15 min metric's own buckets everywhere, and with
 * them the anomaly band a roll-up drops and the forecast that only renders at
 * the native granularity.
 */
export function granularityFitsRange(
  granularity: MetricsGranularity,
  rangeDays: number,
  native: MetricsGranularity | null = null,
): boolean {
  if (granularity === native) return true
  return (rangeDays * GRANULARITY_SPAN_MS.day) / GRANULARITY_SPAN_MS[granularity]
    <= MAX_POINTS_PER_SERIES
}

/**
 * `granularity`, bumped up to the finest one at least as coarse that
 * {@link granularityFitsRange} allows. A manual pick stays sticky across range
 * changes, so without this a "15 min" chosen at 7d followed the reader to 90d.
 * The series' `native` granularity counts as fitting, so it is never bumped and
 * a finer pick stops at it rather than jumping past it.
 */
export function clampGranularityToRange(
  granularity: MetricsGranularity,
  rangeDays: number,
  native: MetricsGranularity | null = null,
): MetricsGranularity {
  return GRANULARITY_ORDER
    .slice(GRANULARITY_ORDER.indexOf(granularity))
    .find(option => granularityFitsRange(option, rangeDays, native)) ?? 'month'
}

const MINUTE_MS = 60 * 1000
const QUARTER_HOUR_MS = 15 * MINUTE_MS
const HOUR_MS = 60 * MINUTE_MS
const SIX_HOUR_MS = 6 * HOUR_MS
const DAY_MS = 24 * HOUR_MS
const WEEK_MS = 7 * DAY_MS

/**
 * Anchor for sub-week buckets: the Unix epoch, 1970-01-01T00:00:00Z. Mirrors
 * `EPOCH` in backend/src/tripl/core/bucketing.py — hour and day divide a UTC day
 * evenly, so an epoch anchor lands every boundary on a natural clock boundary.
 */
const EPOCH_MS = 0

/**
 * Anchor for week buckets: 1970-01-05T00:00:00Z, the first Monday at or after
 * the epoch. Mirrors `WEEK_ORIGIN` in backend/src/tripl/core/bucketing.py.
 *
 * Weeks start on MONDAY. Binning weeks straight off the epoch — which is what
 * this module used to do — starts them on a THURSDAY, because 1970-01-01 was a
 * Thursday. The warehouse adapters all say "Monday" explicitly (`toMonday` /
 * `TIMESTAMP_TRUNC(..., WEEK(MONDAY))` / `date_bin` off this origin), so the
 * epoch grid put every chart week three days ahead of the server-computed
 * bucket it was supposed to line up with (tripl-64n8.2).
 */
const WEEK_ORIGIN_MS = Date.UTC(1970, 0, 5)

/**
 * Floor `ms` onto a fixed-width grid of `sizeMs` cells measured from `originMs`.
 * `Math.floor` rounds toward -Infinity, so timestamps before the origin floor
 * backwards rather than toward it.
 */
function floorToGrid(ms: number, sizeMs: number, originMs: number): number {
  return originMs + Math.floor((ms - originMs) / sizeMs) * sizeMs
}

/**
 * Floor an ISO timestamp to the start of its bucket, as an ISO UTC string.
 *
 * This is the frontend half of the bucket contract in
 * backend/src/tripl/core/bucketing.py, and must agree with `floor_to_bucket`
 * for the same instant. Everything is UTC — deliberately no local-time date
 * math anywhere below, or a user in UTC+14 would floor a timestamp into a
 * different bucket than the one the server computed and stored.
 */
export function getBucketStart(dateStr: string, granularity: MetricsGranularity): string {
  const ms = new Date(dateStr).getTime()

  switch (granularity) {
    // 15 min, 1h and 6h all divide a UTC day evenly, so an epoch anchor puts every
    // boundary on a natural clock boundary — and lands on the same grid the backend
    // bins to (sub-week buckets are epoch-anchored there too).
    case '15min':
      return new Date(floorToGrid(ms, QUARTER_HOUR_MS, EPOCH_MS)).toISOString()
    case 'hour':
      return new Date(floorToGrid(ms, HOUR_MS, EPOCH_MS)).toISOString()
    case '6h':
      return new Date(floorToGrid(ms, SIX_HOUR_MS, EPOCH_MS)).toISOString()
    case 'day':
      return new Date(floorToGrid(ms, DAY_MS, EPOCH_MS)).toISOString()
    case 'week':
      return new Date(floorToGrid(ms, WEEK_MS, WEEK_ORIGIN_MS)).toISOString()
    case 'month': {
      // Months are calendar units, not a fixed-width grid, so they can't be
      // binned off an origin. Still UTC-only. (The backend has no month
      // interval; this granularity is a chart-side rollup.)
      const date = new Date(ms)
      return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1)).toISOString()
    }
  }
}

/**
 * Re-flag an aggregated bucket only when the *rolled-up* count is itself
 * significant against the rolled-up baseline. A fixed rollup gate, deliberately
 * NOT the detector's sigma_threshold: that one defaults to 4.0 and is a
 * per-project setting (see website/docs/use/anomaly-detection.md), and this code
 * has no project context to read it from. 3 is the floor at which a single
 * anomalous hour can no longer redden a day/week bucket that is otherwise
 * unremarkable — often the lowest point of the week (tripl-dmch.10).
 */
const AGGREGATE_ANOMALY_Z_THRESHOLD = 3

/**
 * How a coarser display bucket combines the source buckets inside it.
 *
 * `sum` is right for anything additive — event counts, a fact `count` or `sum`.
 * Everything else (ratios, averages, percentages, min/max, distinct counts) is
 * NOT additive: summing 24 hourly values of an 8 % conversion rate plots 192 %
 * per day (MON-2 / MET-12). Those roll up by `mean`, which keeps the value on
 * the scale it was collected on.
 */
export type MetricRollupMode = 'sum' | 'mean'

/**
 * Roll `points` up onto `granularity` buckets.
 *
 * In `mean` mode a bucket that merged several source points carries no
 * expected/stddev and is not re-tested: the detector's baseline describes one
 * native bucket, and there is no honest way to average a band and a sigma
 * into one for the merged value. A single-point bucket passes through as-is.
 */
export function aggregateMetricPoints(
  points: EventMetricPoint[],
  granularity: MetricsGranularity,
  mode: MetricRollupMode = 'sum',
): EventMetricPoint[] {
  const grouped = new Map<string, EventMetricPoint[]>()

  for (const point of points) {
    const bucket = getBucketStart(point.bucket, granularity)
    const existing = grouped.get(bucket) ?? []
    existing.push(point)
    grouped.set(bucket, existing)
  }

  return Array.from(grouped.entries())
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([bucket, bucketPoints]) => {
      const strongestAnomaly = bucketPoints
        .filter(point => point.is_anomaly)
        .sort((left, right) => Math.abs(right.z_score ?? 0) - Math.abs(left.z_score ?? 0))[0]

      const total = bucketPoints.reduce((sum, point) => sum + point.count, 0)

      if (mode === 'mean') {
        const [only] = bucketPoints
        if (bucketPoints.length === 1 && only) return { ...only, bucket }
        return {
          bucket,
          count: total / bucketPoints.length,
          expected_count: null,
          stddev: null,
          is_anomaly: strongestAnomaly !== undefined,
          anomaly_direction: strongestAnomaly?.anomaly_direction ?? null,
          z_score: strongestAnomaly?.z_score ?? null,
          // The flagged source bucket's verdict (#254), for the marker tooltip.
          verdict: strongestAnomaly?.verdict ?? undefined,
        }
      }
      const count = total

      // Only roll up a baseline when *every* source bucket carries one.
      // Summing a partial set (e.g. only the single scored/anomalous hour)
      // against a full-count aggregate produces an expected ~1/N of the count
      // and a nonsensical tooltip, so drop expected/stddev instead of
      // reporting a corrupt one (tripl-dmch.10).
      const hasFullExpected = bucketPoints.every(point => point.expected_count !== null)
      const expectedCount = hasFullExpected
        ? bucketPoints.reduce((sum, point) => sum + (point.expected_count ?? 0), 0)
        : null
      // Buckets are treated as independent samples, so variance adds —
      // stddev for the aggregate is sqrt(Σ σᵢ²). Null unless every source
      // bucket carried a stddev.
      const hasFullStddev = bucketPoints.every(point => point.stddev !== null)
      const stddev = hasFullStddev
        ? Math.sqrt(
            bucketPoints.reduce((sum, point) => {
              const s = point.stddev ?? 0
              return sum + s * s
            }, 0),
          )
        : null

      // Re-test significance at the aggregate level. Single buckets (and
      // groups with no coherent aggregate baseline) fall through to the
      // un-aggregated pass-through so hourly behavior is unchanged; multi-hour
      // rollups are re-flagged only when the total itself is significant. The
      // inline null checks also narrow expectedCount/stddev to numbers.
      let isAnomaly: boolean
      let zScore: number | null
      let anomalyDirection: EventMetricPoint['anomaly_direction']

      if (bucketPoints.length > 1 && expectedCount !== null && stddev !== null && stddev > 0) {
        const aggregateZ = (count - expectedCount) / stddev
        isAnomaly = Math.abs(aggregateZ) >= AGGREGATE_ANOMALY_Z_THRESHOLD
        zScore = isAnomaly ? aggregateZ : null
        anomalyDirection = isAnomaly ? (aggregateZ >= 0 ? 'spike' : 'drop') : null
      } else {
        isAnomaly = strongestAnomaly !== undefined
        zScore = strongestAnomaly?.z_score ?? null
        anomalyDirection = strongestAnomaly?.anomaly_direction ?? null
      }

      return {
        bucket,
        count,
        expected_count: expectedCount,
        stddev,
        is_anomaly: isAnomaly,
        anomaly_direction: anomalyDirection,
        z_score: zScore,
        // A verdict answers the native bucket's signal; a rolled-up marker
        // shows the strongest flagged source's, and only while it is flagged.
        verdict: isAnomaly ? (strongestAnomaly?.verdict ?? undefined) : undefined,
      }
    })
}
