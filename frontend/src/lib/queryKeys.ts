/**
 * Canonical React Query keys for the caches that have drifted.
 *
 * A query key is a string literal, so nothing catches two spellings of the same
 * cache — the reader and the writer simply stop seeing each other and the UI
 * goes quietly stale. That has now happened three times:
 *
 * - `['data-sources']` (metric card, metric form, fact-table form and list) vs
 *   `['dataSources']`, the one DataSourcesPage invalidates and `setQueryData`s
 *    — four surfaces kept showing a source you had just edited;
 * - `['plan-branches', slug]` in the sidebar switcher vs `['planBranches', slug]`
 *   invalidated by BranchesTab — creating or merging a branch
 *   left the switcher stale;
 * - three spellings of the expanded signals list.
 *
 * Importing the key instead of retyping it makes a fourth impossible: a typo is
 * a compile error rather than a silent second cache. Every key lives here now
 * — ESLint rejects an array literal as a `queryKey` anywhere else —
 * grouped by domain below.
 *
 * Within a family the narrower keys are BUILT from the wider ones
 * (`[...projectScanJobsKey(slug), scanConfigId]`), never retyped: React Query
 * matches an invalidation by prefix, so a narrower key that stopped extending
 * its prefix would silently stop being refreshed. A prefix gets its own builder
 * rather than an optional trailing argument: `key(slug)` building
 * `[family, slug, undefined]` is a three-element filter that matches no
 * branch-scoped cache (see {@link projectVariablesKey}).
 */

import { queryOptions } from '@tanstack/react-query'
import { projectsApi } from '@/api/projects'
import { projectMembersApi } from '@/api/projectMembers'
import { sourceFreshnessApi } from '@/api/sourceFreshness'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { orgRoot } from '@/lib/activeOrg'

/** Every key below starts with the organization root; see {@link orgRoot}. */
export { keySegment, orgRoot } from '@/lib/activeOrg'

/** Workspace data sources — `GET /data-sources`, one list for the whole app. */
export const dataSourcesKey = () => [...orgRoot(), 'dataSources'] as const

/** Plan branches for one project — `GET /projects/{slug}/branches`. */
export const planBranchesKey = (slug: string | undefined) => [...orgRoot(), 'planBranches', slug] as const

/** The list WITH ahead/behind counts. A sibling of `planBranchesKey`, not an
 * extension: the counted list builds one plan snapshot per open branch plus
 * one for main, with no cap, and a status change (submit, approve, request
 * changes) moves no count. So it is refreshed only by what changes a plan —
 * see `invalidateBranchCounts` in pages/settings/branches/branchQueryKeys.ts —
 * not by every invalidation of the plain list. */
export const planBranchCountsKey = (slug: string) => [...orgRoot(), 'planBranchCounts', slug] as const

/** One branch's review screen: its diff against main, detail, conflicts,
 * comments and implementation tickets. Which of them each branch action
 * refreshes lives next to the Branches tab (branches/branchQueryKeys.ts). */
export const planBranchDiffKey = (slug: string, branchId: string | undefined) =>
  [...orgRoot(), 'planBranchDiff', slug, branchId] as const
export const planBranchDetailKey = (slug: string, branchId: string) =>
  [...orgRoot(), 'planBranchDetail', slug, branchId] as const
export const planBranchConflictsKey = (slug: string, branchId: string) =>
  [...orgRoot(), 'planBranchConflicts', slug, branchId] as const
/** The "Update from main" dialog's preview. */
export const planBranchUpdatePreviewKey = (slug: string, branchId: string) =>
  [...orgRoot(), 'planBranchUpdatePreview', slug, branchId] as const
export const planBranchCommentsKey = (slug: string, branchId: string) =>
  [...orgRoot(), 'planBranchComments', slug, branchId] as const
export const planBranchTicketsKey = (slug: string, branchId: string) =>
  [...orgRoot(), 'planBranchImplementationTickets', slug, branchId] as const

/** The project's merge policy — `GET /branch-settings`. */
export const branchSettingsKey = (slug: string) => [...orgRoot(), 'branchSettings', slug] as const

/** `GET /tracker-config`, read by the branch tickets panel and TrackerConfigDialog. */
/** Every project's tracker config: what an organization's tracker-default save invalidates. */
export const trackerConfigRootKey = () => [...orgRoot(), 'trackerConfig'] as const
export const trackerConfigKey = (slug: string) => [...trackerConfigRootKey(), slug] as const

/**
 * Project variables, ITEMS ONLY — `variablesApi.list`, an array.
 *
 * The fourth drift, and the first that crashed rather than went stale: four
 * queries shared the literal `['variables', slug, branchId]`, but VariablesTab
 * fetched `listPage`, whose value is the `{items, total}` envelope, while the
 * events table, the events page data hook and the event form fetched `list`,
 * whose value is the array. One cache, two shapes — so opening
 * Settings -> Variables and then switching to Events in the sidebar handed the
 * event rows an object, and `for (const variable of variables)` threw
 * "t is not iterable" on production.
 *
 * Same spelling, different value shape: the key-spelling check below would not
 * have caught it, which is exactly why the two shapes now have two keys.
 */
export const variablesKey = (slug: string | undefined, branchId?: string | null) =>
  [...projectVariablesKey(slug), branchId] as const

/**
 * EVERY variables cache for a project, across all branches.
 *
 * For a caller that changes variables without knowing which branch is on
 * screen — the owner-only retirement pass in the project danger zone resolves
 * the branch server-side, so that button holds a slug and nothing else. React
 * Query matches invalidations by prefix and this is a strict prefix of
 * {@link variablesKey}, so one call refreshes the settings table whichever
 * branch it is showing. Invalidating `variablesKey(slug)` instead would build
 * `['variables', slug, undefined]`, which matches no branch-scoped cache at all.
 */
export const projectVariablesKey = (slug: string | undefined) => [...orgRoot(), 'variables', slug] as const

/**
 * Project variables, PAGE ENVELOPE — `variablesApi.listPage`, `{items, total}`.
 *
 * Deliberately an extension of {@link variablesKey} rather than a sibling: React
 * Query matches invalidations by prefix, so every existing
 * `invalidateQueries({ queryKey: variablesKey(...) })` still refreshes both
 * caches after a variable is created, edited or deleted.
 */
