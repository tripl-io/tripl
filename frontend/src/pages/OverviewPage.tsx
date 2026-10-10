import { Fragment, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Database } from 'lucide-react'
import { activityApi } from '@/api/activity'
import { ActivityFeed } from '@/components/activity-feed'
import { useActivityRailInline } from '@/components/activity-rail-store'
import { ApiError } from '@/api/client'
import { dataSourcesApi } from '@/api/dataSources'
import { eventMetricsApi } from '@/api/eventMetrics'
import NotFoundPage from '@/pages/NotFoundPage'
import { ErrorState } from '@/components/error-state'
import { OnboardingChecklist } from '@/components/onboarding-checklist'
import { countRealSources } from '@/components/onboarding-utils'
import { SyntheticSourceBadge } from '@/demo/capabilityBadges'
import { DemoWelcomePanel } from '@/demo/DemoWelcomePanel'
import { Chip } from '@/components/primitives/chip'
import { Dot } from '@/components/primitives/dot'
import { MiniStat, MiniStatStrip } from '@/components/primitives/mini-stat'
import { Sparkline } from '@/components/primitives/sparkline'
import { Panel } from '@/components/settings/kit'
import { OverviewPlanHealthPanel } from './OverviewPlanHealthPanel'
import { PageContainer } from '@/components/primitives/page-container'
import { PageHeader } from '@/components/primitives/page-header'
import { EmptyState } from '@/components/empty-state'
import { StatValueSkeleton } from '@/components/states'
import { ConnectDataSourceButton } from '@/components/first-scan-actions'
import { SERIES_COLORS } from '@/components/ui/chart-format'
import { Skeleton } from '@/components/ui/skeleton'
import { useTheme } from '@/components/theme-provider'
import { useCanWriteProject, useIsOwner } from '@/lib/permissions'
import { useOnboardingDismissed } from '@/lib/onboardingDismissal'
import { currentOrgSlug, getAlertingPath, projectPath, settingsPath } from '@/lib/navigation'
import { metricCadence } from '@/lib/metricFormat'
import { formatPlanCoverage, planCoverageTone } from '@/lib/coverage'
import {
  dataSourceHealthLexeme,
  signalDirectionColor,
  signalDirectionTone,
  type StatusLexeme,
} from '@/lib/statusLexicon'
import { formatDateTime, formatRelativeTime, formatShortTimestamp } from '@/lib/datetime'
import { APP_LOCALE, formatNumber } from '@/lib/format'
import { formatRatioDelta } from '@/lib/percentDelta'
import { countOf } from '@/lib/plural'
import { formatSignalEffect, formatSignalEffectDetail, getMonitoringPath } from '@/lib/monitoring'
import { selectSignificantSignals } from '@/lib/signalMagnitude'
import { formatSignalValues, signalTimeTitle } from '@/lib/signalMetricFormat'
import { useExpandedSignals } from '@/hooks/useExpandedSignals'
import { useSourceFreshness } from '@/hooks/useSourceFreshness'
import { FreshnessChip } from '@/components/source-freshness/freshness-chip'
import { dataSourceFreshness } from '@/components/data-sources/data-source-freshness-model'
import { holdingItems } from '@/lib/sourceFreshness'
import { useLiveTimeRange } from '@/hooks/useLiveTimeRange'
import { useActiveBranchId } from '@/hooks/useBranch'
import {
  signalScopeLabel,
  signalScopeRefLabel,
  unnamedScopeLabel,
} from '@/lib/signalScope'
import { useAdaptiveRefetchInterval } from '@/realtime/streamContext'
import {
  dbTypeLabel,
  type DataSource,
  type EventMetricPoint,
  type MonitoringSignal,
  type SourceFreshnessItem,
} from '@/types'
import {
  activityPreviewKey,
  dataSourcesKey,
  overviewKpiSeriesKey,
  overviewTopEventsKey,
  overviewVolumeKey,
  projectQueryOptions,
} from '@/lib/queryKeys'

const SIGNAL_LIMIT = 6
const ACTIVITY_LIMIT = 8
// The magnitude gate lives in @/lib/signalMagnitude, shared with AnomaliesPage,
// the top-bar bell and the backend's metrics_insights_service. Gating the "Open
// signals" headline on it keeps the number equal to the sidebar badge (project
// summary monitoring_signal_count) and the Anomalies page's default
// "Significant" view.
// A successful source connection test older than this is shown as "stale" rather
// than a confident "healthy" — an old green check is misleading.
const SOURCE_HEALTH_STALE_MS = 24 * 60 * 60 * 1000
// The volume card asked for the scan's ENTIRE metric history — the endpoint's
// from/to simply were never passed — so it was still fetching 2.2 s after every
// other panel on the page had rendered. Seven days matches the
// documented default window for project-total charts.
const VOLUME_WINDOW_DAYS = 7
const VOLUME_WINDOW_MS = VOLUME_WINDOW_DAYS * 24 * 60 * 60 * 1000
// Says what the card covers, set against Top events' "Across every scan in
// this project." below it, rather than what it is not.
const VOLUME_SUBTITLE = 'This scan only — Top events below counts every scan.'

