import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { GitBranch, GitCompareArrows, Grid3x3, Layers } from 'lucide-react'
import { eventCommentsApi } from '@/api/eventComments'
import { eventTypesApi } from '@/api/eventTypes'
import { eventsApi } from '@/api/events'
import { metaFieldsApi } from '@/api/metaFields'
import { eventMetricsApi } from '@/api/eventMetrics'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { scansApi } from '@/api/scans'
import { variablesApi } from '@/api/variables'
import { SignalsHeldNotice } from '@/components/source-freshness/signals-held-notice'
import { isHoldingSignals } from '@/lib/sourceFreshness'
import { PageContainer } from '@/components/primitives/page-container'
import { EmptyState } from '@/components/empty-state'
import { EntityBranchBanner } from '@/components/EntityBranchBanner'
import EventPhotosSection from '@/components/event-photos-section'
import { EventValueDriftPanel } from '@/pages/events/EventValueDriftPanel'
import { EventPropertiesGrid } from '@/pages/events/EventPropertiesGrid'
import { useEventPropertyIds } from '@/pages/events/useEventPropertyIds'
import { PropertyDriftList } from '@/pages/events/PropertyDriftList'
import { EventHealthCard } from '@/pages/events/EventHealthCard'
import { EventSpecCard } from '@/components/EventSpecCard'
import { MetricDefinitionCard } from '@/components/monitoring/metric-definition-card'
import { UsedBySection } from '@/components/dependencies/UsedBySection'
import { DocNotesSection } from '@/components/docs/DocNotesSection'
import { SeasonalityHeatmap } from '@/components/monitoring/seasonality-heatmap'
import { EntityNotFound, PageSkeleton, QueryErrorState, SectionSkeleton } from '@/components/states'
import { Card, CardContent } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useActiveBranchId, useBranchLinkProps } from '@/hooks/useBranch'
import { useLiveTimeRange } from '@/hooks/useLiveTimeRange'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import {
  adaptMetricSeries,
  defaultDrilldownGranularity,
  granularityForInterval,
  metricRollupMode,
} from '@/lib/metricAdapters'
import { formatMetricValue, metricAxisFormatter } from '@/lib/metricFormat'
import { aggregateMetricPoints, clampGranularityToRange, type MetricsGranularity } from '@/lib/metrics'
import { resolveDetailScope } from '@/lib/monitoring'
import { currentOrgSlug, getAlertingPath, projectPath } from '@/lib/navigation'
import { useCanWriteProject } from '@/lib/permissions'
import {
  eventCommentsKey,
  eventHistoryKey,
  eventKey,
  eventsRootKey,
  eventTypesKey,
  metaFieldsKey,
  metricDefinitionKey,
  monitoringSeriesRangeKey,
  scanConfigKey,
  variablesKey,
} from '@/lib/queryKeys'
import { useAdaptiveRefetchInterval } from '@/realtime/streamContext'
import type { EventType, FieldDefinition, MetaFieldDefinition, Variable } from '@/types'
import { BreakdownsTab } from './monitoring/BreakdownsTab'
import { DistributionTab, type DistributionScope } from './monitoring/DistributionTab'
import { EventDetailHero, EventDetailSkeleton } from './monitoring/event/EventDetailHero'
import { EventDiscussion } from './monitoring/event/EventDiscussion'
import { EventFieldsTable } from './monitoring/event/EventFieldsTable'
import { EventSideColumn } from './monitoring/event/EventSideColumn'
import { EventLifecyclePanel } from './monitoring/event/EventLifecyclePanel'
import { LIVE_STATUSES } from './monitoring/event/surface'
import { MonitoringDetailHeader } from './monitoring/MonitoringDetailHeader'
import { useAnnotateHandoff } from './monitoring/useAnnotateHandoff'
import { useCommentDraftHandoff } from './monitoring/useCommentDraftHandoff'
import { SignalVerdictCard } from './monitoring/SignalVerdictCard'
import { IncidentSummary } from './alerting/IncidentSummary'
import { signalIncidentId } from './anomalies/signalTriage'
import { useChartAnnotations } from './monitoring/useChartAnnotations'
import { useMetricCollect } from './monitoring/useMetricCollect'
import {
  breakdownValueSearch,
  useMonitoringDetailSearch,
  type MonitoringDetailTab,
} from './monitoring/useMonitoringDetailSearch'
import { WhyChangedPanel, type AttributionValueHref } from './monitoring/WhyChangedPanel'
import { partialWindow } from './monitoring/partialBuckets'
import { VersionsTab } from './monitoring/VersionsTab'
import { VolumeTab } from './monitoring/VolumeTab'
import { usePageTitle } from '@/components/shell-chrome-context'

// Stable empty reference so `metaFieldsQuery.data ?? EMPTY_META_FIELDS`
// doesn't mint a new array each render and bust the memoized lookup map.
const EMPTY_META_FIELDS: MetaFieldDefinition[] = []
const EMPTY_VARIABLES: Variable[] = []

/**
 * One page, four scopes: an event, an event type, a scan's project total, and a
 * catalog metric. The page owns the queries that define the entity (the event,
 * the metric definition, the series); every secondary tab lives under
 * `pages/monitoring/` and owns its own query and error state.
 */

