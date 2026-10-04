import type { IntervalCode, ScanConfigPreview, ScanJob } from '@/types'
import { formatRelativeTime } from '@/lib/datetime'
import type { ScanStatus } from './scanLayoutConstants'

// Derive a canonical scan status from its most recent job. The real ScanConfig
// has no status column, so the latest job's state drives the dot/label.
export interface ScanRunInfo {
  status: ScanStatus
  lastRunLabel: string
  lastJob: ScanJob | null
}

/**
 * Run info for a scan whose job list has not arrived yet. "Never run" is a
 * verdict, not a placeholder: coercing the in-flight query to `[]` made every
 * row claim it had never run while the activity rail on the same screen listed
 * completed runs. Callers pass `undefined` for a loading query
 * and get the neutral `unknown` state instead.
 */
export const LOADING_SCAN_RUN_INFO: ScanRunInfo = {
  status: 'unknown',
  lastRunLabel: '',
  lastJob: null,
}

/**
 * Whether any job is still in a non-terminal state (pending/running). Drives the
 * adaptive polling fallback: fast polling stops once every job settles, and the
 * live stream (`scan_job.updated`) refreshes the list in the meantime.
 */
export function scanJobsHaveActiveWork(jobs: ScanJob[] | undefined): boolean {
  return (jobs ?? []).some((job) => job.status === 'running' || job.status === 'pending')
}

export function deriveScanRunInfo(jobs: ScanJob[] | undefined): ScanRunInfo {
  // `undefined` means "the job query has not resolved yet" — distinct from an
  // empty array, which really does mean this scan has never run.
  if (!jobs) return LOADING_SCAN_RUN_INFO
  const lastJob = jobs[0] ?? null
  if (!lastJob) return { status: 'idle', lastRunLabel: 'never', lastJob: null }
  if (lastJob.status === 'running' || lastJob.status === 'pending') {
    return { status: 'running', lastRunLabel: 'running', lastJob }
  }
  const stamp = lastJob.completed_at ?? lastJob.started_at ?? lastJob.created_at
  const lastRunLabel = formatRelativeTime(stamp)
  if (lastJob.status === 'failed') return { status: 'failed', lastRunLabel, lastJob }
  return { status: 'ok', lastRunLabel, lastJob }
}

// Total scan rows from a job's result summary, falling back across the fields the
// backend may populate (query rows scanned vs. processed).
export function jobRowsScanned(job: ScanJob | null): number | null {
  if (!job?.result_summary) return null
  const summary = job.result_summary
  return summary.query_rows_scanned ?? summary.catalog_rows_scanned ?? summary.scan_rows_processed ?? null
}

/**
 * What a run's scanned figure counts. Metrics collection reports warehouse rows
 * (`query_rows_scanned`); the catalog analyzer reports the rows of its
 * `GROUP BY ALL` breakdown (`scan_rows_processed`), which are distinct column
 * combinations grouped in the warehouse, not warehouse rows. Printing both as
 * "rows" put a catalog run 180× below its dry run's row count (#247 DA-4).
 */
export type JobScannedUnit = 'rows' | 'combinations'

export interface JobScanned {
  value: number
  unit: JobScannedUnit
  /**
   * A catalog run that reported both: the distinct combinations its GROUP BY
   * returned, behind the warehouse rows in `value` (i9mt.16).
   */
  combinations?: number
}

/**
 * Same precedence as `jobRowsScanned`, with the population named. Takes any job
 * shape with a result summary, so the Projects page's latest-run card reads it
 * through the same rule as the scan pages.
 */
export function jobScanned(job: Pick<ScanJob, 'result_summary'> | null): JobScanned | null {
  const summary = job?.result_summary
  if (!summary) return null
  if (summary.query_rows_scanned != null) return { value: summary.query_rows_scanned, unit: 'rows' }
  // A catalog run now also sums the `_cnt` of its breakdown rows: the warehouse
  // rows the dry run reports, so the two finally print the same kind of number.
  if (summary.catalog_rows_scanned != null) {
    return {
      value: summary.catalog_rows_scanned,
      unit: 'rows',
      ...(summary.scan_rows_processed != null ? { combinations: summary.scan_rows_processed } : {}),
    }
  }
  if (summary.scan_rows_processed != null) {
    return { value: summary.scan_rows_processed, unit: 'combinations' }
  }
  return null
}

