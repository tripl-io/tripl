import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { DemoScenarioProvider } from '@/demo/DemoScenarioProvider'
import {
  buildChapterSteps,
  initialScenarioState,
  readScenarioState,
  writeScenarioState,
} from '@/demo/scenarioModel'
import { liveLoopState } from '@/demo/scenarioTestState'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import type { Project, Role } from '@/types'
import { ScansTab } from './ScansTab'
import ProjectScansPage from '../ProjectScansPage'
import { at } from '@/test/at'
import { personaAuth, type Persona } from '@/test/persona'
import { PersonaProject } from '@/test/PersonaProject'

const navigateMock = vi.fn()

// Only useNavigate is stubbed: the scenario provider needs the real router
// (useLocation, MemoryRouter) to know which surface the user is standing on.
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigateMock,
}))

// CodeMirror needs real layout measurement that jsdom can't provide and
// tokenizes SQL across many spans. Stub it with a plain textarea that exposes
// the value, keeping the suite deterministic and editor content queryable.
vi.mock('@uiw/react-codemirror', () => ({
  default: ({
    value,
    onChange,
    placeholder,
  }: {
    value: string
    onChange: (v: string) => void
    placeholder?: string
  }) => (
    <textarea
      value={value}
      onChange={e => onChange(e.target.value)}
      placeholder={placeholder}
    />
  ),
}))

vi.mock('@/hooks/useBranch', () => ({
  useActiveBranchId: () => null,
}))

function mockJsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

type ActivityOverrides = {
  scan_config_id?: string
  failing_streak?: number
  rows_read_24h?: number
  warehouse_rows_24h?: number
  catalog_combinations_24h?: number
}

/** GET /scans/activity: the server's exact streak and 24h rows per scan. */
function activityResponse(items: ActivityOverrides[] = [{}]) {
  return {
    window_from: '2026-01-31T00:00:00Z',
    window_to: '2026-02-01T00:00:00Z',
    items: items.map(item => ({
      scan_config_id: 'scan-1',
      latest_job: null,
      failing_streak: 0,
      rows_read_24h: 0,
      warehouse_rows_24h: 0,
      catalog_combinations_24h: 0,
      ...item,
    })),
  }
}

const dataSource = {
  id: 'ds-1',
  name: 'Web Production',
  db_type: 'clickhouse',
  host: 'h',
  port: 8123,
  database_name: 'analytics',
  username: 'u',
  password_set: true,
  timeout_seconds: null,
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
  last_test_at: null,
  last_test_status: null,
  last_test_message: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const scanConfig = {
  id: 'scan-1',
  data_source_id: 'ds-1',
  project_id: 'p1',
  event_type_id: null,
  name: 'Main events scan',
  base_query: 'SELECT * FROM analytics.events_v2\nWHERE _ingested_at > {{since}}',
  event_type_column: 'event_category',
  time_column: 'received_at',
  event_name_format: '{action}',
  json_value_paths: ['properties.plan'],
  event_group_rules: [],
  metric_breakdown_columns: ['platform'],
  metric_breakdown_values_limit: 50,
  distribution_drift_fields: [],
  cardinality_threshold: 100,
  interval: '15m',
  replay_chunk_interval: '1d',
  scan_lookback_hours: 24,
  scan_row_limit: null,
  metrics_row_limit: null,
  app_version_column: 'app_version',
  app_version_keep_releases: 5,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

function setupFetch() {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    const url = String(input)
    if (url.includes('/data-sources/') && url.includes('/schema')) {
      return mockJsonResponse({
        tables: [
          { name: 'events', columns: [{ name: 'id', data_type: 'UInt64' }] },
        ],
      })
    }
    if (url.endsWith('/api/v1/data-sources')) return mockJsonResponse([dataSource])
    if (url.endsWith('/api/v1/projects/demo/scans')) return mockJsonResponse([scanConfig])
    if (url.endsWith('/api/v1/projects/demo/scans/activity')) return mockJsonResponse(activityResponse())
    if (url.includes('/scans/scan-1/jobs')) return mockJsonResponse([])
    if (url.includes('/eventTypes') || url.includes('/event-types')) return mockJsonResponse([])
    throw new Error(`Unhandled fetch: ${url}`)
  })
}

const failedJob = (id: string, ts: string) => ({
  id,
  scan_config_id: 'scan-1',
  status: 'failed',
  started_at: ts,
  completed_at: ts,
  result_summary: null,
  error_message: 'Scan failed: could not connect to the data source.',
  created_at: ts,
  updated_at: ts,
})

