import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'

import { useTheme, type Density } from '@/components/theme-provider'
import type { EventListItem } from '@/types'
import type { useEventsQuery } from './useEventsQuery'
import { PHONE_CARD_HEIGHT_ESTIMATE, PHONE_CARD_QUERY } from './eventsPhoneCard'

const VIRTUAL_THRESHOLD = 100

/**
 * `--row-h` per density class (index.css). The estimate has to follow the
 * density the theme applies: a fixed 36px was 30% too tall under the default
 * compact density, so the spacers mis-mapped the scrollbar and the next-page
 * trigger fired early. Rows are still measured once rendered — an
 * expanded JSON cell makes one several times taller — this only sizes the rows
 * not rendered yet.
 */
export const ROW_HEIGHT_BY_DENSITY: Record<Density, number> = {
  compact: 28,
  cozy: 36,
  comfy: 44,
}

/**
 * Height to assume for a row not rendered yet. Below md every row is a
 * wrapping card (eventsPhoneCard.ts), roughly two and a half desktop rows
 * tall, whatever the density.
 */
export function estimateRowHeight(density: Density, phoneCards: boolean): number {
  if (phoneCards) return PHONE_CARD_HEIGHT_ESTIMATE
  return ROW_HEIGHT_BY_DENSITY[density] ?? ROW_HEIGHT_BY_DENSITY.cozy
}

function subscribePhoneCards(onChange: () => void): () => void {
  if (typeof window.matchMedia !== 'function') return () => {}
  const query = window.matchMedia(PHONE_CARD_QUERY)
  query.addEventListener('change', onChange)
  return () => query.removeEventListener('change', onChange)
}

function readPhoneCards(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia(PHONE_CARD_QUERY).matches
}

/**
 * Whether the table should ask for the next page right now.
 *
 * A virtualized list pages as the viewport nears the end of what is loaded. A
 * short list pages eagerly so every row is on screen. Under a client-side
 * field/meta filter the loaded page can hold zero matches while later pages
 * hold many, so the sweep keeps going regardless of how many rows matched so
 * far; stopping at an empty page showed "No events match" over a catalog that
 * had them.
 */
export function shouldFetchNextPage({
  hasNextPage,
  isFetchingNextPage,
  virtualize,
  isClientFiltered,
  loadedCount,
  lastVisibleIndex,
}: {
  hasNextPage: boolean
  isFetchingNextPage: boolean
  virtualize: boolean
  isClientFiltered: boolean
  /** Rows the table holds (after any client-side filter). */
  loadedCount: number
  /** Index of the last rendered virtual row, when virtualizing. */
  lastVisibleIndex: number | undefined
}): boolean {
  if (!hasNextPage || isFetchingNextPage) return false
  if (lastVisibleIndex !== undefined && lastVisibleIndex >= loadedCount - 50) return true
  if (!virtualize) return loadedCount > 0 || isClientFiltered
  return false
}

/**
 * Whether a client-side filter is still sweeping unloaded pages for matches.
 * Only while a page is wanted or on its way: a virtualized list pages on
 * scroll, so with the viewport far from the end nothing is fetching and
 * "Searching the rest…" would sit there forever.
 */
export function isScanningForMatches({
  isClientFiltered,
  hasNextPage,
  wantsNextPage,
  isFetchingNextPage,
}: {
  isClientFiltered: boolean
  hasNextPage: boolean
  wantsNextPage: boolean
  isFetchingNextPage: boolean
}): boolean {
  return isClientFiltered && hasNextPage && (wantsNextPage || isFetchingNextPage)
}

/**
 * Whether every row the table renders comes from the list's first page, so a
 * refresh may cut the list back to that page without changing what is on
 * screen (see `refreshEventsLists`). `lastRenderedIndex` is the last rendered
 * row: the last virtual row (overscan included) when virtualizing, else the
 * last row. A row past the loaded set is a placeholder for a later page.
 */
export function isFirstPageInView({
  events,
  firstPage,
  pageCount,
  lastRenderedIndex,
}: {
  events: Pick<EventListItem, 'id'>[]
  firstPage: Pick<EventListItem, 'id'>[] | undefined
  pageCount: number
  lastRenderedIndex: number | undefined
}): boolean {
  if (pageCount <= 1 || lastRenderedIndex === undefined || lastRenderedIndex < 0) return true
  const lastId = events[lastRenderedIndex]?.id
  return lastId !== undefined && !!firstPage?.some(event => event.id === lastId)
}

/**
 * Row count the virtualizer sizes its scroll spacer to. Use the server `total`
 * (not the loaded-so-far page length) so the spacer is full-height from the
 * first paint and the scrollbar maps linearly to every row; rows past the
 * loaded set render as placeholders until their page streams in. Client-side
 * field/meta filters narrow the loaded rows without a matching server count, so
 * fall back to the filtered length there. Returns 0 when not virtualizing.
 */
export function computeVirtualRowCount({
  virtualize,
  isClientFiltered,
  loadedCount,
  total,
}: {
  virtualize: boolean
  isClientFiltered: boolean
  loadedCount: number
  total: number
}): number {
  if (!virtualize) return 0
  if (isClientFiltered) return loadedCount
  return Math.max(loadedCount, total)
}

