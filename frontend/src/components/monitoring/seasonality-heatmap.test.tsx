import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'

import { eventMetricsApi } from '@/api/eventMetrics'
import type { SeasonalityCell, SeasonalityHeatmap } from '@/types/metrics'

import { SeasonalityHeatmap as SeasonalityHeatmapComponent } from './seasonality-heatmap'

vi.mock('@/api/eventMetrics', () => ({
  eventMetricsApi: { getSeasonalityHeatmap: vi.fn() },
}))

function cell(
  overrides: Partial<SeasonalityCell> &
    Pick<SeasonalityCell, 'weekday' | 'hour' | 'count'>,
): SeasonalityCell {
  return { anomaly_count: 0, ...overrides }
}

function heatmap(
  cells: SeasonalityCell[],
  overrides: Partial<SeasonalityHeatmap> = {},
): SeasonalityHeatmap {
  const counts = cells.map(item => item.count)
  return {
    scan_config_id: 'scan-1',
    scope_type: 'event',
    scope_ref: 'e1',
    cells,
    max_count: counts.length ? Math.max(...counts) : 0,
    total_count: counts.reduce((sum, value) => sum + value, 0),
    interval: '1h',
    hourly_resolution: true,
    ...overrides,
  }
}

const WEEK_WINDOW = { from: '2026-01-01T00:00:00', to: '2026-01-08T00:00:00' }

function heatmapTree(
  client: QueryClient,
  rangeDays = 7,
  timeRange: { from: string; to: string } = WEEK_WINDOW,
) {
  return (
    <QueryClientProvider client={client}>
      <SeasonalityHeatmapComponent
        slug="demo"
        scanConfigId="scan-1"
        scopeType="event"
        scopeRef="e1"
        rangeDays={rangeDays}
        timeRange={timeRange}
      />
    </QueryClientProvider>
  )
}

function renderHeatmap() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(heatmapTree(client))
}

