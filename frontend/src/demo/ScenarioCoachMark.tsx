/**
 * The coached demo scenario, on the surface itself.
 *
 * The strip tells the user what the next step is; this points at the control
 * that does it: a ring and a tag on the control ("Click here"), and the demo
 * guide in a corner of the page with the step's instruction and the exact
 * gesture. Product pages wrap their action element and name a step — they
 * learn nothing about scenario state, and a page that is not part of any
 * scenario, or a project that is not a demo, renders exactly what it always
 * rendered: the anchor gets no extra props, and no guide, ring or portal is
 * mounted.
 *
 * A mark also coaches as the way back. A step whose control lives behind
 * another one — the editor's fields behind the pencil on the event's row —
 * names that control's step as its `entry`. While the step's own control is
 * not on screen (the user left the editor, or reloaded the list), the entry's
 * mark points at the way back in, with the step's own words.
 *
 * Coaching is a hint, never a dialog. It never takes focus from the control
 * it points at, never traps it, and cannot be broken by Escape or a click
 * elsewhere — the scenario is not something the user can accidentally cancel.
 * The guide offers "Hide hints", which quiets the coaching for the browser
 * session (per project, in sessionStorage) while leaving the scenario running
 * and the strip coaching. The strip offers the same toggle, so a keyboard
 * user does not have to Tab through the whole page to the portalled guide to
 * reach it.
 *
 * One gate controls everything this file does: the guide, the ring, the
 * one-shot scroll to an off-screen anchor, and the presence report the strip
 * and the guide host read all key off the same `visible` — the guide and the
 * report off `speaking`, which only a covered way back turns off.
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
import { CoachBeacon, type CoachAlign, type CoachSide } from './CoachBeacon'
import { clippedAxes, clippingAncestors, visibleFrame } from './coachGeometry'
import { DemoGuide } from './DemoGuide'
import {
  entryPresenceKey,
  useCoachPresence,
  useDemoScenario,
  useDemoScenarioActions,
} from './demoScenarioContext'
import type { ScenarioStepId } from './scenarioModel'

interface ScenarioCoachMarkProps {
  /**
   * The step this action belongs to. The mark shows only while it is the
   * active one — or while the active step names it as its way back
   * (`ScenarioStep.entry`).
   */
  step: ScenarioStepId
  /** Extra page-local condition — e.g. only the scan config the scenario is watching. */
  when?: boolean
  /** Overrides for the step's own placement of the tag (`ScenarioStep.coach`). Rarely needed. */
  side?: CoachSide
  align?: CoachAlign
  emphasis?: 'ring' | 'none'
  children: ReactNode
}

