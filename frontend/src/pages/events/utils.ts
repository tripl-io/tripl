import { APP_LOCALE } from '@/lib/format'
import { arrayMove } from '@dnd-kit/sortable'
import type {
  EventMetricPoint,
  EventType,
  MetaFieldDefinition,
  MonitoringSignal,
  Variable,
} from '@/types'
import { GRANULARITY_OPTIONS, RANGE_OPTIONS } from '@/lib/metrics'

export const TAB_METRICS_RANGE_DAYS_DEFAULT = 7
export const ROW_METRICS_RANGE_HOURS = 48
export const ROW_METRICS_LABEL = `${ROW_METRICS_RANGE_HOURS}h`

export const TAB_METRICS_RANGE_OPTIONS = RANGE_OPTIONS
export const TAB_METRICS_GRANULARITY_OPTIONS = GRANULARITY_OPTIONS

// Stable empty references — lets the consumers feed through `??` without
// minting a new array/object every render and busting React.memo.
export const EMPTY_EVENT_TYPES: EventType[] = []
export const EMPTY_META_FIELDS: MetaFieldDefinition[] = []
export const EMPTY_VARIABLES: Variable[] = []
export const EMPTY_TAGS: string[] = []
export const EMPTY_SIGNALS: MonitoringSignal[] = []

// Per-row queries key on the event ids they cover, so the ids are split into
// fixed-size, INDEX-ALIGNED buckets rather than sent as one accumulated list.
// With 200-row pages + infinite scroll, an all-ids key changed on every append
// and re-sent the entire set; bucketing keeps each already-loaded bucket's key
// (and cache entry) stable, so appending a page only fetches the new bucket.
// Window metrics were fixed this way first; the signals query next to
// them kept the accumulating key for a while longer.
export const EVENT_ID_BUCKET_SIZE = 100

export function chunkEventIds(eventIds: string[]): string[][] {
  const buckets: string[][] = []
  for (let start = 0; start < eventIds.length; start += EVENT_ID_BUCKET_SIZE) {
    buckets.push(eventIds.slice(start, start + EVENT_ID_BUCKET_SIZE))
  }
  return buckets
}
export const EMPTY_EVENT_WINDOW_METRICS: {
  event_id: string
  scan_config_id: string | null
  interval: string
  total_count: number
  data: EventMetricPoint[]
}[] = []
export const EMPTY_WINDOW_POINTS: EventMetricPoint[] = []

const compactCountFormatter = new Intl.NumberFormat(APP_LOCALE, {
  notation: 'compact',
  compactDisplay: 'short',
  maximumFractionDigits: 0,
})

/**
 * Compact volume label for the 48h column — "505K", "4M".
 *
 * Case is load-bearing here and must NOT be lowercased: the neighbouring
 * "Last seen" cell renders relative durations ("1m ago", "1h ago") in a
 * similarly sized tabular-figure cell, so a lowercase "1m" volume under a
 * header that reads only "48h" invited a six-order-of-magnitude misread.
 * Uppercase M also matches every other count formatter in the
 * app (lib/metricFormat.ts, components/ui/chart-format.ts), including the
 * chart directly above this table.
 */
export function formatCompactCount(value: number): string {
  return compactCountFormatter.format(value)
}

const HOUR_MS = 60 * 60 * 1000
const DAY_MS = 24 * HOUR_MS

/** Width of each half of the Δ column's comparison, in hours. */
export const WINDOW_DELTA_HOURS = 24

/**
 * Least of its 24 hours a window may carry and still be divided by the other.
 *
 * The two windows are summed raw, so a coverage gap between them leaks straight
 * into the printed percentage: at 22 of 24 hours (the demo's 2h collection lag)
 * the number is ~8% low, which the "*" marker and the tooltip disclose. At 1 of
 * 24 hours it is ~24x off, which is how 13 rows on one stand came out between
 * +1673% and +1907% — no marker rescues a number that is mostly
 * gap. Three quarters caps the leak at a third and still keeps the column alive
 * through a six-hour lag.
 */
const MIN_WINDOW_COVERAGE = 0.75

const exactCountFormatter = new Intl.NumberFormat(APP_LOCALE)

/**
 * Width of the collection grid, read off the series instead of assumed. The
 * window-metrics endpoint returns stored EventMetric rows only — no zero fill —
 * so the smallest gap between consecutive buckets is the scan interval whenever
 * any two adjacent buckets survived. A gap-riddled series only inflates it,
 * which makes the coverage below read LOW, i.e. it errs toward disclosing an
 * incomplete window rather than hiding one.
 */