export const variablesPageKey = (slug: string | undefined, branchId?: string | null) =>
  [...variablesKey(slug, branchId), 'page'] as const

/**
 * EVERY event-type cache for a project, across all branches.
 *
 * The fifth drift, and the same prefix trap as {@link projectVariablesKey}: two
 * readers hold a project but no branch — the Scans tab, which maps type ids to
 * names for its "Review events" deep links, and the alert-rule editor's filter
 * rows — so they fetched `['eventTypes', slug]`. Every mutation invalidated the
 * branch-scoped `['eventTypes', slug, branchId]`, and a three-element filter is
 * never a prefix of a two-element key, so renaming or deleting a type left both
 * of those surfaces pointing at a name that no longer existed.
 *
 * Invalidate THIS one after a write: it is a strict prefix of
 * {@link eventTypesKey}, so it refreshes the branch-scoped caches too.
 */
export const projectEventTypesKey = (slug: string | undefined) => [...orgRoot(), 'eventTypes', slug] as const

/** Event types for one project on one branch — `eventTypesApi.list(slug, branchId)`. */
export const eventTypesKey = (slug: string | undefined, branchId?: string | null) =>
  [...projectEventTypesKey(slug), branchId] as const

/** Every metrics-catalog cache in every project. */
export const metricsCatalogRootKey = () => [...orgRoot(), 'metrics-catalog'] as const

/** Every metrics-catalog list cache for a project (filters extend the key). */
export const metricsCatalogKey = (slug: string | undefined) =>
  [...metricsCatalogRootKey(), slug] as const

/** One filtered page of the metrics catalog. */
export const metricsCatalogListKey = (
  slug: string | undefined,
  status: string,
  kind: string,
  search: string,
  review = '',
) => [...metricsCatalogKey(slug), status, kind, search, review] as const

/** One catalog metric's definition — or, without `metricId`, all of them. */
export const metricDefinitionKey = (slug: string | undefined, metricId?: string) =>
  metricId === undefined
    ? ([...orgRoot(), 'metricDefinition', slug] as const)
    : ([...orgRoot(), 'metricDefinition', slug, metricId] as const)

/** The generated batch SQL of every metric in a project. */
export const metricGeneratedSqlKey = (slug: string | undefined) =>
  [...orgRoot(), 'metric-generated-sql', slug] as const

/** The generated batch SQL one metric's card shows. */
export const metricGeneratedSqlForMetricKey = (slug: string | undefined, metricId: string) =>
  [...metricGeneratedSqlKey(slug), metricId] as const

/**
 * The drilldown caches MonitoringDetailPage fills for one entity, by scope
 * (`event`, `event_type`, `project_total`, `metric`). The page extends each with
 * its range and filters; these prefixes are what a save, a collect or the
 * realtime layer invalidates.
 */
export const monitoringSeriesRootKey = () => [...orgRoot(), 'monitoringMetrics'] as const
export const projectMonitoringSeriesKey = (slug: string | undefined) =>
  [...monitoringSeriesRootKey(), slug] as const
/** Every series of one scope — a fact collect refreshes every dependent metric. */
export const monitoringSeriesScopeKey = (slug: string | undefined, scope: string) =>
  [...projectMonitoringSeriesKey(slug), scope] as const
export const monitoringSeriesKey = (slug: string | undefined, scope: string, scopeId: string) =>
  [...monitoringSeriesScopeKey(slug, scope), scopeId] as const
export const monitoringSeriesRangeKey = (
  slug: string | undefined,
  scope: string,
  scopeId: string,
  rangeDays: number,
) => [...monitoringSeriesKey(slug, scope, scopeId), rangeDays] as const

export const projectMonitoringBreakdownsKey = (slug: string | undefined) =>
  [...orgRoot(), 'eventMetricBreakdowns', slug] as const
export const monitoringBreakdownsKey = (slug: string | undefined, scope: string, scopeId: string) =>
  [...projectMonitoringBreakdownsKey(slug), scope, scopeId] as const
export const monitoringBreakdownsColumnKey = (
  slug: string | undefined,
  scope: string,
  scopeId: string,
  column: string,
  rangeDays: number,
) => [...monitoringBreakdownsKey(slug, scope, scopeId), column, rangeDays] as const

export const projectAppVersionSeriesKey = (slug: string | undefined) =>
  [...orgRoot(), 'appVersionSeries', slug] as const
export const appVersionSeriesKey = (slug: string | undefined, scope: string, scopeId: string) =>
  [...projectAppVersionSeriesKey(slug), scope, scopeId] as const
export const appVersionSeriesRangeKey = (
  slug: string | undefined,
  scope: string,
  scopeId: string,
  scanConfigId: string | null | undefined,
  rangeDays: number,
) => [...appVersionSeriesKey(slug, scope, scopeId), scanConfigId, rangeDays] as const

/** Chart annotations shown on one entity's drilldown. */
export const projectChartAnnotationsKey = (slug: string | undefined) =>
  [...orgRoot(), 'chartAnnotations', slug] as const
/** Every planned-event list of one project (F18), for invalidation. */
export const projectPlannedEventsKey = (slug: string | undefined) =>
  [...orgRoot(), 'plannedEvents', slug] as const
/** The project-wide Annotations page's unfiltered lists. */
export const allPlannedEventsKey = (slug: string | undefined) =>
  [...projectPlannedEventsKey(slug), 'all'] as const
/** Suggested recurring windows; under the planned-event root, so planning one refreshes it. */
export const plannedWindowSuggestionsKey = (slug: string | undefined) =>
  [...projectPlannedEventsKey(slug), 'suggestions'] as const
export const allChartAnnotationsKey = (slug: string | undefined) =>
  [...projectChartAnnotationsKey(slug), 'all'] as const
export const plannedEventsRangeKey = (
  slug: string | undefined,
  scope: string,
  scopeId: string,
  rangeDays: number,
) => [...projectPlannedEventsKey(slug), scope, scopeId, rangeDays] as const
export const chartAnnotationsKey = (slug: string | undefined, scope: string, scopeId: string) =>
  [...projectChartAnnotationsKey(slug), scope, scopeId] as const
