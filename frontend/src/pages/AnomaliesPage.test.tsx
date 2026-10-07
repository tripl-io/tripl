import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation, useParams } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MonitoringSignal, Project, ScanConfig } from '@/types'
import { DemoScenarioProvider } from '@/demo/DemoScenarioProvider'
import { writeScenarioState } from '@/demo/scenarioModel'
import { chapterState } from '@/demo/scenarioTestState'
import AnomaliesPage from './AnomaliesPage'
import { PageHeader } from '@/components/primitives/page-header'

vi.mock('@/api/eventMetrics', () => ({
  eventMetricsApi: { getActiveSignals: vi.fn(), getSignalSeries: vi.fn() },
}))
// Kept mocked although the page no longer imports it: the point is
// that this catalog download (limit 10_000 — 2641 rows / 1.7s on acme-ios) must
// never come back as a way to label rows, and the only way to assert an absent
// request is to hold a spy that stays at zero calls.
vi.mock('@/api/events', () => ({
  eventsApi: { list: vi.fn() },
}))
vi.mock('@/api/scans', () => ({
  scansApi: { list: vi.fn() },
}))
vi.mock('@/api/sourceFreshness', () => ({
  sourceFreshnessApi: { list: vi.fn() },
}))

import { eventMetricsApi } from '@/api/eventMetrics'
import { eventsApi } from '@/api/events'
import { scansApi } from '@/api/scans'
import { sourceFreshnessApi } from '@/api/sourceFreshness'

/**
 * Matches a row's scope label by its full text. The "Spike on" / "Drop on"
 * prefix is its own element (visually hidden on phones), so the default
 * text matcher, which reads only an element's own text nodes, no longer sees
 * "Spike on Event · Signup" as one string.
 */
function rowLabel(text: string | RegExp) {
  return (_content: string, element: Element | null) => {
    if (!element?.hasAttribute('data-anomaly-label')) return false
    const full = element.textContent ?? ''
    return typeof text === 'string' ? full === text : text.test(full)
  }
}

function makeSignal(overrides: Partial<MonitoringSignal>): MonitoringSignal {
  return {
    scan_config_id: 'scan-1',
    scope_type: 'metric',
    scope_ref: '9136d575-0000-4000-8000-000000000001',
    state: 'latest_scan',
    event_id: null,
    event_type_id: null,
    bucket: '2026-07-01T00:00:00Z',
    // Default relative effect = |120 − 80| / 80 = 0.5, which clears the default
    // "Significant" (≥0.5) filter — so a plain makeSignal() is always visible.
    actual_count: 120,
    expected_count: 80,
    stddev: 5,
    z_score: 8,
    direction: 'spike',
    // Resolved server-side alongside the signal; null means the server could not
    // name the scope (deleted entity), never "still loading".
    scope_name: null,
    incident_child: false,
    unit: null,
    detected_at: null,
    ...overrides,
  }
}

// Only `id` + `name` feed the scan id → name map behind the scan facet.
function makeScans(items: Array<{ id: string; name: string }>): ScanConfig[] {
  return items as unknown as ScanConfig[]
}

/** Probe target for the metric drilldown route the row should navigate to. */
function MetricDetailProbe() {
  const { metricId } = useParams<{ metricId: string }>()
  return <div>metric-detail:{metricId}</div>
}

/** Exposes the live URL so the `?scan=` / `?level=` round trips are assertable. */
function LocationProbe() {
  const location = useLocation()
  return <div>anomalies-location:{location.pathname}{location.search}</div>
}

