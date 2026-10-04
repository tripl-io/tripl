/**
 * Centralized event → query-key invalidation map.
 *
 * One place that maps each server event type to the set of React-Query keys that
 * must be refreshed, replacing per-widget polling. Keys are targeted by PREFIX
 * (react-query's default `invalidateQueries` match), so an entry like
 * `['scans', slug]` refreshes every query whose key starts with it
 * (`['scans', slug, ...]`). This tolerates the small divergence in existing keys
 * (some carry a sub-segment before the slug, e.g. `['overview', 'volume', slug]`)
 * without a risky rewrite of every call site: broad roots like `['overview']`
 * and `['reconciliation']` cover all their sub-keys, and only the mounted
 * project's queries exist to match.
 */

import type { QueryClient, QueryKey } from '@tanstack/react-query'
import { refreshEventsLists } from '@/lib/eventsListCache'
import {
  activeSignalsKey,
  activityKey,
  alertDeliveriesAnyKey,
  alertDeliveriesKey,
  alertInboxGroupKey,
  alertInboxKey,
  eventsMetricsKey,
  keySegment,
  metricDefinitionKey,
  metricsCatalogKey,
  monitorsSummaryKey,
  overviewRootKey,
  overviewTopEventsKey,
  overviewVolumeRootKey,
  projectAppVersionAdoptionKey,
  projectAppVersionSeriesKey,
  projectBreakdownTimelineKey,
  projectChartAnnotationsKey,
  projectDistributionDriftsKey,
  projectEventHistoryKey,
  projectEventKey,
  projectEventsKey,
  projectEventTypesKey,
  projectEventWindowMetricsKey,
  projectKey,
  projectMonitorHistoryKey,
  projectMonitoringBreakdownsKey,
  projectMonitoringSeriesKey,
  projectMonitorKey,
  projectReleaseRegressionsKey,
  projectScanJobsKey,
  projectSeasonalityKey,
  projectsKey,
  projectTopMoversKey,
  reconciliationRootKey,
  scansKey,
  topbarNotificationsKey,
} from '@/lib/queryKeys'

export const PROJECT_EVENT_TYPES = [
  'scan_job.updated',
  'metric_collection.updated',
  'activity.created',
  'signals.updated',
  'project_summary.updated',
] as const

export type ProjectEventType = (typeof PROJECT_EVENT_TYPES)[number]

export function isProjectEventType(value: string): value is ProjectEventType {
  return (PROJECT_EVENT_TYPES as readonly string[]).includes(value)
}

/**
 * The alert inbox: the incident queue, one incident's deliveries, and the
 * "has this project ever delivered" probe. The inbox does not poll while the
 * stream is live, so without these a new incident never appeared in an open
 * Inbox until a reload (#194).
 */
function alertInboxKeys(slug: string): QueryKey[] {
  return [alertInboxKey(slug), alertInboxGroupKey(slug), alertDeliveriesAnyKey(slug)]
}

/** Activity rail keys — project feed + the workspace ('workspace' fallback) feed. */
function activityKeys(slug: string): QueryKey[] {
  return [activityKey(slug), activityKey(undefined)]
}

/**
 * Query-key prefixes to invalidate for a given event type in a given project.
 * Exhaustive over {@link ProjectEventType}.
 */
export function invalidationKeysFor(type: ProjectEventType, slug: string): QueryKey[] {
  switch (type) {
    case 'scan_job.updated':
      return [
        scansKey(slug),
        projectScanJobsKey(slug),
        projectEventsKey(slug),
        projectEventTypesKey(slug),
        // The events-tab dynamics chart does not poll while the stream is live.
        eventsMetricsKey(slug),
        overviewRootKey(),
        ...activityKeys(slug),
      ]
    case 'metric_collection.updated':
      return [
        scansKey(slug),
        projectScanJobsKey(slug),
        metricsCatalogKey(slug),
        eventsMetricsKey(slug),
        projectMonitoringSeriesKey(slug),
        metricDefinitionKey(slug),
        projectMonitoringBreakdownsKey(slug),
        projectEventHistoryKey(slug),
        projectEventKey(slug),
        projectEventWindowMetricsKey(slug),
        // The By version series sits right above the adoption chart; refreshing
        // only the adoption made the two cards disagree after a collection,
        // since neither polls while the stream is live.
        projectAppVersionSeriesKey(slug),
        projectAppVersionAdoptionKey(slug),
        projectChartAnnotationsKey(slug),
        projectBreakdownTimelineKey(slug),
        projectDistributionDriftsKey(slug),
        projectSeasonalityKey(slug),
        projectTopMoversKey(slug),
        projectReleaseRegressionsKey(slug),
        monitorsSummaryKey(slug),
        projectMonitorKey(slug),
        projectMonitorHistoryKey(slug),
        reconciliationRootKey(),
        // Covers the Events tabs/rows signals AND the expanded list shared by
        // the bell, Overview and the Anomalies page. The old
        // per-surface prefixes ('anomalies'/'overview'/'topbarNotifications'
        // + signals) are gone — one key now, so a new surface cannot forget to
        // register itself here.
        activeSignalsKey(slug),
        topbarNotificationsKey(slug),
        // This project's volume chart and top events only. Not the
        // Overview root: it matches every project's Overview, and the KPI
        // series counts event CREATIONS, which a metric collection does not
        // change — scan_job.updated and project_summary.updated refresh it.
        overviewVolumeRootKey(slug),
        overviewTopEventsKey(slug),
        ...activityKeys(slug),
      ]
    case 'signals.updated':
      return [
        activeSignalsKey(slug),
        monitorsSummaryKey(slug),
        projectMonitorKey(slug),
        projectMonitorHistoryKey(slug),
        metricDefinitionKey(slug),
        projectMonitoringSeriesKey(slug),
        projectAppVersionSeriesKey(slug),
        topbarNotificationsKey(slug),
        // No Overview root here: the signals Overview reads are the
        // shared active-signals list above, and the root refetched every
        // project's volume, top events and KPI series on each signal change.
        ...activityKeys(slug),
        ...alertInboxKeys(slug),
      ]
    case 'activity.created':
      return [
        ...activityKeys(slug),
        topbarNotificationsKey(slug),
        alertDeliveriesKey(slug),
        ...alertInboxKeys(slug),
      ]
    case 'project_summary.updated':
      return [projectsKey(), projectKey(slug), overviewRootKey()]
  }
}

/**
 * Invalidate every mapped query key for an event. Idempotent — a duplicated
 * event (e.g. replayed on reconnect) simply re-marks the same keys stale. The
 * events lists refresh the way a bulk edit refreshes them
 * (`refreshEventsLists`), so a scan landing does not re-request every page
 * the catalog table has scrolled through when it need not.
 */
export function invalidateForEvent(
  queryClient: QueryClient,
  type: ProjectEventType,
  slug: string,
): void {
  for (const queryKey of invalidationKeysFor(type, slug)) {
    if (keySegment(queryKey, 0) === 'events') void refreshEventsLists(queryClient, queryKey)
    else void queryClient.invalidateQueries({ queryKey })
  }
}
