import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { invalidateForEvent } from '@/realtime/invalidationMap'
import { TabMetricsCard } from './TabMetricsCard'
import { unappliedChartFilters } from './utils'
import { at } from '@/test/at'

vi.mock('@/components/ui/chart-lazy', () => ({
  MetricsChart: ({ sigmaThreshold }: { sigmaThreshold?: number }) => (
    <div data-testid="metrics-chart" data-sigma-threshold={sigmaThreshold ?? ''} />
  ),
}))

function installFetch(extra: Record<string, unknown> = {}) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    const url = String(input)
    if (url.includes('/api/v1/projects/demo/events-metrics')) {
      return new Response(
        JSON.stringify({
          ...extra,
          scope: 'events_total',
          scan_config_id: 'scan-1',
          event_id: null,
          event_type_id: null,
          interval: '1h',
          latest_signal: null,
          sigma_threshold: 6,
          data: [
            {
              bucket: '2026-01-01T00:00:00Z',
              count: 10,
              expected_count: null,
              stddev: null,
              is_anomaly: false,
              anomaly_direction: null,
              z_score: null,
            },
          ],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      )
    }
    throw new Error(`Unhandled fetch: ${url}`)
  })
}

function renderCard(
  branchId: string | null,
  { isOpen = true, unappliedFilters }: { isOpen?: boolean; unappliedFilters?: string[] } = {},
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TabMetricsCard
          slug="demo"
          activeEt={null}
          activeTabLabel="All events"
          activeTabSignal={null}
          isOpen={isOpen}
          onOpenChange={() => {}}
          branchId={branchId}
          unappliedFilters={unappliedFilters}
          filters={{
            filterEtId: undefined,
            debouncedSearch: '',
            queryStatuses: ['implemented'],
            filterTag: 'checkout',
          }}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return queryClient
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('TabMetricsCard', () => {
  it('hands the chart the sigma threshold the payload serves', async () => {
    installFetch()
    renderCard(null)

    // 6, not undefined: dropping `sigmaThreshold={tabMetrics?.sigma_threshold}`
    // sends the band back to the chart's own default of 4.
    const chart = await screen.findByTestId('metrics-chart')
    expect(chart).toHaveAttribute('data-sigma-threshold', '6')
  })

  it('sends the active branch so the tag and status filter reads its events', async () => {
    const fetchSpy = installFetch()
    renderCard('branch-1')

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    const url = new URL(String(at(fetchSpy.mock.calls, 0)[0]), 'http://localhost')
    expect(url.pathname).toContain('/api/v1/projects/demo/events-metrics')
    expect(url.searchParams.get('branch')).toBe('branch-1')
    expect(url.searchParams.get('tag')).toBe('checkout')
    expect(url.searchParams.getAll('status')).toEqual(['implemented'])
  })

  it('names no branch on main', async () => {
    const fetchSpy = installFetch()
    renderCard(null)

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    const url = new URL(String(at(fetchSpy.mock.calls, 0)[0]), 'http://localhost')
    expect(url.searchParams.has('branch')).toBe(false)
  })

  it.each(['scan_job.updated', 'metric_collection.updated'] as const)(
    'refetches when the stream reports %s, since it does not poll while live',
    async (type) => {
      const fetchSpy = installFetch()
      const queryClient = renderCard('branch-1')
      await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1))

      invalidateForEvent(queryClient, type, 'demo')

      await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(2))
    },
  )

  it('titles the card in sentence case by what it draws (DS-29)', async () => {
    installFetch()
    renderCard(null, { isOpen: false })

    expect(await screen.findByRole('heading', { name: 'Event volume' })).toBeInTheDocument()
    expect(screen.queryByText(/Dynamics/)).not.toBeInTheDocument()
  })

  it('neither fetches nor polls while the chart is collapsed (EVT-20)', async () => {
    const fetchSpy = installFetch()
    renderCard(null, { isOpen: false })

    expect(await screen.findByRole('button', { name: /Show chart/ })).toBeInTheDocument()
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('sums the last 7 days against the week before in the subtitle (EV-21)', async () => {
    installFetch({ week_total: 612_000, prior_week_total: 588_000 })
    renderCard(null)

    expect(await screen.findByText(/612k in 7d · \+4% vs prior week/)).toBeInTheDocument()
  })

  it('leaves the weekly line out when the response has no weekly totals', async () => {
    installFetch()
    renderCard(null)

    expect(await screen.findByText(/Last 7 days, grouped by hour/)).toBeInTheDocument()
    expect(screen.queryByText(/in 7d/)).not.toBeInTheDocument()
  })

  it('shows no range or bucket controls while collapsed (EV-22)', async () => {
    installFetch()
    renderCard(null, { isOpen: false })

    expect(await screen.findByRole('button', { name: /Show chart/ })).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Time range' })).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Time granularity' })).not.toBeInTheDocument()
  })

  it('picks its range with the shared segmented control (LIVE-26)', async () => {
    const fetchSpy = installFetch()
    renderCard(null)

    const group = await screen.findByRole('group', { name: 'Time range' })
    expect(within(group).getByRole('button', { name: '7d' })).toHaveAttribute('aria-pressed', 'true')
    expect(await screen.findByText(/Last 7 days, grouped by hour/)).toBeInTheDocument()

    fireEvent.click(within(group).getByRole('button', { name: '30d' }))

    expect(within(group).getByRole('button', { name: '30d' })).toHaveAttribute('aria-pressed', 'true')
    expect(within(group).getByRole('button', { name: '7d' })).toHaveAttribute('aria-pressed', 'false')
    // A month of hourly buckets is a sawtooth: the wider range buckets by day (EV-21).
    expect(await screen.findByText(/Last 30 days, grouped by day/)).toBeInTheDocument()
    // The wider window is a new query.
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(2))
  })

  it('names the table filters its series does not apply (EVT-20)', async () => {
    installFetch()
    renderCard(null, { unappliedFilters: ['activity', 'column filters'] })

    expect(
      await screen.findByText(/Not narrowed by activity, column filters\./),
    ).toBeInTheDocument()
  })
})

describe('unappliedChartFilters', () => {
  it('lists only the active filters the metrics endpoint cannot take', () => {
    expect(
      unappliedChartFilters({
        filterSilentDays: undefined,
        filterReviewed: undefined,
        filterOpenQuestions: undefined,
        hasColumnFilters: false,
      }),
    ).toEqual([])
    expect(
      unappliedChartFilters({
        filterSilentDays: 7,
        filterReviewed: false,
        filterOpenQuestions: true,
        hasColumnFilters: true,
      }),
    ).toEqual(['activity', 'reviewed', 'questions', 'column filters'])
  })
})
