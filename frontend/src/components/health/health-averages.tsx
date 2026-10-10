import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { chipVariants } from '@/components/primitives/chip-variants'
import { cn } from '@/lib/utils'
import {
  HEALTH_COMPONENT_LABELS,
  HEALTH_GRADE_LABEL,
  HEALTH_GRADE_TONE,
  formatHealthValue,
  gradeForScore,
  healthAriaLabel,
  orderedAverages,
} from '@/lib/health'
import type { ComponentAverage, HealthGrade } from '@/types/health'
import { formatNumber } from '@/lib/format'
import { countOf } from '@/lib/plural'

export type GradeCountsValue = {
  healthy_count: number
  warning_count: number
  unhealthy_count: number
}

/** "12 healthy · 3 need attention · 1 unhealthy", each in its grade's tone. */
export function HealthGradeCounts({ counts, className }: { counts: GradeCountsValue; className?: string }) {
  const rows: [HealthGrade, number][] = [
    ['healthy', counts.healthy_count],
    ['warning', counts.warning_count],
    ['unhealthy', counts.unhealthy_count],
  ]
  return (
    <ul className={cn('flex flex-wrap gap-1.5', className)} aria-label="Events by grade">
      {rows.map(([grade, count]) => (
        <li
          key={grade}
          className={chipVariants({ tone: HEALTH_GRADE_TONE[grade], size: 'xs' })}
          data-grade={grade}
        >
          <span className="tnum">{formatNumber(count)}</span> {HEALTH_GRADE_LABEL[grade].toLowerCase()}
        </li>
      ))}
    </ul>
  )
}

/**
 * The mean value of each component over the events it applies to, with how
 * many that is. A component no event qualifies for reads "not applicable".
 */
export function HealthComponentAverages({
  averages,
  className,
}: {
  averages: readonly ComponentAverage[]
  className?: string
}) {
  const ordered = orderedAverages(averages)
  if (ordered.length === 0) return null
  return (
    <dl className={cn('grid grid-cols-[1fr_auto] gap-x-3 gap-y-1 text-caption', className)}>
      {ordered.map((average) => {
        const excluded = average.applies_count === 0 || average.value === null
        return (
          <div key={average.key} className="contents" data-component={average.key}>
            <dt className={excluded ? 'text-fg-tertiary' : 'text-fg-secondary'}>
              {HEALTH_COMPONENT_LABELS[average.key] ?? average.key}
            </dt>
            <dd className="tnum text-right">
              {excluded ? (
                <span className="text-fg-tertiary">not applicable</span>
              ) : (
                <>
                  {formatHealthValue(average.value)}
                  <span className="text-fg-tertiary">
                    {' '}
                    · {countOf(average.applies_count, 'event', 'events')}
                  </span>
                </>
              )}
            </dd>
          </div>
        )
      })}
    </dl>
  )
}

export type AggregateHealth = GradeCountsValue & {
  score: number | null
  grade: HealthGrade | null
  scored_events: number
  component_averages: ComponentAverage[]
}

/**
 * An aggregate score (an event type's) as a badge that opens its component
 * averages and grade counts. A type with no scored events shows a dash.
 */
export function AggregateHealthPopover({
  health,
  name,
  align = 'start',
}: {
  health: AggregateHealth
  name: string
  align?: 'start' | 'center' | 'end'
}) {
  if (health.score === null) {
    return (
      <span className="text-caption text-fg-tertiary" title="No scored events on the main plan">
        —
      </span>
    )
  }
  const grade = health.grade ?? gradeForScore(health.score)
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`${healthAriaLabel(health.score)} for ${name}. Show details`}
          data-grade={grade}
          className={cn(
            chipVariants({ tone: HEALTH_GRADE_TONE[grade], size: 'xs' }),
            'tnum cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50',
          )}
        >
          {health.score}
        </button>
      </PopoverTrigger>
      <PopoverContent align={align} className="w-80 p-3" aria-label={`Health of ${name}`}>
        <div className="flex flex-col gap-2.5">
          <p className="text-body-sm">
            <span className="tnum font-semibold">{health.score}</span>
            <span className="text-fg-tertiary">/100 · mean of {countOf(health.scored_events, 'event', 'events')}</span>
          </p>
          <HealthGradeCounts counts={health} />
          <HealthComponentAverages averages={health.component_averages} />
        </div>
      </PopoverContent>
    </Popover>
  )
}
