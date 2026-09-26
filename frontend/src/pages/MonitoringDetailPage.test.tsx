import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import type { EventMetricPoint, Project } from '@/types'
import { DemoScenarioProvider } from '@/demo/DemoScenarioProvider'
import {
  buildChapterSteps,
  initialScenarioState,
  readScenarioState,
  writeScenarioState,
  type ScenarioState,
} from '@/demo/scenarioModel'
import { liveLoopState } from '@/demo/scenarioTestState'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { stopAllMetricCollectionWatches } from '@/hooks/useMetricCollectionWatcher'
import MonitoringDetailPage from './MonitoringDetailPage'
import { at } from '@/test/at'

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))
vi.mock('sonner', () => ({
  toast: { success: toastSuccess, error: toastError },
  Toaster: () => null,
}))

vi.mock('@/components/ui/chart-lazy', () => ({
  MetricsChart: ({
    data,
    forecast,
    valueFormatter,
    tooltipFormatter,
    sigmaThreshold,
  }: {
    data?: Array<{ bucket: string; count?: number }>
    forecast?: unknown[]
    valueFormatter?: (value: number) => string
    tooltipFormatter?: (value: number) => string
    sigmaThreshold?: number
  }) => (
    <div
      data-testid="metrics-chart"
      data-forecast-count={forecast?.length ?? 0}
      // The band multiplier the page handed the chart (tripl-2yww); the chart's
      // own honouring of it is pinned in chart.test.tsx.
      data-sigma-threshold={sigmaThreshold ?? ''}
      data-points={data?.length ?? 0}
      data-first-bucket={data?.[0]?.bucket ?? ''}
      data-first-count={data?.[0]?.count ?? ''}
      // Probe the optional formatter: percent metrics turn 0.08 into '8%'.
      data-value-sample={valueFormatter ? valueFormatter(0.08) : ''}
      // Probe the tooltip spelling on a sub-1 and a four-digit value.
      data-tooltip-sample={tooltipFormatter ? tooltipFormatter(0.0045) : ''}
      data-tooltip-large={tooltipFormatter ? tooltipFormatter(1234) : ''}
      data-axis-small={valueFormatter ? valueFormatter(0.0045) : ''}
    />
  ),
  MetricsMultiSeriesChart: ({
    series,
    emptyLabel,
    seriesLabel,
    valueFormatter,
  }: {
    series: Array<{ label: string }>
    emptyLabel?: string
    seriesLabel?: string
    valueFormatter?: (value: number) => string
  }) => (
    <div
      data-testid="multi-chart"
      data-labels={series.map(item => item.label).join('|')}
      data-series-label={seriesLabel ?? ''}
      // Probe the optional formatter: percent metrics turn 0.08 into '8%'.
      data-value-sample={valueFormatter ? valueFormatter(0.08) : ''}
    >
      {series.length ? series.map(item => <span key={item.label}>{item.label}</span>) : emptyLabel}
    </div>
  ),
}))

vi.mock('@/components/sql-editor', () => ({
  SqlEditor: ({
    ariaLabel,
    value,
    readOnly,
  }: {
    ariaLabel?: string
    value: string
    readOnly?: boolean
  }) => <textarea aria-label={ariaLabel} value={value} readOnly={readOnly} onChange={() => {}} />,
}))

/** The page writes its view state to the URL (MON-24); this reads it back. */
function LocationProbe() {
  const location = useLocation()
  return <output data-testid="location-search">{location.search}</output>
}

