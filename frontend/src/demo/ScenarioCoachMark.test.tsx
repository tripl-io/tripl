import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useEffect, type Ref } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { scansApi } from '@/api/scans'
import type { MetricDefinitionDetailResponse, Project, ScanJob } from '@/types'
import { DemoScenarioProvider } from './DemoScenarioProvider'
import { useDemoScenario, useDemoScenarioActions } from './demoScenarioContext'
import { ScenarioCoachMark } from './ScenarioCoachMark'
import { buildChapterSteps, initialScenarioState, writeScenarioState } from './scenarioModel'
import { chapterState, liveLoopState } from './scenarioTestState'
import { at } from '@/test/at'

const SLUG = 'acme'
const POLL_MS = 10

const STEPS = buildChapterSteps(SLUG, 'live-loop', initialScenarioState())
const RUN_SCAN_INSTRUCTION = STEPS[0].instruction
const COLLECT_INSTRUCTION = at(STEPS, 2).instruction

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

const collectMetricState = () => liveLoopState('live-loop/collect-metric')

/** Mirrors the provider's live state so a mute can be told apart from a dismiss. */
function Probe() {
  const { active, hintsMuted } = useDemoScenario()
  return (
    <div>
      <span data-testid="active">{String(active)}</span>
      <span data-testid="muted">{String(hintsMuted)}</span>
    </div>
  )
}

