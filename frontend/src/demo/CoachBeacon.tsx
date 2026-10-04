/**
 * The pulsing ring a coach mark draws around its anchor.
 *
 * An overlay, not a wrapper: the ring is a fixed-position div portalled to
 * document.body and sized from the anchor's client rect, so anchors whose DOM
 * position is load-bearing — the <tr> in ScanDetail — keep valid markup and
 * nothing in the page shifts. `pointer-events: none` (in .coach-ring) keeps the
 * exact control clickable through the ring.
 *
 * Clipped to what can actually be seen of the anchor: the viewport
 * and every scroll container around it. A row action scrolled out of an
 * `overflow-x-auto` table wrapper used to keep its ring floating over the page
 * beside the table, and a ring near the edge of a narrow screen ran past it.
 */

import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { clippingAncestors, intersect, visibleFrame, type Box } from './coachGeometry'

/** The ring sits this far outside the anchor on every edge. */
const RING_PAD = 2

/** Below the popover card (z-50) so the card is never occluded by its own ring. */
const RING_Z_INDEX = 49

/** The ring's box on screen, or null when none of it can be seen. */
function readRing(anchor: HTMLElement, ancestors: readonly Element[]): Box | null {
  const rect = anchor.getBoundingClientRect()
  // A 0x0 rect means the anchor is not laid out (hidden tab, display:none) —
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

function sameBox(a: Box | null, b: Box | null): boolean {
  if (a === null || b === null) return a === b
  return a.top === b.top && a.left === b.left && a.right === b.right && a.bottom === b.bottom
}

export function CoachBeacon({ anchor }: { anchor: HTMLElement }) {
  const [ring, setRing] = useState<Box | null>(() => readRing(anchor, clippingAncestors(anchor)))

  useEffect(() => {
    // Which ancestors clip is fixed for a mounted anchor; where they sit is not.
    const ancestors = clippingAncestors(anchor)
    const refresh = () => {
      setRing((prev) => {
        const next = readRing(anchor, ancestors)
        return sameBox(prev, next) ? prev : next
      })
    }
    refresh()
    // Older browsers / constrained webviews may lack ResizeObserver — degrade
    // to the resize + scroll listeners below instead of throwing.
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(refresh)
    observer?.observe(anchor)
    // The body too: layout shifts that MOVE the anchor without resizing it
    // (a diff row collapsing above it, form hints appearing) change the body's
    // height, so this catches them without any polling loop.
    observer?.observe(document.body)
    window.addEventListener('resize', refresh)
    // Capture: the anchor may live inside any scroll container, not just the page.
    window.addEventListener('scroll', refresh, { capture: true, passive: true })
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', refresh)
      window.removeEventListener('scroll', refresh, { capture: true })
    }
  }, [anchor])

  if (!ring) return null

  return createPortal(
    <div
      aria-hidden
      className="coach-ring"
      style={{
        position: 'fixed',
        top: ring.top,
        left: ring.left,
        width: ring.right - ring.left,
        height: ring.bottom - ring.top,
        zIndex: RING_Z_INDEX,
      }}
    />,
    document.body,
  )
}
