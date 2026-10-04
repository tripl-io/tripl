import { formatCompactNumber } from '@/lib/format'
import {
  Activity,
  AlertTriangle,
  Bell,
  Braces,
  Copy,
  GitBranch,
  GitCompare,
  History,
  LineChart,
  Link2,
  NotebookText,
  ScanLine,
  ScrollText,
  ShieldCheck,
  StickyNote,
  Table2,
  Tag,
  Variable,
  type LucideIcon,
} from 'lucide-react'
import type { ActivityItemType, ProjectSummary } from '@/types'
import { currentOrgSlug, projectPath, stripOrgPrefix, withActiveOrg } from '@/lib/activeOrg'

// The project/organization address builders live with the active organization
// (lib/activeOrg.ts, which the API client and query keys also read); every
// link builder imports them from here.
export {
  currentOrgSlug,
  orgHomePath,
  projectPath,
  settingsPath,
  stripOrgPrefix,
  withActiveOrg,
  workspacePath,
} from '@/lib/activeOrg'

/**
 * Shared navigation model for the job-based information architecture
 * (Plan / Observe / Govern). Both the sidebar (rendering) and the top-bar
 * breadcrumbs (area resolution) consume this so the two stay in sync.
 */

export type NavTone = 'danger' | 'warning' | 'accent' | 'info'

export type NavItem = {
  id: string
  label: string
  icon: LucideIcon
  href: string
  match: (path: string) => boolean
  count?: string
  tone?: NavTone
  /**
   * The count is an unacknowledged alert backlog: the sidebar paints it solid
   * red (DS-6, DS-28). Only Alerting's open incidents set it; a danger tone
   * alone (Anomalies) stays a neutral count.
   */
  urgent?: boolean
  /**
   * Something on the page wants a look, said beside the count rather than
   * instead of it: a warning dot whose label is the sentence ("3 open property
   * drifts"). Properties sets it for open property drift (F23, #306), which
   * the Properties page lists; its count stays the property count.
   */
  attention?: string
  // Hidden from non-owners by the sidebar. This mirrors a real 403 on the
  // route behind it — it is a "don't walk them into a wall" affordance, never
  // the gate itself (see settings/nav.ts for the same flag on that side).
  ownerOnly?: boolean
}

export type NavGroup = { label: string; items: NavItem[] }

/**
 * A sidebar badge count: the shared compact formatter (DS-30). The local copy
 * printed "1.0k" for 1,000 and "1500k" for 1.5 million.
 */
export function formatCount(n: number): string {
  return formatCompactNumber(n)
}

/**
 * Build the three job-based groups for a project. Each item maps to a route that
 * already exists — the IA redesign gives every major surface a first-class home
 * instead of burying it under a flat "Settings" tab list. Counts/tones come from
 * the cheap project summary; surfaces without a backing count omit it. Data
 * sources / API keys are genuine configuration, so they live in workspace
 * Settings rather than as a top-level "Connect" nav group.
 */
