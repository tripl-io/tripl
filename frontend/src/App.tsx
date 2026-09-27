import { Suspense, useState, type ReactNode } from 'react'
import { Link, Navigate, Route, Routes, useLocation, useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { AuthProvider } from './components/auth-provider'
import { useAuth } from './components/auth-context'
import { ErrorState } from './components/error-state'
import { RouteErrorBoundary } from './components/error-boundary'
import { KeyedRoute } from './components/keyed-route'
import Layout from './components/Layout'
import { ThemeProvider } from './components/theme-provider'
import { Button } from './components/ui/button'
import { Toaster } from './components/ui/sonner'
import { PageSkeleton, ShellSkeleton, type PageSkeletonVariant } from './components/states/skeletons'
import {
  NOT_FOUND_TITLE_LABEL,
  entityTitleLabel,
  resolveEntityKind,
  resolveTitleFromPath,
  useDocumentTitle,
} from './hooks/useDocumentTitle'
import { DocumentEntityTitleContext } from './components/shell-chrome-context'
import { postLoginDestination } from './lib/authRedirect'
import { lazyWithReload } from './lib/lazyWithReload'
import { projectHomePath } from './lib/navigation'
import { projectsQueryOptions } from './lib/queryKeys'
// Static on purpose: the page is a few hundred bytes and the shell already
// renders its NotFoundState, so a lazy split bought nothing but a warning.
import NotFoundPage from './pages/NotFoundPage'

const AuthPage = lazyWithReload(() => import('./pages/AuthPage'))
const InvitePage = lazyWithReload(() => import('./pages/InvitePage'))
const MainPage = lazyWithReload(() => import('./pages/ProjectsPage'))
const EventsPage = lazyWithReload(() => import('./pages/EventsPage'))
const EventEditPage = lazyWithReload(() => import('./pages/events/EventForm'))
const EventBulkPage = lazyWithReload(() => import('./pages/events/EventBulkForm'))
const OverviewPage = lazyWithReload(() => import('./pages/OverviewPage'))
const MonitorDetailPage = lazyWithReload(() => import('./pages/MonitorDetailPage'))
const MonitoringDetailPage = lazyWithReload(() => import('./pages/MonitoringDetailPage'))
const ProjectSettingsPage = lazyWithReload(() => import('./pages/ProjectSettingsPage'))
const ProjectScansPage = lazyWithReload(() => import('./pages/ProjectScansPage'))
const ReconciliationPage = lazyWithReload(() => import('./pages/ReconciliationPage'))
const DuplicatesPage = lazyWithReload(() => import('./pages/DuplicatesPage'))
const AnomaliesPage = lazyWithReload(() => import('./pages/AnomaliesPage'))
const MetricsPage = lazyWithReload(() => import('./pages/metrics/MetricsPage'))
const MetricEditPage = lazyWithReload(() => import('./pages/metrics/MetricForm'))
const FactTableEditPage = lazyWithReload(() => import('./pages/fact-tables/FactTableForm'))
const CoveragePage = lazyWithReload(() => import('./pages/CoveragePage'))
const ConceptsPage = lazyWithReload(() => import('./pages/ConceptsPage'))
const SettingsArea = lazyWithReload(() => import('./pages/settings-area/SettingsArea'))

/**
 * Route-level loading: a page-shaped skeleton rather than "Loading page…" in
 * an empty column (#237 SH-23 group). The variant follows the page's shape,
 * so the header, stat strip and first card are where the page will draw them.
 */
function RouteFallback({ variant = 'list', label = 'Loading page…' }: {
  variant?: PageSkeletonVariant
  label?: string
}) {
  return <PageSkeleton variant={variant} label={label} />
}

/**
 * Suspense for one lazy page, keyed by the PAGE it renders.
 *
 * One policy for every route. An unkeyed boundary is reused across
 * navigations, and because router navigations run in a transition React keeps
 * the previous, already-resolved page on screen while the next chunk downloads
 * — with nothing saying anything is happening, so on a slow network a nav
 * click reads as ignored and gets clicked again. A boundary keyed by page
 * mounts fresh when the page changes and shows its fallback at once.
 *
 * The key names the page, not the route pattern: the events list and an
 * event's detail are two patterns serving ONE EventsPage, and a per-pattern key
 * would remount it (losing filters and scroll) on every row click. Metric
 * routes keep one key per form or tab, as before.
 */
function withSuspense(
  pageKey: string,
  element: ReactNode,
  variant: PageSkeletonVariant = 'list',
  label = 'Loading page…',
) {
  return (
    <Suspense key={pageKey} fallback={<RouteFallback variant={variant} label={label} />}>
      {element}
    </Suspense>
  )
}

function withMetricSuspense(routeKey: string, element: ReactNode, variant: PageSkeletonVariant = 'list') {
  return withSuspense(routeKey, element, variant, 'Loading metrics…')
}

/**
 * The session check. Kept as a quiet centred line rather than a shell
 * skeleton: it resolves to either the app or the sign-in card, and a skeleton
 * of the app would flash under /auth.
 */
function FullScreenFallback({ label }: { label: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex min-h-screen items-center justify-center bg-background px-6 text-body text-fg-tertiary"
    >
      {label}
    </div>
  )
}

function SessionFallback() {
  return <FullScreenFallback label="Checking session…" />
}

function SessionError() {
  const auth = useAuth()

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-6">
      <div className="w-full max-w-lg">
        <ErrorState
          title="Authentication unavailable"
          description="The frontend could not verify the current session."
          error={auth.error}
          onRetry={auth.refresh}
          retryLabel="Retry session check"
        />
      </div>
    </div>
  )
}

