import { describe, expect, it } from 'vitest'
import {
  AVOID_MARGIN,
  chooseCorner,
  cornerBox,
  coveredArea,
  pickCorner,
  pickNarrowCorner,
  type Box,
  type GuideCorner,
  type GuideFrame,
} from './guidePlacement'

/** A 1000×800 content column with its top bar already cleared. */
const FRAME: GuideFrame = { left: 0, right: 1000, top: 50, bottom: 800 }
const CARD = { width: 300, height: 200 }
const PHONE_ORDER: readonly GuideCorner[] = ['bottom-left', 'top-left']

function box(left: number, top: number, width: number, height: number): Box {
  return { left, top, right: left + width, bottom: top + height }
}

/** Across the top of the frame: both top corners sit on it. */
const TOP_ROW = box(0, 100, 1000, 30)
/** Across the bottom of the frame: both bottom corners sit on it. */
const BOTTOM_ROW = box(0, 700, 1000, 30)
/** A control in the bottom-right corner. */
const BOTTOM_RIGHT_CONTROL = box(900, 700, 50, 30)
/** As big as the frame — a dialog that fills the screen. */
const EVERYTHING = box(0, 0, 1000, 800)

describe('cornerBox', () => {
  it('puts the card against each corner of the frame', () => {
    expect(cornerBox('bottom-right', FRAME, CARD)).toEqual({ left: 700, right: 1000, top: 600, bottom: 800 })
    expect(cornerBox('bottom-left', FRAME, CARD)).toEqual({ left: 0, right: 300, top: 600, bottom: 800 })
    expect(cornerBox('top-right', FRAME, CARD)).toEqual({ left: 700, right: 1000, top: 50, bottom: 250 })
    expect(cornerBox('top-left', FRAME, CARD)).toEqual({ left: 0, right: 300, top: 50, bottom: 250 })
  })

  it('never makes the card wider than the frame', () => {
    const narrow: GuideFrame = { left: 12, right: 212, top: 50, bottom: 800 }

    expect(cornerBox('bottom-right', narrow, CARD)).toEqual({ left: 12, right: 212, top: 600, bottom: 800 })
  })
})

describe('coveredArea', () => {
  const card = box(0, 0, 100, 100)

  it('is nothing for a box further away than the margin', () => {
    expect(coveredArea(card, [box(100 + AVOID_MARGIN, 0, 50, 50)])).toBe(0)
  })

  it('counts the margin around what must stay clear', () => {
    // Edge to edge: the box itself covers nothing, its margin does.
    expect(coveredArea(card, [box(100, 0, 50, 50)])).toBe(AVOID_MARGIN * (50 + AVOID_MARGIN))
  })

  it('adds up everything the card would sit on', () => {
    const inside = box(20, 20, 10, 10)

    expect(coveredArea(card, [inside, inside])).toBe(2 * (10 + 2 * AVOID_MARGIN) ** 2)
  })
})

describe('chooseCorner', () => {
  it('takes the first corner when there is nothing to avoid', () => {
    expect(chooseCorner(FRAME, CARD, [])).toBe('bottom-right')
    expect(chooseCorner(FRAME, CARD, [], PHONE_ORDER)).toBe('bottom-left')
  })

  it('skips the corners that touch what it must avoid', () => {
    expect(chooseCorner(FRAME, CARD, [BOTTOM_RIGHT_CONTROL])).toBe('bottom-left')
    expect(chooseCorner(FRAME, CARD, [BOTTOM_ROW])).toBe('top-right')
    expect(chooseCorner(FRAME, CARD, [BOTTOM_ROW], PHONE_ORDER)).toBe('top-left')
  })

  it('settles for the corner covering least when none is clear', () => {
    // The bottom half and the top-right corner are covered whole; the top-left
    // only by a small box at its edge.
    const avoid = [box(0, 400, 1000, 400), box(700, 50, 300, 200), box(0, 50, 20, 20)]

    expect(chooseCorner(FRAME, CARD, avoid)).toBe('top-left')
  })

  it('keeps to the order when every corner is covered alike', () => {
    expect(chooseCorner(FRAME, CARD, [EVERYTHING])).toBe('bottom-right')
  })
})

