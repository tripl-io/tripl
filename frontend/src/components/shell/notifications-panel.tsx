import { useEffect, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Bell,
  CheckCircle2,
  ChevronRight,
  Loader2,
  RotateCcw,
  Send,
  XCircle,
} from 'lucide-react'
import { alertingApi } from '@/api/alerting'
import { useExpandedSignals } from '@/hooks/useExpandedSignals'
import { useConfirm, type ConfirmOptions } from '@/hooks/useConfirm'
import {
  formatIncidentCount,
  incidentMagnitudeLabel,
  incidentReasonLabel,
  isDriftOnly,
} from '@/lib/alertStatus'
import { formatRelativeTime } from '@/lib/datetime'
import { currentOrgSlug, getAlertingPath, projectPath, workspacePath } from '@/lib/navigation'
import { channelLabel, TICKET_CHANNELS } from '@/lib/alertChannels'
import {
  signalScopeLabel,
  signalScopeRefLabel,
  unnamedScopeLabel,
} from '@/lib/signalScope'
import { getErrorMessage } from '@/lib/utils'
import { formatSignalEffect, formatSignalEffectDetail, getMonitoringPath } from '@/lib/monitoring'
import { selectSignificantSignals } from '@/lib/signalMagnitude'
import { Dot } from '@/components/primitives/dot'
import { Chip } from '@/components/primitives/chip'
import { CountBadge } from '@/components/primitives/count-badge'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import type { AlertDelivery, AlertInboxGroup, MonitoringSignal, Project } from '@/types'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { alertDeliveriesKey, topbarDeliveriesKey, topbarInboxKey } from '@/lib/queryKeys'
import { useTopbarDeliveries } from './notifications-queries'
import { NotificationsInbox } from './notifications-inbox'

export type BellTab = 'notifications' | 'signals'

const SIGNAL_PREVIEW_LIMIT = 4
const INCIDENT_PREVIEW_LIMIT = 4
const PROJECT_PREVIEW_LIMIT = 6

/**
 * The bell's popover body, in its own chunk: the shell only draws the bell and
 * its badge, and nobody pays for these rows, their icons and the retry flow
 * until the popover opens.
 *
 * In a project it lists the open incidents (what the badge counts), then the
 * Significant signals, then the latest deliveries. On a workspace route it
 * lists the projects that need attention, off the project list the shell
 * already holds, instead of "open a project" beside a workspace page that
 * headlines open signals.
 */
export default function NotificationsPanel({
  projectSlug,
  projects,
  openIncidentCount,
  onConfirmingChange,
  unreadCount = 0,
  initialTab = 'signals',
  onNavigate,
}: {
  projectSlug?: string
  /** The shell's project list (summaries included); undefined while loading. */
  projects: Project[] | undefined
  /** The badge's count: the project's open incidents, or all projects' on the workspace. */
  openIncidentCount: number
  /** True while a retry confirmation is up, so the popover ignores focus leaving. */
  onConfirmingChange: (confirming: boolean) => void
  /** The reader's unread notifications, across their projects (#259). */
  unreadCount?: number
  /** The tab the popover opens on: read once, at mount. */
  initialTab?: BellTab
  /** Closes the popover when a notification row is followed. */
  onNavigate?: () => void
}) {
  // Two tabs (#259): the reader's own notifications — comments, mentions,
  // reviews on what they watch — and the project's signals and incidents,
  // which is everything this popover held before.
  const [tab, setTab] = useState<BellTab>(initialTab)
  return (
    <Tabs value={tab} onValueChange={next => setTab(next === 'notifications' ? 'notifications' : 'signals')}>
      <div className="border-b px-2.5 pt-1.5 border-border-subtle">
        <TabsList aria-label="Bell sections" size="sm">
          <TabsTrigger value="notifications">
            Notifications
            {unreadCount > 0 && (
              <>
                <CountBadge count={unreadCount} max={99} className="ml-1.5" />
                {/* The badge is aria-hidden; the tab's name carries the count. */}
                <span className="sr-only">, {unreadCount} unread</span>
              </>
            )}
          </TabsTrigger>
          <TabsTrigger value="signals">
            Signals
            {openIncidentCount > 0 && <CountBadge count={openIncidentCount} max={9} urgent className="ml-1.5" />}
          </TabsTrigger>
        </TabsList>
      </div>
      <TabsContent value="notifications" className="mt-0">
        <NotificationsInbox unreadCount={unreadCount} onNavigate={onNavigate} />
      </TabsContent>
      <TabsContent value="signals" className="mt-0">
        <AlertsPanel
          projectSlug={projectSlug}
          projects={projects}
          openIncidentCount={openIncidentCount}
          onConfirmingChange={onConfirmingChange}
        />
      </TabsContent>
    </Tabs>
  )
}

