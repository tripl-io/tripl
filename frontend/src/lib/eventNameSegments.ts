/**
 * Splitting a colon-namespaced event name for display.
 *
 * Lives in lib/ so the shared `EventName` component does not import a page
 * module: components/event-name.tsx used to reach into pages/events/utils.ts,
 * which pulled page code into every chunk that only needed the primitive and
 * invited circular imports.
 */

export const NAME_SEGMENT_SEPARATOR = ':'

export type NameSegment = { text: string; empty: boolean }

// An empty colon-segment can arrive as "" or as the serialized sentinel "0".
// Kept in sync with ReconciliationPage's DeadEventName so the events list and
// the reconciliation list render glitchy names identically.
function isEmptyNameSegment(segment: string): boolean {
  return segment === '' || segment === '0'
}

/**
 * Split a colon-namespaced event name into segments, but only when one of the
 * segments is empty (e.g. "spot::services"). A bare "::" reads as a rendering
 * bug, so the empty piece is surfaced as an intentional placeholder. Returns
 * `null` for ordinary names so they render unchanged.
 */
export function splitEventName(name: string): NameSegment[] | null {
  const parts = name.split(NAME_SEGMENT_SEPARATOR)
  if (parts.length === 1 || !parts.some(isEmptyNameSegment)) return null
  return parts.map((p) => ({ text: p, empty: isEmptyNameSegment(p) }))
}
