import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Layers, Pencil } from 'lucide-react'
import { eventMetricsApi } from '@/api/eventMetrics'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { ErrorState } from '@/components/error-state'
import { Chip } from '@/components/primitives/chip'
import { ChartSkeleton } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardTitle } from '@/components/ui/card'
import { MetricsMultiSeriesChart } from '@/components/ui/chart-lazy'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { formatNumber } from '@/lib/format'
import { adaptMetricBreakdowns } from '@/lib/metricAdapters'
import type { MetricRollupMode, MetricsGranularity } from '@/lib/metrics'
import { monitoringBreakdownsColumnKey } from '@/lib/queryKeys'
import {
  BREAKDOWN_SERIES_CAP,
  breakdownLabel,
  buildBreakdownEntries,
  formatPercent,
  legendValueKindFor,
  selectBreakdownChartSeries,
  type BreakdownSeriesEntry,
  type LegendValueKind,
} from './chartSeries'
import { ChartCardHeader, MetricsRangeControls } from './MetricsRangeControls'
import { SeriesSwatch } from './SeriesSwatch'

export interface BreakdownsTabProps {
  slug: string
  scope: 'event' | 'metric'
  scopeId: string
  rangeDays: number
  timeRange: { from: string; to: string }
  granularity: MetricsGranularity
  rollupMode: MetricRollupMode
  refetchInterval: number | false
  column: string
  selectedValues: string[]
  seriesLabel: string
  valueFormatter?: (value: number) => string
  /** The chart tooltip and legend spelling; see MetricsChartProps.tooltipFormatter. */
  tooltipFormatter?: (value: number) => string
  metricEditPath: string
  /** The series' collection granularity; see MetricsRangeControls. */
  nativeGranularity: MetricsGranularity | null
  onRangeDaysChange: (days: number) => void
  onGranularityChange: (granularity: MetricsGranularity) => void
  onColumnChange: (column: string) => void
  onSelectedValuesChange: (values: string[]) => void
}

/**
 * Breakdowns: split this event's volume (or this metric's value) into a series
 * per value of a chosen column. Columns come from the entity's configured
 * breakdown columns plus scan-wide breakdown columns that have collected data.
 */