export default function OverviewPage() {
  const { slug } = useParams<{ slug: string }>()
  const { chartStyle } = useTheme()
  // Adaptive fallback cadence: the live stream refreshes signals/activity via the
  // invalidation map, so poll only while the stream is unavailable.
  const refetchInterval = useAdaptiveRefetchInterval({ activeMs: 60_000 })

  // On a working branch the plan KPIs (active, implemented, in review) count
  // that branch's events, like the lists beside them.
  const branchId = useActiveBranchId()
  const projectQuery = useQuery({
    ...projectQueryOptions(slug, branchId),
    enabled: !!slug,
  })
  // A live bound rather than a mount-time snapshot, so a long-open tab keeps
  // asking for the current 7 days.
  const volumeRange = useLiveTimeRange(VOLUME_WINDOW_MS)
  // The project query is the single authority on whether the slug exists. Gate
  // every project-scoped widget query on its success so they never fan out
  // 404s against a nonexistent project.
  const volumeQuery = useQuery({
    // The window length is in the key but its moving bounds are NOT: every
    // refetch reads the live range, while keying on `to` would mint a fresh
    // cache entry — and drop the card back to its skeleton — every five
    // minutes. Same split as MonitoringDetailPage's metricsQuery.
    queryKey: overviewVolumeKey(slug, VOLUME_WINDOW_DAYS),
    queryFn: () => eventMetricsApi.getProjectTotalMetrics(slug!, volumeRange),
    enabled: !!slug && projectQuery.isSuccess,
    staleTime: 60_000,
  })
  const topEventsQuery = useQuery({
    queryKey: overviewTopEventsKey(slug),
    queryFn: () => eventMetricsApi.getTopEvents(slug!, { windowHours: 48, limit: 6 }),
    enabled: !!slug && projectQuery.isSuccess,
    staleTime: 60_000,
  })
  const kpiSeriesQuery = useQuery({
    queryKey: overviewKpiSeriesKey(slug),
    queryFn: () => eventMetricsApi.getOverviewKpiSeries(slug!, 14),
    enabled: !!slug && projectQuery.isSuccess,
    staleTime: 60_000,
  })
  // Expanded (all scopes, incident children tagged) so the headline count matches
  // the sidebar badge and the Anomalies page rather than only project-total /
  // event-type incidents. Shared key with the top bar and
  // the Anomalies page.
  const signalsQuery = useExpandedSignals(slug, { enabled: projectQuery.isSuccess })
  // With the rail open inline beside the page, the page's own "Recent activity"
  // panel listed the same items a second time, side by side. The
  // panel steps aside while the rail is there and comes back when it closes.
  const railShowsActivity = useActivityRailInline()
  const activityQuery = useQuery({
    // Its own key under the rail's: the two asked for different page sizes
    // under ONE key, so whichever fetched last set the length of both lists.
    queryKey: activityPreviewKey(slug, ACTIVITY_LIMIT),
    queryFn: () => activityApi.list({ slug, limit: ACTIVITY_LIMIT }),
    enabled: !!slug && projectQuery.isSuccess && !railShowsActivity,
    staleTime: 30_000,
    refetchInterval,
  })
  const sourcesQuery = useQuery({
    queryKey: dataSourcesKey(),
    queryFn: dataSourcesApi.list,
  })
  // Per-scan source freshness (F16, #269): a late source or an overdue scan
  // is a Source health fact, and it explains a quiet drop column.
  const freshnessItems = useSourceFreshness(slug)
  const lateScans = holdingItems(freshnessItems)

  const summary = projectQuery.data?.summary
  const projectId = projectQuery.data?.id
  const volumePoints = volumeQuery.data?.data ?? []
  const volumeCounts = volumePoints.map((p) => p.count)
  const topEvents = topEventsQuery.data ?? []
  const maxTopVolume = topEvents.reduce((m, e) => Math.max(m, e.total_count), 0)
  // Match the AnomaliesPage default "Significant" view so the "Open signals"
  // headline, the sidebar badge (monitoring_signal_count) and the Anomalies page
  // all report the same count. Sorted biggest-effect-first so
  // the capped panel previews the top anomalies.
  const signals = selectSignificantSignals(signalsQuery.data)
  const activity = activityQuery.data ?? []
  // Scope the Source-health rail to this project: workspace-global sources
  // (project_id == null) plus sources owned by the current project. Without this
  // a demo project's project-scoped synthetic source leaks into unrelated
  // projects. Falls back to the full list until the project loads.
  const allSources = sourcesQuery.data ?? []
  const sources = projectId
    ? allSources.filter((s) => s.project_id == null || s.project_id === projectId)
    : allSources

  // "Open signals" comes from the SAME array the panel below renders —
  // now the significant, all-scope signals — so the headline equals the sidebar
  // badge (monitoring_signal_count) and the Anomalies page.
  const signalCount = signals.length
  const reviewCount = summary?.review_pending_event_count ?? 0
  // Events CREATED per day (main branch) — NOT a history of the "Active events"
  // stat beside it. The series was captioned "Active trend" / "Active events by
  // day" while a single day could exceed the whole active catalog (4,618 on a
  // project with 2,413 active events); the caption now says what the numbers are.
  const newEventsSeries = kpiSeriesQuery.data?.new_events ?? []
  // The volume card charts ONE scan config (summing every scan double-counts the
  // events a legacy/backfill scan also collected), so it names that scan instead
  // of claiming to be the project total.
  const volumeScanName = volumeQuery.data?.scan_config_name ?? null
  // Present exactly when a scan config was resolved, which is what separates
  // "this scan collected nothing in the window" from "nothing collects at all"
  // — the backend returns a bare empty series with no scan id in the second
  // case (metrics_service.get_project_total_metrics).
  const volumeScanConfigId = volumeQuery.data?.scan_config_id ?? null
  // `isPending`, not `isLoading`: while the query waits on the projectQuery gate
  // it is pending but NOT fetching, so an `isLoading` check let the card claim
  // "No volume data yet." before it had even asked.
  const isVolumePending = volumeQuery.isPending && !projectQuery.isError
  // Same for every project-gated panel: pending-and-waiting is still pending.
  const isSignalsPending = signalsQuery.isPending && !projectQuery.isError
  const isTopEventsPending = topEventsQuery.isPending && !projectQuery.isError
  // The card's headline: the last 24 hours against the 24 before, not the
  // newest (partial) bucket, and a caption in dates rather than "167 buckets".
  const volumeSummary = summarizeVolume(volumePoints)
  const volumeInterval = volumeQuery.data?.interval
  const volumeCadence = metricCadence(volumeInterval)
  const projectTotalPath = volumeScanConfigId
    ? getMonitoringPath(slug!, { scope_type: 'project_total', scope_ref: volumeScanConfigId })
    : null
  // Nothing to show below the checklist yet: no active event and no source.
  // Five empty panels of chrome competed with the checklist that does teach,
  // so the page shows one empty state instead.
  const isBlankProject =
    !!summary && summary.active_event_count === 0 && sourcesQuery.isSuccess && sources.length === 0
  // The blank project's own "Connect a data source" button only when the
  // getting-started checklist above is not already offering it as step 1. The
  // checklist shows for anyone who can write here until it is dismissed, so
  // for an owner that is once they have dismissed it. Subscribed, not read
  // once: dismissing the checklist re-renders this page, so the button shows
  // the moment the checklist goes.
  const canWriteHere = useCanWriteProject()
  // The owner of the organization the app acts in, not of the default one.
  const isOrgOwner = useIsOwner()
  const checklistDismissed = useOnboardingDismissed(slug, projectQuery.data?.id)
  const offerConnectSource =
    isOrgOwner && (!canWriteHere || (!!slug && checklistDismissed))

  // A nonexistent slug is a 404 on the project query itself: replace the whole
  // widget grid with the app's full-page not-found. Non-404 project
  // failures (500/503) keep the compact KPI-strip ErrorState below so a transient
  // outage is not misreported as a missing project.
  if (projectQuery.error instanceof ApiError && projectQuery.error.status === 404) {
    return <NotFoundPage />
  }

  return (
    <PageContainer>
      {/* Header. The eyebrow is the nav group, never the project name: the
          top bar's breadcrumb already carries that. The title
          is "Overview", what the nav and the URL call the project home; "Live
          activity" named it after one of its cards. The one-line
          status under it answers "is everything OK?" before any panel. */}
      <PageHeader
        eyebrow="Observe"
        title="Overview"
        description={
          slug && summary && !isBlankProject ? (
            <OverviewStatus
              slug={slug}
              signalCount={signalsQuery.data ? signalCount : null}
              openIncidents={summary.open_incident_count}
              failingScans={summary.failing_scan_config_count}
              failingDestinations={summary.failing_alert_destination_count ?? 0}
              sources={sourcesQuery.isSuccess ? sources : null}
            />
          ) : undefined
        }
      />

      {/* A freshly-created demo lands here (not Events): orient the user and
          launch the tour before anything else below the title. */}
      {projectQuery.data?.is_demo && <DemoWelcomePanel project={projectQuery.data} />}

      {/* Guided first-run checklist — a "start here": connect a
          source, run a scan, review what it imported, define a metric, set up
          alerting (#250). Self-derives done-state from REAL project data,
          is dismissible, and auto-hides once complete. Synthetic demo sources
          are excluded from the "connect a source" step. The metric step reads
          `summary.metric_count` and stays out until the backend sends it. */}
      {slug && (
        <OnboardingChecklist
          slug={slug}
          projectId={projectQuery.data?.id}
          summary={summary}
          sourceCount={countRealSources(sources)}
          isDemo={projectQuery.data?.is_demo}
        />
      )}

      {isBlankProject ? (
        <EmptyState
          icon={Database}
          title="Overview fills in after your first scan"
          description="Connect a data source and run a scan. Volume, top events, anomalies and source health then show up here."
          action={
            offerConnectSource ? <ConnectDataSourceButton /> : undefined
          }
        />
      ) : (
      <>
      {/* KPI strip. The exception tiles link where the work is. */}
      {projectQuery.isError ? (
        <ErrorState
          title="Overview unavailable"
          error={projectQuery.error}
          onRetry={() => {
            void projectQuery.refetch()
          }}
          retryLabel="Retry"
          compact
        />
      ) : (
        <MiniStatStrip boxed>
          {/* Neutral figures by default; only the exceptions carry a colour.
              A pending value is a skeleton, never a "0". */}
          <MiniStat
            label="Active events"
            value={summary ? formatNumber(summary.active_event_count) : <StatValueSkeleton />}
          />
          <MiniStat
            label="Implemented"
            value={summary ? formatNumber(summary.implemented_event_count) : <StatValueSkeleton />}
          />
          {/* "In review", the one name for the status count everywhere:
              the tile, the Events tab and the glossary. */}
          <KpiLink to={slug ? projectPath(currentOrgSlug(), slug, '/events/review') : undefined}>
            <MiniStat
              label="In review"
              value={summary ? formatNumber(reviewCount) : <StatValueSkeleton />}
            />
          </KpiLink>
          <KpiLink to={slug ? projectPath(currentOrgSlug(), slug, '/anomalies') : undefined}>
            <MiniStat
              label="Open signals"
              value={signalsQuery.data ? formatNumber(signalCount) : <StatValueSkeleton />}
              tone={signalsQuery.data && signalCount > 0 ? 'danger' : 'neutral'}
              valueTone={signalsQuery.data && signalCount > 0 ? 'danger' : undefined}
              // The one pulse on the page: the rows below are static. MiniStat
              // draws its pulse in the delta, so the delta is the dot alone:
              // a visible word read "Open signals 3 active", the same idea
              // twice. Screen readers still hear it, in the signal's own word
              // "open" ("active" is what the events tile counts).
              pulse={signalCount > 0}
              delta={signalCount > 0 ? <span className="sr-only">open</span> : undefined}
            />
          </KpiLink>
          {/* "Plan coverage", as on the Coverage page it opens, in the same
              colour there and here. */}
          <KpiLink to={slug ? projectPath(currentOrgSlug(), slug, '/coverage') : undefined}>
            <MiniStat
              label="Plan coverage"
              // Nothing planned yet reads "—": no score, rather than a 0%.
              value={
                summary ? (
                  formatPlanCoverage(summary.implemented_event_count, summary.active_event_count)
                ) : (
                  <StatValueSkeleton />
                )
              }
              tone={
                summary
                  ? planCoverageTone(summary.implemented_event_count, summary.active_event_count)
                  : 'neutral'
              }
            />
          </KpiLink>
          {newEventsSeries.length > 1 && (
            <>
              {/* Stacked like the MiniStat columns (caption above, figure below):
                  same `gap-px` label→figure rhythm and a 24px figure so the top
                  caption sits on the same baseline as the numeric stats in this
                  `items-center` row. `shrink-0` keeps its room when the strip
                  wraps, and `pr-1` lifts the line off the card edge (the SVG draws
                  to x=width with overflow visible) so it reads as a finished stat
                  rather than a stray line crammed against the edge. Tooltip +
                  role="img" alt spell out what the line is (issue #12). */}
              <div
                className="m-0 flex shrink-0 flex-col gap-px pr-1"
                title="New events added per day over the last 14 days"
              >
                <span
                  className="micro-label text-fg-tertiary"
                >
                  New events · 14d
                </span>
                <div role="img" aria-label={newEventsTrendLabel(newEventsSeries)}>
                  <Sparkline
                    data={newEventsSeries}
                    variant={chartStyle}
                    width={120}
                    height={24}
                  />
                  <span className="sr-only">
                    New events added by day: {newEventsSeries.map((c) => formatNumber(c)).join(', ')}.
                  </span>
                </div>
              </div>
            </>
          )}
        </MiniStatStrip>
      )}

      {/* Active signals, straight under the KPIs: the widget that answers "is
          anything wrong?" sat fourth, below the fold at 1440. Capped at
          SIGNAL_LIMIT rows while the headline can count dozens, so the full
          list is one click away. */}
      <Panel
        title="Active signals"
        right={
          slug && signals.length > 0 ? (
            <Link
              to={projectPath(currentOrgSlug(), slug, '/anomalies')}
              className="rounded-md px-2 py-1 text-body-sm no-underline transition-colors hover:bg-[var(--surface-hover)] text-accent"
            >
              View all ({formatNumber(signals.length)})
            </Link>
          ) : undefined
        }
      >
        <div className="p-4">
        {signalsQuery.isError && (
          <ErrorState
            title="Signals unavailable"
            error={signalsQuery.error}
            onRetry={() => {
              void signalsQuery.refetch()
            }}
            retryLabel="Retry"
            compact
          />
        )}
        {!signalsQuery.isError && isSignalsPending && (
          <RowsSkeleton rows={3} label="Loading signals…" />
        )}
        {!signalsQuery.isError && !isSignalsPending && signals.length === 0 && (
          <div className="text-body-sm text-fg-tertiary">
            No active monitoring signals.
          </div>
        )}
        {signals.length > 0 && slug && (
          <div className="divide-y border-border-subtle">
            {signals.slice(0, SIGNAL_LIMIT).map((signal) => (
              <SignalRow
                // Signals are per scan config: two scans watching one event
                // each open their own, and a scope-only key collided.
                key={`${signal.scan_config_id ?? 'metric'}:${signal.scope_type}:${signal.scope_ref}:${signal.bucket}`}
                slug={slug}
                signal={signal}
              />
            ))}
          </div>
        )}
        </div>
      </Panel>

      {/* Volume — one scan config, named. Once labelled "project total", where it plotted 2.4 % of
          acme-ios's volume directly above a "Top events" row 12× larger. */}
      <Panel
        // The window is in the title because the card is capped at it; the
        // sibling panel below already names its own ("Top events · 48h").
        title={
          volumeScanName
            ? `Volume · ${volumeScanName} · ${VOLUME_WINDOW_DAYS}d`
            : `Volume · ${VOLUME_WINDOW_DAYS}d`
        }
        // Held through the pending state as well, so the header keeps its second
        // line instead of growing one when the series lands.
        subtitle={volumeScanName || isVolumePending ? VOLUME_SUBTITLE : undefined}
        // The card leads to the chart it summarises.
        right={
          projectTotalPath && volumePoints.length > 0 ? (
            <Link
              to={projectTotalPath}
              className="rounded-md px-2 py-1 text-body-sm no-underline transition-colors hover:bg-[var(--surface-hover)] text-accent"
            >
              Open chart
            </Link>
          ) : undefined
        }
      >
        <div className="p-4">
        {volumeQuery.isError && (
          <ErrorState
            title="Volume unavailable"
            error={volumeQuery.error}
            onRetry={() => {
              void volumeQuery.refetch()
            }}
            retryLabel="Retry"
            compact
          />
        )}
        {!volumeQuery.isError && isVolumePending && <VolumeSkeleton />}
        {!volumeQuery.isError && !isVolumePending && volumePoints.length === 0 && (
          // Two different facts, and the card used to report only the second.
          // A scan whose last bucket predates the window has months of history
          // and nothing here — that is a scan that stopped, the state the
          // failing-scan chip above exists to surface, not an empty project.
          // The drilldown carries a range selector, so it can show the rest.
          <div className="text-body-sm text-fg-tertiary">
            {projectTotalPath ? (
              <>
                No volume in the last {VOLUME_WINDOW_DAYS} days.{' '}
                <Link
                  to={projectTotalPath} className="text-accent"
                >
                  See this scan’s full history
                </Link>
              </>
            ) : (
              'No volume data yet.'
            )}
          </div>
        )}
        {volumePoints.length > 0 && (
          // The chart takes the rest of the row and scales to it. A fixed 320px
          // SVG beside the figure ran off the card on a phone, cutting off the
          // newest buckets, and left half of a wide card empty.
          <div className="flex flex-wrap items-end gap-x-4 gap-y-2">
            <div
              role="group"
              aria-label={volumeHeadlineLabel(volumeSummary)}
              className="flex shrink-0 flex-col gap-px"
            >
              {/* The hero figure: the last 24 hours, not the newest bucket —
                  a partial hour is not a meaningful total. Sans with
                  tabular digits on the display step. */}
              <span className="flex items-baseline gap-2">
                <span className="tnum text-display font-semibold">
                  {formatNumber(volumeSummary.last24h)}
                </span>
                {/* Says what it compares. A bare grey "−6%" under a signal row's
                    "+62%" read as a contradiction: that one is the newest
                    bucket against its expected value, this the whole day
                    against the day before. */}
                {volumeSummary.changePct != null && (
                  <span className="tnum text-caption text-fg-tertiary">
                    {`${formatRatioDelta(volumeSummary.changePct)} vs prior 24h`}
                  </span>
                )}
              </span>
              <span className="text-caption text-fg-tertiary">
                last 24h
              </span>
            </div>
            <div
              role="img"
              aria-label={volumeChartLabel(volumeCounts, volumeScanName)}
              className="min-w-[8rem] flex-1"
            >
              {/* Flagged buckets get the chart's anomaly marker, so the spike
                  behind the Open signals figure is visible here too. */}
              <Sparkline
                data={volumeCounts}
                variant={chartStyle}
                width={320}
                height={48}
                responsive
                anomalyIdx={volumeSummary.lastAnomalyIdx}
              />
              {/* A time axis in words: where the line starts and its cadence,
                  in place of "167 buckets". */}
              <div
                aria-hidden="true"
                className="mt-1 flex justify-between text-micro text-fg-tertiary"
              >
                <span>{volumeSummary.firstLabel}</span>
                <span>{volumeCadence ? `now · ${volumeCadence}` : 'now'}</span>
              </div>
              <span className="sr-only">
                Volume by bucket: {volumeCounts.map((c) => formatNumber(c)).join(', ')}.
              </span>
            </div>
          </div>
        )}
        </div>
      </Panel>

      {/* Top events by volume — summed across EVERY scan config, unlike the
          volume card above it, which charts one. Saying so is what stops the
          two panels reading as a contradiction. */}
      <Panel title="Top events · 48h" subtitle="Across every scan in this project.">
        <div className="p-4">
        {topEventsQuery.isError && (
          <ErrorState
            title="Top events unavailable"
            error={topEventsQuery.error}
            onRetry={() => {
              void topEventsQuery.refetch()
            }}
            retryLabel="Retry"
            compact
          />
        )}
        {!topEventsQuery.isError && isTopEventsPending && (
          <RowsSkeleton rows={4} label="Loading top events…" />
        )}
        {!topEventsQuery.isError && !isTopEventsPending && topEvents.length === 0 && (
          <div className="text-body-sm text-fg-tertiary">
            No event volume in the last 48 hours.
          </div>
        )}
        {topEvents.length > 0 && (
          <div role="list" aria-label="Top events by volume, last 48 hours" className="space-y-1">
            {topEvents.map((e) => {
              // The event's share of the project's volume over the same window.
              // Left out when the total is unknown or zero rather than
              // printed as 0%.
              const share = e.window_total_count > 0 ? e.total_count / e.window_total_count : null
              const shareLabel =
                share == null ? null : formatNumber(share, { style: 'percent', maximumFractionDigits: share < 0.1 ? 1 : 0 })
              const row = (
                <>
                  {/* The label column grows with the panel instead of sitting
                      at a fixed 10rem. Event names share long prefixes
                      (`feature_flag:flag_use:app` vs `…:growthbook`), so a fixed
                      column truncated the top rows to one identical string and
                      the ranking became unreadable. Capped
                      narrower so the bar starts near the names rather than mid
                      card, and on a phone the name sits above its bar instead
                      of being squeezed beside it. Sans: a display name
                      is not code. */}
                  <span
                    className="w-full truncate text-body-sm sm:w-[min(40%,16rem)] sm:shrink-0"
                    title={e.name}
                  >
                    {e.name}
                  </span>
                  <span className="flex min-w-0 flex-1 items-center gap-3">
                    <span
                      aria-hidden="true"
                      className="relative h-2 flex-1 overflow-hidden rounded-full bg-surface-active"
                    >
                      <span
                        className="absolute inset-y-0 left-0 rounded-full"
                        style={{
                          width: `${maxTopVolume > 0 ? (e.total_count / maxTopVolume) * 100 : 0}%`,
                          background: SERIES_COLORS[0],
                        }}
                      />
                    </span>
                    {/* The counts are the data: body ink, not the faintest
                        text on the card. */}
                    <span className="tnum w-20 shrink-0 text-right text-caption text-fg">
                      {formatNumber(e.total_count)}
                    </span>
                    {shareLabel && (
                      <span
                        className="tnum w-10 shrink-0 text-right text-caption text-fg-tertiary"
                        title="Share of the project's volume in the same window"
                      >
                        {shareLabel}
                      </span>
                    )}
                  </span>
                </>
              )
              const rowClass = 'flex flex-wrap items-center gap-x-3 gap-y-0.5 rounded-sm py-0.5 sm:flex-nowrap'
              return (
                <div
                  key={e.event_id}
                  role="listitem"
                  aria-label={`${e.name}: ${formatNumber(e.total_count)} events${shareLabel ? `, ${shareLabel} of the total` : ''}`}
                >
                  {/* Each row opens the event's own monitoring page. */}
                  {slug ? (
                    <Link
                      to={getMonitoringPath(slug, { scope_type: 'event', scope_ref: e.event_id })}
                      className={`${rowClass} no-underline transition-colors hover:bg-[var(--surface-hover)] text-inherit`}
                    >
                      {row}
                    </Link>
                  ) : (
                    <div className={rowClass}>{row}</div>
                  )}
                </div>
              )
            })}
          </div>
        )}
        </div>
      </Panel>

      {/* Recent activity — not while the rail shows the same feed beside it. */}
      {!railShowsActivity && (
      <Panel title="Recent activity">
        <div className="p-4">
        {activityQuery.isError && (
          <ErrorState
            title="Activity unavailable"
            error={activityQuery.error}
            onRetry={() => {
              void activityQuery.refetch()
            }}
            retryLabel="Retry"
            compact
          />
        )}
        {!activityQuery.isError && activityQuery.isLoading && (
          <RowsSkeleton rows={3} label="Loading activity…" />
        )}
        {!activityQuery.isError && !activityQuery.isLoading && activity.length === 0 && (
          <div className="text-body-sm text-fg-tertiary">
            No recent activity.
          </div>
        )}
        {/* The rail's own rows, so the feed reads the same with the rail
            open or closed. */}
        {activity.length > 0 && (
          <div className="divide-y border-border-subtle">
            <ActivityFeed items={activity} variant="panel" />
          </div>
        )}
        </div>
      </Panel>
      )}

      {/* Plan health (F15, #268): the main plan's score beside the sources'. */}
      {slug && <OverviewPlanHealthPanel slug={slug} sparklineVariant={chartStyle} />}

      {/* Source health */}
      <Panel title="Source health">
        <div className="p-4">
        {sourcesQuery.isError && (
          <ErrorState
            title="Data sources unavailable"
            error={sourcesQuery.error}
            onRetry={() => {
              void sourcesQuery.refetch()
            }}
            retryLabel="Retry"
            compact
          />
        )}
        {!sourcesQuery.isError && sourcesQuery.isLoading && (
          <RowsSkeleton rows={2} label="Loading data sources…" />
        )}
        {!sourcesQuery.isError && !sourcesQuery.isLoading && sources.length === 0 && (
          <div className="text-body-sm text-fg-tertiary">
            No data sources connected.
          </div>
        )}
        {sources.length > 0 && (
          <div className="divide-y border-border-subtle">
            {sources.map((source) => (
              <SourceRow
                key={source.id}
                source={source}
                freshness={dataSourceFreshness(source.id, freshnessItems)}
              />
            ))}
          </div>
        )}
        {/* The scans behind a late source, each opening its scan page: the
            source row says THAT something is late, this says which scan and
            by how much (F16, #269). */}
        {slug && lateScans.length > 0 && (
          <ul aria-label="Late or overdue scans" className="mt-2 space-y-1 border-t pt-2 border-border-subtle">
            {lateScans.map((item) => (
              <li key={item.id} className="flex items-center gap-2 text-body-sm">
                <Link
                  to={projectPath(currentOrgSlug(), slug, `/scans/${item.id}`)}
                  className="min-w-0 flex-1 truncate no-underline hover:underline text-inherit"
                >
                  {item.name}
                </Link>
                <FreshnessChip freshness={item.freshness} name={item.name} className="shrink-0" />
              </li>
            ))}
          </ul>
        )}
        </div>
      </Panel>
      </>
      )}
    </PageContainer>
  )
}


