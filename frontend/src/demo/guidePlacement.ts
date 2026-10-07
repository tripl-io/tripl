/**
 * Where the demo guide sits: a corner of the content column, never on the
 * control it is pointing at.
 *
 * The coach card used to open beside its control. Beside a control there is
 * always something else — a description, the next rows, a tab, the button
 * next to it — and visitors found the card covering text, and buttons covering
 * the card. The guide lives in a corner instead, the first one in
 * `CORNER_ORDER` that keeps clear of the control; the ring around the control
 * says where to act.
 *
 * Pure geometry, so the choice is testable without a browser.
 */

export interface Box {
  top: number
  left: number
  right: number
  bottom: number
}

export type GuideCorner = 'bottom-right' | 'bottom-left' | 'top-right' | 'top-left'

/** Bottom first, where pages end in empty space; right first, where reading ends. */
export const CORNER_ORDER: readonly GuideCorner[] = [
  'bottom-right',
  'bottom-left',
  'top-right',
  'top-left',
]

/** The space the guide may use: the content column, clear of the shell's bars. */
export interface GuideFrame {
  left: number
  right: number
  top: number
  bottom: number
}

export interface GuideSize {
  width: number
  height: number
}

/** How far the guide keeps from a control it must not cover. */
export const AVOID_MARGIN = 8

/** Something on the page the guide would rather not sit on, by how much it matters. */
export interface WeightedBox {
  box: Box
  weight: number
}

export function cornerBox(corner: GuideCorner, frame: GuideFrame, size: GuideSize): Box {
  const width = Math.min(size.width, Math.max(0, frame.right - frame.left))
  const left = corner.endsWith('right') ? frame.right - width : frame.left
  const top = corner.startsWith('bottom') ? frame.bottom - size.height : frame.top
  return { top, left, right: left + width, bottom: top + size.height }
}

function overlapArea(a: Box, b: Box): number {
  const width = Math.min(a.right, b.right) - Math.max(a.left, b.left)
  const height = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top)
  return width > 0 && height > 0 ? width * height : 0
}

function inflate(box: Box, by: number): Box {
  return { top: box.top - by, left: box.left - by, right: box.right + by, bottom: box.bottom + by }
}

/** How much of everything in `avoid` (with its margin) `box` would cover. */
export function coveredArea(box: Box, avoid: readonly Box[]): number {
  return avoid.reduce((sum, other) => sum + overlapArea(box, inflate(other, AVOID_MARGIN)), 0)
}

/**
 * The corner for the guide's card: clear of everything in `avoid` — the
 * control being coached, any open dialog or menu — and of those, the one
 * sitting on the least of the page itself (`busy`: its controls and its
 * text, weighted), first in `order` on a tie. Null when no corner is clear.
 */
export function pickCorner(
  frame: GuideFrame,
  size: GuideSize,
  avoid: readonly Box[],
  busy: readonly WeightedBox[],
  order: readonly GuideCorner[] = CORNER_ORDER,
): GuideCorner | null {
  let best: GuideCorner | null = null
  let bestScore = Number.POSITIVE_INFINITY
  for (const corner of order) {
    const box = cornerBox(corner, frame, size)
    if (coveredArea(box, avoid) > 0) continue
    let score = 0
    for (const item of busy) if (overlapArea(box, item.box) > 0) score += item.weight
    if (score < bestScore) {
      best = corner
      bestScore = score
    }
  }
  return best
}

/**
 * A card for beside a dialog too wide to leave the column a corner: the
 * widest of `widths` that fits a corner of `frame` (the whole screen — the
 * dialog's backdrop already covers the sidebar) clear of `avoid`. Testers
 * saw the guide fold to its face whenever a dialog opened, the step's words
 * gone just when the step moved into the dialog. Null when not even the
 * narrowest card fits.
 */
export function pickNarrowCorner(
  frame: GuideFrame,
  widths: readonly number[],
  heightAt: (width: number) => number,
  avoid: readonly Box[],
  order: readonly GuideCorner[] = CORNER_ORDER,
): { corner: GuideCorner; size: GuideSize } | null {
  for (const width of widths) {
    const size = { width, height: heightAt(width) }
    const corner = pickCorner(frame, size, avoid, [], order)
    if (corner) return { corner, size }
  }
  return null
}

/**
 * The first corner that keeps clear of everything in `avoid`; when every
 * corner touches something (a dialog as big as the screen), the one that
 * covers least of it.
 */
export function chooseCorner(
  frame: GuideFrame,
  size: GuideSize,
  avoid: readonly Box[],
  order: readonly GuideCorner[] = CORNER_ORDER,
): GuideCorner {
  const first = order[0] ?? 'bottom-right'
  if (avoid.length === 0) return first
  let best = first
  let bestArea = Number.POSITIVE_INFINITY
  for (const corner of order) {
    const area = coveredArea(cornerBox(corner, frame, size), avoid)
    if (area === 0) return corner
    if (area < bestArea) {
      best = corner
      bestArea = area
    }
  }
  return best
}