/**
 * "4,428 rows" / "153 combos". `format` prints the figure (full digits by
 * default, the list's compact form where space is short); the noun agrees with
 * the raw value, not the printed one.
 */
export function formatJobScanned(
  scanned: JobScanned | null,
  format: (value: number) => string = value => value.toLocaleString(),
): string {
  if (!scanned) return '—'
  const { value, unit } = scanned
  const noun = unit === 'rows'
    ? (value === 1 ? 'row' : 'rows')
    : (value === 1 ? 'combo' : 'combos')
  return `${format(value)} ${noun}`
}

/**
 * Metric time-series points a run upserted. The four counters are disjoint
 * populations; summing them is the only number that means "points written".
 *
 * The detail stat card and the list chip MUST both call this. They used to
 * compute it two different ways — the card read
 * `breakdown_event_metrics ?? event_metrics`, the chip summed all four — so any
 * scan with breakdowns showed two different totals for the same run on two
 * screens, and a scan with only per-event series showed the breakdown count of
 * a run that had none.
 *
 * Null only when the run has no result summary, or reported none of the four.
 */
export function jobMetricPoints(job: ScanJob | null): number | null {
  const summary = job?.result_summary
  if (!summary) return null
  const counters = [
    summary.event_metrics,
    summary.type_metrics,
    summary.breakdown_event_metrics,
    summary.breakdown_type_metrics,
  ]
  if (counters.every((value) => value == null)) return null
  return counters.reduce((total: number, value) => total + (value ?? 0), 0)
}

const INTERVAL_MS: Record<IntervalCode, number> = {
  '15m': 15 * 60_000,
  '1h': 60 * 60_000,
  '6h': 6 * 60 * 60_000,
  '1d': 24 * 60 * 60_000,
  '1w': 7 * 24 * 60 * 60_000,
}

/**
 * A run that advanced the metric schedule: a scheduled collection, or (for a
 * run that names no mode) one that reported points. A replay does not count —
 * it backfills an explicit past window, so a replay run today must not make a
 * stalled schedule look current (the worker applies the same rule).
 */
function isMetricsRun(job: ScanJob): boolean {
  if (job.status !== 'completed') return false
  const mode = job.result_summary?.mode
  if (mode) return mode === 'metrics_collection'
  return (jobMetricPoints(job) ?? 0) > 0
}

export interface MetricsFreshness {
  /** The newest completed metrics run, or null when none has finished. */
  job: ScanJob | null
  /** When the newest metrics run finished, or null when none has. */
  lastAt: string | null
  /** When the schedule is next due, or null. */
  nextAt: number | null
  /** No point for more than two intervals: the schedule is not keeping up. */
  overdue: boolean
}

/**
 * The server's answer for one scan (`GET /scans/{id}`, i9mt.16): the newest
 * scheduled collection and the scheduler's own next due moment.
 */
export interface MetricsSchedule {
  lastRunAt: string | null
  nextRunAt: string | null
}

function parseMs(value: string | null | undefined): number | null {
  if (!value) return null
  const ms = Date.parse(value)
  return Number.isNaN(ms) ? null : ms
}

/**
 * How current a monitoring scan's metric series is, from its own job list.
 * The detail page's "Metric points" card read the newest run, which is usually
 * a catalog Run now with no points, so every monitoring scan showed "—" and
 * nothing said whether the series was up to date (#247 DA-5).
 *
 * With the server's `schedule`, the next run is the scheduler's due check
 * (the next interval boundary, not "last run + interval"), and the last run
 * survives a job page where catalog runs buried the newest collection.
 */
