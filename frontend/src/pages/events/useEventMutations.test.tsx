import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, type InfiniteData } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { EventListItem, EventListResponse } from '@/types'
import {
  buildBulkUndo,
  bulkPreviousValues,
  permuteInfinitePages,
  useEventMutations,
} from './useEventMutations'

vi.mock('@/api/events', () => ({
  eventsApi: {
    bulkDelete: vi.fn(),
    bulkUpdate: vi.fn(),
    reorder: vi.fn(),
  },
}))

import { eventsApi } from '@/api/events'
import { at } from '@/test/at'

const SLUG = 'demo'
const BRANCH = null
const INFINITE_KEY = ['events', SLUG, BRANCH, 'infinite'] as const
const FLAT_KEY = ['events', SLUG, BRANCH, 'flat'] as const

function makeItem(id: string, overrides: Partial<EventListItem> = {}): EventListItem {
  return { id, status: 'draft', sunset_at: null, ...overrides } as unknown as EventListItem
}

let queryClient: QueryClient

function wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: queryClient }, children)
}

function seedCaches(items: EventListItem[]) {
  const infinite: InfiniteData<EventListResponse> = {
    pages: [{ items, total: items.length }],
    pageParams: [0],
  }
  const flat: EventListResponse = { items, total: items.length }
  queryClient.setQueryData(INFINITE_KEY, infinite)
  queryClient.setQueryData(FLAT_KEY, flat)
}

function infiniteItems(): EventListItem[] {
  return at(queryClient.getQueryData<InfiniteData<EventListResponse>>(INFINITE_KEY)!.pages, 0).items
}

function flatItems(): EventListItem[] {
  return queryClient.getQueryData<EventListResponse>(FLAT_KEY)!.items
}

function renderMutations() {
  return renderHook(() => useEventMutations({ slug: SLUG, branchId: BRANCH }), { wrapper })
}

beforeEach(() => {
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  vi.clearAllMocks()
})

afterEach(() => {
  queryClient.clear()
})

