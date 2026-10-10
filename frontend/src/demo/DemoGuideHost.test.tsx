import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { scansApi } from '@/api/scans'
import { ActiveProjectContext } from '@/components/active-project-context'
import { AuthContext } from '@/components/auth-context'
import { MAIN_CONTENT_ID, SIDEBAR_ID } from '@/components/landmarks'
import { QUICK_START_URL } from '@/lib/docsSite'
import { authStatusKey } from '@/lib/queryKeys'
import type { MetricDefinitionDetailResponse, Project, ScanJob } from '@/types'
import { at } from '@/test/at'
import { personaAuth, type Persona } from '@/test/persona'
import { DemoGuideHost } from './DemoGuideHost'
import { DemoScenarioProvider } from './DemoScenarioProvider'
import { ScenarioCoachMark } from './ScenarioCoachMark'
import {
  CHAPTER_IDS,
  CHAPTER_TITLES,
  buildChapterSteps,
  initialScenarioState,
  writeScenarioState,
  type ChapterId,
  type ScenarioState,
  type ScenarioStep,
} from './scenarioModel'
import { chapterState, liveLoopState } from './scenarioTestState'
import { setWelcomeDismissed } from './welcomeDismissal'

const SLUG = 'acme'
const POLL_MS = 10_000

/** How long a step goes without a mark before the host speaks. */
const GUIDE_DELAY_MS = 400
/** How long on the step's surface without a mark before it calls the control missing. */
const MISSING_TARGET_DELAY_MS = 1000
/** jsdom's animation frame, under fake timers. */
const FRAME_MS = 16

const MISSING_COPY =
  "The highlighted control isn't visible — it may be filtered out, below the fold, or already handled."
const RESET_COPY = 'Resetting the demo project restores every guided example.'

const SCANS_ROUTE = `/p/${SLUG}/scans`
const EVENTS_ROUTE = `/p/${SLUG}/events`

const stepsOf = (chapter: ChapterId) => buildChapterSteps(SLUG, chapter, initialScenarioState())
const LIVE_LOOP = stepsOf('live-loop')
const RUN_SCAN = LIVE_LOOP[0]
const SET_VALUE = at(stepsOf('edit-event'), 1)
const OPEN_VARIABLES = stepsOf('variables')[0]
const USE_SEARCH = at(stepsOf('explore'), 2)

/** A step's gesture, failing loudly if the model dropped it. */
function cueOf(step: ScenarioStep): string {
  if (!step.cue) throw new Error(`${step.id} has no cue`)
  return step.cue
}

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

const runScanMark = (
  <ScenarioCoachMark step="live-loop/run-scan">
    <button type="button">Run scan</button>
  </ScenarioCoachMark>
)

/** The pencil on the event's row: the way back to the editor's steps. */
const pencilMark = (
  <ScenarioCoachMark step="edit-event/open-editor">
    <button type="button">Edit Trial Started</button>
  </ScenarioCoachMark>
)

interface HostOptions {
  /** A page's coach mark, mounted beside the host. */
  mark?: ReactNode
  /** The shell around the page — the sidebar. */
  shell?: ReactNode
  /** Signed in as this role; with no session at all when absent. */
  persona?: Persona
  /** The instance is a public demo. */
  publicDemo?: boolean
}