/**
 * The loaded card's shape, held while the series is in flight.
 *
 * The panel is the first content under the KPI strip, and it sat on a bare
 * "Loading…" in an empty box for 2.2 s after the KPI numbers, the 14d sparkline,
 * Top events, Active signals and Recent activity had all rendered — pending, but
 * reading as broken. The blocks match the loaded layout (figure + caption beside
 * a 48px chart that fills the row) so the card reserves its height.
 */
function VolumeSkeleton() {
  return (
    <div className="flex flex-wrap items-end gap-x-4 gap-y-2">
      <div className="flex flex-col gap-1">
        <Skeleton className="h-7 w-24" />
        <Skeleton className="h-3 w-32" />
      </div>
      <Skeleton className="h-12 min-w-[8rem] flex-1" />
      {/* Skeleton is aria-hidden, so the pending state still needs to be said. */}
      <span role="status" className="sr-only">
        Loading volume…
      </span>
    </div>
  )
}

/**
 * A panel body's rows while its query is in flight: the loaded shape instead of
 * a "Loading…" word, so the cards hold their height. One
 * `role="status"` with the label; the bars are aria-hidden.
 */
function RowsSkeleton({ rows, label }: { rows: number; label: string }) {
  return (
    <div role="status" aria-live="polite" aria-busy="true" className="space-y-2.5">
      <span className="sr-only">{label}</span>
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="flex items-center gap-3">
          <Skeleton className="h-3 w-1/3" />
          <Skeleton className="h-3 flex-1" />
          <Skeleton className="h-3 w-12" />
        </div>
      ))}
    </div>
  )
}

