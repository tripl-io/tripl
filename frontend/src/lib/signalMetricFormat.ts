/**
 * Signal value and time formatting, kept out of `signalMagnitude.ts` on
 * purpose: that module is on the first-load path (the top-bar bell reads it),
 * and this one pulls in the metric and incident formatters.
 */
import { formatNumber } from '@/lib/format'
import { formatTimestamp, formatUtcOffset } from '@/lib/datetime'
import { formatIncidentCount } from '@/lib/alertStatus'
import { formatMetricValue } from '@/lib/metricFormat'
import type { MonitoringSignal } from '@/types'

/**
 * A signal's "actual vs expected", in the metric's own unit.
 *
 * Catalog-metric signals are not counts: a percent metric read "0.043 vs 0.12"
 * here while its detail page said "4.3 % vs 12 %". The unit rides on
 * the signal when the server sends one (`unit`, metric scope only); without it
 * the values keep the count formatting every event scope uses.
 */
export function formatSignalValues(
  signal: Pick<MonitoringSignal, 'actual_count' | 'expected_count' | 'unit'>,
): string {
  if (signal.unit) {
    return `${formatMetricValue(signal.actual_count, signal.unit)} vs ${formatMetricValue(signal.expected_count, signal.unit)}`
  }
  return `${formatNumber(signal.actual_count)} vs ${formatIncidentCount(signal.expected_count)}`
}

/**
 * The tooltip for a signal's time: what it is, the full timestamp and the zone
 * it is in, named the way every local time beside UTC is named, since the short
 * form (`formatShortTimestamp`) is shown in it —
 * "Bucket starting Sep 25, 2026, 6:00 PM (UTC+2)".
 */
export function signalTimeTitle(what: string, iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return what
  return `${what} ${formatTimestamp(iso)} (${formatUtcOffset(date)})`
}
