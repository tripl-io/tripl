import type { VirtualItem } from '@tanstack/react-virtual'
import { describe, expect, it } from 'vitest'
import { visibleBucketRange } from './useEventRowMetrics'

function virtualItem(index: number): VirtualItem {
  return { index, key: index, start: index * 36, end: (index + 1) * 36, size: 36, lane: 0 }
}

describe('visibleBucketRange', () => {
  // Regression: window-metrics is the events page's dominant
  // cost (2 calls, ~103 KB, up to 4.5 s on a 200-row first page) and every
  // bucket also carries its own refresh timer, so bucketing over every
  // accumulated row made both the initial cost and the recurring refresh scale
  // with how far the user had scrolled.
  it('asks for one bucket while the first page is on screen', () => {
    const items = Array.from({ length: 44 }, (_, i) => virtualItem(i))

    expect(visibleBucketRange(items)).toEqual({ first: 0, last: 0 })
  })

  it('follows the viewport instead of accumulating every scrolled-past bucket', () => {
    // Row 1,500 of a 2.4k list: buckets 0-14 must not still be in flight.
    const items = Array.from({ length: 44 }, (_, i) => virtualItem(1500 + i))

    expect(visibleBucketRange(items)).toEqual({ first: 15, last: 15 })
  })

  it('spans both buckets when the viewport straddles a boundary', () => {
    const items = [virtualItem(95), virtualItem(105)]

    expect(visibleBucketRange(items)).toEqual({ first: 0, last: 1 })
  })

  it('defaults to the first bucket before the virtualizer has measured', () => {
    // Also the non-virtualized path: ≤100 rows is a single bucket anyway.
    expect(visibleBucketRange([])).toEqual({ first: 0, last: 0 })
  })
})