function RequireAuth({ children }: { children: ReactNode }) {
  const auth = useAuth()
  const location = useLocation()

  if (auth.status === 'loading') {
    return <SessionFallback />
  }
  if (auth.status === 'error') {
    return <SessionError />
  }
  if (auth.status === 'anonymous') {
    return <Navigate to="/auth" replace state={{ from: location }} />
  }
  return <>{children}</>
}

/**
 * A signed-in visitor on a link meant for someone without a session — an
 * invitation, a password reset. Bouncing them to `/` dropped the token and read
 * as a broken link (SHELL-16); this says who they are signed in as and lets
 * them sign out without leaving the URL.
 */
function SignedInInterstitial({ purpose }: { purpose: string }) {
  const auth = useAuth()
  const who = auth.user?.email ?? auth.user?.name ?? 'another account'
  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-6">
      <div
        className="w-full max-w-md space-y-4 rounded-card border p-6"
        style={{ borderColor: 'var(--border)', background: 'var(--surface)' }}
      >
        <h1 className="text-heading font-semibold">You are already signed in</h1>
        <p className="text-body" style={{ color: 'var(--fg-muted)' }}>
          You are signed in as <strong>{who}</strong>. Sign out to {purpose}.
        </p>
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            onClick={() => void auth.logout()}
            disabled={auth.isLoggingOut}
          >
            {auth.isLoggingOut ? 'Signing out…' : 'Sign out and continue'}
          </Button>
          <Button asChild variant="outline">
            <Link to="/">Back to the app</Link>
          </Button>
        </div>
      </div>
    </div>
  )
}

function AnonymousOnly({
  children,
  signedInPurpose,
}: {
  children: ReactNode
  /** Set for links that must not silently redirect a signed-in visitor. */
  signedInPurpose?: string
}) {
  const auth = useAuth()
  const location = useLocation()
  // Only someone who ARRIVED signed in gets the interstitial. Signing in on the
  // page itself — accepting the invitation, or logging in after a reset — is
  // the page doing its job, and goes on to the destination as before.
  const [arrivedSignedIn, setArrivedSignedIn] = useState<boolean | null>(null)
  if (auth.status === 'anonymous' && arrivedSignedIn !== false) setArrivedSignedIn(false)
  if (auth.status === 'authenticated' && arrivedSignedIn === null) setArrivedSignedIn(true)

  if (auth.status === 'loading') {
    return <SessionFallback />
  }
  if (auth.status === 'error') {
    return <SessionError />
  }
  if (auth.status === 'authenticated') {
    if (signedInPurpose && arrivedSignedIn !== false) {
      return <SignedInInterstitial purpose={signedInPurpose} />
    }
    return <Navigate to={postLoginDestination(location.state)} replace />
  }
  return <>{children}</>
}