/**
 * A KPI that opens where its number is worked on. The whole stat is
 * the link, so its name reads "In review 8".
 */
function KpiLink({ to, children }: { to?: string; children: ReactNode }) {
  if (!to) return <>{children}</>
  return (
    <Link
      to={to}
      className="-m-1 block rounded-sm p-1 no-underline outline-none transition-colors hover:bg-[var(--surface-hover)] focus-visible:ring-2 focus-visible:ring-[var(--accent)] text-inherit"
    >
      {children}
    </Link>
  )
}

/**
 * The one-line answer to "is everything OK?" under the title: open
 * signals, incidents still owed an answer, failing scans, alert destinations
 * whose deliveries fail and source health,
 * each linking where it is dealt with. A clause whose data has not arrived is
 * left out rather than guessed.
 */
function OverviewStatus({
  slug,
  signalCount,
  openIncidents,
  failingScans,
  failingDestinations,
  sources,
}: {
  slug: string
  /** Null while the signals are still loading. */
  signalCount: number | null
  openIncidents: number
  failingScans: number
  /** Enabled alert destinations whose latest delivery failed. */
  failingDestinations: number
  /** Null while the sources are still loading. */
  sources: DataSource[] | null
}) {
  const linkStyle = { color: 'var(--accent)' }
  const parts: ReactNode[] = []
  // "Open signals", the KPI's word for this count: the line said "3 open
  // anomalies" a few pixels above "Open signals 3", which read as two things.
  if (signalCount != null) {
    parts.push(
      signalCount > 0 ? (
        <Link to={projectPath(currentOrgSlug(), slug, '/anomalies')} style={linkStyle}>
          {countOf(signalCount, 'open signal', 'open signals')}
        </Link>
      ) : (
        'No open signals'
      ),
    )
  }
  if (openIncidents > 0) {
    parts.push(
      <Link to={getAlertingPath(slug)} style={linkStyle}>
        {countOf(openIncidents, 'open incident', 'open incidents')}
      </Link>,
    )
  }
  if (failingScans > 0) {
    parts.push(
      <Link to={projectPath(currentOrgSlug(), slug, '/scans')} style={linkStyle}>
        {countOf(failingScans, 'failing scan', 'failing scans')}
      </Link>,
    )
  }
  // A broken channel means incidents fire and nobody hears them, so it sits
  // next to the failing scans rather than only on the Alerting page.
  if (failingDestinations > 0) {
    parts.push(
      <Link to={getAlertingPath(slug)} style={linkStyle}>
        {countOf(failingDestinations, 'broken alert channel', 'broken alert channels')}
      </Link>,
    )
  }
  if (sources && sources.length > 0) {
    const tones = sources.map((source) => sourceHealth(source).tone)
    const failing = tones.filter((tone) => tone === 'danger').length
    if (failing > 0) parts.push(countOf(failing, 'source failing', 'sources failing'))
    else if (tones.every((tone) => tone === 'success')) parts.push('sources healthy')
  }
  if (parts.length === 0) return null
  return (
    <span>
      {parts.map((part, index) => (
        <Fragment key={index}>
          {index > 0 && ' · '}
          {part}
        </Fragment>
      ))}
    </span>
  )
}

