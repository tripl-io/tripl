import type { ReactNode } from 'react'
import { RangeSegmentedControl } from '@/components/range-segmented-control'
import { CardHeader } from '@/components/ui/card'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select'
import { GRANULARITY_OPTIONS, granularityFitsRange, type MetricsGranularity } from '@/lib/metrics'

/**
 * Range + granularity for a drilldown chart. Granularities that would draw more
 * than the per-series point cap over the selected range are disabled rather
 * than offered; the page clamps a sticky pick the same way. The
 * series' native collection granularity is always offered, and an option
 * that is not says why beside its label.
 *
 * Both controls share one 32px height and the 12.5px control text, so the pair
 * no longer reads as two sizes side by side.
 */
export function MetricsRangeControls({
  rangeDays,
  granularity,
  nativeGranularity,
  onRangeDaysChange,
  onGranularityChange,
}: {
  rangeDays: number
  granularity: MetricsGranularity
  /** The series' collection granularity, exempt from the point cap. */
  nativeGranularity: MetricsGranularity | null
  onRangeDaysChange: (days: number) => void
  onGranularityChange: (granularity: MetricsGranularity) => void
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <RangeSegmentedControl value={rangeDays} onChange={onRangeDaysChange} />
      <Select
        value={granularity}
        onValueChange={(value: MetricsGranularity) => onGranularityChange(value)}
      >
        <SelectTrigger className="w-[130px]" aria-label="Time granularity">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {GRANULARITY_OPTIONS.map(option => {
            const fits = granularityFitsRange(option.value, rangeDays, nativeGranularity)
            return (
              <SelectItem
                key={option.value}
                value={option.value}
                disabled={!fits}
              >
                {option.label}
                {/* A greyed option with no reason read as broken. The reason
                    is visible on the row but stays out of the option's name. */}
                {!fits && (
                  <span aria-hidden="true" className="text-caption text-fg-subtle">
                    {`— too many points for ${rangeDays}d`}
                  </span>
                )}
              </SelectItem>
            )
          })}
        </SelectContent>
      </Select>
    </div>
  )
}

/**
 * A chart card's header bar: the title on its own row on a phone, the controls
 * wrapping beneath it, side by side from `sm` up. It is the
 * card's `CardHeader` (the shared section-card geometry), so the
 * chart goes in a `CardContent` after it. Pass the title as
 * `<CardTitle as="h2">`.
 */
export function ChartCardHeader({
  title,
  children,
}: {
  title: ReactNode
  children: ReactNode
}) {
  return (
    <CardHeader className="gap-3 py-2.5 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between">
      <div className="flex min-w-0 flex-wrap items-center gap-2">{title}</div>
      <div className="flex min-w-0 flex-wrap items-center gap-2">{children}</div>
    </CardHeader>
  )
}
