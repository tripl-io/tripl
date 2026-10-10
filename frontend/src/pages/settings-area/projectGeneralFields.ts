import { useEffect, useState } from 'react'

/**
 * Helpers for Project settings › General, kept out of ProjectGeneralSection so
 * that file stays under the size limit: the transient "Saved" flag beside a
 * Save button and the danger-zone reset windows. The Timezone select's options
 * are in ./timeZones.
 */

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
