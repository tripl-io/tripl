// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { orgStorageKey } from '@/lib/activeOrg'
import { setOnboardingDismissed, useOnboardingDismissed } from './onboardingDismissal'

describe('useOnboardingDismissed', () => {
  beforeEach(() => localStorage.clear())

  it('follows a dismissal and its undo made elsewhere in this tab', () => {
    const { result } = renderHook(() => useOnboardingDismissed('demo', 'p-1'))
    expect(result.current).toBe(false)

    // The toast's Undo and the palette's "Show getting started" write the
    // flag without the checklist knowing; the reader still redraws.
    act(() => setOnboardingDismissed('demo', 'p-1', true))
    expect(result.current).toBe(true)

    act(() => setOnboardingDismissed('demo', 'p-1', false))
    expect(result.current).toBe(false)
  })

  it('follows a dismissal made in another tab', () => {
    const { result } = renderHook(() => useOnboardingDismissed('demo', 'p-1'))
    const key = orgStorageKey('tripl-onboarding-dismissed:p-1')
    act(() => {
      localStorage.setItem(key, '1')
      window.dispatchEvent(new StorageEvent('storage', { key }))
    })
    expect(result.current).toBe(true)
  })

  it('is false while there is no project slug yet', () => {
    const { result } = renderHook(() => useOnboardingDismissed(undefined))
    expect(result.current).toBe(false)
  })
})
