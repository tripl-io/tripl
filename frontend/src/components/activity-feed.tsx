import {
  AlertTriangle,
  Archive,
  Bell,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleDot,
  Eye,
  Pencil,
  Plus,
  TrendingUp,
  type LucideIcon,
} from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useNow } from '@/hooks/useNow'
import { formatRelativeTime } from '@/lib/datetime'
import { resolveActivityTargetPath } from '@/lib/navigation'
import { friendlyScanError } from '@/lib/scanError'
import type { ActivityItem, ActivityItemSeverity, ActivityItemType } from '@/types'

/**
 * The activity feed's rows, shared by the activity rail and the Overview's
 * "Recent activity" card.
 *
 * The card used to keep its own copy of the row, and the two drifted: the copy
 * put one check mark on every event kind (#238), printed a scan-generated
 * event by its raw `key=value` signature, sent an alert row to the bare
 * Alerting page instead of its delivery, and never collapsed a scan's burst —
 * while the rail printed a failed scan's raw error that the card had hidden.
 * The card steps aside while the rail sits beside it, so the same feed looked
 * different depending on whether the rail was open.
 */

/**
 * `rail`: the narrow activity rail — text wraps, the time sits under it and a
 * severity edge marks the left side.
 * `panel`: a row in a page card — one line each for title and detail, cut
 * with the full text on hover, and the time at the right.
 */
export type ActivityFeedVariant = 'rail' | 'panel'

/** Names a row's project on the workspace feed, where rows mix projects. */
export type ProjectNamer = (projectSlug: string) => string

// One scan implements every discovered event in a single pass, so each of those
// items shares the scan's completion timestamp and lands as a burst of
// near-identical rows. Collapse a run of same-type items that arrived within
// this window into one expandable summary instead of flooding the feed.
const BURST_WINDOW_MS = 2 * 60_000
// Runs smaller than this read fine expanded; only collapse a genuine flood.
const MIN_BURST = 3
// How many item names to preview on a collapsed summary row before "+N more".
const PREVIEW_NAMES = 3

const KIND_ICON: Record<ActivityItemType, LucideIcon> = {
  anomaly: AlertTriangle,
  scan: TrendingUp,
  alert: Bell,
  event: CircleDot,
}

type RowIcon = { icon: LucideIcon; tone?: string }

// Event rows are told apart by what happened to the event. One check mark for
// every kind made "Event needs review" and "Event archived" read as done
// (#238). Matched on the action in the title stem ("Event archived").
const EVENT_ACTION_ICON: ReadonlyArray<[RegExp, RowIcon]> = [
  [/implemented/i, { icon: CheckCircle2, tone: 'var(--success)' }],
  [/review/i, { icon: Eye, tone: 'var(--warning)' }],
  [/archived/i, { icon: Archive }],
  [/updated|changed|edited/i, { icon: Pencil }],
  [/added|created|new/i, { icon: Plus }],
]

function rowIcon(item: ActivityItem): RowIcon {
  if (item.type === 'event') {
    const stem = titleStem(item.title)
    const match = EVENT_ACTION_ICON.find(([pattern]) => pattern.test(stem))
    if (match) return match[1]
  }
  return { icon: KIND_ICON[item.type] }
}

// The noun a collapsed burst counts. A `scan` item is one scan RUN, not one
// scan, so three completed runs of one nightly scan must read "3 runs
// completed" — "3 scans completed" claimed the project had three scans.
// The title stem already carries the scan noun ("Scan completed").
const TYPE_PLURAL: Record<ActivityItemType, string> = {
  anomaly: 'anomalies',
  scan: 'runs',
  alert: 'alerts',
  event: 'events',
}

const SEVERITY_RANK: Record<ActivityItemSeverity, number> = {
  high: 2,
  medium: 1,
  low: 0,
}

function severityColor(sev: ActivityItemSeverity): string {
  switch (sev) {
    case 'high':
      return 'var(--danger)'
    case 'medium':
      return 'var(--warning)'
    default:
      return 'var(--fg-muted)'
  }
}

/** The rail's left edge: coloured for a high or medium item only. */
function severityEdge(sev: ActivityItemSeverity): string {
  return `2px solid ${sev === 'low' ? 'transparent' : severityColor(sev)}`
}

// Backend copy is "<Noun> <action>: <name>" (e.g. "Event implemented: Signup").
// The stem before ": " identifies what happened; the suffix is the item name.
function titleStem(title: string): string {
  const sep = title.indexOf(': ')
  return sep === -1 ? title : title.slice(0, sep)
}

function itemName(item: ActivityItem): string {
  const sep = item.title.indexOf(': ')
  return displayName(sep === -1 ? item.title : item.title.slice(sep + 2))
}

/**
 * A scan-generated event is named by its key/value signature
 * ("event_name=Home Screen View | screen_name=Home"). A preview reads the
 * values, "Home Screen View · Home", not the raw keys (#238).
 */
