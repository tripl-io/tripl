/**
 * Where a coach anchor can actually be seen.
 *
 * The ring and the scroll-into-view both used the WINDOW as the only frame. An
 * anchor inside a scroll container — a row action in an `overflow-x-auto`
 * table wrapper on a 375px screen, a list in a scrolling panel — can be clipped
 * by that container while still sitting inside the window, and the fixed ring
 * went on floating over whatever was drawn there instead.
 */

export interface Box {
  top: number
  left: number
  right: number
  bottom: number
}

function clips(element: Element): boolean {
  const style = window.getComputedStyle(element)
  return /(auto|scroll|hidden|clip)/.test(`${style.overflow} ${style.overflowX} ${style.overflowY}`)
}

/** The anchor's ancestors that clip their content, nearest first. */
export function clippingAncestors(anchor: Element): Element[] {
  const found: Element[] = []
  for (let node = anchor.parentElement; node && node !== document.body; node = node.parentElement) {
    if (clips(node)) found.push(node)
  }
  return found
}

export function intersect(a: Box, b: Box): Box | null {
  const box = {
    top: Math.max(a.top, b.top),
    left: Math.max(a.left, b.left),
    right: Math.min(a.right, b.right),
    bottom: Math.min(a.bottom, b.bottom),
  }
  return box.right > box.left && box.bottom > box.top ? box : null
}

/** The part of the viewport the anchor can show through, or null if none. */
export function visibleFrame(ancestors: readonly Element[]): Box | null {
  let frame: Box | null = { top: 0, left: 0, right: window.innerWidth, bottom: window.innerHeight }
  for (const ancestor of ancestors) {
    if (!frame) return null
    const rect = ancestor.getBoundingClientRect()
    frame = intersect(frame, rect)
  }
  return frame
}

/**
 * Whether one axis of an anchor, `start`..`end`, can be seen through the same
 * axis of its frame. An anchor larger than the frame on that axis can never
 * fit — a table row wider than its `overflow-x-auto` wrapper on a phone — so
 * there it counts as seen whenever the two overlap; asking it to fit made such
 * an anchor scroll the page on every step.
 */
function axisSeen(start: number, end: number, frameStart: number, frameEnd: number): boolean {
  if (start >= frameStart && end <= frameEnd) return true
  return end - start > frameEnd - frameStart && end > frameStart && start < frameEnd
}

/**
 * Which axes of `rect` a scroll must bring into `frame` — decided per axis, so
 * an anchor clipped only sideways does not also jump the page vertically. A
 * null frame (nothing of the anchor can show) needs both.
 */
export function clippedAxes(
  frame: Box | null,
  rect: Box,
): { vertical: boolean; horizontal: boolean } {
  if (!frame) return { vertical: true, horizontal: true }
  return {
    vertical: !axisSeen(rect.top, rect.bottom, frame.top, frame.bottom),
    horizontal: !axisSeen(rect.left, rect.right, frame.left, frame.right),
  }
}