/** The Signals tab: open incidents, signals and deliveries (the bell before #259). */
function AlertsPanel({
  projectSlug,
  projects,
  openIncidentCount,
  onConfirmingChange,
}: {
  projectSlug?: string
  projects: Project[] | undefined
  openIncidentCount: number
  onConfirmingChange: (confirming: boolean) => void
}) {
  return (
    <>
      <div
        className="flex items-center gap-2 border-b px-3.5 py-2.5 border-border-subtle"
      >
        <Bell className="h-3.5 w-3.5 text-fg-secondary" aria-hidden="true" />
        <span className="text-body-sm font-semibold">Alerts</span>
        <div className="flex-1" />
        {openIncidentCount > 0 && (
          <span className="tnum text-micro text-fg-tertiary">
            {openIncidentCount} open
          </span>
        )}
      </div>
      {projectSlug ? (
        <ProjectNotifications
          projectSlug={projectSlug}
          openIncidentCount={openIncidentCount}
          onConfirmingChange={onConfirmingChange}
        />
      ) : (
        <WorkspaceNotifications projects={projects} />
      )}
    </>
  )
}

/**
 * All projects at a glance: one row per project with open incidents,
 * Significant signals or open property drifts, worst first ("Demo Project 2 ·
 * 1 open incident · 3 signals · 2 property drifts"). A row opens the project's
 * inbox when something is open there, else its Anomalies list, else its
 * Properties page, which lists the drifts (F23).
 */
function WorkspaceNotifications({ projects }: { projects: Project[] | undefined }) {
  if (!projects) {
    return <EmptyNotifications message="Loading projects…" />
  }
  const needing = projects
    .filter((project) =>
      project.summary.open_incident_count > 0
      || project.summary.monitoring_signal_count > 0
      || openPropertyDrifts(project) > 0)
    .sort(
      (a, b) =>
        b.summary.open_incident_count - a.summary.open_incident_count ||
        b.summary.monitoring_signal_count - a.summary.monitoring_signal_count ||
        openPropertyDrifts(b) - openPropertyDrifts(a) ||
        a.name.localeCompare(b.name),
    )
  const shown = needing.slice(0, PROJECT_PREVIEW_LIMIT)
  return (
    <>
      {needing.length === 0 ? (
        <EmptyNotifications message="No open incidents, signals or property drifts in any project." />
      ) : (
        <div className="max-h-[420px] overflow-y-auto py-2">
          <NotificationSection title="Projects needing attention" count={needing.length}>
            {shown.map((project) => (
              <ProjectAttentionRow key={project.id} project={project} />
            ))}
            {needing.length > shown.length && (
              <Link
                to={workspacePath()}
                className="px-1.5 py-1 text-caption no-underline hover:underline text-fg-secondary"
              >
                +{needing.length - shown.length} more
              </Link>
            )}
          </NotificationSection>
        </div>
      )}
      <div className="border-t px-3.5 py-2 border-border-subtle">
        <Link
          to={workspacePath()}
          className="text-caption font-medium no-underline hover:underline text-fg-secondary"
        >
          All projects →
        </Link>
      </div>
    </>
  )
}

/** Open property drifts (F23); an older server's summary omits the count. */
function openPropertyDrifts(project: Project): number {
  return project.summary.open_property_drift_count ?? 0
}

function attentionPath(project: Project): string {
  if (project.summary.open_incident_count > 0) return `${getAlertingPath(project.slug)}?section=inbox`
  if (project.summary.monitoring_signal_count > 0) return projectPath(currentOrgSlug(), project.slug, '/anomalies')
  return projectPath(currentOrgSlug(), project.slug, '/variables')
}

function ProjectAttentionRow({ project }: { project: Project }) {
  const incidents = project.summary.open_incident_count
  const signals = project.summary.monitoring_signal_count
  const drifts = openPropertyDrifts(project)
  const parts = [
    incidents > 0 ? `${incidents} open ${incidents === 1 ? 'incident' : 'incidents'}` : null,
    signals > 0 ? `${signals} ${signals === 1 ? 'signal' : 'signals'}` : null,
    drifts > 0 ? `${drifts} property ${drifts === 1 ? 'drift' : 'drifts'}` : null,
  ].filter((part): part is string => part !== null)
  return (
    <Link
      to={attentionPath(project)}
      className="flex items-center gap-2 rounded-md px-1.5 py-2 no-underline transition-colors hover:bg-[var(--surface-active)] text-inherit"
    >
      <Dot tone={incidents > 0 ? 'danger' : 'warning'} size={7} />
      <div className="min-w-0 flex-1">
        <div className="truncate text-body-sm font-medium">{project.name}</div>
        <div className="tnum mt-0.5 truncate text-micro text-fg-tertiary">
          {parts.join(' · ')}
        </div>
      </div>
      <ChevronRight className="size-3 shrink-0 text-fg-tertiary" aria-hidden="true" />
    </Link>
  )
}

