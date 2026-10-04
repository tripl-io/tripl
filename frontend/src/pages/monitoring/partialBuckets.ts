import type { PartialWindow } from '@/components/ui/chart'
import { getBucketStart, type MetricsGranularity } from '@/lib/metrics'
import type { EventMetricPoint } from '@/types'

const HOUR_MS = 60 * 60 * 1000
const SPAN_MS: Record<Exclude<MetricsGranularity, 'month'>, number> = {
  '15min': 15 * 60 * 1000,
  hour: HOUR_MS,
  '6h': 6 * HOUR_MS,
  day: 24 * HOUR_MS,
  week: 7 * 24 * HOUR_MS,
}

/** The instant a display bucket starting at `start` ends (months are calendar months). */
function bucketEnd(start: string, granularity: MetricsGranularity): number {
  const ms = new Date(start).getTime()
  if (granularity === 'month') {
    const date = new Date(ms)
    return Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 1)
  }
  return ms + SPAN_MS[granularity]
}

/**
 * Which rolled-up buckets the raw series does not fully cover.
 *
 * At 30 days in days, or 90 days in weeks, the first display bucket starts
 * before the data does and the last one is still in progress, so their sums
 * are a fraction of a full bucket and drew as cliffs. `first` is the instant
 * the data starts inside the first bucket; `last` the instant it runs through
 * in the last one. Undefined at the native granularity, where every bucket is
 * one collection.
 */
export function partialWindow(
  raw: EventMetricPoint[],
  granularity: MetricsGranularity,
  nativeGranularity: MetricsGranularity | null,
): PartialWindow | undefined {
  if (!nativeGranularity || nativeGranularity === granularity || raw.length === 0) return undefined
  const firstRaw = raw[0]
  const lastRaw = raw[raw.length - 1]
  if (!firstRaw || !lastRaw) return undefined
  const result: PartialWindow = {}

  const firstStart = getBucketStart(firstRaw.bucket, granularity)
  if (new Date(firstRaw.bucket).getTime() > new Date(firstStart).getTime()) {
    result.first = firstRaw.bucket
  }

  const lastStart = getBucketStart(lastRaw.bucket, granularity)
  const lastThrough = bucketEnd(lastRaw.bucket, nativeGranularity)
  if (lastThrough < bucketEnd(lastStart, granularity)) {
    result.last = new Date(lastThrough).toISOString()
  }

  return result.first || result.last ? result : undefined
}
