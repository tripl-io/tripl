import { describe, expect, it } from 'vitest'
import {
  pickCardCorner,
  pickCorner,
  type Box,
  type GuideCorner,
  type GuideFrame,
  type WeightedBox,
} from './guidePlacement'

/** A 1000×800 content column with its top bar already cleared. */
const FRAME: GuideFrame = { left: 0, right: 1000, top: 50, bottom: 800 }
const CARD = { width: 300, height: 200 }
const PHONE_ORDER: readonly GuideCorner[] = ['bottom-left', 'top-left']

function box(left: number, top: number, width: number, height: number): Box {
  return { left, top, right: left + width, bottom: top + height }
}

/** Across the bottom of the frame: both bottom corners sit on it. */
const BOTTOM_ROW = box(0, 700, 1000, 30)
/** A control in the bottom-right corner. */
const BOTTOM_RIGHT_CONTROL = box(900, 700, 50, 30)
/** As big as the frame: a dialog that fills the screen. */
const EVERYTHING = box(0, 0, 1000, 800)

/**
 * The Events page at the top of its scroll: the title top-left, the toolbar
 * and its "New event" top-right, and a table's rows filling the bottom, a
 * name link in each row's first cell.
 */
function eventsPage(): WeightedBox[] {
  const page: WeightedBox[] = [{ box: box(20, 70, 160, 30), weight: 1 }]
  for (let x = 720; x < 1000; x += 70) page.push({ box: box(x, 220, 60, 28), weight: 3 })
  for (let y = 610; y < 800; y += 28) {
    page.push({ box: box(20, y, 160, 20), weight: 3 })
    for (let x = 200; x < 1000; x += 100) page.push({ box: box(x, y, 90, 20), weight: 1 })
  }
  return page
}

describe('pickCardCorner', () => {
  it('keeps the card off the top of a busy page, where no scroll uncovers the title', () => {
    // Counted corner by corner, the title alone weighs least: the card took
    // the top and covered it, as it covered "New event" on Events.
    expect(pickCorner(FRAME, CARD, [], eventsPage())).toBe('top-left')
    // At the bottom the page can be scrolled out from under it; the emptier
    // bottom corner, away from the rows' name links.
    expect(pickCardCorner(FRAME, CARD, [], eventsPage())).toBe('bottom-right')
  })

  it('goes to the top only while the control sits in both bottom corners', () => {
    expect(pickCardCorner(FRAME, CARD, [BOTTOM_RIGHT_CONTROL], [])).toBe('bottom-left')
    expect(pickCardCorner(FRAME, CARD, [BOTTOM_ROW], [])).toBe('top-right')
  })

  it('leaves a top corner for a bottom one once that is clear again', () => {
    expect(pickCardCorner(FRAME, CARD, [], [], undefined, 'top-right')).toBe('bottom-right')
  })

  it('keeps a top corner while the control still holds the bottom', () => {
    // The top-left would be emptier, but the card is not chased there.
    const busy = [{ box: box(800, 100, 100, 30), weight: 3 }]

    expect(pickCardCorner(FRAME, CARD, [BOTTOM_ROW], busy, undefined, 'top-right')).toBe('top-right')
  })

  it('keeps a bottom corner while it stays clear, however busy', () => {
    const busy = [{ box: box(50, 700, 100, 30), weight: 3 }]

    expect(pickCardCorner(FRAME, CARD, [], busy)).toBe('bottom-right')
    expect(pickCardCorner(FRAME, CARD, [], busy, undefined, 'bottom-left')).toBe('bottom-left')
  })

  it('leaves the corner the control moves into', () => {
    expect(pickCardCorner(FRAME, CARD, [BOTTOM_RIGHT_CONTROL], [], undefined, 'bottom-right')).toBe(
      'bottom-left',
    )
  })

  it('docks a phone card at the bottom over a busy page, at the top only for the control', () => {
    const busyBottom = [{ box: box(20, 700, 200, 40), weight: 3 }]

    expect(pickCardCorner(FRAME, CARD, [], busyBottom, PHONE_ORDER)).toBe('bottom-left')
    expect(pickCardCorner(FRAME, CARD, [BOTTOM_ROW], [], PHONE_ORDER)).toBe('top-left')
  })

  it('is null when no corner is clear', () => {
    expect(pickCardCorner(FRAME, CARD, [EVERYTHING], [])).toBeNull()
    expect(pickCardCorner(FRAME, CARD, [EVERYTHING], [], PHONE_ORDER, 'bottom-left')).toBeNull()
  })
})
