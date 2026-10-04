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
 *    format as "100.0%" is clamped down to "99.9%" so the display never lies.
 */

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
 * @example formatPlanCoverage(0, 0)     // "0%"
 */
export function formatPlanCoverage(implemented: number, active: number): string {
  if (active <= 0) {
    return '0%'
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
