import type { ChipTone } from '@/components/primitives/chip-variants'
import { formatRelativeTime } from '@/lib/datetime'
import type { SourceFreshness, SourceFreshnessItem, SourceFreshnessStatus } from '@/types'

/**
 * Source freshness, as the UI reads it (F16, #269). The status itself is
 * computed server-side (`services/source_freshness.py`); this file only words
 * it, so every chip and notice says one thing about one delay.
 */

const MINUTE = 60
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

/** "45m", "7h", "3d": the lag in the one unit a reader needs. */
export function formatLag(seconds: number): string {
  const safe = Math.max(0, seconds)
  if (safe < HOUR) return `${Math.max(1, Math.round(safe / MINUTE))}m`
  if (safe < 2 * DAY) return `${Math.round(safe / HOUR)}h`
  return `${Math.round(safe / DAY)}d`
}

/**
 * Whether the metrics worker is holding this scan's drop-direction volume
 * signals: while a source is late, or its scan overdue, a drop is the delay
 * and not the product, so no drop anomaly is raised until data arrives.
 */
export function isHoldingSignals(freshness: SourceFreshness | null | undefined): boolean {
  return freshness?.status === 'late' || freshness?.status === 'overdue'
}

/**
 * How long the source may lag before it counts as late, in seconds — from the
 * two stamps the server sends. Null when either is missing.
 */
export function expectedWithinSeconds(freshness: SourceFreshness): number | null {
  if (!freshness.expected_by || !freshness.last_event_at) return null
  const expectedBy = Date.parse(freshness.expected_by)
  const lastEvent = Date.parse(freshness.last_event_at)
  if (!Number.isFinite(expectedBy) || !Number.isFinite(lastEvent)) return null
  const within = (expectedBy - lastEvent) / 1000
  return within > 0 ? within : null
}

export interface FreshnessChipContent {
  label: string
  tone: ChipTone
  /** The longer sentence, for a tooltip / accessible name. */
  description: string
}

/**
 * What a freshness chip says, or null when it should say nothing: a fresh
 * source is the quiet default, and `unknown` (a manual scan, no collection yet)
 * is not a problem worth a pill.
 */
export function freshnessChipContent(
  freshness: SourceFreshness | null | undefined,
  now: number = Date.now(),
): FreshnessChipContent | null {
  if (!freshness) return null
  switch (freshness.status) {
    case 'late': {
      const lag = freshness.lag_seconds
      const within = expectedWithinSeconds(freshness)
      const newest = lag != null ? `Newest event ${formatLag(lag)} ago` : 'No new events'
      const expected = within != null ? ` (expected within ${formatLag(within)})` : ''
      return {
        label: lag != null ? `Data late · ${formatLag(lag)}` : 'Data late',
        tone: 'warning',
        description: `${newest}${expected}. Drop signals are held until data arrives.`,
      }
    }
    case 'overdue': {
      const last = freshness.last_collection_at
        ? `Last collection ${formatRelativeTime(freshness.last_collection_at, now)}`
        : 'No collection has finished'
      return {
        label: 'Scan overdue',
        tone: 'danger',
        description: `${last}; the scan is not running on schedule. Drop signals are held until it does.`,
      }
    }
    case 'fresh':
    case 'unknown':
      return null
  }
}

const SEVERITY: Record<SourceFreshnessStatus, number> = {
  unknown: 0,
  fresh: 1,
  late: 2,
  overdue: 3,
}

/** The most severe of several freshness readings (overdue > late > fresh > unknown). */
export function worstFreshness(
  readings: readonly (SourceFreshness | null | undefined)[],
): SourceFreshness | null {
  let worst: SourceFreshness | null = null
  for (const reading of readings) {
    if (!reading) continue
    if (!worst || SEVERITY[reading.status] > SEVERITY[worst.status]) {
      worst = reading
    } else if (
      reading.status === worst.status
      && (reading.lag_seconds ?? 0) > (worst.lag_seconds ?? 0)
    ) {
      // Same status: the longer delay is the one worth naming.
      worst = reading
    }
  }
  return worst
}

/**
 * The scans holding signals, optionally narrowed to some scan ids (a
 * drilldown bound to one scan). `scanConfigIds` null / undefined means all.
 */
export function holdingItems(
  items: readonly SourceFreshnessItem[] | undefined,
  scanConfigIds?: readonly string[] | null,
): SourceFreshnessItem[] {
  if (!items) return []
  const wanted = scanConfigIds ? new Set(scanConfigIds) : null
  return items.filter(
    item => isHoldingSignals(item.freshness) && (!wanted || wanted.has(item.id)),
  )
}

/** Counts of the Overview's source health line. */
export function freshnessCounts(items: readonly SourceFreshnessItem[]): Record<SourceFreshnessStatus, number> {
  const counts: Record<SourceFreshnessStatus, number> = { fresh: 0, late: 0, overdue: 0, unknown: 0 }
  for (const item of items) counts[item.freshness.status] += 1
  return counts
}
