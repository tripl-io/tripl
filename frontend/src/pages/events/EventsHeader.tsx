import { formatNumber } from '@/lib/format'
import { Info } from 'lucide-react'
import { Link } from 'react-router-dom'

import { MiniStat, MiniStatStrip } from '@/components/primitives/mini-stat'
import { PageHeader } from '@/components/primitives/page-header'
import { StatValueSkeleton } from '@/components/states'
import { SEGMENTED_TRACK, segmentedItemVariants } from '@/components/ui/segmented-variants'
import { cn } from '@/lib/utils'
import { ScenarioCoachMark } from '@/demo/ScenarioCoachMark'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import type { EventType, MonitoringSignal } from '@/types'

import { EventDriftBadge } from './EventDriftBadge'
import { EVENT_VIEWS, eventsPageTitle } from './eventsViews'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

/** One event type with open schema drift, as the header shows it. */
export type EventTypeDrift = {
  eventTypeId: string
  label: string
  count: number
  /** The coached demo step points at this badge (reconcile/review-drift). */
  coach?: boolean
}

// One sentence each. The long form lived here as a five-line paragraph
// about "incident rollup" and "Significant threshold". The point that matters:
// this count covers the charted series (project total + event types), so it can
// differ from the sidebar Anomalies badge in either direction.
const OPEN_SIGNALS_HELP =
  'Open anomalies on the volume the chart shows — the project total and each event type.'

// The one stat in this row that does NOT follow the tab, filters or search: it
// is a separate project-wide query (useEventsPageData `inReviewCount`), while
// "Events" beside it is the filtered list count. Unlabelled, the row read as one
// sentence — the archived tab showed "TOTAL 1 · IN REVIEW 6 pending" over a
// single archived row — so the delta names the wider scope.
const IN_REVIEW_HELP =
  'Events with status In review across the whole project; it ignores the tab, filters and search.'

/**
 * The `(i)` affordance beside a stat whose scope is not self-evident. A Radix
 * tooltip rather than a bare `title`, so the note opens on keyboard focus as
 * well as hover; the icon stays aria-hidden because the trigger's label already
 * carries the sentence. The provider is local because the header renders
 * outside EventsTable's, and Radix throws without one in scope.
 */
function StatHelp({ help }: { help: string }) {
  return (
    <TooltipProvider delayDuration={0}>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className="inline-flex shrink-0 rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
            aria-label={help}
          >
            <Info className="size-3 text-fg-tertiary" aria-hidden />
          </button>
        </TooltipTrigger>
        <TooltipContent side="bottom" align="end" className="whitespace-normal">
          {help}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}

/**
 * Open schema drift, one badge per event type. The page header shows
 * it; the embedded table (an event type's detail view), which has no header,
 * shows it above its toolbar.
 */
export function EventTypeDriftBadges({
  slug,
  typeDrifts,
  namesType,
}: {
  slug: string
  typeDrifts: EventTypeDrift[]
  /** The page already names the one type shown, so the badge need not. */
  namesType: boolean
}) {
  if (typeDrifts.length === 0) return null
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5 self-center">
      {typeDrifts.map(drift => (
        <ScenarioCoachMark key={drift.eventTypeId} step="reconcile/review-drift" when={!!drift.coach}>
          {/* A span, not the badge itself: the badge's own root is a
              Radix PopoverTrigger slot, which must keep its ref. */}
          <span className="inline-flex">
            <EventDriftBadge
              slug={slug}
              eventTypeId={drift.eventTypeId}
              count={drift.count}
              typeLabel={namesType ? undefined : drift.label}
              coached={!!drift.coach}
            />
          </span>
        </ScenarioCoachMark>
      ))}
    </span>
  )
}

/**
 * All · Review queue (n) · Archived, as links in the segmented look: each is a
 * route of its own, so they stay real anchors (open in a new tab, copy link).
 * On a type tab the type is the current view and none of the three is.
 */
function EventViewTabs({
  slug,
  activeTab,
  activeType,
  inReviewCount,
}: {
  slug: string
  activeTab: string
  activeType: EventType | null
  inReviewCount: number | undefined
}) {
  const item = cn(
    segmentedItemVariants({ size: 'sm' }),
    'aria-[current=page]:bg-surface aria-[current=page]:text-fg aria-[current=page]:shadow-sm',
  )
  return (
    <nav aria-label="Event views" className={SEGMENTED_TRACK}>
      {EVENT_VIEWS.map(view => (
        <Link
          key={view.tab}
          to={view.tab === 'all' ? projectPath(currentOrgSlug(), slug, '/events') : projectPath(currentOrgSlug(), slug, `/events/${view.tab}`)}
          aria-current={!activeType && activeTab === view.tab ? 'page' : undefined}
          className={item}
        >
          {view.label}
          {view.tab === 'review' && inReviewCount !== undefined && inReviewCount > 0 && (
            <span className="tnum text-fg-tertiary">{formatNumber(inReviewCount)}</span>
          )}
        </Link>
      ))}
      {activeType && (
        <span aria-current="page" className={item}>
          {activeType.display_name}
        </span>
      )}
    </nav>
  )
}

