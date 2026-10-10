import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  ArrowDown, ArrowUp, BellRing, CalendarPlus, CheckCheck, Copy, Link2, MessageSquare, MoreHorizontal,
  Pencil, TrendingUp,
} from 'lucide-react'
import { toast } from 'sonner'
import { Chip } from '@/components/primitives/chip'
import { CountBadge } from '@/components/primitives/count-badge'
import { Dot } from '@/components/primitives/dot'
import { MiniStat, MiniStatStrip, type MiniStatTone } from '@/components/primitives/mini-stat'
import { PageHeader } from '@/components/primitives/page-header'
import { StatValueSkeleton } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { MetricsChart } from '@/components/ui/chart-lazy'
import { WatchButton } from '@/components/watch-button'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { formatIncidentCount } from '@/lib/alertStatus'
import { formatRelativeTime, formatTimestamp } from '@/lib/datetime'
import { eventNameLabel } from '@/lib/eventName'
import { EVENT_STATUS_LABELS, EVENT_STATUS_TONE, type EventStatus } from '@/lib/eventStatus'
import { granularityForInterval } from '@/lib/metricAdapters'
import { formatSignalEffectDetail } from '@/lib/monitoring'
import { NO_BASELINE_LABEL, formatRatioDelta, ratioDelta } from '@/lib/percentDelta'
import { signalDirectionTone, type SignalDirectionTone } from '@/lib/statusLexicon'
import type { Event as TEvent, EventMetricPoint, EventMetricsResponse, EventType, MonitoringSignal } from '@/types'
import { computeEventStats, type EventDetailStats } from './eventStats'
import { SURFACE_STYLE } from './surface'
import { formatNumber } from '@/lib/format'

export function EventDetailHero({
  event,
  eventType,
  metrics,
  onEdit,
  onOpenCharts,
  onAnnotate,
  discussionCount,
  onDiscuss,
  onMarkVerified,
  alertsPath,
  slug,
}: {
  event: TEvent
  eventType: EventType | undefined
  /** Undefined while the series loads: the KPI strip then waits, not "—". */
  metrics: EventMetricsResponse | undefined
  /** Omitted for a viewer, who gets no Edit action. */
  onEdit?: () => void
  /** Brings the page's chart tabs into view, on the Volume chart. */
  onOpenCharts: () => void
  /** Opens the annotation form at a bucket; omitted for a viewer. */
  onAnnotate?: (bucket: string) => void
  /** Comments on the event's discussion; undefined while they load. */
  discussionCount?: number
  /** Scrolls to the discussion below the tabs. */
  onDiscuss?: () => void
  /** Sets the verified flag on this one event; editors only, and only while unset. */
  onMarkVerified?: () => void
  /** The alert inbox, where the signal's incident carries its triage actions. */
  alertsPath?: string
  /** The project, for the Watch button (#259); omitted, there is none. */
  slug?: string
}) {
  const stats = computeEventStats(metrics?.data)
  const signal = metrics?.latest_signal ?? null
  const signalTone = signalDirectionTone(signal?.direction ?? 'spike')
  return (
    <div className="space-y-[18px]">
      <EventDetailHeader
        event={event}
        eventType={eventType}
        signal={signal}
        onEdit={onEdit}
        onOpenCharts={onOpenCharts}
        discussionCount={discussionCount}
        onDiscuss={onDiscuss}
        onMarkVerified={onMarkVerified}
        slug={slug}
        stats={<EventStatStrip event={event} stats={stats} pending={metrics === undefined} />}
      />
      {signal && (
        <EventSignalBanner
          signal={signal}
          tone={signalTone}
          onAnnotate={onAnnotate ? () => onAnnotate(signal.bucket) : undefined}
          onDiscuss={onDiscuss}
          alertsPath={alertsPath}
        />
      )}
      {signal && (
        <EventSignalMiniChart
          data={metrics?.data ?? []}
          interval={metrics?.interval ?? null}
          sigmaThreshold={metrics?.sigma_threshold}
          signal={signal}
          color={eventType?.color || undefined}
          onOpenFullChart={onOpenCharts}
        />
      )}
    </div>
  )
}

