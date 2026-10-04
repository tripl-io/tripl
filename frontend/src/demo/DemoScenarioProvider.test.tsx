import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { ApiError } from '@/api/client'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { scansApi } from '@/api/scans'
import type { MetricDefinitionDetailResponse, Project, ScanJob } from '@/types'
import { DemoScenarioProvider } from './DemoScenarioProvider'
import { useDemoScenario, useDemoScenarioActions } from './demoScenarioContext'
import { readScenarioState, writeScenarioState } from './scenarioModel'
import { chapterState, liveLoopState } from './scenarioTestState'

const SLUG = 'acme'
const POLL_MS = 10

function demoProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'p-1',
    name: 'Demo',
    slug: SLUG,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    is_demo: true,
    generation_status: 'ready',
    ...overrides,
  } as Project
}

function scanJob(status: ScanJob['status']): ScanJob {
  return {
    id: 'job-1',
    scan_config_id: 'sc-1',
    status,
    started_at: null,
    completed_at: null,
    result_summary: null,
    error_message: null,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
  }
}

function metricDefinition(status: string | null): MetricDefinitionDetailResponse {
  return { id: 'm-1', last_collection_status: status } as MetricDefinitionDetailResponse
}

/** Renders the scenario's live state and wires the notify actions to buttons. */
function Probe() {
  const { active, state, activeChapter, step, hintsMuted, isWatching } = useDemoScenario()
  const actions = useDemoScenarioActions()
  const chapter = activeChapter ? state.chapters[activeChapter] : undefined
  return (
    <div>
      <span data-testid="step">{step.id}</span>
      <span data-testid="chapter">{activeChapter ?? 'none'}</span>
      <span data-testid="status">{chapter?.status ?? 'none'}</span>
      <span data-testid="hint">{chapter?.hint ?? 'none'}</span>
      <span data-testid="active">{String(active)}</span>
      <span data-testid="watching">{String(isWatching)}</span>
      <span data-testid="muted">{String(hintsMuted)}</span>
      <button onClick={() => actions.notifyScanRunStarted(scanJob('pending'))}>run</button>
      <button
        onClick={() =>
          actions.notifyScanRunStarted({
            ...scanJob('pending'),
            id: 'job-2',
            scan_config_id: 'sc-2',
          })
        }
      >
        run again
      </button>
      <button onClick={() => actions.notifyMetricCollectStarted('m-1')}>collect</button>
      <button onClick={() => actions.notifyStepCompleted('edit-event/save')}>save event</button>
      <button onClick={() => actions.startChapter('edit-event')}>start edit-event</button>
      <button onClick={() => actions.dismissChapter('live-loop')}>dismiss live-loop</button>
      <button onClick={() => actions.restartChapter('live-loop')}>restart live-loop</button>
      <button onClick={actions.muteHints}>mute</button>
    </div>
  )
}

function renderProvider(
  project: Project | undefined,
  route = `/p/${SLUG}/overview`,
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[route]}>
          <DemoScenarioProvider project={project} pollIntervalMs={POLL_MS}>
            <Probe />
          </DemoScenarioProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  }
}

const step = () => screen.getByTestId('step').textContent
const status = () => screen.getByTestId('status').textContent
const chapter = () => screen.getByTestId('chapter').textContent

beforeEach(() => {
  vi.spyOn(scansApi, 'getJob').mockResolvedValue(scanJob('running'))
  vi.spyOn(metricsCatalogApi, 'get').mockResolvedValue(metricDefinition('running'))
})

