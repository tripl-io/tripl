// Pure tick/axis-sizing helpers for the chart components. Kept out of chart.tsx
// so that file exports only components (react-refresh/only-export-components);
// chart.tsx and the chart tests import these.

import type { MetricsGranularity } from '@/lib/metrics'
import { APP_LOCALE, formatCompactNumber, formatNumber } from '@/lib/format'
import { pluralize } from '@/lib/plural'

// Time-zone policy for bucket labels, mirrored next to
// `formatDateTime` in lib/datetime.ts:
//
//  - Day, week and month buckets are CALENDAR buckets the server cut in UTC
//    (see getBucketStart / backend bucketing.py). They are labelled in UTC:
//    formatted in the viewer's zone, a Monday week bucket would print as "Week
//    of Sun Jun 7" west of Greenwich and a day bucket under the wrong date
//    .
//  - 15-minute, hour and 6-hour buckets are INSTANTS. They are labelled in the
//    viewer's local zone, like every other timestamp in the app (formatDateTime,
//    the signal card, the annotation list, the `datetime-local` annotation
//    input). Labelled in UTC with no zone, a spike at 14:00 local read "11:00"
//    on the chart for a UTC+3 reader while the anomaly card beside it said
//    "14:00".
const UTC = 'UTC'

/** True for the granularities whose buckets are instants, not calendar days. */
export function isSubDayGranularity(granularity: MetricsGranularity): boolean {
  return granularity === '15min' || granularity === 'hour' || granularity === '6h'
}

export function formatTick(dateStr: string, granularity: MetricsGranularity): string {
  const d = new Date(dateStr)

  switch (granularity) {
    // A 15-minute bucket needs its minutes on the axis: without them, the four
    // buckets of an hour all render as the same tick.
    case '15min':
      return d.toLocaleString(APP_LOCALE, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      })
    // Buckets start on the UTC hour, which in a half- or quarter-hour zone
    // (Asia/Kolkata +5:30, Adelaide, Kathmandu) is xx:30 or xx:45 local: an
    // hour-only tick read "05 AM" under a "05:30 AM" tooltip.
    case 'hour':
    case '6h':
      return d.toLocaleString(APP_LOCALE, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        ...(d.getMinutes() !== 0 ? { minute: '2-digit' } : {}),
      })
    case 'day':
      return d.toLocaleDateString(APP_LOCALE, { month: 'short', day: 'numeric', timeZone: UTC })
    case 'week':
      return d.toLocaleDateString(APP_LOCALE, { month: 'short', day: 'numeric', timeZone: UTC })
    case 'month':
      return d.toLocaleDateString(APP_LOCALE, { month: 'short', year: 'numeric', timeZone: UTC })
  }
}

/** A day-boundary tick on a sub-day axis: the local date alone ("Sep 20"). */
export function formatDayTick(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString(APP_LOCALE, { month: 'short', day: 'numeric' })
}

const DAY_MS = 86_400_000
// Below two days an hourly axis is read by the hour; from two days on, the
// reader wants to find a day, so the ticks move to day boundaries.
const DAY_TICK_MIN_SPAN_MS = 2 * DAY_MS
const MAX_DAY_TICKS = 8

/**
 * Explicit x-axis ticks for a sub-day series spanning two days or more: the
 * first bucket of each LOCAL calendar day, thinned to at most `maxTicks`.
 * Recharts' own spacing put a 7-day hourly chart's ticks every ~21 hours
 * ("Sep 20, 01 AM", "Sep 20, 10 PM", "Sep 21, 07 PM"), long labels that
 * crowded each other and never landed on a day.
 *
 * Returns null when the default ticks should stay: a calendar granularity, a
 * span under two days, or no day boundary inside the data. `buckets` must be in
 * ascending order, as every chart's rows are. The first bucket only counts
 * when it sits exactly on local midnight — otherwise "Sep 20" would label a
 * bucket from the middle of that day.
 */
export function dayBoundaryTicks(
  buckets: string[],
  granularity: MetricsGranularity,
  maxTicks: number = MAX_DAY_TICKS,
): string[] | null {
  if (!isSubDayGranularity(granularity) || buckets.length < 2) return null
  const first = Date.parse(buckets[0] ?? '')
  const last = Date.parse(buckets[buckets.length - 1] ?? '')
  if (!Number.isFinite(first) || !Number.isFinite(last) || last - first < DAY_TICK_MIN_SPAN_MS) {
    return null
  }
  const boundaries: string[] = []
  let previousDay: string | null = null
  for (const bucket of buckets) {
    const d = new Date(bucket)
    if (Number.isNaN(d.getTime())) continue
    const day = `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`
    const startsDay = previousDay === null
      ? d.getHours() === 0 && d.getMinutes() === 0
      : day !== previousDay
    if (startsDay) boundaries.push(bucket)
    previousDay = day
  }
  if (boundaries.length === 0) return null
  const step = Math.max(1, Math.ceil(boundaries.length / Math.max(1, maxTicks)))
  return boundaries.filter((_, index) => index % step === 0)
}

