import type { ReactNode } from 'react'
import { act, renderHook } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it } from 'vitest'

import { withThrowingStorage } from '@/test/storage'
import { chartOpenStorageKey, useEventsViewState } from './useEventsViewState'

function wrapper({ children }: { children: ReactNode }) {
  return <MemoryRouter>{children}</MemoryRouter>
}

function renderViewState(slug: string, activeTab: string) {
  return renderHook(
    (props: { slug: string; activeTab: string }) =>
      useEventsViewState({
        slug: props.slug,
        activeTab: props.activeTab,
        activeEt: null,
        eventTypeSignals: new Map(),
        fieldColumns: [],
        fieldFilters: {},
        filterStatuses: [],
        filterSilentDays: undefined,
        filterReviewed: undefined,
        filterOpenQuestions: undefined,
        filterTag: '',
        hiddenColumns: new Set(),
        metaFields: [],
        metaFilters: {},
        projectTotalSignal: null,
      }),
    { wrapper, initialProps: { slug, activeTab } },
  )
}

beforeEach(() => {
  localStorage.clear()
})

describe('useEventsViewState volume chart toggle', () => {
  it('starts collapsed', () => {
    const { result } = renderViewState('demo', 'all')
    expect(result.current.isTabChartOpen).toBe(false)
  })

  it('remembers an opened chart per tab across a remount', () => {
    const first = renderViewState('demo', 'all')
    act(() => first.result.current.setIsTabChartOpen(true))
    expect(first.result.current.isTabChartOpen).toBe(true)
    first.unmount()

    expect(renderViewState('demo', 'all').result.current.isTabChartOpen).toBe(true)
    expect(renderViewState('demo', 'review').result.current.isTabChartOpen).toBe(false)
    expect(JSON.parse(localStorage.getItem(chartOpenStorageKey('demo')) ?? '{}')).toEqual({ all: true })
  })

  it('reads the stored choice of the project it is switched to', () => {
    localStorage.setItem(chartOpenStorageKey('other'), JSON.stringify({ all: true }))
    const { result, rerender } = renderViewState('demo', 'all')
    expect(result.current.isTabChartOpen).toBe(false)

    rerender({ slug: 'other', activeTab: 'all' })
    expect(result.current.isTabChartOpen).toBe(true)
  })

  it('ignores a malformed stored value', () => {
    localStorage.setItem(chartOpenStorageKey('demo'), '["all"]')
    expect(renderViewState('demo', 'all').result.current.isTabChartOpen).toBe(false)
  })

  it('keeps the choice for the session when storage throws', () => {
    withThrowingStorage()
    const { result } = renderViewState('demo', 'all')
    act(() => result.current.setIsTabChartOpen(true))
    expect(result.current.isTabChartOpen).toBe(true)
  })
})