/** The host under a real `/p/:slug/*` route, as the app shell mounts it. */
function renderHost(
  state: ScenarioState,
  route: string,
  { mark, shell, persona, publicDemo = false }: HostOptions = {},
) {
  writeScenarioState(SLUG, state)
  // A viewer is an organization member whose row in this demo is `viewer`:
  // the server answers the project to them as read-only.
  const project = demoProject(
    persona === undefined
      ? {}
      : {
          created_by_user_id: 'someone-else',
          ...(persona === 'viewer' ? { my_role: 'viewer' as const, can_mutate: false } : {}),
        },
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (publicDemo) {
    client.setQueryData(authStatusKey(), {
      has_users: true,
      registration_enabled: false,
      public_demo: true,
    })
  }
  const tree = (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <DemoScenarioProvider project={project} pollIntervalMs={POLL_MS}>
          <Routes>
            <Route
              path="/p/:slug/*"
              element={
                <>
                  {shell}
                  {mark}
                  <DemoGuideHost />
                </>
              }
            />
          </Routes>
        </DemoScenarioProvider>
      </MemoryRouter>
    </QueryClientProvider>
  )
  if (persona === undefined) return render(tree)
  return render(
    <AuthContext.Provider value={personaAuth(persona)}>
      <ActiveProjectContext.Provider value={project}>{tree}</ActiveProjectContext.Provider>
    </AuthContext.Provider>,
  )
}

const guide = () => screen.queryByRole('note', { name: 'Demo hint' })
const takeMeThere = () => screen.queryByRole('link', { name: /Take me there/ })
const missingLine = () => screen.queryByText(MISSING_COPY)
const ring = () => document.querySelector('.coach-ring')
const tag = () => document.querySelector('[data-coach-tag]')

function advance(ms: number) {
  act(() => {
    vi.advanceTimersByTime(ms)
  })
}

interface Rect {
  top: number
  left: number
  width: number
  height: number
}

/** jsdom lays nothing out: every element's box by `rectOf`. */
function stubRects(rectOf: (element: Element) => Rect) {
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (
    this: Element,
  ) {
    const rect = rectOf(this)
    return {
      ...rect,
      right: rect.left + rect.width,
      bottom: rect.top + rect.height,
      x: rect.left,
      y: rect.top,
      toJSON: () => ({}),
    } as DOMRect
  })
}