function displayName(name: string): string {
  const parts = name.split('|').map((part) => part.trim())
  if (parts.length === 0 || !parts.every((part) => /^[\w.-]+=/.test(part))) return name
  return parts.map((part) => part.slice(part.indexOf('=') + 1).trim()).filter(Boolean).join(' · ')
}

/** The row title with a scan signature shown by its values (see displayName). */
function rowTitle(item: ActivityItem): string {
  const sep = item.title.indexOf(': ')
  if (sep === -1) return item.title
  return `${item.title.slice(0, sep)}: ${displayName(item.title.slice(sep + 2))}`
}

// A failed run's detail is the scan job's stored error. Workers write a safe
// "Scan failed: …" sentence, but older runs and genuinely raw strings carry
// host/port/ORM internals, so the row shows the friendly form (H3).
function rowDetail(item: ActivityItem): string {
  if (item.type === 'scan' && item.title.startsWith('Scan failed')) {
    return friendlyScanError(item.detail).message
  }
  return item.detail
}

function burstKey(item: ActivityItem): string {
  return `${item.type}::${titleStem(item.title)}`
}

function withinWindow(a: string, b: string): boolean {
  const ta = Date.parse(a)
  const tb = Date.parse(b)
  if (Number.isNaN(ta) || Number.isNaN(tb)) return false
  return Math.abs(ta - tb) <= BURST_WINDOW_MS
}

// A burst always holds at least one item, so its head is always present.
type Burst = [ActivityItem, ...ActivityItem[]]

type FeedEntry =
  | { kind: 'single'; item: ActivityItem }
  | { kind: 'group'; id: string; items: Burst }

// Collapse consecutive same-type items that arrived in one burst (same scan /
// tight time window) into a single expandable group; everything else stays a
// standalone row. The feed is already newest-first, so a burst is contiguous.
function buildFeed(items: readonly ActivityItem[]): FeedEntry[] {
  const entries: FeedEntry[] = []
  const flush = (run: Burst) => {
    if (run.length >= MIN_BURST) {
      entries.push({ kind: 'group', id: `group:${run[0].id}`, items: run })
    } else {
      for (const item of run) entries.push({ kind: 'single', item })
    }
  }
  let run: Burst | null = null
  let prev: ActivityItem | null = null
  for (const item of items) {
    if (
      run &&
      prev &&
      burstKey(item) === burstKey(run[0]) &&
      withinWindow(prev.occurred_at, item.occurred_at)
    ) {
      run.push(item)
    } else {
      if (run) flush(run)
      run = [item]
    }
    prev = item
  }
  if (run) flush(run)
  return entries
}

// "Event implemented" -> "implemented": drop the leading noun so the summary
// reads "12 events implemented" instead of repeating the noun.
function burstAction(stem: string): string {
  const sep = stem.indexOf(' ')
  return sep === -1 ? '' : stem.slice(sep + 1).toLowerCase()
}

// The stem is written for one item ("Event needs review"); a count of them
// takes the plural verb, or the summary read "6 events needs review".
const PLURAL_VERB: Record<string, string> = { needs: 'need', is: 'are', was: 'were', has: 'have' }

function pluralAction(action: string): string {
  const [verb, ...rest] = action.split(' ')
  const plural = verb ? PLURAL_VERB[verb] : undefined
  return plural ? [plural, ...rest].join(' ') : action
}

function groupSummary(items: Readonly<Burst>): string {
  const action = pluralAction(burstAction(titleStem(items[0].title)))
  const noun = TYPE_PLURAL[items[0].type]
  return action ? `${items.length} ${noun} ${action}` : `${items.length} ${noun}`
}

function groupPreview(items: readonly ActivityItem[]): string {
  const names = items.map(itemName)
  const shown = names.slice(0, PREVIEW_NAMES)
  const extra = names.length - shown.length
  return extra > 0 ? `${shown.join(', ')} +${extra} more` : shown.join(', ')
}

function groupSeverity(items: readonly ActivityItem[]): ActivityItemSeverity {
  return items.reduce<ActivityItemSeverity>(
    (worst, item) =>
      SEVERITY_RANK[item.severity] > SEVERITY_RANK[worst] ? item.severity : worst,
    'low',
  )
}

const RAIL_ROW_CLASS =
  'flex gap-2.5 px-3.5 py-[9px] no-underline transition-colors hover:bg-[var(--surface-hover)]'
const PANEL_ROW_CLASS =
  'flex min-h-(--row-h) items-start gap-2.5 py-2 no-underline transition-colors hover:bg-[var(--surface-hover)] text-inherit'

interface RowContext {
  now: number
  projectName?: ProjectNamer
  variant: ActivityFeedVariant
}

/**
 * The feed, newest first, with each scan's burst collapsed into one
 * expandable row. Owns the clock its relative times count from, so "just now"
 * keeps moving while nothing refetches — with the live stream up the feed
 * never polls.
 */
