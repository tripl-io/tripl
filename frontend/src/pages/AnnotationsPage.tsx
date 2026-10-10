import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { CalendarRange, StickyNote } from 'lucide-react'
import { chartAnnotationsApi } from '@/api/chartAnnotations'
import { plannedEventsApi } from '@/api/plannedEvents'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { Chip } from '@/components/primitives/chip'
import { PageContainer } from '@/components/primitives/page-container'
import { PageHeader } from '@/components/primitives/page-header'
import { Panel } from '@/components/settings/kit'
import { SectionSkeleton } from '@/components/states'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { useConfirm } from '@/hooks/useConfirm'
import { getMonitoringPath } from '@/lib/monitoring'
import { currentOrgSlug, projectPath } from '@/lib/navigation'
import { useCanWriteProject } from '@/lib/permissions'
import { allChartAnnotationsKey, allPlannedEventsKey } from '@/lib/queryKeys'
import type { ChartAnnotation, MetricScopeType, PlannedEvent } from '@/types'
import { PlannedWindowSuggestions } from './annotations/PlannedWindowSuggestions'
import { AnnotationItem } from './monitoring/AnnotationItem'
import { annotationDeleteConfirm, useAnnotationDelete } from './monitoring/annotationMutations'
import { ExpectedWindowItem } from './monitoring/ExpectedWindowItem'
import { plannedEventDeleteConfirm, usePlannedEventDelete } from './monitoring/plannedEventMutations'

type SourceFilter = 'all' | 'manual' | 'release' | 'api'

const SOURCE_OPTIONS: { value: SourceFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'manual', label: 'Manual' },
  { value: 'release', label: 'Releases' },
  { value: 'api', label: 'API' },
]

const SCOPE_NOUN: Record<string, string> = {
  project_total: 'Project total',
  event_type: 'Event type',
  event: 'Event',
  metric: 'Metric',
}

interface ScopedRow {
  scope_type: string | null
  scope_ref: string | null
  scope_name?: string | null
}

/** "Event · Purchase" linking to its chart, or a project-wide chip. */
function ScopeCell({ slug, row }: { slug: string; row: ScopedRow }) {
  if (!row.scope_type || !row.scope_ref) return <Chip variant="outline" size="xs">project-wide</Chip>
  const noun = SCOPE_NOUN[row.scope_type] ?? row.scope_type
  if (!row.scope_name) {
    // The series was deleted: nothing to link to.
    return <span className="text-fg-tertiary">{noun} · deleted</span>
  }
  const href = getMonitoringPath(slug, {
    scope_type: row.scope_type as MetricScopeType,
    scope_ref: row.scope_ref,
  })
  return (
    <span className="text-fg-tertiary">
      {noun} ·{' '}
      <Link to={href} className="text-fg underline-offset-2 hover:underline">{row.scope_name}</Link>
    </span>
  )
}

/**
 * Every chart annotation and expected window (a `planned_event` in the API) of
 * the project on one page: the deploys, releases and notes that charts show
 * one series at a time, and the windows in which anomalies are expected (F18).
 * Each scoped row links to its chart; editors can delete from here, with the
 * same confirm and the same refresh as from the chart.
 */
