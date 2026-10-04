import { computeWindowDelta, describeWindowDelta, type WindowDelta } from '@/pages/events/utils'
import type { EventMetricPoint } from '@/types'

export interface EventDetailStats {
  /** Volume of the last 24h before now; null when there is no series at all. */
  volume24h: number | null
  /** Percent change vs the 24h before that; null when it cannot be computed. */
  delta24h: number | null
  /** Either 24h half is short of its hours, so the delta compares less than a window. */
  partial: boolean
  /** The sentence the Events list puts on the same Δ cell. */
  deltaHint: string
}

/**
 * The event hero's "Volume · 24h" and "Δ · 24h".
 *
 * Read through the Events list's own `computeWindowDelta`, so both halves are
 * anchored on NOW and carry the same coverage verdict. The hero used to split on
 * the newest bucket with no coverage check, which compared a partial day with a
 * full one and printed a different Δ than the Events row for the same event at
 * the same moment — "+2%" here, "−3%*" there.
 */
export function computeEventStats(
  points: EventMetricPoint[] | undefined,
  now: number = Date.now(),
): EventDetailStats {
  const delta: WindowDelta = computeWindowDelta(points ?? [], now)
  return {
    volume24h: delta.status === 'no-series' ? null : delta.recentTotal,
    delta24h: delta.pct,
    partial: delta.partial,
    deltaHint: describeWindowDelta(delta),
  }
}
