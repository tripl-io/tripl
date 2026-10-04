import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { eventsApi } from '@/api/events'
import { usersApi } from '@/api/users'
import { CodeToken } from '@/components/primitives/code-token'
import { Dot } from '@/components/primitives/dot'
import { ErrorState } from '@/components/error-state'
import { ImplementationTicketRow } from '@/components/implementation-ticket-row'
import { useActiveBranchId, useBranchLinkProps } from '@/hooks/useBranch'
import { formatRelativeTime, formatTimestamp } from '@/lib/datetime'
import { EVENT_STATUS_LABELS, type EventStatus } from '@/lib/eventStatus'
import { historyFieldLabel, historyValueLabel } from '@/lib/eventHistory'
import { resolveMetaFieldHref } from '@/lib/metaFields'
import { getMonitoringPath } from '@/lib/monitoring'
import type { Event as TEvent, EventType, MetaFieldDefinition } from '@/types'
import { SURFACE_CARD, SURFACE_STYLE } from './surface'
import { eventImplementationTicketsKey, eventKey, usersKey } from '@/lib/queryKeys'

type EventHistoryItem = {
  id: string
  field: string
  created_at: string
  new_value: string | null
  user_email?: string | null
  /** Set when no person made the change (a scan's auto-live, #258); wins over the email. */
  author_label?: string | null
}

/** Who made a history entry: the system label when set, else the person's email. */
function historyAuthor(change: EventHistoryItem): string | null {
  return change.author_label || change.user_email || null
}

function PropertyRow({ label, value, mono }: { label: string; value: ReactNode; mono?: boolean }) {
  return (
    <div role="row" className="flex gap-3 px-4 py-[6px] text-body-sm">
      <span role="rowheader" className="w-[120px] flex-shrink-0 text-fg-tertiary">{label}</span>
      <span role="cell" className={`min-w-0 flex-1 break-words ${mono ? 'mono' : ''} text-fg`}>
        {value}
      </span>
    </div>
  )
}

function EventMetaCard({
  event,
  metaFieldMap,
}: {
  event: TEvent
  metaFieldMap: Map<string, MetaFieldDefinition>
}) {
  if (event.meta_values.length === 0) return null
  return (
    <div className={SURFACE_CARD} style={SURFACE_STYLE}>
      <h2 className="m-0 border-b px-4 py-3 text-body-sm font-semibold border-border-subtle">
        Meta fields
      </h2>
      <div role="table" aria-label="Meta fields" className="py-[6px]">
        {event.meta_values.map(mv => {
          const def = metaFieldMap.get(mv.meta_field_definition_id)
          const href = def ? resolveMetaFieldHref(def, mv.value) : null
          const display = def?.field_type === 'boolean'
            ? (mv.value === 'true' ? '✓' : '✗')
            : (mv.value || '—')
          return (
            <PropertyRow
              key={mv.id}
              label={def?.display_name ?? def?.name ?? 'Unknown'}
              value={href
                ? <a href={href} target="_blank" rel="noopener noreferrer" className="underline text-accent">{mv.value}</a>
                : display}
              mono
            />
          )
        })}
      </div>
    </div>
  )
}

function EventTicketsCard({ slug, event }: { slug: string; event: TEvent }) {
  const branchId = useActiveBranchId()
  const { data: tickets } = useQuery({
    queryKey: eventImplementationTicketsKey(slug, branchId, event.id),
    queryFn: () => eventsApi.implementationTickets(slug, event.id, branchId),
  })
  // Hidden, not empty. Rows exist only where the Jira integration is on and a
  // branch has merged, so "no tickets" is the normal state for most events and
  // an empty card would be noise on every one of them — the same rule the
  // branch panel states for itself. No merged-status gate here: an event has
  // no branch status to gate on, and these tickets come from branches that
  // already merged.
  if (!tickets || tickets.length === 0) return null
  return (
    <div className={SURFACE_CARD} style={SURFACE_STYLE}>
      <h2
        className="m-0 border-b px-4 py-3 text-body-sm font-semibold border-border-subtle"
      >
        Implementation tickets
      </h2>
      <div>
        {tickets.map(ticket => (
          <ImplementationTicketRow key={ticket.id} ticket={ticket} />
        ))}
      </div>
    </div>
  )
}

