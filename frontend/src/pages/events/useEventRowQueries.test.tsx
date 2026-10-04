import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { VirtualItem } from '@tanstack/react-virtual'
import { createElement, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { EventListItem, EventWindowMetrics } from '@/types'
import { useEventRowMetrics } from './useEventRowMetrics'
import { useEventRowSignals } from './useEventsSignals'

let liveRange = { from: '2026-01-01T00:00:00Z', to: '2026-01-03T00:00:00Z' }
vi.mock('@/hooks/useLiveTimeRange', () => ({
  useLiveTimeRange: () => liveRange,
}))

vi.mock('@/api/eventMetrics', () => ({
  eventMetricsApi: {
    getEventsWindowMetrics: vi.fn(),
    getActiveSignals: vi.fn(),
  },
}))

import { eventMetricsApi } from '@/api/eventMetrics'
import { at } from '@/test/at'

let queryClient: QueryClient

function wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: queryClient }, children)
}

function events(count: number): EventListItem[] {
  return Array.from({ length: count }, (_, i) => ({ id: `e${i}` }) as unknown as EventListItem)
}

function virtualRows(first: number, last: number): VirtualItem[] {
  return Array.from({ length: last - first + 1 }, (_, i) => ({
    index: first + i,
    key: first + i,
    start: (first + i) * 28,
    end: (first + i + 1) * 28,
    size: 28,
    lane: 0,
  }))
}

function metric(eventId: string): EventWindowMetrics {
  return { event_id: eventId, total_count: 5, data: [] } as unknown as EventWindowMetrics
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.clearAllMocks()
})

afterEach(() => {
  queryClient.clear()
})

describe('useEventRowMetrics', () => {
  it('keeps the rows filled while the live window steps to its next key', async () => {
    vi.mocked(eventMetricsApi.getEventsWindowMetrics).mockResolvedValue([metric('e0')])
    const rows = events(3)
    const { result, rerender } = renderHook(
      () => useEventRowMetrics({ slug: 'demo', events: rows, eventSignals: new Map(), virtualItems: [] }),
      { wrapper },
    )
    await waitFor(() => expect(result.current.eventWindowMetricsByEvent.has('e0')).toBe(true))

    // The next window's request never answers: the cells must keep the last
    // data rather than blank to "—" until it does.
    vi.mocked(eventMetricsApi.getEventsWindowMetrics).mockReturnValue(new Promise(() => {}))
    liveRange = { from: '2026-01-01T00:05:00Z', to: '2026-01-03T00:05:00Z' }
    rerender()

    await waitFor(() => expect(eventMetricsApi.getEventsWindowMetrics).toHaveBeenCalledTimes(2))
    // The refetch asks for the NEW window; the key does not carry it, so a
    // query function that captured the old one would refetch stale data.
    expect(at(vi.mocked(eventMetricsApi.getEventsWindowMetrics).mock.calls, 1)[1]).toMatchObject({
      time_from: '2026-01-01T00:05:00Z',
      time_to: '2026-01-03T00:05:00Z',
    })
    expect(result.current.eventWindowMetricsByEvent.has('e0')).toBe(true)
  })
})

describe('useEventRowSignals', () => {
  it('asks for the signals of the buckets on screen, not every loaded bucket', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([])
    const rows = events(450)

    renderHook(
      () => useEventRowSignals({ slug: 'demo', events: rows, virtualItems: virtualRows(210, 240) }),
      { wrapper },
    )

    await waitFor(() => expect(eventMetricsApi.getActiveSignals).toHaveBeenCalled())
    const requested = vi.mocked(eventMetricsApi.getActiveSignals).mock.calls.map(([, ids]) => ids?.[0])
    expect(requested).toEqual(['e200'])
  })
})
