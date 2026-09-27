import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react'
import { Link, useBlocker, useNavigate, type Location } from 'react-router-dom'
import { ArrowUpRight, Check, ChevronLeft, ChevronsUpDown, LogOut, Menu, X } from 'lucide-react'
import { useAuth } from '@/components/auth-context'
import { useConfirm } from '@/hooks/useConfirm'
import { UserAvatar } from '@/components/ui/user-avatar'
import { Chip } from '@/components/primitives/chip'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { SETTINGS_CONTENT_ID } from './landmarks'
import { backToLabel, sectionLabel, sectionPathForUrl, visibleGroupsAll } from './nav'
import { SettingsCommandPalette } from './settings-palette'
import {
  LEAVE_CONFIRMED,
  UNSAVED_CONFIRM_COPY,
  UnsavedChangesProvider,
  type UnsavedWork,
} from './unsaved-changes'
import type { Project } from '@/types'
import { isOwner as isOwnerRole, isPlatformAdmin as isPlatformAdminUser } from '@/lib/permissions'

const RAIL_TITLE_ID = 'settings-rail-title'
const RAIL_ID = 'settings-rail'
/** From here up the rail is pinned in flow; below it, it is an off-canvas drawer. */
const RAIL_PINNED_QUERY = '(min-width: 768px)'

/**
 * Whether the rail is pinned (`md` and up). Without `matchMedia` it answers
 * "pinned", so the rail is never made inert unmeasured — the same fallback the
 * app shell's sidebar uses.
 */
function useRailPinned(): boolean {
  const subscribe = useCallback((onChange: () => void) => {
    if (typeof window.matchMedia !== 'function') return () => {}
    const mql = window.matchMedia(RAIL_PINNED_QUERY)
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [])
  return useSyncExternalStore(
    subscribe,
    () => (typeof window.matchMedia === 'function' ? window.matchMedia(RAIL_PINNED_QUERY).matches : true),
    () => true,
  )
}

function firstFocusable(root: HTMLElement | null): HTMLElement | null {
  return (
    root?.querySelector<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ) ?? null
  )
}

/**
 * Full-viewport takeover shell for the Settings area (Linear/Vercel pattern).
 * A 264px sunken left rail holds the back-to-app link and one grouped nav that
 * lists every settings group (project + workspace) together — no project/
 * workspace context toggle. The content column is centered at 768px.
 * Recreated from design/tripl/project/settings-kit.jsx (SettingsLayout).
 *
 * Below `md` the rail slides off-canvas behind a hamburger, mirroring the app
 * shell in `Layout.tsx`. Pinned in flow it would eat 264px of a 390px phone and
 * leave the settings forms a ~45px column (tripl-jfm3.40).
 */
