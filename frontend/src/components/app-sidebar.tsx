import { useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  BookOpen,
  ChevronLeft,
  ChevronsUpDown,
  Database,
  LayoutDashboard,
  Palette,
  Search,
  Settings,
  SlidersHorizontal,
  X,
} from 'lucide-react'
import { eventTypesApi } from '@/api/eventTypes'
import { useAuth } from '@/components/auth-context'
import { BranchSwitcher } from '@/components/branch-switcher'
import {
  COMMAND_PALETTE_TRIGGER_ATTR,
  useCommandPalette,
} from '@/components/command-palette-context'
import { Kbd } from '@/components/primitives/kbd'
import { AccountMenuContent } from '@/components/shell/account-menu'
import { CollapsedSidebar } from '@/components/shell/collapsed-sidebar'
import { hasMoreBelow, revealCurrentRow } from '@/components/shell/nav-scroll'
import { OrgSwitcherSlot } from '@/components/shell/org-switcher-slot'
import { ProjectSwitcher } from '@/components/shell/project-switcher'
import { EmptyNav, NavGroupSection } from '@/components/shell/sidebar-nav'
import {
  capitalize,
  navLinkClass,
  navLinkStyle,
  projectSettingsHref,
} from '@/components/shell/sidebar-style'
import { useTweaksPanel } from '@/components/tweaks-panel-context'
import { DropdownMenu, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { initialsOf } from '@/components/ui/initials'
import { UserAvatar } from '@/components/ui/user-avatar'
import { TrifoldMark } from '@/components/states/brand-mark'
import { useActiveBranchId } from '@/hooks/useBranch'
import { buildNavGroups, currentOrgSlug, type NavGroup, projectPath, settingsPath, stripOrgPrefix, switchProjectPath, workspacePath } from '@/lib/navigation'
import { cn } from '@/lib/utils'
import { usePublicDemo } from '@/lib/deploymentMode'
import { commandPaletteShortcutLabel } from '@/lib/platform'
import type { Project } from '@/types'
import { eventTypesKey, projectsQueryOptions } from '@/lib/queryKeys'
import { canWrite, isOwner as isOwnerRole } from '@/lib/permissions'
import { LAST_PROJECT_SLUG_KEY, orgStorageKey } from '@/lib/activeOrg'

const SIDEBAR_STORAGE_KEY = 'tripl-sidebar-collapsed'

/**
 * Workspace-scoped nav shown on global routes (built per render: its links
 * carry the active organization) (no `:slug` in the URL, e.g.
 * `/workspace` or `/settings`). It replaces the per-project Plan/Observe/Govern
 * groups so the multi-project dashboard is not decorated with the last visited
 * project's counts and event-type tree. Reuses the same NavGroup/NavItem shape
 * so it renders through NavGroupSection / CollapsedSidebar unchanged.
 */
function workspaceNavGroup(): NavGroup {
  return {
  label: 'Workspace',
  items: [
    {
      id: 'all-projects',
      label: 'All projects',
      icon: LayoutDashboard,
      href: workspacePath(),
      match: (p) => stripOrgPrefix(p) === '/workspace' || p === '/',
    },
    {
      id: 'data-sources',
      label: 'Data sources',
      icon: Database,
      href: settingsPath('/settings/data-sources'),
      match: (p) => p.startsWith('/settings/data-sources'),
    },
    {
      id: 'workspace-settings',
      label: 'Settings',
      icon: Settings,
      href: settingsPath('/settings'),
      match: (p) => p.startsWith('/settings') && !p.startsWith('/settings/data-sources'),
    },
  ],
  }
}

function useSidebarCollapsed() {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(SIDEBAR_STORAGE_KEY) === '1'
    } catch {
      return false
    }
  })

  useEffect(() => {
    try {
      localStorage.setItem(SIDEBAR_STORAGE_KEY, collapsed ? '1' : '0')
    } catch {
      /* ignore */
    }
  }, [collapsed])

  return [collapsed, setCollapsed] as const
}

/**
 * Persist the last-visited project slug whenever the URL carries one. The
 * sidebar renders strictly from the real route slug now, so it no longer *reads*
 * this value — but other surfaces (e.g. the settings area's default-project
 * resolution) still depend on it being kept up to date here.
 */