export const chartAnnotationsRangeKey = (
  slug: string | undefined,
  scope: string,
  scopeId: string,
  rangeDays: number,
) => [...chartAnnotationsKey(slug, scope, scopeId), rangeDays] as const

/**
 * Prefixes of every drilldown cache one catalog metric fills: its series, its
 * breakdowns and its app-version series (MonitoringDetailPage keys them all
 * `[family, slug, 'metric', metricId, …]`). A save that redefines the metric
 * makes the backend delete what those hold, so they must be refetched rather
 * than served stale for the minute of `staleTime`.
 */
export const metricDrilldownKeys = (slug: string | undefined, metricId: string) =>
  [
    monitoringSeriesKey(slug, 'metric', metricId),
    monitoringBreakdownsKey(slug, 'metric', metricId),
    appVersionSeriesKey(slug, 'metric', metricId),
  ] as const

/** Project templates — `GET /project-templates`, workspace-wide (F21). */
export const projectTemplatesKey = () => [...orgRoot(), 'projectTemplates'] as const

/** Every project the viewer can see — `GET /projects`, one list for the app. */
export const projectsKey = () => [...orgRoot(), 'projects'] as const

/**
 * The one definition of the projects-list query. Six components read this
 * cache; each used to redeclare it with slightly different options, so
 * whichever mounted first decided how it behaved. Spread it and override only
 * what a reader genuinely needs (`enabled: false` for a cache-only read).
 *
 * Silent for every reader, because its failure has exactly two owners that
 * render it: Layout's "Backend is unavailable" card for every page inside the
 * app shell (the workspace dashboard included — it shows no card of its own),
 * and the settings takeover's card, which mounts outside Layout. Opting out on
 * one observer was not enough — a query's meta is whichever observer set its
 * options last — so the card used to come with a toast saying the same thing.
 * A reader that adds its own error card for this query reports it twice.
 */
export const projectsQueryOptions = () =>
  queryOptions({
    queryKey: projectsKey(),
    queryFn: ({ signal }) => projectsApi.list(signal),
    meta: SILENT_ERROR_META,
  })

// --- Organizations (F20 PR7) ------------------------------------------------

/**
 * The caller's organizations — `GET /orgs`. An ACCOUNT key, like the session:
 * the list is the same whichever organization is active, so it has no
 * organization root.
 */
export const orgsKey = () => ['orgs'] as const
/** Every cached query of one organization: the root its own keys all start with. */
export const orgRootKey = (org: string) => [org] as const
/** One organization, rooted at THAT organization. */
export const orgKey = (org: string) => [org, 'org'] as const
/** One organization's own settings (mail, AI, row limits), rooted at THAT organization. */
export const orgSettingsKey = (org: string) => [org, 'orgSettings'] as const
/** An organization's Jira/Linear defaults (F20 PR12). */
export const orgTrackerDefaultsKey = (org: string) => [org, 'orgTrackerDefaults'] as const
/**
 * Every cached query whose data inherits the platform (operator) settings, in any organization:
 * an organization's settings view (its inherited values and ceilings) and the row caps the scan
 * form quotes. A platform save refreshes them all.
 */
export const inheritsPlatformSettings = (queryKey: readonly unknown[]): boolean =>
  queryKey.includes('orgSettings') || queryKey.includes('rowLimitDefaults')
/**
 * One organization's projects, named explicitly rather than by the active
 * organization: the `/p/{slug}` redirect asks every organization the user
 * belongs to whether it holds the slug. The same key {@link projectsKey}
 * builds while that organization is active, so the two share a cache.
 */
export const orgProjectsKey = (org: string) => [org, 'projects'] as const

/**
 * Every events-tab dynamics cache for a project — `eventMetricsApi.getEventsMetrics`.
 * TabMetricsCard extends it with branch, filters and range; the realtime layer
 * invalidates this prefix because the card does not poll while the stream is
 * live, so a finished scan or collection would otherwise never reach the chart.
 */
export const eventsMetricsKey = (slug: string | undefined) => [...orgRoot(), 'eventsMetrics', slug] as const

/** Every single-project cache, whichever project it holds. */
export const projectRootKey = () => [...orgRoot(), 'project'] as const

/** One project — `GET /projects/{slug}`. */
export const projectKey = (slug: string | undefined) => [...projectRootKey(), slug] as const

/**
 * The one definition of the single-project query; spread it to add `enabled`.
 * With a working branch the summary's plan counters are that branch's,
 * so the branch joins the key; main keeps the bare `projectKey`, which every
 * `projectKey(slug)` invalidation still reaches as a prefix.
 */
export const projectQueryOptions = (slug: string | undefined, branchId?: string | null) =>
  queryOptions({
    queryKey: branchId ? [...projectKey(slug), branchId] : projectKey(slug),
    queryFn: ({ signal }) => projectsApi.get(slug as string, signal, branchId),
  })

// ---------------------------------------------------------------------------
// Alerting
// ---------------------------------------------------------------------------

/**
 * The alert inbox caches for a project: the grouped inbox list, one group's
 * deliveries, and the "has this project ever delivered" probe. The realtime
 * layer invalidates all three when a delivery lands.
 */
export const alertInboxKey = (slug: string | undefined) => [...orgRoot(), 'alertInbox', slug] as const
/** The top-bar bell's open-incident slice; under the inbox prefix, so every
 * inbox invalidation refreshes it too. */
export const topbarInboxKey = (slug: string | undefined) =>
  [...alertInboxKey(slug), 'topbar'] as const
export const alertInboxGroupKey = (slug: string | undefined) => [...orgRoot(), 'alertInboxGroup', slug] as const
export const alertDeliveriesAnyKey = (slug: string | undefined) =>
  [...orgRoot(), 'alertDeliveriesAny', slug] as const

/** One incident group, as the deep-linked (pinned) copy caches it. */
export const alertInboxGroupItemKey = (
  slug: string | undefined,
  correlationGroupId: string | undefined,
) =>
  [...alertInboxGroupKey(slug), correlationGroupId] as const

