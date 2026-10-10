/**
 * Shared metric value formatting.
 *
 * Convention: a metric whose unit is '%' stores FRACTIONS (0.08 for an 8%
 * conversion), so every display multiplies by 100. All other units render the
 * stored value unchanged.
 */

import type { MetricScanInterval } from '@/types'
import { formatCompactNumber, formatNumber } from '@/lib/format'

/**
 * Human-readable collection cadence, one spelling for every metric surface —
 * the form's interval selects, the catalog's Latest-cell tooltip and the
 * drilldown's definition card used to carry three copies (two of them in
 * different case, the card the raw `1h` token). The option VALUES stay the raw
 * tokens the backend expects; only what is painted goes through this map.
 */
export const METRIC_INTERVAL_LABEL: Record<MetricScanInterval, string> = {
  '15m': 'Every 15 min',
  '1h': 'Hourly',
  '6h': 'Every 6 h',
  '1d': 'Daily',
  '1w': 'Weekly',
}

/**
 * The cadence for running text ("collected hourly", "every 15 min"), from an
 * interval a response carries as a plain string. Null for no interval or a
 * token this map does not know, so a caller never prints the raw `1h`.
 */
export function metricCadence(interval: string | null | undefined): string | null {
  if (!interval || !Object.hasOwn(METRIC_INTERVAL_LABEL, interval)) return null
  return METRIC_INTERVAL_LABEL[interval as MetricScanInterval].toLowerCase()
}

const INTERVAL_MINUTES: Record<MetricScanInterval, number> = {
  '15m': 15,
  '1h': 60,
  '6h': 6 * 60,
  '1d': 24 * 60,
  '1w': 7 * 24 * 60,
}

/**
 * True when `a` is a strictly shorter span than `b`. The backend refuses a
 * `replay_chunk_interval` finer than the collection interval
 * (`check_replay_chunk_against_interval`), so the form uses this to drop a
 * stored chunk the moment a coarser interval would make the save a 422.
 */
export function isIntervalFinerThan(a: MetricScanInterval, b: MetricScanInterval): boolean {
  return INTERVAL_MINUTES[a] < INTERVAL_MINUTES[b]
}

/**
 * The display precision shared by tiles, tables and tooltips, in the app
 * locale: whole numbers at >= 100, two decimals from 1 to 100, and two
 * SIGNIFICANT digits below 1. The old rule rounded everything under 100 to two
 * decimals, so a 0.004 s latency or a small rate read "0".
 */
function formatForDisplay(value: number): string {
  const abs = Math.abs(value)
  if (abs >= 100) return formatNumber(value, { maximumFractionDigits: 0 })
  if (abs >= 1 || abs === 0) return formatNumber(value, { maximumFractionDigits: 2 })
  return formatNumber(value, { maximumSignificantDigits: 2 })
}

export function isPercentUnit(unit: string | null | undefined): boolean {
  return unit?.trim() === '%'
}

// Currency symbols read in front of the number ("$1,234"), not after it: the
// "Revenue" and "AOV" templates seed `$`, which used to print "1,234 $".
const PREFIX_UNITS = new Set(['$', '€', '£', '¥', '₽'])

function withUnit(text: string, unit: string | null | undefined): string {
  const trimmed = unit?.trim()
  if (!trimmed) return text
  if (PREFIX_UNITS.has(trimmed)) {
    return text.startsWith('-') ? `-${trimmed}${text.slice(1)}` : `${trimmed}${text}`
  }
  return `${text} ${trimmed}`
}

/**
 * Human-readable metric value with its unit. Percent units render the stored
 * fraction ×100 with the sign attached ('8%', the spelling the chart axis uses
 * too — the tile and the axis used to disagree on '8 %' vs '8%');
 * currency units lead ('$1,234'); other units trail ('123 ms').
 */
export function formatMetricValue(value: number | null | undefined, unit: string | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (isPercentUnit(unit)) {
    return `${formatForDisplay(value * 100)}%`
  }
  return withUnit(formatForDisplay(value), unit)
}

/**
 * Compact axis number: '1.5k' / '-2.5M' from a thousand up (by magnitude, so
 * negatives compact too), the display precision below — never the raw float
 * ('0.30000000000000004') the old branch returned.
 */
function formatAxisNumber(value: number): string {
  return Math.abs(value) >= 1_000 ? formatCompactNumber(value) : formatForDisplay(value)
}

/**
 * Tick/tooltip formatter for metric charts. Percent units render compact ×100
 * values ('8%', '0.5%'); anything else renders a compact number ('8', '1.5k',
 * '-2.5M', '0.05'), prefixed by a currency unit when there is one. Other units
 * stay off the axis, where every tick would repeat them.
 */
export function metricAxisFormatter(unit: string | null | undefined): (value: number) => string {
  if (isPercentUnit(unit)) {
    return value => `${formatAxisNumber(value * 100)}%`
  }
  const prefix = unit?.trim()
  if (prefix && PREFIX_UNITS.has(prefix)) {
    return value => withUnit(formatAxisNumber(value), prefix)
  }
  return formatAxisNumber
}
