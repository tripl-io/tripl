import type { MetricsGranularity } from '@/lib/metrics'

/**
 * The empty buckets that stretch a series out to the window the reader asked
 * for.
 *
 * A categorical x-axis spans only the buckets it is given, so a scope that
 * started reporting two days into a 30-day window drew those two days
 * edge-to-edge — "30 days" on the range picker, two days on the axis, and
 * nothing saying the other 28 were empty. Padding the ends with valueless
 * buckets keeps the axis honest without inventing zeros: the line simply does
 * not reach them.
 *
 * Only the ENDS are padded. Gaps inside the series are the server's to
 * densify, and it does, per scope; a client guess at an interior bucket would
 * disagree with it at every DST shift and month boundary.
 */

// Enough for the longest window at its finest allowed granularity
// (lib/metrics MAX_POINTS_PER_SERIES), so a bad pair of bounds cannot turn one
// render into an unbounded loop.
const MAX_PADDED_BUCKETS = 500

const STEP_MS: Record<Exclude<MetricsGranularity, 'month'>, number> = {
  '15min': 15 * 60 * 1000,
  hour: 60 * 60 * 1000,
  '6h': 6 * 60 * 60 * 1000,
  day: 24 * 60 * 60 * 1000,
  week: 7 * 24 * 60 * 60 * 1000,
}

function stepFrom(time: number, granularity: MetricsGranularity, direction: 1 | -1): number {
  if (granularity === 'month') {
    const date = new Date(time)
    return Date.UTC(
      date.getUTCFullYear(),
      date.getUTCMonth() + direction,
      date.getUTCDate(),
      date.getUTCHours(),
      date.getUTCMinutes(),
    )
  }
  return time + direction * STEP_MS[granularity]
}

export function windowPaddingBuckets(
  firstBucket: string,
  lastBucket: string,
  window: { from?: string; to?: string },
  granularity: MetricsGranularity,
): { before: string[]; after: string[] } {
  const first = Date.parse(firstBucket)
  const last = Date.parse(lastBucket)
  const from = window.from ? Date.parse(window.from) : Number.NaN
  const to = window.to ? Date.parse(window.to) : Number.NaN
  const before: string[] = []
  const after: string[] = []
  if (Number.isNaN(first) || Number.isNaN(last)) return { before, after }

  if (!Number.isNaN(from)) {
    // A bucket belongs to the window while any of it falls inside: its END is
    // past `from`.
    let start = stepFrom(first, granularity, -1)
    while (stepFrom(start, granularity, 1) > from && before.length < MAX_PADDED_BUCKETS) {
      before.unshift(new Date(start).toISOString())
      start = stepFrom(start, granularity, -1)
    }
  }
  if (!Number.isNaN(to)) {
    let start = stepFrom(last, granularity, 1)
    while (start < to && after.length < MAX_PADDED_BUCKETS) {
      after.push(new Date(start).toISOString())
      start = stepFrom(start, granularity, 1)
    }
  }
  return { before, after }
}
