import type { ComponentProps } from 'react'
import { Chip, type ChipSize } from '@/components/primitives/chip'
import { cn } from '@/lib/utils'
import {
  HEALTH_GRADE_LABEL,
  HEALTH_GRADE_TONE,
  gradeForScore,
  healthAriaLabel,
} from '@/lib/health'
import type { HealthGrade } from '@/types/health'

export type HealthBadgeProps = Omit<ComponentProps<'span'>, 'children'> & {
  score: number
  /** The server's grade; derived from the score by the fixed thresholds when absent. */
  grade?: HealthGrade | null
  size?: ChipSize
}

/**
 * The health score pill (F15, #268): the number, toned by its grade. The
 * visible text is the bare number, so the pill names itself for a screen
 * reader ("Health 72 of 100") and the grade rides in the native title.
 */
export function HealthBadge({ score, grade, size = 'xs', className, ...props }: HealthBadgeProps) {
  const resolved = grade ?? gradeForScore(score)
  const rounded = Math.round(score)
  return (
    <Chip
      tone={HEALTH_GRADE_TONE[resolved]}
      size={size}
      role="img"
      aria-label={healthAriaLabel(rounded)}
      title={`${HEALTH_GRADE_LABEL[resolved]} · ${rounded}/100`}
      data-grade={resolved}
      className={cn('tnum', className)}
      {...props}
    >
      <span aria-hidden="true">{rounded}</span>
    </Chip>
  )
}