export function formatTooltipLabel(dateStr: string, granularity: MetricsGranularity): string {
  const d = new Date(dateStr)

  switch (granularity) {
    case '15min':
    case 'hour':
    case '6h':
      return d.toLocaleString(APP_LOCALE, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      })
    case 'day':
      return d.toLocaleDateString(APP_LOCALE, {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
        timeZone: UTC,
      })
    case 'week':
      return `Week of ${d.toLocaleDateString(APP_LOCALE, {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
        timeZone: UTC,
      })}`
    case 'month':
      return d.toLocaleDateString(APP_LOCALE, {
        month: 'long',
        year: 'numeric',
        timeZone: UTC,
      })
  }
}

/**
 * What a chart's values count. A plain string is printed as written (callers
 * pass units like "%" or "events (p95)"); the pair form agrees with the number,
 * so a one-event bucket reads "1 event", not "1 events".
 */
export type SeriesNoun = string | { singular: string; plural: string }

/** The default noun of every volume chart. */
export const EVENTS_NOUN = { singular: 'event', plural: 'events' } as const

function nounPair(noun: SeriesNoun): { singular: string; plural: string } | null {
  if (typeof noun !== 'string') return noun
  // The bare "events" most callers pass is the default noun spelled as a string.
  return noun === EVENTS_NOUN.plural ? EVENTS_NOUN : null
}

/** The plural spelling, for labels that name the series rather than a count. */
export function seriesNounPlural(noun: SeriesNoun): string {
  return typeof noun === 'string' ? noun : noun.plural
}

/**
 * `1 event`, `1,234 events`, `12 %`: a tooltip value in the app locale (the
 * same one the bucket label beside it uses) followed by the agreeing noun.
 */
export function formatSeriesValue(value: number, noun: SeriesNoun): string {
  const text = formatNumber(value)
  const pair = nounPair(noun)
  if (pair) return `${text} ${pluralize(value, pair.singular, pair.plural)}`
  return noun ? `${text} ${noun}` : text
}

/**
 * The categorical palette for multi-series charts, one list
 * for the chart and the monitoring tabs that pin series to slots.
 *
 * Each slot reads a `--series-N` theme token when the theme defines one. The
 * fallbacks are fixed hues with a light and a dark value (`light-dark()`, which
 * follows the `color-scheme` the theme sets on the root), never a neutral,
 * accent or status token: the By version chart draws the latest release in
 * `--primary` (the accent), a pre-release in `--warning` and "Other" in
 * `--fg-tertiary` (grey), and anomaly dots in `--danger`, so a slot built
 * from any of those drew two series in one colour, and under the rose accent
 * the first slot was the anomaly red. The hues skip the red / amber band
 * (danger, warning, the rose and amber accents) and the default teal accent.
 */
export const SERIES_COLORS = [
  'var(--series-1, light-dark(oklch(0.52 0.15 258), oklch(0.72 0.13 258)))',
  'var(--series-2, light-dark(oklch(0.52 0.13 148), oklch(0.75 0.15 148)))',
  'var(--series-3, light-dark(oklch(0.52 0.17 305), oklch(0.74 0.14 305)))',
  'var(--series-4, light-dark(oklch(0.55 0.11 105), oklch(0.8 0.14 105)))',
  'var(--series-5, light-dark(oklch(0.55 0.1 225), oklch(0.78 0.11 225)))',
  'var(--series-6, light-dark(oklch(0.53 0.18 340), oklch(0.74 0.15 340)))',
  'var(--series-7, light-dark(oklch(0.42 0.08 170), oklch(0.66 0.1 170)))',
  'var(--series-8, light-dark(oklch(0.45 0.14 280), oklch(0.8 0.09 280)))',
] as const

// Pluralization-aware anomaly-count sentence shared by every chart sr-only
// summary (single-series, multi-series, compact) so the singular/plural split is
// defined once. "1 anomaly detected" vs "N anomalies detected" — never the
// broken "1 anomalies detected".
export function formatAnomalyCount(count: number): string {
  return count === 1 ? '1 anomaly detected' : `${count} anomalies detected`
}

