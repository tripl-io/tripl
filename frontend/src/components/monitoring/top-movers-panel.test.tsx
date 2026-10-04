import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ComponentProps } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { eventMetricsApi } from '@/api/eventMetrics'
import type { TopMoverItem } from '@/types'

import { topMoversKey } from '@/lib/queryKeys'
import { formatSeriesValue, type SeriesNoun } from '@/components/ui/chart-format'

import { TopMoversPanel } from './top-movers-panel'

// Pass-through wrapper: the real chart renders, and the noun it was handed is
// kept for the tooltip-grammar assertion (the tooltip never paints in jsdom).
const { chartNouns } = vi.hoisted(() => ({ chartNouns: [] as SeriesNoun[] }))
vi.mock('@/components/ui/chart', async importOriginal => {
  const actual = await importOriginal<typeof import('@/components/ui/chart')>()
  return {
    ...actual,
    MetricsChart: (props: ComponentProps<typeof actual.MetricsChart>) => {
      if (props.seriesLabel) chartNouns.push(props.seriesLabel)
      return <actual.MetricsChart {...props} />
    },
  }
})

vi.mock('@/api/eventMetrics', () => ({
  eventMetricsApi: { getTopMovers: vi.fn(), getBreakdownSeries: vi.fn(), getBreakdownTimeline: vi.fn() },
}))

function mover(overrides: Partial<TopMoverItem> = {}): TopMoverItem {
  return {
    breakdown_column: 'platform',
    breakdown_value: 'ios',
    is_other: false,
    actual_count: 240,
    expected_count: 100,
    stddev: 12,
    z_score: 11.7,
    direction: 'spike',
    ...overrides,
  }
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <TopMoversPanel
        slug="demo"
        scanConfigId="scan-1"
        scopeType="event"
        scopeRef="event-1"
        bucket="2026-01-02T00:00:00Z"
      />
    </QueryClientProvider>,
  )
}