function errorResponse(status = 500) {
  return new Response(JSON.stringify({ detail: 'boom' }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function mockJsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function metricPoint(bucket: string, count: number): EventMetricPoint {
  return {
    bucket,
    count,
    expected_count: null,
    stddev: null,
    is_anomaly: false,
    anomaly_direction: null,
    z_score: null,
  }
}

function appVersionResponse(scanConfigId: string) {
  return {
    scan_config_id: scanConfigId,
    scope_type: 'project_total',
    scope_ref: scanConfigId,
    event_id: null,
    event_type_id: null,
    app_version_column: 'app_version',
    interval: '1h',
    latest_version: '2.10.0',
    versions: [
      { version: '2.10.0', is_other: false, is_latest: true, is_active: true },
      { version: '2.9.0', is_other: false, is_latest: false, is_active: true },
      { version: 'Other', is_other: true, is_latest: false, is_active: false },
    ],
    series: [
      {
        version: '2.10.0',
        is_other: false,
        is_latest: true,
        is_active: true,
        total_count: 120,
        data: [metricPoint('2026-01-02T00:00:00Z', 120)],
      },
      {
        version: '2.9.0',
        is_other: false,
        is_latest: false,
        is_active: true,
        total_count: 80,
        data: [metricPoint('2026-01-02T00:00:00Z', 80)],
      },
      {
        version: 'Other',
        is_other: true,
        is_latest: false,
        is_active: false,
        total_count: 10,
        data: [metricPoint('2026-01-02T00:00:00Z', 10)],
      },
    ],
  }
}

// Same shape, but the SemVer-newest release (2.10.0) has NOT taken a real share
// of traffic yet (is_active=false): the backend still reports it as is_latest via
// the raw-SemVer-max fallback, so the page must treat it as a pre-release rather
// than the primary rolled-out "latest".
function appVersionPreReleaseResponse(scanConfigId: string) {
  return {
    ...appVersionResponse(scanConfigId),
    versions: [
      { version: '2.10.0', is_other: false, is_latest: true, is_active: false },
      { version: '2.9.0', is_other: false, is_latest: false, is_active: true },
      { version: 'Other', is_other: true, is_latest: false, is_active: false },
    ],
    series: [
      {
        version: '2.10.0',
        is_other: false,
        is_latest: true,
        is_active: false,
        total_count: 4,
        data: [metricPoint('2026-01-02T00:00:00Z', 4)],
      },
      {
        version: '2.9.0',
        is_other: false,
        is_latest: false,
        is_active: true,
        total_count: 180,
        data: [metricPoint('2026-01-02T00:00:00Z', 180)],
      },
      {
        version: 'Other',
        is_other: true,
        is_latest: false,
        is_active: false,
        total_count: 10,
        data: [metricPoint('2026-01-02T00:00:00Z', 10)],
      },
    ],
  }
}

function appVersionPreReleaseAdoptionResponse(scanConfigId: string) {
  return {
    ...appVersionPreReleaseResponse(scanConfigId),
    totals: [{ bucket: '2026-01-02T00:00:00Z', count: 194 }],
  }
}

function appVersionAdoptionResponse(scanConfigId: string) {
  return {
    ...appVersionResponse(scanConfigId),
    totals: [{ bucket: '2026-01-02T00:00:00Z', count: 210 }],
  }
}

function renderMonitoringPage(search = '') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/p/demo/monitoring/project-total/scan-1${search}`]}>
        <Routes>
          <Route path="/p/:slug/monitoring/:scope/:id" element={<MonitoringDetailPage />} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  // Collect watches are detached from the page, so one test's run must not
  // keep polling into the next. Unmount first: stopping a watch notifies every
  // mounted reader.
  cleanup()
  stopAllMetricCollectionWatches()
  vi.restoreAllMocks()
})

describe('MonitoringDetailPage app-version view', () => {
  it('renders semver-ordered version charts and filters to latest', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)

      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/metrics/total')) {
        return mockJsonResponse({
          scope: 'project_total',
          scan_config_id: 'scan-1',
          event_id: null,
          event_type_id: null,
          interval: '1h',
          latest_signal: null,
          data: [metricPoint('2026-01-02T00:00:00Z', 210)],
          forecast: [],
        })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1')) {
        return mockJsonResponse({ id: 'scan-1', app_version_column: 'app_version' })
      }
      if (url.includes('/api/v1/projects/demo/annotations')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/scans/scan-1/app-versions')) {
        return mockJsonResponse(appVersionResponse('scan-1'))
      }
      if (url.includes('/api/v1/projects/demo/scans/scan-1/version-adoption')) {
        return mockJsonResponse(appVersionAdoptionResponse('scan-1'))
      }
      if (url.includes('/api/v1/projects/demo/scans/scan-1/release-regressions')) {
        return mockJsonResponse({
          scan_config_id: 'scan-1',
          app_version_column: 'app_version',
          latest_version: '2.10.0',
          items: [],
        })
      }

      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderMonitoringPage()

    const byVersionTab = await screen.findByRole('tab', { name: /By version/i })
    fireEvent.pointerDown(byVersionTab, { button: 0, ctrlKey: false })
    fireEvent.mouseDown(byVersionTab, { button: 0, ctrlKey: false })
    fireEvent.pointerUp(byVersionTab, { button: 0, ctrlKey: false })
    fireEvent.mouseUp(byVersionTab, { button: 0, ctrlKey: false })
    fireEvent.click(byVersionTab)

    expect(await screen.findByText('latest 2.10.0')).toBeInTheDocument()
    await waitFor(() => {
      const charts = screen.getAllByTestId('multi-chart')
      expect(charts[0]).toHaveAttribute('data-labels', '2.10.0 · latest|2.9.0|Other')
      expect(charts[1]).toHaveAttribute('data-labels', '2.10.0 · latest|2.9.0|Other')
    })

    fireEvent.click(screen.getByRole('button', { name: 'Latest' }))

    await waitFor(() => {
      const charts = screen.getAllByTestId('multi-chart')
      expect(charts[0]).toHaveAttribute('data-labels', '2.10.0 · latest')
      expect(charts[1]).toHaveAttribute('data-labels', '2.10.0 · latest')
    })
  })

  it('treats a non-active SemVer-newest version as a pre-release, not the primary latest', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)

      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/metrics/total')) {
        return mockJsonResponse({
          scope: 'project_total',
          scan_config_id: 'scan-1',
          event_id: null,
          event_type_id: null,
          interval: '1h',
          latest_signal: null,
          data: [metricPoint('2026-01-02T00:00:00Z', 194)],
          forecast: [],
        })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1')) {
        return mockJsonResponse({ id: 'scan-1', app_version_column: 'app_version' })
      }
      if (url.includes('/api/v1/projects/demo/annotations')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/scans/scan-1/app-versions')) {
        return mockJsonResponse(appVersionPreReleaseResponse('scan-1'))
      }
      if (url.includes('/api/v1/projects/demo/scans/scan-1/version-adoption')) {
        return mockJsonResponse(appVersionPreReleaseAdoptionResponse('scan-1'))
      }
      if (url.includes('/api/v1/projects/demo/scans/scan-1/release-regressions')) {
        return mockJsonResponse({
          scan_config_id: 'scan-1',
          app_version_column: 'app_version',
          latest_version: '2.9.0',
          items: [],
        })
      }

      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderMonitoringPage()

    const byVersionTab = await screen.findByRole('tab', { name: /By version/i })
    fireEvent.pointerDown(byVersionTab, { button: 0, ctrlKey: false })
    fireEvent.mouseDown(byVersionTab, { button: 0, ctrlKey: false })
    fireEvent.pointerUp(byVersionTab, { button: 0, ctrlKey: false })
    fireEvent.mouseUp(byVersionTab, { button: 0, ctrlKey: false })
    fireEvent.click(byVersionTab)

    // Header badge reads "pre-release 2.10.0", NOT the primary "latest 2.10.0".
    expect(await screen.findByText('pre-release 2.10.0')).toBeInTheDocument()
    expect(screen.queryByText('latest 2.10.0')).not.toBeInTheDocument()

    // The chart series labels the newest release "· pre-release" rather than "· latest".
    await waitFor(() => {
      const charts = screen.getAllByTestId('multi-chart')
      expect(charts[0]).toHaveAttribute('data-labels', '2.10.0 · pre-release|2.9.0|Other')
    })

    // The Latest filter carries a warning affordance (pre-release / low traffic).
    expect(screen.getByRole('button', { name: 'Latest' })).toHaveAttribute(
      'title',
      'The newest release is a pre-release with little traffic — not yet rolled out.',
    )
  })

  it('hides the tab when the scan has no app version column', async () => {
    const calls: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      calls.push(url)

      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/metrics/total')) {
        return mockJsonResponse({
          scope: 'project_total',
          scan_config_id: 'scan-1',
          event_id: null,
          event_type_id: null,
          interval: '1h',
          latest_signal: null,
          data: [metricPoint('2026-01-02T00:00:00Z', 210)],
          forecast: [],
        })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1')) {
        return mockJsonResponse({ id: 'scan-1', app_version_column: null })
      }
      if (url.includes('/api/v1/projects/demo/annotations')) return mockJsonResponse([])

      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderMonitoringPage()

    await waitFor(() => {
      expect(calls.some(url => url.endsWith('/api/v1/projects/demo/scans/scan-1'))).toBe(true)
    })
    expect(screen.queryByRole('tab', { name: /By version/i })).not.toBeInTheDocument()
    expect(calls.some(url => url.includes('/app-versions'))).toBe(false)
    expect(calls.some(url => url.includes('/version-adoption'))).toBe(false)
  })
})

describe('MonitoringDetailPage volume granularity follows range (tripl-7l83.10)', () => {
  // Radix Select drives selection through pointer capture, which jsdom omits.
  beforeAll(() => {
    if (!Element.prototype.hasPointerCapture) {
      Element.prototype.hasPointerCapture = () => false
    }
    if (!Element.prototype.releasePointerCapture) {
      Element.prototype.releasePointerCapture = () => {}
    }
  })

  // Three project-total points that bucket to distinct counts per granularity:
  //   hour -> 3 buckets, day -> 2 (the two 2026-01-01 points merge),
  //   week -> 1 (all three land in the epoch-anchored 2026-01-01 week).
  function installProjectTotalFetch(
    forecast: Array<{ bucket: string; expected_count: number; stddev: number }> = [],
    interval = '1h',
  ) {
    return vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)

      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/metrics/total')) {
        return mockJsonResponse({
          scope: 'project_total',
          scan_config_id: 'scan-1',
          event_id: null,
          event_type_id: null,
          interval,
          latest_signal: null,
          data: [
            metricPoint('2026-01-01T05:00:00Z', 5),
            metricPoint('2026-01-01T18:00:00Z', 7),
            metricPoint('2026-01-02T10:00:00Z', 3),
          ],
          forecast,
          sigma_threshold: 6,
        })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1')) {
        return mockJsonResponse({ id: 'scan-1', app_version_column: null })
      }
      if (url.includes('/api/v1/projects/demo/annotations')) return mockJsonResponse([])

      throw new Error(`Unhandled fetch: ${url}`)
    })
  }

  // Each range change refetches (no placeholderData) and remounts the chart, so
  // always re-query the testid rather than holding a stale node reference.
  const chartPoints = () => screen.getByTestId('metrics-chart').getAttribute('data-points')
  const chartForecastCount = () => screen.getByTestId('metrics-chart').getAttribute('data-forecast-count')

  it('hands the chart the sigma threshold the payload serves (tripl-2yww)', async () => {
    installProjectTotalFetch()
    renderMonitoringPage()

    // 6, not undefined: dropping `sigmaThreshold={metrics?.sigma_threshold}`
    // from the render site sends the band back to the chart's default of 4.
    const chart = await screen.findByTestId('metrics-chart')
    await waitFor(() => expect(chart).toHaveAttribute('data-sigma-threshold', '6'))
  })

  it('defaults to 7d hours and follows later range changes', async () => {
    const fetchSpy = installProjectTotalFetch()
    renderMonitoringPage()

    await screen.findByTestId('metrics-chart')
    // Initial 7d default: hourly buckets, so every point stays distinct.
    await waitFor(() => expect(chartPoints()).toBe('3'))
    const initialMetricsUrl = fetchSpy.mock.calls
      .map(([input]) => String(input))
      .find(url => url.includes('/api/v1/projects/demo/metrics/total'))
    expect(initialMetricsUrl).toBeDefined()
    const initialRange = new URL(initialMetricsUrl!, 'http://localhost').searchParams
    const initialFrom = new Date(initialRange.get('from')!).getTime()
    const initialTo = new Date(initialRange.get('to')!).getTime()
    expect(initialTo - initialFrom).toBe(7 * 24 * 60 * 60 * 1000)

    // 30d: daily buckets, so the two 2026-01-01 points collapse.
    fireEvent.click(screen.getByRole('button', { name: '30d' }))
    await waitFor(() => expect(chartPoints()).toBe('2'))

    // 90d: weekly buckets, all three points collapse into one.
    fireEvent.click(screen.getByRole('button', { name: '90d' }))
    await waitFor(() => expect(chartPoints()).toBe('1'))

    // 7d: hourly buckets, every point is its own bucket.
    fireEvent.click(screen.getByRole('button', { name: '7d' }))
    await waitFor(() => expect(chartPoints()).toBe('3'))
  })

  it('keeps a manual granularity override sticky across range changes', async () => {
    installProjectTotalFetch()
    renderMonitoringPage()

    await screen.findByTestId('metrics-chart')
    await waitFor(() => expect(chartPoints()).toBe('3'))

    // Manually override the 7d hourly default to Days.
    fireEvent.click(screen.getByRole('combobox', { name: /time granularity/i }))
    fireEvent.click(await screen.findByRole('option', { name: 'Days' }))
    await waitFor(() => expect(chartPoints()).toBe('2'))

    // Changing the range must NOT reset the override back to a range default:
    // 90d would default to weekly (1 point), but the sticky override keeps 2.
    fireEvent.click(screen.getByRole('button', { name: '90d' }))
    await waitFor(() => expect(chartPoints()).toBe('2'))
  })

  it('only renders a forecast at the native collection granularity', async () => {
    installProjectTotalFetch([
      {
        bucket: '2026-01-02T11:00:00Z',
        expected_count: 4,
        stddev: 1,
      },
    ])
    renderMonitoringPage()

    await screen.findByTestId('metrics-chart')
    // The 7d default keeps native hourly buckets and their one-hour forecast.
    await waitFor(() => expect(chartForecastCount()).toBe('1'))

    // The 30d preset rolls hourly actuals into days. A one-hour forecast is not
    // a forecast for the whole day and must not be appended to that series.
    fireEvent.click(screen.getByRole('button', { name: '30d' }))
    await waitFor(() => expect(chartForecastCount()).toBe('0'))

    fireEvent.click(screen.getByRole('button', { name: '90d' }))
    await waitFor(() => expect(chartForecastCount()).toBe('0'))

    // Back at 7d a manual "Hours" pick is the native granularity again.
    fireEvent.click(screen.getByRole('button', { name: '7d' }))
    fireEvent.click(screen.getByRole('combobox', { name: /time granularity/i }))
    fireEvent.click(await screen.findByRole('option', { name: 'Days' }))
    await waitFor(() => expect(chartForecastCount()).toBe('0'))
    fireEvent.click(screen.getByRole('combobox', { name: /time granularity/i }))
    fireEvent.click(await screen.findByRole('option', { name: 'Hours' }))
    await waitFor(() => expect(chartForecastCount()).toBe('1'))
  })

  it('refuses a granularity that would draw too many points over the range (MON-23)', async () => {
    installProjectTotalFetch([], '6h')
    renderMonitoringPage()
    await screen.findByTestId('metrics-chart')

    // 90 days of hourly buckets is 2,160 points per series: not offered for a
    // series collected every 6 hours (its own 6 hours always is).
    fireEvent.click(screen.getByRole('button', { name: '90d' }))
    fireEvent.click(screen.getByRole('combobox', { name: /time granularity/i }))
    expect(await screen.findByRole('option', { name: 'Hours' })).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByRole('option', { name: '6 hours' })).not.toHaveAttribute('aria-disabled')
  })

  it('bumps a sticky fine pick coarser when the range grows (MON-23)', async () => {
    installProjectTotalFetch([], '6h')
    renderMonitoringPage('?gran=15min&range=90')

    await screen.findByTestId('metrics-chart')
    // Clamped to 6 hours, the finest that fits 90d: the three points stay apart.
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /time granularity/i })).toHaveTextContent('6 hours'))
    expect(chartPoints()).toBe('3')
  })

  it('bumps a sticky fine pick only as far as the native granularity (MON-23)', async () => {
    installProjectTotalFetch()
    renderMonitoringPage('?gran=15min&range=90')

    await screen.findByTestId('metrics-chart')
    // The hourly series' own granularity is always allowed, so 15 min settles
    // on Hours rather than jumping past it to 6 hours.
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /time granularity/i })).toHaveTextContent('Hours'))
  })

  it('always offers a 15 min series its native granularity, forecast included', async () => {
    // The smallest preset is 7d, where 15 min is 672 points — over the cap. A
    // 15 min scan used to be unreadable at its own resolution anywhere, and so
    // was its forecast, which only renders at the native granularity.
    installProjectTotalFetch([{ bucket: '2026-01-02T10:15:00Z', expected_count: 4, stddev: 1 }], '15m')
    renderMonitoringPage('?gran=15min')

    await screen.findByTestId('metrics-chart')
    const control = () => screen.getByRole('combobox', { name: /time granularity/i })
    await waitFor(() => expect(control()).toHaveTextContent('15 min'))
    expect(chartForecastCount()).toBe('1')

    // Not clamped at 90d either: the native pick is exempt from the cap...
    fireEvent.click(screen.getByRole('button', { name: '90d' }))
    await waitFor(() => expect(chartForecastCount()).toBe('1'))
    expect(control()).toHaveTextContent('15 min')
    fireEvent.click(control())
    expect(await screen.findByRole('option', { name: '15 min' })).not.toHaveAttribute('aria-disabled')
    // ...while a non-native pick over it is still refused.
    expect(screen.getByRole('option', { name: 'Hours' })).toHaveAttribute('aria-disabled', 'true')
  })
})

function eventTypeFixture() {
  return {
    id: 'type-1',
    project_id: 'project-1',
    name: 'page',
    display_name: 'Page',
    description: '',
    color: '#0ea5e9',
    order: 0,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    field_definitions: [
      {
        id: 'field-country',
        event_type_id: 'type-1',
        name: 'country',
        display_name: 'Country',
        field_type: 'string',
        is_required: true,
        enum_options: null,
        description: '',
        order: 0,
        sensitivity: 'pii',
      },
    ],
  }
}

function eventFixture() {
  return {
    id: 'event-1',
    project_id: 'project-1',
    event_type_id: 'type-1',
    event_type: { id: 'type-1', name: 'page', display_name: 'Page', color: '#0ea5e9' },
    name: 'checkout_completed',
    // The API always sends the key; equal to the name here, which is the case
    // where the Properties card deliberately shows no separate row.
    source_name: 'checkout_completed',
    description: 'Fired on checkout.',
    order: 0,
    status: 'live',
    sunset_at: null,
    last_seen_at: '2026-01-02T00:00:00Z',
    metric_breakdown_columns: ['platform'],
    drift_count: 2,
    tags: [{ id: 'tag-1', name: 'revenue' }],
    field_values: [
      { id: 'fv-1', field_definition_id: 'field-country', value: 'US', variable_values: [] },
    ],
    meta_values: [],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-02T00:00:00Z',
  }
}

function renderEventDetail(search = '') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/p/demo/monitoring/event/event-1${search}`]}>
        <Routes>
          <Route path="/p/:slug/monitoring/:scope/:id" element={<MonitoringDetailPage />} />
          <Route path="/p/:slug/events/:tab/:eventId/edit" element={<div>edit-page</div>} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('MonitoringDetailPage back affordance (tripl-lkox)', () => {
  function installProjectTotalOnlyFetch() {
    return vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/metrics/total')) {
        return mockJsonResponse({
          scope: 'project_total',
          scan_config_id: 'scan-1',
          event_id: null,
          event_type_id: null,
          interval: '1h',
          latest_signal: null,
          data: [metricPoint('2026-01-01T05:00:00Z', 5)],
          forecast: [],
        })
      }
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1')) {
        return mockJsonResponse({ id: 'scan-1', app_version_column: null })
      }
      if (url.includes('/api/v1/projects/demo/annotations')) return mockJsonResponse([])

      throw new Error(`Unhandled fetch: ${url}`)
    })
  }

  it('names Observe and the scope in the eyebrow of a project-total drilldown', async () => {
    // navigation.ts assigns /monitoring/project-total/ (and /monitoring/
    // event-type/) to Anomalies, under Observe. The eyebrow names the nav group
    // and the scope, as on every Observe page, in place of the separate back
    // button that sat above the header (DS-2 / MO-40); it must never point the
    // reader at Events, where they had not been.
    installProjectTotalOnlyFetch()
    const { container } = renderMonitoringPage()

    // Titled for what it shows (MO-12); the eyebrow still names the scope.
    await screen.findByRole('heading', { level: 1, name: 'Total volume' })
    expect(container.querySelector('[data-slot="page-eyebrow"]')).toHaveTextContent(
      'Observe · Project total',
    )
    expect(screen.queryByRole('button', { name: /back to/i })).toBeNull()
  })
})

