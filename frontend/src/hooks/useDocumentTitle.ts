import { useEffect } from 'react'
import { matchPath, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ApiError } from '@/api/client'
import {
  LEGACY_SETTINGS_REDIRECTS,
  SETTINGS_NAV,
  SETTINGS_SUB_ROUTED_FAMILIES,
  contextForPath,
} from '@/components/settings/nav'
import { extensionRoutes } from '@/extensions'
import { stripOrgPrefix } from '@/lib/activeOrg'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import {
  PROJECT_SURFACES_MOVED_FROM_SETTINGS,
  buildNavGroups,
  resolveProjectEditorPage,
} from '@/lib/navigation'
import { projectQueryOptions, projectsQueryOptions } from '@/lib/queryKeys'

/**
 * The browser-tab title of every route, in one scheme. A title runs from the
 * most specific thing on the page to the least, then the app:
 *
 * - a project page:          "Overview · Demo Project · tripl"
 * - one row of a project:    "Home Screen View · Event · Demo Project · tripl"
 *                            (the row's name, what kind of row it is, the project)
 * - a settings section:      "General · Project settings · Demo Project · tripl",
 *                            "Members · Organization settings · tripl"
 * - anything else:           "Sign in · tripl"
 *
 * A project is named by its name, as the sidebar and the top bar name it; the
 * slug ("demo-0793b8") read like an internal id. Until the name is known the
 * project is left out rather than echo whatever the address says.
 *
 * `resolveTitleFromPath` reads what the address alone says;
 * `composeDocumentTitle` adds what only the running app knows (the project's
 * name, the row a page has loaded). {@link useDocumentTitle} joins the two from
 * one always-mounted place, so no page manages its own title.
 */

const APP_NAME = 'tripl'

// U+00B7 MIDDLE DOT with surrounding spaces — the separator the app uses for
// compact hierarchical labels.
const TITLE_SEPARATOR = ' · '

/**
 * Join title segments, most specific first, and end with the app's name.
 * Blank segments are dropped, so the title never shows an empty separator.
 * Pure: `buildDocumentTitle('Anomalies', 'Demo')` → `"Anomalies · Demo · tripl"`.
 */
export function buildDocumentTitle(...segments: ReadonlyArray<string | null | undefined>): string {
  return [...segments, APP_NAME]
    .map((segment) => segment?.trim() ?? '')
    .filter((segment) => segment.length > 0)
    .join(TITLE_SEPARATOR)
}

/**
 * Label for any path that has no page behind it. The catch-all route renders
 * NotFoundPage for these, so the tab has to say so too — otherwise a 404 keeps
 * whatever title the previous surface left behind.
 */
export const NOT_FOUND_TITLE_LABEL = 'Page not found'

/** What the shell's unknown-project screen is headed, and so its tab. */
export const PROJECT_NOT_FOUND_TITLE_LABEL = 'Project not found'

// Pages outside any project and outside Settings, by their first segment.
// `/invite/:token` and `/verify-email` are the first (often only) tripl page an
// invited member or a hosted sign-up opens; titled "Page not found", they
// looked like dead links.
const TOP_LEVEL_LABELS: Record<string, string> = {
  auth: 'Sign in',
  invite: 'Invitation',
  'verify-email': 'Verify email',
  workspace: 'All projects',
  projects: 'All projects',
}

// Top-level addresses that only redirect into Settings now, by the section they
// land on: the redirect's frame is titled as its destination.
const LEGACY_TOP_LEVEL_SECTIONS: Record<string, string> = {
  'data-sources': 'data-sources',
  users: 'members',
  account: 'profile',
}

// The sidebar's label for each project surface, keyed by its address segment
// (`event-types` → "Event types"), so a page the sidebar lists is titled by the
// name it is listed under. A hand-kept copy of these left Annotations out, and
// its tab read "Page not found" on a page that worked.
const SIDEBAR_SURFACE_LABELS: Record<string, string> = Object.fromEntries(
  buildNavGroups('_', undefined).flatMap((group) =>
    group.items.map((item) => [stripOrgPrefix(item.href).split('/')[3] ?? '', item.label] as const),
  ),
)

// Every surface segment under `/p/:slug` that resolves to a real route: the
// sidebar's, then the ones it does not list, INCLUDING those that only redirect
// (`monitors`, `monitoring`, `fact-tables`) — they render for a frame before the
// redirect commits and must not flash "Page not found". A segment absent here
// has no route and is titled as not-found.
const PROJECT_SURFACE_LABELS: Record<string, string> = {
  ...SIDEBAR_SURFACE_LABELS,
  monitors: 'Alert rules',
  monitoring: 'Monitoring',
  'fact-tables': 'Fact tables',
  concepts: 'Concepts',
  settings: 'Project settings',
}

function surfaceLabels(surfaces: readonly string[]): Record<string, string> {
  return Object.fromEntries(
    surfaces.flatMap((surface) => {
      const label = PROJECT_SURFACE_LABELS[surface]
      return label ? [[surface, label] as const] : []
    }),
  )
}

