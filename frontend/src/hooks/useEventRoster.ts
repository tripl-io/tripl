import { useMemo } from 'react'
import { keepPreviousData, queryOptions, useQuery } from '@tanstack/react-query'
import { eventsApi } from '@/api/events'
import type { ErrorFeedbackMeta } from '@/lib/errorFeedback'
import { eventsPickerKey } from '@/lib/queryKeys'
import { useDebouncedValue } from './useDebouncedValue'

/**
 * Events every search-as-you-type picker offers at once. Small on purpose:
 * /events returns full list rows (tags, field values, meta values), so pulling
 * thousands into a picker to spare the typing is the wrong trade. The search is
 * server-side (the backend matches name, description and source name), so
 * anything outside the page is one keystroke away, and each picker prints how
 * many matches it left out. One size for all of them, so the pickers share one
 * cache entry per search, and a short page cached by one can never answer
 * another's "N more match".
 */
export const EVENT_PICKER_PAGE_SIZE = 100

/**
 * One page of the event roster for `search` on `branchId`. Exported for a
 * reader that needs the page without the debounce: the metric form's kind
 * step reads the unfiltered page's `total` to say a project has no events,
 * under the same key, so the picker then opens from that cache.
 */
export function eventRosterQuery(slug: string, branchId: string | null, search: string) {
  return queryOptions({
    queryKey: eventsPickerKey(slug, branchId, search),
    queryFn: () =>
      eventsApi.list(
        slug,
        { search: search || undefined, limit: EVENT_PICKER_PAGE_SIZE, offset: 0 },
        branchId,
      ),
    // Each debounced search is a new key; without this the list would drop to
    // empty until the next page lands, on every keystroke.
    placeholderData: keepPreviousData,
  })
}

/**
 * The searched event roster a picker shows: the search debounced, the page
 * for it, and how many matches the page left out. `enabled` lets a picker
 * wait until someone reaches for it; `meta` is for one that reports the
 * error itself (`SILENT_ERROR_META`).
 */
export function useEventRoster({
  slug,
  branchId,
  search,
  enabled = true,
  debounceMs,
  meta,
}: {
  slug: string
  branchId: string | null
  search: string
  enabled?: boolean
  debounceMs?: number
  meta?: ErrorFeedbackMeta
}) {
  const debouncedSearch = useDebouncedValue(search, debounceMs)
  const query = useQuery({ ...eventRosterQuery(slug, branchId, debouncedSearch), enabled, meta })
  const events = useMemo(() => query.data?.items ?? [], [query.data])
  return {
    query,
    events,
    debouncedSearch,
    hiddenCount: Math.max(0, (query.data?.total ?? 0) - events.length),
  }
}