/** /auth — a password-reset link keeps its token when someone is signed in. */
function AuthRoute() {
  const [searchParams] = useSearchParams()
  const resetting = searchParams.has('reset_token')
  return (
    <AnonymousOnly signedInPurpose={resetting ? 'reset the password' : undefined}>
      {withSuspense('auth', <AuthPage />, 'form')}
    </AnonymousOnly>
  )
}

function ProjectSettingsRedirect({ tab }: { tab: string }) {
  const { slug } = useParams<{ slug: string }>()
  return <Navigate to={`/p/${slug}/settings/${tab}`} replace />
}

function DataSourceRedirect() {
  const { dsId } = useParams<{ dsId: string }>()
  return <Navigate to={`/settings/data-sources/${dsId}`} replace />
}

/**
 * Legacy `/p/:slug/events/detail/:eventId` → canonical event monitoring detail.
 * The legacy URL mounted MonitoringDetailPage with no `:scope` segment, which
 * crashed scope resolution; redirecting keeps every entry point on the
 * canonical `/monitoring/event/:id` route.
 */
function EventDetailRedirect() {
  const { slug, eventId } = useParams<{ slug: string; eventId: string }>()
  return <Navigate to={`/p/${slug}/monitoring/event/${eventId}`} replace />
}

/**
 * `/p/:slug/monitors` → the Monitors section of Alerting.
 *
 * The standalone page rendered the same AlertRule rows the Alerting page
 * already owned, so it was merged in (tripl-89ps). The path stays reachable
 * because it is in bookmarks and in the "Mute or tune a rule" links written
 * before the merge. The per-rule detail at `/monitors/:monitorId` is NOT
 * redirected — it is the rule's fired history, which no section carries.
 */
function MonitorsRedirect() {
  const { slug } = useParams<{ slug: string }>()
  return <Navigate to={`/p/${slug}/alerting?section=monitors`} replace />
}

/**
 * Legacy fact-tables routes → the Fact tables tab under Metrics. Fact tables are
 * no longer a standalone top-level surface; these redirects keep existing links
 * and bookmarks working while preserving the `:slug` / `:factTableId` params.
 */
function FactTablesRedirect() {
  const { slug } = useParams<{ slug: string }>()
  return <Navigate to={`/p/${slug}/metrics/fact-tables`} replace />
}

/**
 * Legacy `/p/:slug/settings/scans[/:itemId]` → the top-level Scans surface.
 * Scans are an operational surface, not a settings tab. This redirect is
 * permanent: bookmarks, the activity feed's own deep links, and every doc
 * written before the move go through it.
 */
function ScansRedirect() {
  const { slug, itemId } = useParams<{ slug: string; itemId?: string }>()
  return <Navigate to={itemId ? `/p/${slug}/scans/${itemId}` : `/p/${slug}/scans`} replace />
}

/**
 * Bare `/p/:slug` → the project's home. It used to render the Events list, so
 * a project link from outside the app opened on a different page than every
 * in-app project entry (#250 JR-1).
 */
function ProjectHomeRedirect() {
  const { slug } = useParams<{ slug: string }>()
  return <Navigate to={projectHomePath(slug ?? '')} replace />
}

function FactTablesNewRedirect() {
  const { slug } = useParams<{ slug: string }>()
  return <Navigate to={`/p/${slug}/metrics/fact-tables/new`} replace />
}

function FactTableEditRedirect() {
  const { slug, factTableId } = useParams<{ slug: string; factTableId: string }>()
  return <Navigate to={`/p/${slug}/metrics/fact-tables/${factTableId}/edit`} replace />
}

const SETTINGS_STORAGE_KEY = 'tripl.settings'

/**
 * /settings index: resume the last-visited section if it's a known config path,
 * otherwise land on Members (the first workspace section).
 */
