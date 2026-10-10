// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { measureOverflow, measurePinnedGeometry } from './useEventsTableOverflow'

/**
 * jsdom never lays anything out, so the offsets are stubbed to the real
 * catalog's geometry: reorder handle 34px (not sticky), checkbox 40px (sticky
 * at left: 0), EVENT 220px starting at its natural x of 74.
 */
function makeHeaderCell(options: {
  offsetLeft: number
  offsetWidth: number
  sticky?: boolean
  pinned?: boolean
  label?: string
}): HTMLTableCellElement {
  const cell = document.createElement('th')
  if (options.sticky) cell.className = 'tripl-pin-l'
  if (options.pinned) cell.dataset.pinned = 'true'
  if (options.label) cell.textContent = options.label
  Object.defineProperty(cell, 'offsetLeft', { value: options.offsetLeft })
  Object.defineProperty(cell, 'offsetWidth', { value: options.offsetWidth })
  return cell
}

function catalogHeaderCells(): HTMLTableCellElement[] {
  return [
    makeHeaderCell({ offsetLeft: 0, offsetWidth: 34 }),
    // The select-all cell holds the phone card's "Select all" caption.
    makeHeaderCell({ offsetLeft: 34, offsetWidth: 40, sticky: true, label: 'Select all' }),
    makeHeaderCell({ offsetLeft: 74, offsetWidth: 220, sticky: true, pinned: true, label: 'Event' }),
  ]
}

describe('measurePinnedGeometry', () => {
  it('pins EVENT to the stuck checkbox, not to its natural offset', () => {
    // The bug: pinning at EVENT's own offsetLeft (74 = handle 34 + checkbox 40)
    // parked it clear of a handle that is not sticky and scrolls away, leaving
    // the 34px band between the stuck checkbox and EVENT showing un-pinned cells.
    expect(measurePinnedGeometry(catalogHeaderCells(), 500).pinLeft).toBe(40)
  })

  it('tracks the cluster edge while it still scrolls, then stops at the stuck edge', () => {
    const cells = catalogHeaderCells()

    expect(measurePinnedGeometry(cells, 0).pinnedRight).toBe(294)
    expect(measurePinnedGeometry(cells, 500).pinnedRight).toBe(260)
  })

  it('reports no pinned cluster when no header claims the pin', () => {
    const cells = [makeHeaderCell({ offsetLeft: 0, offsetWidth: 34 })]

    expect(measurePinnedGeometry(cells, 0)).toEqual({ pinLeft: 0, pinnedRight: 0 })
  })
})

describe('measureOverflow', () => {
  /** A table in a 300px scroller, header cells stubbed like makeHeaderCell. */
  function tableWith(cells: HTMLTableCellElement[]): HTMLTableElement {
    const scroller = document.createElement('div')
    Object.defineProperty(scroller, 'clientWidth', { value: 300 })
    Object.defineProperty(scroller, 'scrollLeft', { value: 0 })
    const table = document.createElement('table')
    const row = table.createTHead().insertRow()
    for (const cell of cells) row.appendChild(cell)
    scroller.appendChild(table)
    return table
  }

  it('counts a laid-out column past the right edge', () => {
    const table = tableWith([
      ...catalogHeaderCells(),
      makeHeaderCell({ offsetLeft: 294, offsetWidth: 80, label: 'Status' }),
    ])

    expect(measureOverflow(table)).toBe(1)
  })

  it('does not count the sticky select-all cell for its caption', () => {
    // Its "Select all" text read as a column header left of the pinned edge,
    // so every table claimed one phantom column off-screen — on a phone, where
    // nothing scrolls sideways, that phantom was the whole count.
    const table = tableWith(catalogHeaderCells())

    expect(measureOverflow(table)).toBe(0)
  })

  it('does not count a header the phone card layout does not show', () => {
    // `display: none` leaves a cell with no box: offsetLeft and offsetWidth 0,
    // which read as "under the pinned cluster" and so "off-screen".
    const table = tableWith([
      ...catalogHeaderCells(),
      makeHeaderCell({ offsetLeft: 0, offsetWidth: 0, label: 'Status' }),
      makeHeaderCell({ offsetLeft: 0, offsetWidth: 0, label: 'Owner' }),
    ])

    expect(measureOverflow(table)).toBe(0)
  })
})
