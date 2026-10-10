import { useCallback, useMemo } from "react"
import { Panel } from '@/components/settings/kit'
import { Link, useNavigate } from "react-router-dom"
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query"
import { Plus, RotateCw } from "lucide-react"
import { eventTypesApi } from "@/api/eventTypes"
import { scansApi } from "@/api/scans"
import { useDemoScenarioActions, useScenarioArtifacts } from "@/demo/demoScenarioContext"
import { ScenarioCoachMark } from "@/demo/ScenarioCoachMark"
import type { ScanActivityResponse, ScanConfig, ScanJob } from "@/types"
import { Button } from "@/components/ui/button"
import { EmptyState } from "@/components/empty-state"
import { ErrorState } from "@/components/error-state"
import { Skeleton } from "@/components/ui/skeleton"
import { Chip } from "@/components/primitives/chip"
import { Search } from "lucide-react"
import { RunStatusPill, ScanListRow } from "./scans/ScanConfigRow"
import { FirstScanEmptyState } from "./scans/FirstScanEmptyState"
import { runPillStatus } from "./scans/scanRunStatus"
import { scanModeOf } from "./scans/scanMode"
import { PageHeader } from '@/components/primitives/page-header'
import { TermHint } from '@/components/term-hint'
import { TERM_HINTS } from '@/lib/termHints'
import { PageContainer } from '@/components/primitives/page-container'
import { MiniStat, MiniStatStrip } from '@/components/primitives/mini-stat'
import { formatCompactNumber } from "@/lib/format"
import { INTERVAL_LABEL } from "./scans/scanLayoutConstants"
import { LOADING_SCAN_RUN_INFO, consecutiveFailedRuns, deriveScanRunInfo, formatJobScanned, jobDurationSeconds, jobScanned, scanJobsHaveActiveWork, summarizeScanChanges, type JobScanned, type ScanChange, type ScanRunInfo } from "./scans/scanUtils"
import { useAdaptiveRefetchIntervalFn } from "@/realtime/streamContext"
import { friendlyScanError } from "@/lib/scanError"
import { formatRelativeTime } from "@/lib/datetime"
import { countOf } from "@/lib/plural"
import { getErrorMessage } from '@/lib/utils'
import { projectEventTypesKey, scanActivityKey, scanJobsKey, scanJobsLimitedKey, scansKey } from '@/lib/queryKeys'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useProjectDataSources } from '@/hooks/useProjectDataSources'
import { useCanWriteProject, useIsOwner } from '@/lib/permissions'
import { DisabledReason, ReadOnlyNotice, StatValueSkeleton, disabledReasonAria } from '@/components/states'
import { currentOrgSlug, projectPath, settingsPath } from '@/lib/navigation'

/**
 * Jobs per scan the list asks for. It shows the head of each history (the last
 * run, and a collapsed failing streak), so 50 full jobs per scan, re-polled for
 * every scan while any one is active, was almost all waste. The
 * figures that need the whole history — the streak's length and the 24h rows —
 * come from the activity endpoint instead, so this cap no longer bounds them.
 */
const SCAN_LIST_JOBS_LIMIT = 10

// Module-level, so `useQueries` keeps one `combine` and hands back a stable
// result while the underlying data has not changed.
const jobsData = (results: { data?: ScanJob[] }[]) => results.map(result => result.data)

interface RecentRun {
  jobId: string
  scanId: string
  scanName: string
  startedAt: string | null
  /** Warehouse rows or catalog combinations, unit named (#247). */
  scanned: JobScanned | null
  durationSec: number | null
  status: ScanJob['status']
  errorMessage: string | null
  // Current failing streak (consecutive failed runs for this scan, counted by
  // the server over its whole history). Only meaningful on the collapsed
  // streak row; 0 on every other row, and until the activity has loaded.
  failingStreak: number
  // The scan's newest settled run. Only there does "Run again" answer the
  // failure: on an older one a success has already followed (#247).
  latestSettled: boolean
  // What the completed job actually changed (+N events / metrics / signals …).
  changes: ScanChange[]
}

/** Why New scan is off: a scan reads from a data source. */
const NEW_SCAN_BLOCKER = 'Add a data source first.'

/** Rows the "Recent runs" panel shows across every scan. */
const RECENT_RUNS_SHOWN = 6

