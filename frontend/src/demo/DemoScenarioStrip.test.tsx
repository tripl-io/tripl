import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { scansApi } from '@/api/scans'
import type { MetricDefinitionDetailResponse, Project, ScanJob } from '@/types'
import { DemoScenarioProvider } from './DemoScenarioProvider'
import { DemoScenarioStrip } from './DemoScenarioStrip'
import { ScenarioCoachMark } from './ScenarioCoachMark'
import {
  CHAPTER_IDS,
  CHAPTER_STEP_IDS,
  CHAPTER_TITLES,
  SCENARIO_HINT_COPY,
  initialScenarioState,
  readScenarioState,
  scenarioReducer,
  writeScenarioState,
  type ScenarioState,
} from './scenarioModel'
import { chapterState, liveLoopState } from './scenarioTestState'
import { setWelcomeDismissed } from './welcomeDismissal'
import { at } from '@/test/at'
import { ActiveProjectContext } from '@/components/active-project-context'
import { AuthContext } from '@/components/auth-context'
import { personaAuth, type Persona } from '@/test/persona'

const SLUG = 'acme'
const POLL_MS = 10_000

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

/** Every chapter walked, so `nextChapter` resolves to null — the end of the demo. */
function everyChapterCompleted(): ScenarioState {
  const chapters: ScenarioState['chapters'] = {}
  for (const id of CHAPTER_IDS) {
    chapters[id] = { status: 'completed', step: CHAPTER_STEP_IDS[id][0] }
  }
  return { v: 3, activeChapter: at(CHAPTER_IDS, -1), chapters }
}

const scanArtifact = () => ({ scanConfigId: 'sc-1', scanJobId: 'job-1', startedAt: Date.now() })
const metricArtifact = () => ({ metricId: 'm-1', startedAt: Date.now() })

