import { useMemo, useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, Copy, ExternalLink } from 'lucide-react'
import { toast } from 'sonner'
import { duplicatesApi } from '@/api/duplicates'
import { eventsApi } from '@/api/events'
import { projectsApi } from '@/api/projects'
import { ConfirmImpactMessage } from '@/components/dependencies/ImpactNotice'
import { duplicateEventPath, formatDuplicateScore } from '@/components/duplicates/duplicateHints'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { EventName } from '@/components/event-name'
import { Chip } from '@/components/primitives/chip'
import { PageContainer } from '@/components/primitives/page-container'
import { PageHeader } from '@/components/primitives/page-header'
import { Panel } from '@/components/settings/kit'
import { ReadOnlyNotice, SectionSkeleton } from '@/components/states'
import { Button } from '@/components/ui/button'
import { useActiveBranchId } from '@/hooks/useBranch'
import { useConfirm } from '@/hooks/useConfirm'
import { DOCS_SITE_URL } from '@/lib/docsSite'
import { eventNameLabel } from '@/lib/eventName'
import { EVENT_STATUS_LABELS, EVENT_STATUS_TONE } from '@/lib/eventStatus'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useCanWriteProject } from '@/lib/permissions'
import { branchEventsKey, duplicateClustersKey, projectEventKey, projectKey } from '@/lib/queryKeys'
import type { DuplicateCluster, DuplicateClusterEvent } from '@/types'
import { clusterKey, proposedKeeper, uniqueClusters, volumeLabel } from './duplicates/duplicateClusters'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

/** How the view finds and groups duplicates, for a reader looking at none. */
const DUPLICATES_DOCS_URL = `${DOCS_SITE_URL}/use/duplicates-and-naming#the-duplicates-view`

/**
 * Likely duplicates already in the catalog (F12, #265): events that look
 * alike pairwise at or above the threshold, grouped into clusters.
 *
 * Merging is not a new operation. "Set successor & deprecate" is the ordinary
 * event update — the retired event is deprecated with the kept one as its
 * successor, exactly what the event form does — so history, the migration
 * view (#258) and the dependents notice (#257) all apply unchanged. "Not a
 * duplicate" records the pair so it stops being reported.
 */
