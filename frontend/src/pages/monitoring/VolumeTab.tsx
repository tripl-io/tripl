import { TrendingUp } from 'lucide-react'
import { EmptyState } from '@/components/empty-state'
import { TopMoversPanel } from '@/components/monitoring/top-movers-panel'
import { ChartSkeleton } from '@/components/states'
import { Card, CardContent, CardTitle } from '@/components/ui/card'
import type { PartialWindow } from '@/components/ui/chart'
import { MetricsChart } from '@/components/ui/chart-lazy'
import { formatIncidentCount } from '@/lib/alertStatus'
import { formatNumber } from '@/lib/format'
import { formatMetricValue } from '@/lib/metricFormat'
import type { MetricsGranularity } from '@/lib/metrics'
import type { MonitoringScope } from '@/lib/monitoring'
import type { EventMetricPoint, EventMetricsResponse } from '@/types'
import { AnnotationsCard } from './AnnotationsCard'
import { PlannedEventsCard } from './PlannedEventsCard'
import { chartCaption } from './chartCaption'
import { ChartCardHeader, MetricsRangeControls } from './MetricsRangeControls'
import { SignalSummary } from './SignalSummary'
import type { useChartAnnotations } from './useChartAnnotations'
import { usePlannedEvents } from './usePlannedEvents'

/**
 * The Volume (or, on a catalog metric, Value) tab: the latest signal in one
 * sentence, what moved it, the series chart, and the annotations and planned
 * events (F18) on it.
 */