// Fetch stub whose /jobs feed returns the given jobs and whose manual-trigger
// (POST /scans/scan-1/run) records each call so a test can assert "Run again"
// hits the existing endpoint.
function setupFetchWithJobs(
  jobs: unknown[],
  runCalls?: { method: string; url: string }[],
  activity: ActivityOverrides = {},
) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input)
    const method = (init?.method ?? 'GET').toUpperCase()
    if (url.includes('/data-sources/') && url.includes('/schema')) return mockJsonResponse({ tables: [] })
    if (url.endsWith('/api/v1/data-sources')) return mockJsonResponse([dataSource])
    if (url.endsWith('/api/v1/projects/demo/scans')) return mockJsonResponse([scanConfig])
    if (url.endsWith('/api/v1/projects/demo/scans/activity')) {
      return mockJsonResponse(activityResponse([activity]))
    }
    if (url.includes('/scans/scan-1/run')) {
      runCalls?.push({ method, url })
      return mockJsonResponse({
        id: 'job-new',
        scan_config_id: 'scan-1',
        status: 'pending',
        started_at: null,
        completed_at: null,
        result_summary: null,
        error_message: null,
        created_at: '2026-02-01T00:00:00Z',
        updated_at: '2026-02-01T00:00:00Z',
      })
    }
    if (url.includes('/scans/scan-1/jobs')) return mockJsonResponse(jobs)
    if (url.includes('/eventTypes') || url.includes('/event-types')) return mockJsonResponse([])
    throw new Error(`Unhandled fetch: ${url}`)
  })
}