export default function DuplicatesPage() {
  const { slug } = useParams<{ slug: string }>()
  const branchId = useActiveBranchId()
  const qc = useQueryClient()
  const canWrite = useCanWriteProject()
  const { confirm, dialog } = useConfirm()
  // The member each cluster keeps, when the reader picked one other than the
  // proposal. Keyed by cluster so a refetch that reorders keeps the choice.
  const [keepers, setKeepers] = useState<Record<string, string>>({})

  // The plan's size, so an empty project is not told its check came back clean.
  const projectQuery = useQuery({
    queryKey: projectKey(slug),
    queryFn: () => projectsApi.get(slug!),
    enabled: !!slug,
  })
  const clustersQuery = useInfiniteQuery({
    queryKey: duplicateClustersKey(slug, branchId),
    queryFn: ({ pageParam, signal }) => duplicatesApi.clusters(slug!, pageParam, branchId, signal),
    initialPageParam: null as string | null,
    getNextPageParam: lastPage => lastPage.next_cursor ?? undefined,
    enabled: !!slug,
  })
  // Pages are cut from a list that can shift between requests (a dismissal, a
  // merge, a new event), so the same cluster can arrive on two pages. Keyed by
  // its members, it would render twice under one React key.
  const clusters = useMemo(
    () => uniqueClusters(clustersQuery.data?.pages.flatMap(page => page.items) ?? []),
    [clustersQuery.data],
  )

  const refresh = () => {
    // The clusters sit under the branch's events prefix, so this refreshes
    // them, the events list and every open duplicate check at once.
    void qc.invalidateQueries({ queryKey: branchEventsKey(slug, branchId) })
    void qc.invalidateQueries({ queryKey: projectEventKey(slug) })
  }

  const dismissMutation = useMutation({
    // Its failure is toasted below, with the pair named.
    meta: SILENT_ERROR_META,
    mutationFn: ({ a, b }: { a: string; b: string }) =>
      duplicatesApi.dismiss(slug!, { event_a_id: a, event_b_id: b }, branchId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: duplicateClustersKey(slug, branchId) })
    },
  })

  const replace = async (retired: DuplicateClusterEvent, kept: DuplicateClusterEvent) => {
    if (!slug) return
    const ok = await confirm({
      title: `Deprecate ${eventNameLabel(retired.name)}?`,
      message: (
        <ConfirmImpactMessage
          slug={slug}
          branchId={branchId}
          changes={[{ kind: 'event', id: retired.id, change: 'deprecate' }]}
          message={
            <p className="m-0">
              “{eventNameLabel(retired.name)}” is deprecated with “{eventNameLabel(kept.name)}” as its
              successor. Its history stays; the migration view compares the two until the old one
              goes quiet.
            </p>
          }
        />
      ),
      confirmLabel: 'Set successor & deprecate',
      variant: 'primary',
      pendingLabel: 'Deprecating…',
      errorPrefix: `Could not deprecate ${eventNameLabel(retired.name)}`,
      action: () =>
        eventsApi.update(
          slug,
          retired.id,
          { status: 'deprecated', superseded_by_event_id: kept.id },
          branchId,
        ),
    })
    if (!ok) return
    toast.success(`Deprecated ${eventNameLabel(retired.name)}; ${eventNameLabel(kept.name)} replaces it.`)
    refresh()
  }

  const dismiss = (cluster: DuplicateCluster, a: DuplicateClusterEvent, b: DuplicateClusterEvent) => {
    dismissMutation.mutate(
      { a: a.id, b: b.id },
      {
        onSuccess: () => {
          toast.success(`${eventNameLabel(a.name)} and ${eventNameLabel(b.name)} are no longer reported as duplicates.`)
          // The keeper choice belonged to a cluster that has now changed shape.
          setKeepers(current => {
            const next = { ...current }
            delete next[clusterKey(cluster)]
            return next
          })
        },
        onError: err => {
          toast.error(err instanceof Error ? err.message : 'Could not dismiss the pair')
        },
      },
    )
  }

  let body: ReactNode
  // An empty answer waits for the plan's size too, so an empty plan never
  // flashes the all-clear first.
  const awaitingPlanSize = !clustersQuery.isError && clusters.length === 0 && projectQuery.isPending
  if (clustersQuery.isPending || awaitingPlanSize) {
    body = <SectionSkeleton variant="rows" rows={4} label="Looking for likely duplicates…" />
  } else if (clustersQuery.isError) {
    body = (
      <ErrorState
        title="Could not load the duplicates"
        error={clustersQuery.error}
        onRetry={() => void clustersQuery.refetch()}
      />
    )
  } else if (clusters.length === 0 && projectQuery.data?.summary.event_count === 0) {
    // "No likely duplicates" read as a check that passed, on a plan with
    // nothing in it to compare.
    body = (
      <EmptyState
        icon={Copy}
        title="No events to compare yet"
        description="Duplicates are looked for among Live, Implemented and Ready for dev events of the same event type. Add events to your plan, and any that look alike show up here."
        action={
          slug ? (
            <Button asChild size="sm">
              <Link to={projectPath(currentOrgSlug(), slug, '/events')} className="no-underline">
                Go to Events
                <ArrowRight aria-hidden="true" />
              </Link>
            </Button>
          ) : undefined
        }
      />
    )
  } else if (clusters.length === 0) {
    // The status names as the Events list prints them: "ready" was the
    // internal ready_for_dev.
    body = (
      <EmptyState
        icon={Copy}
        title="No likely duplicates"
        description="No two Live, Implemented or Ready for dev events of the same event type look alike enough to be the same event."
        action={
          <a
            href={DUPLICATES_DOCS_URL}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-body-sm text-accent"
          >
            How duplicates are found
            <ExternalLink aria-hidden="true" className="size-3.5" />
          </a>
        }
      />
    )
  } else {
    body = (
      <div className="space-y-4">
        {clusters.map(cluster => {
          const key = clusterKey(cluster)
          const kept =
            cluster.events.find(event => event.id === keepers[key]) ?? proposedKeeper(cluster)
          return (
            <ClusterPanel
              key={key}
              slug={slug!}
              cluster={cluster}
              clusterId={key}
              kept={kept}
              canWrite={canWrite}
              busy={dismissMutation.isPending}
              onKeep={id => setKeepers(current => ({ ...current, [key]: id }))}
              onReplace={retired => {
                if (kept) void replace(retired, kept)
              }}
              onDismiss={other => {
                if (kept) dismiss(cluster, kept, other)
              }}
            />
          )
        })}
        {clustersQuery.hasNextPage && (
          <div className="flex justify-center">
            <Button
              variant="outline"
              size="sm"
              disabled={clustersQuery.isFetchingNextPage}
              onClick={() => void clustersQuery.fetchNextPage()}
            >
              {clustersQuery.isFetchingNextPage ? 'Loading…' : 'Load more'}
            </Button>
          </div>
        )}
      </div>
    )
  }

  return (
    <PageContainer>
      {dialog}
      <PageHeader
        eyebrow="Govern"
        title="Duplicates"
        description="Events that look like the same thing under different names. Keep one, and retire the others with it as their successor."
        actions={
          slug ? (
            // The sibling Govern view, drawn as Coverage draws its link there.
            <Button asChild variant="outline" size="sm">
              <Link to={projectPath(currentOrgSlug(), slug, '/reconciliation')} className="no-underline">
                <ArrowRight aria-hidden="true" />
                Reconciliation
              </Link>
            </Button>
          ) : undefined
        }
      />
      {!canWrite && <ReadOnlyNotice className="mb-4" />}
      {body}
    </PageContainer>
  )
}

