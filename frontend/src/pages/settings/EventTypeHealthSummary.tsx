import { HealthBadge } from '@/components/health/health-badge'
import { HealthComponentAverages, HealthGradeCounts } from '@/components/health/health-averages'
import { HealthWorstList } from '@/components/health/health-worst-list'
import { Panel } from '@/components/settings/kit'
import { HEALTH_GRADE_LABEL, gradeForScore } from '@/lib/health'
import { useEventTypesHealth } from './useEventTypesHealth'

/**
 * An event type's health on the main plan (F15, #268): the mean score of its
 * events, how many sit in each grade, each component's average and the least
 * healthy events. Renders nothing on a branch, before the answer, on failure,
 * or for a type with no scored events: the Summary tab has its own content.
 */
export function EventTypeHealthSummary({
  slug,
  eventTypeId,
  onMain,
  className,
}: {
  slug: string
  eventTypeId: string
  onMain: boolean
  className?: string
}) {
  const healthByType = useEventTypesHealth(slug, onMain)
  const health = healthByType?.get(eventTypeId)
  if (!health || health.score === null) return null
  const grade = health.grade ?? gradeForScore(health.score)
  return (
    <Panel
      className={className}
      title="Health"
      subtitle={`Mean of ${health.scored_events.toLocaleString()} scored event${health.scored_events === 1 ? '' : 's'} on the main plan`}
      right={
        <span className="flex items-center gap-2 text-caption text-fg-secondary">
          <HealthBadge score={health.score} grade={grade} size="sm" />
          {HEALTH_GRADE_LABEL[grade]}
        </span>
      }
    >
      <div className="grid gap-4 p-4 md:grid-cols-2">
        <div className="flex flex-col gap-3">
          <HealthGradeCounts counts={health} />
          <HealthComponentAverages averages={health.component_averages} />
        </div>
        <div>
          <h3 className="mb-1 micro-label text-fg-tertiary">Least healthy</h3>
          <HealthWorstList slug={slug} items={health.worst ?? []} />
        </div>
      </div>
    </Panel>
  )
}