export default function MonitoringDetailPage() {
  const { slug, scope: scopeParam, id, eventId } = useParams<{
    slug: string
    scope?: string
    id?: string
    eventId?: string
  }>()
  const navigate = useNavigate()
  const location = useLocation()
  // Edit, collect, delete and annotations are EditorUserDep; a viewer reads the
  // page without them instead of meeting each as a 403.
  const canWrite = useCanWriteProject()
  // The legacy `/events/detail/:eventId` route carries no `:scope`; default to
  // the event scope when an eventId is present so the page never crashes on an
  // undefined scope (it now redirects to the canonical URL, but stay defensive).
  const scope = resolveDetailScope(scopeParam, eventId)
  // One page, THREE surfaces — the same three-way split navigation.ts makes for
  // these exact routes: `/monitoring/event/` is an Events drilldown (Plan),
  // `/monitoring/metric/` a Metrics one, and everything left under
  // `/monitoring/` (event-type, project-total) belongs to Anomalies.
  // The eyebrow names the nav group and the scope, the rule
  // every Observe page follows, instead of a separate back button above the
  // header; the top bar's breadcrumb is the way back.
  const eyebrow = scope === 'metric'
    ? 'Observe · Metric'
    : scope === 'event_type'
      ? 'Observe · Event type'
      : 'Observe · Project total'

  // Tab, range, granularity and filters live in the URL.
  const [search, searchActions] = useMonitoringDetailSearch()
  const { rangeDays } = search
  const metricsRef = useRef<HTMLDivElement>(null)

  const branchId = useActiveBranchId()
  const branchLink = useBranchLinkProps()
  const scopeId = id ?? eventId ?? ''
  // Reused by the header Edit button and the metric-scope Breakdowns empty state.
  const metricEditPath = projectPath(currentOrgSlug(), slug, `/metrics/${scopeId}/edit`)
  // Catalog metrics measure values (ratios, averages), not event volumes, so the
  // primary chart/tab reads "Value" for the metric scope and "Volume" elsewhere.
  const volumeLabel = scope === 'metric' ? 'Value' : 'Volume'

  // Follows the clock: the upper bound used to be pinned at mount, so a chart
  // left open never showed a bucket recorded after you opened it.
  const timeRange = useLiveTimeRange(rangeDays * 24 * 60 * 60 * 1000)
  // Live-metric/monitoring queries fall back to polling only while the stream is
  // unavailable; metric_collection.updated / signals.updated refresh them live.
  const refetchInterval = useAdaptiveRefetchInterval({ activeMs: 60_000 })

  const eventQuery = useQuery({
    queryKey: eventKey(slug, branchId, scopeId),
    queryFn: () => eventsApi.get(slug!, scopeId, branchId),
    enabled: scope === 'event' && !!slug && !!scopeId,
    meta: SILENT_ERROR_META,
  })
  // The event's property list, for the drift list's type changes (F23); the
  // grid below reads the same cached query.
  const eventPropertyIds = useEventPropertyIds(slug, branchId, scope === 'event' ? scopeId : undefined)
  const event = eventQuery.data

  const historyQuery = useQuery({
    queryKey: eventHistoryKey(slug, branchId, scopeId),
    queryFn: () => eventsApi.history(slug!, scopeId, branchId),
    enabled: scope === 'event' && !!slug && !!scopeId,
    meta: SILENT_ERROR_META,
  })

  // Only the event and event-type pages read event types (the type's fields,
  // name and colour); a metric or project-total page used to download every
  // type with its field definitions, and blank itself if that failed.
  const eventTypesQuery = useQuery({
    queryKey: eventTypesKey(slug, branchId),
    queryFn: () => eventTypesApi.list(slug!, branchId),
    enabled: !!slug && (scope === 'event' || scope === 'event_type'),
    meta: SILENT_ERROR_META,
  })
  const eventTypes = eventTypesQuery.data

  // Secondary: a failure only costs the meta-field labels, and the global
  // toast says so — it is not a reason to blank the page.
  const metaFieldsQuery = useQuery({
    queryKey: metaFieldsKey(slug, branchId),
    queryFn: () => metaFieldsApi.list(slug!, branchId),
    enabled: scope === 'event' && !!slug,
  })
  const metaFields = metaFieldsQuery.data ?? EMPTY_META_FIELDS

  // Secondary as well: the properties grid resolves the field values' `${…}`
  // tokens with them, the same way the edit page does (and from the same
  // cache), so both pages count the same properties the list is missing.
  const variablesQuery = useQuery({
    queryKey: variablesKey(slug, branchId),
    queryFn: () => variablesApi.list(slug!, branchId),
    enabled: scope === 'event' && !!slug,
    meta: SILENT_ERROR_META,
  })

  // Catalog metric definition (header / color / version-column) — only the
  // `metric` scope; the other scopes derive their title from event(-type) data.
  const metricDefinitionQuery = useQuery({
    queryKey: metricDefinitionKey(slug, scopeId),
    queryFn: () => metricsCatalogApi.get(slug!, scopeId),
    enabled: scope === 'metric' && !!slug && !!scopeId,
    meta: SILENT_ERROR_META,
  })
  const metricDefinition = metricDefinitionQuery.data
  // Owned here, not by the header's Collect button: that button unmounts when
  // the page swaps to its error state or canWrite flickers, and an in-progress
  // watch must outlive it.
  const metricCollect = useMetricCollect(scopeId)

  // Every catalog metric renders through the shared metric formatters, in the
  // chart ticks, the tooltip and the stat card alike: percent units store
  // fractions (0.08 for 8 %) and render ×100, currency units lead
  // ('$1,234', not '1,234 $'), and a sub-1 value keeps two significant digits
  // (a 0.004 s latency used to tick and tooltip as '0'). The
  // axis leaves a trailing unit off, where every tick would repeat it; the
  // tooltip spells it out. Event scopes keep the count rendering.
  const metricUnit = metricDefinition?.unit ?? null
  const isMetricScope = scope === 'metric'
  const metricValueFormatter = useMemo(
    () => (isMetricScope ? metricAxisFormatter(metricUnit) : undefined),
    [isMetricScope, metricUnit],
  )
  const metricTooltipFormatter = useMemo(
    () => (isMetricScope ? (value: number) => formatMetricValue(value, metricUnit) : undefined),
    [isMetricScope, metricUnit],
  )
  // One tooltip/aria label for every chart on this page: catalog metrics carry
  // their unit ('%', 'ms', …, falling back to 'value'); event scopes keep the
  // historical 'events'.
  const metricSeriesLabel = scope === 'metric' ? metricDefinition?.unit || 'value' : 'events'
  // Event volumes sum into a coarser bucket; a ratio, average or percentage
  // metric averages instead.
  const rollupMode = scope === 'metric' ? metricRollupMode(metricDefinition) : 'sum'
  // Until a metric's definition arrives its rollup is unknown: anything drawn
  // with the 'sum' fallback would show a ratio metric summed, then snap.
  const rollupPending = scope === 'metric' && metricDefinitionQuery.isPending

  const metricsQuery = useQuery({
    // Keyed on the range length, not the live bounds: the bound steps every five
    // minutes, and the query function reads the current window on each fetch.
    queryKey: monitoringSeriesRangeKey(slug, scope, scopeId, rangeDays),
    queryFn: () => {
      if (scope === 'metric') {
        return metricsCatalogApi.getSeries(slug!, scopeId, timeRange).then(adaptMetricSeries)
      }
      if (scope === 'project_total') {
        return eventMetricsApi.getProjectTotalMetrics(slug!, {
          scan_config_id: scopeId,
          ...timeRange,
        })
      }
      if (scope === 'event_type') {
        return eventMetricsApi.getEventTypeMetrics(slug!, scopeId, timeRange)
      }
      return eventMetricsApi.getEventMetrics(slug!, scopeId, timeRange)
    },
    enabled: !!slug && !!scopeId,
    refetchInterval,
    // Keep the previous range's series on screen while the new range loads so the
    // chart doesn't remount into a loading flash on range change.
    placeholderData: keepPreviousData,
    meta: SILENT_ERROR_META,
  })
  const metrics = metricsQuery.data
  // One default rule for every scope: the range's readable default,
  // never finer than the collection interval. A manual pick wins and stays
  // sticky across range changes — but is bumped coarser when it would draw more
  // points than a chart can take over the new range.
  // The collection interval's own granularity is exempt from that cap, so a
  // 15 min series can still be read at 15 min with its band and forecast.
  const nativeGranularity = granularityForInterval(metrics?.interval)
  const defaultGranularity = defaultDrilldownGranularity(rangeDays, metrics?.interval)
  const granularity = clampGranularityToRange(
    search.granularity ?? defaultGranularity,
    rangeDays,
    nativeGranularity,
  )
  // A pick equal to the default stays out of the URL, like every other param.
  const setGranularity = (next: MetricsGranularity) =>
    searchActions.setGranularity(next, defaultGranularity)
  const scanConfigId = metrics?.scan_config_id ?? (scope === 'project_total' ? scopeId : null)

  // Secondary: without it the By version tab stays hidden, and the global toast
  // names the failure.
  const scanConfigQuery = useQuery({
    queryKey: scanConfigKey(slug, scanConfigId),
    queryFn: () => scansApi.get(slug!, scanConfigId!),
    enabled: scope !== 'metric' && !!slug && !!scanConfigId,
  })
  // The scan feeding this series is late or overdue (F16, #269): its drop
  // signals are held, so a fall on the chart may not carry a marker yet. Read
  // off the same scan config response, no extra request.
  const scanConfig = scanConfigQuery.data
  const heldScans =
    scope !== 'metric' && scanConfig?.freshness && isHoldingSignals(scanConfig.freshness)
      ? [{
          id: scanConfig.id,
          name: scanConfig.name,
          data_source_id: scanConfig.data_source_id,
          freshness: scanConfig.freshness,
        }]
      : []
  // Catalog metrics expose their version column on the definition; the other
  // scopes read it off the resolved scan config.
  const hasVersionColumn = scope === 'metric'
    ? Boolean(metricDefinition?.app_version_column)
    : Boolean(scanConfigQuery.data?.app_version_column)
  // Which tabs this scope actually renders a trigger for — kept in step with
  // the TabsList below. A URL can ask for any of them, so the fallback has to
  // cover every absent tab: a value with no trigger leaves an empty page.
  const availableTabs = useMemo<MonitoringDetailTab[]>(
    () => [
      'volume',
      ...(hasVersionColumn ? (['versions'] as const) : []),
      ...(scope !== 'metric' ? (['heatmap', 'distribution'] as const) : []),
      ...(scope === 'event' || scope === 'metric' ? (['breakdowns'] as const) : []),
    ],
    [hasVersionColumn, scope],
  )
  const selectedTab: MonitoringDetailTab = availableTabs.includes(search.tab) ? search.tab : 'volume'

  // On a phone the tab strip scrolls; keep the active tab inside it, and fade
  // the right edge so the tabs past it are discoverable. The strip is
  // scrolled directly: scrollIntoView would also scroll the page to it. The
  // strip is held in state, not a ref: the page first paints a skeleton, and a
  // ref's effect keyed on the tab alone never re-ran once the strip mounted, so
  // a `?tab=breakdowns` deep link stayed scrolled to the start (F26).
  const [tabStrip, setTabStrip] = useState<HTMLDivElement | null>(null)
  useEffect(() => {
    const active = tabStrip?.querySelector<HTMLElement>('[role="tab"][data-state="active"]')
    if (!tabStrip || !active) return
    const left = active.offsetLeft
    const right = left + active.offsetWidth
    if (left < tabStrip.scrollLeft) tabStrip.scrollTo({ left: Math.max(0, left - 8) })
    else if (right > tabStrip.scrollLeft + tabStrip.clientWidth) {
      tabStrip.scrollTo({ left: right - tabStrip.clientWidth + 24 })
    }
  }, [selectedTab, tabStrip])

  // The hero's "Discussion (n)" chip and the banner's "Discuss".
  // The thread's own query and cache key, so the count and the thread below
  // cannot disagree, and a posted comment updates both.
  const discussionQuery = useQuery({
    queryKey: eventCommentsKey(slug ?? '', scopeId),
    queryFn: () => eventCommentsApi.list(slug!, scopeId),
    enabled: scope === 'event' && !!slug && !!scopeId,
    meta: SILENT_ERROR_META,
  })
  const jumpToDiscussion = () => {
    document.getElementById('event-discussion')?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
    // Only a writer has a composer; a viewer just lands on the thread.
    document.getElementById('event-detail-discussion-body')?.focus({ preventScroll: true })
  }

  // The Events list's bulk "Mark as verified", for this one event.
  const queryClient = useQueryClient()
  const markVerifiedMutation = useMutation({
    mutationFn: () => eventsApi.bulkUpdate(slug!, [scopeId], { reviewed: true }, branchId),
    onSuccess: () => {
      toast.success('Marked as verified')
      void queryClient.invalidateQueries({ queryKey: eventsRootKey() })
      void queryClient.invalidateQueries({ queryKey: eventKey(slug, branchId, scopeId) })
    },
  })

  const eventDistributionEventTypeId = event?.event_type_id ?? null
  const distributionScope = useMemo<DistributionScope | null>(() => {
    if (scope === 'project_total' && scopeId) {
      return { scope_type: 'project_total', scope_ref: scopeId, scan_config_id: scopeId }
    }
    if (scope === 'event_type' && scopeId) {
      return { scope_type: 'event_type', scope_ref: scopeId }
    }
    if (scope === 'event' && eventDistributionEventTypeId) {
      return { scope_type: 'event_type', scope_ref: eventDistributionEventTypeId }
    }
    return null
  }, [eventDistributionEventTypeId, scope, scopeId])

  const chartData = useMemo(
    () => aggregateMetricPoints(metrics?.data ?? [], granularity, rollupMode),
    [granularity, metrics?.data, rollupMode],
  )
  // Where the collected series ends: the newest bucket's start plus one
  // bucket. An annotation past it is parked on that bucket, and the form says
  // so.
  const dataEnd = useMemo(() => {
    const points = metrics?.data ?? []
    const last = points.at(-1)
    if (!last) return null
    const previous = points.at(-2)
    const lastTime = new Date(last.bucket).getTime()
    const span = previous ? Math.max(0, lastTime - new Date(previous.bucket).getTime()) : 0
    return new Date(lastTime + span).toISOString()
  }, [metrics?.data])
  const annotationsQuery = useChartAnnotations({ slug, scope, scopeId, rangeDays, timeRange })

  const eventType = (eventTypes ?? []).find((candidate: EventType) => (
    scope === 'event'
      ? candidate.id === event?.event_type_id
      : scope === 'event_type' && candidate.id === scopeId
  ))
  const fieldDefMap = useMemo(
    () => new Map(
      (eventType?.field_definitions ?? []).map((field: FieldDefinition) => [field.id, field]),
    ),
    [eventType?.field_definitions],
  )
  const metaFieldMap = useMemo(
    () => new Map(
      metaFields.map((metaField: MetaFieldDefinition) => [metaField.id, metaField]),
    ),
    [metaFields],
  )

  const headerTitle = (() => {
    if (scope === 'metric') return metricDefinition?.display_name ?? 'Metric'
    if (scope === 'project_total') return 'Total volume'
    if (scope === 'event_type') return eventType?.display_name ?? 'Event type'
    // The label an analyst wrote leads when there is one; the identity the scan
    // matches on then sits beneath it in mono.
    return event?.title || (event?.name ?? 'Event')
  })()
  // The top bar names the entity once it has loaded, not the generic fallback.
  const titleEntity = scope === 'metric' ? metricDefinition : scope === 'event_type' ? eventType : scope === 'event' ? event : scope
  usePageTitle(titleEntity ? headerTitle : null)
  const headerIdentity = scope === 'event' && event?.title ? (event.source_name || event.name) : null
  const headerDescription = (() => {
    if (scope === 'metric') {
      if (metricDefinition?.description) return metricDefinition.description
      // A placeholder sentence said nothing; the gap is an invitation to fill
      // it in (#246).
      return metricDefinition && canWrite ? (
        <Link
          to={metricEditPath}
          className="text-fg-tertiary underline decoration-fg-tertiary/50 underline-offset-2 hover:decoration-current"
        >
          Add a description…
        </Link>
      ) : undefined
    }
    if (scope === 'project_total') return 'Every event the scan counts, in one series.'
    if (scope === 'event_type') return eventType?.description || 'Aggregated volume for the event type.'
    return event?.description || 'Monitoring detail for the selected event.'
  })()
  // Why the chart is empty, per scope (#246): a draft is never
  // collected (the header's Activate is the action), and a fact or SQL metric
  // is computed on its own schedule rather than by a scan.
  const chartEmptyDescription = (() => {
    if (scope !== 'metric' || !metricDefinition) {
      return 'Run a scan to start collecting volume metrics for this scope.'
    }
    if (metricDefinition.status === 'draft') return "This metric is a draft and isn't collected."
    if (metricDefinition.kind === 'fact' || metricDefinition.kind === 'sql') {
      return 'No values yet — compute now or wait for the next scheduled run.'
    }
    return 'Run a scan to start collecting values for this metric.'
  })()
  const partialBuckets = useMemo(
    () => partialWindow(metrics?.data ?? [], granularity, nativeGranularity),
    [granularity, metrics?.data, nativeGranularity],
  )
  const isEventScope = scope === 'event'

  // Only the queries that define the entity blank the page; every tab renders
  // its own failure inside itself. A disabled query never errors, so
  // the list covers every scope.
  const entityQueries = [eventQuery, eventTypesQuery, metricDefinitionQuery, metricsQuery]
  const failedEntityQuery = entityQueries.find(query => query.isError)
  // Whether the Volume tab is on screen rather than a skeleton or an error —
  // the moment an annotation handed over from the Anomalies list can take focus.
  const detailReady = !failedEntityQuery
    && !(scope === 'metric' && metricDefinitionQuery.isPending)
    && !(scope === 'event_type' && eventTypesQuery.isPending)
    && !(isEventScope && !event)
  const { annotatePrefill, startAnnotation } = useAnnotateHandoff({
    ready: detailReady,
    showVolumeTab: () => searchActions.setTab('volume'),
  })
  // A tracking-bug verdict's "Open a comment on the event" (#254), from the
  // Signal card below or handed over by the Anomalies row menu.
  const { commentDraft, startDraft } = useCommentDraftHandoff({ ready: detailReady && isEventScope })
  // The flagged bucket the Signal card is about: the scope's latest signal.
  const latestSignal = metrics?.latest_signal ?? null
  const latestSignalIncidentId = latestSignal ? signalIncidentId(latestSignal) : null
  // The Why panel's per-value links (#255): the Breakdowns tab narrowed to the
  // value where this page has one (the event scope; the metric scope carries
  // no attribution). Event-type and project-total pages have no breakdown
  // view, so their values stay plain text.
  const attributionValueHref: AttributionValueHref | undefined = availableTabs.includes('breakdowns')
    ? (column, value) => `${location.pathname}${breakdownValueSearch(location.search, column, value)}`
    : undefined
  const scanSettingsHref = slug && scanConfigId ? projectPath(currentOrgSlug(), slug, `/scans/${scanConfigId}`) : null
  // A missing entity is not a failure to retry: a deleted event or
  // metric, or a stale link, says so and offers the way back to its list.
  const notFound = scope === 'metric'
    ? { title: 'Metric not found', back: { to: projectPath(currentOrgSlug(), slug, '/metrics'), label: 'Back to Metrics' } }
    : scope === 'event'
      ? { title: 'Event not found', back: { to: projectPath(currentOrgSlug(), slug, '/events'), label: 'Back to Events' } }
      : scope === 'event_type'
        ? { title: 'Event type not found', back: { to: projectPath(currentOrgSlug(), slug, '/anomalies'), label: 'Back to Anomalies' } }
        : { title: 'Scan not found', back: { to: projectPath(currentOrgSlug(), slug, '/anomalies'), label: 'Back to Anomalies' } }
  const entityNoun = scope === 'metric'
    ? 'metric'
    : scope === 'event' ? 'event' : scope === 'event_type' ? 'event type' : 'scan total'
  if (failedEntityQuery) {
    return (
      <PageContainer>
        <QueryErrorState
          title={`Could not load this ${entityNoun}`}
          description="The monitoring page could not fetch data from the backend."
          error={failedEntityQuery.error}
          notFound={notFound}
          // Retry exactly what failed: the metric definition was never retried
          // before, and refetching a disabled query ignores `enabled` and fired
          // a request with a null scan id.
          onRetry={() => {
            for (const query of entityQueries) {
              if (query.isError) void query.refetch()
            }
          }}
        />
      </PageContainer>
    )
  }

  // The metric and event-type pages are titled by their definition: until it
  // arrives, the page's shape, not a generic "Metric" / "Event type" header
  // that then swaps its title.
  if (
    (scope === 'metric' && metricDefinitionQuery.isPending)
    || (scope === 'event_type' && eventTypesQuery.isPending)
  ) {
    return (
      <PageContainer>
        <PageSkeleton
          variant="detail"
          label={scope === 'metric' ? 'Loading metric…' : 'Loading event type…'}
        />
      </PageContainer>
    )
  }
  // The list loaded without this id: a deleted type, or a link from another
  // branch. Titled "Event type" over an empty chart, it read as a real page.
  if (scope === 'event_type' && !eventType) {
    return (
      <PageContainer>
        <EntityNotFound title={notFound.title} back={notFound.back} />
      </PageContainer>
    )
  }

  // The event hero, not the generic header, is what an event page settles into;
  // painting the generic one first made the layout jump.
  if (isEventScope && !event) {
    return (
      <PageContainer>
        <EventDetailSkeleton />
      </PageContainer>
    )
  }

  const chartIsLoading = metricsQuery.isLoading
    // A metric's rollup depends on its definition; charting before it arrives
    // would draw a sum and then snap to a mean.
    || rollupPending

  return (
    // The list pages' container: no padding of its own inside the shell's, and
    // no narrower centred column, so the page lines up with the banner and the
    // top bar.
    <PageContainer>
      {/* Above the title, where the edit page has it: under the chart and
          the KPI tiles nobody saw it. */}
      {isEventScope && event && slug && (
        <EntityBranchBanner
          slug={slug}
          rowBranchId={event.branch_id}
          path={projectPath(currentOrgSlug(), slug, `/monitoring/event/${event.id}`)}
          // Not this id on main: it is the branch row's, which main would
          // render again under a mismatch warning. The main twin's
          // page when the server names one, else the events list on main.
          mainPath={
            event.main_event_id
              ? projectPath(currentOrgSlug(), slug, `/monitoring/event/${event.main_event_id}`)
              : projectPath(currentOrgSlug(), slug, '/events')
          }
        />
      )}
      {isEventScope && event ? (
        <EventDetailHero
          event={event}
          eventType={eventType}
          metrics={metrics}
          // Branch-aware: a bare path would drop the branch out of the URL and
          // leave the editor relying on context alone.
          onEdit={canWrite ? () => {
            const link = branchLink(
              projectPath(currentOrgSlug(), slug, `/events/${event.event_type?.name ?? 'all'}/${event.id}/edit`),
              event.branch_id ?? branchId,
            )
            link.onClick()
            navigate(link.to)
          } : undefined}
          onMetrics={() => {
            searchActions.setTab('volume')
            metricsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
          }}
          onAnnotate={canWrite ? startAnnotation : undefined}
          discussionCount={discussionQuery.data?.length}
          onDiscuss={jumpToDiscussion}
          onMarkVerified={canWrite && !event.reviewed && !markVerifiedMutation.isPending
            ? () => markVerifiedMutation.mutate()
            : undefined}
          // The signal's incident carries Ack / Mute / Resolve:
          // its inbox item when the payload names it (#254), else the inbox.
          alertsPath={slug
            ? getAlertingPath(slug, {
                incidentId: latestSignal?.incident?.id ?? latestSignal?.incident_id ?? null,
              })
            : undefined}
          slug={slug}
        />
      ) : (
        <MonitoringDetailHeader
          slug={slug}
          scope={scope}
          scopeId={scopeId}
          eyebrow={eyebrow}
          title={headerTitle}
          identity={headerIdentity}
          description={headerDescription}
          eventType={eventType}
          metrics={metrics}
          metricDefinition={metricDefinition}
          metricEditPath={metricEditPath}
          metricCollect={metricCollect}
          canWrite={canWrite}
        />
      )}

      {/* The verdict on the flagged bucket, where it is investigated (#254). */}
      {slug && latestSignal && (
        <SignalVerdictCard
          // Re-seeded when the bucket or its verdict changes underneath it.
          key={[
            latestSignal.bucket,
            latestSignal.verdict?.verdict ?? '',
            latestSignal.verdict?.created_at ?? '',
            latestSignal.incident?.status ?? latestSignal.incident_status ?? '',
          ].join('|')}
          slug={slug}
          signal={latestSignal}
          canWrite={canWrite}
          onOpenComment={isEventScope ? startDraft : undefined}
        />
      )}

      {/* The summary of the incident this signal was routed into (F14, #267);
          an unrouted signal has none, and nothing renders while AI is off. */}
      {slug && latestSignalIncidentId && (
        <IncidentSummary
          slug={slug}
          correlationGroupId={latestSignalIncidentId}
          canWrite={canWrite}
          defaultOpen
        />
      )}

      {/* Which breakdown values and release the flagged change comes from (#255). */}
      {slug && latestSignal && (
        <WhyChangedPanel
          signal={latestSignal}
          valueHref={attributionValueHref}
          scanSettingsHref={scanSettingsHref}
        />
      )}

      {slug && <SignalsHeldNotice slug={slug} items={heldScans} />}

      {/* Sunset watch findings and successor adoption (#258). */}
      {isEventScope && event && slug && <EventLifecyclePanel slug={slug} event={event} />}

      {/* What this catalog metric computes, visible without opening Edit. */}
      {scope === 'metric' && slug && metricDefinition && (
        <MetricDefinitionCard slug={slug} definition={metricDefinition} />
      )}

      {/* The spec comes first for an event that is not yet live: that page is
          where a developer is sent to instrument it, and the metrics below can
          only say "no data" until they have. Once the event is
          live the chart leads and the spec follows the fields. */}
      {isEventScope && event && slug && !LIVE_STATUSES.has(event.status) && (
        <EventSpecCard slug={slug} event={event} eventType={eventType} metaFieldMap={metaFieldMap} />
      )}

      {isEventScope && event && (
        // grid-cols-1 is minmax(0, 1fr): an auto column grew to the Fields
        // table's width on a phone and the page scrolled sideways.
        <div className="grid grid-cols-1 items-start gap-[14px] lg:grid-cols-[1.5fr_1fr] [&>*]:min-w-0">
          <EventFieldsTable eventType={eventType} event={event} fieldDefMap={fieldDefMap} />
          <EventSideColumn
            slug={slug ?? ''}
            event={event}
            eventType={eventType}
            history={historyQuery.data ?? []}
            historyError={historyQuery.isError ? historyQuery.error : undefined}
            onRetryHistory={() => void historyQuery.refetch()}
            metaFieldMap={metaFieldMap}
          />
        </div>
      )}

      {/* The event's property list (F23), read-only here: it is edited on the
          event's own page. */}
      {isEventScope && event && slug && (
        <EventPropertiesGrid
          slug={slug}
          branchId={branchId}
          eventId={event.id}
          threshold={event.required_presence_threshold ?? null}
          canWrite={false}
          projectVariables={variablesQuery.data ?? EMPTY_VARIABLES}
          fieldValues={event.field_values}
        />
      )}

      {isEventScope && event && slug && LIVE_STATUSES.has(event.status) && (
        <EventSpecCard slug={slug} event={event} eventType={eventType} metaFieldMap={metaFieldMap} />
      )}

      {/* The hero's "Metrics" action scrolls here. The anchor used to be a
          separate span with -mt-5, which cancelled the page gap and glued the
          tab strip to the card above it. */}
      <div ref={metricsRef} className="min-w-0 scroll-mt-4">
        <Tabs value={selectedTab} onValueChange={value => searchActions.setTab(value as MonitoringDetailTab)}>
          {/* The strip scrolls on its own on a phone instead of widening the
              page: five triggers do not fit 375px. */}
          <div
            ref={setTabStrip}
            className="tripl-scroll-x relative -mx-1 overflow-x-auto px-1 pr-8 [mask-image:linear-gradient(to_right,black_85%,transparent)] sm:pr-1 sm:[mask-image:none]"
          >
            <TabsList className="text-fg-muted">
              <TabsTrigger value="volume">{volumeLabel}</TabsTrigger>
              {hasVersionColumn && (
                <TabsTrigger value="versions">
                  <GitBranch className="h-3.5 w-3.5" />
                  By version
                </TabsTrigger>
              )}
              {scope !== 'metric' && (
                <TabsTrigger value="heatmap">
                  <Grid3x3 className="h-3.5 w-3.5" />
                  Heatmap
                </TabsTrigger>
              )}
              {scope !== 'metric' && (
                <TabsTrigger value="distribution">
                  <GitCompareArrows className="h-3.5 w-3.5" />
                  Distribution
                </TabsTrigger>
              )}
              {(scope === 'event' || scope === 'metric') && (
                <TabsTrigger value="breakdowns">
                  <Layers className="h-3.5 w-3.5" />
                  Breakdowns
                </TabsTrigger>
              )}
            </TabsList>
          </div>

          <TabsContent value="volume" className="space-y-6">
            <VolumeTab
              slug={slug}
              scope={scope}
              scopeId={scopeId}
              label={volumeLabel}
              metrics={metrics}
              metricUnit={metricUnit}
              chartData={chartData}
              chartIsLoading={chartIsLoading}
              chartEmptyDescription={chartEmptyDescription}
              chartColor={eventType?.color || metricDefinition?.color || undefined}
              granularity={granularity}
              nativeGranularity={nativeGranularity}
              rangeDays={rangeDays}
              timeRange={timeRange}
              partialBuckets={partialBuckets}
              seriesLabel={metricSeriesLabel}
              valueFormatter={metricValueFormatter}
              tooltipFormatter={metricTooltipFormatter}
              annotationsQuery={annotationsQuery}
              annotatePrefill={annotatePrefill}
              dataEnd={dataEnd}
              canWrite={canWrite}
              // The event hero's signal banner carries its own Annotate.
              onAnnotate={canWrite && !isEventScope ? startAnnotation : undefined}
              onRangeDaysChange={searchActions.setRangeDays}
              onGranularityChange={setGranularity}
            />
          </TabsContent>

          {hasVersionColumn && slug && (
            <TabsContent value="versions" className="space-y-4">
              <VersionsTab
                slug={slug}
                scope={scope}
                scopeId={scopeId}
                scanConfigId={scanConfigId}
                rangeDays={rangeDays}
                timeRange={timeRange}
                granularity={granularity}
                nativeGranularity={nativeGranularity}
                rollupMode={rollupMode}
                refetchInterval={refetchInterval}
                versionFilter={search.versionFilter}
                seriesLabel={metricSeriesLabel}
                valueFormatter={metricValueFormatter}
                tooltipFormatter={metricTooltipFormatter}
                onRangeDaysChange={searchActions.setRangeDays}
                onGranularityChange={setGranularity}
                onVersionFilterChange={searchActions.setVersionFilter}
              />
            </TabsContent>
          )}

          <TabsContent value="heatmap">
            {metrics?.scan_config_id ? (
              <SeasonalityHeatmap
                slug={slug!}
                scanConfigId={metrics.scan_config_id}
                scopeType={scope}
                scopeRef={scopeId}
                rangeDays={rangeDays}
                timeRange={timeRange}
                color={eventType?.color || 'var(--chart-3)'}
              />
            ) : (
              <Card>
                <CardContent>
                  <EmptyState
                    icon={Grid3x3}
                    title="No scan for this scope yet"
                    description="Run a scan to see volume by weekday and hour."
                  />
                </CardContent>
              </Card>
            )}
          </TabsContent>

          {slug && (
            <TabsContent value="distribution">
              <DistributionTab
                slug={slug}
                distributionScope={distributionScope}
                rangeDays={rangeDays}
                timeRange={timeRange}
                refetchInterval={refetchInterval}
                selectedField={search.distributionField}
                onSelectedFieldChange={searchActions.setDistributionField}
              />
            </TabsContent>
          )}

          {slug && (scope === 'event' || scope === 'metric') && (
            <TabsContent value="breakdowns">
              {rollupPending ? (
                // Same reason as the volume chart: no summed values for a
                // ratio metric while its definition is on the way.
                <SectionSkeleton variant="chart" label="Loading breakdowns…" />
              ) : (
                <BreakdownsTab
                  slug={slug}
                  scope={scope}
                  scopeId={scopeId}
                  rangeDays={rangeDays}
                  timeRange={timeRange}
                  granularity={granularity}
                  rollupMode={rollupMode}
                  refetchInterval={refetchInterval}
                  column={search.breakdownColumn}
                  selectedValues={search.breakdownValues}
                  seriesLabel={metricSeriesLabel}
                  valueFormatter={metricValueFormatter}
                  tooltipFormatter={metricTooltipFormatter}
                  metricEditPath={metricEditPath}
                  nativeGranularity={nativeGranularity}
                  onRangeDaysChange={searchActions.setRangeDays}
                  onGranularityChange={setGranularity}
                  onColumnChange={searchActions.setBreakdownColumn}
                  onSelectedValuesChange={searchActions.setBreakdownValues}
                />
              )}
            </TabsContent>
          )}
        </Tabs>
      </div>

      {/* The event's health score and its breakdown (F15, #268). Main-plan
          only: the card renders nothing on a branch or for an archived event. */}
      {scope === 'event' && scopeId && slug && (
        <EventHealthCard slug={slug} eventId={scopeId} status={event?.status} />
      )}

      {/* What depends on this event or metric (#257): the metrics built on the
          event, the alert rules filtered on or scoped to either. The event is
          read on the branch on screen; a metric is project-level, so main. */}
      {isEventScope && event && slug && (
        <UsedBySection slug={slug} entity={{ kind: 'event', id: event.id }} branchId={branchId} />
      )}
      {scope === 'metric' && slug && metricDefinition && (
        <UsedBySection slug={slug} entity={{ kind: 'metric', id: scopeId }} branchId={null} />
      )}
      {/* Docs-catalog notes that link to this event by name (F22). Links
          resolve against main, so the card is for the main plan only. */}
      {scope === 'event' && event && slug && branchId === null && (
        <DocNotesSection slug={slug} kind="event" name={event.name} />
      )}
      {/* …and to this catalog metric (F24); metrics are project-level. */}
      {scope === 'metric' && slug && metricDefinition && (
        <DocNotesSection slug={slug} kind="metric" name={metricDefinition.name} />
      )}

      {scope === 'event' && scopeId && (
        <EventValueDriftPanel slug={slug!} eventId={scopeId} />
      )}
      {/* Property drift (F23): detected against main, and Accept edits main. */}
      {scope === 'event' && scopeId && branchId === null && (
        <PropertyDriftList
          slug={slug!}
          eventId={scopeId}
          variableIds={eventPropertyIds}
          readOnly={!canWrite}
        />
      )}
      {scope === 'event' && scopeId && (
        <EventPhotosSection slug={slug!} eventId={scopeId} />
      )}
      {scope === 'event' && scopeId && (
        // The hero's "Discussion" chip and the banner's "Discuss" scroll here.
        <div id="event-discussion" className="scroll-mt-4">
          <EventDiscussion
            // Remounted per hand-over: the composer reads its draft once.
            key={commentDraft?.seq ?? 0}
            slug={slug!}
            eventId={scopeId}
            initialBody={commentDraft?.text}
          />
        </div>
      )}
    </PageContainer>
  )
}
