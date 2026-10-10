import {
  Suspense,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
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
import { MAIN_CONTENT_ID, SIDEBAR_ID } from '@/components/landmarks'
import { BranchStrip, TopBar } from '@/components/top-bar'
import { TweaksPanelProvider } from '@/components/tweaks-panel'
import { LazyDemoScenarioProvider } from '@/demo/LazyDemoScenarioProvider'
import { DemoBannerPlaceholder } from '@/demo/DemoBannerPlaceholder'
import { ShellSkeleton } from '@/components/states/skeletons'
import { ProjectNotFound } from '@/components/states/project-not-found'
import { OrgSuspendedState } from '@/components/states/org-suspended'
import { ShellStandIn } from '@/components/states/shell-stand-in'
import { PublicDemoBanner } from '@/components/shell/public-demo-banner'
import { useActiveOrg } from '@/components/active-org-context'
import { extensionShellBanners, extensionShellGates } from '@/extensions'
import { orgIsSuspended } from '@/lib/orgStatus'
import { DocumentEntityTitleContext, ShellChromeContext } from '@/components/shell-chrome-context'
import { ProjectEventStreamProvider } from '@/realtime/ProjectEventStreamProvider'
import { resolveCrumbs, topBarHeading } from '@/components/shell/crumbs'
import { useShellShortcuts } from '@/components/shell/shell-shortcuts'
import { useMediaQuery } from '@/hooks/useMediaQuery'
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
const DemoGuideHost = lazyWithReload(() =>
  import('@/demo/DemoGuideHost').then((m) => ({ default: m.DemoGuideHost })),
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

/**
 * The stand-in for the app shell around the project lookup error and the
 * project-not-found state, in the app's background and body text so neither
 * reads as a broken page.
 */
function ShellFallback({ children }: { children: ReactNode }) {
  return <ShellStandIn className="text-body text-fg-secondary">{children}</ShellStandIn>
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
  // single top-bar toggle drives whichever mode is active. Unmeasured (no
  // `matchMedia`), the rail guesses narrow so it never blocks the content
  // column, and the sidebar guesses wide so it is never made inert.
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

  const routeCrumbs = useMemo(
    () => resolveCrumbs(location.pathname, slug, project?.name ?? slug),
    [location.pathname, project?.name, slug],
  )
  // What the page has named itself (usePageTitle) settles the trail's end.
  const { crumbs: headerCrumbs, title: headerTitle } = topBarHeading(routeCrumbs, pageTitle)

  // What an organization gate names, and the organizations it offers instead.
  const gateOrgName = activeOrg.membership?.name ?? activeOrg.slug ?? 'This organization'
  const gateOtherOrgs = activeOrg.orgs.filter(
    (org) => org.slug !== activeOrg.slug && org.status !== 'suspended',
  )

  // A suspended organization (F20) refuses every request inside it, so the
  // shell would only be a wall of failing panels: say what happened instead.
  // Known from the membership when the session carries its status, otherwise
  // from the server's refusal of the first request.
  if (orgIsSuspended(activeOrg.membership, projectsQuery.error, confirmProject.error)) {
    return <OrgSuspendedState orgName={gateOrgName} otherOrgs={gateOtherOrgs} />
  }

  // An extension may replace the shell when the shell's own requests are
  // refused: an organization that requires single sign-on refuses a session
  // that did not come through its identity provider, on every request inside
  // it, so it offers that sign-in instead of a wall of failing panels.
  for (const gate of extensionShellGates) {
    const screen = gate({
      errors: [projectsQuery.error, confirmProject.error],
      orgName: gateOrgName,
      orgSlug: activeOrg.slug,
      returnTo: `${location.pathname}${location.search}${location.hash}`,
      otherOrgs: gateOtherOrgs,
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
            {/* The extensions' banners (Enterprise: a platform admin's read-only
                step-in, the license notice). */}
            {extensionShellBanners.map((Banner, index) => (
              <Banner key={index} />
            ))}
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
                      project — synthetic/local data, freshness (the demo data
                      version is under "What's simulated"), and creator/owner
                      reset + delete controls. */}
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
                  {/* The demo guide, for the moments no coach mark speaks: a
                      step whose control is on another page, or not on screen.
                      Portalled, so where it sits here does not matter; its own
                      boundary, so a failure costs the guide and nothing else. */}
                  {project?.is_demo && !railSuppressed && (
                    <ErrorBoundary fallback={() => null}>
                      <Suspense fallback={null}>
                        <DemoGuideHost />
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
                      ends exactly where the cards do. The demo guide takes a
                      corner of it and depends on that; while the guide sits
                      in a bottom corner, the column ends with as much room as
                      the guide takes (`--demo-guide-clearance`), so a page's
                      last rows can always be scrolled out from under it.

                      `scroll-mt-*` mirrors that parent padding because jumping
                      to a fragment scrolls its top flush to the viewport: with
                      no scroll margin the skip link would eat the page's own top
                      gutter (32px at lg) on EVERY surface, demo or not. Matched
                      to the padding, a non-demo page does not move at all. */}
                  <main
                    id={MAIN_CONTENT_ID}
                    ref={mainRef}
                    tabIndex={-1}
                    className="scroll-mt-3 pb-[var(--demo-guide-clearance,0px)] focus:outline-none sm:scroll-mt-5 lg:scroll-mt-8"
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