/** One page of the grouped inbox. `request` is what the server is asked, so two
 * filter states that ask the same question share one cache entry. */
export const alertInboxListKey = (slug: string | undefined, status: string, request: unknown) =>
  [...alertInboxKey(slug), status, request] as const

/** Every delivery-log cache for a project. */
export const alertDeliveriesKey = (slug: string | undefined) => [...orgRoot(), 'alertDeliveries', slug] as const

/** One filtered page of the delivery log. */
export const alertDeliveriesPageKey = (slug: string | undefined, filters: unknown, offset: number) =>
  [...alertDeliveriesKey(slug), filters, offset] as const

/** The deliveries one incident sent. */
export const incidentDeliveriesKey = (slug: string | undefined, correlationGroupId: string) =>
  [...alertDeliveriesKey(slug), 'incident', correlationGroupId] as const

/** One delivery, frozen payload included. */
export const alertDeliveryKey = (slug: string | undefined, deliveryId: string | null | undefined) =>
  [...orgRoot(), 'alertDelivery', slug, deliveryId] as const

export const alertDestinationsKey = (slug: string | undefined) =>
  [...orgRoot(), 'alertDestinations', slug] as const

export const monitorsSummaryKey = (slug: string | undefined) => [...orgRoot(), 'monitors-summary', slug] as const

/** One monitor, and its history. */
export const projectMonitorKey = (slug: string | undefined) => [...orgRoot(), 'monitor', slug] as const
export const monitorDetailKey = (slug: string | undefined, monitorId: string | undefined) =>
  [...projectMonitorKey(slug), monitorId] as const
export const projectMonitorHistoryKey = (slug: string | undefined) =>
  [...orgRoot(), 'monitor-history', slug] as const
export const monitorHistoryKey = (slug: string | undefined, monitorId: string | undefined) =>
  [...projectMonitorHistoryKey(slug), monitorId] as const

/** The top bar's bell. */
export const topbarNotificationsKey = (slug: string | null | undefined) =>
  [...orgRoot(), 'topbarNotifications', slug] as const
export const topbarDeliveriesKey = (slug: string | null | undefined) =>
  [...topbarNotificationsKey(slug), 'deliveries'] as const

// ---------------------------------------------------------------------------
// Session, workspace and instance
// ---------------------------------------------------------------------------

/** Whether the instance still needs its first owner — `GET /auth/status`. */
export const authStatusKey = () => ['auth', 'status'] as const
/** One redemption of an emailed verification token — `POST /auth/verify-email/confirm`.
 * Under `auth` so signing in or out never drops the answer the page is showing. */
export const verifyEmailKey = (token: string) => ['auth', 'verify-email', token] as const

export const usersKey = () => [...orgRoot(), 'users'] as const
/** Who belongs to one project — `GET /projects/{slug}/members`. The reviewer
 * and event-type owner pickers read it too, since only members can be picked. */
export const projectMembersKey = (slug: string | undefined) => [...orgRoot(), 'projectMembers', slug] as const
/** The one definition of the project members query; spread it to add `enabled`. */
export const projectMembersQueryOptions = (slug: string | undefined) =>
  queryOptions({
    queryKey: projectMembersKey(slug),
    queryFn: ({ signal }) => projectMembersApi.list(slug as string, signal),
  })
export const invitationsKey = () => [...orgRoot(), 'invitations'] as const
export const invitationPreviewKey = (token: string | undefined) =>
  [...orgRoot(), 'invitationPreview', token] as const
export const apiKeysKey = () => [...orgRoot(), 'api-keys'] as const
export const serviceSettingsKey = () => [...orgRoot(), 'serviceSettings'] as const
/** The operator's usage-telemetry status (Settings › Platform › Runtime). */
export const telemetryStatusKey = () => [...serviceSettingsKey(), 'telemetry'] as const
/** The built-in AI prompts: fixed per deploy, so outside the settings root. */
export const aiPromptDefaultsKey = () => [...orgRoot(), 'aiPromptDefaults'] as const
/** The instance row caps, readable by any signed-in user (scan form hints). */
export const rowLimitDefaultsKey = () => [...orgRoot(), 'rowLimitDefaults'] as const

/** AI availability. The root is what a settings write invalidates, so every
 * project's copy refreshes. */
export const aiStatusRootKey = () => [...orgRoot(), 'aiStatus'] as const
export const aiStatusKey = (slug: string | null | undefined) => [...aiStatusRootKey(), slug] as const

/** The activity rail: one project's feed, or the workspace feed without one. */
export const activityKey = (slug: string | undefined) =>
  [...orgRoot(), 'activity', slug ?? 'workspace'] as const
/** A shorter page of the same feed (Overview); under the rail's key, so it refreshes with it. */
export const activityPreviewKey = (slug: string | undefined, limit: number) =>
  [...activityKey(slug), 'preview', limit] as const

/** Command palette search; the root is what a reindex invalidates. */
export const commandPaletteSearchRootKey = () => [...orgRoot(), 'commandPaletteSearch'] as const
export const commandPaletteSearchKey = (slug: string | null | undefined, query: string) =>
  [...commandPaletteSearchRootKey(), slug, query] as const
/** The keyword-only answer shown until the full one arrives. */
export const commandPaletteLexicalSearchKey = (slug: string | null | undefined, query: string) =>
  [...commandPaletteSearchKey(slug, query), 'lexical'] as const

export const dataSourceSchemaKey = (dataSourceId: string | null | undefined) =>
  [...orgRoot(), 'data-source-schema', dataSourceId] as const

// `source` is an `AuditSource.key`: one project's history or the organization's.
export const auditKey = (source: string, params: unknown) =>
  [...orgRoot(), 'audit', source, params] as const
export const auditActionsKey = () => [...orgRoot(), 'auditActions'] as const
export const auditEntryKey = (source: string, entryId: string | null) =>
  [...orgRoot(), 'auditEntry', source, entryId] as const

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

/** Every events-list cache in every project. */
export const eventsRootKey = () => [...orgRoot(), 'events'] as const
export const projectEventsKey = (slug: string | undefined) => [...eventsRootKey(), slug] as const
export const branchEventsKey = (slug: string | undefined, branchId: string | null | undefined) =>
  [...projectEventsKey(slug), branchId] as const