export function metricsFreshness(
  jobs: ScanJob[],
  interval: string | null,
  now: number = Date.now(),
  schedule: MetricsSchedule | null = null,
): MetricsFreshness {
  const last = jobs.find(isMetricsRun) ?? null
  const jobLastAt = last ? (last.completed_at ?? last.started_at ?? last.created_at) : null
  const serverLastAt = schedule?.lastRunAt ?? null
  const serverLastMs = parseMs(serverLastAt)
  const jobLastMs = parseMs(jobLastAt)
  const lastAt = serverLastMs != null && (jobLastMs == null || serverLastMs > jobLastMs)
    ? serverLastAt
    : jobLastAt
  const lastMs = parseMs(lastAt)
  const intervalMs = interval ? INTERVAL_MS[interval as IntervalCode] : undefined
  const serverNextMs = parseMs(schedule?.nextRunAt)
  if (lastMs == null || !intervalMs) {
    return { job: last, lastAt, nextAt: serverNextMs, overdue: false }
  }
  return {
    job: last,
    lastAt,
    nextAt: serverNextMs ?? lastMs + intervalMs,
    overdue: now - lastMs > 2 * intervalMs,
  }
}

/** "in 48m" / "in 3h" / "due now" — the future twin of `formatRelativeTime`. */
export function formatDueIn(at: number, now: number = Date.now()): string {
  const diffMin = Math.round((at - now) / 60_000)
  if (diffMin <= 0) return 'due now'
  if (diffMin < 60) return `in ${diffMin}m`
  if (diffMin < 24 * 60) return `in ${Math.floor(diffMin / 60)}h`
  return `in ${Math.floor(diffMin / (24 * 60))}d`
}

export function jobDurationSeconds(job: ScanJob): number | null {
  if (!job.started_at || !job.completed_at) return null
  return (new Date(job.completed_at).getTime() - new Date(job.started_at).getTime()) / 1000
}

/** A single "what changed" delta from a completed scan/collection. */
export interface ScanChange {
  label: string
  tone: 'success' | 'info' | 'warning' | 'danger'
}

/**
 * Summarise what a completed job actually changed (events written, metric rows,
 * signals, alerts). Returns only the non-zero deltas, so a scan/collection that
 * finishes can show "+N events · +N metric points · +N signals" instead of leaving the
 * user guessing whether anything happened. Empty for jobs that
 * are unfinished or produced no changes.
 */
export function summarizeScanChanges(job: ScanJob | null): ScanChange[] {
  const summary = job?.result_summary
  if (!summary) return []
  const changes: ScanChange[] = []
  const push = (value: number | undefined, singular: string, plural: string, tone: ScanChange['tone']) => {
    if (value != null && value > 0) {
      changes.push({ label: `+${value} ${value === 1 ? singular : plural}`, tone })
    }
  }
  push(summary.events_created, 'event', 'events', 'success')
  // These are time-series POINTS collected, not metric definitions created.
  // Calling them "metrics" made an ordinary scan read as "+2000 metrics" — as
  // though it had just defined two thousand metrics. One formula,
  // shared with the detail page's stat card.
  push(jobMetricPoints(job) || undefined, 'metric point', 'metric points', 'info')
  push(summary.variables_created, 'property', 'properties', 'info')
  push(summary.signals_added, 'signal', 'signals', 'danger')
  push(summary.alerts_queued, 'alert', 'alerts', 'warning')
  return changes
}

// Number of consecutive most-recent runs that FAILED. `jobs` is newest-first.
// Active (pending/running) jobs at the head are skipped so an in-flight retry
// does not reset the count; the streak stops at the first settled non-failed run.
// Used to collapse a wall of identical failed rows into one "failed last N runs"
// indicator.
export function consecutiveFailedRuns(jobs: ScanJob[]): number {
  let streak = 0
  for (const job of jobs) {
    if (job.status === 'pending' || job.status === 'running') continue
    if (job.status === 'failed') {
      streak += 1
      continue
    }
    break
  }
  return streak
}


export function formatPreviewCell(value: unknown): string {
  if (value == null) return '—'
  if (typeof value === 'string') return value
  return JSON.stringify(value)
}

export function splitFullJsonPath(fullPath: string): { column: string; path: string } | null {
  const separatorIndex = fullPath.indexOf('.')
  if (separatorIndex <= 0 || separatorIndex === fullPath.length - 1) return null
  return {
    column: fullPath.slice(0, separatorIndex),
    path: fullPath.slice(separatorIndex + 1),
  }
}

