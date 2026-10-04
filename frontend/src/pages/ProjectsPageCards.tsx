import type { ElementType, ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  AlertTriangle,
  ArrowRight,
  ChevronRight,
  MoreHorizontal,
  PlayCircle,
  Settings2,
  Trash2,
} from 'lucide-react'
import { Chip } from '@/components/primitives/chip'
import { Dot } from '@/components/primitives/dot'
import { buildOnboardingSteps, onboardingProgress } from '@/components/onboarding-steps'
import { Button } from '@/components/ui/button'
import { IconButton } from '@/components/ui/icon-button'
import { Card } from '@/components/ui/card'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { formatIncidentCount } from '@/lib/alertStatus'
import { formatPlanCoverage, planCoverageRatio } from '@/lib/coverage'
import { formatDate, formatDateTime, formatRelativeTime } from '@/lib/datetime'
import { getMonitoringPath } from '@/lib/monitoring'
import { currentOrgSlug, projectHomePath, projectPath } from '@/lib/navigation'
import { countOf, pluralize } from '@/lib/plural'
import { friendlyScanError } from '@/lib/scanError'
import { formatJobScanned, jobScanned } from './settings/scans/scanUtils'
import type {
  Project,
  ProjectLatestScanJob,
  ProjectLatestSignal,
  ProjectSummary,
} from '@/types'
import { TONE_VARS, type StatTone } from './ProjectsPagePortfolio'

/**
 * An action-needed stat. The surface stays neutral in every state: a 3px
 * accent bar on the left carries the tone, so the strip no longer opens on a
 * big amber block beside a big pink one. It reads label first, then
 * the figure, then where it is — the order a reader scans it in.
 */
export function AttentionStat({
  icon: Icon,
  label,
  value,
  unit,
  hint,
  tone = 'neutral',
  pulse = false,
}: {
  icon: ElementType
  label: string
  value: string
  unit?: string
  hint: ReactNode
  tone?: StatTone
  pulse?: boolean
}) {
  const { color: toneColor } = TONE_VARS[tone]
  const needsAttention = tone === 'warning' || tone === 'danger' || tone === 'info'
  return (
    <div
      // `min-w-0` and no flex basis: the grid column decides the width now, and
      // a min-width here is what used to push the third card onto its own row.
      className="relative flex min-w-0 items-start gap-2.5 overflow-hidden rounded-lg border py-2.5 pl-4 pr-3 bg-surface border-border"
    >
      <span
        aria-hidden="true"
        data-slot="attention-accent"
        className="absolute inset-y-0 left-0 w-[3px]"
        style={{ background: needsAttention ? toneColor : 'var(--border)' }}
      />
      {/* Term first, as a <dl> requires, and now also first on screen:
          the label, the figure, then the breakdown. */}
      <dl className="m-0 min-w-0 flex-1">
        <dt className="flex items-center gap-1.5 text-body-sm font-medium text-fg-secondary">
          <Icon aria-hidden="true" className="h-3.5 w-3.5 shrink-0" style={{ color: toneColor }} />
          {label}
        </dt>
        <dd className="m-0 mt-1 flex items-baseline gap-1">
          {pulse && needsAttention && (
            <Dot tone={tone === 'warning' ? 'warning' : 'danger'} size={6} pulse />
          )}
          <span className="tnum text-title font-semibold leading-none">{value}</span>
          {unit ? (
            <span className="text-caption text-fg-tertiary">
              {unit}
            </span>
          ) : null}
        </dd>
        <dd className="m-0 mt-1 text-caption leading-[1.35] text-fg-tertiary">
          {hint}
        </dd>
      </dl>
    </div>
  )
}

const STATUS_TONE: Readonly<Record<ProjectStatusLabel, StatTone>> = {
  'Set up': 'neutral',
  'In review': 'warning',
  Ready: 'success',
  'In progress': 'info',
}

