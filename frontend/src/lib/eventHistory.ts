import { formatTimestamp } from '@/lib/datetime'
import { eventAttributeLabel } from '@/lib/eventAttributes'
import { EVENT_STATUS_LABELS, type EventStatus } from '@/lib/eventStatus'
import { formatThreshold } from '@/lib/propertyEntries'
import { EXPECTED_REASON_OPTIONS, VERDICT_OPTIONS, verdictLabel } from '@/lib/signalVerdict'
import type { EventChange, SignalExpectedReason, SignalVerdictKind } from '@/types'

/** The history field a signal verdict on the event is recorded under (#254). */
export const SIGNAL_VERDICT_FIELD = 'signal_verdict'

/**
 * The history's `field` as a person reads it. The backend records each field
 * value and each meta value under its own key (`field:<name>`,
 * `meta:<name>`), a `created` row first, a signal verdict under its own name,
 * and every other edit (the tags included) under the event's own attribute
 * name, labelled from the attribute map every event surface reads.
 */
export function historyFieldLabel(field: string): string {
  if (field === 'created') return 'Created'
  if (field === SIGNAL_VERDICT_FIELD) return 'Signal verdict'
  if (field.startsWith('field:')) return `Field · ${field.slice('field:'.length)}`
  if (field.startsWith('meta:')) return `Meta · ${field.slice('meta:'.length)}`
  return eventAttributeLabel(field)
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

// Python's `str(datetime)`: a space between date and time, and up to six
// fractional digits. Not every engine parses that, so it is rewritten to the
// ISO shape `new Date` reads everywhere.
const PY_DATETIME = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)(\.\d{1,3})?\d*(.*)$/

/** A recorded timestamp as the event page prints one; the raw text if it does not parse. */
function timestampValueLabel(value: string): string {
  const match = PY_DATETIME.exec(value.trim())
  if (!match) return formatTimestamp(value) || value
  const [, day = '', time = '', millis = '', zone = ''] = match
  return formatTimestamp(`${day}T${time}${millis}${zone}`) || value
}

/** "95%" from the recorded `0.95`; the raw text if it is not a number. */
function thresholdValueLabel(value: string): string {
  const threshold = Number(value)
  return value.trim() !== '' && Number.isFinite(threshold) ? formatThreshold(threshold) : value
}

/** What the history row needs from the page to name a successor. */
export interface HistoryContext {
  /**
   * The event's current successor, once resolved. A `superseded_by_event_id`
   * row that names it reads as its name; a row naming an earlier successor
   * has nothing loaded to name it by.
   */
  successor?: { id: string; name: string } | null
}

/**
 * The history's `new_value` as a person reads it, or null when there is
 * nothing to show. Values are formatted the way the event page shows the
 * attribute (a status by its label, a date as a date, a threshold as a
 * percentage); a value removed reads "cleared", and a threshold removed reads
 * as the default it falls back to.
 */
export function historyValueLabel(
  field: string,
  value: string | null,
  context: HistoryContext = {},
): string | null {
  if (field === SIGNAL_VERDICT_FIELD) {
    return value === null ? 'cleared' : signalVerdictValueLabel(value)
  }
  if (field === 'required_presence_threshold') {
    return value === null ? `default (${formatThreshold(null)})` : thresholdValueLabel(value)
  }
  if (value === null) return field === 'created' ? null : 'cleared'
  if (field === 'status') return EVENT_STATUS_LABELS[value as EventStatus] ?? value
  if (field === 'sunset_at') return timestampValueLabel(value)
  if (field === 'superseded_by_event_id') {
    return context.successor && context.successor.id === value ? context.successor.name : 'another event'
  }
  return value
}

/**
 * One history row as a sentence's parts: "Status → Live", or
 * "Created as Home Screen View" for the first row, which names the event
 * rather than a change to it.
 */
export function describeHistoryChange(
  change: Pick<EventChange, 'field' | 'new_value'>,
  context: HistoryContext = {},
): { label: string; connector: string; value: string | null } {
  return {
    label: historyFieldLabel(change.field),
    connector: change.field === 'created' ? 'as' : '→',
    value: historyValueLabel(change.field, change.new_value, context),
  }
}

/**
 * Who made a history entry: the system label when no person did (a scan's
 * auto-live, #258), else the person by name when the roster knows them,
 * else by the email the entry recorded.
 */
export function historyAuthor(
  change: Pick<EventChange, 'author_label' | 'user_id' | 'user_email'>,
  nameOf: (userId: string) => string | null | undefined,
): string | null {
  if (change.author_label) return change.author_label
  const name = change.user_id ? nameOf(change.user_id) : null
  return name || change.user_email || null
}
