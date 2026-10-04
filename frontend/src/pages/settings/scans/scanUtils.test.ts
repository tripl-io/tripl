import { describe, expect, it } from 'vitest'
import type { ScanJob } from '@/types'
import { SCAN_STATUS_LABEL, formatCount } from './scanLayoutConstants'
import {
  LOADING_SCAN_RUN_INFO,
  consecutiveFailedRuns,
  deriveScanRunInfo,
  eligibleChunkIntervals,
  formatDueIn,
  formatJobScanned,
  jobDurationSeconds,
  jobMetricPoints,
  jobRowsScanned,
  jobScanned,
  metricsFreshness,
  parseOptionalPositiveInt,
  parseOptionalShare,
  positiveIntError,
  shareError,
  summarizeScanChanges,
} from './scanUtils'

function job(overrides: Partial<ScanJob>): ScanJob {
  return {
    id: 'job',
    scan_config_id: 'scan',
    status: 'completed',
    started_at: '2026-01-01T00:00:00Z',
    completed_at: '2026-01-01T00:00:10Z',
    result_summary: null,
    error_message: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:10Z',
    ...overrides,
  }
}

describe('summarizeScanChanges', () => {
  it('is empty for a job with no result summary', () => {
    expect(summarizeScanChanges(job({ result_summary: null }))).toEqual([])
    expect(summarizeScanChanges(null)).toEqual([])
  })

  it('surfaces only the non-zero deltas of a completed run', () => {
    const changes = summarizeScanChanges(
      job({
        result_summary: {
          events_created: 12,
          event_metrics: 3,
          breakdown_event_metrics: 2,
          signals_added: 1,
          alerts_queued: 0,
        },
      }),
    )
    const labels = changes.map((change) => change.label)
    expect(labels).toContain('+12 events')
    // Time-series rows collected — NOT metric definitions created.
    expect(labels).toContain('+5 metric points')
    expect(labels).toContain('+1 signal')
    // A zero delta is omitted, not shown as "+0".
    expect(labels.some((label) => label.includes('alert'))).toBe(false)
  })
})

describe('deriveScanRunInfo', () => {
  it('returns idle when there are no jobs', () => {
    expect(deriveScanRunInfo([])).toMatchObject({ status: 'idle', lastRunLabel: 'never' })
  })

  // "Never run" is a verdict. Coercing an unresolved job query to `[]` made
  // every row claim it had never run while the activity rail on the same screen
  // listed completed runs.
  it('reports unknown — not idle — while the job query is still loading', () => {
    expect(deriveScanRunInfo(undefined)).toEqual(LOADING_SCAN_RUN_INFO)
    expect(deriveScanRunInfo(undefined).status).toBe('unknown')
    expect(deriveScanRunInfo(undefined).lastRunLabel).not.toBe('never')
    expect(SCAN_STATUS_LABEL[deriveScanRunInfo(undefined).status]).not.toBe('Never run')
  })

  it('reports running for an in-flight latest job', () => {
    expect(deriveScanRunInfo([job({ status: 'running' })])).toMatchObject({
      status: 'running',
      lastRunLabel: 'running',
    })
  })

  it('reports failed when the latest job failed', () => {
    expect(deriveScanRunInfo([job({ status: 'failed' })]).status).toBe('failed')
  })

  it('reports ok for a completed latest job', () => {
    expect(deriveScanRunInfo([job({ status: 'completed' })]).status).toBe('ok')
  })
})

describe('jobRowsScanned', () => {
  it('prefers query_rows_scanned then scan_rows_processed', () => {
    expect(jobRowsScanned(job({ result_summary: { query_rows_scanned: 42 } }))).toBe(42)
    expect(jobRowsScanned(job({ result_summary: { scan_rows_processed: 7 } }))).toBe(7)
    expect(jobRowsScanned(job({ result_summary: {} }))).toBeNull()
    expect(jobRowsScanned(null)).toBeNull()
  })
})

describe('jobScanned (#247)', () => {
  it('names a catalog figure as combinations and a metrics figure as rows', () => {
    expect(jobScanned(job({ result_summary: { query_rows_scanned: 4428, scan_rows_processed: 9 } })))
      .toEqual({ value: 4428, unit: 'rows' })
    expect(jobScanned(job({ result_summary: { scan_rows_processed: 153 } })))
      .toEqual({ value: 153, unit: 'combinations' })
    expect(jobScanned(job({ result_summary: {} }))).toBeNull()
  })

  it('reads a catalog run in warehouse rows once the worker reports them', () => {
    expect(jobScanned(job({ result_summary: { catalog_rows_scanned: 28160, scan_rows_processed: 153 } })))
      .toEqual({ value: 28160, unit: 'rows', combinations: 153 })
    expect(jobRowsScanned(job({ result_summary: { catalog_rows_scanned: 28160, scan_rows_processed: 153 } })))
      .toBe(28160)
  })

  it('prints the unit after the figure, agreeing with the raw count', () => {
    expect(formatJobScanned({ value: 4428, unit: 'rows' })).toBe(`${(4428).toLocaleString()} rows`)
    expect(formatJobScanned({ value: 1, unit: 'combinations' })).toBe('1 combo')
    expect(formatJobScanned({ value: 1500, unit: 'combinations' }, formatCount)).toBe('1.5K combos')
    expect(formatJobScanned(null)).toBe('—')
  })
})