export function ProjectCard({
  project,
  canDelete,
  isOwner,
  canSetUp = false,
  sourceCount = 0,
  isDeleting,
  deleteLocked = false,
  onDelete,
}: {
  project: Project
  canDelete: boolean
  isOwner: boolean
  /**
   * The reader can take the getting-started steps (an editor or owner). An
   * empty project then offers "Continue setup" instead of a row of zeros; a
   * viewer, whose checklist is hidden, just reads that the plan is empty.
   */
  canSetUp?: boolean
  /** Real (non-synthetic) data sources in the workspace, for the setup progress. */
  sourceCount?: number
  /** A delete of this project is in flight: the card says so and its menu is shut. */
  isDeleting: boolean
  /**
   * Some delete is in flight, maybe of another card: the menu is shut, since
   * starting a second delete would reset the first one's pending state.
   */
  deleteLocked?: boolean
  onDelete: () => void
}) {
  const { summary } = project
  const status = getProjectStatus(summary)
  const home = projectHomePath(project.slug)
  const hasSignals = summary.monitoring_signal_count > 0
  const needsReview = summary.review_pending_event_count > 0
  // One needs-attention status leads the card in a saturated color; the rest
  // render calm/muted so the eye lands on what matters. Live monitoring
  // signals outrank a pending review queue.
  const attention: 'signals' | 'review' | null = hasSignals
    ? 'signals'
    : needsReview
      ? 'review'
      : null
  const statusTone = attention === 'signals' ? 'neutral' : STATUS_TONE[status.label]
  // An empty plan with nothing run has nothing to tabulate: every tile would be
  // 0 and both panels placeholder paragraphs. It gets one setup line instead.
  const isEmpty =
    summary.active_event_count === 0 &&
    summary.latest_scan_job === null &&
    summary.monitoring_signal_count === 0

  return (
    <Card
      // The Card primitive is the section card now: surface fill, no
      // outer padding or gap. The card is one compact row by default — name,
      // status, the four facts that matter and Open — with today's detail one
      // click away, so a portfolio of ten projects is not ten screens.
      className="overflow-hidden"
      aria-busy={isDeleting || undefined}
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
        <div className="min-w-0 flex-1 basis-[240px]">
          <div className="flex flex-wrap items-center gap-2">
            {/* The name is the way in, like the Open button. */}
            <Link
              to={home}
              className="truncate text-heading font-semibold no-underline hover:underline text-fg"
            >
              {project.name}
            </Link>
            <Chip tone={statusTone} size="xs">
              {status.label}
            </Chip>
            {isDeleting && (
              <Chip tone="danger" size="xs">
                Deleting…
              </Chip>
            )}
          </div>
          {project.description && (
            <p className="mt-0.5 truncate text-body-sm text-fg-tertiary">
              {project.description}
            </p>
          )}
          <p className="mt-0.5 text-caption text-fg-tertiary">
            <span className="mono">{project.slug}</span> · Updated {formatDate(project.updated_at)}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {/* The project's front door is its home (projectHomePath), which
              hosts the getting-started checklist; Events used to be hard-coded
              here, so a new project opened on an empty table. */}
          <Button asChild size="sm">
            <Link to={home}>
              Open project
              <ArrowRight className="h-3.5 w-3.5" />
            </Link>
          </Button>
          {canDelete && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <IconButton
                  variant="ghost"
                  className="shrink-0 text-fg-tertiary"
                  label={`Project actions for ${project.name}`}
                  disabled={isDeleting || deleteLocked}
                >
                  <MoreHorizontal className="h-3.5 w-3.5" />
                </IconButton>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" sideOffset={6} className="w-[176px]">
                <DropdownMenuItem
                  variant="destructive"
                  className="text-body-sm"
                  onSelect={onDelete}
                >
                  <Trash2 className="h-3.5 w-3.5 shrink-0" />
                  Delete project
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      </div>

      {isEmpty ? (
        <SetupLine project={project} canSetUp={canSetUp} isOwner={isOwner} sourceCount={sourceCount} />
      ) : (
        <>
          <ProjectFacts project={project} attention={attention} />
          <ProjectDetails project={project} isOwner={isOwner} />
        </>
      )}
    </Card>
  )
}

/**
 * The one line an empty project gets: how far its getting-started checklist
 * is, and the next step, leading to the checklist on the project home. The
 * counts come from the same steps the checklist shows (onboarding-steps.ts).
 */
function SetupLine({
  project,
  canSetUp,
  isOwner,
  sourceCount,
}: {
  project: Project
  canSetUp: boolean
  isOwner: boolean
  sourceCount: number
}) {
  const rowClass = 'border-t px-4 py-2.5 text-body-sm'
  const rowStyle = { borderColor: 'var(--border-subtle)' }
  // A demo drives its own welcome tour, and a viewer has no checklist: both
  // just read that the plan is empty.
  if (!canSetUp || project.is_demo) {
    return (
      <p className={rowClass} style={{ ...rowStyle, color: 'var(--fg-subtle)' }}>
        No events in this plan yet.
      </p>
    )
  }
  const steps = buildOnboardingSteps(project.slug, project.summary, sourceCount)
  const { completed, total, next } = onboardingProgress(steps, isOwner)
  return (
    <div className={rowClass} style={rowStyle}>
      <Link
        to={projectHomePath(project.slug)}
        className="inline-flex flex-wrap items-center gap-x-2 gap-y-1 no-underline hover:underline text-accent"
      >
        <span className="font-medium">Continue setup</span>
        <span className="tnum text-fg-tertiary">
          {`${completed} of ${total} set up`}
          {next ? ` · Next: ${next.title}` : ''}
        </span>
        <ArrowRight aria-hidden="true" className="h-3.5 w-3.5" />
      </Link>
    </div>
  )
}

/**
 * The compact facts row: coverage, the review queue, the last run and open
 * signals. The chip row that used to sit above the tiles repeated all of them,
 * so it went.
 */
function ProjectFacts({
  project,
  attention,
}: {
  project: Project
  attention: 'signals' | 'review' | null
}) {
  const { summary } = project
  const coverageDisplay = formatPlanCoverage(
    summary.implemented_event_count,
    summary.active_event_count,
  )
  const coverageRatio = planCoverageRatio(summary.implemented_event_count, summary.active_event_count)
  const job = summary.latest_scan_job
  return (
    <div
      className="flex flex-wrap items-center gap-x-5 gap-y-2 border-t px-4 py-2.5 text-caption border-border-subtle text-fg-tertiary"
    >
      <div className="flex min-w-0 items-center gap-2">
        <span className="font-medium text-fg">
          {summary.active_event_count > 0 ? `${coverageDisplay} implemented` : 'No active events'}
        </span>
        {/* A real progressbar, so the bar's value is not only a width. */}
        <div
          role="progressbar"
          aria-label="Implementation progress"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(coverageRatio * 100)}
          aria-valuetext={
            summary.active_event_count > 0
              ? `${summary.implemented_event_count} of ${summary.active_event_count} active events implemented`
              : 'No active events'
          }
          className="h-1.5 w-20 overflow-hidden rounded-full bg-bg-sunken"
        >
          <div
            className="h-full rounded-full transition-[width]"
            style={{ width: `${coverageRatio * 100}%`, background: 'var(--accent)' }}
          />
        </div>
        <span className="tnum">
          {summary.implemented_event_count}/{summary.active_event_count || 0}
        </span>
      </div>

      {/* The workspace tile above is only a readout; each card's own count is
          the link into that project's queue — unambiguous, one per queue, and
          cmd-clickable. */}
      {summary.review_pending_event_count > 0 ? (
        <Link
          to={projectPath(currentOrgSlug(), project.slug, '/events/review')}
          aria-label={`In review in ${project.name}: ${pluralize(
            summary.review_pending_event_count,
            '1 event',
            `${summary.review_pending_event_count} events`,
          )}`}
          className="rounded-full no-underline"
        >
          <Chip tone={attention === 'review' ? 'warning' : 'neutral'} size="xs">
            {summary.review_pending_event_count} in review
          </Chip>
        </Link>
      ) : (
        <span>None in review</span>
      )}

      <span className="flex items-center gap-1.5">
        {job ? (
          <>
            <span>Last scan</span>
            <Chip size="xs" {...scanJobStatusChip(job.status)}>
              {SCAN_STATUS_LABEL[job.status]}
            </Chip>
            <span className="tnum">{formatRelativeTime(job.completed_at ?? job.started_at ?? job.created_at)}</span>
          </>
        ) : summary.scan_count > 0 ? (
          pluralize(summary.scan_count, '1 scan configured', `${summary.scan_count} scans configured`)
        ) : (
          'No scan coverage'
        )}
        {/* A config that fails every run is hidden by the single newest
            latest_scan_job once a sibling config succeeds — surface the
            per-config failing count so it never goes unnoticed. */}
        {summary.failing_scan_config_count > 0 && (
          <Chip tone="danger" size="xs">
            {pluralize(
              summary.failing_scan_config_count,
              '1 scan failing',
              `${summary.failing_scan_config_count} scans failing`,
            )}
          </Chip>
        )}
      </span>

      {summary.monitoring_signal_count > 0 ? (
        <Chip tone={attention === 'signals' ? 'danger' : 'neutral'} size="xs">
          {pluralize(
            summary.monitoring_signal_count,
            '1 open signal',
            `${summary.monitoring_signal_count} open signals`,
          )}
        </Chip>
      ) : (
        <span>No open signals</span>
      )}

      {/* The same number the sidebar's Alerting badge shows. */}
      {summary.open_incident_count > 0 && (
        <Chip tone="danger" size="xs">
          {pluralize(
            summary.open_incident_count,
            '1 open incident',
            `${summary.open_incident_count} open incidents`,
          )}
        </Chip>
      )}
    </div>
  )
}