export function buildNavGroups(slug: string, summary: ProjectSummary | undefined): NavGroup[] {
  // Built against the org-less address, then moved under the active
  // organization below: hrefs get the `/o/{org}` prefix and `match` reads a
  // pathname with it taken off, so an item is active on either address.
  const base = `/p/${slug}`
  // `firing_monitor_count` is deliberately NOT read here any more. It badged the
  // standalone Monitors item, and with that item merged into Alerting
  // a firing rule is already counted by `open_incident_count`
  // below: firing produces a delivery, a delivery opens an incident, and the
  // incident is the thing somebody still owes an answer on. Badging both would
  // put two danger counts on one nav group for one event — which is the
  // complaint that started the merge. The firing/warning/healthy rollup lives on
  // the Monitors section itself, where it can be read against the rules it
  // counts.
  const openSignals = summary?.monitoring_signal_count ?? 0
  const openIncidents = summary?.open_incident_count ?? 0
  // Open property drift (F23): the shared count the health score also reads
  // (`_open_signals.open_property_drift_counts`). Optional: an older server
  // or a cached summary omits it.
  const openPropertyDrifts = summary?.open_property_drift_count ?? 0

  const groups: NavGroup[] = [
    {
      label: 'Plan',
      items: [
        {
          id: 'events',
          label: 'Events',
          icon: Table2,
          href: `${base}/events`,
          // The catalog-event drilldown lives under /monitoring/event/:id
          // (getMonitoringPath, scope_type 'event') but is an Events surface —
          // reached from the Events catalog, breadcrumbs read "Events › Detail" —
          // so it activates Events here and is excluded from Monitors below,
          // mirroring how the catalog-metric case is handled. The trailing slash
          // keeps /monitoring/event-type/ (a Monitors drilldown) from matching.
          match: (p) =>
            p === base
            || p.startsWith(`${base}/events`)
            || p.startsWith(`${base}/monitoring/event/`),
          count: summary ? formatCount(summary.active_event_count) : undefined,
        },
        {
          id: 'event-types',
          label: 'Event types',
          icon: Tag,
          href: `${base}/event-types`,
          match: (p) => p.startsWith(`${base}/event-types`),
          count: summary ? formatCount(summary.event_type_count) : undefined,
        },
        {
          id: 'schema',
          // Named for what the page holds: the meta fields every event
          // carries. "Schema & fields" promised the per-type schema, which
          // lives on each event type (#238 AU-10 / JR-30). The old name stays a
          // palette keyword.
          label: 'Meta fields',
          icon: Braces,
          href: `${base}/meta-fields`,
          match: (p) => p.startsWith(`${base}/meta-fields`),
        },
        {
          id: 'variables',
          label: 'Properties',
          icon: Variable,
          href: `${base}/variables`,
          match: (p) => p.startsWith(`${base}/variables`),
          count: summary ? formatCount(summary.variable_count) : undefined,
          attention: openPropertyDrifts > 0
            ? `${openPropertyDrifts} open property ${openPropertyDrifts === 1 ? 'drift' : 'drifts'}`
            : undefined,
        },
        {
          id: 'relations',
          label: 'Relations',
          icon: Link2,
          href: `${base}/relations`,
          match: (p) => p.startsWith(`${base}/relations`),
        },
        {
          // The docs catalog (F22): Markdown notes for people and agents,
          // project notes plus the organization's shared ones. Not BookOpen:
          // the sidebar footer's Concepts link already wears it.
          id: 'docs',
          label: 'Docs',
          icon: NotebookText,
          href: `${base}/docs`,
          match: (p) => p === `${base}/docs` || p.startsWith(`${base}/docs/`),
        },
        {
          id: 'branches',
          label: 'Plan branches',
          icon: GitBranch,
          href: `${base}/branches`,
          match: (p) => p.startsWith(`${base}/branches`),
        },
        {
          id: 'history',
          label: 'Plan history',
          icon: History,
          href: `${base}/history`,
          // The immutable snapshot trail of the merges made on Plan branches
          // directly above. It is the one settings tab the IA redesign never
          // gave a sidebar home, so deleting the old tab strip left a fully
          // built page — 13 revisions and a "Snapshot now" action on the
          // production project — with zero inbound links anywhere in the
          // product and no nav item to highlight while you stood on it.
          match: (p) => p.startsWith(`${base}/history`),
        },
      ],
    },
    {
      label: 'Observe',
      items: [
        {
          id: 'overview',
          // The project's home (projectHomePath): KPIs, volume, signals and the
          // get-started checklist. "Live activity" undersold it and collided
          // with the top bar's activity feed (#238 SH-8 / JR-35).
          label: 'Overview',
          icon: Activity,
          href: `${base}/overview`,
          match: (p) => p.startsWith(`${base}/overview`),
        },
        {
          id: 'metrics',
          label: 'Metrics',
          icon: LineChart,
          href: `${base}/metrics`,
          // The catalog-metric drilldown lives under /monitoring/metric/:id
          // (getMetricMonitoringPath) but is a Metrics surface — breadcrumbs
          // read "Metrics › Detail" — so it activates Metrics here and is
          // excluded from Monitors below.
          match: (p) =>
            p.startsWith(`${base}/metrics`)
            || p.startsWith(`${base}/monitoring/metric/`),
        },
        {
          id: 'anomalies',
          label: 'Anomalies',
          icon: AlertTriangle,
          href: `${base}/anomalies`,
          // Anomalies owns the DETECTION layer end to end, which is two things
          // the vanished Monitors item used to hold by accident:
          //
          //  - `/settings/monitoring`, the detection settings. They decide what
          //    gets FLAGGED and notify nobody, so they never belonged with the
          //    rules that route the result.
          //  - the per-scope volume drilldowns under `/monitoring/`, minus the
          //    catalog-metric (`/monitoring/metric/`) and catalog-event
          //    (`/monitoring/event/`) ones, which Metrics and Events claim
          //    above. What is left — event-type and project-total — is reached
          //    from a signal on this page, so this is where it belongs. Note
          //    `/monitoring/event-type/` is safe from the event exclusion: it
          //    does not start with `/monitoring/event/`.
          match: (p) =>
            p.startsWith(`${base}/anomalies`)
            || p.startsWith(`${base}/settings/monitoring`)
            || (p.startsWith(`${base}/monitoring`)
              && !p.startsWith(`${base}/monitoring/metric/`)
              && !p.startsWith(`${base}/monitoring/event/`)),
          // Badges the open-signal population (monitoring_signal_count) — the raw
          // anomalies feed this page lists. Warning, not danger: signals are
          // information about what detection found; the work somebody owes is
          // the incident on Alerting, which keeps danger (#240 JR-6). Omitted
          // when nothing is open.
          count: openSignals > 0 ? formatCount(openSignals) : undefined,
          tone: openSignals > 0 ? 'warning' : undefined,
        },
        {
          // Every chart annotation and planned event in one list (F18).
          id: 'annotations',
          label: 'Annotations',
          icon: StickyNote,
          href: `${base}/annotations`,
          match: (p) => p.startsWith(`${base}/annotations`),
        },
        {
          id: 'alerting',
          label: 'Alerting',
          icon: Bell,
          href: `${base}/alerting`,
          // `/monitors/:id` is the per-rule fired history, and a rule is an
          // Alerting object — the standalone Monitors list that used to own
          // this prefix rendered the same AlertRule rows under a second noun,
          // and is now the Monitors SECTION of this page.
          match: (p) =>
            p.startsWith(`${base}/alerting`)
            || p.startsWith(`${base}/monitors`),
          // Badges the OPEN INCIDENT population (open_incident_count) — the
          // backlog the inbox on that page owes an answer on — in a danger tone
          // when any are open. It used to badge alert_destination_count, which
          // is configuration, not work: the sidebar read "Alerting 1" (one
          // telegram destination) directly under "Anomalies 68" while 52
          // incidents sat open, so the one surface with a real queue looked like
          // the quietest thing in the group. That is the third
          // badge-parity bug in this file after the two above, and it has the
          // same cause every time: the badge was bound to whatever count the
          // summary already happened to carry rather than to what the page
          // counts. The backend computes open_incident_count over the same
          // 30-day window and mute-expiry rules as list_alert_inbox precisely so
          // the two cannot disagree. Omitted when the inbox is clear.
          count: openIncidents > 0 ? formatCount(openIncidents) : undefined,
          tone: openIncidents > 0 ? 'danger' : undefined,
          urgent: openIncidents > 0 || undefined,
        },
      ],
    },
    {
      label: 'Govern',
      items: [
        {
          id: 'reconciliation',
          label: 'Reconciliation',
          icon: GitCompare,
          href: `${base}/reconciliation`,
          match: (p) => p.startsWith(`${base}/reconciliation`),
        },
        {
          // Likely duplicates already in the catalog, and what to keep (F12,
          // #265). Beside Reconciliation: both ask whether the plan and the
          // data describe each event once.
          id: 'duplicates',
          label: 'Duplicates',
          icon: Copy,
          href: `${base}/duplicates`,
          match: (p) => p.startsWith(`${base}/duplicates`),
        },
        {
          id: 'coverage',
          label: 'Coverage',
          icon: ShieldCheck,
          href: `${base}/coverage`,
          match: (p) => p.startsWith(`${base}/coverage`),
        },
        {
          id: 'scans',
          label: 'Scans',
          // Not the magnifier: on the collapsed rail Scans and Search were the
          // same icon a few rows apart (#238 SH-13).
          icon: ScanLine,
          href: `${base}/scans`,
          match: (p) => p.startsWith(`${base}/scans`),
        },
        {
          id: 'audit',
          label: 'Audit log',
          icon: ScrollText,
          href: `${base}/audit`,
          match: (p) => p.startsWith(`${base}/audit`),
          // THIS project's changes: the page asks /audit with project_slug
          // (AuditTab), so "who changed my plan?" is answered for this plan
          // alone. Actions that belong to no project (members, API keys, a
          // project's deletion) are in Settings › Instance › Audit log
          // (#238 JR-26). Owner-only because the endpoint is.
          ownerOnly: true,
        },
      ],
    },
  ]
  const org = currentOrgSlug()
  return groups.map((group) => ({
    ...group,
    items: group.items.map((item) => ({
      ...item,
      href: projectPath(org, slug, item.href.slice(base.length)),
      match: (path: string) => item.match(stripOrgPrefix(path)),
    })),
  }))
}

