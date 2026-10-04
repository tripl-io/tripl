import { useMemo } from 'react'
import { useQueries, useQuery } from '@tanstack/react-query'
import type { VirtualItem } from '@tanstack/react-virtual'

import { eventMetricsApi } from '@/api/eventMetrics'
import { useAdaptiveRefetchInterval } from '@/realtime/streamContext'
import type { EventListItem } from '@/types'

import { visibleBucketRange } from './useEventRowMetrics'
import { EMPTY_SIGNALS, chunkEventIds, mapLatestSignals, pickLatestSignal } from './utils'
import { eventRowSignalsKey, eventsTabSignalsKey } from '@/lib/queryKeys'

/**
 * Monitoring signals for the project tabs: the project total and one per
 * event type. The per-row signals live in `useEventRowSignals`, which needs the
 * virtualizer's window and so runs later in the page.
 */
export function useEventsSignals({ slug }: { slug: string | undefined }) {
  const refetchInterval = useAdaptiveRefetchInterval({ activeMs: 60_000 })

  const tabSignalsQuery = useQuery({
    queryKey: eventsTabSignalsKey(slug),
    queryFn: () => eventMetricsApi.getActiveSignals(slug!),
    enabled: !!slug,
    refetchInterval,
  })
  const tabSignals = tabSignalsQuery.data ?? EMPTY_SIGNALS

  const projectTotalSignal = useMemo(
    () => pickLatestSignal(tabSignals, 'project_total'),
    [tabSignals],
  )
  const eventTypeSignals = useMemo(
    () => mapLatestSignals(tabSignals, 'event_type'),
    [tabSignals],
  )

  return {
    projectTotalSignal,
    eventTypeSignals,
    // "0 · none" before the query settles is a false all-clear.
    signalsPending: tabSignalsQuery.isPending,
  }
}

/**
 * Per-row monitoring signals, keyed by event id, for the rows on screen.
 *
 * Bucketed for the same reason the window-metrics query is:
 * keying on the whole accumulated id list minted a fresh cache entry on every
 * infinite-scroll append and re-sent every id already loaded, so page 12 posted
 * 2,400 ids to learn about the 200 that were new.
 * Index-aligned buckets keep each loaded bucket's key stable, so an append
 * fetches one bucket.
 *
 * And only the buckets on screen get a query, as in useEventRowMetrics: each
 * one polls, so after scrolling 2,400 rows 24 signal queries kept refreshing
 * every minute for rows long out of view.
 */
export function useEventRowSignals({
  slug,
  events,
  virtualItems,
}: {
  slug: string | undefined
  /** The rows the table lists (after client-side filters), in table order. */
  events: EventListItem[]
  /** Rows the table is currently rendering; empty when not virtualizing. */
  virtualItems: readonly VirtualItem[]
}) {
  const refetchInterval = useAdaptiveRefetchInterval({ activeMs: 60_000 })
  const eventIdBuckets = useMemo(
    () => chunkEventIds(events.map(event => event.id)),
    [events],
  )
  const { first: firstVisibleBucket, last: lastVisibleBucket } = visibleBucketRange(virtualItems)
  const visibleBuckets = useMemo(
    () =>
      eventIdBuckets.filter(
        (_bucket, index) => index >= firstVisibleBucket && index <= lastVisibleBucket,
      ),
    [eventIdBuckets, firstVisibleBucket, lastVisibleBucket],
  )
  const rowSignals = useQueries({
    queries: visibleBuckets.map(bucketIds => ({
      queryKey: eventRowSignalsKey(slug, bucketIds),
      queryFn: () => eventMetricsApi.getActiveSignals(slug!, bucketIds),
      enabled: !!slug && bucketIds.length > 0,
      refetchInterval,
    })),
    // Structural sharing keeps this array stable while the data is unchanged,
    // so the downstream map memo does not rebuild on every render.
    combine: results => results.flatMap(result => result.data ?? EMPTY_SIGNALS),
  })

  return useMemo(() => mapLatestSignals(rowSignals, 'event'), [rowSignals])
}
