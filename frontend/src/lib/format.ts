/**
 * Number formatting shared by every surface, with ONE locale policy.
 *
 * The UI copy is English, so numbers and dates are painted in the app locale
 * rather than the browser's: a chart tooltip used to print "1 234 events" (the
 * reader's locale) under an en-US "Sep 24, 2:00 PM" (forced in the chart), and
 * the Events table and Settings disagreed on the grouping separator for the
 * same count. lib/datetime.ts and lib/metricFormat.ts format through the same
 * constant, and lib/plural.ts puts a count next to its noun. A bare
 * `toLocaleString()` (the browser's locale) is a lint error
 * (`tripl/no-bare-locale`).
 */
export const APP_LOCALE = 'en-US'

/** The typographic minus sign, so a signed figure's "−" is as wide as its "+". */
export const MINUS_SIGN = '−'

/** `1,234` / `0.35` in the app locale. */
export function formatNumber(value: number, options?: Intl.NumberFormatOptions): string {
  return value.toLocaleString(APP_LOCALE, options)
}

/**
 * The units a compact count escalates through, largest first. Each starts at
 * the value that would round to 1,000 of the unit below it: 999_500 already
 * rounds up to "1M", so it never renders as "1000k".
 */
const COMPACT_UNITS = [
  { from: 999_500_000, size: 1_000_000_000, suffix: 'B' },
  { from: 999_500, size: 1_000_000, suffix: 'M' },
  { from: 1_000, size: 1_000, suffix: 'k' },
] as const

/**
 * Compact count for axes, legends, badges and tight cells: 380000 -> "380k",
 * 1_500_000 -> "1.5M", 2_500_000_000 -> "2.5B", -2_000_000 -> "-2M". Whole
 * numbers at >= 100 of a unit, one decimal (trailing .0 stripped) below, so a
 * label stays <= ~5 characters at any size.
 *
 * Thousands are a lowercase "k"; millions and billions are uppercase, so a
 * volume of "1M" beside a "Last seen 1m ago" cell cannot read as one minute.
 *
 * Sub-1000 values are ROUNDED, not stringified verbatim: a fractional
 * confidence-band bound like -0.99 would otherwise render as
 * "-0.9900000000000001". The magnitude, not the signed value, picks the unit,
 * so a negative count compacts like a positive one.
 */
export function formatCompactNumber(value: number): string {
  const abs = Math.abs(value)
  for (const { from, size, suffix } of COMPACT_UNITS) {
    if (abs >= from) return `${compactUnit(value / size)}${suffix}`
  }
  return String(Math.round(value))
}

function compactUnit(scaled: number): number {
  return Math.abs(scaled) >= 100 ? Math.round(scaled) : Math.round(scaled * 10) / 10
}

/** `12.3%` from 0.123: a fraction as a percentage with a fixed number of decimals. */
export function formatPercent(fraction: number, digits = 1): string {
  return `${formatNumber(fraction * 100, { minimumFractionDigits: digits, maximumFractionDigits: digits })}%`
}

/**
 * `92%` from 0.92: a share of a whole (0..1) in whole percent, and `<1%` for
 * a share that is there but rounds to nothing. A value outside 0..1 (a share
 * computed from noisy counts) is clipped, so it never reads "104%" or "-3%".
 */
export function formatShare(share: number): string {
  const clipped = Number.isFinite(share) ? Math.min(1, Math.max(0, share)) : 0
  if (clipped > 0 && clipped < 0.005) return '<1%'
  return `${Math.round(clipped * 100)}%`
}
