import { useContext } from 'react'
import { AlertTriangle, Lock, RefreshCw } from 'lucide-react'
import { ApiError } from '@/api/client'
import { AuthContext } from '@/components/auth-context'
import { Button } from '@/components/ui/button'
import { cn, getErrorMessage } from '@/lib/utils'

/**
 * A 401 while the session-expired dialog is open means the session ran out and
 * the dialog is already asking for the password over this page. A red
 * "Authentication required" card under it read as data loss beside a dialog
 * promising nothing was lost (#237 SH-35); the query refetches once the user
 * signs back in. With no dialog up (signed out, or a 401 the provider did not
 * treat as an expiry) there is nothing to wait for, so the normal card shows.
 */
function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401
}

export function ErrorState({
  title,
  error,
  description,
  onRetry,
  retryLabel = 'Try again',
  compact = false,
  className,
  headingLevel = 2,
}: {
  title: string
  error?: unknown
  description?: string
  onRetry?: () => void
  retryLabel?: string
  compact?: boolean
  className?: string
  /**
   * Level of the title heading. 2 when the error replaces a page's content
   * under its h1; 3 inside a card that already has an h2 title, so the outline
   * does not flatten into two siblings (DS-16).
   */
  headingLevel?: 2 | 3 | 4
}) {
  const message = getErrorMessage(error)
  const Heading = `h${headingLevel}` as const
  const sessionExpired = useContext(AuthContext)?.sessionExpired ?? false

  if (sessionExpired && isUnauthorized(error)) {
    return (
      <div
        role="status"
        data-slot="error-state-paused"
        className={cn(
          'flex items-center gap-2 rounded-card border border-border bg-bg-sunken text-body-sm text-fg-secondary',
          compact ? 'p-3' : 'p-4',
          className,
        )}
      >
        <Lock className="size-3.5 shrink-0 text-fg-tertiary" aria-hidden="true" />
        Waiting for you to sign in again. This loads as soon as you do.
      </div>
    )
  }

  return (
    <div
      role="alert"
      className={cn(
        'rounded-card border border-destructive/35 bg-destructive/5 text-left',
        compact ? 'p-3' : 'p-5',
        className,
      )}
    >
      <div className={cn('flex gap-3', compact ? 'items-start' : 'items-center')}>
        <div className="mt-0.5 rounded-full bg-destructive/10 p-2 text-destructive">
          <AlertTriangle className="size-4" aria-hidden="true" />
        </div>
        <div className="min-w-0 flex-1">
          {/* h2 for the same reason as EmptyState: an error surface replaces a
              page's content directly under its h1. On
              EmptyState's scale too: heading/body, or body-sm/caption when
              compact, so it no longer out-sizes the panel around it (DS-21). */}
          <Heading className={cn('font-semibold text-foreground', compact ? 'text-body-sm' : 'text-heading')}>{title}</Heading>
          {description && (
            <p className={cn('mt-1 text-fg-secondary', compact ? 'text-caption' : 'text-body')}>
              {description}
            </p>
          )}
          <p className={cn('mt-1 break-words text-destructive', compact ? 'text-caption' : 'text-body')}>
            {message}
          </p>
          {onRetry && (
            <Button type="button" variant="outline" size={compact ? 'sm' : 'default'} className="mt-3" onClick={onRetry}>
              {/* No margin or size here: Button already spaces and sizes its
                  icons, and `mr-2` on top doubled the gap (DS-47). */}
              <RefreshCw aria-hidden="true" />
              {retryLabel}
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}
