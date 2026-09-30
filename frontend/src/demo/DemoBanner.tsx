/**
 * Persistent demo banner (tripl-2su6.9).
 *
 * Shown across every surface of a demo project (mounted in the app Layout). It
 * makes the workspace's synthetic/local nature unmistakable, shows the recipe
 * version and runtime freshness, and exposes Reset / Delete behind a "Manage
 * demo" menu — both confirmed, both scoped to the demo endpoints, both offered
 * only to the demo's creator or a workspace owner. On delete it returns to the
 * Projects list.
 *
 * It also owns the one way back into the guided onboarding (tripl-imco): being
 * mounted on every demo surface, its "Tour & chapters" opens the tour, and the
 * tour offers the dismissed welcome panel back (DEMO-26). For the same reason
 * it hosts the tour's docked card, which follows the reader from surface to
 * surface (#251 JR-22).
 *
 * One row, not a stack (LIVE-9): the banner and the scenario strip used to be
 * two blocks above every page title, ~170 px on a desktop and ~250 px on a
 * phone. The strip now arrives as the `scenario` slot and sits in the middle of
 * the banner's own row — about 44 px, on one line from `lg` up, where the
 * actions give up their visible labels while a scenario shares the row (their
 * names stay). Below `lg` the whole bar folds into a pill that opens it.
 */

import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ChevronDown, Compass, FlaskConical, Info, MoreHorizontal, RotateCcw, Trash2 } from 'lucide-react'
import { ApiError } from '@/api/client'
import { projectsApi } from '@/api/projects'
import { clearProtectedQueries, useAuth } from '@/components/auth-context'
import { Chip } from '@/components/primitives/chip'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useBranchContext } from '@/hooks/useBranch'
import { useConfirm } from '@/hooks/useConfirm'
import { formatRelativeTime } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { canManageProject } from '@/lib/permissions'
import { projectKey, projectsKey } from '@/lib/queryKeys'
import { cn, getErrorMessage } from '@/lib/utils'
import type { Project } from '@/types'
import { ProductTour, TourDock } from './ProductTour'
import { ProvisioningPhaseList } from './ProvisioningPhaseList'
import { DemoDataBadge } from './capabilityBadges'
import { useDemoScenarioActions } from './demoScenarioContext'
import { forgetDemoLocalState } from './demoLocalState'
import { DEMO_PROVISION_ESTIMATE, DEMO_PROVISION_TIMEOUT_MS } from './provisioningPhases'
import { useEstimatedPhase } from './useEstimatedPhase'
import { setWelcomeDismissed } from './welcomeDismissal'
import { currentOrgSlug, projectPath, workspacePath } from '@/lib/navigation'

/** How long a reset may run before the page stops waiting for it (DEMO-4). */
const TIMEOUT_SECONDS = Math.round(DEMO_PROVISION_TIMEOUT_MS / 1000)

/** How often a reset the page stopped waiting for is checked on. */
const RESEED_POLL_MS = 5_000
/** How long it is checked on before Reset and Delete are offered again. */
const RESEED_WATCH_MS = 3 * 60_000

/**
 * Reset is a single blocking ~10 s POST that re-seeds the whole recipe in one
 * transaction (deliberately, for atomicity). A button that just says "Resetting…"
 * for ten seconds reads as a hang, so the wait gets the same estimated-phase
 * narration the create dialog uses (tripl-jfm3.75). It is not dismissable while
 * the request runs: unlike a create there is nothing to abandon — the demo is
 * already mid-replacement.
 */
function DemoResetProgressDialog() {
  // Mounted only while the reset is in flight, so every run narrates from the
  // first phase.
  const phaseIndex = useEstimatedPhase()
  return (
    <Dialog open>
      <DialogContent className="max-w-md" showCloseButton={false}>
        <DialogHeader>
          <DialogTitle>Re-seeding demo workspace</DialogTitle>
          <DialogDescription>
            Replacing this demo&apos;s content with a fresh synthetic dataset. This takes{' '}
            {DEMO_PROVISION_ESTIMATE}.
          </DialogDescription>
        </DialogHeader>
        <ProvisioningPhaseList
          phaseIndex={phaseIndex}
          slowMessage={`Taking longer than usual. The page stops waiting after ${TIMEOUT_SECONDS} seconds; the server may still finish.`}
        />
      </DialogContent>
    </Dialog>
  )
}

/**
 * A reset the browser stopped waiting for (DEMO-4). The request had no timeout,
 * so a stalled connection left the modal above on screen forever with a page
 * reload as the only way out. Now it is bounded like a create, and what it
 * says afterwards is only what is known: the server may still be re-seeding.
 */