function ProjectNotifications({
  projectSlug,
  openIncidentCount,
  onConfirmingChange,
}: {
  projectSlug: string
  openIncidentCount: number
  onConfirmingChange: (confirming: boolean) => void
}) {
  // A ticket-channel retry asks first. While the dialog is up the popover
  // ignores the focus moving into it, so the row that asked (and its error
  // line) is still there when it answers.
  const { confirm, dialog } = useConfirm()
  const confirmRetry = async (options: ConfirmOptions) => {
    onConfirmingChange(true)
    try {
      return await confirm(options)
    } finally {
      onConfirmingChange(false)
    }
  }
  useEffect(() => () => onConfirmingChange(false), [onConfirmingChange])
  // The trigger's keys: both lists are cached by the time the panel opens.
  const signalsQuery = useExpandedSignals(projectSlug)
  const deliveriesQuery = useTopbarDeliveries(projectSlug)
  // The incidents themselves only while the popover is open (this panel is
  // mounted only then). Under the inbox prefix, so every inbox invalidation
  // (stream or triage action) refreshes it too.
  const incidentsQuery = useQuery({
    meta: SILENT_ERROR_META,
    queryKey: topbarInboxKey(projectSlug),
    queryFn: () => alertingApi.listInbox(projectSlug, { status: 'open', limit: INCIDENT_PREVIEW_LIMIT }),
    staleTime: 30_000,
  })
  const incidents = incidentsQuery.data?.items ?? []
  // The list's own total when it has answered, else the summary's.
  const incidentTotal = incidentsQuery.data?.total ?? openIncidentCount

  // Sorted biggest-effect-first, so the four rows previewed below are the four
  // worst rather than an arbitrary slice.
  const signals = selectSignificantSignals(signalsQuery.data)
  const previewSignals = signals.slice(0, SIGNAL_PREVIEW_LIMIT)
  const deliveries = deliveriesQuery.data?.items ?? []
  const failedDeliveryCount = deliveries.filter(delivery => delivery.status === 'failed').length
  const isError = signalsQuery.isError || deliveriesQuery.isError

  return (
    <>
      {isError ? (
        <EmptyNotifications message="Alerts could not be loaded from the backend." />
      ) : (
        <div className="max-h-[420px] overflow-y-auto py-2">
          {/* The incidents first: they are what the badge counts and what
              somebody still owes an answer on. */}
          <NotificationSection title="Open incidents" count={incidentTotal}>
            {incidentsQuery.isPending ? (
              <EmptySectionText>Loading incidents…</EmptySectionText>
            ) : incidentsQuery.isError ? (
              <EmptySectionText>Incidents could not be loaded.</EmptySectionText>
            ) : incidents.length === 0 ? (
              <EmptySectionText>No open incidents.</EmptySectionText>
            ) : (
              <>
                {incidents.map(group => (
                  <IncidentNotification key={group.correlation_group_id} slug={projectSlug} group={group} />
                ))}
                {incidentTotal > incidents.length && (
                  <Link
                    to={`${getAlertingPath(projectSlug)}?section=inbox`}
                    className="px-1.5 py-1 text-caption no-underline hover:underline text-fg-secondary"
                  >
                    +{incidentTotal - incidents.length} more
                  </Link>
                )}
              </>
            )}
          </NotificationSection>

          <NotificationSection title="Active signals" count={signals.length}>
            {signals.length === 0 ? (
              <EmptySectionText>No active monitoring signals.</EmptySectionText>
            ) : (
              <>
                {previewSignals.map(signal => (
                  <SignalNotification
                    key={`${signal.scope_type}:${signal.scope_ref}`}
                    slug={projectSlug}
                    signal={signal}
                  />
                ))}
                {/* The rest are one click away rather than silently cut.
                    */}
                {signals.length > previewSignals.length && (
                  <Link
                    to={projectPath(currentOrgSlug(), projectSlug, '/anomalies')}
                    className="px-1.5 py-1 text-caption no-underline hover:underline text-fg-secondary"
                  >
                    +{signals.length - previewSignals.length} more
                  </Link>
                )}
              </>
            )}
          </NotificationSection>

          <NotificationSection
            title="Recent alert deliveries"
            count={deliveries.length}
            accent={
              failedDeliveryCount > 0 ? (
                <Chip
                  tone="danger"
                  size="xs"
                  className="tnum"
                  icon={<XCircle aria-hidden="true" />}
                >
                  {failedDeliveryCount} failed
                </Chip>
              ) : null
            }
          >
            {deliveries.length === 0 ? (
              <EmptySectionText>No alert deliveries yet.</EmptySectionText>
            ) : (
              deliveries.map(delivery => (
                <DeliveryNotification
                  key={delivery.id}
                  slug={projectSlug}
                  delivery={delivery}
                  confirm={confirmRetry}
                />
              ))
            )}
          </NotificationSection>
        </div>
      )}

      <div
        className="border-t px-3.5 py-2 border-border-subtle"
      >
        {/* The two lists this popover previews, each in full. */}
        <div className="flex items-center justify-between gap-3">
          <Link
            to={projectPath(currentOrgSlug(), projectSlug, '/anomalies')}
            className="text-caption font-medium no-underline hover:underline text-fg-secondary"
          >
            All anomalies →
          </Link>
          <Link
            to={`${getAlertingPath(projectSlug)}?section=inbox`}
            className="text-caption font-medium no-underline hover:underline text-fg-secondary"
          >
            Alert inbox →
          </Link>
        </div>
      </div>
      {dialog}
    </>
  )
}