// The scan rows now carry a real <Link> to the detail page, so the tab needs a
// router. useNavigate is still the mock above — Link resolves its href through
// react-router's own internals, which the mock does not intercept.
function authAs(role: Role): AuthContextValue {
  return {
    user: {
      id: `${role}-1`,
      email: `${role}@example.com`,
      name: role,
      role,
      is_platform_admin: false,
      orgs: [],
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

/** As an owner unless a test says otherwise: authoring a scan is owner-only. */
function renderTab(role: Persona = 'owner') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={personaAuth(role)}>
        <PersonaProject persona={role}>
          <MemoryRouter initialEntries={['/p/demo/scans']}>
            <ScansTab slug="demo" />
          </MemoryRouter>
        </PersonaProject>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

/** The routed page, for the paths ScansTab alone cannot answer (`/scans/new`). */
function renderRoute(path: string, role: Persona = 'owner') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={personaAuth(role)}>
        <PersonaProject persona={role}>
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route path="/p/:slug/scans/:scanId" element={<ProjectScansPage />} />
              <Route path="/p/:slug/scans" element={<ProjectScansPage />} />
            </Routes>
          </MemoryRouter>
        </PersonaProject>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  navigateMock.mockReset()
  window.localStorage.clear()
})

describe('ScansTab', () => {
  it('offers an editor Run now but no scan authoring', async () => {
    setupFetch()
    renderTab('member')

    expect(await screen.findByRole('note')).toHaveTextContent(/done by an owner/)
    expect(await screen.findByRole('button', { name: 'Run Main events scan now' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /New scan/ })).not.toBeInTheDocument()
  })

  it('offers a viewer neither authoring nor runs', async () => {
    setupFetchWithJobs([failedJob('job-f1', '2026-01-01T00:00:00Z')])
    renderTab('viewer')

    expect(await screen.findByRole('note')).toHaveTextContent(/viewer role/)
    await screen.findByText('Recent runs')
    expect(screen.queryByRole('button', { name: /Run .* now|Run again/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /New scan/ })).not.toBeInTheDocument()
  })

  it('renders the scan list with KPIs and config rows', async () => {
    setupFetch()
    renderTab()

    expect(await screen.findByText('Main events scan')).toBeInTheDocument()
    // "Scans" names three separate surfaces now that the KPI label and panel
    // title were renamed off "Scan configs": the page heading, the KPI tile and
    // the list panel. The heading renders in every state — zero scans, error,
    // still loading — so a bare `getAllByText('Scans').length >= 1` was
    // satisfied by it alone and deleting the tile or renaming the panel left
    // this test green. Pin each surface by the text around it instead.
    const scansSurfaces = screen
      .getAllByText('Scans')
      .map((el) => el.parentElement?.textContent)
    // The KPI tile: the label sits directly over the config count.
    expect(scansSurfaces).toContain('Scans1')
    // The list panel is "All scans", an h2 under the page's h1 "Scans" — two
    // same-named headings at one level gave the page no single title.
    // Its title
    // sits directly over its count subtitle, and the count agrees with its
    // noun. This fixture has ONE scan on purpose — the state every project is
    // in the moment it finishes the onboarding checklist's "Run a scan" step —
    // so "1 scans" would fail here.
    expect(screen.getByRole('heading', { level: 1, name: 'Scans' })).toBeInTheDocument()
    expect(screen.getByText('Govern')).toBeInTheDocument()
    const listPanel = screen.getByRole('heading', { level: 2, name: 'All scans' })
    expect(listPanel.parentElement?.textContent).toContain('All scans1 scan')
    // Heading and tile, and nothing else — a third would make the tile
    // assertion above ambiguous again.
    expect(scansSurfaces).toHaveLength(2)
    // "Monitoring" is both the KPI label and this row's mode badge.
    expect(screen.getAllByText('Monitoring')).toHaveLength(2)
    // Warehouse rows only; catalog runs' grouped combinations are a different
    // unit and stay out of the figure (#247).
    expect(screen.getByText('Warehouse rows · 24h')).toBeInTheDocument()
    // The aggregate the strip was missing: scans whose latest run failed.
    expect(screen.getByText('Failing').parentElement?.textContent).toBe('Failing0')
    // Rows lead with a human summary (source · cadence); the raw SQL is demoted
    // to a faint secondary line rather than its own prominent column.
    expect(screen.getByText(/Web Production · Every 15 min/)).toBeInTheDocument()
    expect(screen.getByText(/SELECT \* FROM analytics\.events_v2/)).toBeInTheDocument()
  })

  // The recent-runs rail prints its count through `formatCount`, which compacts
  // (1.8M) — so the noun has to agree with the raw number, not with the string
  // that reaches the screen. A run that read a single warehouse row said
  // "1 rows".
  it('agrees with the count on a run that read exactly one row', async () => {
    setupFetchWithJobs([
      {
        id: 'job-one-row',
        scan_config_id: 'scan-1',
        status: 'completed',
        started_at: '2026-02-01T00:00:00Z',
        completed_at: '2026-02-01T00:00:05Z',
        result_summary: { query_rows_scanned: 1 },
        error_message: null,
        created_at: '2026-02-01T00:00:00Z',
        updated_at: '2026-02-01T00:00:05Z',
      },
    ])
    renderTab()

    expect(await screen.findByText('1 row')).toBeInTheDocument()
    expect(screen.queryByText('1 rows')).not.toBeInTheDocument()
  })

  // The per-scan job queries used to be coerced to `[]` while in flight, so
  // every row rendered a finished-looking "Never run" chip and the 24h tile read
  // 0 — contradicting the completed runs listed in the activity rail on the same
  // screen.
  it('shows a loading placeholder, not "Never run", while the job queries are in flight', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.includes('/data-sources/') && url.includes('/schema')) {
        return mockJsonResponse({ tables: [] })
      }
      if (url.endsWith('/api/v1/data-sources')) return mockJsonResponse([dataSource])
      if (url.endsWith('/api/v1/projects/demo/scans')) return mockJsonResponse([scanConfig])
      // Never settles: holds the row in its loading state for the assertions.
      if (url.includes('/scans/scan-1/jobs')) return new Promise<Response>(() => {})
      if (url.endsWith('/api/v1/projects/demo/scans/activity')) return new Promise<Response>(() => {})
      if (url.includes('/eventTypes') || url.includes('/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderTab()

    expect(await screen.findByText('Main events scan')).toBeInTheDocument()
    expect(
      screen.getByLabelText('Loading last run for Main events scan'),
    ).toBeInTheDocument()
    expect(screen.queryByText('Never run')).not.toBeInTheDocument()
    // The 24h rows KPI stays "—" rather than asserting a definitive 0.
    expect(screen.getByText('—')).toBeInTheDocument()
    expect(screen.queryByText('0')).not.toBeInTheDocument()
  })

  it('says "Never run" once the job list has loaded and is genuinely empty', async () => {
    setupFetch()
    renderTab()

    expect(await screen.findByText('Never run')).toBeInTheDocument()
    expect(
      screen.queryByLabelText('Loading last run for Main events scan'),
    ).not.toBeInTheDocument()
  })

  it('labels failed runs as text with a sanitised message and no raw internals', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.includes('/data-sources/') && url.includes('/schema')) {
        return mockJsonResponse({ tables: [] })
      }
      if (url.endsWith('/api/v1/data-sources')) return mockJsonResponse([dataSource])
      if (url.endsWith('/api/v1/projects/demo/scans')) return mockJsonResponse([scanConfig])
      if (url.endsWith('/api/v1/projects/demo/scans/activity')) {
        return mockJsonResponse(activityResponse([{ failing_streak: 1 }]))
      }
      if (url.includes('/scans/scan-1/jobs')) {
        return mockJsonResponse([
          {
            id: 'job-fail',
            scan_config_id: 'scan-1',
            status: 'failed',
            started_at: '2026-01-01T00:00:00Z',
            completed_at: '2026-01-01T00:07:12Z',
            result_summary: null,
            error_message:
              "HTTPSConnectionPool(host='clickhouse.internal', port=8443): Read timed out. (read timeout=30)",
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:07:12Z',
          },
        ])
      }
      if (url.includes('/eventTypes') || url.includes('/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderTab()

    // The friendly message renders (Last-run column + Recent-runs feed); the
    // failure is labelled "Failed" as text, and no raw host/port leaks.
    expect(
      (await screen.findAllByText('Scan failed: the data source did not respond in time.')).length,
    ).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('Failed').length).toBeGreaterThanOrEqual(1)
    expect(screen.queryByText(/clickhouse\.internal/)).not.toBeInTheDocument()
  })

  it('collapses a failing streak into one "failed last N runs" row with a single Run again', async () => {
    setupFetchWithJobs(
      [
        failedJob('job-f3', '2026-01-03T00:00:00Z'),
        failedJob('job-f2', '2026-01-02T00:00:00Z'),
        failedJob('job-f1', '2026-01-01T00:00:00Z'),
      ],
      undefined,
      { failing_streak: 3 },
    )
    renderTab()

    // The three identical failures collapse into one Recent-runs row tagged with
    // the streak, instead of N repeated failed rows.
    expect(await screen.findByText(/failed last 3 runs/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Run again/i })).toHaveLength(1)
  })

  // The loaded page holds 10 jobs; the streak runs past it. It used to read
  // "failed last 10+ runs" — a floor — and now carries the server's exact count
  // over the whole history.
  it("tags the streak with the server's exact count when it runs past the loaded page", async () => {
    setupFetchWithJobs(
      Array.from({ length: 10 }, (_, i) =>
        failedJob(`job-f${i}`, `2026-01-${String(20 - i).padStart(2, '0')}T00:00:00Z`),
      ),
      undefined,
      { failing_streak: 37 },
    )
    renderTab()

    expect(await screen.findByText('failed last 37 runs')).toBeInTheDocument()
    expect(screen.queryByText(/\+ runs/)).not.toBeInTheDocument()
  })

  it("shows the server's exact 24h warehouse rows across every scan, without a floor", async () => {
    const scanConfig2 = { ...scanConfig, id: 'scan-2', name: 'Backfill scan' }
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/data-sources')) return mockJsonResponse([dataSource])
      if (url.endsWith('/api/v1/projects/demo/scans')) return mockJsonResponse([scanConfig, scanConfig2])
      if (url.endsWith('/api/v1/projects/demo/scans/activity')) {
        return mockJsonResponse(
          activityResponse([
            { scan_config_id: 'scan-1', rows_read_24h: 1353, warehouse_rows_24h: 1200, catalog_combinations_24h: 153 },
            { scan_config_id: 'scan-2', rows_read_24h: 300, warehouse_rows_24h: 300 },
          ]),
        )
      }
      if (url.includes('/jobs')) return mockJsonResponse([])
      if (url.includes('/eventTypes') || url.includes('/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderTab()

    const label = await screen.findByText('Warehouse rows · 24h')
    await waitFor(() => expect(label.parentElement?.textContent).toBe('Warehouse rows · 24h1.5K'))
    expect(label.parentElement?.textContent).not.toContain('+')
    // The catalog runs' combinations are named beside it, not added in.
    expect(label.closest('[title]')).toHaveAttribute(
      'title',
      'Warehouse rows read by runs in the last 24 hours. Catalog runs that report no warehouse rows also read back 153 column combinations.',
    )
  })

  // The tag used to take its number from the activity endpoint alone, so it
  // vanished while that loaded, if it failed, or when it lagged a run that
  // finished since. The page's own count stands in (review A).
  it("keeps the streak tag when the server's count is behind the loaded page", async () => {
    setupFetchWithJobs(
      [
        failedJob('job-f3', '2026-01-03T00:00:00Z'),
        failedJob('job-f2', '2026-01-02T00:00:00Z'),
        failedJob('job-f1', '2026-01-01T00:00:00Z'),
      ],
      undefined,
      { failing_streak: 0 },
    )
    renderTab()

    expect(await screen.findByText(/failed last 3 runs/)).toBeInTheDocument()
  })

  it('re-runs a failed scan from the run row via the manual trigger endpoint', async () => {
    const runCalls: { method: string; url: string }[] = []
    setupFetchWithJobs([failedJob('job-f1', '2026-01-01T00:00:00Z')], runCalls)
    renderTab()

    // A single failure shows no streak badge but still offers Run again.
    expect(screen.queryByText(/failed last/)).not.toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: /Run again/i }))

    await waitFor(() => expect(runCalls.length).toBeGreaterThanOrEqual(1))
    expect(at(runCalls, 0).method).toBe('POST')
    expect(at(runCalls, 0).url).toContain('/projects/demo/scans/scan-1/run')
  })

  it('marks only the pending row busy while its re-run is in flight', async () => {
    const scanConfig2 = { ...scanConfig, id: 'scan-2', name: 'Backfill scan' }
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.includes('/data-sources/') && url.includes('/schema')) return mockJsonResponse({ tables: [] })
      if (url.endsWith('/api/v1/data-sources')) return mockJsonResponse([dataSource])
      if (url.endsWith('/api/v1/projects/demo/scans')) return mockJsonResponse([scanConfig, scanConfig2])
      if (url.endsWith('/api/v1/projects/demo/scans/activity')) {
        return mockJsonResponse(activityResponse([{ scan_config_id: 'scan-1' }, { scan_config_id: 'scan-2' }]))
      }
      // Hold the POST open so the mutation stays pending for the assertion window.
      if (url.includes('/scans/scan-1/run')) return new Promise<Response>(() => {})
      if (url.includes('/scans/scan-1/jobs')) return mockJsonResponse([failedJob('job-a1', '2026-01-02T00:00:00Z')])
      if (url.includes('/scans/scan-2/jobs')) {
        return mockJsonResponse([{ ...failedJob('job-b1', '2026-01-01T00:00:00Z'), scan_config_id: 'scan-2' }])
      }
      if (url.includes('/eventTypes') || url.includes('/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderTab()

    // Recent runs sort newest-first: scan-1's failure (Jan 2) renders above
    // scan-2's (Jan 1), so the first Run again belongs to scan-1.
    const runAgainButtons = await screen.findAllByRole('button', { name: /Run again/i })
    expect(runAgainButtons).toHaveLength(2)
    fireEvent.click(at(runAgainButtons, 0))

    // Only the clicked row's button goes busy; the other scan's stays live.
    expect(await screen.findByRole('button', { name: 'Starting…' })).toBeDisabled()
    expect(screen.getByRole('button', { name: /Run again/i })).not.toBeDisabled()
  })

  it('runs a scan from the config row via the manual trigger endpoint', async () => {
    const runCalls: { method: string; url: string }[] = []
    setupFetchWithJobs([], runCalls)
    renderTab()

    // Every config row exposes a "Run now" control on the list itself — the
    // surface the demo coach's step-1 CTA opens.
    const runButton = await screen.findByRole('button', { name: 'Run Main events scan now' })
    expect(runButton).toHaveTextContent('Run now')
    fireEvent.click(runButton)

    await waitFor(() => expect(runCalls.length).toBeGreaterThanOrEqual(1))
    expect(at(runCalls, 0).method).toBe('POST')
    expect(at(runCalls, 0).url).toContain('/projects/demo/scans/scan-1/run')
  })

  // A schedule with no time column is never dispatched, so it collects no metric
  // point, raises no anomaly and sends no alert — but every run still goes green.
  // Counting it as Monitoring was the list agreeing with that lie.
  it('excludes a scheduled scan with no time column from the Monitoring count and flags it on its row', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.includes('/data-sources/') && url.includes('/schema')) {
        return mockJsonResponse({ tables: [] })
      }
      if (url.endsWith('/api/v1/data-sources')) return mockJsonResponse([dataSource])
      if (url.endsWith('/api/v1/projects/demo/scans')) {
        return mockJsonResponse([{ ...scanConfig, time_column: null, interval: '1h' }])
      }
      if (url.endsWith('/api/v1/projects/demo/scans/activity')) return mockJsonResponse(activityResponse())
      if (url.includes('/scans/scan-1/jobs')) return mockJsonResponse([])
      if (url.includes('/eventTypes') || url.includes('/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderTab()

    expect(await screen.findByText('Main events scan')).toBeInTheDocument()
    // The badge has to name the fault, not restate what a healthy "Catalog only"
    // row also does — in a list holding both, "No metrics collected" left the
    // reader with the tone and a hover title to tell a choice from a defect.
    expect(screen.getByText('Needs a time column')).toBeInTheDocument()
    expect(screen.queryByText('No metrics collected')).toBeNull()

    // The KPI grid is [Scans, Monitoring, Failing, Warehouse rows · 24h]; the
    // Monitoring tile must read 0, not 1.
    const monitoringLabel = screen.getByText('Monitoring')
    expect(monitoringLabel.parentElement?.textContent).toBe('Monitoring0')
  })

  it('navigates to detail by URL (not the in-place create view)', async () => {
    setupFetch()
    renderTab()

    // The name is the row's link, and its href is the live /p/:slug/scans/:id
    // route. The legacy /p/:slug/settings/scans/:id form is only a redirect in
    // App.tsx, so shipping it here would send every keyboard user through a
    // bounce.
    const link = await screen.findByRole('link', { name: 'Main events scan' })
    expect(link).toHaveAttribute('href', '/p/demo/scans/scan-1')

    // The mouse shortcut on the rest of the row goes to the same place — one
    // href feeds both, so they cannot drift.
    fireEvent.click(link.closest('tr')!)
    expect(navigateMock).toHaveBeenCalledWith('/p/demo/scans/scan-1')
  })

  it('opens the create page on its own route, so Back and reload keep it', async () => {
    setupFetch()
    renderTab()

    // Wait for data sources to load so the "New scan" button is enabled.
    await screen.findByText('Main events scan')
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /New scan/i })).not.toBeDisabled(),
    )
    fireEvent.click(screen.getByRole('button', { name: /New scan/i }))

    expect(navigateMock).toHaveBeenCalledWith('/p/demo/scans/new')
  })

  it('renders the create page at /scans/new and gates column mapping behind preview', async () => {
    setupFetch()
    renderRoute('/p/demo/scans/new')

    // Scoped to the heading: the list's own "New scan" BUTTON carries the same
    // words, so a bare text query would match it and pass either way.
    expect(await screen.findByRole('heading', { name: 'New scan' })).toBeInTheDocument()
    // The essentials block is always visible; everything else is a collapsed
    // section whose fields are not mounted until it is opened.
    expect(screen.getByLabelText('Name')).toBeInTheDocument()
    expect(screen.getByLabelText('Data source')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Limits/ })).toBeInTheDocument()
    expect(screen.queryByLabelText('Row cap per run')).toBeNull()

    // Cancel on an untouched form goes back to the list without asking.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(navigateMock).toHaveBeenCalledWith('/p/demo/scans')
  })

  it('replaces /scans/new with the created scan, so Back returns to the list', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      if (url.endsWith('/api/v1/data-sources')) return mockJsonResponse([dataSource])
      if (url.endsWith('/api/v1/projects/demo/scans') && method === 'POST') {
        return mockJsonResponse({ ...scanConfig, id: 'scan-new' })
      }
      if (url.endsWith('/api/v1/projects/demo/scans')) return mockJsonResponse([scanConfig])
      if (url.includes('/data-sources/') && url.includes('/schema')) return mockJsonResponse({ tables: [] })
      if (url.includes('/event-types')) {
        return mockJsonResponse([{ id: 'et-1', name: 'click', display_name: 'Click', fields: [] }])
      }
      throw new Error(`Unhandled fetch: ${method} ${url}`)
    })
    renderRoute('/p/demo/scans/new')

    await screen.findByRole('heading', { name: 'New scan' })
    fireEvent.click(screen.getByLabelText('Catalog only'))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Main scan' } })
    fireEvent.change(screen.getByLabelText('Data source'), { target: { value: 'ds-1' } })
    // The SQL editor is a lazy chunk. Its placeholder is the example as an SQL
    // comment, so match the example inside it.
    fireEvent.change(await screen.findByPlaceholderText(/SELECT \* FROM analytics\.events/), {
      target: { value: 'SELECT * FROM analytics.events' },
    })
    await screen.findByRole('option', { name: 'Click' })
    fireEvent.change(screen.getByLabelText('Event type'), { target: { value: 'et-1' } })
    fireEvent.click(screen.getByRole('button', { name: /Create scan/ }))

    await waitFor(() =>
      expect(navigateMock).toHaveBeenCalledWith('/p/demo/scans/scan-new', { replace: true }),
    )
  })

  it('sends a non-owner who opens /scans/new to the list', async () => {
    setupFetch()
    renderRoute('/p/demo/scans/new', 'member')

    expect(await screen.findByText('Main events scan')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'New scan' })).not.toBeInTheDocument()
  })
})