function renderMark(ui: React.ReactElement, project: Project | undefined = demoProject()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // A `wrapper` (rather than wrapping `ui` inline) so `rerender` keeps the
  // providers: the scroll tests toggle the mark's props across rerenders.
  return render(ui, {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/p/${SLUG}/scans`]}>
          <DemoScenarioProvider project={project} pollIntervalMs={POLL_MS}>
            {children}
            <Probe />
          </DemoScenarioProvider>
        </MemoryRouter>
      </QueryClientProvider>
    ),
  })
}

const runButton = () => screen.getByRole('button', { name: 'Run scan' })
const callout = () => document.querySelector('[data-slot="popover-content"]')
const ring = () => document.querySelector('.coach-ring')

/** jsdom lays nothing out, so anchor geometry is stubbed per test. */
function stubAnchorRect(rect: { top: number; left: number; width: number; height: number }) {
  const domRect = {
    ...rect,
    right: rect.left + rect.width,
    bottom: rect.top + rect.height,
    x: rect.left,
    y: rect.top,
    toJSON: () => ({}),
  } as DOMRect
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue(domRect)
}

const IN_VIEWPORT_RECT = { top: 100, left: 100, width: 120, height: 30 }
const BELOW_FOLD_RECT = { top: 5000, left: 100, width: 120, height: 30 }

beforeEach(() => {
  vi.spyOn(scansApi, 'getJob').mockResolvedValue(scanJob('running'))
  vi.spyOn(metricsCatalogApi, 'get').mockResolvedValue(metricDefinition('running'))
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  window.localStorage.clear()
})

describe('ScenarioCoachMark — when it stays out of the way', () => {
  it('renders children untouched and mounts no popover when the step is not the active one', () => {
    writeScenarioState(SLUG, collectMetricState())
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(runButton()).toBeInTheDocument()
    expect(callout()).toBeNull()
    expect(screen.queryByText(RUN_SCAN_INSTRUCTION)).not.toBeInTheDocument()
  })

  it('mounts no popover for a project that is not a demo', () => {
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
      demoProject({ is_demo: false }),
    )

    expect(runButton()).toBeInTheDocument()
    expect(callout()).toBeNull()
  })

  it('is suppressed by when={false} even on the active step', () => {
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan" when={false}>
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(runButton()).toBeInTheDocument()
    expect(callout()).toBeNull()
    expect(screen.queryByText(RUN_SCAN_INSTRUCTION)).not.toBeInTheDocument()
  })
})

describe('ScenarioCoachMark — on the active step', () => {
  it('anchors a callout carrying the step instruction and its place in the chain', () => {
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(screen.getByText(RUN_SCAN_INSTRUCTION)).toBeInTheDocument()
    expect(screen.getByText(`Step 1 of ${STEPS.length}`)).toBeInTheDocument()
    expect(runButton()).toBeInTheDocument()
  })

  it('uses an opaque elevated surface so nearby page text cannot bleed through', () => {
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(callout()?.getAttribute('style')).toContain('background: var(--bg-elevated)')
    expect(callout()?.getAttribute('style')).toContain('border-color: var(--accent)')
    expect(callout()?.querySelector('svg')?.getAttribute('style')).toContain(
      'fill: var(--bg-elevated)',
    )
  })

  it('counts a later step from the scenario chain rather than a fixed length', () => {
    writeScenarioState(SLUG, collectMetricState())
    renderMark(
      <ScenarioCoachMark step="live-loop/collect-metric">
        <button type="button">Collect now</button>
      </ScenarioCoachMark>,
    )

    expect(screen.getByText(COLLECT_INSTRUCTION)).toBeInTheDocument()
    expect(screen.getByText(`Step 3 of ${STEPS.length}`)).toBeInTheDocument()
  })

  it('never takes focus from the action it points at', () => {
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    const content = callout()
    expect(content).not.toBeNull()
    // Opening must not move focus into the hint, nor scope it there.
    expect(content?.contains(document.activeElement)).toBe(false)

    runButton().focus()
    expect(document.activeElement).toBe(runButton())
  })
})

describe('ScenarioCoachMark — emphasizing the click target', () => {
  it('stamps the anchor with data-coach-target while visible', () => {
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(runButton()).toHaveAttribute('data-coach-target', 'live-loop/run-scan')
  })

  it('leaves the anchor unstamped when the step is not the active one', () => {
    writeScenarioState(SLUG, collectMetricState())
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(runButton()).not.toHaveAttribute('data-coach-target')
  })

  it('draws the beacon ring while the mark is visible and the anchor has layout', () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(ring()).not.toBeNull()
    expect(ring()).toHaveAttribute('aria-hidden')
  })

  it('draws no ring when the mark is not visible', () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    writeScenarioState(SLUG, collectMetricState())
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(ring()).toBeNull()
  })

  it('draws no ring for an anchor with no layout (0x0 rect)', () => {
    // jsdom's default rect is 0x0 — exactly the not-laid-out case.
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(callout()).not.toBeNull()
    expect(ring()).toBeNull()
  })

  it('does not scroll towards an anchor with no layout (DEMO-10)', () => {
    // jsdom's default rect is 0x0: nothing sensible to scroll to.
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(scrollSpy).not.toHaveBeenCalled()
  })

  it('stands down for an anchor that is mounted but not rendered (DEMO-10)', () => {
    // A hidden tab panel keeps its controls mounted with no box; the card used
    // to open pinned to the page corner, pointing at nothing.
    stubAnchorRect(IN_VIEWPORT_RECT)
    const original = Element.prototype.checkVisibility
    Element.prototype.checkVisibility = function checkVisibility() {
      return false
    }
    try {
      renderMark(
        <ScenarioCoachMark step="live-loop/run-scan">
          <button type="button">Run scan</button>
        </ScenarioCoachMark>,
      )

      expect(runButton()).toBeInTheDocument()
      expect(callout()).toBeNull()
      expect(ring()).toBeNull()
      expect(runButton()).not.toHaveAttribute('data-coach-target')
    } finally {
      // jsdom has no checkVisibility of its own; leave none behind.
      if (original) Element.prototype.checkVisibility = original
      else delete (Element.prototype as { checkVisibility?: unknown }).checkVisibility
    }
  })

  it('follows the anchor as it is hidden and shown again, measured after each commit (DEMO-10)', () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    const original = Element.prototype.checkVisibility
    // Answers from the DOM, as the browser does — so a measure taken during
    // render sees the attribute the PREVIOUS commit left.
    Element.prototype.checkVisibility = function checkVisibility(this: Element) {
      return this.closest('[hidden]') === null
    }
    const section = (collapsed: boolean) => (
      <div hidden={collapsed}>
        <ScenarioCoachMark step="live-loop/run-scan">
          <button type="button">Run scan</button>
        </ScenarioCoachMark>
      </div>
    )
    // By text, not role: a button inside a hidden section has no role to find.
    const anchor = () => screen.getByText('Run scan')
    try {
      const view = renderMark(section(false))
      expect(callout()).not.toBeNull()

      // The render that collapses the section still sees it laid out.
      view.rerender(section(true))
      expect(callout()).toBeNull()
      expect(anchor()).not.toHaveAttribute('data-coach-target')

      // And the render that reveals it again still saw it hidden.
      view.rerender(section(false))
      expect(callout()).not.toBeNull()
      expect(anchor()).toHaveAttribute('data-coach-target', 'live-loop/run-scan')
    } finally {
      if (original) Element.prototype.checkVisibility = original
      else delete (Element.prototype as { checkVisibility?: unknown }).checkVisibility
    }
  })

  it('Hide hints removes the ring through the same gate as the card', () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )
    expect(ring()).not.toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Hide hints' }))

    expect(ring()).toBeNull()
    expect(callout()).toBeNull()
  })
})

describe('ScenarioCoachMark — scrolling an off-screen anchor into view', () => {
  it('scrolls the anchor into view once when it sits fully outside the viewport', () => {
    stubAnchorRect(BELOW_FOLD_RECT)
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})

    const view = renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(scrollSpy).toHaveBeenCalledTimes(1)
    expect(scrollSpy).toHaveBeenCalledWith({ behavior: 'smooth', block: 'center', inline: 'nearest' })

    // Toggling the mark off and back on must not scroll again: once per step.
    view.rerender(
      <ScenarioCoachMark step="live-loop/run-scan" when={false}>
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )
    view.rerender(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(scrollSpy).toHaveBeenCalledTimes(1)
  })

  it('does not scroll when the anchor is already inside the viewport', () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})

    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(scrollSpy).not.toHaveBeenCalled()
  })

  it('uses an instant scroll when the user prefers reduced motion', () => {
    stubAnchorRect(BELOW_FOLD_RECT)
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockReturnValue({ matches: true, media: '(prefers-reduced-motion: reduce)' }),
    )

    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(scrollSpy).toHaveBeenCalledTimes(1)
    expect(scrollSpy).toHaveBeenCalledWith({ behavior: 'auto', block: 'center', inline: 'nearest' })
  })
})

describe('ScenarioCoachMark — hiding the hints', () => {
  it('mutes every mark for the session while the scenario keeps running', () => {
    renderMark(
      <>
        <ScenarioCoachMark step="live-loop/run-scan">
          <button type="button">Run scan</button>
        </ScenarioCoachMark>
        <ScenarioCoachMark step="live-loop/run-scan" side="top">
          <button type="button">Run scan again</button>
        </ScenarioCoachMark>
      </>,
    )

    expect(screen.getAllByText(RUN_SCAN_INSTRUCTION)).toHaveLength(2)

    fireEvent.click(at(screen.getAllByRole('button', { name: 'Hide hints' }), 0))

    // Both marks go quiet — the mute is scenario state, not per-mark state.
    expect(screen.queryByText(RUN_SCAN_INSTRUCTION)).not.toBeInTheDocument()
    expect(callout()).toBeNull()
    expect(runButton()).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run scan again' })).toBeInTheDocument()

    // Muted, not dismissed: the strip carries on coaching.
    expect(screen.getByTestId('muted').textContent).toBe('true')
    expect(screen.getByTestId('active').textContent).toBe('true')
  })
})

describe('ScenarioCoachMark — a row control has no free side', () => {
  it('docks the card clear of the grid instead of opening over the rows it explains', () => {
    // Anchored to a row action, every side Radix can pick lands on the table
    // body: it only flips to avoid the VIEWPORT edge, not the content beneath.
    renderMark(
      <table>
        <tbody>
          <tr>
            <td>Trial Started</td>
            <td>
              <ScenarioCoachMark step="live-loop/run-scan">
                <button type="button">Run scan</button>
              </ScenarioCoachMark>
            </td>
          </tr>
        </tbody>
      </table>,
    )

    expect(screen.getByText(RUN_SCAN_INSTRUCTION)).toBeInTheDocument()
    // No popover over the rows…
    expect(callout()).toBeNull()
    // …the card is docked, and the ring still points at the control.
    const docked = document.querySelector('[data-coach-docked="true"]')
    expect(docked).not.toBeNull()
    expect(docked?.className).toContain('fixed')
    expect(runButton()).toHaveAttribute('data-coach-target', 'live-loop/run-scan')
  })

  it('does not inherit the cell’s right-align, and clears the tweaks FAB', () => {
    // A position:fixed card still inherits text-align, and row actions sit in a
    // `text-right` <td>: the card rendered ragged-left with "Hide hints" pushed
    // under the tweaks FAB (then fixed bottom-5 right-5, h-9, same z-index and
    // later in the DOM), which then won clicks aimed at the button.
    renderMark(
      <table>
        <tbody>
          <tr>
            <td className="text-right">
              <ScenarioCoachMark step="live-loop/run-scan">
                <button type="button">Run scan</button>
              </ScenarioCoachMark>
            </td>
          </tr>
        </tbody>
      </table>,
    )

    const docked = document.querySelector('[data-coach-docked="true"]')
    expect(docked?.className).toContain('text-left')
    // Above the FAB's top edge — now bottom-1 + h-8 = 36px, since tripl-tvqk
    // tucked it into the activity rail's footer strip — not level with it.
    expect(docked?.className).toContain('bottom-[68px]')
    expect(docked?.className).not.toContain('bottom-4')
  })

  it('still opens as a normal popover when the anchor is not inside a table', () => {
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(callout()).not.toBeNull()
    expect(document.querySelector('[data-coach-docked="true"]')).toBeNull()
  })
})

const rowMark = (
  <table>
    <tbody>
      <tr>
        <td>
          <ScenarioCoachMark step="live-loop/run-scan">
            <button type="button">Run scan</button>
          </ScenarioCoachMark>
        </td>
      </tr>
    </tbody>
  </table>
)

describe('ScenarioCoachMark — the anchor is never remounted (DEMO-2)', () => {
  it('mounts a docked anchor once, so focus and local state survive docking', () => {
    let mounts = 0
    // React 19 passes `ref` as a plain prop, so the mark's clone reaches the button.
    function CountingButton({ ref }: { ref?: Ref<HTMLButtonElement> }) {
      useEffect(() => {
        mounts += 1
      }, [])
      return (
        <button type="button" ref={ref}>
          Run scan
        </button>
      )
    }

    renderMark(
      <table>
        <tbody>
          <tr>
            <td>
              <ScenarioCoachMark step="live-loop/run-scan">
                <CountingButton />
              </ScenarioCoachMark>
            </td>
          </tr>
        </tbody>
      </table>,
    )

    expect(document.querySelector('[data-coach-docked="true"]')).not.toBeNull()
    expect(mounts).toBe(1)
  })

  it('keeps the coached control mounted and focused when activating it completes the step', () => {
    writeScenarioState(SLUG, chapterState('branches', 'branches/review-diff'))
    let mounts = 0
    function CompletingButton({ ref }: { ref?: Ref<HTMLButtonElement> }) {
      const { notifyStepCompleted } = useDemoScenarioActions()
      useEffect(() => {
        mounts += 1
      }, [])
      return (
        <button type="button" ref={ref} onClick={() => notifyStepCompleted('branches/review-diff')}>
          Review diff
        </button>
      )
    }

    renderMark(
      <ScenarioCoachMark step="branches/review-diff">
        <CompletingButton />
      </ScenarioCoachMark>,
    )
    const button = screen.getByRole('button', { name: 'Review diff' })
    expect(callout()).not.toBeNull()
    button.focus()

    fireEvent.click(button)

    // The step moved on, so the mark stopped coaching — without swapping the
    // tree around the control (the old bare-children return remounted it).
    expect(callout()).toBeNull()
    expect(button).not.toHaveAttribute('data-coach-target')
    expect(screen.getByRole('button', { name: 'Review diff' })).toBe(button)
    expect(mounts).toBe(1)
    expect(document.activeElement).toBe(button)
  })

  it('keeps the coached control mounted and focused when the hints are muted', () => {
    let mounts = 0
    function CountingButton({ ref }: { ref?: Ref<HTMLButtonElement> }) {
      useEffect(() => {
        mounts += 1
      }, [])
      return (
        <button type="button" ref={ref}>
          Run scan
        </button>
      )
    }

    renderMark(
      <table>
        <tbody>
          <tr>
            <td>
              <ScenarioCoachMark step="live-loop/run-scan">
                <CountingButton />
              </ScenarioCoachMark>
            </td>
          </tr>
        </tbody>
      </table>,
    )
    const button = runButton()
    button.focus()

    fireEvent.click(screen.getByRole('button', { name: 'Hide hints' }))

    expect(document.querySelector('[data-coach-docked="true"]')).toBeNull()
    expect(runButton()).toBe(button)
    expect(mounts).toBe(1)
    expect(document.activeElement).toBe(button)
  })

  it('keeps focus on the anchor when it is hidden and shown again', () => {
    const original = Element.prototype.checkVisibility
    Element.prototype.checkVisibility = function checkVisibility(this: Element) {
      return this.closest('[hidden]') === null
    }
    const section = (collapsed: boolean) => (
      <div hidden={collapsed}>
        <ScenarioCoachMark step="live-loop/run-scan">
          <button type="button">Run scan</button>
        </ScenarioCoachMark>
      </div>
    )
    try {
      const view = renderMark(section(false))
      const button = runButton()
      view.rerender(section(true))
      view.rerender(section(false))
      // The same node: the tree around it did not change between placements.
      expect(runButton()).toBe(button)
    } finally {
      if (original) Element.prototype.checkVisibility = original
      else delete (Element.prototype as { checkVisibility?: unknown }).checkVisibility
    }
  })
})

describe('ScenarioCoachMark — the docked card (DEMO-1, DEMO-13 / LIVE-13)', () => {
  it('is portalled to <body>, never left as a <div> inside <tbody>', () => {
    renderMark(rowMark)

    const docked = document.querySelector('[data-coach-docked="true"]')
    expect(docked?.parentElement).toBe(document.body)
    expect(document.querySelector('tbody div')).toBeNull()
  })

  it('docks at the top when its anchor is in the lower half of the viewport', () => {
    stubAnchorRect({ top: window.innerHeight - 60, left: 100, width: 120, height: 30 })
    renderMark(rowMark)

    const docked = document.querySelector('[data-coach-docked="true"]')
    expect(docked).toHaveAttribute('data-coach-edge', 'top')
  })

  it('docks at the bottom when its anchor is in the upper half', () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    renderMark(rowMark)

    const docked = document.querySelector('[data-coach-docked="true"]')
    expect(docked).toHaveAttribute('data-coach-edge', 'bottom')
  })

  // LIVE-13: a control at the left of a table got its card at the far right.
  it('sits on the side of the screen its anchor is on', () => {
    stubAnchorRect({ top: 100, left: 20, width: 120, height: 30 })
    const left = renderMark(rowMark)
    expect(document.querySelector('[data-coach-docked="true"]')).toHaveAttribute(
      'data-coach-side',
      'left',
    )
    left.unmount()

    stubAnchorRect({ top: 100, left: window.innerWidth - 140, width: 120, height: 30 })
    renderMark(rowMark)
    expect(document.querySelector('[data-coach-docked="true"]')).toHaveAttribute(
      'data-coach-side',
      'right',
    )
  })

  it('docks under the demo banner, not on its controls (#251 SH-5)', () => {
    // The card used to sit at a fixed top-14, exactly over the banner's hide
    // hints / dismiss / tour / Reset / Delete: the controls that put it away.
    const banner = document.createElement('div')
    banner.setAttribute('data-demo-banner', '')
    document.body.appendChild(banner)
    const box = (top: number, height: number) =>
      ({
        top,
        left: 0,
        width: 100,
        height,
        right: 100,
        bottom: top + height,
        x: 0,
        y: top,
        toJSON: () => ({}),
      }) as DOMRect
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (
      this: Element,
    ) {
      // The banner under the top bar; the row near the bottom of the screen.
      return this === banner ? box(56, 46) : box(window.innerHeight - 60, 30)
    })
    try {
      renderMark(rowMark)

      const docked = document.querySelector<HTMLElement>('[data-coach-docked="true"]')
      expect(docked).toHaveAttribute('data-coach-edge', 'top')
      // Banner bottom (102) plus the 8px gap.
      expect(docked?.style.top).toBe('110px')
    } finally {
      banner.remove()
    }
  })

  it('docks every mark on a phone, clear of the page title (#251 DA-45)', () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    vi.stubGlobal(
      'matchMedia',
      vi.fn((query: string) => ({
        matches: query === '(max-width: 639px)',
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
      })),
    )

    // A plain button, not a table row: from `sm` up it gets the anchored card.
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(callout()).toBeNull()
    const docked = document.querySelector('[data-coach-docked="true"]')
    // The anchor is in the upper half, near the title: the card takes the bottom.
    expect(docked).toHaveAttribute('data-coach-edge', 'bottom')
  })

  it('stays a bottom sheet on a phone when the anchor is low on the screen (#251 DA-45)', () => {
    // Docked at the top, the full-width card sat on the page title and its
    // Overview / Configuration tabs whenever the row's Run button was low.
    stubAnchorRect({ top: window.innerHeight - 60, left: 100, width: 120, height: 30 })
    vi.stubGlobal(
      'matchMedia',
      vi.fn((query: string) => ({
        matches: query === '(max-width: 639px)',
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
      })),
    )

    renderMark(rowMark)

    const docked = document.querySelector<HTMLElement>('[data-coach-docked="true"]')
    expect(docked).toHaveAttribute('data-coach-edge', 'bottom')
    expect(docked?.style.top).toBe('')
  })

  it('follows the banner when it grows or lands late (#251 SH-5)', async () => {
    // Opening the phone pill, the strip's chunk landing and a failure line all
    // move the banner's bottom edge without a resize or a scroll; on a hard
    // load the banner is not there at all when the card mounts.
    const resizeCallbacks: Array<() => void> = []
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(callback: () => void) {
          resizeCallbacks.push(callback)
        }
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    )
    let bannerHeight = 46
    const banner = document.createElement('div')
    banner.setAttribute('data-demo-banner', '')
    const box = (top: number, height: number) =>
      ({
        top,
        left: 0,
        width: 100,
        height,
        right: 100,
        bottom: top + height,
        x: 0,
        y: top,
        toJSON: () => ({}),
      }) as DOMRect
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (
      this: Element,
    ) {
      return this === banner ? box(56, bannerHeight) : box(window.innerHeight - 60, 30)
    })
    try {
      renderMark(rowMark)
      const docked = () => document.querySelector<HTMLElement>('[data-coach-docked="true"]')
      // No banner yet: just under the top bar.
      expect(docked()?.style.top).toBe('56px')

      // The banner lands: the card moves under it.
      document.body.appendChild(banner)
      await waitFor(() => expect(docked()?.style.top).toBe('110px'))

      // It grows (the pill opens): the card follows.
      bannerHeight = 120
      act(() => {
        for (const callback of resizeCallbacks) callback()
      })
      await waitFor(() => expect(docked()?.style.top).toBe('184px'))
    } finally {
      banner.remove()
    }
  })

  it('collapses to its step line, so it never has to cover a tap target for good', () => {
    renderMark(rowMark)

    const collapse = screen.getByRole('button', { name: 'Collapse demo hint' })
    expect(collapse).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(collapse)

    const expand = screen.getByRole('button', { name: 'Expand demo hint' })
    expect(expand).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByText(`Step 1 of ${STEPS.length}`)).toBeInTheDocument()
    // Still in the tree, so the anchor's description keeps resolving.
    expect(screen.getByText(RUN_SCAN_INSTRUCTION)).toBeInTheDocument()
    // Collapsing is not muting: the scenario and the hints carry on.
    expect(screen.getByTestId('muted').textContent).toBe('false')
  })
})

describe('ScenarioCoachMark — tied to its control (DEMO-12)', () => {
  it('describes the anchor with the step instruction', () => {
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(runButton()).toHaveAccessibleDescription(RUN_SCAN_INSTRUCTION)
  })

  it('keeps a description the anchor already had', () => {
    renderMark(
      <>
        <p id="own-hint">Runs against the demo warehouse.</p>
        <ScenarioCoachMark step="live-loop/run-scan">
          <button type="button" aria-describedby="own-hint">
            Run scan
          </button>
        </ScenarioCoachMark>
      </>,
    )

    const describedBy = runButton().getAttribute('aria-describedby') ?? ''
    expect(describedBy.split(' ')).toContain('own-hint')
    expect(runButton()).toHaveAccessibleDescription(
      `Runs against the demo warehouse. ${RUN_SCAN_INSTRUCTION}`,
    )
  })

  it('describes the control inside a wrapper anchor that nobody tabs to', () => {
    // EventsHeader's drift mark wraps the badge's trigger button in a span.
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <span className="inline-flex">
          <button type="button">Run scan</button>
        </span>
      </ScenarioCoachMark>,
    )

    expect(runButton()).toHaveAccessibleDescription(RUN_SCAN_INSTRUCTION)

    fireEvent.click(screen.getByRole('button', { name: 'Hide hints' }))

    expect(runButton()).not.toHaveAttribute('aria-describedby')
  })

  it('names the hint as a note', () => {
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(screen.getByRole('note', { name: 'Demo hint' })).toBeInTheDocument()
  })

  it('drops the description when the mark goes quiet', () => {
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Hide hints' }))

    expect(runButton()).not.toHaveAttribute('aria-describedby')
  })
})

describe('ScenarioCoachMark — clipped by its scroll container (DEMO-11)', () => {
  function clippedMark() {
    return (
      <div data-testid="scroller" style={{ overflow: 'auto' }}>
        <ScenarioCoachMark step="live-loop/run-scan">
          <button type="button">Run scan</button>
        </ScenarioCoachMark>
      </div>
    )
  }

  /** The anchor sits inside the window but right of its container's visible box. */
  function stubClippedLayout(anchorRect = { x: 600, y: 100, width: 120, height: 30 }) {
    const scrollerRect = { x: 0, y: 0, width: 300, height: 400 }
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      const init =
        this instanceof HTMLElement && this.dataset.testid === 'scroller' ? scrollerRect : anchorRect
      return {
        ...init,
        top: init.y,
        left: init.x,
        right: init.x + init.width,
        bottom: init.y + init.height,
        toJSON: () => ({}),
      } as DOMRect
    })
  }

  it('draws no ring for an anchor scrolled out of its container', () => {
    stubClippedLayout()
    renderMark(clippedMark())

    expect(callout()).not.toBeNull()
    expect(ring()).toBeNull()
  })

  it('scrolls an anchor that its container clips, even inside the window', () => {
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
    stubClippedLayout()

    renderMark(clippedMark())

    expect(scrollSpy).toHaveBeenCalledTimes(1)
    // Clipped only sideways: the page does not also jump vertically.
    expect(scrollSpy).toHaveBeenCalledWith({ behavior: 'smooth', block: 'nearest', inline: 'nearest' })
  })

  it('does not scroll an anchor wider than its container that is already in view', () => {
    // A table row on a phone, wider than its overflow-x-auto wrapper: it can
    // never fit, and it was centred vertically on every step regardless.
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
    stubClippedLayout({ x: 0, y: 100, width: 800, height: 30 })

    renderMark(clippedMark())

    expect(scrollSpy).not.toHaveBeenCalled()
  })

  it('centres an anchor its container clips vertically', () => {
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
    stubClippedLayout({ x: 50, y: 500, width: 120, height: 30 })

    renderMark(clippedMark())

    expect(scrollSpy).toHaveBeenCalledWith({ behavior: 'smooth', block: 'center', inline: 'nearest' })
  })
})