export function BreakdownsTab({
  slug,
  scope,
  scopeId,
  rangeDays,
  timeRange,
  granularity,
  rollupMode,
  refetchInterval,
  column,
  selectedValues,
  seriesLabel,
  valueFormatter,
  tooltipFormatter,
  metricEditPath,
  nativeGranularity,
  onRangeDaysChange,
  onGranularityChange,
  onColumnChange,
  onSelectedValuesChange,
}: BreakdownsTabProps) {
  const navigate = useNavigate()
  const query = useQuery({
    queryKey: monitoringBreakdownsColumnKey(slug, scope, scopeId, column, rangeDays),
    queryFn: () => {
      if (scope === 'metric') {
        return metricsCatalogApi
          .getBreakdowns(slug, scopeId, { column: column || undefined, ...timeRange })
          .then(adaptMetricBreakdowns)
      }
      return eventMetricsApi.getEventMetricBreakdowns(slug, scopeId, {
        column: column || undefined,
        ...timeRange,
      })
    },
    enabled: !!scopeId,
    refetchInterval,
    placeholderData: keepPreviousData,
    meta: SILENT_ERROR_META,
  })
  const breakdowns = query.data
  const selectedColumn = column || breakdowns?.selected_column || ''
  const legendKind = legendValueKindFor(rollupMode)

  const entries = useMemo(
    () => buildBreakdownEntries(breakdowns?.series ?? [], legendKind),
    [breakdowns?.series, legendKind],
  )
  // Only labels that still exist in the response count: a stale selection
  // (e.g. after a range change drops a value) falls back to "show everything"
  // instead of an inexplicably empty chart.
  const effectiveFilter = useMemo(
    () => selectedValues.filter(label => entries.some(entry => entry.label === label)),
    [entries, selectedValues],
  )
  const chart = useMemo(
    () => selectBreakdownChartSeries(entries, effectiveFilter, granularity, rollupMode),
    [entries, effectiveFilter, granularity, rollupMode],
  )
  const latestParityAnomalies = useMemo(
    () => (breakdowns?.series ?? []).flatMap(series => {
      const latest = [...(series.parity_anomalies ?? [])]
        .sort((left, right) => left.bucket.localeCompare(right.bucket))
        .at(-1)
      return latest ? [{ series, anomaly: latest }] : []
    }),
    [breakdowns?.series],
  )
  const toggleValue = (label: string) => {
    onSelectedValuesChange(
      effectiveFilter.includes(label)
        ? effectiveFilter.filter(value => value !== label)
        : [...effectiveFilter, label],
    )
  }

  return (
    <Card>
      <ChartCardHeader
        title={(
          <>
            <Layers aria-hidden="true" className="size-4 text-fg-tertiary" />
            <CardTitle as="h2">Breakdowns</CardTitle>
          </>
        )}
      >
        <Select
          value={selectedColumn}
          onValueChange={onColumnChange}
          disabled={!breakdowns?.columns.length}
        >
          <SelectTrigger className="w-full sm:w-[200px]" aria-label="Breakdown column">
            <SelectValue placeholder="Column" />
          </SelectTrigger>
          <SelectContent>
            {breakdowns?.columns.map(option => (
              <SelectItem key={option} value={option}>
                {option}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {/* The window this chart covers, set here too: it used to follow the
            Volume tab's controls without saying so. */}
        <MetricsRangeControls
          rangeDays={rangeDays}
          granularity={granularity}
          nativeGranularity={nativeGranularity}
          onRangeDaysChange={onRangeDaysChange}
          onGranularityChange={onGranularityChange}
        />
      </ChartCardHeader>
      <CardContent>
        {query.isError ? (
          // Not "No breakdown groups yet" — that told the reader to go
          // configure something that was already configured.
          <ErrorState
            compact
            title="Could not load breakdowns"
            error={query.error}
            onRetry={() => void query.refetch()}
          />
        ) : query.isLoading ? (
          <ChartSkeleton height={280} label="Loading breakdowns…" />
        ) : !breakdowns?.columns.length ? (
          <div className="flex h-[280px] flex-col items-center justify-center gap-1 text-center text-body text-fg-tertiary">
            <p>No breakdown groups yet.</p>
            {scope === 'metric' ? (
              <>
                <p className="text-body-sm">
                  Add breakdown columns in the metric settings — each configured
                  column splits this metric into a series per value after the next
                  collection.
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-2"
                  onClick={() => navigate(metricEditPath)}
                >
                  <Pencil className="mr-2 h-4 w-4" />
                  Edit metric
                </Button>
              </>
            ) : (
              <p className="text-body-sm">
                Edit this event and add a column under “Metric breakdowns”, then run a
                scan — its volume will split into a series per value of that column.
              </p>
            )}
          </div>
        ) : (
          <>
            <MetricsMultiSeriesChart
              series={chart.series}
              height={280}
              granularity={granularity}
              seriesLabel={seriesLabel}
              valueFormatter={valueFormatter}
              tooltipFormatter={tooltipFormatter}
              from={timeRange.from}
              to={timeRange.to}
            />
            {chart.hiddenCount > 0 && (
              <p className="mt-2 text-body-sm text-fg-tertiary">
                Showing the first {BREAKDOWN_SERIES_CAP} of {chart.series.length + chart.hiddenCount} values
                — pick values below to compare others.
              </p>
            )}
            <BreakdownValueChips
              options={entries}
              selected={effectiveFilter}
              valueFormatter={tooltipFormatter ?? valueFormatter}
              valueKind={legendKind}
              onToggle={toggleValue}
              onReset={() => onSelectedValuesChange([])}
            />
            {latestParityAnomalies.length > 0 && (
              <div className="mt-4 rounded-md border border-border bg-muted/30 p-3">
                <p className="mb-2 text-body-sm font-medium text-fg-secondary">
                  {selectedColumn} share anomalies
                </p>
                <div className="flex flex-wrap gap-2">
                  {latestParityAnomalies.map(({ series, anomaly }) => (
                    // A status flag in the one pill idiom: a drop in the
                    // danger tone, a spike in the warning tone.
                    <Chip
                      key={`${series.breakdown_value}-${anomaly.bucket}`}
                      aria-label={`${breakdownLabel(series)} share ${anomaly.direction}: ${formatPercent(anomaly.expected_share)} -> ${formatPercent(anomaly.actual_share)}`}
                      tone={anomaly.direction === 'drop' ? 'danger' : 'warning'}
                    >
                      {breakdownLabel(series)}
                      {' share '}{anomaly.direction}:{' '}
                      {formatPercent(anomaly.expected_share)} {'->'} {formatPercent(anomaly.actual_share)}
                    </Chip>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}

// Legend-style toggles for the breakdown chart: when the selected column has
// many values, click chips to isolate one or several series.
// Empty selection = every value shown, and a single-value breakdown has
// nothing to filter, so the row hides itself.
function BreakdownValueChips({
  options,
  selected,
  valueFormatter,
  valueKind,
  onToggle,
  onReset,
}: {
  options: BreakdownSeriesEntry[]
  selected: string[]
  valueFormatter?: (value: number) => string
  valueKind: LegendValueKind
  onToggle: (label: string) => void
  onReset: () => void
}) {
  if (options.length < 2) return null
  const hasFilter = selected.length > 0
  return (
    <div className="mt-3 flex flex-wrap items-center gap-2">
      {options.map(option => {
        const isSelected = selected.includes(option.label)
        const isVisible = !hasFilter || isSelected
        const value = valueFormatter ? valueFormatter(option.legendValue) : formatNumber(option.legendValue)
        return (
          <button
            key={option.label}
            type="button"
            aria-pressed={isSelected}
            aria-label={`Toggle ${option.label}`}
            onClick={() => onToggle(option.label)}
            className={`flex min-w-0 items-center gap-2 rounded-md border px-2 py-1 text-body-sm transition-colors hover:bg-muted/40 ${
              isSelected
                ? 'border-[var(--accent)]/60 bg-[var(--accent-soft)]'
                : isVisible
                  ? 'bg-background'
                  : 'bg-background opacity-50'
            }`}
          >
            <SeriesSwatch color={option.color} dash={option.dash} />
            <span className="min-w-0 truncate font-mono">{option.label}</span>
            <span className="shrink-0 text-fg-tertiary">
              {valueKind === 'latest' ? `latest ${value}` : value}
            </span>
          </button>
        )
      })}
      {hasFilter && (
        <Button type="button" variant="ghost" size="xs" onClick={onReset}>
          Show all
        </Button>
      )}
    </div>
  )
}