function renderAnomalies(entry = '/p/demo/anomalies') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <LocationProbe />
        <Routes>
          <Route path="/p/:slug/anomalies" element={<AnomaliesPage />} />
          <Route path="/p/:slug/monitoring/metric/:metricId" element={<MetricDetailProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

// The filters are FilterSelect chips: "Magnitude: Significant",
// "Scan: All scans 7". A chip names itself "<Label> filter: <value>", so the
// value is announced with it; these match the label part whatever is set.
const MAGNITUDE = /^Magnitude filter: /
const SCAN = /^Scan filter: /

function filterChip(name: RegExp): HTMLElement {
  return screen.getByRole('combobox', { name })
}

/** Opens a filter chip and picks one of its options by name. */
async function chooseFilter(name: RegExp, option: string | RegExp): Promise<void> {
  fireEvent.click(await screen.findByRole('combobox', { name }))
  fireEvent.click(await screen.findByRole('option', { name: option }))
}

/** Opens a filter chip and returns the names of its options, then closes it. */
async function filterOptions(name: RegExp): Promise<string[]> {
  fireEvent.click(await screen.findByRole('combobox', { name }))
  const options = (await screen.findAllByRole('option')).map((option) => option.textContent ?? '')
  fireEvent.keyDown(await screen.findByRole('listbox'), { key: 'Escape' })
  return options
}

beforeEach(() => {
  vi.mocked(eventMetricsApi.getActiveSignals).mockReset()
  vi.mocked(eventsApi.list).mockReset()
  vi.mocked(scansApi.list).mockReset()
  vi.mocked(scansApi.list).mockResolvedValue(makeScans([]))
  vi.mocked(eventMetricsApi.getSignalSeries).mockReset()
  vi.mocked(eventMetricsApi.getSignalSeries).mockResolvedValue([])
  vi.mocked(sourceFreshnessApi.list).mockReset()
  vi.mocked(sourceFreshnessApi.list).mockResolvedValue([])
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('AnomaliesPage — scope names', () => {
  it('renders a metric signal with the name the server resolved and links to the drilldown', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ scope_ref: 'metric-abc', scope_name: 'Checkout conversion' }),
    ])

    renderAnomalies()

    // Label reads "Metric · <display name>" straight off the signal.
    // A linkable row is a real link, so it opens in a new tab, and a
    // screen reader announces something it can follow.
    const link = await screen.findByRole('link', { name: 'Spike on Metric · Checkout conversion' })
    expect(link).toHaveAttribute('href', '/p/demo/monitoring/metric/metric-abc')
    expect(link.closest('[role="row"]')).not.toHaveAttribute('tabindex')
    fireEvent.click(link)
    expect(await screen.findByText('metric-detail:metric-abc')).toBeInTheDocument()
  })

  it('never labels a row with a bare scope ref when the server could not name it', async () => {
    // The metric was deleted out from under the anomaly row, so the server sends
    // scope_name: null. "Drop on Metric 9136d575" reads as a name and is what the
    // page used to show for every row for the first 4.4s — the whole bug.
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ scope_ref: '9136d575-0000-4000-8000-000000000001', direction: 'drop' }),
    ])

    renderAnomalies()

    const placeholder = await screen.findByRole('img', { name: 'Metric 9136d575' })
    const row = placeholder.closest('[role="row"]') as HTMLElement
    expect(row).not.toBeNull()
    expect(row).not.toHaveTextContent('9136d575')
    // Still a real, navigable row — the missing name costs the label, not the link.
    const link = within(row).getByRole('link')
    fireEvent.click(link)
    expect(
      await screen.findByText('metric-detail:9136d575-0000-4000-8000-000000000001'),
    ).toBeInTheDocument()
  })

  it('says an unnameable scope is gone, instead of shimmering at the operator forever', async () => {
    // scope_name null is terminal — the entity was deleted, never "still
    // loading". `animate-pulse` is this app's Skeleton and OverviewPage uses the
    // identical h-3 w-32 bar to mean "fetching", while the table here is already
    // gated on isLoading — so the shimmer made a permanent state read as a
    // pending one, and the operator waits and refreshes on a row that will never
    // change.
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ direction: 'drop' }),
      makeSignal({ scope_type: 'event', scope_ref: 'ev-9', event_id: null }),
    ])

    renderAnomalies()

    const metricScope = await screen.findByRole('img', { name: 'Metric 9136d575' })
    expect(metricScope.className).not.toContain('animate-pulse')
    // Readable at a glance and selectable, not hover-only.
    expect(metricScope).toHaveTextContent('deleted metric')
    expect(await screen.findByRole('img', { name: 'Event ev-9' })).toHaveTextContent(
      'deleted event',
    )
  })

  it('names event-type and event scopes from the signal payload', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ scope_type: 'event_type', scope_ref: 'et-1', scope_name: 'Signup' }),
      makeSignal({ scope_type: 'event', scope_ref: 'ev-1', scope_name: 'Checkout tapped' }),
    ])

    renderAnomalies()

    expect(await screen.findByText(rowLabel('Spike on Event type · Signup'))).toBeInTheDocument()
    expect(await screen.findByText(rowLabel('Spike on Event · Checkout tapped'))).toBeInTheDocument()
  })

  it('does not download the event catalog just to label rows', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ scope_type: 'event', scope_ref: 'ev-1', scope_name: 'Checkout tapped' }),
    ])

    renderAnomalies()

    await screen.findByText(rowLabel('Spike on Event · Checkout tapped'))
    expect(eventsApi.list).not.toHaveBeenCalled()
  })

  it('tags incident children folded under a project_total spike, but not the parent', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ scope_type: 'project_total', scope_ref: 'pt-1', incident_child: false }),
      makeSignal({
        scope_type: 'event_type',
        scope_ref: 'et-12345678',
        scope_name: 'Signup',
        incident_child: true,
      }),
    ])

    renderAnomalies()

    // Both scopes are listed (no collapse), and only the child carries the tag.
    const parentRow = (await screen.findByText(rowLabel('Spike on Project total'))).closest(
      '[role="row"]',
    ) as HTMLElement
    const childRow = screen
      .getByText(rowLabel('Spike on Event type · Signup'))
      .closest('[role="row"]') as HTMLElement
    // Worded as what it means, not the "part of total" annotation.
    expect(childRow).toHaveTextContent('within total spike')
    expect(childRow).not.toHaveTextContent('part of total')
    expect(parentRow).not.toHaveTextContent('within total spike')
  })

  it('says "within total drop" on a child folded under a project_total drop', async () => {
    // Children are keyed to parents by direction, so a drop child's parent is
    // a total drop; "within total spike" on it was wrong.
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({
        scope_type: 'event_type',
        scope_ref: 'et-12345678',
        scope_name: 'Signup',
        direction: 'drop',
        actual_count: 40,
        expected_count: 80,
        z_score: -8,
        incident_child: true,
      }),
    ])

    renderAnomalies()

    const row = (await screen.findByText(rowLabel('Drop on Event type · Signup'))).closest(
      '[role="row"]',
    ) as HTMLElement
    expect(row).toHaveTextContent('within total drop')
    expect(row).not.toHaveTextContent('within total spike')
  })

  it('keeps the direction prefix in the accessible name but hides it visually on phones', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ scope_ref: 'metric-abc', scope_name: 'Checkout conversion' }),
    ])

    renderAnomalies()

    const link = await screen.findByRole('link', { name: 'Spike on Metric · Checkout conversion' })
    // The arrow carries the direction below sm; the words return from sm up.
    const prefix = within(link).getByText('Spike on')
    expect(prefix).toHaveClass('sr-only', 'sm:not-sr-only')
  })
})

