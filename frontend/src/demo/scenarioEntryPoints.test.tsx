/**
 * The two ways into the coached scenario (chapter picker): the welcome panel a
 * fresh demo lands on, and the tour — which shows the surfaces but makes nothing happen on them. Both list every chapter
 * with its status; picking one starts (or resumes) it and navigates to its
 * first surface.
 *
 * Both must be invisible outside a ready demo: they are rendered in unit tests
 * and in real projects, where there is no scenario to start.
 */

import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { scansApi } from '@/api/scans'
import type { MetricDefinitionDetailResponse, Project, ScanJob } from '@/types'
import { DemoScenarioProvider } from './DemoScenarioProvider'
import { DemoWelcomePanel } from './DemoWelcomePanel'
import { ProductTour } from './ProductTour'
import {
  CHAPTER_BLURBS,
  CHAPTER_IDS,
  CHAPTER_TITLES,
  readScenarioState,
  writeScenarioState,
} from './scenarioModel'
import { chapterState, liveLoopState } from './scenarioTestState'
import { setWelcomeDismissed } from './welcomeDismissal'
import { toast } from 'sonner'

vi.mock('sonner', () => ({ toast: vi.fn() }))
import { at } from '@/test/at'

const SLUG = 'acme'

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

function scanJob(): ScanJob {
  return {
    id: 'job-1',
    scan_config_id: 'sc-1',
    status: 'running',
    started_at: null,
    completed_at: null,
    result_summary: null,
    error_message: null,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
  }
}

/** Reports where the entry point sent the user. */
function LocationProbe() {
  const location = useLocation()
  return <span data-testid="path">{location.pathname}</span>
}