export function ScansTab({ slug }: { slug: string }) {
  const navigate = useNavigate()
  const { notifyScanRunStarted } = useDemoScenarioActions()
  // Null for every non-demo project — no run row is ever the scenario's row.
  const { scanJobId } = useScenarioArtifacts()
  // Authoring a scan (and its SQL) is OwnerUserDep; running one is an editor's
  // job. Each control below is offered only to a role that can use it.
  const isOwner = useIsOwner()
  const canRun = useCanWriteProject()

  // Scoped to this project, and only a LOADED empty list means "no
  // data sources": during a cold load the empty state and the disabled New
  // scan used to flash for everyone.
  const {
    data: dataSources = [],
    isSuccess: dataSourcesLoaded,
    isError: dataSourcesFailed,
    error: dataSourcesError,
    refetch: refetchDataSources,
  } = useProjectDataSources()
  const noDataSources = dataSourcesLoaded && dataSources.length === 0

  const {
    data: scanConfigs = [],
    isLoading: scanConfigsLoading,
    isError: scanConfigsError,
    error: scanConfigsErrorObj,
    refetch: refetchScanConfigs,
  } = useQuery({
    queryKey: scansKey(slug),
    queryFn: () => scansApi.list(slug),
  })

  // Resolve a scan's single event type to its name so "Review events" can open
  // that type's pending-review queue; scans with no fixed type (or a per-row
  // event_type_column) fall back to the whole review tab.
  const { data: eventTypes = [] } = useQuery({
    queryKey: projectEventTypesKey(slug),
    queryFn: () => eventTypesApi.list(slug),
  })
  const eventTypeNameById = useMemo(
    () => new Map(eventTypes.map(et => [et.id, et.name])),
    [eventTypes],
  )
  const reviewEventsHref = useCallback(
    (sc: ScanConfig) => {
      const typeName = sc.event_type_id ? eventTypeNameById.get(sc.event_type_id) : undefined
      return typeName
        ? projectPath(currentOrgSlug(), slug, `/events/${typeName}?status=in_review`)
        : projectPath(currentOrgSlug(), slug, '/events/review')
    },
    [slug, eventTypeNameById],
  )

  // Per-scan jobs power the "Last run" status and the "Recent runs" feed. The
  // backend exposes jobs per scan only, so this is still one query per config,
  // each capped at the head of its history.
  const jobsRefetchInterval = useAdaptiveRefetchIntervalFn<ScanJob[]>({
    activeMs: 10000,
    isActive: scanJobsHaveActiveWork,
  })
  // Its own key under the scan's `['scanJobs', slug, id]` prefix: the detail
  // page caches the full history there, and a capped list must not stand in for
  // it. Every invalidation of the prefix still reaches both.
  const jobsByScan = useQueries({
    queries: scanConfigs.map((sc: ScanConfig) => ({
      queryKey: scanJobsLimitedKey(slug, sc.id, SCAN_LIST_JOBS_LIMIT),
      queryFn: () => scansApi.listJobs(slug, sc.id, { limit: SCAN_LIST_JOBS_LIMIT }),
      refetchInterval: jobsRefetchInterval,
    })),
    combine: jobsData,
  })

  // The exact figures the capped job pages cannot give: each scan's failing
  // streak over its whole history, and the rows read in the last 24 hours,
  // aggregated by the server. Keyed under the `['scanJobs',
  // slug]` prefix so the stream's scan-job invalidation refreshes it too.
  const activityRefetchInterval = useAdaptiveRefetchIntervalFn<ScanActivityResponse>({
    activeMs: 10000,
    isActive: data =>
      scanJobsHaveActiveWork(
        data?.items.flatMap(item => (item.latest_job ? [item.latest_job] : [])),
      ),
  })
  const { data: activity } = useQuery({
    queryKey: scanActivityKey(slug),
    queryFn: () => scansApi.activity(slug),
    refetchInterval: activityRefetchInterval,
    // Refetched on every visit: a run started on a scan's own page invalidates
    // only that scan's keys, and without a live stream the list would otherwise
    // come back to a minute-old streak.
    staleTime: 0,
  })
  const failingStreakById = useMemo(
    () => new Map((activity?.items ?? []).map(item => [item.scan_config_id, item.failing_streak])),
    [activity],
  )

  const dsMap = useMemo(
    () => new Map(dataSources.map(ds => [ds.id, ds])),
    [dataSources],
  )

  // A job query that has not resolved yet passes `undefined` through, so the row
  // renders a loading placeholder instead of the definitive "Never run" verdict
  // it used to show while the fan-out was still in flight.
  const runInfoById = useMemo(() => {
    const map = new Map<string, ScanRunInfo>()
    scanConfigs.forEach((sc: ScanConfig, index: number) => {
      map.set(sc.id, deriveScanRunInfo(jobsByScan[index]))
    })
    return map
  }, [scanConfigs, jobsByScan])

  const recentRuns = useMemo<RecentRun[]>(() => {
    const runs: RecentRun[] = []
    // Two runs a scan keeps one busy scan from filling the panel, but with one
    // or two scans it cut the history to 2-4 rows and read as a short one
    // (#247): take enough per scan to fill the panel.
    const perScan = scanConfigs.length < 3
      ? Math.ceil(RECENT_RUNS_SHOWN / Math.max(scanConfigs.length, 1))
      : 2
    scanConfigs.forEach((sc: ScanConfig, index: number) => {
      const jobs = jobsByScan[index] ?? []
      if (jobs.length === 0) return
      // Jobs arrive newest-first. The failing streak is the detail page's own
      // count (`consecutiveFailedRuns`), which looks past a queued or running
      // retry: counting only LEADING failures made a pending retry after five
      // failures read "failed last 5 runs" on the detail page and nothing here.
      // The streak collapses into its newest failure, tagged, after
      // any active run; with no streak the two most recent jobs show as before.
      // The loaded page decides which rows collapse; the number on the tag is
      // the server's, which counts past the page.
      const streak = consecutiveFailedRuns(jobs)
      const firstSettled = jobs.findIndex(job => job.status !== 'pending' && job.status !== 'running')
      const streakHead = streak > 0 ? jobs[firstSettled] : null
      const collapsed = streakHead
        ? [
          ...jobs.slice(0, firstSettled),
          streakHead,
          ...jobs.slice(firstSettled + streak, firstSettled + streak + perScan),
        ].slice(0, perScan)
        : jobs.slice(0, perScan)
      collapsed.forEach(job => {
        runs.push({
          jobId: job.id,
          scanId: sc.id,
          scanName: sc.name,
          startedAt: job.started_at ?? job.created_at,
          scanned: jobScanned(job),
          durationSec: jobDurationSeconds(job),
          status: job.status,
          errorMessage: job.error_message,
          // The server counts past the loaded page; the page's own count stands
          // in while the activity loads, if it failed, or if it is older than
          // the page (a run finished since), so the tag never vanishes.
          failingStreak: job === streakHead ? Math.max(failingStreakById.get(sc.id) ?? 0, streak) : 0,
          latestSettled: firstSettled >= 0 && job === jobs[firstSettled],
          changes: summarizeScanChanges(job),
        })
      })
    })
    return runs
      .sort((a, b) => (b.startedAt ?? '').localeCompare(a.startedAt ?? ''))
      .slice(0, RECENT_RUNS_SHOWN)
  }, [scanConfigs, jobsByScan, failingStreakById])

  // Null until the activity has arrived: "0" while loading contradicted the
  // completed runs already listed in the activity rail, so the tile reads "—"
  // until then. Exact, not a floor: the server sums every
  // job in the window rather than the capped page this list loads.
  // Warehouse rows and catalog combinations are summed apart: they are
  // different units, and adding them made the tile a number with no name
  // (#247).
  const warehouseRows24h = useMemo<number | null>(
    () =>
      activity
        ? activity.items.reduce((total, item) => total + (item.warehouse_rows_24h ?? 0), 0)
        : null,
    [activity],
  )
  const catalogCombinations24h = useMemo<number | null>(
    () =>
      activity
        ? activity.items.reduce((total, item) => total + (item.catalog_combinations_24h ?? 0), 0)
        : null,
    [activity],
  )

  // Both the per-row "Run now" and the failed-row "Run again" reuse the manual
  // scan trigger (POST /scans/{id}/run). On success we refetch that scan's jobs
  // so the new pending run appears in the feed.
  const queryClient = useQueryClient()
  const runScan = useMutation({
    // Rendered inline, naming the scan it was for.
    meta: SILENT_ERROR_META,
    mutationFn: (scanId: string) => scansApi.run(slug, scanId),
    onSuccess: (job, scanId) => {
      // Only the job this POST returned can advance the coached demo scenario:
      // the demo's tick creates scan jobs on its own.
      notifyScanRunStarted(job)
      void queryClient.invalidateQueries({ queryKey: scanJobsKey(slug, scanId) })
      void queryClient.invalidateQueries({ queryKey: scanActivityKey(slug) })
    },
  })
  // The UI tracks one visibly-pending manual run via this shared mutation's
  // variables; a rapid second click on another row moves the busy indicator to
  // the newest request. Both the row-level "Run now" and the failed-row
  // "Run again" derive their busy state from this id.
  const pendingScanId = runScan.isPending ? runScan.variables : undefined
  const failedRunScanName = runScan.isError
    ? scanConfigs.find((sc: ScanConfig) => sc.id === runScan.variables)?.name ?? 'this scan'
    : null

  // Counting `interval` alone counted the broken quadrant — a schedule with no
  // time column is never dispatched, so it monitors nothing.
  const monitoringCount = scanConfigs.filter(
    (sc: ScanConfig) => scanModeOf(sc) === 'monitoring',
  ).length
  // Scans whose latest settled run failed, by the server's streak: the one
  // aggregate the strip was missing (#247). Null until it answers.
  const failingCount = activity
    ? activity.items.filter(item => item.failing_streak > 0).length
    : null

  // A loaded, empty list is the whole page: one empty state that says what
  // setting up a scan involves, instead of zero tiles over "No data sources"
  // over an empty "All scans" panel (#247).
  const noScans = !scanConfigsLoading && !scanConfigsError && scanConfigs.length === 0

  return (
    <PageContainer>
      {/* The shared page header: a real h1 under the Govern
          eyebrow, like Reconciliation and Coverage, instead of an h2 text-heading
          with a 14px paragraph. The description says what a scan PRODUCES and
          what consumes it, because a scan's output reaches the user as
          anomalies and alerts. */}
      <PageHeader
        eyebrow="Govern"
        title="Scans"
        titleAddon={<TermHint slug={slug} {...TERM_HINTS.scans} />}
        description="Scans read your warehouse into your tracking plan; monitoring scans also record the metric points that anomalies and alerts are built on."
        actions={
          isOwner && !noScans && (
            // The reason New scan is off is a caption under it, not a `title`
            // a disabled button never shows (#237).
            <div className="flex flex-col items-end gap-1">
              <Button
                size="sm"
                disabled={noDataSources}
                {...disabledReasonAria('new-scan', noDataSources ? NEW_SCAN_BLOCKER : null)}
                onClick={() => navigate(projectPath(currentOrgSlug(), slug, '/scans/new'))}
              >
                <Plus className="size-3.5" />
                New scan
              </Button>
              <DisabledReason id="new-scan" reason={noDataSources ? NEW_SCAN_BLOCKER : null} />
            </div>
          )
        }
        // The one page-KPI strip, in place of three bordered tiles.
        // Hidden while there is nothing to count (#247).
        stats={
          noScans ? undefined : (
            <MiniStatStrip boxed>
              {/* Not "0" before the list answers (#237). */}
              <MiniStat label="Scans" value={scanConfigsLoading ? <StatValueSkeleton /> : scanConfigs.length} />
              <MiniStat
                label="Monitoring"
                value={scanConfigsLoading ? <StatValueSkeleton /> : monitoringCount}
              />
              <MiniStat
                label="Failing"
                value={failingCount == null ? <StatValueSkeleton /> : failingCount}
                valueTone={failingCount ? 'danger' : undefined}
              />
              {/* Warehouse rows only: metrics runs, and catalog runs that report
                  the rows behind their breakdown. An older catalog run reports
                  only grouped column combinations, a different unit, so those
                  are named in the title instead of being added in (#247). */}
              <div
                title={
                  catalogCombinations24h
                    ? `Warehouse rows read by runs in the last 24 hours. Catalog runs that report no warehouse rows also read back ${formatCompactNumber(catalogCombinations24h)} column combinations.`
                    : 'Warehouse rows read by runs in the last 24 hours.'
                }
              >
                <MiniStat label="Warehouse rows · 24h" value={warehouseRows24h == null ? '—' : formatCompactNumber(warehouseRows24h)} />
              </div>
            </MiniStatStrip>
          )
        }
      />

      {!isOwner && (
        <ReadOnlyNotice>
          {canRun
            ? 'Creating and changing scans is done by an owner. You can run the scans below.'
            : undefined}
        </ReadOnlyNotice>
      )}

      {noScans ? (
        <FirstScanEmptyState
          isOwner={isOwner}
          dataSourcesSettled={dataSourcesLoaded || dataSourcesFailed}
          noDataSources={noDataSources}
          dataSourcesError={dataSourcesFailed ? dataSourcesError : null}
          onRetryDataSources={() => { void refetchDataSources() }}
          onNewScan={() => navigate(projectPath(currentOrgSlug(), slug, '/scans/new'))}
        />
      ) : (
        <>
          {noDataSources && (
            <EmptyState
              icon={Search}
              title="No data sources"
              description={
                isOwner
                  ? 'Add a data source connection first to create a scan.'
                  : 'An owner has to add a data source connection before scans can be created.'
              }
              action={
                // The empty state used to name the page that fixes it and leave the
                // reader to find it; the link IS the remedy now. Only
                // for an owner: data sources are owner-only, and anyone else landed
                // on a page with nothing they could add.
                isOwner ? (
                  <Button asChild size="sm">
                    <Link to={settingsPath('/settings/data-sources')}>
                      <Plus className="size-3.5" />
                      Add connection
                    </Link>
                  </Button>
                ) : undefined
              }
            />
          )}

          {/* A project has exactly one scan the moment it finishes the onboarding
              checklist's "Run a scan" step, so "1 scans" was the first thing a new
              user read on the page this epic exists to make comprehensible. */}
          <Panel title="All scans" subtitle={countOf(scanConfigs.length, 'scan', 'scans')}>
            {failedRunScanName && (
              <p role="alert" className="border-b px-4 py-2 text-body text-danger border-border-subtle">
                Could not start {failedRunScanName}: {getErrorMessage(runScan.error)}
              </p>
            )}
            {scanConfigsLoading ? (
              <div className="space-y-2 px-4 py-4" aria-busy="true" aria-label="Loading scans">
                {[0, 1, 2].map((index) => (
                  <Skeleton key={index} className="h-10 w-full" />
                ))}
              </div>
            ) : scanConfigsError ? (
              <div className="p-4">
                <ErrorState
                  compact
                  title="Couldn't load scans"
                  error={scanConfigsErrorObj}
                  onRetry={() => {
                    void refetchScanConfigs()
                  }}
                />
              </div>
            ) : (
              <table className="w-full border-collapse">
                {/* Phones get the rows as stacked cards (ScanListRow), so the
                    column headings have nothing to head there. */}
                <thead className="hidden sm:table-header-group">
                  <tr className="bg-bg-sunken">
                    {['Scan', 'Last run'].map(h => (
                      <th
                        key={h}
                        className="px-3.5 py-2 text-left micro-label text-fg-tertiary"
                      >
                        {h}
                      </th>
                    ))}
                    <th className="w-10" />
                  </tr>
                </thead>
                <tbody>
                  {scanConfigs.map((sc: ScanConfig, index: number) => {
                    // One href, two consumers: the row's name link (the keyboard and
                    // screen-reader route) and the row's mouse click. Deriving them
                    // from separate literals is how they drift apart — and this is
                    // the live route, NOT the /settings/scans/ form, which App.tsx
                    // only keeps as a redirect.
                    const detailHref = projectPath(currentOrgSlug(), slug, `/scans/${sc.id}`)
                    return (
                      <ScanListRow
                        key={sc.id}
                        sc={sc}
                        dataSource={dsMap.get(sc.data_source_id) ?? null}
                        runInfo={runInfoById.get(sc.id) ?? LOADING_SCAN_RUN_INFO}
                        intervalLabel={INTERVAL_LABEL}
                        detailHref={detailHref}
                        onNavigate={() => navigate(detailHref)}
                        onRun={canRun ? () => runScan.mutate(sc.id) : undefined}
                        runPending={pendingScanId === sc.id}
                        // The step-1 CTA opens this list; point the coach at the first
                        // row's Run control (inert unless the demo scenario is active).
                        runCoachMark={index === 0}
                        onReviewEvents={() => navigate(reviewEventsHref(sc))}
                      />
                    )
                  })}
                </tbody>
              </table>
            )}
          </Panel>

          {recentRuns.length > 0 && (
            <Panel title="Recent runs" subtitle="Latest runs across all scans">
              <div>
                {recentRuns.map(run => {
                  const isFailed = run.status === 'failed'
                  const friendly = isFailed ? friendlyScanError(run.errorMessage).message : null
                  const showStreak = isFailed && run.failingStreak > 1
                  const showRunAgain = isFailed && canRun && run.latestSettled
                  return (
                    <ScenarioCoachMark
                      key={run.jobId}
                      step="live-loop/watch-scan"
                      // Exactly one row: the run the user's own action started.
                      when={run.jobId === scanJobId}
                      side="top"
                      align="start"
                    >
                      {/* One row from `sm` up. Below it the row wraps — pill, name
                          and the rows/duration figures on the first line, what
                          happened on the next, a failed run's actions under that —
                          because the fixed 150px name and 52px duration left a
                          375px screen nothing for the rest, and "3h ago" ran into
                          "4.8k rows". */}
                      <div
                        className="flex min-h-(--row-h) flex-wrap items-center gap-x-3 gap-y-1.5 border-t px-4 py-2.5 first:border-t-0 sm:flex-nowrap border-border-subtle"
                      >
                        {/* A fixed slot as wide as the widest label
                            ("Succeeded"), so the name, time and figures start
                            at the same x on every row: a narrower "Failed"
                            pulled its row's columns left of the rows above. */}
                        <span data-slot="run-status" className="shrink-0 sm:w-24">
                          <RunStatusPill status={runPillStatus(run.status)} title={friendly ?? undefined} />
                        </span>
                        <span className="min-w-0 flex-1 truncate text-body-sm font-medium sm:w-[150px] sm:flex-none sm:shrink-0">
                          {run.scanName}
                        </span>
                        <div className="order-last flex min-w-0 basis-full flex-col gap-1 sm:order-none sm:basis-auto sm:flex-1">
                          <span className="text-caption text-fg-tertiary">
                            {run.startedAt ? formatRelativeTime(run.startedAt) : '—'}
                          </span>
                          {friendly && (
                            <span className="truncate text-caption text-danger">{friendly}</span>
                          )}
                          {/* What this completed run changed — surfaced inline so a
                              finished scan/collection shows its impact, not just a
                              status pill. */}
                          {!isFailed && run.changes.length > 0 && (
                            <div className="flex flex-wrap gap-1">
                              {run.changes.map((change) => (
                                <Chip key={change.label} tone={change.tone} size="xs">
                                  {change.label}
                                </Chip>
                              ))}
                            </div>
                          )}
                        </div>
                        {/* A failed run's actions take the figures' place; a
                            failed run with none to offer keeps the figures,
                            so its row does not end in a blank. */}
                        {showStreak || showRunAgain ? (
                          <div className="order-last flex shrink-0 flex-wrap items-center gap-2 sm:order-none">
                            {showStreak && (
                              <Chip tone="danger" size="xs" className="whitespace-nowrap">
                                failed last {run.failingStreak} runs
                              </Chip>
                            )}
                            {showRunAgain && (
                              <Button
                                size="xs"
                                variant="outline"
                                disabled={pendingScanId === run.scanId}
                                onClick={() => runScan.mutate(run.scanId)}
                              >
                                <RotateCw className="size-3" aria-hidden="true" />
                                {pendingScanId === run.scanId ? 'Starting…' : 'Run again'}
                              </Button>
                            )}
                          </div>
                        ) : (
                          <>
                            {/* Figures in sans + tabular digits, not mono. */}
                            {/* Full digits and the unit, the same as the scan's
                                own page ("4,428 rows", "153 combos"), in a fixed
                                right-aligned column (#247). */}
                            <span className="tnum shrink-0 whitespace-nowrap text-right text-caption sm:w-[104px] text-fg-tertiary">
                              {formatJobScanned(run.scanned)}
                            </span>
                            <span className="tnum shrink-0 whitespace-nowrap text-right text-caption sm:w-[52px] text-fg-tertiary">
                              {run.durationSec == null ? '—' : `${run.durationSec.toFixed(1)}s`}
                            </span>
                          </>
                        )}
                      </div>
                    </ScenarioCoachMark>
                  )
                })}
              </div>
            </Panel>
          )}
        </>
      )}
    </PageContainer>
  )
}
