import { useCallback, useEffect, useState } from 'react'
import type { CSSProperties, RefCallback } from 'react'

/**
 * Measured x-offset of the pinned EVENT column, published on the `<table>` so
 * the pinned cells can position themselves without every row re-rendering.
 */
const PIN_LEFT_VAR = '--tripl-events-pin-left'
/** `none`, or the shadow the pinned cluster casts once content scrolls under it. */
const PIN_SHADOW_VAR = '--tripl-events-pin-shadow'

/** The left-sticky class from index.css. Every cell carrying it sticks at
 *  `left: 0`, so the cells left of EVENT that carry it are exactly the ones
 *  EVENT has to clear once the table scrolls. */
const PIN_CLASS = 'tripl-pin-l'

/** Checkbox width (40px) — the only cell stuck left of EVENT — used until the
 *  real measurement lands. NOT the EVENT column's natural offset (handle 34px +
 *  checkbox 40px): the reorder handle is not sticky, so it scrolls out from
 *  under EVENT and a 74px pin would leave a 34px hole in the cluster with
 *  un-pinned header labels and row text scrolling through it. */
const PIN_LEFT_FALLBACK_PX = 40

// Tinted from --fg rather than a fixed black so the edge reads in both themes.
const PIN_SHADOW = '8px 0 10px -8px color-mix(in srgb, var(--fg) 30%, transparent)'

/**
 * Inline overrides for the pinned EVENT cell.
 *
 * `.tripl-pin-l` (index.css) already supplies `position: sticky` plus the
 * opaque default/hover/selected backgrounds a sticky cell needs; what it cannot
 * supply is this column's `left`, because that depends on the rendered width of
 * the sticky checkbox column it butts against. Its `left: 0` and tight 2px/8px
 * gutters belong to the checkbox column, so both are overridden here.
 *
 * The EVENT *header* cell must also carry `data-pinned="true"` — that is what
 * the measurement below locates the column by, and what keeps a column that
 * never leaves out of the off-screen count.
 */
/**
 * Widest the pinned EVENT column's content may grow. Narrow viewports get a
 * share of the screen rather than a fixed width, so the pinned cluster never
 * covers the columns it is meant to label (EVT-7).
 */
export const PINNED_EVENT_CONTENT_MAX_WIDTH = 'min(360px, 45vw)'

export const PINNED_EVENT_CELL_STYLE: CSSProperties = {
  left: `var(${PIN_LEFT_VAR}, ${PIN_LEFT_FALLBACK_PX}px)`,
  boxShadow: `var(${PIN_SHADOW_VAR}, none)`,
  paddingLeft: 10,
  paddingRight: 10,
}

/**
 * Where the pinned EVENT column comes to rest, and where the pinned cluster
 * ends, both in the scroller's viewport space.
 *
 * `pinLeft` is the width of the cells that stay put in front of EVENT — the
 * `PIN_CLASS` ones, which index.css sticks at `left: 0` — NOT EVENT's own
 * `offsetLeft`. The reorder handle sits in that natural offset but is not
 * sticky, so it scrolls away; pinning EVENT behind it left the band between the
 * stuck checkbox and EVENT showing whatever un-pinned column happened to be
 * under it.
 */
export function measurePinnedGeometry(
  headerCells: readonly HTMLTableCellElement[],
  scrollLeft: number,
): { pinLeft: number; pinnedRight: number } {
  const pinnedIndex = headerCells.findIndex(cell => cell.dataset.pinned === 'true')
  const pinned = headerCells[pinnedIndex]
  if (!pinned) return { pinLeft: 0, pinnedRight: 0 }

  let pinLeft = 0
  for (const cell of headerCells.slice(0, pinnedIndex)) {
    if (cell.classList.contains(PIN_CLASS)) pinLeft += cell.offsetWidth
  }

  // The cluster stops moving once it is stuck, so its right edge is measured
  // from whichever is further right: EVENT's scrolled x, or its stuck x.
  return {
    pinLeft,
    pinnedRight: Math.max(pinLeft, pinned.offsetLeft - scrollLeft) + pinned.offsetWidth,
  }
}

