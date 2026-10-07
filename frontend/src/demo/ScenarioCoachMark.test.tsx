import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useEffect, type Ref } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { scansApi } from '@/api/scans'
import type { MetricDefinitionDetailResponse, Project, ScanJob } from '@/types'
import { DemoScenarioProvider } from './DemoScenarioProvider'
import { useDemoScenario, useDemoScenarioActions } from './demoScenarioContext'
import { ScenarioCoachMark } from './ScenarioCoachMark'
import {
  CHAPTER_TITLES,
  buildChapterSteps,
  initialScenarioState,
  writeScenarioState,
  type ScenarioStep,
} from './scenarioModel'
import { chapterState, liveLoopState } from './scenarioTestState'
import { at } from '@/test/at'

const SLUG = 'acme'
const POLL_MS = 10

const STEPS = buildChapterSteps(SLUG, 'live-loop', initialScenarioState())
const RUN_SCAN = STEPS[0]
const COLLECT = at(STEPS, 2)
const RUN_SCAN_INSTRUCTION = RUN_SCAN.instruction
const COLLECT_INSTRUCTION = COLLECT.instruction

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
/** The demo guide: portalled to <body>, in a corner of the page. */
const guide = () => document.querySelector<HTMLElement>('[data-demo-guide]')
const ring = () => document.querySelector('.coach-ring')
/** The tag on the control itself, naming the gesture ("Click here"). */
const tag = () => document.querySelector('[data-coach-tag]')
const faceName = `Show the demo guide — step 1 of ${STEPS.length}: ${RUN_SCAN.title}`

/**
 * One animation frame, inside act: the scroll to an off-screen anchor waits a
 * frame, and the guide and the ring re-measure on one.
 */
async function nextFrame() {
  await act(async () => {
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
  })
}

interface Rect {
  top: number
  left: number
  width: number
  height: number
}

function domRect(rect: Rect): DOMRect {
  return {
    ...rect,
    right: rect.left + rect.width,
    bottom: rect.top + rect.height,
    x: rect.left,
    y: rect.top,
    toJSON: () => ({}),
  } as DOMRect
}

/** jsdom lays nothing out, so anchor geometry is stubbed per test. */
function stubAnchorRect(rect: Rect) {
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue(domRect(rect))
}

/** Every element's box by `rectOf`, for a test that needs more than the anchor. */
function stubRects(rectOf: (element: Element) => Rect) {
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (
    this: Element,
  ) {
    return domRect(rectOf(this))
  })
}

/** A phone: below `sm`, where the guide spans the screen. */
function stubPhone() {
  vi.stubGlobal(
    'matchMedia',
    vi.fn((query: string) => ({
      matches: query === '(max-width: 639px)',
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    })),
  )
}

/**
 * The open guide's height by the width it is given, as a browser lays it out:
 * squeezed to the face's 56px, the card wraps a word a line and stands taller
 * than the screen. jsdom lays nothing out, so every offsetHeight is 0.
 */
function stubGuideHeight() {
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (
    this: HTMLElement,
  ) {
    if (!this.hasAttribute('data-demo-guide')) return 0
    return Number.parseFloat(this.style.width) < 100 ? 2 * window.innerHeight : 180
  })
}

/** Something drawn over the middle of every control: a dialog, a menu. */
function stubCoveringLayer(): () => void {
  const layer = document.createElement('div')
  document.body.appendChild(layer)
  // jsdom has no hit-testing of its own; the beacon asks this one.
  Object.defineProperty(document, 'elementFromPoint', {
    configurable: true,
    value: () => layer,
  })
  return () => {
    delete (document as { elementFromPoint?: unknown }).elementFromPoint
    layer.remove()
  }
}

// jsdom's viewport is 1024×768: the guide's corners sit 16px in from the
// content column's edges, the top ones under the 56px top bar.
const IN_VIEWPORT_RECT = { top: 100, left: 100, width: 120, height: 30 }
const BELOW_FOLD_RECT = { top: 5000, left: 100, width: 120, height: 30 }
/** A row across the bottom of the screen: both lower corners would sit on it. */
const BOTTOM_ROW_RECT = { top: window.innerHeight - 100, left: 0, width: window.innerWidth, height: 30 }