/** Today's full card detail, one click away. */
function ProjectDetails({ project, isOwner }: { project: Project; isOwner: boolean }) {
  const { summary } = project
  return (
    <details className="group border-t border-border-subtle">
      <summary
        className="flex cursor-pointer list-none items-center gap-1 px-4 py-2 text-caption font-medium select-none hover:bg-[var(--surface-hover)] [&::-webkit-details-marker]:hidden text-fg-tertiary"
      >
        <ChevronRight aria-hidden="true" className="h-3.5 w-3.5 transition-transform group-open:rotate-90" />
        Details
      </summary>
      <div className="space-y-3 px-4 pb-4 pt-1">
        {/* `items-start`: the four small tiles used to stretch to the height of
            the scan and monitoring panels beside them. */}
        <div className="grid items-start gap-3 lg:grid-cols-[minmax(0,auto)_minmax(0,1fr)_minmax(0,1fr)]">
          <div className="grid grid-cols-2 gap-2">
            <Metric label="Event types" value={String(summary.event_type_count)} />
            <Metric label="Active events" value={String(summary.active_event_count)} />
            <Metric label="Properties" value={String(summary.variable_count)} />
            {/* Incidents awaiting triage, as the sidebar's Alerting badge
                counts them. "Alerts" used to show the destination count, so
                the sidebar could say 1 while this said 2. */}
            <Metric
              label="Open incidents"
              value={String(summary.open_incident_count)}
              danger={summary.open_incident_count > 0}
            />
          </div>
          <Panel icon={PlayCircle} title="Latest scan">
            <LatestScanJobSummary job={summary.latest_scan_job} isOwner={isOwner} />
          </Panel>
          <Panel icon={AlertTriangle} title="Monitoring">
            <LatestSignalSummary
              slug={project.slug}
              signal={summary.latest_signal}
              signalCount={summary.monitoring_signal_count}
            />
          </Panel>
        </div>
        <Button asChild variant="outline" size="sm">
          <Link to={projectPath(currentOrgSlug(), project.slug, '/settings')}>
            <Settings2 className="h-3.5 w-3.5" />
            Settings
          </Link>
        </Button>
      </div>
    </details>
  )
}