beforeEach(() => {
  // The guide waits out a route change before it speaks; the tests step
  // through that wait.
  vi.useFakeTimers()
  // The watches must stay pending: a settled poll would advance the step out
  // from under the assertion.
  vi.spyOn(scansApi, 'getJob').mockResolvedValue(scanJob('running'))
  vi.spyOn(metricsCatalogApi, 'get').mockResolvedValue(metricDefinition('running'))
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe('DemoGuideHost — a step with no mark on screen', () => {
  it("says what to do after a moment, with the way to the step's page", () => {
    renderHost(liveLoopState('live-loop/run-scan'), EVENTS_ROUTE)

    // Not at once: a route change unmounts one page's mark before the next
    // page mounts its own, and the guide would flicker in between.
    expect(guide()).toBeNull()
    advance(GUIDE_DELAY_MS - 1)
    expect(guide()).toBeNull()
    advance(1)

    const note = screen.getByRole('note', { name: 'Demo hint' })
    expect(within(note).getByText(RUN_SCAN.title)).toBeInTheDocument()
    expect(within(note).getByText(RUN_SCAN.instruction)).toBeInTheDocument()
    expect(note).toHaveTextContent(
      `Step 1 of ${LIVE_LOOP.length} · ${CHAPTER_TITLES['live-loop']}`,
    )
    // Off the step's page, the first gesture is getting there.
    expect(within(note).getByText('Open its page first: click Take me there.')).toBeInTheDocument()
    expect(within(note).getByRole('link', { name: /Take me there/ })).toHaveAttribute(
      'href',
      SCANS_ROUTE,
    )
    // Not the step's page, so nothing is missing from it.
    expect(missingLine()).toBeNull()
  })

  it('rings the sidebar item that leads there', () => {
    stubRects(() => ({ top: 200, left: 16, width: 200, height: 32 }))
    renderHost(liveLoopState('live-loop/run-scan'), EVENTS_ROUTE, {
      shell: (
        <nav id={SIDEBAR_ID}>
          <a href={EVENTS_ROUTE}>Events</a>
          <a href={SCANS_ROUTE}>Scans</a>
        </nav>
      ),
    })

    advance(GUIDE_DELAY_MS)
    // Found a frame after the guide shows, once the route's sidebar is there.
    advance(FRAME_MS)

    const note = screen.getByRole('note', { name: 'Demo hint' })
    expect(
      within(note).getByText('Open its page first — the highlighted sidebar item, or Take me there.'),
    ).toBeInTheDocument()
    expect(ring()).not.toBeNull()
    // Under the item, inside the sidebar: beside it, the tag sat on the page.
    expect(tag()).toHaveAttribute('data-coach-tag', 'bottom')
    expect(tag()).toHaveTextContent('Click here')
  })

  it('scrolls the sidebar to an item below its fold', () => {
    // A long sidebar scrolls, and Scans sat below its fold, ringed where
    // nobody could see it.
    stubRects((element) =>
      element.id === SIDEBAR_ID
        ? { top: 0, left: 0, width: 240, height: 300 }
        : { top: 500, left: 16, width: 200, height: 32 },
    )
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
    renderHost(liveLoopState('live-loop/run-scan'), EVENTS_ROUTE, {
      shell: (
        <nav id={SIDEBAR_ID} style={{ overflow: 'auto' }}>
          <a href={SCANS_ROUTE}>Scans</a>
        </nav>
      ),
    })

    advance(GUIDE_DELAY_MS)
    advance(FRAME_MS)

    expect(scrollSpy).toHaveBeenCalledTimes(1)
    expect(scrollSpy).toHaveBeenCalledWith({ block: 'center' })
  })

  it('sends a visit step to its page, where arriving is the step', () => {
    renderHost(chapterState('variables', 'variables/open-variables'), EVENTS_ROUTE)
    advance(MISSING_TARGET_DELAY_MS * 2)

    const note = screen.getByRole('note', { name: 'Demo hint' })
    // "Click Properties in the sidebar" already is the way there.
    expect(within(note).getByText(cueOf(OPEN_VARIABLES))).toBeInTheDocument()
    expect(takeMeThere()).toHaveAttribute('href', `/p/${SLUG}/variables`)
    // A visit step has no control to go missing.
    expect(missingLine()).toBeNull()
  })

  it('offers no way there for a step done wherever the user is', () => {
    renderHost(chapterState('explore', 'explore/use-search'), EVENTS_ROUTE)
    advance(GUIDE_DELAY_MS)

    const note = screen.getByRole('note', { name: 'Demo hint' })
    expect(within(note).getByText(cueOf(USE_SEARCH))).toBeInTheDocument()
    expect(takeMeThere()).toBeNull()
  })

  it('leaves an untouched first visit to the Overview to the welcome panel', () => {
    renderHost(liveLoopState('live-loop/run-scan'), `/p/${SLUG}/overview`)
    advance(MISSING_TARGET_DELAY_MS * 2)

    expect(guide()).toBeNull()
  })

  it('speaks on the Overview once the welcome panel is put away', () => {
    act(() => {
      setWelcomeDismissed(SLUG, true)
    })
    renderHost(liveLoopState('live-loop/run-scan'), `/p/${SLUG}/overview`)
    advance(GUIDE_DELAY_MS)

    expect(guide()).not.toBeNull()
    expect(takeMeThere()).toHaveAttribute('href', SCANS_ROUTE)
  })

  it('says nothing while no chapter runs', () => {
    renderHost(liveLoopState('live-loop/watch-scan', { status: 'dismissed' }), EVENTS_ROUTE)
    advance(MISSING_TARGET_DELAY_MS * 2)

    expect(guide()).toBeNull()
  })

  it('folds to its face on its own Hide hints, and the face brings them back', () => {
    renderHost(liveLoopState('live-loop/run-scan'), EVENTS_ROUTE)
    advance(GUIDE_DELAY_MS)

    fireEvent.click(screen.getByRole('button', { name: 'Hide hints' }))
    advance(MISSING_TARGET_DELAY_MS * 2)

    // Quiet, but still in its corner: with nothing left on the page, testers
    // scrolled up to the strip to find the hints again.
    expect(guide()).toHaveAttribute('data-guide-mode', 'face')
    expect(screen.queryByRole('button', { name: 'Hide hints' })).toBeNull()
    expect(takeMeThere()).toBeNull()

    fireEvent.click(
      screen.getByRole('button', {
        name: `Show demo hints — step 1 of ${LIVE_LOOP.length}: ${RUN_SCAN.title}`,
      }),
    )
    advance(GUIDE_DELAY_MS)

    expect(guide()).toHaveAttribute('data-guide-mode', 'card')
    expect(takeMeThere()).toHaveAttribute('href', SCANS_ROUTE)
  })

  it("rings the page's own tab when the step lives in another of its sections", () => {
    // On Alerting's Inbox, the rule step's Rules is a tab away: the sidebar
    // has nothing to point at, and "Open its page first" read as a riddle.
    stubRects((element) =>
      element.id === MAIN_CONTENT_ID
        ? { top: 0, left: 0, width: 1024, height: 768 }
        : element.getAttribute('data-tab-value') === 'monitors'
          ? { top: 120, left: 400, width: 80, height: 32 }
          : { top: 120, left: 300, width: 80, height: 32 },
    )
    renderHost(chapterState('alerting', 'alerting/create-rule'), `/p/${SLUG}/alerting`, {
      shell: (
        <main id={MAIN_CONTENT_ID}>
          <div role="tablist" aria-label="Alerting sections">
            <button type="button" role="tab" data-tab-value="inbox">
              Inbox
            </button>
            <button type="button" role="tab" data-tab-value="monitors">
              Rules
            </button>
          </div>
        </main>
      ),
    })

    advance(GUIDE_DELAY_MS)
    // Found a frame after the guide shows.
    advance(FRAME_MS)

    const note = screen.getByRole('note', { name: 'Demo hint' })
    expect(
      within(note).getByText('Open the highlighted tab first, or click Take me there.'),
    ).toBeInTheDocument()
    expect(tag()).toHaveTextContent('Open this tab')
    expect(tag()).toHaveAttribute('data-coach-tag', 'bottom')
    // Around Rules, not the Inbox the user is on.
    expect((ring() as HTMLElement).style.left).toBe('398px')
    expect(takeMeThere()).toHaveAttribute('href', `/p/${SLUG}/alerting?section=monitors`)
  })
})

/** Every chapter landed, the last one still on screen. */
function everyChapterLanded(): ScenarioState {
  const chapters: ScenarioState['chapters'] = {}
  for (const id of CHAPTER_IDS) {
    const steps = stepsOf(id)
    chapters[id] = { status: 'completed', step: at(steps, steps.length - 1).id }
  }
  return { v: 3, activeChapter: 'explore', chapters }
}

describe('DemoGuideHost — a chapter that has landed', () => {
  it('says so, and offers the next chapter where the user is looking', () => {
    // The strip offered it alone, scrolled away above the page.
    renderHost(liveLoopState('live-loop/see-chart', { status: 'completed' }), EVENTS_ROUTE)

    const note = screen.getByRole('note', { name: 'Demo hint' })
    expect(note).toHaveTextContent(`Chapter complete · ${CHAPTER_TITLES['live-loop']}`)
    expect(within(note).getByText(`Next: ${CHAPTER_TITLES['edit-event']}`)).toBeInTheDocument()
    expect(within(note).getByRole('link', { name: /Start the chapter/ })).toHaveAttribute(
      'href',
      EVENTS_ROUTE,
    )
  })

  it('ends a public demo on the quick start, never on a project it cannot make', () => {
    renderHost(everyChapterLanded(), EVENTS_ROUTE, { publicDemo: true })

    const note = screen.getByRole('note', { name: 'Demo hint' })
    expect(within(note).getByRole('link', { name: /Run tripl yourself/ })).toHaveAttribute(
      'href',
      QUICK_START_URL,
    )
    expect(screen.queryByRole('link', { name: /Create a real project/ })).toBeNull()
  })

  it("ends a demo on an instance of one's own in a real project", () => {
    renderHost(everyChapterLanded(), EVENTS_ROUTE)

    const note = screen.getByRole('note', { name: 'Demo hint' })
    expect(within(note).getByRole('link', { name: /Create a real project/ })).toBeInTheDocument()
  })

  it('says nothing once the landed chapter is put away', () => {
    renderHost(liveLoopState('live-loop/see-chart', { status: 'dismissed' }), EVENTS_ROUTE)
    advance(GUIDE_DELAY_MS)

    expect(guide()).toBeNull()
  })
})

describe('DemoGuideHost — standing back for a coach mark', () => {
  it('says nothing while a mark for the step is on screen', () => {
    renderHost(liveLoopState('live-loop/run-scan'), SCANS_ROUTE, { mark: runScanMark })
    advance(MISSING_TARGET_DELAY_MS * 2)

    // One guide: the mark's own, beside its ring.
    expect(screen.getAllByRole('note', { name: 'Demo hint' })).toHaveLength(1)
    expect(takeMeThere()).toBeNull()
    expect(missingLine()).toBeNull()
  })

  it('says nothing while the way back to the step is on screen', () => {
    renderHost(chapterState('edit-event', 'edit-event/set-value'), EVENTS_ROUTE, {
      mark: pencilMark,
    })
    advance(MISSING_TARGET_DELAY_MS * 2)

    const notes = screen.getAllByRole('note', { name: 'Demo hint' })
    expect(notes).toHaveLength(1)
    // The pencil's guide, in the step's own words.
    expect(within(at(notes, 0)).getByText(SET_VALUE.instruction)).toBeInTheDocument()
    expect(missingLine()).toBeNull()
  })
})

describe('DemoGuideHost — when the coached control is nowhere on screen', () => {
  it('says nothing about it at first, then flags the missing control after the grace period', () => {
    renderHost(liveLoopState('live-loop/run-scan'), SCANS_ROUTE)

    // The guide comes first, with the step's own gesture: this is its page.
    advance(GUIDE_DELAY_MS)
    const note = screen.getByRole('note', { name: 'Demo hint' })
    expect(within(note).getByText(cueOf(RUN_SCAN))).toBeInTheDocument()
    expect(missingLine()).toBeNull()

    // Just short of the longer delay: still quiet — a slow page is the
    // likelier reason, and route transitions must not flicker.
    advance(MISSING_TARGET_DELAY_MS - GUIDE_DELAY_MS - 1)
    expect(missingLine()).toBeNull()

    advance(1)
    expect(missingLine()).not.toBeNull()
    // Already on Scans: no "Take me there" that goes nowhere (#251).
    expect(takeMeThere()).toBeNull()
  })

  it('offers the reset only to whoever can reset the demo (#251)', () => {
    renderHost(liveLoopState('live-loop/run-scan'), SCANS_ROUTE, { persona: 'owner' })
    advance(MISSING_TARGET_DELAY_MS)

    expect(screen.getByText(`${MISSING_COPY} ${RESET_COPY}`)).toBeInTheDocument()
  })

  it('tells a viewer the step needs edit access instead of pointing at a hidden control (#251)', () => {
    renderHost(liveLoopState('live-loop/run-scan'), SCANS_ROUTE, { persona: 'viewer' })
    advance(MISSING_TARGET_DELAY_MS)

    expect(missingLine()).toBeNull()
    expect(screen.getByText(/This step needs edit access/)).toBeInTheDocument()
    expect(screen.queryByText(/Resetting the demo project/)).toBeNull()
  })

  it('stays quiet about it away from the step surface, where a mark is not expected', () => {
    renderHost(liveLoopState('live-loop/run-scan'), EVENTS_ROUTE)
    advance(MISSING_TARGET_DELAY_MS * 2)

    expect(guide()).not.toBeNull()
    expect(missingLine()).toBeNull()
    // Off the step's page the link still goes somewhere.
    expect(takeMeThere()).toHaveAttribute('href', SCANS_ROUTE)
  })

  it('stays quiet about it on another tab of the step page, where the link leads to the control', () => {
    renderHost(chapterState('alerting', 'alerting/create-rule'), `/p/${SLUG}/alerting`)
    advance(MISSING_TARGET_DELAY_MS * 2)

    expect(missingLine()).toBeNull()
    expect(takeMeThere()).toHaveAttribute('href', `/p/${SLUG}/alerting?section=monitors`)
  })

  it("stays quiet about it while the step's second gesture is ringed", () => {
    // The rule dialog opened from the empty list's own button: its Create is
    // ringed, with no "Add rule" behind it to carry the step's own mark.
    const createMark = (
      <ScenarioCoachMark step="alerting/create-rule" followUp tag="Name it, then click here">
        <button type="button">Create</button>
      </ScenarioCoachMark>
    )
    renderHost(chapterState('alerting', 'alerting/create-rule'), `/p/${SLUG}/alerting?section=monitors`, {
      mark: createMark,
    })
    advance(MISSING_TARGET_DELAY_MS * 2)

    expect(screen.getByRole('button', { name: 'Create' })).toHaveAttribute(
      'data-coach-target',
      'alerting/create-rule@then',
    )
    expect(missingLine()).toBeNull()
  })

  it('Hide hints silences the fallback along with the marks', () => {
    renderHost(liveLoopState('live-loop/run-scan'), SCANS_ROUTE, { mark: runScanMark })

    // Muting unmounts the mark, so presence empties — but the muted scenario
    // must not start warning about a control it was told to stop pointing at.
    fireEvent.click(screen.getByRole('button', { name: 'Hide hints' }))
    advance(MISSING_TARGET_DELAY_MS * 2)

    // The guide's face alone, the way back to the hints.
    expect(guide()).toHaveAttribute('data-guide-mode', 'face')
    expect(ring()).toBeNull()
    expect(missingLine()).toBeNull()
  })
})