function ClusterPanel({
  slug,
  cluster,
  clusterId,
  kept,
  canWrite,
  busy,
  onKeep,
  onReplace,
  onDismiss,
}: {
  slug: string
  cluster: DuplicateCluster
  clusterId: string
  kept: DuplicateClusterEvent | undefined
  canWrite: boolean
  busy: boolean
  onKeep: (eventId: string) => void
  onReplace: (retired: DuplicateClusterEvent) => void
  onDismiss: (other: DuplicateClusterEvent) => void
}) {
  const radioName = `keep-${clusterId}`
  return (
    <Panel
      title={`${cluster.events.length} events · ${formatDuplicateScore(cluster.score)} alike`}
      headingLevel={2}
    >
      <ul className="m-0 list-none p-0" aria-label="Events in this cluster">
        {cluster.events.map(event => {
          const isKept = kept?.id === event.id
          const label = eventNameLabel(event.name)
          return (
            <li
              key={event.id}
              className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-t px-4 py-2 first:border-t-0 border-border-subtle"
            >
              {canWrite && (
                <label className="inline-flex items-center gap-1.5 text-caption text-fg-tertiary">
                  <input
                    type="radio"
                    name={radioName}
                    checked={isKept}
                    onChange={() => onKeep(event.id)}
                    aria-label={`Keep ${label}`}
                  />
                  Keep
                </label>
              )}
              <span className="mono min-w-0 flex-1 truncate text-body-sm text-fg">
                <EventName name={event.name} />
              </span>
              <Chip size="xs" tone={EVENT_STATUS_TONE[event.status]}>
                {EVENT_STATUS_LABELS[event.status]}
              </Chip>
              <span className="tnum text-caption text-fg-tertiary">{volumeLabel(event.volume_7d)}</span>
              <div className="flex shrink-0 flex-wrap items-center gap-1.5">
                <Button asChild size="sm" variant="ghost">
                  <Link to={duplicateEventPath(slug, event.id)} className="no-underline" aria-label={`Open ${label}`}>
                    Open
                  </Link>
                </Button>
                {canWrite && kept && !isKept && (
                  <>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy}
                      onClick={() => onReplace(event)}
                      aria-label={`Set ${eventNameLabel(kept.name)} as successor of ${label} and deprecate it`}
                    >
                      Set successor &amp; deprecate
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy}
                      onClick={() => onDismiss(event)}
                      aria-label={`${label} is not a duplicate of ${eventNameLabel(kept.name)}`}
                    >
                      Not a duplicate
                    </Button>
                  </>
                )}
                {canWrite && isKept && cluster.events.length > 1 && (
                  <span className="text-caption text-fg-tertiary">Kept</span>
                )}
              </div>
            </li>
          )
        })}
      </ul>
    </Panel>
  )
}