interface VolumeSummary {
  /** Events in the buckets that started in the last 24 hours. */
  last24h: number
  /** % change against the 24 hours before; null when those are not covered. */
  changePct: number | null
  /** The newest flagged bucket, for the sparkline's anomaly marker. */
  lastAnomalyIdx: number | null
  /** "Sep 19": where the line starts. */
  firstLabel: string
}

const DAY_MS = 24 * 60 * 60 * 1000

/**
 * The volume card's headline. The card read "6,556 · latest bucket":
 * one partial hour, not a meaningful total. This sums the last 24 hours and
 * compares them with the 24 before — the newest bucket is still filling, so a
 * small dip in the change is expected late in an hour.
 */
function summarizeVolume(points: EventMetricPoint[], now: number = Date.now()): VolumeSummary {
  let last24h = 0
  let prior24h = 0
  let priorCovered = false
  let lastAnomalyIdx: number | null = null
  for (let index = 0; index < points.length; index += 1) {
    const point = points[index]!
    if (point.is_anomaly) lastAnomalyIdx = index
    const start = Date.parse(point.bucket)
    if (Number.isNaN(start)) continue
    if (start > now - DAY_MS) last24h += point.count
    else if (start > now - 2 * DAY_MS) {
      prior24h += point.count
      priorCovered = true
    }
  }
  const first = points[0] ? new Date(points[0].bucket) : null
  return {
    last24h,
    changePct: priorCovered && prior24h > 0 ? ((last24h - prior24h) / prior24h) * 100 : null,
    lastAnomalyIdx,
    firstLabel:
      first && !Number.isNaN(first.getTime())
        ? first.toLocaleDateString(APP_LOCALE, { month: 'short', day: 'numeric' })
        : '',
  }
}