/** The events table: one branch's list under the table's filters and sort. */
export const eventsListKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  filters: {
    filterEtId: string | undefined
    debouncedSearch: string
    queryStatuses: readonly string[] | undefined
    filterTag: string
    filterSilentDays: number | undefined
    filterReviewed: boolean | undefined
    filterOpenQuestions: boolean | undefined
    sort: string
    /** F23; last and only when set, so every other key keeps its shape. */
    filterProperty?: string
  },
) =>
  [
    ...branchEventsKey(slug, branchId),
    filters.filterEtId,
    filters.debouncedSearch,
    filters.queryStatuses,
    filters.filterTag,
    filters.filterSilentDays,
    filters.filterReviewed,
    filters.filterOpenQuestions,
    filters.sort,
    ...(filters.filterProperty ? [filters.filterProperty] : []),
  ] as const

/**
 * The sample of a type's events the single-event form reads the naming
 * convention off. Under the branch's events, so a create refreshes it.
 */
export const eventNameSampleKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  eventTypeId: string,
) => [...branchEventsKey(slug, branchId), 'nameSample', eventTypeId] as const

/** A search-as-you-type event picker; `picker` names which one. */
export const eventsPickerKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  picker: 'alert-filter' | 'override-picker' | 'successor-picker' | 'metric-picker' | 'property-picker',
  search: string,
) => [...branchEventsKey(slug, branchId), picker, search] as const

/** How many events on a branch wait for review. */
export const eventsInReviewCountKey = (slug: string | undefined, branchId: string | null | undefined) =>
  [...branchEventsKey(slug, branchId), 'inReviewCount'] as const

/** One event. */
export const projectEventKey = (slug: string | undefined) => [...orgRoot(), 'event', slug] as const
export const branchEventKey = (slug: string | undefined, branchId: string | null | undefined) =>
  [...projectEventKey(slug), branchId] as const
export const eventKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  eventId: string | null | undefined,
) => [...branchEventKey(slug, branchId), eventId] as const

export const projectEventTagsKey = (slug: string | undefined) => [...orgRoot(), 'eventTags', slug] as const
export const eventTagsKey = (slug: string | undefined, branchId: string | null | undefined) =>
  [...projectEventTagsKey(slug), branchId] as const

export const projectEventHistoryKey = (slug: string | undefined) => [...orgRoot(), 'eventHistory', slug] as const
export const branchEventHistoryKey = (slug: string | undefined, branchId: string | null | undefined) =>
  [...projectEventHistoryKey(slug), branchId] as const
export const eventHistoryKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  eventId: string,
) => [...branchEventHistoryKey(slug, branchId), eventId] as const

/** Every identity probe on one branch, whatever the type and name — what a
 *  create invalidates, since the name it just took is no longer free. */
export const branchEventIdentityProbesKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
) => [...orgRoot(), 'eventIdentityProbe', slug, branchId] as const

/** Which of a list of names already identify events of that type (one
 *  exact-name lookup); under the branch's probes, so a create invalidates it. */
export const eventIdentityLookupKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  eventTypeId: string,
  names: readonly string[],
) => [...branchEventIdentityProbesKey(slug, branchId), eventTypeId, 'names', names] as const

/** Whether a typed name already identifies an event of that type. */
export const eventIdentityProbeKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  eventTypeId: string,
  name: string | null,
) => [...orgRoot(), 'eventIdentityProbe', slug, branchId, eventTypeId, name] as const

export const eventImplementationTicketsKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  eventId: string,
) => [...orgRoot(), 'eventImplementationTickets', slug, branchId, eventId] as const

/** A deprecated event's traffic next to its successor's (#258). */
export const eventMigrationKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  eventId: string,
) => [...orgRoot(), 'eventMigration', slug, branchId, eventId] as const

/** The identities a bulk create would collide with. */
export const bulkIdentitiesKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  eventTypeId: string,
) => [...orgRoot(), 'bulkIdentities', slug, branchId, eventTypeId] as const

export const eventCommentsKey = (slug: string, eventId: string) =>
  [...orgRoot(), 'eventComments', slug, eventId] as const
export const eventPhotosKey = (slug: string, eventId: string) =>
  [...orgRoot(), 'eventPhotos', slug, eventId] as const
export const eventPhotoCommentsKey = (slug: string, eventId: string, photoId: string) =>
  [...orgRoot(), 'eventPhotoComments', slug, eventId, photoId] as const
/** The instance-wide photo upload limit; the same for every project and event. */
export const photoLimitsKey = () => [...orgRoot(), 'photoLimits'] as const

/** Window metrics of the visible event rows, one cache per row bucket. */
export const projectEventWindowMetricsKey = (slug: string | undefined) =>
  [...orgRoot(), 'eventWindowMetrics', slug] as const
export const eventWindowMetricsKey = (slug: string | undefined, bucketIds: readonly string[]) =>
  [...projectEventWindowMetricsKey(slug), bucketIds.join(',')] as const

/**
 * Active signals. One family for every surface — the Events tabs and rows, and
 * the expanded list the bell, Overview and Anomalies share —
 * so a single prefix refreshes them all.
 */
export const activeSignalsRootKey = () => [...orgRoot(), 'activeSignals'] as const
export const activeSignalsKey = (slug: string | undefined) =>
  [...activeSignalsRootKey(), slug] as const
export const expandedSignalsKey = (slug: string | undefined) =>
  [...activeSignalsKey(slug), 'expanded'] as const
export const eventsTabSignalsKey = (slug: string | undefined) =>
  [...activeSignalsKey(slug), 'tabs'] as const
/** Verdict counts per project (#254, read by the health score); under the
 * signals family, so a verdict or a signals.updated refreshes it with the lists. */
export const signalVerdictCountsKey = (slug: string | undefined) =>
  [...activeSignalsKey(slug), 'verdictCounts'] as const
export const eventRowSignalsKey = (slug: string | undefined, bucketIds: readonly string[]) =>
  [...activeSignalsKey(slug), 'rows', bucketIds.join(',')] as const