describe('useEventMutations optimistic apply/rollback', () => {
  it('bulkUpdate optimistically patches status in both cache shapes', async () => {
    vi.mocked(eventsApi.bulkUpdate).mockResolvedValue(undefined as never)
    seedCaches([makeItem('a'), makeItem('b'), makeItem('c')])
    const { result } = renderMutations()

    result.current.bulkUpdateMut.mutate({ eventIds: ['a', 'c'], status: 'in_review' })

    await waitFor(() => {
      expect(infiniteItems().find(e => e.id === 'a')?.status).toBe('in_review')
    })
    expect(infiniteItems().find(e => e.id === 'c')?.status).toBe('in_review')
    expect(infiniteItems().find(e => e.id === 'b')?.status).toBe('draft')
    expect(flatItems().find(e => e.id === 'a')?.status).toBe('in_review')
    expect(flatItems().find(e => e.id === 'c')?.status).toBe('in_review')
    expect(flatItems().find(e => e.id === 'b')?.status).toBe('draft')
  })

  it('bulkUpdate rolls both cache shapes back on error', async () => {
    vi.mocked(eventsApi.bulkUpdate).mockRejectedValue(new Error('boom'))
    seedCaches([makeItem('a'), makeItem('b')])
    const { result } = renderMutations()

    result.current.bulkUpdateMut.mutate({ eventIds: ['a'], status: 'implemented' })

    await waitFor(() => {
      expect(result.current.bulkUpdateMut.isError).toBe(true)
    })
    expect(infiniteItems().find(e => e.id === 'a')?.status).toBe('draft')
    expect(flatItems().find(e => e.id === 'a')?.status).toBe('draft')
  })

  it('bulkDelete optimistically removes from both cache shapes', async () => {
    vi.mocked(eventsApi.bulkDelete).mockResolvedValue(undefined as never)
    seedCaches([makeItem('a'), makeItem('b'), makeItem('c')])
    const onSuccess = vi.fn()
    const { result } = renderHook(
      () => useEventMutations({ slug: SLUG, branchId: BRANCH, onBulkDeleteSuccess: onSuccess }),
      { wrapper },
    )

    result.current.bulkDeleteMut.mutate(['a', 'c'])

    await waitFor(() => {
      expect(infiniteItems().map(e => e.id)).toEqual(['b'])
    })
    expect(flatItems().map(e => e.id)).toEqual(['b'])
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1))
  })

  it('keeps the selection when a bulk mutation fails', async () => {
    // Clearing it optimistically lost a "select all 2,400" sweep on a 4xx/5xx,
    // so the operator had to redo it before they could retry.
    vi.mocked(eventsApi.bulkUpdate).mockRejectedValue(new Error('boom'))
    vi.mocked(eventsApi.bulkDelete).mockRejectedValue(new Error('boom'))
    seedCaches([makeItem('a'), makeItem('b')])
    const onDeleteSuccess = vi.fn()
    const onUpdateSuccess = vi.fn()
    const { result } = renderHook(
      () =>
        useEventMutations({
          slug: SLUG,
          branchId: BRANCH,
          onBulkDeleteSuccess: onDeleteSuccess,
          onBulkUpdateSuccess: onUpdateSuccess,
        }),
      { wrapper },
    )

    result.current.bulkUpdateMut.mutate({ eventIds: ['a'], status: 'live' })
    result.current.bulkDeleteMut.mutate(['b'])

    await waitFor(() => {
      expect(result.current.bulkUpdateMut.isError).toBe(true)
      expect(result.current.bulkDeleteMut.isError).toBe(true)
    })
    expect(onUpdateSuccess).not.toHaveBeenCalled()
    expect(onDeleteSuccess).not.toHaveBeenCalled()
  })

  it('bulkDelete restores both cache shapes on error', async () => {
    vi.mocked(eventsApi.bulkDelete).mockRejectedValue(new Error('boom'))
    seedCaches([makeItem('a'), makeItem('b'), makeItem('c')])
    const { result } = renderMutations()

    result.current.bulkDeleteMut.mutate(['a', 'c'])

    await waitFor(() => {
      expect(result.current.bulkDeleteMut.isError).toBe(true)
    })
    expect(infiniteItems().map(e => e.id)).toEqual(['a', 'b', 'c'])
    expect(flatItems().map(e => e.id)).toEqual(['a', 'b', 'c'])
  })

  it('reconciles a multi-page list with one request, not one per page', async () => {
    vi.mocked(eventsApi.bulkUpdate).mockResolvedValue(undefined as never)
    const infinite: InfiniteData<EventListResponse> = {
      pages: [
        { items: [makeItem('a')], total: 3 },
        { items: [makeItem('b')], total: 3 },
        { items: [makeItem('c')], total: 3 },
      ],
      pageParams: [0, 1, 2],
    }
    queryClient.setQueryData(INFINITE_KEY, infinite)
    const { result } = renderMutations()

    result.current.bulkUpdateMut.mutate({ eventIds: ['a'], status: 'live' })

    await waitFor(() => expect(result.current.bulkUpdateMut.isSuccess).toBe(true))
    const after = queryClient.getQueryData<InfiniteData<EventListResponse>>(INFINITE_KEY)!
    expect(after.pages).toHaveLength(1)
    expect(after.pageParams).toEqual([0])
  })

  it('does not re-request the list after a successful drag', async () => {
    vi.mocked(eventsApi.reorder).mockResolvedValue([] as never)
    seedCaches([makeItem('a'), makeItem('b'), makeItem('c')])
    const { result } = renderMutations()

    result.current.reorderEventsMut.mutate(['c', 'b'])

    await waitFor(() => expect(result.current.reorderEventsMut.isSuccess).toBe(true))
    expect(infiniteItems().map(e => e.id)).toEqual(['a', 'c', 'b'])
    expect(queryClient.getQueryState(INFINITE_KEY)?.isInvalidated).toBe(true)
    expect(queryClient.isFetching()).toBe(0)
  })

  it('keeps a drag across a page boundary in the order dropped', async () => {
    // Each page was permuted on its own: 'c' (page 2) dragged above 'b'
    // (page 1) stayed below it, and nothing refetched to correct it.
    vi.mocked(eventsApi.reorder).mockResolvedValue([] as never)
    queryClient.setQueryData<InfiniteData<EventListResponse>>(INFINITE_KEY, {
      pages: [
        { items: [makeItem('a'), makeItem('b')], total: 4 },
        { items: [makeItem('c'), makeItem('d')], total: 4 },
      ],
      pageParams: [0, 2],
    })
    const { result } = renderMutations()

    result.current.reorderEventsMut.mutate(['c', 'b'])

    await waitFor(() => expect(result.current.reorderEventsMut.isSuccess).toBe(true))
    const after = queryClient.getQueryData<InfiniteData<EventListResponse>>(INFINITE_KEY)!
    expect(after.pages.map(page => page.items.map(e => e.id))).toEqual([['a', 'c'], ['b', 'd']])
  })

  it('undoes every group as one mutation: selection kept, lists refreshed once', async () => {
    vi.mocked(eventsApi.bulkUpdate).mockResolvedValue(undefined as never)
    seedCaches([makeItem('a', { status: 'archived' }), makeItem('b', { status: 'archived' })])
    const onUpdateSuccess = vi.fn()
    const { result } = renderHook(
      () => useEventMutations({ slug: SLUG, branchId: BRANCH, onBulkUpdateSuccess: onUpdateSuccess }),
      { wrapper },
    )
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    result.current.bulkUndoMut.mutate([
      { eventIds: ['a'], status: 'draft' },
      { eventIds: ['b'], status: 'live' },
    ])

    await waitFor(() => expect(result.current.bulkUndoMut.isSuccess).toBe(true))
    expect(eventsApi.bulkUpdate).toHaveBeenCalledTimes(2)
    expect(infiniteItems().map(e => e.status)).toEqual(['draft', 'live'])
    // The selection made since the original action is not the undo's to clear.
    expect(onUpdateSuccess).not.toHaveBeenCalled()
    // Once for the lists, once for the main plan's health scores (F15).
    const keys = invalidate.mock.calls.map(([filters]) => filters?.queryKey)
    expect(keys).toHaveLength(2)
    expect(keys).toContainEqual(['events', SLUG, 'health'])
  })

  it('rolls every undo group back when one fails', async () => {
    vi.mocked(eventsApi.bulkUpdate)
      .mockResolvedValueOnce(undefined as never)
      .mockRejectedValueOnce(new Error('boom'))
    seedCaches([makeItem('a', { status: 'archived' }), makeItem('b', { status: 'archived' })])
    const { result } = renderMutations()

    result.current.bulkUndoMut.mutate([
      { eventIds: ['a'], status: 'draft' },
      { eventIds: ['b'], status: 'live' },
    ])

    await waitFor(() => expect(result.current.bulkUndoMut.isError).toBe(true))
    expect(flatItems().map(e => e.status)).toEqual(['archived', 'archived'])
  })
})

