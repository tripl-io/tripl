import { useEffect, useState } from 'react'

/**
 * Helpers for Project settings › General, kept out of ProjectGeneralSection so
 * that file stays under the size limit: the Timezone select's options and the
 * transient "Saved" flag beside a Save button.
 */

/**
 * Whether the browser knows `zone` as an IANA time zone. The server validates
 * too (backend `validate_timezone`); this catches a typo before the round trip.
 */
export function isKnownTimeZone(zone: string): boolean {
  if (!zone) return false
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: zone })
    return true
  } catch {
    return false
  }
}

/**
 * The zones the Timezone select offers: UTC first, then every IANA zone the
 * browser knows. The field used to be free text, so `Europe/Moskow` could reach
 * the server — and the zone drives alert digest schedules. A stored
 * value the list lacks (an alias the browser does not enumerate) stays
 * selectable, so opening the page never silently changes it.
 */
export function timeZoneOptions(current: string): { value: string; label: string }[] {
  let zones: string[] = []
  try {
    zones = Intl.supportedValuesOf('timeZone')
  } catch {
    /* an engine without supportedValuesOf still offers UTC and the current value */
  }
  const ordered = ['UTC', ...zones.filter((zone) => zone !== 'UTC')]
  if (current && !ordered.includes(current)) {
    ordered.unshift(current)
  }
  return ordered.map((zone) => ({
    value: zone,
    label: zone === current && !isKnownTimeZone(zone) ? `${zone} (not recognised)` : zone,
  }))
}

/** How long "Saved" stays beside a Save button after a successful save. */
export const SAVED_FEEDBACK_MS = 2500

/**
 * True for a moment after `mark()` — the "Saved" confirmation a Save button
 * otherwise lacked: it only went disabled again.
 */
export function useTransientFlag(durationMs: number): [boolean, () => void, () => void] {
  const [shownAt, setShownAt] = useState<number | null>(null)
  useEffect(() => {
    if (shownAt === null) return
    const timer = window.setTimeout(() => setShownAt(null), durationMs)
    return () => window.clearTimeout(timer)
  }, [shownAt, durationMs])
  return [shownAt !== null, () => setShownAt(Date.now()), () => setShownAt(null)]
}

/** Danger-zone reset windows. Each maps to a `before` cutoff (older rows go). */
export const RESET_PERIODS: { value: string; label: string; days: number | null }[] = [
  { value: '7d', label: 'Older than 7 days', days: 7 },
  { value: '30d', label: 'Older than 30 days', days: 30 },
  { value: '90d', label: 'Older than 90 days', days: 90 },
  { value: 'all', label: 'All time', days: null },
]
