/**
 * Which open signal the demo's "drill into the latest incident" step points at.
 *
 * The demo warehouse injects an incident every day on a global calendar —
 * a spike, a drop, an error burst, a whole event type dipping — so there is no
 * one seeded row to name any more: the step points at whatever opened last.
 * Kept free of React so the rule is unit-testable on plain signal lists.
 */

import type { MonitoringSignal } from '@/types'

/**
 * Scopes whose row opens a drilldown under `/monitoring/`, the arrival that
 * completes the step (`stepCompletedByPath`). A catalog metric's row opens
 * the metric page instead, and drift scopes open no chart at all.
 */
const SCOPE_RANK: Partial<Record<MonitoringSignal['scope_type'], number>> = {
  // The most concrete chart first when one incident opened several scopes in
  // the same bucket: the event, then its type, then the project's total.
  event: 0,
  event_type: 1,
  project_total: 2,
}

function bucketTime(signal: MonitoringSignal): number {
  const time = Date.parse(signal.bucket)
  return Number.isNaN(time) ? Number.NEGATIVE_INFINITY : time
}

function magnitude(signal: MonitoringSignal): number {
  return Math.abs(signal.relative_effect ?? 0)
}

/**
 * The newest open incident with a drilldown, or undefined when none is open.
 * A muted signal or one marked expected (the weekly promo) is not an incident
 * the visitor is meant to investigate, so it is never picked.
 */
export function pickCoachedIncident(
  signals: readonly MonitoringSignal[],
): MonitoringSignal | undefined {
  let best: MonitoringSignal | undefined
  for (const signal of signals) {
    const rank = SCOPE_RANK[signal.scope_type]
    if (rank === undefined || signal.hidden || signal.muted || signal.expected) continue
    if (best === undefined) {
      best = signal
      continue
    }
    const newer = bucketTime(signal) - bucketTime(best)
    if (newer !== 0) {
      if (newer > 0) best = signal
      continue
    }
    const narrower = rank - (SCOPE_RANK[best.scope_type] ?? 0)
    if (narrower !== 0) {
      if (narrower < 0) best = signal
      continue
    }
    if (magnitude(signal) > magnitude(best)) best = signal
  }
  return best
}