describe('metricsFreshness (#247)', () => {
  const now = Date.parse('2026-01-01T12:00:00Z')

  it('reads the newest metrics run, not the newest run', () => {
    const jobs = [
      job({ id: 'catalog', status: 'completed', completed_at: '2026-01-01T11:59:00Z', result_summary: { scan_rows_processed: 5 } }),
      job({ id: 'metrics', status: 'completed', completed_at: '2026-01-01T11:30:00Z', result_summary: { mode: 'metrics_collection' } }),
    ]
    const freshness = metricsFreshness(jobs, '1h', now)
    expect(freshness.job?.id).toBe('metrics')
    expect(freshness.lastAt).toBe('2026-01-01T11:30:00Z')
    expect(freshness.nextAt).toBe(Date.parse('2026-01-01T12:30:00Z'))
    expect(freshness.overdue).toBe(false)
  })

  it('flags a series with no point for more than two intervals', () => {
    const jobs = [
      job({ status: 'completed', completed_at: '2026-01-01T09:00:00Z', result_summary: { event_metrics: 3 } }),
    ]
    expect(metricsFreshness(jobs, '1h', now).overdue).toBe(true)
  })

  it('does not let a replay stand in for the scheduled collection', () => {
    const jobs = [
      job({ id: 'replay', status: 'completed', completed_at: '2026-01-01T11:55:00Z', result_summary: { mode: 'metrics_replay', event_metrics: 40 } }),
      job({ id: 'metrics', status: 'completed', completed_at: '2026-01-01T08:00:00Z', result_summary: { mode: 'metrics_collection', event_metrics: 3 } }),
    ]
    const freshness = metricsFreshness(jobs, '1h', now)
    expect(freshness.job?.id).toBe('metrics')
    expect(freshness.overdue).toBe(true)
  })

  it('takes the next run from the scheduler when the server sent it', () => {
    const jobs = [
      job({ status: 'completed', completed_at: '2026-01-01T11:30:00Z', result_summary: { mode: 'metrics_collection' } }),
    ]
    const freshness = metricsFreshness(jobs, '1h', now, {
      lastRunAt: '2026-01-01T11:30:00Z',
      nextRunAt: '2026-01-01T13:00:00Z',
    })
    // The next boundary, not "last run + interval" (12:30).
    expect(freshness.nextAt).toBe(Date.parse('2026-01-01T13:00:00Z'))
  })

  it('keeps the last run the server knows when catalog runs buried it in the job page', () => {
    const jobs = [
      job({ status: 'completed', completed_at: '2026-01-01T11:59:00Z', result_summary: { scan_rows_processed: 5 } }),
    ]
    const freshness = metricsFreshness(jobs, '1h', now, {
      lastRunAt: '2026-01-01T11:00:00Z',
      nextRunAt: '2026-01-01T13:00:00Z',
    })
    expect(freshness.job).toBeNull()
    expect(freshness.lastAt).toBe('2026-01-01T11:00:00Z')
    expect(freshness.overdue).toBe(false)
  })

  it('has nothing to say before the first metrics run', () => {
    expect(metricsFreshness([], '1h', now)).toEqual({ job: null, lastAt: null, nextAt: null, overdue: false })
  })

  it('says when the next run is due', () => {
    expect(formatDueIn(now + 48 * 60_000, now)).toBe('in 48m')
    expect(formatDueIn(now + 3 * 3_600_000, now)).toBe('in 3h')
    expect(formatDueIn(now - 1000, now)).toBe('due now')
  })
})

