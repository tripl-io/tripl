import { useEffect } from 'react'
import { SETTINGS_NAV } from '@/components/settings/nav'
import { stripOrgPrefix } from '@/lib/activeOrg'

/**
 * Centralized per-page document-title mechanism.
 *
 * Historically every route shared the single static `<title>tripl</title>` from
 * `index.html`, so browser tabs, history and bookmarks were indistinguishable.
 * The app shell ({@link Layout}) already resolves a route→label for its
 * breadcrumbs; this module turns that label plus the active project slug into a
 * descriptive tab title and pushes it to `document.title` — no page component
 * has to manage its own title.
 */

const APP_NAME = 'tripl'

// U+00B7 MIDDLE DOT with surrounding spaces — the same visual separator the app
// uses elsewhere for compact hierarchical labels (e.g. "Anomalies · acme · tripl").
const TITLE_SEPARATOR = ' · '

/**
 * Compose a descriptive document title from a page label and the active project
 * slug. Pure and DOM-free so it is trivially unit-testable.
 *
 * - With a slug:    `buildDocumentTitle('Anomalies', 'acme')` → `"Anomalies · acme · tripl"`
 * - Without a slug: `buildDocumentTitle('Settings')`          → `"Settings · tripl"`
 * - Blank label:    `buildDocumentTitle('')`                  → `"tripl"`
 *
 * Blank/whitespace-only segments are dropped so the title never contains empty
 * separators.
 */
export function buildDocumentTitle(pageLabel: string, slug?: string | null): string {
  const segments = [pageLabel, slug, APP_NAME]
    .map((segment) => segment?.trim() ?? '')
    .filter((segment) => segment.length > 0)
  return segments.join(TITLE_SEPARATOR)
}

/**
 * Label for any path that has no page behind it. The catch-all route renders
 * {@link NotFoundPage} for these, so the tab has to say so too — otherwise a
 * 404 keeps whatever title the previous surface left behind.
 */
export const NOT_FOUND_TITLE_LABEL = 'Page not found'

// Human labels per top-level project surface (the `/p/:slug/<surface>` segment).
// Every segment that resolves to a real route belongs here, INCLUDING the ones
// that only redirect (`monitoring`, `fact-tables`) — they render for a frame
// before the redirect commits and must not flash "Page not found". Anything
// absent from this map has no route and is titled as not-found.
//
// The Plan, Observe and Govern pages that used to sit under `/settings/<x>`
// are top-level surfaces now (#238), named with the
// labels the sidebar uses.
const PROJECT_SURFACE_LABELS: Record<string, string> = {
  events: 'Events',
  'event-types': 'Event types',
  'meta-fields': 'Meta fields',
  variables: 'Properties',
  relations: 'Relations',
  branches: 'Plan branches',
  history: 'Plan history',
  overview: 'Overview',
  monitors: 'Alert rules',
  monitoring: 'Monitoring',
  anomalies: 'Anomalies',
  alerting: 'Alerting',
  audit: 'Audit log',
  reconciliation: 'Reconciliation',
  duplicates: 'Duplicates',
  coverage: 'Coverage',
  metrics: 'Metrics',
  'fact-tables': 'Fact tables',
  scans: 'Scans',
  concepts: 'Concepts',
  docs: 'Docs',
  settings: 'Project settings',
}

// Top-level paths that exist only to redirect somewhere else. Same reasoning as
// the redirect-only project surfaces above: they are real routes, so they must
// not resolve to "Page not found" for the frame before the redirect commits.
const LEGACY_TOP_LEVEL_LABELS: Record<string, string> = {
  'data-sources': 'Data sources',
  users: 'Members',
  account: 'Profile',
}

