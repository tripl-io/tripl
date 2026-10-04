import { useCallback, useDeferredValue, useMemo, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useInfiniteQuery } from '@tanstack/react-query'

import { eventsApi } from '@/api/events'
import { SEARCH_DEBOUNCE_MS, useDebouncedValue } from '@/hooks/useDebouncedValue'
import { FIRST_PAGE_IN_VIEW_META } from '@/lib/eventsListCache'
import { EVENT_STATUSES, type EventStatus } from '@/lib/eventStatus'
import type { EventListItem, EventType } from '@/types'
import { eventsListKey } from '@/lib/queryKeys'

const SPECIAL_TABS = new Set(['all', 'review', 'archived'])
const EVENTS_PAGE_SIZE = 200
// Bulk "select all matching" pages through the whole result set in chunks. The
// backend caps `limit` at 10 000 (events.py: Query(le=10000)), so this is the
// largest page we can request without a 422 — bigger pages mean fewer requests.
const EVENTS_ID_FETCH_PAGE_SIZE = 10000

// Statuses shown by default (all tab) — excludes archived to preserve
// the existing UX where archived events are hidden unless explicitly selected.
const DEFAULT_ACTIVE_STATUSES: EventStatus[] = EVENT_STATUSES.filter(s => s !== 'archived')

/**
 * Statuses the list request should filter by, from tab + explicit user filter.
 *
 * The explicit dropdown filter ALWAYS wins: the review/archived tabs only
 * provide defaults, so picking "Draft" (or "Any status") on those tabs is
 * honored instead of being silently overridden — "Any status" shows every
 * status except archived (the established hidden-unless-asked default).
 */
export function resolveQueryStatuses(
  activeTab: string,
  filterStatuses: EventStatus[],
): EventStatus[] {
  if (filterStatuses.length > 0) return filterStatuses
  return tabDefaultStatuses(activeTab) ?? DEFAULT_ACTIVE_STATUSES
}

/**
 * The statuses a tab narrows the list to when no status is picked, or `null`
 * for a tab that shows the usual everything-but-archived. The status filter
 * names these, so it does not read "any" over a list of archived events.
 */
export function tabDefaultStatuses(activeTab: string): EventStatus[] | null {
  if (activeTab === 'review') return REVIEW_TAB_STATUSES
  if (activeTab === 'archived') return ARCHIVED_TAB_STATUSES
  return null
}

// Stable references, so a memoised toolbar does not see a new array each render.
const REVIEW_TAB_STATUSES: EventStatus[] = ['in_review']
const ARCHIVED_TAB_STATUSES: EventStatus[] = ['archived']

// Review-queue sort order: 'catalog' keeps the manual/creation order; 'volume'
// asks the server for busiest-first (24h EventMetric volume); 'health' for
// least healthy first (F15, #268), which exists on the main plan only.
export type EventsSortOrder = 'catalog' | 'volume' | 'health'

/** The persisted `sort` URL value, read back; anything else is catalog order. */
export function parseEventsSort(
  raw: string | null,
  branchId: string | null | undefined,
  activeTab?: string,
): EventsSortOrder {
  if (raw === 'volume') return 'volume'
  // Health scores exist for the main plan only: a `sort=health` link opened on
  // a branch falls back to catalog order instead of asking for a 400. Archived
  // events are never scored, and the Archived tab's Sort select offers no
  // health option, so a bookmarked `/events/archived?sort=health` is catalog
  // order too.
  if (raw === 'health' && !branchId && activeTab !== 'archived') return 'health'
  return 'catalog'
}