describe('AnomaliesPage — severity label', () => {
  it('shows "dropped to zero" instead of the clamped z-score for a drop-to-zero signal', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({
        scope_type: 'event_type',
        scope_ref: 'et-1',
        scope_name: 'Signup',
        direction: 'drop',
        actual_count: 0,
        expected_count: 80,
        z_score: -20,
      }),
    ])

    renderAnomalies()

    const row = (await screen.findByText(rowLabel('Drop on Event type · Signup'))).closest(
      '[role="row"]',
    ) as HTMLElement
    expect(row).toHaveTextContent('dropped to zero')
    // The low-information clamped z-score must not be surfaced.
    expect(row).not.toHaveTextContent('z=-20')
  })

  it('leads with the % change and keeps the z-score in the tooltip', async () => {
    // makeSignal() defaults to a spike with z_score 8, 120 actual vs 80
    // expected: +50%, exactly on the Significant bar.
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ scope_type: 'event_type', scope_ref: 'et-1', scope_name: 'Signup' }),
    ])

    renderAnomalies()

    const row = (await screen.findByText(rowLabel('Spike on Event type · Signup'))).closest(
      '[role="row"]',
    ) as HTMLElement
    const change = within(row).getByText('+50%')
    // The magnitude in words, on the filter's own bars, and the z-score for
    // whoever wants it — neither as the headline figure.
    expect(change).toHaveAttribute('title', 'Significant · z=8.0')
    expect(row).toHaveTextContent('Significant')
    expect(row).not.toHaveTextContent('z=8.0')
  })

  it('puts the change straight after the scope, not last', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ scope_name: 'Checkout conversion' }),
    ])

    renderAnomalies()

    await screen.findByRole('link', { name: 'Spike on Metric · Checkout conversion' })
    expect(screen.getAllByRole('columnheader').map((header) => header.textContent)).toEqual([
      'Anomaly',
      'Trend',
      'Change',
      'Actual / expected',
      'When',
      // The row menu's column, named for screen readers only.
      'Actions',
    ])
    // The table is no longer a fixed-width strip inside a sideways scroller,
    // which hid the change and the time off a phone screen.
    expect(screen.getByRole('table', { name: 'Anomaly signals' }).className).not.toContain('min-w-')
  })
})

describe('AnomaliesPage — row actions', () => {
  it('offers Open detail and View alerts from the row menu', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ scope_type: 'event_type', scope_ref: 'et-1', scope_name: 'Signup' }),
    ])

    renderAnomalies()

    const row = (await screen.findByText(rowLabel('Spike on Event type · Signup'))).closest(
      '[role="row"]',
    ) as HTMLElement
    fireEvent.keyDown(within(row).getByRole('button', { name: 'Signal actions' }), { key: 'Enter' })

    expect(await screen.findByRole('menuitem', { name: 'Open detail' })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/event-type/et-1',
    )
    expect(screen.getByRole('menuitem', { name: 'View alerts' })).toHaveAttribute(
      'href',
      '/p/demo/alerting',
    )
  })

  it('links a routed signal to its incident, naming the status', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({
        scope_type: 'event_type',
        scope_ref: 'et-1',
        scope_name: 'Signup',
        incident_id: 'inc-1',
        incident_status: 'acknowledged',
      }),
    ])

    renderAnomalies()

    expect(await screen.findByRole('link', { name: 'Incident · acknowledged' })).toHaveAttribute(
      'href',
      '/p/demo/alerting?incident=inc-1',
    )
  })
})

describe('AnomaliesPage — counts', () => {
  it('keeps a sub-unit baseline instead of rounding it to zero', async () => {
    // `metric` is a first-class scope here and a `%` catalog metric STORES a
    // fraction (0.08 == 8%), so Math.round wrote "1.2 vs 0" on a row whose
    // severity was computed from 0.4. Below 1 the decimals are the number.
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({
        scope_name: 'Checkout conversion',
        actual_count: 1.2,
        expected_count: 0.4,
      }),
    ])

    renderAnomalies()

    const row = (await screen.findByText(rowLabel('Spike on Metric · Checkout conversion'))).closest(
      '[role="row"]',
    ) as HTMLElement
    expect(row).toHaveTextContent('1.2 vs 0.4')
  })
})