export function ActivityFeed({
  items,
  projectName,
  variant = 'rail',
}: {
  items: readonly ActivityItem[]
  /** Set on the workspace feed, where rows come from several projects. */
  projectName?: ProjectNamer
  variant?: ActivityFeedVariant
}) {
  const now = useNow(60_000)
  const context: RowContext = { now, projectName, variant }
  return (
    <>
      {buildFeed(items).map((entry) =>
        entry.kind === 'group' ? (
          <ActivityGroupRow key={entry.id} items={entry.items} {...context} />
        ) : (
          <ActivityRow key={entry.item.id} item={entry.item} {...context} />
        ),
      )}
    </>
  )
}

/** The kind (or event action) glyph a row leads with, in its severity colour. */
function KindIcon({ item, severity }: { item: ActivityItem; severity: ActivityItemSeverity }) {
  const { icon: Icon, tone } = rowIcon(item)
  return (
    <div
      className="mt-px flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-sm"
      style={{
        background: 'var(--surface)',
        color: severity === 'low' ? (tone ?? 'var(--fg-muted)') : severityColor(severity),
      }}
    >
      <Icon className="size-3" aria-hidden="true" />
    </div>
  )
}

/** When it happened, and on the workspace feed in which project. */
function RowWhen({
  occurredAt,
  projectSlug,
  now,
  projectName,
  variant,
}: RowContext & { occurredAt: string; projectSlug: string }) {
  const project = projectName ? (
    <span className="text-fg-tertiary">{` · ${projectName(projectSlug)}`}</span>
  ) : null
  if (variant === 'panel') {
    return (
      <span className="tnum shrink-0 text-micro text-fg-tertiary">
        {formatRelativeTime(occurredAt, now)}
        {project}
      </span>
    )
  }
  // Sans, not mono: a relative time is prose, not an identifier.
  return (
    <div className="mt-[3px] text-caption font-medium text-fg-secondary">
      {formatRelativeTime(occurredAt, now)}
      {project}
    </div>
  )
}

function ActivityRow({ item, ...context }: RowContext & { item: ActivityItem }) {
  const panel = context.variant === 'panel'
  const title = rowTitle(item)
  const detail = rowDetail(item)
  const when = <RowWhen occurredAt={item.occurred_at} projectSlug={item.project_slug} {...context} />
  const content = (
    <>
      <KindIcon item={item} severity={item.severity} />
      <div className="min-w-0 flex-1">
        <div
          className={`${panel ? 'truncate ' : ''}text-body-sm font-medium leading-[1.35]`}
          title={panel ? title : undefined}
        >
          {title}
        </div>
        <div
          className={`mt-0.5 ${panel ? 'truncate ' : ''}text-caption leading-[1.3] text-fg-tertiary`}
          title={panel ? detail : undefined}
        >
          {detail}
        </div>
        {!panel && when}
      </div>
      {panel && when}
    </>
  )
  const className = panel ? PANEL_ROW_CLASS : RAIL_ROW_CLASS
  const style = panel ? undefined : { borderLeft: severityEdge(item.severity), color: 'inherit' }

  // Not `item.target_path` directly: the feed's alert rows arrive with the bare
  // /p/:slug/alerting, which drops the reader at the top of a page
  // holding every delivery and every incident — strictly worse than the telegram
  // message the same delivery sent, which links to the exact row.
  // `resolveActivityTargetPath` rebuilds the deep link from the delivery id the
  // row already carries in its own id, and returns `target_path` untouched for
  // everything else.
  const targetPath = resolveActivityTargetPath(item)

  if (targetPath) {
    return (
      <Link to={targetPath} className={className} style={style}>
        {content}
      </Link>
    )
  }

  return (
    <div className={className} style={style}>
      {content}
    </div>
  )
}

// A collapsed burst: one summary row that expands to reveal the individual
// items it stands in for.
function ActivityGroupRow({ items, ...context }: RowContext & { items: Burst }) {
  const [expanded, setExpanded] = useState(false)
  const panel = context.variant === 'panel'
  const first = items[0]
  const severity = groupSeverity(items)
  const preview = groupPreview(items)
  const Chevron = expanded ? ChevronDown : ChevronRight
  const when = <RowWhen occurredAt={first.occurred_at} projectSlug={first.project_slug} {...context} />

  return (
    <div>
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
        className={`${panel ? PANEL_ROW_CLASS : RAIL_ROW_CLASS} w-full text-left`}
        style={panel ? undefined : { borderLeft: severityEdge(severity), color: 'inherit' }}
      >
        <KindIcon item={first} severity={severity} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1 text-body-sm font-medium leading-[1.35]">
            <Chevron
              className="h-3 w-3 shrink-0 text-fg-secondary"
              aria-hidden="true"
            />
            <span className={panel ? 'truncate' : undefined}>{groupSummary(items)}</span>
          </div>
          <div
            className="mt-0.5 truncate text-caption leading-[1.3] text-fg-tertiary"
            title={panel ? preview : undefined}
          >
            {preview}
          </div>
          {!panel && when}
        </div>
        {panel && when}
      </button>
      {expanded && (
        <div className={panel ? 'divide-y border-t bg-surface border-border-subtle' : 'bg-surface'}>
          {items.map((item) => (
            <ActivityRow key={item.id} item={item} {...context} />
          ))}
        </div>
      )}
    </div>
  )
}
