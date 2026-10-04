/**
 * Below md the events table stops being a 17-column strip a phone scrolls
 * sideways through behind a pinned name: every row becomes a card — handle,
 * checkbox and event name on the first line, type / status / signal / 48h
 * volume / tags wrapping under it.
 *
 * All of it is `max-md:` classes, so nothing changes from md up and the row
 * keeps one DOM (and one measured height for the virtualizer, which sizes rows
 * with `measureElement`). Several carry `!`: the `.tripl-table` rules in
 * index.css are unlayered, so they beat plain Tailwind utilities.
 */

/** On the `<table>`: block layout, so rows can be flex cards. */
export const PHONE_TABLE = 'max-md:block max-md:[&>thead]:block max-md:[&>tbody]:block'

/**
 * On the header row: only the reorder spacer and the pinned cells (select-all
 * and "Event") stay; the column headers and their filters have no column to
 * sit over in a card. The empty reorder header stays while the cards carry a
 * 32px drag handle, so select-all lines up over the row checkboxes (the table
 * hides it when they do not); the gap and padding match PHONE_ROW for the same
 * reason.
 */
export const PHONE_HEADER_ROW =
  'max-md:flex max-md:items-center max-md:gap-x-2 max-md:px-2 max-md:py-2 '
  + 'max-md:[&>th]:static! max-md:[&>th]:p-0! '
  + 'max-md:[&>th:not(.tripl-pin-l):not(:first-child)]:hidden '
  // No bordered "EVENT" box and orphan rule over the cards: the bar is the
  // select-all checkbox alone.
  + 'max-md:[&>th]:border-0! max-md:[&>th[data-pinned=true]]:hidden'

/**
 * On the select cells (row checkbox and select-all). Their desktop `w-10`
 * leaves 24px of air after a 16px checkbox, which on a card indented the name
 * as if a column still sat between them.
 */
export const PHONE_SELECT_CELL = 'max-md:w-auto'

/**
 * On the select-all header cell: the checkbox and a "Select all" caption on
 * one line, so the bar says what it is instead of reading as an empty header
 * over the cards.
 */
export const PHONE_SELECT_ALL_HEAD = `${PHONE_SELECT_CELL} max-md:flex max-md:items-center max-md:gap-2`

/**
 * On the "Select all" caption: phone only, the desktop header has columns. The
 * 2px drop matches the one TableHead gives the checkbox beside it.
 */
export const PHONE_SELECT_ALL_CAPTION = 'cursor-pointer translate-y-[2px] md:hidden'

/** On each event row. */
export const PHONE_ROW =
  'max-md:flex max-md:h-auto! max-md:flex-wrap max-md:items-center max-md:gap-x-2 max-md:gap-y-1.5 '
  + 'max-md:border-b max-md:px-2 max-md:py-2.5 '
  + 'max-md:[&>td]:static! max-md:[&>td]:border-0! max-md:[&>td]:p-0! '
  // A cell with nothing in it (the drag handle's when the list cannot be
  // reordered, the checkbox's for a viewer) would still take its width and a
  // gap, and push the name right of the checkbox above it.
  + 'max-md:[&>td:empty]:hidden '
  // The pinned cells keep their opaque sticky fill on desktop; inside a card
  // it painted darker boxes behind the checkbox and name.
  + 'max-md:[&>td.tripl-pin-l]:bg-transparent! '
  // index.css skips painting off-screen rows at the desktop `--row-h`; a card
  // skipped that way measures a desktop row tall until it is painted.
  + 'max-md:[content-visibility:visible]! max-md:[contain-intrinsic-size:none]!'

/**
 * On the Signal cell of a row with no open signal. Its desktop "—" says "this
 * column is quiet"; a card has no column, so the dash sat alone at the start of
 * the chip line and read as a stray mark. A row with a signal keeps its chip.
 */
export const PHONE_QUIET_CELL = 'max-md:hidden'

/**
 * On the event-name cell: it takes the rest of the first line (handle and
 * checkbox are at most 32px + 16px), which is what pushes every later cell
 * onto the next one.
 */
export const PHONE_NAME_CELL = 'max-md:min-w-0 max-md:flex-1 max-md:basis-[calc(100%-6rem)]'

/** On the name cell's content box, whose inline max-width is a desktop cap. */
export const PHONE_NAME_CONTENT = 'max-md:max-w-none!'

/**
 * Columns a card does without: they read only under a column header (reviewed,
 * last seen, owner, custom field and meta values). Δ 24h stays, beside the
 * count.
 */
export const PHONE_DROPPED_CELL = 'max-md:hidden'

/**
 * A full-width row that is not an event (loading, empty state). `h-auto!`: the
 * block row kept the desktop `--row-h` from index.css, and the card's
 * `overflow-hidden` clipped "No events yet" to a 28px strip.
 */
export const PHONE_FULL_ROW = 'max-md:block max-md:h-auto! max-md:[&>td]:block'

/** The viewport the `max-md:` classes above apply to (Tailwind's md is 48rem). */
export const PHONE_CARD_QUERY = '(max-width: 47.99rem)'

/**
 * What an unrendered phone card is assumed to measure: two to three lines
 * (name, then the chips) plus 20px of padding. Rows are measured once
 * rendered; this only sizes the placeholders the scroll spacer is built from,
 * which at the desktop row height left the spacer a third of its real length
 * and the scroll thumb jumping as pages loaded.
 */
export const PHONE_CARD_HEIGHT_ESTIMATE = 76
