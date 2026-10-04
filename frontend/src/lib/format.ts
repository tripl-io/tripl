/**
 * Number formatting shared by every surface, with ONE locale policy.
 *
 * The UI copy is English, so numbers and dates are painted in the app locale
 * rather than the browser's: a chart tooltip used to print "1 234 events" (the
 * reader's locale) under an en-US "Sep 24, 2:00 PM" (forced in the chart), and
 * the Events table and Settings disagreed on the grouping separator for the
 * same count. lib/datetime.ts and lib/metricFormat.ts format through the same
 * constant.
 */
export const APP_LOCALE = 'en-US'

/** `1,234` / `0.35` in the app locale. */
export function formatNumber(value: number, options?: Intl.NumberFormatOptions): string {
  return value.toLocaleString(APP_LOCALE, options)
}

/**
 * Compact count for axes, legends and tight cells: 380000 -> "380k",
 * 1_500_000 -> "1.5M", 1_000_000 -> "1M", -2_000_000 -> "-2M". Whole numbers at
 * >= 100 of a unit, one decimal (trailing .0 stripped) below, so a label stays
 * <= ~5 characters across the 100k–9.9M range.
 *
 * Sub-1000 values are ROUNDED, not stringified verbatim: a fractional
 * confidence-band bound like -0.99 would otherwise render as
 * "-0.9900000000000001". The magnitude, not the signed value, picks the unit,
 * so a negative count compacts like a positive one.
 */
export function formatCompactNumber(value: number): string {
  const abs = Math.abs(value)
  // 999_500 already rounds up to "1M" — escalate before it renders "1000k".
  if (abs >= 999_500) return `${compactUnit(value / 1_000_000)}M`
  if (abs >= 1_000) return `${compactUnit(value / 1_000)}k`
  return String(Math.round(value))
}

function compactUnit(scaled: number): number {
  return Math.abs(scaled) >= 100 ? Math.round(scaled) : Math.round(scaled * 10) / 10
}
