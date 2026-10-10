import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { dataSourcesApi } from '@/api/dataSources'
import { projectsApi } from '@/api/projects'
import { useAuth } from '@/components/auth-context'
import { MiniStat, MiniStatStrip } from '@/components/primitives/mini-stat'
import { PageContainer } from '@/components/primitives/page-container'
import { PageHeader } from '@/components/primitives/page-header'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { StatValueSkeleton } from '@/components/states'
import { countRealSources } from '@/components/onboarding-utils'
import { WorkspaceWelcome } from '@/components/workspace-welcome'
import { DemoProvisioningDialog } from '@/demo/DemoProvisioningDialog'
import {
  demoGenerationBlockedReason,
  demoGenerationWarning,
  ownedDemoCount,
} from '@/demo/demoGenerationGuard'
import { useAutoStartDemo } from '@/demo/autoStartDemo'
import { useDemoProvisioning } from '@/demo/useDemoProvisioning'
import { forgetDemoLocalState, sweepOrphanedDemoLocalState } from '@/demo/demoLocalState'
import { useConfirm } from '@/hooks/useConfirm'
import { useOrgDefaultProjectRole } from '@/hooks/useOrgDefaultProjectRole'
import { deleteProjectConfirmation } from '@/lib/projectDeletion'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { formatPlanCoverage } from '@/lib/coverage'
import { formatNumber } from '@/lib/format'
import { pluralize } from '@/lib/plural'
import type { Project } from '@/types'
import {
  Activity,
  AlertTriangle,
  BellRing,
  Plus,
  Sparkles,
} from 'lucide-react'
import { dataSourcesKey, projectKey, projectsKey, projectsQueryOptions } from '@/lib/queryKeys'
import { useCanWrite, useIsOrgOwner, useIsOwner } from '@/lib/permissions'
import { usePublicDemo } from '@/lib/deploymentMode'
import { AttentionStat, ProjectCard } from './ProjectsPageCards'
import { CreateProjectDialog } from './ProjectsPageCreateDialog'
import { reviewQueueHint, summarizePortfolio } from './ProjectsPagePortfolio'

