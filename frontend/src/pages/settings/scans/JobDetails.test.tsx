import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { MonitoringSignal, ScanJob, ScanJobResultSummary } from '@/types'
import { AuthContext } from '@/components/auth-context'
import { JobDetails } from './JobDetails'
import type { ScanMode } from './scanMode'
import { personaAuth, type Persona } from '@/test/persona'
import { PersonaProject } from '@/test/PersonaProject'

/** Every counter the panel used to BE, in the order it renders them. */
const RAW_COUNTERS = [
  'Events created',
  'Properties created',
  'Events skipped',
  'Columns analyzed',
  'Event breakdowns',
  'Distribution rows',
  'Signals added',
  'Alerts queued',
]

/** A run that populates all eight, including the four optional ones. */
const FULL_SUMMARY: ScanJobResultSummary = {
  query_rows_scanned: 900,
  events_created: 12,
  events_skipped: 340,
  variables_created: 3,
  columns_analyzed: 17,
  breakdown_event_metrics: 5131,
  distribution_drifts: 4,
  signals_added: 1,
  alerts_queued: 1,
}

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

/** One open signal belonging to `scanConfigId`; only that field is read. */
function signal(scanConfigId: string | null): MonitoringSignal {
  return {
    scan_config_id: scanConfigId,
    scope_type: 'event',
    scope_ref: 'event-1',
    state: 'latest_scan',
    event_id: 'event-1',
    event_type_id: null,
    bucket: '2026-01-01T00:00:00Z',
    actual_count: 10,
    expected_count: 100,
    stddev: 5,
    z_score: -18,
    direction: 'drop',
    incident_child: false,
    muted: false,
    expected: false,
    hidden: false,
    attribution_status: 'not_computed',
    unit: null,
    detected_at: null,
  }
}

/**
 * The panel reads the project's open signals so the report can put the scan's
 * current count beside this run's delta. The cache is seeded rather than the
 * request mocked, so "the answer has arrived" is a fact about the first render
 * instead of a race an absence assertion could win by accident. `openNow` null
 * seeds nothing — the state a freshly-expanded run is in.
 */
function renderDetails(
  summary: ScanJobResultSummary | null,
  mode: ScanMode = 'monitoring',
  openNow: number | null = null,
  hiddenNow = 0,
) {
  // The signals request hangs rather than answering, so an unseeded render is
  // pinned to "the answer has not arrived". Anything else is a bug in the test.
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    const url = String(input)
    if (url.includes('/anomalies/signals')) return new Promise<Response>(() => {})
    throw new Error(`Unhandled fetch: ${url}`)
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (openNow != null) {
    queryClient.setQueryData(['activeSignals', 'demo', 'expanded'], [
      ...Array.from({ length: openNow }, () => signal('scan-1')),
      // Another scan's open signal must not be counted into this scan's total.
      signal('scan-2'),
      // Nor may this scan's muted or expected ones.
      ...Array.from({ length: hiddenNow }, () => ({ ...signal('scan-1'), hidden: true })),
    ])
  }
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <JobDetails job={job(summary)} slug="demo" scanConfigId="scan-1" mode={mode} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('JobDetails', () => {
  it('leads with what the run did and demotes the eight raw counters behind a disclosure', () => {
    renderDetails(FULL_SUMMARY)

    // The sentences are there without clicking anything: this is the answer to
    // "did my events arrive, and which ones".
    expect(screen.getByText('What this run did')).toBeInTheDocument()
    expect(screen.getByText('Added 12 events to your tracking plan.')).toBeInTheDocument()
    expect(
      screen.getByText('340 events were already in your plan and were left as they are.'),
    ).toBeInTheDocument()
    expect(screen.getByText('Recorded 5,131 metric points.')).toBeInTheDocument()

    // ...and not one of the eight internal counters is in the DOM yet.
    RAW_COUNTERS.forEach(label => {
      expect(
        screen.queryByText(label),
        `raw counter "${label}" is in the DOM before "Show raw counters" was clicked`,
      ).toBeNull()
    })

    fireEvent.click(screen.getByRole('button', { name: 'Show raw counters' }))

    // Nothing was lost — every counter is still reachable, under its own label.
    RAW_COUNTERS.forEach(label => {
      expect(
        screen.queryByText(label),
        `raw counter "${label}" went missing when the disclosure was opened`,
      ).toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: 'Hide raw counters' })).toBeInTheDocument()
    // The report does not go away when the counters come out.
    expect(screen.getByText('Added 12 events to your tracking plan.')).toBeInTheDocument()
  })

  it('links what the run raised out to what it raised it into', () => {
    renderDetails(FULL_SUMMARY)

    const signals = screen.getByText('Raised 1 anomaly signal.')
    expect(signals).toHaveAttribute('href', '/p/demo/anomalies?scan=scan-1')
    expect(signals).toHaveAttribute('title', 'View anomalies from this scan')

    const alerts = screen.getByText('Queued 1 alert.')
    expect(alerts).toHaveAttribute('href', '/p/demo/alerting?scan=scan-1')
  })

  it('names the scan\'s open count beside the run delta when they differ', () => {
    // The run says it raised 1; the page that link lands on shows 4 open for the
    // scan. Both are right, and the screen used to reconcile them with a
    // permanent 20-word paragraph that also claimed they always disagree.
    renderDetails(FULL_SUMMARY, 'monitoring', 4)

    expect(screen.getByText('Raised 1 anomaly signal.')).toBeInTheDocument()
    expect(screen.getByText('4 signals from this scan are open now.')).toBeInTheDocument()
  })

  it('leaves muted and expected signals out of the scan\'s open count', () => {
    // Four listed for the scan, three of them hidden: one is open, which is
    // what the run raised, so there is nothing to put beside it.
    renderDetails(FULL_SUMMARY, 'monitoring', 1, 3)

    expect(screen.getByText('Raised 1 anomaly signal.')).toBeInTheDocument()
    expect(screen.queryByText(/open now/)).toBeNull()
  })

  it('says nothing about open signals when the run delta is the whole story', () => {
    // The answer is in the cache before the first render, so this absence is
    // about the match and not about a request that had not landed yet.
    renderDetails(FULL_SUMMARY, 'monitoring', 1)

    expect(screen.getByText('Raised 1 anomaly signal.')).toBeInTheDocument()
    expect(screen.queryByText(/open now/)).toBeNull()
  })

  it('tells a catalog-only run why it produced no signals and no alerts', () => {
    renderDetails({ scan_rows_processed: 900, events_created: 12 }, 'catalog')

    expect(
      screen.getByText('Catalog-only scan — no metric points, so no signals and no alerts.'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/^Recorded /)).toBeNull()
  })

  it('still shows the failure and the log for a run that has neither', () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <JobDetails
            job={{ ...job(null), status: 'failed', error_message: 'Read timed out.' }}
            slug="demo"
            scanConfigId="scan-1"
            mode="monitoring"
          />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect(screen.getByText('Run details')).toBeInTheDocument()
    // No summary means no report and no disclosure — not an empty shell of both.
    expect(screen.queryByText('What this run did')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Show raw counters' })).toBeNull()
  })
})

