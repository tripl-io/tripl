import type { ReactNode } from 'react'
import { Info } from 'lucide-react'
import { cn } from '@/lib/utils'
import { disabledReasonId } from './disabled-reason-aria'

/**
 * Why a button is disabled, as visible text next to it (#237).
 *
 * A disabled `<button>` gets `pointer-events: none` from the Button primitive,
 * so a `title` on it never shows, and keyboard and touch users cannot reach it
 * either: the reader saw a grey button and had to guess. Render the blocker
 * beside (or under) the button instead, and point the button at it:
 *
 *   const blocker = !name ? 'A scan needs a name, a data source and a base query.' : null
 *   <Button disabled={!!blocker} {...disabledReasonAria('create-scan', blocker)}>Create scan</Button>
 *   <DisabledReason id="create-scan" reason={blocker} />
 *
 * Renders nothing when `reason` is empty, so the pair can stay mounted.
 * `tone="warning"` for a blocker the user can fix in the form (the default);
 * `"muted"` for one they cannot (role, plan state). Blocking VALIDATION errors
 * belong to the form pattern instead (FieldError + SaveBar `missingSummary`).
 */
export function DisabledReason({
  id,
  reason,
  tone = 'warning',
  className,
}: {
  /** Same base id passed to {@link disabledReasonAria}; the text gets `<id>-reason`. */
  id: string
  reason: ReactNode
  tone?: 'warning' | 'muted'
  className?: string
}) {
  if (reason === null || reason === undefined || reason === false || reason === '') return null
  return (
    <p
      id={disabledReasonId(id)}
      data-slot="disabled-reason"
      className={cn(
        'm-0 inline-flex items-start gap-1.5 text-caption',
        tone === 'warning' ? 'text-(--warning)' : 'text-fg-tertiary',
        className,
      )}
    >
      <Info className="mt-0.5 size-3 shrink-0" aria-hidden="true" />
      <span>{reason}</span>
    </p>
  )
}