// Human labels for sub-surfaces that are their own destination but happen to be
// routed under a parent surface (`/p/:slug/<surface>/<sub-surface>`). Keyed by
// the segment directly after the surface, so deeper paths (detail ids, editors)
// inherit the sub-surface label the same way `/settings/instance/<x>` does.
//
// Only genuinely distinct surfaces belong here. The sibling segments that are
// merely filtered views of the parent — the Events tabs (`review`, `archived`,
// one per event type) and the metric editors (`new`, `<id>/edit`) — must stay
// absent, or every tab switch would rewrite the browser-tab title.
//
// `settings` holds project configuration only. Its old surface segments
// (`/settings/event-types`, `/settings/alerting`, …) are redirect-only since
// those pages moved to `/p/:slug/<surface>`; they stay named here because the
// redirect renders for a frame, and that frame must not flash "Page not found".
const PROJECT_SUBSURFACE_LABELS: Record<string, Record<string, string>> = {
  metrics: { 'fact-tables': 'Fact tables' },
  settings: {
    'event-types': 'Event types',
    'meta-fields': 'Meta fields',
    variables: 'Properties',
    relations: 'Relations',
    branches: 'Plan branches',
    history: 'Plan history',
    // Three surfaces named this page at once — tab title "Project settings",
    // breadcrumb terminal "Anomalies", heading "Detection settings" — and it
    // was the only route in the production walk where all three disagreed.
    // This string is the page's own H2 (MonitoringTab) and the
    // breadcrumb leaf in `lib/navigation.ts`; a test pins the two together.
    monitoring: 'Detection settings',
    alerting: 'Alerting',
    scans: 'Scans',
    audit: 'Audit log',
  },
}

// Human labels for the full-takeover Settings sections (`/settings/<section>`),
// which mount OUTSIDE the app shell — the top-level resolver still names them.
// Derived from the settings rail's own model so the tab title is always the
// label on the item the reader clicked, and a renamed section cannot leave a
// stale title behind.
const SETTINGS_RAIL_LABELS: Record<string, string> = Object.fromEntries(
  Object.values(SETTINGS_NAV).flatMap((groups) =>
    groups.flatMap((group) => group.items.map((item) => [item.path, item.label] as const)),
  ),
)

// The two section parents. Neither has a page of its own — both only ever
// render a child — so the rail does not list them, and they stand in for any
// path beneath them the rail does not list either.
const SETTINGS_PARENT_LABELS: Record<string, string> = {
  project: 'Project settings',
  instance: 'Instance settings',
}

/**
 * Resolve a page label (and the active project slug, when the route is
 * project-scoped) from a pathname. Covers EVERY route family — project routes,
 * the full-takeover Settings pages, `/auth`, and the workspace dashboard — so a
 * single always-mounted component can title them all, including the routes that
 * mount outside the app shell. Pure and DOM-free for unit-testing.
 */
