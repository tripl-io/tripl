/**
 * Status lexicon — the ONE source of truth for "what colour does this status
 * mean?" across the app.
 *
 * COLOUR MEANING KEY (do not break this contract):
 *   success / green  → healthy, done, passing            (var(--success))
 *   warning / amber  → needs attention, stale, degraded  (var(--warning))
 *   danger  / red    → error, failed, firing             (var(--danger))
 *   info    / blue   → in-progress, informational        (var(--info))
 *   accent  / brand  → brand / primary emphasis          (var(--accent))
 *   neutral          → inert, untested, not-applicable   (var(--fg-*))
 *
 * Saturated colour (success/warning/danger) is reserved for states the user
 * may need to act on. Calm/inert states stay neutral so the real alerts stand
 * out. Every entry pairs a human {label} with its canonical {tone}; surfaces
 * import from here instead of re-deriving the mapping, which is how the same
 * status stays the same colour and word on every screen.
 *
 * Tones reuse the shared `ChipTone`/`DotTone` vocabulary (identical unions), so
 * a lexeme's tone drops straight into `<Chip>`, `<Dot>` and `<MiniStat>`.
 */
import type { ChipTone } from '@/components/primitives/chip'
import type { AlertDeliveryStatus, MonitoringSignal, MonitorStatus } from '@/types'
import type { RunPillStatus } from '@/types'
import { EVENT_STATUS_LABELS, EVENT_STATUS_TONE, type EventStatus } from './eventStatus'

/** A status rendered as a coloured word: its human label and canonical tone. */
export interface StatusLexeme {
  label: string
  tone: ChipTone
}

/** Project the `.tone` of every lexeme in a family into a flat record. */
function lexemeTones<K extends string>(family: Record<K, StatusLexeme>): Record<K, ChipTone> {
  const out = {} as Record<K, ChipTone>
  for (const key of Object.keys(family) as K[]) out[key] = family[key].tone
  return out
}

/** Project the `.label` of every lexeme in a family into a flat record. */
function lexemeLabels<K extends string>(family: Record<K, StatusLexeme>): Record<K, string> {
  const out = {} as Record<K, string>
  for (const key of Object.keys(family) as K[]) out[key] = family[key].label
  return out
}

// ---------------------------------------------------------------------------
// Event lifecycle — re-exported from eventStatus.ts (the canonical map); never
// duplicated here. Composed into a lexeme accessor for parity with the rest.
// ---------------------------------------------------------------------------
export {
  EVENT_STATUSES,
  EVENT_STATUS_LABELS,
  EVENT_STATUS_TONE,
  EVENT_STATUS_DOT_TONE,
  type EventStatus,
} from './eventStatus'

export function eventStatusLexeme(status: EventStatus): StatusLexeme {
  return { label: EVENT_STATUS_LABELS[status], tone: EVENT_STATUS_TONE[status] }
}

// ---------------------------------------------------------------------------
// Monitor status — firing / warning / healthy. Previously copy-pasted into
// three surfaces (MonitorsPage, MonitorDetailPage, RoutingRulesPanel); now one
// definition so a monitor is never red on one screen and amber on another.
// ---------------------------------------------------------------------------
export const MONITOR_STATUS: Record<MonitorStatus, StatusLexeme> = {
  firing: { label: 'Firing', tone: 'danger' },
  warning: { label: 'Warning', tone: 'warning' },
  healthy: { label: 'Healthy', tone: 'success' },
}

export const MONITOR_STATUS_TONE: Record<MonitorStatus, ChipTone> = lexemeTones(MONITOR_STATUS)
export const MONITOR_STATUS_LABEL: Record<MonitorStatus, string> = lexemeLabels(MONITOR_STATUS)

// ---------------------------------------------------------------------------
// Scan run status — succeeded / failed / running / queued / cancelled / never.
// `pending` is surfaced to the user as "Queued". Icons stay in the component
// (presentation); the word + colour live here.
// ---------------------------------------------------------------------------
export const SCAN_RUN_STATUS: Record<RunPillStatus, StatusLexeme> = {
  succeeded: { label: 'Succeeded', tone: 'success' },
  failed: { label: 'Failed', tone: 'danger' },
  running: { label: 'Running', tone: 'info' },
  pending: { label: 'Queued', tone: 'neutral' },
  cancelled: { label: 'Cancelled', tone: 'neutral' },
  never: { label: 'Never run', tone: 'neutral' },
}

// ---------------------------------------------------------------------------
// Signal state — an open anomaly on an event row. A signal from the latest scan
// is Open (danger); an older still-open one is Recent (warning). "Live" is
// reserved for the event lifecycle status (green, EV-5/DS-7), so a red "Live"
// one column over from a green one no longer means two different things.
//
// These deliberately do NOT reuse the MONITOR_STATUS words. Signals are raised
// by detection and exist whether or not a monitor does, so labelling one
// "Firing" put a firing verdict on 30 event rows of a project whose Monitors
// page correctly read "No monitors yet". Firing/Warning/Healthy
// stay reserved for monitors (alert rules).
// ---------------------------------------------------------------------------
export const SIGNAL_LEVEL = {
  firing: { label: 'Open', tone: 'danger' },
  warning: { label: 'Recent', tone: 'warning' },
} as const satisfies Record<'firing' | 'warning', { label: string; tone: 'danger' | 'warning' }>