describe('ScansTab — coached demo scenario', () => {
  const SLUG = 'demo'
  const RUN_SCAN_INSTRUCTION = buildChapterSteps(SLUG, 'live-loop', initialScenarioState())[0].instruction
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

  const completedJob = (id: string, ts: string) => ({
    id,
    scan_config_id: 'scan-1',
    status: 'completed',
    started_at: ts,
    completed_at: ts,
    result_summary: { events_created: 3 },
    error_message: null,
    created_at: ts,
    updated_at: ts,
  })

  /** Feed rows plus the scenario's own by-id watch of the job it is following. */
  function setupDemoFetch(jobs: unknown[], runCalls: string[] = []) {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.includes('/data-sources/') && url.includes('/schema')) return mockJsonResponse({ tables: [] })
      if (url.endsWith('/api/v1/data-sources')) return mockJsonResponse([dataSource])
      if (url.endsWith('/api/v1/projects/demo/scans')) return mockJsonResponse([scanConfig])
      if (url.endsWith('/api/v1/projects/demo/scans/activity')) return mockJsonResponse(activityResponse())
      if (url.includes('/scans/scan-1/run')) {
        runCalls.push((init?.method ?? 'GET').toUpperCase())
        return mockJsonResponse(completedJob('job-new', '2026-02-01T00:00:00Z'))
      }
      if (url.includes('/scans/scan-1/jobs/')) {
        return mockJsonResponse({ ...completedJob('job-new', '2026-02-01T00:00:00Z'), status: 'running' })
      }
      if (url.includes('/scans/scan-1/jobs')) return mockJsonResponse(jobs)
      if (url.includes('/eventTypes') || url.includes('/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
  }

  function renderInScenario(project: Project) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={authAs('owner')}>
          <MemoryRouter initialEntries={[`/p/${SLUG}/scans`]}>
            <DemoScenarioProvider project={project} pollIntervalMs={10}>
              <ScansTab slug={SLUG} />
            </DemoScenarioProvider>
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )
  }

  it('binds the scenario to the ScanJob "Run again" created', async () => {
    const runCalls: string[] = []
    setupDemoFetch([failedJob('job-f1', '2026-01-01T00:00:00Z')], runCalls)
    renderInScenario(demoProject())

    fireEvent.click(await screen.findByRole('button', { name: /Run again/i }))

    await waitFor(() => expect(readScenarioState(SLUG).chapters['live-loop']?.step).toBe('live-loop/watch-scan'))
    expect(runCalls[0]).toBe('POST')
    // The artifact is the job the POST returned, never a job already in the feed.
    expect(readScenarioState(SLUG).chapters['live-loop']?.artifacts).toMatchObject({
      scanConfigId: 'scan-1',
      scanJobId: 'job-new',
    })
  })

  it('runs from the config row and binds the scenario to the returned ScanJob', async () => {
    const runCalls: string[] = []
    setupDemoFetch([], runCalls)
    renderInScenario(demoProject())

    fireEvent.click(await screen.findByRole('button', { name: 'Run Main events scan now' }))

    await waitFor(() => expect(readScenarioState(SLUG).chapters['live-loop']?.step).toBe('live-loop/watch-scan'))
    expect(runCalls[0]).toBe('POST')
    // The artifact is the job the POST returned (notifyScanRunStarted), never a
    // job already in the feed.
    expect(readScenarioState(SLUG).chapters['live-loop']?.artifacts).toMatchObject({
      scanConfigId: 'scan-1',
      scanJobId: 'job-new',
    })
  })

  it('points the run-scan coach mark at the first config row for a ready demo', async () => {
    setupDemoFetch([])
    renderInScenario(demoProject())

    // The step-1 coach mark renders on the surface the CTA opens (the list),
    // anchored to the first config row's Run control — exactly one note.
    await waitFor(() =>
      expect(screen.getByRole('note')).toHaveTextContent(RUN_SCAN_INSTRUCTION),
    )
    expect(screen.getAllByRole('note')).toHaveLength(1)
  })

  it('marks only the recent-run row of the job the scenario is watching', async () => {
    setupDemoFetch([
      completedJob('job-b', '2026-01-02T00:00:00Z'),
      completedJob('job-a', '2026-01-01T00:00:00Z'),
    ])
    writeScenarioState(
      SLUG,
      liveLoopState('live-loop/watch-scan', {
        scan: { scanConfigId: 'scan-1', scanJobId: 'job-b', startedAt: Date.now() },
      }),
    )
    renderInScenario(demoProject())

    // Both runs render; only the user's own run is coached — the demo's tick
    // produces the rest, and marking them all would coach nothing.
    await waitFor(() => expect(screen.getByRole('note')).toHaveTextContent(WATCH_SCAN_INSTRUCTION))
    expect(screen.getAllByRole('note')).toHaveLength(1)
    expect(screen.getAllByText('Main events scan').length).toBeGreaterThanOrEqual(2)
  })

  it('leaves a non-demo project untouched: no coach mark, no scenario', async () => {
    setupDemoFetch([failedJob('job-f1', '2026-01-01T00:00:00Z')])
    renderInScenario(demoProject({ is_demo: false }))

    fireEvent.click(await screen.findByRole('button', { name: /Run again/i }))

    await waitFor(() => expect(screen.getByText('Recent runs')).toBeInTheDocument())
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
    expect(window.localStorage.getItem(`tripl-demo-scenario:${SLUG}`)).toBeNull()
  })
})

