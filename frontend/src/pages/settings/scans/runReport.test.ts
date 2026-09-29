import { describe, expect, it } from 'vitest'
import type { ScanJob, ScanJobResultSummary } from '@/types'
import { buildRunReport, jobRowsReadTitle, type RunReportLine } from './runReport'

function job(summary: ScanJobResultSummary | null): ScanJob {
  return {
    id: 'job-1',
    scan_config_id: 'scan-1',
    status: 'completed',
    started_at: '2026-01-01T00:00:00Z',
    completed_at: '2026-01-01T00:00:10Z',
    result_summary: summary,
    error_message: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:10Z',
  }
}

function lineById(lines: RunReportLine[], id: string): RunReportLine | undefined {
  return lines.find(line => line.id === id)
}

describe('buildRunReport — "Rows read" covers two populations', () => {
  it('describes a catalog run and a metrics run of the same size differently', () => {
    // Same figure, two meanings: `scan_rows_processed` is what the catalog
    // analyzer read under the scan row cap, `query_rows_scanned` is what metrics
    // collection read across its chunks under the metrics row cap. One label has
    // always covered both.
    const catalogRows = lineById(buildRunReport(job({ scan_rows_processed: 900 }), 'catalog'), 'rows-read')!
    const metricsRows = lineById(buildRunReport(job({ query_rows_scanned: 900 }), 'monitoring'), 'rows-read')!

    // The catalog analyzer groups in the warehouse, so its figure is distinct
    // column combinations, not warehouse rows (#247 DA-4).
    expect(catalogRows.text).toBe('Read 900 distinct column combinations (grouped in the warehouse).')
    expect(metricsRows.text).toBe('Read 900 warehouse rows.')

    // The titles say which cap bounded each. If these ever collapse to one
    // string — or to two undefineds — the user is back to one number that
    // silently means two things.
    expect(catalogRows.title).not.toEqual(metricsRows.title)
    expect(catalogRows.title).toContain('catalog analyzer')
    expect(catalogRows.title).toContain('capped by the row cap')
    expect(metricsRows.title).toContain('every metrics chunk')
    expect(metricsRows.title).toContain('capped by the metrics row cap')
  })

  it('reads a catalog run in warehouse rows, with its combinations beside them (i9mt.16)', () => {
    const summary = { catalog_rows_scanned: 28160, scan_rows_processed: 153 }
    const line = lineById(buildRunReport(job(summary), 'catalog'), 'rows-read')!
    expect(line.text).toBe(
      'Read 28,160 warehouse rows (153 distinct column combinations, grouped in the warehouse).',
    )
    expect(jobRowsReadTitle(job(summary))).toContain('Warehouse rows behind the column combinations')
  })

  it('labels the kind by the counter jobRowsScanned actually returns', () => {
    // jobRowsScanned prefers query_rows_scanned; the title must agree with it or
    // a metrics run would be described under the catalog cap. Asserted through
    // the title the user actually reads — the classifier behind it is module
    // business.
    expect(jobRowsReadTitle(job({ query_rows_scanned: 5, scan_rows_processed: 900 })))
      .toContain('every metrics chunk')
    expect(jobRowsReadTitle(job({ scan_rows_processed: 900 }))).toContain('catalog analyzer')
    expect(jobRowsReadTitle(job({}))).toBeUndefined()
    expect(jobRowsReadTitle(job(null))).toBeUndefined()
  })

  it('names the chunks a replay read', () => {
    const lines = buildRunReport(
      job({
        mode: 'metrics_replay',
        query_rows_scanned: 900,
        replay_chunks_total: 4,
        replay_chunks_completed: 4,
      }),
      'monitoring',
    )
    expect(lineById(lines, 'rows-read')!.text).toBe('Read 900 warehouse rows across 4 chunks.')
  })
})

describe('buildRunReport — a catalog-only run', () => {
  it('says why it recorded nothing instead of leaving the user to infer it', () => {
    const lines = buildRunReport(job({ scan_rows_processed: 900, events_created: 3 }), 'catalog')

    expect(lines.map(line => line.text)).toContain(
      'Catalog-only scan — no metric points, so no signals and no alerts.',
    )
    // ...and it never claims points it cannot have collected.
    expect(lineById(lines, 'metric-points')).toBeUndefined()
    expect(lines.some(line => line.text.startsWith('Recorded'))).toBe(false)
  })

  // A schedule without a time column also collects nothing, and that silence
  // also has to be said out loud — but NOT in the catalog-only sentence. The
  // user configured monitoring; telling them they are looking at a "Catalog-only
  // scan" names a mode they never picked and reads as working-as-configured.
  it('does not call a never-dispatched scan a catalog-only scan', () => {
    const lines = buildRunReport(job({ scan_rows_processed: 900 }), 'misconfigured')
    const texts = lines.map(line => line.text)

    expect(texts).not.toContain(
      'Catalog-only scan — no metric points, so no signals and no alerts.',
    )
    expect(lineById(lines, 'catalog-only')).toBeUndefined()
  })

  it('says the schedule is the thing that never runs, and how to fix it', () => {
    const lines = buildRunReport(job({ scan_rows_processed: 900 }), 'misconfigured')
    const line = lineById(lines, 'never-scheduled')!

    // The run being read is proof the scan itself can run: it is in the history
    // because someone pressed Run now, which trigger_scan does not guard.
    expect(line.text).toContain('the scheduler never runs it')
    expect(line.text).toContain('no signals and no alerts')
    expect(line.text).toContain('Add a time column to fix it.')
  })

  it('never appears on a monitoring run that recorded points', () => {
    const lines = buildRunReport(
      job({ query_rows_scanned: 900, event_metrics: 2, type_metrics: 3, breakdown_event_metrics: 5, breakdown_type_metrics: 7 }),
      'monitoring',
    )
    expect(lineById(lines, 'metric-points')!.text).toBe('Recorded 17 metric points.')
    expect(lineById(lines, 'catalog-only')).toBeUndefined()
  })
})