/**
 * Sub-surfaces a nav item MATCHES but does not NAME, keyed by the path suffix
 * under `/p/:slug`.
 *
 * `/settings/monitoring` is the detection settings, and detection is what
 * Anomalies owns, so the Anomalies item claims the path (see its match above)
 * and the sidebar highlights Anomalies while you stand on it. That is right, and
 * it left the breadcrumb terminal reading "Anomalies" in bold over a page headed
 * "Detection settings": both wayfinding controls named a different page than the
 * one displayed, so arriving from the Anomalies page's own link produced no
 * on-screen confirmation you had navigated at all.
 *
 * Deliberately tiny. A leaf is only correct where the sub-surface has a name of
 * its own; tabs, detail ids and editors legitimately inherit their surface's
 * name and must stay out, or every event-type tab would rewrite the trail.
 */
const NAV_SUBSURFACE_LEAVES: Record<string, string> = {
  '/settings/monitoring': 'Detection settings',
}

/**
 * The one landing page of a project. "/" sends a single-project account here,
 * and the project switcher and palette used to pick Events instead, so the same
 * project had two front doors (SHELL-44).
 */
export function projectHomePath(slug: string): string {
  return projectPath(currentOrgSlug(), slug, '/overview')
}

/**
 * The Plan, Observe and Govern pages that used to be routed as
 * `/p/:slug/settings/<surface>` and are `/p/:slug/<surface>` now
 * (#238 JR-25 / AL-42 / ST-5). The sidebar presents them as first-class pages,
 * and the `settings/` in their address — echoed by the palette and the
 * breadcrumb — framed them as configuration. `/p/:slug/settings` keeps only
 * project settings: general, plan rules and detection (`settings/monitoring`).
 */
