import type { DataSource, ScanConfig } from '@/types'
import { Button } from '@/components/ui/button'
import { Link } from 'react-router-dom'
import { Chip } from '@/components/primitives/chip'
import { FreshnessChip } from '@/components/source-freshness/freshness-chip'
import { Ban, CheckCircle2, Clock, Loader2, MinusCircle, Play, XCircle, type LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Skeleton } from '@/components/ui/skeleton'
import { friendlyScanError } from '@/lib/scanError'
import { SCAN_RUN_STATUS } from '@/lib/statusLexicon'
import { ScenarioCoachMark } from '@/demo/ScenarioCoachMark'
import { SrcIcon } from './scanLayout'
import { type RunPillStatus } from './scanRunStatus'
import { SCAN_MODE_BADGE, scanModeOf } from './scanMode'
import { formatDueIn, type ScanRunInfo } from './scanUtils'

// Icon + spin are presentation; the word + colour come from the status lexicon.
const RUN_PILL_ICON: Record<RunPillStatus, { icon: LucideIcon; spin?: boolean }> = {
  succeeded: { icon: CheckCircle2 },
  failed: { icon: XCircle },
  running: { icon: Loader2, spin: true },
  pending: { icon: Clock },
  cancelled: { icon: Ban },
  never: { icon: MinusCircle },
}

// Status pill rendered as label text + icon shape. `title` surfaces the friendly
// failure message on hover; it never carries raw backend internals.
export function RunStatusPill({ status, title }: { status: RunPillStatus; title?: string }) {
  const { tone, label } = SCAN_RUN_STATUS[status]
  const { icon: Icon, spin } = RUN_PILL_ICON[status]
  return (
    <Chip
      tone={tone}
      size="xs"
      title={title}
      icon={<Icon className={cn('size-3', spin && 'animate-spin')} aria-hidden="true" />}
    >
      {label}
    </Chip>
  )
}

/**
 * What this scan actually does, derived from the two columns the dispatcher
 * filters on. It leads every badge strip because a config that collects nothing
 * used to look identical to one that collects everything.
 *
 * Module-private on purpose: `ScanBadges` already renders it, and `ScanBadges`
 * IS the public entry point. An outside caller reaching for the badge directly
 * would mount it beside the strip that already contains it.
 */
function ScanModeBadge({ sc }: { sc: ScanConfig }) {
  const { label, tone, title } = SCAN_MODE_BADGE[scanModeOf(sc)]
  return (
    <Chip size="xs" tone={tone} title={title}>
      {label}
    </Chip>
  )
}

// Config badges on the detail header (mode, ⏱ interval, lookback, caps, JSON, etc.).
export function ScanBadges({
  sc,
  intervalLabel,
  nextRunAt = null,
}: {
  sc: ScanConfig
  intervalLabel: Record<string, string>
  /**
   * When the scheduler next collects metrics (`next_metrics_run_at`, i9mt.16
   * DA-5). Shown beside the interval; omitted where the server has not said.
   */
  nextRunAt?: string | null
}) {
  // Three of the badges below describe `collect_metrics` and nothing else, and
  // only a monitoring scan is ever dispatched to it — the scheduler selects on
  // `interval IS NOT NULL AND time_column IS NOT NULL`
  // (worker/tasks/metrics/schedule.py:349-350). A manual run reads
  // `scan_row_limit` and never the metrics bounds (worker/tasks/scan.py:184), so
  // on Catalog only (and on the misconfigured pair) those three state limits
  // nothing applies. Saved values survive: the form keeps them across a mode
  // switch on purpose, this only stops the header advertising them.
  const monitoring = scanModeOf(sc) === 'monitoring'
  const items: string[] = []
  if (sc.interval) items.push(`⏱ ${intervalLabel[sc.interval] ?? sc.interval}`)
  // Only a scan the scheduler collects has a next run; the server sends null
  // for any other, and a stale value must not outlive a mode switch.
  const nextRunMs = monitoring && nextRunAt ? Date.parse(nextRunAt) : Number.NaN
  if (!Number.isNaN(nextRunMs)) {
    const due = formatDueIn(nextRunMs)
    items.push(due === 'due now' ? 'Next run due now' : `Next run ${due}`)
  }
  // A lookback is the predicate `<time column> >= now() - N`, so with no time
  // column it bounds nothing. Showing it anyway put "Lookback 24h" on the header
  // of a scan whose Source & query panel reads "Time column: None" — leaving the
  // badge as the only surface still claiming a bound the run does not apply.
  if (sc.scan_lookback_hours && sc.time_column) items.push(`Lookback ${sc.scan_lookback_hours}h`)
  if (sc.scan_row_limit) items.push(`Scan cap ${sc.scan_row_limit.toLocaleString()}`)
  if (monitoring && sc.metrics_row_limit) items.push(`Metrics cap ${sc.metrics_row_limit.toLocaleString()}`)
  if (sc.json_value_paths.length) items.push(`JSON keep ${sc.json_value_paths.length}`)
  if (monitoring && sc.metric_breakdown_columns.length) {
    items.push(`Breakdowns ${sc.metric_breakdown_columns.length}`)
  }
  if (monitoring && sc.distribution_drift_fields.length) {
    items.push(`Distribution ${sc.distribution_drift_fields.length}`)
  }
  if (sc.app_version_column) {
    items.push(`Version ${sc.app_version_column}`)
  }
  if (sc.event_group_rules.length) items.push(`Groups ${sc.event_group_rules.length}`)

  return (
    <div className="flex flex-wrap gap-1.5">
      <ScanModeBadge sc={sc} />
      {/* Late / overdue source (F16, #269); nothing when fresh or unknown. */}
      <FreshnessChip freshness={sc.freshness} />
      {items.map((label, i) => (
        <Chip key={i} size="xs" variant="outline">{label}</Chip>
      ))}
    </div>
  )
}

