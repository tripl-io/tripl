/**
 * Canonical plan-coverage formatting.
 *
 * Coverage is the share of active events that are implemented. Screens used to
 * format it ad hoc (`Math.round(...)` on one page, `toFixed(1)` on another),
 * which let the SAME ratio render as "99%" in one place and "100.0%" in
 * another. The rules here keep every surface consistent:
 *
 *  - only a fully-covered plan (implemented >= active) ever shows "100%";
 *  - everything else is shown to one decimal place;
 *  - a partial plan can never round UP to a perfect score — a value that would
 *    format as "100.0%" is clamped down to "99.9%" so the display never lies;
 *  - a plan with nothing active has no score: "—", not a "0%" that reads as a
 *    failing grade.
 *
 * It also holds the two governance definitions side by side: plan coverage
 * (catalog entries) and Reconciliation's data match (warehouse rows) both read
 * as "coverage", so each page's help names the other measure in the same words.
 */

import { formatNumber } from '@/lib/format'
import { COVERAGE_GOOD_PCT } from '@/lib/statusLexicon'

/**
 * Look-back window for "dead" (implemented but silent) events, shared by every
 * surface that asks the question.
 *
 * Coverage's "Instrumentation gaps" panel links straight into Reconciliation's
 * "Dead events" panel with "Triage in Reconciliation". While the two pages held
 * their own copies (30 here, 14 there) that hand-off silently changed the
 * question: a shorter window is a WEAKER silence test, so Reconciliation listed
 * more events than the count that sent the user there. One
 * constant keeps the hand-off honest.
 *
 * 30 is the value the backend already defaults to
 * (`reconciliation_service.DEFAULT_DEAD_EVENT_DAYS`) and the widest option on
 * the Events page's "Silent > 30d" filter, so all four surfaces now agree.
 */
export const DEAD_EVENT_DAYS = 30

/** Coverage as a ratio in [0, 1]. Returns 0 when there are no active events. */
export function planCoverageRatio(implemented: number, active: number): number {
  if (active <= 0) {
    return 0
  }
  if (implemented >= active) {
    return 1
  }
  if (implemented <= 0) {
    return 0
  }
  return implemented / active
}

/**
 * Format plan coverage for display.
 *
 * @example formatPlanCoverage(320, 323) // "99.1%"
 * @example formatPlanCoverage(323, 323) // "100%"
 * @example formatPlanCoverage(0, 0)     // "—"
 */
export function formatPlanCoverage(implemented: number, active: number): string {
  if (active <= 0) {
    return '—'
  }
  if (implemented >= active) {
    return '100%'
  }

  const ratio = planCoverageRatio(implemented, active)
  const formatted = (ratio * 100).toFixed(1)

  // A partial plan must never round up to a perfect score: clamp a value that
  // formats as "100.0%" down to "99.9%".
  if (Number(formatted) >= 100) {
    return '99.9%'
  }

  return `${formatted}%`
}

/**
 * What plan coverage counts, for the info tip on its stat. Coverage sits one nav
 * item from Reconciliation's data match, so the tip names that measure too —
 * in its own unit, warehouse rows, which an earlier wording got wrong.
 */
export const PLAN_COVERAGE_HELP =
  'Share of active planned events marked implemented. Different from the Reconciliation data match, which is the share of tracked event occurrences (warehouse rows) that matched a planned event.'

/**
 * What Reconciliation's data match counts over its `days`-day window. The unit
 * is warehouse OCCURRENCES (rows), not catalog entries — every other surface
 * uses "events" for the latter, so this one has to say which it means.
 */
export function dataMatchHelp(days: number): string {
  return `Share of tracked event occurrences in warehouse data that matched a planned event, over the last ${days} days. Counts occurrences (warehouse rows), not catalog entries. Different from plan coverage on the Coverage page, the share of active events marked implemented.`
}

/**
 * The plan-coverage stat's tone, one rule for the Overview's tile and the
 * Coverage page's, which it links to: the same 58.8% was amber on one and
 * alarm red on the other. Only the exception carries a colour. Below the good
 * bar it is a warning and never red — an unfinished plan is work to do, not an
 * incident. A good figure, or a plan with nothing active to cover, is neutral.
 */
export function planCoverageTone(implemented: number, active: number): 'warning' | 'neutral' {
  if (active <= 0) return 'neutral'
  return planCoverageRatio(implemented, active) * 100 >= COVERAGE_GOOD_PCT ? 'neutral' : 'warning'
}

/**
 * The line under the Coverage bar that splits its not-implemented remainder
 * (active − implemented), so the tiles and the bar add up at a glance.
 *
 * The project summary counts only the review queue; the rest is drafts, events
 * ready for development and deprecated ones, with no count per status. So the
 * rest is named as one group: "1 draft, ready for dev or deprecated" read as
 * one event holding three statuses, or as "1 draft" and a third item.
 *
 * An empty group is left out rather than counted as "0 …". An empty review
 * queue still gets its clause, as "none in review": "another status" is
 * another than review, so the group needs it beside it.
 */
export function formatNotImplementedBreakdown(inReview: number, notImplemented: number): string {
  const inReviewShare = Math.min(Math.max(inReview, 0), notImplemented)
  const otherShare = notImplemented - inReviewShare
  const parts = [inReviewShare > 0 ? `${formatNumber(inReviewShare)} in review` : 'none in review']
  if (otherShare > 0) {
    parts.push(`${formatNumber(otherShare)} in another status (draft, ready for dev or deprecated)`)
  }
  return `Not implemented: ${parts.join(', ')}.`
}
