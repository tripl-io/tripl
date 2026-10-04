import { formatNumber } from '@/lib/format'

/**
 * Count-aware copy, in one place.
 *
 * Every surface that prints a number next to a noun needs the same two lines of
 * logic, and the codebase had grown four separate answers to it: a `pluralize`
 * on ProjectsPage, two byte-identical `count` helpers (one in `runReport.ts`,
 * one in `ScanDryRunSummary.tsx`), and bare inline ternaries elsewhere. Four
 * copies is not a style problem — the copies do not agree, and the surfaces
 * with no copy at all are the ones that shipped "1 scans" to every project that
 * had just finished onboarding.
 *
 * The two exported shapes are genuinely different jobs, which is why the
 * duplicates diverged:
 *
 *  - {@link pluralize} chooses between two ready-made strings and never emits
 *    the number. Callers need it when the count is rendered separately (a KPI
 *    tile's value and its unit are different elements) or when agreement runs
 *    past the noun into the verb — "1 project currently has" against
 *    "3 projects currently have".
 *  - {@link countOf} is the common case built on top: the number, formatted for
 *    the reader's locale, then the noun that agrees with it.
 */

/**
 * The `singular` string when `count` is exactly 1, otherwise `plural`.
 *
 * Deliberately dumb about English: it picks between two strings the caller
 * wrote, so a caller whose plural is irregular ("1 entity" / "2 entities") or
 * whose agreement reaches the verb gets it right by writing both forms out.
 */
export function pluralize(count: number, singular: string, plural: string): string {
  return count === 1 ? singular : plural
}

/**
 * `1 scan`, `12 scans`, `4,812 rows` — the count and the noun that agrees with
 * it, as one string.
 *
 * The number goes through `formatNumber` (the app locale), so a five-figure count reads as
 * "10,000 rows" rather than "10000 rows" on every surface at once. Callers that
 * need the compact form instead (`1.8M`) format the number themselves and reach
 * for {@link pluralize} for the noun.
 */
export function countOf(value: number, singular: string, plural: string): string {
  return `${formatNumber(value)} ${pluralize(value, singular, plural)}`
}
