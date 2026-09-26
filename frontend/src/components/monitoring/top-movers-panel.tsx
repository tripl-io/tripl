import { formatNumber } from '@/lib/format'
import { useMemo, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, ChevronDown, ChevronRight } from 'lucide-react'

import { eventMetricsApi } from '@/api/eventMetrics'
import { ErrorState } from '@/components/error-state'
import { Chip } from '@/components/primitives/chip'
import { SectionSkeleton } from '@/components/states'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { MetricsChart } from '@/components/ui/chart'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { granularityForInterval } from '@/lib/metricAdapters'
import { formatSignalEffectDetail } from '@/lib/monitoring'
import { NO_BASELINE_LABEL, formatRatioDelta, ratioDelta } from '@/lib/percentDelta'
import { signalDirectionColor, signalDirectionTone } from '@/lib/statusLexicon'
import type {
  BreakdownTimelinePoint,
  ChartAnnotation,
  EventMetricPoint,
  TopMoverItem,
} from '@/types'
import { breakdownTimelineKey, topMoversKey } from '@/lib/queryKeys'

interface TopMoversPanelProps {
  slug: string
  scanConfigId: string
  scopeType: string
  scopeRef: string
  bucket: string
  limit?: number
  /** Keys the drilldown timeline: the window's length, not its moving bounds. */
  rangeDays?: number
  /** The live window, read by each timeline fetch. */
  timeRange?: { from: string; to: string }
}

// The app locale, not the browser's: the chart beside this list already
// prints its numbers in it (DS-30).
function formatCount(value: number): string {
  return formatNumber(Math.round(value))
}

/**
 * What the chip beside a top-mover row says: a signed percentage, the words
 * `no baseline`, or '' when there is genuinely nothing to add.
 *
 * Two cases used to share the empty string and so looked identical in the row:
 * a real change too small to round to a whole percent, and a breakdown value
 * with no baseline at all. Only the first is genuinely nothing to say — the
 * signed absolute-delta badge next to this chip already carries it. The second
 * is a fact, and one the detector admits on purpose (a brand-new breakdown value
 * whose median expectation is 0 passes `min_expected_count` at its floor), so it
 * is named rather than left as a gap in a column every other row fills
 * (tripl-l429.27).
 */
function percentDelta(actual: number, expected: number): string {
  const pct = ratioDelta(actual, expected)
  if (pct === null) return NO_BASELINE_LABEL
  if (Math.abs(pct) < 0.5) return ''
  return formatRatioDelta(pct)
}

export function TopMoversPanel({
  slug,
  scanConfigId,
  scopeType,
  scopeRef,
  bucket,
  limit = 8,
  rangeDays,
  timeRange,
}: TopMoversPanelProps) {
  const [expandedKey, setExpandedKey] = useState<string | null>(null)
  const { data, isLoading, isError, isPlaceholderData, error, refetch } = useQuery({
    // Rendered inline below (MON-30).
    meta: SILENT_ERROR_META,
    queryKey: topMoversKey(slug, scanConfigId, scopeType, scopeRef, bucket, limit),
    queryFn: () =>
      eventMetricsApi.getTopMovers(slug, scanConfigId, {
        scope_type: scopeType,
        scope_ref: scopeRef,
        bucket,
        limit,
      }),
    enabled: Boolean(slug && scanConfigId && scopeRef && bucket),
  })

  if (isLoading) {
    return <SectionSkeleton variant="list" rows={3} label="Loading top movers…" />
  }

  // A failed request used to return null, so an outage looked exactly like an
  // anomaly with no breakdown behind it (MON-30). Only when there is nothing
  // of this query's own on screen, as in the seasonality heatmap: a failed
  // background refetch behind loaded rows (possibly with a drilldown open)
  // keeps them and says so inline below.
  if (isError && (!data || isPlaceholderData)) {
    return (
      <ErrorState
        title="Top movers unavailable"
        error={error}
        onRetry={() => {
          void refetch()
        }}
        retryLabel="Retry"
        compact
      />
    )
  }

  if (!data || data.length === 0) {
    return null
  }

  return (
    // The shared section-card geometry (DS-4 / MO-10): a header bar with the
    // 12.5px h2 and its subtitle, then the rows.
    <Card>
      <CardHeader>
        <CardTitle as="h2">Top movers</CardTitle>
        <CardDescription>
          Breakdown rows ranked by |z|, for this anomaly bucket. Click a row to
          see its timeline.
        </CardDescription>
        {isError && (
          <p role="status" className="mt-1 text-body-sm text-fg-tertiary">
            Refresh failed — showing the last loaded rows.{' '}
            <button
              type="button"
              className="underline underline-offset-2 hover:text-foreground"
              onClick={() => {
                void refetch()
              }}
            >
              Retry
            </button>
          </p>
        )}
      </CardHeader>
      <CardContent className="py-1">
        <ul className="divide-y divide-border text-body">
          {data.map(item => {
            const rowKey = `${item.breakdown_column}:${item.breakdown_value}:${item.is_other}`
            const isExpanded = expandedKey === rowKey
            return (
              <li key={rowKey}>
                <TopMoverRow
                  item={item}
                  isExpanded={isExpanded}
                  onToggle={() => setExpandedKey(prev => (prev === rowKey ? null : rowKey))}
                />
                {isExpanded && (
                  <BreakdownDrilldown
                    slug={slug}
                    scanConfigId={scanConfigId}
                    scopeType={scopeType}
                    scopeRef={scopeRef}
                    bucket={bucket}
                    item={item}
                    rangeDays={rangeDays}
                    timeRange={timeRange}
                  />
                )}
              </li>
            )
          })}
        </ul>
      </CardContent>
    </Card>
  )
}