afterEach(() => {
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe('DemoScenarioProvider — watching the scan the user started', () => {
  it('advances to watch-scan on the run, then to collect-metric when that job completes', async () => {
    renderProvider(demoProject())
    expect(step()).toBe('live-loop/run-scan')

    fireEvent.click(screen.getByText('run'))
    expect(step()).toBe('live-loop/watch-scan')
    expect(screen.getByTestId('watching').textContent).toBe('true')

    vi.mocked(scansApi.getJob).mockResolvedValue(scanJob('completed'))
    await waitFor(() => expect(step()).toBe('live-loop/collect-metric'))
    // The job is polled by the id the user's own run returned.
    expect(scansApi.getJob).toHaveBeenCalledWith(SLUG, 'sc-1', 'job-1')
  })

  it('synchronizes the completed watched job into the same-tab scan list cache', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(['scanJobs', SLUG, 'sc-1'], [scanJob('running')])
    // The Scans list's capped history, cached under the same prefix.
    client.setQueryData(['scanJobs', SLUG, 'sc-1', { limit: 10 }], [scanJob('running')])
    vi.mocked(scansApi.getJob).mockResolvedValue(scanJob('completed'))
    renderProvider(demoProject(), `/p/${SLUG}/scans`, client)

    fireEvent.click(screen.getByText('run'))
    await waitFor(() => expect(step()).toBe('live-loop/collect-metric'))

    for (const key of [['scanJobs', SLUG, 'sc-1'], ['scanJobs', SLUG, 'sc-1', { limit: 10 }]]) {
      expect(client.getQueryData<ScanJob[]>(key)?.[0]).toMatchObject({
        id: 'job-1',
        status: 'completed',
      })
    }
  })

  it('seeds and invalidates an initially absent scan list cache on completion', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries')
    vi.mocked(scansApi.getJob).mockResolvedValue(scanJob('completed'))
    renderProvider(demoProject(), `/p/${SLUG}/scans`, client)

    fireEvent.click(screen.getByText('run'))
    await waitFor(() => expect(step()).toBe('live-loop/collect-metric'))

    expect(client.getQueryData<ScanJob[]>(['scanJobs', SLUG, 'sc-1'])).toEqual([
      scanJob('completed'),
    ])
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ['scanJobs', SLUG, 'sc-1'],
    })
  })

  it('sends a failed job back to run-scan with a hint', async () => {
    vi.mocked(scansApi.getJob).mockResolvedValue(scanJob('failed'))
    renderProvider(demoProject())
    fireEvent.click(screen.getByText('run'))

    await waitFor(() => expect(step()).toBe('live-loop/run-scan'))
    expect(screen.getByTestId('hint').textContent).toBe('scan-failed')
  })

  it('treats a job that vanished under a demo reset as stale', async () => {
    vi.mocked(scansApi.getJob).mockRejectedValue(new ApiError('gone', 404))
    renderProvider(demoProject())
    fireEvent.click(screen.getByText('run'))

    await waitFor(() => expect(screen.getByTestId('hint').textContent).toBe('scan-stale'))
    expect(step()).toBe('live-loop/run-scan')
  })

  it('keeps watching the newest job when Run is fired twice', async () => {
    renderProvider(demoProject())
    fireEvent.click(screen.getByText('run'))
    fireEvent.click(screen.getByText('run again'))

    await waitFor(() => expect(scansApi.getJob).toHaveBeenCalledWith(SLUG, 'sc-2', 'job-2'))
    expect(readScenarioState(SLUG).chapters['live-loop']?.artifacts?.scanJobId).toBe('job-2')
  })

  it('resumes a mid-scan watch after a reload', async () => {
    writeScenarioState(
      SLUG,
      liveLoopState('live-loop/watch-scan', {
        scan: { scanConfigId: 'sc-1', scanJobId: 'job-1', startedAt: Date.now() },
      }),
    )
    vi.mocked(scansApi.getJob).mockResolvedValue(scanJob('completed'))

    // A fresh mount is what a reload looks like: no notify, only storage.
    renderProvider(demoProject())
    expect(step()).toBe('live-loop/watch-scan')
    await waitFor(() => expect(step()).toBe('live-loop/collect-metric'))
  })

  it('resumes a v1 persisted watch through the migration', async () => {
    window.localStorage.setItem(
      `tripl-demo-scenario:${SLUG}`,
      JSON.stringify({
        v: 1,
        status: 'active',
        step: 'watch-scan',
        scan: { scanConfigId: 'sc-1', scanJobId: 'job-1', startedAt: Date.now() },
      }),
    )
    vi.mocked(scansApi.getJob).mockResolvedValue(scanJob('completed'))

    renderProvider(demoProject())
    expect(chapter()).toBe('live-loop')
    expect(step()).toBe('live-loop/watch-scan')
    await waitFor(() => expect(step()).toBe('live-loop/collect-metric'))
  })
})

