import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { eventsPickerKey } from '@/lib/queryKeys'
import { EVENT_PICKER_PAGE_SIZE, eventRosterQuery, useEventRoster } from './useEventRoster'

vi.mock('@/api/events', () => ({
  eventsApi: { list: vi.fn() },
}))

import { eventsApi } from '@/api/events'

const event = (id: string) => ({ id, name: id })

function wrapperWith(queryClient: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
}

// The app's own 60s freshness, so a second reader of a cached page does not
// refetch it.
function newClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 60_000 } } })
}

afterEach(() => {
  vi.clearAllMocks()
})

describe('eventRosterQuery', () => {
  it('asks for one page of the search on the branch, under the shared picker key', async () => {
    vi.mocked(eventsApi.list).mockResolvedValue({ items: [], total: 0 } as never)
    const options = eventRosterQuery('demo', 'b-1', 'pay')
    expect(options.queryKey).toEqual(eventsPickerKey('demo', 'b-1', 'pay'))
    await newClient().fetchQuery(options)
    expect(eventsApi.list).toHaveBeenCalledWith(
      'demo',
      { search: 'pay', limit: EVENT_PICKER_PAGE_SIZE, offset: 0 },
      'b-1',
    )
  })

  it('sends no search for an empty one', async () => {
    vi.mocked(eventsApi.list).mockResolvedValue({ items: [], total: 0 } as never)
    await newClient().fetchQuery(eventRosterQuery('demo', null, ''))
    expect(eventsApi.list).toHaveBeenCalledWith(
      'demo',
      { search: undefined, limit: EVENT_PICKER_PAGE_SIZE, offset: 0 },
      null,
    )
  })
})

describe('useEventRoster', () => {
  it('says how many matches the page left out', async () => {
    vi.mocked(eventsApi.list).mockResolvedValue({ items: [event('a'), event('b')], total: 2466 } as never)
    const { result } = renderHook(
      () => useEventRoster({ slug: 'demo', branchId: null, search: '' }),
      { wrapper: wrapperWith(newClient()) },
    )
    await waitFor(() => expect(result.current.events).toHaveLength(2))
    expect(result.current.hiddenCount).toBe(2464)
  })

  it('fetches nothing until the picker is enabled', async () => {
    vi.mocked(eventsApi.list).mockResolvedValue({ items: [], total: 0 } as never)
    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useEventRoster({ slug: 'demo', branchId: null, search: '', enabled }),
      { wrapper: wrapperWith(newClient()), initialProps: { enabled: false } },
    )
    expect(eventsApi.list).not.toHaveBeenCalled()
    rerender({ enabled: true })
    await waitFor(() => expect(eventsApi.list).toHaveBeenCalledTimes(1))
  })

  it('keeps the last page on screen while the next search lands', async () => {
    // The alert filter's copy had no placeholder, so each debounced keystroke
    // emptied the list until its page arrived.
    let resolveNext: (value: unknown) => void = () => {}
    vi.mocked(eventsApi.list)
      .mockResolvedValueOnce({ items: [event('first')], total: 1 } as never)
      .mockImplementationOnce(() => new Promise(resolve => { resolveNext = resolve }) as never)

    const { result, rerender } = renderHook(
      ({ search }: { search: string }) => useEventRoster({ slug: 'demo', branchId: null, search, debounceMs: 0 }),
      { wrapper: wrapperWith(newClient()), initialProps: { search: '' } },
    )
    await waitFor(() => expect(result.current.events.map(e => e.id)).toEqual(['first']))

    rerender({ search: 'sec' })
    await waitFor(() => expect(eventsApi.list).toHaveBeenCalledTimes(2))
    expect(result.current.events.map(e => e.id)).toEqual(['first'])
    expect(result.current.query.isFetching).toBe(true)

    resolveNext({ items: [event('second')], total: 1 })
    await waitFor(() => expect(result.current.events.map(e => e.id)).toEqual(['second']))
  })

  it('shares one cache entry between pickers asking for the same search', async () => {
    vi.mocked(eventsApi.list).mockResolvedValue({ items: [event('a')], total: 1 } as never)
    const queryClient = newClient()
    const first = renderHook(
      () => useEventRoster({ slug: 'demo', branchId: 'b-1', search: '' }),
      { wrapper: wrapperWith(queryClient) },
    )
    await waitFor(() => expect(first.result.current.events).toHaveLength(1))

    const second = renderHook(
      () => useEventRoster({ slug: 'demo', branchId: 'b-1', search: '' }),
      { wrapper: wrapperWith(queryClient) },
    )
    expect(second.result.current.events).toHaveLength(1)
    expect(eventsApi.list).toHaveBeenCalledTimes(1)
  })
})
