import type { ReactNode } from 'react'
import { act, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import { chartAnnotationsApi } from '@/api/chartAnnotations'
import { plannedEventsApi } from '@/api/plannedEvents'
import {
  activeSignalsKey,
  chartAnnotationsKey,
  projectChartAnnotationsKey,
  projectKey,
  projectMonitoringSeriesKey,
  projectPlannedEventsKey,
  projectsKey,
} from '@/lib/queryKeys'
import {
  annotationDeleteConfirm,
  invalidateAnnotationLists,
  useAnnotationDelete,
} from './annotationMutations'
import {
  invalidatePlannedEventEffects,
  plannedEventDeleteConfirm,
  usePlannedEventDelete,
} from './plannedEventMutations'

vi.mock('@/api/chartAnnotations', () => ({
  chartAnnotationsApi: { delete: vi.fn().mockResolvedValue(undefined) },
}))
vi.mock('@/api/plannedEvents', () => ({
  plannedEventsApi: { delete: vi.fn().mockResolvedValue(undefined) },
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

function invalidatedKeys(spy: { mock: { calls: unknown[][] } }): unknown[] {
  return spy.mock.calls.map(call => (call[0] as { queryKey: unknown }).queryKey)
}

function wrapperFor(qc: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  }
}

// A window deleted from the Annotations page left the sidebar badge and the
// Overview's Open signals counting signals it had just retagged: the page
// refreshed three of the five things a chart's card did.
describe('expected-window writes', () => {
  it('refresh the windows, the series, the signals and the project summary', () => {
    const qc = new QueryClient()
    const spy = vi.spyOn(qc, 'invalidateQueries')

    invalidatePlannedEventEffects(qc, 'demo')

    expect(invalidatedKeys(spy)).toEqual([
      projectPlannedEventsKey('demo'),
      projectMonitoringSeriesKey('demo'),
      activeSignalsKey('demo'),
      projectKey('demo'),
      projectsKey(),
    ])
  })

  it('delete through one hook that refreshes the same set', async () => {
    const qc = new QueryClient()
    const spy = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => usePlannedEventDelete('demo'), { wrapper: wrapperFor(qc) })

    await act(async () => {
      await result.current.mutateAsync('pe-1')
    })

    expect(plannedEventsApi.delete).toHaveBeenCalledWith('demo', 'pe-1')
    expect(invalidatedKeys(spy)).toContainEqual(projectKey('demo'))
    expect(invalidatedKeys(spy)).toContainEqual(projectsKey())
  })

  it('confirm alike everywhere, and say when a window covers every chart', () => {
    expect(plannedEventDeleteConfirm({ label: 'Spring promo', scope_type: 'metric' })).toMatchObject({
      title: 'Delete expected window?',
      message: 'Anomalies inside "Spring promo" will raise signals and alerts again.',
      variant: 'danger',
    })
    const projectWide = plannedEventDeleteConfirm({ label: 'Maintenance', scope_type: null })
    expect(projectWide.title).toBe('Delete project-wide expected window?')
    expect(projectWide.message).toContain('covers every chart in this project')
  })
})

// A chart card refreshed only its own list, so a project-wide marker deleted
// there stayed on every other chart.
describe('annotation writes', () => {
  it('refresh every annotation list of the project, not one chart’s', async () => {
    const qc = new QueryClient()
    const spy = vi.spyOn(qc, 'invalidateQueries')

    invalidateAnnotationLists(qc, 'demo')
    expect(invalidatedKeys(spy)).toEqual([projectChartAnnotationsKey('demo')])
    // The root covers a chart's own list.
    expect(chartAnnotationsKey('demo', 'event', 'e-1').slice(0, projectChartAnnotationsKey('demo').length))
      .toEqual([...projectChartAnnotationsKey('demo')])

    spy.mockClear()
    const { result } = renderHook(() => useAnnotationDelete('demo'), { wrapper: wrapperFor(qc) })
    await act(async () => {
      await result.current.mutateAsync('a-1')
    })
    expect(chartAnnotationsApi.delete).toHaveBeenCalledWith('demo', 'a-1')
    expect(invalidatedKeys(spy)).toEqual([projectChartAnnotationsKey('demo')])
  })

  it('confirm alike everywhere, and say when a marker is on every chart', () => {
    expect(annotationDeleteConfirm({ label: 'Hotfix', scope_type: 'event' })).toMatchObject({
      title: 'Delete annotation?',
      message: '"Hotfix" will be removed from its chart.',
    })
    const projectWide = annotationDeleteConfirm({ label: 'Release 1.4.0', scope_type: null })
    expect(projectWide.title).toBe('Delete project-wide annotation?')
    expect(projectWide.message).toContain('every chart in this project')
  })
})