/**
 * The shared page header: one h1 in the page title's sans, since the
 * name is a display name and not code; the nav group and collection
 * as the eyebrow; the KPI strip in its `stats` slot. The in-page
 * "Plan / Events / <name>" breadcrumb that sat above it repeated the top bar's
 * trail 120px apart, so the top bar is the one trail now.
 */
function EventDetailHeader({
  event,
  eventType,
  signal,
  onEdit,
  onOpenCharts,
  discussionCount,
  onDiscuss,
  onMarkVerified,
  slug,
  stats,
}: {
  event: TEvent
  eventType: EventType | undefined
  signal: MonitoringSignal | null
  onEdit?: () => void
  onOpenCharts: () => void
  discussionCount?: number
  onDiscuss?: () => void
  onMarkVerified?: () => void
  slug?: string
  stats: ReactNode
}) {
  const status = event.status as EventStatus
  const statusTone = EVENT_STATUS_TONE[status] ?? 'neutral'
  const typeColor = eventType?.color ?? 'var(--fg-faint)'
  const typeLabel = eventType?.display_name ?? event.event_type?.display_name ?? 'Event'
  return (
    <PageHeader
      eyebrow="Plan · Event"
      // Never an empty top-level heading: a blank name gave the whole page no
      // accessible title. The status dot is decoration.
      title={
        <>
          <span className="mr-2.5 inline-flex align-middle">
            {signal
              ? <Dot tone={signalDirectionTone(signal.direction)} pulse size={8} />
              : <Dot tone={statusTone} size={8} />}
          </span>
          {eventNameLabel(event.name)}
        </>
      }
      titleAddon={
        <>
          <Chip tone={statusTone} size="sm">{EVENT_STATUS_LABELS[status] ?? event.status}</Chip>
          {/* Tags are kind tags: outline, not a status fill. */}
          {event.tags.map(tag => <Chip key={tag.id} variant="outline" size="xs">{tag.name}</Chip>)}
          {/* The thread sits below the tabs, past the fold: the count says
              there is one and the chip jumps to it. */}
          {onDiscuss && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 gap-1.5 px-2 text-caption text-fg-muted"
              onClick={onDiscuss}
              aria-label={discussionCount === undefined ? 'Discussion' : `Discussion (${discussionCount})`}
            >
              <MessageSquare aria-hidden="true" />
              Discussion
              {discussionCount !== undefined && <CountBadge count={discussionCount} max={99} />}
            </Button>
          )}
        </>
      }
      description={
        <>
          <span className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-[5px]">
              <span className="h-[7px] w-[7px] rounded-sm" style={{ background: typeColor }} />
              {typeLabel}
            </span>
            <span className="text-fg-tertiary">·</span>
            <span>updated {formatRelativeTime(event.updated_at)}</span>
          </span>
          {event.description && (
            <span className="mt-[7px] block max-w-[62ch] text-body leading-snug text-fg-secondary">
              {event.description}
            </span>
          )}
        </>
      }
      // The Button primitive, so the primary Edit gets the shared hover, focus
      // ring and disabled look instead of an inline accent fill.
      actions={
        <>
          {/* One watch per event (#259): a branch copy watches its twin on
              main when the payload names it, as the discussion is shared. */}
          {slug && <WatchButton slug={slug} entityType="event" entityId={event.main_event_id ?? event.id} />}
          {/* Named for what it opens, the chart tabs below: "Metrics" is the
              catalog under Observe, which this page lists under "Used by". */}
          <Button variant="outline" onClick={onOpenCharts}>
            <TrendingUp aria-hidden="true" />
            Charts
          </Button>
          {onEdit && (
            <Button onClick={onEdit}>
              <Pencil aria-hidden="true" />
              Edit
            </Button>
          )}
          <EventActionOverflow eventName={event.name} onMarkVerified={onMarkVerified} />
        </>
      }
      stats={stats}
    />
  )
}