export function jsonColumnsWithSelectedPaths(
  preview: ScanConfigPreview,
  selectedJsonValuePaths: string[],
): ScanConfigPreview['json_columns'] {
  const byColumn = new Map<string, ScanConfigPreview['json_columns'][number]>()

  preview.json_columns.forEach(jsonColumn => {
    byColumn.set(jsonColumn.column, {
      column: jsonColumn.column,
      paths: jsonColumn.paths.map(path => ({ ...path, sample_values: [...path.sample_values] })),
    })
  })

  selectedJsonValuePaths.forEach(fullPath => {
    const parsed = splitFullJsonPath(fullPath)
    if (!parsed) return

    const jsonColumn = byColumn.get(parsed.column) ?? { column: parsed.column, paths: [] }
    if (!jsonColumn.paths.some(path => path.full_path === fullPath)) {
      jsonColumn.paths.push({ full_path: fullPath, path: parsed.path, sample_values: [] })
    }
    byColumn.set(parsed.column, jsonColumn)
  })

  return Array.from(byColumn.values()).map(jsonColumn => ({
    ...jsonColumn,
    paths: [...jsonColumn.paths].sort((a, b) => a.path.localeCompare(b.path)),
  }))
}

// Ordered finest → coarsest. A replay chunk must be >= the collection interval,
// so eligible chunk sizes are the interval itself and anything coarser.
export const INTERVAL_ORDER: IntervalCode[] = ['15m', '1h', '6h', '1d', '1w']
export const CHUNK_LABELS: Record<IntervalCode, string> = {
  '15m': '15 minutes',
  '1h': '1 hour',
  '6h': '6 hours',
  '1d': '1 day',
  '1w': '1 week',
}

export function eligibleChunkIntervals(interval: string): IntervalCode[] {
  const idx = INTERVAL_ORDER.indexOf(interval as IntervalCode)
  if (idx < 0) return []
  return INTERVAL_ORDER.slice(idx)
}

/**
 * A blank field (the backend default) or a whole number of at least 1, else null.
 *
 * Every numeric scan limit is `ge=1` on the backend. Truncating `0`, `-3` or
 * `2.5` into a number used to send it anyway and come back as a raw 422; an
 * invalid value now never reaches the wire, and {@link positiveIntError} is what
 * tells the user why the form will not save it (DATA-25).
 */
export function parseOptionalPositiveInt(value: string): number | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  const parsed = Number(trimmed)
  return Number.isInteger(parsed) && parsed >= 1 ? parsed : null
}

/** Why a numeric limit cannot be saved, or null when it can. */
export function positiveIntError(value: string, { required = false } = {}): string | null {
  const trimmed = value.trim()
  if (!trimmed) return required ? 'Enter a whole number of 1 or more.' : null
  if (parseOptionalPositiveInt(trimmed) !== null) return null
  return required
    ? 'Enter a whole number of 1 or more.'
    : 'Enter a whole number of 1 or more, or leave it empty for the default.'
}

// Activation traffic share is a fraction in (0, 1). Blank maps to null so the
// backend keeps its default (0.05); an out-of-range value maps to null too, but
// {@link shareError} blocks the save first, so it is never silently dropped.
export function parseOptionalShare(value: string): number | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  const parsed = Number(trimmed)
  if (!Number.isFinite(parsed) || parsed <= 0 || parsed >= 1) return null
  return parsed
}

/**
 * Why a traffic share cannot be saved, or null when it can. `5` meaning 5% used
 * to become null and save as the 0.05 default, so the value the user typed just
 * disappeared (DATA-25).
 */
export function shareError(value: string): string | null {
  if (!value.trim() || parseOptionalShare(value) !== null) return null
  return 'Enter a share between 0 and 1, e.g. 0.05 for 5%, or leave it empty for the default.'
}

export function isJsonPreviewType(typeName: string) {
  return typeName.toLowerCase().includes('json')
}

// One shared native-select class, focus ring included (DATA-48): this copy had
// none, so keyboard focus on the scan form's selects was invisible.
export { SELECT_CLASS } from '@/components/data-sources/connection-settings'
