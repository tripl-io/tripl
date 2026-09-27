import { useMemo } from 'react'
import { useQueries, type UseQueryResult } from '@tanstack/react-query'

import { HEALTH_BATCH_MAX_IDS, healthApi } from '@/api/health'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { chunkIds } from '@/lib/health'
import { eventsHealthKey } from '@/lib/queryKeys'
import type { EventHealth, EventListItem } from '@/types'

const EMPTY_HEALTH: EventHealth[] = []
const EMPTY_HEALTH_MAP: ReadonlyMap<string, EventHealth> = new Map()

// Module-level so react-query can keep the combined array stable while no
// chunk's data changed, and the map memo below does not rebuild every render.
function combineHealth(results: UseQueryResult<EventHealth[]>[]): EventHealth[] {
  if (results.every((result) => !result.data || result.data.length === 0)) return EMPTY_HEALTH
  return results.flatMap((result) => result.data ?? EMPTY_HEALTH)
}

/**
 * Health scores for the catalog's loaded rows (F15, #268), keyed by event id.
 *
 * One batched GET per HEALTH_BATCH_MAX_IDS (150) loaded ids. The chunks are index-aligned, so an
 * infinite-scroll append keeps every full chunk's key and refetches only the
 * last one. Health is about the MAIN plan: on a branch (`enabled: false`) the
 * hook asks nothing and returns an empty map. Silent on failure: the badge is
 * a hint over a table that has its own content.
 */
export function useEventsHealth({
  slug,
  events,
  enabled,
}: {
  slug: string | undefined
  /** The loaded rows, in table order; may be sparse while pages stream in. */
  events: readonly (EventListItem | undefined)[]
  enabled: boolean
}): ReadonlyMap<string, EventHealth> {
  const chunks = useMemo(() => {
    if (!enabled) return []
    const ids = events.filter((ev): ev is EventListItem => Boolean(ev)).map((ev) => ev.id)
    return chunkIds(ids, HEALTH_BATCH_MAX_IDS)
  }, [enabled, events])

  const items = useQueries({
    queries: chunks.map((ids) => ({
      queryKey: eventsHealthKey(slug, ids),
      queryFn: ({ signal }: { signal: AbortSignal }) => healthApi.events(slug!, ids, signal),
      enabled: !!slug && ids.length > 0,
      staleTime: 60_000,
      meta: SILENT_ERROR_META,
    })),
    combine: combineHealth,
  })

  return useMemo(() => {
    if (items.length === 0) return EMPTY_HEALTH_MAP
    return new Map(items.map((health) => [health.event_id, health]))
  }, [items])
}