describe('AnomaliesPage — magnitude filter', () => {
  it('hides low-magnitude signals at the default level and reveals them under "All"', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      // relEffect = 220/80 = 2.75 → clears "Significant".
      makeSignal({
        scope_type: 'event_type',
        scope_ref: 'et-major',
        scope_name: 'Big move',
        actual_count: 300,
        expected_count: 80,
      }),
      // relEffect = 4/80 = 0.05 → below "Significant".
      makeSignal({
        scope_type: 'event_type',
        scope_ref: 'et-minor',
        scope_name: 'Tiny wiggle',
        actual_count: 84,
        expected_count: 80,
      }),
    ])

    renderAnomalies()

    // Default "Significant" keeps the big one and drops the tiny one.
    expect(await screen.findByText(rowLabel('Spike on Event type · Big move'))).toBeInTheDocument()
    expect(screen.queryByText(rowLabel('Spike on Event type · Tiny wiggle'))).not.toBeInTheDocument()

    // Switch the magnitude filter to "All" — the small one now appears.
    await chooseFilter(MAGNITUDE, 'All')
    expect(await screen.findByText(rowLabel('Spike on Event type · Tiny wiggle'))).toBeInTheDocument()
    // The big one is still there.
    expect(screen.getByText(rowLabel('Spike on Event type · Big move'))).toBeInTheDocument()
  })

  it('keeps the magnitude control reachable by its accessible name', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ scope_name: 'Checkout conversion' }),
    ])

    renderAnomalies()

    await screen.findByText(rowLabel(/Spike on Metric/))
    expect(filterChip(MAGNITUDE)).toBeVisible()
    expect(filterChip(MAGNITUDE)).toHaveTextContent('Magnitude:Significant (≥50%)')
  })

  it('names each level’s threshold in the % the rows show', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ scope_name: 'Checkout conversion' }),
    ])

    renderAnomalies()

    await screen.findByText(rowLabel(/Spike on Metric/))
    expect(await filterOptions(MAGNITUDE)).toEqual(['All', 'Significant (≥50%)', 'Major (≥100%)'])
  })

  it('shows a lower-the-filter hint (not the empty state) when the level hides everything', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      // relEffect = 2/80 = 0.025 → below the default "Significant".
      makeSignal({
        scope_type: 'event_type',
        scope_ref: 'et-minor',
        scope_name: 'Tiny wiggle',
        actual_count: 82,
        expected_count: 80,
      }),
    ])

    renderAnomalies()

    // The single tiny signal is hidden by default → hint instead of rows.
    expect(await screen.findByText('Nothing at the significant level')).toBeInTheDocument()
    // The "No anomalies right now" hard-empty state must NOT be shown (signals exist).
    expect(screen.queryByText('No anomalies right now')).not.toBeInTheDocument()

    // The hint's "Show all" action drops the filter and reveals the row.
    fireEvent.click(screen.getByRole('button', { name: /Show all/ }))
    expect(await screen.findByText(rowLabel(/Spike on Event type/))).toBeInTheDocument()
  })
})

// The rows on this page are links off the route (each one opens a monitoring
// detail page), so Back is the primary way out of an investigation. With the
// level in component state that Back re-hid 162 of acme-ios's 209 signals every
// single time.
describe('AnomaliesPage — ?level= facet', () => {
  function tinySignal(): MonitoringSignal {
    // relEffect = 2/80 = 0.025 → visible only at "All".
    return makeSignal({
      scope_type: 'event_type',
      scope_ref: 'et-minor',
      scope_name: 'Tiny wiggle',
      actual_count: 82,
      expected_count: 80,
    })
  }

  it('pre-selects the level named by ?level= so a bookmarked view survives', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([tinySignal()])

    renderAnomalies('/p/demo/anomalies?level=all')

    // Landed already widened: no click, and the sub-threshold row is on screen.
    expect(await screen.findByText(rowLabel('Spike on Event type · Tiny wiggle'))).toBeInTheDocument()
    expect(filterChip(MAGNITUDE)).toHaveTextContent('Magnitude:All')
  })

  it('writes the level back to the URL, and clears the parameter on the default', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([tinySignal()])

    renderAnomalies()

    await chooseFilter(MAGNITUDE, 'All')
    expect(await screen.findByText('anomalies-location:/p/demo/anomalies?level=all'))
      .toBeInTheDocument()

    // Back to the default writes no parameter rather than `level=significant`.
    await chooseFilter(MAGNITUDE, /^Significant/)
    expect(await screen.findByText('anomalies-location:/p/demo/anomalies')).toBeInTheDocument()
  })

  it('degrades an unknown ?level= to the default instead of showing nothing', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ scope_type: 'event_type', scope_ref: 'et-1', scope_name: 'Signup' }),
      tinySignal(),
    ])

    renderAnomalies('/p/demo/anomalies?level=enormous')

    expect(await screen.findByText(rowLabel('Spike on Event type · Signup'))).toBeInTheDocument()
    expect(screen.queryByText(rowLabel('Spike on Event type · Tiny wiggle'))).not.toBeInTheDocument()
    expect(filterChip(MAGNITUDE)).toHaveTextContent('Magnitude:Significant (≥50%)')
  })

  it('keeps ?scan= and ?level= independent of each other', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([tinySignal()])
    vi.mocked(scansApi.list).mockResolvedValue(makeScans([{ id: 'scan-1', name: 'Live' }]))

    renderAnomalies('/p/demo/anomalies?scan=scan-1&level=all')

    expect(await screen.findByText(rowLabel('Spike on Event type · Tiny wiggle'))).toBeInTheDocument()
    // Flipping the level leaves the scan selection in the URL untouched.
    await chooseFilter(MAGNITUDE, /^Significant/)
    expect(await screen.findByText('anomalies-location:/p/demo/anomalies?scan=scan-1'))
      .toBeInTheDocument()
  })
})