function inferBucketMs(points: EventMetricPoint[]): number {
  let smallest = Number.POSITIVE_INFINITY
  let previous: EventMetricPoint | undefined
  for (const point of points) {
    if (previous) {
      const gap = Date.parse(point.bucket) - Date.parse(previous.bucket)
      if (gap > 0 && gap < smallest) smallest = gap
    }
    previous = point
  }
  return Number.isFinite(smallest) ? smallest : 0
}

export type WindowDeltaStatus =
  | 'ok'
  | 'no-series'
  | 'no-prior-volume'
  | 'window-too-thin'

export interface WindowDelta {
  status: WindowDeltaStatus
  /** Percent change; non-null only when `status` is 'ok'. */
  pct: number | null
  /** Raw volume summed in each half, as quoted back in the tooltip. */
  recentTotal: number
  priorTotal: number
  /** How many of each window's 24 hours actually carry a bucket. */
  recentCoveredHours: number
  priorCoveredHours: number
  /** Hours between the newest bucket and now; 0 when collection is current. */
  trailingGapHours: number
  /** Either half is short of its 24 hours, so it compares less than a window. */
  partial: boolean
}

/**
 * 24h volume delta for a catalog row: percent change of the most recent 24h of
 * volume versus the prior 24h, read off the same window-metric series the event
 * detail page uses (the first implementation keyed off per-bucket anomaly
 * `expected_count`, which is null on non-anomaly buckets, so nearly every row
 * rendered "—"). Summing raw points equals summing an hourly aggregation, so no
 * pre-bucketing is needed.
 *
 * Both halves are anchored on NOW, not on the newest bucket that carries data —
 * "Δ · 24h" is a claim about the last 24 hours, and anchoring on the newest
 * bucket made a lagging collection shrink the PRIOR half instead of the recent
 * one, which inflates the ratio. Anchored here, a lag shrinks the
 * recent half, so the error is conservative and, above `MIN_WINDOW_COVERAGE`,
 * small enough to print with a marker.
 *
 * It does NOT gate on the series spanning a full 47h. That blanket span guard
 * blanked the whole column on the fresh demo, where 47 hourly points span 46.0h
 * because collection ends ~2h before now, while the same payload carries recent
 * 51,456 against prior 45,812 — a perfectly sound +12%. Coverage is
 * now measured per half and reported instead of being a reason to render
 * nothing; the genuine empty states are a prior half with no volume at all and
 * a half too thin to divide by.
 *
 * NOT yet shared with MonitoringDetailPage.computeEventStats, which still splits
 * on the newest bucket with no coverage check at all — so the detail page prints
 * the inflated figure this column stopped printing. Folding it onto this
 * function is follow-up work in that file.
 */
export function computeWindowDelta(
  points: EventMetricPoint[],
  now: number = Date.now(),
): WindowDelta {
  const noSeries: WindowDelta = {
    status: 'no-series',
    pct: null,
    recentTotal: 0,
    priorTotal: 0,
    recentCoveredHours: 0,
    priorCoveredHours: 0,
    trailingGapHours: 0,
    partial: true,
  }
  if (points.length < 2) return noSeries
  const bucketMs = inferBucketMs(points)
  if (bucketMs <= 0) return noSeries

  let recentTotal = 0
  let priorTotal = 0
  let recentBuckets = 0
  let priorBuckets = 0
  let latest = Number.NEGATIVE_INFINITY
  for (const point of points) {
    const at = Date.parse(point.bucket)
    if (!Number.isFinite(at)) continue
    if (at > latest) latest = at
    const age = now - at
    // A bucket newer than `now` (clock skew between browser and collector)
    // belongs to the recent half, not to neither.
    if (age < DAY_MS) {
      recentTotal += point.count
      recentBuckets += 1
    } else if (age <= 2 * DAY_MS) {
      // Inclusive at the far edge: the fetch window is [now-48h, now]
      // (ROW_METRICS_RANGE_HOURS), so the bucket stamped exactly now-48h is in
      // the payload and belongs to the prior half rather than to neither.
      priorTotal += point.count
      priorBuckets += 1
    }
  }
  if (!Number.isFinite(latest)) return noSeries

  // Expressed in hours rather than buckets so the tooltip reads the same on any
  // collection grid: 23 hourly buckets and 1 daily bucket are "23 of 24" and
  // "24 of 24" hours respectively.
  const coveredHours = (buckets: number): number =>
    Math.min(WINDOW_DELTA_HOURS, Math.round((buckets * bucketMs) / HOUR_MS))
  const recentCoveredHours = coveredHours(recentBuckets)
  const priorCoveredHours = coveredHours(priorBuckets)
  const measured = {
    recentTotal,
    priorTotal,
    recentCoveredHours,
    priorCoveredHours,
    trailingGapHours: Math.max(0, Math.round((now - latest) / HOUR_MS)),
    partial:
      recentCoveredHours < WINDOW_DELTA_HOURS || priorCoveredHours < WINDOW_DELTA_HOURS,
  }

  const minCoveredHours = WINDOW_DELTA_HOURS * MIN_WINDOW_COVERAGE
  if (recentCoveredHours < minCoveredHours || priorCoveredHours < minCoveredHours) {
    return { ...measured, status: 'window-too-thin', pct: null }
  }
  if (priorTotal <= 0) return { ...measured, status: 'no-prior-volume', pct: null }
  return { ...measured, status: 'ok', pct: ((recentTotal - priorTotal) / priorTotal) * 100 }
}

