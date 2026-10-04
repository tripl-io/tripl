/**
 * The coached demo scenario, on the surface itself.
 *
 * The strip tells the user what the next step is; this points at the button that
 * does it. Product pages wrap their action element and name a step — they learn
 * nothing about scenario state, and a page that is not part of any scenario, or
 * a project that is not a demo, renders exactly what it always rendered: the
 * Popover root and its asChild anchor add no DOM, the anchor gets no extra
 * props, and no card, ring or portal is mounted.
 *
 * A coach mark is a hint, never a dialog. It never takes focus from the control
 * it points at, never traps it, and cannot be broken by Escape or a click
 * elsewhere — the scenario is not something the user can accidentally cancel.
 * The one control it offers is "Hide hints", which quiets the marks for the
 * browser session (per project, in sessionStorage) while leaving the scenario
 * running and the strip coaching. The strip offers the same toggle, so a
 * keyboard user does not have to Tab through the whole page to the portalled
 * card to reach it (DEMO-12).
 *
 * One gate controls everything this file does: the card, the
 * pulsing ring around the anchor, the one-shot scroll to an off-screen anchor,
 * and the presence report the strip reads all key off the same `visible`.
 */

import {
  cloneElement,
  isValidElement,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
  type Ref,
  type RefCallback,
} from 'react'
import { createPortal } from 'react-dom'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { MAIN_CONTENT_ID } from '@/components/landmarks'
import { Popover, PopoverAnchor, PopoverArrow, PopoverContent } from '@/components/ui/popover'
import { CoachBeacon } from './CoachBeacon'
import { clippedAxes, clippingAncestors, visibleFrame } from './coachGeometry'
import { useCoachPresence, useDemoScenario, useDemoScenarioActions } from './demoScenarioContext'
import type { ScenarioStepId } from './scenarioModel'

interface ScenarioCoachMarkProps {
  /** The step this action belongs to. The mark shows only while it is the active one. */
  step: ScenarioStepId
  /** Extra page-local condition — e.g. only the scan config the scenario is watching. */
  when?: boolean
  /** Overrides for the step's own placement (`ScenarioStep.coach`). Rarely needed. */
  side?: 'top' | 'right' | 'bottom' | 'left'
  align?: 'start' | 'center' | 'end'
  emphasis?: 'ring' | 'none'
  children: ReactNode
}

/**
 * How the card is shown, decided from the MEASURED anchor after each commit:
 * - `pending`: not measured yet (the anchor has not mounted) — nothing shows;
 * - `hidden`: mounted without a box — the mark stands down;
 * - `anchored`: a popover beside the control;
 * - `docked`: a fixed card clear of a data table (see below).
 */
type Placement = 'pending' | 'hidden' | 'anchored' | 'docked'

/** Mounted but without a box (display:none on it or an ancestor). */
function isUnrendered(anchor: HTMLElement): boolean {
  return typeof anchor.checkVisibility === 'function' && !anchor.checkVisibility()
}

/** Below `sm`: a phone, where an anchored card has no side that is not page. */
const PHONE_QUERY = '(max-width: 639px)'

function isPhone(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia(PHONE_QUERY).matches
}

function placementOf(anchor: HTMLElement | null): Placement {
  if (!anchor) return 'pending'
  if (isUnrendered(anchor)) return 'hidden'
  // A control inside a data table — or the <tr> itself — has no adjacent
  // space that is not table: every side the card can open on lands on the
  // rows it is explaining, and Radix only flips to avoid the VIEWPORT edge,
  // not the content underneath. Such marks dock the card.
  // So does every mark on a phone (#251 DA-45): at 390px a 16rem card beside
  // a row's Run button covered the page title and its tabs, which could not
  // be used until the hints were hidden. Docked, it is a full-width bottom
  // sheet there, and it collapses.
  return anchor.closest('table') || isPhone() ? 'docked' : 'anchored'
}

