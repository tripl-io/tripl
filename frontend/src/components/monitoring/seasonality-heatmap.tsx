import { useMemo, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'

import { eventMetricsApi } from '@/api/eventMetrics'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { SectionSkeleton } from '@/components/states'
import { ErrorState } from '@/components/error-state'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import type { SeasonalityCell } from '@/types/metrics'
import { seasonalityKey } from '@/lib/queryKeys'
import { formatCompactNumber, formatNumber } from '@/lib/format'
import { pluralize } from '@/lib/plural'

interface SeasonalityHeatmapProps {
  slug: string
  scanConfigId: string
  scopeType: string
  scopeRef: string
  /** Keys the query: the window's length, not its moving bounds. */
  rangeDays: number
  /** The live window, read by each fetch. */
  timeRange: { from: string; to: string }
  color?: string
}

const WEEKDAYS_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'] as const
const HOURS_FULL = Array.from({ length: 24 }, (_, hour) => hour)
// Show every 3rd hour as an axis label so labels don't overlap on narrow viewports.
const HOUR_LABEL_EVERY = 3
// Empty slots stay faintly tinted so the grid structure reads, while active slots
// ramp across a wide opacity band so real variance (not just the single peak) shows.
const EMPTY_OPACITY = 0.06
const MIN_FILL_OPACITY = 0.18
const MAX_FILL_OPACITY = 0.96
// Discrete stops used to paint the legend gradient bar.
const LEGEND_STOPS = [0, 0.25, 0.5, 0.75, 1] as const

// The shared compact count: this file's own copy printed "1.0k" where every
// chart prints "1k" (MON-36 / DS-30).
const formatCount = formatCompactNumber

function fillOpacity(intensity: number): number {
  return MIN_FILL_OPACITY + (MAX_FILL_OPACITY - MIN_FILL_OPACITY) * intensity
}

// The grid is cut server-side from UTC bucket starts (weekday()/hour of an
// aware UTC datetime), so every slot names its zone: a UTC+3 reader would
// otherwise take "Wed 14:00" for their own afternoon (MON-5).
function slotLabel(weekday: number, hour: number): string {
  const day = WEEKDAYS_SHORT[weekday] ?? `Day ${weekday}`
  return `${day} ${hour.toString().padStart(2, '0')}:00 UTC`
}

function eventCount(count: number): string {
  return `${formatNumber(count)} ${pluralize(count, 'event', 'events')}`
}

interface HeatScale {
  opacityFor: (count: number) => number
  minCount: number
  maxCount: number
  busiest: SeasonalityCell | null
  quietest: SeasonalityCell | null
}

// Rank active cells into quantile buckets so a ~4x volume swing spreads across the
// full luminance ramp instead of collapsing into a near-uniform block — which is
// what a plain count / max_count ratio does when the distribution is skewed.
function buildScale(cells: SeasonalityCell[]): HeatScale {
  const active = cells.filter(cell => cell.count > 0)
  const uniqueSorted = Array.from(new Set(active.map(cell => cell.count))).sort(
    (a, b) => a - b,
  )
  const rankByCount = new Map<number, number>()
  uniqueSorted.forEach((count, index) => {
    const intensity = uniqueSorted.length <= 1 ? 1 : index / (uniqueSorted.length - 1)
    rankByCount.set(count, intensity)
  })

  let busiest: SeasonalityCell | null = null
  let quietest: SeasonalityCell | null = null
  for (const cell of active) {
    if (!busiest || cell.count > busiest.count) busiest = cell
    if (!quietest || cell.count < quietest.count) quietest = cell
  }

  return {
    opacityFor: count =>
      count > 0 ? fillOpacity(rankByCount.get(count) ?? 0) : EMPTY_OPACITY,
    minCount: uniqueSorted[0] ?? 0,
    maxCount: uniqueSorted[uniqueSorted.length - 1] ?? 0,
    busiest,
    quietest,
  }
}

export function SeasonalityHeatmap({
  slug,
  scanConfigId,
  scopeType,
  scopeRef,
  rangeDays,
  timeRange,
  color = 'var(--chart-1)',
}: SeasonalityHeatmapProps) {
  const { data, isLoading, isError, error, refetch } = useQuery({
    // The card renders the failure itself (MON-30).
    meta: SILENT_ERROR_META,
    // Keyed on the range length, not the live bounds: those step every five
    // minutes, and a key that moved with them refetched the grid each time
    // (MON-3). The query function reads the current window on each fetch, as
    // the By version and Breakdowns tabs do.
    queryKey: seasonalityKey(slug, scanConfigId, scopeType, scopeRef, rangeDays),
    queryFn: () =>
      eventMetricsApi.getSeasonalityHeatmap(slug, scanConfigId, {
        scope_type: scopeType,
        scope_ref: scopeRef,
        from: timeRange.from,
        to: timeRange.to,
      }),
    enabled: Boolean(slug && scanConfigId && scopeRef),
    // Keep the grid on screen while a new range loads instead of flashing
    // "Loading…".
    placeholderData: keepPreviousData,
  })

  const cellsByKey = useMemo(() => {
    const map = new Map<string, { count: number; anomaly_count: number }>()
    for (const cell of data?.cells ?? []) {
      map.set(`${cell.weekday}:${cell.hour}`, {
        count: cell.count,
        anomaly_count: cell.anomaly_count,
      })
    }
    return map
  }, [data?.cells])

  const scale = useMemo(() => buildScale(data?.cells ?? []), [data?.cells])
  // The slot under the pointer or last tapped, spelled out under the grid: a
  // native `title` was the only way to read a cell, invisible on touch and
  // unstyled in dark mode (MO-30).
  const [activeSlot, setActiveSlot] = useState<string | null>(null)

  if (isLoading) {
    return <SectionSkeleton variant="chart" label="Loading heatmap…" />
  }

  // Only when there is nothing on screen: a failed refetch behind a grid that
  // is still showing keeps the grid (placeholderData above).
  if (isError && !data) {
    // An outage used to read "Not enough data to build a seasonality heatmap",
    // which is a claim about the scope, not about the request (MON-30).
    return (
      <ErrorState
        title="Seasonality heatmap unavailable"
        error={error}
        onRetry={() => {
          void refetch()
        }}
        retryLabel="Retry"
        compact
      />
    )
  }

  if (!data || data.max_count === 0) {
    return (
      <Card>
        <CardContent className="text-body-sm text-fg-tertiary">
          Not enough data to build a seasonality heatmap for this scope yet.
        </CardContent>
      </Card>
    )
  }

  // A daily or weekly scan floors every bucket into hour 0, and a 6h scan fills
  // only 4 of 24 columns, so most cells can never hold
  // anything. Drawing the grid anyway reads as missing data — say what is
  // actually true instead.
  if (data.hourly_resolution === false) {
    return (
      <Card>
        <CardHeader>
          <CardTitle as="h2">Hour × weekday heatmap</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-body-sm text-fg-tertiary">
            This scan collects every <span className="font-medium">{data.interval}</span>, so
            there is no hour-of-day detail to plot — every bucket falls on a few fixed hours.
            Set the scan to an hourly (or finer) interval to see this heatmap.
          </p>
        </CardContent>
      </Card>
    )
  }

  const { busiest, quietest } = scale
  const gridSummary =
    busiest && quietest
      ? `Volume by weekday and hour (UTC). Busiest slot ${slotLabel(busiest.weekday, busiest.hour)} with ${eventCount(busiest.count)}; quietest active slot ${slotLabel(quietest.weekday, quietest.hour)} with ${eventCount(quietest.count)}.`
      : 'Volume by weekday and hour (UTC).'

  return (
    // The shared section-card geometry (DS-4 / MO-10): a header bar with the
    // 12.5px h2 and its subtitle, then the grid in the body.
    <Card>
      <CardHeader>
        <CardTitle as="h2">Hour × weekday heatmap</CardTitle>
        <CardDescription>
          Total volume by day-of-week and hour-of-day, in UTC. A red ring and dot mark
          slots with detected anomalies. Total in window:{' '}
          <span className="font-medium">{formatCount(data.total_count)}</span>.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* The ramp is a RANK scale, not a count scale: buildScale spreads active
            slots across the luminance range by quantile so a skewed distribution
            does not collapse into a uniform block. Labelling the ends with the
            min and max while implying a linear count in between made a mid-tone
            unreadable — it means "middle of the pack", not the midpoint of these
            two numbers. */}
        <div className="flex items-center gap-2 text-caption text-fg-tertiary">
          <span className="tabular-nums">{formatCount(scale.minCount)}</span>
          <div
            className="flex h-2 w-24 overflow-hidden rounded-sm ring-1 ring-border/60"
            aria-hidden="true"
          >
            {LEGEND_STOPS.map(stop => (
              <div
                key={stop}
                className="h-full flex-1"
                style={{ backgroundColor: color, opacity: fillOpacity(stop) }}
              />
            ))}
          </div>
          <span className="tabular-nums">{formatCount(scale.maxCount)}</span>
          <span
            className="ml-0.5"
            title="Slots are shaded by rank among the active slots, not linearly by count, so a skewed distribution still spreads across the ramp. Hover or tap a cell for its exact count."
          >
            events / slot, shaded by rank
          </span>
        </div>
        <div className="overflow-x-auto">
          <table
            className="w-full table-fixed text-caption"
            onMouseLeave={() => setActiveSlot(null)}
          >
            <caption className="sr-only">{gridSummary}</caption>
            <thead>
              <tr>
                <th scope="col" className="w-10 pb-1 pr-2 text-right font-normal text-fg-tertiary">
                  UTC
                </th>
                {HOURS_FULL.map(hour => (
                  <th
                    key={hour}
                    scope="col"
                    className="px-0 pb-1 text-center font-normal text-fg-tertiary"
                  >
                    {hour % HOUR_LABEL_EVERY === 0 ? hour.toString().padStart(2, '0') : ''}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {WEEKDAYS_SHORT.map((label, weekday) => (
                <tr key={label}>
                  <th scope="row" className="pr-2 text-right font-normal text-fg-tertiary">{label}</th>
                  {HOURS_FULL.map(hour => {
                    const key = `${weekday}:${hour}`
                    const cell = cellsByKey.get(key)
                    const count = cell?.count ?? 0
                    const anomalyCount = cell?.anomaly_count ?? 0
                    const hasAnomaly = anomalyCount > 0
                    const tooltipText = `${slotLabel(weekday, hour)} — ${eventCount(count)}${
                      hasAnomaly
                        ? ` · ${anomalyCount} ${pluralize(anomalyCount, 'anomaly bucket', 'anomaly buckets')}`
                        : ''
                    }`
                    return (
                      <td
                        key={hour}
                        className="p-[1px]"
                        data-active={activeSlot === tooltipText ? 'true' : undefined}
                        onMouseEnter={() => setActiveSlot(tooltipText)}
                        onClick={() => setActiveSlot(tooltipText)}
                      >
                        <span className="sr-only">{tooltipText}</span>
                        {/* The fill is faded on its own layer. `opacity` on the
                            ringed element faded the ring with it — to 6–18 % on
                            empty and quiet slots, exactly where an anomaly (a drop
                            to near zero) matters most. The ring stays on the
                            unfaded element, and a dot adds a shape so the mark does
                            not rest on colour alone (MON-18). The ring is its own
                            overlay painted AFTER the fill: an inset box-shadow on
                            the wrapper paints below its children, so the fill
                            (up to 96 % opaque on the busiest slot) covered it. */}
                        <div
                          data-anomaly={hasAnomaly ? 'true' : undefined}
                          className={
                            // An empty slot gets an outline, not the lightest
                            // ramp colour: "no traffic yet" and "a little
                            // traffic" looked the same (MO-30).
                            `relative h-6 w-full rounded-sm${count === 0 ? ' border border-dashed border-border' : ''}${
                              activeSlot === tooltipText ? ' ring-2 ring-ring' : ''}`
                          }
                        >
                          <div
                            aria-hidden="true"
                            data-count={count}
                            className="absolute inset-0 rounded-sm"
                            style={{
                              backgroundColor: color,
                              opacity: scale.opacityFor(count),
                            }}
                          />
                          {hasAnomaly && (
                            <span
                              aria-hidden="true"
                              data-testid="heatmap-anomaly-ring"
                              className="pointer-events-none absolute inset-0 rounded-sm ring-2 ring-inset ring-destructive"
                            />
                          )}
                          {hasAnomaly && (
                            <span
                              aria-hidden="true"
                              data-testid="heatmap-anomaly-mark"
                              className="absolute left-1/2 top-1/2 h-1.5 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-destructive ring-1 ring-background"
                            />
                          )}
                        </div>
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {/* aria-hidden: each cell already carries its sentence for screen
            readers; this line is for sighted pointer and touch users. */}
        <p
          aria-hidden="true"
          data-testid="heatmap-slot-detail"
          className="min-h-4 text-caption text-fg-tertiary tabular-nums"
        >
          {activeSlot ?? 'Hover or tap a cell for its count.'}
        </p>
      </CardContent>
    </Card>
  )
}
