import { EDIT_PAGE_TITLE_PREFIX } from '@/components/shell-chrome-context'
import { NOT_FOUND_TITLE_LABEL } from '@/hooks/useDocumentTitle'
import {
  buildNavGroups,
  currentOrgSlug,
  projectHomePath,
  projectPath,
  resolveNavLocation,
  resolveProjectEditorPage,
  stripOrgPrefix,
} from '@/lib/navigation'

/**
 * One top-bar crumb. With `to` it is a link to that surface ("Observe ›
 * Alerting › Rules" where Alerting and Rules open their lists); a nav
 * group ("Plan", "Observe") is not a page and stays plain text.
 */
export type Crumb = { label: string; to?: string }

/**
 * A crumb for a sidebar surface, linked to the same href the sidebar uses, so
 * the trail and the nav cannot point at different pages. A label the nav does
 * not know stays plain.
 */
export function navCrumb(slug: string | undefined, label: string): Crumb {
  if (!slug) return { label }
  for (const group of buildNavGroups(slug, undefined)) {
    const item = group.items.find((candidate) => candidate.label === label)
    if (item) return { label, to: item.href }
  }
  return { label }
}

/** What the address alone says the top bar shows: the trail, and the page's own title. */
export type RouteCrumbs = {
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

// Old top-level addresses that only redirect into Settings, which mounts
// outside the shell; their one frame in it is named for where they go.
const SETTINGS_REDIRECT_ROUTE = /^\/(?:data-sources|users|account)(?:\/|$)/

// Concepts sits below the sidebar divider rather than inside the Plan / Observe
// / Govern nav, so `resolveNavLocation` cannot name it. Without this it fell
// through to the catch-all and claimed to be "Overview". The
// area label matches the page's own eyebrow (ConceptsPage `PageHead`).
const CONCEPTS_AREA = 'Help & reference'

// One row of a list surface — a plan branch, an event type, a property, a
// scan, a note: "Plan › Event types › <name>", the page naming the row once it
// has loaded (#243). Without it the list's crumb was the one replaced by the
// row's name, and the trail skipped the list ("Plan › Screen View").
const LIST_ROW_ROUTE = /^\/p\/[^/]+\/(?:branches|event-types|variables|scans|docs)\/[^/]/

/** Project settings proper, which hands general and plan rules to the takeover. */
const PROJECT_SETTINGS_ROUTE = /^\/p\/[^/]+\/settings(?:\/|$)/

/**
 * The top bar's trail and title for a route. Pure: it reads the address, the
 * project's slug and its name, and the sidebar's nav model.
 */
export function resolveCrumbs(fullPathname: string, slug?: string, projectName?: string): RouteCrumbs {
  // `/o/{org}/p/…` reads as `/p/…`, and `/o/{org}` as the workspace (F20 PR7).
  const pathname = stripOrgPrefix(fullPathname)
  if (WORKSPACE_PATHS.includes(pathname)) return { crumbs: [], title: WORKSPACE_TITLE }
  if (SETTINGS_REDIRECT_ROUTE.test(pathname)) return { crumbs: [], title: 'Settings' }

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
  // a generic "Detail"; the area's page then stands in as the title. An
  // event's catalog detail is served under /monitoring/event/<id> (the
  // canonical event route), but it belongs to Plan › Events — only
  // project-total/event-type signal detail falls through to the generic
  // branch. Check the event scope first.
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
  // An alert rule's history: "Observe › Alerting › Rules › <rule>", the tab
  // the rule lives on, instead of "Observe › <rule>" (#241, #238).
  if (/^\/p\/[^/]+\/monitors\/[^/]+/.test(pathname)) {
    const alerting = nav('Alerting')
    const rules: Crumb = alerting.to ? { label: 'Rules', to: `${alerting.to}?section=monitors` } : { label: 'Rules' }
    return { crumbs: withProject('Observe', alerting, rules), title: DETAIL_PENDING_TITLE }
  }

  // Map the route to its grouped-nav area (Plan / Observe / Govern) using the
  // same model the sidebar renders from.
  const navLocation = slug ? resolveNavLocation(slug, pathname) : null
  if (navLocation) {
    const surface = nav(navLocation.label)
    // Create and edit pages name themselves under their surface instead of
    // passing for its list — "Events › New event", "Metrics › Fact tables ›
    // Edit fact table" (#246) — in the words the browser tab uses too.
    const editor = resolveProjectEditorPage(pathname)
    if (editor) {
      const parent: Crumb[] = editor.parent
        ? [{ label: editor.parent.label, ...(slug ? { to: projectPath(currentOrgSlug(), slug, editor.parent.path) } : {}) }]
        : []
      return {
        crumbs: withProject(navLocation.area, surface, ...parent),
        title: editor.title,
        ...(editor.entityAction ? { entityAction: editor.entityAction } : {}),
      }
    }
    if (LIST_ROW_ROUTE.test(pathname)) {
      return { crumbs: withProject(navLocation.area, surface), title: DETAIL_PENDING_TITLE }
    }
    // A sub-surface names itself: the nav item it matched is its parent, not the
    // page. Without the leaf, Detection settings presented itself as Anomalies.
    // `leaf` is absent everywhere else, so nothing else moves.
    return navLocation.leaf
      ? { crumbs: withProject(navLocation.area, surface), title: navLocation.leaf }
      : { crumbs: withProject(navLocation.area), title: navLocation.label }
  }

  if (pathname.endsWith('/concepts')) {
    return { crumbs: withProject(CONCEPTS_AREA), title: 'Concepts' }
  }
  if (PROJECT_SETTINGS_ROUTE.test(pathname)) {
    return { crumbs: withProject(), title: 'Settings' }
  }
  // Nothing claimed this path, which is exactly what the catch-all route renders
  // NotFoundPage for — so the trail says so, in the page's own words, instead
  // of naming a page ("Overview", "Settings") the user is not on.
  return { crumbs: withProject(), title: NOT_FOUND_TITLE_LABEL }
}

/**
 * The trail and title the top bar shows once the page has named its entity
 * (usePageTitle), or not. A detail route whose entity has not named itself —
 * still loading, or it failed to load — promotes its last crumb to the title:
 * "Plan › Events" rather than "Plan › Events ›" with nothing after the
 * chevron, and a page name on phones, where the crumbs are hidden. An editor
 * that has named its entity reads "… › <entity> › Edit".
 */
export function topBarHeading(route: RouteCrumbs, pageTitle: string | null): { crumbs: Crumb[]; title: string } {
  const { crumbs, title, entityAction } = route
  const editedEntity =
    entityAction && pageTitle?.startsWith(EDIT_PAGE_TITLE_PREFIX)
      ? pageTitle.slice(EDIT_PAGE_TITLE_PREFIX.length)
      : null
  if (editedEntity && entityAction) {
    return { crumbs: [...crumbs, { label: editedEntity }], title: entityAction }
  }
  const entityTitle = pageTitle ?? title
  return {
    crumbs: entityTitle ? crumbs : crumbs.slice(0, -1),
    title: entityTitle || (crumbs[crumbs.length - 1]?.label ?? ''),
  }
}
