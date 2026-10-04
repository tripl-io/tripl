import { keepPreviousData, queryOptions } from '@tanstack/react-query'
import { eventsApi } from '@/api/events'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { eventsPickerKey } from '@/lib/queryKeys'

// Events offered at once. Small on purpose, for the reason the variables tab
// spells out: the search is server-side, so anything outside the
// page is one keystroke away, and the count of what is missing is printed.
export const EVENT_PICKER_PAGE_SIZE = 100

/**
 * One page of the metric event picker's roster for `search`. Shared with the
 * kind step, which reads the unfiltered page's `total` to say a project has no
 * events yet (MT-3): the same key, so the picker opens from that cache.
 */
export function eventRosterQuery(slug: string, search: string) {
  return queryOptions({
    queryKey: eventsPickerKey(slug, null, 'metric-picker', search),
    queryFn: () =>
      eventsApi.list(slug, {
        search: search || undefined,
        limit: EVENT_PICKER_PAGE_SIZE,
        offset: 0,
      }),
    placeholderData: keepPreviousData,
    // Rendered inline under the picker, with a retry.
    meta: SILENT_ERROR_META,
  })
}