/** Seeds the persisted scenario, then mounts the real provider around the strip. */
function renderStrip(
  state: ScenarioState | null,
  project: Project = demoProject(),
  route = `/p/${SLUG}/overview`,
) {
  if (state) writeScenarioState(SLUG, state)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <DemoScenarioProvider project={project} pollIntervalMs={POLL_MS}>
          <DemoScenarioStrip />
        </DemoScenarioProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const strip = () => screen.queryByRole('region', { name: 'Demo scenario' })
const cta = (name: RegExp) => screen.getByRole('link', { name })

beforeEach(() => {
  // The watches must stay pending: a settled poll would advance the step out
  // from under the assertion.
  vi.spyOn(scansApi, 'getJob').mockResolvedValue(scanJob('running'))
  vi.spyOn(metricsCatalogApi, 'get').mockResolvedValue(metricDefinition('running'))
})

afterEach(() => {
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe('DemoScenarioStrip — the active chapter', () => {
  it('names the chapter and coaches its current step with a deep link', () => {
    renderStrip(liveLoopState('live-loop/run-scan'))

    expect(screen.getByText(CHAPTER_TITLES['live-loop'])).toBeInTheDocument()
    expect(screen.getByText('Run a scan')).toBeInTheDocument()
    expect(
      screen.getByText('Run a scan to pull fresh volume from the demo warehouse.'),
    ).toBeInTheDocument()
    expect(screen.getByText('Step 1 of 4')).toBeInTheDocument()
    expect(cta(/Open Scans/)).toHaveAttribute('href', `/p/${SLUG}/scans`)
  })

  it('marks itself for the banner row it sits in, and keeps its controls named (LIVE-9)', () => {
    renderStrip(liveLoopState('live-loop/run-scan'))

    // The banner gives up its own labels only while this slot is filled.
    expect(screen.getByRole('region', { name: 'Demo scenario' })).toHaveAttribute('data-demo-scenario')
    // Icon-only where the row is shared, but a name is a name at every width.
    expect(screen.getByRole('button', { name: 'Dismiss' })).toHaveAttribute('title', 'Dismiss')
    expect(
      screen.getByText('Run a scan to pull fresh volume from the demo warehouse.'),
    ).toHaveAttribute('title', 'Run a scan to pull fresh volume from the demo warehouse.')
  })

  it('sizes the progress to the chapter, not to a global step count', () => {
    renderStrip(chapterState('edit-event', 'edit-event/set-value'))

    expect(screen.getByText(CHAPTER_TITLES['edit-event'])).toBeInTheDocument()
    expect(screen.getByText('Step 2 of 4')).toBeInTheDocument()
    expect(screen.getByText('Enter a sample Product ID')).toBeInTheDocument()
  })

  it('points a step on another tab of the same page at that tab', () => {
    // Alerting opens on the Inbox; "Add rule" lives on the Rules tab. Hiding the
    // link because the pathname matched left the user on the Inbox with only
    // "the highlighted control isn't visible" to go on.
    renderStrip(chapterState('alerting', 'alerting/create-rule'), demoProject(), `/p/${SLUG}/alerting`)
    expect(cta(/Open Rules/)).toHaveAttribute('href', `/p/${SLUG}/alerting?section=monitors`)
  })

  it('drops the link once the user is on the step tab', () => {
    renderStrip(
      chapterState('alerting', 'alerting/create-rule'),
      demoProject(),
      `/p/${SLUG}/alerting?section=monitors`,
    )
    expect(screen.queryByRole('link', { name: /Open Rules/ })).not.toBeInTheDocument()
  })

  it('links watch-scan at the run the user started', () => {
    renderStrip(liveLoopState('live-loop/watch-scan', { scan: scanArtifact() }))

    expect(screen.getByText('Watch it land')).toBeInTheDocument()
    expect(cta(/Open the run/)).toHaveAttribute('href', `/p/${SLUG}/scans/sc-1`)
  })

  it('pulses while the scan the user started is being watched', () => {
    const { container } = renderStrip(
      liveLoopState('live-loop/watch-scan', { scan: scanArtifact() }),
    )

    expect(container.querySelector('.pulse-dot')).not.toBeNull()
  })

  it('explains a regression instead of silently rewinding', () => {
    renderStrip(liveLoopState('live-loop/run-scan', { hint: 'scan-failed' }))

    expect(screen.getByText(SCENARIO_HINT_COPY['scan-failed'])).toBeInTheDocument()
  })

  it('announces the step text politely', () => {
    const { container } = renderStrip(liveLoopState('live-loop/run-scan'))

    const live = container.querySelector('[aria-live="polite"]')
    expect(live).not.toBeNull()
    expect(live?.textContent).toContain('Run a scan')
  })
})

describe('DemoScenarioStrip — dismissal and completion', () => {
  it('dismiss hides the strip and persists the per-chapter dismissal', () => {
    renderStrip(liveLoopState('live-loop/run-scan'))

    fireEvent.click(screen.getByRole('button', { name: /Dismiss/ }))

    expect(strip()).toBeNull()
    expect(readScenarioState(SLUG).chapters['live-loop']?.status).toBe('dismissed')
    expect(readScenarioState(SLUG).activeChapter).toBeNull()
  })

  it('stays hidden on the next mount once dismissed', () => {
    const first = renderStrip(liveLoopState('live-loop/run-scan'))
    fireEvent.click(screen.getByRole('button', { name: /Dismiss/ }))
    first.unmount()

    // No seed: a fresh mount reads the dismissal back out of storage.
    renderStrip(null)
    expect(strip()).toBeNull()
  })

  it('celebrates a completed chapter and offers the next one in order', () => {
    renderStrip(liveLoopState('live-loop/see-chart', { status: 'completed' }))

    expect(screen.getByText(/Chapter complete/)).toBeInTheDocument()

    const next = cta(new RegExp(`Next: ${CHAPTER_TITLES['edit-event']}`))
    expect(next).toHaveAttribute('href', `/p/${SLUG}/events`)

    // Clicking starts the offered chapter, so the strip flips to coaching it.
    fireEvent.click(next)
    expect(screen.getByText(CHAPTER_TITLES['edit-event'])).toBeInTheDocument()
    expect(screen.getByText('Step 1 of 4')).toBeInTheDocument()
    expect(readScenarioState(SLUG).activeChapter).toBe('edit-event')
  })

  it('restart starts the same chapter over from step 1', () => {
    renderStrip(liveLoopState('live-loop/see-chart', { status: 'completed' }))

    fireEvent.click(screen.getByRole('button', { name: /Restart chapter/ }))

    expect(screen.getByText('Run a scan')).toBeInTheDocument()
    expect(screen.getByText('Step 1 of 4')).toBeInTheDocument()
    expect(readScenarioState(SLUG).chapters['live-loop']?.status).toBe('active')
  })

  it('lets the victory lap be put away, so it is not permanent chrome', () => {
    renderStrip(liveLoopState('live-loop/see-chart', { status: 'completed' }))

    fireEvent.click(screen.getByRole('button', { name: /Dismiss/ }))

    expect(strip()).toBeNull()
    // Only the strip goes away — the chapter stays completed, so the picker
    // never demotes a finished chapter to Paused.
    expect(readScenarioState(SLUG).chapters['live-loop']?.status).toBe('completed')
    expect(readScenarioState(SLUG).activeChapter).toBeNull()
  })

  it('ends the last chapter by pointing out of the demo, not at a dead stop', () => {
    // The demo is the accented default CTA on an empty workspace, so this is the
    // moment of highest intent — and it offered only Restart and Dismiss, with
    // nothing anywhere in the demo naming the real product.
    renderStrip(everyChapterCompleted())

    expect(screen.getByText(/That was the last one/)).toBeInTheDocument()
    // The dashboard, where "New project — start empty and connect your own
    // warehouse" lives; not a demo-scoped link to the global connection page.
    expect(cta(/Create a real project/)).toHaveAttribute('href', '/workspace')
    expect(screen.queryByRole('link', { name: /^Next: / })).toBeNull()
    // Restarting the walk is still there beside it.
    expect(screen.getByRole('button', { name: /Restart chapter/ })).toBeInTheDocument()
  })
})

describe('DemoScenarioStrip — when the coached control is nowhere on screen', () => {
  const SCANS_ROUTE = `/p/${SLUG}/scans`
  const MISSING_COPY =
    "The highlighted control isn't visible — it may be filtered out, below the fold, or already handled."
  const missingLine = () => screen.queryByText(MISSING_COPY)

  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  /** The strip plus a live coach mark for run-scan, inside one real provider. */
  function renderStripWithMark(route: string) {
    writeScenarioState(SLUG, liveLoopState('live-loop/run-scan'))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[route]}>
          <DemoScenarioProvider project={demoProject()} pollIntervalMs={POLL_MS}>
            <DemoScenarioStrip />
            <ScenarioCoachMark step="live-loop/run-scan">
              <button type="button">Run scan</button>
            </ScenarioCoachMark>
          </DemoScenarioProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    )
  }

  it('says nothing at first, then flags the missing control after the grace period', () => {
    renderStrip(liveLoopState('live-loop/run-scan'), demoProject(), SCANS_ROUTE)

    expect(missingLine()).toBeNull()

    // Just short of the delay: still quiet — route transitions must not flicker.
    act(() => {
      vi.advanceTimersByTime(999)
    })
    expect(missingLine()).toBeNull()

    act(() => {
      vi.advanceTimersByTime(1)
    })
    expect(missingLine()).not.toBeNull()
    // Already on Scans: no "Open Scans" that goes nowhere (#251 SH-6).
    expect(screen.queryByRole('link', { name: /Open Scans/ })).toBeNull()
  })

  /** The strip on the step's surface for a signed-in `role` on this demo. */
  function renderStripAs(role: Persona) {
    writeScenarioState(SLUG, liveLoopState('live-loop/run-scan'))
    // A viewer is an organization member whose row in this demo is `viewer`:
    // the server answers the project to them as read-only.
    const project = demoProject({
      created_by_user_id: 'someone-else',
      ...(role === 'viewer' ? { my_role: 'viewer' as const, can_mutate: false } : {}),
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={client}>
        <AuthContext.Provider value={personaAuth(role)}>
          <ActiveProjectContext.Provider value={project}>
            <MemoryRouter initialEntries={[SCANS_ROUTE]}>
              <DemoScenarioProvider project={project} pollIntervalMs={POLL_MS}>
                <DemoScenarioStrip />
              </DemoScenarioProvider>
            </MemoryRouter>
          </ActiveProjectContext.Provider>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )
  }

  it('offers the reset only to whoever can reset the demo (#251 JR-17)', () => {
    renderStripAs('owner')
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(
      screen.getByText(`${MISSING_COPY} Resetting the demo project restores every guided example.`),
    ).toBeInTheDocument()
  })

  it('tells a viewer the step needs edit access instead of pointing at a hidden control (#251 JR-17)', () => {
    renderStripAs('viewer')
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(missingLine()).toBeNull()
    expect(screen.getByText(/This step needs edit access/)).toBeInTheDocument()
    expect(screen.queryByText(/Resetting the demo project/)).toBeNull()
  })

  it('stays quiet away from the step surface, where a mark is not expected', () => {
    renderStrip(liveLoopState('live-loop/run-scan'), demoProject(), `/p/${SLUG}/overview`)

    act(() => {
      vi.advanceTimersByTime(2000)
    })

    expect(missingLine()).toBeNull()
    // Off the step's page the link still goes somewhere.
    expect(cta(/Open Scans/)).toHaveAttribute('href', `/p/${SLUG}/scans`)
  })

  it('stays quiet on another tab of the step page, where the link leads to the control', () => {
    renderStrip(chapterState('alerting', 'alerting/create-rule'), demoProject(), `/p/${SLUG}/alerting`)

    act(() => {
      vi.advanceTimersByTime(2000)
    })

    expect(missingLine()).toBeNull()
    expect(cta(/Open Rules/)).toHaveAttribute('href', `/p/${SLUG}/alerting?section=monitors`)
  })

  it('expects no mark on a deep-link step with no on-surface anchor', () => {
    renderStrip(
      chapterState('variables', 'variables/open-variables'),
      demoProject(),
      // Not the variables route: arriving there would complete the step.
      `/p/${SLUG}/overview`,
    )

    act(() => {
      vi.advanceTimersByTime(2000)
    })

    expect(missingLine()).toBeNull()
  })

  it('stays quiet while a coach mark for the step is actually mounted', () => {
    renderStripWithMark(SCANS_ROUTE)

    act(() => {
      vi.advanceTimersByTime(2000)
    })

    expect(missingLine()).toBeNull()
  })

  it('Hide hints silences the fallback along with the marks', () => {
    renderStripWithMark(SCANS_ROUTE)

    // Muting unmounts the mark, so presence empties — but the muted scenario
    // must not start warning about a control it was told to stop pointing at.
    fireEvent.click(screen.getByRole('button', { name: 'Hide hints' }))
    act(() => {
      vi.advanceTimersByTime(2000)
    })

    expect(missingLine()).toBeNull()
  })

  it('Show hints puts the marks back without restarting the chapter', () => {
    // "Hide hints" is the coach card's only control, and it used to be a
    // one-way door: nothing turned the marks back on short of a reload.
    renderStripWithMark(SCANS_ROUTE)

    fireEvent.click(screen.getByRole('button', { name: 'Hide hints' }))
    expect(screen.queryByRole('button', { name: 'Hide hints' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Show hints' }))

    expect(screen.getByRole('button', { name: 'Hide hints' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Show hints' })).toBeNull()
    // Un-muting is not a restart: the chapter is still exactly where it was.
    expect(readScenarioState(SLUG).chapters['live-loop']?.step).toBe('live-loop/run-scan')
  })
})

describe('DemoScenarioStrip — projects with no scenario', () => {
  it('renders nothing for a project that is not a demo', () => {
    renderStrip(liveLoopState('live-loop/run-scan'), demoProject({ is_demo: false }))

    expect(strip()).toBeNull()
  })

  it('renders nothing for a demo that is still seeding', () => {
    renderStrip(
      liveLoopState('live-loop/run-scan'),
      demoProject({ generation_status: 'seeding' }),
    )

    expect(strip()).toBeNull()
  })

  it('renders nothing once every chapter is out of the picture', () => {
    renderStrip(liveLoopState('live-loop/watch-scan', { status: 'dismissed' }))

    expect(strip()).toBeNull()
  })

  it('does not resurrect a completed chapter on a non-demo project', () => {
    renderStrip(
      liveLoopState('live-loop/see-chart', { status: 'completed', metric: metricArtifact() }),
      demoProject({ is_demo: false }),
    )

    expect(strip()).toBeNull()
  })
})

describe('DemoScenarioStrip — hints and the welcome panel (DEMO-12, LIVE-9)', () => {
  /** The strip under a real `/p/:slug/*` route, as it is in the app shell. */
  function renderRoutedStrip(state: ScenarioState, route: string, withMark = false) {
    writeScenarioState(SLUG, state)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[route]}>
          <DemoScenarioProvider project={demoProject()} pollIntervalMs={POLL_MS}>
            <Routes>
              <Route
                path="/p/:slug/*"
                element={
                  <>
                    <DemoScenarioStrip />
                    {withMark && (
                      <ScenarioCoachMark step="live-loop/run-scan">
                        <button type="button">Run scan</button>
                      </ScenarioCoachMark>
                    )}
                  </>
                }
              />
            </Routes>
          </DemoScenarioProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    )
  }

  it('offers "Hide hints" in the strip, in the normal tab order', () => {
    renderRoutedStrip(liveLoopState('live-loop/run-scan'), `/p/${SLUG}/scans`, true)
    expect(screen.getByRole('button', { name: 'Hide hints' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Hide hints on the page' }))

    // The card's own copy is gone with the mark, and the strip offers the way back.
    expect(screen.queryByRole('button', { name: 'Hide hints' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Show hints' })).toBeInTheDocument()
    expect(strip()).not.toBeNull()
  })

  it('offers no "Hide hints" for a step with no on-surface mark', () => {
    renderRoutedStrip(chapterState('variables', 'variables/open-variables'), `/p/${SLUG}/scans`)

    expect(screen.queryByRole('button', { name: 'Hide hints on the page' })).toBeNull()
  })

  it('gives way to the welcome panel on a first visit to the Overview', () => {
    renderRoutedStrip(liveLoopState('live-loop/run-scan'), `/p/${SLUG}/overview`)

    expect(strip()).toBeNull()
  })

  it('stays on the Overview once the welcome panel is put away', () => {
    act(() => {
      setWelcomeDismissed(SLUG, true)
    })
    renderRoutedStrip(liveLoopState('live-loop/run-scan'), `/p/${SLUG}/overview`)

    expect(strip()).not.toBeNull()
  })

  it('stays on the Overview for a live loop the user started or restarted themselves', () => {
    // Same step as a pristine scenario, but chosen: the strip is what coaches it.
    renderRoutedStrip(
      scenarioReducer(initialScenarioState(), { type: 'restartChapter', chapter: 'live-loop' }),
      `/p/${SLUG}/overview`,
    )

    expect(strip()).not.toBeNull()
  })

  it('stays on the Overview once the user has moved past the first step', () => {
    renderRoutedStrip(
      liveLoopState('live-loop/collect-metric'),
      `/p/${SLUG}/overview`,
    )

    expect(strip()).not.toBeNull()
  })

  it('stays everywhere else on a first visit', () => {
    renderRoutedStrip(liveLoopState('live-loop/run-scan'), `/p/${SLUG}/events`)

    expect(strip()).not.toBeNull()
  })
})