/**
 * Map a raw row-signal `state` onto its lexeme. Anything that is not the latest
 * scan is a (calmer) warning — mirrors the previous inline rule exactly.
 */
export function rowSignalLevel(state: string): (typeof SIGNAL_LEVEL)['firing' | 'warning'] {
  return state === 'latest_scan' ? SIGNAL_LEVEL.firing : SIGNAL_LEVEL.warning
}

// ---------------------------------------------------------------------------
// Review status — has a human verified this event yet? Shown as "Verified",
// not "Reviewed": the In review STATUS and this flag are independent, and
// "Mark reviewed" read as emptying the review queue, which it never did
// (#238 JR-27). The field stays `reviewed` on the wire.
// ---------------------------------------------------------------------------
export const REVIEW_STATUS = {
  reviewed: { label: 'Verified', tone: 'success' },
  needs_review: { label: 'Not verified', tone: 'neutral' },
} as const satisfies Record<'reviewed' | 'needs_review', StatusLexeme>

// ---------------------------------------------------------------------------
// Data-source health — healthy / stale / failing / untested. A FAILED test is
// red (an error), matching the inline error banner on the same card; a stale
// "healthy" check is amber (needs a re-test, not an error). Both the overview
// list and the connections grid resolve health through here so a failed source
// is never amber on one screen and red on another.
// ---------------------------------------------------------------------------
//
// Title Case like every other lexeme here: these four were the only lower-case
// labels, so a status word changed case between screens (DS-45).
export const DATA_SOURCE_HEALTH = {
  healthy: { label: 'Healthy', tone: 'success' },
  stale: { label: 'Stale', tone: 'warning' },
  failing: { label: 'Failing', tone: 'danger' },
  untested: { label: 'Untested', tone: 'neutral' },
} as const satisfies Record<'healthy' | 'stale' | 'failing' | 'untested', StatusLexeme>

/**
 * Canonical {label, tone} for a data source. `isStale` is supplied by the
 * caller because each surface owns its own staleness window; the colour/word
 * decision is centralised so the two never disagree.
 */
export function dataSourceHealthLexeme(
  lastTestStatus: string | null | undefined,
  isStale: boolean,
): StatusLexeme {
  if (lastTestStatus === 'failed') return DATA_SOURCE_HEALTH.failing
  if (lastTestStatus === 'success') {
    return isStale ? DATA_SOURCE_HEALTH.stale : DATA_SOURCE_HEALTH.healthy
  }
  return DATA_SOURCE_HEALTH.untested
}

// ---------------------------------------------------------------------------
// Alert delivery — sent / failed / pending.
// ---------------------------------------------------------------------------
export const ALERT_DELIVERY: Record<AlertDeliveryStatus, StatusLexeme> = {
  sent: { label: 'Sent', tone: 'success' },
  failed: { label: 'Failed', tone: 'danger' },
  pending: { label: 'Pending', tone: 'info' },
}

export const ALERT_DELIVERY_TONE: Record<AlertDeliveryStatus, ChipTone> = lexemeTones(ALERT_DELIVERY)

// ---------------------------------------------------------------------------
// Coverage — share of active events that are implemented. Healthy coverage is
// green (success), NOT brand/accent: good coverage means "done", and reserving
// accent for brand emphasis keeps a calm metric from competing with real
// alerts. Below target is amber; well below is red.
// ---------------------------------------------------------------------------
export const COVERAGE_GOOD_PCT = 90
export const COVERAGE_WARN_PCT = 70

/** Canonical coverage tone for a percentage in [0, 100]. */
export function coverageTone(pct: number | undefined | null): ChipTone {
  if (pct == null) return 'neutral'
  if (pct >= COVERAGE_GOOD_PCT) return 'success'
  if (pct >= COVERAGE_WARN_PCT) return 'warning'
  return 'danger'
}

// ---------------------------------------------------------------------------
// Tone → CSS variable, for the few surfaces that paint a raw background/colour
// (e.g. the reconciliation heatmap) instead of rendering a <Chip>/<Dot>.
// ---------------------------------------------------------------------------
const TONE_VAR: Record<ChipTone, string> = {
  neutral: 'var(--fg-faint)',
  accent: 'var(--accent)',
  success: 'var(--success)',
  warning: 'var(--warning)',
  danger: 'var(--danger)',
  info: 'var(--info)',
}

/** The semantic CSS variable for a tone (e.g. 'success' → 'var(--success)'). */
export function toneVar(tone: ChipTone): string {
  return TONE_VAR[tone]
}

// ───────── Signal direction ─────────

export type SignalDirection = MonitoringSignal['direction']
export type SignalDirectionTone = 'danger' | 'warning'

/**
 * spike → danger, drop → warning: the colour convention every signal surface
 * shares (Anomalies, Overview, the event hero and banner, the chart marks). Top
 * movers kept their own and painted a spike green, so the breakdown rows behind
 * a red "Volume spike detected" banner looked healthy (MON-19).
 */
export const SIGNAL_DIRECTION: Record<SignalDirection, StatusLexeme> = {
  spike: { label: 'Spike', tone: 'danger' },
  drop: { label: 'Drop', tone: 'warning' },
}

export function signalDirectionTone(direction: SignalDirection): SignalDirectionTone {
  return direction === 'drop' ? 'warning' : 'danger'
}

/** {@link signalDirectionTone} as a CSS colour. */
export function signalDirectionColor(direction: SignalDirection): string {
  return `var(--${signalDirectionTone(direction)})`
}
