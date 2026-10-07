/**
 * The pulsing ring a coach mark draws around its control, and the tag beside
 * it that says what to do there: "Click here", "Type here", "Open this menu".
 * Visitors could not always tell which control a step meant — a ring alone
 * read as decoration — so the beacon names the gesture on the control itself.
 *
 * An overlay, not a wrapper: ring and tag are fixed-position elements
 * portalled to document.body and placed from the anchor's client rect, so
 * anchors whose DOM position is load-bearing — the <tr> in ScanDetail — keep
 * valid markup and nothing in the page shifts. Neither takes pointer events
 * (`.coach-ring`, `.coach-tag`), so the control stays clickable through them.
 *
 * Clipped to where the anchor can actually be seen: the viewport and every
 * scroll container around it. A row action scrolled out of an
 * `overflow-x-auto` table wrapper used to keep its ring floating over the
 * page beside the table.
 *
 * Above dialogs, so a control inside one is still pointed at — and gone while
 * anything else covers the control, so a dialog or menu opened over it never
 * gets a ring drawn on top of itself.
 *
 * It follows the control. A rule inserted above the coached one moved it down
 * with nothing resized, and the ring stayed on the empty spot; the beacon now
 * re-reads on any change to the document, coalesced to one read a frame.
 */

import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import { createPortal } from 'react-dom'
import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp } from 'lucide-react'
import { clippingAncestors, intersect, visibleFrame, type Box } from './coachGeometry'

/** How far the ring sits outside the anchor on every edge. */
const RING_PAD = 2

/** Between the ring and its tag. */
const TAG_GAP = 6

/** Kept between the tag and the edge of the screen. */
const EDGE_GAP = 4

export const DEFAULT_COACH_TAG = 'Click here'

export type CoachSide = 'top' | 'right' | 'bottom' | 'left'
export type CoachAlign = 'start' | 'center' | 'end'

const OPPOSITE: Record<CoachSide, CoachSide> = {
  top: 'bottom',
  bottom: 'top',
  left: 'right',
  right: 'left',
}

/** The arrow points from the tag at the control. */
const ARROW = { top: ArrowDown, bottom: ArrowUp, left: ArrowRight, right: ArrowLeft }

/** Which way the tag nudges: towards the control. */
const NUDGE: Record<CoachSide, { dx: string; dy: string }> = {
  top: { dx: '0px', dy: '3px' },
  bottom: { dx: '0px', dy: '-3px' },
  left: { dx: '3px', dy: '0px' },
  right: { dx: '-3px', dy: '0px' },
}

/** The ring's box on screen, or null when none of it can be seen. */
function readRing(anchor: HTMLElement, ancestors: readonly Element[]): Box | null {
  const rect = anchor.getBoundingClientRect()
  // A 0x0 rect means the anchor is not laid out (a hidden tab, display:none) —
  // a ring there would point at the page corner.
  if (rect.width === 0 && rect.height === 0) return null
  const frame = visibleFrame(ancestors)
  if (!frame) return null
  return intersect(frame, {
    top: rect.top - RING_PAD,
    left: rect.left - RING_PAD,
    right: rect.right + RING_PAD,
    bottom: rect.bottom + RING_PAD,
  })
}

/**
 * Whether something else is drawn over the middle of the control: a dialog's
 * overlay, a menu, a sticky header. The demo guide does not count — it is the
 * coaching, and it keeps clear of the control anyway. Engines without
 * hit-testing (jsdom) report nothing covered.
 */
function isCovered(anchor: HTMLElement, ring: Box): boolean {
  if (typeof document.elementFromPoint !== 'function') return false
  const hit = document.elementFromPoint((ring.left + ring.right) / 2, (ring.top + ring.bottom) / 2)
  if (!hit) return false
  // An ancestor answers for a control that takes no pointer events itself (a
  // disabled button).
  if (anchor.contains(hit) || hit.contains(anchor)) return false
  return hit.closest('[data-demo-guide]') === null
}

interface BeaconRead {
  ring: Box | null
  covered: boolean
}

function readBeacon(anchor: HTMLElement, ancestors: readonly Element[]): BeaconRead {
  const ring = readRing(anchor, ancestors)
  return { ring, covered: ring !== null && isCovered(anchor, ring) }
}

function sameBox(a: Box | null, b: Box | null): boolean {
  if (a === b) return true
  if (!a || !b) return false
  return a.top === b.top && a.left === b.left && a.right === b.right && a.bottom === b.bottom
}

function sameRead(a: BeaconRead, b: BeaconRead): boolean {
  return a.covered === b.covered && sameBox(a.ring, b.ring)
}

interface TagSize {
  width: number
  height: number
}

/** The tag's top-left corner on `side` of the ring, lined up by `align`. */
function tagAt(ring: Box, size: TagSize, side: CoachSide, align: CoachAlign) {
  const across =
    side === 'top' || side === 'bottom'
      ? align === 'start'
        ? ring.left
        : align === 'end'
          ? ring.right - size.width
          : (ring.left + ring.right - size.width) / 2
      : align === 'start'
        ? ring.top
        : align === 'end'
          ? ring.bottom - size.height
          : (ring.top + ring.bottom - size.height) / 2
  switch (side) {
    case 'top':
      return { top: ring.top - TAG_GAP - size.height, left: across }
    case 'bottom':
      return { top: ring.bottom + TAG_GAP, left: across }
    case 'left':
      return { top: across, left: ring.left - TAG_GAP - size.width }
    case 'right':
      return { top: across, left: ring.right + TAG_GAP }
  }
}

