import { useEffect, useState } from 'react'

import type { VariableValueDrift, VariableValueDriftStatus } from '@/api/variableDrifts'
import { formatDateTime } from './datetime'

/**
 * Where a drift sits in review. THREE states, because the backend has three and
 * reading only two is what put the table badge and the review panels at odds:
 * `_active_drift_predicates` counts `open` AND `snoozed` rows and
 * then drops the ones whose `snoozed_until` is still in the future, so snoozing
 * a variable's last drift feeds a ZERO into `open_drift_count` and the list says
 * nothing needs attention — while the panels, which knew only "resolved vs not",
 * kept that same row in the warning-toned active list with the full Accept /
 * Snooze / False positive row and no way to collapse it.
 *
 * `snoozed` is deliberately NOT folded into `resolved`. A resolution is a
 * decision and comes back only through Reopen; a snooze is a promise to look
 * again, and it lapses on its own. All the two share is that neither is asking
 * for attention right now, which is exactly what the badge counts.
 */
export type DriftReviewState = 'active' | 'snoozed' | 'resolved'

/**
 * Statuses that close review. Such a row comes back only through Reopen — or,
 * for `accepted`, when a scan observes a value OUTSIDE the accepted set (the
 * backend reopens the row itself). The review therefore has to keep resolved
 * rows reachable.
 */
const RESOLVED_DRIFT_STATUSES: ReadonlySet<VariableValueDriftStatus> = new Set([
  'accepted',
  'false_positive',
])

/**
 * The instant a snooze runs out, or `NaN` when the row carries none that can be
 * read — a NULL, an unparseable timestamp, or a status that is not `snoozed` at
 * all. The classification below and the clock that wakes it up both read the
 * deadline through here, so the timer cannot be armed for an instant the
 * classification ignores, nor miss one it acts on.
 */
function snoozeDeadline(drift: Pick<VariableValueDrift, 'status' | 'snoozed_until'>): number {
  if (drift.status !== 'snoozed' || drift.snoozed_until === null) return Number.NaN
  return Date.parse(drift.snoozed_until)
}

/**
 * Mirrors `_active_drift_predicates` in `variable_value_drift_service.py`, which
 * is the query behind the table's drift badge — the two must give the same
 * answer or the badge and the review contradict each other again.
 *
 * Note what the backend does NOT say: a snooze whose time has already PASSED is
 * active again, and so is a `snoozed` row carrying no `snoozed_until` at all.
 * Both land back in the open list here exactly as they do in the count.
 *
 * `now` is a parameter rather than a call, so that one instant classifies a
 * whole render and the caller owns when it advances. The review takes its own
 * from `useDriftReviewClock`, which moves it the moment the nearest snooze runs
 * out.
 */
export function driftReviewState(
  drift: Pick<VariableValueDrift, 'status' | 'snoozed_until'>,
  now: number = Date.now(),
): DriftReviewState {
  if (RESOLVED_DRIFT_STATUSES.has(drift.status)) return 'resolved'
  if (drift.status !== 'snoozed') return 'active'
  const until = snoozeDeadline(drift)
  // An unparseable timestamp reads as "no snooze in force", the same way the
  // backend reads a NULL: showing a drift that is not asking for attention is a
  // smaller error than hiding one that is.
  return Number.isNaN(until) || until <= now ? 'active' : 'snoozed'
}

/**
 * When the earliest snooze still in force among `drifts` runs out, or `null` if
 * none is. Nothing but the clock can change a row's review state without new
 * data arriving, so these are the only instants a panel has to wake up for.
 */
export function nextDriftSnoozeExpiry(
  drifts: readonly Pick<VariableValueDrift, 'status' | 'snoozed_until'>[],
  now: number = Date.now(),
): number | null {
  let soonest: number | null = null
  for (const drift of drifts) {
    const until = snoozeDeadline(drift)
    // The same rejections `driftReviewState` makes: a deadline it already reads
    // as spent or unusable is not one to wake up for.
    if (Number.isNaN(until) || until <= now) continue
    if (soonest === null || until < soonest) soonest = until
  }
  return soonest
}

/**
 * `setTimeout` keeps its delay in a signed 32-bit int; a larger one wraps and
 * fires immediately. A snooze further out than ~24.8 days is therefore reached
 * in stages — every wake-up re-arms for the remainder — instead of spinning.
 */