describe('AnomaliesPage — scan facet', () => {
  // Mirrors the shape of the acme-ios stream: a legacy scan watching most of
  // the catalog contributes the bulk of open event-scope signals purely by
  // size, and without a scan facet the live scan's rows are unfindable.
  function legacyAndLiveSignals(): MonitoringSignal[] {
    return [
      ...Array.from({ length: 6 }, (_, i) =>
        makeSignal({
          scan_config_id: 'scan-legacy',
          scope_type: 'event',
          scope_ref: `legacy-ev-${i}`,
          scope_name: 'Legacy tap',
        }),
      ),
      makeSignal({
        scan_config_id: 'scan-live',
        scope_type: 'event',
        scope_ref: 'live-ev-1',
        scope_name: 'Live tap',
      }),
    ]
  }

  const scans = makeScans([
    { id: 'scan-legacy', name: 'Old events (iOS)' },
    { id: 'scan-live', name: 'Snowplow Events (iOS)' },
  ])

  it('narrows the list to one scan, with per-scan counts on the options', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue(legacyAndLiveSignals())
    vi.mocked(scansApi.list).mockResolvedValue(scans)

    renderAnomalies()

    // Both streams are visible before the facet is touched.
    expect(await screen.findByText(rowLabel('Spike on Event · Live tap'))).toBeInTheDocument()
    expect(screen.getAllByText(rowLabel('Spike on Event · Legacy tap'))).toHaveLength(6)

    // The option label carries the count, so the size difference is legible
    // before clicking: 6 legacy against 1 live.
    expect(filterChip(SCAN)).toHaveTextContent('Scan:All scans 7')
    expect(await filterOptions(SCAN)).toEqual([
      'All scans 7',
      'Old events (iOS) 6',
      'Snowplow Events (iOS) 1',
    ])

    await chooseFilter(SCAN, 'Snowplow Events (iOS) 1')

    expect(await screen.findByText(rowLabel('Spike on Event · Live tap'))).toBeInTheDocument()
    expect(screen.queryByText(rowLabel('Spike on Event · Legacy tap'))).not.toBeInTheDocument()
    // The subtitle attributes the omission to the scan filter, not the level.
    expect(screen.getByText(/1 of 7 open · 6 in other scans/)).toBeInTheDocument()
  })

  it('omits the facet when every signal comes from the same scan', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({
        scan_config_id: 'scan-legacy',
        scope_type: 'event',
        scope_ref: 'legacy-ev-0',
        scope_name: 'Legacy tap',
      }),
    ])
    vi.mocked(scansApi.list).mockResolvedValue(scans)

    renderAnomalies()

    await screen.findByText(rowLabel(/Spike on Event/))
    expect(screen.queryByRole('combobox', { name: SCAN })).not.toBeInTheDocument()
    // The magnitude control is untouched by the facet's absence.
    expect(filterChip(MAGNITUDE)).toBeVisible()
  })

  it('gives catalog-metric signals their own option instead of crashing on a null scan', async () => {
    // A catalog MetricDefinition series is project-global, so the API sends
    // scan_config_id: null for it. Keyed by the raw scan id, that null reached
    // `id.slice(0, 8)` in the option label and threw, white-screening the whole
    // page the first hour any metric fired.
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      ...legacyAndLiveSignals(),
      makeSignal({
        scan_config_id: null,
        scope_type: 'metric',
        scope_ref: 'metric-1',
        scope_name: 'Checkout conversion',
      }),
    ])
    vi.mocked(scansApi.list).mockResolvedValue(scans)

    renderAnomalies()

    const options = await filterOptions(SCAN)
    expect(options).toContain('Catalog metrics 1')
    expect(options).toContain('All scans 8')

    // And the option is reachable: selecting it keeps the metric row and drops
    // every scan-bound one, so the signal is not merely un-crashing but findable.
    await chooseFilter(SCAN, 'Catalog metrics 1')
    expect(await screen.findByText(rowLabel(/Spike on Metric/))).toBeInTheDocument()
    expect(screen.queryByText(/Legacy tap/)).not.toBeInTheDocument()
  })

  it('falls back to the short scan ref when the scan list has not resolved', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue(legacyAndLiveSignals())
    // Scan names unavailable (still loading, or the scan was deleted).
    vi.mocked(scansApi.list).mockResolvedValue(makeScans([]))

    renderAnomalies()

    expect(await filterOptions(SCAN)).toContain('Scan scan-leg 6')
  })

  it('offers "show all scans" when the selected scan has nothing at this level', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      // relEffect 2.75 → clears "Major" (≥1); relEffect 0.05 → clears neither.
      makeSignal({
        scan_config_id: 'scan-legacy',
        scope_type: 'event',
        scope_ref: 'legacy-ev-0',
        scope_name: 'Legacy tap',
        actual_count: 300,
        expected_count: 80,
      }),
      // relEffect 0.5 → clears "Significant" but not "Major".
      makeSignal({
        scan_config_id: 'scan-live',
        scope_type: 'event',
        scope_ref: 'live-ev-1',
        scope_name: 'Live tap',
      }),
      makeSignal({
        scan_config_id: 'scan-live',
        scope_type: 'event',
        scope_ref: 'live-ev-2',
        scope_name: 'Live tap two',
      }),
    ])
    vi.mocked(scansApi.list).mockResolvedValue(scans)

    renderAnomalies()

    // Pick the live scan, then raise the level past everything it has while the
    // legacy scan still has one — so the emptiness is the scan filter's doing.
    // The option survives the level change (its count drops to 0), which is the
    // point: it must not evaporate and silently reset the page to "all scans".
    await chooseFilter(SCAN, 'Snowplow Events (iOS) 2')
    await chooseFilter(MAGNITUDE, /^Major/)
    expect(filterChip(SCAN)).toHaveTextContent('Scan:Snowplow Events (iOS) 0')
    expect(
      await screen.findByText('Nothing in Snowplow Events (iOS) at this level'),
    ).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Show all scans/ }))
    expect(await screen.findByText(rowLabel(/Spike on Event/))).toBeInTheDocument()
  })

  // `?scan=` is what makes a scan's "Signals added" counter reach the anomalies
  // it produced. Before this the facet was component state only, so the link had
  // nowhere to land but the unfiltered page.
  it('pre-selects the scan named by ?scan= and shows only its signals', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue(legacyAndLiveSignals())
    vi.mocked(scansApi.list).mockResolvedValue(scans)

    renderAnomalies('/p/demo/anomalies?scan=scan-live')

    // Landed already narrowed: no click, and the legacy stream that drowns this
    // one out by size is gone.
    expect(await screen.findByText(rowLabel('Spike on Event · Live tap'))).toBeInTheDocument()
    expect(screen.queryByText(rowLabel('Spike on Event · Legacy tap'))).not.toBeInTheDocument()
    expect(filterChip(SCAN)).toHaveTextContent('Scan:Snowplow Events (iOS) 1')
  })

  it('degrades an unknown ?scan= to All rather than rendering an empty page', async () => {
    // A deleted scan, a stale bookmark or a hand-edited URL must not produce a
    // page that shows nothing and explains nothing. This is the `activeScanId`
    // guard: dropping it on the way to reading the URL would empty the list.
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue(legacyAndLiveSignals())
    vi.mocked(scansApi.list).mockResolvedValue(scans)

    renderAnomalies('/p/demo/anomalies?scan=does-not-exist')

    // The FULL list, both scans — not an empty state, not one scan.
    expect(await screen.findByText(rowLabel('Spike on Event · Live tap'))).toBeInTheDocument()
    expect(screen.getAllByText(rowLabel('Spike on Event · Legacy tap'))).toHaveLength(6)
    expect(filterChip(SCAN)).toHaveTextContent('Scan:All scans 7')
    // No phantom option is manufactured for the id that does not exist.
    expect((await filterOptions(SCAN)).some((option) => option.includes('does-not-exist'))).toBe(false)
  })

  it('keeps a real ?scan= whose signals have all closed, and explains the empty page', async () => {
    // "Raised 2 anomaly signals" on a run from last week links here; both have
    // since closed. Silently widening to "all" answers a question the user did
    // not ask — a full list of a DIFFERENT scan's anomalies, with no control
    // showing that the filter was discarded.
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({
        scan_config_id: 'scan-legacy',
        scope_type: 'event',
        scope_ref: 'legacy-ev-0',
        scope_name: 'Legacy tap',
      }),
    ])
    vi.mocked(scansApi.list).mockResolvedValue(scans)

    renderAnomalies('/p/demo/anomalies?scan=scan-live')

    // The scan the link named is still the selection, carrying an honest 0.
    expect(await screen.findByRole('combobox', { name: SCAN })).toHaveTextContent(
      'Scan:Snowplow Events (iOS) 0',
    )

    // ...and the page says why it is empty rather than filling itself with the
    // other scan's rows.
    expect(
      screen.getByText('No open anomalies from Snowplow Events (iOS)'),
    ).toBeInTheDocument()
    expect(screen.queryByText(rowLabel('Spike on Event · Legacy tap'))).not.toBeInTheDocument()

    // The way out is one click, and it is labelled with what it will show.
    fireEvent.click(screen.getByRole('button', { name: 'Show all scans (1)' }))
    expect(await screen.findByText(rowLabel('Spike on Event · Legacy tap'))).toBeInTheDocument()
  })

  it('writes the facet selection back to ?scan= so the narrowed view is linkable', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue(legacyAndLiveSignals())
    vi.mocked(scansApi.list).mockResolvedValue(scans)

    renderAnomalies()

    await chooseFilter(SCAN, 'Snowplow Events (iOS) 1')
    expect(await screen.findByText('anomalies-location:/p/demo/anomalies?scan=scan-live'))
      .toBeInTheDocument()

    // ...and clearing it removes the parameter rather than leaving `scan=all`.
    await chooseFilter(SCAN, 'All scans 7')
    expect(await screen.findByText('anomalies-location:/p/demo/anomalies')).toBeInTheDocument()
  })
})