// Cap for how many humanized bucket labels a screen-reader summary enumerates
// before collapsing the tail into "and N more". Keeps the sr-only text from
// reading out dozens of timestamps one by one.
const BUCKET_PREVIEW_LIMIT = 5

// Humanize a list of bucket instants into a short, comma-separated preview for
// sr-only text. Reuses formatTooltipLabel so raw ISO strings never surface, and
// bounds the enumeration to the first BUCKET_PREVIEW_LIMIT entries, appending
// "and N more" for the remainder.
export function summarizeBuckets(
  buckets: string[],
  granularity: MetricsGranularity,
  max: number = BUCKET_PREVIEW_LIMIT,
): string {
  if (buckets.length === 0) return ''
  const shown = buckets.slice(0, max).map(bucket => formatTooltipLabel(bucket, granularity))
  const remaining = buckets.length - shown.length
  const preview = shown.join(', ')
  return remaining > 0 ? `${preview}, and ${remaining} more` : preview
}

// Collapse a forecast series (one bucket per point) into a single humanized
// range ("Forecast from <start> to <end>") instead of one span per bucket.
export function summarizeForecastRange(
  buckets: string[],
  granularity: MetricsGranularity,
): string {
  const first = buckets[0]
  const last = buckets[buckets.length - 1]
  if (first === undefined || last === undefined) return ''
  const start = formatTooltipLabel(first, granularity)
  if (buckets.length === 1) return `Forecast for ${start}`
  const end = formatTooltipLabel(last, granularity)
  return `Forecast from ${start} to ${end}`
}

// Compact axis/tick labels that never blow out the reserved Y-axis width
// ("380k", "1.5M"). Defined once in lib/format.ts; the chart and its tests keep
// the historical name.
export function formatCount(value: number): string {
  return formatCompactNumber(value)
}

// Approximate advance width (px) of one tick character at `text-body-sm`
// (12.5px, the axis tick size in chart.tsx) in the default sans stack —
// digits/'k'/'M'/'%' average out a touch under this. It was 7.4 for 12px.
const Y_AXIS_CHAR_PX = 7.7
// Gap between the (hidden) tick line and the label — matches YAxis tickMargin.
const Y_AXIS_TICK_MARGIN = 8
// Breathing room so the leftmost digit never touches the chart edge.
const Y_AXIS_PADDING = 8
// Never dip below recharts' comfortable default for 1–2 char labels.
const Y_AXIS_MIN_WIDTH = 40
// Hard ceiling so a pathological label (float noise, NaN/Infinity, or an
// oversized custom formatter) can never reserve a Y-axis wide enough to shove
// the plot area sideways. A compact count/percent label tops out near 6 chars
// (~63px), so 80px leaves headroom without ever eating the chart.
const Y_AXIS_MAX_WIDTH = 80

/**
 * Reserve a Y axis wide enough for the widest tick label the axis will paint,
 * measured through the ACTIVE formatter (compact counts, or a caller's percent
 * formatter). Fixes 6-digit labels ("380k") clipping their left digits when the
 * axis width was a fixed 48px. Probes the domain extremes plus a 10% overshoot
 * to catch the "nice" tick recharts rounds up to above the data max.
 */
export function axisWidthForValues(
  values: number[],
  formatter: (value: number) => string,
): number {
  const [head] = values
  if (head === undefined) return Y_AXIS_MIN_WIDTH
  let min = head
  let max = head
  for (const value of values) {
    if (value < min) min = value
    if (value > max) max = value
  }
  // The ±10% overshoot anticipates the "nice" tick recharts rounds up to above
  // the data max. Skip non-finite probes so NaN/Infinity can't reach the
  // formatter and produce a garbage-length label.
  const candidates = [min, max, max * 1.1, min * 1.1].filter(Number.isFinite)
  let maxLen = 0
  for (const candidate of candidates) {
    const len = formatter(candidate).length
    if (len > maxLen) maxLen = len
  }
  const width = Math.ceil(Y_AXIS_TICK_MARGIN + maxLen * Y_AXIS_CHAR_PX + Y_AXIS_PADDING)
  return Math.min(Y_AXIS_MAX_WIDTH, Math.max(Y_AXIS_MIN_WIDTH, width))
}

/**
 * Keeps the recharts <svg class="recharts-surface"> out of the tab order.
 *
 * Recharts focuses its surface by default, which added an unnamed tab stop on
 * every page carrying a chart. Each chart wrapper already
 * carries role="img", an aria-label and an sr-only text summary of the series,
 * so the surface itself has nothing to announce.
 */
export const CHART_SURFACE_TAB_INDEX = -1