export function EventsHeader({
  total,
  totalPending = false,
  columnFilter = null,
  inReviewCount,
  inReviewPending = false,
  projectTotalSignal,
  eventTypeSignals,
  signalsPending = false,
  activeType = null,
  activeTab = 'all',
  slug,
  typeDrifts = [],
  hideStats = false,
}: {
  /**
   * Events the tab, search and server-side filters match (the server count);
   * formatted like the table footer.
   */
  total: number
  /** The list query has not settled: `total` is a placeholder 0, not a count. */
  totalPending?: boolean
  /**
   * Set while a column (field/meta) filter narrows the table. Those filters are
   * client-side, so `total` does not count their matches; the header then says
   * what the footer does — the matches among the rows checked so far.
   */
  columnFilter?: { matching: number; checked: number } | null
  /**
   * Events whose STATUS is `in_review` — not the count of unreviewed events.
   * The two are independent axes (an event can be marked reviewed and still be
   * in_review), and the old `unreviewedCount` name claimed otherwise while the
   * "Mark reviewed" button next to it moved neither this number nor the queue.
   */
  inReviewCount: number
  inReviewPending?: boolean
  projectTotalSignal: MonitoringSignal | null
  eventTypeSignals: Map<string, MonitoringSignal>
  /** The signals query has not settled: "0 · none" would be a false all-clear. */
  signalsPending?: boolean
  // When a type tab is active (e.g. /events/pv) the heading reflects it
  // ("Page View events") instead of the generic "Events".
  activeType?: EventType | null
  /** Route tab: 'all', 'review', 'archived' or a type name. */
  activeTab?: string
  slug?: string
  /**
   * Open schema drift, once per event type. Drift belongs to the type, and the
   * backend copies the type's count onto every event of it, so a badge per row
   * repeated the same number down hundreds of rows and read as a per-event
   * count.
   */
  typeDrifts?: EventTypeDrift[]
  /** A project with no events: three zeroes teach nothing. */
  hideStats?: boolean
}) {
  const openSignalCount = eventTypeSignals.size + (projectTotalSignal ? 1 : 0)
  const hasOpenSignal = openSignalCount > 0
  const inReviewValue = inReviewPending ? (
    <StatValueSkeleton />
  ) : slug ? (
    // The queue this number counts is one click away.
    <Link
      to={projectPath(currentOrgSlug(), slug, '/events/review')}
      className="underline-offset-4 hover:underline"
    >
      {formatNumber(inReviewCount)}
    </Link>
  ) : (
    formatNumber(inReviewCount)
  )

  return (
    <PageHeader
      className="mb-3"
      eyebrow="Plan"
      title={eventsPageTitle(activeTab, activeType)}
      titleAddon={
        slug ? (
          <EventTypeDriftBadges slug={slug} typeDrifts={typeDrifts} namesType={!!activeType} />
        ) : undefined
      }
      // The page KPIs sit under the title in the one boxed strip, as on
      // Overview, Metrics and Anomalies, instead of right-aligned in the
      // actions slot. The strip wraps on a phone-width viewport.
      stats={
        hideStats ? undefined : (
          <div className="flex flex-col gap-3">
            {slug && (
              <EventViewTabs
                slug={slug}
                activeTab={activeTab}
                activeType={activeType}
                inReviewCount={inReviewPending ? undefined : inReviewCount}
              />
            )}
            {/* Pending values are a skeleton with no delta or tone: "0 · none"
                before the queries settle was a false all-clear. */}
            <MiniStatStrip boxed>
              {/* The one place the count appears in the header: the heading used to
                  repeat it beside the h1, unformatted, while the footer formatted
                  the same number. */}
              {columnFilter ? (
                <MiniStat
                  label="Matching"
                  value={formatNumber(columnFilter.matching)}
                  delta={`${formatNumber(columnFilter.checked)} of ${formatNumber(total)} checked`}
                />
              ) : (
                <MiniStat
                  label="Events"
                  value={totalPending ? <StatValueSkeleton /> : formatNumber(total)}
                />
              )}
              {/* The help icon rides on the caption it explains: beside the whole
                  stat it sat far from the label, next to the following stat.
                  "Open"/"none", not "live"/"quiet": "Live" is the
                  lifecycle status of a shipped event, in green, one column over.
                  */}
              {signalsPending ? (
                <MiniStat
                  label="Open signals"
                  value={<StatValueSkeleton />}
                  labelAddon={<StatHelp help={OPEN_SIGNALS_HELP} />}
                />
              ) : (
                <MiniStat
                  label="Open signals"
                  value={String(openSignalCount)}
                  delta={hasOpenSignal ? 'open' : 'none'}
                  tone={hasOpenSignal ? 'danger' : 'success'}
                  pulse={hasOpenSignal}
                  labelAddon={<StatHelp help={OPEN_SIGNALS_HELP} />}
                />
              )}
              {/* "In review", the one name for this count app-wide. */}
              <MiniStat
                label="In review"
                value={inReviewValue}
                delta={inReviewPending ? undefined : 'project-wide'}
                tone="neutral"
                valueTone={!inReviewPending && inReviewCount > 0 ? 'warning' : undefined}
                labelAddon={<StatHelp help={IN_REVIEW_HELP} />}
              />
            </MiniStatStrip>
          </div>
        )
      }
    />
  )
}