/** The 44px top bar plus a gap: `top-14`. */
const TOP_BAR_CLEARANCE_PX = 56
const BELOW_BANNER_GAP_PX = 8

/**
 * Where a card docked at the top starts (#251 SH-5). `top-14` cleared only
 * the top bar, so at 1440 the card landed exactly on the demo banner's own
 * controls — hide hints, dismiss, the tour, Reset, Delete — hiding the very
 * buttons that would put it away. While the banner is on screen the card
 * starts under it; scrolled away, it goes back up to the top bar. Never past
 * the upper third, where it would reach down into the half it is keeping
 * clear.
 */
function dockTop(): number {
  const banner = document.querySelector('[data-demo-banner]')
  if (!banner) return TOP_BAR_CLEARANCE_PX
  const below = banner.getBoundingClientRect().bottom + BELOW_BANNER_GAP_PX
  return Math.round(
    Math.max(TOP_BAR_CLEARANCE_PX, Math.min(below, window.innerHeight / 3)),
  )
}

/** Which half of the viewport the anchor's centre is in — the docked card takes the other. */
function anchorHalf(anchor: HTMLElement): 'upper' | 'lower' {
  const rect = anchor.getBoundingClientRect()
  return rect.top + rect.height / 2 > window.innerHeight / 2 ? 'lower' : 'upper'
}

/**
 * Which side of the viewport the anchor's own control sits on, so the docked
 * card lines up with it instead of always hugging the right edge (LIVE-13): a
 * "Run now" at the left of a scans table got its card on the far side of the
 * screen. For a whole-row anchor that is the row's first focusable control,
 * which is what the step asks the reader to use.
 */
function anchorColumn(anchor: HTMLElement): 'left' | 'right' {
  const target = describedDescendant(anchor) ?? anchor
  const rect = target.getBoundingClientRect()
  return rect.left + rect.width / 2 < window.innerWidth / 2 ? 'left' : 'right'
}

/** What a keyboard user can land on — the element a description is heard from. */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

/**
 * The element that must carry the step's description besides the anchor: its
 * first focusable descendant, when the anchor itself is a wrapper nobody tabs
 * to (EventsHeader wraps the drift badge's trigger in a span). None when the
 * anchor is focusable — the clone already describes it.
 */
function describedDescendant(anchor: HTMLElement): HTMLElement | null {
  if (anchor.matches(FOCUSABLE)) return null
  return anchor.querySelector<HTMLElement>(FOCUSABLE)
}

function describedByIds(element: Element): string[] {
  return (element.getAttribute('aria-describedby') ?? '').split(/\s+/).filter(Boolean)
}

function mergeRefs(...refs: Array<Ref<HTMLElement> | undefined>): RefCallback<HTMLElement> {
  return (node) => {
    for (const ref of refs) {
      if (typeof ref === 'function') ref(node)
      else if (ref) ref.current = node
    }
  }
}

