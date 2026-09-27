/**
 * Reading a signal's attribution (#255): the flagged bucket's delta
 * (actual − expected) split across the scan's breakdown columns, and the
 * release that reached traffic within the anomaly's window.
 *
 * The numbers are computed by the metrics worker when the anomaly is written
 * and stored with it, and so are the sentences: the headline and the release
 * line come from the backend (`core/analyzers/attribution.py`) and the panel
 * prints them verbatim, so the alert that went out and the drilldown's Why
 * panel read the same words. Nothing here re-derives them; these helpers only
 * draw the bars and label the values.
 */
import { formatNumber } from '@/lib/format'
import type { MetricScopeType, SignalAttributionColumn } from '@/types'

/** The scopes the worker attributes: volume anomalies only. */
export const ATTRIBUTED_SCOPES: ReadonlySet<MetricScopeType> = new Set<MetricScopeType>([
  'project_total',
  'event_type',
  'event',
])

const MINUS = '−'

/** `−3,120` / `+120` / `0`: a signed count with a real minus sign. */
export function formatSignedCount(value: number): string {
  const abs = Math.abs(value)
  const rounded = formatNumber(abs, { maximumFractionDigits: abs >= 10 ? 0 : 1 })
  if (rounded === '0') return '0'
  return `${value < 0 ? MINUS : '+'}${rounded}`
}

/**
 * `92%` from 0.92; whole percent, `<1%` for a visible-but-tiny share. For a
 * column's `explained_share` (0..1) only — a value's `share` is signed and is
 * never printed as a percentage.
 */
export function formatShare(share: number): string {
  const clipped = clipShare(share)
  if (clipped > 0 && clipped < 0.005) return '<1%'
  return `${Math.round(clipped * 100)}%`
}

function clipShare(share: number): number {
  if (!Number.isFinite(share)) return 0
  return Math.min(1, Math.max(0, share))
}

/**
 * The column's values that are not in its top list, as one remainder: the
 * values and it sum to the scope's delta, which is what the bars show.
 */
export function columnRemainder(column: SignalAttributionColumn, delta: number): number {
  const listed = column.values.reduce((sum, value) => sum + value.delta, 0)
  const remainder = delta - listed
  // Float dust from a sum that is exact on the backend is not a bar.
  return Math.abs(remainder) < 0.5 ? 0 : remainder
}

/** The largest |delta| a column's bars are drawn against (its values and remainder). */
export function columnScale(column: SignalAttributionColumn, delta: number): number {
  const magnitudes = column.values.map(value => Math.abs(value.delta))
  magnitudes.push(Math.abs(columnRemainder(column, delta)))
  return Math.max(0, ...magnitudes)
}

/** How a breakdown value is shown: an empty string reads as `(empty)`. */
export function attributionValueLabel(value: string): string {
  return value === '' ? '(empty)' : value
}
