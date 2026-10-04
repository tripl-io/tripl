/**
 * Pure series builders for the monitoring drilldown's multi-series charts (the
 * By version and Breakdowns tabs). Kept out of the components so the contracts
 * they encode (pre-release vs latest, legend value kind, the render cap and the
 * palette) are table-tested directly.
 */
import {
  aggregateMetricPoints,
  type MetricRollupMode,
  type MetricsGranularity,
} from '@/lib/metrics'
import type { AppVersionMetricSeries, EventMetricBreakdownSeries, EventMetricPoint } from '@/types'
import { SERIES_COLORS } from '@/components/ui/chart-format'

// The chart's own categorical palette: this used to be a
// second copy of the list, hard-coded hexes included.

/** One slot of the series palette: a colour plus, past the eighth, a dash. */
export interface SeriesSlot {
  color: string
  dash?: string
}

/**
 * The palette slot for the `index`-th series. Eight hues, then the hues repeat
 * dashed and then dotted: value #9 used to get #1's colour and draw an
 * identical line.
 */
export function seriesSlot(index: number): SeriesSlot {
  const color = SERIES_COLORS[index % SERIES_COLORS.length] ?? SERIES_COLORS[0]
  const round = Math.floor(index / SERIES_COLORS.length)
  if (round === 0) return { color }
  return { color, dash: round === 1 ? '6 3' : '2 3' }
}

/** Which number a legend entry prints: the window total, or the newest value. */
export type LegendValueKind = 'total' | 'latest'

/** A sum is only meaningful for additive series; everything else shows its latest value. */
export function legendValueKindFor(mode: MetricRollupMode): LegendValueKind {
  return mode === 'sum' ? 'total' : 'latest'
}

export type VersionFilter = 'all' | 'latest'

export interface VersionChartSeries extends SeriesSlot {
  label: string
  version: string
  isOther: boolean
  isLatest: boolean
  // The SemVer-newest release that has NOT taken a real share of traffic yet
  // (backend is_latest && !is_active): a pre-release / not-yet-rolled-out build.
  isPreRelease: boolean
  totalCount: number
  legendValue: number
  data: EventMetricPoint[]
  isHighlighted: boolean
}

export function formatVersionLabel(version: AppVersionMetricSeries): string {
  const label = version.is_other ? 'Other' : (version.version || '(empty)')
  if (version.is_other || !version.is_latest) return label
  // The gated-newest release reads "· latest" once rolled out, but "· pre-release"
  // while it hasn't taken a real share of traffic (is_active=false).
  return version.is_active ? `${label} · latest` : `${label} · pre-release`
}

export function buildVersionChartSeries(
  series: AppVersionMetricSeries[],
  granularity: MetricsGranularity,
  versionFilter: VersionFilter,
  latestVersion: string | null | undefined,
  rollupMode: MetricRollupMode = 'sum',
): VersionChartSeries[] {
  const legendValueKind = legendValueKindFor(rollupMode)
  return series
    .filter(item => versionFilter === 'all' || (!!latestVersion && item.is_latest))
    .map((item, index) => {
      const isNewest = item.is_latest && !item.is_other
      // Only the rolled-out (active) newest release keeps the primary "latest"
      // identity and highlight; a not-yet-active newest release is a pre-release.
      const isActiveLatest = isNewest && item.is_active
      const isPreRelease = isNewest && !item.is_active
      const slot = seriesSlot(index)
      return {
        label: formatVersionLabel(item),
        version: item.version,
        isOther: item.is_other,
        isLatest: isActiveLatest,
        isPreRelease,
        totalCount: item.total_count,
        legendValue: legendValueKind === 'latest'
          ? item.data.at(-1)?.count ?? item.total_count
          : item.total_count,
        data: aggregateMetricPoints(item.data, granularity, rollupMode),
        color: isActiveLatest
          ? 'var(--primary)'
          : isPreRelease
            ? 'var(--warning)'
            : item.is_other
              ? 'var(--fg-tertiary)'
              : slot.color,
        dash: isActiveLatest || isPreRelease || item.is_other ? undefined : slot.dash,
        isHighlighted: isActiveLatest,
      }
    })
}

/** The most breakdown series the chart draws at once. */
export const BREAKDOWN_SERIES_CAP = 8

export interface BreakdownSeriesEntry extends SeriesSlot {
  label: string
  /** What the chip prints: the window total, or the newest value (see legendKind). */
  legendValue: number
  data: EventMetricPoint[]
}

export function breakdownLabel(series: Pick<EventMetricBreakdownSeries, 'is_other' | 'breakdown_value'>): string {
  return series.is_other ? 'Other' : (series.breakdown_value || '(empty)')
}

/**
 * One entry per breakdown value, each pinned to a palette slot from the
 * UNFILTERED order so a series keeps its colour when the value filter hides its
 * neighbours.
 */
export function buildBreakdownEntries(
  series: EventMetricBreakdownSeries[],
  legendKind: LegendValueKind,
): BreakdownSeriesEntry[] {
  return series.map((item, index) => ({
    label: breakdownLabel(item),
    ...seriesSlot(index),
    legendValue: legendKind === 'latest'
      ? item.data.at(-1)?.count ?? item.total_count
      : item.total_count,
    data: item.data,
  }))
}

/**
 * The series the chart draws: the picked values (or every value), filtered
 * BEFORE the render cap so a value outside the top eight becomes visible once
 * picked. `hiddenCount` is how many matching values the cap left out, which the
 * tab says out loud instead of dropping them silently.
 */
export function selectBreakdownChartSeries(
  entries: BreakdownSeriesEntry[],
  selected: string[],
  granularity: MetricsGranularity,
  rollupMode: MetricRollupMode,
): { series: Array<SeriesSlot & { label: string; data: EventMetricPoint[] }>; hiddenCount: number } {
  const matching = entries.filter(entry => selected.length === 0 || selected.includes(entry.label))
  return {
    series: matching.slice(0, BREAKDOWN_SERIES_CAP).map(entry => ({
      label: entry.label,
      color: entry.color,
      dash: entry.dash,
      data: aggregateMetricPoints(entry.data, granularity, rollupMode),
    })),
    hiddenCount: Math.max(0, matching.length - BREAKDOWN_SERIES_CAP),
  }
}

export function formatPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`
}