async function copyToClipboard(text: string, success: string) {
  try {
    await navigator.clipboard.writeText(text)
    toast.success(success)
  } catch {
    toast.error('Could not copy to the clipboard')
  }
}

/**
 * Overflow ("…") menu with actions that work. It used to hold only disabled
 * "Coming soon" items, the first place a reader looked for more and found
 * nothing.
 */
function EventActionOverflow({
  eventName,
  onMarkVerified,
}: {
  eventName: string
  onMarkVerified?: () => void
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="icon" aria-label="More actions" className="text-fg-muted">
          <MoreHorizontal aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" sideOffset={6} className="w-[180px]">
        <DropdownMenuItem
          className="text-body-sm"
          onSelect={() => void copyToClipboard(window.location.href, 'Link copied')}
        >
          <Link2 className="h-3.5 w-3.5 shrink-0 text-fg-tertiary" /> Copy link
        </DropdownMenuItem>
        <DropdownMenuItem
          className="text-body-sm"
          onSelect={() => void copyToClipboard(eventName, 'Event name copied')}
        >
          <Copy className="h-3.5 w-3.5 shrink-0 text-fg-tertiary" /> Copy event name
        </DropdownMenuItem>
        {/* The Events list's bulk "Mark as verified", for this one event. */}
        {onMarkVerified && (
          <DropdownMenuItem className="text-body-sm" onSelect={onMarkVerified}>
            <CheckCheck className="h-3.5 w-3.5 shrink-0 text-fg-tertiary" /> Mark as verified
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function EventSignalBanner({
  signal,
  tone,
  onAnnotate,
  onDiscuss,
  alertsPath,
}: {
  signal: MonitoringSignal
  tone: SignalDirectionTone
  /** "Annotate" records the verdict on the flagged bucket; editors only. */
  onAnnotate?: () => void
  /** "Discuss" jumps to the event's thread, to ask whoever owns it. */
  onDiscuss?: () => void
  /** The alert inbox, where the incident's Ack / Mute live. */
  alertsPath?: string
}) {
  // No baseline is a fact about the signal, not a missing value: dropping the
  // clause left the banner silently shorter on exactly the anomalies that moved
  // the most — an event firing where nothing was expected, a scope resuming
  // after an outage — so it says so instead.
  const delta = ratioDelta(signal.actual_count, signal.expected_count)
  const Arrow = signal.direction === 'drop' ? ArrowDown : ArrowUp
  return (
    <div
      className="flex flex-wrap items-center gap-[10px] rounded-card px-[14px] py-[10px]"
      style={{
        background: `var(--${tone}-soft)`,
        border: `1px solid color-mix(in oklab, var(--${tone}) 35%, var(--border))`,
      }}
    >
      <Arrow size={16} style={{ color: `var(--${tone})` }} />
      {/* The % change is the sentence; the z-score moved to the tooltip, where
          it no longer asks a PM to read statistics. A drop that
          bottomed out says so: "−100%" is right but reads as a rounding. */}
      <span
        className="text-body-sm text-fg-secondary"
        title={formatSignalEffectDetail(signal)}
      >
        {signal.direction === 'drop' ? 'Volume drop' : 'Volume spike'} detected
        {signal.direction === 'drop' && signal.actual_count === 0
          ? ' — dropped to zero.'
          : delta === null
            ? ` — ${NO_BASELINE_LABEL} to compare against.`
            : ` — ${formatRatioDelta(delta)} vs. baseline.`}
      </span>
      <div className="flex-1" />
      <span className="text-caption text-fg-tertiary">
        {formatTimestamp(signal.bucket)}
      </span>
      {/* A signal is not a dead end: record a verdict on the
          bucket, ask about it, or go to the incident's triage actions. */}
      {onAnnotate && (
        <Button variant="outline" size="sm" onClick={onAnnotate}>
          <CalendarPlus aria-hidden="true" />
          Annotate
        </Button>
      )}
      {onDiscuss && (
        <Button variant="outline" size="sm" onClick={onDiscuss}>
          <MessageSquare aria-hidden="true" />
          Discuss
        </Button>
      )}
      {alertsPath && (
        <Button variant="outline" size="sm" asChild>
          <Link to={alertsPath}>
            <BellRing aria-hidden="true" />
            View alerts
          </Link>
        </Button>
      )}
    </div>
  )
}

/**
 * Compact volume chart rendered beside {@link EventSignalBanner} so the anomaly
 * the banner describes is visible in context, without the extra click into the
 * Volume tab. Reuses the already-fetched series and the same
 * {@link MetricsChart}; native granularity keeps the flagged point
 * un-aggregated, so its anomaly dot never merges into a neighbouring bucket.
 *
 * It is titled "Volume", not "Volume vs. baseline". MetricsChart does draw a
 * dashed expectation and a sigma band from each point's expected_count/stddev,
 * but the backend fills those two fields ONLY on buckets it flagged
 * (metrics_service._build_metric_points): the expected series has a single
 * non-null point, and a `connectNulls={false} dot={false}` Line through one
 * point paints nothing. So the panel promised the comparison that justifies the
 * alert and then showed one bare series — the reader had to take "+198% vs.
 * baseline" on faith from the chart they were handed to check it with. The one
 * baseline that does exist is the flagged bucket's, so it is named in words
 * beside the title instead of implied by a line that is not there.
 */
function EventSignalMiniChart({
  data,
  interval,
  sigmaThreshold,
  signal,
  color,
  onOpenFullChart,
}: {
  data: EventMetricPoint[]
  interval: string | null
  /** `EventMetricsResponse.sigma_threshold`, threaded from the hero's already-fetched series. */
  sigmaThreshold: number | undefined
  signal: MonitoringSignal
  /** The event type's colour, the same one the Volume tab draws. */
  color: string | undefined
  /** The Volume tab's full chart, with its range controls. */
  onOpenFullChart: () => void
}) {
  if (data.length === 0) return null
  const granularity = granularityForInterval(interval) ?? 'hour'
  return (
    <div
      data-testid="signal-volume-chart"
      className="rounded-card border px-[14px] pb-[6px] pt-[10px]"
      style={SURFACE_STYLE}
    >
      <div className="mb-[6px] flex flex-wrap items-baseline gap-x-3 gap-y-1 text-caption">
        <span className="font-medium text-fg-tertiary">Volume</span>
        {/* Same `expected > 0` gate the banner uses, so the two cannot disagree
            about whether this signal had a baseline at all — and the SAME
            value-aware formatter the signal card 1200 lines up already uses, so
            they cannot disagree about what it was. `expected_count` is a mean of
            prior buckets, so a rare event's baseline is legitimately sub-unit
            (0.4/hour); `Math.round` wrote that as "baseline 0", contradicting
            the gate that had just decided a baseline existed. */}
        <span className="ml-auto text-fg-tertiary">
          {signal.expected_count > 0
            ? `baseline ${formatIncidentCount(signal.expected_count)} at the flagged bucket`
            : `${NO_BASELINE_LABEL} at the flagged bucket`}
        </span>
        {/* The same series as the Volume tab: say so and link to it, so the
            two do not read as different data. */}
        <Button variant="link" size="sm" className="h-auto p-0 text-caption" onClick={onOpenFullChart}>
          Open full chart
        </Button>
      </div>
      <MetricsChart
        data={data}
        height={104}
        // The event type's colour, as on the Volume tab below, so the two
        // read as one series. The anomaly is marked by its danger mark, not by
        // painting the whole line red.
        color={color}
        granularity={granularity}
        seriesLabel="events"
        sigmaThreshold={sigmaThreshold}
      />
    </div>
  )
}

/**
 * One stat of the header's KPI strip. The hover hint explains an empty value;
 * `empty` greys a no-data figure ("—") instead of printing it in full ink.
 */
function EventStat({
  label,
  value,
  tone,
  hint,
  empty,
}: {
  label: string
  value: ReactNode
  tone?: MiniStatTone
  /** Hover/long-press explanation, e.g. for an empty "—" value. */
  hint?: string
  /** De-emphasise the value when it represents a no-data ("—") state. */
  empty?: boolean
}) {
  return (
    <div title={hint}>
      <MiniStat
        label={label}
        value={empty ? <span className="text-fg-tertiary">{value}</span> : value}
        valueTone={tone}
      />
    </div>
  )
}

/**
 * The page-KPI strip every page uses, in place of four bordered tiles
 * with 19px mono figures: sans tabular figures, so "2h ago" no longer
 * reads as code.
 */
function EventStatStrip({
  event,
  stats,
  pending,
}: {
  event: TEvent
  stats: EventDetailStats
  /** The series has not arrived: no "—" / "No events" claim yet. */
  pending: boolean
}) {
  const deltaTone: MiniStatTone | undefined = stats.delta24h == null
    ? undefined
    : stats.delta24h > 20 ? 'danger' : stats.delta24h < -20 ? 'warning' : undefined
  const deltaText = stats.delta24h == null
    ? '—'
    : `${stats.delta24h > 0 ? '+' : ''}${stats.delta24h.toFixed(0)}%`
  return (
    <MiniStatStrip boxed>
      <EventStat
        label="Volume · 24h"
        value={pending
          ? <StatValueSkeleton />
          : stats.volume24h == null ? '—' : formatNumber(stats.volume24h)}
        empty={!pending && stats.volume24h == null}
        hint={!pending && stats.volume24h == null ? 'No events in the last 24h' : undefined}
      />
      <EventStat
        label="Change vs prior 24h"
        // The Events list's figure, so the two surfaces cannot disagree about
        // the same event. A window short of 24h says so in words
        // under the figure, not with a "*" explained only on hover.
        value={pending
          ? <StatValueSkeleton />
          : stats.partial && stats.delta24h != null
            ? (
              <span className="inline-flex flex-col">
                <span>{deltaText}</span>
                <span className="text-caption font-normal text-fg-tertiary">
                  partial window
                </span>
              </span>
            )
            : deltaText}
        tone={pending ? undefined : deltaTone}
        empty={!pending && stats.delta24h == null}
        hint={pending ? undefined : stats.deltaHint}
      />
      <EventStat
        label="Schema drifts"
        value={formatNumber(event.drift_count)}
        tone={event.drift_count > 0 ? 'warning' : undefined}
        // Zero drifts is a real, reassuring count — render "0", not the
        // no-data glyph the empty state would otherwise show.
        hint={event.drift_count === 0 ? 'No schema drifts detected' : undefined}
      />
      <EventStat
        label="Last seen"
        value={event.last_seen_at ? formatRelativeTime(event.last_seen_at) : '—'}
        empty={!event.last_seen_at}
        hint={event.last_seen_at ? undefined : 'No hits recorded yet'}
      />
    </MiniStatStrip>
  )
}

/**
 * Hero-shaped placeholder while the event loads. Without it the page painted
 * the generic header ("Event", "Back to events") full width first and then
 * jumped to the hero inside the narrower column.
 */
export function EventDetailSkeleton() {
  return (
    <div role="status" aria-label="Loading event" className="space-y-[18px]">
      <Skeleton className="h-4 w-48" />
      <div className="space-y-2">
        <Skeleton className="h-6 w-64 max-w-full" />
        <Skeleton className="h-4 w-40" />
      </div>
      <Skeleton className="h-[58px]" />
      <Skeleton className="h-[240px]" />
    </div>
  )
}