function Metric({ label, value, danger = false }: { label: string; value: string; danger?: boolean }) {
  return (
    <dl
      className="m-0 rounded-md border px-2.5 py-2 bg-bg-sunken border-border-subtle"
    >
      <dt
        className="micro-label text-fg-tertiary"
      >
        {label}
      </dt>
      {/* A figure, not an identifier: sans with tabular digits. */}
      <dd
        className="tnum m-0 mt-0.5 text-heading font-semibold tracking-[-0.01em]"
        style={danger ? { color: 'var(--danger)' } : undefined}
      >
        {value}
      </dd>
    </dl>
  )
}

function Panel({
  icon: Icon,
  title,
  children,
}: {
  icon: ElementType
  title: string
  children: ReactNode
}) {
  return (
    <div
      className="rounded-md border p-3 bg-bg-sunken border-border-subtle"
    >
      <div className="mb-2 flex items-center gap-2">
        <div
          className="flex h-6 w-6 items-center justify-center rounded-sm bg-accent-soft text-accent"
        >
          <Icon className="h-3 w-3" />
        </div>
        <p className="text-body-sm font-medium">{title}</p>
      </div>
      {children}
    </div>
  )
}

function LatestScanJobSummary({
  job,
  isOwner,
}: {
  job: ProjectLatestScanJob | null
  isOwner: boolean
}) {
  if (!job) {
    return (
      <div className="text-caption text-fg-tertiary">
        No scan runs yet. Configure a scan and run it once to start surfacing execution
        history here.
      </div>
    )
  }

  const scanError = job.error_message ? friendlyScanError(job.error_message) : null
  // `jobScanned` is the rule the scan pages use, so one run shows the same
  // number AND the same unit here and on its scan page. A catalog run's
  // `scan_rows_processed` counts column combinations its GROUP BY returned, not
  // warehouse rows: printing it as "warehouse rows read" put "153 combos" on the
  // scan page and "153 warehouse rows read" here for one run (#247).
  const scanned = jobScanned(job)
  // Zero deltas are suppressed, all three alike. A green "+0 events" announced
  // in the success colour that nothing happened, while its zero siblings were
  // correctly silent — the card then read as a positive result at a glance.
  const eventsCreated = job.result_summary?.events_created ?? 0
  const signalsAdded = job.result_summary?.signals_added ?? 0
  const alertsQueued = job.result_summary?.alerts_queued ?? 0

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-body-sm font-medium">{job.scan_name}</p>
        <Chip size="xs" {...scanJobStatusChip(job.status)}>
          {job.status}
        </Chip>
      </div>
      <div className="space-y-0.5 text-caption text-fg-tertiary">
        <p>{describeScanJobTiming(job)}</p>
        {/* What this number counts, spelled out in the line: warehouse rows a
            metrics run read, or the column combinations a catalog run grouped —
            never the Monitoring tile's event count for one bucket. */}
        {scanned?.unit === 'rows' && (
          <p
            className="tnum"
            title="Warehouse rows this run read from the data source. Not an event count."
          >
            {scanned.value.toLocaleString()}{' '}
            warehouse rows read
          </p>
        )}
        {scanned?.unit === 'combinations' && (
          <p
            className="tnum"
            title="Distinct column combinations the warehouse grouped for this catalog run. Not warehouse rows, and not an event count."
          >
            {formatJobScanned(scanned)} grouped in the warehouse
          </p>
        )}
        {scanError && (
          <div className="space-y-1">
            <p className="line-clamp-2 text-danger">
              {scanError.message}
            </p>
            {isOwner && scanError.technical && (
              <details className="text-caption text-fg-tertiary">
                <summary className="cursor-pointer select-none">View technical details</summary>
                <p className="mono mt-1 whitespace-pre-wrap break-words">{scanError.technical}</p>
              </details>
            )}
          </div>
        )}
      </div>
      {(eventsCreated > 0 || signalsAdded > 0 || alertsQueued > 0) && (
        <div className="flex flex-wrap gap-1.5">
          {eventsCreated > 0 && (
            <Chip size="xs" tone="success">
              +{countOf(eventsCreated, 'event', 'events')}
            </Chip>
          )}
          {signalsAdded > 0 && (
            <Chip size="xs" tone="danger">
              +{countOf(signalsAdded, 'signal', 'signals')}
            </Chip>
          )}
          {alertsQueued > 0 && (
            <Chip size="xs" tone="warning">
              +{countOf(alertsQueued, 'alert', 'alerts')}
            </Chip>
          )}
        </div>
      )}
    </div>
  )
}