/**
 * The Δ cell's tooltip. It always names what was compared and how much of each
 * window was there to compare, because the sentence it replaces ("No prior 24h
 * window to compare against") was asserted for every null — including the fresh
 * demo, where the prior window held 45,812 events.
 */
export function describeWindowDelta(delta: WindowDelta): string {
  const {
    status,
    recentTotal,
    priorTotal,
    recentCoveredHours,
    priorCoveredHours,
    trailingGapHours,
  } = delta
  const lag =
    trailingGapHours > 0 ? `; the series ends ${trailingGapHours}h before now` : ''

  if (status === 'no-series') {
    return 'No metrics collected for this event in the last 48h.'
  }
  if (status === 'window-too-thin') {
    return (
      `Too little of the window to compare: the last 24h are covered to `
      + `${recentCoveredHours} of ${WINDOW_DELTA_HOURS} hours and the 24h before it to `
      + `${priorCoveredHours} of ${WINDOW_DELTA_HOURS}${lag}.`
    )
  }
  if (status === 'no-prior-volume') {
    return (
      `No volume in the prior 24h to compare against — the last 24h carry `
      + `${exactCountFormatter.format(recentTotal)}.`
    )
  }

  const compared =
    `Last 24h ${exactCountFormatter.format(recentTotal)} vs `
    + `${exactCountFormatter.format(priorTotal)} in the 24h before it.`
  if (!delta.partial) return `${compared} Both windows fully covered.`
  return (
    `${compared} Incomplete window: the last 24h are covered to `
    + `${recentCoveredHours} of ${WINDOW_DELTA_HOURS} hours and the 24h before it to `
    + `${priorCoveredHours} of ${WINDOW_DELTA_HOURS}${lag}, so this compares less `
    + `than a full window.`
  )
}

/**
 * Compute the new row order produced by a drag-reorder.
 *
 * When `activeId` belongs to a multi-row selection, the whole selection moves
 * as a contiguous block to the drop target, preserving the selected rows'
 * relative order. Otherwise a single row moves. Returns `null` when there is
 * nothing to apply — unknown ids, a no-op drop, or the block dropped onto one
 * of its own rows.
 */
export function reorderWithSelection(
  ids: string[],
  selectedSet: Set<string>,
  activeId: string,
  overId: string,
): string[] | null {
  if (activeId === overId) return null
  const oldIndex = ids.indexOf(activeId)
  const newIndex = ids.indexOf(overId)
  if (oldIndex < 0 || newIndex < 0) return null

  const isMultiDrag = selectedSet.size > 1 && selectedSet.has(activeId)
  if (!isMultiDrag) {
    return arrayMove(ids, oldIndex, newIndex)
  }

  const movingIds = ids.filter((id) => selectedSet.has(id))
  const remaining = ids.filter((id) => !selectedSet.has(id))
  const overInRemaining = remaining.indexOf(overId)
  if (overInRemaining < 0) return null // dropped onto another selected row
  const insertAt = oldIndex < newIndex ? overInRemaining + 1 : overInRemaining
  return [
    ...remaining.slice(0, insertAt),
    ...movingIds,
    ...remaining.slice(insertAt),
  ]
}

/**
 * The contiguous span of `next` that differs from `prev` (two orderings of the
 * same ids), or `[]` when nothing moved. The ids in that span are a permutation
 * of the ids in the same span of `prev`, which is what the reorder endpoint
 * needs to renumber them among their own order slots.
 */
export function changedSlice(prev: readonly string[], next: readonly string[]): string[] {
  let first = 0
  while (first < next.length && prev[first] === next[first]) first += 1
  if (first === next.length) return []
  let last = next.length - 1
  while (last > first && prev[last] === next[last]) last -= 1
  return next.slice(first, last + 1)
}