describe('jobMetricPoints', () => {
  // The detail stat card used to read `breakdown_event_metrics ?? event_metrics`
  // while the list chip summed all four counters, so the same run reported two
  // different metric-point totals on two screens. There is now one formula.
  it('sums all four metric counters — they are disjoint populations', () => {
    const run = job({
      result_summary: {
        event_metrics: 2,
        type_metrics: 3,
        breakdown_event_metrics: 5,
        breakdown_type_metrics: 7,
      },
    })
    // The old `breakdown_event_metrics ?? event_metrics` fallback returns 5.
    expect(jobMetricPoints(run)).toBe(17)
  })

  it('counts the counters that are present and treats the absent ones as zero', () => {
    expect(jobMetricPoints(job({ result_summary: { event_metrics: 4 } }))).toBe(4)
  })

  it('is null when the run reported no metric counters at all', () => {
    expect(jobMetricPoints(job({ result_summary: { events_created: 3 } }))).toBeNull()
    expect(jobMetricPoints(job({ result_summary: null }))).toBeNull()
    expect(jobMetricPoints(null)).toBeNull()
  })

  it('is the same number the list chip renders', () => {
    const run = job({
      result_summary: {
        event_metrics: 2,
        type_metrics: 3,
        breakdown_event_metrics: 5,
        breakdown_type_metrics: 7,
      },
    })
    const chip = summarizeScanChanges(run).find((change) => change.label.includes('metric point'))
    expect(chip?.label).toBe(`+${jobMetricPoints(run)} metric points`)
  })
})

describe('jobDurationSeconds', () => {
  it('computes seconds between start and completion', () => {
    expect(jobDurationSeconds(job({}))).toBe(10)
  })

  it('returns null when not finished', () => {
    expect(jobDurationSeconds(job({ completed_at: null }))).toBeNull()
  })
})

describe('consecutiveFailedRuns', () => {
  it('returns 0 when there are no jobs', () => {
    expect(consecutiveFailedRuns([])).toBe(0)
  })

  it('counts leading failed runs (newest-first) and stops at the first success', () => {
    expect(
      consecutiveFailedRuns([
        job({ status: 'failed' }),
        job({ status: 'failed' }),
        job({ status: 'failed' }),
        job({ status: 'completed' }),
        job({ status: 'failed' }),
      ]),
    ).toBe(3)
  })

  it('skips an in-flight retry at the head so the streak is not reset', () => {
    expect(
      consecutiveFailedRuns([
        job({ status: 'running' }),
        job({ status: 'failed' }),
        job({ status: 'failed' }),
      ]),
    ).toBe(2)
  })

  it('returns 0 when the latest settled run succeeded', () => {
    expect(consecutiveFailedRuns([job({ status: 'completed' }), job({ status: 'failed' })])).toBe(0)
  })

  it('stops the streak at a cancelled run', () => {
    expect(
      consecutiveFailedRuns([
        job({ status: 'failed' }),
        job({ status: 'cancelled' }),
        job({ status: 'failed' }),
      ]),
    ).toBe(1)
  })
})

describe('eligibleChunkIntervals', () => {
  it('returns the interval and coarser sizes', () => {
    expect(eligibleChunkIntervals('1h')).toEqual(['1h', '6h', '1d', '1w'])
    expect(eligibleChunkIntervals('')).toEqual([])
  })
})

describe('formatCount', () => {
  it('moves up a unit when rounding reaches 1000 of the smaller one', () => {
    expect(formatCount(999_949)).toBe('999.9K')
    expect(formatCount(999_950)).toBe('1M')
    expect(formatCount(999_999)).toBe('1M')
    expect(formatCount(999_995_000)).toBe('1B')
  })

  it('keeps the ordinary cases', () => {
    expect(formatCount(null)).toBe('—')
    expect(formatCount(999)).toBe('999')
    expect(formatCount(1_000)).toBe('1K')
    expect(formatCount(100_000)).toBe('100K')
    expect(formatCount(1_800_000)).toBe('1.8M')
    expect(formatCount(2_500_000_000)).toBe('2.5B')
  })
})

describe('numeric limit parsing', () => {
  it('never turns 0, a negative or a fraction into a limit the backend refuses', () => {
    expect(parseOptionalPositiveInt('0')).toBeNull()
    expect(parseOptionalPositiveInt('-3')).toBeNull()
    expect(parseOptionalPositiveInt('2.5')).toBeNull()
    expect(parseOptionalPositiveInt(' 24 ')).toBe(24)
    expect(parseOptionalPositiveInt('')).toBeNull()
  })

  it('explains the refusal instead', () => {
    expect(positiveIntError('0')).toMatch(/whole number of 1 or more/)
    expect(positiveIntError('')).toBeNull()
    expect(positiveIntError('', { required: true })).toMatch(/whole number of 1 or more/)
    expect(positiveIntError('12')).toBeNull()
  })

  it('refuses a share outside (0, 1) rather than dropping it', () => {
    expect(parseOptionalShare('0.05')).toBe(0.05)
    expect(shareError('0.05')).toBeNull()
    expect(shareError('5')).toMatch(/between 0 and 1/)
    expect(shareError('')).toBeNull()
  })
})
