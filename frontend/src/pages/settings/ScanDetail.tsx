import { Fragment, useState } from "react"
import { Panel } from '@/components/settings/kit'
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Ban, ChevronDown, GitMerge, RotateCcw, XCircle } from "lucide-react"
import { scansApi } from "@/api/scans"
import { useDemoScenarioActions, useScenarioArtifacts } from "@/demo/demoScenarioContext"
import { ScenarioCoachMark } from "@/demo/ScenarioCoachMark"
import type { DataSource, EventType, ScanConfig, ScanJob } from "@/types"
import { Button } from "@/components/ui/button"
import { IconButton } from "@/components/ui/icon-button"
import { Chip } from "@/components/primitives/chip"
import { ErrorState } from "@/components/error-state"
import { SectionSkeleton, StatValueSkeleton } from '@/components/states'
import { countOf } from '@/lib/plural'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { getErrorMessage } from '@/lib/utils'
import { friendlyScanError } from '@/lib/scanError'
import { formatRelativeTime } from '@/lib/datetime'
import {
  KV,
  NoneTag,
  SrcIcon,
} from './scans/scanLayout'
import { MiniStat, MiniStatStrip } from '@/components/primitives/mini-stat'
import { RunStatusPill } from './scans/ScanConfigRow'
import { runPillStatus } from './scans/scanRunStatus'
import { JobDetails } from './scans/JobDetails'
import { ReplayChunkProgress } from './scans/ReplayChunkProgress'
import { jobRowsReadTitle } from './scans/runReport'
import { SCAN_MODE_DETAIL_LABEL, type ScanMode, scanModeOf } from './scans/scanMode'
import {
  consecutiveFailedRuns,
  formatDueIn,
  formatJobScanned,
  jobDurationSeconds,
  jobMetricPoints,
  jobScanned,
  metricsFreshness,
  scanJobsHaveActiveWork,
  type MetricsSchedule,
} from './scans/scanUtils'
import { useAdaptiveRefetchIntervalFn } from '@/realtime/streamContext'
import {
  platformPresenceKey,
  projectEventsKey,
  projectEventTypesKey,
  scanActivityKey,
  scanJobsKey,
  scansKey,
} from '@/lib/queryKeys'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useCanWriteProject, useIsOwner } from '@/lib/permissions'
import { useConfirm } from '@/hooks/useConfirm'
import { ScanErrorTechnicalDetails } from './scans/ScanErrorTechnicalDetails'

function chipList(values: string[]) {
  if (values.length === 0) return <NoneTag />
  return (
    <span className="inline-flex flex-wrap gap-1">
      {values.map(value => <Chip key={value} size="xs">{value}</Chip>)}
    </span>
  )
}

