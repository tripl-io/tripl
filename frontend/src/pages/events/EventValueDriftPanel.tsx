import { useQuery } from '@tanstack/react-query'

import { variableDriftsApi } from '@/api/variableDrifts'
import { useActiveBranchId } from '@/hooks/useBranch'
import { eventVariableDriftsKey } from '@/lib/queryKeys'
import { DriftReviewList } from '@/pages/settings/variable-detail/DriftReviewList'

/**
 * Value drift for one event, row by property; renders nothing when clean. The
 * review itself is `DriftReviewList`, shared with the property's page, so a
 * viewer gets the same read-only rows here as there.
 */
export function EventValueDriftPanel({
  slug,
  eventId,
  canWrite,
}: {
  slug: string
  eventId: string
  canWrite: boolean
}) {
  const branchId = useActiveBranchId()
  const { data } = useQuery({
    queryKey: eventVariableDriftsKey(slug, branchId, eventId),
    queryFn: () => variableDriftsApi.list(slug, { eventId }, branchId),
  })
  const drifts = data?.items ?? []
  if (drifts.length === 0) return null

  return (
    <DriftReviewList
      slug={slug}
      branchId={branchId}
      drifts={drifts}
      canWrite={canWrite}
      rowLabel={drift => <span className="font-mono">{`\${${drift.variable_name}}`}</span>}
    />
  )
}
