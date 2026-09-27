import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'

import { healthApi } from '@/api/health'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { eventTypesHealthKey } from '@/lib/queryKeys'
import type { EventTypeHealth } from '@/types/health'

/**
 * Every event type's aggregate health (F15, #268), by type id, or `undefined`
 * until it has answered (or when it failed: the column is a hint, silent).
 * Main-plan only: type ids on a branch are the branch's deep copies, so a
 * branch asks nothing.
 */
export function useEventTypesHealth(
  slug: string,
  onMain: boolean,
): ReadonlyMap<string, EventTypeHealth> | undefined {
  const query = useQuery({
    queryKey: eventTypesHealthKey(slug),
    queryFn: ({ signal }) => healthApi.eventTypes(slug, signal),
    enabled: onMain && !!slug,
    meta: SILENT_ERROR_META,
    staleTime: 60_000,
  })
  const data = query.data
  return useMemo(() => {
    if (!onMain || !Array.isArray(data)) return undefined
    return new Map(
      data
        .filter((item) => item && typeof item.event_type_id === 'string')
        .map((item) => [item.event_type_id, item]),
    )
  }, [data, onMain])
}