export function ScenarioCoachMark({
  step,
  when = true,
  side,
  align,
  emphasis,
  children,
}: ScenarioCoachMarkProps) {
  const { active, step: activeStep, steps, hintsMuted } = useDemoScenario()
  const { muteHints } = useDemoScenarioActions()
  const { report } = useCoachPresence()
  const instructionId = useId()

  const coaching = active && !hintsMuted && when && activeStep.id === step

  // The anchor is state, not a ref: the beacon and the scroll effect must
  // re-run when the element appears, and a ref mutation would not tell them.
  const [anchorEl, setAnchorEl] = useState<HTMLElement | null>(null)
  const childRef = isValidElement(children)
    ? (children.props as { ref?: Ref<HTMLElement> }).ref
    : undefined
  const anchorRef = useMemo(() => mergeRefs(childRef, setAnchorEl), [childRef])

  // An anchor that is mounted but not rendered — inside a hidden tab panel or a
  // collapsed section (display:none) — has no box, and Radix pinned the card to
  // the page's top-left corner, pointing at nothing (DEMO-10). Such a mark
  // stands down like any other invisible one, so the strip's "not on screen"
  // notice speaks instead. `checkVisibility` is absent in older engines (and
  // jsdom); there the anchor is taken as shown, which is the old behaviour.
  //
  // Measured after the commit, never during render: a render reads the DOM the
  // previous commit left, so the render that reveals a panel still saw it
  // hidden, and the one that collapses a section still saw it laid out. A
  // layout effect re-measures after every commit, before paint, so neither
  // shows; the observer catches a box that appears or vanishes with no render.
  //
  // Docking is decided by the same measurement (DEMO-2). It used to be read
  // from the anchor during render — null on the first one — so the card
  // painted undocked for a frame and the switch swapped the element tree
  // around the anchor, remounting the very control being coached (and
  // dropping its focus).
  //
  // Keyed on `children` rather than run after every commit: a parent that
  // reveals or collapses the anchor re-renders this mark with a new element,
  // while this mark's own updates (the measurement included) keep the same
  // one — so the measurement can never feed itself.
  const [placement, setPlacement] = useState<Placement>('pending')
  useLayoutEffect(() => {
    const measure = () => setPlacement(coaching ? placementOf(anchorEl) : 'pending')
    measure()
  }, [anchorEl, children, coaching])
  useEffect(() => {
    if (!coaching || !anchorEl || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => setPlacement(placementOf(anchorEl)))
    observer.observe(anchorEl)
    return () => observer.disconnect()
  }, [coaching, anchorEl])
  const visible = coaching && (placement === 'anchored' || placement === 'docked')
  const docked = visible && placement === 'docked'

  // Tell the strip a mark for this step is actually on screen, so it can say
  // so when one is not. Keyed on the same gate as the card.
  useEffect(() => {
    if (!visible) return
    report(step, true)
    return () => report(step, false)
  }, [visible, step, report])

  // The description reaches the control a screen reader user actually tabs to
  // (DEMO-12). The clone below describes the anchor; a non-focusable wrapper
  // anchor passes it on to its first focusable descendant here, after the
  // commit, and takes it back when the mark goes quiet. Re-run on `children`
  // so a descendant that re-mounts gets it again.
  useLayoutEffect(() => {
    if (!visible || !anchorEl) return
    const target = describedDescendant(anchorEl)
    if (!target || describedByIds(target).includes(instructionId)) return
    target.setAttribute('aria-describedby', [...describedByIds(target), instructionId].join(' '))
    return () => {
      const rest = describedByIds(target).filter((id) => id !== instructionId)
      if (rest.length > 0) target.setAttribute('aria-describedby', rest.join(' '))
      else target.removeAttribute('aria-describedby')
    }
  }, [visible, anchorEl, instructionId, children])

  // Bring an anchor into view once per step when it is not fully visible:
  // coaching towards a control below the fold is coaching towards nothing.
  // "Visible" means inside every scroll container around it, not just inside
  // the window (DEMO-11) — a row action scrolled out of a table wrapper, or
  // half under the scrolling pane's edge, is not on screen either. One-shot,
  // so the user keeps control of their own scrolling afterwards.
  const scrolledStepRef = useRef<ScenarioStepId | null>(null)
  useEffect(() => {
    if (!visible || !anchorEl) return
    if (scrolledStepRef.current === step) return
    scrolledStepRef.current = step
    // Guarded because jsdom (tests) may not implement it — same pattern as
    // pages/metrics/MetricForm.tsx.
    if (typeof anchorEl.scrollIntoView !== 'function') return
    const rect = anchorEl.getBoundingClientRect()
    // 0x0 means not laid out; nothing sensible to scroll to.
    if (rect.width === 0 && rect.height === 0) return
    // Per axis: an anchor clipped only sideways (a row action in a table
    // wrapper) must not also jump the page to centre a row already in view.
    const clipped = clippedAxes(visibleFrame(clippingAncestors(anchorEl)), rect)
    if (!clipped.vertical && !clipped.horizontal) return
    const reduceMotion =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    anchorEl.scrollIntoView({
      behavior: reduceMotion ? 'auto' : 'smooth',
      block: clipped.vertical ? 'center' : 'nearest',
      inline: 'nearest',
    })
  }, [visible, anchorEl, step])

  // Radix flips and shifts only to avoid the VIEWPORT edge, so on the 1512px
  // shell a card anchored near the right of the content column keeps going and
  // lands on the activity rail (its left border sits at x≈1208), cutting the
  // rail's rows mid-glyph. Bound the card to the landmark the anchor lives in:
  // the rail and the sidebar are SIBLINGS of it, never inside, so a card can
  // only shift within the page it is coaching.
  //
  // The landmark is the content COLUMN, not the scroll container around it —
  // Layout keeps the page's 32px gutter as padding on a wrapper OUTSIDE
  // #main-content. That distinction is the whole fix: floating-ui clips
  // to an element's PADDING box, so bounding to the container let a card stop
  // 8px short of x≈1208 — about 24px past x≈1176, where the right edge of every
  // card on the page sits. Measured on the scan header, whose `align: 'start'`
  // mark had just been moved off the description into that gutter.
  // Bounded to the column, the same card stops at 1168 and still clears the
  // description, which wraps at x≈891.
  //
  // No landmark (tests, a portalled anchor) falls back to the viewport, which
  // is what every mark had before.
  const boundary: Element | Element[] = anchorEl?.closest(`#${MAIN_CONTENT_ID}`) ?? []

  // A single element child keeps the same tree around it whether or not the
  // mark is coaching, and in every placement (DEMO-2): only the anchor's props
  // and the portalled siblings change. Measuring, docking, hiding — and the
  // step completing, often from a click on the anchor itself, or "Hide hints"
  // — never remount the control, so a keyboard user keeps focus on it. The
  // Popover root and the asChild anchor render no DOM of their own.
  //
  // A non-element child (text, a fragment) cannot take the asChild anchor; it
  // needs a wrapper while coaching, and a non-demo page must not get one.
  if (!coaching && !isValidElement(children)) return <>{children}</>

  const position = steps.findIndex((candidate) => candidate.id === activeStep.id) + 1
  // Read only while coaching (the card renders only then), when the gate
  // guarantees activeStep.id === step, so this is this step's config.
  // Deep-link steps carry no placement of their own; a mark placed on one
  // anyway falls back to a neutral bottom/center card.
  const coach = activeStep.coach
  const ringed = (emphasis ?? coach?.emphasis ?? 'ring') === 'ring'
  const card = (
    <CoachCard
      position={position}
      total={steps.length}
      instruction={activeStep.instruction}
      instructionId={instructionId}
      onMute={muteHints}
    />
  )

  // The instruction describes the control it points at (DEMO-12), so a screen
  // reader user who tabs to it hears the step — the card itself sits at the
  // end of <body>, far from the control in reading order.
  const ownDescribedBy = isValidElement(children)
    ? (children.props as { 'aria-describedby'?: string })['aria-describedby']
    : undefined
  const anchorProps: Record<string, unknown> = !coaching
    ? // Not coaching: the child keeps its own ref and attributes untouched.
      {}
    : visible
    ? {
        ref: anchorRef,
        'data-coach-target': step,
        'aria-describedby': [ownDescribedBy, instructionId].filter(Boolean).join(' '),
      }
    : // Keep the ref on a hidden anchor, so the measurement above can see it
      // come back.
      { ref: anchorRef }

  return (
    // Open with no `onOpenChange`: Escape and outside clicks reach Radix and
    // resolve to nothing, so no stray interaction can silence the coaching.
    <Popover open={visible && !docked}>
      {/* Slot renders nothing for a non-element child, so only merge onto the
          child when there is a single element to merge onto. The clone stamps
          the exact click target with data-coach-target and captures it for the
          beacon; the beacon itself is an overlay, never a wrapper, so anchors
          with position-sensitive DOM (the <tr> in ScanDetail) stay valid. */}
      {isValidElement(children) ? (
        <PopoverAnchor asChild>
          {cloneElement(children as ReactElement<Record<string, unknown>>, anchorProps)}
        </PopoverAnchor>
      ) : (
        <PopoverAnchor ref={anchorRef}>{children}</PopoverAnchor>
      )}
      {visible && ringed && anchorEl && <CoachBeacon anchor={anchorEl} />}
      {docked && anchorEl && <DockedCoachCard anchor={anchorEl}>{card}</DockedCoachCard>}
      {visible && !docked && (
        <PopoverContent
          role="note"
          aria-label="Demo hint"
          side={side ?? coach?.side ?? 'bottom'}
          align={align ?? coach?.align ?? 'center'}
          sideOffset={8}
          collisionBoundary={boundary}
          avoidCollisions
          // Enough that a shifted card reads as sitting beside the rail rather
          // than welded to its border.
          collisionPadding={8}
          // The action stays focused; the hint must not pull the caret out of the
          // control it is describing, nor scope focus to itself.
          onOpenAutoFocus={(event) => event.preventDefault()}
          onCloseAutoFocus={(event) => event.preventDefault()}
          className="w-64 max-w-[calc(100vw-16px)] rounded-lg border p-3 shadow-sm motion-reduce:animate-none"
          style={{ background: 'var(--bg-elevated)', borderColor: 'var(--accent)' }}
        >
          <PopoverArrow
            width={12}
            height={6}
            style={{ fill: 'var(--bg-elevated)', stroke: 'var(--accent)' }}
          />
          {card}
        </PopoverContent>
      )}
    </Popover>
  )
}