/** Mounted but without a box (display:none on it or an ancestor). */
function isUnrendered(anchor: HTMLElement): boolean {
  return typeof anchor.checkVisibility === 'function' && !anchor.checkVisibility()
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

/** The box a non-element child (text, a fragment) needs to be pointed at — while coaching only. */
function AnchorWrapper({
  anchorRef,
  children,
}: {
  anchorRef: RefCallback<HTMLElement>
  children: ReactNode
}) {
  return <div ref={anchorRef}>{children}</div>
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
  const { active, activeChapter, chapters, step: activeStep, steps, hintsMuted } =
    useDemoScenario()
  const { muteHints } = useDemoScenarioActions()
  const { present, report } = useCoachPresence()
  const instructionId = useId()

  // The mark coaches its own step, or — as the way back — the step whose
  // surface its control opens, while that step's own control is not on screen.
  const own = activeStep.id === step
  const asEntry = !own && activeStep.entry === step && !present.has(activeStep.id)
  const coaching = active && !hintsMuted && when && (own || asEntry)
  const presenceKey = own ? step : entryPresenceKey(activeStep.id)

  // The anchor is state, not a ref: the beacon and the scroll effect must
  // re-run when the element appears, and a ref mutation would not tell them.
  const [anchorEl, setAnchorEl] = useState<HTMLElement | null>(null)
  const childRef = isValidElement(children)
    ? (children.props as { ref?: Ref<HTMLElement> }).ref
    : undefined
  const anchorRef = useMemo(() => mergeRefs(childRef, setAnchorEl), [childRef])

  // An anchor that is mounted but not rendered — inside a hidden tab panel or a
  // collapsed section (display:none) — has no box to point at. Such a mark
  // stands down like any other invisible one, so the guide's "not on screen"
  // note speaks instead. `checkVisibility` is absent in older engines (and
  // jsdom); there the anchor is taken as shown.
  //
  // Measured after the commit, never during render: a render reads the DOM the
  // previous commit left, so the render that reveals a panel still saw it
  // hidden, and the one that collapses a section still saw it laid out. A
  // layout effect re-measures after every commit, before paint, so neither
  // shows; the observer catches a box that appears or vanishes with no render.
  //
  // Keyed on `children` rather than run after every commit: a parent that
  // reveals or collapses the anchor re-renders this mark with a new element,
  // while this mark's own updates (the measurement included) keep the same
  // one — so the measurement can never feed itself.
  const [rendered, setRendered] = useState(false)
  useLayoutEffect(() => {
    const measure = () => setRendered(coaching && anchorEl !== null && !isUnrendered(anchorEl))
    measure()
  }, [anchorEl, children, coaching])
  useEffect(() => {
    if (!coaching || !anchorEl || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => setRendered(!isUnrendered(anchorEl)))
    observer.observe(anchorEl)
    return () => observer.disconnect()
  }, [coaching, anchorEl])
  const visible = coaching && rendered

  // The way back has done its job once what it opens covers it: the user is
  // in the dialog it leads to. If the step's own control is not there either
  // (its drift row already accepted), it stands down so the guide host can
  // say so — rather than sending the user round to the same pencil again.
  const [covered, setCovered] = useState(false)
  const speaking = visible && !(asEntry && covered)

  // Tell the strip and the guide host a mark for this step is on screen, so
  // they stand back while it coaches and speak up when it does not. A layout
  // effect: the host's own guide, and an entry mark that stands down for this
  // one, must never get a frame of their own.
  useLayoutEffect(() => {
    if (!speaking) return
    report(presenceKey, true)
    return () => report(presenceKey, false)
  }, [speaking, presenceKey, report])

  // The description reaches the control a screen reader user actually tabs to.
  // The clone below describes the anchor; a non-focusable wrapper
  // anchor passes it on to its first focusable descendant here, after the
  // commit, and takes it back when the mark goes quiet. Re-run on `children`
  // so a descendant that re-mounts gets it again.
  useLayoutEffect(() => {
    if (!speaking || !anchorEl) return
    const target = describedDescendant(anchorEl)
    if (!target || describedByIds(target).includes(instructionId)) return
    target.setAttribute('aria-describedby', [...describedByIds(target), instructionId].join(' '))
    return () => {
      const rest = describedByIds(target).filter((id) => id !== instructionId)
      if (rest.length > 0) target.setAttribute('aria-describedby', rest.join(' '))
      else target.removeAttribute('aria-describedby')
    }
  }, [speaking, anchorEl, instructionId, children])

  // Bring an anchor into view once per step when it is not fully visible:
  // coaching towards a control below the fold is coaching towards nothing.
  // "Visible" means inside every scroll container around it, not just inside
  // the window — a row action scrolled out of a table wrapper, or
  // half under the scrolling pane's edge, is not on screen either. One-shot,
  // so the user keeps control of their own scrolling afterwards.
  //
  // A frame later, not at once: a mark coaching as the way back stands down
  // the moment the step's own control reports on the same page (the branch
  // list beside the branch it opens), and the page must not have scrolled to
  // it in the meantime.
  const scrolledKeyRef = useRef<string | null>(null)
  useEffect(() => {
    if (!visible || !anchorEl) return
    if (scrolledKeyRef.current === presenceKey) return
    const frame = requestAnimationFrame(() => {
      scrolledKeyRef.current = presenceKey
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
    })
    return () => cancelAnimationFrame(frame)
  }, [visible, anchorEl, presenceKey])

  // A single element child keeps the same tree around it whether or not the
  // mark is coaching: only the anchor's props and the portalled siblings
  // change. Measuring, hiding — and the step completing, often from a click
  // on the anchor itself, or "Hide hints" — never remount the control, so a
  // keyboard user keeps focus on it.
  //
  // A non-element child (text, a fragment) cannot take the anchor's ref; it
  // needs a wrapper while coaching, and a non-demo page must not get one.
  if (!coaching && !isValidElement(children)) return <>{children}</>

  // Read only while coaching (the guide renders only then), when the gate
  // guarantees this is this mark's step or the step it is the way back to.
  // As the way back, the ring's tag and the gesture are this control's own
  // ("Click the pencil…"), and the words are the active step's.
  const markStep = own ? activeStep : steps.find((candidate) => candidate.id === step)
  const coach = markStep?.coach
  const ringed = (emphasis ?? coach?.emphasis ?? 'ring') === 'ring'
  const position = steps.findIndex((candidate) => candidate.id === activeStep.id) + 1
  const chapterTitle = chapters.find((chapter) => chapter.id === activeChapter)?.title

  // The instruction describes the control it points at, so a screen
  // reader user who tabs to it hears the step — the guide itself sits at the
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
          // Described only while the guide holding the instruction is up.
          'aria-describedby': speaking
            ? [ownDescribedBy, instructionId].filter(Boolean).join(' ')
            : ownDescribedBy,
        }
      : // Keep the ref on a hidden anchor, so the measurement above can see it
        // come back.
        { ref: anchorRef }

  return (
    <>
      {/* The clone stamps the exact click target with data-coach-target and
          captures it for the beacon; the beacon itself is an overlay, never a
          wrapper, so anchors with position-sensitive DOM (the <tr> in
          ScanDetail) stay valid. */}
      {isValidElement(children) ? (
        cloneElement(children as ReactElement<Record<string, unknown>>, anchorProps)
      ) : (
        <AnchorWrapper anchorRef={anchorRef}>{children}</AnchorWrapper>
      )}
      {visible && anchorEl && (
        <CoachBeacon
          anchor={anchorEl}
          tag={coach?.tag}
          side={side ?? coach?.side}
          align={align ?? coach?.align}
          ring={ringed}
          onCoveredChange={setCovered}
        />
      )}
      {speaking && anchorEl && (
        <>
          <DemoGuide
            // Folding is a choice about one step: the next one opens unfolded.
            key={activeStep.id}
            stepKey={activeStep.id}
            position={position}
            total={steps.length}
            chapter={chapterTitle}
            title={activeStep.title}
            instruction={activeStep.instruction}
            instructionId={instructionId}
            cue={markStep?.cue}
            avoid={anchorEl}
            onMute={muteHints}
          />
        </>
      )}
    </>
  )
}
