import {
  Suspense,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react'
import { Outlet, useLocation, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ApiError } from '@/api/client'
import { AppSidebar } from '@/components/app-sidebar'
import { BranchProvider } from '@/components/branch-context'
import { CommandPaletteProvider } from '@/components/command-palette'
import { ActiveProjectContext } from '@/components/active-project-context'
import { ErrorBoundary, RouteErrorBoundary } from '@/components/error-boundary'
import { ErrorState } from '@/components/error-state'
import { MAIN_CONTENT_ID } from '@/components/landmarks'
import { BranchStrip, TopBar } from '@/components/top-bar'
import { TweaksPanelProvider } from '@/components/tweaks-panel'
import { LazyDemoScenarioProvider } from '@/demo/LazyDemoScenarioProvider'
import { DemoBannerPlaceholder } from '@/demo/DemoBannerPlaceholder'
import { ShellSkeleton } from '@/components/states/skeletons'
import { ProjectNotFound } from '@/components/states/project-not-found'
import { OrgSuspendedState } from '@/components/states/org-suspended'
import { StepInBanner } from '@/components/shell/step-in-banner'
import { PublicDemoBanner } from '@/components/shell/public-demo-banner'
import { useActiveOrg } from '@/components/active-org-context'
import { extensionShellGates } from '@/extensions'
import { orgIsSuspended } from '@/lib/orgStatus'
import {
  DocumentEntityTitleContext,
  EDIT_PAGE_TITLE_PREFIX,
  ShellChromeContext,
} from '@/components/shell-chrome-context'
import { ProjectEventStreamProvider } from '@/realtime/ProjectEventStreamProvider'
import { currentOrgSlug, projectHomePath, projectPath, resolveNavLocation, stripOrgPrefix } from '@/lib/navigation'
import { navCrumb, type Crumb } from '@/components/shell/crumbs'
import { useShellShortcuts } from '@/components/shell/shell-shortcuts'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { projectQueryOptions, projectsQueryOptions } from '@/lib/queryKeys'
import { lazyWithReload } from '@/lib/lazyWithReload'
import { forgetLastProjectSlug } from '@/lib/lastProjectSlug'

// Demo-only chrome, rendered for a demo project alone. Loaded on demand so the
// product tour, chapter picker and reset dialog stay out of every other
// user's first load (#194).
const DemoBanner = lazyWithReload(() =>
  import('@/demo/DemoBanner').then((m) => ({ default: m.DemoBanner })),
)
const DemoScenarioStrip = lazyWithReload(() =>
  import('@/demo/DemoScenarioStrip').then((m) => ({ default: m.DemoScenarioStrip })),
)
// The way back to the Get-started checklist (#250). It only renders on a
// URL tagged `?onboarding=…`, so its chunk is fetched for those alone.
const OnboardingReturnBar = lazyWithReload(() =>
  import('@/components/onboarding-return-bar').then((m) => ({ default: m.OnboardingReturnBar })),
)
// The activity feed is a side rail, not the page: loading it after the shell
// keeps its feed rendering out of the entry chunk. Nothing renders while it
// loads, the same as a closed rail.
const ActivityPanel = lazyWithReload(() =>
  import('@/components/activity-panel').then((m) => ({ default: m.ActivityPanel })),
)
// The `?` shortcut sheet, fetched on the first `?` alone.
const ShortcutsDialog = lazyWithReload(() => import('@/components/shell/shortcuts-dialog'))

const ACTIVITY_STORAGE_KEY = 'tripl-activity-open'

function useActivityOpen() {
  const [open, setOpen] = useState(() => {
    try {
      const stored = localStorage.getItem(ACTIVITY_STORAGE_KEY)
      if (stored === '0') return false
      if (stored === '1') return true
    } catch {
      /* ignore */
    }
    return typeof window !== 'undefined' ? window.innerWidth >= ACTIVITY_INLINE_MIN_WIDTH : true
  })
  useEffect(() => {
    try {
      localStorage.setItem(ACTIVITY_STORAGE_KEY, open ? '1' : '0')
    } catch {
      /* ignore */
    }
  }, [open])
  return [open, setOpen] as const
}

// At/above this width the activity rail sits inline in the flex flow (it is
// also the rail's default-open threshold above). Below it the 304px rail
// collapses to an off-canvas drawer toggled from the top bar. It was 1280px,
// and at 1440 — the commonest laptop — the rail and the sidebar left the Events
// table 830px, ten of its seventeen columns off-screen.
const ACTIVITY_INLINE_MIN_WIDTH = 1600
const ACTIVITY_INLINE_QUERY = `(min-width: ${ACTIVITY_INLINE_MIN_WIDTH}px)`

// At/above this width the sidebar is pinned in flow; below it, it is a drawer.
// Pinned from 768px it left a tablet ~528px of page. Mirrors the
// `lg:` utilities on the sidebar wrapper, the backdrop and the hamburger.
const NAV_PERSISTENT_QUERY = '(min-width: 1024px)'

/** The drawer's id, for the hamburger's `aria-controls`. */
const SIDEBAR_ID = 'app-sidebar'

/**
 * Subscribe to a CSS media query. Uses `useSyncExternalStore` so the value is
 * read consistently and updates on viewport changes without tripping the
 * `set-state-in-effect` lint. When `matchMedia` is unavailable (jsdom/SSR) it
 * answers `fallback`: narrow for the rail, so it never blocks the content
 * column; wide for the sidebar, so it is never made inert unmeasured.
 */
function useMediaQuery(query: string, fallback = false): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
        return () => {}
      }
      const mql = window.matchMedia(query)
      mql.addEventListener('change', onChange)
      return () => mql.removeEventListener('change', onChange)
    },
    [query],
  )
  const getSnapshot = () =>
    typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia(query).matches
      : fallback
  return useSyncExternalStore(subscribe, getSnapshot, () => fallback)
}

