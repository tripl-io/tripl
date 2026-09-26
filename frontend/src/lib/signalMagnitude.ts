/**
 * The one definition of "how big is this signal", shared by every surface that
 * reports an open-signal count.
 *
 * These numbers used to live in each page. They drifted twice: the sidebar badge
 * disagreed with the Anomalies page (tripl-yfsj.1), and after that was fixed for
 * three surfaces the top-bar bell was left behind on a different query entirely
 * (tripl-jfm3.89). A shared module makes "the counts agree" the default rather
 * than something four call sites have to remember.
 *
 * The magnitude itself is no longer mirrored: the backend ships
 * ``relative_effect`` on each signal and this module reads it, so the sidebar's
 * ``monitoring_signal_count`` and this gate cannot disagree by construction.
 * Only the THRESHOLD is still duplicated, in
 * ``services/metrics_insights_service.SIGNIFICANT_MIN_REL_EFFECT``.
 */
import type { MonitoringSignal } from '@/types'

/**
 * Relative effect: |actual − expected| / max(expected, 1).
 *
 * Preferred over the z-score for user-facing filtering because z is inflated on
 * low-volume series, letting a tiny absolute change on a quiet scope outrank a
 * large one on a busy scope.
 */
export function relativeEffect(
  signal: Pick<MonitoringSignal, 'actual_count' | 'expected_count' | 'relative_effect'>,
): number {
  // The server computes this now and ships it on the signal. It is the only
  // side that knows whether a catalog metric's series is count-shaped, and a
  // fractional one must NOT have its denominator floored at 1 — a ratio
  // expected at 0.12 and observed at 0.04 scored 0.08 here and never cleared
  // the bar below, so metric anomalies were invisible at the default level
  // (tripl-yf8c). Recomputing locally is also what let this formula drift from
  // the backend's twice before (tripl-yfsj.1, tripl-jfm3.89).
  //
  // The fallback stays for payloads that carry no value — it is the old
  // count-shaped estimate, correct for every scope except a fractional metric.
  if (signal.relative_effect !== null && signal.relative_effect !== undefined) {
    return signal.relative_effect
  }
  return Math.abs(signal.actual_count - signal.expected_count) / Math.max(signal.expected_count, 1)
}

/** The "Significant" bar (±50%): the AnomaliesPage default and the badge gate. */
export const SIGNIFICANT_MIN_REL_EFFECT = 0.5

/** The "Major" bar (±100%): the top magnitude filter level. */
export const MAJOR_MIN_REL_EFFECT = 1

/**
 * The magnitude filter levels. `threshold` is the bar in the words the rows
 * use (% change from expected), so the filter explains itself instead of
 * leaving "why is this row not Major?" to the reader (MO-3).
 */
export const MAGNITUDE_PRESETS = [
  { id: 'all', label: 'All', minRelEffect: 0, threshold: null },
  {
    id: 'significant',
    label: 'Significant',
    minRelEffect: SIGNIFICANT_MIN_REL_EFFECT,
    threshold: '≥50%',
  },
  { id: 'major', label: 'Major', minRelEffect: MAJOR_MIN_REL_EFFECT, threshold: '≥100%' },
] as const

export type MagnitudeLevel = (typeof MAGNITUDE_PRESETS)[number]['id']

/** "Significant (≥50%)": a preset's option label, threshold included (MO-3). */
export function magnitudePresetLabel(preset: (typeof MAGNITUDE_PRESETS)[number]): string {
  return preset.threshold ? `${preset.label} (${preset.threshold})` : preset.label
}

export type MagnitudeWord = 'Minor' | 'Significant' | 'Major'

/**
 * How unusual a signal is, in words, on the same bars as the filter: a PM
 * reads "Major" where a z-score means nothing to them (JR-31). "Minor" is
 * everything under the Significant bar, i.e. what only "All" shows.
 */
export function signalMagnitudeWord(
  signal: Pick<MonitoringSignal, 'actual_count' | 'expected_count' | 'relative_effect'>,
): MagnitudeWord {
  const effect = relativeEffect(signal)
  if (effect >= MAJOR_MIN_REL_EFFECT) return 'Major'
  if (effect >= SIGNIFICANT_MIN_REL_EFFECT) return 'Significant'
  return 'Minor'
}

/** Default to "Significant" so a flooded list is usable without touching the filter. */
export const DEFAULT_MAGNITUDE_LEVEL: MagnitudeLevel = 'significant'

/**
 * The significant open signals a user has not hidden, biggest effect first.
 *
 * Hidden is a triage verdict — the scope was muted, or this signal was marked
 * expected (MO-4 / JR-5). A signal with a verdict (#254) — expected, tracking
 * bug, false positive, real issue, or its incident's — has been answered and
 * leaves the count too; acknowledged alone has not. The backend's badge count
 * applies the same two rules, so the bell, the Overview headline and the
 * sidebar still agree.
 *
 * Callers MUST pass the EXPANDED signal list (`getActiveSignals(slug, undefined,
 * { expanded: true })`). The collapsed variant queries only project_total and
 * event_type scopes, so an incident made purely of event-scope anomalies is not
 * rolled up into a parent — it is dropped, and the count silently reads zero
 * during a live incident (tripl-jfm3.89).
 *
 * `filter` returns a fresh array, so the `sort` never mutates a React Query cache.
 */
export function selectSignificantSignals(
  signals: readonly MonitoringSignal[] | undefined,
): MonitoringSignal[] {
  return (signals ?? [])
    .filter(
      signal =>
        !signal.hidden && !signal.verdict && relativeEffect(signal) >= SIGNIFICANT_MIN_REL_EFFECT,
    )
    // The shared comparator, so a tie on relative effect breaks on |z| here
    // exactly as it does on the Anomalies page.
    .sort(compareSignalsByMagnitude)
}

/**
 * The one ranking of open signals: biggest relative effect first, |z| as the
 * tie-breaker.
 *
 * The Anomalies page sorted by |z| alone while Overview and the bell sorted by
 * `relativeEffect`, so the top anomaly on Overview was not the top row of the
 * Anomalies list, and quiet-scope noise — where z is inflated — led the page
 * (MON-14). A comparator, not a selector, so a caller that has already filtered
 * sorts its own copy.
 */
export function compareSignalsByMagnitude(a: MonitoringSignal, b: MonitoringSignal): number {
  return (
    relativeEffect(b) - relativeEffect(a)
    || Math.abs(b.z_score) - Math.abs(a.z_score)
  )
}

