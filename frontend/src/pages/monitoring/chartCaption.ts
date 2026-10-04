import { formatDateTime, formatRelativeTime } from '@/lib/datetime'
import type { MetricsGranularity } from '@/lib/metrics'

// The collection cadence in words, for the chart caption.
const CADENCE_LABEL: Record<MetricsGranularity, string> = {
  '15min': 'Every 15 minutes',
  hour: 'Hourly',
  '6h': 'Every 6 hours',
  day: 'Daily',
  week: 'Weekly',
  month: 'Monthly',
}

// The scheduler checks every five minutes, so a next run inside that window
// (or already past) is "due now", not a clock time a minute from now.
const DUE_NOW_WINDOW_MS = 5 * 60 * 1000

/**
 * The line under the Volume chart: the cadence, the newest bucket, and when the
 * scan last collected and next will (L4) — "Hourly · newest bucket 12m ago ·
 * collected 8m ago · next Sep 26, 3:00 PM". Parts the response does not carry
 * are left out rather than guessed.
 */
export function chartCaption({
  cadence,
  lastBucket,
  lastCollectedAt,
  nextCollectionAt,
  now = Date.now(),
}: {
  cadence: MetricsGranularity
  lastBucket?: string | null
  lastCollectedAt?: string | null
  nextCollectionAt?: string | null
  now?: number
}): string {
  const parts = [CADENCE_LABEL[cadence]]
  if (lastBucket) parts.push(`newest bucket ${formatRelativeTime(lastBucket, now)}`)
  if (lastCollectedAt) parts.push(`collected ${formatRelativeTime(lastCollectedAt, now)}`)
  if (nextCollectionAt) {
    const next = Date.parse(nextCollectionAt)
    if (!Number.isNaN(next)) {
      parts.push(
        next - now <= DUE_NOW_WINDOW_MS
          ? 'next collection due now'
          : `next ${formatDateTime(nextCollectionAt)}`,
      )
    }
  }
  return parts.join(' · ')
}