const runScanMark = (
  <ScenarioCoachMark step="live-loop/run-scan">
    <button type="button">Run scan</button>
  </ScenarioCoachMark>
)

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
  it('renders children untouched and mounts no guide when the step is not the active one', () => {
    writeScenarioState(SLUG, collectMetricState())
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(runButton()).toBeInTheDocument()
    expect(guide()).toBeNull()
    expect(screen.queryByText(RUN_SCAN_INSTRUCTION)).not.toBeInTheDocument()
  })

  it('mounts no guide for a project that is not a demo', () => {
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
      demoProject({ is_demo: false }),
    )

    expect(runButton()).toBeInTheDocument()
    expect(guide()).toBeNull()
  })

  it('is suppressed by when={false} even on the active step', () => {
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan" when={false}>
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(runButton()).toBeInTheDocument()
    expect(guide()).toBeNull()
    expect(screen.queryByText(RUN_SCAN_INSTRUCTION)).not.toBeInTheDocument()
  })
})

describe('ScenarioCoachMark — on the active step', () => {
  it('shows the guide with the step, its place in the chain and its chapter', () => {
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    const note = screen.getByRole('note', { name: 'Demo hint' })
    expect(within(note).getByText(RUN_SCAN.title)).toBeInTheDocument()
    expect(within(note).getByText(RUN_SCAN_INSTRUCTION)).toBeInTheDocument()
    expect(note).toHaveTextContent(`Step 1 of ${STEPS.length} · ${CHAPTER_TITLES['live-loop']}`)
    expect(runButton()).toBeInTheDocument()
  })

  it('names the exact gesture under the instruction', () => {
    // Visitors read the instruction and still could not tell which control it
    // meant; the cue names it the way the page labels it.
    renderMark(runScanMark)

    const note = screen.getByRole('note', { name: 'Demo hint' })
    expect(within(note).getByText(cueOf(RUN_SCAN))).toBeInTheDocument()
  })

  it('sits on an opaque elevated surface so nearby page text cannot bleed through', () => {
    renderMark(runScanMark)

    const card = guide()?.firstElementChild
    expect(card).toHaveClass('bg-bg-elevated')
    expect(card?.getAttribute('style')).toContain('border-color: var(--accent)')
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

    const shown = guide()
    expect(shown).not.toBeNull()
    // Opening must not move focus into the guide, nor scope it there.
    expect(shown?.contains(document.activeElement)).toBe(false)

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

  it('names the gesture on the control itself: "Click here" unless the step says otherwise', () => {
    // A ring alone read as decoration: visitors could not tell it was the
    // control the step meant.
    stubAnchorRect(IN_VIEWPORT_RECT)
    renderMark(runScanMark)

    expect(tag()).toHaveTextContent('Click here')
    // Decoration for assistive technology: the instruction describes the control.
    expect(tag()).toHaveAttribute('aria-hidden')
    // On the side the step's placement names.
    expect(tag()).toHaveAttribute('data-coach-tag', 'bottom')
  })

  it("prints the step's own tag, and its own gesture in the guide", () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    writeScenarioState(SLUG, collectMetricState())
    renderMark(
      <ScenarioCoachMark step="live-loop/collect-metric">
        <button type="button">Actions for Signups</button>
      </ScenarioCoachMark>,
    )

    expect(tag()).toHaveTextContent('Open this menu')
    expect(
      within(screen.getByRole('note', { name: 'Demo hint' })).getByText(cueOf(COLLECT)),
    ).toBeInTheDocument()
  })

  it('draws only the tag when the mark asks for no ring', () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan" emphasis="none">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(ring()).toBeNull()
    expect(tag()).not.toBeNull()
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
    expect(tag()).toBeNull()
  })

  it('draws no ring for an anchor with no layout (0x0 rect)', () => {
    // jsdom's default rect is 0x0 — exactly the not-laid-out case.
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    expect(guide()).not.toBeNull()
    expect(ring()).toBeNull()
    expect(tag()).toBeNull()
  })

  it('does not scroll towards an anchor with no layout', async () => {
    // jsdom's default rect is 0x0: nothing sensible to scroll to.
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )
    await nextFrame()

    expect(scrollSpy).not.toHaveBeenCalled()
  })

  it('stands down for an anchor that is mounted but not rendered', () => {
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
      expect(guide()).toBeNull()
      expect(ring()).toBeNull()
      expect(runButton()).not.toHaveAttribute('data-coach-target')
    } finally {
      // jsdom has no checkVisibility of its own; leave none behind.
      if (original) Element.prototype.checkVisibility = original
      else delete (Element.prototype as { checkVisibility?: unknown }).checkVisibility
    }
  })

  it('follows the anchor as it is hidden and shown again, measured after each commit', () => {
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
      expect(guide()).not.toBeNull()

      // The render that collapses the section still sees it laid out.
      view.rerender(section(true))
      expect(guide()).toBeNull()
      expect(anchor()).not.toHaveAttribute('data-coach-target')

      // And the render that reveals it again still saw it hidden.
      view.rerender(section(false))
      expect(guide()).not.toBeNull()
      expect(anchor()).toHaveAttribute('data-coach-target', 'live-loop/run-scan')
    } finally {
      if (original) Element.prototype.checkVisibility = original
      else delete (Element.prototype as { checkVisibility?: unknown }).checkVisibility
    }
  })

  it('Hide hints removes the ring through the same gate as the guide', () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )
    expect(ring()).not.toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Hide hints' }))

    expect(ring()).toBeNull()
    expect(tag()).toBeNull()
    expect(guide()).toBeNull()
  })
})