/** Anomalies row sparklines: a signals invalidation refreshes them too. */
export const signalSeriesKey = (slug: string | undefined, rowKeys: readonly string[]) =>
  [...activeSignalsKey(slug), 'series', rowKeys.join(',')] as const

export const eventTypeDriftsRootKey = () => [...orgRoot(), 'eventTypeDrifts'] as const
export const eventTypeDriftsKey = (slug: string | undefined, eventTypeId: string) =>
  [...eventTypeDriftsRootKey(), slug, eventTypeId] as const

/** Every event-type cache in every project. */
export const eventTypesRootKey = () => [...orgRoot(), 'eventTypes'] as const

export const projectEventTypeOwnersKey = (slug: string | undefined) =>
  [...orgRoot(), 'eventTypeOwners', slug] as const
export const eventTypeOwnersKey = (slug: string | undefined, eventTypeId: string) =>
  [...projectEventTypeOwnersKey(slug), eventTypeId] as const

export const eventTypeDeletionImpactKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  eventTypeId: string,
) => [...orgRoot(), 'eventTypeDeletionImpact', slug, branchId, eventTypeId] as const

export const projectMetaFieldsKey = (slug: string | undefined) => [...orgRoot(), 'metaFields', slug] as const
export const metaFieldsKey = (slug: string | undefined, branchId: string | null | undefined) =>
  [...projectMetaFieldsKey(slug), branchId] as const

export const projectRelationsKey = (slug: string | undefined) => [...orgRoot(), 'relations', slug] as const
export const relationsKey = (slug: string | undefined, branchId: string | null | undefined) =>
  [...projectRelationsKey(slug), branchId] as const

export const projectPlanRevisionsKey = (slug: string | undefined) =>
  [...orgRoot(), 'planRevisions', slug] as const
export const planRevisionsKey = (slug: string | undefined, offset: number) =>
  [...projectPlanRevisionsKey(slug), offset] as const
export const planRevisionDiffKey = (
  slug: string | undefined,
  revisionId: string | null | undefined,
  compareTo: string | null | undefined,
) => [...orgRoot(), 'planRevisionDiff', slug, revisionId, compareTo] as const

/** Variables settings table, filtered by usage. */
export const variablesUsagePageKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  usageFilter: string,
) => [...variablesPageKey(slug, branchId), usageFilter] as const

export const branchVariableDriftsKey = (slug: string | undefined, branchId: string | null | undefined) =>
  [...orgRoot(), 'variable-drifts', slug, branchId] as const
export const variableDriftsKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  variableId: string,
) => [...branchVariableDriftsKey(slug, branchId), variableId] as const
/** The drifts one event's values show. */
export const eventVariableDriftsKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  eventId: string,
) => [...branchVariableDriftsKey(slug, branchId), 'event', eventId] as const

/**
 * Property drift (F23, #306) — `propertyDriftsApi.list`. Main-only on the
 * server (drift is detected against main), so no branch segment. Every list
 * extends the project prefix, so one invalidation after a triage refreshes
 * the event page's list and the Properties page's roster together.
 */
export const projectPropertyDriftsKey = (slug: string | undefined) =>
  [...orgRoot(), 'property-drifts', slug] as const
/** The active drifts of one event. */
export const eventPropertyDriftsKey = (slug: string | undefined, eventId: string) =>
  [...projectPropertyDriftsKey(slug), 'event', eventId] as const
/** The active type changes of the project (per property, no event). */
export const typeChangePropertyDriftsKey = (slug: string | undefined) =>
  [...projectPropertyDriftsKey(slug), 'type_change'] as const
/** Every active drift of the project. */
export const activePropertyDriftsKey = (slug: string | undefined) =>
  [...projectPropertyDriftsKey(slug), 'active'] as const

export const variableOverridesKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  variableId: string,
) => [...orgRoot(), 'variable-overrides', slug, branchId, variableId] as const

/** Every property-list read on a branch, from either side (F23): one prefix to
 *  invalidate after any entry write. The per-property overrides list reads the
 *  same rows and sits under `variableOverridesKey`. */
export const branchPropertyEntriesKey = (slug: string | undefined, branchId: string | null | undefined) =>
  [...orgRoot(), 'property-entries', slug, branchId] as const
/** An event's property list. */
export const eventPropertiesKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  eventId: string,
) => [...branchPropertyEntriesKey(slug, branchId), 'event', eventId] as const
/** The events one property is on. */
export const propertyEventsKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  variableId: string,
) => [...branchPropertyEntriesKey(slug, branchId), 'property', variableId] as const
/** Prefix of every `variableOverridesKey` on a branch. */
export const branchVariableOverridesKey = (slug: string | undefined, branchId: string | null | undefined) =>
  [...orgRoot(), 'variable-overrides', slug, branchId] as const

export const branchVariableValuesKey = (slug: string | undefined, branchId: string | null | undefined) =>
  [...orgRoot(), 'variable-values', slug, branchId] as const
export const variableValuesKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  variableId: string,
) => [...branchVariableValuesKey(slug, branchId), variableId] as const

/** The events-tab dynamics chart under the table's filters and a live range. */
export const eventsMetricsChartKey = (
  slug: string | undefined,
  branchId: string | null,
  filters: {
    filterEtId: string | undefined
    debouncedSearch: string
    queryStatuses: readonly string[] | undefined
    filterTag: string
  },
  range: { from: string; to: string },
) =>
  [
    ...eventsMetricsKey(slug),
    branchId,
    filters.filterEtId,
    filters.debouncedSearch,
    filters.queryStatuses,
    filters.filterTag,
    range.from,
    range.to,
  ] as const

// ---------------------------------------------------------------------------
// Scans
// ---------------------------------------------------------------------------

export const scansKey = (slug: string | undefined) => [...orgRoot(), 'scans', slug] as const
export const scanConfigKey = (slug: string | undefined, scanConfigId: string | null | undefined) =>
  [...orgRoot(), 'scanConfig', slug, scanConfigId] as const

/** Scan jobs: every scan's, one scan's, and one scan's capped list. */
export const projectScanJobsKey = (slug: string | undefined) => [...orgRoot(), 'scanJobs', slug] as const
export const scanJobsKey = (slug: string | undefined, scanConfigId: string) =>
  [...projectScanJobsKey(slug), scanConfigId] as const
