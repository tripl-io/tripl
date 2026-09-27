import { Suspense, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Activity, Bell, ChevronRight, GitBranch, Loader2, Menu, Search } from 'lucide-react'
import { planBranchesApi } from '@/api/planBranches'
import { useBranchContext } from '@/hooks/useBranch'
import { requestPageLeave } from '@/hooks/useUnsavedChangesGuard'
import { useExpandedSignals } from '@/hooks/useExpandedSignals'
import { useUnreadNotificationCount } from '@/hooks/useNotifications'
import { lazyWithReload } from '@/lib/lazyWithReload'
import { commandPaletteShortcutLabel } from '@/lib/platform'
import {
  COMMAND_PALETTE_TRIGGER_ATTR,
  preloadCommandPalette,
  useCommandPalette,
} from '@/components/command-palette-context'
import { ErrorBoundary } from '@/components/error-boundary'
import { CountBadge } from '@/components/primitives/count-badge'
import {
  loadNotificationsPanel,
  preloadNotificationsPanel,
  useTopbarDeliveries,
} from '@/components/shell/notifications-queries'
import type { Crumb } from '@/components/shell/crumbs'
import { SectionSkeleton } from '@/components/states/skeletons'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { planBranchesKey, projectsQueryOptions } from '@/lib/queryKeys'
// The branch pages' own status words, so the strip cannot drift from them.
import { STATUS_LABEL } from '@/lib/branchStatus'

type TopBarProps = {
  title: string
  /** The trail above the title; a crumb with `to` is a link (MO-13). */
  crumbs?: Crumb[]
  projectSlug?: string
  /**
   * The project's display name. Below `sm` the crumbs are hidden, so it rides
   * as a muted line under the title: with three look-alike projects a phone
   * user could not tell which one they were in (#238 SH-14).
   */
  projectName?: string
  activityOpen?: boolean
  onToggleActivity?: () => void
  onOpenMobileNav?: () => void
  /** Whether the navigation drawer is open, for the hamburger's aria-expanded. */
  mobileNavOpen?: boolean
  /** Id of the navigation drawer, for the hamburger's aria-controls. */
  mobileNavId?: string
  right?: ReactNode
}

export function TopBar({
  title,
  crumbs = [],
  projectSlug,
  projectName,
  activityOpen,
  onToggleActivity,
  onOpenMobileNav,
  mobileNavOpen = false,
  mobileNavId,
  right,
}: TopBarProps) {
  const palette = useCommandPalette()
  return (
    // The page's banner landmark, outside <main> (SHELL-47). 48px on phones so
    // its controls can be 36-40px touch targets; 44px from sm up
    // (SH-16 / ST-12 / AU-39 / AL-41).
    <header
      className="flex h-12 flex-shrink-0 sm:h-11 items-center gap-3 border-b px-3 sm:px-4 bg-background border-border"
    >
      {onOpenMobileNav && (
        <button
          type="button"
          aria-label="Open navigation"
          aria-expanded={mobileNavOpen}
          aria-controls={mobileNavId}
          onClick={onOpenMobileNav}
          className="-ml-1 flex h-10 w-10 shrink-0 items-center justify-center rounded-md sm:h-8 sm:w-8 transition-colors hover:bg-[var(--surface-active)] lg:hidden text-fg-secondary"
        >
          <Menu className="size-4" aria-hidden="true" />
        </button>
      )}
      <div className="flex min-w-0 items-center gap-1.5 text-body-sm">
        {crumbs.map((crumb, i) => (
          <div key={`${crumb.label}-${i}`} className="hidden min-w-0 items-center gap-1.5 sm:flex">
            {crumb.to ? (
              <Link
                to={crumb.to}
                className="truncate rounded-sm no-underline underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] text-fg-secondary"
              >
                {crumb.label}
              </Link>
            ) : (
              <span className="truncate text-fg-secondary">{crumb.label}</span>
            )}
            <ChevronRight className="size-3 text-fg-tertiary" aria-hidden="true" />
          </div>
        ))}
        <div className="flex min-w-0 flex-col">
          <span className="truncate font-semibold text-fg">
            {title}
          </span>
          {projectName && (
            <span
              data-testid="topbar-project"
              className="truncate text-caption sm:hidden text-fg-tertiary"
            >
              {projectName}
            </span>
          )}
        </div>
      </div>
      <div className="flex-1" />
      <div className="flex items-center gap-1.5">
        {right}
        <NotificationsMenu projectSlug={projectSlug} />
        <button
          type="button"
          aria-label="Command palette"
          title={`Search or jump — ${commandPaletteShortcutLabel()}`}
          {...{ [COMMAND_PALETTE_TRIGGER_ATTR]: '' }}
          onClick={() => palette.setOpen(true)}
          onPointerEnter={preloadCommandPalette}
          onFocus={preloadCommandPalette}
          // One search entry point per viewport (#238 SH-21): from lg the
          // pinned sidebar carries "Search or jump… Ctrl K", and a second
          // trigger with the same shortcut beside it was noise. Below lg the
          // sidebar is a drawer, so the magnifier here is the way in.
          className="flex h-9 w-9 items-center justify-center rounded-md transition-colors hover:bg-[var(--surface-active)] sm:h-8 sm:w-8 lg:hidden"
          style={{ color: 'var(--fg-muted)' }}
        >
          <Search className="size-4" aria-hidden="true" />
        </button>
        {onToggleActivity && (
          <>
            <div className="mx-1 h-4 w-px bg-border" />
            <button
              type="button"
              onClick={onToggleActivity}
              // One name for the feed everywhere (#238 SH-8): the toggle said
              // "Now" and opened a rail titled "Recent activity".
              aria-label="Toggle activity feed"
              aria-pressed={activityOpen}
              className="flex h-9 items-center gap-1.5 rounded-md px-2 text-body-sm font-medium transition-colors sm:h-8"
              style={{
                background: activityOpen ? 'var(--surface)' : 'transparent',
                color: activityOpen ? 'var(--fg)' : 'var(--fg-muted)',
                border: activityOpen ? '1px solid var(--border)' : '1px solid transparent',
              }}
            >
              <Activity className="size-4" aria-hidden="true" />
              <span className="hidden sm:inline">Activity</span>
            </button>
          </>
        )}
      </div>
    </header>
  )
}

