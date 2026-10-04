/** The pure half of the branch-diff value renderer.
 *
 * Separate from `DiffValue.tsx` for the reason `branchDiffFanout.ts` is
 * separate from `BranchesTab.tsx`: `react-refresh/only-export-components` is
 * an error in this project, and it fires on ANY module that exports both a
 * component and something else — not only on page modules. Shape-testing these
 * predicates directly is worth a second file.
 */

/** Header text for a collection member's keys. The key repeats identically on
 * every row, so as a column it carries no information after the first. */
export const COLUMN_LABEL: Record<string, string> = {
  field_name: 'Field',
  meta_field_name: 'Meta field',
  value: 'Value',
}

/** Rows of a record table; a table always has at least one. */
export type RecordRows = [Record<string, unknown>, ...Record<string, unknown>[]]

/** The subset of arrays a table can honestly render: every member a flat record
 * carrying the same keys. Anything else keeps the prose form — a photo member
 * has a nested `comments` list and an override member a nested `values` list,
 * so neither reaches this branch at all. */
export function uniformRecords(value: unknown): RecordRows | null {
  if (!Array.isArray(value)) return null
  if (!value.every(isFlatRecord)) return null
  const [first] = value
  if (first === undefined) return null
  const shape = Object.keys(first).sort().join(' ')
  return value.every((item) => Object.keys(item).sort().join(' ') === shape)
    ? (value as RecordRows)
    : null
}

/** Below this length a value reads fine inline, and growing a disclosure for it
 * costs more than it saves. */
export const JSON_CELL_MIN_LENGTH = 40

export function isFlatRecord(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === 'object' &&
    value !== null &&
    !Array.isArray(value) &&
    Object.values(value).every((item) => typeof item !== 'object' || item === null)
  )
}

/** Flattens a small record to `key: value · key: value` — the shape an event
 * field value or a photo takes once its natural key is stripped off. */
export function inlineRecord(value: Record<string, unknown>): string {
  return Object.entries(value)
    .map(([key, item]) => `${key}: ${item === null || item === '' ? '∅' : String(item)}`)
    .join(' · ')
}
