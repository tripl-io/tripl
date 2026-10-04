import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { chartAnnotationsApi } from '@/api/chartAnnotations'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import type { MonitoringScope } from '@/lib/monitoring'
import { chartAnnotationsRangeKey } from '@/lib/queryKeys'

/**
 * The annotations of one drilldown, shared by the volume chart (markers) and
 * the Annotations card (list + form). Keyed on the range length, not on the
 * live window's moving bounds, so the list does not drop to "(0)" and the
 * markers do not vanish every five minutes.
 */
export function useChartAnnotations({
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
  timeRange: { from: string; to: string }
}) {
  return useQuery({
    queryKey: chartAnnotationsRangeKey(slug, scope, scopeId, rangeDays),
    queryFn: () =>
      chartAnnotationsApi.list(slug!, {
        scope_type: scope,
        scope_ref: scopeId,
        from: timeRange.from,
        to: timeRange.to,
      }),
    enabled: !!slug && !!scopeId,
    placeholderData: keepPreviousData,
    meta: SILENT_ERROR_META,
  })
}