export const LAST_SEEN_COL_KEY = 'last_seen'

export { formatRelativeTime } from '@/lib/datetime'

export function getSignalTone(signal: MonitoringSignal) {
  if (signal.state === 'latest_scan') {
    return {
      compact: 'text-destructive',
      regular: 'bg-destructive text-destructive-foreground',
      // Outline, not solid red: solid red means "this destroys something",
      // and "View signal" only navigates. The icon carries the danger tone
      // (DS-20).
      button: 'outline' as const,
      buttonClassName: '',
      iconClassName: 'text-danger',
      title: 'Open latest scan anomaly',
    }
  }

  // Soft fill rather than the solid amber this used to paint: the tone token
  // is the AA-safe ink for its own fill in both themes, whereas a solid needs a
  // foreground that flips with the theme. It also keeps the louder solid
  // treatment above exclusive to the latest scan, which is the urgent one.
  return {
    compact: 'text-warning',
    regular: 'bg-warning-soft text-warning ring-1 ring-warning/40',
    button: 'outline' as const,
    buttonClassName: 'border-warning/50 bg-warning-soft text-warning hover:bg-warning/20',
    iconClassName: '',
    title: 'Open recent anomaly',
  }
}

export function pickLatestSignal(
  signals: MonitoringSignal[],
  scopeType: MonitoringSignal['scope_type'],
) {
  return signals
    .filter(signal => signal.scope_type === scopeType)
    .sort((left, right) => right.bucket.localeCompare(left.bucket))[0] ?? null
}

export function mapLatestSignals(
  signals: MonitoringSignal[],
  scopeType: MonitoringSignal['scope_type'],
) {
  const entries = new Map<string, MonitoringSignal>()
  signals
    .filter(signal => signal.scope_type === scopeType)
    .sort((left, right) => right.bucket.localeCompare(left.bucket))
    .forEach(signal => {
      if (!entries.has(signal.scope_ref)) entries.set(signal.scope_ref, signal)
    })
  return entries
}

export function deriveRowSignalFromMetrics(
  eventId: string,
  scanConfigId: string | null | undefined,
  points: EventMetricPoint[],
): MonitoringSignal | null {
  const anomalyPoints = points.filter(
    point => point.is_anomaly && point.anomaly_direction !== null,
  )
  const latestAnomaly = anomalyPoints[anomalyPoints.length - 1]
  if (!latestAnomaly) return null

  const latestBucket = points[points.length - 1]?.bucket ?? latestAnomaly.bucket

  return {
    scan_config_id: scanConfigId ?? '',
    scope_type: 'event',
    scope_ref: eventId,
    state: latestAnomaly.bucket === latestBucket ? 'latest_scan' : 'recent',
    event_id: eventId,
    event_type_id: null,
    bucket: latestAnomaly.bucket,
    actual_count: latestAnomaly.count,
    expected_count: latestAnomaly.expected_count ?? latestAnomaly.count,
    stddev: 0,
    z_score: latestAnomaly.z_score ?? 0,
    direction: latestAnomaly.anomaly_direction ?? 'drop',
    // An event-scope signal has no display unit, and a point read off the
    // series carries no detection time.
    unit: null,
    detected_at: null,
    // A per-event row signal is standalone here, never an incident rollup child.
    incident_child: false,
  }
}

// --- Glitchy / templated event-name + value rendering helpers (UX-9, UX-21) ---

const TEMPLATE_TOKEN_SPLIT = /(\$\{[^}{]*\})/g
const TEMPLATE_TOKEN_MATCH = /^\$\{[^}{]*\}$/

// The name-segment split lives in lib/ now, so components/event-name.tsx no
// longer imports this page module (DS-41); re-exported for the page's callers.
export { NAME_SEGMENT_SEPARATOR, splitEventName, type NameSegment } from '@/lib/eventNameSegments'

export type ValuePart = { text: string; token: boolean; known?: boolean }

export type ResolvedTemplateToken = {
  token: string
  variable: Variable | null
}

/**
 * Resolve complete `${token}` placeholders against canonical variable names,
 * legacy scan identities, and user-configured bindings.
 */
export function resolveTemplateTokens(
  value: string,
  variables: Variable[],
): ResolvedTemplateToken[] {
  if (!value.includes('${')) return []

  const variablesByToken = new Map<string, Variable>()
  for (const variable of variables) {
    for (const token of [variable.name, variable.source_name, ...variable.bindings]) {
      if (token && !variablesByToken.has(token)) variablesByToken.set(token, variable)
    }
  }

  return value
    .split(TEMPLATE_TOKEN_SPLIT)
    .filter((part) => TEMPLATE_TOKEN_MATCH.test(part))
    .map((token) => ({
      token,
      variable: variablesByToken.get(token.slice(2, -1)) ?? null,
    }))
}

