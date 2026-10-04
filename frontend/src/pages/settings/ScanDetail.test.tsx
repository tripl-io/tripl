import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { DemoScenarioProvider } from '@/demo/DemoScenarioProvider'
import {
  buildChapterSteps,
  initialScenarioState,
  readScenarioState,
  writeScenarioState,
} from '@/demo/scenarioModel'
import { liveLoopState } from '@/demo/scenarioTestState'
import type { Project, ScanConfig } from '@/types'
import { ScanDetail } from './ScanDetail'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { at } from '@/test/at'
import { authAs } from '@/test/auth'

function mockJsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const scanConfig: ScanConfig = {
  id: 'scan-1',
  data_source_id: 'ds-1',
  project_id: 'project-1',
  event_type_id: null,
  name: 'Main scan',
  base_query: 'SELECT * FROM analytics.events',
  // A scan names its events one of two ways; this one uses the column.
  event_type_column: 'event_name',
  time_column: 'created_at',
  event_name_format: null,
  json_value_paths: [],
  event_group_rules: [],
  metric_breakdown_columns: [],
  metric_breakdown_values_limit: null,
  distribution_drift_fields: [],
  cardinality_threshold: 100,
  interval: '1h',
  replay_chunk_interval: '1h',
  scan_lookback_hours: null,
  scan_row_limit: null,
  metrics_row_limit: null,
  app_version_column: null,
  app_version_keep_releases: null,
  app_version_prerelease_pattern: null,
  app_version_active_share_min: null,
  platform_column: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

/** GET /scans/activity with this scan's server-side streak. */
function activityFor(failingStreak = 0) {
  return {
    window_from: '2026-01-01T00:00:00Z',
    window_to: '2026-01-02T00:00:00Z',
    items: [{ scan_config_id: 'scan-1', latest_job: null, failing_streak: failingStreak, rows_read_24h: 0 }],
  }
}

afterEach(() => {
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe('ScanDetail', () => {
  it('offers Apply to existing events only to owners, on the group rules row (#247)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/scans/activity')) return mockJsonResponse(activityFor())
      if (url.endsWith('/platform-presence')) {
        return mockJsonResponse({ scan_config_id: 'scan-1', platform_column: null, platforms: [], items: [] })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1/jobs')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const withRules: ScanConfig = {
      ...scanConfig,
      event_group_rules: [
        { name: 'Home', condition_logic: 'all', conditions: [{ field: 'event_name', pattern: '^Home' }] },
      ],
    }
    const auth = (role: 'member' | 'owner'): AuthContextValue => ({
      user: {
        id: 'u-1', email: 'operator@example.com', name: 'Operator', role,
        is_platform_admin: false, orgs: [],
        created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
      },
      status: 'authenticated', error: null, isLoggingOut: false,
      logout: async () => {}, refresh: () => {},
    })
    const detail = (role: 'member' | 'owner') => (
      <AuthContext.Provider value={auth(role)}>
        <QueryClientProvider client={queryClient}>
          <ScanDetail slug="demo" scanConfig={withRules} eventTypes={[]} />
        </QueryClientProvider>
      </AuthContext.Provider>
    )

    const view = render(detail('member'))
    expect(await screen.findByText('Recent runs')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Apply to existing events/ })).not.toBeInTheDocument()

    view.rerender(detail('owner'))
    expect(screen.getByRole('button', { name: /Apply to existing events/ })).toBeInTheDocument()
    expect(screen.getByText('1 rule')).toBeInTheDocument()
  })

  it('offers no Apply action on a scan with no group rules (#247)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/scans/activity')) return mockJsonResponse(activityFor())
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1/jobs')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <AuthContext.Provider value={{
        user: {
          id: 'u-1', email: 'owner@example.com', name: 'Owner', role: 'owner',
          is_platform_admin: false, orgs: [],
          created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
        },
        status: 'authenticated', error: null, isLoggingOut: false,
        logout: async () => {}, refresh: () => {},
      }}>
        <QueryClientProvider client={queryClient}>
          <ScanDetail slug="demo" scanConfig={scanConfig} eventTypes={[]} />
        </QueryClientProvider>
      </AuthContext.Provider>,
    )
    expect(await screen.findByText('Recent runs')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Apply/ })).not.toBeInTheDocument()
  })

  it('shows replay chunk progress for running jobs', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/scans/activity')) return mockJsonResponse(activityFor())
      if (url.endsWith('/platform-presence')) {
        return mockJsonResponse({ scan_config_id: 'scan-1', platform_column: null, platforms: [], items: [] })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1/jobs')) {
        return mockJsonResponse([
          {
            id: 'job-1',
            scan_config_id: 'scan-1',
            status: 'running',
            started_at: '2026-01-01T00:00:00Z',
            completed_at: null,
            result_summary: {
              mode: 'metrics_replay',
              catalog_sync_skipped: true,
              time_from: '2026-01-01T00:00:00Z',
              time_to: '2026-01-01T04:00:00Z',
              replay_chunk_interval: '1h',
              replay_chunks_total: 4,
              replay_chunks_completed: 2,
              replay_current_chunk_index: 3,
              replay_current_chunk_from: '2026-01-01T02:00:00Z',
              replay_current_chunk_to: '2026-01-01T03:00:00Z',
              replay_progress_percent: 50,
              replay_progress_phase: 'collecting',
            },
            error_message: null,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:20:00Z',
          },
        ])
      }

      throw new Error(`Unhandled fetch: ${url}`)
    })

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    render(
      <QueryClientProvider client={queryClient}>
        <ScanDetail slug="demo" scanConfig={scanConfig} eventTypes={[]} />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('2/4 chunks')).toBeInTheDocument()
    expect(screen.getByText('processing 3/4')).toBeInTheDocument()
    expect(screen.getAllByRole('progressbar', { name: 'Replay chunks' })[0]).toHaveAttribute(
      'aria-valuenow',
      '50',
    )
  })

  it('renders the overview panels with real ScanConfig fields', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/scans/activity')) return mockJsonResponse(activityFor())
      if (url.endsWith('/platform-presence')) {
        return mockJsonResponse({ scan_config_id: 'scan-1', platform_column: null, platforms: [], items: [] })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1/jobs')) {
        return mockJsonResponse([])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    render(
      <QueryClientProvider client={queryClient}>
        <ScanDetail slug="demo" scanConfig={scanConfig} eventTypes={[]} />
      </QueryClientProvider>,
    )

    // Overview stat cards + panels use the real fields.
    expect(await screen.findByText('Source & query')).toBeInTheDocument()
    expect(screen.getByText('Event mapping')).toBeInTheDocument()
    expect(screen.getByText('Metrics & drift')).toBeInTheDocument()
    expect(screen.getByText('Last run')).toBeInTheDocument()
    expect(screen.getByText('SELECT * FROM analytics.events')).toBeInTheDocument()
    // The mocked config has time_column set and no event type, so the name
    // comes from the event type column instead — which is what the row says.
    expect(screen.getByText('created_at')).toBeInTheDocument()
    expect(screen.getByText('Named from a column')).toBeInTheDocument()
    expect(await screen.findByText(/No runs yet/)).toBeInTheDocument()
  })

  it('labels a failed job, offers a retry, and never shows raw scan internals', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/scans/activity')) return mockJsonResponse(activityFor())
      if (url.endsWith('/platform-presence')) {
        return mockJsonResponse({ scan_config_id: 'scan-1', platform_column: null, platforms: [], items: [] })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1/jobs')) {
        return mockJsonResponse([
          {
            id: 'job-fail',
            scan_config_id: 'scan-1',
            status: 'failed',
            // 381.8s elapsed — a silent timeout under the old red-dot UI.
            started_at: '2026-01-01T00:00:00Z',
            completed_at: '2026-01-01T00:06:21Z',
            result_summary: null,
            error_message:
              "HTTPSConnectionPool(host='clickhouse.internal', port=8443): Read timed out. (read timeout=30)",
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:06:21Z',
          },
        ])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    const report = (role: 'owner' | 'member') => (
      <QueryClientProvider client={queryClient}>
        {/* The failure box links to its fixes, so the report needs a router.
            Data-source pages are owner-only, so an owner is signed in. */}
        <AuthContext.Provider value={authAs(role)}>
          <MemoryRouter>
            <ScanDetail slug="demo" scanConfig={scanConfig} eventTypes={[]} />
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>
    )
    const view = render(report('owner'))
    /** The raw error is the owner's, and only behind "View technical details",
     * folded: never in the row, the summary or the next step. */
    const expectRawErrorOnlyInClosedTechnicalDetails = () => {
      expect(screen.getAllByText('View technical details').length).toBeGreaterThan(0)
      for (const pattern of [/clickhouse\.internal/, /8443/]) {
        for (const node of screen.queryAllByText(pattern)) {
          const details = node.closest('details')
          expect(details).not.toBeNull()
          expect(details).not.toHaveAttribute('open')
          expect(within(details as HTMLElement).getByText('View technical details')).toBeInTheDocument()
        }
      }
    }

    // Status is readable as text (not a bare red dot) and a retry is wired.
    expect(await screen.findByText('Failed')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry scan' })).toBeInTheDocument()
    // Raw host/port never reaches the collapsed row.
    expect(screen.queryByText(/clickhouse\.internal/)).not.toBeInTheDocument()

    // Why it failed is readable in the row itself, not only in a title or
    // after expanding (#247).
    expect(screen.getByText('Scan failed: the data source did not respond in time.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Expand run details' }))
    expect(
      screen.getAllByText('Scan failed: the data source did not respond in time.'),
).toHaveLength(2)
    // …and what to do about it, not only the diagnosis (#247).
    expect(screen.getByText(/Read less per run/)).toBeInTheDocument()
    // The link names the section, so Limits opens and scrolls into view.
    expect(screen.getByRole('link', { name: 'Open Limits' })).toHaveAttribute(
      'href',
      '/?tab=configuration#scan-limits',
    )
    expect(screen.getByRole('link', { name: 'Open the connection' })).toHaveAttribute(
      'href',
      '/settings/data-sources/ds-1',
    )
    expectRawErrorOnlyInClosedTechnicalDetails()

    // Anyone but an owner never gets the raw error in the DOM at all.
    view.rerender(report('member'))
    expect(screen.getAllByText('Scan failed: the data source did not respond in time.')).toHaveLength(2)
    expect(screen.queryByText('View technical details')).not.toBeInTheDocument()
    expect(screen.queryByText(/clickhouse\.internal/)).not.toBeInTheDocument()
    expect(screen.queryByText(/8443/)).not.toBeInTheDocument()
  })

  it('counts metric points the same way the list chip does, and never calls them "Metric rows"', async () => {
    // The stat card used to read `breakdown_event_metrics ?? event_metrics`,
    // so a run with breakdowns reported 5 here and 17 on the scan list — two
    // numbers for one run. Both now call scanUtils.jobMetricPoints.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/scans/activity')) return mockJsonResponse(activityFor())
      if (url.endsWith('/platform-presence')) {
        return mockJsonResponse({ scan_config_id: 'scan-1', platform_column: null, platforms: [], items: [] })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1/jobs')) {
        return mockJsonResponse([
          {
            id: 'job-metrics',
            scan_config_id: 'scan-1',
            status: 'completed',
            started_at: '2026-01-01T00:00:00Z',
            completed_at: '2026-01-01T00:00:10Z',
            result_summary: {
              event_metrics: 2,
              type_metrics: 3,
              breakdown_event_metrics: 5,
              breakdown_type_metrics: 7,
            },
            error_message: null,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:10Z',
          },
        ])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    render(
      <QueryClientProvider client={queryClient}>
        <ScanDetail slug="demo" scanConfig={scanConfig} eventTypes={[]} />
      </QueryClientProvider>,
    )

    // 2 + 3 + 5 + 7. The old fallback rendered 5.
    await waitFor(() => {
      const card = screen.getByText('Metric points').parentElement!
      expect(card).toHaveTextContent('17')
    })
    // "Metric rows" collided with Observe › Metrics, the user-defined catalog.
    expect(screen.queryByText('Metric rows')).toBeNull()
  })

  it('says which population each "Rows read" figure counted', async () => {
    // `Rows read` is one label over two numbers: the catalog analyzer's
    // scan_rows_processed and metrics collection's query_rows_scanned, each
    // bounded by its own cap. The column header cannot vary per run, so the
    // stat card and the cell carry it as a title.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/scans/activity')) return mockJsonResponse(activityFor())
      if (url.endsWith('/platform-presence')) {
        return mockJsonResponse({ scan_config_id: 'scan-1', platform_column: null, platforms: [], items: [] })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1/jobs')) {
        return mockJsonResponse([
          {
            id: 'job-metrics',
            scan_config_id: 'scan-1',
            status: 'completed',
            started_at: '2026-01-01T00:00:00Z',
            completed_at: '2026-01-01T00:00:10Z',
            result_summary: { query_rows_scanned: 900 },
            error_message: null,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:10Z',
          },
        ])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <ScanDetail slug="demo" scanConfig={scanConfig} eventTypes={[]} />
      </QueryClientProvider>,
    )

    // The "Rows read · last run" card and the run's own cell, both explained.
    const explained = await screen.findAllByTitle(
      'Warehouse rows read across every metrics chunk (capped by the metrics row cap).',
    )
    expect(explained).toHaveLength(2)
    // The catalog wording must not be what a metrics run gets.
    expect(
      screen.queryByTitle('Warehouse rows the catalog analyzer read this run (capped by the row cap).'),
    ).toBeNull()
  })

  // A scan's output reaches the user as anomalies and Telegram alerts, and the
  // run report used to print those two counts as dead numbers — the owner got
  // "Scan: Snowplow Events (iOS)" in Telegram and could reach nothing from it.
  async function renderExpandedRun(summary: Record<string, number>) {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/scans/activity')) return mockJsonResponse(activityFor())
      if (url.endsWith('/platform-presence')) {
        return mockJsonResponse({ scan_config_id: 'scan-1', platform_column: null, platforms: [], items: [] })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1/jobs')) {
        return mockJsonResponse([
          {
            id: 'job-signals',
            scan_config_id: 'scan-1',
            status: 'completed',
            started_at: '2026-01-01T00:00:00Z',
            completed_at: '2026-01-01T00:00:10Z',
            result_summary: summary,
            error_message: null,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:10Z',
          },
        ])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/p/demo/scans/scan-1']}>
          <ScanDetail slug="demo" scanConfig={scanConfig} eventTypes={[]} />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Expand run details' }))
    // The raw counters are demoted behind a disclosure; the run
    // report leads instead. These cases are about the cards, so open them.
    fireEvent.click(screen.getByRole('button', { name: 'Show raw counters' }))
  }

  it('links Signals added and Alerts queued to this scan on the surfaces that hold them', async () => {
    await renderExpandedRun({ signals_added: 3, alerts_queued: 2 })

    // The counter is the only affordance connecting a run to its anomalies.
    const signalsCard = screen.getByText('Signals added').parentElement!
    const signalsLink = within(signalsCard).getByRole('link')
    expect(signalsLink).toHaveAttribute('href', '/p/demo/anomalies?scan=scan-1')
    expect(signalsLink).toHaveAttribute('title', 'View anomalies from this scan')
    expect(signalsLink).toHaveTextContent('3')

    // ...and the same for the alert that actually reached Telegram.
    const alertsCard = screen.getByText('Alerts queued').parentElement!
    const alertsLink = within(alertsCard).getByRole('link')
    expect(alertsLink).toHaveAttribute('href', '/p/demo/alerting?scan=scan-1')
    expect(alertsLink).toHaveAttribute('title', 'View alerts from this scan')
    expect(alertsLink).toHaveTextContent('2')
  })

  it('leaves a zero counter as plain text — a link to a guaranteed-empty page is worse than none', async () => {
    await renderExpandedRun({ signals_added: 0, alerts_queued: 0 })

    const signalsCard = screen.getByText('Signals added').parentElement!
    expect(within(signalsCard).queryByRole('link')).toBeNull()
    expect(signalsCard).toHaveTextContent('0')

    const alertsCard = screen.getByText('Alerts queued').parentElement!
    expect(within(alertsCard).queryByRole('link')).toBeNull()
  })

  it('renders the platform presence grid from a mocked response', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/scans/activity')) return mockJsonResponse(activityFor())
      if (url.endsWith('/platform-presence')) {
        return mockJsonResponse({
          scan_config_id: 'scan-1',
          platform_column: 'platform',
          platforms: ['android', 'ios'],
          items: [
            { event_id: 'evt-checkout', event_name: 'checkout', present_platforms: ['android', 'ios'] },
            { event_id: 'evt-signup', event_name: 'signup', present_platforms: ['ios'] },
          ],
        })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1/jobs')) {
        return mockJsonResponse([])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    render(
      <QueryClientProvider client={queryClient}>
        <ScanDetail slug="demo" scanConfig={{ ...scanConfig, platform_column: 'platform' }} eventTypes={[]} />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('Platform presence')).toBeInTheDocument()
    // Column headers are the sorted distinct platform values (await the async grid load).
    expect(await screen.findByRole('columnheader', { name: 'android' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'ios' })).toBeInTheDocument()
    // Rows are events; cells encode presence as ✓ / — with accessible labels.
    expect(screen.getByText('checkout')).toBeInTheDocument()
    expect(screen.getByText('signup')).toBeInTheDocument()
    expect(screen.getByLabelText('checkout present on android')).toHaveTextContent('✓')
    expect(screen.getByLabelText('signup absent on android')).toHaveTextContent('—')
    expect(screen.getByLabelText('signup present on ios')).toHaveTextContent('✓')
    // The panel says how much of the plan the matrix covers (#247).
    expect(screen.getByText('1 of 2 events seen on every platform value')).toBeInTheDocument()
  })

  it('says there is no platform column in Event mapping instead of an empty panel', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/scans/activity')) return mockJsonResponse(activityFor())
      if (url.endsWith('/platform-presence')) {
        return mockJsonResponse({
          scan_config_id: 'scan-1',
          platform_column: null,
          platforms: [],
          items: [],
        })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1/jobs')) {
        return mockJsonResponse([])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    render(
      <QueryClientProvider client={queryClient}>
        <ScanDetail slug="demo" scanConfig={scanConfig} eventTypes={[]} />
      </QueryClientProvider>,
    )

    // No platform column: one line in Event mapping, not a full-width panel
    // holding a dead end (#247).
    expect(await screen.findByText('Set in Configuration › App version')).toBeInTheDocument()
    expect(screen.queryByText('Platform presence')).not.toBeInTheDocument()
  })

  it('says the platform presence failed to load instead of claiming no column', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/scans/activity')) return mockJsonResponse(activityFor())
      if (url.endsWith('/platform-presence')) {
        return new Response(JSON.stringify({ detail: 'boom' }), {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1/jobs')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(
      <QueryClientProvider client={queryClient}>
        <ScanDetail slug="demo" scanConfig={{ ...scanConfig, platform_column: 'platform' }} eventTypes={[]} />
      </QueryClientProvider>,
    )

    expect(await screen.findByText("Couldn't load platform presence")).toBeInTheDocument()
    expect(screen.queryByText('No platform column configured')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })

  it('shows when a queued run was queued, and asks before stopping it', async () => {
    const queuedAt = new Date(Date.now() - 5_000).toISOString()
    const cancel = vi.fn()
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/scans/activity')) return mockJsonResponse(activityFor())
      if (url.endsWith('/platform-presence')) {
        return mockJsonResponse({ scan_config_id: 'scan-1', platform_column: null, platforms: [], items: [] })
      }
      if (url.endsWith('/jobs/job-q/cancel') && init?.method === 'POST') {
        cancel()
        return mockJsonResponse({ id: 'job-q', status: 'cancelled' })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1/jobs')) {
        return mockJsonResponse([{
          id: 'job-q',
          scan_config_id: 'scan-1',
          status: 'pending',
          started_at: null,
          completed_at: null,
          result_summary: null,
          error_message: null,
          created_at: queuedAt,
          updated_at: queuedAt,
        }])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(
      <QueryClientProvider client={queryClient}>
        <ScanDetail slug="demo" scanConfig={scanConfig} eventTypes={[]} />
      </QueryClientProvider>,
    )

    expect(await screen.findByText(/^queued /)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Stop run' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(cancel).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Stop run' }))
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1))
  })

  it('collapses a wall of consecutive failed runs behind an expander', async () => {
    const failedJob = (id: string, startedAt: string) => ({
      id,
      scan_config_id: 'scan-1',
      status: 'failed',
      started_at: startedAt,
      completed_at: startedAt,
      result_summary: null,
      error_message: 'Read timed out.',
      created_at: startedAt,
      updated_at: startedAt,
    })
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/scans/activity')) return mockJsonResponse(activityFor())
      if (url.endsWith('/platform-presence')) {
        return mockJsonResponse({ scan_config_id: 'scan-1', platform_column: null, platforms: [], items: [] })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1/jobs')) {
        return mockJsonResponse([
          failedJob('j1', '2026-01-04T00:00:00Z'),
          failedJob('j2', '2026-01-03T00:00:00Z'),
          failedJob('j3', '2026-01-02T00:00:00Z'),
          failedJob('j4', '2026-01-01T00:00:00Z'),
        ])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <ScanDetail slug="demo" scanConfig={scanConfig} eventTypes={[]} />
      </QueryClientProvider>,
    )

    // The streak banner summarizes the four failures...
    expect(await screen.findByText(/Failed last 4 runs/)).toBeInTheDocument()
    // ...and the four identical failed rows collapse behind one expander, so no
    // per-row Retry actions are shown until the streak is expanded.
    const expander = screen.getByRole('button', { name: /Show 4 repeated failed runs/ })
    expect(screen.queryAllByRole('button', { name: 'Retry scan' })).toHaveLength(0)

    // Expanding reveals the individual failed job rows.
    fireEvent.click(expander)
    expect(screen.getByRole('button', { name: /Hide 4 repeated failed runs/ })).toBeInTheDocument()
    // Retry rides only on the latest settled failure: on the older ones it
    // offered nothing Run again does not (#247).
    expect(screen.getAllByRole('button', { name: 'Retry scan' })).toHaveLength(1)
  })
})

