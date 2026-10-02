import { useQuery } from '@tanstack/react-query'

import { healthApi } from '@/api/health'
import { HealthBadge } from '@/components/health/health-badge'
import { HealthBreakdown } from '@/components/health/health-breakdown'
import { Panel } from '@/components/settings/kit'
import { useActiveBranchId } from '@/hooks/useBranch'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { HEALTH_GRADE_LABEL, gradeForScore, isEventHealth } from '@/lib/health'
import { eventHealthKey } from '@/lib/queryKeys'

/**
 * One event's health score with its breakdown always open (F15, #268): what
 * each component earned, what was left out and why. Shown on the event page
 * (EventEditPage) and its monitoring page (MonitoringDetailPage), which is
 * where a viewer lands.
 *
 * Health is about the MAIN plan: on a branch the card is not rendered, and an
 * event outside the scored population (archived, or a branch row) answers 404,
 * which renders nothing too. An archived event is known before asking, so it is
 * not asked about: the 404 was a red line in every archived event's console.
 * Silent on failure, and nothing while it loads: the card sits below the page's
 * own content.
 */
export function EventHealthCard({
  slug,
  eventId,
  status,
  className,
}: {
  slug: string
  eventId: string
  /** The event's status, once known: an archived event is never scored. */
  status?: string
  className?: string
}) {
  const branchId = useActiveBranchId()
  const onMain = !branchId && status !== undefined && status !== 'archived'
  const { data } = useQuery({
    queryKey: eventHealthKey(slug, eventId),
    queryFn: ({ signal }) => healthApi.event(slug, eventId, signal),
    enabled: onMain && !!slug && !!eventId,
    meta: SILENT_ERROR_META,
    staleTime: 60_000,
    retry: false,
  })
  if (!onMain || !isEventHealth(data)) return null
  const grade = data.grade ?? gradeForScore(data.score)
  return (
    <Panel
      className={className}
      title="Health"
      subtitle={
        data.top_issue ? (
          <>
            <span className="sr-only">Top issue: </span>
            {data.top_issue}
          </>
        ) : (
          'Nothing is costing this event points.'
        )
      }
      right={
        <span className="flex items-center gap-2 text-caption text-fg-secondary">
          <HealthBadge score={data.score} grade={grade} size="sm" />
          {HEALTH_GRADE_LABEL[grade]}
        </span>
      }
    >
      <div className="p-4">
        <HealthBreakdown health={data} hideSummary />
      </div>
    </Panel>
  )
}