export const MAX_DRIFT_TIMER_DELAY_MS = 2_147_483_647

/**
 * The instant the drift review (`DriftReviewList`, on the property's page and
 * the event's) classifies its drifts against, advanced exactly when the nearest
 * snooze runs out.
 *
 * The clock used to be a bare `useState(() => Date.now())` on each panel. A lazy
 * initializer runs ONCE per mount, so that `now` was frozen for the life of the
 * mount: a snooze lapsing while the panel sat open kept reading as snoozed, the
 * panel kept the row collapsed behind the toggle with only Un-snooze on it,
 * while the badge beside it — which the backend recomputes per request — had
 * already moved. That is the badge/panel disagreement the fix exists to
 * remove, coming back in through the clock. A refetch does not rescue it: new
 * data re-renders the panel, and re-rendering does not re-run an initializer.
 *
 * One timeout aimed at the nearest deadline, not a tick: `driftReviewState` can
 * only change at those instants, so waking for anything else would re-render for
 * nothing. `Date.now()` is read in the effect and in the timer callback, never
 * during render — `react-hooks/purity` governs render only — and the seed keeps
 * this repo's lazy-`useState` render-clock idiom (see `useLiveWindowEnd`).
 *
 * It lives here, beside `driftReviewState` and the `snoozeDeadline` both read,
 * so the clock cannot wake for an instant the classification ignores. The
 * review used to exist twice, and a guard on one of the two copies is how they
 * fell out of step to begin with.
 */
export function useDriftReviewClock(
  drifts: readonly Pick<VariableValueDrift, 'status' | 'snoozed_until'>[],
): number {
  const [now, setNow] = useState(() => Date.now())
  // A primitive, so the timer is re-armed when the nearest deadline really moves
  // and left alone by a refetch that returns the same rows — the `?? []` array
  // the callers hand over changes identity on every single render.
  const nextExpiry = nextDriftSnoozeExpiry(drifts, now)
  useEffect(() => {
    if (nextExpiry === null) return
    // `nextExpiry` is strictly greater than `now` by construction, so the delay
    // is at least 1ms and the callback cannot re-seed the instant it already
    // holds — React bails out of that, and nothing would re-arm the timer.
    const delay = Math.min(nextExpiry - now, MAX_DRIFT_TIMER_DELAY_MS)
    const id = window.setTimeout(() => setNow(Date.now()), delay)
    return () => window.clearTimeout(id)
  }, [nextExpiry, now])
  return now
}

/** The vocabulary the status pill uses. Reached through `driftStatusNote`, which
 * is the only reading that also accounts for a snooze that has lapsed. */
const DRIFT_STATUS_LABEL: Record<VariableValueDriftStatus, string> = {
  open: 'open',
  snoozed: 'snoozed',
  accepted: 'accepted',
  false_positive: 'false positive',
}

/**
 * Text for the row's status pill. A snooze is the one state whose useful content
 * is a TIME: "snoozed" on its own never said when the row comes back, and
 * `snoozed_until` was fetched by `api/variableDrifts.ts` and rendered NOWHERE.
 * Deferring review is only defensible if the deferral is legible.
 */
export function driftStatusNote(
  drift: Pick<VariableValueDrift, 'status' | 'snoozed_until'>,
  now: number = Date.now(),
): string {
  if (driftReviewState(drift, now) === 'snoozed' && drift.snoozed_until) {
    const until = formatDateTime(drift.snoozed_until)
    if (until) return `snoozed until ${until}`
  }
  return DRIFT_STATUS_LABEL[drift.status]
}

/**
 * Names the collapsed group behind the "Show N …" toggle. The group holds two
 * unlike things and the button must not claim otherwise — calling a snoozed row
 * "resolved" is the very conflation the badge disagreed with.
 */
export function collapsedDriftLabel(counts: { snoozed: number; resolved: number }): string {
  if (counts.snoozed === 0) return 'resolved'
  if (counts.resolved === 0) return 'snoozed'
  return 'snoozed or resolved'
}

/**
 * The one action that returns a collapsed row to the open list. Both readings
 * post the same backend `reopen`, which clears `snoozed_until` as well as the
 * resolution — so the word differs only because what the reader is undoing
 * does.
 */
export const DRIFT_REVIVE_LABEL: Record<Exclude<DriftReviewState, 'active'>, string> = {
  snoozed: 'Un-snooze',
  resolved: 'Reopen',
}
