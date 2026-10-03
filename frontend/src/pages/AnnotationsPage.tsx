import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CalendarRange, ExternalLink, Rocket, StickyNote, Tag, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { chartAnnotationsApi } from '@/api/chartAnnotations'
import { plannedEventsApi } from '@/api/plannedEvents'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { Chip } from '@/components/primitives/chip'
import { PageContainer } from '@/components/primitives/page-container'
import { PageHeader } from '@/components/primitives/page-header'
import { Panel } from '@/components/settings/kit'
import { SectionSkeleton } from '@/components/states'
import { IconButton } from '@/components/ui/icon-button'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { useConfirm } from '@/hooks/useConfirm'
import {
  annotationMarkerColor,
  annotationSourceLabel,
  isAutomaticAnnotation,
  safeAnnotationUrl,
} from '@/lib/chartAnnotations'
import { formatTimestamp } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { getMonitoringPath } from '@/lib/monitoring'
import { useCanWriteProject } from '@/lib/permissions'
import { plannedEventExpectation } from '@/lib/plannedEvents'
import {
  activeSignalsKey,
  allChartAnnotationsKey,
  allPlannedEventsKey,
  projectChartAnnotationsKey,
  projectMonitoringSeriesKey,
  projectPlannedEventsKey,
} from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type { ChartAnnotation, MetricScopeType, PlannedEvent } from '@/types'

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
 * Every chart annotation and planned event of the project on one page: the
 * deploys, releases and notes that charts show one series at a time, and the
 * windows in which anomalies are expected (F18). Each scoped row links to its
 * chart; editors can delete from here.
 */
export default function AnnotationsPage() {
  const { slug } = useParams<{ slug: string }>()
  const qc = useQueryClient()
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

  const deleteAnnotation = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (id: string) => chartAnnotationsApi.delete(slug!, id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: projectChartAnnotationsKey(slug) }),
    onError: error => toast.error(`Could not delete the annotation — ${getErrorMessage(error)}`),
  })
  const deletePlanned = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (id: string) => plannedEventsApi.delete(slug!, id),
    onSuccess: () => {
      // Deleting a window re-marks anomalies: refresh what reads them too.
      void qc.invalidateQueries({ queryKey: projectPlannedEventsKey(slug) })
      void qc.invalidateQueries({ queryKey: projectMonitoringSeriesKey(slug) })
      void qc.invalidateQueries({ queryKey: activeSignalsKey(slug) })
    },
    onError: error => toast.error(`Could not delete the planned event — ${getErrorMessage(error)}`),
  })

  const confirmDeleteAnnotation = async (row: ChartAnnotation) => {
    const ok = await confirm({
      title: 'Delete annotation?',
      message: `"${row.label}" will be removed from ${row.scope_type ? 'its chart' : 'every chart in this project'}.`,
      variant: 'danger',
      confirmLabel: 'Delete',
    })
    if (ok) deleteAnnotation.mutate(row.id)
  }
  const confirmDeletePlanned = async (row: PlannedEvent) => {
    const ok = await confirm({
      title: 'Delete planned event?',
      message: `Anomalies inside "${row.label}" will raise signals and alerts again.`,
      variant: 'danger',
      confirmLabel: 'Delete',
    })
    if (ok) deletePlanned.mutate(row.id)
  }

  if (!slug) return null

  return (
    <PageContainer>
      {dialog}
      <PageHeader
        eyebrow="Observe"
        title="Annotations"
        description="Every chart marker in the project — deploys, releases and notes — and the planned events in which anomalies are expected rather than alerted."
      />

      <Panel
        title={`Planned events (${planned.length})`}
        subtitle="Windows in which a move is expected: anomalies inside them are drawn but raise no alert. Add one from a chart's Volume tab."
      >
        {plannedQuery.isPending ? (
          <SectionSkeleton />
        ) : plannedQuery.isError ? (
          <ErrorState
            compact
            title="Could not load planned events"
            error={plannedQuery.error}
            onRetry={() => void plannedQuery.refetch()}
          />
        ) : planned.length === 0 ? (
          <EmptyState icon={CalendarRange} title="No planned events" size="sm" />
        ) : (
          <ul className="divide-y divide-border text-body-sm" data-testid="planned-events-list">
            {planned.map(row => (
              <li key={row.id} className="flex items-center justify-between gap-2 py-2">
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="text-fg-tertiary">
                    {formatTimestamp(row.starts_at)} – {formatTimestamp(row.ends_at)}
                  </span>
                  <span className="min-w-0 break-words font-medium">{row.label}</span>
                  <Chip variant="outline" size="xs">{plannedEventExpectation(row.direction)}</Chip>
                  <ScopeCell slug={slug} row={row} />
                </div>
                {canWrite && (
                  <IconButton
                    variant="ghost"
                    className="h-7 w-7 shrink-0 text-fg-tertiary hover:text-destructive"
                    onClick={() => void confirmDeletePlanned(row)}
                    disabled={deletePlanned.isPending && deletePlanned.variables === row.id}
                    label={`Delete planned event ${row.label}`}
                  >
                    <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />
                  </IconButton>
                )}
              </li>
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
            {annotations.map(row => {
              const automatic = isAutomaticAnnotation(row)
              const url = safeAnnotationUrl(row.url)
              return (
                <li key={row.id} className="flex items-center justify-between gap-2 py-2">
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <span
                      aria-hidden="true"
                      className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                      style={{ backgroundColor: annotationMarkerColor(row) }}
                    />
                    <span className="text-fg-tertiary">{formatTimestamp(row.bucket)}</span>
                    <span className={`min-w-0 break-words font-medium${automatic ? ' text-fg-secondary' : ''}`}>
                      {row.label}
                    </span>
                    {automatic && (
                      <Chip
                        variant="outline"
                        size="xs"
                        icon={row.source === 'release' ? <Tag aria-hidden="true" /> : <Rocket aria-hidden="true" />}
                      >
                        {annotationSourceLabel(row.source)}
                      </Chip>
                    )}
                    <ScopeCell slug={slug} row={row} />
                    {url && (
                      <a
                        href={url}
                        target="_blank"
                        rel="noopener noreferrer"
                        aria-label={`Details for ${row.label} (opens in a new tab)`}
                        className="inline-flex items-center gap-1 text-caption text-fg-tertiary hover:text-fg underline-offset-2 hover:underline"
                      >
                        Details
                        <ExternalLink aria-hidden="true" className="size-3" />
                      </a>
                    )}
                  </div>
                  {canWrite && (
                    <IconButton
                      variant="ghost"
                      className="h-7 w-7 shrink-0 text-fg-tertiary hover:text-destructive"
                      onClick={() => void confirmDeleteAnnotation(row)}
                      disabled={deleteAnnotation.isPending && deleteAnnotation.variables === row.id}
                      label={`Delete annotation ${row.label}`}
                    >
                      <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />
                    </IconButton>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </Panel>
    </PageContainer>
  )
}
