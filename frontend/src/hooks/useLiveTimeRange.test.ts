// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { LIVE_WINDOW_STEP_MS, useLiveTimeRange } from './useLiveTimeRange'

const HOUR_MS = 60 * 60 * 1000
const START = new Date('2026-07-29T10:02:00.000Z')

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(START)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useLiveTimeRange', () => {
  it('covers the in-progress bucket by rounding the bound up', () => {
    const { result } = renderHook(() => useLiveTimeRange(HOUR_MS))

    // 10:02 → 10:05, i.e. ahead of now, so a bucket that starts this instant is
    // already inside the window rather than one step away from being visible.
    expect(result.current.to).toBe('2026-07-29T10:05:00.000Z')
    expect(new Date(result.current.to).getTime()).toBeGreaterThan(START.getTime())
    expect(result.current.from).toBe('2026-07-29T09:05:00.000Z')
  })

  it('advances once the clock crosses a step', () => {
    const { result } = renderHook(() => useLiveTimeRange(HOUR_MS))
    const first = result.current

    act(() => {
      vi.advanceTimersByTime(LIVE_WINDOW_STEP_MS)
    })

    // The whole bug: the old `const to = new Date()` inside a useMemo keyed on
    // the range length never moved, so a chart left open kept asking for the
    // window that was current when the page mounted.
    expect(result.current.to).not.toBe(first.to)
    expect(new Date(result.current.to).getTime()).toBeGreaterThan(new Date(first.to).getTime())
    // The span is preserved as the window slides.
    expect(new Date(result.current.to).getTime() - new Date(result.current.from).getTime()).toBe(
      HOUR_MS,
    )
  })

  it('does not churn within a step', () => {
    const { result } = renderHook(() => useLiveTimeRange(HOUR_MS))
    const first = result.current

    act(() => {
      vi.advanceTimersByTime(LIVE_WINDOW_STEP_MS / 2)
    })

    // Query keys are built from these strings; a continuously-moving bound would
    // mint a new cache entry on every render and refetch in a loop.
    expect(result.current).toBe(first)
  })
})