function LatestSignalSummary({
  slug,
  signal,
  signalCount,
}: {
  slug: string
  signal: ProjectLatestSignal | null
  signalCount: number
}) {
  if (!signal) {
    return (
      <div className="text-caption text-fg-tertiary">
        No recent monitoring signals. Once metrics collection finds anomalies, the latest signal
        will appear here.
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <Chip tone={signal.state === 'recent' ? 'warning' : 'danger'} size="xs">
          {signal.state === 'recent' ? 'Recent signal' : 'Latest scan signal'}
        </Chip>
        {/* The count is monitoring_signal_count, which the rest of the card
            calls open signals; "N recent" named a different population. */}
        <Chip size="xs">{signalCount} open</Chip>
      </div>
      <div className="space-y-0.5">
        {/* "Spike on <scope>" is the sentence the bell and the Anomalies list
            already use, so the scope name cannot be read as a readout of its
            own. */}
        <p className="text-body-sm font-medium">
          {signal.direction === 'drop' ? 'Drop' : 'Spike'} on {signal.scope_name}
        </p>
        {/* The value that series carried in ONE bucket, against the baseline the
            detector expected — not the run's row count the scan tile beside it
            reports. The noun is on the line ("events … in this bucket") so the
            two figures cannot read as one number disagreeing with itself: the
            workspace summary only carries project_total / event_type / event
            scopes, and all three are EventMetric volume. */}
        <p
          className="text-caption text-fg-tertiary"
          title="What the detector measured in this one bucket, against the baseline it expected. Not a row count."
        >
          <span className="tnum">{signal.actual_count.toLocaleString()}</span> events in this
          bucket vs <span className="tnum">{formatIncidentCount(signal.expected_count)}</span>{' '}
          expected
        </p>
        {/* "Bucket" names the timestamp, so it is not read as the scan tile's
            "Completed <time>". */}
        <p className="text-caption text-fg-tertiary">
          Bucket {formatDateTime(signal.bucket)} · via {signal.scan_name}
        </p>
      </div>
      <Button asChild variant="outline" size="sm">
        <Link to={getMonitoringPath(slug, signal)}>
          Open signal
          <ArrowRight className="h-3.5 w-3.5" />
        </Link>
      </Button>
    </div>
  )
}

// Sentence case, like every other label in the shell.
// "In review" is the status name used everywhere else.
type ProjectStatusLabel = 'Set up' | 'In review' | 'Ready' | 'In progress'

function getProjectStatus(summary: ProjectSummary): { label: ProjectStatusLabel } {
  if (summary.active_event_count === 0) return { label: 'Set up' }
  if (summary.review_pending_event_count > 0) return { label: 'In review' }
  if (summary.implemented_event_count === summary.active_event_count) return { label: 'Ready' }
  return { label: 'In progress' }
}

const SCAN_STATUS_LABEL: Readonly<Record<ProjectLatestScanJob['status'], string>> = {
  pending: 'Queued',
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

/** A run's lifecycle as a status chip: soft tone, outline while queued. */
function scanJobStatusChip(
  status: ProjectLatestScanJob['status'],
): { tone: 'success' | 'danger' | 'neutral'; variant?: 'outline' } {
  if (status === 'completed') return { tone: 'success' }
  if (status === 'failed') return { tone: 'danger' }
  if (status === 'running') return { tone: 'neutral' }
  return { tone: 'neutral', variant: 'outline' }
}

function describeScanJobTiming(job: ProjectLatestScanJob) {
  if (job.started_at && job.completed_at) {
    return `Completed ${formatDateTime(job.completed_at)}`
  }
  if (job.started_at) {
    return `Started ${formatDateTime(job.started_at)}`
  }
  return `Queued ${formatDateTime(job.created_at)}`
}