export default function AnnotationsPage() {
  const { slug } = useParams<{ slug: string }>()
  const canWrite = useCanWriteProject()
  const { confirm, dialog } = useConfirm()
  const [source, setSource] = useState<SourceFilter>('all')

  const annotationsQuery = useQuery({
    queryKey: allChartAnnotationsKey(slug),
    queryFn: () => chartAnnotationsApi.list(slug!),
    enabled: !!slug,
  })
  const plannedQuery = useQuery({
    queryKey: allPlannedEventsKey(slug),
    queryFn: () => plannedEventsApi.list(slug!),
    enabled: !!slug,
  })

  // Newest first: the list is read from "what just happened" backwards.
  const annotations = useMemo(
    () =>
      [...(annotationsQuery.data ?? [])]
        .filter(row => source === 'all' || (row.source ?? 'manual') === source)
        .sort((a, b) => b.bucket.localeCompare(a.bucket)),
    [annotationsQuery.data, source],
  )
  const planned = useMemo(
    () => [...(plannedQuery.data ?? [])].sort((a, b) => b.starts_at.localeCompare(a.starts_at)),
    [plannedQuery.data],
  )

  const deleteAnnotation = useAnnotationDelete(slug)
  const deletePlanned = usePlannedEventDelete(slug)
  const confirmDeleteAnnotation = async (row: ChartAnnotation) => {
    if (await confirm(annotationDeleteConfirm(row))) deleteAnnotation.mutate(row.id)
  }
  const confirmDeletePlanned = async (row: PlannedEvent) => {
    if (await confirm(plannedEventDeleteConfirm(row))) deletePlanned.mutate(row.id)
  }

  if (!slug) return null

  return (
    <PageContainer>
      {dialog}
      <PageHeader
        eyebrow="Observe"
        title="Annotations"
        description="Every chart marker in the project — deploys, releases and notes — and the expected windows, in which anomalies are drawn but never become a signal or an alert."
      />

      {canWrite && <PlannedWindowSuggestions slug={slug} />}

      {/* "Expected windows", not "planned events": in a tracking-plan product
          that name already belongs to the events of the plan. */}
      <Panel
        title={`Expected windows (${planned.length})`}
        subtitle="A campaign, sale or holiday you expect to move a chart. Anomalies inside one are drawn muted and never become a signal or an alert. Add one under any event or metric chart."
      >
        {plannedQuery.isPending ? (
          <SectionSkeleton />
        ) : plannedQuery.isError ? (
          <ErrorState
            compact
            title="Could not load expected windows"
            error={plannedQuery.error}
            onRetry={() => void plannedQuery.refetch()}
          />
        ) : planned.length === 0 ? (
          <EmptyState
            icon={CalendarRange}
            title="No expected windows"
            description={
              <>
                Once a scan collects volume, mark one under any event or metric chart, or add a
                country&rsquo;s public holidays from{' '}
                <Link
                  to={projectPath(currentOrgSlug(), slug, '/settings/monitoring')}
                  className="text-accent"
                >
                  Detection settings
                </Link>
                .
              </>
            }
            size="sm"
          />
        ) : (
          <ul className="divide-y divide-border text-body-sm" data-testid="planned-events-list">
            {planned.map(row => (
              <ExpectedWindowItem
                key={row.id}
                inset
                event={row}
                scope={<ScopeCell slug={slug} row={row} />}
                onDelete={canWrite ? () => void confirmDeletePlanned(row) : undefined}
                deleting={deletePlanned.isPending && deletePlanned.variables === row.id}
              />
            ))}
          </ul>
        )}
      </Panel>

      <Panel
        title={`Chart annotations (${annotations.length})`}
        right={
          <SegmentedControl
            size="sm"
            value={source}
            onChange={setSource}
            options={SOURCE_OPTIONS}
            aria-label="Annotation source"
          />
        }
      >
        {annotationsQuery.isPending ? (
          <SectionSkeleton />
        ) : annotationsQuery.isError ? (
          <ErrorState
            compact
            title="Could not load annotations"
            error={annotationsQuery.error}
            onRetry={() => void annotationsQuery.refetch()}
          />
        ) : annotations.length === 0 ? (
          <EmptyState
            icon={StickyNote}
            title={source === 'all' ? 'No annotations yet' : 'No annotations from this source'}
            description={source === 'all' ? 'Add one from a chart, or post one from your deploy pipeline.' : undefined}
            size="sm"
          />
        ) : (
          <ul className="divide-y divide-border text-body-sm" data-testid="annotations-list">
            {annotations.map(row => (
              <AnnotationItem
                key={row.id}
                inset
                annotation={row}
                scope={<ScopeCell slug={slug} row={row} />}
                onDelete={canWrite ? () => void confirmDeleteAnnotation(row) : undefined}
                deleting={deleteAnnotation.isPending && deleteAnnotation.variables === row.id}
              />
            ))}
          </ul>
        )}
      </Panel>
    </PageContainer>
  )
}
