import { Link } from 'react-router-dom'
import { ListPlus, Plus, Radar } from 'lucide-react'
import { EmptyState } from '@/components/empty-state'
import { Button } from '@/components/ui/button'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

interface EventsFirstRunProps {
  slug: string | undefined
  canWrite: boolean
  /** The event types have loaded, and there are none. */
  noEventTypes: boolean
  onNewEvent: () => void
  onBulkNew: () => void
}

/**
 * The Events page of a project with no events: no stat strip of zeroes, no
 * table frame with column headers over nothing, and one place to start —
 * including the scan path, which is how most events arrive.
 *
 * Every event belongs to an event type. Where the project has none, "New
 * event" and "Add many events" led to forms that could not create anything,
 * so the first step offered is the type. A scan creates types and events
 * both, so it stays on offer either way.
 */
export function EventsFirstRun({ slug, canWrite, noEventTypes, onNewEvent, onBulkNew }: EventsFirstRunProps) {
  const scanButton = (
    <Button asChild size="sm" variant="outline">
      <Link to={projectPath(currentOrgSlug(), slug, '/scans')}>
        <Radar />
        Import from a scan
      </Link>
    </Button>
  )

  if (noEventTypes) {
    return (
      <EmptyState
        icon={ListPlus}
        title="No events yet"
        description="Every event belongs to an event type. Create one, then add events by hand or paste a list — or let a warehouse scan import both."
        action={
          <div className="flex flex-wrap justify-center gap-2">
            {canWrite && (
              <Button asChild size="sm">
                <Link to={projectPath(currentOrgSlug(), slug, '/event-types')}>
                  <Plus />
                  Create an event type
                </Link>
              </Button>
            )}
            {scanButton}
          </div>
        }
      />
    )
  }

  return (
    <EmptyState
      icon={ListPlus}
      title="No events yet"
      description="Define events by hand, paste a list, or let a warehouse scan discover them."
      action={
        <div className="flex flex-col items-center gap-3">
          <div className="flex flex-wrap justify-center gap-2">
            {canWrite && (
              <Button onClick={onNewEvent} size="sm">
                <Plus />
                New event
              </Button>
            )}
            {scanButton}
          </div>
          {canWrite && (
            <button
              type="button"
              onClick={onBulkNew}
              className="text-body-sm text-primary underline-offset-4 hover:underline"
            >
              Add many events…
            </button>
          )}
        </div>
      }
    />
  )
}