describe('ScanDetail — streak past the loaded page', () => {
  it("banners the server's count, which agrees with the Scans list", async () => {
    const failedJob = (id: string, startedAt: string) => ({
      id,
      scan_config_id: 'scan-1',
      status: 'failed',
      started_at: startedAt,
      completed_at: startedAt,
      result_summary: null,
      error_message: 'Read timed out.',
      created_at: startedAt,
      updated_at: startedAt,
    })
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      // 120 straight failures; the page below only holds the newest two.
      if (url.endsWith('/scans/activity')) return mockJsonResponse(activityFor(120))
      if (url.endsWith('/platform-presence')) {
        return mockJsonResponse({ scan_config_id: 'scan-1', platform_column: null, platforms: [], items: [] })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1/jobs')) {
        return mockJsonResponse([
          failedJob('j1', '2026-01-04T00:00:00Z'),
          failedJob('j2', '2026-01-03T00:00:00Z'),
        ])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <ScanDetail slug="demo" scanConfig={scanConfig} eventTypes={[]} />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('Failed last 120 runs')).toBeInTheDocument()
    // The expander counts the rows it hides, which are the loaded ones.
    expect(screen.getByRole('button', { name: /Show 2 repeated failed runs/ })).toBeInTheDocument()
  })

  it('renders the runs as a table whose every column is named, the actions column included', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/scans/activity')) return mockJsonResponse(activityFor())
      if (url.endsWith('/platform-presence')) {
        return mockJsonResponse({ scan_config_id: 'scan-1', platform_column: null, platforms: [], items: [] })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1/jobs')) {
        return mockJsonResponse([{
          id: 'j1',
          scan_config_id: 'scan-1',
          status: 'completed',
          started_at: '2026-01-04T00:00:00Z',
          completed_at: '2026-01-04T00:00:05Z',
          result_summary: { events_created: 2 },
          error_message: null,
          created_at: '2026-01-04T00:00:00Z',
          updated_at: '2026-01-04T00:00:05Z',
        }])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <ScanDetail slug="demo" scanConfig={scanConfig} eventTypes={[]} />
      </QueryClientProvider>,
    )

    const headers = await screen.findAllByRole('columnheader')
    expect(headers.map(header => header.textContent)).toEqual([
      'Started', 'Duration', 'Scanned', 'Events', 'Status', 'Actions',
    ])
  })
})