describe('bulkPreviousValues', () => {
  it('reads the values from the rows it is given', () => {
    const rows = [
      makeItem('a', { status: 'live', reviewed: true, owner_id: 'u1' } as Partial<EventListItem>),
      makeItem('b'),
    ]
    expect(bulkPreviousValues(rows, ['a', 'z'])).toEqual(
      new Map([['a', { status: 'live', sunset_at: null, reviewed: true, owner_id: 'u1' }]]),
    )
  })
})

describe('permuteInfinitePages', () => {
  it('permutes across pages and keeps each page its size', () => {
    const data: InfiniteData<EventListResponse> = {
      pages: [
        { items: [makeItem('a'), makeItem('b'), makeItem('c')], total: 5 },
        { items: [makeItem('d'), makeItem('e')], total: 5 },
      ],
      pageParams: [0, 3],
    }
    const after = permuteInfinitePages(data, ['e', 'b', 'd'])
    expect(after.pages.map(page => page.items.map(e => e.id))).toEqual([['a', 'e', 'c'], ['b', 'd']])
    expect(after.pageParams).toEqual([0, 3])
  })
})

describe('buildBulkUndo', () => {
  const previous = new Map([
    ['a', { status: 'draft' as const, sunset_at: null, reviewed: false, owner_id: 'u1' }],
    ['b', { status: 'draft' as const, sunset_at: null, reviewed: true, owner_id: null }],
  ])

  it('groups ids by the value they held before', () => {
    expect(buildBulkUndo(['a', 'b'], { owner_id: 'u2' }, previous)).toEqual([
      { eventIds: ['a'], owner_id: 'u1' },
      { eventIds: ['b'], owner_id: null },
    ])
    expect(buildBulkUndo(['a', 'b'], { status: 'live' }, previous)).toEqual([
      { eventIds: ['a', 'b'], status: 'draft' },
    ])
  })

  it('offers no undo when any row was never loaded', () => {
    // "Select all N matching" covers rows the table never fetched; a partial
    // undo would leave the sweep half reverted.
    expect(buildBulkUndo(['a', 'z'], { reviewed: true }, previous)).toBeNull()
  })
})