describe('ScenarioCoachMark — scrolling an off-screen anchor into view', () => {
  it('scrolls the anchor into view once, a frame later, when it sits outside the viewport', async () => {
    stubAnchorRect(BELOW_FOLD_RECT)
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})

    const view = renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )

    // Not at once: a way back that stands down on the same commit must not
    // have moved the page first.
    expect(scrollSpy).not.toHaveBeenCalled()
    await nextFrame()
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
    await nextFrame()

    expect(scrollSpy).toHaveBeenCalledTimes(1)
  })

  it('does not scroll when the anchor is already inside the viewport', async () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})

    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )
    await nextFrame()

    expect(scrollSpy).not.toHaveBeenCalled()
  })

  it('uses an instant scroll when the user prefers reduced motion', async () => {
    stubAnchorRect(BELOW_FOLD_RECT)
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
    vi.stubGlobal(
      'matchMedia',
      vi.fn((query: string) => ({
        matches: query === '(prefers-reduced-motion: reduce)',
        media: query,
      })),
    )

    renderMark(
      <ScenarioCoachMark step="live-loop/run-scan">
        <button type="button">Run scan</button>
      </ScenarioCoachMark>,
    )
    await nextFrame()

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
    expect(guide()).toBeNull()
    expect(runButton()).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run scan again' })).toBeInTheDocument()

    // Muted, not dismissed: the strip carries on coaching.
    expect(screen.getByTestId('muted').textContent).toBe('true')
    expect(screen.getByTestId('active').textContent).toBe('true')
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

describe('ScenarioCoachMark — the guide keeps to a corner, off its control', () => {
  it('takes the first corner clear of the control it points at, measured before it shows', () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    renderMark(runScanMark)

    expect(guide()).toHaveAttribute('data-guide-corner', 'bottom-right')
    // Unmeasured, it would have painted at the screen's corner for a frame.
    expect(guide()?.style.visibility).toBe('visible')
    expect(guide()?.style.width).toBe('336px')
  })

  it('moves to the next corner when the control sits in the first', () => {
    stubAnchorRect({
      top: window.innerHeight - 100,
      left: window.innerWidth - 200,
      width: 120,
      height: 30,
    })
    renderMark(runScanMark)

    expect(guide()).toHaveAttribute('data-guide-corner', 'bottom-left')
  })

  it('goes to the top when a row across the bottom takes both lower corners', () => {
    stubAnchorRect(BOTTOM_ROW_RECT)
    renderMark(rowMark)

    expect(guide()).toHaveAttribute('data-guide-corner', 'top-right')
    // Just under the top bar.
    expect(guide()?.style.top).toBe('56px')
  })

  it('keeps clear of an open menu', () => {
    // A menu that opened down into the guide's corner had its items covered.
    stubRects((element) =>
      element.getAttribute('role') === 'menu'
        ? { top: 560, left: 640, width: 240, height: 160 }
        : IN_VIEWPORT_RECT,
    )
    renderMark(
      <>
        {runScanMark}
        <div role="menu">Collect now</div>
      </>,
    )

    expect(guide()).toHaveAttribute('data-guide-corner', 'bottom-left')
  })

  it('takes a narrower card beside a dialog too wide to leave the column a corner', () => {
    // 480px wide and nearly as tall as the screen: no corner of the column
    // clears it, 256px of the screen does on either side. The step moved into
    // the dialog, and folding to the face took its words away just then.
    stubRects((element) =>
      element.getAttribute('role') === 'dialog'
        ? { top: 38, left: 272, width: 480, height: 691 }
        : IN_VIEWPORT_RECT,
    )
    renderMark(
      <>
        {runScanMark}
        <div role="dialog" aria-label="Edit product_id">
          Product ID
        </div>
      </>,
    )

    expect(guide()).toHaveAttribute('data-guide-mode', 'narrow')
    expect(guide()).toHaveAttribute('data-guide-corner', 'bottom-right')
    expect(guide()?.style.width).toBe('240px')
    expect(within(screen.getByRole('note', { name: 'Demo hint' })).getByText(RUN_SCAN_INSTRUCTION)).toBeInTheDocument()
  })

  it('steps aside to its face when no corner is clear, and opens when asked', () => {
    // A dialog as big as the screen: every corner of it would cover the dialog.
    stubRects((element) =>
      element.getAttribute('role') === 'dialog'
        ? { top: 0, left: 0, width: window.innerWidth, height: window.innerHeight }
        : IN_VIEWPORT_RECT,
    )
    renderMark(
      <>
        {runScanMark}
        <div role="dialog" aria-label="Edit event">
          Product ID
        </div>
      </>,
    )

    const face = screen.getByRole('button', { name: faceName })
    expect(face).toHaveAttribute('aria-expanded', 'false')
    expect(guide()).toHaveAttribute('data-minimised', 'true')

    fireEvent.click(face)

    // Asked for, it opens over the dialog after all.
    expect(screen.getByRole('button', { name: 'Minimise the demo guide' })).toBeInTheDocument()
    expect(guide()).not.toHaveAttribute('data-minimised')
  })

  it('keeps a top corner under the demo banner, clear of its controls (#251)', () => {
    // The card used to sit at a fixed top-14, exactly over the banner's hide
    // hints / dismiss / tour / Reset / Delete: the controls that put it away.
    const banner = document.createElement('div')
    banner.setAttribute('data-demo-banner', '')
    document.body.appendChild(banner)
    stubRects((element) =>
      element === banner
        ? { top: 56, left: 0, width: window.innerWidth, height: 46 }
        : BOTTOM_ROW_RECT,
    )
    try {
      renderMark(rowMark)

      expect(guide()).toHaveAttribute('data-guide-corner', 'top-right')
      // Banner bottom (102) plus the 8px gap.
      expect(guide()?.style.top).toBe('110px')
    } finally {
      banner.remove()
    }
  })

  it('follows the banner when it grows or lands late (#251)', async () => {
    // Opening the phone pill, the strip's chunk landing and a failure line all
    // move the banner's bottom edge without a resize or a scroll; on a hard
    // load the banner is not there at all when the guide mounts.
    let bannerHeight = 46
    const banner = document.createElement('div')
    banner.setAttribute('data-demo-banner', '')
    stubRects((element) =>
      element === banner
        ? { top: 56, left: 0, width: window.innerWidth, height: bannerHeight }
        : BOTTOM_ROW_RECT,
    )
    try {
      renderMark(rowMark)
      // No banner yet: just under the top bar.
      expect(guide()?.style.top).toBe('56px')

      // The banner lands: the guide moves under it.
      document.body.appendChild(banner)
      await waitFor(() => expect(guide()?.style.top).toBe('110px'))

      // It grows (the pill opens): the guide follows.
      bannerHeight = 120
      banner.setAttribute('data-open', '')
      await waitFor(() => expect(guide()?.style.top).toBe('184px'))
      // Let the re-measure its own move set off land inside act.
      await nextFrame()
    } finally {
      banner.remove()
    }
  })

  it('spans the screen on a phone, below a control high on the page', () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    stubPhone()
    renderMark(runScanMark)

    expect(guide()).toHaveAttribute('data-guide-corner', 'bottom-left')
    // The screen's width, less a 12px gutter on each side.
    expect(guide()?.style.width).toBe(`${window.innerWidth - 24}px`)
  })

  it('takes the top on a phone when the control is low on the screen', () => {
    stubAnchorRect({ top: window.innerHeight - 60, left: 100, width: 120, height: 30 })
    stubPhone()
    renderMark(rowMark)

    expect(guide()).toHaveAttribute('data-guide-corner', 'top-left')
    expect(guide()?.style.top).toBe('56px')
  })

  it('is portalled to <body>, never left as a <div> inside <tbody>', () => {
    renderMark(rowMark)

    expect(guide()?.parentElement).toBe(document.body)
    expect(document.querySelector('tbody div')).toBeNull()
  })
})

