import { InfiniteQueryObserver, QueryClient, type InfiniteData } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { FIRST_PAGE_IN_VIEW_META, refreshEventsLists } from './eventsListCache'
import { at } from '@/test/at'

type Page = { items: string[]; total: number }

const KEY = ['events', 'demo', null, 'list'] as const
const THREE_PAGES: InfiniteData<Page> = {
  pages: [
    { items: ['a'], total: 3 },
    { items: ['b'], total: 3 },
    { items: ['c'], total: 3 },
  ],
  pageParams: [0, 1, 2],
}

let queryClient: QueryClient
let unsubscribe: (() => void) | undefined

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(() => {
  unsubscribe?.()
  unsubscribe = undefined
  queryClient.clear()
})

/** A mounted table: an active observer whose viewport answers `inView`. */
function observe(inView: boolean) {
  const queryFn = vi.fn(async ({ pageParam }: { pageParam: number }) => at(THREE_PAGES.pages, pageParam))
  queryClient.setQueryData(KEY, THREE_PAGES)
  const observer = new InfiniteQueryObserver(queryClient, {
    queryKey: KEY,
    queryFn,
    initialPageParam: 0,
    getNextPageParam: (_last: Page, all: Page[]) => (all.length < 3 ? all.length : undefined),
    staleTime: Infinity,
    meta: { [FIRST_PAGE_IN_VIEW_META]: () => inView },
  })
  unsubscribe = observer.subscribe(() => {})
  return queryFn
}

function pageParams() {
  return queryClient.getQueryData<InfiniteData<Page>>(KEY)!.pageParams
}

describe('refreshEventsLists', () => {
  it('cuts a list no table shows back to its first page', async () => {
    queryClient.setQueryData(KEY, THREE_PAGES)

    await refreshEventsLists(queryClient, ['events', 'demo'])

    expect(pageParams()).toEqual([0])
    expect(queryClient.getQueryState(KEY)?.isInvalidated).toBe(true)
  })

  it('cuts a shown list when its viewport is inside the first page', async () => {
    const queryFn = observe(true)

    await refreshEventsLists(queryClient, ['events', 'demo'])

    expect(pageParams()).toEqual([0])
    expect(queryFn).toHaveBeenCalledTimes(1)
  })

  it('refetches every page in place for a table scrolled past the first page', async () => {
    // Cutting it blanked the rows on screen until each page came back in turn,
    // and under a column filter shrank the spacer under the scroll position.
    const queryFn = observe(false)

    const refresh = refreshEventsLists(queryClient, ['events', 'demo'])
    expect(pageParams()).toEqual([0, 1, 2])
    await refresh

    expect(pageParams()).toEqual([0, 1, 2])
    expect(queryFn).toHaveBeenCalledTimes(3)
  })
})