function renderWithScenario(ui: ReactNode, project: Project | undefined) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/p/${SLUG}/overview`]}>
        <DemoScenarioProvider project={project} pollIntervalMs={10_000}>
          <Routes>
            <Route
              path="/p/:slug/*"
              element={
                <>
                  {ui}
                  <LocationProbe />
                </>
              }
            />
          </Routes>
        </DemoScenarioProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const path = () => screen.getByTestId('path').textContent

/** The chapter picker rows, scoped so both hosts share the queries. */
const picker = () => at(screen.getAllByRole('list', { name: 'Scenario chapters' }), 0)
const chapterRow = (title: string) =>
  within(picker()).getByRole('button', { name: new RegExp(title) })

beforeEach(() => {
  vi.spyOn(scansApi, 'getJob').mockResolvedValue(scanJob())
  vi.spyOn(metricsCatalogApi, 'get').mockResolvedValue({
    id: 'm-1',
    last_collection_status: 'running',
  } as MetricDefinitionDetailResponse)
})

afterEach(() => {
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe('DemoWelcomePanel — how much of the Overview it occupies', () => {
  it('is one row: no second chapter list, no expander (#251)', () => {
    renderWithScenario(<DemoWelcomePanel project={demoProject()} />, demoProject())

    // Expanded, this panel pushed the Overview's own heading ~500-770px down
    // and listed the same seven chapters the "Tour & chapters" dialog does.
    expect(screen.getByRole('heading', { name: /Welcome to your demo workspace/ })).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Scenario chapters' })).toBeNull()
    expect(screen.queryByRole('button', { name: /Show me around/ })).toBeNull()
    expect(screen.queryByText('Metric building blocks')).toBeNull()
  })

  it('comes back when the dismissal is cleared elsewhere', () => {
    renderWithScenario(<DemoWelcomePanel project={demoProject()} />, demoProject())

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss demo welcome' }))
    expect(screen.queryByRole('heading', { name: /Welcome to your demo workspace/ })).toBeNull()

    // The restore control lives in the demo banner — a different subtree — so
    // the panel has to notice the cleared flag without being remounted.
    act(() => {
      setWelcomeDismissed(SLUG, false)
    })

    expect(screen.getByRole('heading', { name: /Welcome to your demo workspace/ })).toBeInTheDocument()
  })

  it('browses the chapters in the tour, at the step stored since the panel mounted', () => {
    renderWithScenario(<DemoWelcomePanel project={demoProject()} />, demoProject())

    // The banner hosts a second ProductTour on this same page. Stepping that one
    // forward writes the position, and nothing remounts the panel — so a tour
    // held mounted here would still be on the index it captured at first render
    // and its first Next would write that back over the stored step.
    window.localStorage.setItem('tripl-tour:acme', '3')

    fireEvent.click(screen.getByRole('button', { name: /Browse chapters/ }))

    expect(screen.getByText(/^Step 4 of/)).toBeInTheDocument()
    expect(picker()).toBeInTheDocument()
  })

  it('points at the real product, not only at more demo', () => {
    renderWithScenario(<DemoWelcomePanel project={demoProject()} />, demoProject())

    expect(screen.getByRole('link', { name: /Create a real project/ })).toHaveAttribute(
      'href',
      '/workspace',
    )
  })
})

describe('DemoWelcomePanel — dismissing it', () => {
  it('offers Undo and names the way back', () => {
    renderWithScenario(<DemoWelcomePanel project={demoProject()} />, demoProject())

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss demo welcome' }))
    expect(screen.queryByRole('heading', { name: /Welcome to your demo workspace/ })).toBeNull()

    const [message, options] = vi.mocked(toast).mock.calls.at(-1) ?? []
    expect(message).toBe('Demo welcome hidden')
    const { action, description } = options as {
      action: { label: string; onClick: () => void }
      description: string
    }
    expect(description).toContain('Tour & chapters')
    expect(action.label).toBe('Undo')

    act(() => action.onClick())
    expect(screen.getByRole('heading', { name: /Welcome to your demo workspace/ })).toBeInTheDocument()
  })

  it('follows a dismissal made in another tab', () => {
    renderWithScenario(<DemoWelcomePanel project={demoProject()} />, demoProject())

    act(() => {
      window.localStorage.setItem(`tripl-demo-welcome-dismissed:${SLUG}`, '1')
      window.dispatchEvent(
        new StorageEvent('storage', { key: `tripl-demo-welcome-dismissed:${SLUG}` }),
      )
    })

    expect(screen.queryByRole('heading', { name: /Welcome to your demo workspace/ })).toBeNull()
  })

  it('does not repeat the banner\'s "Local synthetic data" badge', () => {
    renderWithScenario(<DemoWelcomePanel project={demoProject()} />, demoProject())

    expect(screen.queryByText('Local synthetic data')).toBeNull()
  })
})

describe('DemoWelcomePanel — the one way in (#251)', () => {
  it('starts the first chapter and lands the user on its first surface', () => {
    renderWithScenario(<DemoWelcomePanel project={demoProject()} />, demoProject())

    fireEvent.click(
      screen.getByRole('button', { name: `Start: ${CHAPTER_TITLES['live-loop']}` }),
    )

    expect(path()).toBe(`/p/${SLUG}/scans`)
    expect(readScenarioState(SLUG).activeChapter).toBe('live-loop')
  })

  it('offers the next unfinished chapter once one is done', () => {
    writeScenarioState(SLUG, liveLoopState('live-loop/see-chart', { status: 'completed' }))
    renderWithScenario(<DemoWelcomePanel project={demoProject()} />, demoProject())

    expect(
      screen.getByRole('button', { name: `Start: ${CHAPTER_TITLES['edit-event']}` }),
    ).toBeInTheDocument()
  })

  it('continues the chapter the user is in, where it left off', () => {
    // `engaged`: the user picked it themselves, unlike the live loop a fresh
    // demo starts with.
    writeScenarioState(SLUG, {
      ...chapterState('branches', 'branches/review-diff', 'active'),
      engaged: true,
    })
    renderWithScenario(<DemoWelcomePanel project={demoProject()} />, demoProject())

    fireEvent.click(
      screen.getByRole('button', { name: `Continue: ${CHAPTER_TITLES.branches}` }),
    )

    expect(readScenarioState(SLUG).chapters.branches).toMatchObject({
      status: 'active',
      step: 'branches/review-diff',
    })
  })

  it('offers no chapter when there is no scenario', () => {
    renderWithScenario(
      <DemoWelcomePanel project={demoProject()} />,
      demoProject({ is_demo: false }),
    )

    expect(screen.queryByRole('button', { name: /^Start:/ })).toBeNull()
    // The tour is still the way in.
    expect(screen.getByRole('button', { name: /Take the tour/ })).toBeInTheDocument()
  })
})

describe('ChapterPicker — status', () => {
  it('lists every chapter in order', () => {
    renderWithScenario(<ProductTour slug={SLUG} open onOpenChange={() => {}} />, demoProject())

    expect(picker()).toHaveClass('min-w-0')
    const rows = within(picker()).getAllByRole('button')
    expect(rows).toHaveLength(CHAPTER_IDS.length)
    for (const row of rows) expect(row).toHaveClass('min-w-0')
    expect(rows[0]).toHaveTextContent(CHAPTER_TITLES['live-loop'])
    expect(rows[rows.length - 1]).toHaveTextContent(CHAPTER_TITLES.explore)
  })

  it('marks what is done and leaves a chapter not started plain (#251)', () => {
    writeScenarioState(SLUG, liveLoopState('live-loop/see-chart', { status: 'completed' }))
    renderWithScenario(<ProductTour slug={SLUG} open onOpenChange={() => {}} />, demoProject())

    expect(chapterRow(CHAPTER_TITLES['live-loop'])).toHaveTextContent('Completed')
    expect(chapterRow(CHAPTER_TITLES['edit-event'])).not.toHaveTextContent('Not started')
    expect(chapterRow(CHAPTER_TITLES['edit-event'])).toHaveTextContent(CHAPTER_BLURBS['edit-event'])
  })
})

describe('ProductTour — handing off to a chapter', () => {
  it('closes the dialog, starts the picked chapter and opens its first surface', () => {
    const onOpenChange = vi.fn()
    renderWithScenario(<ProductTour slug={SLUG} open onOpenChange={onOpenChange} />, demoProject())

    fireEvent.click(chapterRow(CHAPTER_TITLES.reconcile))

    expect(onOpenChange).toHaveBeenCalledWith(false)
    expect(path()).toBe(`/p/${SLUG}/reconciliation`)
    expect(readScenarioState(SLUG).activeChapter).toBe('reconcile')
  })

  it('does not offer the hand-off outside a demo', () => {
    renderWithScenario(
      <ProductTour slug={SLUG} open onOpenChange={() => {}} />,
      demoProject({ is_demo: false }),
    )

    expect(screen.queryByRole('list', { name: 'Scenario chapters' })).toBeNull()
    // The tour itself is unchanged — the stepper still works.
    expect(screen.getByRole('button', { name: /Next/ })).toBeInTheDocument()
  })
})
