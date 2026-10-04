import { EXPECTED_REASON_OPTIONS, VERDICT_OPTIONS, verdictLabel } from '@/lib/signalVerdict'
import type { SignalExpectedReason, SignalVerdictKind } from '@/types'

/** The history field a signal verdict on the event is recorded under (#254). */
export const SIGNAL_VERDICT_FIELD = 'signal_verdict'

/**
 * The history's `field` as a person reads it. The backend records the tags,
 * each field value and each meta value under their own keys
 * (`tags`, `field:<name>`, `meta:<name>`) and a `created` row first.
 */
export function historyFieldLabel(field: string): string {
  if (field === 'created') return 'Created'
  if (field === 'tags') return 'Tags'
  if (field === 'title') return 'Title'
  if (field === 'sunset_at') return 'Sunset'
  if (field === SIGNAL_VERDICT_FIELD) return 'Signal verdict'
  if (field.startsWith('field:')) return `Field · ${field.slice('field:'.length)}`
  if (field.startsWith('meta:')) return field.slice('meta:'.length)
  return field
}

const VERDICT_KINDS = new Set<string>(VERDICT_OPTIONS.map((option) => option.verdict))
const EXPECTED_REASONS = new Set<string>(EXPECTED_REASON_OPTIONS.map((option) => option.reason))

function isVerdictKind(value: string): value is SignalVerdictKind {
  return VERDICT_KINDS.has(value)
}

function isExpectedReason(value: string): value is SignalExpectedReason {
  return EXPECTED_REASONS.has(value)
}

const NOTE_SEPARATOR = ' — '

/**
 * A signal-verdict history value as a person reads it. The backend writes
 * `<verdict>` or `expected:<reason>`, then ` — <note>` when there is one:
 * `tracking_bug` reads "Tracking bug", `expected:campaign` "Expected ·
 * campaign". A value it does not recognise is shown as recorded.
 */
function signalVerdictValueLabel(value: string): string {
  const cut = value.indexOf(NOTE_SEPARATOR)
  const head = cut === -1 ? value : value.slice(0, cut)
  const note = cut === -1 ? '' : value.slice(cut)
  const [kind = '', reason] = head.split(':', 2)
  if (!isVerdictKind(kind)) return value
  const expectedReason = kind === 'expected' && reason && isExpectedReason(reason) ? reason : null
  return `${verdictLabel({ verdict: kind, expected_reason: expectedReason })}${note}`
}

/**
 * The history's `new_value` as a person reads it, or null when there is
 * nothing to show. A cleared signal verdict has no value and reads "cleared".
 */
export function historyValueLabel(field: string, value: string | null): string | null {
  if (field === SIGNAL_VERDICT_FIELD) {
    return value === null ? 'cleared' : signalVerdictValueLabel(value)
  }
  return value
}
