import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ThemeProvider } from '@/components/theme-provider'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { ActivityPanel } from '@/components/activity-panel'
import { setOnboardingDismissed } from '@/lib/onboardingDismissal'
import OverviewPage from './OverviewPage'
import { formatNumber } from '@/lib/format'

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const PROJECT = {
  id: 'project-1',
  name: 'Demo',
  slug: 'demo',
  description: '',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  summary: {
    event_type_count: 6,
    event_count: 2483,
    active_event_count: 323,
    implemented_event_count: 320,
    review_pending_event_count: 8,
    archived_event_count: 12,
    variable_count: 40,
    scan_count: 5,
    // Superset, event-scope-inclusive count. The Overview must NOT use this for
    // "Open signals" — it has to follow the (empty) signals array instead (H1).
    monitoring_signal_count: 5,
    latest_scan_job: null,
    latest_signal: null,
  },
}

/** An ISO bucket start `hoursAgo` hours before now. */
function hoursAgo(hours: number): string {
  return new Date(Date.now() - hours * 60 * 60 * 1000).toISOString()
}

interface MockOpts {
  /** Replaces the project summary (merged over PROJECT.summary). */
  summary?: Partial<typeof PROJECT.summary> & Record<string, unknown>
  activity?: unknown[]
  signals?: unknown[]
  sources?: unknown[]
  /** `GET /source-freshness` rows (F16, #269). */
  freshness?: unknown[]
  scanConfigName?: string | null
  /** `null` models a project with no scan config at all — the bare empty series. */
  scanConfigId?: string | null
  /** `[]` models a scan whose buckets all predate the card's 7-day window. */
  volumeData?: Array<{
    bucket: string
    count: number
    expected_count: number | null
    is_anomaly?: boolean
  }>
  kpiSeries?: number[]
  topEvents?: Array<{ event_id: string; name: string; total_count: number; window_total_count?: number }>
  /** Never settles the volume request, so the card stays in its pending state. */
  holdVolume?: boolean
}

function mockFetch(opts?: MockOpts) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input)
    if (url.includes('/metrics/total')) {
      if (opts?.holdVolume) return new Promise<Response>(() => {})
      return jsonResponse({
        scope: 'project_total',
        scan_config_id: opts?.scanConfigId === undefined ? 'scan-1' : opts.scanConfigId,
        // The card charts ONE scan config and names it.
        scan_config_name: opts?.scanConfigName ?? 'Snowplow Pageviews (iOS)',
        event_id: null,
        event_type_id: null,
        interval: 'hour',
        latest_signal: null,
        data: opts?.volumeData ?? [
          { bucket: hoursAgo(2), count: 10, expected_count: null },
          { bucket: hoursAgo(1), count: 25, expected_count: null },
        ],
        forecast: [],
      })
    }
    if (url.includes('/overview/kpi-series')) {
      const series = opts?.kpiSeries ?? [1, 2, 3]
      return jsonResponse({ days: series.length, new_events: series })
    }
    if (url.includes('/overview/top-events')) return jsonResponse(opts?.topEvents ?? [])
    // No branch for the metrics catalog, the event-type list or the per-id event
    // lookup: every signal carries its own `scope_name`, so those three requests
    // are gone and any survivor lands on the throw below.
    if (url.includes('/anomalies/signals')) return jsonResponse(opts?.signals ?? [])
    if (url.includes('/activity/projects/')) return jsonResponse(opts?.activity ?? [])
    if (url.includes('/data-sources')) return jsonResponse(opts?.sources ?? [])
    if (url.includes('/source-freshness')) return jsonResponse(opts?.freshness ?? [])
    // Plan health (F15, #268): a project with nothing scored yet.
    if (url.includes('/projects/demo/health')) {
      return jsonResponse({
        score: null,
        grade: null,
        scored_events: 0,
        healthy_count: 0,
        warning_count: 0,
        unhealthy_count: 0,
        component_averages: [],
        worst: [],
        trend: [],
        previous_score: null,
        computed_at: '2026-01-01T00:00:00Z',
      })
    }
    if (url.endsWith('/projects/demo')) {
      return jsonResponse({ ...PROJECT, summary: { ...PROJECT.summary, ...opts?.summary } })
    }
    throw new Error(`Unhandled fetch: ${url}`)
  })
}

