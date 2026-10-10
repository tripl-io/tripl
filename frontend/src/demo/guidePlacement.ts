/**
 * Where the demo guide sits: a corner of the content column, never on the
 * control it is pointing at.
 *
 * The coach card used to open beside its control. Beside a control there is
 * always something else — a description, the next rows, a tab, the button
 * next to it — and visitors found the card covering text, and buttons covering
 * the card. The guide lives in a corner instead, one that keeps clear of the
 * control (`pickCardCorner`); the ring around the control says where to act.
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

/** The guide's width from `sm` up; below it the guide spans the screen. */
export const GUIDE_WIDTH_PX = 336

/**
 * Narrower cards, for beside a dialog too wide to leave the column a corner.
 * At 1280px the 768px rule editor leaves room for 208, and an 896px dialog
 * (a property, a rule's replay) for 168: with 240 the narrowest, the guide
 * folded to its face in both just as the step moved into them.
 */
export const NARROW_WIDTHS_PX: readonly number[] = [GUIDE_WIDTH_PX, 288, 240, 208, 168]

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
 * The open card's corner: a bottom one whenever one is clear of `avoid`, a
 * top one only when neither is.
 *
 * While the card holds a bottom corner the column ends with room for it
 * (`--demo-guide-clearance`), so whatever it sits on there can be scrolled out
 * from under it. No room is made above a top corner, so the top of the page
 * stays under it — where a page keeps its title, its toolbar and its main
 * action. Chosen by how busy each corner was, the card took the top over a
 * table's many rows: on Events it covered "New event", and on a phone the
 * page's heading.
 *
 * Within a row, the corner sitting on the least of the page (`pickCorner`).
 * The corner the card is in (`held`) is kept while it stays clear, or it would
 * hop to the emptiest corner on every scroll — except that a top corner gives
 * way once a bottom one is clear again. Null when no corner is clear.
 */
export function pickCardCorner(
  frame: GuideFrame,
  size: GuideSize,
  avoid: readonly Box[],
  busy: readonly WeightedBox[],
  order: readonly GuideCorner[] = CORNER_ORDER,
  held: GuideCorner | null = null,
): GuideCorner | null {
  const rows = [
    order.filter((corner) => corner.startsWith('bottom')),
    order.filter((corner) => corner.startsWith('top')),
  ]
  for (const row of rows) {
    if (held && row.includes(held) && coveredArea(cornerBox(held, frame, size), avoid) === 0) {
      return held
    }
    const corner = pickCorner(frame, size, avoid, busy, row)
    if (corner) return corner
  }
  return null
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
 *
 * Never one on a control in `keepClear` — the ringed controls — while another
 * corner leaves them free: opened over a dialog with no corner to spare, the
 * card took the one with the dialog's Create in it, and the step could not be
 * finished without folding the card away again.
 */
export function chooseCorner(
  frame: GuideFrame,
  size: GuideSize,
  avoid: readonly Box[],
  order: readonly GuideCorner[] = CORNER_ORDER,
  keepClear: readonly Box[] = [],
): GuideCorner {
  const free = order.filter((corner) => coveredArea(cornerBox(corner, frame, size), keepClear) === 0)
  const candidates = free.length > 0 ? free : order
  const first = candidates[0] ?? 'bottom-right'
  if (avoid.length === 0) return first
  let best = first
  let bestArea = Number.POSITIVE_INFINITY
  for (const corner of candidates) {
    const area = coveredArea(cornerBox(corner, frame, size), avoid)
    if (area === 0) return corner
    if (area < bestArea) {
      best = corner
      bestArea = area
    }
  }
  return best
}