function volumeHeadlineLabel(summary: VolumeSummary): string {
  const change =
    summary.changePct == null
      ? ''
      : `, ${formatRatioDelta(summary.changePct)} against the 24 hours before`
  return `Volume in the last 24 hours: ${formatNumber(summary.last24h)}${change}`
}

// Text alternative for the volume sparkline: the SVG itself is
// aria-hidden, so the surrounding role="img" needs an accessible summary. Names
// the scan the series is scoped to rather than calling it the project
// total, which it never was.
function volumeChartLabel(counts: number[], scanName: string | null): string {
  const scope = scanName ? `Volume sparkline for scan ${scanName}` : 'Volume sparkline'
  if (counts.length === 0) return scope
  const latest = counts[counts.length - 1]!
  const min = Math.min(...counts)
  const max = Math.max(...counts)
  return `${scope}. ${counts.length} buckets. Latest ${formatNumber(latest)}, range ${formatNumber(min)} to ${formatNumber(max)}.`
}

// Text alternative for the new-events trend sparkline (issue #12). Mirrors
// volumeChartLabel: the SVG is decorative, so the wrapping role="img" needs an
// accessible summary of what the 14-day line actually shows. It says "new
// events" because that is what the series counts — announcing it as "active
// events" made the screen-reader text state a falsehood.
function newEventsTrendLabel(counts: number[]): string {
  if (counts.length === 0) return 'New events added per day over the last 14 days'
  const latest = counts[counts.length - 1]!
  const min = Math.min(...counts)
  const max = Math.max(...counts)
  return `New events added per day over the last 14 days. Latest ${formatNumber(latest)}, range ${formatNumber(min)} to ${formatNumber(max)}.`
}