export const PROJECT_SURFACES_MOVED_FROM_SETTINGS = [
  'event-types',
  'meta-fields',
  'variables',
  'relations',
  'branches',
  'history',
  'alerting',
  'audit',
] as const

export type ProjectSurfaceMovedFromSettings = (typeof PROJECT_SURFACES_MOVED_FROM_SETTINGS)[number]

export function isSurfaceMovedFromSettings(tab: string): tab is ProjectSurfaceMovedFromSettings {
  return (PROJECT_SURFACES_MOVED_FROM_SETTINGS as readonly string[]).includes(tab)
}

// The moved surfaces App.tsx registers a `/p/:slug/<surface>/:itemId` route for.
const SURFACES_WITH_ITEM_ROUTES: ReadonlySet<ProjectSurfaceMovedFromSettings> = new Set([
  'event-types',
  'variables',
  'branches',
  'alerting',
])

/**
 * Where an old `/p/:slug/settings/<tab>[/:itemId]` address lives now, keeping
 * the item id, query string and hash — an alert message sent before the move
 * carries `?item=` and `?incident=` anchors that must survive the hop. Null for
 * a tab that did not move (detection settings, general).
 */
export function legacySettingsRedirectPath(
  slug: string,
  tab: string,
  itemId?: string,
  search = '',
  hash = '',
): string | null {
  if (!isSurfaceMovedFromSettings(tab)) return null
  // Only surfaces with a `/:itemId` route keep the id; the rest ignored it under
  // `/settings/<tab>/<id>` too, and appending it now would land on NotFound.
  const path = itemId && SURFACES_WITH_ITEM_ROUTES.has(tab)
    ? projectPath(currentOrgSlug(), slug, `/${tab}/${itemId}`)
    : projectPath(currentOrgSlug(), slug, `/${tab}`)
  return `${path}${search}${hash}`
}