export function EventSideColumn({
  slug,
  event,
  eventType,
  history,
  historyError,
  onRetryHistory,
  metaFieldMap,
}: {
  slug: string
  event: TEvent
  eventType: EventType | undefined
  history: EventHistoryItem[]
  /** The history request's failure; the card says so instead of "No recent changes". */
  historyError?: unknown
  onRetryHistory?: () => void
  metaFieldMap: Map<string, MetaFieldDefinition>
}) {
  const breakdowns = event.metric_breakdown_columns
  const activeBranchId = useActiveBranchId()
  const branchLink = useBranchLinkProps()
  const usersQuery = useQuery({
    queryKey: usersKey(),
    queryFn: () => usersApi.list(),
    enabled: Boolean(event.owner_id),
  })
  // The successor is guaranteed to sit on the same branch as this event (the
  // server refuses a cross-branch pointer), so it resolves against the row's
  // OWN branch — the same fallback the edit link uses, since a detail page can
  // answer for a branch that is not the active one.
  const successorBranchId = event.branch_id ?? activeBranchId
  const successorId = event.superseded_by_event_id ?? null
  const successorQuery = useQuery({
    // Same key shape as the page's own event query, so a successor already
    // visited is read from cache instead of refetched.
    queryKey: eventKey(slug, successorBranchId, successorId),
    queryFn: () => eventsApi.get(slug, successorId!, successorBranchId),
    enabled: Boolean(successorId),
  })
  const owner = event.owner_id ? usersQuery.data?.find(user => user.id === event.owner_id) : undefined
  // An owner the roster no longer lists (a removed member, or a roster the
  // request could not fetch) reads as unknown, not as still loading.
  const ownerLabel = !event.owner_id
    ? '—'
    : owner
      ? owner.name || owner.email
      : usersQuery.isPending
        ? '…'
        : 'Unknown user'
  return (
    <div className="flex flex-col gap-[14px]">
      <div className={SURFACE_CARD} style={SURFACE_STYLE}>
        <h2 className="m-0 border-b px-4 py-3 text-body-sm font-semibold border-border-subtle">
          Properties
        </h2>
        <div role="table" aria-label="Properties" className="py-[6px]">
          <PropertyRow label="Event type" value={eventType?.display_name ?? event.event_type?.display_name ?? '—'} />
          <PropertyRow label="Status" value={EVENT_STATUS_LABELS[event.status as EventStatus] ?? event.status} />
          <PropertyRow label="Event ID" value={event.id} mono />
          {!!event.source_name && event.source_name !== event.name && (
            // Shown only when the two have parted. The scan matches on
            // source_name, so once a rename moves the display name away from it
            // this row is the only place that says which event the warehouse is
            // still feeding. When they agree the name IS the
            // identity and a second row saying so would be noise.
            <PropertyRow label="Scan identity" value={event.source_name} mono />
          )}
          <PropertyRow label="Owner" value={ownerLabel} />
          {/* Authored and seen are two dates: an event planned before it
              shipped was "first seen" on a day nothing was.
              First seen is set once, by the scan that first saw volume (#258). */}
          <PropertyRow label="Created" value={formatTimestamp(event.created_at)} />
          <PropertyRow label="First seen in data" value={event.first_seen_at ? formatTimestamp(event.first_seen_at) : '—'} />
          <PropertyRow label="Updated" value={formatRelativeTime(event.updated_at)} />
          <PropertyRow label="Last seen" value={event.last_seen_at ? formatTimestamp(event.last_seen_at) : '—'} />
          {event.sunset_at && <PropertyRow label="Sunset" value={formatTimestamp(event.sunset_at)} />}
          {/* What to send instead. Shown whenever the pointer is set, not only
              on a deprecated event: an analyst can name the successor while the
              old event is still live, and hiding the row until the status flips
              would lose the one answer the retirement notice owes its reader.
              Falls back to the raw id if the successor cannot
              be loaded — a link to a name we do not have is worse than the id. */}
          {successorId && (
            <PropertyRow
              label="Replaced by"
              mono={!successorQuery.data}
              value={
                successorQuery.data ? (
                  <Link
                    {...branchLink(
                      getMonitoringPath(slug, { scope_type: 'event', scope_ref: successorId }),
                      successorBranchId,
                    )}
                    className="underline underline-offset-2 text-fg"
                  >
                    {successorQuery.data.name}
                  </Link>
                ) : successorQuery.isPending ? (
                  '…'
                ) : (
                  successorId
                )
              }
            />
          )}
        </div>
      </div>

      <EventMetaCard event={event} metaFieldMap={metaFieldMap} />

      <EventTicketsCard slug={slug} event={event} />

      <div className={SURFACE_CARD} style={SURFACE_STYLE}>
        <h2 className="m-0 border-b px-4 py-3 text-body-sm font-semibold border-border-subtle">
          Metric breakdowns
        </h2>
        <div className="flex flex-wrap gap-[6px] px-4 py-[12px]">
          {breakdowns.length > 0
            // Column names are identifiers: code tokens, not pills.
            ? breakdowns.map(column => <CodeToken key={column}>{column}</CodeToken>)
            : <span className="text-body-sm text-fg-tertiary">No event-level breakdowns</span>}
        </div>
      </div>

      <div className={SURFACE_CARD} style={SURFACE_STYLE}>
        <h2 className="m-0 border-b px-4 py-3 text-body-sm font-semibold border-border-subtle">
          Recent activity
        </h2>
        <div className="py-[4px]">
          {historyError ? (
            <div className="px-4 py-3">
              <ErrorState
                compact
                title="Could not load recent activity"
                error={historyError}
                onRetry={onRetryHistory}
              />
            </div>
          ) : history.length === 0 ? (
            <div className="px-4 py-5 text-center text-fg-tertiary">
              <p className="text-caption font-medium text-fg-secondary">
                No recent changes
              </p>
              <p className="mt-1 text-micro">
                Edits to this event's definition will show up here.
              </p>
            </div>
          ) : history.slice(0, 4).map(change => (
            <div key={change.id} className="flex gap-[10px] border-t px-4 py-2 border-border-subtle">
              <Dot tone="neutral" size={6} className="mt-[5px]" />
              <div className="min-w-0 flex-1">
                <div className="text-caption font-medium">
                  <span className={change.field.startsWith('field:') || change.field.startsWith('meta:') ? 'mono' : ''}>
                    {historyFieldLabel(change.field)}
                  </span>
                  {historyValueLabel(change.field, change.new_value) != null && (
                    <span className="text-fg-secondary"> → {historyValueLabel(change.field, change.new_value)}</span>
                  )}
                </div>
                <div className="mt-[2px] text-micro text-fg-tertiary">
                  {formatRelativeTime(change.created_at)}
                  {historyAuthor(change) && (
                    <span data-testid="event-history-author"> · {historyAuthor(change)}</span>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