describe('SeasonalityHeatmap', () => {
  it('paints very different counts with visibly different fills', async () => {
    vi.mocked(eventMetricsApi.getSeasonalityHeatmap).mockResolvedValue(
      heatmap([
        cell({ weekday: 0, hour: 0, count: 50 }),
        cell({ weekday: 3, hour: 14, count: 800 }),
      ]),
    )

    const { container } = renderHeatmap()

    await screen.findByText('Hour × weekday heatmap')

    const low = container.querySelector<HTMLElement>('[data-count="50"]')
    const high = container.querySelector<HTMLElement>('[data-count="800"]')
    expect(low).not.toBeNull()
    expect(high).not.toBeNull()

    const lowOpacity = Number(low?.style.opacity)
    const highOpacity = Number(high?.style.opacity)
    // A 16x volume swing must be plainly visible, not a near-uniform tint.
    expect(highOpacity).toBeGreaterThan(lowOpacity + 0.3)
  })

  it('renders a legend showing the min and max slot volumes', async () => {
    vi.mocked(eventMetricsApi.getSeasonalityHeatmap).mockResolvedValue(
      heatmap([
        cell({ weekday: 0, hour: 0, count: 50 }),
        cell({ weekday: 3, hour: 14, count: 800 }),
      ]),
    )

    renderHeatmap()

    await screen.findByText('Hour × weekday heatmap')
    expect(screen.getByText('50')).toBeInTheDocument()
    expect(screen.getByText('800')).toBeInTheDocument()
  })

  // this file's own formatter printed "1.0k" where every chart prints "1k".
  it('prints compact counts the way the charts do', async () => {
    vi.mocked(eventMetricsApi.getSeasonalityHeatmap).mockResolvedValue(
      heatmap([
        cell({ weekday: 0, hour: 0, count: 50 }),
        cell({ weekday: 3, hour: 14, count: 1000 }),
      ]),
    )
    renderHeatmap()

    await screen.findByText('Hour × weekday heatmap')
    expect(screen.getByText('1k')).toBeInTheDocument()
    expect(screen.queryByText('1.0k')).toBeNull()
  })

  // the grid is cut from UTC buckets and used to say nothing about it.
  it('labels the grid and each slot as UTC', async () => {
    vi.mocked(eventMetricsApi.getSeasonalityHeatmap).mockResolvedValue(
      heatmap([
        cell({ weekday: 0, hour: 0, count: 50 }),
        cell({ weekday: 3, hour: 14, count: 800 }),
      ]),
    )
    renderHeatmap()

    await screen.findByText('Hour × weekday heatmap')
    expect(screen.getByRole('columnheader', { name: 'UTC' })).toBeInTheDocument()
    expect(screen.getByText(/Thu 14:00 UTC — 800 events/)).toBeInTheDocument()
  })

  // a native title was the only way to read a cell, and touch has none.
  it('spells out the hovered or tapped slot under the grid', async () => {
    vi.mocked(eventMetricsApi.getSeasonalityHeatmap).mockResolvedValue(
      heatmap([
        cell({ weekday: 0, hour: 0, count: 50 }),
        cell({ weekday: 3, hour: 14, count: 800 }),
      ]),
    )
    renderHeatmap()

    await screen.findByText('Hour × weekday heatmap')
    const detail = screen.getByTestId('heatmap-slot-detail')
    expect(detail).toHaveTextContent('Hover or tap a cell for its count.')

    const slot = screen.getByText(/Thu 14:00 UTC — 800 events/).closest('td')!
    expect(slot).not.toHaveAttribute('title')
    fireEvent.click(slot)
    expect(detail).toHaveTextContent('Thu 14:00 UTC — 800 events')
  })

  it('says the ramp is a rank scale, not a linear count scale', async () => {
    vi.mocked(eventMetricsApi.getSeasonalityHeatmap).mockResolvedValue(
      heatmap([
        cell({ weekday: 0, hour: 0, count: 50 }),
        cell({ weekday: 3, hour: 14, count: 800 }),
      ]),
    )

    renderHeatmap()

    // The min/max labels are true endpoints, but the shading in between is by
    // quantile rank — the legend used to read as "events / slot" alone, which
    // invites reading a mid-tone as a mid-count.
    expect(await screen.findByText(/shaded by rank/)).toBeInTheDocument()
  })

  it('does not draw a grid a coarse interval can never fill', async () => {
    // A daily scan floors every bucket into hour 0, so 23 of each row's 24
    // cells are structurally empty and the grid reads as missing data.
    vi.mocked(eventMetricsApi.getSeasonalityHeatmap).mockResolvedValue(
      heatmap([cell({ weekday: 0, hour: 0, count: 500 })], {
        interval: '1d',
        hourly_resolution: false,
      }),
    )

    const { container } = renderHeatmap()

    expect(await screen.findByText(/no hour-of-day detail/)).toBeInTheDocument()
    expect(screen.getByText('1d')).toBeInTheDocument()
    expect(container.querySelector('table')).toBeNull()
  })

  it('keys on the range length, not the live window that steps every few minutes', async () => {
    const fetchHeatmap = vi.mocked(eventMetricsApi.getSeasonalityHeatmap)
    fetchHeatmap.mockReset()
    fetchHeatmap.mockResolvedValue(heatmap([cell({ weekday: 0, hour: 0, count: 5 })]))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { rerender } = render(heatmapTree(client))
    await screen.findByText('Hour × weekday heatmap')
    expect(fetchHeatmap).toHaveBeenCalledTimes(1)

    // The live bound moved five minutes: same range, no refetch.
    rerender(heatmapTree(client, 7, { from: '2026-01-01T00:05:00', to: '2026-01-08T00:05:00' }))
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(fetchHeatmap).toHaveBeenCalledTimes(1)

    // A new range refetches, and reads the window current at fetch time.
    const monthWindow = { from: '2025-12-09T00:05:00', to: '2026-01-08T00:05:00' }
    rerender(heatmapTree(client, 30, monthWindow))
    await waitFor(() => expect(fetchHeatmap).toHaveBeenCalledTimes(2))
    expect(fetchHeatmap).toHaveBeenLastCalledWith('demo', 'scan-1', expect.objectContaining(monthWindow))
  })
  it('keeps the anomaly ring off the faded fill and adds a non-colour mark', async () => {
    vi.mocked(eventMetricsApi.getSeasonalityHeatmap).mockResolvedValue(
      heatmap([
        // The quiet slot is the one whose anomaly used to vanish: its fill sits
        // at the bottom of the ramp, and the ring shared that opacity.
        cell({ weekday: 1, hour: 3, count: 2, anomaly_count: 1 }),
        cell({ weekday: 3, hour: 14, count: 800 }),
      ]),
    )

    const { container } = renderHeatmap()
    await screen.findByText('Hour × weekday heatmap')

    const flagged = container.querySelector<HTMLElement>('[data-anomaly="true"]')
    expect(flagged).not.toBeNull()
    // The ringed wrapper is never faded; only its fill layer is.
    expect(flagged?.style.opacity).toBe('')
    expect(flagged?.querySelector('[data-count="2"]')).not.toBeNull()
    expect(screen.getAllByTestId('heatmap-anomaly-mark')).toHaveLength(1)
    expect(screen.getByText(/Tue 03:00 UTC — 2 events · 1 anomaly bucket/)).toBeInTheDocument()
  })

  it('paints the anomaly ring above the fill so a busy slot keeps it', async () => {
    vi.mocked(eventMetricsApi.getSeasonalityHeatmap).mockResolvedValue(
      heatmap([
        // The busiest slot: its fill sits near full opacity, and an inset ring
        // on the wrapper painted beneath that fill.
        cell({ weekday: 3, hour: 14, count: 800, anomaly_count: 2 }),
        cell({ weekday: 1, hour: 3, count: 2 }),
      ]),
    )

    const { container } = renderHeatmap()
    await screen.findByText('Hour × weekday heatmap')

    const flagged = container.querySelector<HTMLElement>('[data-anomaly="true"]')
    expect(flagged).not.toBeNull()
    // No box-shadow ring on the wrapper: it would sit under the fill child.
    expect(flagged?.className).not.toMatch(/\bring-/)
    const fill = flagged?.querySelector<HTMLElement>('[data-count="800"]')
    const ring = screen.getByTestId('heatmap-anomaly-ring')
    expect(fill).not.toBeNull()
    expect(ring.parentElement).toBe(flagged)
    expect(ring.className).toMatch(/ring-destructive/)
    // Later sibling of the absolutely positioned fill = painted on top of it.
    expect(fill!.compareDocumentPosition(ring) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(ring.style.opacity).toBe('')
  })

  it('shows an error with a retry instead of "not enough data" when the request fails', async () => {
    const fetchHeatmap = vi.mocked(eventMetricsApi.getSeasonalityHeatmap)
    fetchHeatmap.mockReset()
    fetchHeatmap.mockRejectedValueOnce(new Error('upstream timeout'))
    fetchHeatmap.mockResolvedValue(heatmap([cell({ weekday: 0, hour: 0, count: 5 })]))
    renderHeatmap()

    expect(await screen.findByText('Seasonality heatmap unavailable')).toBeInTheDocument()
    expect(screen.queryByText(/Not enough data/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Hour × weekday heatmap')).toBeInTheDocument()
  })
})
