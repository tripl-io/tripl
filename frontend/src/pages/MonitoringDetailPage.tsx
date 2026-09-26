import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
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
import { SignalsHeldNotice } from '@/components/source-freshness/signals-held-notice'
import { isHoldingSignals } from '@/lib/sourceFreshness'
import { PageContainer } from '@/components/primitives/page-container'
import { EmptyState } from '@/components/empty-state'
import { EntityBranchBanner } from '@/components/EntityBranchBanner'
import EventPhotosSection from '@/components/event-photos-section'
import { EventValueDriftPanel } from '@/pages/events/EventValueDriftPanel'
import { EventSpecCard } from '@/components/EventSpecCard'
import { MetricDefinitionCard } from '@/components/monitoring/metric-definition-card'
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
import { getAlertingPath } from '@/lib/navigation'
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
} from '@/lib/queryKeys'
import { useAdaptiveRefetchInterval } from '@/realtime/streamContext'
import type { EventType, FieldDefinition, MetaFieldDefinition } from '@/types'
import { BreakdownsTab } from './monitoring/BreakdownsTab'
import { DistributionTab, type DistributionScope } from './monitoring/DistributionTab'
import { EventDetailHero, EventDetailSkeleton } from './monitoring/event/EventDetailHero'
import { EventDiscussion } from './monitoring/event/EventDiscussion'
import { EventFieldsTable } from './monitoring/event/EventFieldsTable'
import { EventSideColumn } from './monitoring/event/EventSideColumn'
import { LIVE_STATUSES } from './monitoring/event/surface'
import { MonitoringDetailHeader } from './monitoring/MonitoringDetailHeader'
import { useAnnotateHandoff } from './monitoring/useAnnotateHandoff'
import { useChartAnnotations } from './monitoring/useChartAnnotations'
import { useMetricCollect } from './monitoring/useMetricCollect'
import { useMonitoringDetailSearch, type MonitoringDetailTab } from './monitoring/useMonitoringDetailSearch'
import { partialWindow } from './monitoring/partialBuckets'
import { VersionsTab } from './monitoring/VersionsTab'
import { VolumeTab } from './monitoring/VolumeTab'
import { usePageTitle } from '@/components/shell-chrome-context'

// Stable empty reference so `metaFieldsQuery.data ?? EMPTY_META_FIELDS`
// doesn't mint a new array each render and bust the memoized lookup map.
const EMPTY_META_FIELDS: MetaFieldDefinition[] = []

/**
 * One page, four scopes: an event, an event type, a scan's project total, and a
 * catalog metric. The page owns the queries that define the entity (the event,
 * the metric definition, the series); every secondary tab lives under
 * `pages/monitoring/` and owns its own query and error state (MON-35).
 */

