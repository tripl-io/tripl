/**
 * The two long navigation lists — the app sidebar and the settings rail —
 * scroll inside their own box, with a fade over the bottom edge that says the
 * list goes on. These keep the fade honest and the current page in sight.
 */

/** How much of a list must be hidden before the fade says so: about half a row. */
export const HIDDEN_SLIVER_PX = 12

/**
 * Whether a scroller has rows hidden below its fold. Its own bottom padding
 * does not count: it is empty space, and counting it put the fade over a last
 * row that was fully visible, which then read as disabled.
 */
export function hasMoreBelow(scroller: HTMLElement): boolean {
  const padding = Number.parseFloat(getComputedStyle(scroller).paddingBottom) || 0
  const hidden = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight - padding
  return hidden > HIDDEN_SLIVER_PX
}

export interface RevealOptions {
  /** The height of the bottom fade: a row under it counts as out of sight. */
  bottomInset?: number
  /**
   * `nearest` scrolls just far enough, so a row clicked half under the fade
   * moves a little and not out from under the pointer. `upper-third` puts the
   * row a third of the way down, with its neighbours below it in view: for a
   * page opened by its address, where nothing on the list was clicked.
   */
  align?: 'nearest' | 'upper-third'
}

/**
 * Scrolls `scroller` — its own scroll position, never the page's — until the
 * row marked `aria-current="page"` in it is in sight. Does nothing when that
 * row is already in sight, when there is none, or when the list fits.
 */
export function revealCurrentRow(
  scroller: HTMLElement,
  { bottomInset = 0, align = 'nearest' }: RevealOptions = {},
): void {
  if (scroller.scrollHeight <= scroller.clientHeight) return
  const row = scroller.querySelector<HTMLElement>('[aria-current="page"]')
  if (!row) return
  const box = scroller.getBoundingClientRect()
  const rect = row.getBoundingClientRect()
  const visibleBottom = box.bottom - bottomInset
  const above = rect.top < box.top
  const below = rect.bottom > visibleBottom
  if (!above && !below) return
  if (align === 'upper-third') {
    scroller.scrollTop += rect.top - box.top - scroller.clientHeight / 3
  } else if (above) {
    scroller.scrollTop -= box.top - rect.top
  } else {
    scroller.scrollTop += rect.bottom - visibleBottom
  }
}
