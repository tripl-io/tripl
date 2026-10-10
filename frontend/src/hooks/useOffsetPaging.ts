import { useState } from 'react'

/** The two flags of a TanStack query this hook reads; a panel handed them as
 * props can pass them without the query object. */
export interface PagedQueryState {
  isSuccess: boolean
  isPlaceholderData: boolean
}

export interface OffsetPaging {
  /** The offset the rows ON SCREEN were fetched from. */
  settledOffset: number
  /** 1-based position of the first visible row. */
  rangeStart: number
  /** 1-based position of the last visible row. */
  rangeEnd: number
  /** Rows exist before the visible page (newer, in a newest-first log). */
  hasPrev: boolean
  /** Rows exist after the visible page (older, in a newest-first log). */
  hasNext: boolean
  /** Another page is in flight while the previous one stays on screen. Hold
   * the paging buttons shut for as long as this is true. */
  isPaging: boolean
}

/**
 * Where an offset-paged list really is, for a query that keeps the previous
 * page on screen (`placeholderData: keepPreviousData`).
 *
 * The placeholder holds the old rows for the whole round trip, so the requested
 * `offset` — which moves the instant Older or Next is clicked — describes rows
 * that are not there yet. Read off it, the caption claimed "Showing 51–100"
 * above rows 1–50, and `hasNext` kept the button live for a second click that
 * jumped straight to 100 and dropped the page in flight unrendered. Everything
 * here is read off the offset the VISIBLE rows came from instead, and
 * `isPaging` says when the two differ.
 *
 * `shown` is the number of rows the page renders from the response (after any
 * look-ahead row is sliced off); `total` is the response's count of the whole
 * filtered set.
 */
export function useOffsetPaging({
  query,
  offset,
  shown,
  total,
}: {
  query: PagedQueryState
  offset: number
  shown: number
  total: number
}): OffsetPaging {
  const [settledOffset, setSettledOffset] = useState(offset)
  // Adjusted during render, not in an effect: this follows the query the way
  // React documents following a prop, and an effect would paint one frame with
  // the fresh rows still described by the previous offset.
  if (query.isSuccess && !query.isPlaceholderData && settledOffset !== offset) {
    setSettledOffset(offset)
  }
  const rangeEnd = settledOffset + shown
  return {
    settledOffset,
    rangeStart: settledOffset + 1,
    rangeEnd,
    hasPrev: settledOffset > 0,
    hasNext: rangeEnd < total,
    isPaging: query.isPlaceholderData,
  }
}
