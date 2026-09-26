import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ChevronDown } from 'lucide-react'

import { eventMetricsApi } from '@/api/eventMetrics'
import { useLiveTimeRange } from '@/hooks/useLiveTimeRange'
import { useAdaptiveRefetchInterval } from '@/realtime/streamContext'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { MetricsChart } from '@/components/ui/chart-lazy'
import { ChartSkeleton } from '@/components/states'
import { RangeSegmentedControl } from '@/components/range-segmented-control'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { aggregateMetricPoints, type MetricsGranularity } from '@/lib/metrics'
import type { ChartAnnotation, EventType, MonitoringSignal } from '@/types'

import { getMonitoringPath } from '@/lib/monitoring'
import { formatWeekSummary } from '@/lib/monitoringWeekTotals'
import { eventsMetricsChartKey } from '@/lib/queryKeys'
import {
  TAB_METRICS_GRANULARITY_OPTIONS,
  TAB_METRICS_RANGE_DAYS_DEFAULT,
  TAB_METRICS_RANGE_OPTIONS,
  getSignalTone,
} from './utils'

type TabMetricsFilters = {
  filterEtId: string | undefined
  debouncedSearch: string
  queryStatuses: string[] | undefined
  filterTag: string
}

/**
 * The card title: "Event volume" on the All tab, "<Type> volume" on a type
 * tab, "Review queue volume" / "Archived events volume" on the queues.
 */
function volumeTitle(tabLabel: string, isTypeTab: boolean): string {
  return !isTypeTab && tabLabel === 'All events' ? 'Event volume' : `${tabLabel} volume`
}

/** The bucket a range reads best at: hourly past a week is a dense sawtooth (EV-21). */
function defaultGranularity(rangeDays: number): MetricsGranularity {
  return rangeDays > 7 ? 'day' : 'hour'
}

/**
 * The open signal as a marker on the chart, so "View signal" points at
 * something the reader can see: the line alone showed no anomaly (EV-21).
 */
function signalAnnotation(signal: MonitoringSignal | null): ChartAnnotation[] | undefined {
  if (!signal?.bucket) return undefined
  return [
    {
      id: `signal-${signal.scope_type}-${signal.scope_ref}-${signal.bucket}`,
      project_id: '',
      // Only the chart reads this: the bucket, label and colour.
      scope_type: null,
      scope_ref: signal.scope_ref,
      bucket: signal.bucket,
      label: 'Open signal',
      description: null,
      color: 'var(--danger)',
      source: 'manual',
      url: null,
      created_by_user_id: null,
      created_at: signal.bucket,
    },
  ]
}

/**
 * "<Tab> volume" card sitting above the events table. Owns its own
 * range/granularity state plus the eventsMetrics query — only the filters
 * that scope the query and the active-tab signal need to flow in from the
 * page.
 */