describe('JobDetails — the raw error behind "Scan failed."', () => {
  const RAW = 'HTTPSConnectionPool(host=ch.internal, port=8443): Read timed out'

  function renderFailed(role: Persona) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const failed = { ...job(null), status: 'failed' as const, error_message: RAW }
    return render(
      <AuthContext.Provider value={personaAuth(role)}>
        <PersonaProject persona={role}>
          <QueryClientProvider client={queryClient}>
            <MemoryRouter>
              <JobDetails job={failed} slug="demo" scanConfigId="scan-1" mode="monitoring" dataSourceId="ds-1" />
            </MemoryRouter>
          </QueryClientProvider>
        </PersonaProject>
      </AuthContext.Provider>,
    )
  }

  it('lets an owner open the technical details the safe message replaced', () => {
    renderFailed('owner')

    expect(screen.getByText('Scan failed: the data source did not respond in time.')).toBeInTheDocument()
    fireEvent.click(screen.getByText('View technical details'))
    expect(screen.getByText(RAW)).toBeVisible()
  })

  it('never shows the raw text to anyone else', () => {
    renderFailed('member')

    expect(screen.getByText('Scan failed: the data source did not respond in time.')).toBeInTheDocument()
    expect(screen.queryByText('View technical details')).not.toBeInTheDocument()
    expect(screen.queryByText(RAW)).not.toBeInTheDocument()
  })

  it('links an owner to the Limits section and to the connection', () => {
    renderFailed('owner')

    expect(screen.getByRole('link', { name: 'Open Limits' })).toHaveAttribute(
      'href',
      '/?tab=configuration#scan-limits',
    )
    expect(screen.getByRole('link', { name: 'Open the connection' })).toHaveAttribute(
      'href',
      '/settings/data-sources/ds-1',
    )
  })

  it('offers no connection link to a role the data-source page would bounce', () => {
    // Data-source pages are owner-only; an editor following the link landed
    // back on the list with nothing to test.
    renderFailed('member')

    expect(screen.getByRole('link', { name: 'Open Limits' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Open the connection' })).not.toBeInTheDocument()
  })
})
