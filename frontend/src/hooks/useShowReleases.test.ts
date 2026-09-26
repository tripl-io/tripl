// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { withThrowingStorage } from '@/test/storage'
import { SHOW_RELEASES_STORAGE_KEY, setShowReleases, useShowReleases } from './useShowReleases'

describe('useShowReleases (#256)', () => {
  it('defaults to on', () => {
    const { result } = renderHook(() => useShowReleases())
    expect(result.current[0]).toBe(true)
  })

  it('reads a stored "off"', () => {
    localStorage.setItem(SHOW_RELEASES_STORAGE_KEY, 'false')
    const { result } = renderHook(() => useShowReleases())
    expect(result.current[0]).toBe(false)
  })

  it('persists the choice and updates every reader', () => {
    const first = renderHook(() => useShowReleases())
    const second = renderHook(() => useShowReleases())

    act(() => first.result.current[1](false))

    expect(localStorage.getItem(SHOW_RELEASES_STORAGE_KEY)).toBe('false')
    expect(first.result.current[0]).toBe(false)
    expect(second.result.current[0]).toBe(false)

    act(() => second.result.current[1](true))
    expect(first.result.current[0]).toBe(true)
  })

  it('follows a change made in another tab', () => {
    const { result } = renderHook(() => useShowReleases())
    act(() => {
      localStorage.setItem(SHOW_RELEASES_STORAGE_KEY, 'false')
      window.dispatchEvent(new StorageEvent('storage', { key: SHOW_RELEASES_STORAGE_KEY }))
    })
    expect(result.current[0]).toBe(false)
  })

  it('keeps the choice in memory when storage throws', () => {
    withThrowingStorage()
    const { result } = renderHook(() => useShowReleases())
    expect(result.current[0]).toBe(true)
    act(() => result.current[1](false))
    expect(result.current[0]).toBe(false)
    // Leave the module default as the next test expects it: a write that
    // lands also clears the failed-write flag.
    vi.unstubAllGlobals()
    act(() => setShowReleases(true))
  })

  it('keeps the choice when only the write fails (full quota)', () => {
    localStorage.setItem(SHOW_RELEASES_STORAGE_KEY, 'true')
    const setItem = vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
      throw new DOMException('The quota has been exceeded.', 'QuotaExceededError')
    })
    try {
      const { result } = renderHook(() => useShowReleases())
      expect(result.current[0]).toBe(true)
      act(() => result.current[1](false))
      // The stale stored "true" must not snap the checkbox back.
      expect(localStorage.getItem(SHOW_RELEASES_STORAGE_KEY)).toBe('true')
      expect(result.current[0]).toBe(false)
    } finally {
      setItem.mockRestore()
      // A successful write clears the failed-write flag for the next test.
      act(() => setShowReleases(true))
    }
  })
})