describe('TopMoversPanel', () => {
  it('shows the signed percentage for a row that has a baseline', async () => {
    vi.mocked(eventMetricsApi.getTopMovers).mockResolvedValue([mover()])
    renderPanel()

    expect(await screen.findByText('+140%')).toBeInTheDocument()
    expect(screen.queryByText('no baseline')).not.toBeInTheDocument()
  })

  it('says there is no baseline instead of leaving the cell blank', async () => {
    // A brand-new breakdown value: nothing was expected, so the ratio is
    // undefined. The row used to render an empty span, which reads as missing
    // data — indistinguishable from a value the panel simply failed to load.
    vi.mocked(eventMetricsApi.getTopMovers).mockResolvedValue([
      mover({ breakdown_value: 'visionos', actual_count: 137, expected_count: 0, z_score: 9.1 }),
    ])
    renderPanel()

    const label = await screen.findByText('no baseline')
    expect(label).toHaveAttribute(
      'title',
      'No baseline to compare against for this breakdown value',
    )
    // The absolute move is what there is to report, and it stays beside it.
    expect(screen.getByText('+137')).toBeInTheDocument()
  })

  it('still prints nothing for a real change too small to round to a percent', async () => {
    // The two cases used to share the empty string. This one genuinely has
    // nothing to add: the absolute-delta badge beside it already says +0.
    vi.mocked(eventMetricsApi.getTopMovers).mockResolvedValue([
      mover({ actual_count: 1000, expected_count: 999, z_score: 3.2 }),
    ])
    renderPanel()

    expect(await screen.findByText('+1')).toBeInTheDocument()
    expect(screen.queryByText('no baseline')).not.toBeInTheDocument()
    expect(screen.queryByText(/%$/)).not.toBeInTheDocument()
  })

  it('keys the row timeline on the range length, not the moving live window (MON-3)', async () => {
    vi.mocked(eventMetricsApi.getTopMovers).mockResolvedValue([mover()])
    const fetchTimeline = vi.mocked(eventMetricsApi.getBreakdownTimeline)
    fetchTimeline.mockReset()
    fetchTimeline.mockResolvedValue({
      scan_config_id: 'scan-1',
      scope_type: 'event',
      scope_ref: 'event-1',
      breakdown_column: 'platform',
      breakdown_value: 'ios',
      is_other: false,
      interval: '1h',
      data: [],
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const tree = (timeRange: { from: string; to: string }) => (
      <QueryClientProvider client={client}>
        <TopMoversPanel
          slug="demo"
          scanConfigId="scan-1"
          scopeType="event"
          scopeRef="event-1"
          bucket="2026-01-02T00:00:00Z"
          rangeDays={7}
          timeRange={timeRange}
        />
      </QueryClientProvider>
    )
    const { rerender } = render(tree({ from: '2026-01-01T00:00:00Z', to: '2026-01-08T00:00:00Z' }))

    fireEvent.click(await screen.findByRole('button', { name: /platform=ios/ }))
    expect(await screen.findByText(/No timeline data/)).toBeInTheDocument()
    expect(fetchTimeline).toHaveBeenCalledTimes(1)
    expect(fetchTimeline).toHaveBeenCalledWith('demo', 'scan-1', expect.objectContaining({
      from: '2026-01-01T00:00:00Z',
      to: '2026-01-08T00:00:00Z',
    }))

    // The live bound stepped five minutes: the same range, so no refetch.
    rerender(tree({ from: '2026-01-01T00:05:00Z', to: '2026-01-08T00:05:00Z' }))
    await new Promise(resolve => setTimeout(resolve, 20))
    await waitFor(() => expect(fetchTimeline).toHaveBeenCalledTimes(1))
  })
  it('colours a spike as danger and a drop as warning, like every other signal surface (MON-19)', async () => {
    vi.mocked(eventMetricsApi.getTopMovers).mockResolvedValue([
      mover(),
      mover({
        breakdown_value: 'android',
        actual_count: 20,
        expected_count: 100,
        z_score: -6.7,
        direction: 'drop',
      }),
    ])
    renderPanel()

    expect((await screen.findByText('+140')).closest('[data-tone]')).toHaveAttribute('data-tone', 'danger')
    expect(screen.getByText('-80').closest('[data-tone]')).toHaveAttribute('data-tone', 'warning')
  })

  it('shows an error with a retry instead of vanishing when the request fails (MON-30)', async () => {
    const fetchMovers = vi.mocked(eventMetricsApi.getTopMovers)
    fetchMovers.mockReset()
    fetchMovers.mockRejectedValueOnce(new Error('upstream timeout'))
    fetchMovers.mockResolvedValue([mover()])
    renderPanel()

    expect(await screen.findByText('Top movers unavailable')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('+140%')).toBeInTheDocument()
  })

  it('keeps loaded rows and an open drilldown when a refetch fails', async () => {
    const fetchMovers = vi.mocked(eventMetricsApi.getTopMovers)
    fetchMovers.mockReset()
    fetchMovers.mockResolvedValueOnce([mover()])
    fetchMovers.mockRejectedValueOnce(new Error('upstream timeout'))
    fetchMovers.mockResolvedValue([mover()])
    const fetchTimeline = vi.mocked(eventMetricsApi.getBreakdownTimeline)
    fetchTimeline.mockReset()
    fetchTimeline.mockResolvedValue({
      scan_config_id: 'scan-1',
      scope_type: 'event',
      scope_ref: 'event-1',
      breakdown_column: 'platform',
      breakdown_value: 'ios',
      is_other: false,
      interval: '1h',
      data: [{ bucket: '2026-01-02T00:00:00Z', count: 240 }],
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <TopMoversPanel
          slug="demo"
          scanConfigId="scan-1"
          scopeType="event"
          scopeRef="event-1"
          bucket="2026-01-02T00:00:00Z"
        />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('+140%')).toBeInTheDocument()
    const row = screen.getByRole('button', { name: /platform=ios/ })
    fireEvent.click(row)
    expect(row).toHaveAttribute('aria-expanded', 'true')

    // Refetch the movers list only; the drilldown keeps its own query.
    await client
      .refetchQueries({
        queryKey: topMoversKey('demo', 'scan-1', 'event', 'event-1', '2026-01-02T00:00:00Z', 8),
      })
      .catch(() => undefined)

    expect(await screen.findByText(/Refresh failed/)).toBeInTheDocument()
    expect(screen.queryByText('Top movers unavailable')).not.toBeInTheDocument()
    expect(screen.getByText('+140%')).toBeInTheDocument()
    expect(row).toHaveAttribute('aria-expanded', 'true')

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.queryByText(/Refresh failed/)).not.toBeInTheDocument())
    expect(screen.getByText('+140%')).toBeInTheDocument()
  })

  it('charts the drilldown with the shared chart and marks the anomaly bucket (MON-20)', async () => {
    vi.mocked(eventMetricsApi.getTopMovers).mockResolvedValue([mover()])
    const fetchTimeline = vi.mocked(eventMetricsApi.getBreakdownTimeline)
    fetchTimeline.mockReset()
    fetchTimeline.mockResolvedValue({
      scan_config_id: 'scan-1',
      scope_type: 'event',
      scope_ref: 'event-1',
      breakdown_column: 'platform',
      breakdown_value: 'ios',
      is_other: false,
      interval: '1h',
      data: [
        { bucket: '2026-01-01T23:00:00Z', count: 90 },
        // The same instant as the panel's bucket, spelled differently.
        { bucket: '2026-01-02T00:00:00+00:00', count: 240 },
        { bucket: '2026-01-02T01:00:00Z', count: 110 },
      ],
    })
    renderPanel()

    fireEvent.click(await screen.findByRole('button', { name: /platform=ios/ }))
    const chart = await screen.findByRole('img', { name: 'events (platform=ios) over time' })
    expect(chart).toHaveAccessibleDescription(/3 data points\. 1 anomal/)
  })

  // DS-26: the drilldown handed the chart a plain string, so a one-event
  // bucket's tooltip read "1 events (platform=ios)".
  it('agrees the drilldown noun with a one-event bucket', async () => {
    vi.mocked(eventMetricsApi.getTopMovers).mockResolvedValue([mover()])
    const fetchTimeline = vi.mocked(eventMetricsApi.getBreakdownTimeline)
    fetchTimeline.mockReset()
    fetchTimeline.mockResolvedValue({
      scan_config_id: 'scan-1',
      scope_type: 'event',
      scope_ref: 'event-1',
      breakdown_column: 'platform',
      breakdown_value: 'ios',
      is_other: false,
      interval: '1h',
      data: [{ bucket: '2026-01-02T00:00:00Z', count: 1 }],
    })
    chartNouns.length = 0
    renderPanel()

    fireEvent.click(await screen.findByRole('button', { name: /platform=ios/ }))
    await screen.findByRole('img', { name: 'events (platform=ios) over time' })
    const noun = chartNouns.at(-1)
    expect(noun).toBeDefined()
    expect(formatSeriesValue(1, noun!)).toBe('1 event (platform=ios)')
    expect(formatSeriesValue(3, noun!)).toBe('3 events (platform=ios)')
  })

  it('says so inline when the drilldown timeline fails (MON-20)', async () => {
    vi.mocked(eventMetricsApi.getTopMovers).mockResolvedValue([mover()])
    const fetchTimeline = vi.mocked(eventMetricsApi.getBreakdownTimeline)
    fetchTimeline.mockReset()
    fetchTimeline.mockRejectedValue(new Error('upstream timeout'))
    renderPanel()

    fireEvent.click(await screen.findByRole('button', { name: /platform=ios/ }))
    expect(await screen.findByText('Timeline unavailable')).toBeInTheDocument()
    expect(screen.queryByText(/No timeline data/)).not.toBeInTheDocument()
  })
})