describe('MonitoringDetailPage event detail', () => {
  it('renders the event-aware detail with signal banner and routes Edit to the edit page', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)

      if (url.endsWith('/api/v1/projects/demo/event-types')) {
        return mockJsonResponse([eventTypeFixture()])
      }
      if (url.endsWith('/api/v1/projects/demo/meta-fields')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/variables')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events/event-1/history')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events/event-1/metrics')) {
        return mockJsonResponse({
          scope: 'event',
          scan_config_id: 'scan-1',
          event_id: 'event-1',
          event_type_id: 'type-1',
          interval: '1h',
          latest_signal: {
            scan_config_id: 'scan-1',
            scope_type: 'event',
            scope_ref: 'event-1',
            state: 'recent',
            event_id: 'event-1',
            event_type_id: null,
            bucket: '2026-01-02T00:00:00Z',
            actual_count: 200,
            expected_count: 100,
            stddev: 10,
            z_score: 4.2,
            direction: 'spike',
          },
          data: [metricPoint('2026-01-02T00:00:00Z', 200)],
          forecast: [],
        })
      }
      if (url.includes('/api/v1/projects/demo/events/event-1/photos')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events/event-1/comments')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/users')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/settings/photo-limits')) return mockJsonResponse({ photo_max_size_mb: 10 })
      if (url.endsWith('/api/v1/projects/demo/events/event-1')) return mockJsonResponse(eventFixture())
      if (url.endsWith('/api/v1/projects/demo/scans/scan-1')) {
        return mockJsonResponse({ id: 'scan-1', app_version_column: null })
      }
      if (url.includes('/api/v1/projects/demo/annotations')) return mockJsonResponse([])

      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderEventDetail()

    expect(await screen.findByRole('heading', { name: 'checkout_completed' })).toBeInTheDocument()
    // Signal banner derived from latest_signal (spike, +100% vs baseline).
    expect(screen.getByText(/Volume spike detected/)).toBeInTheDocument()
    // Fields table shows the schema field with its sensitivity chip. The field
    // also heads a row of the Spec card that leads the page for an event that
    // is not live yet (tripl-kjhi.8), so the name appears twice.
    expect(screen.getAllByText('country').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByTestId('event-spec-card')).toBeInTheDocument()
    expect(screen.getByText('PII')).toBeInTheDocument()
    // Real breakdown column from the event surfaces in the side column.
    expect(screen.getByText('platform')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Edit/ }))
    expect(await screen.findByText('edit-page')).toBeInTheDocument()
  })
})

function installEventDetailFetch(
  opts: {
    metricsData?: EventMetricPoint[]
    event?: Record<string, unknown>
    breakdowns?: Record<string, unknown>
    latestSignal?: Record<string, unknown> | null
    tickets?: Record<string, unknown>[]
    /** The event `superseded_by_event_id` points at. `null` answers 404, the
     *  same as a successor the reader cannot see. */
    successor?: Record<string, unknown> | null
    sigmaThreshold?: number
  } = {},
) {
  const metricsData = opts.metricsData ?? [metricPoint('2026-01-02T00:00:00Z', 200)]
  const latestSignal = opts.latestSignal ?? null
  const event = opts.event ?? eventFixture()
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    const url = String(input)
    if (url.endsWith('/api/v1/projects/demo/event-types')) {
      return mockJsonResponse([eventTypeFixture()])
    }
    if (url.endsWith('/api/v1/projects/demo/meta-fields')) return mockJsonResponse([])
    if (url.endsWith('/api/v1/projects/demo/variables')) return mockJsonResponse([])
    if (url.includes('/api/v1/projects/demo/events/event-1/history')) return mockJsonResponse([])
    if (url.includes('/api/v1/projects/demo/events/event-1/metrics/breakdowns')) {
      return mockJsonResponse(opts.breakdowns ?? {
        event_id: 'event-1',
        scan_config_id: 'scan-1',
        interval: '1h',
        columns: [],
        selected_column: null,
        series: [],
      })
    }
    if (url.includes('/api/v1/projects/demo/events/event-1/metrics')) {
      return mockJsonResponse({
        scope: 'event',
        scan_config_id: 'scan-1',
        event_id: 'event-1',
        event_type_id: 'type-1',
        interval: '1h',
        latest_signal: latestSignal,
        data: metricsData,
        forecast: [],
        ...(opts.sigmaThreshold === undefined ? {} : { sigma_threshold: opts.sigmaThreshold }),
      })
    }
    if (url.includes('/api/v1/projects/demo/events/event-1/photos')) return mockJsonResponse([])
    if (url.includes('/api/v1/projects/demo/events/event-1/comments')) return mockJsonResponse([])
    if (url.endsWith('/api/v1/users')) return mockJsonResponse([])
    if (url.endsWith('/api/v1/settings/photo-limits')) return mockJsonResponse({ photo_max_size_mb: 10 })
    if (url.includes('/api/v1/projects/demo/events/event-1/implementation-tickets')) {
      return mockJsonResponse(opts.tickets ?? [])
    }
    if (url.endsWith('/api/v1/projects/demo/events/event-1')) return mockJsonResponse(event)
    if (url.endsWith('/api/v1/projects/demo/events/event-2')) {
      return opts.successor
        ? mockJsonResponse(opts.successor)
        : new Response('{}', { status: 404, headers: { 'Content-Type': 'application/json' } })
    }
    if (url.endsWith('/api/v1/projects/demo/scans/scan-1')) {
      return mockJsonResponse({ id: 'scan-1', app_version_column: null })
    }
    if (url.includes('/api/v1/projects/demo/annotations')) return mockJsonResponse([])

    throw new Error(`Unhandled fetch: ${url}`)
  })
}

// A drop that bottomed out at zero: the detector clamps such z-scores to a
// constant magnitude (here -20), so the banner must read the outcome
// ("dropped to zero") rather than the uninformative number.
function dropToZeroSignal(): Record<string, unknown> {
  return {
    scan_config_id: 'scan-1',
    scope_type: 'event',
    scope_ref: 'event-1',
    state: 'recent',
    event_id: 'event-1',
    event_type_id: null,
    bucket: '2026-01-02T00:00:00Z',
    actual_count: 0,
    expected_count: 120,
    stddev: 6,
    z_score: -20,
    direction: 'drop',
  }
}