export const scanJobsLimitedKey = (slug: string | undefined, scanConfigId: string, limit: number) =>
  [...scanJobsKey(slug, scanConfigId), { limit }] as const

/**
 * `GET /scans/activity` (every scan's latest job, exact failing streak and 24h
 * rows). Under the `['scanJobs', slug]` prefix on purpose, so the stream's
 * scan-job invalidation reaches it; a mutation on ONE scan invalidates only
 * `scanJobsKey(slug, id)`, so it must invalidate this key too.
 */
export const scanActivityKey = (slug: string | undefined) =>
  [...projectScanJobsKey(slug), 'activity'] as const

/**
 * `GET /projects/{slug}/source-freshness` (F16, #269). Under the
 * `['scanJobs', slug]` prefix on purpose: freshness moves when a metrics
 * collection finishes, and the stream's scan-job invalidation already reaches
 * that prefix.
 */
export const sourceFreshnessKey = (slug: string | undefined) =>
  [...projectScanJobsKey(slug), 'sourceFreshness'] as const

/**
 * The one shape every freshness reader uses. Silent: freshness is a hint laid
 * over pages that have their own content, so a failed read renders nothing
 * rather than a toast about a chip.
 */
export const sourceFreshnessQueryOptions = (slug: string) =>
  queryOptions({
    queryKey: sourceFreshnessKey(slug),
    queryFn: ({ signal }) => sourceFreshnessApi.list(slug, signal),
    meta: SILENT_ERROR_META,
    staleTime: 60_000,
  })

export const platformPresenceKey = (slug: string | undefined, scanConfigId: string) =>
  [...orgRoot(), 'platformPresence', slug, scanConfigId] as const

/** The demo scenario's watchers on a scan run and a metric collection. */
export const demoScenarioScanWatchKey = (slug: string | undefined, scanJobId: string | undefined) =>
  [...orgRoot(), 'demo-scenario-scan-watch', slug, scanJobId] as const
export const demoScenarioCollectWatchKey = (
  slug: string | undefined,
  metricId: string | undefined,
  startedAt: number | undefined,
) => [...orgRoot(), 'demo-scenario-collect-watch', slug, metricId, startedAt] as const

/** The watcher that follows a metric collection to its end. */
export const metricCollectWatchKey = (
  slug: string | undefined,
  metricId: string | undefined,
  startedAt: number | undefined,
) => [...orgRoot(), 'metric-collect-watch', slug, metricId, startedAt] as const

// ---------------------------------------------------------------------------
// Metrics, fact tables and monitoring
// ---------------------------------------------------------------------------

export const factTablesKey = (slug: string | undefined) => [...orgRoot(), 'fact-tables', slug] as const
export const projectFactTableKey = (slug: string | undefined) => [...orgRoot(), 'fact-table', slug] as const
export const factTableKey = (slug: string | undefined, factTableId: string | null | undefined) =>
  [...projectFactTableKey(slug), factTableId] as const

export const projectAppVersionAdoptionKey = (slug: string | undefined) =>
  [...orgRoot(), 'appVersionAdoption', slug] as const
export const appVersionAdoptionKey = (
  slug: string | undefined,
  scanConfigId: string | null | undefined,
  rangeDays: number,
) => [...projectAppVersionAdoptionKey(slug), scanConfigId, rangeDays] as const

export const projectTopMoversKey = (slug: string | undefined) => [...orgRoot(), 'topMovers', slug] as const
export const topMoversKey = (
  slug: string | undefined,
  scanConfigId: string | null | undefined,
  scopeType: string,
  scopeRef: string,
  bucket: string | null | undefined,
  limit: number,
) => [...projectTopMoversKey(slug), scanConfigId, scopeType, scopeRef, bucket, limit] as const

export const projectBreakdownTimelineKey = (slug: string | undefined) =>
  [...orgRoot(), 'breakdownTimeline', slug] as const
/** Keyed on the range LENGTH, not the live bounds: those step every five
 * minutes, and a key that moved with them refetched the timeline each time. */
export const breakdownTimelineKey = (
  slug: string | undefined,
  scanConfigId: string | null | undefined,
  scopeType: string,
  scopeRef: string,
  breakdownColumn: string,
  breakdownValue: string,
  isOther: boolean,
  rangeDays: number | undefined,
) =>
  [
    ...projectBreakdownTimelineKey(slug),
    scanConfigId,
    scopeType,
    scopeRef,
    breakdownColumn,
    breakdownValue,
    isOther,
    rangeDays,
  ] as const

export const projectSeasonalityKey = (slug: string | undefined) => [...orgRoot(), 'seasonality', slug] as const
export const seasonalityKey = (
  slug: string | undefined,
  scanConfigId: string | null | undefined,
  scopeType: string,
  scopeRef: string,
  rangeDays: number,
) => [...projectSeasonalityKey(slug), scanConfigId, scopeType, scopeRef, rangeDays] as const

export const projectReleaseRegressionsKey = (slug: string | undefined) =>
  [...orgRoot(), 'releaseRegressions', slug] as const
export const releaseRegressionsKey = (slug: string | undefined, scanConfigId: string | null | undefined) =>
  [...projectReleaseRegressionsKey(slug), scanConfigId] as const

export const distributionDriftsRootKey = () => [...orgRoot(), 'distributionDrifts'] as const
export const projectDistributionDriftsKey = (slug: string | undefined) =>
  [...distributionDriftsRootKey(), slug] as const
export const distributionDriftsKey = (slug: string | undefined, scope: unknown, rangeDays: number) =>
  [...projectDistributionDriftsKey(slug), scope, rangeDays] as const

export const anomalyScopeOverridesKey = (slug: string | undefined) =>
  [...orgRoot(), 'anomalyScopeOverrides', slug] as const
export const projectAnomalySettingsKey = (slug: string | undefined) =>
  [...orgRoot(), 'projectAnomalySettings', slug] as const
/** The fixed list of countries a holiday calendar can use. */
export const holidayCountriesKey = (slug: string | undefined) =>
  [...orgRoot(), 'holidayCountries', slug] as const