describe('ScenarioCoachMark — minimising the guide', () => {
  it('folds to its face, so it never has to cover a tap target for good', () => {
    renderMark(rowMark)

    const minimise = screen.getByRole('button', { name: 'Minimise the demo guide' })
    expect(minimise).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(minimise)

    const face = screen.getByRole('button', { name: faceName })
    expect(face).toHaveAttribute('aria-expanded', 'false')
    expect(guide()).toHaveAttribute('data-minimised', 'true')
    // Still in the tree, so the anchor's description keeps resolving.
    expect(runButton()).toHaveAccessibleDescription(RUN_SCAN_INSTRUCTION)
    // Minimising is not muting: the scenario and the hints carry on.
    expect(screen.getByTestId('muted').textContent).toBe('false')

    fireEvent.click(face)

    expect(screen.getByRole('button', { name: 'Minimise the demo guide' })).toBeInTheDocument()
    expect(guide()).not.toHaveAttribute('data-minimised')
  })

  it('opens again at full width when its face is tapped on a phone', () => {
    // Unfolded, the card took the width of the box it last held — the face's
    // — and measured a word a line: too tall for any corner, it folded
    // straight back, and the tap on the face did nothing.
    stubAnchorRect(IN_VIEWPORT_RECT)
    stubPhone()
    stubGuideHeight()
    renderMark(runScanMark)

    fireEvent.click(screen.getByRole('button', { name: 'Minimise the demo guide' }))
    fireEvent.click(screen.getByRole('button', { name: faceName }))

    expect(screen.getByRole('button', { name: 'Minimise the demo guide' })).toBeInTheDocument()
    expect(guide()).not.toHaveAttribute('data-minimised')
    expect(guide()?.style.width).toBe(`${window.innerWidth - 24}px`)
  })

  it('stays folded for the step across pages, and opens again for the next one', () => {
    const first = renderMark(runScanMark)
    fireEvent.click(screen.getByRole('button', { name: 'Minimise the demo guide' }))
    first.unmount()

    // Another page coaching the same step: still folded.
    const second = renderMark(runScanMark)
    expect(guide()).toHaveAttribute('data-minimised', 'true')
    second.unmount()

    writeScenarioState(SLUG, collectMetricState())
    renderMark(
      <ScenarioCoachMark step="live-loop/collect-metric">
        <button type="button">Collect now</button>
      </ScenarioCoachMark>,
    )
    expect(guide()).not.toHaveAttribute('data-minimised')
    expect(screen.getByText(COLLECT_INSTRUCTION)).toBeInTheDocument()
  })

  it('keeps its clicks from the row it sits in', () => {
    // The guide is portalled, and React bubbles a portal's events through the
    // component tree: a click on the old card's Collapse reached the scan
    // row's onClick and opened the scan.
    const onRow = vi.fn()
    renderMark(
      <table>
        <tbody>
          <tr onClick={onRow}>
            <td>
              <ScenarioCoachMark step="live-loop/run-scan">
                <button type="button">Run scan</button>
              </ScenarioCoachMark>
            </td>
          </tr>
        </tbody>
      </table>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Minimise the demo guide' }))
    fireEvent.click(screen.getByRole('button', { name: faceName }))
    fireEvent.click(screen.getByRole('button', { name: 'Hide hints' }))
    expect(onRow).not.toHaveBeenCalled()
  })
})

describe('ScenarioCoachMark — the anchor is never remounted', () => {
  it('mounts the anchor once, so focus and local state survive the guide', () => {
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

    expect(guide()).not.toBeNull()
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
    expect(guide()).not.toBeNull()
    button.focus()

    fireEvent.click(button)

    // The step moved on, so the mark stopped coaching — without swapping the
    // tree around the control (the old bare-children return remounted it).
    expect(guide()).toBeNull()
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

    expect(guide()).toBeNull()
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

describe('ScenarioCoachMark — tied to its control', () => {
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

describe('ScenarioCoachMark — the way back to a step', () => {
  // set-value's field lives in the editor the pencil on the event's row opens.
  const EDIT_STEPS = buildChapterSteps(SLUG, 'edit-event', initialScenarioState())
  const OPEN_EDITOR = EDIT_STEPS[0]
  const SET_VALUE = at(EDIT_STEPS, 1)
  const pencilMark = (
    <ScenarioCoachMark step="edit-event/open-editor">
      <button type="button">Edit Trial Started</button>
    </ScenarioCoachMark>
  )
  const productIdMark = (
    <ScenarioCoachMark step="edit-event/set-value">
      <input aria-label="Product ID" />
    </ScenarioCoachMark>
  )
  const pencil = () => screen.getByRole('button', { name: 'Edit Trial Started' })

  beforeEach(() => {
    writeScenarioState(SLUG, chapterState('edit-event', 'edit-event/set-value'))
  })

  it("points at the pencil with the active step's words while its field is not on screen", () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    renderMark(pencilMark)

    const note = screen.getByRole('note', { name: 'Demo hint' })
    expect(within(note).getByText(SET_VALUE.title)).toBeInTheDocument()
    expect(within(note).getByText(SET_VALUE.instruction)).toBeInTheDocument()
    expect(note).toHaveTextContent(`Step 2 of ${EDIT_STEPS.length}`)
    // The gesture and the tag are the pencil's own: that is what to do here.
    expect(within(note).getByText(cueOf(OPEN_EDITOR))).toBeInTheDocument()
    expect(tag()).toHaveTextContent('Click here')
    expect(pencil()).toHaveAttribute('data-coach-target', 'edit-event/open-editor')
    expect(pencil()).toHaveAccessibleDescription(SET_VALUE.instruction)
  })

  it("stands down once the step's own control is on screen", () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    renderMark(
      <>
        {pencilMark}
        {productIdMark}
      </>,
    )

    expect(screen.getAllByRole('note', { name: 'Demo hint' })).toHaveLength(1)
    expect(document.querySelectorAll('[data-coach-tag]')).toHaveLength(1)
    expect(tag()).toHaveTextContent('Type here')
    expect(pencil()).not.toHaveAttribute('data-coach-target')
    expect(screen.getByLabelText('Product ID')).toHaveAttribute(
      'data-coach-target',
      'edit-event/set-value',
    )
  })

  it('stands down once what it opens covers it', () => {
    // The editor dialog over the list: the pencil has done its job, and the
    // guide host says what is missing rather than sending the user round to
    // the same pencil again.
    stubAnchorRect(IN_VIEWPORT_RECT)
    const restore = stubCoveringLayer()
    try {
      renderMark(pencilMark)

      expect(guide()).toBeNull()
      expect(ring()).toBeNull()
      expect(pencil()).not.toHaveAttribute('aria-describedby')
    } finally {
      restore()
    }
  })

  it('keeps speaking on its own step while something covers the control', () => {
    // Only the way back stands down: the ring goes, the guide stays.
    writeScenarioState(SLUG, liveLoopState('live-loop/run-scan'))
    stubAnchorRect(IN_VIEWPORT_RECT)
    const restore = stubCoveringLayer()
    try {
      renderMark(runScanMark)

      expect(screen.getByRole('note', { name: 'Demo hint' })).toBeInTheDocument()
      expect(ring()).toBeNull()
    } finally {
      restore()
    }
  })

  it('scrolls to the way back when it is all there is on the page', async () => {
    stubAnchorRect(BELOW_FOLD_RECT)
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})

    renderMark(pencilMark)
    await nextFrame()

    expect(scrollSpy).toHaveBeenCalledTimes(1)
  })

  it('never scrolls to a way back that stands down on the commit it shows on', async () => {
    // The list beside what it opens — the branch list beside the branch — puts
    // both marks on screen at once, and the page jumped to the pencil far
    // below while the field it opens sat in view.
    stubRects((element) =>
      element instanceof HTMLButtonElement && element.textContent === 'Edit Trial Started'
        ? BELOW_FOLD_RECT
        : IN_VIEWPORT_RECT,
    )
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})

    renderMark(
      <>
        {pencilMark}
        {productIdMark}
      </>,
    )
    await nextFrame()

    expect(scrollSpy).not.toHaveBeenCalled()
  })
})

describe('ScenarioCoachMark — clipped by its scroll container', () => {
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

    expect(guide()).not.toBeNull()
    expect(ring()).toBeNull()
  })

  it('scrolls an anchor that its container clips, even inside the window', async () => {
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
    stubClippedLayout()

    renderMark(clippedMark())
    await nextFrame()

    expect(scrollSpy).toHaveBeenCalledTimes(1)
    // Clipped only sideways: the page does not also jump vertically.
    expect(scrollSpy).toHaveBeenCalledWith({ behavior: 'smooth', block: 'nearest', inline: 'nearest' })
  })

  it('does not scroll an anchor wider than its container that is already in view', async () => {
    // A table row on a phone, wider than its overflow-x-auto wrapper: it can
    // never fit, and it was centred vertically on every step regardless.
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
    stubClippedLayout({ x: 0, y: 100, width: 800, height: 30 })

    renderMark(clippedMark())
    await nextFrame()

    expect(scrollSpy).not.toHaveBeenCalled()
  })

  it('centres an anchor its container clips vertically', async () => {
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
    stubClippedLayout({ x: 50, y: 500, width: 120, height: 30 })

    renderMark(clippedMark())
    await nextFrame()

    expect(scrollSpy).toHaveBeenCalledWith({ behavior: 'smooth', block: 'center', inline: 'nearest' })
  })
})