describe('DemoScenarioProvider — watching the collection the user started', () => {
  async function reachCollectStep() {
    vi.mocked(scansApi.getJob).mockResolvedValue(scanJob('completed'))
    renderProvider(demoProject())
    fireEvent.click(screen.getByText('run'))
    await waitFor(() => expect(step()).toBe('live-loop/collect-metric'))
  }

  it('advances to see-chart when the collection succeeds', async () => {
    await reachCollectStep()
    vi.mocked(metricsCatalogApi.get).mockResolvedValue(metricDefinition('success'))

    fireEvent.click(screen.getByText('collect'))
    await waitFor(() => expect(step()).toBe('live-loop/see-chart'))
    expect(metricsCatalogApi.get).toHaveBeenCalledWith(SLUG, 'm-1')
  })

  it('keeps a failed collection on collect-metric with a hint', async () => {
    await reachCollectStep()
    vi.mocked(metricsCatalogApi.get).mockResolvedValue(metricDefinition('error'))

    fireEvent.click(screen.getByText('collect'))
    await waitFor(() => expect(screen.getByTestId('hint').textContent).toBe('collect-failed'))
    expect(step()).toBe('live-loop/collect-metric')
  })
})

describe('DemoScenarioProvider — completing on the chart', () => {
  const chartRoute = `/p/${SLUG}/monitoring/metric/m-1`

  const seeChartState = () =>
    liveLoopState('live-loop/see-chart', { metric: { metricId: 'm-1', startedAt: Date.now() } })

  it('completes when the user opens the chart of the metric they collected', () => {
    writeScenarioState(SLUG, seeChartState())
    renderProvider(demoProject(), chartRoute)

    expect(status()).toBe('completed')
    expect(readScenarioState(SLUG).chapters['live-loop']?.status).toBe('completed')
  })

  it('does not complete on some other metric chart', () => {
    writeScenarioState(SLUG, seeChartState())
    renderProvider(demoProject(), `/p/${SLUG}/monitoring/metric/m-other`)

    expect(status()).toBe('active')
    expect(step()).toBe('live-loop/see-chart')
  })
})

