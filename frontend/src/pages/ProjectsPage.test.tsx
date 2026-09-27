import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import ProjectsPage from './ProjectsPage'
import { at } from '@/test/at'

function authValue(role: 'owner' | 'editor' | 'viewer'): AuthContextValue {
  return {
    user: {
      id: `${role}-1`,
      email: `${role}@example.com`,
      name: role,
      role,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    status: 'authenticated',
    error: null,
    isLoggingOut: false,
    logout: async () => {},
    refresh: () => {},
  }
}

function renderProjectsPage(role: 'owner' | 'editor' | 'viewer' = 'owner') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })

  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={authValue(role)}>
        <MemoryRouter>
          <ProjectsPage />
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('ProjectsPage', () => {
  it('shows portfolio metrics and project summaries', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([
          {
            id: 'proj-1',
            name: 'Alpha',
            slug: 'alpha',
            description: 'Landing coverage and funnel events.',
            created_at: '2026-04-01T09:00:00Z',
            updated_at: '2026-04-10T09:00:00Z',
            summary: {
              event_type_count: 3,
              event_count: 8,
              active_event_count: 6,
              implemented_event_count: 4,
              review_pending_event_count: 2,
              archived_event_count: 2,
              variable_count: 5,
              scan_count: 2,
              alert_destination_count: 1,
              alert_rule_count: 0,
              monitoring_signal_count: 2,
              failing_scan_config_count: 0,
              latest_scan_job: {
                id: 'job-1',
                scan_config_id: 'scan-1',
                scan_name: 'Production scan',
                status: 'completed',
                started_at: '2026-04-10T08:00:00Z',
                completed_at: '2026-04-10T08:05:00Z',
                result_summary: {
                  events_created: 4,
                  signals_added: 2,
                  alerts_queued: 1,
                },
                error_message: null,
                created_at: '2026-04-10T08:05:00Z',
              },
              latest_signal: {
                scan_config_id: 'scan-1',
                scan_name: 'Production scan',
                scope_type: 'event_type',
                scope_ref: 'type-1',
                scope_name: 'Page View',
                state: 'latest_scan',
                bucket: '2026-04-10T08:00:00Z',
                actual_count: 42,
                expected_count: 21,
                z_score: 7,
                direction: 'spike',
              },
            },
          },
        ]))
      }

      if (url.endsWith('/api/v1/data-sources')) {
        return Promise.resolve(jsonResponse([
          {
            id: 'ds-1',
            name: 'Warehouse',
            db_type: 'clickhouse',
            host: 'localhost',
            port: 8123,
            database_name: 'analytics',
            username: 'default',
            password_set: false,
            connection_settings: {
              location: null,
              maximum_bytes_billed: null,
              dataset_allowlist: null,
              sslmode: null,
              sslrootcert: null,
              sslcert: null,
              search_path: null,
              sslkey_set: false,
            },
            created_at: '2026-04-01T09:00:00Z',
            updated_at: '2026-04-10T09:00:00Z',
          },
        ]))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderProjectsPage()

    expect(await screen.findByText('Alpha')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1, name: 'All projects' })).toBeInTheDocument()
    expect(screen.getByText('Project portfolio')).toBeInTheDocument()
    expect(screen.getByText('Landing coverage and funnel events.')).toBeInTheDocument()
    expect(screen.getByText('66.7% implemented')).toBeInTheDocument()
    expect(screen.getByText('2 in review')).toBeInTheDocument()
    expect(screen.getByText('Latest scan')).toBeInTheDocument()
    expect(screen.getByText('Production scan')).toBeInTheDocument()
    expect(screen.getByText('Latest scan signal')).toBeInTheDocument()
    // tripl-h5um: the scope is named inside the sentence the bell and the
    // Anomalies list use, so it cannot be read as a readout of its own. The
    // direction is in that sentence and NOT also on a chip beside it.
    expect(screen.getByText('Spike on Page View')).toBeInTheDocument()
    expect(screen.queryByText('spike')).not.toBeInTheDocument()
    // H1: the dashboard signal count never speaks of "active". It is the open
    // signal count, so it says "open" like the rest of the card (WS-43).
    expect(screen.getByText('2 open')).toBeInTheDocument()
    expect(screen.queryByText('2 recent')).not.toBeInTheDocument()
    expect(screen.queryByText('2 active')).not.toBeInTheDocument()
    expect(screen.getByText('Open signal')).toBeInTheDocument()
    expect(screen.getByText('Open project')).toBeInTheDocument()

    // UX-10: create-actions live in the header action area, not the stat strip.
    expect(screen.getByRole('button', { name: /New project/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Generate demo project/i })).toBeInTheDocument()
    // UX-10: each STATE metric is shown exactly once — no duplicated stat tiers.
    expect(screen.getAllByText('Projects')).toHaveLength(1)
    expect(screen.getAllByText('Coverage')).toHaveLength(1)
    expect(screen.getByText('Data sources')).toBeInTheDocument()
    // tripl-14eh: the tile says what it counts. "Automation 8 · 3 covered" named
    // neither the unit nor the denominator.
    expect(screen.getByText('Scans')).toBeInTheDocument()
    expect(screen.queryByText('Automation')).not.toBeInTheDocument()
    expect(screen.getByText('in 1 of 1 project')).toBeInTheDocument()
    expect(screen.queryByText('1 covered')).not.toBeInTheDocument()
    // UX-10: action-needed metrics each appear once, distinct from STATE metrics.
    expect(screen.getByText('In review', { selector: 'dt' })).toBeInTheDocument()
    // Settled vocabulary: an execution is a "run" on every web-UI surface, and
    // /projects is the first screen after login (tripl-3y7z).
    expect(screen.getByText('Failed runs')).toBeInTheDocument()
    expect(screen.queryByText('Failed jobs')).not.toBeInTheDocument()
    expect(screen.getByText('Signals')).toBeInTheDocument()
  })

  it('hides project deletion from editors', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([
          {
            id: 'proj-1',
            name: 'Alpha',
            slug: 'alpha',
            description: '',
            created_at: '2026-04-01T09:00:00Z',
            updated_at: '2026-04-10T09:00:00Z',
            summary: {
              event_type_count: 0,
              event_count: 0,
              active_event_count: 0,
              implemented_event_count: 0,
              review_pending_event_count: 0,
              archived_event_count: 0,
              variable_count: 0,
              scan_count: 0,
              alert_destination_count: 0,
              alert_rule_count: 0,
              monitoring_signal_count: 0,
              latest_scan_job: null,
              latest_signal: null,
            },
          },
        ]))
      }

      if (url.endsWith('/api/v1/data-sources')) {
        return Promise.resolve(jsonResponse([]))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderProjectsPage('editor')

    expect(await screen.findByText('Alpha')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Delete Alpha/i })).not.toBeInTheDocument()
  })

  it('enters a project at its home, from the title or the Open button (JR-1, JR-34)', async () => {
    mockSingleProject()

    renderProjectsPage('owner')

    expect(await screen.findByRole('link', { name: 'Beta' })).toHaveAttribute('href', '/p/beta/overview')
    expect(screen.getByRole('link', { name: /Open project/ })).toHaveAttribute('href', '/p/beta/overview')
  })

  it('keeps the card to one row of facts, with the rest under Details (SH-25)', async () => {
    mockSingleProject()

    renderProjectsPage('owner')

    expect(await screen.findByText('Beta')).toBeInTheDocument()
    const details = screen.getByText('Details').closest('details')
    expect(details).not.toBeNull()
    expect(details).not.toHaveAttribute('open')
    // The tiles and the scan/monitoring panels live in the fold.
    expect(within(details as HTMLElement).getByText('Latest scan')).toBeInTheDocument()
    expect(within(details as HTMLElement).getByText('Event types')).toBeInTheDocument()
    // The old chip row repeated the facts row; it is gone.
    expect(screen.queryByText('1 scan configured')).not.toBeInTheDocument()
  })

  it('counts open incidents on the card, not alert destinations (SH-26)', async () => {
    mockSingleProject()

    renderProjectsPage('owner')

    expect(await screen.findByText('Beta')).toBeInTheDocument()
    expect(screen.queryByText('Alerts')).not.toBeInTheDocument()
    const tile = screen.getByText('Open incidents').closest('dl')
    // Beta has one destination and no open incident.
    expect(tile).toHaveTextContent('0')
  })

  it('gives an empty project one setup line instead of a row of zeros (SH-25, JR-34)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url
      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([
          {
            id: 'proj-empty',
            name: 'Empty',
            slug: 'empty',
            description: '',
            created_at: '2026-05-01T09:00:00Z',
            updated_at: '2026-05-10T09:00:00Z',
            summary: {
              event_type_count: 0,
              event_count: 0,
              active_event_count: 0,
              implemented_event_count: 0,
              review_pending_event_count: 0,
              archived_event_count: 0,
              variable_count: 0,
              scan_count: 0,
              alert_destination_count: 0,
              alert_rule_count: 0,
              monitoring_signal_count: 0,
              firing_monitor_count: 0,
              open_incident_count: 0,
              failing_scan_config_count: 0,
              latest_scan_job: null,
              latest_signal: null,
            },
          },
        ]))
      }
      if (url.endsWith('/api/v1/data-sources')) return Promise.resolve(jsonResponse([]))
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderProjectsPage('owner')

    const setup = await screen.findByRole('link', { name: /Continue setup/ })
    expect(setup).toHaveAttribute('href', '/p/empty/overview')
    expect(setup).toHaveTextContent('0 of 4 set up · Next: Connect a data source')
    expect(screen.queryByText('Details')).not.toBeInTheDocument()
    expect(screen.queryByText('Latest scan')).not.toBeInTheDocument()
  })

  const RAW_SCAN_ERROR =
    "HTTPSConnectionPool(host='clickhouse.internal', port=8443): Read timed out. (read timeout=30)"

  function mockSingleProject() {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([
          {
            id: 'proj-2',
            name: 'Beta',
            slug: 'beta',
            description: 'Near-complete plan with one failed scan.',
            created_at: '2026-05-01T09:00:00Z',
            updated_at: '2026-05-10T09:00:00Z',
            summary: {
              event_type_count: 4,
              event_count: 400,
              active_event_count: 323,
              implemented_event_count: 320,
              review_pending_event_count: 1,
              archived_event_count: 0,
              variable_count: 3,
              scan_count: 1,
              alert_destination_count: 1,
              alert_rule_count: 0,
              monitoring_signal_count: 1,
              open_incident_count: 0,
              failing_scan_config_count: 1,
              latest_scan_job: {
                id: 'job-2',
                scan_config_id: 'scan-2',
                scan_name: 'Nightly scan',
                status: 'failed',
                started_at: '2026-05-10T08:00:00Z',
                completed_at: '2026-05-10T08:01:00Z',
                result_summary: null,
                error_message: RAW_SCAN_ERROR,
                created_at: '2026-05-10T08:01:00Z',
              },
              latest_signal: null,
            },
          },
        ]))
      }

      if (url.endsWith('/api/v1/data-sources')) {
        return Promise.resolve(jsonResponse([]))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })
  }

  it('formats coverage like Live activity and pluralizes counts (H2, M10, L1)', async () => {
    mockSingleProject()

    renderProjectsPage('owner')

    expect(await screen.findByText('Beta')).toBeInTheDocument()
    // H2: 320/323 renders as "99.1%", never "99%".
    expect(screen.getByText('99.1% implemented')).toBeInTheDocument()
    expect(screen.getAllByText('99.1%').length).toBeGreaterThan(0)
    expect(screen.queryByText('99% implemented')).not.toBeInTheDocument()
    // L1 + M10: singular, unit-aware copy. The rollup counts failing scans
    // (per-scan latest run), not just the single newest run (tripl-7l83.3).
    // The UI noun is "scan", never "scan config" (tripl-3y7z).
    expect(screen.getByText('1 scan failing across 1 project')).toBeInTheDocument()
    // tripl-a1d1: the review hint names the project holding the queue instead of
    // counting projects it does not open.
    expect(screen.getByText('1 in Beta')).toBeInTheDocument()
    expect(screen.queryByText('across 1 project')).not.toBeInTheDocument()
    // The compact row names the last run instead of the configured count (SH-25).
    expect(screen.getByText('Failed').closest('[data-slot="chip"]')).toHaveAttribute('data-tone', 'danger')
    expect(screen.getByText('1 open signal')).toBeInTheDocument()
    // UX-10: the monitoring-signal metric lives once now, as an action-needed
    // stat — no separate Automation banner repeating the count.
    expect(screen.getByText('Signals')).toBeInTheDocument()
    // H1: open-signal copy drops "active" — the dashboard counts open signals.
    expect(
      screen.getByText('1 project currently has open signals'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/active or recent signals/)).not.toBeInTheDocument()
  })

  it('shows a friendly scan error with an owner-only technical expander (H3)', async () => {
    mockSingleProject()

    renderProjectsPage('owner')

    expect(await screen.findByText('Beta')).toBeInTheDocument()
    expect(
      screen.getByText('Scan failed: the data source did not respond in time.'),
    ).toBeInTheDocument()
    // Owner can drill into the raw exception behind an expander.
    expect(screen.getByText('View technical details')).toBeInTheDocument()
    expect(screen.getByText(/HTTPSConnectionPool/)).toBeInTheDocument()
  })

  it('hides raw scan internals from non-owners (H3)', async () => {
    mockSingleProject()

    renderProjectsPage('viewer')

    expect(await screen.findByText('Beta')).toBeInTheDocument()
    expect(
      screen.getByText('Scan failed: the data source did not respond in time.'),
    ).toBeInTheDocument()
    // The raw host/port exception and the expander must not exist for viewers.
    expect(screen.queryByText('View technical details')).not.toBeInTheDocument()
    expect(screen.queryByText(/HTTPSConnectionPool/)).not.toBeInTheDocument()
  })

  it('leads the project card with one attention color and calms the rest (UX-23)', async () => {
    mockSingleProject()

    renderProjectsPage('owner')

    expect(await screen.findByText('Beta')).toBeInTheDocument()
    // Live monitoring signals are the needs-attention lead → saturated danger.
    // (Chip is class-based since DS-6, so the tone is read off data-tone.)
    const chipOf = (text: string) => screen.getByText(text).closest('[data-slot="chip"]')
    expect(chipOf('1 open signal')).toHaveAttribute('data-tone', 'danger')
    // The review queue renders calm/muted so it does not compete.
    expect(chipOf('1 in review')).toHaveAttribute('data-tone', 'neutral')
  })

  it('surfaces the latest scan result with rows scanned (UX-18)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([
          {
            id: 'proj-3',
            name: 'Gamma',
            slug: 'gamma',
            description: 'Healthy plan with a recent metrics scan.',
            created_at: '2026-06-01T09:00:00Z',
            updated_at: '2026-06-10T09:00:00Z',
            summary: {
              event_type_count: 2,
              event_count: 10,
              active_event_count: 10,
              implemented_event_count: 10,
              review_pending_event_count: 0,
              archived_event_count: 0,
              variable_count: 2,
              scan_count: 1,
              alert_destination_count: 0,
              alert_rule_count: 0,
              monitoring_signal_count: 0,
              latest_scan_job: {
                id: 'job-3',
                scan_config_id: 'scan-3',
                scan_name: 'Hourly scan',
                status: 'completed',
                started_at: '2026-06-10T08:00:00Z',
                completed_at: '2026-06-10T08:02:00Z',
                result_summary: {
                  query_rows_scanned: 12345,
                },
                error_message: null,
                created_at: '2026-06-10T08:02:00Z',
              },
              latest_signal: null,
            },
          },
        ]))
      }

      if (url.endsWith('/api/v1/data-sources')) {
        return Promise.resolve(jsonResponse([]))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderProjectsPage('owner')

    expect(await screen.findByText('Gamma')).toBeInTheDocument()
    expect(screen.getByText('Hourly scan')).toBeInTheDocument()
    // The Latest scan panel reports a real last result, not just a status.
    const rowsLine = screen.getByText(/warehouse rows read/)
    expect(rowsLine).toBeInTheDocument()
    expect(rowsLine.textContent?.replace(/[^0-9]/g, '')).toBe('12345')
    // tripl-h5um: the line says which population it counts. Beside a Monitoring
    // tile printing an event count for one bucket, a bare "12,345 rows scanned"
    // read as the same figure disagreeing with itself.
    expect(rowsLine).toHaveAttribute(
      'title',
      'Warehouse rows this run read from the data source. Not an event count.',
    )
  })

  it('suppresses a zero scan delta instead of announcing it in the success colour (tripl-h5um)', async () => {
    // The demo runtime's own ticks report events_created: 0 — a completed run
    // that changed nothing. The events chip used to render whenever the counter
    // was present, so it printed a green "+0 events" while its zero siblings
    // (signals, alerts) were correctly silent.
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([
          {
            id: 'proj-zero',
            name: 'Zeta',
            slug: 'zeta',
            description: 'A completed run that created nothing.',
            created_at: '2026-06-01T09:00:00Z',
            updated_at: '2026-06-10T09:00:00Z',
            summary: {
              event_type_count: 2,
              event_count: 10,
              active_event_count: 10,
              implemented_event_count: 10,
              review_pending_event_count: 0,
              archived_event_count: 0,
              variable_count: 2,
              scan_count: 1,
              alert_destination_count: 0,
              alert_rule_count: 0,
              monitoring_signal_count: 0,
              failing_scan_config_count: 0,
              latest_scan_job: {
                id: 'job-zero',
                scan_config_id: 'scan-zero',
                scan_name: 'Nightly scan',
                status: 'completed',
                started_at: '2026-06-10T08:00:00Z',
                completed_at: '2026-06-10T08:02:00Z',
                result_summary: {
                  events_created: 0,
                  signals_added: 0,
                  alerts_queued: 0,
                  query_rows_scanned: 8261,
                },
                error_message: null,
                created_at: '2026-06-10T08:02:00Z',
              },
              latest_signal: null,
            },
          },
        ]))
      }

      if (url.endsWith('/api/v1/data-sources')) {
        return Promise.resolve(jsonResponse([]))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderProjectsPage('owner')

    expect(await screen.findByText('Zeta')).toBeInTheDocument()
    expect(screen.getByText('Nightly scan')).toBeInTheDocument()
    expect(screen.getByText(/warehouse rows read/)).toBeInTheDocument()
    expect(screen.queryByText('+0 events')).not.toBeInTheDocument()
    expect(screen.queryByText('+0 signals')).not.toBeInTheDocument()
    expect(screen.queryByText('+0 alerts')).not.toBeInTheDocument()
  })

  it('agrees with the scan detail page about how many rows a run read (tripl-h5um)', async () => {
    // A run that reports BOTH counters: settings/scans/scanUtils.ts prefers
    // query_rows_scanned, so this card must too — otherwise one run shows 900
    // here and 12,345 on its own scan page.
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([
          {
            id: 'proj-both',
            name: 'Eta',
            slug: 'eta',
            description: 'A combined run reporting both row counters.',
            created_at: '2026-06-01T09:00:00Z',
            updated_at: '2026-06-10T09:00:00Z',
            summary: {
              event_type_count: 2,
              event_count: 10,
              active_event_count: 10,
              implemented_event_count: 10,
              review_pending_event_count: 0,
              archived_event_count: 0,
              variable_count: 2,
              scan_count: 1,
              alert_destination_count: 0,
              alert_rule_count: 0,
              monitoring_signal_count: 0,
              failing_scan_config_count: 0,
              latest_scan_job: {
                id: 'job-both',
                scan_config_id: 'scan-both',
                scan_name: 'Combined scan',
                status: 'completed',
                started_at: '2026-06-10T08:00:00Z',
                completed_at: '2026-06-10T08:02:00Z',
                result_summary: {
                  scan_rows_processed: 900,
                  query_rows_scanned: 12345,
                },
                error_message: null,
                created_at: '2026-06-10T08:02:00Z',
              },
              latest_signal: null,
            },
          },
        ]))
      }

      if (url.endsWith('/api/v1/data-sources')) {
        return Promise.resolve(jsonResponse([]))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderProjectsPage('owner')

    expect(await screen.findByText('Eta')).toBeInTheDocument()
    const rowsLine = screen.getByText(/warehouse rows read/)
    expect(rowsLine.textContent?.replace(/[^0-9]/g, '')).toBe('12345')
  })

  it('names a catalog run\'s figure as column combinations, not warehouse rows (#247 DA-4)', async () => {
    // A catalog run reports only scan_rows_processed: the rows of its GROUP BY
    // breakdown, i.e. distinct column combinations. The scan page prints "153
    // combos" for this run; the card must not call the same 153 warehouse rows.
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([
          {
            id: 'proj-catalog',
            name: 'Iota',
            slug: 'iota',
            description: 'A catalog run.',
            created_at: '2026-06-01T09:00:00Z',
            updated_at: '2026-06-10T09:00:00Z',
            summary: {
              event_type_count: 2,
              event_count: 10,
              active_event_count: 10,
              implemented_event_count: 10,
              review_pending_event_count: 0,
              archived_event_count: 0,
              variable_count: 2,
              scan_count: 1,
              alert_destination_count: 0,
              alert_rule_count: 0,
              monitoring_signal_count: 0,
              failing_scan_config_count: 0,
              latest_scan_job: {
                id: 'job-catalog',
                scan_config_id: 'scan-catalog',
                scan_name: 'Catalog scan',
                status: 'completed',
                started_at: '2026-06-10T08:00:00Z',
                completed_at: '2026-06-10T08:02:00Z',
                result_summary: {
                  scan_rows_processed: 153,
                },
                error_message: null,
                created_at: '2026-06-10T08:02:00Z',
              },
              latest_signal: null,
            },
          },
        ]))
      }

      if (url.endsWith('/api/v1/data-sources')) {
        return Promise.resolve(jsonResponse([]))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderProjectsPage('owner')

    expect(await screen.findByText('Iota')).toBeInTheDocument()
    const line = screen.getByText(/combos grouped in the warehouse/)
    expect(line).toHaveTextContent('153 combos grouped in the warehouse')
    expect(line.getAttribute('title')).toMatch(/column combinations/)
    expect(screen.queryByText(/warehouse rows read/)).not.toBeInTheDocument()
  })

  it('says what the monitoring tile counts and when (tripl-h5um)', async () => {
    // The two tiles inside one project card print 8,261 and 13,373 for the same
    // scan name at the same clock time. They are a warehouse row count and an
    // event count for one bucket; unlabelled they read as a disagreement.
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([
          {
            id: 'proj-total',
            name: 'Theta',
            slug: 'theta',
            description: 'A project-total spike beside its scan run.',
            created_at: '2026-06-01T09:00:00Z',
            updated_at: '2026-06-10T09:00:00Z',
            summary: {
              event_type_count: 2,
              event_count: 10,
              active_event_count: 10,
              implemented_event_count: 10,
              review_pending_event_count: 0,
              archived_event_count: 0,
              variable_count: 2,
              scan_count: 1,
              alert_destination_count: 0,
              alert_rule_count: 0,
              monitoring_signal_count: 3,
              failing_scan_config_count: 0,
              latest_scan_job: {
                id: 'job-total',
                scan_config_id: 'scan-total',
                scan_name: 'zebrascan28366',
                status: 'completed',
                started_at: '2026-06-10T08:00:00Z',
                completed_at: '2026-06-10T09:00:00Z',
                result_summary: {
                  events_created: 0,
                  query_rows_scanned: 8261,
                },
                error_message: null,
                created_at: '2026-06-10T09:00:00Z',
              },
              latest_signal: {
                scan_config_id: 'scan-total',
                scan_name: 'zebrascan28366',
                scope_type: 'project_total',
                scope_ref: 'proj-total',
                scope_name: 'Project total',
                state: 'latest_scan',
                bucket: '2026-06-10T09:00:00Z',
                actual_count: 13373,
                expected_count: 8392,
                z_score: 11.9,
                direction: 'spike',
              },
            },
          },
        ]))
      }

      if (url.endsWith('/api/v1/data-sources')) {
        return Promise.resolve(jsonResponse([]))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderProjectsPage('owner')

    expect(await screen.findByText('Theta')).toBeInTheDocument()
    // "Project total" is the name of the series that fired, not a total of the
    // project — the verb in front of it is what says so.
    expect(screen.getByText('Spike on Project total')).toBeInTheDocument()
    // Both halves of the pair name their population IN THE LINE. A `title` is
    // not an answer: it never renders, so the reader comparing the two tiles
    // still saw a bare 13,373 beside "8,261 warehouse rows read" under one scan
    // name and one clock time.
    expect(screen.getByText(/warehouse rows read/)).toHaveTextContent(
      '8,261 warehouse rows read',
    )
    const countsLine = screen.getByText(/events in this bucket vs/)
    expect(countsLine).toHaveTextContent('13,373 events in this bucket vs 8,392 expected')
    expect(countsLine).toHaveAttribute(
      'title',
      'What the detector measured in this one bucket, against the baseline it expected. Not a row count.',
    )
    // The scan tile's timestamp is a completion; this one is a bucket. Both read
    // 9:00 AM on the demo stand, so each says which it is.
    expect(screen.getByText(/^Bucket .* · via zebrascan28366$/)).toBeInTheDocument()
    expect(screen.getByText(/^Completed /)).toBeInTheDocument()
  })

  it('surfaces a failing scan even when the newest run overall succeeded (tripl-7l83.3)', async () => {
    // The project's single newest job (latest_scan_job) COMPLETED, but two other
    // scan configs fail every run. The old rollup keyed off latest_scan_job would
    // report "healthy" and hide them; failing_scan_config_count must not.
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([
          {
            id: 'proj-4',
            name: 'Delta',
            slug: 'delta',
            description: 'One config succeeds; two others fail every run.',
            created_at: '2026-06-01T09:00:00Z',
            updated_at: '2026-06-10T09:00:00Z',
            summary: {
              event_type_count: 2,
              event_count: 10,
              active_event_count: 10,
              implemented_event_count: 10,
              review_pending_event_count: 0,
              archived_event_count: 0,
              variable_count: 2,
              scan_count: 3,
              alert_destination_count: 0,
              alert_rule_count: 0,
              monitoring_signal_count: 0,
              failing_scan_config_count: 2,
              latest_scan_job: {
                id: 'job-4',
                scan_config_id: 'scan-4',
                scan_name: 'Hourly success',
                status: 'completed',
                started_at: '2026-06-10T08:00:00Z',
                completed_at: '2026-06-10T08:02:00Z',
                result_summary: { events_created: 3 },
                error_message: null,
                created_at: '2026-06-10T08:02:00Z',
              },
              latest_signal: null,
            },
          },
        ]))
      }

      if (url.endsWith('/api/v1/data-sources')) {
        return Promise.resolve(jsonResponse([]))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderProjectsPage('owner')

    expect(await screen.findByText('Delta')).toBeInTheDocument()
    // Workspace rollup counts the failing scans, not "healthy".
    expect(screen.getByText('2 scans failing across 1 project')).toBeInTheDocument()
    expect(screen.queryByText('Latest scan runs are healthy')).not.toBeInTheDocument()
    // The project card surfaces the failing scans even though the latest run
    // shown in its Latest scan panel succeeded.
    expect(screen.getByText('2 scans failing')).toBeInTheDocument()
    expect(screen.getByText('Hourly success')).toBeInTheDocument()
  })

  it('tucks project deletion behind an overflow menu, not a bare trash button (tripl-7l83.17)', async () => {
    mockSingleProject()

    renderProjectsPage('owner')

    expect(await screen.findByText('Beta')).toBeInTheDocument()
    // The one-click destructive trash button is gone from the card header.
    expect(screen.queryByRole('button', { name: /^Delete Beta$/i })).not.toBeInTheDocument()

    // Delete now lives behind an accessible overflow ("...") menu trigger.
    const trigger = screen.getByRole('button', { name: /project actions for beta/i })
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu')

    // The menu is keyboard-openable and the trash is reachable inside it.
    fireEvent.keyDown(trigger, { key: 'Enter' })
    expect(await screen.findByRole('menuitem', { name: /delete/i })).toBeInTheDocument()
  })

  it('renders the workspace coverage fraction in a neutral tone, not danger (tripl-7l83.17)', async () => {
    mockSingleProject()

    renderProjectsPage('owner')

    expect(await screen.findByText('Beta')).toBeInTheDocument()
    const coverageStat = screen.getByText('Coverage').closest('dl')
    expect(coverageStat).not.toBeNull()
    // The Coverage MiniStat delta ("implemented/active events") must read as
    // neutral — not danger/red — so a healthy 99% coverage never implies a
    // problem. The unit is part of the delta since tripl-14eh.
    const fraction = within(coverageStat as HTMLElement).getByText('320/323 events')
    expect(fraction).toHaveStyle({ color: 'var(--fg-subtle)' })
    expect(fraction).not.toHaveStyle({ color: 'var(--danger)' })
  })

  it('links the pending-review count into that project, not the workspace total (tripl-a1d1)', async () => {
    mockSingleProject()

    renderProjectsPage('owner')

    expect(await screen.findByText('Beta')).toBeInTheDocument()
    // The workspace total is a readout: there is no workspace-wide review queue
    // to open, so the tile no longer opens one project's while naming them all.
    expect(
      screen.queryByRole('link', { name: /^In review: 1 event/i }),
    ).not.toBeInTheDocument()
    // The card's own count carries the link, and says whose queue it opens.
    const reviewLink = screen.getByRole('link', {
      name: 'In review in Beta: 1 event',
    })
    expect(reviewLink).toHaveAttribute('href', '/p/beta/events/review')
    expect(reviewLink).toHaveTextContent('1 in review')
  })

  function mockPendingReviewProjects() {
    const project = (
      name: string,
      slug: string,
      reviewPending: number,
      updatedAt: string,
    ) => ({
      id: slug,
      name,
      slug,
      description: '',
      created_at: '2026-04-01T09:00:00Z',
      updated_at: updatedAt,
      summary: {
        event_type_count: 2,
        event_count: 10,
        active_event_count: 10,
        implemented_event_count: 10,
        review_pending_event_count: reviewPending,
        archived_event_count: 0,
        variable_count: 0,
        scan_count: 1,
        alert_destination_count: 0,
        alert_rule_count: 0,
        monitoring_signal_count: 0,
        failing_scan_config_count: 0,
        latest_scan_job: null,
        latest_signal: null,
      },
    })

    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(
          jsonResponse([
            // The SMALLER queue is the most recently updated project, which is
            // exactly what the old single link followed.
            project('Acme Web', 'acme-web', 1441, '2026-04-01T09:00:00Z'),
            project('Acme Android', 'acme-android', 55, '2026-06-10T09:00:00Z'),
          ]),
        )
      }
      if (url.endsWith('/api/v1/data-sources')) return Promise.resolve(jsonResponse([]))
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })
  }

  it('names where the review backlog is and links each queue to its project (tripl-a1d1)', async () => {
    mockPendingReviewProjects()

    renderProjectsPage('owner')

    expect(await screen.findByText('Acme Web')).toBeInTheDocument()

    // The tile still totals the workspace...
    const reviewTile = screen.getByText('In review', { selector: 'dt' }).closest('dl')
    expect(reviewTile).not.toBeNull()
    expect(reviewTile).toHaveTextContent('1496')
    // ...and now says where those events actually are, biggest queue first,
    // instead of the bare "across 2 projects" that opened only one of them.
    expect(screen.getByText('1441 in Acme Web · 55 in Acme Android')).toBeInTheDocument()
    expect(screen.queryByText('across 2 projects')).not.toBeInTheDocument()

    // Each card links to ITS OWN queue. The old tile link followed the most
    // recently updated project (Acme Android, 55) and left the other 1441
    // unreachable from anywhere on the page.
    expect(
      screen.getByRole('link', { name: 'In review in Acme Web: 1441 events' }),
    ).toHaveAttribute('href', '/p/acme-web/events/review')
    expect(
      screen.getByRole('link', { name: 'In review in Acme Android: 55 events' }),
    ).toHaveAttribute('href', '/p/acme-android/events/review')
  })

  it('leaves a failed project list to the shell, and does not call it an empty workspace', async () => {
    // Layout's "Backend is unavailable" card reports this failure (fj5g.6);
    // the page's own card said it a second time. See Layout.test.tsx for the
    // two rendered together.
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'))

    const { container } = renderProjectsPage()

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    await waitFor(() => expect(container.querySelector('[data-slot="skeleton"]')).toBeNull())
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByText('Keep your product analytics honest')).not.toBeInTheDocument()
  })

  it('enters the new project after creating it instead of staying on the workspace (tripl-q7i1.8)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === 'string'
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url
        const method = (init?.method ?? 'GET').toUpperCase()

        if (url.endsWith('/api/v1/projects') && method === 'POST') {
          return Promise.resolve(
            jsonResponse(
              {
                id: 'proj-new',
                name: 'My Project',
                // The server assigns a slug that DIFFERS from the client-derived
                // 'my-project' (e.g. collision handling appends a suffix), so the
                // assertion below proves navigation uses the server's returned slug
                // rather than the value the user typed (tripl-q7i1.8).
                slug: 'my-project-2',
                description: '',
                created_at: '2026-07-16T09:00:00Z',
                updated_at: '2026-07-16T09:00:00Z',
                summary: {
                  event_type_count: 0,
                  event_count: 0,
                  active_event_count: 0,
                  implemented_event_count: 0,
                  review_pending_event_count: 0,
                  archived_event_count: 0,
                  variable_count: 0,
                  scan_count: 0,
                  alert_destination_count: 0,
                  alert_rule_count: 0,
                  monitoring_signal_count: 0,
                  failing_scan_config_count: 0,
                  latest_scan_job: null,
                  latest_signal: null,
                },
              },
              201,
            ),
          )
        }

        if (url.endsWith('/api/v1/projects')) {
          return Promise.resolve(jsonResponse([]))
        }

        if (url.endsWith('/api/v1/data-sources')) {
          return Promise.resolve(jsonResponse([]))
        }

        return Promise.reject(new Error(`Unexpected request: ${url}`))
      },
    )

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    function LocationProbe() {
      const location = useLocation()
      return <div data-testid="location">{location.pathname}</div>
    }

    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={authValue('owner')}>
          <MemoryRouter initialEntries={['/workspace']}>
            <Routes>
              <Route path="/workspace" element={<ProjectsPage />} />
              <Route path="/p/:slug/overview" element={<LocationProbe />} />
            </Routes>
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )

    // Wait for the empty workspace welcome to settle, then open the create dialog.
    expect(await screen.findByText('Keep your product analytics honest')).toBeInTheDocument()
    fireEvent.click(at(screen.getAllByRole('button', { name: /New project/i }), 0))

    // Filling the name auto-derives the slug; submit the form.
    fireEvent.change(screen.getByLabelText(/project name/i), {
      target: { value: 'My Project' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    // On success the user is routed into the new project's overview, not left
    // stranded on /workspace.
    await waitFor(() => {
      expect(screen.getByTestId('location')).toHaveTextContent('/p/my-project-2/overview')
    })
  })

  function mockEmptyWorkspace() {
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === 'string'
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url
        const method = (init?.method ?? 'GET').toUpperCase()

        if (url.endsWith('/api/v1/projects/demo') && method === 'POST') {
          // Never settles: keeps the page in its provisioning state so tests can
          // observe the disabled/Generating… CTA without a fake clock.
          return new Promise<Response>(() => {})
        }
        if (url.endsWith('/api/v1/projects')) {
          return Promise.resolve(jsonResponse([]))
        }
        if (url.endsWith('/api/v1/data-sources')) {
          return Promise.resolve(jsonResponse([]))
        }
        return Promise.reject(new Error(`Unexpected request: ${url}`))
      },
    )
  }

  it('welcomes an empty workspace with the product hero instead of zero stats (tripl-odrj.1)', async () => {
    mockEmptyWorkspace()

    renderProjectsPage('owner')

    expect(await screen.findByText('Keep your product analytics honest')).toBeInTheDocument()
    // The h1 stays, but the duplicate header CTA pair is gone — each CTA now
    // exists exactly once, inside the hero.
    expect(screen.getByRole('heading', { level: 1, name: 'All projects' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Generate demo project/i })).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: /New project/i })).toHaveLength(1)
    // The all-zero stat band is hidden until the first project exists.
    expect(screen.queryByText('Coverage')).not.toBeInTheDocument()
    expect(screen.queryByText('In review', { selector: 'dt' })).not.toBeInTheDocument()
    expect(screen.queryByText('Scans')).not.toBeInTheDocument()
    // The old bare EmptyState copy is retired in favour of the hero.
    expect(screen.queryByText('No projects yet')).not.toBeInTheDocument()
  })

  it('starts demo provisioning from the hero CTA and disables it while running (tripl-odrj.1)', async () => {
    mockEmptyWorkspace()

    renderProjectsPage('owner')

    const demoButton = await screen.findByRole('button', { name: /Generate demo project/i })
    fireEvent.click(demoButton)

    // The click dispatched POST /projects/demo (held pending by the mock) and
    // opened the modal provisioning dialog, which aria-hides the page behind
    // it — so the swapped CTA must be queried with hidden: true.
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    const generating = await screen.findByRole('button', { name: /Generating…/, hidden: true })
    expect(generating).toBeDisabled()
  })

  it('shows viewers the pillars and an ask-an-owner note without create buttons (tripl-odrj.1)', async () => {
    mockEmptyWorkspace()

    renderProjectsPage('viewer')

    expect(await screen.findByText('Keep your product analytics honest')).toBeInTheDocument()
    expect(screen.getByText('Design what should be tracked')).toBeInTheDocument()
    expect(screen.getByText('Watch the real data')).toBeInTheDocument()
    expect(screen.getByText('Stay in control')).toBeInTheDocument()
    expect(
      screen.getByText(/Ask a workspace owner or editor to create the first project/),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /Generate demo project/i }),
    ).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /New project/i })).not.toBeInTheDocument()
  })

  it('returns the stat band and header CTAs once the first project exists (tripl-odrj.1)', async () => {
    mockSingleProject()

    renderProjectsPage('owner')

    expect(await screen.findByText('Beta')).toBeInTheDocument()
    expect(screen.queryByText('Keep your product analytics honest')).not.toBeInTheDocument()
    // Stat band is back.
    expect(screen.getByText('Coverage')).toBeInTheDocument()
    expect(screen.getByText('In review', { selector: 'dt' })).toBeInTheDocument()
    // Header CTA pair is back — one instance of each, in the header only.
    expect(screen.getAllByRole('button', { name: /Generate demo project/i })).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: /New project/i })).toHaveLength(1)
  })
})

describe('ProjectsPage ?new=1 (#238 SH-15)', () => {
  it('opens the create dialog when the switcher sends the user here to create one', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      if (url.endsWith('/api/v1/projects') || url.endsWith('/api/v1/data-sources')) {
        return Promise.resolve(jsonResponse([]))
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={authValue('owner')}>
          <MemoryRouter initialEntries={['/workspace?new=1']}>
            <Routes>
              <Route path="/workspace" element={<ProjectsPage />} />
            </Routes>
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )
    expect(await screen.findByLabelText(/project name/i)).toBeInTheDocument()
  })
})