// Sub-surfaces that are a destination of their own though routed under a parent
// surface (`/p/:slug/<surface>/<sub-surface>`). Deeper paths (detail ids)
// inherit the sub-surface's label. The parent's filtered views — the Events
// tabs (`review`, `archived`, one per event type) — must stay absent, or every
// tab switch would rewrite the tab title; create and edit pages are named by
// `resolveProjectEditorPage`.
//
// `settings` holds project configuration only. The old addresses of the
// surfaces that moved out of it (`/settings/event-types`, `/settings/scans`, …)
// only redirect now, and keep their names because the redirect renders for a
// frame.
const PROJECT_SUBSURFACE_LABELS: Record<string, Record<string, string>> = {
  metrics: { 'fact-tables': 'Fact tables' },
  settings: {
    ...surfaceLabels([...PROJECT_SURFACES_MOVED_FROM_SETTINGS, 'scans']),
    // Three surfaces named this page at once — tab title "Project settings",
    // breadcrumb terminal "Anomalies", heading "Detection settings" — and it
    // was the only route in the production walk where all three disagreed.
    // This string is the page's own H2 (MonitoringTab) and the
    // breadcrumb leaf in `lib/navigation.ts`; a test pins the two together.
    monitoring: 'Detection settings',
  },
}

// What one row of a surface is, for the tab of a page that names the row it
// shows (usePageTitle): "Screen View · Event type", not the list's plural.
// Keyed by the surface segment; monitoring by its scope segment instead.
const ROW_KINDS: Record<string, string> = {
  events: 'Event',
  'event-types': 'Event type',
  variables: 'Property',
  branches: 'Plan branch',
  docs: 'Note',
  metrics: 'Metric',
  monitors: 'Alert rule',
  scans: 'Scan',
}
const SUBSURFACE_ROW_KINDS: Record<string, Record<string, string>> = {
  metrics: { 'fact-tables': 'Fact table' },
}
const MONITORING_SCOPE_KINDS: Record<string, string> = {
  event: 'Event',
  'event-type': 'Event type volume',
  'project-total': 'Volume',
  metric: 'Metric',
}

// Each settings section's title, from the rail's own model: the label on the
// item the reader clicked, framed by its group. A bare rail label was
// ambiguous in a tab strip — every project's "General" read the same, and the
// organization's "Audit log" passed for a project's.
const SETTINGS_SECTIONS: Record<string, { label: string; scope: string }> = Object.fromEntries(
  Object.values(SETTINGS_NAV).flatMap((groups) =>
    groups.flatMap((group) =>
      group.items.map((item) => [item.path, { label: item.label, scope: `${group.label} settings` }] as const),
    ),
  ),
)

/** What the address alone says about a route's title. */
export type RouteTitle = {
  /** The page: a surface ("Events"), an editor ("New metric"), a settings section ("General"). */
  label: string
  /** The project the page belongs to, by slug: the `/p/:slug`, or project settings' `?project=`. */
  slug?: string
  /** The settings group a section sits in ("Project settings"). */
  scope?: string
  /** What one row of the surface is ("Alert rule"), for a page that names the row it shows. */
  kind?: string
}

function resolveSettingsTitle(segments: readonly string[], search: string): RouteTitle {
  const [first] = segments
  // `/settings` alone resumes the last section.
  if (first === undefined) return { label: 'Settings' }
  // A pre-takeover address is titled as the section it redirects to.
  const sectionPath = (segments.length === 1 ? LEGACY_SETTINGS_REDIRECTS[first] : undefined) ?? segments.join('/')
  // The longest rail entry wins: a route can be deeper than the entry it belongs
  // to (`/settings/data-sources/<id>`, where opening a data source lands).
  const section =
    SETTINGS_SECTIONS[sectionPath] ?? SETTINGS_SECTIONS[segments.slice(0, 2).join('/')] ?? SETTINGS_SECTIONS[first]
  if (section) {
    const slug = contextForPath(sectionPath) === 'project' ? new URLSearchParams(search).get('project') : null
    return { ...section, ...(slug ? { slug } : {}) }
  }
  // A section the rail does not list, in a family routed by `:sub`: the area
  // redirects it, and the redirect's frame keeps a settings title. Anything
  // else here — `/settings/foo`, a bare `/settings/project` — has no route and
  // renders the 404 page.
  const routedFamily = (SETTINGS_SUB_ROUTED_FAMILIES as readonly string[]).includes(first)
  if (routedFamily && segments.length === 2) return { label: 'Settings' }
  return { label: NOT_FOUND_TITLE_LABEL }
}

function rowKind(surface: string, sub: string | undefined, id: string | undefined): string | undefined {
  if (sub === undefined) return undefined // a list, not a row
  if (surface === 'monitoring') return id ? MONITORING_SCOPE_KINDS[sub] : undefined
  return SUBSURFACE_ROW_KINDS[surface]?.[sub] ?? ROW_KINDS[surface]
}

