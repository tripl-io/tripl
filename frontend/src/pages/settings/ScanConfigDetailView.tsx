import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Play, RotateCcw, Sliders } from 'lucide-react'
import { dataSourcesApi } from '@/api/dataSources'
import { eventTypesApi } from '@/api/eventTypes'
import { scansApi } from '@/api/scans'
import { useDemoScenarioActions } from '@/demo/demoScenarioContext'
import { ScenarioCoachMark } from '@/demo/ScenarioCoachMark'
import { useAdaptiveRefetchIntervalFn } from '@/realtime/streamContext'
import { scanJobsHaveActiveWork } from './scans/scanUtils'
import type { DataSource, ScanConfig, ScanJob } from '@/types'
import { Button } from '@/components/ui/button'
import { Dot } from '@/components/primitives/dot'
import { PageContainer } from '@/components/primitives/page-container'
import { PageBackLink, PageHeader } from '@/components/primitives/page-header'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { ErrorState } from '@/components/error-state'
import { EntityNotFound } from '@/components/states'
import { Skeleton } from '@/components/ui/skeleton'
import { getErrorMessage } from '@/lib/utils'
import { useUnsavedChangesGuard } from '@/hooks/useUnsavedChangesGuard'
import { ScanDetail } from './ScanDetail'
import { ScanCausalNote } from './scans/ScanCausalNote'
import { ScanConfigurationTab } from './scans/ScanConfigForm'
import { ReplayDialog } from './scans/ReplayDialog'
import { ScanBadges } from './scans/ScanConfigRow'
import { INTERVAL_LABEL, SCAN_STATUS_LABEL, STATUS_META } from './scans/scanLayoutConstants'
import { deriveScanRunInfo } from './scans/scanUtils'
import { useMetricsSchedule } from './scans/useMetricsSchedule'
import { dataSourcesKey, eventTypesKey, scanActivityKey, scanJobsKey, scansKey } from '@/lib/queryKeys'
import { useCanWriteProject, useIsOwner } from '@/lib/permissions'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { usePageTitle } from '@/components/shell-chrome-context'
import { currentOrgSlug, projectPath, settingsPath } from '@/lib/navigation'

type DetailTab = 'overview' | 'configuration'

