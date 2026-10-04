/**
 * Fact-operand row-filter model + mapping helpers, kept separate from the
 * {@link FactFilterEditor} component so the component file only exports a
 * component (react-refresh) and so these pure functions can be unit-tested.
 */

import { uid } from '@/lib/uid'
import type { FactTableColumn } from '@/types/factTables'
import {
  factColumnValueKind,
  type FactColumnValueKind,
} from '@/lib/factColumnValueKind'
import {
  VALUELESS_CONDITION_OPERATORS,
  isListConditionOperator,
  type FactConditionConfig,
  type FactConditionOperator,
  type FactOperandConfig,
} from '@/lib/factOperandConfig'

export { VALUELESS_CONDITION_OPERATORS, type FactConditionOperator }

export type FactConditionPayload = FactConditionConfig

/**
 * One row-filter on a fact operand: a NAMED filter (a reusable WHERE fragment
 * defined on the fact table), a free-text SQL WHERE fragment, or a structured
 * column/operator/value condition. A fact operand carries an ordered list of
 * these; all are combined with AND at collection time.
 *
 * A condition holds `value` for scalar operators and `values` for `in` /
 * `not in`. The list used to be one comma-split string, so a value containing
 * a comma ("Smith, John") could not be matched at all.
 */
export type FactFilter =
  | { id: string; kind: 'named'; name: string }
  | { id: string; kind: 'sql'; sql: string }
  | {
      id: string
      kind: 'condition'
      column: string
      operator: FactConditionOperator
      value: string
      values: string[]
      /**
       * The condition exactly as stored, on a row loaded from a saved metric.
       * While the row still shows what loading made of it, the save re-sends
       * this instead of re-serialising the text: a scalar `in` value, or a
       * quoted `'3'` on a number column, would otherwise come back changed, and
       * the backend deletes a metric's history on any change of definition.
       */
      stored?: FactConditionPayload
    }

export type FactConditionFilter = Extract<FactFilter, { kind: 'condition' }>

export function makeNamedFilter(name = ''): FactFilter {
  return { id: uid(), kind: 'named', name }
}
export function makeSqlFilter(sql = ''): FactFilter {
  return { id: uid(), kind: 'sql', sql }
}
export function makeConditionFilter(
  column = '',
  operator: FactConditionOperator = 'eq',
  value: string | string[] = '',
): FactFilter {
  return Array.isArray(value)
    ? { id: uid(), kind: 'condition', column, operator, value: '', values: value }
    : { id: uid(), kind: 'condition', column, operator, value, values: [] }
}

/**
 * Switch a condition's operator, carrying the typed value across the
 * scalar/list boundary instead of dropping it: `= a` becomes `in (a)`, and
 * `in (a, b)` becomes `= a`.
 */
export function withConditionOperator(
  filter: FactConditionFilter,
  operator: FactConditionOperator,
): FactConditionFilter {
  const wasList = isListConditionOperator(filter.operator)
  const isList = isListConditionOperator(operator)
  if (wasList === isList) return { ...filter, operator }
  if (isList) {
    const value = filter.value.trim()
    return { ...filter, operator, values: value ? [value] : [], value: '' }
  }
  return { ...filter, operator, value: filter.values[0] ?? '', values: [] }
}

/**
 * True when `sql` starts with an opening paren whose matching closing paren is
 * the final character — i.e. the whole fragment is one parenthesised group and
 * the outer pair is semantically redundant. Quoted spans (`'…'` literals and
 * `"…"` / `` `…` `` identifiers, with doubled-quote escaping) are skipped so
 * parentheses inside strings never confuse the scan. A fragment with
 * unbalanced parens or an unterminated quote returns false (never "repaired").
 */
function isFullyWrapped(sql: string): boolean {
  if (sql.length < 2 || !sql.startsWith('(') || !sql.endsWith(')')) return false
  let depth = 0
  let quote: string | null = null
  for (let i = 0; i < sql.length; i++) {
    const char = sql[i]
    if (quote !== null) {
      if (char !== quote) continue
      // SQL escapes a quote inside a quoted span by doubling it; skip the pair.
      if (sql[i + 1] === quote) {
        i++
        continue
      }
      quote = null
      continue
    }
    if (char === "'" || char === '"' || char === '`') {
      quote = char
    } else if (char === '(') {
      depth++
    } else if (char === ')') {
      depth--
      if (depth === 0) return i === sql.length - 1
      if (depth < 0) return false
    }
  }
  return false
}

/**
 * Normalise a free-text SQL boolean fragment by stripping redundant
 * fully-wrapping outer parens: `((x = 1))` → `x = 1`, while
 * `(a = 1) AND (b = 2)` is untouched (its leading paren closes mid-string, so
 * removing it would change AND/OR evaluation order). Applied on BOTH load
 * ({@link filtersFromConfig}) and save ({@link filtersToPayload}) so the edit
 * round trip is a fixed point and definitions polluted by the historical
 * wrap-on-every-save bug self-heal on their next save.
 */