describe('buildRunReport — the counters that read as bad news', () => {
  it('gives "skipped" a reason instead of a bare count', () => {
    // "Events skipped: 340" read as 340 lost events. It is the opposite.
    const nothingNew = buildRunReport(job({ events_created: 0, events_skipped: 340 }), 'monitoring')
    expect(lineById(nothingNew, 'events-none-new')!.text).toBe(
      'No new events — all 340 were already in your plan.',
    )

    const someNew = buildRunReport(job({ events_created: 2, events_skipped: 340 }), 'monitoring')
    expect(lineById(someNew, 'events-created')!.text).toBe('Added 2 events to your tracking plan.')
    expect(lineById(someNew, 'events-skipped')!.text).toBe(
      '340 events were already in your plan and were left as they are.',
    )
    // The two skipped sentences are alternatives, never both.
    expect(lineById(someNew, 'events-none-new')).toBeUndefined()
  })

  it('reads "columns analyzed" as coverage, not as a to-do', () => {
    const lines = buildRunReport(job({ columns_analyzed: 17 }), 'monitoring')
    expect(lineById(lines, 'columns-analyzed')!.text).toBe('Looked at 17 columns in your query.')
  })

  it('keeps its plurals honest for a run of one', () => {
    const lines = buildRunReport(
      job({ scan_rows_processed: 1, events_created: 1, events_skipped: 1, variables_created: 1, columns_analyzed: 1 }),
      'catalog',
    )
    expect(lines.map(line => line.text)).toEqual([
      'Read 1 distinct column combination (grouped in the warehouse).',
      'Added 1 event to your tracking plan.',
      '1 event was already in your plan and was left as it is.',
      'Added 1 property.',
      'Looked at 1 column in your query.',
      'Catalog-only scan — no metric points, so no signals and no alerts.',
    ])
  })
})

describe('buildRunReport — what this run raised vs what is open now', () => {
  it('links the run delta out, and stays quiet when there is nothing to reconcile', () => {
    // This run's delta and the scan's open count are different questions, and a
    // run whose signal is the scan's only open one has the same answer to both.
    // Nothing to reconcile, so nothing is said.
    const lines = buildRunReport(job({ signals_added: 1, alerts_queued: 2 }), 'monitoring', 1)

    const signals = lineById(lines, 'signals-added')!
    expect(signals.text).toBe('Raised 1 anomaly signal.')
    expect(signals.target).toBe('anomalies')
    expect(signals.hint).toBeUndefined()

    const alerts = lineById(lines, 'alerts-queued')!
    expect(alerts.text).toBe('Queued 2 alerts.')
    expect(alerts.target).toBe('alerts')
  })

  it('names the open count only where it differs from the run delta', () => {
    // The disclaimer this replaced was attached to EVERY run that raised
    // anything and asserted as fact that "the two numbers differ". Naming the
    // other number is shorter, true, and only printed when there IS another
    // number to name.
    const grew = buildRunReport(job({ signals_added: 2 }), 'monitoring', 5)
    expect(lineById(grew, 'signals-added')!.hint).toBe('5 signals from this scan are open now.')

    const one = buildRunReport(job({ signals_added: 2 }), 'monitoring', 1)
    expect(lineById(one, 'signals-added')!.hint).toBe('1 signal from this scan is open now.')

    // Everything this run raised has closed since — what a user reading an old
    // run most often hits, and the case the link lands on empty.
    const closed = buildRunReport(job({ signals_added: 2 }), 'monitoring', 0)
    expect(lineById(closed, 'signals-added')!.hint).toBe('None from this scan are open now.')

    // Not known yet (the query has not resolved): say nothing rather than guess.
    const unknown = buildRunReport(job({ signals_added: 2 }), 'monitoring')
    expect(lineById(unknown, 'signals-added')!.hint).toBeUndefined()
  })

  it('omits both when the run raised nothing — a zero is not a sentence', () => {
    const lines = buildRunReport(job({ signals_added: 0, alerts_queued: 0 }), 'monitoring')
    expect(lineById(lines, 'signals-added')).toBeUndefined()
    expect(lineById(lines, 'alerts-queued')).toBeUndefined()
  })
})

describe('buildRunReport — nothing to report', () => {
  it('returns no lines for a run without a result summary', () => {
    expect(buildRunReport(job(null), 'monitoring')).toEqual([])
    expect(buildRunReport(null, 'monitoring')).toEqual([])
  })
})

describe('buildRunReport — drop signals held for a late source (F16, #269)', () => {
  it('says how many drops were held and why', () => {
    const late = buildRunReport(job({ signals_held: 3, freshness_status: 'late' }), 'monitoring')
    expect(lineById(late, 'signals-held')!.text).toBe('Held 3 drop signals: the source data is late.')

    const overdue = buildRunReport(job({ signals_held: 1, freshness_status: 'overdue' }), 'monitoring')
    expect(lineById(overdue, 'signals-held')!.text).toBe('Held 1 drop signal: the scan is overdue.')
  })

  it('says nothing when nothing was held', () => {
    const lines = buildRunReport(job({ signals_held: 0, freshness_status: 'fresh' }), 'monitoring')
    expect(lineById(lines, 'signals-held')).toBeUndefined()
  })
})
