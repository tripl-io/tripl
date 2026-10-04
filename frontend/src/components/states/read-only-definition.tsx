import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

export type DefinitionItem = {
  /** Sentence-case label: "Name", "Data source", "Interval". */
  label: string
  /** The value. `null`/`undefined`/'' renders a muted em dash. */
  value: ReactNode
  /** A block value (SQL, JSON, a chip list) spans the full width under its label. */
  block?: boolean
}

/**
 * The read view of a definition, for a viewer who cannot edit it
 * (#237). A disabled form kept its live borders,
 * required stars, placeholders and author hints, and made the reader scroll
 * 2,500px of form chrome; this is a two-column description list instead. Put a
 * `ReadOnlyNotice` above it and title the page after the entity ("Metric
 * definition", the event's name).
 *
 * Mono only inside the value you pass (SQL, identifiers); labels are sans.
 */
export function ReadOnlyDefinition({
  items,
  className,
}: {
  items: DefinitionItem[]
  className?: string
}) {
  return (
    <dl
      data-slot="read-only-definition"
      className={cn(
        'grid grid-cols-1 gap-x-6 gap-y-3 text-body-sm sm:grid-cols-[minmax(120px,180px)_minmax(0,1fr)]',
        className,
      )}
    >
      {items.map((item) => {
        const empty = item.value === null || item.value === undefined || item.value === ''
        return (
          <div key={item.label} className="contents">
            <dt className={cn('text-fg-tertiary', item.block && 'sm:col-span-2')}>{item.label}</dt>
            <dd className={cn('m-0 min-w-0 break-words text-fg', item.block && 'sm:col-span-2')}>
              {empty ? <span className="text-fg-tertiary">—</span> : item.value}
            </dd>
          </div>
        )
      })}
    </dl>
  )
}