function NotificationSection({
  title,
  count,
  accent,
  children,
}: {
  title: string
  count: number
  accent?: ReactNode
  children: ReactNode
}) {
  return (
    // Named, so each list is a landmark a screen reader can jump between.
    <section aria-label={title} className="px-2 py-1.5">
      <div className="flex items-center gap-2 px-1.5 pb-1">
        <span
          className="micro-label text-fg-tertiary"
        >
          {title}
        </span>
        <span className="tnum text-micro text-fg-tertiary">
          {count}
        </span>
        {accent && (
          <>
            <div className="flex-1" />
            {accent}
          </>
        )}
      </div>
      <div className="flex flex-col gap-px">{children}</div>
    </section>
  )
}

function IncidentNotification({ slug, group }: { slug: string; group: AlertInboxGroup }) {
  // A drift went neither way, so its kind names it ("Schema drift on …").
  const kind = incidentReasonLabel(group.direction, group.scope_types)
  const verb = isDriftOnly(group.scope_types)
    ? kind.charAt(0).toUpperCase() + kind.slice(1)
    : group.direction === 'drop'
      ? 'Drop'
      : 'Spike'
  const names = group.scope_names.join(', ')
  const title = `${verb} on ${names || 'a deleted scope'}`
  return (
    <Link
      // The incident's own card in the inbox, not the top of the page.
      to={getAlertingPath(slug, { incidentId: group.correlation_group_id })}
      className="flex gap-2 rounded-md px-1.5 py-2 no-underline transition-colors hover:bg-[var(--surface-active)] text-inherit"
    >
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-danger" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-body-sm font-medium" title={title}>
          {title}
        </div>
        <div className="tnum mt-0.5 truncate text-micro text-fg-tertiary">
          {incidentMagnitudeLabel(group)} · {formatRelativeTime(group.latest_delivery_at)}
        </div>
      </div>
    </Link>
  )
}

function SignalNotification({
  slug,
  signal,
}: {
  slug: string
  signal: MonitoringSignal
}) {
  const tone = signal.state === 'latest_scan' ? 'danger' : 'warning'
  const verb = signal.direction === 'drop' ? 'Drop' : 'Spike'
  const scopeLabel = signalScopeLabel(signal)
  // The ref lives in the tooltip and nowhere else: it is what keeps a row the
  // server could not name traceable back to the detector, while printing it as
  // the label is what put a uuid here and a name on the activity rail for one
  // and the same incident.
  const title = `${verb} on ${scopeLabel ?? signalScopeRefLabel(signal)}`
  return (
    <Link
      to={getMonitoringPath(slug, signal)}
      className="flex gap-2 rounded-md px-1.5 py-2 no-underline transition-colors hover:bg-[var(--surface-active)] text-inherit"
    >
      <div className="mt-0.5">
        <Dot tone={tone} pulse size={7} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-body-sm font-medium" title={title}>
          {verb} on {scopeLabel ?? unnamedScopeLabel(signal)}
        </div>
        {/* The % change, not `z=40.7`; the z-score stays in the tooltip for
            whoever wants it. */}
        <div
          className="tnum mt-0.5 text-micro text-fg-tertiary"
          title={formatSignalEffectDetail(signal)}
        >
          {signal.actual_count.toLocaleString()} actual vs{' '}
          {formatIncidentCount(signal.expected_count)} expected · {formatSignalEffect(signal)}
        </div>
      </div>
    </Link>
  )
}

