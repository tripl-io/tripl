import { formatCompactNumber } from '@/lib/format'
import { formatRatioDelta, ratioDelta } from '@/lib/percentDelta'
import type { EventMetricsResponse } from '@/types'

/**
 * The Events tab's one-line summary of its series: "612K in 7d · +4%
 * vs prior week". Null when the response carries no weekly totals (a scope
 * other than the Events series, or a server that predates them). A prior week
 * with nothing in it has no baseline, so the comparison is left off rather
 * than printed as "+∞%".
 */
export function formatWeekSummary(
  metrics: Pick<EventMetricsResponse, 'week_total' | 'prior_week_total'> | undefined,
): string | null {
  const week = metrics?.week_total
  if (week == null) return null
  const parts = [`${formatCompactNumber(week)} in 7d`]
  const prior = metrics?.prior_week_total
  const delta = prior == null ? null : ratioDelta(week, prior)
  if (delta !== null) parts.push(`${formatRatioDelta(delta)} vs prior week`)
  return parts.join(' · ')
}