export function useEventsTableVirtualization({
  events,
  total,
  eventsQuery,
  isClientFiltered,
  reportFirstPageInView,
}: {
  events: EventListItem[]
  total: number
  eventsQuery: ReturnType<typeof useEventsQuery>['eventsQuery']
  isClientFiltered: boolean
  /** Told whether the rows on screen all come from the first page. */
  reportFirstPageInView?: (inView: boolean) => void
}) {
  const tableScrollRef = useRef<HTMLDivElement>(null)
  const { density } = useTheme()
  const phoneCards = useSyncExternalStore(subscribePhoneCards, readPhoneCards, () => false)
  const rowHeightEstimate = estimateRowHeight(density, phoneCards)
  const virtualize = events.length > VIRTUAL_THRESHOLD
  // Size the scroll spacer to the FULL known row count up front so the
  // scrollbar maps linearly to every row from the first paint. The list is
  // paginated (200/page) and appended as the user scrolls, so `events.length`
  // only reflects the rows loaded so far — sizing the virtualizer to it made
  // the spacer grow one page at a time (scrollHeight ~7k → 14k → 21k px), so
  // dragging the thumb to the bottom landed mid-list and the true end needed
  // many repeated drags. Use the server `total` instead; rows past the loaded
  // set render as height-preserving placeholders until their page streams in.
  // Client-side field/meta filters narrow the loaded rows without a matching
  // server count, so fall back to the filtered length in that mode.
  const rowCount = computeVirtualRowCount({
    virtualize,
    isClientFiltered,
    loadedCount: events.length,
    total,
  })
  // eslint-disable-next-line react-hooks/incompatible-library
  const rowVirtualizer = useVirtualizer({
    count: rowCount,
    getScrollElement: () => tableScrollRef.current,
    estimateSize: () => rowHeightEstimate,
    overscan: 12,
    getItemKey: (index) => events[index]?.id ?? index,
  })
  const rawVirtualItems = rowVirtualizer.getVirtualItems()
  const virtualItems = useMemo(
    () => (virtualize ? rawVirtualItems : []),
    [virtualize, rawVirtualItems],
  )
  const totalVirtualSize = virtualize ? rowVirtualizer.getTotalSize() : 0
  const fetchNextPage = eventsQuery.fetchNextPage

  // The estimate changes with density and with the phone breakpoint;
  // re-measure so the spacers follow.
  useEffect(() => {
    rowVirtualizer.measure()
  }, [rowVirtualizer, rowHeightEstimate])

  const lastVisibleIndex = virtualItems[virtualItems.length - 1]?.index
  const wantsNextPage = shouldFetchNextPage({
    hasNextPage: eventsQuery.hasNextPage,
    isFetchingNextPage: eventsQuery.isFetchingNextPage,
    virtualize,
    isClientFiltered,
    loadedCount: events.length,
    lastVisibleIndex,
  })
  useEffect(() => {
    if (wantsNextPage) void fetchNextPage()
  }, [wantsNextPage, fetchNextPage, events.length])

  const firstPageInView = isFirstPageInView({
    events,
    firstPage: eventsQuery.data?.pages[0]?.items,
    pageCount: eventsQuery.data?.pages.length ?? 0,
    lastRenderedIndex: virtualize ? lastVisibleIndex : events.length - 1,
  })
  useEffect(() => {
    reportFirstPageInView?.(firstPageInView)
  }, [reportFirstPageInView, firstPageInView])

  // The rows actually inside the scroll viewport, for the footer's "Showing
  // X–Y". `virtualItems` also holds the overscan rows either side, so the range
  // read off them was up to 24 rows wider than what was on screen.
  const scrollOffset = rowVirtualizer.scrollOffset ?? 0
  const viewportHeight = rowVirtualizer.scrollRect?.height ?? 0
  const onScreen = virtualItems.filter(
    item => item.end > scrollOffset && item.start < scrollOffset + viewportHeight,
  )
  const firstOnScreen = onScreen[0]
  const lastOnScreen = onScreen[onScreen.length - 1]
  const visibleRange =
    virtualize && firstOnScreen && lastOnScreen
      ? { first: firstOnScreen.index, last: lastOnScreen.index }
      : null

  return {
    tableScrollRef,
    virtualize,
    /** Zero-based first/last row index inside the viewport, when virtualized. */
    visibleRange,
    virtualItems,
    totalVirtualSize,
    /** Ref for a rendered row (with `data-index`) so its real height is used. */
    measureRow: rowVirtualizer.measureElement,
    /** Bring a row into view when virtualized: it may not be rendered yet, so
     *  the DOM has nothing to `scrollIntoView`. */
    scrollToIndex: rowVirtualizer.scrollToIndex,
    /** A client-side filter is still sweeping unloaded pages for matches. */
    isScanningForMatches: isScanningForMatches({
      isClientFiltered,
      hasNextPage: eventsQuery.hasNextPage,
      wantsNextPage,
      isFetchingNextPage: eventsQuery.isFetchingNextPage,
    }),
  }
}
