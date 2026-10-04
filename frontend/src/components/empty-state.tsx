import type { LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

export type EmptyStateSize = 'default' | 'sm'

/**
 * "Nothing here yet" for a page or a panel.
 *
 * `size="sm"` is the in-panel form: a compact block that fits inside a card or
 * a table body, where the page-level `py-16` block was too tall, so pages kept
 * writing one-off "No … yet" lines that looked different everywhere.
 * Convention: tables load with skeleton rows (`LoadingState`), panels show a
 * compact EmptyState.
 */
export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  size = 'default',
  headingLevel = 2,
  className,
}: {
  icon?: LucideIcon
  title: string
  description?: ReactNode
  action?: ReactNode
  size?: EmptyStateSize
  /**
   * 2 by default: a page-level empty state sits directly under the page's h1,
   * so h3 opened a 1→3 gap in the outline. 3 (or 4) inside a
   * card or panel that already has its own heading.
   */
  headingLevel?: 2 | 3 | 4
  className?: string
}) {
  const Heading = headingLevel === 4 ? 'h4' : headingLevel === 3 ? 'h3' : 'h2'
  const compact = size === 'sm'
  return (
    <div
      data-slot="empty-state"
      className={cn(
        'flex flex-col items-center justify-center text-center',
        compact ? 'px-4 py-7' : 'py-16',
        className,
      )}
    >
      {Icon &&
        (compact ? (
          // A sunken well in the panel form too, so an empty panel reads as
          // intentional rather than as a stray icon.
          <div className="mb-2.5 flex size-10 items-center justify-center rounded-full bg-bg-sunken">
            <Icon className="size-5 text-fg-tertiary" aria-hidden="true" />
          </div>
        ) : (
          <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-muted">
            <Icon className="size-5 text-fg-tertiary" aria-hidden="true" />
          </div>
        ))}
      {/* On the type scale: the page form titles at 15px over 13px
          body; the panel form sits under a 12.5px panel title, so it titles
          at that size over caption body instead of out-sizing it. */}
      <Heading
        className={`font-semibold text-foreground ${compact ? 'text-body-sm' : 'text-heading'}`}
      >
        {title}
      </Heading>
      {description && (
        <p
          className={cn(
            'mt-1 max-w-sm text-fg-secondary',
            compact ? 'text-caption' : 'text-body',
          )}
        >
          {description}
        </p>
      )}
      {action && <div className={compact ? 'mt-3' : 'mt-4'}>{action}</div>}
    </div>
  )
}