describe('AnomaliesPage — ranking and keys', () => {
  it('ranks by relative effect like Overview and the bell, not by |z|', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      // Quiet scope: a huge z on a tiny absolute move (relative effect 0.6).
      makeSignal({ scope_ref: 'quiet', scope_name: 'Quiet', actual_count: 8, expected_count: 5, z_score: 40, relative_effect: 0.6 }),
      // Busy scope: a modest z on a large move (relative effect 2).
      makeSignal({ scope_ref: 'busy', scope_name: 'Busy', actual_count: 3000, expected_count: 1000, z_score: 6, relative_effect: 2 }),
    ])

    renderAnomalies()

    await screen.findByRole('link', { name: 'Spike on Metric · Busy' })
    const links = screen.getAllByRole('link', { name: /^Spike on Metric/ })
    expect(links.map((link) => link.textContent)).toEqual([
      'Spike on Metric · Busy',
      'Spike on Metric · Quiet',
    ])
  })

  it('renders both open signals when two scans flag the same scope on the same bucket', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ scan_config_id: 'scan-legacy', scope_type: 'event', scope_ref: 'ev-1', scope_name: 'Login' }),
      makeSignal({ scan_config_id: 'scan-live', scope_type: 'event', scope_ref: 'ev-1', scope_name: 'Login' }),
    ])

    renderAnomalies()

    await screen.findAllByText(rowLabel('Spike on Event · Login'))
    // Both rows render; React's duplicate-key warning is a console.error, which
    // the test setup turns into a failure.
    expect(screen.getAllByText(rowLabel('Spike on Event · Login'))).toHaveLength(2)
  })
})