function SettingsIndexRedirect() {
  let last: string | null = null
  try {
    last = localStorage.getItem(SETTINGS_STORAGE_KEY)
  } catch {
    /* ignore */
  }
  const target = last && /^[a-z/-]+$/.test(last) ? last : 'members'
  return <Navigate to={`/settings/${target}`} replace />
}

/**
 * Auth-gated mount of the full-takeover Settings area for a given section.
 *
 * Ctrl+K here is served by the settings shell's own palette, not by the app's
 * `CommandPaletteProvider`: that one scopes itself with `useParams().slug`, no
 * /settings/* route declares one, and its fallback is `projects[0]` — so
 * wrapping the takeover in it searched a project the area was not bound to
 * while offering none of that project's destinations. See
 * components/settings/settings-palette.tsx.
 */
function Takeover({ section }: { section: string }) {
  return (
    <RequireAuth>
      {/* Its own boundary: a section that throws keeps the takeover (and a way
          out of it) instead of blanking the whole app. Reset on navigation. */}
      <RouteErrorBoundary>
        {/* A rail and section skeleton, not a word on a blank screen (ST-35). */}
        <Suspense fallback={<ShellSkeleton label="Loading settings…" />}>
          <SettingsArea section={section} />
        </Suspense>
      </RouteErrorBoundary>
    </RequireAuth>
  )
}

/** Instance section takeover — reads the owner-only section from the route. */
function TakeoverInstance() {
  const { instSection } = useParams<{ instSection: string }>()
  return <Takeover section={`instance/${instSection ?? 'runtime'}`} />
}

/**
 * Bare "/" entry. Most accounts have exactly one project, so landing on the
 * multi-project workspace dashboard is a redundant hop (UX-11 / UX-25). When
 * exactly one project exists we send the user straight into it; with 0 or 2+
 * projects we render the workspace dashboard unchanged. The dashboard stays
 * reachable for single-project users via the stable `/workspace` route, which
 * this redirect never bounces. Auth is already guaranteed by the enclosing
 * RequireAuth, and we reuse the app-wide `['projects']` query so the list is
 * shared (not refetched) with the sidebar and dashboard.
 */
function HomeRoute() {
  const projectsQuery = useQuery(projectsQueryOptions())

  // While the project list is still loading, show the in-Layout page fallback
  // so single-project users never flash the dashboard before redirecting.
  if (projectsQuery.isPending) {
    return <RouteFallback />
  }

  const projects = projectsQuery.data ?? []
  const [only] = projects
  if (projects.length === 1 && only) {
    return <Navigate to={projectHomePath(only.slug)} replace />
  }

  return withSuspense('workspace', <MainPage />)
}

/**
 * Single, always-mounted driver for the browser-tab title. Mounted at the app
 * root (inside AuthProvider, beside <Routes>), it reacts to EVERY navigation —
 * including the full-takeover Settings pages and /auth that render OUTSIDE the
 * app shell (Layout) — so every route gets a descriptive title and none is ever
 * left stale from the previous page.
 */
function DocumentTitle({ entityTitle }: { entityTitle: string | null }) {
  const { pathname } = useLocation()
  const { label, slug } = resolveTitleFromPath(pathname)
  // `enabled: false` — this only READS the shared `['projects']` cache that the
  // shell already populates; it must never fetch, because DocumentTitle is also
  // mounted on `/auth` where there is no session. Until the cache fills, an
  // unknown slug is indistinguishable from a not-yet-loaded one and the
  // path-derived title stands.
  const { data: projects } = useQuery({ ...projectsQueryOptions(), enabled: false })
  const slugIsUnknown = !!slug && !!projects && !projects.some((p) => p.slug === slug)

  // An invented slug must not be echoed back as if it named a real workspace —
  // the shell shows a not-found state for it, so the tab has to agree.
  //
  // A detail page that has loaded its entity names the tab after it instead,
  // "Screen View · Event type volume · tripl" (JR-33): three open monitoring
  // tabs used to read "Monitoring · acme · tripl" alike.
  const entityLabel =
    entityTitle && !slugIsUnknown
      ? entityTitleLabel(entityTitle, resolveEntityKind(pathname) ?? label)
      : null
  useDocumentTitle(
    entityLabel ?? (slugIsUnknown ? NOT_FOUND_TITLE_LABEL : label),
    entityLabel || slugIsUnknown ? undefined : slug,
  )
  return null
}