// Surfaces that exist in every project under the same address, so switching
// project can keep the reader on the one they are comparing.
const PORTABLE_SURFACES: ReadonlySet<string> = new Set<string>([
  'overview',
  'events',
  'metrics',
  'anomalies',
  'annotations',
  'reconciliation',
  'coverage',
  'scans',
  'concepts',
  ...PROJECT_SURFACES_MOVED_FROM_SETTINGS,
])

/**
 * Where switching from project `fromSlug` to `toSlug` lands: the same surface
 * in the new project when it has one, the project's home otherwise.
 *
 * Only the surface survives. Anything after it names a row of the old project
 * (an event type, a scan, a metric, a settings item) and would 404 or, worse,
 * resolve to an unrelated row in the new one. The query string goes too: its
 * `?branch=` and item anchors belong to the old project.
 */
export function switchProjectPath(
  currentPath: string,
  fromSlug: string | undefined,
  toSlug: string,
): string {
  const home = projectHomePath(toSlug)
  if (!fromSlug) return home
  const base = `/p/${fromSlug}`
  const path = stripOrgPrefix(currentPath)
  if (path !== base && !path.startsWith(`${base}/`)) return home
  const [surface, sub] = path.slice(base.length).split('/').filter(Boolean)
  if (!surface) return home
  if (surface === 'settings' && sub) {
    return isSurfaceMovedFromSettings(sub) ? projectPath(currentOrgSlug(), toSlug, `/${sub}`) : projectPath(currentOrgSlug(), toSlug, `/settings/${sub}`)
  }
  if (surface === 'metrics' && sub === 'fact-tables') return projectPath(currentOrgSlug(), toSlug, '/metrics/fact-tables')
  return PORTABLE_SURFACES.has(surface) ? projectPath(currentOrgSlug(), toSlug, `/${surface}`) : home
}

/**
 * Resolve which nav area (group) and item a project-scoped pathname belongs to,
 * for breadcrumbs like "project › Plan › Events". Returns null when no grouped
 * nav item matches (e.g. project general settings, overview).
 *
 * `leaf` is set only when the matched item does not name the page you are on:
 * render it as the you-are-here terminal with `label` as a crumb above it
 * ("project › Observe › Anomalies › Detection settings"), so the trail keeps
 * naming the surface that owns the route AND the page in front of the reader.
 */
export function resolveNavLocation(
  slug: string,
  pathname: string,
): { area: string; label: string; leaf?: string } | null {
  const base = `/p/${slug}`
  const path = stripOrgPrefix(pathname)
  for (const group of buildNavGroups(slug, undefined)) {
    for (const item of group.items) {
      if (!item.match(path)) continue
      const leaf = NAV_SUBSURFACE_LEAVES[path.slice(base.length)]
      return { area: group.label, label: item.label, ...(leaf ? { leaf } : {}) }
    }
  }
  return null
}