describe('AnomaliesPage — when column', () => {
  it('shows the bucket start as an absolute time, and says it is the bucket start', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ scope_name: 'Checkout conversion', bucket: '2026-07-01T00:00:00Z' }),
    ])

    renderAnomalies()

    await screen.findByRole('link', { name: 'Spike on Metric · Checkout conversion' })
    expect(screen.getByRole('columnheader', { name: 'When' })).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Bucket' })).not.toBeInTheDocument()
    const time = document.querySelector('time[datetime="2026-07-01T00:00:00Z"]')
    expect(time).not.toBeNull()
    // Absolute ("Jul 1, 02:00" in the viewer's zone), never "3mo ago" beside a
    // second relative time it seems to contradict.
    expect(time?.textContent).toMatch(/\d{2}:\d{2}$/)
    expect(time?.textContent).not.toMatch(/ago/)
    expect(time?.getAttribute('title')).toMatch(/^Bucket starting .+\(.+\)$/)
    // No detection time on the payload, so nothing claims one.
    expect(screen.queryByText(/^found /)).not.toBeInTheDocument()
  })

  it('says when the detector found it, under when the bucket began', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({
        scope_name: 'Checkout conversion',
        bucket: '2026-07-01T00:00:00Z',
        detected_at: '2026-07-01T01:05:00Z',
      }),
    ])

    renderAnomalies()

    await screen.findByRole('link', { name: 'Spike on Metric · Checkout conversion' })
    const detected = document.querySelector('time[datetime="2026-07-01T01:05:00Z"]')
    expect(detected).toHaveTextContent(/^found /)
    expect(detected?.getAttribute('title')).toMatch(/^Detected .+\(.+\)$/)
  })
})

describe('AnomaliesPage — rollup tones', () => {
  it('colours the Spikes and Drops figures, which have no delta to carry a tone', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ scope_ref: 'a', scope_name: 'A' }),
      makeSignal({ scope_ref: 'b', scope_name: 'B', direction: 'drop', actual_count: 20, expected_count: 80 }),
    ])

    renderAnomalies()

    await screen.findByRole('link', { name: 'Spike on Metric · A' })
    const spikes = screen.getByText('Spikes').closest('dl') as HTMLElement
    const drops = screen.getByText('Drops').closest('dl') as HTMLElement
    expect(within(spikes).getByText('1')).toHaveAttribute('data-tone', 'danger')
    expect(within(drops).getByText('1')).toHaveAttribute('data-tone', 'warning')
  })
})

describe('AnomaliesPage — filter chips', () => {
  it('filters with chips that name their current value, not a segmented control', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([makeSignal({ scope_name: 'A' })])

    renderAnomalies()

    const chip = await screen.findByRole('combobox', { name: MAGNITUDE })
    expect(chip).toHaveTextContent('Magnitude:Significant (≥50%)')
    expect(chip).toHaveAccessibleName('Magnitude filter: Significant (≥50%)')
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument()

    await chooseFilter(MAGNITUDE, /^Major/)
    expect(await screen.findByText(/anomalies-location:.*level=major/)).toBeInTheDocument()
    expect(filterChip(MAGNITUDE)).toHaveTextContent('Magnitude:Major (≥100%)')
    expect(filterChip(MAGNITUDE)).toHaveAccessibleName('Magnitude filter: Major (≥100%)')
  })
})

// Anomalies sat beside Metrics and Coverage under the kit's own page
// head (22px title, 12px description) while its siblings used PageHeader.
describe('AnomaliesPage — the shared page header', () => {
  it('renders the same title element as its Observe siblings', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([])
    renderAnomalies()
    const heading = await screen.findByRole('heading', { level: 1, name: 'Anomalies' })

    const { container } = render(<PageHeader eyebrow="Observe" title="Metrics" />)
    const reference = container.querySelector('h1')
    expect(reference).not.toBeNull()
    expect(heading.className).toBe(reference?.className)
  })
})

describe('AnomaliesPage — page states', () => {
  it('holds a skeleton, not zeros, until the signals arrive', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockReturnValue(new Promise<MonitoringSignal[]>(() => {}))

    renderAnomalies()

    expect(await screen.findByRole('status')).toHaveTextContent('Loading anomalies…')
    expect(screen.queryByText('Open signals')).not.toBeInTheDocument()
    expect(screen.queryByText('No anomalies right now')).not.toBeInTheDocument()
  })

  it('says monitoring is not running when no scan collects volume', async () => {
    // A project with no scan (or only Catalog only scans) got the same
    // reassuring "No anomalies right now" as a healthy one.
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([])
    vi.mocked(scansApi.list).mockResolvedValue(makeScans([]))

    renderAnomalies()

    expect(await screen.findByRole('heading', { name: 'Monitoring isn’t running yet' }))
      .toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Run a scan' })).toHaveAttribute('href', '/p/demo/scans')
    expect(screen.getAllByRole('link', { name: /Detection settings/ }).length).toBeGreaterThan(0)
    expect(screen.queryByText('No anomalies right now')).not.toBeInTheDocument()
    // Three zeros say nothing about a project that is not monitored.
    expect(screen.queryByText('Open signals')).not.toBeInTheDocument()
  })

  it('keeps the all-clear, in neutral, for a project whose scans do collect volume', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([])
    vi.mocked(scansApi.list).mockResolvedValue(
      [{ id: 'scan-1', name: 'Live', interval: '1h' }] as unknown as ScanConfig[],
    )

    renderAnomalies()

    expect(await screen.findByText('No anomalies right now')).toBeInTheDocument()
    expect(screen.queryByText('Monitoring isn’t running yet')).not.toBeInTheDocument()
    // A green 0 read as praise: zero is neutral.
    const open = screen.getByText('Open signals').closest('dl') as HTMLElement
    expect(within(open).getByText('0')).not.toHaveAttribute('data-tone')
  })

  it('points at Alerting as the place where triage happens', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([makeSignal({ scope_name: 'A' })])

    renderAnomalies()

    await screen.findByRole('link', { name: 'Spike on Metric · A' })
    expect(screen.getByText(/Signals are what detection found/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Alerting' })).toHaveAttribute(
      'href',
      '/p/demo/alerting',
    )
  })
})

