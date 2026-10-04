import type { ReactNode } from 'react'
import { Lock } from 'lucide-react'
import { VIEWER_READ_ONLY_HINT } from '@/lib/permissions'
import { cn } from '@/lib/utils'

/**
 * The one read-only notice (#237): a lock, one sentence that says who
 * can change this, and an optional way out. Members used a loose 14px
 * paragraph, Project General a dashed box and the owner-only sections a bare
 * card; a viewer now meets the same line everywhere.
 *
 * Once per section, directly under its header, never per control. Defaults to
 * the generic viewer copy; pass children for a narrower rule ("Only owners can
 * change roles."). `action` is a link or small button after the text ("Go to
 * Profile").
 */
export function ReadOnlyNotice({
  children = VIEWER_READ_ONLY_HINT,
  action,
  className,
}: {
  children?: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div
      role="note"
      data-slot="read-only-notice"
      className={cn(
        'flex flex-wrap items-start gap-x-2.5 gap-y-1 rounded-card border border-border bg-bg-sunken px-3 py-2.5 text-body-sm text-fg-secondary',
        className,
      )}
    >
      <Lock className="mt-0.5 size-3.5 shrink-0 text-fg-tertiary" aria-hidden="true" />
      <span className="min-w-0 flex-1">{children}</span>
      {action && <span className="shrink-0">{action}</span>}
    </div>
  )
}
