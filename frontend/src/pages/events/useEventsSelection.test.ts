// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { EventListItem } from '@/types'
import { useEventsSelection } from './useEventsSelection'

const EVENTS = ['a', 'b', 'c'].map(id => ({ id }) as unknown as EventListItem)

describe('useEventsSelection', () => {
  it('drops the selection when the tab, branch or server filters change', () => {
    // 20 rows ticked on Review, then "Set status" on Archived, changed 20
    // events the operator could no longer see.
    const { result, rerender } = renderHook(
      ({ scopeKey }: { scopeKey: string }) => useEventsSelection({ events: EVENTS, scopeKey }),
      { initialProps: { scopeKey: 'review' } },
    )
    act(() => result.current.toggleEventSelected('a', true))
    expect(result.current.selectedCount).toBe(1)

    rerender({ scopeKey: 'review' })
    expect(result.current.selectedCount).toBe(1)

    rerender({ scopeKey: 'archived' })
    expect(result.current.selectedCount).toBe(0)
  })

  it('adds many ids in one update without duplicating any', () => {
    const { result } = renderHook(() => useEventsSelection({ events: EVENTS }))
    act(() => result.current.toggleEventSelected('a', true))
    act(() => result.current.selectMany(['a', 'b', 'z']))

    expect(result.current.selectedEventIds).toEqual(['a', 'b', 'z'])
    expect(result.current.selectedVisibleEventIds).toEqual(['a', 'b'])
  })
})