function SignalRow({
  slug,
  signal,
}: {
  slug: string
  signal: MonitoringSignal
}) {
  const verb = signal.direction === 'drop' ? 'Drop' : 'Spike'
  const scopeLabel = signalScopeLabel(signal)
  // Full text drives both the visible label and its hover tooltip so a long
  // scope name (e.g. page_value_question_page_value_…) stays readable when the
  // row ellipsizes. When the server could not name the scope the tooltip is
  // where its ref goes — visible, that hex prefix reads as a name and puts a
  // second name on an incident the activity rail already named.
  const signalSummary = `${verb} on ${scopeLabel ?? unnamedScopeLabel(signal)}`
  const signalTitle = `${verb} on ${scopeLabel ?? signalScopeRefLabel(signal)}`
  return (
    <Link
      to={getMonitoringPath(slug, signal)}
      className="flex min-h-(--row-h) items-center gap-2 py-1 no-underline transition-colors hover:bg-[var(--surface-hover)] text-inherit"
    >
      {/* Static: only the Open signals KPI pulses, so motion still means
          "live" rather than shimmering down every row. */}
      <Dot tone={signalDirectionTone(signal.direction)} size={7} />
      <span className="flex-1 truncate text-body-sm font-medium" title={signalTitle}>
        {signalSummary}
      </span>
      {/* "7,173 vs 2,403 expected" and the bucket it counts, as the Anomalies
          page's columns say: bare "7,173 vs 2,403" named neither the baseline
          nor the time. */}
      <span className="tnum hidden shrink-0 text-caption sm:inline text-fg-tertiary">
        {`${formatSignalValues(signal)} expected`}
      </span>
      <time
        dateTime={signal.bucket}
        title={signalTimeTitle('Bucket starting', signal.bucket)}
        className="tnum hidden shrink-0 text-caption md:inline text-fg-tertiary"
      >
        {formatShortTimestamp(signal.bucket, { today: true })}
      </time>
      {/* "+203%", not z=40.7: the change in the reader's terms, with the
          magnitude word and z-score on hover. */}
      <span
        className="tnum w-24 shrink-0 text-right text-caption font-semibold"
        style={{ color: signalDirectionColor(signal.direction) }}
        title={formatSignalEffectDetail(signal)}
      >
        {formatSignalEffect(signal)}
      </span>
    </Link>
  )
}