function renderLegacyEventDetail() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/p/demo/events/detail/event-1']}>
        <Routes>
          {/* Legacy scope-less shape mounted directly to exercise the defensive
              scope default; in the app this URL redirects to the canonical route. */}
          <Route path="/p/:slug/events/detail/:eventId" element={<MonitoringDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('MonitoringDetailPage event-detail header and semantics', () => {
  it('renders without crashing when mounted from the legacy scope-less route', async () => {
    installEventDetailFetch()
    renderLegacyEventDetail()

    // B1: resolveDetailScope defaults to the event scope when only an eventId is
    // present, so the page renders the event hero instead of throwing.
    expect(await screen.findByRole('heading', { name: 'checkout_completed' })).toBeInTheDocument()
  })

  it('titles the page when the event has no name (tripl-wkwv.5)', async () => {
    // windy-ios holds one event whose name is the empty string. The <h1>
    // rendered it raw, so the page had an empty top-level heading.
    installEventDetailFetch({ event: { ...eventFixture(), name: '' } })
    renderEventDetail()

    expect(
      await screen.findByRole('heading', { level: 1, name: '(unnamed event)' }),
    ).toBeInTheDocument()
  })

  it('keeps only live actions in the primary row and tucks coming-soon ones into an overflow menu', async () => {
    installEventDetailFetch()
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    // Live actions stay in the primary row, enabled.
    expect(screen.getByRole('button', { name: 'Metrics' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled()

    // Coming-soon actions no longer sit in the primary row as inert disabled buttons.
    expect(screen.queryByRole('button', { name: 'Watch' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Implementation' })).not.toBeInTheDocument()

    // They live behind an overflow ("…") menu instead.
    expect(screen.getByRole('button', { name: 'More actions' })).toBeInTheDocument()
  })

  it('surfaces an inline volume-vs-baseline mini-chart beside the signal banner (tripl-yfsj.11)', async () => {
    installEventDetailFetch({
      latestSignal: dropToZeroSignal(),
      metricsData: [
        metricPoint('2026-01-01T00:00:00Z', 120),
        {
          ...metricPoint('2026-01-02T00:00:00Z', 0),
          is_anomaly: true,
          anomaly_direction: 'drop',
          expected_count: 120,
          stddev: 6,
          z_score: -20,
        },
      ],
    })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    // The claim in the banner ("vs. baseline") is now visible in context: a
    // compact chart sits in the hero, not only behind the Metrics tab, and it
    // reuses the already-fetched series (both points reach it).
    const miniChart = within(await screen.findByTestId('signal-volume-chart'))
      .getByTestId('metrics-chart')
    expect(miniChart).toHaveAttribute('data-points', '2')
  })

  it('hands the signal mini-chart the served sigma threshold (tripl-2yww)', async () => {
    installEventDetailFetch({ latestSignal: dropToZeroSignal(), sigmaThreshold: 6 })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    const miniChart = within(await screen.findByTestId('signal-volume-chart'))
      .getByTestId('metrics-chart')
    expect(miniChart).toHaveAttribute('data-sigma-threshold', '6')
  })

  it('names the baseline instead of titling a chart that cannot draw one (tripl-v2lm)', async () => {
    // expected_count/stddev are persisted only on FLAGGED buckets, so the
    // dashed expectation has a single non-null point and paints nothing. The
    // panel was titled "Volume vs. baseline" above one bare series — the
    // comparison that justifies the alert, promised and then not shown.
    installEventDetailFetch({
      latestSignal: {
        scan_config_id: 'scan-1',
        scope_type: 'event',
        scope_ref: 'event-1',
        state: 'recent',
        event_id: 'event-1',
        event_type_id: null,
        bucket: '2026-01-02T00:00:00Z',
        actual_count: 8_400,
        expected_count: 2_915,
        stddev: 140,
        z_score: 39.6,
        direction: 'spike',
      },
    })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    const panel = within(await screen.findByTestId('signal-volume-chart'))
    expect(panel.getByText('Volume')).toBeInTheDocument()
    expect(panel.queryByText(/vs\. baseline/i)).toBeNull()
    // Locale-safe: the page groups through toLocaleString, like every other count.
    expect(
      panel.getByText(`baseline ${(2915).toLocaleString()} at the flagged bucket`),
    ).toBeInTheDocument()
  })

  it('keeps a sub-unit baseline readable instead of rounding it to the no-baseline case', async () => {
    // `expected_count` is a mean of prior buckets, so a rare event's baseline is
    // legitimately below 1. The caption's `> 0` gate admits it and then
    // `Math.round` printed "baseline 0" — the caption contradicting the gate
    // that had just decided a baseline existed, on the same page whose signal
    // card already formats this value-aware.
    installEventDetailFetch({
      latestSignal: { ...dropToZeroSignal(), expected_count: 0.4, z_score: -6.2 },
    })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    const panel = within(await screen.findByTestId('signal-volume-chart'))
    expect(
      panel.getByText(`baseline ${(0.4).toLocaleString()} at the flagged bucket`),
    ).toBeInTheDocument()
    expect(panel.queryByText(/baseline 0 at the flagged bucket/)).toBeNull()
  })

  it('shows the event discussion on the detail page, where viewers now land (EV-34)', async () => {
    installEventDetailFetch()
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    expect(
      await screen.findByText('Nothing raised yet. Questions and notes here stay out of the spec.'),
    ).toBeInTheDocument()
  })

  it('counts the discussion in the hero and jumps to it (JR-7)', async () => {
    installEventDetailFetch()
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    // The thread sits below the tabs; the chip says it exists and how big it is.
    const chip = await screen.findByRole('button', { name: 'Discussion (0)' })
    // Spied, not replaced and deleted: deleting it also removed test-setup's
    // polyfill, and every Radix Select opened later in the file then threw.
    const scrollIntoView = vi.spyOn(Element.prototype, 'scrollIntoView')
    try {
      fireEvent.click(chip)
      expect(scrollIntoView).toHaveBeenCalled()
      expect(document.getElementById('event-discussion')).not.toBeNull()
    } finally {
      scrollIntoView.mockRestore()
    }
  })

  it('gives a signal its next steps: annotate, discuss, and the alert inbox (MO-4 / JR-5)', async () => {
    installEventDetailFetch({ latestSignal: { ...dropToZeroSignal(), actual_count: 81, z_score: -6.5 } })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })
    await screen.findByText(/Volume drop detected/)

    expect(screen.getByRole('button', { name: 'Annotate' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Discuss' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'View alerts' })).toHaveAttribute(
      'href',
      '/p/demo/alerting',
    )
  })

  it('states the flagged bucket as one sentence and says why it was flagged (MO-2)', async () => {
    installEventDetailFetch({
      latestSignal: {
        ...dropToZeroSignal(),
        direction: 'spike',
        actual_count: 5767,
        expected_count: 3174,
        z_score: 16,
      },
      sigmaThreshold: 4,
    })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    const summary = await screen.findByTestId('signal-summary')
    expect(summary.textContent).toMatch(/5\D?767 events, 82% above the expected 3\D?174/)
    expect(summary.textContent).toContain('(16.0σ)')
    expect(summary.textContent).toContain('Why flagged: 16.0σ from the expected value; anything past 4σ is flagged.')
    // The 4-up grid of raw figures is gone.
    expect(screen.queryByText('Flagged bucket')).toBeNull()
    expect(screen.queryByText('Deviation')).toBeNull()
  })

  it('marks one event as verified from the overflow menu (JR-8)', async () => {
    const spy = installEventDetailFetch()
    const base = spy.getMockImplementation()!
    spy.mockImplementation(async (input, init) => {
      if (String(input).includes('/api/v1/projects/demo/events/bulk-update')) return mockJsonResponse({})
      return base(input, init)
    })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    fireEvent.keyDown(screen.getByRole('button', { name: 'More actions' }), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('menuitem', { name: /Mark as verified/ }))

    await waitFor(() => {
      const post = spy.mock.calls.find(([url]) => String(url).includes('/events/bulk-update'))
      expect(post).toBeDefined()
      const body = JSON.parse(String(post![1]?.body)) as Record<string, unknown>
      expect(body).toMatchObject({ event_ids: ['event-1'], reviewed: true })
    })
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Marked as verified'))
  })

  it('omits the signal mini-chart when the event has no active anomaly', async () => {
    installEventDetailFetch() // latest_signal defaults to null → no banner, no chart
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    expect(screen.queryByTestId('signal-volume-chart')).not.toBeInTheDocument()
  })

  it('reads "dropped to zero" instead of a clamped z-score when a drop bottoms out (tripl-yfsj.9)', async () => {
    installEventDetailFetch({ latestSignal: dropToZeroSignal() })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    const banner = screen.getByText(/Volume drop detected/)
    expect(banner.textContent).toContain('dropped to zero')
    // The uninformative clamped magnitude (z=-20.0) is suppressed in the banner.
    expect(banner.textContent).not.toMatch(/z\s*=/)
  })

  it('names a zero baseline in the banner rather than dropping the clause (tripl-l429.27)', async () => {
    // An event firing where nothing was expected. The "vs. baseline" clause used
    // to be omitted silently, so the banner was quietly shorter on exactly the
    // signals that moved the most and a reader could not tell whether the
    // comparison was missing or undefined.
    installEventDetailFetch({
      latestSignal: {
        ...dropToZeroSignal(),
        direction: 'spike',
        actual_count: 137,
        expected_count: 0,
        stddev: 1,
        z_score: 9.1,
      },
    })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    const banner = screen.getByText(/Volume spike detected/)
    expect(banner.textContent).toContain('no baseline to compare against')
    // Never the undefined ratio written as a number.
    expect(banner.textContent).not.toContain('vs. baseline')
    expect(banner.textContent).not.toMatch(/[+-]?\d+% vs/)
  })

  it('states a partial drop as a % change and keeps the z-score in the tooltip (MO-2)', async () => {
    installEventDetailFetch({
      latestSignal: {
        ...dropToZeroSignal(),
        actual_count: 81,
        expected_count: 100,
        stddev: 6,
        z_score: -3.3,
      },
    })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    const banner = screen.getByText(/Volume drop detected/)
    expect(banner.textContent).toMatch(/19(\.0)?% vs\. baseline/)
    expect(banner.textContent).not.toMatch(/z\s*=/)
    expect(banner).toHaveAttribute('title', expect.stringContaining('z=-3.3'))
    expect(banner.textContent).not.toContain('dropped to zero')
  })

  it('shows platform share anomalies separately from breakdown volume series', async () => {
    installEventDetailFetch({
      breakdowns: {
        event_id: 'event-1',
        scan_config_id: 'scan-1',
        interval: '1h',
        columns: ['platform'],
        selected_column: 'platform',
        series: [
          {
            breakdown_value: 'ios',
            is_other: false,
            total_count: 60,
            data: [metricPoint('2026-01-02T00:00:00Z', 10)],
            parity_anomalies: [
              {
                bucket: '2026-01-02T00:00:00Z',
                actual_share: 0.1,
                expected_share: 0.5,
                stddev: 0.02,
                z_score: -20,
                direction: 'drop',
              },
            ],
          },
        ],
      },
    })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    const breakdownsTab = screen.getByRole('tab', { name: /Breakdowns/i })
    fireEvent.pointerDown(breakdownsTab, { button: 0, ctrlKey: false })
    fireEvent.mouseDown(breakdownsTab, { button: 0, ctrlKey: false })
    fireEvent.pointerUp(breakdownsTab, { button: 0, ctrlKey: false })
    fireEvent.mouseUp(breakdownsTab, { button: 0, ctrlKey: false })
    fireEvent.click(breakdownsTab)

    expect(await screen.findByText('platform share anomalies')).toBeInTheDocument()
    expect(screen.getByLabelText('ios share drop: 50.0% -> 10.0%')).toBeInTheDocument()

    // Event-scope breakdowns keep today's rendering exactly: the 'events'
    // label and NO value formatter (tripl-4dej regression guard).
    const chart = screen.getByTestId('multi-chart')
    expect(chart).toHaveAttribute('data-series-label', 'events')
    expect(chart).toHaveAttribute('data-value-sample', '')
  })

  it('filters breakdown series to the selected values (tripl-egt5)', async () => {
    const point = metricPoint('2026-01-02T00:00:00Z', 10)
    installEventDetailFetch({
      breakdowns: {
        event_id: 'event-1',
        scan_config_id: 'scan-1',
        interval: '1h',
        columns: ['platform'],
        selected_column: 'platform',
        series: [
          { breakdown_value: 'ios', is_other: false, total_count: 60, data: [point], parity_anomalies: [] },
          { breakdown_value: 'android', is_other: false, total_count: 40, data: [point], parity_anomalies: [] },
          { breakdown_value: 'web', is_other: false, total_count: 20, data: [point], parity_anomalies: [] },
        ],
      },
    })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    const breakdownsTab = screen.getByRole('tab', { name: /Breakdowns/i })
    fireEvent.pointerDown(breakdownsTab, { button: 0, ctrlKey: false })
    fireEvent.mouseDown(breakdownsTab, { button: 0, ctrlKey: false })
    fireEvent.pointerUp(breakdownsTab, { button: 0, ctrlKey: false })
    fireEvent.mouseUp(breakdownsTab, { button: 0, ctrlKey: false })
    fireEvent.click(breakdownsTab)

    // Default: every value renders.
    const chart = await screen.findByTestId('multi-chart')
    expect(chart).toHaveAttribute('data-labels', 'ios|android|web')

    // Picking one value isolates its series…
    fireEvent.click(screen.getByRole('button', { name: 'Toggle android' }))
    await waitFor(() =>
      expect(screen.getByTestId('multi-chart')).toHaveAttribute('data-labels', 'android'))
    expect(screen.getByRole('button', { name: 'Toggle android' }))
      .toHaveAttribute('aria-pressed', 'true')

    // …picking a second adds it (response order preserved)…
    fireEvent.click(screen.getByRole('button', { name: 'Toggle web' }))
    await waitFor(() =>
      expect(screen.getByTestId('multi-chart')).toHaveAttribute('data-labels', 'android|web'))

    // …and "Show all" resets to the full set.
    fireEvent.click(screen.getByRole('button', { name: 'Show all' }))
    await waitFor(() =>
      expect(screen.getByTestId('multi-chart')).toHaveAttribute('data-labels', 'ios|android|web'))
  })

  it('uses the shared page header, with no second in-page breadcrumb (DS-3 / JR-33)', async () => {
    installEventDetailFetch()
    const { container } = renderEventDetail()
    const heading = await screen.findByRole('heading', { level: 1, name: 'checkout_completed' })

    // The top bar already carries "Plan › Events › <name>"; the in-page trail
    // under it repeated that 120px lower.
    expect(screen.queryByRole('navigation', { name: 'Breadcrumb' })).not.toBeInTheDocument()
    // The eyebrow names the nav group and the collection instead.
    expect(container.querySelector('[data-slot="page-eyebrow"]')).toHaveTextContent('Plan · Event')
    // A display name, set in the page title's sans, not mono (DS-17).
    expect(heading).not.toHaveClass('mono')
    // The KPI strip sits in the header's stats slot (DS-5).
    expect(
      container.querySelector('[data-slot="page-stats"] [data-slot="mini-stat-strip"]'),
    ).not.toBeNull()
  })

  it('explains the empty 24h metrics instead of rendering a bare dash', async () => {
    installEventDetailFetch({ metricsData: [] })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    // The strip waits for the series (DS-25), then explains the empty state.
    await waitFor(() =>
      expect(screen.getByText('Volume · 24h').closest('[title]')).toHaveAttribute(
        'title',
        'No events in the last 24h',
      ))
    // The Events list's own sentence for the same state (MON-28).
    expect(screen.getByText('Change vs prior 24h').closest('[title]')).toHaveAttribute(
      'title',
      'No metrics collected for this event in the last 48h.',
    )
  })

  it('does not claim "no events" before the series arrives (DS-25)', async () => {
    const fetchSpy = installEventDetailFetch({ metricsData: [] })
    const base = fetchSpy.getMockImplementation()!
    fetchSpy.mockImplementation(async (input, init) => {
      if (String(input).includes('/events/event-1/metrics')) return new Promise<Response>(() => {})
      return base(input, init)
    })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    expect(screen.getByText('Volume · 24h').closest('[title]')).toBeNull()
    expect(screen.queryByTitle('No events in the last 24h')).toBeNull()
  })

  it('de-emphasises and explains empty drift and last-seen stats', async () => {
    // Visual de-emphasis (var(--fg-faint)) is verified by inspection; the testable
    // contract is the hover hint that explains the empty "0" / "—" values.
    installEventDetailFetch({
      event: { ...eventFixture(), drift_count: 0, last_seen_at: null },
    })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    expect(screen.getByText('Schema drifts').closest('[title]')).toHaveAttribute(
      'title',
      'No schema drifts detected',
    )
    // "Last seen" also labels a Properties row, so pick the stat card (the only
    // "Last seen" wrapped in a title-bearing element).
    const lastSeenStat = screen
      .getAllByText('Last seen')
      .map(node => node.closest('[title]'))
      .find(Boolean)
    expect(lastSeenStat).toHaveAttribute('title', 'No hits recorded yet')
  })

  it('coaches empty volume metrics and empty change history instead of blank panels', async () => {
    // No metric points and (per installEventDetailFetch) no history entries, so
    // both lower panels should render their coached empty states.
    installEventDetailFetch({ metricsData: [] })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    expect(screen.getByText('No metrics data available')).toBeInTheDocument()
    expect(
      screen.getByText('Run a scan to start collecting volume metrics for this scope.'),
    ).toBeInTheDocument()

    expect(screen.getByText('No recent changes')).toBeInTheDocument()
    expect(
      screen.getByText("Edits to this event's definition will show up here."),
    ).toBeInTheDocument()
  })

  it('exposes table semantics for the Fields and Properties tables', async () => {
    installEventDetailFetch()
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    expect(screen.getByRole('table', { name: 'Fields' })).toBeInTheDocument()

    const properties = screen.getByRole('table', { name: 'Properties' })
    expect(properties).toBeInTheDocument()
    expect(within(properties).getAllByRole('row').length).toBeGreaterThan(0)
    expect(within(properties).getAllByRole('rowheader')[0]).toHaveTextContent('Event type')
  })

  it('shows the scan identity only once a rename has parted it from the name', async () => {
    installEventDetailFetch()
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })
    // The fixture's identity equals its name, so a row saying so would be noise.
    expect(screen.queryByText('Scan identity')).not.toBeInTheDocument()

    cleanup()
    installEventDetailFetch({
      event: {
        ...eventFixture(),
        name: 'Checkout completed',
        source_name: 'checkout_completed',
      },
    })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'Checkout completed' })

    // Once they differ this row is the only place that says which event the
    // warehouse is still feeding (tripl-u2h9.10).
    const properties = screen.getByRole('table', { name: 'Properties' })
    expect(within(properties).getByText('Scan identity')).toBeInTheDocument()
    expect(within(properties).getByText('checkout_completed')).toBeInTheDocument()
  })

  it('tells the authoring date and the first traffic apart (tripl-kjhi.10)', async () => {
    installEventDetailFetch({
      event: { ...eventFixture(), first_seen_at: '2026-01-03T00:00:00Z' },
    })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    const properties = screen.getByRole('table', { name: 'Properties' })
    const row = (label: string) =>
      within(properties).getByText(label).closest('[role="row"]') as HTMLElement
    expect(within(row('Created')).getByText(/2026/)).toHaveTextContent(/Jan 1|01/)
    expect(within(row('First seen')).getByText(/2026/)).toHaveTextContent(/Jan 3|03/)
    expect(within(row('Owner')).getByText('—')).toBeInTheDocument()
  })

  it('says an unseen event has not been seen, rather than naming its authoring date', async () => {
    installEventDetailFetch({ event: { ...eventFixture(), first_seen_at: null } })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    const properties = screen.getByRole('table', { name: 'Properties' })
    const firstSeen = within(properties)
      .getByText('First seen')
      .closest('[role="row"]') as HTMLElement
    expect(within(firstSeen).getByText('—')).toBeInTheDocument()
  })

  it('names the successor, and links to it, once one is set (tripl-h2sx.13)', async () => {
    installEventDetailFetch({
      event: { ...eventFixture(), superseded_by_event_id: 'event-2' },
      successor: { ...eventFixture(), id: 'event-2', name: 'checkout_finished' },
    })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    const properties = screen.getByRole('table', { name: 'Properties' })
    const link = await within(properties).findByRole('link', { name: 'checkout_finished' })
    expect(link).toHaveAttribute('href', '/p/demo/monitoring/event/event-2')
  })

  it('falls back to the successor id when the successor cannot be read', async () => {
    // A link to a name we do not have is worse than the id: the id is at least
    // something the reader can look up.
    installEventDetailFetch({
      event: { ...eventFixture(), superseded_by_event_id: 'event-2' },
      successor: null,
    })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    const properties = screen.getByRole('table', { name: 'Properties' })
    expect(await within(properties).findByText('event-2')).toBeInTheDocument()
    expect(within(properties).queryByRole('link')).toBeNull()
  })

  it('says nothing about a replacement when none is named', async () => {
    installEventDetailFetch()
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    expect(screen.queryByText('Replaced by')).not.toBeInTheDocument()
  })

  it('names an owner the roster cannot resolve as unknown, not as still loading', async () => {
    // The harness answers no `/users` request, so the roster query fails —
    // the same outcome as a member who has since been removed.
    installEventDetailFetch({ event: { ...eventFixture(), owner_id: 'u-ghost' } })
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    const properties = screen.getByRole('table', { name: 'Properties' })
    const ownerRow = within(properties).getByText('Owner').closest('[role="row"]') as HTMLElement
    expect(await within(ownerRow).findByText('Unknown user')).toBeInTheDocument()
  })
})

describe('MonitoringDetailPage catalog-metric drilldown', () => {
  function metricSeriesPoint(bucket: string, value: number) {
    return {
      bucket,
      value,
      expected_count: null,
      stddev: null,
      is_anomaly: false,
      anomaly_direction: null,
      z_score: null,
    }
  }

  function metricDefinitionResponse(
    interval: string | null,
    overrides: Record<string, unknown> = {},
  ) {
    return {
      ...metricDefinitionBase(interval),
      ...overrides,
    }
  }

  function metricDefinitionBase(interval: string | null) {
    return {
      id: 'metric-1',
      project_id: 'p-1',
      name: 'dau',
      display_name: 'Daily Active Users',
      description: '',
      color: '#8884d8',
      order: 0,
      unit: null,
      status: 'active',
      owner_id: null,
      reviewed: false,
      kind: 'sql',
      aggregation: null,
      composition: null,
      config: {},
      breakdown_columns: [],
      breakdown_values_limit: null,
      app_version_column: null,
      platform_column: null,
      data_source_id: null,
      interval,
      replay_chunk_interval: null,
      numerator_event_id: null,
      numerator_event_type_id: null,
      denominator_event_id: null,
      denominator_event_type_id: null,
      anomaly_detection_enabled: true,
      last_collected_at: null,
      last_collection_status: null,
      last_collection_error: null,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    }
  }

  function metricAnnotationFixture(overrides: Record<string, unknown> = {}) {
    return {
      id: 'ann-1',
      project_id: 'p-1',
      scope_type: 'metric',
      scope_ref: 'metric-1',
      bucket: '2026-01-02T00:00:00Z',
      label: 'v2.0 release',
      description: null,
      color: '#ef4444',
      source: 'manual',
      url: null,
      created_by_user_id: null,
      created_at: '2026-01-01T00:00:00Z',
      ...overrides,
    }
  }

  function installMetricDetailFetch(
    interval: string,
    definitionOverrides: Record<string, unknown> = {},
    seriesOverrides: Record<string, unknown> = {},
    annotations: Array<Record<string, unknown>> = [],
    breakdownsOverrides: Record<string, unknown> = {},
    versionsOverrides: Record<string, unknown> = {},
    settledCollectionStatus: 'success' | 'error' | null = null,
  ) {
    let collectStarted = false
    return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)

      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      // The Definition card resolves each referenced event by id (MET-2).
      if (url.endsWith('/api/v1/projects/demo/events/event-a')) {
        return mockJsonResponse({ id: 'event-a', name: 'checkout_completed' })
      }
      if (url.endsWith('/api/v1/projects/demo/events/event-b')) {
        return mockJsonResponse({ id: 'event-b', name: 'session_started' })
      }
      if (url.endsWith('/api/v1/projects/demo/fact-tables')) {
        return mockJsonResponse({
          items: [
            {
              id: 'ft-1',
              project_id: 'p-1',
              name: 'orders',
              display_name: 'Orders',
              description: '',
              color: '#6366f1',
              order: 0,
              data_source_id: null,
              timestamp_column: 'created_at',
              created_at: '2026-01-01T00:00:00Z',
              updated_at: '2026-01-01T00:00:00Z',
            },
          ],
          total: 1,
        })
      }
      if (url.endsWith('/api/v1/projects/demo/fact-tables/ft-1')) {
        return mockJsonResponse({
          id: 'ft-1',
          project_id: 'p-1',
          name: 'orders',
          display_name: 'Orders',
          description: '',
          color: '#6366f1',
          order: 0,
          data_source_id: null,
          timestamp_column: 'created_at',
          sql: 'SELECT * FROM orders',
          columns: [{ name: 'created_at', type: 'timestamp' }],
          identifier_columns: [],
          row_filters: [],
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
        })
      }
      if (url.includes('/api/v1/projects/demo/metrics/metric-1/series')) {
        return mockJsonResponse({
          metric_id: 'metric-1',
          scan_config_id: null,
          interval,
          latest_signal: null,
          // Two same-day points: they collapse into one daily bucket only when
          // the effective granularity is 'day'.
          data: [
            metricSeriesPoint('2026-01-02T05:00:00Z', 10),
            metricSeriesPoint('2026-01-02T18:00:00Z', 20),
          ],
          ...seriesOverrides,
        })
      }
      if (url.includes('/api/v1/projects/demo/metrics/metric-1/breakdowns')) {
        return mockJsonResponse({
          metric_id: 'metric-1',
          scan_config_id: null,
          interval,
          columns: [],
          selected_column: null,
          series: [],
          ...breakdownsOverrides,
        })
      }
      if (url.includes('/api/v1/projects/demo/metrics/metric-1/versions')) {
        return mockJsonResponse({
          metric_id: 'metric-1',
          scan_config_id: null,
          app_version_column: 'app_version',
          interval,
          latest_version: '2.0.0',
          versions: [
            { version: '2.0.0', is_other: false, is_latest: true, is_active: true },
          ],
          series: [
            {
              version: '2.0.0',
              is_other: false,
              is_latest: true,
              is_active: true,
              total_value: 0.3,
              data: [
                metricSeriesPoint('2026-01-01T00:00:00Z', 0.1),
                metricSeriesPoint('2026-01-02T00:00:00Z', 0.2),
              ],
            },
          ],
          ...versionsOverrides,
        })
      }
      // Manual "Collect now" — the POST the scenario's collect step hangs on.
      if (url.endsWith('/api/v1/projects/demo/metrics/metric-1/collect')) {
        collectStarted = true
        return mockJsonResponse({
          metric_id: 'metric-1',
          status: 'queued',
          window_from: null,
          window_to: null,
          task_id: 'task-1',
          // The batch is capped, so a click reports the size it actually got.
          metric_count: 3,
        })
      }
      if (url.endsWith('/api/v1/projects/demo/metrics/metric-1')) {
        return mockJsonResponse(metricDefinitionResponse(interval, {
          ...definitionOverrides,
          ...(collectStarted && settledCollectionStatus
            ? { last_collection_status: settledCollectionStatus }
            : {}),
        }))
      }
      if (url.includes('/api/v1/projects/demo/annotations')) {
        if (init?.method === 'POST') {
          const posted = JSON.parse(String(init.body)) as Record<string, unknown>
          return new Response(JSON.stringify(metricAnnotationFixture(posted)), {
            status: 201,
            headers: { 'Content-Type': 'application/json' },
          })
        }
        return mockJsonResponse(annotations)
      }

      throw new Error(`Unhandled fetch: ${url}`)
    })
  }

  function renderMetricDetail(auth: AuthContextValue | null = null) {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    const result = render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={auth}>
          <MemoryRouter initialEntries={['/p/demo/monitoring/metric/metric-1']}>
            <Routes>
              <Route path="/p/:slug/monitoring/:scope/:id" element={<MonitoringDetailPage />} />
            </Routes>
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )
    return { ...result, queryClient }
  }

  it('offers a viewer no edit, collect, delete or annotation controls (MON-6)', async () => {
    installMetricDetailFetch('1h')
    renderMetricDetail({
      user: {
        id: 'viewer-1',
        email: 'viewer@example.com',
        name: 'Viewer',
        role: 'viewer',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
      },
      status: 'authenticated',
      error: null,
      isLoggingOut: false,
      logout: async () => {},
      refresh: () => {},
    })

    await screen.findByTestId('metrics-chart')
    expect(await screen.findByRole('heading', { name: 'Annotations' })).toBeInTheDocument()
    expect(screen.getByText(/Adding and removing them is done by an editor or owner/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Collect now|Recompute/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Label')).not.toBeInTheDocument()
  })

  it('takes an Annotate handed over from the Anomalies list once the definition loads', async () => {
    // The row menu navigates here with the bucket in the location state. The
    // metric page paints a skeleton until its definition arrives, so focusing
    // on arrival found no form and the handoff looked ignored (i9mt.10).
    installMetricDetailFetch('1h')
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={null}>
          <MemoryRouter
            initialEntries={[
              {
                pathname: '/p/demo/monitoring/metric/metric-1',
                state: { annotateBucket: '2026-04-01T10:00:00Z' },
              },
            ]}
          >
            <Routes>
              <Route path="/p/:slug/monitoring/:scope/:id" element={<MonitoringDetailPage />} />
            </Routes>
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )

    const label = await screen.findByLabelText('Label')
    await waitFor(() => expect(label).toHaveFocus())
  })

  it('says a draft is not collected and invites a description (JR-16)', async () => {
    installMetricDetailFetch('1h', { status: 'draft' }, { data: [] })
    renderMetricDetail()

    expect(await screen.findByText("This metric is a draft and isn't collected.")).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Add a description…' })).toHaveAttribute(
      'href',
      '/p/demo/metrics/metric-1/edit',
    )
  })

  it('tells an active SQL metric with no values to compute now (JR-16)', async () => {
    installMetricDetailFetch('1h', {}, { data: [] })
    renderMetricDetail()

    expect(
      await screen.findByText('No values yet — compute now or wait for the next scheduled run.'),
    ).toBeInTheDocument()
  })

  it('opens on 7d like every scope and keeps the 1d interval as its granularity (MON-43, tripl-4m86)', async () => {
    const fetchSpy = installMetricDetailFetch('1d')
    renderMetricDetail()

    const chart = await screen.findByTestId('metrics-chart')
    await waitFor(() => expect(chart).toHaveAttribute('data-points', '1'))
    expect(chart).toHaveAttribute('data-first-bucket', '2026-01-02T00:00:00.000Z')
    const seriesUrl = fetchSpy.mock.calls
      .map(([input]) => String(input))
      .find(url => url.includes('/api/v1/projects/demo/metrics/metric-1/series'))
    expect(seriesUrl).toBeDefined()
    const range = new URL(seriesUrl!, 'http://localhost').searchParams
    const from = new Date(range.get('from')!).getTime()
    const to = new Date(range.get('to')!).getTime()
    expect(to - from).toBe(7 * 24 * 60 * 60 * 1000)
  })

  it('threads the metric series sigma threshold into the chart (tripl-4cgl)', async () => {
    // A project that moved its sigma to 6: `adaptMetricSeries` has to carry the
    // served value, or the catalog metric's band falls back to 4 while the
    // event charts on the same page draw 6.
    installMetricDetailFetch('1h', {}, { sigma_threshold: 6 })
    renderMetricDetail()

    const chart = await screen.findByTestId('metrics-chart')
    await waitFor(() => expect(chart).toHaveAttribute('data-sigma-threshold', '6'))
  })

  it('keeps the hourly default for sub-daily metrics', async () => {
    installMetricDetailFetch('1h')
    renderMetricDetail()

    const chart = await screen.findByTestId('metrics-chart')
    await waitFor(() => expect(chart).toHaveAttribute('data-points', '2'))
  })

  it('renders percent-unit metrics ×100 in the stat card and chart formatter (tripl-nxk2.1)', async () => {
    installMetricDetailFetch(
      '1d',
      { unit: '%' },
      {
        latest_signal: {
          scan_config_id: null,
          scope_type: 'metric',
          scope_ref: 'metric-1',
          state: 'latest_scan',
          event_id: null,
          event_type_id: null,
          bucket: '2026-01-02T00:00:00Z',
          actual_count: 0.08,
          expected_count: 0.05,
          stddev: 0.01,
          z_score: 3,
          direction: 'spike',
        },
      },
    )
    renderMetricDetail()

    const chart = await screen.findByTestId('metrics-chart')
    // The percent-aware formatter reached the chart: 0.08 → '8%'.
    expect(chart).toHaveAttribute('data-value-sample', '8%')
    // The latest-signal summary renders the stored fractions ×100 (a sentence
    // now, not a 4-up stat grid — MO-2).
    const summary = screen.getByTestId('signal-summary')
    expect(summary.textContent).toMatch(/: 8%, 60% above the expected 5% \(/)
  })

  // DS-31 / MET-40: only '%' used to get a formatter, so a 0.0045 s latency
  // ticked and tooltipped as '0' and a '$' metric read '1,234 $' in the tooltip.
  it('formats a sub-1 non-percent metric on the axis and in the tooltip', async () => {
    installMetricDetailFetch('1d', { unit: 's' })
    renderMetricDetail()

    const chart = await screen.findByTestId('metrics-chart')
    await waitFor(() => expect(chart).toHaveAttribute('data-axis-small', '0.0045'))
    expect(chart).toHaveAttribute('data-tooltip-sample', '0.0045 s')
  })

  it('leads with the currency symbol in the tooltip of a $ metric', async () => {
    installMetricDetailFetch(
      '1d',
      { unit: '$' },
      {
        latest_signal: {
          scan_config_id: null,
          scope_type: 'metric',
          scope_ref: 'metric-1',
          state: 'latest_scan',
          event_id: null,
          event_type_id: null,
          bucket: '2026-01-02T00:00:00Z',
          actual_count: 1234,
          expected_count: 1000,
          stddev: 10,
          z_score: 3,
          direction: 'spike',
        },
      },
    )
    renderMetricDetail()

    const chart = await screen.findByTestId('metrics-chart')
    await waitFor(() => expect(chart).toHaveAttribute('data-tooltip-large', '$1,234'))
    // The signal summary spells the value the same way the tooltip does.
    const summary = screen.getByTestId('signal-summary')
    expect(summary.textContent).toMatch(/: \$1,234, 23% above the expected \$1,000 \(/)
  })

  it('labels the primary tab and card "Value" for the metric scope, not "Volume"', async () => {
    installMetricDetailFetch('1d')
    renderMetricDetail()

    await screen.findByTestId('metrics-chart')
    // Catalog metrics (ratios/averages) are values, not volumes.
    expect(screen.getByRole('tab', { name: 'Value' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Value' })).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: 'Volume' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Volume' })).not.toBeInTheDocument()
  })

  it('renders the Annotations card with metric-scope annotations (tripl-nxk2.13)', async () => {
    installMetricDetailFetch('1d', {}, {}, [metricAnnotationFixture()])
    renderMetricDetail()

    await screen.findByTestId('metrics-chart')
    expect(screen.getByRole('heading', { name: 'Annotations' })).toBeInTheDocument()
    expect(await screen.findByText('v2.0 release')).toBeInTheDocument()
    expect(screen.getByText('(1)')).toBeInTheDocument()
  })

  it('creates a metric-scope annotation with scope_type metric and the metric id', async () => {
    const fetchSpy = installMetricDetailFetch('1d')
    renderMetricDetail()

    await screen.findByTestId('metrics-chart')
    fireEvent.change(screen.getByLabelText('Date and time, time'), {
      target: { value: '10:00' },
    })
    fireEvent.change(screen.getByPlaceholderText('Label (e.g. v1.4 deploy)'), {
      target: { value: 'campaign launch' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    await waitFor(() => {
      const postCall = fetchSpy.mock.calls.find(
        ([callUrl, callInit]) =>
          String(callUrl).includes('/api/v1/projects/demo/annotations')
          && callInit?.method === 'POST',
      )
      expect(postCall).toBeDefined()
      const body = JSON.parse(String(postCall![1]?.body)) as Record<string, unknown>
      expect(body.scope_type).toBe('metric')
      expect(body.scope_ref).toBe('metric-1')
      expect(body.label).toBe('campaign launch')
    })
    // Confirmed, not only by a list growing below the fold (MO-8). Today's
    // 10:00 is past the fixture's collected series, so the toast also says the
    // marker is parked on the newest bucket until the next collection.
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Annotation added', {
      description: expect.stringMatching(/past the collected data, so it shows on the newest bucket/),
    }))
  })

  it('renders percent-metric breakdowns with the percent formatter and unit label (tripl-4dej)', async () => {
    installMetricDetailFetch(
      '1d',
      { unit: '%' },
      {},
      [],
      {
        columns: ['platform'],
        selected_column: 'platform',
        series: [
          {
            breakdown_value: 'ios',
            is_other: false,
            total_value: 0.4,
            data: [metricSeriesPoint('2026-01-02T00:00:00Z', 0.08)],
          },
          {
            breakdown_value: 'android',
            is_other: false,
            total_value: 0.3,
            data: [metricSeriesPoint('2026-01-02T00:00:00Z', 0.05)],
          },
        ],
      },
    )
    renderMetricDetail()

    await screen.findByTestId('metrics-chart')
    const breakdownsTab = screen.getByRole('tab', { name: /Breakdowns/i })
    fireEvent.pointerDown(breakdownsTab, { button: 0, ctrlKey: false })
    fireEvent.mouseDown(breakdownsTab, { button: 0, ctrlKey: false })
    fireEvent.pointerUp(breakdownsTab, { button: 0, ctrlKey: false })
    fireEvent.mouseUp(breakdownsTab, { button: 0, ctrlKey: false })
    fireEvent.click(breakdownsTab)

    const chart = await screen.findByTestId('multi-chart')
    expect(chart).toHaveAttribute('data-labels', 'ios|android')
    // The percent-aware formatter reached the breakdown chart (0.08 → '8%'),
    // and the tooltip label is the metric's unit — never 'events'.
    expect(chart).toHaveAttribute('data-value-sample', '8%')
    expect(chart).toHaveAttribute('data-series-label', '%')
  })

  it('shows the latest ratio value in the version legend instead of a summed total', async () => {
    installMetricDetailFetch('1d', {
      unit: '%',
      kind: 'fact',
      composition: 'ratio',
      app_version_column: 'app_version',
    })
    renderMetricDetail()

    await screen.findByTestId('metrics-chart')
    const byVersionTab = screen.getByRole('tab', { name: /By version/i })
    fireEvent.pointerDown(byVersionTab, { button: 0, ctrlKey: false })
    fireEvent.mouseDown(byVersionTab, { button: 0, ctrlKey: false })
    fireEvent.pointerUp(byVersionTab, { button: 0, ctrlKey: false })
    fireEvent.mouseUp(byVersionTab, { button: 0, ctrlKey: false })
    fireEvent.click(byVersionTab)

    expect(await screen.findByText('latest value: 20%')).toBeInTheDocument()
    expect(screen.queryByText('30%')).not.toBeInTheDocument()
  })

  it('coaches the metric-scope Breakdowns empty state with an Edit metric link', async () => {
    installMetricDetailFetch('1d')
    renderMetricDetail()

    await screen.findByTestId('metrics-chart')

    const breakdownsTab = screen.getByRole('tab', { name: /Breakdowns/i })
    fireEvent.pointerDown(breakdownsTab, { button: 0, ctrlKey: false })
    fireEvent.mouseDown(breakdownsTab, { button: 0, ctrlKey: false })
    fireEvent.pointerUp(breakdownsTab, { button: 0, ctrlKey: false })
    fireEvent.mouseUp(breakdownsTab, { button: 0, ctrlKey: false })
    fireEvent.click(breakdownsTab)

    // Metric-scope copy — no "event"/"scan" language, points at the metric settings.
    expect(
      await screen.findByText(/Add breakdown columns in the metric settings/i),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Edit metric/i })).toBeInTheDocument()
    // Event-scope copy must not leak into the metric scope.
    expect(
      screen.queryByText(/Edit this event and add a column/i),
    ).not.toBeInTheDocument()
  })

  it('renders the Definition card for a SQL metric with collapsed SQL that expands', async () => {
    installMetricDetailFetch('1d', {
      config: {
        metric_sql: 'SELECT day, dau FROM daily_users',
        time_column: 'day',
        value_column: 'dau',
      },
    })
    renderMetricDetail()

    expect(await screen.findByRole('heading', { name: 'Definition' })).toBeInTheDocument()
    // Kind chip + collection-interval meta chip.
    expect(screen.getByText('SQL')).toBeInTheDocument()
    expect(screen.getByText('Daily')).toBeInTheDocument()
    // Time/value column chips from the SQL config.
    expect(screen.getByText('day')).toBeInTheDocument()
    expect(screen.getByText('dau')).toBeInTheDocument()

    // The SQL itself is collapsed behind a "Show SQL" disclosure by default…
    // (The editor is a lazy chunk, so it can arrive a tick after the card.)
    const sql = await screen.findByDisplayValue('SELECT day, dau FROM daily_users')
    expect(sql).not.toBeVisible()
    // …and expands on click.
    fireEvent.click(screen.getByText('Show SQL'))
    expect(sql).toBeVisible()
    expect(sql).toHaveAttribute('readonly')
  })

  it('renders an event-composition ratio as "A ÷ B" with resolved event names', async () => {
    installMetricDetailFetch('1d', {
      kind: 'event_composition',
      composition: 'ratio',
      numerator_event_id: 'event-a',
      denominator_event_id: 'event-b',
    })
    renderMetricDetail()

    expect(await screen.findByRole('heading', { name: 'Definition' })).toBeInTheDocument()
    expect(screen.getByText('Event composition')).toBeInTheDocument()
    // Event ids resolve to names by id; joined by ÷.
    expect(await screen.findByText('checkout_completed')).toBeInTheDocument()
    expect(screen.getByText('session_started')).toBeInTheDocument()
    expect(screen.getByText('÷')).toBeInTheDocument()
  })

  it('explains that collecting a fact metric refreshes all dependents in one batch', async () => {
    const fetchSpy = installMetricDetailFetch('1d', {
      kind: 'fact',
      composition: 'single',
      aggregation: 'count',
      fact_table_id: 'ft-1',
    })
    renderMetricDetail()

    const button = await screen.findByRole('button', { name: 'Recompute' })
    expect(button.getAttribute('title')).toMatch(
      /current warehouse data.*all dependent active metrics.*one batch/i,
    )
    fireEvent.click(button)

    await waitFor(() => {
      expect(fetchSpy.mock.calls.some(
        ([url, init]) =>
          String(url).endsWith('/api/v1/projects/demo/metrics/metric-1/collect')
          && init?.method === 'POST',
      )).toBe(true)
    })
    expect(screen.getByRole('button', { name: 'Recomputing…' })).toBeDisabled()
  })

  it('reports how many metrics the fact batch actually refreshes', async () => {
    // The batch is capped, so "all dependent metrics" could promise more than the
    // click started. The response says how many it got; the toast repeats it.
    toastSuccess.mockClear()
    installMetricDetailFetch('1d', {
      kind: 'fact',
      composition: 'single',
      aggregation: 'count',
      fact_table_id: 'ft-1',
    })
    renderMetricDetail()

    fireEvent.click(await screen.findByRole('button', { name: 'Recompute' }))

    await waitFor(() => {
      expect(toastSuccess).toHaveBeenCalledWith(expect.stringContaining('3 metrics'))
    })
  })

  it('refreshes every metric-series cache after a fact batch completes', async () => {
    installMetricDetailFetch(
      '1d',
      {
        kind: 'fact',
        composition: 'single',
        aggregation: 'count',
        fact_table_id: 'ft-1',
      },
      {},
      [],
      {},
      {},
      'success',
    )
    const { queryClient } = renderMetricDetail()
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')

    fireEvent.click(await screen.findByRole('button', { name: 'Recompute' }))

    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({
        queryKey: ['monitoringMetrics', 'demo', 'metric'],
      })
    })
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ['metricDefinition', 'demo', 'metric-1'],
    })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['metrics-catalog', 'demo'] })
  })

  it('keeps an in-progress collect watch when the header actions unmount (canWrite flicker)', async () => {
    // The definition never settles (status stays null), so the watch keeps polling.
    installMetricDetailFetch('1d')
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const viewer: AuthContextValue = {
      user: {
        id: 'viewer-1',
        email: 'viewer@example.com',
        name: 'Viewer',
        role: 'viewer',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
      },
      status: 'authenticated',
      error: null,
      isLoggingOut: false,
      logout: async () => {},
      refresh: () => {},
    }
    const tree = (auth: AuthContextValue | null) => (
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={auth}>
          <MemoryRouter initialEntries={['/p/demo/monitoring/metric/metric-1']}>
            <Routes>
              <Route path="/p/:slug/monitoring/:scope/:id" element={<MonitoringDetailPage />} />
            </Routes>
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>
    )
    const { rerender } = render(tree(null))

    const button = await screen.findByRole('button', { name: 'Collect now' })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    expect(await screen.findByRole('button', { name: 'Collecting…' })).toBeDisabled()

    // The permission flickers: the header actions unmount, then come back.
    rerender(tree(viewer))
    expect(screen.queryByRole('button', { name: /Collect/ })).not.toBeInTheDocument()
    rerender(tree(null))

    // The watch lived on the page, so the run still reads as in progress.
    expect(await screen.findByRole('button', { name: 'Collecting…' })).toBeDisabled()
  })

  it('does not render breakdowns before the metric definition fixes the rollup', async () => {
    // Until the definition says "ratio", the rollup falls back to a sum; the
    // tab must wait instead of drawing summed values and then snapping.
    const fetchSpy = installMetricDetailFetch('1h', { kind: 'fact', composition: 'ratio', unit: '%' })
    const base = fetchSpy.getMockImplementation()!
    let releaseDefinition: () => void = () => {}
    const definitionGate = new Promise<void>(resolve => { releaseDefinition = resolve })
    fetchSpy.mockImplementation(async (input, init) => {
      if (String(input).endsWith('/api/v1/projects/demo/metrics/metric-1')) await definitionGate
      return base(input, init)
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/p/demo/monitoring/metric/metric-1?tab=breakdowns']}>
          <Routes>
            <Route path="/p/:slug/monitoring/:scope/:id" element={<MonitoringDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )

    // The page waits in its own skeleton for the definition that titles it.
    expect(await screen.findByText('Loading metric…')).toBeInTheDocument()
    // Give the series a chance to land: the tab still has not asked for data.
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([input]) => String(input).includes('/metrics/metric-1/series'))).toBe(true))
    expect(fetchSpy.mock.calls.some(([input]) => String(input).includes('/metrics/metric-1/breakdowns'))).toBe(false)

    releaseDefinition()
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([input]) => String(input).includes('/metrics/metric-1/breakdowns'))).toBe(true))
  })

  it('does not render the Definition card outside the metric scope', async () => {
    installEventDetailFetch()
    renderEventDetail()
    await screen.findByRole('heading', { name: 'checkout_completed' })

    expect(screen.queryByRole('heading', { name: 'Definition' })).not.toBeInTheDocument()
    expect(screen.queryByText('Show SQL')).not.toBeInTheDocument()
  })

  it('averages a ratio metric rolled up to days instead of summing it (MON-2)', async () => {
    installMetricDetailFetch('1h', { kind: 'fact', composition: 'ratio', unit: '%' })
    renderMetricDetail()

    const chart = await screen.findByTestId('metrics-chart')
    await waitFor(() => expect(chart).toHaveAttribute('data-points', '2'))
    fireEvent.click(screen.getByRole('combobox', { name: /time granularity/i }))
    fireEvent.click(await screen.findByRole('option', { name: 'Days' }))

    // 10 and 20 in one day: the day reads 15, not 30.
    await waitFor(() => expect(screen.getByTestId('metrics-chart')).toHaveAttribute('data-first-count', '15'))
  })

  it('still sums an additive count metric rolled up to days (MON-2)', async () => {
    installMetricDetailFetch('1h', { kind: 'fact', aggregation: 'count' })
    renderMetricDetail()

    const chart = await screen.findByTestId('metrics-chart')
    await waitFor(() => expect(chart).toHaveAttribute('data-points', '2'))
    fireEvent.click(screen.getByRole('combobox', { name: /time granularity/i }))
    fireEvent.click(await screen.findByRole('option', { name: 'Days' }))

    await waitFor(() => expect(screen.getByTestId('metrics-chart')).toHaveAttribute('data-first-count', '30'))
  })

  it('never fetches event types on a metric page (MON-37)', async () => {
    const fetchSpy = installMetricDetailFetch('1d')
    renderMetricDetail()

    await screen.findByTestId('metrics-chart')
    expect(fetchSpy.mock.calls.some(([input]) => String(input).endsWith('/event-types'))).toBe(false)
  })

  // SH-33: a deleted metric is not a failure to retry.
  it('says a missing metric was not found and links back to Metrics', async () => {
    const fetchSpy = installMetricDetailFetch('1d')
    const base = fetchSpy.getMockImplementation()!
    fetchSpy.mockImplementation(async (input, init) => {
      if (String(input).endsWith('/api/v1/projects/demo/metrics/metric-1')) return errorResponse(404)
      return base(input, init)
    })
    renderMetricDetail()

    expect(await screen.findByRole('heading', { name: 'Metric not found' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Back to Metrics/ })).toHaveAttribute('href', '/p/demo/metrics')
    expect(screen.queryByRole('button', { name: /Try again/ })).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('retries the failed metric definition from the page error (MON-7)', async () => {
    const fetchSpy = installMetricDetailFetch('1d')
    const base = fetchSpy.getMockImplementation()!
    let definitionCalls = 0
    fetchSpy.mockImplementation(async (input, init) => {
      if (String(input).endsWith('/api/v1/projects/demo/metrics/metric-1')) {
        definitionCalls += 1
        if (definitionCalls === 1) return errorResponse()
      }
      return base(input, init)
    })
    renderMetricDetail()

    fireEvent.click(await screen.findByRole('button', { name: /Try again/ }))

    expect(await screen.findByRole('heading', { name: 'Daily Active Users' })).toBeInTheDocument()
    expect(definitionCalls).toBe(2)
  })

  it('keeps the range and granularity in the URL (MON-24)', async () => {
    const fetchSpy = installMetricDetailFetch('1h')
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/p/demo/monitoring/metric/metric-1?range=30&gran=day']}>
          <Routes>
            <Route path="/p/:slug/monitoring/:scope/:id" element={<MonitoringDetailPage />} />
          </Routes>
          <LocationProbe />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    await screen.findByTestId('metrics-chart')
    // The link's range reached the request, and its granularity the control.
    const seriesUrl = fetchSpy.mock.calls
      .map(([input]) => String(input))
      .find(url => url.includes('/metrics/metric-1/series'))
    const range = new URL(seriesUrl!, 'http://localhost').searchParams
    expect(new Date(range.get('to')!).getTime() - new Date(range.get('from')!).getTime())
      .toBe(30 * 24 * 60 * 60 * 1000)
    expect(screen.getByRole('combobox', { name: /time granularity/i })).toHaveTextContent('Days')
    expect(screen.getByRole('button', { name: '30d' })).toHaveAttribute('aria-pressed', 'true')

    // A change is written back, the other params kept.
    fireEvent.click(screen.getByRole('button', { name: '90d' }))
    await waitFor(() => {
      const params = new URLSearchParams(screen.getByTestId('location-search').textContent ?? '')
      expect(params.get('range')).toBe('90')
      expect(params.get('gran')).toBe('day')
    })
    // Back to the default: the param leaves the URL instead of spelling it out.
    fireEvent.click(screen.getByRole('button', { name: '7d' }))
    await waitFor(() =>
      expect(new URLSearchParams(screen.getByTestId('location-search').textContent ?? '').has('range')).toBe(false))
    // The same for granularity: Hours is the 7d default of a 1h metric.
    fireEvent.click(screen.getByRole('combobox', { name: /time granularity/i }))
    fireEvent.click(await screen.findByRole('option', { name: 'Hours' }))
    await waitFor(() =>
      expect(new URLSearchParams(screen.getByTestId('location-search').textContent ?? '').has('gran')).toBe(false))
    expect(screen.getByRole('combobox', { name: /time granularity/i })).toHaveTextContent('Hours')
  })

  it('sends a neutral colour, names the time zone and caps the label (MON-25, MON-27)', async () => {
    const fetchSpy = installMetricDetailFetch('1d')
    renderMetricDetail()

    await screen.findByTestId('metrics-chart')
    // The design-system picker, not a native datetime-local input (LIVE-21),
    // prefilled with now, so "we just deployed" is one field away.
    expect(screen.getByRole('group', { name: 'Date and time' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Date and time, date: / })).not.toHaveTextContent('Pick a date')
    expect((screen.getByLabelText('Date and time, time') as HTMLInputElement).value).toMatch(
      /^\d{2}:\d{2}$/,
    )
    expect(document.querySelector('input[type="datetime-local"]')).toBeNull()
    expect(screen.getByText(/Your local time \(UTC/)).toBeInTheDocument()
    expect(screen.queryByText('YYYY-MM-DD HH:mm')).not.toBeInTheDocument()
    const label = screen.getByPlaceholderText('Label (e.g. v1.4 deploy)')
    expect(label).toHaveAttribute('maxLength', '200')

    fireEvent.change(label, { target: { value: 'deploy' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    await waitFor(() => {
      const postCall = fetchSpy.mock.calls.find(
        ([callUrl, callInit]) => String(callUrl).includes('/annotations') && callInit?.method === 'POST',
      )
      expect(postCall).toBeDefined()
      // Never the backend's red default, which is the anomaly colour.
      expect(JSON.parse(String(postCall![1]?.body)).color).toBe('var(--info)')
    })
  })

  it('confirms before deleting, and says a project-wide marker goes everywhere (MON-26)', async () => {
    const fetchSpy = installMetricDetailFetch('1d', {}, {}, [
      metricAnnotationFixture({ scope_type: null, scope_ref: null, label: 'Global freeze' }),
    ])
    renderMetricDetail()

    const deleteButton = await screen.findByRole('button', { name: 'Delete annotation Global freeze' })
    const deletes = () => fetchSpy.mock.calls.filter(([, callInit]) => callInit?.method === 'DELETE')

    fireEvent.click(deleteButton)
    expect(await screen.findByText('Delete project-wide annotation?')).toBeInTheDocument()
    expect(screen.getByText(/shown on every chart in this project/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByText('Delete project-wide annotation?')).not.toBeInTheDocument())
    expect(deletes()).toHaveLength(0)

    fireEvent.click(deleteButton)
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(deletes()).toHaveLength(1))
  })

  it('says how many breakdown values the chart leaves out (MON-29)', async () => {
    installMetricDetailFetch('1d', {}, {}, [], {
      columns: ['country'],
      selected_column: 'country',
      series: Array.from({ length: 10 }, (_, index) => ({
        breakdown_value: `c${index}`,
        is_other: false,
        total_value: 10 - index,
        data: [metricSeriesPoint('2026-01-02T00:00:00Z', 10 - index)],
      })),
    })
    renderMetricDetail()

    await screen.findByTestId('metrics-chart')
    const breakdownsTab = screen.getByRole('tab', { name: /Breakdowns/i })
    fireEvent.mouseDown(breakdownsTab, { button: 0, ctrlKey: false })
    fireEvent.click(breakdownsTab)

    expect(await screen.findByText(/Showing the first 8 of 10 values/)).toBeInTheDocument()
    expect(screen.getByTestId('multi-chart').getAttribute('data-labels')?.split('|')).toHaveLength(8)
  })

  /**
   * The coached demo scenario (tripl-2su6.21.5). Rendered inside the REAL
   * provider: the persisted state is the only honest witness that the collect
   * the USER fired — not one of the demo tick's own — bound the scenario.
   */
  describe('coached demo scenario', () => {
    const SLUG = 'demo'
    const POLL_MS = 10
    const STEPS = buildChapterSteps(SLUG, 'live-loop', initialScenarioState())
    const COLLECT_INSTRUCTION = at(STEPS, 2).instruction

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

    function collectMetricState(): ScenarioState {
      return liveLoopState('live-loop/collect-metric', {
        scan: { scanConfigId: 'sc-1', scanJobId: 'job-1', startedAt: Date.now() },
      })
    }

    function renderWithScenario(project: Project) {
      const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      return render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={['/p/demo/monitoring/metric/metric-1']}>
            <DemoScenarioProvider project={project} pollIntervalMs={POLL_MS}>
              <Routes>
                <Route path="/p/:slug/monitoring/:scope/:id" element={<MonitoringDetailPage />} />
              </Routes>
            </DemoScenarioProvider>
          </MemoryRouter>
        </QueryClientProvider>,
      )
    }

    const callouts = () => document.querySelectorAll('[data-slot="popover-content"]')
    const collectButton = async () => {
      const button = await screen.findByRole('button', { name: /Collect now/ })
      await waitFor(() => expect(button).toBeEnabled())
      return button
    }

    afterEach(() => {
      window.localStorage.clear()
    })

    it('binds the scenario to this metric when the collect is accepted', async () => {
      writeScenarioState(SLUG, collectMetricState())
      // The definition never settles, so the step stays put for the assertion.
      installMetricDetailFetch('1d')
      renderWithScenario(demoProject())

      fireEvent.click(await collectButton())

      await waitFor(() => expect(readScenarioState(SLUG).chapters['live-loop']?.artifacts?.metricId).toBe('metric-1'))
    })

    it('marks Collect now while the collect step is the active one', async () => {
      writeScenarioState(SLUG, collectMetricState())
      installMetricDetailFetch('1d')
      renderWithScenario(demoProject())

      await collectButton()
      await waitFor(() => expect(screen.getAllByText(COLLECT_INSTRUCTION)).toHaveLength(1))
    })

    it('leaves a project that is not a demo untouched', async () => {
      writeScenarioState(SLUG, collectMetricState())
      installMetricDetailFetch('1d')
      renderWithScenario(demoProject({ is_demo: false }))

      fireEvent.click(await collectButton())

      // The collect still runs; the notify is inert and no mark is mounted.
      await waitFor(() => expect(screen.getByText('Collecting…')).toBeInTheDocument())
      expect(readScenarioState(SLUG).chapters['live-loop']?.artifacts?.metricId).toBeUndefined()
      expect(callouts()).toHaveLength(0)
      expect(screen.queryByText(COLLECT_INSTRUCTION)).not.toBeInTheDocument()
    })
  })
})

describe('MonitoringDetailPage failures stay inside their tab (MON-8, MON-9)', () => {
  it('shows a hero-shaped placeholder, not the generic header, while the event loads', async () => {
    const fetchSpy = installEventDetailFetch()
    const base = fetchSpy.getMockImplementation()!
    fetchSpy.mockImplementation(async (input, init) => {
      if (String(input).endsWith('/api/v1/projects/demo/events/event-1')) return new Promise<Response>(() => {})
      return base(input, init)
    })
    renderEventDetail()

    expect(await screen.findByRole('status', { name: 'Loading event' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Back to events/ })).not.toBeInTheDocument()
    expect(screen.queryByText('Monitoring detail for the selected event.')).not.toBeInTheDocument()
  })

  it('keeps the page when the Distribution endpoint fails', async () => {
    const fetchSpy = installEventDetailFetch()
    const base = fetchSpy.getMockImplementation()!
    fetchSpy.mockImplementation(async (input, init) => {
      if (String(input).includes('/distribution-drift')) return errorResponse()
      return base(input, init)
    })
    renderEventDetail('?tab=distribution')

    expect(await screen.findByText('Could not load distribution drift')).toBeInTheDocument()
    // The header, fields and the other tabs are all still there.
    expect(screen.getByRole('heading', { name: 'checkout_completed' })).toBeInTheDocument()
    expect(screen.getByRole('table', { name: 'Fields' })).toBeInTheDocument()
    expect(screen.queryByText('Failed to load monitoring details')).not.toBeInTheDocument()
  })

  it('says the breakdowns failed instead of claiming there are none', async () => {
    const fetchSpy = installEventDetailFetch()
    const base = fetchSpy.getMockImplementation()!
    fetchSpy.mockImplementation(async (input, init) => {
      if (String(input).includes('/metrics/breakdowns')) return errorResponse()
      return base(input, init)
    })
    renderEventDetail('?tab=breakdowns')

    expect(await screen.findByText('Could not load breakdowns')).toBeInTheDocument()
    expect(screen.queryByText('No breakdown groups yet.')).not.toBeInTheDocument()
  })

  it('says the change history failed instead of "No recent changes"', async () => {
    const fetchSpy = installEventDetailFetch()
    const base = fetchSpy.getMockImplementation()!
    fetchSpy.mockImplementation(async (input, init) => {
      if (String(input).includes('/events/event-1/history')) return errorResponse()
      return base(input, init)
    })
    renderEventDetail()

    expect(await screen.findByText('Could not load recent activity')).toBeInTheDocument()
    expect(screen.queryByText('No recent changes')).not.toBeInTheDocument()
  })

  it('writes the breakdown value filter to the URL (MON-24)', async () => {
    const point = metricPoint('2026-01-02T00:00:00Z', 10)
    installEventDetailFetch({
      breakdowns: {
        event_id: 'event-1',
        scan_config_id: 'scan-1',
        interval: '1h',
        columns: ['platform'],
        selected_column: 'platform',
        series: [
          { breakdown_value: 'ios', is_other: false, total_count: 60, data: [point], parity_anomalies: [] },
          { breakdown_value: 'android', is_other: false, total_count: 40, data: [point], parity_anomalies: [] },
        ],
      },
    })
    renderEventDetail('?tab=breakdowns&value=android')

    // The link's filter is applied on arrival…
    await waitFor(() =>
      expect(screen.getByTestId('multi-chart')).toHaveAttribute('data-labels', 'android'))
    // …and a change is written back.
    fireEvent.click(screen.getByRole('button', { name: 'Toggle ios' }))
    await waitFor(() => {
      const params = new URLSearchParams(screen.getByTestId('location-search').textContent ?? '')
      expect(params.getAll('value')).toEqual(['android', 'ios'])
      expect(params.get('tab')).toBe('breakdowns')
    })
  })
})

describe('MonitoringDetailPage deep links (tripl-h2sx.20)', () => {
  it('opens the tab and the breakdown column the link names', async () => {
    const fetchSpy = installEventDetailFetch({
      breakdowns: {
        event_id: 'event-1',
        scan_config_id: 'scan-1',
        interval: '1h',
        columns: ['platform', 'screen'],
        selected_column: 'screen',
        series: [
          {
            breakdown_value: 'spot',
            is_other: false,
            total_count: 12000,
            data: [metricPoint('2026-01-02T00:00:00Z', 12000)],
            parity_anomalies: [],
          },
        ],
      },
    })
    renderEventDetail('?tab=breakdowns&column=screen')
    await screen.findByRole('heading', { name: 'checkout_completed' })

    // No click: the link IS the navigation, which is the whole point of
    // pointing a field value at the split that answers for it.
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: /Breakdowns/i })).toHaveAttribute(
        'aria-selected',
        'true',
      ),
    )
    await waitFor(() =>
      expect(
        fetchSpy.mock.calls.some(([input]) => {
          const url = String(input)
          return url.includes('/events/event-1/metrics/breakdowns') && url.includes('column=screen')
        }),
      ).toBe(true),
    )
  })

  it('falls back to volume when the link names a tab this scope has no trigger for', async () => {
    // The scan carries no app_version_column, so there is no "By version" tab
    // to land on. Before the URL could pick a tab only `versions` needed this
    // guard; now any of the five can be asked for by a stale or hand-edited
    // link, and a value with no trigger leaves the reader on a blank page.
    installEventDetailFetch()
    renderEventDetail('?tab=versions')
    await screen.findByRole('heading', { name: 'checkout_completed' })

    expect(screen.queryByRole('tab', { name: /By version/i })).not.toBeInTheDocument()
    expect(screen.getAllByRole('tab')[0]).toHaveAttribute('aria-selected', 'true')
  })
})