function fits(top: number, left: number, size: TagSize): boolean {
  return (
    top >= EDGE_GAP &&
    left >= EDGE_GAP &&
    top + size.height <= window.innerHeight - EDGE_GAP &&
    left + size.width <= window.innerWidth - EDGE_GAP
  )
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), Math.max(min, max))
}

/**
 * Where the tag goes: on the asked side when it fits on screen there, else on
 * the opposite one, else the asked side pushed back inside the screen.
 */
function placeTag(ring: Box, size: TagSize, side: CoachSide, align: CoachAlign) {
  for (const candidate of [side, OPPOSITE[side]]) {
    const at = tagAt(ring, size, candidate, align)
    if (fits(at.top, at.left, size)) return { ...at, side: candidate }
  }
  const at = tagAt(ring, size, side, align)
  return {
    top: clamp(at.top, EDGE_GAP, window.innerHeight - EDGE_GAP - size.height),
    left: clamp(at.left, EDGE_GAP, window.innerWidth - EDGE_GAP - size.width),
    side,
  }
}

export interface CoachBeaconProps {
  anchor: HTMLElement
  /** What to do at the control; "Click here" when not given. */
  tag?: string
  /** The side of the control the tag sits on. */
  side?: CoachSide
  align?: CoachAlign
  /** False leaves only the tag, without the pulsing ring. */
  ring?: boolean
  /** Told when something comes to cover the control, and when it leaves. */
  onCoveredChange?: (covered: boolean) => void
}

export function CoachBeacon({
  anchor,
  tag = DEFAULT_COACH_TAG,
  side = 'top',
  align = 'center',
  ring: showRing = true,
  onCoveredChange,
}: CoachBeaconProps) {
  const [read, setRead] = useState<BeaconRead>(() =>
    readBeacon(anchor, clippingAncestors(anchor)),
  )
  const tagRef = useRef<HTMLDivElement | null>(null)
  const [tagSize, setTagSize] = useState<TagSize>({ width: 0, height: 0 })

  useEffect(() => {
    onCoveredChange?.(read.covered)
  }, [read.covered, onCoveredChange])

  useEffect(() => {
    // Which ancestors clip is fixed for a mounted anchor; where they sit is not.
    const ancestors = clippingAncestors(anchor)
    let frame = 0
    const refresh = () => {
      frame = 0
      setRead((prev) => {
        const next = readBeacon(anchor, ancestors)
        return sameRead(prev, next) ? prev : next
      })
    }
    const schedule = () => {
      if (frame === 0) frame = requestAnimationFrame(refresh)
    }
    refresh()
    // Older browsers / constrained webviews may lack the observers — the
    // listeners below still keep the ring roughly in place.
    const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule)
    resize?.observe(anchor)
    resize?.observe(document.body)
    // Rows inserted above the control, a section expanding, a dialog opening
    // over it: none of them resize the anchor, all of them change the document.
    const mutations =
      typeof MutationObserver === 'undefined' ? null : new MutationObserver(schedule)
    mutations?.observe(document.body, { childList: true, subtree: true, attributes: true })
    window.addEventListener('resize', schedule)
    // Capture: the anchor may live inside any scroll container, not just the page.
    window.addEventListener('scroll', schedule, { capture: true, passive: true })
    // A dialog or panel that animates in or out ends somewhere new.
    window.addEventListener('transitionend', schedule, true)
    window.addEventListener('animationend', schedule, true)
    return () => {
      cancelAnimationFrame(frame)
      resize?.disconnect()
      mutations?.disconnect()
      window.removeEventListener('resize', schedule)
      window.removeEventListener('scroll', schedule, { capture: true })
      window.removeEventListener('transitionend', schedule, true)
      window.removeEventListener('animationend', schedule, true)
    }
  }, [anchor])

  const shown = read.ring !== null && !read.covered
  // The tag is measured where it renders, before paint, so it is placed by
  // its real size from the first frame it shows.
  useLayoutEffect(() => {
    const element = tagRef.current
    if (!shown || !element) return
    const next = { width: element.offsetWidth, height: element.offsetHeight }
    setTagSize((prev) =>
      prev.width === next.width && prev.height === next.height ? prev : next,
    )
  }, [shown, tag])

  if (!read.ring || read.covered) return null
  const ring = read.ring
  const placed = placeTag(ring, tagSize, side, align)
  const Arrow = ARROW[placed.side]
  const nudge = NUDGE[placed.side]
  // The arrow leads on the side facing the control.
  const arrowFirst = placed.side === 'right' || placed.side === 'bottom'
  const tagStyle: CSSProperties & Record<'--coach-tag-dx' | '--coach-tag-dy', string> = {
    top: placed.top,
    left: placed.left,
    '--coach-tag-dx': nudge.dx,
    '--coach-tag-dy': nudge.dy,
  }

  return createPortal(
    <>
      {showRing && (
        <div
          aria-hidden
          className="coach-ring fixed z-(--z-coach-ring)"
          style={{
            top: ring.top,
            left: ring.left,
            width: ring.right - ring.left,
            height: ring.bottom - ring.top,
          }}
        />
      )}
      <div
        ref={tagRef}
        aria-hidden
        data-coach-tag={placed.side}
        className="coach-tag fixed z-(--z-coach-ring) flex items-center gap-1 rounded-full px-2 py-0.5 text-caption font-semibold whitespace-nowrap shadow-md bg-accent-solid text-accent-solid-fg"
        style={tagStyle}
      >
        {arrowFirst && <Arrow className="size-3 shrink-0" aria-hidden="true" />}
        <span>{tag}</span>
        {!arrowFirst && <Arrow className="size-3 shrink-0" aria-hidden="true" />}
      </div>
    </>,
    document.body,
  )
}