function DemoResetStalledDialog({ onClose }: { onClose: () => void }) {
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Reset is still running</DialogTitle>
          <DialogDescription>
            The page stopped waiting after {TIMEOUT_SECONDS} seconds, but the server may still be
            re-seeding this demo. This page keeps checking and picks up the fresh demo when it
            lands; Reset and Delete stay off until then.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            Close
          </Button>
          <Button type="button" onClick={() => window.location.reload()}>
            Refresh now
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** The client maps an aborted fetch — here, our own timeout — to ApiError(408). */
function isTimeout(error: unknown): boolean {
  return error instanceof ApiError && error.status === 408
}

const DEMO_LIMITS: readonly string[] = [
  'Alert destinations record deliveries locally and are badged “Local · simulated” — nothing is sent to Slack, Jira or email.',
  'The synthetic warehouse supports common SQL; unsupported queries return a clear capability error instead of silently failing.',
  'Implementation-ticket creation and AI features stay off until you connect a real tracker / enable AI on the server.',
]

export function DemoBanner({
  project,
  resetTimeoutMs = DEMO_PROVISION_TIMEOUT_MS,
  reseedPollMs = RESEED_POLL_MS,
  reseedWatchMs = RESEED_WATCH_MS,
  scenario,
}: {
  project: Project
  /**
   * The scenario strip, placed inside this row rather than under it (LIVE-9).
   * A slot, not an import, so the strip stays its own lazy chunk.
   */
  scenario?: ReactNode
  /** Overridable so tests can exercise the timeout without a fake clock. */
  resetTimeoutMs?: number
  /** Overridable for the same reason: checking on a timed-out reset. */
  reseedPollMs?: number
  reseedWatchMs?: number
}) {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { setBranchId } = useBranchContext()
  const { confirm, dialog } = useConfirm()
  const { resetScenario } = useDemoScenarioActions()
  const [limitsOpen, setLimitsOpen] = useState(false)
  const [tourOpen, setTourOpen] = useState(false)
  // The phone pill's disclosure. Ignored from `lg` up, where the bar is always
  // open.
  const [expanded, setExpanded] = useState(false)
  const panelId = useId()

  // The backend's own pair of gates (EditorUserDep + `_require_demo_manager`):
  // a creator since demoted to viewer is refused, so they are not offered it.
  const canManage = canManageProject(user, project)

  // A re-seed rewrites every entity with a NEW id, so anything still holding
  // an old one now points at a deleted row (tripl-2su6.14):
  //   - the branch id persisted in localStorage would make every
  //     branch-aware query fail with "Branch not found", so drop it;
  //   - the banner is mounted on every surface, so a reset can be triggered
  //     from a metric/event/scan detail page whose URL carries a now-dead
  //     id — leave for the overview rather than render a 404.
  // Harmless when nothing was replaced: the branch falls back to main, and
  // the dropped rows are simply fetched again.
  const dropReseededIds = () => {
    setBranchId(null, { updateUrl: false })
    void navigate(projectPath(currentOrgSlug(), project.slug, '/overview'))
    // Every cached row describes a deleted entity now — drop them outright
    // rather than merely marking them stale. Except three that describe no
    // seeded entity (DEMO-3): the session (dropping it put the signed-in user
    // back to 'loading' and unmounted the app behind the route guard), and
    // the project list and this project, which the shell resolves the route
    // from — dropping those swapped the whole shell for "Loading project…".
    // The project survives a reset; its summary counts are refreshed instead.
    clearProtectedQueries(queryClient, (key) => key[0] === 'projects' || key[0] === 'project')
    void queryClient.invalidateQueries({ queryKey: projectsKey() })
    void queryClient.invalidateQueries({ queryKey: projectKey(project.slug) })
  }

  // The guidance is data too (tripl-imco): a re-seeded demo that came back
  // with every chapter still marked completed and the welcome panel still
  // dismissed was a fresh dataset with no way left into the coaching. Only
  // once a re-seed is KNOWN to have happened: the scenario's artifact ids
  // (scan job, metric) are still good if it rolled back.
  const resetGuidance = () => {
    resetScenario()
    setWelcomeDismissed(project.slug, false)
  }

  // A reset the page stopped waiting for is watched until the server shows
  // its outcome (DEMO-4): the project row itself is replaced by a re-seed, so
  // a new id under the same slug means it landed. Holds the id from before.
  const [watchedFromId, setWatchedFromId] = useState<string | null>(null)

  const resetMut = useMutation({
    meta: SILENT_ERROR_META,
    // The id the reset started from, for the watch above.
    onMutate: () => ({ fromId: project.id }),
    // Bounded like a create (DEMO-4): past the timeout the abort surfaces as
    // ApiError(408), and the stalled dialog takes over from the progress one.
    mutationFn: async () => {
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), resetTimeoutMs)
      try {
        return await projectsApi.resetDemo(project.slug, controller.signal)
      } finally {
        clearTimeout(timer)
      }
    },
    onError: (error, _variables, context) => {
      if (!isTimeout(error)) return
      // The server may still finish the re-seed — the likely case. Everything
      // that does not depend on knowing goes now, so neither "Close" nor
      // "Refresh now" leaves the page on cached rows or a stored branch id
      // for deleted entities; the rest waits for the watch below.
      dropReseededIds()
      setWatchedFromId(context?.fromId ?? project.id)
    },
    onSuccess: () => {
      dropReseededIds()
      resetGuidance()
    },
  })

  // What the watch does once it sees the re-seed land: the same as a reset
  // that answered in time, and the stalled dialog has nothing left to say.
  // Behind a ref so the polling effect does not restart on every render.
  const finishWatchRef = useRef<() => void>(() => {})
  useEffect(() => {
    finishWatchRef.current = () => {
      dropReseededIds()
      resetGuidance()
      if (resetMut.isError) resetMut.reset()
    }
  })

  useEffect(() => {
    if (watchedFromId === null) return
    let stopped = false
    const deadline = Date.now() + reseedWatchMs
    let timer: ReturnType<typeof setTimeout> | undefined
    const check = async () => {
      try {
        const current = await projectsApi.get(project.slug)
        if (stopped) return
        if (current.id !== watchedFromId) {
          stopped = true
          setWatchedFromId(null)
          finishWatchRef.current()
          return
        }
      } catch {
        // A failed check says nothing about the reset; keep watching.
      }
      if (stopped) return
      // Past the window the reset is taken as rolled back, and Reset and
      // Delete are offered again.
      if (Date.now() >= deadline) {
        setWatchedFromId(null)
        return
      }
      timer = setTimeout(() => void check(), reseedPollMs)
    }
    timer = setTimeout(() => void check(), reseedPollMs)
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [watchedFromId, project.slug, reseedPollMs, reseedWatchMs])

  const deleteMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => projectsApi.deleteDemo(project.slug),
    onSuccess: () => {
      // The project is gone; leave no branch selection behind pointing into it.
      setBranchId(null, { updateUrl: false })
      // Nor the tour position, chapter progress, welcome dismissal and hint
      // toggle stored under its random slug, which nothing would ever read
      // again (DEMO-17).
      forgetDemoLocalState(project.slug)
      void queryClient.invalidateQueries({ queryKey: projectsKey() })
      void navigate(workspacePath())
    },
  })

  // A timed-out reset may still be running on the server: a second one (or a
  // delete) started now would race it.
  const reseedRunning = resetMut.isPending || watchedFromId !== null
  const busy = reseedRunning || deleteMut.isPending
  // What the "Manage demo" trigger says while one of its actions runs.
  const busyLabel = reseedRunning ? 'Resetting…' : deleteMut.isPending ? 'Deleting…' : null

  // A failed reset or delete says so until the user does something else here
  // (DEMO-23): the message used to stay pinned under the banner through every
  // later action, and a reset failure went on captioning a delete that worked.
  //
  // Only a SETTLED failure is reset: `reset()` detaches the observer from a
  // mutation still in flight, which read as idle — Reset and Delete came back
  // enabled mid-reseed, and a later failure of it was shown nowhere.
  const clearMutationError = () => {
    if (resetMut.isError) resetMut.reset()
    if (deleteMut.isError) deleteMut.reset()
  }

  const handleReset = async () => {
    clearMutationError()
    const ok = await confirm({
      title: 'Reset demo workspace',
      message:
        'Re-seed this demo from scratch. All current events, metrics and alert rules in the demo are replaced with a fresh synthetic dataset. ' +
        `This runs in one go and takes ${DEMO_PROVISION_ESTIMATE}. It cannot be undone.`,
      confirmLabel: 'Reset demo',
      variant: 'primary',
    })
    if (ok) resetMut.mutate()
  }

  const handleDelete = async () => {
    clearMutationError()
    const ok = await confirm({
      title: 'Delete demo workspace',
      message: `Permanently delete “${project.name}” and its synthetic warehouse. You'll be returned to the projects list.`,
      confirmLabel: 'Delete demo',
      variant: 'danger',
    })
    if (ok) deleteMut.mutate()
  }

  // demo_seeded_at is floored to the hour — the runtime tick anchors its bucket
  // grid to it — so using it as a freshness stamp made a demo created at 10:59
  // read "refreshed 59m ago" the instant it appeared, and runtime ticks never
  // moved it. demo_last_tick_at is when the data was actually last advanced;
  // until the first tick there is nothing to claim, so say so (tripl-2su6.17).
  const freshnessLabel = project.demo_last_tick_at
    ? `updated ${formatRelativeTime(project.demo_last_tick_at)}`
    : 'freshly seeded'

  // A timed-out reset is told by its own dialog, not the one-line alert.
  const resetStalled = resetMut.isError && isTimeout(resetMut.error)
  const mutationError = (resetStalled ? null : resetMut.error) ?? deleteMut.error

  return (
    // `group/demo` lets the row ask whether the scenario slot rendered anything:
    // the strip decides that for itself, and the actions only need to give up
    // their labels when it did.
    // `data-demo-banner`: a docked coach card measures this to sit below the
    // banner rather than on its controls (#251 SH-5).
    <div className="group/demo mb-4" data-demo-banner="">
      {dialog}
      {resetMut.isPending && <DemoResetProgressDialog />}
      {resetStalled && <DemoResetStalledDialog onClose={() => resetMut.reset()} />}

      {/* The phone form (LIVE-9): one pill instead of the bar, so the page
          title is the first thing on the screen. A disclosure button, so a
          screen reader hears what it opens and whether it is open. */}
      <button
        type="button"
        onClick={() => setExpanded((open) => !open)}
        aria-expanded={expanded}
        aria-controls={panelId}
        // An aria-label, not a hidden span: a name is built from each
        // element's trimmed text, so a span's " workspace tools" came out as
        // "Demoworkspace tools". It starts with the visible word.
        aria-label="Demo workspace tools"
        className="inline-flex min-h-8 items-center gap-1.5 rounded-full border px-3 py-1 text-body-sm font-medium lg:hidden bg-warning-soft border-warning"
      >
        <FlaskConical className="h-3.5 w-3.5" aria-hidden="true" />
        Demo
        {/* A chapter is in progress behind the fold: say so without words. */}
        <span
          aria-hidden="true"
          className="hidden h-1.5 w-1.5 rounded-full group-has-[[data-demo-scenario]]/demo:inline-block bg-accent"
        />
        <ChevronDown
          className="h-3 w-3 transition-transform"
          style={{ transform: expanded ? 'rotate(180deg)' : 'none' }}
          aria-hidden="true"
        />
      </button>

      <div
        id={panelId}
        className={cn('rounded-lg border lg:mt-0 lg:block', expanded ? 'mt-2' : 'hidden', 'bg-warning-soft border-warning')}
      >
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-3 py-1.5 lg:min-h-11 lg:flex-nowrap">
          {/* Both groups wrap below `lg` (#251 SH-1): held to one line, the
              phone panel's content came to 462px in a 364px panel — the
              freshness cut, Reset half-shown, the owner's Delete off-screen. */}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 lg:shrink-0 lg:flex-nowrap">
            <DemoDataBadge />
            <span className={cn('shrink-0 text-body-sm font-medium', LABEL_WHEN_ROOMY)}>Demo workspace</span>
            {/* Details, not controls: the first thing to go when the row is
                shared, and still on the phone panel, which has room. */}
            {project.demo_recipe_version && (
              <span className={cn('inline-flex', DETAIL_WHEN_ROOMY)}>
                <Chip tone="neutral" size="xs" title="Demo recipe version">
                  recipe {project.demo_recipe_version}
                </Chip>
              </span>
            )}
            <span
              className={cn('inline-flex text-caption whitespace-nowrap', DETAIL_WHEN_ROOMY, 'text-fg-secondary')}
            >
              {freshnessLabel}
            </span>
          </div>

          {scenario}

          <div className="ml-auto flex flex-wrap items-center gap-1.5 lg:shrink-0 lg:flex-nowrap">
            <button
              type="button"
              onClick={() => {
                clearMutationError()
                setLimitsOpen((open) => !open)
              }}
              aria-expanded={limitsOpen}
              title="What’s simulated"
              className="flex items-center gap-1 rounded-sm px-2 py-1 text-caption font-medium transition-colors hover:bg-[var(--surface-hover)] text-fg-secondary"
            >
              <Info className="h-3.5 w-3.5" aria-hidden="true" />
              <BannerLabel>What’s simulated</BannerLabel>
              <ChevronDown
                className="h-3 w-3 transition-transform"
                style={{ transform: limitsOpen ? 'rotate(180deg)' : 'none' }}
                aria-hidden="true"
              />
            </button>

            {/* The way back into the guided onboarding (tripl-imco). It opens the
                tour and nothing else (DEMO-26): it used to restore the dismissed
                welcome panel on every click, so a user who had put the panel
                away on purpose got it back each time they wanted the tour. The
                tour offers the panel back as its own choice. Offered to
                everyone: restoring guidance is not managing the demo. */}
            <Button
              type="button"
              variant="outline"
              size="sm"
              title="Tour & chapters"
              onClick={() => {
                clearMutationError()
                setTourOpen(true)
              }}
            >
              <Compass className="h-3.5 w-3.5" aria-hidden="true" />
              <BannerLabel>Tour &amp; chapters</BannerLabel>
            </Button>

            {/* Reset and Delete sit behind one "Manage demo" menu (#238 JR-21,
                #251 SH-6): the banner is ahead of the page in tab order on every
                demo surface, and a keyboard user walked through two
                destructive buttons before reaching the page. Now it is one
                stop, and both still open a confirm. Only the demo's creator or
                a workspace owner is offered them. While a reset or delete runs
                the trigger says so, as the buttons used to. */}
            {canManage && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    title="Manage demo"
                    aria-label={busyLabel ? `Manage demo (${busyLabel})` : 'Manage demo'}
                  >
                    <MoreHorizontal className="h-3.5 w-3.5" aria-hidden="true" />
                    <BannerLabel>{busyLabel ?? 'Manage'}</BannerLabel>
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" sideOffset={6} className="w-44">
                  <DropdownMenuItem
                    className="text-body-sm"
                    disabled={busy}
                    onSelect={() => void handleReset()}
                  >
                    <RotateCcw aria-hidden="true" />
                    {reseedRunning ? 'Resetting…' : 'Reset…'}
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  {/* Red, the hierarchy's destructive-in-a-list look; the solid
                      red is kept for the confirm it opens (DS-20). */}
                  <DropdownMenuItem
                    variant="destructive"
                    className="text-body-sm"
                    disabled={busy}
                    onSelect={() => void handleDelete()}
                  >
                    <Trash2 aria-hidden="true" />
                    {deleteMut.isPending ? 'Deleting…' : 'Delete…'}
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            )}
          </div>
        </div>

        {limitsOpen && (
          <ul
            className="space-y-1 border-t px-3 py-2 text-caption leading-[1.45] border-warning text-fg-secondary"
          >
            {DEMO_LIMITS.map((limit) => (
              <li key={limit} className="flex gap-1.5">
                <span aria-hidden="true">·</span>
                <span>{limit}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Outside the folding panel: a failure is shown with the pill closed
          too. */}
      {mutationError && (
        <p className="mt-1.5 px-1 text-caption text-danger" role="alert">
          {getErrorMessage(mutationError)}
        </p>
      )}

      {/* Mounted only while open so it reads its persisted step position fresh:
          the Overview hosts a second ProductTour, and a permanently-mounted one
          here would keep whatever index it captured at first render. */}
      {tourOpen && <ProductTour slug={project.slug} open onOpenChange={setTourOpen} />}

      {/* The tour, docked on the surface its "Open X" led to (#251 JR-22):
          mounted here because the banner is on every demo surface. Portalled,
          so it takes no room in the row. */}
      <TourDock
        slug={project.slug}
        onOpenTour={() => {
          clearMutationError()
          setTourOpen(true)
        }}
      />
    </div>
  )
}

/**
 * Hidden from `lg` to `2xl` while the scenario strip shares the row: the one
 * width band where a single line cannot hold every label (LIVE-9). Everywhere
 * else — the phone panel, which wraps, a wide screen, or a row with no
 * scenario in it — there is room, and it shows. `sr-only`, so a name it
 * carries stays a name.
 */
const LABEL_WHEN_ROOMY =
  'lg:group-has-[[data-demo-scenario]]/demo:sr-only 2xl:group-has-[[data-demo-scenario]]/demo:not-sr-only'

/** The same band for details that name nothing: taken out of the row there. */
const DETAIL_WHEN_ROOMY =
  'lg:group-has-[[data-demo-scenario]]/demo:hidden 2xl:group-has-[[data-demo-scenario]]/demo:inline-flex'

/**
 * A button label that gives way to its icon where the row is shared (LIVE-9),
 * while the button keeps its accessible name; the `title` beside it names the
 * icon for a pointer.
 */
function BannerLabel({ children }: { children: ReactNode }) {
  return <span className={LABEL_WHEN_ROOMY}>{children}</span>
}