/**
 * The card for a table anchor, fixed clear of the grid.
 *
 * Portalled to <body> (DEMO-1): declared beside a <tr> anchor it used to land
 * as a <div> directly inside <tbody>. And it must not become the thing it
 * hides (DEMO-13 / LIVE-13):
 * - it takes the half of the viewport the anchor is NOT in, so a row action
 *   near the bottom gets its card at the top instead of under it — except on
 *   a phone, where it is always a bottom sheet (#251 DA-45);
 * - below `sm` it spans the width with a gutter rather than covering ~70% of
 *   a phone screen from the right edge;
 * - it collapses to a one-line tab, so it never has to block taps for good —
 *   muting every hint was the only way out before.
 *
 * bottom-[68px] clears the tweaks FAB (fixed bottom-1, h-8 → top edge at 36px;
 * tripl-tvqk tucked it into the activity rail's footer strip) rather than
 * fighting it on z-index, which would also put the coach over modal dialogs.
 * top-14 clears the 44px top bar, and the demo banner below it
 * while that is on screen (see `dockTop`).
 */
function DockedCoachCard({ anchor, children }: { anchor: HTMLElement; children: ReactNode }) {
  const [collapsed, setCollapsed] = useState(false)
  const [half, setHalf] = useState<'upper' | 'lower'>(() => anchorHalf(anchor))
  const [column, setColumn] = useState<'left' | 'right'>(() => anchorColumn(anchor))
  const [top, setTop] = useState(dockTop)
  const [phone, setPhone] = useState(isPhone)

  useEffect(() => {
    let frame = 0
    const refresh = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        setHalf(anchorHalf(anchor))
        setColumn(anchorColumn(anchor))
        setTop(dockTop())
        setPhone(isPhone())
      })
    }
    window.addEventListener('resize', refresh)
    // Capture: the anchor may live inside any scroll container, not just the page.
    window.addEventListener('scroll', refresh, { capture: true, passive: true })
    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener('resize', refresh)
      window.removeEventListener('scroll', refresh, { capture: true })
    }
  }, [anchor])

  // The banner's bottom edge moves with no resize or scroll to hear it by
  // (#251 SH-5): the phone pill opens, the scenario strip's chunk lands in the
  // row, a failure line appears under it — and on a hard load the banner is a
  // placeholder first, then a different element. So the banner is watched for
  // its size, and the document for the banner being swapped in or out.
  useEffect(() => {
    let frame = 0
    const refreshTop = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => setTop(dockTop()))
    }
    const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(refreshTop)
    // The banner there at mount is already measured by the initial state.
    let observed: Element | null = document.querySelector('[data-demo-banner]')
    if (observed) resize?.observe(observed)
    const track = () => {
      const banner = document.querySelector('[data-demo-banner]')
      if (banner === observed) return
      if (observed) resize?.unobserve(observed)
      observed = banner
      if (banner) resize?.observe(banner)
      refreshTop()
    }
    const swaps = typeof MutationObserver === 'undefined' ? null : new MutationObserver(track)
    swaps?.observe(document.body, { childList: true, subtree: true })
    return () => {
      cancelAnimationFrame(frame)
      resize?.disconnect()
      swaps?.disconnect()
    }
  }, [])

  // On a phone the card is always a bottom sheet (#251 DA-45): docked at the
  // top it sat on the page title and its tabs whenever the anchor was low on
  // the screen. A wider screen still takes the half the anchor is not in.
  const atTop = half === 'lower' && !phone
  const edge = atTop ? 'top-14' : 'bottom-[68px]'
  // From `sm` up the card is 16rem wide and sits on the anchor's side; below
  // it the card spans the width, so the side does not matter.
  const side = column === 'left' ? 'sm:left-4 sm:right-auto' : 'sm:left-auto sm:right-4'

  return createPortal(
    <div
      role="note"
      aria-label="Demo hint"
      data-coach-docked="true"
      data-coach-edge={atTop ? 'top' : 'bottom'}
      data-coach-side={column}
      data-collapsed={collapsed ? 'true' : undefined}
      className={`group/coach fixed ${edge} left-3 right-3 z-50 rounded-lg border p-3 text-left shadow-lg motion-reduce:animate-none ${side} sm:w-64`}
      style={{
        background: 'var(--bg-elevated)',
        borderColor: 'var(--accent)',
        ...(atTop ? { top } : {}),
      }}
    >
      <button
        type="button"
        onClick={() => setCollapsed((value) => !value)}
        aria-expanded={!collapsed}
        aria-label={collapsed ? 'Expand demo hint' : 'Collapse demo hint'}
        className="absolute right-1.5 top-1.5 flex h-7 w-7 items-center justify-center rounded-sm transition-colors hover:bg-[var(--surface-hover)] text-fg-secondary"
      >
        {/* The chevron points the way the card will move: a bottom card
            collapses downwards and expands upwards, a top card the reverse. */}
        {collapsed === atTop ? (
          <ChevronDown className="h-3.5 w-3.5" />
        ) : (
          <ChevronUp className="h-3.5 w-3.5" />
        )}
      </button>
      {/* Collapsed keeps the card in the tree — the anchor's aria-describedby
          still points at its instruction — and shows only the step line. */}
      <div className="pr-8">{children}</div>
    </div>,
    document.body,
  )
}

/** The card body, identical whether it is anchored or docked. */
function CoachCard({
  position,
  total,
  instruction,
  instructionId,
  onMute,
}: {
  position: number
  total: number
  instruction: string
  instructionId: string
  onMute: () => void
}) {
  return (
    <>
      <p
        className="micro-label text-fg-tertiary"
      >
        Step {position} of {total}
      </p>
      <p
        id={instructionId}
        className="mt-1 text-body-sm leading-[1.5] group-data-[collapsed=true]/coach:sr-only"
      >
        {instruction}
      </p>
      <button
        type="button"
        onClick={onMute}
        className="mt-2 rounded-sm px-1.5 py-0.5 text-caption font-medium transition-colors hover:bg-[var(--surface-hover)] group-data-[collapsed=true]/coach:hidden text-fg-secondary"
      >
        Hide hints
      </button>
    </>
  )
}