function focusedElement(): HTMLElement | null {
  const active = document.activeElement
  return active instanceof HTMLElement ? active : null
}

/** First control inside a drawer, for moving focus into it on open. */
function firstFocusable(root: HTMLElement | null): HTMLElement | null {
  return root?.querySelector<HTMLElement>(
    'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
  ) ?? null
}

type Crumbs = {
  crumbs: Crumb[]
  title: string
  /**
   * An editor route: once the page names itself `editPageTitle(name)`, the
   * entity becomes a crumb and this word the title ("Metrics › Active
   * Sessions › Edit", #246).
   */
  entityAction?: string
}

/** A detail route's own crumb before its entity has loaded. */
const DETAIL_PENDING_TITLE = ''

// Workspace-level surfaces: the portfolio dashboard reachable at three paths.
// None of them is inside a project, so none gets a project root crumb — `/`
// already rendered a bare "Overview" and `/workspace` is the identical page.
const WORKSPACE_PATHS: readonly string[] = ['/', '/workspace', '/projects']
// The page's own name, the sidebar's and the palette's: one name per route.
// It used to be "Overview" here, a word a project page also uses.
const WORKSPACE_TITLE = 'All projects'

// Concepts sits below the sidebar divider rather than inside the Plan / Observe
// / Govern nav, so `resolveNavLocation` cannot name it. Without this it fell
// through to the catch-all and claimed to be "Overview". The
// area label matches the page's own eyebrow (ConceptsPage `PageHead`).
const CONCEPTS_AREA = 'Help & reference'

