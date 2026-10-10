// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { hasMoreBelow, revealCurrentRow } from './nav-scroll'

/** A box `height` px tall whose top edge is `top` px down the viewport. */
function box(top: number, height: number): DOMRect {
  return { x: 0, y: top, top, bottom: top + height, left: 0, right: 240, width: 240, height, toJSON: () => ({}) }
}

/**
 * A scroller of `height` px over `content` px of rows, scrolled to
 * `scrollTop`, with its own box at the top of the viewport. jsdom lays nothing
 * out, so the measurements are set by hand.
 */
function scroller({
  height,
  content,
  scrollTop = 0,
  paddingBottom = 0,
}: {
  height: number
  content: number
  scrollTop?: number
  paddingBottom?: number
}): HTMLElement {
  const el = document.createElement('div')
  el.style.paddingBottom = `${paddingBottom}px`
  Object.defineProperty(el, 'clientHeight', { value: height })
  Object.defineProperty(el, 'scrollHeight', { value: content })
  Object.defineProperty(el, 'scrollTop', { value: scrollTop, writable: true })
  el.getBoundingClientRect = () => box(0, height)
  document.body.append(el)
  return el
}

/** The current row, `top` px below the scroller's top edge as it stands, 30px tall. */
function currentRow(parent: HTMLElement, top: number): HTMLElement {
  const row = document.createElement('a')
  row.setAttribute('aria-current', 'page')
  row.getBoundingClientRect = () => box(top, 30)
  parent.append(row)
  return row
}

describe('hasMoreBelow', () => {
  it('does not count the scroller’s own bottom padding as rows below', () => {
    // The blank project at 1440×900: everything fits but the 8px padding,
    // and the fade used to land on the fully visible last row.
    expect(hasMoreBelow(scroller({ height: 600, content: 610, paddingBottom: 8 }))).toBe(false)
  })

  it('ignores a sliver, and says so once about half a row is hidden', () => {
    expect(hasMoreBelow(scroller({ height: 600, content: 610 }))).toBe(false)
    expect(hasMoreBelow(scroller({ height: 600, content: 630 }))).toBe(true)
  })

  it('says nothing once the list is scrolled to its end', () => {
    expect(hasMoreBelow(scroller({ height: 600, content: 900, scrollTop: 300 }))).toBe(false)
  })
})

describe('revealCurrentRow', () => {
  it('leaves a row in sight where it is, so a clicked row does not move', () => {
    const el = scroller({ height: 600, content: 1200 })
    currentRow(el, 200)

    revealCurrentRow(el, { bottomInset: 24, align: 'upper-third' })

    expect(el.scrollTop).toBe(0)
  })

  it('brings a row below the fold a third of the way down for a page opened by address', () => {
    const el = scroller({ height: 600, content: 1200 })
    currentRow(el, 900)

    revealCurrentRow(el, { bottomInset: 24, align: 'upper-third' })

    expect(el.scrollTop).toBe(900 - 200)
  })

  it('counts a row under the bottom fade as out of sight, and moves it just clear', () => {
    const el = scroller({ height: 600, content: 1200 })
    // Its bottom edge at 590: in the box, but under a 28px fade.
    currentRow(el, 560)

    revealCurrentRow(el, { bottomInset: 28 })

    expect(el.scrollTop).toBe(590 - (600 - 28))
  })

  it('scrolls back up to a row above the top edge', () => {
    const el = scroller({ height: 600, content: 1200, scrollTop: 500 })
    currentRow(el, -120)

    revealCurrentRow(el)

    expect(el.scrollTop).toBe(380)
  })

  it('does nothing when the list fits or no row is current', () => {
    const fits = scroller({ height: 600, content: 600 })
    currentRow(fits, 590)
    revealCurrentRow(fits)
    expect(fits.scrollTop).toBe(0)

    const none = scroller({ height: 600, content: 1200 })
    revealCurrentRow(none)
    expect(none.scrollTop).toBe(0)
  })
})