// One row in the "Scans" table.
export function ScanListRow({
  sc,
  dataSource,
  runInfo,
  intervalLabel,
  detailHref,
  onNavigate,
  onRun,
  runPending,
  runCoachMark,
  onReviewEvents,
}: {
  sc: ScanConfig
  dataSource: DataSource | null
  runInfo: ScanRunInfo
  intervalLabel: Record<string, string>
  /**
   * Where this scan's detail page lives, as a URL.
   *
   * Required, not optional: it is the row's ONLY keyboard and screen-reader
   * route into the scan. The <tr> used to carry role="button" + tabIndex so the
   * whole row was one widget, which put the Run and Review buttons inside an
   * interactive element — axe's nested-interactive, and in practice a screen
   * reader could address neither the row nor the controls it swallowed. The
   * name link below replaces that affordance.
   */
  detailHref: string
  /** Mouse-only convenience: the whole row is still a click target. */
  onNavigate: () => void
  /** Trigger a manual scan run for this row (POST /scans/{id}/run). */
  onRun?: () => void
  /** Disable the Run control while this row's run is in flight. */
  runPending?: boolean
  /** Point the coached demo's run-scan mark at this row's Run control. */
  runCoachMark?: boolean
  /** Jump into the review queue for the events this scan produces. */
  onReviewEvents?: () => void
}) {
  const firstQueryLine = sc.base_query.split('\n')[0]
  const cadenceLabel = sc.interval ? (intervalLabel[sc.interval] ?? sc.interval) : 'Manual'
  // `unknown` means this scan's job query is still in flight. There is no
  // verdict to pill yet, so the cell shows a skeleton rather than the
  // finished-looking "Never run" chip.
  const isRunInfoPending = runInfo.status === 'unknown'
  const pillStatus: RunPillStatus =
    runInfo.status === 'ok'
      ? 'succeeded'
      : runInfo.status === 'idle'
        ? 'never'
        : runInfo.status === 'unknown'
          ? 'pending'
          : runInfo.status
  const failedMessage = runInfo.status === 'failed'
    ? friendlyScanError(runInfo.lastJob?.error_message).message
    : null

  // The list is the surface the demo coach's step-1 CTA opens, so the Run
  // control must live here — not only on the detail page. Reuses the detail
  // page's Play icon; stopPropagation keeps the row's own navigate from firing.
  // The shared Button at the dense row-action size (DS-14), not a hand-rolled
  // 11px bordered button.
  // A run already in flight disables Run now and says so: a second click only
  // earned the backend's 409 (#247 DA-6). Stop lives on the scan's page.
  const runActive = runInfo.status === 'running'
  const runButton = onRun ? (
    <Button
      type="button"
      size="xs"
      variant="outline"
      aria-label={`Run ${sc.name} now`}
      disabled={runPending || runActive}
      onClick={e => { e.stopPropagation(); onRun() }}
    >
      <Play className="size-3" aria-hidden="true" />
      {runPending ? 'Starting…' : runActive ? 'Running…' : 'Run now'}
    </Button>
  ) : null

  return (
    // Deliberately inert markup: no role, no tabIndex, no key handler. The row
    // holds two buttons, so giving it a widget role of its own nested them
    // inside one (nested-interactive). onClick stays because a plain <tr> with a
    // click handler is not in the accessibility tree at all — it is a mouse
    // shortcut layered over the name link, not the way in.
    // Below `sm` the row is a stacked card: the name cell takes the first line
    // and the last run and the actions share the second, instead of a table
    // whose status and Run now sat off-screen to the right (LIVE-15). Same
    // cells, only their display changes, so nothing is rendered twice.
    // `sm:h-(--row-h)`: the Density setting reaches this list too (DS-9).
    <tr
      className="flex cursor-pointer flex-wrap items-center border-t transition-colors hover:bg-[var(--surface-hover)] sm:table-row sm:h-(--row-h) border-border-subtle"
      onClick={onNavigate}
    >
      {/* Lead with a human summary — name over "source · cadence". The raw SQL is
          demoted to a faint secondary line (full query on hover) rather than its
          own prominent column. */}
      <td className="block w-full px-3.5 pb-1 pt-2.5 align-middle sm:table-cell sm:w-auto sm:py-2.5">
        <div className="flex items-center gap-2.5">
          <SrcIcon dbType={dataSource?.db_type ?? null} size={28} />
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              {/* The row's one real control, and the only thing that puts this
                  scan in the tab order. stopPropagation keeps the row's mouse
                  shortcut from firing a second navigation on top of the link's. */}
              <Link
                to={detailHref}
                onClick={e => e.stopPropagation()}
                className="truncate text-body font-semibold no-underline hover:underline text-inherit"
              >
                {sc.name}
              </Link>
              {/* The list is where a never-monitoring config has to announce
                  itself: its runs go green and its row otherwise reads exactly
                  like a healthy monitoring scan. */}
              <ScanModeBadge sc={sc} />
              {/* The list is where a stalled source should be spotted without
                  opening every scan (F16, #269). */}
              <FreshnessChip freshness={sc.freshness} />
            </div>
            <div className="truncate text-caption text-fg-tertiary">
              {dataSource?.name ?? 'Unknown source'} · {cadenceLabel}
            </div>
            {/* Secondary: dropped on phones, where it pushed the name to a few letters. */}
            <div
              className="mono hidden max-w-[280px] truncate text-micro sm:block text-fg-tertiary"
              title={sc.base_query}
            >
              {firstQueryLine}
            </div>
          </div>
        </div>
      </td>
      <td className="block px-3.5 pb-2.5 pt-1 align-middle sm:table-cell sm:py-2.5">
        {isRunInfoPending ? (
          <Skeleton className="h-[18px] w-[132px]" aria-label={`Loading last run for ${sc.name}`} />
        ) : (
        <div className="flex flex-col gap-1">
          <span className="inline-flex items-center gap-1.5">
            <RunStatusPill status={pillStatus} title={failedMessage ?? undefined} />
            {runInfo.status !== 'idle' && runInfo.status !== 'running' && (
              // A relative time, not code: sans with tabular digits (DS-17).
              <span className="tnum text-caption text-fg-tertiary">
                {runInfo.lastRunLabel}
              </span>
            )}
          </span>
          {failedMessage && (
            <span className="text-caption leading-tight text-danger">
              {failedMessage}
            </span>
          )}
        </div>
        )}
      </td>
      <td className="ml-auto block px-3.5 pb-2.5 pt-1 align-middle text-right sm:table-cell sm:py-2.5">
        <div className="flex items-center justify-end gap-2">
          {runButton && (runCoachMark ? (
            <ScenarioCoachMark step="live-loop/run-scan">
              {runButton}
            </ScenarioCoachMark>
          ) : (
            runButton
          ))}
          {onReviewEvents && (
            <Button
              type="button"
              size="xs"
              variant="outline"
              aria-label={`Review events from ${sc.name}`}
              onClick={e => { e.stopPropagation(); onReviewEvents() }}
            >
              Review events
            </Button>
          )}
          <span className="text-[var(--fg-faint)]" aria-hidden="true">›</span>
        </div>
      </td>
    </tr>
  )
}