// A green "healthy" badge over a months-old check is misleading. When the last
// successful test is stale we downgrade the label to "stale" and the recency is
// always made explicit ("checked 2mo ago" + an absolute timestamp tooltip) (M1).
function sourceHealth(source: DataSource, now: number = Date.now()): StatusLexeme {
  const checkedAt = source.last_test_at ? Date.parse(source.last_test_at) : NaN
  const isStale = Number.isNaN(checkedAt) || now - checkedAt > SOURCE_HEALTH_STALE_MS
  return dataSourceHealthLexeme(source.last_test_status, isStale)
}

function SourceRow({
  source,
  freshness,
}: {
  source: DataSource
  /** The worst-freshness scan reading this source, if any (F16, #269). */
  freshness?: SourceFreshnessItem | null
}) {
  const { tone, label } = sourceHealth(source)
  const checkedLabel = source.last_test_at
    ? `checked ${formatRelativeTime(source.last_test_at)}`
    : 'never checked'
  const checkedTitle = source.last_test_at
    ? `Last checked ${formatDateTime(source.last_test_at)}`
    : 'Never checked'
  // Wraps on a phone. The fixed columns and chips used to take the whole row,
  // leaving the source name ~40px and slicing "checked 1h" off the edge; now
  // the name keeps an 8rem basis, the engine (the badge already says
  // "synthetic") drops below `sm`, and the check time moves to a second line.
  //
  // The engine shows only when it adds something: a synthetic source's badge
  // already says "Synthetic", and printing `synthetic` beside it said it twice.
  // It is the warehouse's name ("ClickHouse"), as the Data sources page writes
  // it, not the raw type key.
  // The status is a toned chip, the one status idiom, rather than grey text
  // next to a coloured dot; the row opens the source.
  const showEngine = !(source.is_synthetic && source.db_type === 'synthetic')
  return (
    <Link
      to={settingsPath(`/settings/data-sources/${source.id}`)}
      className="flex min-h-(--row-h) flex-wrap items-center gap-x-2 gap-y-0.5 py-2 no-underline transition-colors hover:bg-[var(--surface-hover)] text-inherit"
    >
      <Database aria-hidden="true" className="h-3.5 w-3.5 shrink-0 text-fg-tertiary" />
      <span className="min-w-0 flex-1 basis-32 truncate text-body-sm font-medium" title={source.name}>
        {source.name}
      </span>
      {source.is_synthetic && <SyntheticSourceBadge />}
      {showEngine && (
        <span className="hidden shrink-0 text-micro sm:inline text-fg-tertiary">
          {dbTypeLabel(source.db_type)}
        </span>
      )}
      <Chip tone={tone} className="shrink-0">
        {label}
      </Chip>
      {freshness && (
        <FreshnessChip freshness={freshness.freshness} name={freshness.name} size="sm" className="shrink-0" />
      )}
      <span
        className="ml-auto shrink-0 truncate text-right text-caption sm:ml-0 sm:w-[104px] text-fg-tertiary"
        title={checkedTitle}
      >
        {checkedLabel}
      </span>
    </Link>
  )
}
