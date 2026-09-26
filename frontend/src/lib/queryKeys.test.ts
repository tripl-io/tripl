import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

import * as keys from './queryKeys'
import {
  dataSourcesKey,
  eventTypesKey,
  metricDrilldownKeys,
  planBranchesKey,
  projectEventTypesKey,
  variablesKey,
  variablesPageKey,
} from './queryKeys'

const SRC = join(import.meta.dirname, '..')

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry)
    if (statSync(path).isDirectory()) return sourceFiles(path)
    return /\.tsx?$/.test(entry) ? [path] : []
  })
}

describe('shared query keys (tripl-jfm3.115, tripl-jfm3.116)', () => {
  it('spells each family exactly one way', () => {
    expect(dataSourcesKey()).toEqual(['dataSources'])
    expect(planBranchesKey('demo')).toEqual(['planBranches', 'demo'])
    expect(variablesKey('demo', 'branch-1')).toEqual(['variables', 'demo', 'branch-1'])
  })

  it('keeps the two variable shapes in separate caches, page nested under items', () => {
    // The items key holds an array and the page key holds {items, total}. Sharing
    // one key handed the events rows an object and crashed the page in
    // production (tripl-lqxb) — so they must differ...
    expect(variablesPageKey('demo', 'branch-1')).not.toEqual(variablesKey('demo', 'branch-1'))

    // ...but the page key must stay a strict EXTENSION of the items key, because
    // every mutation invalidates the items key and React Query matches
    // invalidations by prefix. A sibling key would leave the settings table
    // showing deleted variables until a reload.
    const items = variablesKey('demo', 'branch-1')
    expect(variablesPageKey('demo', 'branch-1').slice(0, items.length)).toEqual([...items])
  })

  it('keeps the project-wide event-type key a strict prefix of the branch one', () => {
    // Mutations invalidate the project-wide form; readers on the Scans tab and
    // the alert-rule editor hold only a slug. If these two ever stop nesting,
    // renaming an event type goes back to leaving those surfaces stale.
    const project = projectEventTypesKey('demo')
    expect(eventTypesKey('demo', 'branch-1')).toEqual(['eventTypes', 'demo', 'branch-1'])
    expect(eventTypesKey('demo', 'branch-1').slice(0, project.length)).toEqual([...project])
  })

  it('makes each metric drilldown key a prefix of the page query it refreshes (MET-27)', () => {
    // MonitoringDetailPage keys the series `[family, slug, scope, scopeId, …range]`;
    // an invalidation only reaches it while these stay prefixes of that shape.
    const [series, breakdowns, versions] = metricDrilldownKeys('demo', 'm-1')
    expect(series).toEqual(['monitoringMetrics', 'demo', 'metric', 'm-1'])
    expect(breakdowns).toEqual(['eventMetricBreakdowns', 'demo', 'metric', 'm-1'])
    expect(versions).toEqual(['appVersionSeries', 'demo', 'metric', 'm-1'])
  })

  it('keys the single-project query by branch, nested under the bare project key (SH-11)', () => {
    // Main keeps the shared key the other project readers use; a branch's
    // summary counts its own plan, so it gets its own cache, still under the
    // `projectKey(slug)` prefix every invalidation of the project reaches.
    expect(keys.projectQueryOptions('demo').queryKey).toEqual(['project', 'demo'])
    expect(keys.projectQueryOptions('demo', null).queryKey).toEqual(['project', 'demo'])
    expect(keys.projectQueryOptions('demo', 'branch-1').queryKey).toEqual(['project', 'demo', 'branch-1'])
  })

  it('is the only place these keys are written', () => {
    // Two spellings of one cache is invisible at runtime — the reader and the
    // writer just stop seeing each other and the screen goes quietly stale. It
    // happened with 'data-sources' vs 'dataSources' across four surfaces, and
    // again with 'plan-branches' vs 'planBranches'. Nothing but a check like
    // this notices the third time.
    const offenders = sourceFiles(SRC)
      .filter((path) => !path.endsWith('queryKeys.ts') && !path.endsWith('queryKeys.test.ts'))
      .filter((path) => {
        const text = readFileSync(path, 'utf8')
        return (
          /\[\s*'data-sources'\s*\]/.test(text)
          || /\[\s*'dataSources'\s*\]/.test(text)
          || /\[\s*'plan-branches'/.test(text)
          || /\[\s*'planBranches'/.test(text)
          // Scoped to the queryKey/invalidate context on purpose: 'variables'
          // is an ordinary word that appears in unrelated tuples elsewhere.
          || /queryKey:\s*\[\s*'variables'/.test(text)
          || /invalidateQueries\(\{\s*queryKey:\s*\[\s*'variables'/.test(text)
          // Same scoping for 'eventTypes': the bare ['eventTypes'] root in
          // ProjectGeneralSection is deliberately project-agnostic and stays.
          || /queryKey:\s*\[\s*'eventTypes'/.test(text)
          || /invalidateQueries\(\{\s*queryKey:\s*\[\s*'eventTypes'/.test(text)
        )
      })
      .map((path) => path.slice(SRC.length + 1))

    expect(offenders).toEqual([])
  })
})

describe('query key values (SHELL-50)', () => {
  // Every key below used to be an array literal typed out at its call sites.
  // Moving them here must not change one value: a cache written under the old
  // spelling would be orphaned, and an invalidation prefix that no longer
  // matches leaves the screen stale with nothing to show for it.
  it.each([
    [keys.authStatusKey(), ['auth', 'status']],
    [keys.usersKey(), ['users']],
    [keys.projectMembersKey('demo'), ['projectMembers', 'demo']],
    [keys.invitationsKey(), ['invitations']],
    [keys.invitationPreviewKey('tok'), ['invitationPreview', 'tok']],
    [keys.apiKeysKey(), ['api-keys']],
    [keys.serviceSettingsKey(), ['serviceSettings']],
    [keys.aiStatusKey('demo'), ['aiStatus', 'demo']],
    [keys.activityKey('demo'), ['activity', 'demo']],
    [keys.activityKey(undefined), ['activity', 'workspace']],
    [keys.commandPaletteSearchKey('demo', 'q'), ['commandPaletteSearch', 'demo', 'q']],
    [keys.commandPaletteLexicalSearchKey('demo', 'q'), ['commandPaletteSearch', 'demo', 'q', 'lexical']],
    [keys.dataSourceSchemaKey('ds-1'), ['data-source-schema', 'ds-1']],
    [keys.auditKey({ offset: 0 }), ['audit', { offset: 0 }]],
    [keys.auditActionsKey(), ['auditActions']],
    [keys.auditEntryKey('a-1'), ['auditEntry', 'a-1']],
    [keys.projectRootKey(), ['project']],
    [keys.projectKey('demo'), ['project', 'demo']],
    [keys.projectsKey(), ['projects']],

    [keys.planBranchCountsKey('demo'), ['planBranchCounts', 'demo']],
    [keys.planBranchDiffKey('demo', 'b-1'), ['planBranchDiff', 'demo', 'b-1']],
    [keys.planBranchDetailKey('demo', 'b-1'), ['planBranchDetail', 'demo', 'b-1']],
    [keys.planBranchConflictsKey('demo', 'b-1'), ['planBranchConflicts', 'demo', 'b-1']],
    [keys.planBranchCommentsKey('demo', 'b-1'), ['planBranchComments', 'demo', 'b-1']],
    [keys.planBranchTicketsKey('demo', 'b-1'), ['planBranchImplementationTickets', 'demo', 'b-1']],
    [keys.branchSettingsKey('demo'), ['branchSettings', 'demo']],
    [keys.trackerConfigKey('demo'), ['trackerConfig', 'demo']],

    [
      keys.eventsListKey('demo', 'b-1', {
        filterEtId: 'et-1',
        debouncedSearch: 'q',
        queryStatuses: ['live'],
        filterTag: 'tag',
        filterSilentDays: 7,
        filterReviewed: true,
        filterOpenQuestions: false,
        sort: 'volume',
      }),
      ['events', 'demo', 'b-1', 'et-1', 'q', ['live'], 'tag', 7, true, false, 'volume'],
    ],
    [keys.eventsPickerKey('demo', null, 'metric-picker', 'q'), ['events', 'demo', null, 'metric-picker', 'q']],
    [keys.eventsInReviewCountKey('demo', 'b-1'), ['events', 'demo', 'b-1', 'inReviewCount']],
    [keys.eventKey('demo', null, 'e-1'), ['event', 'demo', null, 'e-1']],
    [keys.eventTagsKey('demo', 'b-1'), ['eventTags', 'demo', 'b-1']],
    [keys.eventHistoryKey('demo', 'b-1', 'e-1'), ['eventHistory', 'demo', 'b-1', 'e-1']],
    [keys.eventIdentityProbeKey('demo', 'b-1', 'et-1', 'name'), ['eventIdentityProbe', 'demo', 'b-1', 'et-1', 'name']],
    [keys.eventImplementationTicketsKey('demo', 'b-1', 'e-1'), ['eventImplementationTickets', 'demo', 'b-1', 'e-1']],
    [keys.bulkIdentitiesKey('demo', 'b-1', 'et-1'), ['bulkIdentities', 'demo', 'b-1', 'et-1']],
    [keys.eventCommentsKey('demo', 'e-1'), ['eventComments', 'demo', 'e-1']],
    [keys.eventPhotosKey('demo', 'e-1'), ['eventPhotos', 'demo', 'e-1']],
    [keys.eventPhotoCommentsKey('demo', 'e-1', 'p-1'), ['eventPhotoComments', 'demo', 'e-1', 'p-1']],
    [keys.eventWindowMetricsKey('demo', ['e-1', 'e-2']), ['eventWindowMetrics', 'demo', 'e-1,e-2']],
    [keys.expandedSignalsKey('demo'), ['activeSignals', 'demo', 'expanded']],
    [keys.eventsTabSignalsKey('demo'), ['activeSignals', 'demo', 'tabs']],
    [keys.signalVerdictCountsKey('demo'), ['activeSignals', 'demo', 'verdictCounts']],
    [keys.eventRowSignalsKey('demo', ['e-1', 'e-2']), ['activeSignals', 'demo', 'rows', 'e-1,e-2']],
    [keys.eventTypeDriftsKey('demo', 'et-1'), ['eventTypeDrifts', 'demo', 'et-1']],
    [keys.eventTypeOwnersKey('demo', 'et-1'), ['eventTypeOwners', 'demo', 'et-1']],
    [keys.eventTypeDeletionImpactKey('demo', 'b-1', 'et-1'), ['eventTypeDeletionImpact', 'demo', 'b-1', 'et-1']],
    [keys.metaFieldsKey('demo', 'b-1'), ['metaFields', 'demo', 'b-1']],
    [keys.relationsKey('demo', 'b-1'), ['relations', 'demo', 'b-1']],
    [keys.planRevisionsKey('demo', 20), ['planRevisions', 'demo', 20]],
    [keys.planRevisionDiffKey('demo', 'r-2', 'r-1'), ['planRevisionDiff', 'demo', 'r-2', 'r-1']],
    [keys.variablesUsagePageKey('demo', 'b-1', 'unused'), ['variables', 'demo', 'b-1', 'page', 'unused']],
    [keys.variableDriftsKey('demo', 'b-1', 'v-1'), ['variable-drifts', 'demo', 'b-1', 'v-1']],
    [keys.eventVariableDriftsKey('demo', 'b-1', 'e-1'), ['variable-drifts', 'demo', 'b-1', 'event', 'e-1']],
    [keys.variableOverridesKey('demo', 'b-1', 'v-1'), ['variable-overrides', 'demo', 'b-1', 'v-1']],
    [keys.variableValuesKey('demo', 'b-1', 'v-1'), ['variable-values', 'demo', 'b-1', 'v-1']],
    [
      keys.eventsMetricsChartKey(
        'demo',
        null,
        { filterEtId: undefined, debouncedSearch: '', queryStatuses: undefined, filterTag: '' },
        { from: 'f', to: 't' },
      ),
      ['eventsMetrics', 'demo', null, undefined, '', undefined, '', 'f', 't'],
    ],

    [keys.scansKey('demo'), ['scans', 'demo']],
    [keys.scanConfigKey('demo', 'sc-1'), ['scanConfig', 'demo', 'sc-1']],
    [keys.scanJobsKey('demo', 'sc-1'), ['scanJobs', 'demo', 'sc-1']],
    [keys.scanJobsLimitedKey('demo', 'sc-1', 5), ['scanJobs', 'demo', 'sc-1', { limit: 5 }]],
    [keys.scanActivityKey('demo'), ['scanJobs', 'demo', 'activity']],
    [keys.platformPresenceKey('demo', 'sc-1'), ['platformPresence', 'demo', 'sc-1']],
    [keys.demoScenarioScanWatchKey('demo', 'j-1'), ['demo-scenario-scan-watch', 'demo', 'j-1']],
    [keys.demoScenarioCollectWatchKey('demo', 'm-1', 1), ['demo-scenario-collect-watch', 'demo', 'm-1', 1]],
    [keys.metricCollectWatchKey('demo', 'm-1', 1), ['metric-collect-watch', 'demo', 'm-1', 1]],

    [keys.metricsCatalogListKey('demo', 'all', 'all', ''), ['metrics-catalog', 'demo', 'all', 'all', '', '']],
    [keys.metricsCatalogListKey('demo', 'all', 'all', '', 'reviewed'), ['metrics-catalog', 'demo', 'all', 'all', '', 'reviewed']],
    [keys.metricGeneratedSqlForMetricKey('demo', 'm-1'), ['metric-generated-sql', 'demo', 'm-1']],
    [keys.factTablesKey('demo'), ['fact-tables', 'demo']],
    [keys.factTableKey('demo', 'ft-1'), ['fact-table', 'demo', 'ft-1']],
    [keys.monitoringSeriesScopeKey('demo', 'metric'), ['monitoringMetrics', 'demo', 'metric']],
    [keys.monitoringSeriesRangeKey('demo', 'event', 'e-1', 7), ['monitoringMetrics', 'demo', 'event', 'e-1', 7]],
    [
      keys.monitoringBreakdownsColumnKey('demo', 'event', 'e-1', 'os', 7),
      ['eventMetricBreakdowns', 'demo', 'event', 'e-1', 'os', 7],
    ],
    [
      keys.appVersionSeriesRangeKey('demo', 'event', 'e-1', 'sc-1', 7),
      ['appVersionSeries', 'demo', 'event', 'e-1', 'sc-1', 7],
    ],
    [keys.chartAnnotationsRangeKey('demo', 'event', 'e-1', 7), ['chartAnnotations', 'demo', 'event', 'e-1', 7]],
    [keys.appVersionAdoptionKey('demo', 'sc-1', 7), ['appVersionAdoption', 'demo', 'sc-1', 7]],
    [
      keys.topMoversKey('demo', 'sc-1', 'event', 'e-1', 'b', 5),
      ['topMovers', 'demo', 'sc-1', 'event', 'e-1', 'b', 5],
    ],
    [
      keys.breakdownTimelineKey('demo', 'sc-1', 'event', 'e-1', 'os', 'ios', false, 7),
      ['breakdownTimeline', 'demo', 'sc-1', 'event', 'e-1', 'os', 'ios', false, 7],
    ],
    [keys.seasonalityKey('demo', 'sc-1', 'event', 'e-1', 7), ['seasonality', 'demo', 'sc-1', 'event', 'e-1', 7]],
    [keys.releaseRegressionsKey('demo', 'sc-1'), ['releaseRegressions', 'demo', 'sc-1']],
    [keys.distributionDriftsKey('demo', 'all', 7), ['distributionDrifts', 'demo', 'all', 7]],
    [keys.anomalyScopeOverridesKey('demo'), ['anomalyScopeOverrides', 'demo']],
    [keys.projectAnomalySettingsKey('demo'), ['projectAnomalySettings', 'demo']],
    [keys.overviewVolumeKey('demo', 30), ['overview', 'volume', 'demo', 30]],
    [keys.overviewTopEventsKey('demo'), ['overview', 'top-events', 'demo']],
    [keys.overviewKpiSeriesKey('demo'), ['overview', 'kpi-series', 'demo']],
    [keys.reconciliationCoverageKey('demo', 30), ['reconciliation', 'coverage', 'demo', 30]],
    [keys.deadEventsKey('demo', 30), ['reconciliation', 'dead', 'demo', 30]],
    [keys.shadowEventsKey('demo', 'b-1', 'open'), ['reconciliation', 'shadow', 'demo', 'b-1', 'open']],

    [keys.alertDeliveriesPageKey('demo', { status: 'sent' }, 0), ['alertDeliveries', 'demo', { status: 'sent' }, 0]],
    [keys.incidentDeliveriesKey('demo', 'g-1'), ['alertDeliveries', 'demo', 'incident', 'g-1']],
    [keys.alertDeliveryKey('demo', 'd-1'), ['alertDelivery', 'demo', 'd-1']],
    [keys.alertDestinationsKey('demo'), ['alertDestinations', 'demo']],
    [keys.alertInboxListKey('demo', 'open', { q: '' }), ['alertInbox', 'demo', 'open', { q: '' }]],
    [keys.alertInboxGroupItemKey('demo', 'g-1'), ['alertInboxGroup', 'demo', 'g-1']],
    [keys.monitorsSummaryKey('demo'), ['monitors-summary', 'demo']],
    [keys.monitorDetailKey('demo', 'mon-1'), ['monitor', 'demo', 'mon-1']],
    [keys.monitorHistoryKey('demo', 'mon-1'), ['monitor-history', 'demo', 'mon-1']],
    [keys.topbarDeliveriesKey('demo'), ['topbarNotifications', 'demo', 'deliveries']],
  ])('%j', (key, expected) => {
    expect(key).toEqual(expected)
  })

  it.each([
    // [prefix an invalidation uses, key a reader caches under]
    [keys.eventsRootKey(), keys.eventsPickerKey('demo', 'b-1', 'alert-filter', 'q')],
    [keys.projectEventsKey('demo'), keys.eventsInReviewCountKey('demo', 'b-1')],
    [keys.projectEventKey('demo'), keys.eventKey('demo', 'b-1', 'e-1')],
    [keys.projectEventHistoryKey('demo'), keys.eventHistoryKey('demo', 'b-1', 'e-1')],
    [keys.projectEventTagsKey('demo'), keys.eventTagsKey('demo', 'b-1')],
    [keys.projectMetaFieldsKey('demo'), keys.metaFieldsKey('demo', 'b-1')],
    [keys.projectRelationsKey('demo'), keys.relationsKey('demo', 'b-1')],
    [keys.projectPlanRevisionsKey('demo'), keys.planRevisionsKey('demo', 0)],
    [keys.projectEventWindowMetricsKey('demo'), keys.eventWindowMetricsKey('demo', ['e-1'])],
    [keys.activeSignalsRootKey(), keys.eventRowSignalsKey('demo', ['e-1'])],
    [keys.activeSignalsKey('demo'), keys.expandedSignalsKey('demo')],
    [keys.projectEventTypeOwnersKey('demo'), keys.eventTypeOwnersKey('demo', 'et-1')],
    [keys.eventTypeDriftsRootKey(), keys.eventTypeDriftsKey('demo', 'et-1')],
    [keys.eventTypesRootKey(), keys.eventTypesKey('demo', 'b-1')],
    [keys.projectVariablesKey('demo'), keys.variablesUsagePageKey('demo', 'b-1', 'all')],
    [keys.branchVariableDriftsKey('demo', 'b-1'), keys.eventVariableDriftsKey('demo', 'b-1', 'e-1')],
    [keys.branchVariableValuesKey('demo', 'b-1'), keys.variableValuesKey('demo', 'b-1', 'v-1')],
    [keys.eventsMetricsKey('demo'), keys.eventsMetricsChartKey('demo', null, {
      filterEtId: undefined,
      debouncedSearch: '',
      queryStatuses: undefined,
      filterTag: '',
    }, { from: 'f', to: 't' })],
    [keys.projectScanJobsKey('demo'), keys.scanJobsLimitedKey('demo', 'sc-1', 5)],
    // The stream's scan-job invalidation reaches the activity summary.
    [keys.projectScanJobsKey('demo'), keys.scanActivityKey('demo')],
    [keys.scanJobsKey('demo', 'sc-1'), keys.scanJobsLimitedKey('demo', 'sc-1', 5)],
    [keys.projectFactTableKey('demo'), keys.factTableKey('demo', 'ft-1')],
    [keys.metricsCatalogRootKey(), keys.metricsCatalogListKey('demo', 'all', 'all', '')],
    [keys.metricGeneratedSqlKey('demo'), keys.metricGeneratedSqlForMetricKey('demo', 'm-1')],
    [keys.monitoringSeriesRootKey(), keys.monitoringSeriesRangeKey('demo', 'metric', 'm-1', 7)],
    [keys.monitoringSeriesScopeKey('demo', 'metric'), keys.monitoringSeriesKey('demo', 'metric', 'm-1')],
    [keys.projectMonitoringBreakdownsKey('demo'), keys.monitoringBreakdownsColumnKey('demo', 'event', 'e-1', 'os', 7)],
    [keys.projectAppVersionSeriesKey('demo'), keys.appVersionSeriesRangeKey('demo', 'event', 'e-1', 'sc-1', 7)],
    [keys.projectChartAnnotationsKey('demo'), keys.chartAnnotationsRangeKey('demo', 'event', 'e-1', 7)],
    [keys.projectAppVersionAdoptionKey('demo'), keys.appVersionAdoptionKey('demo', 'sc-1', 7)],
    [keys.projectTopMoversKey('demo'), keys.topMoversKey('demo', 'sc-1', 'event', 'e-1', 'b', 5)],
    [keys.projectBreakdownTimelineKey('demo'), keys.breakdownTimelineKey('demo', 'sc-1', 'event', 'e-1', 'os', 'ios', false, 7)],
    [keys.projectSeasonalityKey('demo'), keys.seasonalityKey('demo', 'sc-1', 'event', 'e-1', 7)],
    [keys.projectReleaseRegressionsKey('demo'), keys.releaseRegressionsKey('demo', 'sc-1')],
    [keys.distributionDriftsRootKey(), keys.distributionDriftsKey('demo', 'all', 7)],
    [keys.overviewRootKey(), keys.overviewVolumeKey('demo', 30)],
    [keys.reconciliationRootKey(), keys.shadowEventsKey('demo', 'b-1', 'open')],
    [keys.projectDeadEventsKey('demo'), keys.deadEventsKey('demo', 30)],
    [keys.projectShadowEventsKey('demo'), keys.shadowEventsKey('demo', 'b-1', 'open')],
    [keys.projectRootKey(), keys.projectKey('demo')],
    [keys.aiStatusRootKey(), keys.aiStatusKey('demo')],
    [keys.commandPaletteSearchRootKey(), keys.commandPaletteLexicalSearchKey('demo', 'q')],
    [keys.alertDeliveriesKey('demo'), keys.incidentDeliveriesKey('demo', 'g-1')],
    [keys.alertInboxKey('demo'), keys.alertInboxListKey('demo', 'open', {})],
    [keys.alertInboxGroupKey('demo'), keys.alertInboxGroupItemKey('demo', 'g-1')],
    [keys.projectMonitorKey('demo'), keys.monitorDetailKey('demo', 'mon-1')],
    [keys.projectMonitorHistoryKey('demo'), keys.monitorHistoryKey('demo', 'mon-1')],
    [keys.topbarNotificationsKey('demo'), keys.topbarDeliveriesKey('demo')],
  ])('%j is a prefix of %j', (prefix, key) => {
    expect(key.slice(0, prefix.length)).toEqual([...prefix])
  })
})