describe('AnomaliesPage — row sparklines', () => {
  it('asks for every scan-backed signal in one request and marks the flagged bucket', async () => {
    const flagged = '2026-07-01T20:00:00Z'
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({
        scope_type: 'event',
        scope_ref: 'ev-1',
        scope_name: 'Checkout tapped',
        bucket: flagged,
      }),
      // A catalog metric has no scan series: it is never sent.
      makeSignal({ scope_name: 'Checkout conversion', scan_config_id: null }),
    ])
    vi.mocked(eventMetricsApi.getSignalSeries).mockResolvedValue([
      {
        scan_config_id: 'scan-1',
        scope_type: 'event',
        scope_ref: 'ev-1',
        // Re-spelled by the server: matched as an instant, not as a string.
        bucket: '2026-07-01T20:00:00+00:00',
        interval: '1h',
        data: [
          { bucket: '2026-07-01T18:00:00Z', count: 10 },
          { bucket: '2026-07-01T19:00:00Z', count: 12 },
          { bucket: '2026-07-01T20:00:00Z', count: 60 },
        ],
      },
    ])

    renderAnomalies()

    const link = await screen.findByRole('link', { name: 'Spike on Event · Checkout tapped' })
    const row = link.closest('[role="row"]') as HTMLElement
    // The sparkline's flagged-bucket marker (r=2.5); the row menu's ellipsis
    // icon draws circles too, so the marker is matched by its radius.
    const marker = 'circle[r="2.5"]'
    await waitFor(() => expect(row.querySelector(marker)).not.toBeNull())
    expect(eventMetricsApi.getSignalSeries).toHaveBeenCalledTimes(1)
    expect(eventMetricsApi.getSignalSeries).toHaveBeenCalledWith('demo', [
      { scan_config_id: 'scan-1', scope_type: 'event', scope_ref: 'ev-1', bucket: flagged },
    ])
    // The metric row draws nothing and never shows a loading block.
    const metricRow = screen
      .getByRole('link', { name: 'Spike on Metric · Checkout conversion' })
      .closest('[role="row"]') as HTMLElement
    expect(metricRow.querySelector(marker)).toBeNull()
    expect(within(metricRow).queryByTestId('sparkline-skeleton')).toBeNull()
  })
})

describe('AnomaliesPage — drop signals held while a source is late (F16, #269)', () => {
  it('says so, naming the late scan', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([])
    vi.mocked(sourceFreshnessApi.list).mockResolvedValue([
      {
        id: 'scan-late',
        name: 'App events',
        data_source_id: 'ds-1',
        freshness: {
          status: 'late',
          lag_seconds: 7 * 3600,
          last_event_at: '2026-09-26T03:00:00Z',
          last_collection_at: '2026-09-26T09:55:00Z',
          expected_by: '2026-09-26T06:00:00Z',
        },
      },
    ])
    renderAnomalies()

    expect(await screen.findByText(/drop signals held/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'App events' })).toHaveAttribute('href', '/p/demo/scans/scan-late')
  })

  it('says nothing while every source is fresh', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([])
    renderAnomalies()

    await waitFor(() => expect(sourceFreshnessApi.list).toHaveBeenCalled())
    expect(screen.queryByText(/drop signals held/)).toBeNull()
  })
})

describe('AnomaliesPage — coached demo scenario', () => {
  afterEach(() => {
    window.localStorage.clear()
  })

  it("rings the seeded spike's row for the explore chapter", async () => {
    // The row's label reads "Event · Home Screen View": the step matched the
    // label against the bare name, found no row, and the chapter stuck.
    writeScenarioState('demo', chapterState('explore', 'explore/visit-anomaly'))
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ scope_type: 'event', scope_ref: 'ev-other', event_id: 'ev-other', scope_name: 'Paywall View' }),
      makeSignal({ scope_type: 'event', scope_ref: 'ev-home', event_id: 'ev-home', scope_name: 'Home Screen View' }),
    ])
    const project = { id: 'p-1', name: 'Demo', slug: 'demo', is_demo: true, generation_status: 'ready' } as Project
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/p/demo/anomalies']}>
          <DemoScenarioProvider project={project}>
            <Routes>
              <Route path="/p/:slug/anomalies" element={<AnomaliesPage />} />
            </Routes>
          </DemoScenarioProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    )

    const coached = await screen.findByText(rowLabel('Spike on Event · Home Screen View'))
    await waitFor(() => expect(coached).toHaveAttribute('data-coach-target', 'explore/visit-anomaly'))
    expect(screen.getByText(rowLabel('Spike on Event · Paywall View'))).not.toHaveAttribute('data-coach-target')
  })
})
