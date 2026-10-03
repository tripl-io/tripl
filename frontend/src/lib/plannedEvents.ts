import type { PlannedEvent } from '@/types'

/** A planned event's window as the chart's categorical x-axis can draw it. */
export interface SnappedPlannedEvent {
  id: string
  label: string
  direction: PlannedEvent['direction']
  /** The bucket holding the window's start (or the first bucket drawn). */
  x1: string
  /** The last bucket that starts before the window ends. */
  x2: string
}

export const PLANNED_EVENT_LABEL_MAX = 200

/** What a planned event expects, in words: "Expected rise". */
export function plannedEventExpectation(direction: PlannedEvent['direction']): string {
  if (direction === 'spike') return 'Expected rise'
  if (direction === 'drop') return 'Expected drop'
  return 'Expected rise or drop'
}

/**
 * Snap each planned window `[starts_at, ends_at)` onto the drawn buckets.
 *
 * The x-axis is categorical, so a shaded area can only start and end on a
 * bucket that exists. It starts on the bucket CONTAINING the window's start
 * (the latest one starting at or before it; the first bucket when the window
 * began earlier) and ends on the last bucket starting before the window ends,
 * so the shading covers exactly the buckets whose anomalies the event tags.
 * A window wholly outside the drawn range is dropped.
 */
export function snapPlannedEventsToBuckets(
  events: PlannedEvent[] | undefined,
  data: { bucket: string }[],
): SnappedPlannedEvent[] {
  if (!events?.length || !data.length) return []
  const buckets = data
    .map(point => ({ bucket: point.bucket, time: new Date(point.bucket).getTime() }))
    .filter(point => !Number.isNaN(point.time))
  const firstBucket = buckets[0]
  const lastBucket = buckets[buckets.length - 1]
  if (!firstBucket || !lastBucket) return []
  // Where the newest bucket ends: a window starting at or after it (a promo
  // next week) has no bucket to shade yet.
  const previous = buckets[buckets.length - 2]
  const drawnEnd = previous
    ? lastBucket.time + Math.max(0, lastBucket.time - previous.time)
    : Number.POSITIVE_INFINITY

  const snapped: SnappedPlannedEvent[] = []
  for (const event of events) {
    const start = new Date(event.starts_at).getTime()
    const end = new Date(event.ends_at).getTime()
    if (Number.isNaN(start) || Number.isNaN(end) || end <= start || start >= drawnEnd) continue
    let first: { bucket: string; time: number } | null = null
    let last: { bucket: string; time: number } | null = null
    for (const candidate of buckets) {
      if (candidate.time <= start) first = candidate
      if (candidate.time < end) last = candidate
    }
    // Began before the drawn range: shade from its first bucket, as long as
    // that bucket is still inside the window.
    if (first === null && firstBucket.time < end) first = firstBucket
    if (first === null || last === null || first.time > last.time) continue
    snapped.push({
      id: event.id,
      label: event.label,
      direction: event.direction,
      x1: first.bucket,
      x2: last.bucket,
    })
  }
  return snapped
}

/** Whether the form's start and end make a window the API accepts. */
export function isValidPlannedWindow(startsAt: string, endsAt: string): boolean {
  const start = new Date(startsAt).getTime()
  const end = new Date(endsAt).getTime()
  return !Number.isNaN(start) && !Number.isNaN(end) && end > start
}
