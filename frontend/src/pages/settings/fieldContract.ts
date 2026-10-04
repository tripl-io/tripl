/**
 * Parsing and validation for a field's data-contract inputs.
 *
 * The inputs used to be parsed with no word back: "0,5" or "abc" in Null share,
 * Min or Max became `null`, which quietly cleared that rule, and the same typo in
 * Bad share became `0`, the strictest setting there is, so every value failed
 * the contract on the next scan. Nothing checked the 0–1 range, that
 * Min is not above Max either. Now nothing is dropped or tightened: a number
 * that does not parse is an error the form shows.
 *
 * The regex is NOT validated here. The backend compiles it with Python's `re`
 * and deliberately screens for no single dialect, and the warehouses that run
 * it speak RE2 or PostgreSQL; JavaScript's RegExp rejects valid patterns in all
 * of them (`(?i)`, `(?P<name>…)`), so blocking on it locked fields with a
 * working rule out of any edit. `regexNotice` says so without blocking, and the
 * server's 422 is the verdict.
 */

export interface ContractDraft {
  contract_max_bad_rate: string
  contract_required_max_null_rate: string
  contract_regex: string
  contract_min_value: string
  contract_max_value: string
}

export type ContractErrors = Partial<Record<keyof ContractDraft, string>>

export interface ParsedContract {
  contract_max_bad_rate: number
  contract_required_max_null_rate: number | null
  contract_regex: string | null
  contract_min_value: number | null
  contract_max_value: number | null
}

/**
 * A decimal as typed, or `undefined` when it does not parse.
 *
 * A single decimal comma is read as a point: "0,5" is how half is written in
 * much of the world, and reading it as "no rule" was the defect. "1,000" is
 * left alone — it could be a thousand or one — so it does not parse and the
 * form asks instead of guessing.
 */
export function parseDecimal(value: string): number | undefined {
  const trimmed = value.trim()
  if (!trimmed) return undefined
  const decimalComma = /^[+-]?\d*,\d+$/.test(trimmed) && !/^[+-]?\d{1,3},\d{3}$/.test(trimmed)
  const normalised = decimalComma ? trimmed.replace(',', '.') : trimmed
  const parsed = Number(normalised)
  return Number.isFinite(parsed) ? parsed : undefined
}

const SHARE_MESSAGE = 'Enter a share between 0 and 1, e.g. 0.05.'

function shareError(raw: string, required: boolean): string | undefined {
  if (!raw.trim()) return required ? SHARE_MESSAGE : undefined
  const parsed = parseDecimal(raw)
  if (parsed === undefined || parsed < 0 || parsed > 1) return SHARE_MESSAGE
  return undefined
}

function numberError(raw: string): string | undefined {
  if (!raw.trim()) return undefined
  return parseDecimal(raw) === undefined ? 'Enter a number, e.g. 0 or 12.5.' : undefined
}

/**
 * A non-blocking note for a pattern this browser cannot compile. Many such
 * patterns are valid where the rule actually runs, so it is information, never
 * a reason to refuse the save.
 */
export function regexNotice(raw: string): string | undefined {
  const pattern = raw.trim()
  if (!pattern) return undefined
  try {
    new RegExp(pattern)
    return undefined
  } catch {
    return 'This browser cannot check this pattern (Python or RE2 syntax such as (?i) or (?P<name>…) is fine). The server checks it when you save.'
  }
}

/** Every problem with the draft, keyed by input. Empty when it can be saved. */
export function validateContract(draft: ContractDraft): ContractErrors {
  const errors: ContractErrors = {}
  const bad = shareError(draft.contract_max_bad_rate, true)
  if (bad) errors.contract_max_bad_rate = bad
  const nullShare = shareError(draft.contract_required_max_null_rate, false)
  if (nullShare) errors.contract_required_max_null_rate = nullShare
  const min = numberError(draft.contract_min_value)
  if (min) errors.contract_min_value = min
  const max = numberError(draft.contract_max_value)
  if (max) errors.contract_max_value = max
  if (!min && !max) {
    const lo = parseDecimal(draft.contract_min_value)
    const hi = parseDecimal(draft.contract_max_value)
    if (lo !== undefined && hi !== undefined && lo > hi) {
      errors.contract_max_value = 'Max must be at least Min.'
    }
  }
  return errors
}

/**
 * The payload half of a draft that `validateContract` passed. Blank optional
 * rules become `null` (no rule); a draft with errors must not reach here.
 */
export function parseContract(draft: ContractDraft): ParsedContract {
  return {
    contract_max_bad_rate: parseDecimal(draft.contract_max_bad_rate) ?? Number.NaN,
    contract_required_max_null_rate: parseDecimal(draft.contract_required_max_null_rate) ?? null,
    contract_regex: draft.contract_regex.trim() || null,
    contract_min_value: parseDecimal(draft.contract_min_value) ?? null,
    contract_max_value: parseDecimal(draft.contract_max_value) ?? null,
  }
}