describe('Event implementation tickets (tripl-h2sx.32)', () => {
  const TICKET = {
    id: 'ticket-1',
    project_id: 'p-1',
    branch_id: 'branch-1',
    tracker_type: 'jira',
    external_id: '10042',
    external_key: 'ENG-42',
    external_url: 'https://example.atlassian.net/browse/ENG-42',
    status: 'open',
    summary: 'Implement checkout-v2',
    event_ids: ['event-1'],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    closed_at: null,
  }

  it('links every ticket that named the event', async () => {
    installEventDetailFetch({ tickets: [TICKET, { ...TICKET, id: 'ticket-2', external_key: 'ENG-9', status: 'closed' }] })
    renderEventDetail()

    expect(await screen.findByText('Implementation tickets')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /ENG-42/ })).toHaveAttribute(
      'href',
      'https://example.atlassian.net/browse/ENG-42',
    )
    expect(screen.getByRole('link', { name: /ENG-9/ })).toBeInTheDocument()
    expect(screen.getByText('Done')).toBeInTheDocument()
  })

  it('shows no card at all when nothing named the event', async () => {
    // Hidden, not empty: rows exist only where the tracker is on and a branch
    // merged, so an empty card would be noise on nearly every event.
    installEventDetailFetch({ tickets: [] })
    renderEventDetail()

    await screen.findByText('Metric breakdowns')
    expect(screen.queryByText('Implementation tickets')).not.toBeInTheDocument()
  })
})
