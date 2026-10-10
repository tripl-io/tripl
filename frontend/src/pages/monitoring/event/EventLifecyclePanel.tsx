import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Hourglass } from 'lucide-react'
import { eventsApi } from '@/api/events'
import { useActiveBranchId, useBranchLinkProps } from '@/hooks/useBranch'
import { formatRelativeTime, formatTimestamp } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { formatNumber } from '@/lib/format'
import { getMonitoringPath } from '@/lib/monitoring'
import { eventMigrationKey } from '@/lib/queryKeys'
import type { Event as TEvent, EventMigration, LifecycleFinding } from '@/types'
import { SURFACE_CARD, SURFACE_STYLE } from './surface'
import { countOf } from '@/lib/plural'

/** A daily average as a whole count: "1,240/day". */
function perDay(value: number): string {
  return `${formatNumber(Math.round(value))}/day`
}

/**
 * What one open finding says. The successor-silent finding reads from either
 * side: on the deprecated event (`event_id`) it is about the replacement, on
 * the replacement (`related_event_id`) it is about itself. The side is decided
 * by which id this page's event is, not by its status — a successor can be
 * deprecated in turn, and the deprecated one's status can be edited.
 */
function findingText(finding: LifecycleFinding, event: TEvent): { title: string; detail: string } {
  if (finding.kind === 'sunset_overdue') {
    const volume = finding.volume_24h ?? 0
    const sunset = event.sunset_at ? ` (sunset ${formatTimestamp(event.sunset_at)})` : ''
    return {
      title: 'Past its sunset date and still receiving data',
      detail: `${countOf(volume, 'event', 'events')} in the last 24h${sunset}.`,
    }
  }
  const volume = finding.successor_volume_7d ?? 0
  const count = `${countOf(volume, 'event', 'events')} in the last 7 days.`
  const isSuccessorPage = finding.related_event_id != null
    && finding.related_event_id === event.id
    && finding.event_id !== event.id
  return isSuccessorPage
    ? { title: 'Replaces a deprecated event but receives no data', detail: `This event received ${count}` }
    : { title: 'Its successor has gone silent', detail: `The replacement event received ${count}` }
}

function MigrationProgress({ slug, migration, branchId }: {
  slug: string
  migration: EventMigration
  branchId: string | null
}) {
  const branchLink = useBranchLinkProps()
  const ratio = migration.ratio
  return (
    <div data-testid="event-migration" className="flex flex-wrap items-center gap-x-2 gap-y-1 px-4 py-3 text-body-sm">
      <span className="text-fg-tertiary">Migration</span>{' '}
      <span>
        Old <span className="tnum font-medium">{perDay(migration.old.daily_avg_7d)}</span>
      </span>{' '}
      <span className="text-fg-tertiary">→</span>{' '}
      <span>
        New <span className="tnum font-medium">{perDay(migration.new.daily_avg_7d)}</span>
      </span>{' '}
      <span className="text-fg-tertiary">
        (
        <Link
          {...branchLink(
            getMonitoringPath(slug, { scope_type: 'event', scope_ref: migration.new.event_id }),
            branchId,
          )}
          className="mono underline underline-offset-2 text-fg-secondary"
        >
          {migration.new.name}
        </Link>
        {ratio != null && <>, {formatNumber(ratio, { maximumFractionDigits: 1 })}× the old volume</>}
        , 7-day average)
      </span>
    </div>
  )
}

/**
 * The event page's lifecycle block (#258): open sunset-watch findings, and for
 * a deprecated event with a successor, how far traffic has moved across. Hidden
 * when there is nothing to say — most events are neither deprecated nor
 * replacing one, and an empty card on each of them would be noise.
 */
export function EventLifecyclePanel({ slug, event }: { slug: string; event: TEvent }) {
  const activeBranchId = useActiveBranchId()
  const branchId = event.branch_id ?? activeBranchId
  const findings = (event.lifecycle_findings ?? []).filter(finding => !finding.resolved_at)
  const successorId = event.superseded_by_event_id ?? null
  const wantsMigration = event.status === 'deprecated' && Boolean(successorId)
  const migrationQuery = useQuery({
    queryKey: eventMigrationKey(slug, branchId, event.id),
    queryFn: ({ signal }) => eventsApi.migration(slug, event.id, branchId, signal),
    enabled: wantsMigration && Boolean(slug),
    // A side panel: a failed read hides the progress line rather than toasting.
    meta: SILENT_ERROR_META,
  })
  const migration = wantsMigration ? migrationQuery.data : undefined

  if (findings.length === 0 && !migration) return null
  return (
    <section
      aria-label="Lifecycle"
      className={SURFACE_CARD}
      style={SURFACE_STYLE}
    >
      <h2 className="m-0 border-b px-4 py-3 text-body-sm font-semibold border-border-subtle">
        Lifecycle
      </h2>
      {findings.length > 0 && (
        <ul className="m-0 list-none p-0">
          {findings.map(finding => {
            const { title, detail } = findingText(finding, event)
            return (
              <li
                key={finding.id}
                data-kind={finding.kind}
                className="flex items-start gap-2 border-b px-4 py-3 text-body-sm bg-warning-soft border-border-subtle"
              >
                <Hourglass aria-hidden="true" className="mt-[2px] h-3.5 w-3.5 shrink-0 text-warning" />
                <div className="min-w-0">
                  <div className="font-medium text-fg">{title}</div>
                  <div className="text-fg-secondary">
                    {detail}{' '}
                    <span className="text-fg-tertiary">
                      Flagged {formatRelativeTime(finding.first_seen_at)}.
                    </span>
                  </div>
                </div>
              </li>
            )
          })}
        </ul>
      )}
      {migration && <MigrationProgress slug={slug} migration={migration} branchId={branchId} />}
    </section>
  )
}