/**
 * Split a property value into plain-text and `${…}` template-token parts so the
 * tokens can be tinted to read as variables rather than literal text.
 */
export function splitTemplateValue(value: string, variables?: Variable[]): ValuePart[] {
  if (!value.includes('${')) return [{ text: value, token: false }]
  const knownByToken = variables
    ? new Map(resolveTemplateTokens(value, variables).map(({ token, variable }) => [token, variable !== null]))
    : null
  return value
    .split(TEMPLATE_TOKEN_SPLIT)
    .filter((p) => p !== '')
    .map((p) => {
      const token = TEMPLATE_TOKEN_MATCH.test(p)
      return {
        text: p,
        token,
        ...(token && knownByToken ? { known: knownByToken.get(p) ?? false } : {}),
      }
    })
}

/** Structural subset of Variable used by the ${} autocomplete surfaces. */
export interface VariableSuggestionShape {
  name: string
  description?: string
  bindings?: string[]
  allowed_values?: string[]
}

export function suggestionMatches(v: VariableSuggestionShape, filter: string): boolean {
  const needle = filter.toLowerCase()
  return (
    v.name.toLowerCase().includes(needle)
    || (v.description ?? '').toLowerCase().includes(needle)
    || (v.bindings ?? []).some(binding => binding.toLowerCase().includes(needle))
  )
}

const NAME_FORMAT_TOKEN = /\{([^}]+)\}/g

/**
 * Apply a scan `event_name_format` to entered field values (client mirror of
 * backend core/name_template.py). Dotted keys walk the field's JSON value;
 * unresolved keys stay literal `{key}` and are reported in `missing`.
 */
export function applyEventNameFormat(
  fmt: string,
  valuesByField: Record<string, string>,
): { name: string; missing: string[] } {
  const missing: string[] = []
  const name = fmt.replace(NAME_FORMAT_TOKEN, (whole, key: string) => {
    const direct = valuesByField[key]
    if (direct) return direct
    if (key.includes('.')) {
      const [column = '', ...segments] = key.split('.')
      const raw = valuesByField[column]
      if (raw) {
        try {
          let node: unknown = JSON.parse(raw)
          for (const segment of segments) {
            if (typeof node !== 'object' || node === null || Array.isArray(node)) {
              node = undefined
              break
            }
            node = (node as Record<string, unknown>)[segment]
          }
          if (node !== undefined && node !== null && typeof node !== 'object') return String(node)
        } catch {
          // unparseable (e.g. still ${templated}) — fall through to missing
        }
      }
    }
    missing.push(key)
    return whole
  })
  return { name, missing }
}

/**
 * The field names a scan `event_name_format` builds the event name from.
 *
 * A dotted key walks INTO a column's JSON, so `{page_data.type}` needs the
 * `page_data` field — reducing to the part before the first dot is what makes
 * the answer a set of rows the form can actually point at. Client mirror of
 * `name_format_base_columns` in backend `core/analyzers/event_plan.py`, and it
 * reuses this module's `{key}` regex rather than restating the grammar: four
 * copies of that grammar is what took production down once.
 */
export function nameFormatBaseColumns(fmt: string | null | undefined): Set<string> {
  if (!fmt) return new Set()
  return new Set(
    [...fmt.matchAll(NAME_FORMAT_TOKEN)].flatMap(match => {
      const column = match[1]?.split('.')[0]
      return column === undefined ? [] : [column]
    }),
  )
}

/**
 * The table's active filters the metrics endpoint cannot apply (it takes type,
 * search, status and tag only), by the label the toolbar gives them. The chart
 * names them rather than pass off an unfiltered series as the table's
 * (EVT-20). Lives here so TabMetricsCard.tsx exports only components.
 */
export function unappliedChartFilters({
  filterSilentDays,
  filterReviewed,
  filterOpenQuestions,
  hasColumnFilters,
  filterProperty,
}: {
  filterSilentDays: number | undefined
  filterReviewed: boolean | undefined
  filterOpenQuestions: boolean | undefined
  hasColumnFilters: boolean
  filterProperty?: string
}): string[] {
  const out: string[] = []
  if (filterSilentDays !== undefined) out.push('activity')
  if (filterReviewed !== undefined) out.push('reviewed')
  if (filterOpenQuestions !== undefined) out.push('questions')
  if (hasColumnFilters) out.push('column filters')
  if (filterProperty) out.push('property')
  return out
}