describe('ScanDetail — coached demo scenario', () => {
  const SLUG = 'demo'
  const WATCH_SCAN_INSTRUCTION = at(buildChapterSteps(SLUG, 'live-loop', initialScenarioState()), 1).instruction

  function demoProject(overrides: Partial<Project> = {}): Project {
    return {
      id: 'p-1',
      name: 'Demo',
      slug: SLUG,
      created_at: '2026-07-01T00:00:00Z',
      updated_at: '2026-07-01T00:00:00Z',
      is_demo: true,
      generation_status: 'ready',
      ...overrides,
    } as Project
  }

  const job = (id: string, status: string, errorMessage: string | null = null) => ({
    id,
    scan_config_id: 'scan-1',
    status,
    started_at: '2026-02-01T00:00:00Z',
    completed_at: status === 'completed' ? '2026-02-01T00:01:00Z' : null,
    result_summary: null,
    error_message: errorMessage,
    created_at: '2026-02-01T00:00:00Z',
    updated_at: '2026-02-01T00:00:00Z',
  })

  // The feed carries a job the demo's own runtime tick produced alongside the
  // user's — a mark on the tick's row would be a false positive.
  function setupFetch(runCalls: string[] = []) {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/scans/activity')) return mockJsonResponse(activityFor())
      if (url.endsWith('/platform-presence')) {
        return mockJsonResponse({ scan_config_id: 'scan-1', platform_column: null, platforms: [], items: [] })
      }
      if (url.endsWith('/scans/scan-1/run')) {
        runCalls.push((init?.method ?? 'GET').toUpperCase())
        return mockJsonResponse(job('job-new', 'pending'))
      }
      // The scenario's own watch polls the one job by id.
      if (url.includes('/scans/scan-1/jobs/')) return mockJsonResponse(job('job-new', 'running'))
      if (url.endsWith('/scans/scan-1/jobs')) {
        return mockJsonResponse([job('job-fail', 'failed', 'Read timed out.'), job('job-tick', 'completed')])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })
  }

  function renderInScenario(project: Project) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/p/${SLUG}/scans/scan-1`]}>
          <DemoScenarioProvider project={project} pollIntervalMs={10}>
            <ScanDetail slug={SLUG} scanConfig={scanConfig} eventTypes={[]} />
          </DemoScenarioProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    )
  }

  it('binds the scenario to the ScanJob the retry POST returned', async () => {
    const runCalls: string[] = []
    setupFetch(runCalls)
    renderInScenario(demoProject())

    fireEvent.click(await screen.findByRole('button', { name: 'Retry scan' }))

    await waitFor(() => expect(readScenarioState(SLUG).chapters['live-loop']?.step).toBe('live-loop/watch-scan'))
    expect(runCalls[0]).toBe('POST')
    expect(readScenarioState(SLUG).chapters['live-loop']?.artifacts).toMatchObject({
      scanConfigId: 'scan-1',
      scanJobId: 'job-new',
    })
  })

  it('marks the row of the watched job, and no row when the watched job is not in the feed', async () => {
    setupFetch()
    writeScenarioState(
      SLUG,
      liveLoopState('live-loop/watch-scan', {
        scan: { scanConfigId: 'scan-1', scanJobId: 'job-tick', startedAt: Date.now() },
      }),
    )
    const marked = renderInScenario(demoProject())

    // Exactly one of the two feed rows is coached — the watched one.
    await waitFor(() => expect(screen.getByRole('note')).toHaveTextContent(WATCH_SCAN_INSTRUCTION))
    expect(screen.getAllByRole('note')).toHaveLength(1)
    marked.unmount()

    // A job the feed does not carry marks nothing at all.
    writeScenarioState(
      SLUG,
      liveLoopState('live-loop/watch-scan', {
        scan: { scanConfigId: 'scan-1', scanJobId: 'job-elsewhere', startedAt: Date.now() },
      }),
    )
    renderInScenario(demoProject())

    expect(await screen.findByText('Recent runs')).toBeInTheDocument()
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
  })

  it('leaves a non-demo project untouched: no coach mark, no scenario', async () => {
    setupFetch()
    renderInScenario(demoProject({ is_demo: false }))

    fireEvent.click(await screen.findByRole('button', { name: 'Retry scan' }))

    await waitFor(() => expect(screen.getByText('Recent runs')).toBeInTheDocument())
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
    expect(window.localStorage.getItem(`tripl-demo-scenario:${SLUG}`)).toBeNull()
  })
})