export function ScanConfigDetail({ slug, scanConfigId }: { slug: string; scanConfigId: string }) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { notifyScanRunStarted } = useDemoScenarioActions()
  const canRun = useCanWriteProject()
  const isOwner = useIsOwner()
  // The tab lives in `?tab=` so a reload lands where the reader was, instead of
  // always on Overview. `replace`: flipping tabs is not history.
  const [searchParams, setSearchParams] = useSearchParams()
  const tab: DetailTab = searchParams.get('tab') === 'configuration' ? 'configuration' : 'overview'
  const showTab = (next: DetailTab) =>
    setSearchParams(
      prev => {
        const params = new URLSearchParams(prev)
        if (next === 'overview') params.delete('tab')
        else params.set('tab', next)
        return params
      },
      { replace: true },
    )
  // The Configuration panel is unmounted by the tab switch, so its unsaved
  // edits are guarded here: on leaving the page, and on leaving the tab.
  const [configDirty, setConfigDirty] = useState(false)
  // Bumped by the form's Discard: remounting it from the saved config is the
  // reset that cannot miss a field (#247).
  const [configFormKey, setConfigFormKey] = useState(0)
  const [replayOpen, setReplayOpen] = useState(false)
  const unsaved = useUnsavedChangesGuard(configDirty)
  const setTab = (next: DetailTab) => {
    if (next === tab) return
    unsaved.requestLeave(() => showTab(next))
  }

  const {
    data: scanConfigs = [],
    isSuccess: scansLoaded,
    isError: scansError,
    error: scansErrorObj,
    refetch: refetchScans,
  } = useQuery({
    queryKey: scansKey(slug),
    queryFn: () => scansApi.list(slug),
  })
  const { data: dataSources = [] } = useQuery({
    queryKey: dataSourcesKey(),
    queryFn: () => dataSourcesApi.list(),
  })
  const { data: eventTypes = [] } = useQuery({
    queryKey: eventTypesKey(slug, null),
    queryFn: () => eventTypesApi.list(slug, null),
  })

  const sc = scanConfigs.find(s => s.id === scanConfigId)
  // The scheduler's own next due moment for a monitoring scan:
  // the header chip and the Overview's metrics card both read it.
  const metricsSchedule = useMetricsSchedule(slug, sc)
  usePageTitle(sc?.name)

  const jobsRefetchInterval = useAdaptiveRefetchIntervalFn<ScanJob[]>({
    activeMs: 5000,
    isActive: scanJobsHaveActiveWork,
  })
  const { data: jobs = [] } = useQuery({
    queryKey: scanJobsKey(slug, scanConfigId),
    queryFn: () => scansApi.listJobs(slug, scanConfigId),
    refetchInterval: jobsRefetchInterval,
    enabled: !!sc,
  })

  const runMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => scansApi.run(slug, scanConfigId),
    onSuccess: (job) => {
      // The demo's runtime tick manufactures scan jobs continuously, so only the
      // job *this* POST returned can advance the coached scenario. Inert outside
      // a demo project.
      notifyScanRunStarted(job)
      qc.invalidateQueries({ queryKey: scanJobsKey(slug, scanConfigId) })
      // The Scans list's activity row sits under ['scanJobs', slug] but not
      // under this scan's id, so the key above does not reach it.
      qc.invalidateQueries({ queryKey: scanActivityKey(slug) })
    },
  })

  const scansListPath = projectPath(currentOrgSlug(), slug, '/scans')
  const goBack = () => navigate(scansListPath)
  // A real link: it can be opened in a new tab, and the unsaved-changes guard
  // is a router blocker, so it stops this navigation like any other.
  const backLink = <PageBackLink label="Scans" to={scansListPath} />

  // Loading the config list errored — surface it with a retry instead of a
  // blank screen.
  if (scansError) {
    return (
      <div className="space-y-4">
        <div className="flex">{backLink}</div>
        <ErrorState
          compact
          title="Couldn't load this scan"
          error={scansErrorObj}
          onRetry={() => {
            void refetchScans()
          }}
        />
      </div>
    )
  }
  // A deleted or unknown scan: the not-found state with the way back, not a
  // grey sentence (#237).
  if (scansLoaded && !sc) {
    return (
      <EntityNotFound
        title="Scan not found"
        back={{ to: scansListPath, label: 'Back to Scans' }}
      />
    )
  }
  // Still loading — a skeleton, never a blank render.
  if (!sc) {
    return (
      <div className="space-y-4" aria-busy="true" aria-label="Loading scan">
        <div className="flex">{backLink}</div>
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-40 w-full" />
      </div>
    )
  }

  const dataSource = (dataSources as DataSource[]).find(ds => ds.id === sc.data_source_id) ?? null
  const runInfo = deriveScanRunInfo(jobs)
  const meta = STATUS_META[runInfo.status]
  const runActive = runInfo.status === 'running'
  // Replay re-collects metric points, so it needs both halves of monitoring.
  const canReplay = Boolean(sc.time_column && sc.interval)

  return (
    <PageContainer className="space-y-4">
      {unsaved.dialog}
      {/* The shared page header: the scan's name is the page's h1 under
          the "Govern · Scan" eyebrow, with its run status beside it. */}
      <PageHeader
        back={backLink}
        eyebrow="Govern · Scan"
        title={sc.name}
        titleAddon={
          <span className="inline-flex items-center gap-1.5">
            <Dot tone={meta.tone} pulse={runInfo.status === 'running'} size={6} />
            <span className="text-body-sm" style={{ color: `var(--${meta.tone === 'neutral' ? 'fg-subtle' : meta.tone})` }}>
              {SCAN_STATUS_LABEL[runInfo.status]}
            </span>
          </span>
        }
        description={
          <>
            {/* "Reads from", not "Ingests from": the causal note directly below
                says what a run DOES ("adds events to your tracking plan"), and
                two verbs for one act, one line apart, is the vocabulary drift
                this epic opened with. "Reads" is what concepts.md already uses
                for the warehouse side. */}
            {/* The connection's name leads to it for the one role that can
                manage connections (#248). */}
            <p className="m-0">
              Reads from{' '}
              {dataSource && isOwner ? (
                <Link
                  to={settingsPath(`/settings/data-sources/${dataSource.id}`)}
                  className="underline decoration-fg-tertiary/50 underline-offset-2 hover:decoration-current text-fg-secondary"
                >
                  {dataSource.name}
                </Link>
              ) : (
                <span className="text-fg-secondary">{dataSource?.name ?? 'Unknown source'}</span>
              )}
            </p>
            {/* One line under the header saying what this scan produces and what
                reads it. Above the tab strip, so it holds for both tabs.
                */}
            <div className="mt-1">
              <ScanCausalNote variant="config" config={sc} />
            </div>
          </>
        }
        actions={
          // Run is an editor's action, editing the configuration an owner's;
          // the Configuration tab itself stays open to read.
          (canRun || isOwner) && (
            <>
              {canRun && (
                <ScenarioCoachMark step="live-loop/run-scan">
                  {/* Off while a run is already queued or running: a second
                      click only earned the backend's 409. The label says why;
                      Stop is on the run's row (#247). */}
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={runMut.isPending || runActive}
                    onClick={() => runMut.mutate()}
                  >
                    <Play className="size-3.5" />
                    {runMut.isPending ? 'Starting…' : runActive ? 'Running…' : 'Run now'}
                  </Button>
                </ScenarioCoachMark>
              )}
              {/* Backfill sits next to Run now, as the other way to start a
                  run, not in the Danger zone beside Delete: it re-reads
                  history and deletes nothing (#247). Owner-only, and only
                  for a scan that collects metrics. */}
              {isOwner && canReplay && (
                <Button variant="outline" size="sm" onClick={() => setReplayOpen(true)}>
                  <RotateCcw className="size-3.5" aria-hidden="true" />
                  Replay a period…
                </Button>
              )}
              {isOwner && (
                // Always outline: filled on the Configuration tab it outranked
                // Save and only repeated the selected tab (#247).
                <Button
                  variant="outline"
                  size="sm"
                  // A phone has the Configuration tab a few pixels below; a second
                  // way there only costs the title its width.
                  className="hidden sm:inline-flex"
                  onClick={() => setTab('configuration')}
                >
                  <Sliders className="size-3.5" />
                  Edit
                </Button>
              )}
            </>
          )
        }
      />

      {replayOpen && (
        <ReplayDialog slug={slug} scanConfig={sc} open={replayOpen} onOpenChange={setReplayOpen} />
      )}

      {runMut.isError && (
        <p className="text-body text-danger">{getErrorMessage(runMut.error)}</p>
      )}

      <ScanBadges sc={sc} intervalLabel={INTERVAL_LABEL} nextRunAt={metricsSchedule?.nextRunAt ?? null} />

      {/* The shared Radix tabs: arrow-key roving and the
          tab/tabpanel wiring come from the primitive instead of a hand-rolled
          tablist. The value stays URL-controlled, and a switch still goes
          through the unsaved-changes guard. Manual activation: arrows move
          focus and Enter/Space selects. With selection on focus, the focus
          the guard's dialog hands back to the tab on "Keep editing" would
          select it again and re-open the dialog. */}
      <Tabs
        value={tab}
        onValueChange={next => setTab(next as DetailTab)}
        activationMode="manual"
        className="gap-4"
      >
        <TabsList aria-label="Scan detail sections">
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="configuration">Configuration</TabsTrigger>
        </TabsList>
        <TabsContent value="overview">
          <ScanDetail
            slug={slug}
            scanConfig={sc as ScanConfig}
            eventTypes={eventTypes}
            dataSource={dataSource}
            metricsSchedule={metricsSchedule}
          />
        </TabsContent>
        <TabsContent value="configuration">
          <ScanConfigurationTab
            key={configFormKey}
            slug={slug}
            scanConfig={sc as ScanConfig}
            onDeleted={() => {
              // Deleted: nothing left to lose.
              unsaved.release()
              goBack()
            }}
            onDirtyChange={setConfigDirty}
            onDiscard={() => setConfigFormKey(key => key + 1)}
          />
        </TabsContent>
      </Tabs>
    </PageContainer>
  )
}
