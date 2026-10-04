import type { ScanJob, ScanJobResultSummary } from '@/types'
import type { ScanMode } from './scanMode'
import { countOf } from '@/lib/plural'
import { jobMetricPoints, jobScanned } from './scanUtils'

/**
 * Which population a run's "Rows read" figure counts.
 *
 * `scan_rows_processed` (what the catalog analyzer read, bounded by the scan row
 * cap) and `query_rows_scanned` (what metrics collection read across its chunks,
 * bounded by the metrics row cap) are two different numbers that have always
 * shared one label. A table header cannot vary per row, so the distinction is
 * carried in a per-cell title instead.
 */
type RunRowsKind = 'catalog' | 'catalogRows' | 'metrics'

const ROWS_READ_TITLE: Record<RunRowsKind, string> = {
  // The catalog analyzer groups in the warehouse (GROUP BY ALL), so what it
  // reads back are distinct column combinations, not warehouse rows (#247).
  catalog: 'Distinct column combinations the catalog analyzer read back this run, grouped in the warehouse (capped by the row cap).',
  catalogRows: 'Warehouse rows behind the column combinations the catalog analyzer read back this run (capped by the row cap).',
  metrics: 'Warehouse rows read across every metrics chunk (capped by the metrics row cap).',
}

/**
 * The counter `jobScanned` actually returned for this run, or null when the
 * run reported neither. The precedence MUST match `jobScanned`, or a cell
 * would be labelled as the population it is not.
 */
function jobRowsKind(job: ScanJob | null): RunRowsKind | null {
  const summary = job?.result_summary
  if (!summary) return null
  if (summary.query_rows_scanned != null) return 'metrics'
  if (summary.catalog_rows_scanned != null) return 'catalogRows'
  if (summary.scan_rows_processed != null) return 'catalog'
  return null
}

export function jobRowsReadTitle(job: ScanJob | null): string | undefined {
  const kind = jobRowsKind(job)
  return kind ? ROWS_READ_TITLE[kind] : undefined
}

/** A surface that holds the things a report line counts. */
export type RunReportTarget = 'anomalies' | 'alerts'

/** One plain sentence about what a run did to the user's data. */
export interface RunReportLine {
  /** Stable identity — React key, and the handle a test takes hold of. */
  id: string
  text: string
  /** Hover text, where one number covers two populations. */
  title?: string
  /** A second, quieter line under `text`. */
  hint?: string
  /** When set, the sentence links out to the surface holding what it counts. */
  target?: RunReportTarget
}

/**
 * `signals_added` is this RUN's delta — the scan's open signals after the run
 * minus before it. The Anomalies page the sentence links to counts what is OPEN
 * NOW for the scan. They are different questions with legitimately different
 * answers, and readers have taken the pair for a contradiction.
 *
 * The first attempt at saying so was a 20-word disclaimer under every run that
 * raised anything, asserting as fact that "the two numbers differ" — which is
 * false the moment a fresh signal is the scan's only open one, and a permanent
 * paragraph explaining a mechanism is not what a run report is for. So: state
 * the other number instead of explaining that another number exists, and only
 * when it is genuinely a different number.
 */
function openSignalsLine(openSignals: number): string {
  if (openSignals === 0) return 'None from this scan are open now.'
  return openSignals === 1
    ? '1 signal from this scan is open now.'
    : `${openSignals.toLocaleString()} signals from this scan are open now.`
}

/**
 * A scan with no schedule collects no metric points, so it can raise no signal
 * and send no alert. Said out loud, once, on the run that proves it: a green run
 * that produced nothing downstream otherwise reads as a silent failure.
 */
const CATALOG_ONLY_RUN_LINE =
  'Catalog-only scan — no metric points, so no signals and no alerts.'

/**
 * The same silence, for the opposite reason — and it must not borrow the line
 * above. A scan whose schedule is never dispatched produces no metric points
 * because it is BROKEN, not because its owner chose catalog-only, and calling
 * that a "catalog-only scan" on the run report tells a user who asked for
 * monitoring that they are looking at a mode they never picked.
 *
 * The run being read proves this line's own wording: a manual run is how it got
 * here, so the sentence blames the scheduler rather than claiming the scan never
 * runs.
 */
const NEVER_SCHEDULED_RUN_LINE =
  'This scan has a schedule but no time column, so the scheduler never runs it — no metric'
  + ' points, so no signals and no alerts. Add a time column to fix it.'

/**
 * Chunks this run has actually read, for the replay wording. `null` for an
 * ordinary run, and for a replay that has not finished a chunk yet — "across 0
 * chunks" is not a sentence worth printing.
 */
function chunksRead(summary: ScanJobResultSummary): number | null {
  if (!summary.replay_chunks_total) return null
  const read = summary.replay_chunks_completed ?? summary.replay_chunks_total
  return read > 0 ? read : null
}

/**
 * Turn a run's result summary into plain sentences about the user's data.
 *
 * Pure: no JSX, no React, no routing. `target` names a surface; the caller
 * resolves it to a route, so this module stays testable as data.
 *
 * The eight raw counters are NOT replaced by this — `JobDetails` keeps every one
 * of them, verbatim, behind a disclosure. This is what leads.
 */
