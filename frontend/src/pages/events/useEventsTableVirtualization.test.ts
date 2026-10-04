import { describe, expect, it } from 'vitest'
import { PHONE_CARD_HEIGHT_ESTIMATE } from './eventsPhoneCard'
import {
  ROW_HEIGHT_BY_DENSITY,
  computeVirtualRowCount,
  estimateRowHeight,
  isFirstPageInView,
  isScanningForMatches,
  shouldFetchNextPage,
} from './useEventsTableVirtualization'

describe('computeVirtualRowCount', () => {
  it('sizes the spacer to the server total, not the loaded page', () => {
    // The bug: sizing to loadedCount grew the spacer one page at a time, so the
    // scrollbar could not map linearly to the whole list.
    expect(
      computeVirtualRowCount({
        virtualize: true,
        isClientFiltered: false,
        loadedCount: 200,
        total: 724,
      }),
    ).toBe(724)
  })

  it('never shrinks below the loaded count when total lags', () => {
    expect(
      computeVirtualRowCount({
        virtualize: true,
        isClientFiltered: false,
        loadedCount: 300,
        total: 250,
      }),
    ).toBe(300)
  })

  it('uses the filtered length under a client-side field/meta filter', () => {
    expect(
      computeVirtualRowCount({
        virtualize: true,
        isClientFiltered: true,
        loadedCount: 40,
        total: 724,
      }),
    ).toBe(40)
  })

  it('returns 0 when the list is below the virtualization threshold', () => {
    expect(
      computeVirtualRowCount({
        virtualize: false,
        isClientFiltered: false,
        loadedCount: 20,
        total: 20,
      }),
    ).toBe(0)
  })
})

describe('shouldFetchNextPage', () => {
  const base = {
    hasNextPage: true,
    isFetchingNextPage: false,
    virtualize: false,
    isClientFiltered: false,
    loadedCount: 40,
    lastVisibleIndex: undefined,
  }

  it('keeps paging under a column filter whose first page matched nothing', () => {
    // Stopping at an empty page showed "No events match" over a catalog whose
    // later pages held matches.
    expect(shouldFetchNextPage({ ...base, isClientFiltered: true, loadedCount: 0 })).toBe(true)
  })

  it('does not page an empty unfiltered list, or while a page is in flight', () => {
    expect(shouldFetchNextPage({ ...base, loadedCount: 0 })).toBe(false)
    expect(shouldFetchNextPage({ ...base, isFetchingNextPage: true })).toBe(false)
    expect(shouldFetchNextPage({ ...base, hasNextPage: false })).toBe(false)
  })

  it('pages a virtualized list only as the viewport nears the loaded end', () => {
    const virtual = { ...base, virtualize: true, loadedCount: 400 }
    expect(shouldFetchNextPage({ ...virtual, lastVisibleIndex: 100 })).toBe(false)
    expect(shouldFetchNextPage({ ...virtual, lastVisibleIndex: 360 })).toBe(true)
  })
})

describe('isScanningForMatches', () => {
  const base = {
    isClientFiltered: true,
    hasNextPage: true,
    wantsNextPage: false,
    isFetchingNextPage: false,
  }

  it('does not claim to search while the viewport is far from the loaded end', () => {
    // A virtualized list pages on scroll: with nothing fetching and nothing
    // wanted, "Searching the rest…" stayed up for good.
    expect(isScanningForMatches(base)).toBe(false)
  })

  it('searches while a page is wanted or in flight', () => {
    expect(isScanningForMatches({ ...base, wantsNextPage: true })).toBe(true)
    expect(isScanningForMatches({ ...base, isFetchingNextPage: true })).toBe(true)
  })

  it('never searches without a column filter or with every page loaded', () => {
    expect(isScanningForMatches({ ...base, isClientFiltered: false, wantsNextPage: true })).toBe(false)
    expect(isScanningForMatches({ ...base, hasNextPage: false, isFetchingNextPage: true })).toBe(false)
  })
})

describe('isFirstPageInView', () => {
  const rows = (ids: string[]) => ids.map(id => ({ id }))
  const events = rows(['a', 'b', 'c', 'd'])
  const firstPage = rows(['a', 'b'])

  it('is in view while the last rendered row comes from the first page', () => {
    expect(isFirstPageInView({ events, firstPage, pageCount: 2, lastRenderedIndex: 1 })).toBe(true)
  })

  it('is not once a later page is rendered, or a placeholder for one', () => {
    expect(isFirstPageInView({ events, firstPage, pageCount: 2, lastRenderedIndex: 2 })).toBe(false)
    expect(isFirstPageInView({ events, firstPage, pageCount: 2, lastRenderedIndex: 9 })).toBe(false)
  })

  it('reads a column-filtered row by id, not by index', () => {
    // Filtered rows keep no page positions: row 1 here is page 2's 'd'.
    const filtered = rows(['a', 'd'])
    expect(isFirstPageInView({ events: filtered, firstPage, pageCount: 2, lastRenderedIndex: 1 })).toBe(false)
  })

  it('is trivially in view with one page or nothing rendered', () => {
    expect(isFirstPageInView({ events, firstPage, pageCount: 1, lastRenderedIndex: 3 })).toBe(true)
    expect(isFirstPageInView({ events, firstPage, pageCount: 2, lastRenderedIndex: undefined })).toBe(true)
  })
})

describe('estimateRowHeight', () => {
  it('follows the density on a desktop table', () => {
    expect(estimateRowHeight('compact', false)).toBe(ROW_HEIGHT_BY_DENSITY.compact)
    expect(estimateRowHeight('comfy', false)).toBe(ROW_HEIGHT_BY_DENSITY.comfy)
  })

  it('assumes a card, not a desktop row, below md', () => {
    // At the desktop estimate the spacer for unloaded pages came out a third of
    // its real length, so the scroll thumb jumped as each page was measured.
    expect(estimateRowHeight('compact', true)).toBe(PHONE_CARD_HEIGHT_ESTIMATE)
    expect(PHONE_CARD_HEIGHT_ESTIMATE).toBeGreaterThan(2 * ROW_HEIGHT_BY_DENSITY.comfy - 20)
  })
})
