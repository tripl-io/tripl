// @vitest-environment jsdom
import { renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { useOffsetPaging, type PagedQueryState } from './useOffsetPaging'

interface Props {
  query: PagedQueryState
  offset: number
  shown: number
  total: number
}

const SETTLED: PagedQueryState = { isSuccess: true, isPlaceholderData: false }
// The previous page held on screen while the requested one is in flight.
const IN_FLIGHT: PagedQueryState = { isSuccess: true, isPlaceholderData: true }

function renderPaging(initial: Props) {
  return renderHook((props: Props) => useOffsetPaging(props), { initialProps: initial })
}

describe('useOffsetPaging', () => {
  it('describes the first page of a longer list', () => {
    const { result } = renderPaging({ query: SETTLED, offset: 0, shown: 50, total: 254 })
    expect(result.current).toEqual({
      settledOffset: 0,
      rangeStart: 1,
      rangeEnd: 50,
      hasPrev: false,
      hasNext: true,
      isPaging: false,
    })
  })

  it('keeps describing the rows on screen while the next page is in flight', () => {
    const { result, rerender } = renderPaging({ query: SETTLED, offset: 0, shown: 50, total: 254 })

    // Next was clicked: the offset moved, the rows did not.
    rerender({ query: IN_FLIGHT, offset: 50, shown: 50, total: 254 })
    // The bug this replaces: read off the raw offset, the caption claimed
    // "51–100" above rows 1–50.
    expect(result.current.rangeStart).toBe(1)
    expect(result.current.rangeEnd).toBe(50)
    // Held shut, so a second click cannot jump to 100 and drop 51–100 unseen.
    expect(result.current.isPaging).toBe(true)

    rerender({ query: SETTLED, offset: 50, shown: 50, total: 254 })
    expect(result.current).toMatchObject({
      settledOffset: 50,
      rangeStart: 51,
      rangeEnd: 100,
      hasPrev: true,
      hasNext: true,
      isPaging: false,
    })
  })

  it('does not settle on an offset whose request failed', () => {
    const { result, rerender } = renderPaging({ query: SETTLED, offset: 0, shown: 50, total: 120 })
    rerender({ query: { isSuccess: false, isPlaceholderData: false }, offset: 50, shown: 0, total: 0 })
    expect(result.current.settledOffset).toBe(0)
  })

  it('starts settled on an offset it is mounted with, as from a URL', () => {
    const { result } = renderPaging({ query: SETTLED, offset: 100, shown: 20, total: 120 })
    expect(result.current).toMatchObject({ rangeStart: 101, rangeEnd: 120, hasPrev: true, hasNext: false })
  })
})