/**
 * The element that scrolls the table sideways: the page's `.tripl-table-wrap`,
 * which since EV-3 scrolls both axes (`Table scroll={false}`), else the
 * table's parent — the `Table` primitive's own x-scroller where it keeps one.
 */
function horizontalScrollerOf(table: HTMLTableElement): HTMLElement | null {
  const wrap = table.closest('.tripl-table-wrap')
  return wrap instanceof HTMLElement ? wrap : table.parentElement
}

export function measureOverflow(table: HTMLTableElement): number {
  const scroller = horizontalScrollerOf(table)
  const headerCells = table.tHead?.rows[0]?.cells
  if (!scroller || !headerCells) return 0

  const { scrollLeft, clientWidth } = scroller
  table.style.setProperty(PIN_SHADOW_VAR, scrollLeft > 0 ? PIN_SHADOW : 'none')

  const cells = Array.from(headerCells)
  // Everything left of `pinnedRight` is covered by the pinned cluster, not readable.
  const { pinLeft, pinnedRight } = measurePinnedGeometry(cells, scrollLeft)
  // A zero measurement means the table has no layout yet (hidden pane, first
  // paint) — keep the CSS fallback rather than slamming EVENT against the edge.
  if (pinLeft > 0) table.style.setProperty(PIN_LEFT_VAR, `${pinLeft}px`)

  let offscreen = 0
  for (const cell of cells) {
    // Only headed data columns count. The reorder handle and the select-all
    // checkbox carry no label, and the pinned EVENT column never leaves.
    if (cell.dataset.pinned === 'true' || !cell.textContent?.trim()) continue
    // A header with no box is not off-screen but not shown at all: the phone
    // card layout drops the column headers (eventsPhoneCard.ts), and counting
    // them had the Columns chip claim "11 off-screen" on a table that no
    // longer scrolls sideways.
    if (cell.offsetWidth === 0) continue
    const start = cell.offsetLeft - scrollLeft
    if (start < pinnedRight || start + cell.offsetWidth > clientWidth) offscreen += 1
  }
  return offscreen
}

/**
 * Tracks how much of the events table is outside its horizontal viewport.
 *
 * At 1512px the catalog is ~1665px of columns in a ~902px pane, so 8 of 17
 * columns sit off-screen (10 at 1280px) with nothing but the scrollbar — far
 * below the fold of a tall list — to say so. The
 * count feeds the Columns chip; the scroll position drives the pinned columns'
 * shadow through a custom property, so crossing scrollLeft 0 repaints instead
 * of re-rendering every row.
 *
 * The scroller is the `.tripl-table-wrap` around the `<table>`: one box
 * scrolls both axes, so the sticky header and pinned columns resolve against
 * it (EV-3).
 */
export function useEventsTableOverflow(): {
  tableRef: RefCallback<HTMLTableElement>
  offscreenColumnCount: number
} {
  const [table, setTable] = useState<HTMLTableElement | null>(null)
  const [offscreenColumnCount, setOffscreenColumnCount] = useState(0)
  const tableRef = useCallback((node: HTMLTableElement | null) => setTable(node), [])

  useEffect(() => {
    const scroller = table ? horizontalScrollerOf(table) : null
    if (!table || !scroller) return

    let frame = 0
    const measure = () => {
      frame = 0
      setOffscreenColumnCount(measureOverflow(table))
    }
    const schedule = () => {
      if (frame) return
      frame = requestAnimationFrame(measure)
    }

    measure()
    scroller.addEventListener('scroll', schedule, { passive: true })
    // Both edges move the answer: the pane resizing, and columns being toggled
    // on/off (which changes the table's own width).
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule)
    observer?.observe(scroller)
    observer?.observe(table)

    return () => {
      if (frame) cancelAnimationFrame(frame)
      scroller.removeEventListener('scroll', schedule)
      observer?.disconnect()
    }
  }, [table])

  return { tableRef, offscreenColumnCount }
}
