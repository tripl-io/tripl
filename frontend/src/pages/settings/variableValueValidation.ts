import type { VariableType } from '@/types'

/**
 * What a documented value, an override value or a bulk-added value may look
 * like for each variable type.
 *
 * The type was chosen and then ignored: every value list took any string, so a
 * `number` variable accepted "abc", a `boolean` "yes" and a `date` "tomorrow",
 * and drift then compared observed values against a list that could never match
 * them. An array type's values are its ELEMENTS, one per chip, so a
 * `number_array` value is checked as a number.
 *
 * `string`, `string_array` and anything unknown accept every value.
 */

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/
const DATETIME_RE = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/

function isNumber(value: string): boolean {
  return value.trim() !== '' && Number.isFinite(Number(value))
}

function isCalendarDate(value: string): boolean {
  if (!DATE_RE.test(value)) return false
  const parsed = new Date(`${value}T00:00:00Z`)
  // Rejects 2026-02-30, which Date would roll over into March.
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value
}

function isDatetime(value: string): boolean {
  return DATETIME_RE.test(value) && isCalendarDate(value.slice(0, 10)) && !Number.isNaN(Date.parse(value.replace(' ', 'T')))
}

function isJson(value: string): boolean {
  try {
    JSON.parse(value)
    return true
  } catch {
    return false
  }
}

const RULES: Partial<Record<VariableType, { validate: (value: string) => boolean; message: string }>> = {
  number: { validate: isNumber, message: 'A Number property takes numbers, e.g. 42 or 3.5.' },
  number_array: {
    validate: isNumber,
    message: 'A Number[] property takes numbers, one per value, e.g. 42 or 3.5.',
  },
  boolean: {
    validate: (value) => value === 'true' || value === 'false',
    message: 'A Boolean property takes true or false.',
  },
  date: { validate: isCalendarDate, message: 'A Date property takes dates as YYYY-MM-DD.' },
  datetime: {
    validate: isDatetime,
    message: 'A Datetime property takes ISO date-times, e.g. 2026-09-25T14:30:00Z.',
  },
  json: { validate: isJson, message: 'A JSON property takes valid JSON, e.g. {"a": 1} or "text".' },
}

/** The ChipListInput `validate`/`invalidMessage` pair for a type; empty when any value goes. */
export function valueRuleFor(type: VariableType): { validate?: (value: string) => boolean; invalidMessage?: string } {
  const rule = RULES[type]
  return rule ? { validate: rule.validate, invalidMessage: rule.message } : {}
}

/** The values in `values` that a variable of `type` would not accept. */
export function invalidValuesFor(type: VariableType, values: readonly string[]): string[] {
  const rule = RULES[type]
  return rule ? values.filter((value) => !rule.validate(value)) : []
}

/** How the bulk bar's one-line "Add values" box is read, said beside it. */
export const VALUE_LIST_HINT =
  'Separate values with commas. A comma inside a JSON object or array, or inside double quotes, does not split.'

/**
 * Split one typed line into values on its top-level commas.
 *
 * A plain `split(',')` cut `{"a": 1, "b": 2}` into two halves neither of which
 * is JSON, so a JSON value could not be bulk-added at all. A comma nested in
 * `[]`/`{}` or inside a double-quoted string (with `\"` escapes) now stays part
 * of its value; `a, b` still gives `a` and `b`. Values are trimmed and blanks
 * dropped, as before.
 */
export function splitValueList(text: string): string[] {
  const values: string[] = []
  let depth = 0
  let inString = false
  let start = 0
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i]
    if (inString) {
      if (char === '\\') i += 1
      else if (char === '"') inString = false
    } else if (char === '"') {
      inString = true
    } else if (char === '[' || char === '{') {
      depth += 1
    } else if (char === ']' || char === '}') {
      // A stray closer must not push later commas out of reach.
      depth = Math.max(0, depth - 1)
    } else if (char === ',' && depth === 0) {
      values.push(text.slice(start, i))
      start = i + 1
    }
  }
  values.push(text.slice(start))
  return values.map((value) => value.trim()).filter(Boolean)
}
