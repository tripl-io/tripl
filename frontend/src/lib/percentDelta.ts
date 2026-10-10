/**
 * How a percent delta is written, everywhere it is shown.
 *
 * The percent gate deliberately admits anomalies with no baseline at all
 * — a scope resuming after an outage, an event firing for the
 * first time, a schema drift — and every one of those arrives with
 * `expected_count` 0. `percent_delta` is stored 0.0 for them because the ratio
 * is undefined and the column is NOT NULL, so printing it reported the largest
 * possible relative move as the smallest one. The absolute delta
 * is what there is to report for that class, and it sits in its own column.
 *
 * Mirrors the backend's `alert_templates` — `has_baseline`, `percent_delta_of`,
 * `format_percent_delta` — which fill the `${percent_delta_label}` variable in
 * the alert message. The two describe the SAME stored row — the delivery item —
 * so they have to say the same thing.
 */

import { MINUS_SIGN, formatNumber } from '@/lib/format'

/** What the percentage is called when there is nothing to divide by. */
export const NO_BASELINE_LABEL = 'no baseline'

/**
 * Was there an expectation to divide by?
 *
 * ZERO is the no-baseline condition, and it is the ONLY one — the mirror of the
 * backend's `alert_templates.has_baseline`.
 *
 * A NEGATIVE expectation is a REAL baseline. A `fact` sum/avg/min/max over a
 * signed column, or a `sql` level that legitimately sits below zero, has a level
 * of -100 that is exactly as substantial as one of +100; the detector scores that
 * series on magnitude and the matcher fires on it the same way
 * (`alerting_matching.rule_matches_anomaly` compares `abs(expected)` against
 * `min_expected_count` and `absolute_delta / abs(expected)` against
 * `min_percent_delta`). A reader still asking `expected > 0` therefore prints
 * "no baseline" over the very number that made the rule fire — the renderer
 * contradicting the matcher, and on this side of the wire that is what the user
 * actually sees in the inbox row and the rule replay table.
 *
 * It is one function rather than the same expression repeated per reader because
 * the repetition is exactly how the signed fix reached the backend matcher and
 * left the renderers behind. Every frontend surface that decides "was there a
 * baseline" should route through here.
 *
 * Non-finite input is treated as no baseline. The backend cannot receive it —
 * the column is a float — but the old guards here were written `expected > 0`
 * and `!(expected > 0)`, which answered "no baseline" for a `NaN` arriving from
 * a malformed payload. `!== 0` alone would answer "yes" and go on to render
 * "NaN%", so the check stays explicit rather than relying on the comparison's
 * side effect.
 */
export function hasBaseline(expectedCount: number): boolean {
  return Number.isFinite(expectedCount) && expectedCount !== 0
}

export function formatPercentDelta(
  percentDelta: number | null,
  expectedCount: number,
): string {
  // `hasBaseline` is the exact condition under which the backend computed the
  // stored number (`alert_templates.percent_delta_of`, and the same test in
  // `dispatch._create_deliveries`), so the label and the number cannot disagree
  // about whether there was a baseline.
  //
  // `null` is accepted because the API now sends it: `AlertDeliveryItemResponse
  // .percent_delta` is `float | None`, null exactly when there was no baseline.
  // The two guards agree by construction, but a delivery
  // recorded before that change still carries the stored 0.0 beside
  // `expected_count: 0`, so both conditions must be handled.
  //
  // The stored number is printed as it arrives, unsigned-as-sent: the backend
  // now stores a MAGNITUDE (`percent_delta_of`), while `AlertDeliveryItem
  // .percent_delta` is frozen history and older rows still hold a signed value.
  // Re-signing or re-absolutizing it here would rewrite what was sent in the
  // alert message, which is the one thing this function exists to match.
  if (percentDelta !== null && hasBaseline(expectedCount)) return `${percentDelta.toFixed(1)}%`
  return NO_BASELINE_LABEL
}

/**
 * The signed percentage a value moved against its baseline, or `null` when there
 * is no baseline to divide by.
 *
 * For the surfaces that hold the two counts and compute the ratio themselves —
 * a top-mover row, a monitoring signal banner — rather than reading a stored
 * `percent_delta`. Same {@link hasBaseline} gate as {@link formatPercentDelta}
 * and the backend's `alert_templates`, so no surface can decide on its own that
 * a zero baseline is a baseline.
 *
 * MAGNITUDE and SIGN are decided separately, and the divisor is what makes that
 * possible:
 *
 *  - the magnitude is the backend's, exactly: `Math.abs(ratioDelta(a, e))` is
 *    `abs(a - e) / abs(e) * 100`, the definition in
 *    `alert_templates.percent_delta_of`. Dividing by `abs(expected)` is the
 *    whole fix — a -3 baseline observed at -9 is a 200% move, the same size as
 *    3 observed at 9.
 *  - the sign is the direction of travel on the number line,
 *    `Math.sign(actual - expected)`, which is precisely how the backend picks
 *    the arrow: `anomaly_detector` sets `direction = "spike" if actual >=
 *    expected else "drop"`, with no reference to the baseline's own sign.
 *
 * Signing by the OLD expression `(actual - expected) / expected` flipped under a
 * negative baseline: -3 falling to -9 came out `+200%`, so the banner rendered a
 * down arrow (`direction === 'drop'`) beside a rising percentage. Every caller
 * takes its arrow and its colour from `direction`, never from this number, so
 * that disagreement was visible on screen and is what this sign rule removes.
 * For a POSITIVE baseline the two expressions are identical, so no existing row
 * moves.
 */
export function ratioDelta(actual: number, expected: number): number | null {
  if (!hasBaseline(expected)) return null
  return ((actual - expected) / Math.abs(expected)) * 100
}

/**
 * {@link ratioDelta} written for display: a signed percentage, or the words
 * `no baseline`.
 *
 * An undefined ratio is named, never dropped: a blank cell reads as missing
 * data, which is a different (and fixable) problem from one that is undefined by
 * definition.
 *
 * The `+` stays a real statement about direction because {@link ratioDelta} now
 * signs by `actual - expected`; feed this a bare magnitude and every drop would
 * read "+". A drop takes the typographic minus ("−43%"), as the other signed
 * figures on the monitoring pages do. {@link formatPercentDelta} keeps the
 * ASCII "-" on purpose: it reprints the alert message as it was sent.
 */
export function formatRatioDelta(percent: number | null, digits = 0): string {
  if (percent === null) return NO_BASELINE_LABEL
  const magnitude = formatNumber(Math.abs(percent), {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
  // Signed by what is printed: -0.4 rounds to "0%", not "-0%".
  if (/^[0.]+$/.test(magnitude)) return `${magnitude}%`
  return `${percent > 0 ? '+' : MINUS_SIGN}${magnitude}%`
}
