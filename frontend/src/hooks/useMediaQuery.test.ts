// @vitest-environment jsdom
import { renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { COARSE_POINTER_QUERY, useMediaQuery } from './useMediaQuery'

function stubMatchMedia(matching: string) {
  vi.spyOn(window, 'matchMedia').mockImplementation(query => ({
    matches: query === matching,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

describe('useMediaQuery', () => {
  const realMatchMedia = window.matchMedia

  afterEach(() => {
    Object.defineProperty(window, 'matchMedia', { configurable: true, writable: true, value: realMatchMedia })
  })

  it('reads whether the query matches', () => {
    stubMatchMedia(COARSE_POINTER_QUERY)
    expect(renderHook(() => useMediaQuery(COARSE_POINTER_QUERY)).result.current).toBe(true)
    expect(renderHook(() => useMediaQuery('(max-width: 639.98px)')).result.current).toBe(false)
  })

  it('answers no match under the default jsdom stub', () => {
    expect(renderHook(() => useMediaQuery(COARSE_POINTER_QUERY)).result.current).toBe(false)
  })

  it('answers the fallback where there is no matchMedia', () => {
    Object.defineProperty(window, 'matchMedia', { configurable: true, writable: true, value: undefined })
    expect(renderHook(() => useMediaQuery('(min-width: 1024px)')).result.current).toBe(false)
    expect(renderHook(() => useMediaQuery('(min-width: 1024px)', true)).result.current).toBe(true)
  })

  it('ignores the fallback once matchMedia can answer', () => {
    stubMatchMedia('(min-width: 1024px)')
    expect(renderHook(() => useMediaQuery('(max-width: 639.98px)', true)).result.current).toBe(false)
    expect(renderHook(() => useMediaQuery('(min-width: 1024px)', false)).result.current).toBe(true)
  })
})
