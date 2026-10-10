import type { ReactNode } from 'react'
import { Trash2 } from 'lucide-react'
import { Chip } from '@/components/primitives/chip'
import { IconButton } from '@/components/ui/icon-button'
import { formatExpectedWindowSpan } from '@/lib/chartAnnotations'
import { plannedEventExpectation } from '@/lib/plannedEvents'
import type { PlannedEvent } from '@/types'

/**
 * One expected window in a list: its span, label, what it expects and where it
 * applies, with a delete for an editor. Shared by a chart's Expected windows
 * card and the Annotations page, which differ only in how they show the scope.
 */
export function ExpectedWindowItem({
  event,
  scope,
  onDelete,
  deleting = false,
  inset = false,
}: {
  event: PlannedEvent
  /** Where the window applies: a link to its chart, or a project-wide chip. */
  scope?: ReactNode
  /** Omitted when the reader cannot delete. */
  onDelete?: () => void
  deleting?: boolean
  /** Pad the row itself, for a list that sits flush in a panel. */
  inset?: boolean
}) {
  return (
    <li className={`flex items-center justify-between gap-2 py-2${inset ? ' px-4' : ''}`}>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="text-fg-tertiary">{formatExpectedWindowSpan(event)}</span>
        <span className="min-w-0 break-words font-medium">{event.label}</span>
        <Chip variant="outline" size="xs">{plannedEventExpectation(event.direction)}</Chip>
        {scope}
        {event.source === 'holiday' && <Chip variant="outline" size="xs">Holiday</Chip>}
      </div>
      {/* The holiday calendar owns its rows: changed in Detection settings. */}
      {onDelete && event.source !== 'holiday' && (
        <IconButton
          variant="ghost"
          className="h-7 w-7 shrink-0 text-fg-tertiary hover:text-destructive"
          onClick={onDelete}
          disabled={deleting}
          label={`Delete expected window ${event.label}`}
        >
          <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />
        </IconButton>
      )}
    </li>
  )
}