/**
 * The shell's "you are on a branch" strip (#243 PL-1 / SH-11), under the top
 * bar whenever the pages read a working branch. The only cue used to be the
 * sidebar pill, which sits in the drawer on phones and is a 6px dot on the
 * collapsed rail, so a PM could edit the plan believing it was main, or the
 * reverse. One line at every width: the branch, its status, "not live until
 * merged", and the two ways out.
 */
export function BranchStrip({ slug }: { slug: string | undefined }) {
  const { branchId, setBranchId } = useBranchContext()
  const branchesQuery = useQuery({
    // The switcher's key: the list is already cached, so no second request.
    queryKey: planBranchesKey(slug),
    queryFn: () => planBranchesApi.list(slug!),
    enabled: !!slug && !!branchId,
    meta: SILENT_ERROR_META,
  })
  if (!slug || !branchId) return null
  const branch = branchesQuery.data?.items.find((b) => b.id === branchId)
  // An id that names main, or a branch the settled list no longer has, is not
  // "working on a branch".
  if (branch?.kind === 'main') return null
  if (!branch && !branchesQuery.isPending) return null
  return (
    <div
      role="region"
      aria-label="Plan branch"
      data-testid="branch-strip"
      className="flex min-h-8 flex-shrink-0 items-center gap-2 border-b px-3 py-1 text-caption sm:px-4 bg-info-soft border-border text-fg-secondary"
    >
      <GitBranch className="size-3.5 shrink-0 text-info" aria-hidden="true" />
      <span className="min-w-0 truncate">
        Working on{' '}
        <strong className="font-semibold text-fg" title={branch?.name}>
          {branch?.name ?? 'a plan branch'}
        </strong>
        {branch && (
          <span className="hidden md:inline">
            {' · '}
            {STATUS_LABEL[branch.status]} · changes are not live until merged
          </span>
        )}
      </span>
      <div className="flex-1" />
      <Link
        to={`/p/${slug}/branches/${branchId}`}
        className="hidden shrink-0 font-medium underline-offset-2 hover:underline sm:inline text-fg"
      >
        Review changes
      </Link>
      <button
        type="button"
        // Switching swaps the data under the page; ask the unsaved-changes
        // guard first, as the sidebar switcher does.
        onClick={() => requestPageLeave(() => setBranchId(null))}
        className="shrink-0 rounded-sm font-medium underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] text-fg"
      >
        Back to main
      </button>
    </div>
  )
}

/** "Alerts — 1 open incident, 3 unread notifications": both badges' counts. */
function bellLabel(openIncidentCount: number, unreadCount: number): string {
  const parts = [
    openIncidentCount > 0
      ? `${openIncidentCount} open ${openIncidentCount === 1 ? 'incident' : 'incidents'}`
      : null,
    unreadCount > 0
      ? `${unreadCount} unread ${unreadCount === 1 ? 'notification' : 'notifications'}`
      : null,
  ].filter((part): part is string => part !== null)
  return parts.length > 0 ? `Alerts — ${parts.join(', ')}` : 'Alerts'
}

// The popover body loads on first open (or on hover/focus of the bell): the
// entry chunk carries only the bell and its badge (i9mt.19).
const NotificationsPanel = lazyWithReload(loadNotificationsPanel)