describe('pickCorner', () => {
  it('is null when no corner is clear', () => {
    expect(pickCorner(FRAME, CARD, [EVERYTHING], [])).toBeNull()
    expect(pickCorner(FRAME, CARD, [BOTTOM_ROW], [], PHONE_ORDER.slice(0, 1))).toBeNull()
  })

  it('takes the first corner clear of what it must avoid on an empty page', () => {
    expect(pickCorner(FRAME, CARD, [], [])).toBe('bottom-right')
    expect(pickCorner(FRAME, CARD, [BOTTOM_RIGHT_CONTROL], [])).toBe('bottom-left')
  })

  it('never takes a corner it must avoid, however busy the rest of the page', () => {
    const busy = [{ box: box(50, 700, 100, 30), weight: 3 }]

    // The top is taken and the bottom-right holds the coached control: the
    // bottom-left it is, button and all.
    expect(pickCorner(FRAME, CARD, [BOTTOM_RIGHT_CONTROL, TOP_ROW], busy)).toBe('bottom-left')
  })

  it('takes the clear corner sitting on the least of the page, by weight', () => {
    // Top corners off-limits. Two lines of text in the bottom-left weigh less
    // than one button in the bottom-right, though they are more things.
    const busy = [
      { box: box(800, 700, 80, 30), weight: 3 },
      { box: box(50, 650, 200, 20), weight: 1 },
      { box: box(50, 700, 200, 20), weight: 1 },
    ]

    expect(pickCorner(FRAME, CARD, [TOP_ROW], busy)).toBe('bottom-left')
  })

  it('counts nothing for a box beside the card rather than under it', () => {
    // Edge to edge with the bottom-right card: no margin for the page itself.
    const busy = [{ box: box(690, 700, 10, 30), weight: 3 }]

    expect(pickCorner(FRAME, CARD, [], busy)).toBe('bottom-right')
  })

  it('keeps to the order on a tie', () => {
    const busy = [
      { box: box(900, 700, 50, 30), weight: 3 },
      { box: box(50, 700, 50, 30), weight: 3 },
    ]

    expect(pickCorner(FRAME, CARD, [TOP_ROW], busy)).toBe('bottom-right')
    expect(pickCorner(FRAME, CARD, [TOP_ROW], busy, ['bottom-left', 'bottom-right'])).toBe('bottom-left')
  })
})

describe('pickNarrowCorner', () => {
  /** A 1440×900 screen, less its edge gap. */
  const SCREEN: GuideFrame = { left: 16, right: 1424, top: 16, bottom: 884 }
  /** Taller as it narrows: the same words wrap onto more lines. */
  const heightAt = (width: number) => Math.round((176 * 336) / width)
  const WIDTHS = [336, 288, 240]

  it('takes the widest card that clears a dialog', () => {
    // A 768px dialog leaves 336px either side: room for the middle width.
    const dialog = box(336, 45, 768, 810)

    expect(pickNarrowCorner(SCREEN, WIDTHS, heightAt, [dialog])).toEqual({
      corner: 'bottom-right',
      size: { width: 288, height: heightAt(288) },
    })
  })

  it('narrows to the last width beside the widest dialog', () => {
    // 896px wide: 272px either side, room for 240 and its margin.
    const dialog = box(272, 45, 896, 810)

    expect(pickNarrowCorner(SCREEN, WIDTHS, heightAt, [dialog])?.size.width).toBe(240)
  })

  it('is null when the dialog leaves no room even for the narrowest card', () => {
    // A phone-sized window's dialog: the whole screen.
    expect(pickNarrowCorner(SCREEN, WIDTHS, heightAt, [box(0, 0, 1440, 900)])).toBeNull()
  })
})