export function SettingsLayout({
  activePath,
  backHref,
  projectName,
  projectSlug,
  projects = [],
  children,
}: {
  /** Current section path (e.g. 'project/general'). */
  activePath: string
  /** Where "Back to tripl" returns to. */
  backHref: string
  /** Active project name, used to personalize the Project group sub-label. */
  projectName?: string
  /** The project the Project sections are bound to. Their links carry it as
   *  `?project=`, so moving between them never falls back to whichever
   *  project another tab visited last (SHELL-20). */
  projectSlug?: string
  /** Workspace projects, offered as palette destinations. Already fetched by
   *  SettingsArea, so the palette never issues a query of its own. */
  projects?: readonly Project[]
  children: ReactNode
}) {
  const auth = useAuth()
  const navigate = useNavigate()
  const { confirm, dialog } = useConfirm()
  const isOwner = isOwnerRole(auth.user?.role)
  // The operator sections (security, observability, system) are the platform
  // admin's alone, whatever their organization role (F20 PR4).
  const isPlatformAdmin = isPlatformAdminUser(auth.user)

  // Personalize group sub-labels with live identity, matching the mockup
  // (Project → project name, Account → "You · <name>"). Workspace stays
  // generic until a workspace entity exists. A sub-label that only repeats its
  // group ("Project Project", "Workspace Workspace") is dropped (ST-7).
  const userName = auth.user?.name?.split(/\s+/)[0] ?? auth.user?.email ?? ''
  const subFor = (group: { label: string; sub: string }): string => {
    if (group.label === 'Project' && projectName) return projectName
    if (group.label === 'Account' && userName) return `You · ${userName}`
    if (group.sub === group.label) return ''
    return group.sub
  }
  const backLabel = backToLabel(backHref, projectName)
  const sectionTitle = sectionLabel(activePath)

  // Off-canvas rail state, used only below `md` — above it the `md:*` utilities
  // pin the rail to static flow regardless of this flag.
  const [railOpen, setRailOpen] = useState(false)
  const closeRail = useCallback(() => setRailOpen(false), [])
  const railPinned = useRailPinned()
  // The drawer behaves like the modal it looks like, as the app shell's does
  // (DS-11): below `md` a closed rail is `inert`, so its ~20 links and Sign out
  // leave the Tab order and the accessibility tree instead of being walked
  // through off-screen on every settings page; an open one takes focus, closes
  // on Escape, and while it is open the content behind it is inert, which is
  // also what keeps Tab inside it. Closing hands focus back to the opener.
  const railDrawerActive = railOpen && !railPinned
  const railRef = useRef<HTMLElement | null>(null)
  const railOpenerRef = useRef<HTMLElement | null>(null)
  const openRail = useCallback(() => {
    railOpenerRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null
    setRailOpen(true)
  }, [])
  const wasDrawerActive = useRef(false)
  useEffect(() => {
    if (railDrawerActive) {
      firstFocusable(railRef.current)?.focus()
    } else if (wasDrawerActive.current) {
      const opener = railOpenerRef.current
      // Only when focus would otherwise be lost with the rail: a rail link
      // that navigated has already moved focus on purpose.
      const active = document.activeElement
      const lost = !active || active === document.body || railRef.current?.contains(active)
      if (lost && opener?.isConnected) opener.focus()
    }
    wasDrawerActive.current = railDrawerActive
  }, [railDrawerActive])
  useEffect(() => {
    if (!railDrawerActive) return
    const onKey = (event: KeyboardEvent) => {
      // A Radix layer on top (a dialog, the settings palette) handles Escape
      // at document capture and marks it handled: that press closes that layer
      // only, not the drawer beneath it too.
      if (event.key !== 'Escape' || event.defaultPrevented) return
      event.preventDefault()
      closeRail()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [railDrawerActive, closeRail])
  // Any route change closes the drawer — Back included, which no rail click
  // sees. "Derived state from props" rather than an effect, as Layout does.
  const [lastActivePath, setLastActivePath] = useState(activePath)
  if (lastActivePath !== activePath) {
    setLastActivePath(activePath)
    if (railOpen) setRailOpen(false)
  }

  // Draft held by the section currently rendered in the content column, so the
  // rail can warn before it navigates that draft out of existence (tripl-l8v2).
  const [unsaved, setUnsaved] = useState<UnsavedWork | null>(null)
  const registerUnsaved = useCallback((work: UnsavedWork | null) => setUnsaved(work), [])
  const unsavedChanges = useMemo(() => ({ registerUnsaved }), [registerUnsaved])

  /**
   * The draft that leaving for `settingsPath` would discard — null for a
   * destination that keeps it, or when there is none. `null` as the path means
   * leaving the settings area entirely, which no draft survives.
   *
   * The predicate lives here alone so that every way out of the takeover asks
   * the same question: the rail, the palette and Sign out all used to answer it
   * separately, and two of them answered "no" unconditionally (tripl-l8v2).
   * Browser Back and reload/close reach it through the effects below, which are
   * the two exits with no element to hang an onClick on (tripl-l33u.6).
   */
  const draftAtRisk = useCallback(
    (settingsPath: string | null): UnsavedWork | null => {
      if (!unsaved) return null
      if (settingsPath !== null && unsaved.keptBy(settingsPath)) return null
      return unsaved
    },
    [unsaved],
  )

  /**
   * Resolves true when leaving is safe, or once the user has accepted the loss.
   *
   * Accepting deliberately does not clear the registration. The section that
   * registered the draft owns it and drops it as it unmounts, and the leaves
   * that keep it mounted — AI → Email, a Back that lands inside the instance
   * group — would otherwise leave the shell believing a live draft was gone:
   * beforeunload unregistered, no entry parked, every rail link silent, and the
   * next exit discarding the draft with no warning at all (tripl-l33u.6).
   */
  const confirmLeave = useCallback(
    async (settingsPath: string | null): Promise<boolean> => {
      const work = draftAtRisk(settingsPath)
      if (!work) return true
      return confirm({
        // The one unsaved-changes wording (AU-42): "Keep editing" / "Discard
        // changes", the same dialog a form's own Cancel asks.
        ...UNSAVED_CONFIRM_COPY,
        message: work.message,
        variant: 'danger',
      })
    },
    [confirm, draftAtRisk],
  )

  /**
   * A rail link needs no interception any more: it is a `<Link>`, so the click
   * becomes a router navigation and the blocker below sees it — along with the
   * palette, Back, Forward and everything else. Modified clicks (new tab / new
   * window) never reach the router at all, which is why real anchors were
   * rendered here in the first place (tripl-wd66) and why they still open a
   * second window leaving the draft where it is.
   */
  const guardLeave = () => closeRail()

  /** The palette's way out: no anchor, but still a router navigation. */
  const leaveTo = (href: string) => {
    closeRail()
    navigate(href)
  }

  /**
   * Rebind the Project sections to another project without leaving settings
   * (ST-6). The address keeps the section when it is a project one and opens
   * General otherwise; `?project=` outranks every other source of the slug.
   *
   * The blocker would wave this through — the destination is the same section,
   * which "keeps" its draft — yet the draft belongs to the old project and dies
   * with the switch. So it asks as for a way out, and tells the blocker so.
   */
  const switchProject = (slug: string) => {
    if (slug === projectSlug) return
    const path = activePath.startsWith('project/') ? activePath : 'project/general'
    void confirmLeave(null).then((leave) => {
      if (!leave) return
      closeRail()
      navigate(`/settings/${path}?project=${encodeURIComponent(slug)}`, { state: LEAVE_CONFIRMED })
    })
  }

  /**
   * Sign out is the one exit the blocker cannot own, because the destructive
   * part is not the navigation: logging out first and asking afterwards would
   * end a session the user might have chosen to keep. So it asks, then logs out,
   * then navigates — telling the blocker the question is already answered in the
   * navigation's own state, rather than in a flag that could outlive it.
   */
  const signOut = () => {
    void confirmLeave(null).then((leave) => {
      if (!leave) return
      void auth.logout().then(() => navigate('/auth', { state: LEAVE_CONFIRMED }))
    })
  }

  // Whether a draft exists at all. The section re-registers a new UnsavedWork as
  // it edits, so the listener below is keyed on this rather than on the draft.
  const hasUnsaved = unsaved !== null

  /**
   * Reload and tab-close are not React navigations, so the dialog above cannot
   * run for them — only the browser's own prompt can, and only from a listener
   * that exists while the draft does. Registered off the dirty flag alone so a
   * settings page nobody has typed into never interrupts a reload.
   */
  useEffect(() => {
    if (!hasUnsaved) return
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      // Browsers show their own wording. returnValue is what makes the older
      // ones (Chrome/Edge < 119) prompt at all.
      event.returnValue = true
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [hasUnsaved])

  /**
   * ONE guard for every navigation: the rail, the palette, Back and Forward.
   *
   * A blocker is asked BEFORE the navigation commits, which is the whole reason
   * this needed the data router (see main.tsx). The history-parking attempt it
   * replaces could only ever react AFTER the browser had already moved, which is
   * what made it unfixable: a settings move the draft survives buried the parked
   * entry, and every repair opened another hole (tripl-l33u.14).
   *
   * Note what is NOT here any more: four call sites that each ran their own
   * confirm and then navigated. Under a blocker that shape asks twice — once by
   * hand, once when the navigation it triggers is itself intercepted. The exits
   * now just navigate, and this decides.
   */
  const blocker = useBlocker(
    useCallback(
      ({ nextLocation }: { nextLocation: Location }) => {
        // Sign-out has already asked; see `signOut`.
        if ((nextLocation.state as typeof LEAVE_CONFIRMED | null)?.leaveConfirmed) return false
        return draftAtRisk(sectionPathForUrl(nextLocation.pathname)) !== null
      },
      [draftAtRisk],
    ),
  )

  /**
   * Blocked navigations are resolved here rather than at the call site, because
   * a blocker has no idea which one it caught. `proceed` replays the navigation
   * the user accepted; `reset` returns them to where they were with the draft
   * intact.
   *
   * Deliberately does NOT clear the registration on accept — see `confirmLeave`.
   */
  const askingFor = useRef<string | null>(null)
  useEffect(() => {
    if (blocker.state !== 'blocked') {
      askingFor.current = null
      return
    }
    // ONCE per blocked navigation. `useBlocker` hands back a fresh object every
    // render, so an effect that depends on it re-runs while the dialog is open —
    // and asking again from inside the answer is a loop that never lets the
    // dialog be answered. The key identifies the navigation, not the render.
    if (askingFor.current === blocker.location.key) return
    askingFor.current = blocker.location.key
    void confirmLeave(sectionPathForUrl(blocker.location.pathname)).then((leave) => {
      if (leave) blocker.proceed()
      else blocker.reset()
    })
  }, [blocker, confirmLeave])

  return (
    <div className="relative flex h-screen overflow-hidden bg-background">
      {dialog}
      {/* Ctrl+K. The takeover mounts outside Layout, so the app palette's
          provider never reached these 14 routes (tripl-wd66) — and mounting it
          here would have bound it to `projects[0]`, since no /settings/* route
          carries a :slug. This one is scoped to what the area actually knows,
          and leaves through the same guard the rail uses. */}
      <SettingsCommandPalette
        activePath={activePath}
        backHref={backHref}
        isOwner={isOwner}
        isPlatformAdmin={isPlatformAdmin}
        projects={projects}
        backLabel={backLabel}
        onLeave={leaveTo}
        onSwitchProject={switchProject}
        onSignOut={signOut}
      />
      {/* Same bypass block as the app shell — the settings rail is a ~20-stop
          repeated block on every settings page. */}
      <a href={`#${SETTINGS_CONTENT_ID}`} className="skip-link">
        Skip to main content
      </a>
      <aside
        id={RAIL_ID}
        ref={railRef}
        inert={!railPinned && !railOpen}
        className={
          'fixed inset-y-0 left-0 z-(--z-drawer) flex w-[264px] shrink-0 flex-col transition-transform duration-200 ease-out md:static md:translate-x-0 ' +
          (railOpen ? 'translate-x-0' : '-translate-x-full md:translate-x-0')
        }
        style={{ background: 'var(--bg-sunken)', borderRight: '1px solid var(--border)' }}
      >
        {/* Header: back to app */}
        <div className="relative px-4 pb-2.5 pt-3.5">
          {/* The drawer's own way out below md, beside the backdrop tap and
              Escape it already closed on (ST-13), as the app sidebar's drawer
              has one. Pinned, there is nothing to close. */}
          {!railPinned && (
            <button
              type="button"
              aria-label="Close navigation"
              onClick={closeRail}
              className="absolute right-2 top-2.5 flex h-10 w-10 items-center justify-center rounded-md text-fg-subtle transition-colors hover:bg-sidebar-hover hover:text-fg focus-visible:text-fg md:hidden"
            >
              <X className="size-4" aria-hidden="true" />
            </button>
          )}
          <Link
            to={backHref}
            onClick={guardLeave}
            // Hover and keyboard focus through classes, not JS style swaps that
            // focus never triggered (DS-21).
            className="-ml-1 inline-flex items-center gap-[7px] rounded-md px-2 py-1 pr-2 text-body-sm text-fg-muted no-underline transition-colors hover:text-fg focus-visible:text-fg"
          >
            <ChevronLeft className="size-4" />
            <span>{backLabel}</span>
          </Link>
          {/* Deliberately not a heading: the rail is chrome, and an <h2> here
              sat above every page's <h1> in DOM order, so the heading outline
              opened with a level-2 skip (tripl-jfm3.69). It names the nav
              landmark instead. */}
          {/* No subtitle: "Workspace & account configuration" left out the
              Project and Instance groups, which describe themselves (ST-10). */}
          <div id={RAIL_TITLE_ID} className="mx-1 mt-2.5 text-heading font-semibold tracking-[-0.01em]">
            Settings
          </div>
        </div>

        {/* Grouped nav — every settings group in one rail, no context toggle */}
        {/* The bottom fade says the list goes on under the footer: in the
            phone drawer the last items sat cut off with no hint (ST-13). */}
        <nav
          aria-labelledby={RAIL_TITLE_ID}
          className="flex-1 overflow-y-auto px-3 pb-6 pt-1 [mask-image:linear-gradient(to_bottom,black_calc(100%_-_24px),transparent)]"
        >
          {visibleGroupsAll(isOwner, isPlatformAdmin).map((group) => {
            // Sentence case, not an uppercase eyebrow: these are names ("Demo
            // project 2", "You · Ada"), and caps shouted them (ST-7).
            const sub = subFor(group)
            return (
            <div key={group.label} className="mb-4">
              <div className="px-[9px] pb-1.5">
                <div className="flex min-w-0 items-center gap-1.5">
                  <span className="shrink-0 text-caption font-semibold text-fg">
                    {group.label}
                  </span>
                  {group.label === 'Project' && projects.length > 0 ? (
                    <RailProjectSwitcher
                      projects={projects}
                      currentSlug={projectSlug}
                      currentName={projectName}
                      onPick={switchProject}
                    />
                  ) : (
                    sub && (
                      <span className="min-w-0 truncate text-caption text-fg-tertiary">
                        {sub}
                      </span>
                    )
                  )}
                </div>
                {/* Phones get the labels alone: three lines of chrome per group
                    pushed the last items under the drawer's footer (ST-13). */}
                <p className="mt-0.5 hidden text-micro leading-snug md:block text-fg-tertiary">
                  {group.desc}
                </p>
              </div>
              <div className="flex flex-col gap-px">
                {group.items.map((item) => {
                  const active = item.path === activePath
                  const dirty = unsaved?.dirtyPaths?.includes(item.path) ?? false
                  const Icon = item.icon
                  const href =
                    projectSlug && item.path.startsWith('project/')
                      ? `/settings/${item.path}?project=${encodeURIComponent(projectSlug)}`
                      : `/settings/${item.path}`
                  return (
                    // A real anchor, not a button: as buttons none of these 14
                    // destinations could be cmd-clicked into a new tab,
                    // middle-clicked, hovered for a URL or copied (tripl-wd66).
                    <Link
                      key={item.id}
                      to={href}
                      aria-current={active ? 'page' : undefined}
                      aria-label={dirty ? `${item.label}, unsaved changes` : item.label}
                      onClick={guardLeave}
                      // Match the app shell's sidebar: the current item carries
                      // the sidebar's active tint and an accent bar on its left
                      // edge. The old --surface-hover on the sunken rail was a
                      // 1-2% change in light theme, so the selection read from
                      // the icon alone (ST-8). Hover is a class, not a JS style
                      // swap: the swap left a stale fill when the active item
                      // changed under the pointer and never answered keyboard
                      // focus (DS-21).
                      className={
                        // 40px rows in the phone drawer, the dense 31px rail
                        // from md up (ST-12).
                        'relative flex items-center gap-2 rounded-md px-[9px] py-2.5 md:py-[7px] text-left text-body-sm no-underline transition-colors hover:bg-sidebar-hover focus-visible:bg-sidebar-hover ' +
                        (active ? RAIL_ACTIVE_CLASS : 'font-medium text-fg-muted')
                      }
                    >
                      <Icon
                        className="size-4 shrink-0"
                        style={{ color: active ? 'var(--accent)' : 'var(--fg-subtle)' }}
                      />
                      <span className="flex-1">{item.label}</span>
                      {item.tag && (
                        <Chip size="xs" variant="outline" aria-hidden="true">
                          {item.tag}
                        </Chip>
                      )}
                      {dirty && (
                        // Named through the link's aria-label; the dot is the
                        // sighted half of the same signal.
                        <span
                          aria-hidden="true"
                          title="Unsaved changes"
                          className="h-[7px] w-[7px] shrink-0 rounded-full bg-warning"
                        />
                      )}
                    </Link>
                  )
                })}
                {group.label === 'Project' && projectSlug && (
                  // The project's tracking plan and alerting live in the app,
                  // not in this rail; say so here instead of leaving a
                  // "Project operations" button on General as the only way
                  // there (#238 ST-5). The arrow marks it as leaving the area.
                  <Link
                    to={`/p/${encodeURIComponent(projectSlug)}/event-types`}
                    onClick={guardLeave}
                    className="flex items-center gap-2 rounded-md px-[9px] py-2.5 md:py-[7px] text-left text-body-sm font-medium text-fg-muted no-underline transition-colors hover:bg-sidebar-hover focus-visible:bg-sidebar-hover"
                  >
                    <ArrowUpRight className="size-4 shrink-0 text-fg-tertiary" aria-hidden="true" />
                    <span className="flex-1">Tracking plan &amp; alerting</span>
                  </Link>
                )}
              </div>
            </div>
            )
          })}
        </nav>

        {/* Footer user */}
        <div
          className="flex items-center gap-[9px] p-3 border-t border-border-subtle"
        >
          <UserAvatar name={auth.user?.name ?? auth.user?.email} />
          <div className="min-w-0 flex-1">
            <div className="truncate text-body-sm font-medium leading-[1.1]">
              {auth.user?.name ?? auth.user?.email}
            </div>
            <div
              className="mt-px truncate text-micro leading-[1.1] text-fg-tertiary"
            >
              {auth.user?.role ? capitalize(auth.user.role) : 'Signed in'}
            </div>
          </div>
          <button
            type="button"
            title={auth.isLoggingOut ? 'Signing out…' : 'Sign out'}
            aria-label="Sign out"
            disabled={auth.isLoggingOut}
            onClick={signOut}
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md text-fg-subtle transition-colors hover:text-fg focus-visible:text-fg disabled:opacity-50 md:h-8 md:w-8"
          >
            <LogOut className="size-4" />
          </button>
        </div>
      </aside>

      {/* Backdrop for the off-canvas rail (below md only). */}
      {railOpen && (
        <button
          type="button"
          aria-label="Close settings navigation"
          onClick={closeRail}
          className="fixed inset-0 z-(--z-backdrop) bg-black/40 backdrop-blur-[2px] md:hidden"
        />
      )}

      {/* Content */}
      <main
        id={SETTINGS_CONTENT_ID}
        tabIndex={-1}
        inert={railDrawerActive}
        className="min-w-0 flex-1 overflow-y-auto focus:outline-none"
      >
        {/* Phone-only header: the only way back to the rail once it is
            off-canvas. Hidden from md up, where the rail is always visible.
            Pinned to 52px so a section can park its own sticky bar directly
            below it instead of underneath it (ServiceSettingsPage's Save row
            uses `top-[52px] md:top-0`). */}
        <div
          className="sticky top-0 z-(--z-sticky) flex h-[52px] items-center gap-2 px-4 md:hidden bg-background border-b border-border"
        >
          <button
            type="button"
            aria-label="Open settings navigation"
            aria-expanded={railOpen}
            aria-controls={RAIL_ID}
            onClick={openRail}
            className="flex h-10 w-10 items-center justify-center rounded-md transition-colors hover:bg-[var(--surface-hover)] text-fg-secondary"
          >
            <Menu className="h-4 w-4" />
          </button>
          {/* Where you are once the h1 has scrolled away, and a one-tap way
              back to the app without opening the drawer (ST-11). */}
          <span className="flex min-w-0 flex-1 items-baseline gap-1.5 text-body">
            <span className="shrink-0 font-semibold">Settings</span>
            {sectionTitle && (
              <>
                <span aria-hidden="true" className="text-fg-tertiary">
                  /
                </span>
                <span className="truncate text-fg-secondary">
                  {sectionTitle}
                </span>
              </>
            )}
          </span>
          <Link
            to={backHref}
            aria-label="Close settings"
            title={backLabel}
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md transition-colors hover:bg-[var(--surface-hover)] text-fg-secondary"
          >
            <X className="size-4" aria-hidden="true" />
          </Link>
        </div>
        {/* The narrow content width every form and settings page shares
            (DS-3), left-aligned against the rail instead of floating centred. */}
        <div className="max-w-[880px] px-4 pb-24 pt-6 sm:px-6 md:px-10 md:pt-10">
          <UnsavedChangesProvider value={unsavedChanges}>{children}</UnsavedChangesProvider>
        </div>
      </main>
    </div>
  )
}

/** The current rail item: the app sidebar's active tint and accent bar (ST-8). */
const RAIL_ACTIVE_CLASS =
  "bg-sidebar-active hover:bg-sidebar-active focus-visible:bg-sidebar-active font-semibold text-fg before:absolute before:inset-y-1.5 before:left-0 before:w-[2px] before:rounded-full before:bg-[var(--accent)] before:content-['']"

/**
 * The Project group's sub-label as a switcher (ST-6): the bound project, or
 * "Pick a project", opening the workspace's projects. Picking one rebinds the
 * Project sections in place instead of sending the user out to the app and
 * back.
 */
function RailProjectSwitcher({
  projects,
  currentSlug,
  currentName,
  onPick,
}: {
  projects: readonly Project[]
  currentSlug: string | undefined
  currentName: string | undefined
  onPick: (slug: string) => void
}) {
  const shown = currentName ?? (currentSlug ? currentSlug : 'Pick a project')
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={currentName ? `Switch project (current: ${currentName})` : 'Pick a project'}
          className="-my-0.5 inline-flex min-w-0 items-center gap-1 rounded-sm px-1 py-0.5 text-caption transition-colors hover:bg-sidebar-hover hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
          style={{ color: 'var(--fg-subtle)' }}
        >
          <span className="min-w-0 truncate">{shown}</span>
          <ChevronsUpDown className="size-3 shrink-0" aria-hidden="true" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" sideOffset={4} className="w-[240px]">
        <DropdownMenuLabel className="micro-label text-fg-tertiary">
          Project settings for
        </DropdownMenuLabel>
        <div className="max-h-[320px] overflow-y-auto">
          {projects.map((project) => (
            <DropdownMenuItem
              key={project.slug}
              onSelect={() => onPick(project.slug)}
              className="flex items-center gap-2 text-body-sm"
            >
              <span className="min-w-0 flex-1 truncate">{project.name}</span>
              {project.slug === currentSlug && (
                <Check className="size-3.5 shrink-0 text-accent" aria-hidden="true" />
              )}
            </DropdownMenuItem>
          ))}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function capitalize(value: string): string {
  return value ? value[0]!.toUpperCase() + value.slice(1) : value
}
