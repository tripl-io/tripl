import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { plannedEventsApi } from '@/api/plannedEvents'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import type { MonitoringScope } from '@/lib/monitoring'
import { plannedEventsRangeKey } from '@/lib/queryKeys'

/**
 * The expected windows of one drilldown (F18; planned events in the API),
 * shared by the volume chart (the shaded windows) and the Expected windows
 * card. Keyed on the range length like
 * `useChartAnnotations`, so the live window's moving bounds do not refetch.
 */
export function usePlannedEvents({
  slug,
  scope,
  scopeId,
  rangeDays,
  timeRange,
}: {
  slug: string | undefined
  scope: MonitoringScope
  scopeId: string
  rangeDays: number
  timeRange: { from: string; to?: string }
}) {
  return useQuery({
    queryKey: plannedEventsRangeKey(slug, scope, scopeId, rangeDays),
    queryFn: () =>
      plannedEventsApi.list(slug!, {
        scope_type: scope,
        scope_ref: scopeId,
        // No upper bound: a window planned after the chart's range (next
        // month's sale) is what the card is for, and it vanished on Add. The
        // chart skips windows it has no bucket for yet.
        from: timeRange.from,
      }),
    enabled: !!slug && !!scopeId,
    placeholderData: keepPreviousData,
    meta: SILENT_ERROR_META,
  })
}