function usePersistLastSlug(slug: string | undefined): void {
  useEffect(() => {
    if (!slug) return
    try {
      localStorage.setItem(orgStorageKey(LAST_PROJECT_SLUG_KEY), slug)
    } catch {
      /* ignore */
    }
  }, [slug])
}

/** The height of the nav's bottom fade: the 28px in its mask-image class. */
const NAV_FADE_PX = 28

/**
 * True while the nav scroller has rows hidden below its fold. Drives a bottom
 * fade, the only hint that the list scrolls: without it the last items simply
 * were not there at 1440×900 (#238).
 */
function useMoreBelow(el: HTMLElement | null): boolean {
  const [moreBelow, setMoreBelow] = useState(false)
  useEffect(() => {
    if (!el) return
    const update = () => setMoreBelow(hasMoreBelow(el))
    // No synchronous first call: ResizeObserver reports once on observe, which
    // is the initial measurement.
    el.addEventListener('scroll', update, { passive: true })
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(update) : null
    observer?.observe(el)
    if (el.firstElementChild) observer?.observe(el.firstElementChild)
    return () => {
      el.removeEventListener('scroll', update)
      observer?.disconnect()
    }
  }, [el])
  return moreBelow
}

export function AppSidebar({
  drawer = false,
  onCloseDrawer,
}: {
  /**
   * Rendered as the off-canvas drawer (below lg). The drawer closes instead of
   * collapsing: a collapsed drawer was a useless 52px rail over a blurred page
   * that then persisted to desktop (#238).
   */
  drawer?: boolean
  onCloseDrawer?: () => void
} = {}) {
  const { slug } = useParams()
  const location = useLocation()
  const navigate = useNavigate()
  const auth = useAuth()
  const palette = useCommandPalette()
  const tweaks = useTweaksPanel()
  const branchId = useActiveBranchId()
  const [collapsed, setCollapsed] = useSidebarCollapsed()
  // A callback ref held in state, so the fade re-measures when the scroller
  // remounts (collapsing and expanding the sidebar swaps it out).
  const [scroller, setScroller] = useState<HTMLDivElement | null>(null)
  const moreBelow = useMoreBelow(scroller)
  // The Appearance popover hangs from the footer's palette button, whether it
  // was opened there or from the account menu.
  const appearanceRef = useRef<HTMLButtonElement | null>(null)

  const projectsQuery = useQuery(projectsQueryOptions())
  const projects = projectsQuery.data ?? []

  // Keep the persisted last-slug fresh, but render the nav from the REAL route
  // slug only — so `/workspace` shows a workspace-scoped nav, not the last
  // project's Plan/Observe/Govern groups.
  usePersistLastSlug(slug)

  const project = slug ? projects.find((p) => p.slug === slug) : undefined
  // Owner-only items are dropped rather than shown-and-denied: the routes behind
  // them 403 for everyone else, and a nav entry that always fails reads as a
  // broken app rather than a permission boundary.
  const isOwner = isOwnerRole(auth.user?.role)
  // The Plan counts come from the project summary, which is main's. On a
  // branch the Events page lists the branch's rows, so "Events 12" beside a
  // list of 13 was a claim about a different plan: the counts step aside
  // while a branch is active (#243).
  const onBranch = branchId !== null
  const navGroups: NavGroup[] = slug
    ? buildNavGroups(slug, project?.summary).map((group) => ({
        ...group,
        items: group.items
          .filter((item) => !item.ownerOnly || isOwner)
          .map((item) => (onBranch && group.label === 'Plan' ? { ...item, count: undefined, attention: undefined } : item)),
      }))
    : [workspaceNavGroup()]
  const eventTypesQuery = useQuery({
    queryKey: eventTypesKey(slug, branchId),
    queryFn: () => eventTypesApi.list(slug!, branchId),
    enabled: !!slug,
  })
  const eventTypes = eventTypesQuery.data ?? []
  const currentPath = location.pathname
  // The current page's row in sight: on a laptop screen the last rows (Audit
  // log) sit under the fold, and a page opened by its address lit a row no
  // one could see. Again once the event types load, which push rows down.
  useEffect(() => {
    if (scroller) revealCurrentRow(scroller, { bottomInset: NAV_FADE_PX })
  }, [scroller, currentPath, eventTypes.length])
  const userInitials = initialsOf(auth.user?.name ?? auth.user?.email)
  const userLabel = auth.user?.name ?? auth.user?.email ?? 'Signed in'
  const conceptsActive = !!slug && currentPath === projectPath(currentOrgSlug(), slug, '/concepts')
  // Switching project keeps the surface being compared when the new project
  // has it, and otherwise lands on the project's one home.
  const pickProject = (picked: Project) =>
    navigate(switchProjectPath(currentPath, slug, picked.slug))
  const signOut = () => {
    void auth.logout()
  }
  // A public demo runs on demo projects only.
  const publicDemo = usePublicDemo()
  const canCreateProject = canWrite(auth.user?.role) && !publicDemo

  if (collapsed && !drawer) {
    return (
      <CollapsedSidebar
        onExpand={() => setCollapsed(false)}
        navGroups={navGroups}
        currentPath={currentPath}
        slug={slug}
        activeProject={project}
        projects={projects}
        projectsLoading={projectsQuery.isLoading}
        onPickProject={pickProject}
        canCreateProject={canCreateProject}
        conceptsActive={conceptsActive}
        userInitials={userInitials}
        userLabel={userLabel}
        isLoggingOut={auth.isLoggingOut}
        onSignOut={signOut}
        onOpenTweaks={(anchor) => tweaks.setOpen(true, anchor)}
        onOpenPalette={() => palette.setOpen(true)}
      />
    )
  }

  return (
    <nav
      aria-label="Main navigation"
      className="flex h-full w-[calc(240px+env(safe-area-inset-left))] flex-shrink-0 flex-col border-r pb-[env(safe-area-inset-bottom)] pl-[env(safe-area-inset-left)] bg-bg-sunken border-border"
    >
      {/* Service logo — the triangle is the tripl brand, so it lives here (not
          in the project chip) and links back to the workspace overview. */}
      <div className="flex items-center gap-1 px-3 pt-2.5 pb-1.5">
        <Link
          to={workspacePath()}
          title="tripl — home"
          aria-label="tripl — home"
          className="flex flex-1 items-center gap-2 rounded-md px-1 py-1 no-underline transition-colors hover:bg-sidebar-hover"
        >
          <TrifoldMark size={24} />
          {/* The wordmark is drawn at a fixed 18px beside the 24px mark; it is
              a logo, not UI text, so it sits outside the type scale. */}
          <span
            className="font-bold leading-none tracking-[-0.045em]"
            style={{ color: 'var(--fg)', fontSize: 18 }}
          >
            tripl
          </span>
        </Link>
        {drawer ? (
          <button
            type="button"
            onClick={onCloseDrawer}
            aria-label="Close navigation"
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md transition-colors hover:bg-sidebar-hover text-fg-tertiary"
          >
            <X className="size-4" aria-hidden="true" />
          </button>
        ) : (
          <button
            type="button"
            onClick={() => setCollapsed(true)}
            title="Collapse sidebar"
            aria-label="Collapse sidebar"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md transition-colors hover:bg-sidebar-hover text-fg-tertiary"
          >
            <ChevronLeft className="size-4" aria-hidden="true" />
          </button>
        )}
      </div>

      {/* Organization switcher — above the project switcher, since projects
          belong to an organization. Renders nothing for someone in one. */}
      <div className="px-3 pb-1 empty:hidden">
        <OrgSwitcherSlot />
      </div>

      {/* Project switcher — no service mark; just a monogram, so it reads as
          project-scoped rather than as the service logo. */}
      <div className="px-3 pb-2">
        <ProjectSwitcher
          activeProject={project}
          projects={projects}
          loading={projectsQuery.isLoading}
          onPick={pickProject}
          canCreateProject={canCreateProject}
        />
      </div>

      {/* Branch switcher — plan branches belong to a project, so the control
          lives directly under it (and nowhere else in the shell). */}
      {slug && (
        <div className="px-3 pb-2.5">
          <BranchSwitcher slug={slug} />
        </div>
      )}

      {/* Command / search — the one palette entry point from lg up; the top
          bar's magnifier is hidden there (#238). */}
      <div className="px-3 pb-2.5">
        <button
          type="button"
          onClick={() => palette.setOpen(true)}
          {...{ [COMMAND_PALETTE_TRIGGER_ATTR]: '' }}
          className="flex h-[30px] w-full items-center gap-2 rounded-md border px-2.5 text-left text-body-sm transition-colors hover:bg-sidebar-hover"
          style={{
            background: 'var(--surface)',
            borderColor: 'var(--border-subtle)',
            color: 'var(--fg-subtle)',
          }}
        >
          <Search className="size-3.5" aria-hidden="true" />
          <span className="flex-1 truncate">Search or jump…</span>
          <Kbd>{commandPaletteShortcutLabel()}</Kbd>
        </button>
      </div>

      {/* Grouped nav. The bottom fade says the list goes on below the fold. */}
      <div
        ref={setScroller}
        data-more-below={moreBelow || undefined}
        className={cn(
          'min-h-0 flex-1 overflow-y-auto px-2 pt-1 pb-2',
          moreBelow && '[mask-image:linear-gradient(to_bottom,black_calc(100%-28px),transparent)]',
        )}
      >
        <div>
          {navGroups.length > 0 ? (
            navGroups.map((group) => (
              <NavGroupSection
                key={group.label}
                group={group}
                currentPath={currentPath}
                eventTypes={eventTypes}
                navSlug={slug}
              />
            ))
          ) : (
            <EmptyNav loading={projectsQuery.isLoading || projectsQuery.isError} />
          )}
        </div>
      </div>

      {/* Pinned footer: Project settings and Concepts, then the account menu.
          Project settings used to be the LAST row of the scrolling nav, below
          the fold on the commonest laptop once a project had event types
          (#238). */}
      <div className="px-3 py-3 border-t border-border-subtle">
        {slug && (
          <div className="mb-2 flex flex-col gap-px">
            <Link
              to={projectSettingsHref(slug)}
              // Never "active": project settings open in the full-screen
              // takeover, which does not render this sidebar.
              className={navLinkClass(false, 'px-1.5 text-body-sm')}
              style={navLinkStyle(false)}
            >
              <SlidersHorizontal
                className="size-3.5 shrink-0 text-fg-tertiary"
                aria-hidden="true"
              />
              <span className="flex-1 truncate text-left">Project settings</span>
            </Link>
            <Link
              to={projectPath(currentOrgSlug(), slug, '/concepts')}
              aria-current={conceptsActive ? 'page' : undefined}
              className={navLinkClass(conceptsActive, 'px-1.5 text-body-sm')}
              style={navLinkStyle(conceptsActive)}
            >
              <BookOpen
                className="size-3.5 shrink-0"
                style={{ color: conceptsActive ? 'var(--accent)' : 'var(--fg-subtle)' }}
                aria-hidden="true"
              />
              <span className="flex-1 truncate text-left">Concepts</span>
            </Link>
          </div>
        )}
        <div className="flex items-center gap-1">
          {/* The whole user row is the account menu, as on the collapsed rail
              (#238). Three unlabeled 28px icons sat here, the gear beside
              the name read as "my profile", and Sign out was one pixel-row from
              the others with nothing in between. */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                aria-label={`Account menu — ${userLabel}`}
                className="flex min-w-0 flex-1 items-center gap-1.5 rounded-control px-1 py-1 text-left transition-colors hover:bg-sidebar-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
              >
                <UserAvatar name={auth.user?.name ?? auth.user?.email} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-body-sm font-medium leading-[1.1]">
                    {auth.user?.name ?? auth.user?.email}
                  </span>
                  <span
                    className="mt-px block truncate text-micro leading-[1.1] text-fg-tertiary"
                  >
                    {auth.user?.role ? capitalize(auth.user.role) : 'Signed in'}
                  </span>
                </span>
                <ChevronsUpDown
                  className="size-3 shrink-0 text-fg-tertiary"
                  aria-hidden="true"
                />
              </button>
            </DropdownMenuTrigger>
            <AccountMenuContent
              side="top"
              align="start"
              userLabel={userLabel}
              isLoggingOut={auth.isLoggingOut}
              onSignOut={signOut}
              onOpenTweaks={() => tweaks.setOpen(true, appearanceRef.current)}
            />
          </DropdownMenu>
          {/* Appearance stays one click away: it is the one account control
              people reach for often. It used to be a disc fixed over the
              bottom-right corner of every page. */}
          <button
            ref={appearanceRef}
            type="button"
            title="Appearance"
            aria-label="Appearance"
            aria-haspopup="dialog"
            aria-expanded={tweaks.open}
            onClick={(event) => tweaks.setOpen(!tweaks.open, event.currentTarget)}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md transition-colors hover:bg-sidebar-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] text-fg-tertiary"
          >
            <Palette className="size-4" aria-hidden="true" />
          </button>
        </div>
      </div>
    </nav>
  )
}