export function VolumeTab({
  slug,
  scope,
  scopeId,
  label,
  metrics,
  metricUnit,
  chartData,
  chartIsLoading,
  chartEmptyDescription,
  chartColor,
  granularity,
  nativeGranularity,
  rangeDays,
  timeRange,
  partialBuckets,
  seriesLabel,
  valueFormatter,
  tooltipFormatter,
  annotationsQuery,
  annotatePrefill,
  dataEnd,
  canWrite,
  onAnnotate,
  onRangeDaysChange,
  onGranularityChange,
}: {
  slug: string | undefined
  scope: MonitoringScope
  scopeId: string
  label: string
  metrics: EventMetricsResponse | undefined
  metricUnit: string | null
  chartData: EventMetricPoint[]
  chartIsLoading: boolean
  chartEmptyDescription: string
  chartColor: string | undefined
  granularity: MetricsGranularity
  nativeGranularity: MetricsGranularity | null
  rangeDays: number
  timeRange: { from: string; to: string }
  partialBuckets: PartialWindow | undefined
  seriesLabel: string
  valueFormatter: ((value: number) => string) | undefined
  tooltipFormatter: ((value: number) => string) | undefined
  annotationsQuery: ReturnType<typeof useChartAnnotations>
  annotatePrefill: string | null
  dataEnd: string | null
  canWrite: boolean
  /** The summary's "Annotate"; the event scope has its own in the hero banner. */
  onAnnotate?: (bucket: string) => void
  onRangeDaysChange: (days: number) => void
  onGranularityChange: (granularity: MetricsGranularity) => void
}) {
  const isMetricScope = scope === 'metric'
  const latestSignal = metrics?.latest_signal
  const lastBucket = metrics?.data[metrics.data.length - 1]?.bucket
  // The API forecasts exactly one native collection bucket. Once actuals are
  // rolled up (for example 1h -> day), that single point is not a forecast for
  // the whole display bucket and can even duplicate the last x-axis date.
  const chartForecast = nativeGranularity === granularity ? metrics?.forecast : undefined
  const plannedEventsQuery = usePlannedEvents({ slug, scope, scopeId, rangeDays, timeRange })

  return (
    <>
      {latestSignal && (
        // One sentence and its reason, not a 4-up grid of raw figures
        // the reader had to assemble (MO-2 / MO-4).
        <SignalSummary
          signal={latestSignal}
          formatActual={value => (isMetricScope
            ? formatMetricValue(value, metricUnit)
            : `${formatNumber(value)} ${value === 1 ? 'event' : 'events'}`)}
          formatExpected={value => (isMetricScope
            ? formatMetricValue(value, metricUnit)
            : // Value-aware: an event count can still carry a
              // sub-unit baseline, which plain rounding wrote as "0".
              formatIncidentCount(value))}
          sigmaThreshold={metrics?.sigma_threshold}
          onAnnotate={onAnnotate}
        />
      )}

      {/* scan_config_id is NULL only for metric-scope signals, which the
          scope guard already excludes — but it is checked rather than
          asserted, so a future scope that also lacks one cannot put a null
          into the query key. */}
      {latestSignal?.scan_config_id && slug && !isMetricScope && (
        <TopMoversPanel
          slug={slug}
          scanConfigId={latestSignal.scan_config_id}
          scopeType={latestSignal.scope_type}
          scopeRef={latestSignal.scope_ref}
          bucket={latestSignal.bucket}
          rangeDays={rangeDays}
          timeRange={timeRange}
        />
      )}

      {/* One section-card geometry (DS-4 / MO-10): the header bar with
          a 12.5px h2 and the range controls, a 16px body. */}
      <Card>
        <ChartCardHeader title={<CardTitle as="h2">{label}</CardTitle>}>
          <MetricsRangeControls
            rangeDays={rangeDays}
            granularity={granularity}
            nativeGranularity={nativeGranularity}
            onRangeDaysChange={onRangeDaysChange}
            onGranularityChange={onGranularityChange}
          />
        </ChartCardHeader>
        <CardContent>
          {chartIsLoading ? (
            <ChartSkeleton height={200} label="Loading monitoring data…" />
          ) : chartData.length === 0 ? (
            <div className="h-[200px] flex items-center justify-center">
              <EmptyState
                icon={TrendingUp}
                title="No metrics data available"
                description={chartEmptyDescription}
              />
            </div>
          ) : (
            <MetricsChart
              data={chartData}
              forecast={chartForecast}
              annotations={annotationsQuery.data ?? []}
              plannedEvents={plannedEventsQuery.data ?? []}
              height={200}
              // The entity's own colour when it has one; otherwise the
              // chart's fixed single-series default (DS-27), not an
              // arbitrary chart slot.
              color={chartColor}
              granularity={granularity}
              seriesLabel={seriesLabel}
              valueFormatter={valueFormatter}
              tooltipFormatter={tooltipFormatter}
              // The sigma the detector scored THIS scope with, so the band
              // and the "±Nσ" tooltip agree with the dots inside them. The
              // metric scope serves it too (`adaptMetricSeries`, tripl-4cgl).
              sigmaThreshold={metrics?.sigma_threshold}
              // The axis spans the range picked above, not just the
              // buckets that have data (MON-22).
              from={timeRange.from}
              to={timeRange.to}
              // Rolled-up first/last buckets the data only partly
              // covers draw dashed, not as cliffs (MO-5).
              partial={partialBuckets}
              // What the dashes, whiskers and triangles mean (MO-1).
              legend
            />
          )}
          {/* The cadence, the newest bucket and the scan's last and next
              collection (L4), not the raw interval string; nothing under an
              empty chart, and nothing on a metric, whose Definition already
              names the cadence and the next update (MO-39 / MO-33). */}
          {!chartIsLoading && chartData.length > 0 && !isMetricScope && nativeGranularity && (
            <p className="mt-2 text-caption text-fg-tertiary" data-testid="chart-caption">
              {chartCaption({
                cadence: nativeGranularity,
                lastBucket,
                lastCollectedAt: metrics?.last_collected_at,
                nextCollectionAt: metrics?.next_collection_at,
              })}
            </p>
          )}
        </CardContent>
      </Card>

      {slug && (
        <AnnotationsCard
          slug={slug}
          scope={scope}
          scopeId={scopeId}
          canWrite={canWrite}
          query={annotationsQuery}
          prefillBucket={annotatePrefill}
          dataEnd={dataEnd}
        />
      )}

      {slug && (
        <PlannedEventsCard
          slug={slug}
          scope={scope}
          scopeId={scopeId}
          canWrite={canWrite}
          query={plannedEventsQuery}
        />
      )}
    </>
  )
}