export default function MainPage() {
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const publicDemo = usePublicDemo()
  const ownsWorkspace = useIsOrgOwner()
  // Both read the role in the organization the app acts in, not the default
  // organization's that `user.role` carries.
  const isOwner = useIsOwner()
  const canCreateProject = useCanWrite()
  // `?new=1` opens the create dialog on arrival: the sidebar project
  // switcher's "New project" item lands here (#238).
  const [searchParams, setSearchParams] = useSearchParams()
  const [showForm, setShowFormState] = useState(() => searchParams.get('new') === '1')
  const setShowForm = (open: boolean) => {
    setShowFormState(open)
    if (!open && searchParams.has('new')) {
      const next = new URLSearchParams(searchParams)
      next.delete('new')
      setSearchParams(next, { replace: true })
    }
  }
  // The slug whose delete succeeded but whose list refetch has not landed yet:
  // its card is still listed and must keep saying "Deleting…".
  const [settlingSlug, setSettlingSlug] = useState<string | null>(null)
  const { confirm, dialog } = useConfirm()

  const projectsQuery = useQuery(projectsQueryOptions())
  const dataSourcesQuery = useQuery({
    queryKey: dataSourcesKey(),
    queryFn: dataSourcesApi.list,
  })

  // One pass over the list, memoised: the sort and the roll-up used to run on
  // every render of the page.
  const {
    projects,
    totals: portfolio,
    projectsWithScans,
    projectsWithSignals,
    projectsWithLatestScanJob,
    projectsWithRunningScan,
    projectsWithFailedScan,
    failingScanConfigCount,
  } = useMemo(() => summarizePortfolio(projectsQuery.data ?? []), [projectsQuery.data])
  const dataSourceCount = dataSourcesQuery.data?.length ?? 0
  // What the getting-started checklist counts: a demo's synthetic warehouse is
  // not a connected source (the cards' setup line reads the same steps).
  const realSourceCount = countRealSources(dataSourcesQuery.data ?? [])

  // The workspace list is the one place that knows every project this browser
  // can still reach, so it clears demo state left behind by projects that are
  // gone — deleted elsewhere, or by someone else.
  const loadedProjects = projectsQuery.data
  useEffect(() => {
    if (loadedProjects) sweepOrphanedDemoLocalState(loadedProjects.map((project) => project.slug))
  }, [loadedProjects])

  const coverageDisplay = formatPlanCoverage(
    portfolio.implementedEventCount,
    portfolio.activeEventCount,
  )

  // The delete dialog shows its own failure, and the card shows its own
  // pending state, so neither is left to a toast or to nothing at all.
  //
  // The confirm dialog runs the delete and closes once the DELETE succeeds; the
  // list refetch after it is tracked by `settlingSlug` rather than by keeping
  // the mutation pending, so the dialog does not wait on it.
  const deleteMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (projectSlug: string) => projectsApi.del(projectSlug),
    onSuccess: (_data, projectSlug) => {
      queryClient.removeQueries({ queryKey: projectKey(projectSlug) })
      // A demo's tour, scenario and welcome state is keyed by slug and would
      // otherwise outlive it; harmless for any other project.
      forgetDemoLocalState(projectSlug)
      setSettlingSlug(projectSlug)
      void queryClient
        .invalidateQueries({ queryKey: projectsKey() })
        .finally(() => setSettlingSlug(current => (current === projectSlug ? null : current)))
    },
  })
  const deleteBusy = deleteMut.isPending || settlingSlug !== null

  // Demo provisioning: a blocking create with staged progress, a duplicate-click
  // guard, and success routing to the new demo's Overview welcome (not Events).
  const provisioning = useDemoProvisioning()
  const isProvisioningDemo =
    provisioning.status === 'provisioning' || provisioning.status === 'cancelling'

  // Every demo is an extra synthetic project inside the real roll-ups, so the
  // second one asks first and points at Reset instead.
  //
  // At the cap the button is disabled with the reason beside it: the
  // old confirm there had two buttons that both did nothing.
  const ownedDemos = ownedDemoCount(projects, user?.id)
  const demoBlockedReason = demoGenerationBlockedReason(ownedDemos)
  const handleGenerateDemo = async () => {
    if (demoBlockedReason) return
    const warning = demoGenerationWarning(ownedDemos)
    if (warning) {
      const ok = await confirm({
        title: warning.title,
        message: warning.message,
        confirmLabel: warning.confirmLabel,
        variant: 'primary',
      })
      if (!ok) return
    }
    provisioning.start()
  }

  const handleDelete = (project: Project) => {
    // One delete at a time: the first card keeps "Deleting…" through the list
    // refetch, after the dialog has closed, and a second delete resetting the
    // mutation then wiped it while that card was still listed. Every card's
    // menu is shut meanwhile (deleteLocked below); this is the backstop.
    if (deleteBusy) return
    // A failure from an earlier attempt belongs to that attempt, not this one.
    deleteMut.reset()
    void confirm(deleteProjectConfirmation(project, () => deleteMut.mutateAsync(project.slug)))
  }

  const dataSourceValue = dataSourcesQuery.isError
    ? 'Unavailable'
    : dataSourcesQuery.isLoading
      // A placeholder bar, not "..." (#237): the strip never claims a
      // figure it has not loaded.
      ? <StatValueSkeleton />
      : String(dataSourceCount)
  // A public demo runs on demo projects only: generated, never blank.
  const canCreateBlank = canCreateProject && !publicDemo
  const canDeleteProject = isOwner
  // Loaded-and-empty workspace: the welcome hero replaces the header CTA pair,
  // the all-zero stat band, and the old EmptyState until the first project
  // exists. Loading and error states render exactly as before.
  const isEmptyWorkspace =
    !projectsQuery.isLoading && !projectsQuery.isError && projects.length === 0
  // The list holds only the projects the viewer may open. A member, where the
  // organization's default project access is none, sees one only once added to
  // it: their empty list says nothing about the team's. An owner or admin sees
  // every project, and so does everyone where the default grants access. Only
  // an empty list asks.
  const defaultProjectRole = useOrgDefaultProjectRole({
    enabled: isEmptyWorkspace && !isOwner && !publicDemo,
  })
  const memberSeesOnlyAddedProjects =
    !isOwner && !publicDemo && defaultProjectRole === 'none'
  // A public demo's newcomer signed in for a demo of their own: in the empty
  // workspace that is theirs, the create starts by itself, once
  // (autoStartDemo.ts). Never on a list still being re-read — an empty one
  // cached before an invitation was accepted is not an empty workspace.
  useAutoStartDemo(
    publicDemo && ownsWorkspace && isEmptyWorkspace && !projectsQuery.isFetching
      && provisioning.status === 'idle',
    user?.id,
    provisioning.start,
  )

  return (
    <PageContainer>
      {dialog}

      <DemoProvisioningDialog
        status={provisioning.status}
        phaseIndex={provisioning.phaseIndex}
        error={provisioning.error}
        timedOut={provisioning.timedOut}
        cancelOutcome={provisioning.cancelOutcome}
        onRetry={provisioning.retry}
        onCancel={provisioning.cancel}
        onClose={provisioning.reset}
      />

      {/* Title + create actions. Stats moved into the single stat row below so
          the header no longer doubles as a stat strip. The shared page
          header: the eyebrow is the sidebar group, as on every page. */}
      <PageHeader
        eyebrow="Workspace"
        // One name for this page wherever it is named — the sidebar, the
        // top bar, the tab and the palette all say "All projects".
        title="All projects"
        // Not over the welcome hero: it describes a portfolio that does not
        // exist yet, and the hero says what tripl is right under it.
        description={
          isEmptyWorkspace
            ? undefined
            : 'See which tracking plans are filling out, which projects still need review, and how much scan and alerting coverage exists across the workspace.'
        }
        actions={
          canCreateProject && !isEmptyWorkspace ? (
            <div className="flex flex-col items-end gap-1">
              <div className="flex flex-wrap items-center justify-end gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => void handleGenerateDemo()}
                  disabled={isProvisioningDemo || demoBlockedReason !== null}
                  aria-describedby={demoBlockedReason ? 'demo-generation-blocked' : undefined}
                >
                  <Sparkles />
                  {isProvisioningDemo ? 'Generating…' : 'Generate demo project'}
                </Button>
                {canCreateBlank && (
                  <Button size="sm" onClick={() => setShowForm(true)}>
                    <Plus />
                    New project
                  </Button>
                )}
              </div>
              {demoBlockedReason && (
                <p
                  id="demo-generation-blocked"
                  className="m-0 max-w-[320px] text-right text-caption text-fg-tertiary"
                >
                  {demoBlockedReason}
                </p>
              )}
            </div>
          ) : undefined
        }
      />

      {showForm && canCreateBlank && (
        <CreateProjectDialog
          onClose={() => setShowForm(false)}
          existingSlugs={projects.map((project) => project.slug)}
        />
      )}

      {projectsQuery.isLoading && <ProjectsPageSkeleton />}

      {/* No error card of its own: this page only renders inside Layout, whose
          "Backend is unavailable" card already reports a failed project list,
          with its own retry. A second card here said the same thing twice.
          The empty-workspace hero stays out of it, below. */}

      {isEmptyWorkspace && (
        <WorkspaceWelcome
          memberSeesOnlyAddedProjects={memberSeesOnlyAddedProjects}
          isProvisioningDemo={isProvisioningDemo}
          onGenerateDemo={() => void handleGenerateDemo()}
          onCreateProject={canCreateBlank ? () => setShowForm(true) : undefined}
        />
      )}

      {!projectsQuery.isLoading && !projectsQuery.isError && projects.length > 0 && (
        <>
          {/* One consolidated panel, two tiers: calm STATE metrics on top, a
              divider, then the attention-worthy ACTION-NEEDED cards under them.
              Projects/Coverage live here exactly once — no duplicated tiers,
              and no metric is shown twice on the page.

              The tiers used to sit side by side, which left the three action
              cards ~365px of a 904px panel at 1512px — under the 170px min each
              one asks for, so they wrapped 2+1 and the orphaned Signals card
              stretched to double width to carry the shortest sentence on the
              strip. Stacked, all three get a real third of the panel and the
              calm row stops being centred in a 200px-tall box with ~160px of
              void around it. */}
          <div
            className="flex flex-col gap-3 rounded-card border p-3 bg-surface border-border"
          >
            <MiniStatStrip phoneGrid className="px-1">
              <MiniStat label="Projects" value={String(portfolio.projectCount)} />
              <MiniStat
                label="Coverage"
                value={coverageDisplay}
                // The fraction carries its unit: "2993/5291" beside a percentage
                // left the reader guessing what was being counted.
                delta={
                  portfolio.activeEventCount > 0
                    ? `${portfolio.implementedEventCount}/${portfolio.activeEventCount} events`
                    : undefined
                }
                // The fraction is a plain "implemented of active" readout, not a
                // health signal — keep it neutral so 77% never reads as an error.
                tone="neutral"
              />
              {/* Every source the Data sources page lists, so the two agree; a
                  demo's warehouse is said to be synthetic, since the getting-
                  started step on the cards below counts only real ones. */}
              <MiniStat
                label="Data sources"
                value={dataSourceValue}
                delta={
                  dataSourcesQuery.isSuccess && dataSourceCount > realSourceCount
                    ? `${formatNumber(dataSourceCount - realSourceCount)} synthetic`
                    : undefined
                }
                tone={dataSourcesQuery.isError ? 'danger' : 'neutral'}
              />
              {/* "Automation 8 · 3 covered" was the only tile on the landing page
                  that never said what it counted, and the page subtitle above it
                  ("scan and alerting coverage") invited reading the 8 as scans +
                  alert rules — it is scans alone. */}
              <MiniStat
                label="Scans"
                value={String(portfolio.scanCount)}
                delta={
                  portfolio.scanCount > 0
                    ? `in ${projectsWithScans} of ${pluralize(
                        portfolio.projectCount,
                        '1 project',
                        `${portfolio.projectCount} projects`,
                      )}`
                    : undefined
                }
              />
            </MiniStatStrip>

            <div className="h-px w-full bg-border" />

            {/* A fixed three-column grid, not wrapping flex: these three always
                mean "one row of the same kind of thing", and equal columns are
                what stops the last one reading as a different class of card. */}
            <div className="grid items-stretch gap-2 sm:grid-cols-3">
              {/* The number is a workspace total, but every review queue lives in
                  one project and there is no workspace-wide queue to open. It used
                  to link to whichever project was edited last — "2292 events across
                  3 projects" opening the 796 of them in one — so it names where the
                  events are instead, and each project card's own in-review chip
                  is the link into that project's queue. */}
              <AttentionStat
                icon={BellRing}
                label="In review"
                value={String(portfolio.reviewPendingEventCount)}
                unit={pluralize(portfolio.reviewPendingEventCount, 'event', 'events')}
                hint={reviewQueueHint(projects)}
                tone={portfolio.reviewPendingEventCount > 0 ? 'warning' : 'success'}
              />
              <AttentionStat
                icon={AlertTriangle}
                label="Failed runs"
                // "0 projects" under "Failed runs" read backwards.
                value={projectsWithFailedScan > 0 ? String(projectsWithFailedScan) : 'None'}
                unit={
                  projectsWithFailedScan > 0
                    ? pluralize(projectsWithFailedScan, 'project', 'projects')
                    : undefined
                }
                hint={
                  projectsWithFailedScan > 0
                    ? `${pluralize(
                        failingScanConfigCount,
                        '1 scan failing',
                        `${failingScanConfigCount} scans failing`,
                      )} across ${pluralize(
                        projectsWithFailedScan,
                        '1 project',
                        `${projectsWithFailedScan} projects`,
                      )}`
                    : projectsWithRunningScan > 0
                      ? pluralize(
                          projectsWithRunningScan,
                          '1 project is currently running a scan',
                          `${projectsWithRunningScan} projects are currently running scans`,
                        )
                      : projectsWithLatestScanJob > 0
                        ? 'Latest scan runs are healthy'
                        : 'No project has run a scan yet'
                }
                tone={projectsWithFailedScan > 0 ? 'danger' : projectsWithRunningScan > 0 ? 'info' : 'success'}
              />
              <AttentionStat
                icon={Activity}
                label="Signals"
                value={String(portfolio.monitoringSignalCount)}
                unit={pluralize(portfolio.monitoringSignalCount, 'signal', 'signals')}
                hint={
                  portfolio.monitoringSignalCount > 0
                    ? `${pluralize(projectsWithSignals, '1 project currently has', `${projectsWithSignals} projects currently have`)} open signals`
                    : 'Monitoring is quiet across the workspace'
                }
                tone={portfolio.monitoringSignalCount > 0 ? 'danger' : 'success'}
                pulse={portfolio.monitoringSignalCount > 0}
              />
            </div>
          </div>

          <section className="space-y-3">
            {/* No count chip: the Projects tile above already says how many. */}
            <div>
              <h2 className="text-heading font-semibold tracking-tight">Project portfolio</h2>
              <p className="text-caption text-fg-tertiary">
                Recently updated projects with planning, review, scan, and alerting coverage.
              </p>
            </div>

            <div className="grid gap-3">
              {projects.map((project) => (
                <ProjectCard
                  key={project.id}
                  project={project}
                  canDelete={canDeleteProject}
                  isOwner={isOwner}
                  canSetUp={canCreateProject}
                  sourceCount={realSourceCount}
                  isDeleting={
                    settlingSlug === project.slug ||
                    (deleteMut.isPending && deleteMut.variables === project.slug)
                  }
                  deleteLocked={deleteBusy}
                  onDelete={() => handleDelete(project)}
                />
              ))}
            </div>
          </section>
        </>
      )}
    </PageContainer>
  )
}

function ProjectsPageSkeleton() {
  return (
    <div className="space-y-4">
      <Skeleton className="h-[72px] rounded-lg" />
      <div className="grid gap-3">
        {[0, 1, 2, 3].map((index) => (
          <Skeleton key={index} className="h-28 rounded-lg" />
        ))}
      </div>
    </div>
  )
}
