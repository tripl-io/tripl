/**
 * The row-actions column the Plan catalogs (properties, meta fields,
 * relations) pin to the right edge, so a phone reader can act on a row without
 * first finding the sideways scroll. The opaque cell paints over the
 * scroller's right-edge fade, so it casts its own soft edge to the left:
 * whatever it covers reads as "continues underneath", not as cut off. A shadow,
 * not a border: a collapsed table's borders stay put when the cell sticks.
 *
 * For the head and every body cell of that column. A row whose other cells
 * are `align-top` adds it here too, so the icons stay on the first line.
 */
export const STICKY_ACTIONS_CLASS =
  'sticky right-0 bg-surface shadow-[-8px_0_8px_-8px_color-mix(in_srgb,var(--fg)_20%,transparent)]'