// OverviewPage renders OnboardingChecklist, which reads the current user via
// useAuth(); provide an AuthContext so that hook doesn't throw. An owner user
// keeps the checklist's owner-only steps in their normal (non-gated) state.
const AUTH_VALUE: AuthContextValue = {
  user: {
    id: 'user-1',
    email: 'owner@example.com',
    name: 'Owner',
    role: 'owner',
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

function renderOverview(path = '/p/demo/overview') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={AUTH_VALUE}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route path="/p/:slug/overview" element={<OverviewPage />} />
            </Routes>
          </MemoryRouter>
        </ThemeProvider>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('OverviewPage', () => {
  it('names the nav group in the eyebrow, never the project', async () => {
    mockFetch()
    const { container } = renderOverview()

    await screen.findByRole('heading', { level: 1, name: 'Overview' })
    const eyebrow = container.querySelector('[data-slot="page-eyebrow"]')
    expect(eyebrow).toHaveTextContent('Observe')
    expect(eyebrow).not.toHaveTextContent('Demo')
  })

  it('renders the KPI strip with active events and plan coverage', async () => {
    mockFetch()
    renderOverview()

    expect(await screen.findByRole('heading', { level: 1, name: 'Overview' })).toBeInTheDocument()
    expect(screen.getByText('Active events')).toBeInTheDocument()

    // active_event_count = 323 → "323"
    expect(await screen.findByText('323')).toBeInTheDocument()
    // plan coverage 320/323 → "99.1%", identical to the projects dashboard
    expect(await screen.findByText('99.1%')).toBeInTheDocument()
    // Named as on the Coverage page the tile opens.
    expect(screen.getByText('Plan coverage')).toBeInTheDocument()
  })

  it('names the scan the volume card charts, not "project total"', async () => {
    // The card plots ONE scan. Labelled "Volume · project total" it read
    // as the project's whole volume while showing 2.4 % of it — with a "Top
    // events · 48h" row 12x larger directly beneath.
    mockFetch()
    renderOverview()

    expect(
      await screen.findByText('Volume · Snowplow Pageviews (iOS) · 7d'),
    ).toBeInTheDocument()
    expect(screen.queryByText('Volume · project total')).not.toBeInTheDocument()
    expect(
      screen.getByText('This scan only — Top events below counts every scan.'),
    ).toBeInTheDocument()
    // The headline is the last 24 hours (10 + 25), not the newest bucket.
    expect(await screen.findByText('35')).toBeInTheDocument()
    expect(screen.getByText('last 24h')).toBeInTheDocument()
  })

  it('heads the volume card with the last 24h against the day before, on a dated axis', async () => {
    // "6,556 · latest bucket · 167 buckets" was one partial hour and internal
    // vocabulary; the flagged bucket behind the Open signals KPI was invisible.
    mockFetch({
      volumeData: [
        { bucket: hoursAgo(40), count: 100, expected_count: null },
        { bucket: hoursAgo(30), count: 100, expected_count: null },
        { bucket: hoursAgo(5), count: 150, expected_count: null, is_anomaly: true },
        { bucket: hoursAgo(1), count: 100, expected_count: null },
      ],
    })
    const { container } = renderOverview()

    expect(
      await screen.findByRole('group', {
        name: 'Volume in the last 24 hours: 250, +25% against the 24 hours before',
      }),
    ).toBeInTheDocument()
    // The change says what it compares, in words rather than a hover title.
    expect(screen.getByText('+25% vs prior 24h')).toBeInTheDocument()
    expect(screen.queryByText(/latest bucket/)).not.toBeInTheDocument()
    expect(screen.queryByText(/buckets$/)).not.toBeInTheDocument()
    // The axis ends at "now" at the series' cadence ("hour" is not a scan
    // interval code, so only "now" is printed for this fixture).
    expect(screen.getByText('now')).toBeInTheDocument()
    // The flagged bucket gets the sparkline's anomaly marker.
    expect(container.querySelector('circle[fill="var(--danger)"]')).not.toBeNull()
    // And the card opens the drilldown it summarises.
    expect(screen.getByRole('link', { name: 'Open chart' })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/project-total/scan-1',
    )
  })

  it('bounds the volume request to a 7-day window', async () => {
    // The call passed no params at all, so the endpoint summed the scan's whole
    // metric history — and the panel was still fetching 2.2s after the KPI strip,
    // Top events, Active signals and Recent activity had all rendered.
    const fetchSpy = mockFetch()
    renderOverview()

    expect(await screen.findByText('Volume · Snowplow Pageviews (iOS) · 7d')).toBeInTheDocument()

    const totalUrl = fetchSpy.mock.calls
      .map(([input]) => String(input))
      .find((url) => url.includes('/metrics/total'))
    expect(totalUrl).toBeDefined()
    const params = new URL(totalUrl!, 'http://localhost').searchParams
    const from = Date.parse(params.get('from') ?? '')
    const to = Date.parse(params.get('to') ?? '')
    expect(Number.isNaN(from)).toBe(false)
    expect(Number.isNaN(to)).toBe(false)
    expect(to - from).toBe(7 * 24 * 60 * 60 * 1000)
  })

  it('reserves the volume card with a skeleton while it loads', async () => {
    // On all three production projects the whole rest of the page was rendered
    // while this card read a bare "Loading…" in an empty box — pending, but
    // reading as broken, and growing a subtitle line when the series landed.
    mockFetch({ holdVolume: true })
    renderOverview()

    // The rest of the page has settled...
    expect(await screen.findByText('No active monitoring signals.')).toBeInTheDocument()
    expect(await screen.findByText('No recent activity.')).toBeInTheDocument()

    // ...while the volume card holds its shape instead of a bare string.
    const volumeCard = (await screen.findByText('Volume · 7d')).closest('section')
    expect(volumeCard).not.toBeNull()
    expect(
      (volumeCard as HTMLElement).querySelectorAll('[data-slot="skeleton"]').length,
    ).toBeGreaterThan(0)
    expect(screen.queryByText('Loading…')).not.toBeInTheDocument()
    // ...and never claims emptiness before the answer arrives.
    expect(screen.queryByText('No volume data yet.')).not.toBeInTheDocument()
    // The skeleton blocks are aria-hidden, so the pending state is still spoken.
    expect(screen.getByText('Loading volume…')).toBeInTheDocument()
    // The header keeps its second line rather than growing one on arrival.
    expect(
      screen.getByText('This scan only — Top events below counts every scan.'),
    ).toBeInTheDocument()
  })

  it('falls back to an unnamed volume title when no scan is resolved', async () => {
    mockFetch({ scanConfigName: null })
    renderOverview()

    expect(await screen.findByText('Volume · 7d')).toBeInTheDocument()
    expect(screen.queryByText('Volume · project total')).not.toBeInTheDocument()
  })

  it('names the window on the volume card, and blames the window when it is empty', async () => {
    // The card is capped at 7 days but said so nowhere, so a scan that stopped
    // more than a week ago — the state the failing-scan chip exists to surface —
    // rendered "No volume data yet." over a project with months of buckets.
    mockFetch({ volumeData: [] })
    renderOverview()

    expect(await screen.findByText(/No volume in the last 7 days\./)).toBeInTheDocument()
    expect(screen.queryByText('No volume data yet.')).not.toBeInTheDocument()
    // The drilldown carries a range selector, so it can show what the window hides.
    expect(screen.getByRole('link', { name: /See this scan’s full history/ })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/project-total/scan-1',
    )
  })

  it('still says "yet" when the project has no scan config at all', async () => {
    // The one case where nothing has ever been collected: the endpoint returns a
    // bare empty series with no scan id, so no window can be blamed.
    mockFetch({ volumeData: [], scanConfigId: null, scanConfigName: null })
    renderOverview()

    expect(await screen.findByText('No volume data yet.')).toBeInTheDocument()
    expect(screen.queryByText(/No volume in the last 7 days/)).not.toBeInTheDocument()
  })

  it('says the top-events panel spans every scan', async () => {
    // Top events sums ALL scans while the volume card above charts one, so an
    // event's 48h count can exceed the card's total. Saying so is what keeps
    // the two panels from reading as a contradiction.
    mockFetch()
    renderOverview()

    expect(
      await screen.findByText('Across every scan in this project.'),
    ).toBeInTheDocument()
  })

  it('gives top-event labels room to stay distinct', async () => {
    // The three biggest events on acme-ios share a 21-character prefix, so a
    // fixed 10rem label column truncated all three to the identical string
    // "feature_flag:flag_use…" and the ranking became unreadable.
    const topEvents = [
      { event_id: 'e1', name: 'feature_flag:flag_use:app', total_count: 26514863 },
      { event_id: 'e2', name: 'feature_flag:flag_use:growthbook', total_count: 24077526 },
      { event_id: 'e3', name: 'feature_flag:flag_use:server', total_count: 2788812 },
    ]
    mockFetch({ topEvents })
    renderOverview()

    const label = await screen.findByText('feature_flag:flag_use:growthbook')
    // The column tracks the panel width instead of being pinned at w-40, so a
    // wide panel shows the whole name — capped narrower than before so the bar
    // no longer starts mid-card, and full width (name above bar) on a phone.
    expect(label.className).not.toMatch(/\bw-40\b/)
    expect(label.className).toContain('sm:w-[min(40%,16rem)]')
    expect(label.className).not.toContain('mono')
    // Every row opens the event's monitoring page.
    expect(label.closest('a')).toHaveAttribute('href', '/p/demo/monitoring/event/e2')

    // Every name still renders in full in the DOM (and in the row's a11y name),
    // so nothing depends on hovering to tell the bars apart.
    for (const e of topEvents) {
      expect(
        screen.getByRole('listitem', { name: `${e.name}: ${formatNumber(e.total_count)} events` }),
      ).toBeInTheDocument()
    }
  })

  it("shows each top event's share of the project's volume", async () => {
    mockFetch({
      topEvents: [
        { event_id: 'e1', name: 'app_open', total_count: 600, window_total_count: 1000 },
        { event_id: 'e2', name: 'tap', total_count: 50, window_total_count: 1000 },
      ],
    })
    renderOverview()

    expect(await screen.findByRole('listitem', { name: 'app_open: 600 events, 60% of the total' })).toHaveTextContent('60%')
    expect(screen.getByRole('listitem', { name: 'tap: 50 events, 5% of the total' })).toBeInTheDocument()
  })

  it('captions the KPI sparkline as new events, not active events', async () => {
    // The series is COUNT(Event) grouped by created_at. Captioned "Active trend"
    // beside "ACTIVE EVENTS 2,413" it announced days of 4,618 — more active
    // events in one day than the project has in total — and the sr-only text
    // stated that falsehood outright.
    mockFetch({ kpiSeries: [1, 2, 3, 4] })
    renderOverview()

    expect(await screen.findByText('New events · 14d')).toBeInTheDocument()
    expect(screen.queryByText('Active trend · 14d')).not.toBeInTheDocument()
    expect(screen.getByText(/New events added by day: 1, 2, 3, 4\./)).toBeInTheDocument()
    expect(screen.queryByText(/Active events by day/)).not.toBeInTheDocument()
    expect(
      screen.getByRole('img', { name: /^New events added per day over the last 14 days\./ }),
    ).toBeInTheDocument()
  })

  it('keeps the open-signals headline in agreement with the signals panel', async () => {
    // monitoring_signal_count is 5, but the canonical signals array is empty.
    mockFetch()
    renderOverview()

    expect(await screen.findByText('Open signals')).toBeInTheDocument()
    // The panel renders no rows...
    expect(await screen.findByText('No active monitoring signals.')).toBeInTheDocument()
    // ...so the headline must read 0 — never the superset monitoring_signal_count (5).
    // Widgets now wait for the project query to confirm the slug exists (the .9
    // 404 short-circuit), so the signals stat resolves a tick after the panel —
    // await it rather than reading synchronously.
    expect(await screen.findByText('0')).toBeInTheDocument()
    expect(screen.queryByText('5')).not.toBeInTheDocument()
  })

  it('renders a friendly message for failed-scan activity, hiding raw internals', async () => {
    const rawError =
      "HTTPSConnectionPool(host='clickhouse.internal', port=8443): Read timed out. (read timeout=30)"
    mockFetch({
      activity: [
        {
          id: 'a1',
          project_id: 'project-1',
          project_slug: 'demo',
          project_name: 'Demo',
          type: 'scan',
          severity: 'high',
          title: 'Scan failed: Nightly metrics',
          detail: rawError,
          occurred_at: '2026-06-25T10:00:00Z',
          target_path: null,
        },
      ],
    })
    renderOverview()

    expect(
      await screen.findByText('Scan failed: the data source did not respond in time.'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/HTTPSConnectionPool/)).not.toBeInTheDocument()
    expect(screen.queryByText(/clickhouse\.internal/)).not.toBeInTheDocument()
    // The activity title carries its full text as a native tooltip so a long
    // event reference stays readable when the row ellipsizes.
    expect(screen.getByText('Scan failed: Nightly metrics')).toHaveAttribute(
      'title',
      'Scan failed: Nightly metrics',
    )
  })

  // The card renders the activity rail's rows: a scan-generated event reads by
  // its values, as in the rail, not by its raw key=value signature.
  it('names a scan-generated event in Recent activity as the rail does', async () => {
    mockFetch({
      activity: [
        {
          id: 'event:e1',
          project_id: 'project-1',
          project_slug: 'demo',
          project_name: 'Demo',
          type: 'event',
          severity: 'low',
          title: 'Event created: event_name=Home Screen View | screen_name=Home',
          detail: 'screen_view',
          occurred_at: '2026-06-25T10:00:00Z',
          target_path: '/p/demo/events/e1',
        },
      ],
    })
    renderOverview()

    const row = await screen.findByRole('link', { name: /Event created: Home Screen View · Home/ })
    expect(row).toHaveAttribute('href', '/p/demo/events/e1')
    expect(screen.queryByText(/event_name=/)).not.toBeInTheDocument()
  })

  it('short-circuits to the full-page not-found on a project 404', async () => {
    // A nonexistent slug 404s the project query; the whole widget grid is
    // replaced by NotFoundPage and the project-scoped widgets never fire.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/data-sources')) return jsonResponse([])
      if (url.includes('/projects/does-not-exist')) {
        return new Response(JSON.stringify({ detail: 'Project not found' }), {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderOverview('/p/does-not-exist/overview')

    expect(await screen.findByText('Page not found')).toBeInTheDocument()
    // The KPI strip / widgets must not render alongside the not-found page.
    expect(screen.queryByRole('heading', { level: 1, name: 'Overview' })).not.toBeInTheDocument()
  })

  it('labels a metric-scope signal with the name the server resolved', async () => {
    mockFetch({
      signals: [
        {
          scan_config_id: 'scan-1',
          scope_type: 'metric',
          scope_ref: 'metric-abc',
          scope_name: 'Checkout conversion',
          state: 'latest_scan',
          event_id: null,
          event_type_id: null,
          bucket: '2026-07-01T00:00:00Z',
          actual_count: 40,
          expected_count: 100,
          stddev: 5,
          z_score: -6,
          direction: 'drop',
        },
      ],
    })
    renderOverview()

    // "Metric · <display name>" — never the raw "Event <uuid8>" the old copy
    // produced for every scope it did not handle.
    expect(await screen.findByText('Drop on Metric · Checkout conversion')).toBeInTheDocument()
    expect(screen.queryByText(/Event metric-a/)).not.toBeInTheDocument()
  })

  it('labels event- and event-type-scope signals with their names, not a raw UUID', async () => {
    mockFetch({
      signals: [
        {
          scan_config_id: 'scan-1',
          scope_type: 'event',
          scope_ref: 'd78ddc27-4b1e-4a0c-9f77-2c9f0f2a51bd',
          scope_name: 'map:open:spot',
          state: 'latest_scan',
          event_id: 'd78ddc27-4b1e-4a0c-9f77-2c9f0f2a51bd',
          event_type_id: null,
          bucket: '2026-07-01T00:00:00Z',
          actual_count: 300,
          expected_count: 100,
          stddev: 12,
          z_score: 8,
          direction: 'spike',
        },
        {
          scan_config_id: 'scan-1',
          scope_type: 'event_type',
          scope_ref: '5f2b91aa-0d13-4c7e-9a26-1d8e3b7f42c1',
          scope_name: 'Map',
          state: 'latest_scan',
          event_id: null,
          event_type_id: '5f2b91aa-0d13-4c7e-9a26-1d8e3b7f42c1',
          bucket: '2026-07-01T00:00:00Z',
          actual_count: 20,
          expected_count: 100,
          stddev: 9,
          z_score: -7,
          direction: 'drop',
        },
      ],
    })
    renderOverview()

    // The bug: Overview rendered "Spike on Event d78ddc27" while the Anomalies
    // page showed the real name for the very same signal.
    expect(await screen.findByText('Spike on Event · map:open:spot')).toBeInTheDocument()
    expect(screen.queryByText(/Event d78ddc27/)).not.toBeInTheDocument()
    expect(await screen.findByText('Drop on Event type · Map')).toBeInTheDocument()
    expect(screen.queryByText(/Event type 5f2b91aa/)).not.toBeInTheDocument()
  })

  it('asks for no name catalog at all, because the names ride on the signals', async () => {
    // The panel fired one GET per rendered event id, plus the event-type list and
    // the metrics catalog, and showed uuid stubs on the default landing route
    // until they landed. It had already dropped a GET /events?limit=10000 (2,413
    // rows / 2.7 MB / ~3 s) for the per-id fan-out; this removes
    // the fan-out too.
    const fetchSpy = mockFetch({
      signals: [makeEventSignal('d78ddc27-4b1e-4a0c-9f77-2c9f0f2a51bd')],
    })
    renderOverview()

    expect(await screen.findByText('Spike on Event · map:open:spot')).toBeInTheDocument()
    const nameCalls = fetchSpy.mock.calls
      .map(([input]) => String(input))
      .filter((url) => /\/events(\/|\?|$)|\/event-types|\/metrics(\?|$)/.test(url))
    expect(nameCalls).toEqual([])
  })

  it('says an unnameable scope is gone instead of printing its ref', async () => {
    // scope_name null is terminal — the event was deleted out from under the
    // row, the FKs being ondelete=SET NULL — so the row says so in words and the
    // ref stays in the tooltip, where it cannot be misread as a name.
    mockFetch({
      signals: [
        {
          scan_config_id: 'scan-1',
          scope_type: 'event',
          scope_ref: 'd78ddc27-4b1e-4a0c-9f77-2c9f0f2a51bd',
          scope_name: null,
          state: 'latest_scan',
          event_id: 'd78ddc27-4b1e-4a0c-9f77-2c9f0f2a51bd',
          event_type_id: null,
          bucket: '2026-07-01T00:00:00Z',
          actual_count: 300,
          expected_count: 100,
          stddev: 12,
          z_score: 8,
          direction: 'spike',
        },
      ],
    })
    renderOverview()

    const row = await screen.findByText('Spike on deleted event')
    expect(row).toHaveAttribute('title', 'Spike on Event d78ddc27')
    expect(screen.queryByText(/d78ddc27/)).not.toBeInTheDocument()
  })

  it('keeps a sub-unit baseline instead of rounding it away', async () => {
    // A `%` catalog metric stores a fraction (0.08 == 8%) and `metric` is a
    // first-class scope here, so Math.round put "1.2 vs 0" on the row while the
    // severity beside it was computed from 0.4.
    mockFetch({
      signals: [
        {
          scan_config_id: 'scan-1',
          scope_type: 'metric',
          scope_ref: 'metric-abc',
          scope_name: 'Checkout conversion',
          state: 'latest_scan',
          event_id: null,
          event_type_id: null,
          bucket: '2026-07-01T00:00:00Z',
          actual_count: 1.2,
          expected_count: 0.4,
          stddev: 0.1,
          z_score: 6,
          direction: 'spike',
        },
      ],
    })
    renderOverview()

    expect(await screen.findByText('1.2 vs 0.4 expected')).toBeInTheDocument()
  })

  it('labels a drop-to-zero signal as "dropped to zero", not the clamped z-score', async () => {
    mockFetch({
      signals: [
        {
          scan_config_id: 'scan-1',
          scope_type: 'metric',
          scope_ref: 'metric-abc',
          scope_name: 'Checkout conversion',
          state: 'latest_scan',
          event_id: null,
          event_type_id: null,
          bucket: '2026-07-01T00:00:00Z',
          actual_count: 0,
          expected_count: 80,
          stddev: 5,
          z_score: -20,
          direction: 'drop',
        },
      ],
    })
    renderOverview()

    const row = (await screen.findByText('Drop on Metric · Checkout conversion')).closest('a')
    expect(row).not.toBeNull()
    expect(row).toHaveTextContent('dropped to zero')
    // The repeated, clamped "z=-20.0" must not be surfaced for zeroed drops.
    expect(row).not.toHaveTextContent('z=-20')
  })

  it('scopes the source-health rail to this project plus global sources', async () => {
    mockFetch({
      sources: [
        makeSource({ id: 's-global', name: 'Shared warehouse', project_id: null }),
        makeSource({ id: 's-mine', name: 'Demo synthetic', project_id: 'project-1' }),
        makeSource({ id: 's-other', name: 'Other project source', project_id: 'project-2' }),
      ],
    })
    renderOverview()

    expect(await screen.findByText('Shared warehouse')).toBeInTheDocument()
    expect(screen.getByText('Demo synthetic')).toBeInTheDocument()
    // A source owned by a different project must not leak into this rail.
    expect(screen.queryByText('Other project source')).not.toBeInTheDocument()
  })
})

describe('OverviewPage — active signals panel', () => {
  it('links to the full Anomalies list, with the count, from the capped panel', async () => {
    mockFetch({
      signals: Array.from({ length: 8 }, (_, index) => ({
        ...makeEventSignal(`event-${index}`),
        scope_name: `event_${index}`,
      })),
    })
    renderOverview()

    const link = await screen.findByRole('link', { name: 'View all (8)' })
    expect(link).toHaveAttribute('href', '/p/demo/anomalies')
  })

  it('renders both signals when two scans flag the same event on the same bucket', async () => {
    mockFetch({
      signals: [
        { ...makeEventSignal('ev-1'), scan_config_id: 'scan-legacy' },
        { ...makeEventSignal('ev-1'), scan_config_id: 'scan-live' },
      ],
    })
    renderOverview()

    // A duplicate key is a React console.error, which the test setup fails on.
    expect(await screen.findAllByText('Spike on Event · map:open:spot')).toHaveLength(2)
  })

  it('formats a metric signal in its unit when the server sends one', async () => {
    mockFetch({
      signals: [
        {
          ...makeEventSignal('metric-abc'),
          scope_type: 'metric',
          scope_ref: 'metric-abc',
          scope_name: 'Checkout conversion',
          event_id: null,
          actual_count: 0.043,
          expected_count: 0.12,
          relative_effect: 0.64,
          direction: 'drop',
          unit: '%',
        },
      ],
    })
    renderOverview()

    const row = await screen.findByRole('link', { name: /Drop on Metric · Checkout conversion/ })
    expect(row).toHaveTextContent('4.3% vs 12%')
  })
})

describe('OverviewPage — beside the activity rail', () => {
  function renderWithRail({ inline = true }: { inline?: boolean } = {}) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={AUTH_VALUE}>
          <ThemeProvider>
            <MemoryRouter initialEntries={['/p/demo/overview']}>
              <Routes>
                <Route
                  path="/p/:slug/overview"
                  element={
                    <>
                      <OverviewPage />
                      {/* Layout marks the rail it renders in flow as inline. */}
                      <ActivityPanel open slug="demo" inline={inline} />
                    </>
                  }
                />
              </Routes>
            </MemoryRouter>
          </ThemeProvider>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )
  }

  it('drops its own Recent activity panel while the rail shows the same feed inline', async () => {
    mockFetch()
    renderWithRail()

    const rail = await screen.findByRole('complementary', { name: 'Activity feed' })
    // The rail is "Activity"; "Recent activity" is only Overview's card.
    expect(within(rail).getByText('Activity')).toBeInTheDocument()
    await screen.findByRole('heading', { level: 1, name: 'Overview' })
    // The feed is in the rail, and not a second time in the page body.
    expect(screen.queryByText('Recent activity')).not.toBeInTheDocument()
  })

  it('keeps its panel behind the drawer, which covers the page rather than sitting beside it', async () => {
    mockFetch()
    renderWithRail({ inline: false })

    await screen.findByRole('heading', { level: 1, name: 'Overview' })
    // The page keeps its own card (the drawer's title is "Activity").
    await waitFor(() => expect(screen.getAllByText('Recent activity')).toHaveLength(1))
  })

  it('keeps the panel when the rail is closed', async () => {
    mockFetch()
    renderOverview()

    expect(await screen.findByText('Recent activity')).toBeInTheDocument()
  })
})

function makeEventSignal(scopeRef: string) {
  return {
    scan_config_id: 'scan-1',
    scope_type: 'event',
    scope_ref: scopeRef,
    scope_name: 'map:open:spot',
    state: 'latest_scan',
    event_id: scopeRef,
    event_type_id: null,
    bucket: '2026-07-01T00:00:00Z',
    actual_count: 300,
    expected_count: 100,
    stddev: 12,
    z_score: 8,
    direction: 'spike',
  }
}

function makeSource(overrides: {
  id: string
  name: string
  project_id: string | null
}) {
  return {
    db_type: 'clickhouse',
    is_synthetic: false,
    host: 'h',
    port: 8123,
    database_name: 'db',
    username: 'u',
    password_set: false,
    timeout_seconds: null,
    json_path_discovery: null,
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
    last_test_at: '2026-07-01T00:00:00Z',
    last_test_status: 'success',
    last_test_message: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

describe('OverviewPage — design review follow-ups', () => {
  it('links the KPI tiles where the work is, under one review name', async () => {
    mockFetch()
    renderOverview()

    // "In review", the status name used everywhere, not "Needs review".
    const review = await screen.findByRole('link', { name: /^In review/ })
    expect(review).toHaveAttribute('href', '/p/demo/events/review')
    expect(screen.queryByText('Needs review')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /^Open signals/ })).toHaveAttribute(
      'href',
      '/p/demo/anomalies',
    )
    expect(screen.getByRole('link', { name: /^Plan coverage/ })).toHaveAttribute(
      'href',
      '/p/demo/coverage',
    )
  })

  it('keeps the KPI figures neutral unless they are an exception', async () => {
    mockFetch()
    renderOverview()

    await screen.findByText('323')
    // "Implemented" used to be green whatever the ratio, and a zero signal
    // count green too — praise for nothing.
    const implemented = screen.getByText('Implemented').closest('dl') as HTMLElement
    expect(within(implemented).getByText('320')).not.toHaveAttribute('data-tone')
    const signals = screen.getByText('Open signals').closest('dl') as HTMLElement
    expect(await within(signals).findByText('0')).not.toHaveAttribute('data-tone')
  })

  it('answers "is everything OK?" in one line under the title', async () => {
    mockFetch({
      signals: [makeEventSignal('ev-1')],
      summary: {
        open_incident_count: 2,
        failing_scan_config_count: 1,
        failing_alert_destination_count: 1,
      },
    })
    renderOverview()

    // "Open signals", the KPI's word for the same count, not "open anomalies".
    const signals = await screen.findByRole('link', { name: '1 open signal' })
    expect(signals).toHaveAttribute('href', '/p/demo/anomalies')
    expect(screen.getByRole('link', { name: '2 open incidents' })).toHaveAttribute(
      'href',
      '/p/demo/alerting',
    )
    expect(screen.getByRole('link', { name: '1 failing scan' })).toHaveAttribute(
      'href',
      '/p/demo/scans',
    )
    // A broken channel is named beside the failing scans, linking to Alerting.
    expect(screen.getByRole('link', { name: '1 broken alert channel' })).toHaveAttribute(
      'href',
      '/p/demo/alerting',
    )
  })

  it('shows a signal as a % change with the z-score on hover, and does not pulse it', async () => {
    // 300 vs 100 expected: +200%.
    mockFetch({ signals: [makeEventSignal('ev-1')] })
    const { container } = renderOverview()

    const row = await screen.findByRole('link', { name: /Spike on Event · map:open:spot/ })
    const change = within(row).getByText('+200%')
    expect(change).toHaveAttribute('title', 'Major · z=8.0')
    expect(row).not.toHaveTextContent('z=8.0')
    expect(row.querySelector('.pulse-dot')).toBeNull()
    // The Open signals KPI keeps its pulse; the list rows do not.
    expect(
      container.querySelectorAll('[data-slot="mini-stat-strip"] .pulse-dot'),
    ).toHaveLength(1)
    // Its word for screen readers is the signal's own, "open", not the
    // "active" of the events tile beside it.
    const kpi = screen.getByText('Open signals').closest('dl') as HTMLElement
    expect(within(kpi).getByText('open')).toHaveClass('sr-only')
    expect(kpi).not.toHaveTextContent(/active/)
  })

  it('shows one empty state instead of five empty panels in a blank project', async () => {
    mockFetch({
      summary: { active_event_count: 0, implemented_event_count: 0, review_pending_event_count: 0 },
      sources: [],
    })
    renderOverview()

    expect(
      await screen.findByRole('heading', { name: 'Overview fills in after your first scan' }),
    ).toBeInTheDocument()
    // One way to connect a source: the getting-started checklist's first step.
    // The empty state under it says why the page is empty without a second
    // "Connect a data source" button.
    const steps = screen.getByRole('list', { name: 'Setup steps' })
    expect(within(steps).getByRole('link', { name: /Connect a data source/ })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Connect a data source' })).not.toBeInTheDocument()
    expect(screen.queryByText('Top events · 48h')).not.toBeInTheDocument()
    expect(screen.queryByText('Source health')).not.toBeInTheDocument()
    expect(screen.queryByText('Active signals')).not.toBeInTheDocument()
  })

  it('offers the empty state its own Connect button once the checklist is dismissed', async () => {
    setOnboardingDismissed('demo', 'project-1', true)
    try {
      mockFetch({
        summary: { active_event_count: 0, implemented_event_count: 0, review_pending_event_count: 0 },
        sources: [],
      })
      renderOverview()

      // Owners can act on it straight away.
      expect(await screen.findByRole('link', { name: 'Connect a data source' })).toHaveAttribute(
        'href',
        '/settings/data-sources',
      )
      expect(screen.queryByRole('list', { name: 'Setup steps' })).not.toBeInTheDocument()
    } finally {
      // Unmount first: the reset notifies every mounted reader of the flag,
      // and a re-render after the test is an update outside act().
      cleanup()
      setOnboardingDismissed('demo', 'project-1', false)
    }
  })

  it('offers the Connect button the moment the checklist is dismissed on the page', async () => {
    try {
      mockFetch({
        summary: { active_event_count: 0, implemented_event_count: 0, review_pending_event_count: 0 },
        sources: [],
      })
      renderOverview()

      await screen.findByRole('list', { name: 'Setup steps' })
      expect(screen.queryByRole('link', { name: 'Connect a data source' })).not.toBeInTheDocument()

      // Nothing else re-renders the page here: the button has to follow the
      // dismissal itself, not wait for the next refetch.
      fireEvent.click(screen.getByRole('button', { name: 'Dismiss getting-started checklist' }))
      expect(await screen.findByRole('link', { name: 'Connect a data source' })).toHaveAttribute(
        'href',
        '/settings/data-sources',
      )
    } finally {
      // Unmount first: the reset notifies every mounted reader of the flag,
      // and a re-render after the test is an update outside act().
      cleanup()
      setOnboardingDismissed('demo', 'project-1', false)
    }
  })

  it('names a synthetic source once, with its status as a chip, and links the row', async () => {
    mockFetch({
      sources: [
        makeSource({ id: 's-demo', name: 'Demo warehouse', project_id: null }),
        {
          ...makeSource({ id: 's-syn', name: 'Synthetic warehouse', project_id: null }),
          db_type: 'synthetic',
          is_synthetic: true,
        },
      ],
    })
    renderOverview()

    const synthetic = (await screen.findByText('Synthetic warehouse')).closest('a') as HTMLElement
    expect(synthetic).toHaveAttribute('href', '/settings/data-sources/s-syn')
    // The badge says "Synthetic"; the engine name no longer repeats it.
    expect(within(synthetic).queryByText('synthetic')).not.toBeInTheDocument()
    expect(within(synthetic).getByText('Synthetic')).toBeInTheDocument()
    // A real engine still shows, since it adds information, by the name the
    // Data sources page gives it rather than the raw type key.
    const real = screen.getByText('Demo warehouse').closest('a') as HTMLElement
    expect(within(real).getByText('ClickHouse')).toBeInTheDocument()
    expect(within(real).queryByText('clickhouse')).not.toBeInTheDocument()
    // The status is a toned chip (the check is months old here, so "Stale").
    expect(
      within(real).getByText('Stale').closest('[data-slot="chip"]'),
    ).toHaveAttribute('data-tone', 'warning')
  })
})

describe('OverviewPage — source freshness in Source health (F16, #269)', () => {
  it('flags the late source and names the late scan', async () => {
    mockFetch({
      sources: [makeSource({ id: 's-1', name: 'Events warehouse', project_id: null })],
      freshness: [
        {
          id: 'scan-1',
          name: 'App events',
          data_source_id: 's-1',
          freshness: {
            status: 'late',
            lag_seconds: 7 * 3600,
            last_event_at: hoursAgo(7),
            last_collection_at: hoursAgo(0),
            expected_by: hoursAgo(4),
          },
        },
      ],
    })
    renderOverview()

    const row = (await screen.findByText('Events warehouse')).closest('a') as HTMLElement
    expect(await within(row).findByText('Data late · 7h')).toHaveAttribute('data-tone', 'warning')
    const late = screen.getByRole('list', { name: 'Late or overdue scans' })
    expect(within(late).getByRole('link', { name: 'App events' })).toHaveAttribute('href', '/p/demo/scans/scan-1')
  })
})
