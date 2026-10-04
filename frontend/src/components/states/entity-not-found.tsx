import { SearchX } from 'lucide-react'
import { Link } from 'react-router-dom'
import { isNotFoundError } from './not-found-error'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { Button } from '@/components/ui/button'


export type BackAction = {
  /** Where the way out goes, usually the entity's list. */
  to: string
  /** "Back to Events", "Back to metrics"… */
  label: string
}

/**
 * An entity that does not exist (deleted, moved to another branch, a stale
 * link) inside a page that keeps its shell (#237). Not an error: nothing
 * can be retried, so there is no red card and no "Try again", only the way back
 * to the list. The whole-route 404 is `NotFoundState` (components/not-found-state).
 */
export function EntityNotFound({
  title,
  description = 'It may have been deleted, renamed, or moved to another branch.',
  back,
  headingLevel = 2,
  className,
}: {
  /** "Event not found", "Metric not found"… */
  title: string
  description?: string
  back?: BackAction
  headingLevel?: 2 | 3 | 4
  className?: string
}) {
  return (
    <div data-slot="entity-not-found" className={className}>
      <EmptyState
        icon={SearchX}
        title={title}
        description={description}
        headingLevel={headingLevel}
        action={
          back ? (
            <Button asChild variant="outline">
              <Link to={back.to}>{back.label}</Link>
            </Button>
          ) : undefined
        }
      />
    </div>
  )
}

/**
 * The error branch of a page that fetches ONE entity by id: a 404 renders
 * {@link EntityNotFound} with the way back, anything else (5xx, network) keeps
 * `ErrorState` with its retry. Replaces the "Failed to load … / Try again" card
 * that a missing event, metric, monitor or scan used to show.
 *
 *   if (query.isError) return (
 *     <QueryErrorState error={query.error} title="Could not load this event"
 *       notFound={{ title: 'Event not found', back: { to: listPath, label: 'Back to Events' } }}
 *       onRetry={() => void query.refetch()} />
 *   )
 */
export function QueryErrorState({
  error,
  title,
  description,
  onRetry,
  notFound,
  compact,
  headingLevel,
  className,
}: {
  error: unknown
  /** ErrorState title for a real failure. Name the thing, not the view ("Could not load this metric"). */
  title: string
  description?: string
  onRetry?: () => void
  notFound: {
    title: string
    description?: string
    back?: BackAction
  }
  compact?: boolean
  headingLevel?: 2 | 3 | 4
  className?: string
}) {
  if (isNotFoundError(error)) {
    return (
      <EntityNotFound
        title={notFound.title}
        description={notFound.description}
        back={notFound.back}
        headingLevel={headingLevel}
        className={className}
      />
    )
  }
  return (
    <ErrorState
      title={title}
      description={description}
      error={error}
      onRetry={onRetry}
      compact={compact}
      headingLevel={headingLevel}
      className={className}
    />
  )
}