function resolveCrumbs(fullPathname: string, slug?: string, projectName?: string): Crumbs {
  // `/o/{org}/p/…` reads as `/p/…`, and `/o/{org}` as the workspace (F20 PR7).
  const pathname = stripOrgPrefix(fullPathname)
  if (WORKSPACE_PATHS.includes(pathname)) return { crumbs: [], title: WORKSPACE_TITLE }
  if (pathname.startsWith('/settings') || pathname.startsWith('/data-sources')) {
    return { crumbs: [], title: 'Settings' }
  }
  if (pathname.startsWith('/auth')) return { crumbs: [], title: 'Sign in' }

  // No invented root crumb: a path outside any project simply has no project
  // segment. The literal placeholder this used to emit read as an untranslated
  // template leaking into the UI.
  // Plain strings are nav groups (not pages); a surface passes a Crumb with
  // its link. The project crumb opens the project's home.
  const withProject = (...rest: (string | Crumb)[]): Crumb[] => {
    const trail = rest.map((crumb) => (typeof crumb === 'string' ? { label: crumb } : crumb))
    if (!projectName) return trail
    return [{ label: projectName, ...(slug ? { to: projectHomePath(slug) } : {}) }, ...trail]
  }
  const nav = (label: string): Crumb => navCrumb(slug, label)

  // Detail surfaces carry their nav area so the breadcrumb reads
  // "project › Area › Page › <entity>"; the page names the entity through
  // usePageTitle, and until it has, the crumb stays blank rather than flash
  // a generic "Detail"; Layout then shows the area's page as the
  // title. An event's catalog detail is served under
  // /monitoring/event/<id> (the canonical event route), but it belongs to
  // Plan › Events — only project-total/event-type signal detail falls through
  // to the generic branch. Check the event scope first.
  if (pathname.includes('/monitoring/event/') || pathname.includes('/events/detail/')) {
    return { crumbs: withProject('Plan', nav('Events')), title: DETAIL_PENDING_TITLE }
  }
  // Catalog-metric drilldowns belong to the Metrics surface, so their
  // breadcrumb reads "… › Observe › Metrics" (matching the metrics list nav).
  // Check before the generic /monitoring/ branch.
  if (pathname.includes('/monitoring/metric/')) {
    return { crumbs: withProject('Observe', nav('Metrics')), title: DETAIL_PENDING_TITLE }
  }
  // What is left — event-type and project-total volume drilldowns — is named
  // from the entity, not from the route the reader happened to arrive by: the
  // trail said "Observe › Anomalies" even when the page was opened from the
  // sidebar or an event type (#241). An event type's volume sits under
  // "Plan › Event types", where the nav files Event types; the project total
  // is its own page ("Total volume").
  if (pathname.includes('/monitoring/event-type/')) {
    return { crumbs: withProject('Plan', nav('Event types')), title: DETAIL_PENDING_TITLE }
  }
  if (pathname.includes('/monitoring/')) {
    return { crumbs: withProject('Observe'), title: DETAIL_PENDING_TITLE }
  }
  // One branch: "Plan › Plan branches › <name>", the page naming the branch
  // once it has loaded (#243). The bare list keeps its nav crumb.
  if (/^\/p\/[^/]+\/branches\/[^/]+/.test(pathname)) {
    return { crumbs: withProject('Plan', nav('Plan branches')), title: DETAIL_PENDING_TITLE }
  }
  // An alert rule's history: "Observe › Alerting › Rules › <rule>", the tab
  // the rule lives on, instead of "Observe › <rule>" (#241, #238).
  if (/^\/p\/[^/]+\/monitors\/[^/]+/.test(pathname)) {
    const alerting = nav('Alerting')
    const rules: Crumb = alerting.to ? { label: 'Rules', to: `${alerting.to}?section=monitors` } : { label: 'Rules' }
    return { crumbs: withProject('Observe', alerting, rules), title: DETAIL_PENDING_TITLE }
  }
  // Metric and fact-table editors name themselves under the Metrics surface
  // instead of passing for the list: "Metrics › New metric", "Metrics › Edit
  // metric", "Metrics › Fact tables › Edit fact table" (#246).
  const metricsSub = /^\/p\/[^/]+\/metrics\/(.+)$/.exec(pathname)?.[1]
  const factTables: Crumb = slug
    ? { label: 'Fact tables', to: projectPath(currentOrgSlug(), slug, '/metrics/fact-tables') }
    : { label: 'Fact tables' }
  if (metricsSub === 'new') {
    return { crumbs: withProject('Observe', nav('Metrics')), title: 'New metric' }
  }
  if (metricsSub === 'fact-tables/new') {
    return { crumbs: withProject('Observe', nav('Metrics'), factTables), title: 'New fact table' }
  }
  if (metricsSub && /^fact-tables\/[^/]+\/edit$/.test(metricsSub)) {
    return {
      crumbs: withProject('Observe', nav('Metrics'), factTables),
      title: 'Edit fact table',
      entityAction: 'Edit',
    }
  }
  if (metricsSub && /^[^/]+\/edit$/.test(metricsSub)) {
    return { crumbs: withProject('Observe', nav('Metrics')), title: 'Edit metric', entityAction: 'Edit' }
  }

  // Map the route to its grouped-nav area (Plan / Observe / Govern / Connect)
  // using the same model the sidebar renders from.
  const navLocation = slug ? resolveNavLocation(slug, pathname) : null
  if (navLocation) {
    // A sub-surface names itself: the nav item it matched is its parent, not the
    // page. Without the leaf, Detection settings presented itself as Anomalies.
    // `leaf` is absent everywhere else, so nothing else moves.
    return navLocation.leaf
      ? { crumbs: withProject(navLocation.area, nav(navLocation.label)), title: navLocation.leaf }
      : { crumbs: withProject(navLocation.area), title: navLocation.label }
  }

  if (pathname.endsWith('/concepts')) {
    return { crumbs: withProject(CONCEPTS_AREA), title: 'Concepts' }
  }
  if (pathname.includes('/settings')) {
    return { crumbs: withProject(), title: 'Settings' }
  }
  // Nothing claimed this path, which is exactly what the catch-all route renders
  // NotFoundPage for — so the trail says so instead of naming a page ("Overview")
  // the user is not on.
  return { crumbs: withProject(), title: 'Not found' }
}