describe("ScenarioCoachMark — the step's second gesture", () => {
  // The collect step's ring is on the ⋮ menu; the menu's Collect now is the
  // gesture after it, which testers were left to find.
  const menuMark = (
    <ScenarioCoachMark step="live-loop/collect-metric">
      <button type="button">Actions for Signups</button>
    </ScenarioCoachMark>
  )
  const collectNowMark = (
    <ScenarioCoachMark step="live-loop/collect-metric" followUp tag="Then click here" side="left">
      <button type="button">Collect now</button>
    </ScenarioCoachMark>
  )
  const notes = () => screen.getAllByRole('note', { name: 'Demo hint' })

  it('rings the control the first gesture opened, and the first ring stands down', () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    writeScenarioState(SLUG, collectMetricState())
    const { rerender } = renderMark(menuMark)
    expect(tag()).toHaveTextContent('Open this menu')

    // The menu opens.
    rerender(
      <>
        {menuMark}
        {collectNowMark}
      </>,
    )

    expect(document.querySelectorAll('.coach-ring')).toHaveLength(1)
    expect(tag()).toHaveTextContent('Then click here')
    expect(tag()).toHaveAttribute('data-coach-tag', 'left')
    expect(screen.getByRole('button', { name: 'Collect now' })).toHaveAttribute(
      'data-coach-target',
      'live-loop/collect-metric@then',
    )
    // One guide, the step's own, still saying the whole gesture.
    expect(notes()).toHaveLength(1)
    expect(within(at(notes(), 0)).getByText(COLLECT_INSTRUCTION)).toBeInTheDocument()
  })

  it('hands the ring back when what it opened closes', () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    writeScenarioState(SLUG, collectMetricState())
    const { rerender } = renderMark(
      <>
        {menuMark}
        {collectNowMark}
      </>,
    )
    expect(tag()).toHaveTextContent('Then click here')

    rerender(menuMark)

    expect(tag()).toHaveTextContent('Open this menu')
  })

  it("holds no description of its own: the instruction is in the step mark's guide", () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    writeScenarioState(SLUG, collectMetricState())
    renderMark(
      <>
        {menuMark}
        {collectNowMark}
      </>,
    )

    expect(screen.getByRole('button', { name: 'Collect now' })).not.toHaveAttribute(
      'aria-describedby',
    )
    expect(screen.getByRole('button', { name: 'Actions for Signups' })).toHaveAccessibleDescription(
      COLLECT_INSTRUCTION,
    )
  })

  it('stays out of the way on any other step', () => {
    stubAnchorRect(IN_VIEWPORT_RECT)
    renderMark(collectNowMark)

    expect(ring()).toBeNull()
    expect(tag()).toBeNull()
    expect(screen.getByRole('button', { name: 'Collect now' })).not.toHaveAttribute(
      'data-coach-target',
    )
  })
})
