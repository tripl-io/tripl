/**
 * Pure value handling for the single-event form: what goes into a box from a
 * stored event, and what goes out of it into a save.
 */
import type { FieldDefinition } from '@/types'
import { toLocalDateTimeValue } from '@/lib/datetime'

export function normalizeMetricBreakdownColumns(columns: string[]): string[] {
  const seen = new Set<string>()
  return columns
    .map(column => column.trim())
    .filter(column => {
      if (!column || seen.has(column)) return false
      seen.add(column)
      return true
    })
}

/**
 * A stored sunset instant as a `datetime-local` value, in the reader's zone.
 *
 * The form used to slice the ISO string — UTC wall time shown as if it were
 * local — and post the local string back with no offset, which the backend took
 * as UTC. In UTC+3, 09:00 entered was stored as 09:00Z, and the detail page
 * (which formats the instant locally) showed 12:00.
 */
export function sunsetInputValue(iso: string | null | undefined): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return toLocalDateTimeValue(date)
}

/**
 * A `datetime-local` value as the instant it names, for the API. A string with
 * a date and time but no offset is local time (ECMA-262 Date parsing), which is
 * what the picker shows. Null for an empty or unreadable value.
 */
export function sunsetIsoValue(local: string): string | null {
  if (!local) return null
  const date = new Date(local)
  if (Number.isNaN(date.getTime())) return null
  return date.toISOString()
}

const TEMPLATE_TOKEN = /\$\{[^}]+\}/

/**
 * What a number field may hold: a number, or a value carrying a `${variable}`
 * token that is resolved later. A native number input refused both `$` and `{`,
 * so the token could not be typed, and a stored `${price}` rendered as an empty
 * box while the form still saved it. Empty is "no value".
 */
export function isNumberFieldValue(value: string): boolean {
  const trimmed = value.trim()
  if (trimmed === '') return true
  if (TEMPLATE_TOKEN.test(trimmed)) return true
  return Number.isFinite(Number(normalizeNumberFieldValue(trimmed)))
}

/** One comma and no point, e.g. `1,5`: a decimal comma, not a thousands mark. */
const DECIMAL_COMMA = /^[+-]?\d*,\d+$/

/**
 * A number field value with a decimal comma read as a decimal point. The
 * decimal key of a comma-locale phone keyboard (`inputMode="decimal"`) types
 * `,`, which `Number()` refuses, so without this the author could not enter a
 * fraction at all. Anything else — tokens, other text — is returned as given.
 */
export function normalizeNumberFieldValue(value: string): string {
  return DECIMAL_COMMA.test(value.trim()) ? value.trim().replace(',', '.') : value
}

/**
 * A chip list with the text still sitting in its input added, the way Enter
 * would add it. Text typed into Tags and not committed with Enter used to be
 * dropped on save without a word. Only ever adds: Enter on the
 * breakdown input toggles a column, but text left in a box is never read as
 * "remove this".
 */
export function withPendingChip(
  list: string[],
  pending: string,
  normalize: (value: string) => string = value => value.trim(),
): string[] {
  const next = normalize(pending)
  if (!next || list.includes(next)) return list
  return [...list, next]
}

export const normalizeTag = (value: string): string => value.trim().toLowerCase()

/** Whether `value` is one a control of `field`'s type can show and save. */
function fitsField(field: FieldDefinition, value: string): boolean {
  if (field.field_type === 'boolean') return value === 'true' || value === 'false'
  if (field.field_type === 'enum' && field.enum_options) return field.enum_options.includes(value)
  if (field.field_type === 'number') return isNumberFieldValue(value)
  return true
}

/**
 * The field values of one event type carried onto another, by field name.
 *
 * Changing the type on the create form used to clear every value, even where
 * the new type has the same fields. A value moves when the new type has
 * a field of the same name whose control can hold it; `dropped` names the
 * filled fields that could not come along, so the form can ask first.
 */
export function carryFieldValues(
  from: readonly FieldDefinition[],
  to: readonly FieldDefinition[],
  values: Readonly<Record<string, string>>,
): { values: Record<string, string>; dropped: string[] } {
  const targetByName = new Map(to.map(field => [field.name, field]))
  const carried: Record<string, string> = {}
  const dropped: string[] = []
  for (const field of from) {
    const value = values[field.id]
    if (value === undefined || value === '') continue
    const target = targetByName.get(field.name)
    if (target && fitsField(target, value)) carried[target.id] = value
    else dropped.push(field.display_name)
  }
  return { values: carried, dropped }
}

/** The id of a column's toggle in the Metric breakdowns row. */
export const breakdownChipId = (column: string): string =>
  `form-breakdown-chip-${encodeURIComponent(column)}`

/**
 * Bring a column's breakdown toggle into view and focus it. The per-field line
 * only points here: a second toggle far from the row changed the set
 * without the reader seeing why.
 */
export function focusBreakdownChip(column: string): void {
  const chip = document.getElementById(breakdownChipId(column))
  if (!chip) return
  chip.scrollIntoView({ block: 'center' })
  chip.focus({ preventScroll: true })
}
