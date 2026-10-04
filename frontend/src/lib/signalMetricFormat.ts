/**
 * Signal value formatting, kept out of `signalMagnitude.ts` on purpose: that
 * module is on the first-load path (the top-bar bell reads it), and this one
 * pulls in the metric and incident formatters.
 */
import { formatNumber } from '@/lib/format'
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