describe('ScansTab — data layer and feedback (batch 4)', () => {
  it('asks each scan for the head of its history, not 50 full jobs', async () => {
    setupFetch()
    renderTab()

    await screen.findByText('Main events scan')
    await waitFor(() =>
      expect(vi.mocked(globalThis.fetch).mock.calls.map(([input]) => String(input))).toContainEqual(
        expect.stringContaining('/scans/scan-1/jobs?limit=10'),
      ),
    )
  })

  it('keeps counting a failing streak while a retry is queued', async () => {
    // The server's count looks past the queued retry; the list must still
    // collapse the failures behind it into the tagged row.
    setupFetchWithJobs(
      [
        {
          ...failedJob('job-p', '2026-01-05T00:00:00Z'),
          status: 'pending',
          started_at: null,
          completed_at: null,
          error_message: null,
        },
        failedJob('job-f3', '2026-01-03T00:00:00Z'),
        failedJob('job-f2', '2026-01-02T00:00:00Z'),
        failedJob('job-f1', '2026-01-01T00:00:00Z'),
      ],
      undefined,
      { failing_streak: 3 },
    )
    renderTab()

    expect(await screen.findByText(/failed last 3 runs/)).toBeInTheDocument()
  })

  it('says a Run now failed, and for which scan', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/data-sources')) return mockJsonResponse([dataSource])
      if (url.endsWith('/api/v1/projects/demo/scans')) return mockJsonResponse([scanConfig])
      if (url.includes('/scans/scan-1/run')) {
        return new Response(JSON.stringify({ detail: 'Not allowed for this project' }), {
          status: 403,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/activity')) return mockJsonResponse(activityResponse())
      if (url.includes('/scans/scan-1/jobs')) return mockJsonResponse([])
      if (url.includes('/eventTypes') || url.includes('/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderTab()

    fireEvent.click(await screen.findByRole('button', { name: 'Run Main events scan now' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not start Main events scan: Not allowed for this project',
    )
  })

  it('does not claim "No data sources" before the list has loaded', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      // The data-source list never answers: a cold load.
      if (url.endsWith('/api/v1/data-sources')) return new Promise<Response>(() => {})
      if (url.endsWith('/api/v1/projects/demo/scans')) return mockJsonResponse([scanConfig])
      if (url.endsWith('/api/v1/projects/demo/scans/activity')) return mockJsonResponse(activityResponse())
      if (url.includes('/scans/scan-1/jobs')) return mockJsonResponse([])
      if (url.includes('/eventTypes') || url.includes('/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderTab()

    await screen.findByText('Main events scan')
    expect(screen.queryByText('No data sources')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /New scan/ })).not.toBeDisabled()
  })

  it('says why New scan is off as visible text, not a hidden title (#237)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/data-sources')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/scans')) return mockJsonResponse([scanConfig])
      if (url.endsWith('/api/v1/projects/demo/scans/activity')) return mockJsonResponse(activityResponse())
      if (url.includes('/scans/scan-1/jobs')) return mockJsonResponse([])
      if (url.includes('/eventTypes') || url.includes('/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderTab()

    expect(await screen.findByText('No data sources')).toBeInTheDocument()
    const newScan = screen.getByRole('button', { name: /New scan/ })
    expect(newScan).toBeDisabled()
    expect(newScan).not.toHaveAttribute('title')
    expect(newScan).toHaveAccessibleDescription('Add a data source first.')
  })

  function setupEmptyProject(dataSources: unknown[]) {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/data-sources')) return mockJsonResponse(dataSources)
      if (url.endsWith('/api/v1/projects/demo/scans')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/scans/activity')) return mockJsonResponse(activityResponse([]))
      if (url.includes('/eventTypes') || url.includes('/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
  }

  it('shows one empty state, and no tiles, in a project with no data source (#247)', async () => {
    setupEmptyProject([])
    renderTab()

    expect(
      await screen.findByRole('heading', { name: 'Connect your warehouse to start scanning' }),
    ).toBeInTheDocument()
    // What setting up a scan involves, in order.
    const steps = screen.getByRole('list', { name: 'Setting up a scan' })
    expect(steps).toHaveTextContent('Add a connection to your warehouse')
    expect(screen.getByRole('link', { name: 'Add connection' })).toHaveAttribute('href', '/settings/data-sources')
    // Nothing else competes with it: no stat strip, no second empty state, no
    // empty "All scans" panel, no disabled New scan.
    expect(screen.queryByText('Monitoring')).not.toBeInTheDocument()
    expect(screen.queryByText('No data sources')).not.toBeInTheDocument()
    expect(screen.queryByText('All scans')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /New scan/ })).not.toBeInTheDocument()
  })

  it('offers New scan from the empty state once a data source exists (#247)', async () => {
    setupEmptyProject([dataSource])
    renderTab()

    expect(await screen.findByRole('heading', { name: 'Create your first scan' })).toBeInTheDocument()
    const newScan = screen.getByRole('button', { name: /New scan/ })
    fireEvent.click(newScan)
    expect(navigateMock).toHaveBeenCalledWith('/p/demo/scans/new')
    // One New scan, the empty state's, not a second in the header.
    expect(screen.getAllByRole('button', { name: /New scan/ })).toHaveLength(1)
  })

  it('says the data sources did not load instead of offering a first scan', async () => {
    // A failed list is not a list with a source in it: "Create your first
    // scan" skipped step 1 for a project that may have no connection at all.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/data-sources')) {
        return new Response(JSON.stringify({ detail: 'Database unavailable' }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.endsWith('/api/v1/projects/demo/scans')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/scans/activity')) return mockJsonResponse(activityResponse([]))
      if (url.includes('/eventTypes') || url.includes('/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderTab()

    expect(
      await screen.findByRole('heading', { name: 'Could not load this project’s data sources' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Try again/ })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Create your first scan' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /New scan/ })).not.toBeInTheDocument()
  })

  it('tells a viewer of an empty project who creates scans, with no button (#247)', async () => {
    setupEmptyProject([dataSource])
    renderTab('viewer')

    expect(await screen.findByRole('heading', { name: 'No scans yet' })).toBeInTheDocument()
    expect(screen.getByText(/An owner creates scans/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /New scan/ })).not.toBeInTheDocument()
  })

  it('tones the Failing tile when a scan is failing (#247)', async () => {
    setupFetchWithJobs([failedJob('job-f1', '2026-01-01T00:00:00Z')], undefined, { failing_streak: 1 })
    renderTab()

    await waitFor(() =>
      expect(screen.getByText('Failing').parentElement?.textContent).toBe('Failing1'),
    )
  })

  it('fills Recent runs past two rows when there is only one scan (#247)', async () => {
    const completed = (id: string, ts: string, rows: number) => ({
      id,
      scan_config_id: 'scan-1',
      status: 'completed',
      started_at: ts,
      completed_at: ts,
      result_summary: { scan_rows_processed: rows },
      error_message: null,
      created_at: ts,
      updated_at: ts,
    })
    setupFetchWithJobs([
      completed('j4', '2026-01-04T00:00:00Z', 153),
      completed('j3', '2026-01-03T00:00:00Z', 1),
      completed('j2', '2026-01-02T00:00:00Z', 2),
      completed('j1', '2026-01-01T00:00:00Z', 3),
    ])
    renderTab()

    // All four runs, with the catalog figure named for what it is.
    expect(await screen.findByText('153 combos')).toBeInTheDocument()
    expect(screen.getByText('1 combo')).toBeInTheDocument()
    expect(screen.getByText('2 combos')).toBeInTheDocument()
    expect(screen.getByText('3 combos')).toBeInTheDocument()
  })

  it('offers Run again only on the latest failure, not one a success followed (#247)', async () => {
    setupFetchWithJobs([
      {
        id: 'job-ok',
        scan_config_id: 'scan-1',
        status: 'completed',
        started_at: '2026-01-02T00:00:00Z',
        completed_at: '2026-01-02T00:00:05Z',
        result_summary: { query_rows_scanned: 10 },
        error_message: null,
        created_at: '2026-01-02T00:00:00Z',
        updated_at: '2026-01-02T00:00:05Z',
      },
      failedJob('job-old', '2026-01-01T00:00:00Z'),
    ])
    renderTab()

    expect(await screen.findByText('10 rows')).toBeInTheDocument()
    // The old failure is still listed, with its reason, but nothing to retry.
    expect(screen.getAllByText('Failed').length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: /Run again/i })).not.toBeInTheDocument()
  })
})
