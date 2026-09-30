/**
 * Shared bits of the property-list UI (F23): the event page's grid and the
 * Properties catalog page read the same entries from either side.
 */
import type { QueryClient } from '@tanstack/react-query'
import { branchEventsKey, branchPropertyEntriesKey, branchVariableOverridesKey } from './queryKeys'

/** The backend's default presence threshold (core/property_drift.py). */
export const DEFAULT_REQUIRED_PRESENCE = 0.95

/** "87%", "<1%", "100%"; an em dash when no scan measured it. */
export function formatPresence(rate: number | null | undefined): string {
  if (rate === null || rate === undefined) return '—'
  if (rate > 0 && rate < 0.01) return '<1%'
  if (rate < 1 && rate > 0.99) return '>99%'
  return `${Math.round(rate * 100)}%`
}

/** "95%" for a threshold, the default when the event has none of its own. */
export function formatThreshold(threshold: number | null | undefined): string {
  return `${Math.round((threshold ?? DEFAULT_REQUIRED_PRESENCE) * 1000) / 10}%`
}

/**
 * After any entry write: both sides of the list, the per-property overrides
 * (the same rows), and the events lists a `property` filter narrows.
 */
export function invalidatePropertyEntries(qc: QueryClient, slug: string, branchId: string | null) {
  void qc.invalidateQueries({ queryKey: branchPropertyEntriesKey(slug, branchId) })
  void qc.invalidateQueries({ queryKey: branchVariableOverridesKey(slug, branchId) })
  void qc.invalidateQueries({ queryKey: branchEventsKey(slug, branchId) })
}