export type EventsQueryFilters = {
  search: string
  setSearch: (value: string) => void
  filterStatuses: EventStatus[]
  setFilterStatuses: (value: EventStatus[]) => void
  filterTag: string
  setFilterTag: (value: string) => void
  filterSilentDays: number | undefined
  setFilterSilentDays: (value: number | undefined) => void
  filterReviewed: boolean | undefined
  setFilterReviewed: (value: boolean | undefined) => void
  sort: EventsSortOrder
  setSort: (value: EventsSortOrder) => void
  fieldFilters: Record<string, string>
  updateFieldFilter: (name: string, value: string) => void
  metaFilters: Record<string, string>
  updateMetaFilter: (name: string, value: string) => void
  debouncedSearch: string
  debouncedFieldFilters: Record<string, string>
  debouncedMetaFilters: Record<string, string>
  isFilterPending: boolean
  filterEtId: string | undefined
  /** The status values actually sent to the server query (tab-driven) */
  queryStatuses: EventStatus[] | undefined
}

/**
 * Owns the events list's URL-derived filter state and the infinite-paginated
 * server query. Mutations and table rendering stay in EventsPage; this hook
 * just bundles the "filters → URL → query" loop so the host file shrinks.
 */
export function useEventsQuery({
  slug,
  activeTab,
  eventTypes,
  eventTypesLoaded,
  branchId,
}: {
  slug: string | undefined
  activeTab: string
  eventTypes: EventType[]
  /** The event-type list has resolved (successfully). */
  eventTypesLoaded: boolean
  branchId: string | null
}) {
  const [searchParams, setSearchParams] = useSearchParams()

  const search = searchParams.get('q') || ''
  const setSearch = useCallback(
    (v: string) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          if (v) next.set('q', v)
          else next.delete('q')
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  // Status filter: comma-separated or repeatable `status` param
  const filterStatuses = useMemo((): EventStatus[] => {
    const raw = searchParams.getAll('status').flatMap(s => s.split(','))
    const valid = raw.filter((s): s is EventStatus => (EVENT_STATUSES as string[]).includes(s))
    return valid.length > 0 ? valid : []
  }, [searchParams])

  const setFilterStatuses = useCallback(
    (v: EventStatus[]) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          next.delete('status')
          for (const s of v) next.append('status', s)
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const filterTag = searchParams.get('tag') || ''
  const setFilterTag = useCallback(
    (v: string) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          if (v) next.set('tag', v)
          else next.delete('tag')
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const filterSilentDaysRaw = searchParams.get('silent_days')
  const filterSilentDays =
    filterSilentDaysRaw !== null && /^\d+$/.test(filterSilentDaysRaw)
      ? Number(filterSilentDaysRaw)
      : undefined
  const setFilterSilentDays = useCallback(
    (v: number | undefined) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          if (v !== undefined) next.set('silent_days', String(v))
          else next.delete('silent_days')
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  // Reviewed is an axis of its own (an event can be reviewed and still sit in
  // `in_review` — see the user guide), and it had no filter at all, so "Mark
  // reviewed" wrote a flag nobody could isolate afterwards.
  // Absent param = any; only an explicit true/false is persisted, so the
  // default request stays byte-identical to today.
  const filterReviewedRaw = searchParams.get('reviewed')
  const filterReviewed =
    filterReviewedRaw === 'true' ? true : filterReviewedRaw === 'false' ? false : undefined
  const setFilterReviewed = useCallback(
    (v: boolean | undefined) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          if (v === undefined) next.delete('reviewed')
          else next.set('reviewed', String(v))
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  // "Which events are still waiting on an answer?" — the question the discussion
  // could not be asked until threads could be resolved.
  // Same URL shape as `reviewed`: absent means any, so the
  // default request is unchanged.
  const filterOpenQuestionsRaw = searchParams.get('questions')
  const filterOpenQuestions =
    filterOpenQuestionsRaw === 'true' ? true : filterOpenQuestionsRaw === 'false' ? false : undefined
  const setFilterOpenQuestions = useCallback(
    (v: boolean | undefined) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          if (v === undefined) next.delete('questions')
          else next.set('questions', String(v))
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  // Events whose property list carries a property (F23): the Properties
  // catalog links here with `?property=<name>`. The name, not the id, so the
  // link survives a branch switch (branches copy properties under new ids).
  const filterProperty = searchParams.get('property') || ''
  const setFilterProperty = useCallback(
    (v: string) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          if (v) next.set('property', v)
          else next.delete('property')
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  // Sort order lives in the URL under `sort`; only 'volume' and 'health' are
  // persisted so the default (catalog) request stays byte-identical to today.
  const sort: EventsSortOrder = parseEventsSort(searchParams.get('sort'), branchId, activeTab)
  const setSort = useCallback(
    (v: EventsSortOrder) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          if (v === 'volume' || v === 'health') next.set('sort', v)
          else next.delete('sort')
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  // Field/meta filters live in URL under `f.` / `m.` prefixes, keyed by name.
  const fieldFilters = useMemo(() => {
    const out: Record<string, string> = {}
    searchParams.forEach((v, k) => {
      if (k.startsWith('f.')) out[k.slice(2)] = v
    })
    return out
  }, [searchParams])
  const updateFieldFilter = useCallback(
    (name: string, value: string) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          if (value) next.set(`f.${name}`, value)
          else next.delete(`f.${name}`)
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const metaFilters = useMemo(() => {
    const out: Record<string, string> = {}
    searchParams.forEach((v, k) => {
      if (k.startsWith('m.')) out[k.slice(2)] = v
    })
    return out
  }, [searchParams])
  const updateMetaFilter = useCallback(
    (name: string, value: string) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          if (value) next.set(`m.${name}`, value)
          else next.delete(`m.${name}`)
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  // Defer the URL-derived filter values so the input field stays responsive
  // even when the table re-render is expensive. The debounce on top of the
  // deferred value still controls when we hit the API.
  const deferredSearch = useDeferredValue(search)
  const deferredFieldFilters = useDeferredValue(fieldFilters)
  const deferredMetaFilters = useDeferredValue(metaFilters)
  const debouncedSearch = useDebouncedValue(deferredSearch, SEARCH_DEBOUNCE_MS)
  const debouncedFieldFilters = useDebouncedValue(deferredFieldFilters, SEARCH_DEBOUNCE_MS)
  const debouncedMetaFilters = useDebouncedValue(deferredMetaFilters, SEARCH_DEBOUNCE_MS)
  const isFilterPending =
    deferredSearch !== search ||
    deferredFieldFilters !== fieldFilters ||
    deferredMetaFilters !== metaFilters

  const isTypeTab = !SPECIAL_TABS.has(activeTab)
  const filterEtId = isTypeTab
    ? eventTypes.find((e) => e.name === activeTab)?.id
    : undefined
  // A type tab names its type by key, and the id to scope by only exists once
  // the types have loaded. Until then the list must wait: an undefined id reads
  // as "every type", so each cold load of /events/se first fetched and rendered
  // the whole catalog, and a stale link to a deleted or renamed type showed
  // every event under that type's heading.
  const isUnknownTab = isTypeTab && eventTypesLoaded && !filterEtId
  const canQuery = !!slug && (!isTypeTab || !!filterEtId)

  const queryStatuses = useMemo(
    () => resolveQueryStatuses(activeTab, filterStatuses),
    [activeTab, filterStatuses],
  )

  // The server-side filter set, shared by the paginated list query, the
  // "select all matching" sweep and the CSV export so all three agree on what
  // "the current view" means.
  const serverFilters = useMemo(
    () => ({
      event_type_id: filterEtId,
      search: debouncedSearch || undefined,
      status: queryStatuses,
      tag: filterTag || undefined,
      silent_since_days: filterSilentDays,
      reviewed: filterReviewed,
      has_open_questions: filterOpenQuestions,
      property: filterProperty || undefined,
      order_by: sort === 'catalog' ? undefined : sort,
    }),
    [
      filterEtId,
      debouncedSearch,
      queryStatuses,
      filterTag,
      filterSilentDays,
      filterReviewed,
      filterOpenQuestions,
      filterProperty,
      sort,
    ],
  )

  // Whether the table's viewport lies inside the first page, reported by the
  // virtualizer (`reportFirstPageInView`) and read by `refreshEventsLists`
  // when a mutation or a realtime event refreshes the list.
  const firstPageInViewRef = useRef(true)
  const reportFirstPageInView = useCallback((inView: boolean) => {
    firstPageInViewRef.current = inView
  }, [])
  const listMeta = useMemo(
    () => ({ [FIRST_PAGE_IN_VIEW_META]: () => firstPageInViewRef.current }),
    [],
  )

  const eventsQuery = useInfiniteQuery({
    meta: listMeta,
    queryKey: eventsListKey(slug, branchId, {
      filterEtId,
      debouncedSearch,
      queryStatuses,
      filterTag,
      filterSilentDays,
      filterReviewed,
      filterOpenQuestions,
      sort,
      filterProperty,
    }),
    queryFn: ({ pageParam }) =>
      eventsApi.list(slug!, {
        ...serverFilters,
        offset: pageParam,
        limit: EVENTS_PAGE_SIZE,
      }, branchId),
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) => {
      const loaded = allPages.reduce((sum, page) => sum + page.items.length, 0)
      return loaded < lastPage.total ? loaded : undefined
    },
    enabled: canQuery,
    placeholderData: (prev) => prev,
  })

  const eventsData = useMemo(() => {
    const pages = eventsQuery.data?.pages
    const firstPage = pages?.[0]
    if (!pages || !firstPage) return undefined
    return {
      items: pages.flatMap((page) => page.items),
      total: firstPage.total,
    }
  }, [eventsQuery.data])

  const rawEvents: EventListItem[] = useMemo(
    () => eventsData?.items ?? [],
    [eventsData?.items],
  )
  const total = eventsData?.total ?? 0

  // Fetch EVERY event matching the current server filters (not just the loaded
  // pages), so bulk triage can sweep a whole prefix/tab at once — e.g. accept
  // or archive all 499 pending-review events — and so CSV export writes the
  // whole filtered catalog rather than the pages scrolled so far. Returns an
  // empty list when there is nothing to match.
  //
  // The backend rejects `limit > 10000`, so we can't ask for `limit: total` in
  // one shot on large projects. Page through the match set in cap-sized chunks,
  // deduping by id in case rows shift between requests, and let each response's
  // `total` steer the loop (a short/empty page also ends it).
  //
  // The entry condition reads the LOADED `total`, which is 0 before the first
  // page resolves and — under `placeholderData: prev` — still the previous
  // filters' count while a change is in flight. Callers must gate on the query
  // having settled (see EventsPage's `canExportCsv`) rather than read the empty
  // result of those windows as "nothing matched".
  const fetchAllMatching = useCallback(async (): Promise<EventListItem[]> => {
    if (!slug || total === 0) return []
    const byId = new Map<string, EventListItem>()
    let offset = 0
    let expected = total
    while (offset < expected) {
      const page = await eventsApi.list(
        slug,
        { ...serverFilters, offset, limit: EVENTS_ID_FETCH_PAGE_SIZE },
        branchId,
      )
      expected = page.total
      if (page.items.length === 0) break
      for (const event of page.items) byId.set(event.id, event)
      offset += page.items.length
    }
    return [...byId.values()]
  }, [slug, branchId, serverFilters, total])

  const fetchAllMatchingIds = useCallback(
    async (): Promise<string[]> => (await fetchAllMatching()).map(event => event.id),
    [fetchAllMatching],
  )

  return {
    // filters
    search,
    setSearch,
    filterStatuses,
    setFilterStatuses,
    filterTag,
    setFilterTag,
    filterSilentDays,
    setFilterSilentDays,
    filterReviewed,
    setFilterReviewed,
    filterOpenQuestions,
    setFilterOpenQuestions,
    filterProperty,
    setFilterProperty,
    sort,
    setSort,
    fieldFilters,
    updateFieldFilter,
    metaFilters,
    updateMetaFilter,
    debouncedSearch,
    debouncedFieldFilters,
    debouncedMetaFilters,
    isFilterPending,
    filterEtId,
    queryStatuses,
    serverFilters,
    isUnknownTab,
    // query
    eventsQuery,
    reportFirstPageInView,
    rawEvents,
    total,
    fetchAllMatching,
    fetchAllMatchingIds,
  }
}