function TopMoverRow({
  item,
  isExpanded,
  onToggle,
}: {
  item: TopMoverItem
  isExpanded: boolean
  onToggle: () => void
}) {
  const delta = item.actual_count - item.expected_count
  const pct = percentDelta(item.actual_count, item.expected_count)
  const Icon = item.direction === 'spike' ? ArrowUp : ArrowDown
  const tone = signalDirectionTone(item.direction)
  const ChevronIcon = isExpanded ? ChevronDown : ChevronRight

  return (
    <button
      type="button"
      onClick={onToggle}
      className="flex min-h-(--row-h) w-full items-center justify-between gap-3 py-2 text-left transition-colors hover:bg-muted/40"
      aria-expanded={isExpanded}
    >
      <div className="flex min-w-0 flex-1 items-center gap-2">
        <ChevronIcon aria-hidden="true" className="h-3.5 w-3.5 shrink-0 text-fg-tertiary" />
        <div className="min-w-0">
          <p className="truncate text-body font-medium">
            <span className="text-fg-tertiary">{item.breakdown_column}=</span>
            <span className="font-mono">
              {item.is_other ? '(other)' : item.breakdown_value}
            </span>
          </p>
          <p className="text-body-sm text-fg-tertiary">
            actual {formatCount(item.actual_count)} · expected {formatCount(item.expected_count)}
          </p>
        </div>
      </div>
      <div className="flex items-center gap-2 whitespace-nowrap text-right text-body-sm">
        {/* The shared direction colours: a spike was painted green here, the
            opposite of every other signal surface (MON-19). */}
        {/* The z-score is a hover detail, not a column: the row already says
            the move as a count and a % (MO-2 / JR-31). */}
        <Chip
          tone={tone}
          size="xs"
          icon={<Icon aria-hidden="true" />}
          title={formatSignalEffectDetail(item)}
        >
          {delta > 0 ? '+' : ''}{formatCount(delta)}
        </Chip>
        {pct && (
          <span
            className="text-fg-tertiary"
            title={
              item.expected_count > 0
                ? undefined
                : 'No baseline to compare against for this breakdown value'
            }
          >
            {pct}
          </span>
        )}
      </div>
    </button>
  )
}

/**
 * The breakdown timeline as chart points, with the bucket this panel is about
 * flagged: the timeline endpoint carries counts only, and without the flag the
 * drilldown could not show where the anomaly falls (MON-20). The expected value
 * rides along on that point so the tooltip can say what was expected; no stddev,
 * so no band is drawn around a single point.
 */