/** The Overview page. Keyed kind-first, so the root covers every project's. */
export const overviewRootKey = () => [...orgRoot(), 'overview'] as const
/** Every window of one project's Overview volume chart. */
export const overviewVolumeRootKey = (slug: string | undefined) =>
  [...overviewRootKey(), 'volume', slug] as const
export const overviewVolumeKey = (slug: string | undefined, windowDays: number) =>
  [...overviewVolumeRootKey(slug), windowDays] as const
export const overviewTopEventsKey = (slug: string | undefined) =>
  [...overviewRootKey(), 'top-events', slug] as const
export const overviewKpiSeriesKey = (slug: string | undefined) =>
  [...overviewRootKey(), 'kpi-series', slug] as const

/** Reconciliation. Keyed kind-first like the Overview. */
export const reconciliationRootKey = () => [...orgRoot(), 'reconciliation'] as const
export const reconciliationCoverageKey = (slug: string | undefined, days: number) =>
  [...reconciliationRootKey(), 'coverage', slug, days] as const
export const projectDeadEventsKey = (slug: string | undefined) =>
  [...reconciliationRootKey(), 'dead', slug] as const
export const deadEventsKey = (slug: string | undefined, days: number) =>
  [...projectDeadEventsKey(slug), days] as const
export const projectShadowEventsKey = (slug: string | undefined) =>
  [...reconciliationRootKey(), 'shadow', slug] as const
export const shadowEventsKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  status: string,
) => [...projectShadowEventsKey(slug), branchId, status] as const
/** A shadow-events list read a page at a time by offset. */
export const shadowEventsPagesKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  status: string,
) => [...shadowEventsKey(slug, branchId, status), 'pages'] as const
/** One page size of a shadow-events list; "Show more" raises `limit`. */
export const shadowEventsPageKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  status: string,
  limit: number,
) => [...shadowEventsKey(slug, branchId, status), limit] as const

/** Dependency graph (F04, #257). One family, so a plan write can refresh every
 * "Used by" list, confirm-dialog impact and branch Impact panel at once. */
export const dependenciesRootKey = () => [...orgRoot(), 'dependencies'] as const
export const projectDependenciesKey = (slug: string | undefined) =>
  [...dependenciesRootKey(), slug] as const
/** Every dependency cache for one branch (null = main). */
export const branchDependenciesKey = (slug: string | undefined, branchId: string | null | undefined) =>
  [...projectDependenciesKey(slug), branchId] as const
export const entityDependenciesKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  entity: string,
  depth: number,
) => [...branchDependenciesKey(slug, branchId), 'entity', entity, depth] as const
/** The impact of a planned change set; `changes` is its canonical JSON. */
export const impactKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  changes: string,
) => [...branchDependenciesKey(slug, branchId), 'impact', changes] as const
export const branchImpactKey = (slug: string | undefined, branchId: string) =>
  [...branchDependenciesKey(slug, branchId), 'branchImpact'] as const

/** The reader's own notifications (#259): the list, its unread count and the
 * email prefs share one prefix, so marking read refreshes all of them. */
export const myNotificationsRootKey = () => [...orgRoot(), 'myNotifications'] as const
export const myNotificationsListKey = (unreadOnly: boolean) =>
  [...myNotificationsRootKey(), 'list', unreadOnly] as const
export const myNotificationsUnreadCountKey = () =>
  [...myNotificationsRootKey(), 'unreadCount'] as const
export const myNotificationPrefsKey = () => [...orgRoot(), 'myNotificationPrefs'] as const
/** Every watch state the reader holds in one project. */
export const projectSubscriptionsKey = (slug: string | undefined) => [...orgRoot(), 'subscription', slug] as const
/** The reader's watch state on one entity — `GET /projects/{slug}/subscriptions/{type}/{id}`. */
export const subscriptionKey = (slug: string | undefined, entityType: string, entityId: string | undefined) =>
  [...projectSubscriptionsKey(slug), entityType, entityId] as const

/**
 * Duplicate detection (F12, #265). Under the branch's events prefix, so any
 * event write — a create, a deprecate, a successor — refreshes what the form
 * and the Duplicates page say about the catalog.
 */
export const branchDuplicatesKey = (slug: string | undefined, branchId: string | null | undefined) =>
  [...branchEventsKey(slug, branchId), 'duplicates'] as const
/** One duplicate-check request; `payload` is the candidates, serialized. */
export const duplicateCheckKey = (
  slug: string | undefined,
  branchId: string | null | undefined,
  payload: string,
) => [...branchDuplicatesKey(slug, branchId), 'check', payload] as const
/** The Duplicates page's cluster pages. */
export const duplicateClustersKey = (slug: string | undefined, branchId: string | null | undefined) =>
  [...branchDuplicatesKey(slug, branchId), 'clusters'] as const

/**
 * Plan health score (F15, #268). Health is about the MAIN plan only, so the
 * family sits beside the branch caches under the project's events prefix
 * (`['events', slug, 'health', …]`), not under one of them: an invalidation of
 * `projectEventsKey(slug)` refreshes it, while the optimistic patches that walk
 * `branchEventsKey(slug, branchId)` expecting list pages never reach it.
 */
export const projectHealthRootKey = (slug: string | undefined) =>
  [...projectEventsKey(slug), 'health'] as const
/** The catalog's batch read for one chunk of loaded ids. */
export const eventsHealthKey = (slug: string | undefined, ids: readonly string[]) =>
  [...projectHealthRootKey(slug), 'events', ids.join(',')] as const
/** One event's breakdown — `GET /projects/{slug}/events/{id}/health`. */
export const eventHealthKey = (slug: string | undefined, eventId: string | undefined) =>
  [...projectHealthRootKey(slug), 'event', eventId] as const
/** `GET /projects/{slug}/health/event-types`. */
export const eventTypesHealthKey = (slug: string | undefined) =>
  [...projectHealthRootKey(slug), 'eventTypes'] as const
/** `GET /projects/{slug}/health?trend_days=N`. */
export const projectHealthKey = (slug: string | undefined, trendDays: number) =>
  [...projectHealthRootKey(slug), 'project', trendDays] as const
