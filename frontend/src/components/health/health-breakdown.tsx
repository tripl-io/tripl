import { useId, type ReactNode } from 'react'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'
import {
  HEALTH_GRADE_COLOR,
  HEALTH_GRADE_LABEL,
  HEALTH_GRADE_TONE,
  formatHealthPoints,
  formatHealthValue,
  formatHealthWeight,
  gradeForScore,
  healthAriaLabel,
  orderedComponents,
  renormalizedFootnote,
} from '@/lib/health'
import { chipVariants } from '@/components/primitives/chip-variants'
import type { EventHealth, HealthComponent } from '@/types/health'

/** A value's bar colour: the grade its value would score on its own. */
function valueColor(value: number): string {
  return HEALTH_GRADE_COLOR[gradeForScore(value * 100)]
}

function ValueBar({ value }: { value: number }) {
  const pct = Math.round(Math.min(1, Math.max(0, value)) * 100)
  return (
    <span className="flex items-center gap-1.5">
      <span
        aria-hidden="true"
        className="relative h-1.5 w-12 shrink-0 overflow-hidden rounded-full bg-surface-hover"
      >
        <span
          className="absolute inset-y-0 left-0 rounded-full"
          style={{ width: `${pct}%`, background: valueColor(value) }}
        />
      </span>
      <span className="tnum">{formatHealthValue(value)}</span>
    </span>
  )
}

function ComponentRow({ component }: { component: HealthComponent }) {
  const excluded = !component.applies || component.value === null
  if (excluded) {
    return (
      <tr data-component={component.key} data-excluded="true" className="text-fg-tertiary">
        <th scope="row" className="py-1 pr-2 text-left font-normal">
          {component.label}
        </th>
        <td className="tnum py-1 pr-2 text-right">
          <span className="line-through">{formatHealthWeight(component.weight)}</span>
          <span className="sr-only"> (excluded)</span>
        </td>
        <td className="py-1 pr-2">—</td>
        <td className="tnum py-1 pr-2 text-right">—</td>
        <td className="py-1 italic">{component.excluded_reason ?? 'Does not apply'}</td>
      </tr>
    )
  }
  const value = component.value ?? 0
  return (
    <tr data-component={component.key} className="text-fg-secondary">
      <th scope="row" className="py-1 pr-2 text-left font-medium text-fg">
        {component.label}
      </th>
      <td className="tnum py-1 pr-2 text-right whitespace-nowrap">
        {component.effective_weight !== null && component.effective_weight !== component.weight ? (
          <>
            <span className="text-fg-tertiary">{formatHealthWeight(component.weight)}</span>
            <span aria-hidden="true"> → </span>
            <span className="sr-only"> rescaled to </span>
            {formatHealthWeight(component.effective_weight)}
          </>
        ) : (
          formatHealthWeight(component.effective_weight ?? component.weight)
        )}
      </td>
      <td className="py-1 pr-2">
        <ValueBar value={value} />
      </td>
      <td className="tnum py-1 pr-2 text-right">{formatHealthPoints(component.points)}</td>
      <td className="py-1">{component.detail}</td>
    </tr>
  )
}

export type HealthBreakdownProps = {
  health: EventHealth
  className?: string
  /** Hides the score line on top, for a host that already shows it. */
  hideSummary?: boolean
}

/**
 * Every component of an event's health score (F15, #268): its fixed weight and
 * the weight after renormalization, its value, the points it earned and the
 * reason in words. Components that do not apply are listed greyed out with the
 * reason, under the renormalization footnote, so the score never hides what it
 * left out.
 */
export function HealthBreakdown({ health, className, hideSummary = false }: HealthBreakdownProps) {
  const captionId = useId()
  const components = orderedComponents(health)
  const applicable = components.filter((c) => c.applies && c.value !== null)
  const excluded = components.filter((c) => !c.applies || c.value === null)
  const grade = health.grade ?? gradeForScore(health.score)
  return (
    <div className={cn('flex flex-col gap-2 text-caption', className)} data-testid="health-breakdown">
      {!hideSummary && (
        <p className="flex items-baseline gap-2 text-body-sm">
          <span className="tnum font-semibold text-fg">
            {health.score}
            <span className="text-fg-tertiary">/100</span>
          </span>
          <span className={chipVariants({ tone: HEALTH_GRADE_TONE[grade], size: 'xs' })}>
            {HEALTH_GRADE_LABEL[grade]}
          </span>
        </p>
      )}
      <div className="overflow-x-auto">
        <table className="w-full border-collapse" aria-labelledby={captionId}>
          <caption id={captionId} className="sr-only">
            {`Health score breakdown for ${health.name}: ${healthAriaLabel(health.score)}`}
          </caption>
          <thead>
            <tr className="text-left text-micro text-fg-tertiary">
              <th scope="col" className="pb-1 pr-2 font-medium">Component</th>
              <th scope="col" className="pb-1 pr-2 text-right font-medium">Weight</th>
              <th scope="col" className="pb-1 pr-2 font-medium">Value</th>
              <th scope="col" className="pb-1 pr-2 text-right font-medium">Points</th>
              <th scope="col" className="pb-1 font-medium">Why</th>
            </tr>
          </thead>
          <tbody>
            {applicable.map((component) => (
              <ComponentRow key={component.key} component={component} />
            ))}
            {excluded.map((component) => (
              <ComponentRow key={component.key} component={component} />
            ))}
          </tbody>
        </table>
      </div>
      {(health.renormalized || excluded.length > 0) && (
        <p className="text-micro text-fg-tertiary" data-testid="health-renormalized">
          {renormalizedFootnote(applicable.length)}
          {excluded.length > 0 && `; ${excluded.length} excluded.`}
        </p>
      )}
    </div>
  )
}

export type HealthPopoverProps = {
  health: EventHealth
  /** The trigger's content; the score badge look by default. */
  children?: ReactNode
  align?: 'start' | 'center' | 'end'
  className?: string
}

/**
 * The badge as a button that opens the breakdown. Radix gives it the
 * keyboard (Enter/Space opens, Escape closes and returns focus) and the
 * `aria-expanded` / `aria-controls` wiring.
 */
export function HealthPopover({ health, children, align = 'start', className }: HealthPopoverProps) {
  const grade = health.grade ?? gradeForScore(health.score)
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`${healthAriaLabel(health.score)}. Show breakdown`}
          data-grade={grade}
          className={cn(
            chipVariants({ tone: HEALTH_GRADE_TONE[grade], size: 'xs' }),
            'tnum cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50',
            className,
          )}
        >
          {children ?? health.score}
        </button>
      </PopoverTrigger>
      <PopoverContent
        align={align}
        className="w-[min(34rem,calc(100vw-2rem))] p-3"
        aria-label={`Health breakdown for ${health.name}`}
      >
        <HealthBreakdown health={health} />
      </PopoverContent>
    </Popover>
  )
}