export function resolveTitleFromPath(pathname: string): { label: string; slug?: string } {
  // `/o/{org}/p/…` titles as `/p/…`, `/o/{org}` as the workspace (F20 PR7).
  const parts = stripOrgPrefix(pathname).split('/').filter(Boolean)
  const [head, second] = parts
  if (head === undefined) return { label: 'All projects' } // "/"
  if (parts[0] === 'auth') return { label: 'Sign in' }
  // `/invite/:token` renders outside the app shell and sets no title of its own,
  // so without an entry here the one page a brand-new member ever sees titled
  // its tab "Page not found".
  if (parts[0] === 'invite') return { label: 'Invitation' }
  if (parts[0] === 'workspace' || parts[0] === 'projects') return { label: 'All projects' }
  if (parts[0] === 'settings') {
    // Keyed on the FULL section path, not on its first segment. The rail's paths
    // are a mix of one and two segments (`members` next to `instance/runtime`),
    // so a first-segment lookup gave every one of the seven `instance/*`
    // sections the same title and both `project/*` sections another — eleven
    // routes collapsed onto three tab titles, which is unusable for the
    // owner-operator who has several of them open at once.
    const sectionPath = parts.slice(1).join('/')
    if (!sectionPath || second === undefined) return { label: 'Settings' }
    // Longest rail prefix wins, then the section parent. A route can be deeper
    // than the rail entry it belongs to — `/settings/data-sources/<id>` is the
    // one in App.tsx, and it is where opening a data-source row lands — and
    // exact-matching the full path alone titled that tab the generic "Settings"
    // instead of inheriting "Data sources" from the entry above it.
    const label =
      SETTINGS_RAIL_LABELS[sectionPath] ??
      SETTINGS_RAIL_LABELS[parts.slice(1, 3).join('/')] ??
      SETTINGS_RAIL_LABELS[second] ??
      SETTINGS_PARENT_LABELS[second] ??
      'Settings'
    return { label }
  }
  if (parts[0] === 'p' && parts[1]) {
    const surface = parts[2] ?? 'events'
    // A known sub-surface wins over its parent surface; anything else (tabs,
    // detail ids, editors) falls through to the parent label.
    const subSurface = parts[3] ? PROJECT_SUBSURFACE_LABELS[surface]?.[parts[3]] : undefined
    const label = subSurface ?? PROJECT_SURFACE_LABELS[surface]
    // The slug is still valid on an unmatched project sub-path, so the tab keeps
    // naming the project — only the page half becomes "Page not found".
    return { label: label ?? NOT_FOUND_TITLE_LABEL, slug: parts[1] }
  }
  const legacyTopLevel = LEGACY_TOP_LEVEL_LABELS[head]
  if (legacyTopLevel) return { label: legacyTopLevel }
  return { label: NOT_FOUND_TITLE_LABEL } // unmatched authed path → the 404 page
}

// What a detail route shows, for its tab title. Keyed by the segment
// after `/p/:slug/`, then (for monitoring) by the scope segment.
const MONITORING_SCOPE_KINDS: Record<string, string> = {
  event: 'Event',
  'event-type': 'Event type volume',
  'project-total': 'Volume',
  metric: 'Metric',
}
const DETAIL_SURFACE_KINDS: Record<string, string> = {
  monitors: 'Alert rule',
  scans: 'Scan',
}

/**
 * The kind of entity a project detail route shows ("Event type volume",
 * "Alert rule"), or null when the path is not a detail route. Pure.
 */
export function resolveEntityKind(pathname: string): string | null {
  const [head, slug, surface, sub, id] = stripOrgPrefix(pathname).split('/').filter(Boolean)
  if (head !== 'p' || !slug || !surface || !sub) return null
  if (surface === 'monitoring') return id ? (MONITORING_SCOPE_KINDS[sub] ?? null) : null
  return DETAIL_SURFACE_KINDS[surface] ?? null
}

/**
 * The page label for a detail page once its entity has loaded: the
 * entity's own name, then what kind of thing it is, so three open monitoring
 * tabs no longer all read "Monitoring · acme · tripl". Pass the result to
 * {@link useDocumentTitle} without a slug:
 * `"Screen View · Event type volume · tripl"`.
 */
export function entityTitleLabel(entity: string, kind: string): string {
  return [entity, kind]
    .map((segment) => segment.trim())
    .filter((segment) => segment.length > 0)
    .join(TITLE_SEPARATOR)
}

/**
 * Set `document.title` to the composed per-page title whenever the label or slug
 * changes. Wired in exactly one place — the app shell — rather than in every
 * page component.
 *
 * The previous title is intentionally not restored on unmount: the single
 * top-level `<DocumentTitle>` that drives this hook stays mounted for the app's
 * lifetime and recomputes the correct title on every navigation, so a cleanup
 * step would only ever flash `tripl` between routes.
 */
export function useDocumentTitle(pageLabel: string, slug?: string | null): void {
  useEffect(() => {
    document.title = buildDocumentTitle(pageLabel, slug)
  }, [pageLabel, slug])
}