function toChartPoints(
  points: readonly BreakdownTimelinePoint[],
  bucket: string,
  item: TopMoverItem,
): EventMetricPoint[] {
  const anomalyTime = Date.parse(bucket)
  return points.map(point => {
    const isAnomaly = Date.parse(point.bucket) === anomalyTime
    return {
      bucket: point.bucket,
      count: point.count,
      expected_count: isAnomaly ? item.expected_count : null,
      stddev: null,
      is_anomaly: isAnomaly,
      anomaly_direction: isAnomaly ? item.direction : null,
      z_score: isAnomaly ? item.z_score : null,
    }
  })
}

function BreakdownDrilldown({
  slug,
  scanConfigId,
  scopeType,
  scopeRef,
  bucket,
  item,
  rangeDays,
  timeRange,
}: {
  slug: string
  scanConfigId: string
  scopeType: string
  scopeRef: string
  /** The anomaly bucket the panel was opened for. */
  bucket: string
  item: TopMoverItem
  rangeDays?: number
  timeRange?: { from: string; to: string }
}) {
  const breakdownColumn = item.breakdown_column
  const breakdownValue = item.breakdown_value
  const isOther = item.is_other
  const { data, isLoading, isError, error, refetch } = useQuery({
    meta: SILENT_ERROR_META,
    // The range length, not the live bounds: those step every five minutes,
    // and a key that moved with them refetched the timeline each time (MON-3).
    queryKey: breakdownTimelineKey(
      slug,
      scanConfigId,
      scopeType,
      scopeRef,
      breakdownColumn,
      breakdownValue,
      isOther,
      rangeDays,
    ),
    queryFn: () =>
      eventMetricsApi.getBreakdownTimeline(slug, scanConfigId, {
        scope_type: scopeType,
        scope_ref: scopeRef,
        breakdown_column: breakdownColumn,
        breakdown_value: breakdownValue,
        is_other: isOther,
        from: timeRange?.from,
        to: timeRange?.to,
      }),
    // Keep the timeline on screen while a new range loads.
    placeholderData: keepPreviousData,
  })

  const points = useMemo(
    () => toChartPoints(data?.data ?? [], bucket, item),
    [data?.data, bucket, item],
  )
  // A marker line at the anomaly bucket, drawn through the chart's annotation
  // layer so it snaps to the categorical axis like any other marker.
  const marker = useMemo<ChartAnnotation[]>(
    () => [
      {
        id: `top-mover-anomaly-${bucket}`,
        project_id: '',
        scope_type: null,
        scope_ref: null,
        bucket,
        label: 'This anomaly',
        description: null,
        color: signalDirectionColor(item.direction),
        source: 'manual',
        url: null,
        created_by_user_id: null,
        created_at: bucket,
      },
    ],
    [bucket, item.direction],
  )
  const valueLabel = `${breakdownColumn}=${isOther ? '(other)' : breakdownValue}`

  if (isLoading) {
    return (
      <div className="px-2 pb-3 pt-1 text-body-sm text-fg-tertiary" data-testid="breakdown-drilldown">
        Loading timeline…
      </div>
    )
  }

  // Said inline: a failed request used to read "No timeline data" (MON-20).
  if (isError && !data) {
    return (
      <div className="px-2 pb-3 pt-1" data-testid="breakdown-drilldown">
        <ErrorState
          title="Timeline unavailable"
          error={error}
          onRetry={() => {
            void refetch()
          }}
          retryLabel="Retry"
          compact
        />
      </div>
    )
  }

  if (points.length === 0) {
    return (
      <div className="px-2 pb-3 pt-1 text-body-sm text-fg-tertiary" data-testid="breakdown-drilldown">
        No timeline data for this breakdown value yet.
      </div>
    )
  }

  // The shared chart, not a bare area: it brings axes, a tooltip, local
  // sub-day ticks, the anomaly dot and a size-gated container (MON-20). The
  // noun is a pair so a one-event bucket reads "1 event (…)" (DS-26).
  return (
    <div className="px-2 pb-3 pt-1" data-testid="breakdown-drilldown">
      <MetricsChart
        data={points}
        annotations={marker}
        height={140}
        granularity={granularityForInterval(data?.interval) ?? 'hour'}
        seriesLabel={{ singular: `event (${valueLabel})`, plural: `events (${valueLabel})` }}
        // No `color`: a volume series takes the chart's single-series default
        // (DS-27), as on the Volume tab.
      />
    </div>
  )
}