/* ─── Per-event platform presence matrix (events × platform values, ✓/—) ─── */
function PlatformPresencePanel({ slug, scanConfigId }: { slug: string; scanConfigId: string }) {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: platformPresenceKey(slug, scanConfigId),
    queryFn: () => scansApi.getPlatformPresence(slug, scanConfigId),
    // Rendered inline below, with a retry.
    meta: SILENT_ERROR_META,
  })

  // Say how many events the matrix covers, so one row reads as "one event
  // so far", not as a truncated table (#247).
  const seenEverywhere = data
    ? data.items.filter(item => data.platforms.every(platform => item.present_platforms.includes(platform))).length
    : 0
  const subtitle = data?.platform_column
    ? data.items.length > 0 && data.platforms.length > 0
      ? `${seenEverywhere} of ${countOf(data.items.length, 'event', 'events')} seen on every ${data.platform_column} value`
      : `Per-event coverage across ${data.platform_column}`
    : 'Events seen per platform value'

  let body: React.ReactNode
  if (isLoading) {
    body = <SectionSkeleton variant="rows" rows={2} label="Loading platform presence…" />
  } else if (isError) {
    // Without this branch a failed fetch fell through to "No platform column
    // configured" — false for a scan that has one.
    body = (
      <div className="p-4">
        <ErrorState
          compact
          title="Couldn't load platform presence"
          error={error}
          onRetry={() => {
            void refetch()
          }}
        />
      </div>
    )
  } else if (!data?.platform_column) {
    body = (
      <p className="px-4 py-3 text-caption text-fg-tertiary">
        No platform column configured. Set one in Configuration › App version.
      </p>
    )
  } else if (data.items.length === 0 || data.platforms.length === 0) {
    body = (
      <p className="px-4 py-3 text-caption text-fg-tertiary">
        No platform data yet. It fills in as runs see each event.
      </p>
    )
  } else {
    body = (
      // Every column here is the data (one per platform value), so none hides
      // on a phone; the table scrolls sideways instead.
      <Table>
        <TableHeader>
          <TableRow style={{ background: 'var(--bg-sunken)' }}>
            <TableHead className="px-4">Event</TableHead>
            {data.platforms.map(platform => (
              <TableHead key={platform} className="px-4 text-center">
                {platform}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {data.items.map(item => (
            <TableRow key={item.event_id}>
              <TableCell className="px-4 text-body-sm">{item.event_name}</TableCell>
              {data.platforms.map(platform => {
                const present = item.present_platforms.includes(platform)
                return (
                  <TableCell
                    key={platform}
                    className="px-4 text-center text-body-sm"
                    style={{ color: present ? 'var(--success)' : 'var(--fg-faint)' }}
                  >
                    <span aria-label={`${item.event_name} ${present ? 'present' : 'absent'} on ${platform}`}>
                      {present ? '✓' : '—'}
                    </span>
                  </TableCell>
                )
              })}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    )
  }

  return (
    <Panel title="Platform presence" subtitle={subtitle}>
      {body}
    </Panel>
  )
}

/* ─── Overview tab body: stat cards + source/query + mapping/drift + jobs ─── */
export function ScanDetail({
  slug,
  scanConfig,
  eventTypes,
  dataSource,
  metricsSchedule = null,
}: {
  slug: string
  scanConfig: ScanConfig
  eventTypes: EventType[]
  dataSource?: DataSource | null
  /** The server's metrics schedule (i9mt.16); the job list stands in without it. */
  metricsSchedule?: MetricsSchedule | null
}) {
  const qc = useQueryClient()
  const canApplyGroups = useIsOwner()
  // Retry and Stop are run/cancel, which the backend gives any editor.
  const canRun = useCanWriteProject()
  const { notifyScanRunStarted } = useDemoScenarioActions()
  // Null for every non-demo project — no row is ever the scenario's row.
  const { scanJobId } = useScenarioArtifacts()
  const [expandedJobId, setExpandedJobId] = useState<string | null>(null)
  // Leading identical failed runs collapse behind one expander; the streak
  // banner already summarizes them.
  const [streakExpanded, setStreakExpanded] = useState(false)
  const [applyGroupsMessage, setApplyGroupsMessage] = useState('')
  const [highlightedJobId, setHighlightedJobId] = useState<string | null>(null)
  const { confirm, dialog } = useConfirm()

  const etName = eventTypes.find((et: EventType) => et.id === scanConfig.event_type_id)?.display_name

  const jobsRefetchInterval = useAdaptiveRefetchIntervalFn<ScanJob[]>({
    activeMs: 5000,
    isActive: scanJobsHaveActiveWork,
  })
  const {
    data: jobs = [],
    isLoading,
    isError: jobsError,
    error: jobsErrorObj,
    refetch: refetchJobs,
  } = useQuery({
    queryKey: scanJobsKey(slug, scanConfig.id),
    queryFn: () => scansApi.listJobs(slug, scanConfig.id),
    refetchInterval: jobsRefetchInterval,
  })
  // The streak's length over the whole history: the page above holds 50 runs,
  // and the Scans list tags the same streak from this count.
  // Shared with the list's query, so both surfaces read one number.
  const { data: activity } = useQuery({
    queryKey: scanActivityKey(slug),
    queryFn: () => scansApi.activity(slug),
    staleTime: 0,
    // The page's own count stands in if this fails; nothing to report inline.
    meta: SILENT_ERROR_META,
  })
  const invalidateRuns = () => {
    void qc.invalidateQueries({ queryKey: scanJobsKey(slug, scanConfig.id) })
    // Not under this scan's prefix: the list's exact streak and 24h rows.
    void qc.invalidateQueries({ queryKey: scanActivityKey(slug) })
  }

  const applyGroupsMut = useMutation({
    mutationFn: () => scansApi.applyEventGroups(slug, scanConfig.id),
    onMutate: () => setApplyGroupsMessage(''),
    onSuccess: (job) => {
      // Point at the run it queued too, not only say so: its row is
      // highlighted in Recent runs (#247).
      setApplyGroupsMessage('Group apply queued.')
      setHighlightedJobId(job.id)
      invalidateRuns()
      qc.invalidateQueries({ queryKey: scansKey(slug) })
      qc.invalidateQueries({ queryKey: projectEventsKey(slug) })
      qc.invalidateQueries({ queryKey: projectEventTypesKey(slug) })
    },
  })

  const cancelMut = useMutation({
    mutationFn: (jobId: string) => scansApi.cancelJob(slug, scanConfig.id, jobId),
    onSuccess: invalidateRuns,
  })
  // Stop sits one small icon away from Expand, and a stopped run is not
  // resumed — only started again — so it asks first.
  const requestCancel = async (jobId: string) => {
    const ok = await confirm({
      title: 'Stop this run?',
      message:
        'The run is marked cancelled now and stops at its next checkpoint; whatever it already wrote is kept. You can start the scan again at any time.',
      confirmLabel: 'Stop run',
      variant: 'danger',
    })
    if (ok) cancelMut.mutate(jobId)
  }

  const retryMut = useMutation({
    mutationFn: () => scansApi.run(slug, scanConfig.id),
    onSuccess: (job) => {
      // Bind the coached scenario to the job this retry created — the demo tick's
      // own jobs prove nothing about what the user did.
      notifyScanRunStarted(job)
      invalidateRuns()
    },
  })

  const lastJob = jobs[0] ?? null
  const lastScanned = jobScanned(lastJob)
  const lastEvents = lastJob?.result_summary?.events_created ?? null
  const mode = scanModeOf(scanConfig)
  // The newest METRICS run, not the newest run: a catalog Run now on top of the
  // list left this card "—" on every monitoring scan (#247).
  // The next run is the scheduler's own due check when the server sent it.
  const freshness = metricsFreshness(jobs, scanConfig.interval, undefined, metricsSchedule)
  const lastMetricsJob = freshness.job
  // One formula, shared with the list chip (scanUtils.jobMetricPoints). The old
  // `breakdown_event_metrics ?? event_metrics` fallback disagreed with the chip
  // for every scan that had breakdowns.
  const lastMetricPoints = jobMetricPoints(lastMetricsJob)
  const metricsDelta = mode !== 'monitoring'
    ? undefined
    : freshness.lastAt
      ? `${formatRelativeTime(freshness.lastAt)}${freshness.nextAt != null ? ` · next ${formatDueIn(freshness.nextAt)}` : ''}`
      : 'no collection yet'
  // Retry belongs to the failure that is still current: on an old failure that
  // later runs succeeded past it, it read as something left to fix (#247).
  const latestSettledId = jobs.find(j => j.status !== 'pending' && j.status !== 'running')?.id ?? null
  const platformColumn = scanConfig.platform_column ?? null
  const groupRuleCount = scanConfig.event_group_rules.length

  // Last-good timestamp: when the most recent run failed, surface when the scan
  // last succeeded so a red row is never the only signal.
  const lastGoodJob = jobs.find(j => j.status === 'completed') ?? null
  const lastGoodAt = lastGoodJob ? (lastGoodJob.completed_at ?? lastGoodJob.started_at) : null
  const recentJobsSubtitle = lastGoodAt
    ? `Last succeeded ${formatRelativeTime(lastGoodAt)}`
    : 'Latest runs of this scan'

  // A scan that fails every run produces a wall of identical failed rows. Collapse
  // that into one "failed last N runs" streak banner with the reason and a single
  // "Run again" action, so the failure reads as one ongoing problem.
  const failingStreak = consecutiveFailedRuns(jobs)
  // What the banner says: the server's count, which runs past the loaded page,
  // or the page's own while that is loading, failed, or older than the page.
  const serverStreak = activity?.items.find(item => item.scan_config_id === scanConfig.id)?.failing_streak ?? 0
  const failingStreakShown = Math.max(serverStreak, failingStreak)
  const streakError = failingStreak > 0 ? friendlyScanError(lastJob?.error_message) : null
  // When 2+ consecutive runs failed, hide that leading streak behind an expander
  // so the table isn't a wall of identical failed rows; older (non-streak) jobs
  // stay visible. Below the threshold, every job renders normally.
  const collapseStreak = failingStreak >= 2
  const streakJobs = collapseStreak ? jobs.slice(0, failingStreak) : []
  const restJobs = collapseStreak ? jobs.slice(failingStreak) : jobs
  const renderJobRow = (job: ScanJob) => (
    <JobRow
      key={job.id}
      job={job}
      slug={slug}
      scanConfigId={scanConfig.id}
      dataSourceId={scanConfig.data_source_id}
      mode={mode}
      watched={job.id === scanJobId}
      highlighted={job.id === highlightedJobId}
      expanded={expandedJobId === job.id}
      onToggle={() => setExpandedJobId(expandedJobId === job.id ? null : job.id)}
      onCancel={canRun ? () => void requestCancel(job.id) : undefined}
      cancelPending={cancelMut.isPending && cancelMut.variables === job.id}
      onRetry={canRun && job.id === latestSettledId ? () => retryMut.mutate() : undefined}
      retryPending={retryMut.isPending}
    />
  )

  return (
    <div className="flex flex-col gap-4">
      {dialog}
      {/* The one page-KPI strip: figures in sans + tabular digits, not
          18px mono tiles. */}
      <MiniStatStrip boxed>
        {/* An active run is "Running", not "just now": the relative time of a
            run still in progress read as a finished one. */}
        {/* Before the runs answer, a skeleton, not "never" and "—" (#237). */}
        <MiniStat
          label="Last run"
          value={
            isLoading
              ? <StatValueSkeleton />
              : lastJob && (lastJob.status === 'pending' || lastJob.status === 'running')
              ? 'Running'
              : lastJob
                ? formatRelativeTime(lastJob.completed_at ?? lastJob.started_at ?? lastJob.created_at)
                : 'never'
          }
        />
        {/* "Scanned", with the unit on the figure: a catalog run reads back
            distinct column combinations (grouped in the warehouse), a metrics
            run warehouse rows, and "Rows read 153" was off by ~180× from the
            dry run's row count (#247). The title says which cap applied. */}
        <div title={jobRowsReadTitle(lastJob)}>
          <MiniStat label="Scanned · last run" value={isLoading ? <StatValueSkeleton /> : formatJobScanned(lastScanned)} />
        </div>
        <MiniStat label="Events written" value={isLoading ? <StatValueSkeleton /> : lastEvents == null ? '—' : lastEvents.toLocaleString()} />
        {/* "Metric points", not "Metric rows": these are time-series points on a
            metric, and "Metrics" is the name of a different surface (Observe ›
            Metrics, the user-defined catalog). The figure is the newest
            collection's, with when it landed and when the next is due; amber
            once the series is two intervals behind (#247). */}
        <MiniStat
          label="Metric points"
          value={
            isLoading
              ? <StatValueSkeleton />
              : mode !== 'monitoring'
                ? 'Not collected'
                : lastMetricPoints == null ? '—' : lastMetricPoints.toLocaleString()
          }
          delta={isLoading ? undefined : metricsDelta}
          tone={!isLoading && freshness.overdue ? 'warning' : 'neutral'}
        />
      </MiniStatStrip>

      {/* Source & query */}
      <Panel title="Source & query">
        <KV
          label="Data source"
          value={
            <span className="inline-flex items-center gap-2">
              <SrcIcon dbType={dataSource?.db_type ?? null} size={18} />
              {dataSource?.name ?? 'Unknown source'}
            </span>
          }
        />
        <div className="border-t px-4 py-3 border-border-subtle">
          <div className="mb-1.5 text-body-sm text-fg-tertiary">
            Base query <span className="text-fg-tertiary">· used as subquery</span>
          </div>
          {/* Wrap the query; do not scroll it sideways. The demo's own base
              query is one 130-character line — `SELECT …, app_version FROM
              events`, built on a single line by demo/builders/warehouse.py —
              and at 1512px this box is ~872px wide, so `overflow-x-auto` cut it
              flush after "app_version" with no ellipsis, no fade and no
              scrollbar track. The panel whose whole job is to say what the scan
              reads was showing a SELECT with no FROM clause and no cue that
              anything was missing. `whitespace-pre-wrap` keeps the
              author's own newlines, `break-words` catches an identifier longer
              than the box, and the height cap scrolls vertically instead —
              same treatment the alert payload `<pre>`s already use, and a
              vertical scrollbar is one a reader can actually see. */}
          <pre
            className="mono m-0 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg border p-3 text-body-sm bg-bg-sunken border-border-subtle text-fg"
          >{scanConfig.base_query}</pre>
        </div>
        <KV label="Mode" value={SCAN_MODE_DETAIL_LABEL[scanModeOf(scanConfig)]} />
        <KV label="Time column" value={scanConfig.time_column || <NoneTag />} mono={!!scanConfig.time_column} />
        <KV label="Event name format" value={scanConfig.event_name_format || <NoneTag />} mono={!!scanConfig.event_name_format} />
      </Panel>

      {/* Mapping + metrics grid */}
      <div className="grid items-start gap-3 lg:grid-cols-2">
        <Panel title="Event mapping">
          {/* "Auto-detect" claimed a detection that never happened: with no
              event type AND no event type column a scan cannot name anything,
              and every run of it fails. The form asks the two together now, so
              this row says which of the two answers the config gave. */}
          <KV
            label="Event type"
            value={
              etName
              ?? (scanConfig.event_type_column
                ? <span className="text-fg-tertiary">Named from a column</span>
                : <NoneTag />)
            }
          />
          <KV label="Event type column" value={scanConfig.event_type_column || <NoneTag />} mono={!!scanConfig.event_type_column} />
          <KV
            label="App version column"
            value={
              scanConfig.app_version_column
                ? scanConfig.app_version_column
                : <NoneTag />
            }
            mono={!!scanConfig.app_version_column}
          />
          {/* With no platform column the presence matrix below has nothing to
              show, so the fact is one line here instead of a full-width panel
              that said only "No platform column configured" (#247). */}
          {!platformColumn && (
            <KV
              label="Platform column"
              value={
                <span className="inline-flex flex-wrap items-center gap-x-1.5">
                  <NoneTag />
                  <span className="text-caption text-fg-tertiary">
                    Set in Configuration › App version
                  </span>
                </span>
              }
            />
          )}
          {/* Apply groups sits on the rules it applies, not 400px below in the
              Recent runs header, and only when there are rules (#247). */}
          <KV
            label="Event group rules"
            value={
              groupRuleCount ? (
                <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
                  {countOf(groupRuleCount, 'rule', 'rules')}
                  {canApplyGroups && (
                    <Button
                      size="xs"
                      variant="outline"
                      onClick={() => applyGroupsMut.mutate()}
                      disabled={applyGroupsMut.isPending}
                      title="Merge existing events by the saved group rules"
                    >
                      <GitMerge className="size-3" aria-hidden="true" />
                      {applyGroupsMut.isPending ? 'Applying…' : 'Apply to existing events'}
                    </Button>
                  )}
                </span>
              ) : <NoneTag />
            }
          />
          {applyGroupsMut.isError && (
            <p className="border-t px-4 py-2 text-body-sm text-danger border-border-subtle">
              {getErrorMessage(applyGroupsMut.error)}
            </p>
          )}
          {/* Always mounted, so the live region exists before it has anything
              to say; padded only once it does. */}
          <p
            role="status"
            aria-live="polite"
            aria-atomic="true"
            className={applyGroupsMessage ? 'border-t border-border-subtle px-4 py-2 text-body-sm text-fg-tertiary' : 'sr-only'}
          >
            {applyGroupsMessage}
          </p>
        </Panel>
        <Panel title="Metrics & drift">
          <KV label="Breakdown columns" value={chipList(scanConfig.metric_breakdown_columns)} />
          <KV
            label="Values limit"
            // Unset keeps every value, and the form's placeholder calls that
            // "Unlimited": one word on both screens, not a "default" that
            // names no value (#247).
            value={scanConfig.metric_breakdown_values_limit ?? <span className="text-fg-tertiary">Unlimited</span>}
            mono
          />
          <KV label="Distribution drift" value={chipList(scanConfig.distribution_drift_fields)} />
          <KV label="JSON value paths" value={chipList(scanConfig.json_value_paths)} />
          <KV label="Cardinality threshold" value={scanConfig.cardinality_threshold} mono />
        </Panel>
      </div>

      {/* Platform presence matrix — only for a scan that has a platform
          column; without one, Event mapping says so in a line (#247). */}
      {platformColumn && <PlatformPresencePanel slug={slug} scanConfigId={scanConfig.id} />}

      {/* Recent runs */}
      <Panel title="Recent runs" subtitle={recentJobsSubtitle}>
        {cancelMut.isError && (
          <p className="px-4 py-2 text-body text-danger">{getErrorMessage(cancelMut.error)}</p>
        )}
        {retryMut.isError && (
          <p className="px-4 py-2 text-body text-danger">{getErrorMessage(retryMut.error)}</p>
        )}
        {failingStreak >= 2 && (
          <div
            className="mx-4 mt-3 flex flex-col gap-2 rounded-lg border p-3 border-danger bg-danger-soft"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="inline-flex items-center gap-1.5 text-body font-semibold text-danger">
                <XCircle className="size-3.5" aria-hidden="true" />
                Failed last {failingStreakShown} runs
              </span>
              <Button
                size="sm"
                variant="outline"
                onClick={() => retryMut.mutate()}
                disabled={retryMut.isPending}
              >
                <RotateCcw className="size-3" aria-hidden="true" />
                {retryMut.isPending ? 'Starting…' : 'Run again'}
              </Button>
            </div>
            {streakError && (
              <div className="text-body-sm text-danger">
                <p>{streakError.message}</p>
                <ScanErrorTechnicalDetails technical={streakError.technical} />
              </div>
            )}
          </div>
        )}
        {isLoading && <SectionSkeleton variant="rows" rows={3} label="Loading runs…" />}
        {/* A failed jobs fetch previously fell through to "No runs yet" — surface
            the error with a retry instead of a false empty. */}
        {jobsError && !isLoading && (
          <div className="p-4">
            <ErrorState
              compact
              title="Couldn't load run history"
              error={jobsErrorObj}
              onRetry={() => {
                void refetchJobs()
              }}
            />
          </div>
        )}
        {jobs.length === 0 && !isLoading && !jobsError && (
          <p className="px-4 py-3 text-body text-fg-tertiary">No runs yet. Use “Run now” to start.</p>
        )}
        {jobs.length > 0 && (
          <Table>
            {/* Duration and Events hide below `md`: a phone keeps when, how
                much was read, and how it ended, plus the row's controls. */}
            <TableHeader>
              <TableRow style={{ background: 'var(--bg-sunken)' }}>
                <TableHead className="px-4">Started</TableHead>
                <TableHead className={`px-4 text-right ${LOW_VALUE_COLUMN}`}>Duration</TableHead>
                <TableHead className="px-4 text-right">Scanned</TableHead>
                <TableHead className={`px-4 text-right ${LOW_VALUE_COLUMN}`}>Events</TableHead>
                <TableHead className="px-4">Status</TableHead>
                <TableHead className="w-8"><span className="sr-only">Actions</span></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {collapseStreak && (
                <>
                  <TableRow>
                    <TableCell colSpan={6} className="px-4 py-2">
                      <button
                        type="button"
                        onClick={() => setStreakExpanded((v) => !v)}
                        aria-expanded={streakExpanded}
                        className="text-body-sm font-medium hover:underline text-fg-tertiary"
                      >
                        {streakExpanded ? 'Hide' : 'Show'} {failingStreak} repeated failed runs
                      </button>
                    </TableCell>
                  </TableRow>
                  {streakExpanded && streakJobs.map(renderJobRow)}
                </>
              )}
              {restJobs.map(renderJobRow)}
            </TableBody>
          </Table>
        )}
      </Panel>
    </div>
  )
}

// Columns a phone can do without; the row keeps when, rows read and status.
const LOW_VALUE_COLUMN = 'hidden md:table-cell'

// 24px suits a mouse; a finger gets 32px, since Stop sits right beside Expand.
const RUN_CONTROL_SIZE = 'size-6 pointer-coarse:size-8'

function JobRow({
  job,
  slug,
  scanConfigId,
  dataSourceId,
  mode,
  watched,
  highlighted,
  expanded,
  onToggle,
  onCancel,
  cancelPending,
  onRetry,
  retryPending,
}: {
  job: ScanJob
  /** Both only reach JobDetails, which links its Signals/Alerts counters out. */
  slug: string
  scanConfigId: string
  /** Forwarded to JobDetails, whose failure box links to testing the source. */
  dataSourceId: string
  /** The config's mode, forwarded to the run report's catalog-only line. */
  mode: ScanMode
  /** This is the run the coached demo scenario is following — at most one row. */
  watched: boolean
  /** The run an Apply groups click just queued. */
  highlighted: boolean
  expanded: boolean
  onToggle: () => void
  /** Omitted for a viewer, as is `onRetry`: both are editor actions. */
  onCancel?: () => void
  cancelPending: boolean
  onRetry?: () => void
  retryPending: boolean
}) {
  const durationSec = jobDurationSeconds(job)
  const duration = durationSec != null
    ? `${durationSec.toFixed(1)}s`
    : job.status === 'running' ? 'running…' : '—'
  const scanned = jobScanned(job)
  const events = job.result_summary?.events_created ?? null
  const isActive = job.status === 'pending' || job.status === 'running'
  const expandable = Boolean(job.result_summary || job.error_message)
  const failedMessage = job.status === 'failed'
    ? friendlyScanError(job.error_message).message
    : null

  return (
    <Fragment>
      {/* The mark anchors onto the <tr> itself: the Popover root renders no DOM
          and the content is portalled, so nothing invalid lands in <tbody>.
          The row is not focusable; the mark hands its description to the
          row's first control (Stop run while the watched run is active, the
          details toggle once it has a result). A finished run with neither
          stays undescribed on purpose — a tab stop on a non-interactive row
          would be worse than the hint going unheard. */}
      <ScenarioCoachMark step="live-loop/watch-scan" when={watched}>
        {/* The whole row opens the run report, not only the 24px chevron at the
            far edge (#247). The chevron stays the keyboard control; a
            click on any button in the row (Stop, Retry, the chevron itself) is
            left to that button. */}
        <TableRow
          className={expandable ? 'cursor-pointer' : undefined}
          style={highlighted ? { background: 'var(--accent-soft)' } : undefined}
          onClick={
            expandable
              ? event => {
                if ((event.target as HTMLElement).closest('button, a')) return
                onToggle()
              }
              : undefined
          }
        >
          <TableCell className="px-4 text-body-sm text-fg-secondary">
            {/* A queued run has no start yet; its queue time says more than a
                dash, and it is what the scans list shows for it. */}
            {job.started_at
              ? formatRelativeTime(job.started_at)
              : `queued ${formatRelativeTime(job.created_at)}`}
          </TableCell>
          {/* Durations and counts are figures: sans with tabular digits, not
              mono. */}
          <TableCell className={`tnum px-4 text-right text-caption ${LOW_VALUE_COLUMN} text-fg-tertiary`}>{duration}</TableCell>
          {/* A catalog run and a metrics run count different populations
              under different caps: the unit rides on the figure ("153 combos",
              "4,428 rows"), the cap in the title (#247). */}
          <TableCell className="tnum whitespace-nowrap px-4 text-right text-caption" title={jobRowsReadTitle(job)}>
            {formatJobScanned(scanned)}
          </TableCell>
          <TableCell className={`tnum px-4 text-right text-caption ${LOW_VALUE_COLUMN} text-fg-secondary`}>
            {events == null ? '—' : events.toLocaleString()}
          </TableCell>
          <TableCell className="px-4">
            <RunStatusPill status={runPillStatus(job.status)} title={failedMessage ?? undefined} />
            {/* Why it failed, readable in the table rather than only in a hover
                title or after expanding the row, as the Scans list does
                (#247). */}
            {failedMessage && (
              <span className="mt-0.5 block max-w-64 truncate text-caption text-danger">
                {failedMessage}
              </span>
            )}
          </TableCell>
          <TableCell className="px-2">
            <div className="flex items-center justify-end gap-1 pointer-coarse:gap-2">
              {isActive && onCancel && (
                <IconButton
                  variant="ghost"
                  className={`${RUN_CONTROL_SIZE} text-fg-tertiary hover:text-[var(--danger)]`}
                  label="Stop run"
                  disabled={cancelPending}
                  onClick={onCancel}
                >
                  <Ban className="size-3" aria-hidden="true" />
                </IconButton>
              )}
              {job.status === 'failed' && onRetry && (
                <IconButton
                  variant="ghost"
                  className={`${RUN_CONTROL_SIZE} text-fg-tertiary hover:text-[var(--accent)]`}
                  label="Retry scan"
                  disabled={retryPending}
                  onClick={onRetry}
                >
                  <RotateCcw className="size-3" aria-hidden="true" />
                </IconButton>
              )}
              {expandable && (
                <IconButton
                  variant="ghost"
                  className={RUN_CONTROL_SIZE}
                  label={expanded ? 'Collapse run details' : 'Expand run details'}
                  aria-expanded={expanded}
                  onClick={onToggle}
                >
                  <ChevronDown className={`size-3 transition-transform ${expanded ? 'rotate-180' : ''}`} aria-hidden="true" />
                </IconButton>
              )}
            </div>
          </TableCell>
        </TableRow>
      </ScenarioCoachMark>
      {/* Only a replay has chunk progress to show; for every other run this row
          was an empty 8px strip under the run. */}
      {job.result_summary?.mode === 'metrics_replay' && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={6} className="p-0">
            <div className="px-4 pb-2">
              <ReplayChunkProgress summary={job.result_summary} compact />
            </div>
          </TableCell>
        </TableRow>
      )}
      {expanded && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={6} className="p-0">
            <JobDetails job={job} slug={slug} scanConfigId={scanConfigId} mode={mode} dataSourceId={dataSourceId} />
          </TableCell>
        </TableRow>
      )}
    </Fragment>
  )
}
