import { act, cleanup, render, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { projectsApi } from '@/api/projects'
import { ActiveProjectContext } from '@/components/active-project-context'
import { overviewVolumeRootKey, projectKey, projectMonitoringSeriesKey } from '@/lib/queryKeys'
import type { Project } from '@/types'
import { DemoHistoryFillNotice } from './DemoHistoryFillNotice'
import { useDemoHistoryFill } from './useDemoHistoryFill'

function demoProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'p-1',
    name: 'Demo',
    slug: 'demo',
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    is_demo: true,
    generation_status: 'ready',
    demo_history_status: 'filling',
    demo_history_progress: 40,
    ...overrides,
  } as Project
}

function newClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function renderNotice(project: Project | undefined, { compact = false, client = newClient() } = {}) {
  return render(
    <QueryClientProvider client={client}>
      <ActiveProjectContext.Provider value={project}>
        <DemoHistoryFillNotice compact={compact} />
      </ActiveProjectContext.Provider>
    </QueryClientProvider>,
  )
}

const notice = () => document.querySelector('[data-slot="demo-history-fill-notice"]')

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('DemoHistoryFillNotice', () => {
  it('renders nothing outside a demo, or once the fill is complete', () => {
    renderNotice(undefined)
    expect(notice()).toBeNull()
    cleanup()
    renderNotice(demoProject({ is_demo: false }))
    expect(notice()).toBeNull()
    cleanup()
    renderNotice(demoProject({ demo_history_status: 'complete', demo_history_progress: null }))
    expect(notice()).toBeNull()
    cleanup()
    // A demo built before the fill existed.
    renderNotice(demoProject({ demo_history_status: null, demo_history_progress: null }))
    expect(notice()).toBeNull()
  })

  it('says how far the fill is and that the charts will move', () => {
    renderNotice(demoProject())
    const status = screen.getByRole('status')
    expect(status).toHaveAttribute('data-slot', 'demo-history-fill-notice')
    expect(status).toHaveTextContent('History is still loading — 40%.')
    expect(status).toHaveTextContent(
      'This demo opened with its last 2 days. Older days fill in from the newest back, and the charts update as each week lands.',
    )
  })

  it('says it is loading without a figure the server has not given', () => {
    renderNotice(demoProject({ demo_history_progress: null }))
    expect(screen.getByRole('status')).toHaveTextContent('History is still loading…')
    expect(screen.getByRole('status')).not.toHaveTextContent('%')
  })

  it('says a failed fill left the demo shorter, and how to get the rest', () => {
    renderNotice(demoProject({ demo_history_status: 'failed', demo_history_progress: null }))
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('Older history could not be loaded.')
    expect(status).toHaveTextContent(
      'The charts show the days this demo holds. Reset rebuilds the demo with its full history.',
    )
  })

  it('keeps the same copy in the compact variant', () => {
    renderNotice(demoProject(), { compact: true })
    expect(screen.getByRole('status')).toHaveTextContent('History is still loading — 40%.')
  })

  it('follows the live project row the banner polls, without fetching it itself', async () => {
    const get = vi.spyOn(projectsApi, 'get')
    const client = newClient()
    // The shell's row (the project list) still says 40%; the poll has seen 70%.
    client.setQueryData(projectKey('demo'), demoProject({ demo_history_progress: 70 }))
    renderNotice(demoProject(), { client })
    expect(screen.getByRole('status')).toHaveTextContent('History is still loading — 70%.')
    act(() => {
      client.setQueryData(projectKey('demo'), demoProject({ demo_history_status: 'complete', demo_history_progress: null }))
    })
    // TanStack hands cache updates to observers on its next tick.
    await waitFor(() => expect(notice()).toBeNull())
    expect(get).not.toHaveBeenCalled()
  })
})

describe('useDemoHistoryFill', () => {
  function wrapperFor(client: QueryClient) {
    return ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    )
  }

  it('the poller refetches the series once per new progress, not on mount', async () => {
    const get = vi.spyOn(projectsApi, 'get').mockResolvedValue(demoProject())
    const client = newClient()
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    const { result } = renderHook(() => useDemoHistoryFill(demoProject()), { wrapper: wrapperFor(client) })

    // The first poll answers with the progress the page opened on.
    await waitFor(() => expect(client.getQueryState(projectKey('demo'))?.status).toBe('success'))
    expect(get).toHaveBeenCalledTimes(1)
    expect(result.current).toEqual({ filling: true, failed: false, progress: 40 })
    expect(invalidate).not.toHaveBeenCalled()

    act(() => {
      client.setQueryData(projectKey('demo'), demoProject({ demo_history_progress: 55 }))
    })
    await waitFor(() => expect(result.current.progress).toBe(55))
    expect(invalidate).toHaveBeenCalledTimes(2)
    expect(invalidate).toHaveBeenCalledWith({ queryKey: projectMonitoringSeriesKey('demo') })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: overviewVolumeRootKey('demo') })
  })

  it('a reader never invalidates, so a page with notices does not refetch twice', async () => {
    const get = vi.spyOn(projectsApi, 'get')
    const client = newClient()
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    const { result } = renderHook(() => useDemoHistoryFill(demoProject(), { poll: false }), {
      wrapper: wrapperFor(client),
    })
    act(() => {
      client.setQueryData(projectKey('demo'), demoProject({ demo_history_progress: 55 }))
    })
    await waitFor(() => expect(result.current.progress).toBe(55))
    expect(invalidate).not.toHaveBeenCalled()
    expect(get).not.toHaveBeenCalled()
  })
})
