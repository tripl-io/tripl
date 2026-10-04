import type { ScanConfig } from '@/types'

import { INTERVAL_LABEL } from './scanLayoutConstants'
import { type ScanFormMode, scanModeOf } from './scanMode'

/**
 * The one sentence that connects a scan to what it produces.
 *
 * A scan's output reaches the user as anomalies and Telegram alerts, and until
 * now no scan screen said so: the only string in the product that named the
 * chain was an onboarding hint that disappears once the checklist completes. So
 * "Scan: Snowplow Events (iOS)" arrived in Telegram and nothing on the scan
 * screens explained where it came from, or that a scan with no time column
 * produces none of it.
 *
 * Two audiences, two tenses:
 *  - the FORM note is future ("this scan will…"), driven by the radio the user
 *    is currently standing on, so the consequence of the choice is visible
 *    before it is saved;
 *  - the CONFIG note is present ("runs every hour…"), driven by the saved
 *    config's derived mode, so a scan's own page states what it does today.
 *
 * Everything here is deliberately module-private: the copy is asserted through
 * the rendered output, which is the only place it matters.
 */

/**
 * "Every run" would be false, and falsified by the button next to this note.
 * `run_scan` (backend/src/tripl/worker/tasks/scan.py) generates events and
 * variables and writes no `EventMetric` row; metric points come from
 * `collect_metrics`, which ONLY `check_metrics_due` dispatches, once a config's
 * own interval bucket closes. So `Run now` fills the plan and records nothing,
 * and both notes tie the points to the schedule instead of to the run.
 */
/**
 * Only what the mode radio above it does not already say. The radio
 * descriptions carry the consequence of each mode, and restating it here made
 * four sentences of one idea before the Name field (#247). What the
 * monitoring radio lacks is WHEN points arrive and WHERE their output shows up;
 * Catalog only has nothing to add, so it gets no note.
 */
const FORM_NOTE: Record<ScanFormMode, string | null> = {
  monitoring:
    'Metric points are recorded on the schedule, not by Run now. Signals appear on Anomalies;'
    + ' alerts go out from Alerting.',
  catalog: null,
}

/**
 * "Every hour" is the label the badges and the schedule select use; mid-sentence
 * it has to read as "Runs every hour." An interval with no entry in the table
 * (a value added to the backend enum before this map caught up) degrades to a
 * cadence-free phrasing rather than printing a raw token at the user.
 */
function cadencePhrase(interval: string | null): string {
  const label = interval ? INTERVAL_LABEL[interval] : undefined
  return label ? label.toLowerCase() : 'on its schedule'
}

/** The present-tense sentence for a SAVED config, keyed by its derived mode. */
function configNote(config: Pick<ScanConfig, 'time_column' | 'interval'>): string {
  const mode = scanModeOf(config)
  // "It is never run" is false, and falsified by the page it sits on: `Run now`
  // works (only `trigger_metrics_replay` guards on dispatchability), and Recent
  // runs lists this scan's completed runs directly underneath. What is true is
  // that the SCHEDULER never picks it up, so nothing collects metrics on its own.
  if (mode === 'misconfigured') {
    return 'This scan has a schedule but no time column, so the scheduler never runs it and it'
      + ' collects no metric points. Runs you start by hand still add events to your plan.'
      + ' Add a time column to fix it.'
  }
  if (mode === 'catalog') {
    return 'Runs when you start it. Adds events and fields to your tracking plan only — no'
      + ' metric points, no anomalies, no alerts.'
  }
  // `Run now` sits on this same page, so "each run records metric points" is a
  // promise the user can disprove in one click — see the note above FORM_NOTE.
  return `Runs ${cadencePhrase(config.interval)}. Each run adds events to your tracking plan;`
    + ' metric points for anomaly detection are collected on that schedule, not by Run now.'
}

type ScanCausalNoteProps =
  /** Under the mode radio: what the currently-selected mode will do. */
  | { variant: 'form'; mode: ScanFormMode }
  /** Under a saved scan's header: what that scan does today. */
  | { variant: 'config'; config: Pick<ScanConfig, 'time_column' | 'interval'> }

export function ScanCausalNote(props: ScanCausalNoteProps) {
  const isMisconfigured =
    props.variant === 'config' && scanModeOf(props.config) === 'misconfigured'
  const text = props.variant === 'form' ? FORM_NOTE[props.mode] : configNote(props.config)
  if (!text) return null

  return (
    <p
      data-testid="scan-causal-note"
      className="m-0 max-w-[620px] text-body-sm leading-snug"
      // A saved config that asked for a schedule and never got dispatched is a
      // fault, not a preference — it is the only variant that reads as a warning.
      style={{ color: isMisconfigured ? 'var(--warning)' : 'var(--fg-subtle)' }}
    >
      {text}
    </p>
  )
}