export function buildRunReport(
  job: ScanJob | null,
  mode: ScanMode,
  /**
   * Signals from this scan that are open right now, or null when that is not
   * known yet. Only ever used to put the current number next to this run's
   * delta when the two disagree.
   */
  openSignals: number | null = null,
): RunReportLine[] {
  const summary = job?.result_summary
  if (!summary) return []

  const lines: RunReportLine[] = []

  const scanned = jobScanned(job)
  if (scanned) {
    const chunks = chunksRead(summary)
    // A catalog run's figure is grouped combinations, not warehouse rows: the
    // same data read "28,160 rows" in the dry run and "153 warehouse rows" in
    // the run (#247).
    const rowsText = scanned.unit === 'rows'
      ? scanned.combinations != null
        ? `${countOf(scanned.value, 'warehouse row', 'warehouse rows')} (${countOf(scanned.combinations, 'distinct column combination', 'distinct column combinations')}, grouped in the warehouse)`
        : countOf(scanned.value, 'warehouse row', 'warehouse rows')
      : `${countOf(scanned.value, 'distinct column combination', 'distinct column combinations')} (grouped in the warehouse)`
    lines.push({
      id: 'rows-read',
      text: chunks == null
        ? `Read ${rowsText}.`
        : `Read ${rowsText} across ${countOf(chunks, 'chunk', 'chunks')}.`,
      title: jobRowsReadTitle(job),
    })
  }

  const eventsCreated = summary.events_created ?? null
  const eventsSkipped = summary.events_skipped ?? 0
  if (eventsCreated != null && eventsCreated > 0) {
    lines.push({
      id: 'events-created',
      text: `Added ${countOf(eventsCreated, 'event', 'events')} to your tracking plan.`,
    })
  }
  // "Events skipped: 340" read as data loss. It is the opposite: those events
  // were recognised, so nothing was created for them. Both branches below give
  // the number its reason, and which branch fires depends on whether the run
  // discovered anything at all.
  if (eventsCreated === 0 && eventsSkipped > 0) {
    lines.push({
      id: 'events-none-new',
      text: `No new events — all ${eventsSkipped.toLocaleString()} were already in your plan.`,
    })
  }
  if (eventsSkipped > 0 && eventsCreated != null && eventsCreated > 0) {
    lines.push({
      id: 'events-skipped',
      text: eventsSkipped === 1
        ? '1 event was already in your plan and was left as it is.'
        : `${eventsSkipped.toLocaleString()} events were already in your plan and were left as they are.`,
    })
  }

  const variablesCreated = summary.variables_created ?? 0
  if (variablesCreated > 0) {
    lines.push({
      id: 'variables-created',
      text: `Added ${countOf(variablesCreated, 'property', 'properties')}.`,
    })
  }

  // "Columns analyzed: 17" read as a queue of work. It is coverage: how much of
  // the query the run looked at.
  const columnsAnalyzed = summary.columns_analyzed
  if (columnsAnalyzed != null) {
    lines.push({
      id: 'columns-analyzed',
      text: `Looked at ${countOf(columnsAnalyzed, 'column', 'columns')} in your query.`,
    })
  }

  const metricPoints = jobMetricPoints(job) ?? 0
  if (metricPoints > 0) {
    lines.push({
      id: 'metric-points',
      text: `Recorded ${countOf(metricPoints, 'metric point', 'metric points')}.`,
    })
  }

  const signalsAdded = summary.signals_added ?? 0
  if (signalsAdded > 0) {
    lines.push({
      id: 'signals-added',
      text: `Raised ${countOf(signalsAdded, 'anomaly signal', 'anomaly signals')}.`,
      hint:
        openSignals != null && openSignals !== signalsAdded
          ? openSignalsLine(openSignals)
          : undefined,
      target: 'anomalies',
    })
  }

  // Drops held back while the source was late or the scan overdue (F16, #269):
  // not lost, scored once data arrives — said, so a quiet run is not read as
  // an all-clear.
  const signalsHeld = summary.signals_held ?? 0
  if (signalsHeld > 0) {
    lines.push({
      id: 'signals-held',
      text: `Held ${countOf(signalsHeld, 'drop signal', 'drop signals')}: ${
        summary.freshness_status === 'overdue' ? 'the scan is overdue' : 'the source data is late'
      }.`,
      hint: 'They are scored normally once the data arrives.',
    })
  }

  const alertsQueued = summary.alerts_queued ?? 0
  if (alertsQueued > 0) {
    lines.push({
      id: 'alerts-queued',
      text: `Queued ${countOf(alertsQueued, 'alert', 'alerts')}.`,
      target: 'alerts',
    })
  }

  // Both non-monitoring modes end a run with no metric points, and both need
  // saying so — a green run that produced nothing downstream otherwise reads as
  // a silent failure. But they are not the same fact, and one line for both said
  // "Catalog-only scan" to a user who configured monitoring and got none. The
  // fault gets its own sentence, carrying the fix, matching the badge and the
  // Mode row.
  if (metricPoints === 0 && mode === 'catalog') {
    lines.push({ id: 'catalog-only', text: CATALOG_ONLY_RUN_LINE })
  }
  if (metricPoints === 0 && mode === 'misconfigured') {
    lines.push({ id: 'never-scheduled', text: NEVER_SCHEDULED_RUN_LINE })
  }

  return lines
}