export default function MonitoringDetailPage() {
  const { slug, scope: scopeParam, id, eventId } = useParams<{
    slug: string
    scope?: string
    id?: string
    eventId?: string
  }>()
  const navigate = useNavigate()
  // Edit, collect, delete and annotations are EditorUserDep; a viewer reads the
  // page without them instead of meeting each as a 403 (MON-6).
  const canWrite = useCanWriteProject()
  // The legacy `/events/detail/:eventId` route carries no `:scope`; default to
  // the event scope when an eventId is present so the page never crashes on an
  // undefined scope (it now redirects to the canonical URL, but stay defensive).
  const scope = resolveDetailScope(scopeParam, eventId)
  // One page, THREE surfaces — the same three-way split navigation.ts makes for
  // these exact routes: `/monitoring/event/` is an Events drilldown (Plan),
  // `/monitoring/metric/` a Metrics one, and everything left under
  // `/monitoring/` (event-type, project-total) belongs to Anomalies
  // (tripl-lkox). The eyebrow names the nav group and the scope, the rule
  // every Observe page follows, instead of a separate back button above the
  // header (DS-2 / MO-40); the top bar's breadcrumb is the way back.
  const eyebrow = scope === 'metric'
    ? 'Observe · Metric'
    : scope === 'event_type'
      ? 'Observe · Event type'
      : 'Observe · Project total'

  // Tab, range, granularity and filters live in the URL (MON-24).
  const [search, searchActions] = useMonitoringDetailSearch()
  const { rangeDays } = search
  const metricsRef = useRef<HTMLDivElement>(null)

  const branchId = useActiveBranchId()
  const branchLink = useBranchLinkProps()
  const scopeId = id ?? eventId ?? ''
  // Reused by the header Edit button and the metric-scope Breakdowns empty state.
  const metricEditPath = `/p/${slug}/metrics/${scopeId}/edit`
  // Catalog metrics measure values (ratios, averages), not event volumes, so the
  // primary chart/tab reads "Value" for the metric scope and "Volume" elsewhere.
  const volumeLabel = scope === 'metric' ? 'Value' : 'Volume'

  // Follows the clock: the upper bound used to be pinned at mount, so a chart
  // left open never showed a bucket recorded after you opened it (tripl-jfm3.114).
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
  const event = eventQuery.data

  const historyQuery = useQuery({
    queryKey: eventHistoryKey(slug, branchId, scopeId),
    queryFn: () => eventsApi.history(slug!, scopeId, branchId),
    enabled: scope === 'event' && !!slug && !!scopeId,
    meta: SILENT_ERROR_META,
  })

  // Only the event and event-type pages read event types (the type's fields,
  // name and colour); a metric or project-total page used to download every
  // type with its field definitions, and blank itself if that failed (MON-37).
  const eventTypesQuery = useQuery({
    queryKey: eventTypesKey(slug, branchId),
    queryFn: () => eventTypesApi.list(slug!, branchId),
    enabled: !!slug && (scope === 'event' || scope === 'event_type'),
    meta: SILENT_ERROR_META,
  })
  const eventTypes = eventTypesQuery.data

  // Secondary: a failure only costs the meta-field labels, and the global
  // toast says so — it is not a reason to blank the page (MON-8).
  const metaFieldsQuery = useQuery({
    queryKey: metaFieldsKey(slug, branchId),
    queryFn: () => metaFieldsApi.list(slug!, branchId),
    enabled: scope === 'event' && !!slug,
  })
  const metaFields = metaFieldsQuery.data ?? EMPTY_META_FIELDS

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
  // fractions (0.08 for 8 %, tripl-nxk2.1) and render ×100, currency units lead
  // ('$1,234', not '1,234 $'), and a sub-1 value keeps two significant digits
  // (a 0.004 s latency used to tick and tooltip as '0', DS-31 / MET-40). The
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
  // metric averages instead (MON-2 / MET-12).
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
    // chart doesn't remount into a loading flash on range change (tripl-7l83.10).
    placeholderData: keepPreviousData,
    meta: SILENT_ERROR_META,
  })
  const metrics = metricsQuery.data
  // One default rule for every scope (MON-43): the range's readable default,
  // never finer than the collection interval. A manual pick wins and stays
  // sticky across range changes — but is bumped coarser when it would draw more
  // points than a chart can take over the new range (MON-23).
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
  // names the failure (MON-8).
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
  // the right edge so the tabs past it are discoverable (MO-32). The strip is
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

  // The hero's "Discussion (n)" chip and the banner's "Discuss" (JR-7 / JR-5).
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

  // The Events list's bulk "Mark as verified", for this one event (JR-8).
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
  // so (MO-8).
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
    // matches on then sits beneath it in mono (tripl-kjhi.3).
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
      // it in (#246 JR-16).
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
  // Why the chart is empty, per scope (#246 JR-16): a draft is never
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
  // its own failure inside itself (MON-8). A disabled query never errors, so
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
  // A missing entity is not a failure to retry (SH-33): a deleted event or
  // metric, or a stale link, says so and offers the way back to its list.
  const notFound = scope === 'metric'
    ? { title: 'Metric not found', back: { to: `/p/${slug}/metrics`, label: 'Back to Metrics' } }
    : scope === 'event'
      ? { title: 'Event not found', back: { to: `/p/${slug}/events`, label: 'Back to Events' } }
      : scope === 'event_type'
        ? { title: 'Event type not found', back: { to: `/p/${slug}/anomalies`, label: 'Back to Anomalies' } }
        : { title: 'Scan not found', back: { to: `/p/${slug}/anomalies`, label: 'Back to Anomalies' } }
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
          // a request with a null scan id (MON-7).
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
  // that then swaps its title (batch 5).
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
  // painting the generic one first made the layout jump (MON-9).
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
    // top bar (DS-3 / MO-9).
    <PageContainer>
      {/* Above the title, where the edit page has it: under the chart and
          the KPI tiles nobody saw it (PL-3). */}
      {isEventScope && event && slug && (
        <EntityBranchBanner
          slug={slug}
          rowBranchId={event.branch_id}
          path={`/p/${slug}/monitoring/event/${event.id}`}
          // Not this id on main: it is the branch row's, which main would
          // render again under a mismatch warning (EVT-42). The main twin's
          // page when the server names one, else the events list on main.
          mainPath={
            event.main_event_id
              ? `/p/${slug}/monitoring/event/${event.main_event_id}`
              : `/p/${slug}/events`
          }
        />
      )}
      {isEventScope && event ? (
        <EventDetailHero
          event={event}
          eventType={eventType}
          metrics={metrics}
          // Branch-aware: a bare path would drop the branch out of the URL and
          // leave the editor relying on context alone (tripl-h2sx.2).
          onEdit={canWrite ? () => {
            const link = branchLink(
              `/p/${slug}/events/${event.event_type?.name ?? 'all'}/${event.id}/edit`,
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
          // The signal's incident carries Ack / Mute / Resolve; the signal
          // payload names no incident id, so this is the inbox (MO-4 / JR-5).
          alertsPath={slug ? getAlertingPath(slug) : undefined}
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

      {slug && <SignalsHeldNotice slug={slug} items={heldScans} />}

      {/* What this catalog metric computes, visible without opening Edit. */}
      {scope === 'metric' && slug && metricDefinition && (
        <MetricDefinitionCard slug={slug} definition={metricDefinition} />
      )}

      {/* The spec comes first for an event that is not yet live: that page is
          where a developer is sent to instrument it, and the metrics below can
          only say "no data" until they have (tripl-kjhi.8). Once the event is
          live the chart leads and the spec follows the fields. */}
      {isEventScope && event && slug && !LIVE_STATUSES.has(event.status) && (
        <EventSpecCard slug={slug} event={event} eventType={eventType} metaFieldMap={metaFieldMap} />
      )}

      {isEventScope && event && (
        // grid-cols-1 is minmax(0, 1fr): an auto column grew to the Fields
        // table's width on a phone and the page scrolled sideways (LIVE-5).
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

      {isEventScope && event && slug && LIVE_STATUSES.has(event.status) && (
        <EventSpecCard slug={slug} event={event} eventType={eventType} metaFieldMap={metaFieldMap} />
      )}

      {/* The hero's "Metrics" action scrolls here. The anchor used to be a
          separate span with -mt-5, which cancelled the page gap and glued the
          tab strip to the card above it (LIVE-12). */}
      <div ref={metricsRef} className="min-w-0 scroll-mt-4">
        <Tabs value={selectedTab} onValueChange={value => searchActions.setTab(value as MonitoringDetailTab)}>
          {/* The strip scrolls on its own on a phone instead of widening the
              page: five triggers do not fit 375px (LIVE-5). */}
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
              // The event hero's signal banner carries its own Annotate (JR-5).
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

      {scope === 'event' && scopeId && (
        <EventValueDriftPanel slug={slug!} eventId={scopeId} />
      )}
      {scope === 'event' && scopeId && (
        <EventPhotosSection slug={slug!} eventId={scopeId} />
      )}
      {scope === 'event' && scopeId && (
        // The hero's "Discussion" chip and the banner's "Discuss" scroll here.
        <div id="event-discussion" className="scroll-mt-4">
          <EventDiscussion slug={slug!} eventId={scopeId} />
        </div>
      )}
    </PageContainer>
  )
}