describe('DemoScenarioProvider — notify- and visit-driven chapters', () => {
  it('completes a deep-link step by arriving on its surface', () => {
    writeScenarioState(SLUG, chapterState('variables', 'variables/open-variables'))
    renderProvider(demoProject(), `/p/${SLUG}/variables`)

    expect(step()).toBe('variables/inspect-values')
    expect(readScenarioState(SLUG).chapters.variables?.step).toBe('variables/inspect-values')
  })

  it('persists an arrival advance made in the same commit as a project switch', () => {
    const OTHER = 'other'
    writeScenarioState(OTHER, chapterState('variables', 'variables/open-variables'))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const tree = (project: Project) => (
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/p/${OTHER}/variables`]}>
          <DemoScenarioProvider project={project} pollIntervalMs={POLL_MS}>
            <Probe />
          </DemoScenarioProvider>
        </MemoryRouter>
      </QueryClientProvider>
    )
    const view = render(tree(demoProject()))

    // The switch lands straight on the other demo's current deep-link surface:
    // the step advances in the very commit that loaded it.
    view.rerender(tree(demoProject({ id: 'p-2', slug: OTHER })))

    expect(step()).toBe('variables/inspect-values')
    // And storage has it, so a reload does not lose the advance.
    expect(readScenarioState(OTHER).chapters.variables?.step).toBe('variables/inspect-values')
  })

  it('completes a mutation step through notifyStepCompleted', () => {
    writeScenarioState(SLUG, chapterState('edit-event', 'edit-event/save'))
    renderProvider(demoProject())
    expect(step()).toBe('edit-event/save')

    fireEvent.click(screen.getByText('save event'))

    expect(status()).toBe('completed')
    expect(readScenarioState(SLUG).chapters['edit-event']?.status).toBe('completed')
  })

  it('drops a stray notify that is not the current step', () => {
    writeScenarioState(SLUG, chapterState('edit-event', 'edit-event/open-editor'))
    renderProvider(demoProject())

    fireEvent.click(screen.getByText('save event'))

    expect(step()).toBe('edit-event/open-editor')
    expect(status()).toBe('active')
  })

  it('starting a chapter leaves the previous one’s progress intact', () => {
    renderProvider(demoProject())
    fireEvent.click(screen.getByText('run'))
    fireEvent.click(screen.getByText('start edit-event'))

    expect(chapter()).toBe('edit-event')
    expect(step()).toBe('edit-event/open-editor')
    expect(readScenarioState(SLUG).chapters['live-loop']?.step).toBe('live-loop/watch-scan')
  })
})

describe('DemoScenarioProvider — eligibility and controls', () => {
  it('is inert for a project that is not a demo', () => {
    renderProvider(demoProject({ is_demo: false }))
    expect(screen.getByTestId('active').textContent).toBe('false')

    // The notify actions are noops — a real project never persists a scenario.
    fireEvent.click(screen.getByText('run'))
    expect(step()).toBe('live-loop/run-scan')
    expect(window.localStorage.getItem(`tripl-demo-scenario:${SLUG}`)).toBeNull()
  })

  it('is inert for a demo that is still seeding', () => {
    renderProvider(demoProject({ generation_status: 'seeding' }))
    expect(screen.getByTestId('active').textContent).toBe('false')
  })

  it('is inert outside a project route', () => {
    renderProvider(undefined, '/workspace')
    expect(screen.getByTestId('active').textContent).toBe('false')
  })

  it('dismiss stops the chapter and survives a remount; restart revives it', () => {
    const first = renderProvider(demoProject())
    fireEvent.click(screen.getByText('run'))
    fireEvent.click(screen.getByText('dismiss live-loop'))
    expect(chapter()).toBe('none')
    expect(screen.getByTestId('active').textContent).toBe('false')
    first.unmount()

    renderProvider(demoProject())
    expect(chapter()).toBe('none')
    expect(readScenarioState(SLUG).chapters['live-loop']?.status).toBe('dismissed')

    fireEvent.click(screen.getByText('restart live-loop'))
    expect(status()).toBe('active')
    expect(step()).toBe('live-loop/run-scan')
  })

  it('muting the hints keeps the scenario running, and a restart unmutes', () => {
    renderProvider(demoProject())
    fireEvent.click(screen.getByText('mute'))
    expect(screen.getByTestId('muted').textContent).toBe('true')
    expect(screen.getByTestId('active').textContent).toBe('true')

    fireEvent.click(screen.getByText('restart live-loop'))
    expect(screen.getByTestId('muted').textContent).toBe('false')
  })

  it('keeps "Hide hints" for the session, per project, across a remount', () => {
    const first = renderProvider(demoProject())
    fireEvent.click(screen.getByText('mute'))
    expect(screen.getByTestId('muted').textContent).toBe('true')
    first.unmount()

    renderProvider(demoProject())
    expect(screen.getByTestId('muted').textContent).toBe('true')
  })

  it('adopts progress another tab made instead of overwriting it', () => {
    renderProvider(demoProject())
    expect(step()).toBe('live-loop/run-scan')

    // The other tab starts edit-event and persists it; this tab only hears the
    // storage event.
    const otherTab = chapterState('edit-event', 'edit-event/save')
    act(() => {
      writeScenarioState(SLUG, otherTab)
      window.dispatchEvent(new StorageEvent('storage', { key: `tripl-demo-scenario:${SLUG}` }))
    })

    expect(chapter()).toBe('edit-event')
    expect(step()).toBe('edit-event/save')
    // And this tab's next write builds on it rather than on its stale copy.
    fireEvent.click(screen.getByText('save event'))
    expect(readScenarioState(SLUG).chapters['edit-event']?.status).toBe('completed')
  })

  it('does not poll while nothing the user started is in flight', () => {
    renderProvider(demoProject())
    expect(scansApi.getJob).not.toHaveBeenCalled()
    expect(metricsCatalogApi.get).not.toHaveBeenCalled()
  })

  it('polls the live-loop scan only while its chapter is active', async () => {
    vi.useFakeTimers()
    renderProvider(demoProject())
    fireEvent.click(screen.getByText('run'))

    // Positive control: with its chapter active, the watch polls on every
    // interval, so the negative half below can only pass for the right reason.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * 3)
    })
    expect(scansApi.getJob).toHaveBeenCalled()

    fireEvent.click(screen.getByText('start edit-event'))
    vi.mocked(scansApi.getJob).mockClear()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * 3)
    })

    expect(scansApi.getJob).not.toHaveBeenCalled()
  })
})