function NotificationsMenu({ projectSlug }: { projectSlug?: string }) {
  const [open, setOpen] = useState(false)
  // True while the panel's retry confirmation is up; see onInteractOutside.
  const [confirming, setConfirming] = useState(false)
  // The reader's own unread notifications (#259), polled every minute and on
  // focus. A failure reads as none: the bell still shows the project's alerts.
  const unreadCount = useUnreadNotificationCount().data ?? 0
  // The tab the popover opens on, chosen as it opens: the reader's own news
  // when there is some, else the signals it has always shown.
  const [initialTab, setInitialTab] = useState<'notifications' | 'signals'>('signals')
  const onOpenChange = (next: boolean) => {
    if (next) setInitialTab(unreadCount > 0 ? 'notifications' : 'signals')
    setOpen(next)
  }
  // Expanded, then gated on the shared Significant threshold — the same set the
  // sidebar badge and the Overview headline report. The collapsed variant this
  // used to call queries only project_total/event_type, so a project whose
  // anomalies are all event-scope (prod acme-ios: 150 of them) left the bell
  // completely clean while every other surface showed 30 (tripl-jfm3.89). The
  // request is now shared with Overview and Anomalies under one key
  // (tripl-jfm3.119) — Overview renders this bar, so it used to fetch twice.
  // The panel reads both lists under the same keys.
  const signalsQuery = useExpandedSignals(projectSlug)
  const deliveriesQuery = useTopbarDeliveries(projectSlug)
  // The badge counts OPEN INCIDENTS, off the project summaries the sidebar's
  // Alerting badge reads — the shell already holds this list, so it costs no
  // request. Counting signals here put "3" on the bell beside "Alerting 1" in
  // the sidebar for the same project (AL-40 / SH-17). On a workspace route it
  // is every project's open incidents, which the panel then lists per project.
  const projectsQuery = useQuery(projectsQueryOptions())
  const projects = projectsQuery.data
  const openIncidentCount = projectSlug
    ? projects?.find(project => project.slug === projectSlug)?.summary.open_incident_count ?? 0
    : (projects ?? []).reduce((total, project) => total + (project.summary?.open_incident_count ?? 0), 0)

  // First load only. `isFetching` swapped the bell for a spinner on every
  // stream invalidation and poll, so with a live stream the most visible
  // corner of the app flickered constantly (SHELL-39). A background refresh
  // shows as a small dot instead.
  const isLoading = signalsQuery.isPending || deliveriesQuery.isPending
  const isRefreshing = !isLoading && (signalsQuery.isFetching || deliveriesQuery.isFetching)

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={bellLabel(openIncidentCount, unreadCount)}
          onPointerEnter={preloadNotificationsPanel}
          onFocus={preloadNotificationsPanel}
          className="relative flex h-9 w-9 items-center justify-center rounded-md transition-colors hover:bg-[var(--surface-active)] sm:h-8 sm:w-8"
          style={{ color: openIncidentCount > 0 || unreadCount > 0 ? 'var(--fg)' : 'var(--fg-muted)' }}
        >
          {isLoading && projectSlug ? (
            <Loader2
              className="h-4 w-4 animate-spin"
              aria-hidden="true"
              data-testid="notifications-loading"
            />
          ) : (
            <Bell className="h-4 w-4" aria-hidden="true" />
          )}
          {isRefreshing && projectSlug && openIncidentCount === 0 && unreadCount === 0 && (
            <span
              aria-hidden="true"
              data-testid="notifications-refreshing"
              className="absolute right-1.5 top-1.5 h-1 w-1 rounded-full bg-fg-tertiary"
            />
          )}
          {/* Open incidents keep the red badge; without any, the unread
              notifications get a quiet one (#259). Both are in the label. */}
          {openIncidentCount > 0 ? (
            <CountBadge
              count={openIncidentCount}
              max={9}
              urgent
              className="absolute -right-0.5 -top-0.5"
            />
          ) : unreadCount > 0 ? (
            <CountBadge
              count={unreadCount}
              max={9}
              className="absolute -right-0.5 -top-0.5 text-fg"
              data-testid="notifications-unread-badge"
            />
          ) : null}
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        collisionPadding={8}
        className="w-[min(360px,calc(100vw-16px))] p-0"
        // Focus moving into the retry confirmation is not "outside" in any
        // sense the reader means; closing here would unmount the row mid-ask.
        onInteractOutside={event => {
          if (confirming) event.preventDefault()
        }}
      >
        <ErrorBoundary
          fallback={() => (
            <p className="px-4 py-8 text-center text-caption text-fg-tertiary">
              Alerts could not be loaded.
            </p>
          )}
        >
          <Suspense fallback={<SectionSkeleton variant="rows" rows={3} label="Loading alerts…" />}>
            <NotificationsPanel
              projectSlug={projectSlug}
              projects={projects}
              openIncidentCount={openIncidentCount}
              onConfirmingChange={setConfirming}
              unreadCount={unreadCount}
              initialTab={initialTab}
              onNavigate={() => setOpen(false)}
            />
          </Suspense>
        </ErrorBoundary>
      </PopoverContent>
    </Popover>
  )
}