export function stripRedundantOuterParens(sql: string): string {
  let out = sql.trim()
  while (isFullyWrapped(out)) out = out.slice(1, -1).trim()
  return out
}

/**
 * Split a stored `filter_sql` back into the fragments {@link filtersToPayload}
 * joined: `(a) AND (b)` → `['a', 'b']`. Only a string made ENTIRELY of fully
 * parenthesised groups joined by a top-level AND is split, so a user's own
 * `(x OR y) AND z` stays one row — splitting it would be harmless (every row is
 * ANDed anyway) but would rewrite what they typed. Two SQL filters used to come
 * back from a save as one merged row.
 *
 * This is the lenient scan (any case, any whitespace around AND); the editor
 * loads through {@link sqlFiltersFromStored}, which splits only what joining
 * the parts again reproduces byte for byte.
 */
export function splitAndedFragments(sql: string): string[] {
  const trimmed = sql.trim()
  const parts: string[] = []
  let depth = 0
  let quote: string | null = null
  let start = 0
  for (let i = 0; i < trimmed.length; i++) {
    const char = trimmed.charAt(i)
    if (quote !== null) {
      if (char === quote) {
        if (trimmed[i + 1] === quote) i++
        else quote = null
      }
      continue
    }
    if (char === "'" || char === '"' || char === '`') quote = char
    else if (char === '(') depth++
    else if (char === ')') depth--
    else if (depth === 0 && /\s/.test(char)) {
      const match = /^\s+AND\s+/i.exec(trimmed.slice(i))
      if (!match) continue
      parts.push(trimmed.slice(start, i))
      i += match[0].length - 1
      start = i + 1
    }
  }
  parts.push(trimmed.slice(start))
  if (parts.length < 2 || !parts.every(part => isFullyWrapped(part.trim()))) {
    return trimmed ? [trimmed] : []
  }
  return parts.map(part => stripRedundantOuterParens(part))
}

/** How {@link filtersToPayload} joins two or more SQL fragments into one string. */
function joinSqlFragments(fragments: readonly string[]): string {
  return fragments.map(sql => `(${sql})`).join(' AND ')
}

/**
 * The SQL rows a stored `filter_sql` loads as. It is split into one row per
 * fragment only when it is exactly what {@link filtersToPayload} makes of those
 * rows; anything else — a lowercase `and`, a line break before `AND`, a
 * redundant outer paren pair — stays ONE row, verbatim. Rewriting it on load
 * would send a different `filter_sql` on the next save although the user never
 * touched the filter, and the backend deletes a metric's collected history on
 * any definition change.
 */
export function sqlFiltersFromStored(filterSql: string | null): string[] {
  const stored = (filterSql ?? '').trim()
  if (!stored) return []
  const parts = splitAndedFragments(stored)
  if (parts.length > 1 && parts.every(Boolean) && joinSqlFragments(parts) === stored) {
    return parts
  }
  return [stored]
}

/**
 * Map the UI's mixed filter list to the backend operand contract: named
 * filters become `row_filters` (deduped, order-preserved); free-text SQL
 * fragments are combined into a single `filter_sql` string. A single fragment
 * is stored as typed (trimmed); multiple fragments are each normalised
 * ({@link stripRedundantOuterParens}), parenthesised and ANDed (the parens keep
 * an OR fragment's precedence intact inside the joined string). Adding NO wrap
 * to a single fragment is safe because the collector parenthesises every
 * stored fragment itself before ANDing it with `row_filters` / `conditions`
 * (`_resolve_combined_filter` in metric_collect.py) — and, with
 * {@link sqlFiltersFromStored}, it keeps the load→save round trip a fixed
 * point.
 *
 * A condition row still exactly as it was loaded re-sends its stored form
 * (see `stored` on {@link FactFilter}).
 *
 * Incomplete rows are skipped here, but only because {@link filterRowErrors}
 * refuses to let a form save while one exists: a row dropped at this
 * point would save the metric less filtered than the editor showed.
 */