function DeliveryNotification({
  slug,
  delivery,
  confirm,
}: {
  slug: string
  delivery: AlertDelivery
  confirm: (options: ConfirmOptions) => Promise<boolean>
}) {
  const qc = useQueryClient()
  // Compact re-queue for a failed delivery. Mirrors the alerting-tab row: the
  // backend flips it back to 'pending', so we invalidate the notifications
  // deliveries query (and the full alerting list) to pull the fresh status.
  const retryMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => alertingApi.retryDelivery(slug, delivery.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: topbarDeliveriesKey(slug) })
      qc.invalidateQueries({ queryKey: alertDeliveriesKey(slug) })
    },
  })
  // The delivery row's rule, not a shortcut around it: a retry to Jira or
  // Linear opens a second ticket, so it asks first. Slack, Telegram,
  // email and webhooks repeat a message nobody got, and one click is right.
  const handleRetry = async () => {
    if (TICKET_CHANNELS.has(delivery.channel)) {
      const ok = await confirm({
        title: 'Retry this delivery',
        message: `Retrying sends this alert through "${delivery.destination_name}" again, and ${channelLabel(delivery.channel)} opens a new issue for it.`,
        confirmLabel: 'Retry',
        variant: 'primary',
      })
      if (!ok) return
    }
    if (!retryMut.isPending) retryMut.mutate()
  }
  const StatusIcon = delivery.status === 'sent'
    ? CheckCircle2
    : delivery.status === 'failed'
      ? XCircle
      : Send
  const statusColor = delivery.status === 'sent'
    ? 'var(--success)'
    : delivery.status === 'failed'
      ? 'var(--danger)'
      : 'var(--warning)'
  const isFailed = delivery.status === 'failed'
  return (
    <div className="rounded-md transition-colors hover:bg-[var(--surface-active)]">
      <div className="flex items-center gap-1 pr-1">
        <Link
          to={getAlertingPath(slug, { deliveryId: delivery.id })}
          className="flex min-w-0 flex-1 gap-2 px-1.5 py-2 no-underline text-inherit"
        >
          <StatusIcon className="mt-0.5 h-3.5 w-3.5 shrink-0" style={{ color: statusColor }} />
          <div className="min-w-0 flex-1">
            <div className="truncate text-body-sm font-medium">
              {delivery.rule_name}
            </div>
            {/* Words, not wire values: "Failed · Slack · 3 matched · 2h ago"
                rather than "failed · slack · 3 matched". */}
            <div className="mt-0.5 text-micro text-fg-tertiary">
              {deliveryStatusWord(delivery.status)} · {channelLabel(delivery.channel)} ·{' '}
              {delivery.matched_count} matched · {formatRelativeTime(delivery.created_at)}
            </div>
          </div>
        </Link>
        {isFailed && (
          <button
            type="button"
            onClick={() => void handleRetry()}
            disabled={retryMut.isPending}
            aria-label={`Retry delivery for ${delivery.rule_name}`}
            className="flex h-6 shrink-0 items-center gap-1 rounded-md px-1.5 text-micro font-medium transition-colors hover:bg-[var(--surface-active)] disabled:opacity-60 text-fg-secondary"
          >
            {retryMut.isPending ? (
              <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
            ) : (
              <RotateCcw className="h-3 w-3" aria-hidden="true" />
            )}
            Retry
          </button>
        )}
      </div>
      {isFailed && retryMut.isError && (
        <p role="alert" className="px-1.5 pb-1.5 text-micro text-danger">
          {getErrorMessage(retryMut.error)}
        </p>
      )}
    </div>
  )
}

function deliveryStatusWord(status: string): string {
  return status ? status.charAt(0).toUpperCase() + status.slice(1) : status
}

function EmptyNotifications({ message }: { message: string }) {
  return (
    <div className="px-4 py-8 text-center text-caption text-fg-tertiary">
      {message}
    </div>
  )
}

function EmptySectionText({ children }: { children: ReactNode }) {
  return (
    <div className="px-1.5 py-2 text-caption text-fg-tertiary">
      {children}
    </div>
  )
}