/**
 * Holds the entity name a detail page hands the shell (usePageTitle), for
 * DocumentTitle. `children` are created by App, so a name change re-renders
 * only this and the title driver, never the route tree.
 */
function DocumentTitleRoot({ children }: { children: ReactNode }) {
  const [entityTitle, setEntityTitle] = useState<string | null>(null)
  return (
    <DocumentEntityTitleContext.Provider value={setEntityTitle}>
      <DocumentTitle entityTitle={entityTitle} />
      {children}
    </DocumentEntityTitleContext.Provider>
  )
}

export default function App() {
  return (
    <ThemeProvider defaultTheme="dark" storageKey="tripl-ui-theme">
      <AuthProvider>
        <DocumentTitleRoot>
          <Routes>
            <Route path="/auth" element={<AuthRoute />} />
            {/* Redeeming an invitation needs a signed-out browser. Someone signed
                in is told so and offered a sign-out that keeps the link, rather
                than being bounced to / with the token dropped. */}
            <Route
              path="/invite/:token"
              element={
                <AnonymousOnly signedInPurpose="accept this invitation">
                  {withSuspense('invite', <InvitePage />, 'form')}
                </AnonymousOnly>
              }
            />
            {/* Full-takeover Settings area — its own viewport shell, so each route
                mounts OUTSIDE the app Layout (no app sidebar) but requires auth. */}
            <Route path="/settings" element={<SettingsIndexRedirect />} />
            <Route path="/settings/members" element={<Takeover section="members" />} />
            <Route path="/settings/api-keys" element={<Takeover section="api-keys" />} />
            <Route path="/settings/profile" element={<Takeover section="profile" />} />
            <Route path="/settings/security" element={<Takeover section="security" />} />
            <Route path="/settings/data-sources" element={<Takeover section="data-sources" />} />
            <Route path="/settings/data-sources/:dsId" element={<Takeover section="data-sources" />} />
            <Route path="/settings/project/general" element={<Takeover section="project/general" />} />
            <Route path="/settings/project/plan-rules" element={<Takeover section="project/plan-rules" />} />
            <Route path="/settings/project/members" element={<Takeover section="project/members" />} />
            <Route path="/settings/instance/:instSection" element={<TakeoverInstance />} />
            {/* Legacy → takeover redirects. */}
            <Route path="/settings/users" element={<Navigate to="/settings/members" replace />} />
            <Route path="/settings/account" element={<Navigate to="/settings/profile" replace />} />
            <Route path="/settings/runtime" element={<Navigate to="/settings/instance/runtime" replace />} />
            <Route path="/settings/ai" element={<Navigate to="/settings/instance/ai" replace />} />
            <Route path="/settings/email" element={<Navigate to="/settings/instance/email" replace />} />
            <Route path="/settings/storage" element={<Navigate to="/settings/instance/storage" replace />} />
            <Route
              path="/settings/observability"
              element={<Navigate to="/settings/instance/observability" replace />}
            />
            <Route path="/settings/system" element={<Navigate to="/settings/instance/system" replace />} />
            <Route element={<RequireAuth><Layout /></RequireAuth>}>
              <Route path="/" element={<HomeRoute />} />
              {/* Stable escape: single-project users land in their project from
                  "/", but the portfolio view stays reachable here (never bounced). */}
              <Route path="/workspace" element={withSuspense('workspace', <MainPage />)} />
              <Route path="/projects" element={<Navigate to="/workspace" replace />} />
              <Route path="/data-sources" element={<Navigate to="/settings/data-sources" replace />} />
              <Route path="/data-sources/:dsId" element={<DataSourceRedirect />} />
              <Route path="/users" element={<Navigate to="/settings/members" replace />} />
              <Route path="/account" element={<Navigate to="/settings/profile" replace />} />
              <Route path="/p/:slug/monitoring" element={<ProjectSettingsRedirect tab="monitoring" />} />
              <Route path="/p/:slug/events/detail/:eventId" element={<EventDetailRedirect />} />
              {/* Keyed per entity: the page is reached from itself (successor links, the
                  bell, Back), and a reused instance kept the previous entity's chart,
                  filters and tab under the new header. */}
              <Route
                path="/p/:slug/monitoring/:scope/:id"
                element={withSuspense(
                  'monitoring-detail',
                  <KeyedRoute params={['slug', 'scope', 'id']}><MonitoringDetailPage /></KeyedRoute>,
                  'detail',
                )}
              />
              <Route path="/p/:slug/events/:tab/new" element={withSuspense('event-edit', <EventEditPage />, 'form')} />
              {/* Before /events/:tab/:eventId, or "bulk" resolves as an event id. */}
              <Route path="/p/:slug/events/:tab/bulk" element={withSuspense('event-bulk', <EventBulkPage />, 'form')} />
              <Route path="/p/:slug/events/:tab/:eventId/edit" element={withSuspense('event-edit', <EventEditPage />, 'form')} />
              <Route path="/p/:slug/events/:tab/:eventId" element={withSuspense('events', <EventsPage />)} />
              <Route path="/p/:slug/events/:tab" element={withSuspense('events', <EventsPage />)} />
              <Route path="/p/:slug/events" element={withSuspense('events', <EventsPage />)} />
              <Route path="/p/:slug/overview" element={withSuspense('overview', <OverviewPage />, 'dashboard')} />
              <Route path="/p/:slug/monitors" element={<MonitorsRedirect />} />
              <Route path="/p/:slug/monitors/:monitorId" element={withSuspense('monitor-detail', <MonitorDetailPage />, 'detail')} />
              <Route path="/p/:slug/reconciliation" element={withSuspense('reconciliation', <ReconciliationPage />)} />
              <Route path="/p/:slug/duplicates" element={withSuspense('duplicates', <DuplicatesPage />)} />
              <Route path="/p/:slug/anomalies" element={withSuspense('anomalies', <AnomaliesPage />)} />
              <Route path="/p/:slug/metrics/new" element={withMetricSuspense('metrics-new', <MetricEditPage />, 'form')} />
              <Route path="/p/:slug/metrics/:metricId/edit" element={withMetricSuspense('metrics-edit', <MetricEditPage />, 'form')} />
              {/* Fact tables live as a tab inside Metrics — create/edit forms first,
                  then the two tab list routes. */}
              <Route path="/p/:slug/metrics/fact-tables/new" element={withMetricSuspense('fact-tables-new', <FactTableEditPage />, 'form')} />
              <Route path="/p/:slug/metrics/fact-tables/:factTableId/edit" element={withMetricSuspense('fact-tables-edit', <FactTableEditPage />, 'form')} />
              <Route path="/p/:slug/metrics/fact-tables" element={withMetricSuspense('metrics-fact-tables', <MetricsPage tab="fact-tables" />)} />
              <Route path="/p/:slug/metrics" element={withMetricSuspense('metrics-list', <MetricsPage tab="catalog" />)} />
              {/* Legacy fact-tables routes → Metrics › Fact tables tab. */}
              <Route path="/p/:slug/fact-tables/new" element={<FactTablesNewRedirect />} />
              <Route path="/p/:slug/fact-tables/:factTableId/edit" element={<FactTableEditRedirect />} />
              <Route path="/p/:slug/fact-tables" element={<FactTablesRedirect />} />
              <Route path="/p/:slug/coverage" element={withSuspense('coverage', <CoveragePage />)} />
              <Route path="/p/:slug/concepts" element={withSuspense('concepts', <ConceptsPage />, 'settings')} />
              {/* Govern › Scans — a top-level operational surface, not a settings tab. */}
              <Route path="/p/:slug/scans/:scanId" element={withSuspense('scans', <ProjectScansPage />, 'detail')} />
              <Route path="/p/:slug/scans" element={withSuspense('scans', <ProjectScansPage />)} />
              {/* Legacy Govern › Scans paths. Declared before /p/:slug/settings/:tab
                  so the pair reads in precedence order; the router ranks the static
                  `scans` segment above `:tab` regardless, so DELETING these lines —
                  not reordering them — is what drops a bookmark onto
                  ProjectSettingsPage, which no longer knows the tab and bounces to
                  /p/:slug/events (App.test.tsx pins this). */}
              <Route path="/p/:slug/settings/scans/:itemId" element={<ScansRedirect />} />
              <Route path="/p/:slug/settings/scans" element={<ScansRedirect />} />
              {/* Plan, Observe and Govern surfaces at their own addresses (#238
                  JR-25 / AL-42 / ST-5). One page renders them all, so they share
                  a Suspense key and moving between them never remounts the page. */}
              <Route path="/p/:slug/event-types/:itemId" element={withSuspense('project-settings', <ProjectSettingsPage surface="event-types" />, 'detail')} />
              <Route path="/p/:slug/event-types" element={withSuspense('project-settings', <ProjectSettingsPage surface="event-types" />)} />
              <Route path="/p/:slug/meta-fields" element={withSuspense('project-settings', <ProjectSettingsPage surface="meta-fields" />)} />
              <Route path="/p/:slug/variables/:itemId" element={withSuspense('project-settings', <ProjectSettingsPage surface="variables" />, 'detail')} />
              <Route path="/p/:slug/variables" element={withSuspense('project-settings', <ProjectSettingsPage surface="variables" />)} />
              <Route path="/p/:slug/relations" element={withSuspense('project-settings', <ProjectSettingsPage surface="relations" />)} />
              <Route path="/p/:slug/branches/:itemId" element={withSuspense('project-settings', <ProjectSettingsPage surface="branches" />, 'detail')} />
              <Route path="/p/:slug/branches" element={withSuspense('project-settings', <ProjectSettingsPage surface="branches" />, 'detail')} />
              <Route path="/p/:slug/history" element={withSuspense('project-settings', <ProjectSettingsPage surface="history" />)} />
              {/* `:itemId` is the delivery an alert message links to. */}
              <Route path="/p/:slug/alerting/:itemId" element={withSuspense('project-settings', <ProjectSettingsPage surface="alerting" />)} />
              <Route path="/p/:slug/alerting" element={withSuspense('project-settings', <ProjectSettingsPage surface="alerting" />)} />
              <Route path="/p/:slug/audit" element={withSuspense('project-settings', <ProjectSettingsPage surface="audit" />)} />
              {/* Project settings proper: detection renders here, general and
                  plan rules hand off to the takeover, and every old
                  /settings/<surface>[/:itemId] address redirects (with its query
                  string) to the route above. */}
              <Route path="/p/:slug/settings/:tab/:itemId" element={withSuspense('project-settings', <ProjectSettingsPage />, 'settings')} />
              <Route path="/p/:slug/settings/:tab" element={withSuspense('project-settings', <ProjectSettingsPage />, 'settings')} />
              <Route path="/p/:slug/settings" element={withSuspense('project-settings', <ProjectSettingsPage />, 'settings')} />
              <Route path="/p/:slug" element={<ProjectHomeRedirect />} />
              {/* Project-scoped catch-all. It has to exist separately from the
                  global one below: only a route that declares `:slug` puts the
                  param in scope for Layout, so an unmatched path under a real
                  project keeps THAT project's sidebar and breadcrumb instead of
                  collapsing to the workspace shell (tripl-jfm3.3). */}
              <Route path="/p/:slug/*" element={<NotFoundPage />} />
              {/* Catch-all: render the app shell + not-found state for any
                  unmatched authed path instead of a blank screen. */}
              <Route path="*" element={<NotFoundPage />} />
            </Route>
          </Routes>
        </DocumentTitleRoot>
      </AuthProvider>
      <Toaster />
    </ThemeProvider>
  )
}