/**
 * Full-viewport stand-in for the app shell, used for the project lookup error
 * and the project-not-found state. Keeps the app background/colour so neither
 * reads as a broken page. `justify-center-safe`: the not-found state lists
 * projects and can outgrow a phone screen, and plain centring clipped its top.
 */
function ShellFallback({ children }: { children: ReactNode }) {
  return (
    <div
      className="flex h-screen flex-col items-center justify-center-safe overflow-y-auto px-6 py-8 text-body supports-[height:100dvh]:h-dvh bg-background text-fg-secondary"
    >
      {children}
    </div>
  )
}

export default function Layout() {
  const location = useLocation()
  const { slug } = useParams()
  const activeOrg = useActiveOrg()
  const [activityOpen, setActivityOpen] = useActivityOpen()

  // A page may ask for the rail to stay out of its way (the 404).
  const [railSuppressed, setRailSuppressed] = useState(false)
  // `?` opens the shortcut sheet; `c` presses the page's "New …".
  const [shortcutsOpen, setShortcutsOpen] = useState(false)
  const openShortcuts = useCallback(() => setShortcutsOpen(true), [])
  useShellShortcuts({ onOpenHelp: openShortcuts })
  // A detail page names its entity here (usePageTitle); null keeps the route's.
  const [pageTitle, setPageTitle] = useState<string | null>(null)
  // The same name reaches the browser-tab title.
  const setDocumentEntityTitle = useContext(DocumentEntityTitleContext)
  const shellChrome = useMemo(
    () => ({
      suppressActivityRail: setRailSuppressed,
      setPageTitle: (next: string | null) => {
        setPageTitle(next)
        setDocumentEntityTitle(next)
      },
    }),
    [setDocumentEntityTitle],
  )

  // Below the inline width the rail would squeeze the content column, so it
  // collapses to an off-canvas drawer with its own open state (mirroring the
  // sidebar's drawer). Above it, `activityOpen` drives the inline rail. A
  // single top-bar toggle drives whichever mode is active.
  const isWideActivity = useMediaQuery(ACTIVITY_INLINE_QUERY)
  const isWideNav = useMediaQuery(NAV_PERSISTENT_QUERY, true)
  const [activityDrawerOpen, setActivityDrawerOpen] = useState(false)
  const activityVisible = (isWideActivity ? activityOpen : activityDrawerOpen) && !railSuppressed

  // Below `lg`, the sidebar slides off-canvas; the hamburger in TopBar toggles
  // it. Above `lg`, this flag has no visual effect (the `lg:*` utilities pin
  // the sidebar to static flow regardless).
  const [mobileNavOpen, setMobileNavOpen] = useState(false)

  // Drawers behave like the modal they look like: focus moves in on
  // open, Escape closes, and closing by hand hands focus back to the button
  // that opened it. While one is open the rest of the shell is `inert`, which
  // is also what keeps Tab inside it.
  const drawerOpenerRef = useRef<HTMLElement | null>(null)
  const returnFocusRef = useRef(false)
  const sidebarRef = useRef<HTMLDivElement | null>(null)
  const activityDrawerRef = useRef<HTMLDivElement | null>(null)
  const openMobileNav = useCallback(() => {
    drawerOpenerRef.current = focusedElement()
    setMobileNavOpen(true)
  }, [])
  const closeDrawers = useCallback(() => {
    returnFocusRef.current = true
    setMobileNavOpen(false)
    setActivityDrawerOpen(false)
  }, [])
  const toggleActivity = useCallback(() => {
    if (isWideActivity) {
      setActivityOpen((o) => !o)
      return
    }
    if (activityDrawerOpen) {
      closeDrawers()
    } else {
      drawerOpenerRef.current = focusedElement()
      setActivityDrawerOpen(true)
    }
  }, [activityDrawerOpen, closeDrawers, isWideActivity, setActivityOpen])

  const navDrawerActive = mobileNavOpen && !isWideNav
  const activityDrawerActive = activityDrawerOpen && !isWideActivity && !railSuppressed
  const drawerActive = navDrawerActive || activityDrawerActive

  useEffect(() => {
    if (navDrawerActive) firstFocusable(sidebarRef.current)?.focus()
  }, [navDrawerActive])
  useEffect(() => {
    if (activityDrawerActive) firstFocusable(activityDrawerRef.current)?.focus()
  }, [activityDrawerActive])
  useEffect(() => {
    if (drawerActive || !returnFocusRef.current) return
    returnFocusRef.current = false
    const opener = drawerOpenerRef.current
    if (opener?.isConnected) opener.focus()
  }, [drawerActive])
  useEffect(() => {
    if (!drawerActive) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      closeDrawers()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [drawerActive, closeDrawers])

  // Close the drawer when the route changes. Using the React-documented
  // "derived state from props" pattern (setState during render with a prior-
  // value check) avoids both `react-hooks/set-state-in-effect` and
  // `react-hooks/refs`.
  const [lastPathname, setLastPathname] = useState(location.pathname)
  if (lastPathname !== location.pathname) {
    setLastPathname(location.pathname)
    if (mobileNavOpen) setMobileNavOpen(false)
    if (activityDrawerOpen) setActivityDrawerOpen(false)
  }

  // A client-side navigation announces nothing and leaves focus on the link
  // that was followed, so a keyboard user had to walk the rest of the sidebar
  // to reach the new page. Move focus to the content — unless it is
  // already there: a tab strip inside the page changes the path too, and must
  // keep its own focus. Skipped on the first render, which is a page load.
  const mainRef = useRef<HTMLElement | null>(null)
  const firstPathRef = useRef(true)
  useEffect(() => {
    if (firstPathRef.current) {
      firstPathRef.current = false
      return
    }
    const main = mainRef.current
    if (!main || main.contains(document.activeElement)) return
    main.focus({ preventScroll: true })
  }, [location.pathname])

  // When the viewport grows into the inline range, drop any open drawer so the
  // rail doesn't linger as an overlay on top of its own inline copy. Same
  // render-time "adjust state when a value changes" pattern as above.
  const [lastIsWideActivity, setLastIsWideActivity] = useState(isWideActivity)
  if (lastIsWideActivity !== isWideActivity) {
    setLastIsWideActivity(isWideActivity)
    if (isWideActivity && activityDrawerOpen) setActivityDrawerOpen(false)
  }

  const projectsQuery = useQuery(projectsQueryOptions())
  const projects = projectsQuery.data ?? []
  const activeProject = projects.find((p) => p.slug === slug)

  // The project endpoint, asked IN PARALLEL with the list rather than after it.
  // The list carries per-project summary counts and is the slowest request the
  // shell makes; a deep link used to wait for all of it before anything —
  // sidebar, top bar or the page's own queries — could start. Whichever answer
  // names the project first releases the shell. It also settles a slug the list
  // does not know: the list hides demos that are still seeding and can lag a
  // project created moments ago. So it is asked only while the list is still in
  // flight or does not name the slug; navigating between pages of a project the
  // cached list already holds costs no request. The key is the one the project
  // pages already read, so they pay nothing extra.
  const confirmProject = useQuery({
    ...projectQueryOptions(slug),
    // A pending list names nothing yet, so this also covers the deep link.
    enabled: !!slug && !activeProject,
    retry: false,
    // Rendered below as not-found or a retryable error; no toast on top.
    meta: SILENT_ERROR_META,
  })
  // The same resolution ActiveProjectContext hands the pages: the list's row,
  // else the project endpoint's answer (a deep link whose list has not landed,
  // or a project the list does not show yet).
  const project = activeProject ?? confirmProject.data
  const projectKnown = !!project

  // Deciding this HERE, before the shell mounts, is what stops an invented slug
  // rendering a complete, working-looking project behind a dozen 404ing requests
  // — the sidebar, activity rail, event stream and the routed page
  // all fan out from this component.
  //
  // Only a 404/403 means "no such project". Anything else — a 5xx, the network —
  // says nothing about the slug and is offered as a retry, not as a 404 page
  // (#194). Both wait for the list, which may still name the project.
  const confirmError = confirmProject.error
  const confirmSaysMissing =
    confirmError instanceof ApiError && (confirmError.status === 404 || confirmError.status === 403)
  const listSettled = !projectsQuery.isPending
  const projectMissing = !!slug && !projectKnown && listSettled && confirmSaysMissing
  const projectLookupFailed =
    !!slug && !projectKnown && listSettled && confirmProject.isError && !confirmSaysMissing
  const projectResolving = !!slug && !projectKnown && !projectMissing && !projectLookupFailed

  // A project the server does not show this user (deleted, or they are not a
  // member) must stop being the remembered "last project", or Settings and
  // every later visit keep steering back to a 404.
  useEffect(() => {
    if (projectMissing && slug) forgetLastProjectSlug(slug)
  }, [projectMissing, slug])

  const { crumbs, title, entityAction } = useMemo(
    () => resolveCrumbs(location.pathname, slug, project?.name ?? slug),
    [location.pathname, project?.name, slug],
  )
  // A detail route whose entity has not named itself (still loading, or it
  // failed to load) promotes its last crumb to the title: "Plan › Events"
  // rather than "Plan › Events ›" with nothing after the chevron, and a page
  // name on phones, where the crumbs are hidden.
  const entityTitle = pageTitle ?? title
  // An editor that has named its entity reads "… › <entity> › Edit".
  const editedEntity =
    entityAction && pageTitle?.startsWith(EDIT_PAGE_TITLE_PREFIX)
      ? pageTitle.slice(EDIT_PAGE_TITLE_PREFIX.length)
      : null
  const headerCrumbs = editedEntity
    ? [...crumbs, { label: editedEntity }]
    : entityTitle ? crumbs : crumbs.slice(0, -1)
  const headerTitle = editedEntity && entityAction
    ? entityAction
    : entityTitle || (crumbs[crumbs.length - 1]?.label ?? '')

  // A suspended organization (F20) refuses every request inside it, so the
  // shell would only be a wall of failing panels: say what happened instead.
  // Known from the membership when the session carries its status, otherwise
  // from the server's refusal of the first request.
  if (orgIsSuspended(activeOrg.membership, projectsQuery.error, confirmProject.error)) {
    return (
      <OrgSuspendedState
        orgName={activeOrg.membership?.name ?? activeOrg.slug ?? 'This organization'}
        otherOrgs={activeOrg.orgs.filter(
          (org) => org.slug !== activeOrg.slug && org.status !== 'suspended',
        )}
      />
    )
  }

  // An extension may replace the shell when the shell's own requests are
  // refused: an organization that requires single sign-on refuses a session
  // that did not come through its identity provider, on every request inside
  // it, so it offers that sign-in instead of a wall of failing panels.
  for (const gate of extensionShellGates) {
    const screen = gate({
      errors: [projectsQuery.error, confirmProject.error],
      orgName: activeOrg.membership?.name ?? activeOrg.slug ?? 'This organization',
      orgSlug: activeOrg.slug,
      returnTo: `${location.pathname}${location.search}${location.hash}`,
      otherOrgs: activeOrg.orgs.filter(
        (org) => org.slug !== activeOrg.slug && org.status !== 'suspended',
      ),
    })
    if (screen) return screen
  }

  // Hold the shell until the slug is resolved. Everything below fans out
  // project-scoped requests the moment it mounts, so rendering optimistically is
  // what produced the doomed fan-out in the first place.
  if (projectResolving) {
    // The shell's shape, not one grey sentence on a blank screen (#237).
    return <ShellSkeleton label="Loading project…" />
  }
  if (projectLookupFailed) {
    return (
      <ShellFallback>
        <div className="w-full max-w-lg">
          <ErrorState
            title="Could not open this project"
            description="The server did not answer whether this project exists. This is usually temporary."
            error={confirmProject.error}
            onRetry={() => {
              void confirmProject.refetch()
              if (projectsQuery.isError) void projectsQuery.refetch()
            }}
          />
        </div>
      </ShellFallback>
    )
  }
  if (projectMissing) {
    return (
      <ShellFallback>
        <ProjectNotFound slug={slug ?? ''} projects={projects} />
      </ShellFallback>
    )
  }

  return (
    <BranchProvider slug={slug ?? null}>
    <ProjectEventStreamProvider slug={slug}>
    {/* Holds the coached demo scenario across navigations: the scan the user
        started keeps being watched while they walk to the metrics catalog.
        Inert for every non-demo project, which never downloads its model. */}
    <LazyDemoScenarioProvider project={project}>
    <ActiveProjectContext.Provider value={project}>
    <ShellChromeContext.Provider value={shellChrome}>
    <TweaksPanelProvider>
      <CommandPaletteProvider>
        {/* `dvh`, not `vh`: on mobile Safari and Chrome 100vh is the LARGE
            viewport, so the sidebar footer (Sign out) and the last rows of every
            page sat under the browser toolbar. */}
        <div
          className="relative flex h-screen overflow-hidden supports-[height:100dvh]:h-dvh bg-background text-fg"
        >
          {/* First tab stop on every page: without it a keyboard-only user
              walks all 27 sidebar stops before reaching page content. */}
          <a href={`#${MAIN_CONTENT_ID}`} className="skip-link">
            Skip to main content
          </a>

          {/* Sidebar: in flex flow on lg+, a drawer below lg. Off-canvas it is
              `inert`, or a keyboard user tabbed through ~27 invisible links. */}
          <div
            id={SIDEBAR_ID}
            ref={sidebarRef}
            inert={(!isWideNav && !mobileNavOpen) || activityDrawerActive}
            className={
              'fixed inset-y-0 left-0 z-(--z-drawer) transition-transform duration-200 ease-out lg:static lg:translate-x-0 ' +
              (mobileNavOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0')
            }
          >
            {/* As a drawer it closes rather than collapsing into a 52px rail
                over a blurred page (#238). */}
            <AppSidebar drawer={!isWideNav} onCloseDrawer={closeDrawers} />
          </div>

          {/* Backdrop for the mobile drawer. */}
          {mobileNavOpen && (
            <button
              type="button"
              aria-label="Close navigation"
              tabIndex={-1}
              onClick={closeDrawers}
              className="fixed inset-0 z-(--z-backdrop) bg-black/40 backdrop-blur-[2px] lg:hidden"
            />
          )}

          <div className="flex min-w-0 flex-1 flex-col" inert={drawerActive}>
            {/* A platform admin's read-only step-in to this organization. */}
            <StepInBanner />
            <PublicDemoBanner />
            <TopBar
              title={headerTitle}
              crumbs={headerCrumbs}
              projectSlug={slug}
              projectName={project?.name}
              activityOpen={activityVisible}
              onToggleActivity={railSuppressed ? undefined : toggleActivity}
              mobileNavOpen={mobileNavOpen}
              mobileNavId={SIDEBAR_ID}
              onOpenMobileNav={openMobileNav}
            />
            <BranchStrip slug={slug} />

            <div className="flex flex-1 overflow-hidden">
              <div className="relative min-w-0 flex-1 overflow-y-auto pb-[env(safe-area-inset-bottom)]">
                <div className="p-3 sm:p-5 lg:p-8">
                  {/* Persistent demo marker across every surface of a demo
                      project — synthetic/local data, recipe version, freshness,
                      and creator/owner reset + delete controls. */}
                  {project?.is_demo && (
                    // Its own boundary: this chrome sits outside the route
                    // boundary, so a chunk that fails to load (or a render
                    // error) here used to reach main.tsx's and blank the whole
                    // app. The demo chrome simply goes missing instead.
                    <ErrorBoundary fallback={() => null}>
                      <Suspense fallback={<DemoBannerPlaceholder />}>
                        {/* The placeholder holds the banner's box while its
                            chunk loads, so the page does not jump down when it
                            lands (#251). */}
                        {/* One row, not two stacked cards: the coached
                            scenario sits INSIDE the banner's row. Gated with
                            the banner, but it decides for itself whether there
                            is anything left to coach. Passed as an element so
                            each keeps its own lazy chunk. On the in-project 404
                            (railSuppressed) there is nothing to coach, so the
                            strip is left out (#251). */}
                        {railSuppressed ? (
                          <DemoBanner project={project} />
                        ) : (
                          <DemoBanner project={project} scenario={<DemoScenarioStrip />} />
                        )}
                      </Suspense>
                    </ErrorBoundary>
                  )}
                  {/* The skip link's landmark — and it starts HERE, below the
                      demo chrome, not around it. Both blocks above are shell
                      furniture, and on a demo project they put six controls
                      between the landmark and the page's own first one on the
                      captured stand: "What's simulated", "Tour & chapters",
                      "Reset" and "Delete" (those two owner-only), the strip's
                      CTA, "Dismiss". A keyboard user who asked to skip
                      the shell was therefore walked onto the demo's DESTRUCTIVE
                      Delete before reaching the page they had opened.
                      Nothing is hidden: the chrome is still in the
                      tab order, reached forwards from the top bar or backwards
                      from here.

                      It is the `<main>` landmark too. The top bar used to sit
                      inside `<main>`, so the landmark opened on chrome and the
                      skip target was a nested div.

                      This element's box is ALSO the content column — the page
                      gutter is padding on the parent, so this box starts and
                      ends exactly where the cards do. ScenarioCoachMark bounds
                      its popovers to it and depends on that.

                      `scroll-mt-*` mirrors that parent padding because jumping
                      to a fragment scrolls its top flush to the viewport: with
                      no scroll margin the skip link would eat the page's own top
                      gutter (32px at lg) on EVERY surface, demo or not. Matched
                      to the padding, a non-demo page does not move at all. */}
                  <main
                    id={MAIN_CONTENT_ID}
                    ref={mainRef}
                    tabIndex={-1}
                    className="scroll-mt-3 focus:outline-none sm:scroll-mt-5 lg:scroll-mt-8"
                  >
                    {projectsQuery.isError && (
                      <div className="mb-6">
                        <ErrorState
                          title="Backend is unavailable"
                          description="The frontend is up, but the initial API request failed."
                          error={projectsQuery.error}
                          onRetry={() => {
                            void projectsQuery.refetch()
                          }}
                        />
                      </div>
                    )}
                    {/* Tagged by a Get-started step link; the bar leads back
                        to the checklist. */}
                    {location.search.includes('onboarding=') && (
                      <ErrorBoundary fallback={() => null}>
                        <Suspense fallback={null}>
                          <OnboardingReturnBar className="mb-4" />
                        </Suspense>
                      </ErrorBoundary>
                    )}
                    {/* A page that throws is replaced by an error card here;
                        the sidebar, top bar and toasts stay alive. */}
                    <RouteErrorBoundary>
                      <Outlet />
                    </RouteErrorBoundary>
                  </main>
                </div>
              </div>

              {/* Activity rail, inline from ACTIVITY_INLINE_MIN_WIDTH up. */}
              {isWideActivity && (
                <Suspense fallback={null}>
                  <ActivityPanel open={activityOpen && !railSuppressed} slug={slug} inline />
                </Suspense>
              )}
            </div>
          </div>

          {/* Below the inline width the rail is a drawer, outside the content
              column so the column can go inert behind it. */}
          {activityDrawerActive && (
            <>
              <button
                type="button"
                aria-label="Close activity feed"
                tabIndex={-1}
                onClick={closeDrawers}
                className="fixed inset-0 z-(--z-backdrop) bg-black/40 backdrop-blur-[2px]"
              />
              <div
                ref={activityDrawerRef}
                className="fixed inset-y-0 right-0 z-(--z-drawer) pb-[env(safe-area-inset-bottom)] shadow-xl bg-bg-sunken"
              >
                <Suspense fallback={null}>
                  <ActivityPanel open slug={slug} onClose={closeDrawers} />
                </Suspense>
              </div>
            </>
          )}
        </div>
        {shortcutsOpen && (
          <ErrorBoundary fallback={() => null}>
            <Suspense fallback={null}>
              <ShortcutsDialog open onOpenChange={setShortcutsOpen} />
            </Suspense>
          </ErrorBoundary>
        )}
      </CommandPaletteProvider>
    </TweaksPanelProvider>
    </ShellChromeContext.Provider>
    </ActiveProjectContext.Provider>
    </LazyDemoScenarioProvider>
    </ProjectEventStreamProvider>
    </BranchProvider>
  )
}