/**
 * Query params the alerting page reads to focus one delivery item and one
 * incident. These are the same two anchors an alert MESSAGE carries —
 * `ALERT_AUDIT_ITEM_PARAM` / `ALERT_INCIDENT_PARAM` in
 * backend/src/tripl/worker/tasks/metrics/urls.py — so an in-app link and a link
 * out of someone's telegram history land on exactly the same row and card.
 * Names duplicated rather than imported because nothing generates them into the
 * client; changing either side alone breaks every link already sent.
 */
export const ALERT_ITEM_PARAM = 'item'
export const ALERT_INCIDENT_PARAM = 'incident'

export interface AlertingPathAnchors {
  /** Delivery to open: the `:itemId` segment of /p/:slug/alerting/:itemId. */
  deliveryId?: string | null
  /** `${scope_type}:${scope_ref}` — the one item inside that delivery. */
  itemAnchor?: string | null
  /** Incident (correlation group) whose card carries the triage actions. */
  incidentId?: string | null
}

/**
 * Build a link into the alerting page carrying whichever anchors the caller
 * actually has. With none it is the plain page, which is what most call sites
 * want; each anchor added narrows what the reader lands on.
 *
 * The anchors are percent-encoded by URLSearchParams where the backend builder
 * keeps a literal `:` in the item value. That difference is invisible to the
 * page: it reads both through `useSearchParams`, which decodes.
 */
export function getAlertingPath(slug: string, anchors: AlertingPathAnchors = {}): string {
  const base = projectPath(currentOrgSlug(), slug, '/alerting')
  const path = anchors.deliveryId ? `${base}/${anchors.deliveryId}` : base
  const params = new URLSearchParams()
  if (anchors.itemAnchor) params.set(ALERT_ITEM_PARAM, anchors.itemAnchor)
  if (anchors.incidentId) params.set(ALERT_INCIDENT_PARAM, anchors.incidentId)
  const query = params.toString()
  return query ? `${path}?${query}` : path
}

// Activity feed ids are `<source>:<row id>`; alert rows are minted as
// `alert-delivery:<delivery id>` by `_alert_delivery_items`
// (backend/src/tripl/services/activity_service.py).
const ACTIVITY_ALERT_DELIVERY_PREFIX = 'alert-delivery:'

/** The fields {@link resolveActivityTargetPath} reads; `ActivityItem` satisfies
 * it structurally. */
export interface ActivityTargetInput {
  id: string
  type: ActivityItemType
  project_slug: string
  target_path: string | null
}

/**
 * Where an activity row should actually go.
 *
 * The feed's alert rows arrive with `target_path` = the bare
 * `/p/:slug/alerting`, which drops the reader at the top of a page
 * holding every delivery and every incident. That makes the in-app notification
 * strictly worse than the telegram message the same delivery sent, which links
 * to the exact row. The delivery id was never lost — it is
 * inside the row's own id — so rebuild the deep link here rather than land on
 * the page index.
 *
 * The incident anchor is genuinely absent: `ActivityItemResponse` carries no
 * correlation group, so a delivery-only link is the honest best. That is a
 * shape the page already handles — it opens the audit section for a
 * delivery-only link and the inbox for an incident one — and it is what alerts
 * sent before incidents existed still carry.
 *
 * Anything that is not an alert row, or whose id is not shaped as expected,
 * keeps the path the backend sent: guessing is how this defect started.
 */
export function resolveActivityTargetPath(item: ActivityTargetInput): string | null {
  // The server writes org-less `/p/…` paths; they open in the active organization.
  const serverPath = item.target_path === null ? null : withActiveOrg(item.target_path)
  if (item.type !== 'alert') return serverPath
  if (!item.id.startsWith(ACTIVITY_ALERT_DELIVERY_PREFIX)) return serverPath
  const deliveryId = item.id.slice(ACTIVITY_ALERT_DELIVERY_PREFIX.length)
  if (!deliveryId) return serverPath
  return getAlertingPath(item.project_slug, { deliveryId })
}
