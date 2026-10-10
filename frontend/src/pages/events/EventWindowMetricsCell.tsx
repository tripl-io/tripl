import { memo } from 'react'
import type { EventMetricPoint } from '@/types'
import { MiniMetricsChart } from '@/components/ui/chart-lazy'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { Sparkline } from '@/components/primitives/sparkline'
import { formatCompactNumber } from '@/lib/format'

export const EventWindowMetricsCell = memo(function EventWindowMetricsCell({
  eventName,
  color,
  totalCount,
  data,
  anomalyIdx,
  signalTone,
  pending = false,
}: {
  eventName: string
  /** The event type's colour; unset falls back to the fixed single-series hue. */
  color?: string
  totalCount: number | undefined
  data: EventMetricPoint[]
  anomalyIdx?: number | null
  signalTone?: 'danger' | 'warning' | null
  /** Metrics not answered yet: a pulsing placeholder, never the "—" that
   *  means "no data". */
  pending?: boolean
}) {
  if (pending) {
    return (
      <span
        role="img"
        aria-label={`${eventName} metrics: loading`}
        className="grid w-[106px] grid-cols-[60px_38px] items-center gap-2"
      >
        <span
          aria-hidden="true"
          className="block h-3 w-[60px] animate-pulse rounded-sm bg-surface-hover motion-reduce:animate-none"
        />
        <span
          aria-hidden="true"
          className="ml-auto block h-3 w-6 animate-pulse rounded-sm bg-surface-hover motion-reduce:animate-none"
        />
      </span>
    )
  }
  const noData = totalCount == null
  // A no-data cell ("—") and a real zero both recede; only a populated count
  // carries the regular muted weight so live volume stands out.
  const isEmptyOrZero = noData || totalCount === 0
  const label = noData ? '—' : formatCompactNumber(totalCount)
  const counts = data.map((p) => p.count)
  // The line keeps its series hue whatever the signal: an anomaly is
  // the red dot at `anomalyIdx`, and the tone only colours the count. An unset
  // colour lets Sparkline and the chart fall back to SINGLE_SERIES_COLOR, not
  // the user's accent.
  const sparkColor = color || undefined
  const ariaLabel = noData
    ? `${eventName} metrics: no data for the last 48 hours`
    : `${eventName} metrics: ${label} events in last 48 hours`

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {/* Not a button: it does nothing when pressed, and as one it was an
            extra dead tab stop on every row. The count it shows is
            in the label, so a screen reader loses nothing; the chart in the
            tooltip is a pointer-only enlargement of the sparkline. */}
        <span
          role="img"
          aria-label={ariaLabel}
          className="tnum grid w-[106px] grid-cols-[60px_38px] items-center gap-2 text-caption font-medium hover:text-foreground"
          style={{
            color: signalTone
              ? `var(--${signalTone})`
              : isEmptyOrZero
                ? 'var(--fg-faint)'
                : 'var(--fg-muted)',
          }}
        >
          {counts.length > 1 ? (
            <Sparkline
              data={counts}
              color={sparkColor}
              width={60}
              height={16}
              anomalyIdx={anomalyIdx ?? null}
            />
          ) : (
            <span className="block h-4 w-[60px]" aria-hidden="true" />
          )}
          <span className="block w-[38px] text-right" aria-hidden="true">{label}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent
        className="w-[22rem] max-w-[calc(100vw-2rem)] border bg-popover p-0 text-foreground shadow-md"
        side="top"
      >
        <div className="space-y-3 p-3">
          <div className="space-y-1">
            <p className="break-words text-body-sm font-medium">{eventName}</p>
            <div className="flex items-center justify-between gap-3 text-caption text-fg-tertiary">
              <span>Last 48 hours</span>
              <span>{noData ? 'No data' : `${formatCompactNumber(totalCount)} events`}</span>
            </div>
          </div>
          <MiniMetricsChart data={data} color={sparkColor} height={104} label="Event volume trend" />
        </div>
      </TooltipContent>
    </Tooltip>
  )
})