export function filtersToPayload(
  filters: FactFilter[],
  conditionColumns: readonly FactTableColumn[] = [],
): {
  row_filters: string[]
  filter_sql: string | null
  conditions: FactConditionPayload[]
} {
  const columnTypes = new Map(conditionColumns.map(column => [column.name, column.type]))
  const named: string[] = []
  for (const filter of filters) {
    if (filter.kind === 'named' && filter.name && !named.includes(filter.name)) {
      named.push(filter.name)
    }
  }
  const sqlFragments = filters
    .filter((filter): filter is Extract<FactFilter, { kind: 'sql' }> => filter.kind === 'sql')
    .map(filter => filter.sql.trim())
    .filter(sql => stripRedundantOuterParens(sql))
  const conditions: FactConditionPayload[] = []
  for (const filter of filters) {
    if (filter.kind !== 'condition') continue
    if (filter.stored && isUnchangedCondition(filter, filter.stored)) {
      conditions.push(storedConditionPayload(filter.stored))
      continue
    }
    const column = filter.column.trim()
    if (!column) continue
    if (VALUELESS_CONDITION_OPERATORS.has(filter.operator)) {
      conditions.push({ column, operator: filter.operator })
      continue
    }
    const kind = factColumnValueKind(columnTypes.get(column))
    if (isListConditionOperator(filter.operator)) {
      const values = filter.values.map(value => value.trim()).filter(Boolean)
      if (values.length === 0) continue
      conditions.push({
        column,
        operator: filter.operator,
        value: values.map(value => parseConditionScalar(value, kind)),
      })
      continue
    }
    const value = filter.value.trim()
    if (!value) continue
    conditions.push({ column, operator: filter.operator, value: parseConditionScalar(value, kind) })
  }
  return {
    row_filters: named,
    filter_sql:
      sqlFragments.length === 0
        ? null
        : sqlFragments.length === 1
          ? (sqlFragments[0] ?? null)
          : joinSqlFragments(sqlFragments.map(stripRedundantOuterParens)),
    conditions,
  }
}

function parseConditionScalar(
  value: string,
  kind: FactColumnValueKind,
): string | number | boolean {
  if (kind === 'number') {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : value
  }
  if (kind === 'boolean') {
    if (value.toLowerCase() === 'true') return true
    if (value.toLowerCase() === 'false') return false
  }
  return value
}

/**
 * Why each incomplete row cannot be saved, keyed by filter id. A named filter
 * with no name, a condition with no column or no value, or an empty SQL
 * fragment used to be dropped by {@link filtersToPayload} without a word, so
 * the metric saved UNFILTERED while the editor still showed the row.
 */
export function filterRowErrors(filters: readonly FactFilter[]): Record<string, string> {
  const errors: Record<string, string> = {}
  for (const filter of filters) {
    if (filter.kind === 'named') {
      if (!filter.name) errors[filter.id] = 'Pick a named filter, or remove this row.'
    } else if (filter.kind === 'sql') {
      if (!stripRedundantOuterParens(filter.sql)) {
        errors[filter.id] = 'Enter a SQL condition, or remove this row.'
      }
    } else if (!filter.column.trim()) {
      errors[filter.id] = 'Pick a column for this condition, or remove it.'
    } else if (VALUELESS_CONDITION_OPERATORS.has(filter.operator)) {
      continue
    } else if (isListConditionOperator(filter.operator)) {
      if (!filter.values.some(value => value.trim())) {
        errors[filter.id] = 'Add at least one value for this condition.'
      }
    } else if (!filter.value.trim()) {
      errors[filter.id] = 'Enter a value for this condition.'
    }
  }
  return errors
}

function conditionValueText(value: FactConditionConfig['value']): string {
  if (value === undefined || value === null) return ''
  return String(value)
}

/** The editable fields a stored condition loads as. */
function conditionRowFields(
  condition: FactConditionConfig,
): Pick<FactConditionFilter, 'column' | 'operator' | 'value' | 'values'> {
  const { column, operator, value } = condition
  if (Array.isArray(value)) return { column, operator, value: '', values: value.map(String) }
  if (isListConditionOperator(operator)) {
    return {
      column,
      operator,
      value: '',
      values: value === undefined || value === null ? [] : [String(value)],
    }
  }
  return { column, operator, value: conditionValueText(value), values: [] }
}

function isUnchangedCondition(
  filter: FactConditionFilter,
  stored: FactConditionConfig,
): boolean {
  const loaded = conditionRowFields(stored)
  return (
    filter.column === loaded.column
    && filter.operator === loaded.operator
    && filter.value === loaded.value
    && filter.values.length === loaded.values.length
    && filter.values.every((value, index) => value === loaded.values[index])
  )
}

/** A stored condition as the backend stores it: no `value` on a valueless operator. */
function storedConditionPayload(condition: FactConditionConfig): FactConditionPayload {
  const { column, operator, value } = condition
  return value === undefined || value === null || VALUELESS_CONDITION_OPERATORS.has(operator)
    ? { column, operator }
    : { column, operator, value }
}

/**
 * Rebuild the editable filter list from a stored operand config. Rows come
 * back grouped by type — named, then conditions, then SQL — because the
 * contract stores three separate lists; the editor says so next to the list.
 */
export function filtersFromConfig(
  config: Pick<FactOperandConfig, 'rowFilters' | 'conditions' | 'filterSql'>,
): FactFilter[] {
  const out: FactFilter[] = config.rowFilters.map(name => makeNamedFilter(name))
  for (const condition of config.conditions) {
    out.push({
      id: uid(),
      kind: 'condition',
      ...conditionRowFields(condition),
      stored: condition,
    })
  }
  for (const sql of sqlFiltersFromStored(config.filterSql)) {
    out.push(makeSqlFilter(sql))
  }
  return out
}