function resolveProjectTitle(path: string, slug: string, segments: readonly string[]): RouteTitle {
  // A bare project address redirects to the project's home.
  const [surface = 'overview', sub, id] = segments
  const label =
    resolveProjectEditorPage(path)?.title ??
    (sub === undefined ? undefined : PROJECT_SUBSURFACE_LABELS[surface]?.[sub]) ??
    PROJECT_SURFACE_LABELS[surface]
  // The slug is still valid on an unmatched project sub-path, so the tab keeps
  // naming the project — only the page half becomes "Page not found".
  if (!label) return { label: NOT_FOUND_TITLE_LABEL, slug }
  const kind = rowKind(surface, sub, id)
  return { label, slug, ...(kind ? { kind } : {}) }
}

/**
 * Resolve a route's title from its address: the page, the project it belongs
 * to, and for Settings the group framing the section. Covers EVERY route
 * family — project routes, the full-takeover Settings pages, `/auth`, the
 * workspace, the extensions' own pages — so a single always-mounted driver can
 * title them all, including the ones that mount outside the app shell. Pure.
 */
export function resolveTitleFromPath(pathname: string, search = ''): RouteTitle {
  // `/o/{org}/p/…` titles as `/p/…`, `/o/{org}` as the workspace (F20 PR7).
  const path = stripOrgPrefix(pathname)
  const segments = path.split('/').filter(Boolean)
  const [head, second] = segments
  if (head === undefined) return { label: 'All projects' } // "/"
  if (head === 'settings') return resolveSettingsTitle(segments.slice(1), search)
  if (head === 'p' && second) return resolveProjectTitle(path, second, segments.slice(2))
  const page = TOP_LEVEL_LABELS[head]
  if (page) return { label: page }
  const legacySection = LEGACY_TOP_LEVEL_SECTIONS[head]
  if (legacySection) return resolveSettingsTitle([legacySection], search)
  // An extension's own page (single sign-on's account link) carries its title;
  // one without is titled by the app's name alone, never as a 404.
  const extension = extensionRoutes.find((route) => matchPath(route.path, path))
  if (extension) return { label: extension.title ?? '' }
  return { label: NOT_FOUND_TITLE_LABEL } // unmatched authed path → the 404 page
}

/** What only the running app knows about a page, beside its address. */
export type TitleContext = {
  /** The row a detail page has loaded and named (usePageTitle). */
  entity?: string | null
  /** The route's project, by name, once the shell knows it. */
  projectName?: string | null
  /** The shell has found no such project, and shows "Project not found". */
  projectMissing?: boolean
}

/** The document title for a route, in the scheme at the top of this module. Pure. */
export function composeDocumentTitle(
  route: RouteTitle,
  { entity, projectName, projectMissing = false }: TitleContext = {},
): string {
  // A settings section, framed by its group; project settings also name the
  // project they change. An unknown `?project=` is the section's to answer.
  if (route.scope) return buildDocumentTitle(route.label, route.scope, projectName)
  // The shell answers an unknown project with "Project not found", so the tab
  // does too, instead of echoing the address as if it named a project.
  if (route.slug && projectMissing) return buildDocumentTitle(PROJECT_NOT_FOUND_TITLE_LABEL)
  // A page that has named its row leads with it, then what kind of row it is:
  // three open monitoring tabs used to read "Monitoring · acme" alike.
  if (entity) return buildDocumentTitle(entity, route.kind ?? route.label, projectName)
  return buildDocumentTitle(route.label, projectName)
}

/**
 * The project a route names, as far as the shell has found out. Reads the
 * caches the shell fills and never fetches: the title driver is mounted on
 * `/auth` too, where there is no session. Decided as Layout decides — the list,
 * else the project endpoint (a deep link whose list has not landed, a demo the
 * list hides while it seeds) — and missing only once the list has answered and
 * the endpoint said 404 or 403.
 */
function useTitleProject(slug: string | undefined): Pick<TitleContext, 'projectName' | 'projectMissing'> {
  const list = useQuery({ ...projectsQueryOptions(), enabled: false })
  // With the shell's own options for the key, so reading it can never change
  // how the shell's lookup reports a failure (it renders one, no toast).
  const confirm = useQuery({ ...projectQueryOptions(slug), enabled: false, retry: false, meta: SILENT_ERROR_META })
  if (!slug) return {}
  const projectName = list.data?.find((project) => project.slug === slug)?.name ?? confirm.data?.name
  if (projectName) return { projectName }
  const error = confirm.error
  const refused = error instanceof ApiError && (error.status === 404 || error.status === 403)
  return { projectMissing: !list.isPending && refused }
}

/**
 * Keep `document.title` on the current route's title. Called from exactly one
 * place — App.tsx's DocumentTitle, mounted at the root beside <Routes> — so it
 * follows EVERY navigation, including the Settings takeover and /auth, which
 * render outside the app shell. `entity` is the row a detail page has named.
 *
 * The previous title is not restored on unmount: the driver stays mounted for
 * the app's lifetime and recomputes the title on every navigation, so a cleanup
 * would only flash "tripl" between routes.
 */
export function useDocumentTitle(entity: string | null): void {
  const { pathname, search } = useLocation()
  const route = resolveTitleFromPath(pathname, search)
  const project = useTitleProject(route.slug)
  const title = composeDocumentTitle(route, { entity, ...project })
  useEffect(() => {
    document.title = title
  }, [title])
}