export function TabMetricsCard({
  slug,
  activeEt,
  activeTabLabel,
  activeTabSignal,
  isOpen,
  onOpenChange,
  filters,
  unappliedFilters = [],
  branchId,
}: {
  slug: string
  activeEt: EventType | null
  activeTabLabel: string
  activeTabSignal: MonitoringSignal | null
  isOpen: boolean
  onOpenChange: (open: boolean) => void
  filters: TabMetricsFilters
  /** Active table filters this chart does not apply — see `unappliedChartFilters`. */
  unappliedFilters?: string[]
  // The active plan branch: the tag / status filter must select the events the
  // table beside it lists, not main's (tripl-vk1p).
  branchId?: string | null
}) {
  const [rangeDays, setRangeDaysState] = useState(TAB_METRICS_RANGE_DAYS_DEFAULT)
  const [granularity, setGranularity] = useState<MetricsGranularity>(
    defaultGranularity(TAB_METRICS_RANGE_DAYS_DEFAULT),
  )
  // A new range brings its own bucket size; the select still overrides it.
  const setRangeDays = (days: number) => {
    setRangeDaysState(days)
    setGranularity(defaultGranularity(days))
  }

  // Live bound, not a mount-time snapshot (tripl-jfm3.114).
  const range = useLiveTimeRange(rangeDays * 24 * 60 * 60 * 1000)
  const refetchInterval = useAdaptiveRefetchInterval({ activeMs: 60_000 })

  const { data: tabMetrics, isLoading } = useQuery({
    queryKey: eventsMetricsChartKey(slug, branchId ?? null, filters, range),
    queryFn: () =>
      eventMetricsApi.getEventsMetrics(slug, {
        event_type_id: filters.filterEtId,
        search: filters.debouncedSearch || undefined,
        status: filters.queryStatuses,
        tag: filters.filterTag || undefined,
        from: range.from,
        to: range.to,
      }, branchId),
    // Collapsed, the card shows no chart, so it neither fetches nor polls.
    enabled: !!slug && isOpen,
    refetchInterval,
    placeholderData: (prev) => prev,
  })

  const tabMetricsData = useMemo(
    () => aggregateMetricPoints(tabMetrics?.data ?? [], granularity),
    [tabMetrics?.data, granularity],
  )

  const hasChartData = tabMetricsData.length > 0
  // Loaded and empty: the card stays a header with one line, not a box around
  // "No recent volume" with live range controls (EV-16).
  const isEmpty = isOpen && !isLoading && !hasChartData
  const annotations = useMemo(() => signalAnnotation(activeTabSignal), [activeTabSignal])
  // "612k in 7d · +4% vs prior week" (EV-21). The weekly totals do not follow
  // the chart's range. Collapsed, the card does not fetch (EVT-20), so the
  // strip shows the line only while an earlier open left the series cached.
  const weekSummary = formatWeekSummary(tabMetrics)

  return (
    <Collapsible open={isOpen} onOpenChange={onOpenChange}>
      <Card className="mb-3">
        <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-2.5">
          <div className="min-w-0">
            {/* Sentence case, and says what is drawn: "Event volume", not
                "All Events Dynamics" (DS-29). The title scale is the Panel's
                (DS-4). */}
            <h2 className="text-body-sm font-semibold leading-tight">{volumeTitle(activeTabLabel, !!activeEt)}</h2>
            {/* The series is scoped to ONE scan — summing every scan
                double-counts the events a legacy/backfill scan also collected —
                so name it here rather than let the title imply the project's
                whole volume (tripl-jfm3.20). */}
            {/* Collapsed by default, and then a bare header: the chart pushed
                the table below the fold on every visit (EV-21). */}
            {isOpen && (
              <p className="text-caption leading-tight text-fg-tertiary">
                {isEmpty ? `No volume in the last ${rangeDays} days` : `Last ${rangeDays} days, grouped by ${granularity}`}
                {tabMetrics?.scan_config_name ? ` · scan: ${tabMetrics.scan_config_name}` : ''}
                {/* The collection interval rides in the subtitle instead of a
                    line of its own under the chart (EV-21). */}
                {tabMetrics?.interval ? ` · collected every ${tabMetrics.interval}` : ''}
                {weekSummary ? ` · ${weekSummary}` : ''}.
                {unappliedFilters.length > 0 && ` Not narrowed by ${unappliedFilters.join(', ')}.`}
              </p>
            )}
            {!isOpen && weekSummary && (
              <p className="text-caption leading-tight text-fg-tertiary">{weekSummary}</p>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {/* Range and bucket only while the chart is shown (EV-22); the
                bucket only while there is a series to bucket. The same range
                control the monitoring drilldown uses (LIVE-26). */}
            {isOpen && (
              <RangeSegmentedControl
                size="sm"
                value={rangeDays}
                onChange={setRangeDays}
                options={TAB_METRICS_RANGE_OPTIONS}
              />
            )}
            {isOpen && !isEmpty && (
              <>
                <Select
                  value={granularity}
                  onValueChange={value => setGranularity(value as MetricsGranularity)}
                >
                  <SelectTrigger className="h-7 w-28" aria-label="Time granularity">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {TAB_METRICS_GRANULARITY_OPTIONS.map(option => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </>
            )}
            {activeTabSignal && (
              <Button
                variant={getSignalTone(activeTabSignal).button}
                size="sm"
                className={getSignalTone(activeTabSignal).buttonClassName}
                asChild
              >
                <Link to={getMonitoringPath(slug, activeTabSignal)}>
                  <AlertTriangle className={getSignalTone(activeTabSignal).iconClassName} aria-hidden />
                  View signal
                </Link>
              </Button>
            )}
            <CollapsibleTrigger asChild>
              <Button variant="ghost" size="sm">
                {isOpen ? 'Hide chart' : 'Show chart'}
                <ChevronDown className={`transition-transform ${isOpen ? 'rotate-180' : ''}`} />
              </Button>
            </CollapsibleTrigger>
          </div>
        </div>
        <CollapsibleContent>
          {!isEmpty && (
            <CardContent className="border-t px-4 py-3">
              {isLoading ? (
                <ChartSkeleton height={160} label="Loading volume…" />
              ) : (
                // The served band multiplier rather than the chart's own
                // constant, so this card can never disagree with the drilldown
                // it links to (tripl-0zpq.299). No `color`: volume takes the one
                // fixed single-series hue (DS-27).
                <MetricsChart
                  data={tabMetricsData}
                  forecast={tabMetrics?.forecast}
                  height={160}
                  granularity={granularity}
                  sigmaThreshold={tabMetrics?.sigma_threshold}
                  annotations={annotations}
                />
              )}
            </CardContent>
          )}
        </CollapsibleContent>
      </Card>
    </Collapsible>
  )
}
