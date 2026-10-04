/**
 * One reader for a fact operand stored in a metric's untyped `config` JSON, one
 * writer back to the create/update contract, and the condition-operator table.
 *
 * Three hand-written parsers of the same JSON used to live in the metric form,
 * the catalog's "Duplicate as draft" and the drilldown's definition card, and
 * they disagreed: Duplicate cast `aggregation` unchecked and passed
 * `row_filters` through without filtering, so a malformed config went straight
 * into a create payload and came back as a 422, and it never folded the legacy
 * single `row_filter`, so duplicating an old metric silently lost its named
 * filter. Every caller now narrows through {@link readFactOperandConfig}.
 */

import {
  METRIC_AGGREGATIONS,
  type FactMetricCreate,
  type MetricAggregation,
} from '@/types'
import type { FactColumnValueKind } from '@/lib/factColumnValueKind'

/** The fact operand shape sent to the backend (numerator / denominator). */
export type FactOperandPayload = NonNullable<FactMetricCreate['numerator']>

export type FactConditionOperator =
  | 'eq'
  | 'ne'
  | 'gt'
  | 'gte'
  | 'lt'
  | 'lte'
  | 'contains'
  | 'not_contains'
  | 'like'
  | 'not_like'
  | 'in'
  | 'not_in'
  | 'is_null'
  | 'is_not_null'
  | 'is_true'
  | 'is_false'

export type FactConditionScalar = string | number | boolean

export interface FactConditionConfig {
  column: string
  operator: FactConditionOperator
  value?: FactConditionScalar | FactConditionScalar[] | null
}

interface ConditionOperatorMeta {
  value: FactConditionOperator
  label: string
  /** Takes no value (`column is null`). */
  valueless: boolean
  /** Takes a list of values (`column in (…)`). */
  list: boolean
  /**
   * Column value kinds the operator makes sense for. `contains` on a number,
   * `is_true` on a string or `>` on a bool is rejected by the backend or the
   * warehouse, and the user used to learn that only from "Check filters".
   * Timestamps bucket as `string` here, so ordering stays offered.
   */
  kinds: readonly FactColumnValueKind[]
}

const ALL_KINDS: readonly FactColumnValueKind[] = ['number', 'boolean', 'string']
const ORDERED_KINDS: readonly FactColumnValueKind[] = ['number', 'string']
const TEXT_KINDS: readonly FactColumnValueKind[] = ['string']
const BOOLEAN_KINDS: readonly FactColumnValueKind[] = ['boolean']

/** Every condition operator the backend accepts, in menu order. */
export const FACT_CONDITION_OPERATORS: readonly ConditionOperatorMeta[] = [
  { value: 'eq', label: '=', valueless: false, list: false, kinds: ALL_KINDS },
  { value: 'ne', label: '!=', valueless: false, list: false, kinds: ALL_KINDS },
  { value: 'gt', label: '>', valueless: false, list: false, kinds: ORDERED_KINDS },
  { value: 'gte', label: '>=', valueless: false, list: false, kinds: ORDERED_KINDS },
  { value: 'lt', label: '<', valueless: false, list: false, kinds: ORDERED_KINDS },
  { value: 'lte', label: '<=', valueless: false, list: false, kinds: ORDERED_KINDS },
  { value: 'contains', label: 'contains', valueless: false, list: false, kinds: TEXT_KINDS },
  { value: 'not_contains', label: 'does not contain', valueless: false, list: false, kinds: TEXT_KINDS },
  { value: 'like', label: 'like', valueless: false, list: false, kinds: TEXT_KINDS },
  { value: 'not_like', label: 'not like', valueless: false, list: false, kinds: TEXT_KINDS },
  { value: 'in', label: 'in', valueless: false, list: true, kinds: ALL_KINDS },
  { value: 'not_in', label: 'not in', valueless: false, list: true, kinds: ALL_KINDS },
  { value: 'is_null', label: 'is null', valueless: true, list: false, kinds: ALL_KINDS },
  { value: 'is_not_null', label: 'is not null', valueless: true, list: false, kinds: ALL_KINDS },
  { value: 'is_true', label: 'is true', valueless: true, list: false, kinds: BOOLEAN_KINDS },
  { value: 'is_false', label: 'is false', valueless: true, list: false, kinds: BOOLEAN_KINDS },
]

const OPERATOR_META = new Map(FACT_CONDITION_OPERATORS.map(meta => [meta.value, meta]))

export const VALUELESS_CONDITION_OPERATORS: ReadonlySet<FactConditionOperator> = new Set(
  FACT_CONDITION_OPERATORS.filter(meta => meta.valueless).map(meta => meta.value),
)

export function isFactConditionOperator(value: unknown): value is FactConditionOperator {
  return typeof value === 'string' && OPERATOR_META.has(value as FactConditionOperator)
}

export function isListConditionOperator(operator: FactConditionOperator): boolean {
  return OPERATOR_META.get(operator)?.list ?? false
}

/** SQL-ish display label; an unknown operator is painted as its raw token. */
export function conditionOperatorLabel(operator: string): string {
  return OPERATOR_META.get(operator as FactConditionOperator)?.label ?? operator
}

/**
 * Operators offered for a column of this kind; `null` (no column picked yet)
 * offers all of them.
 */
export function conditionOperatorsFor(
  kind: FactColumnValueKind | null,
): readonly ConditionOperatorMeta[] {
  if (kind === null) return FACT_CONDITION_OPERATORS
  return FACT_CONDITION_OPERATORS.filter(meta => meta.kinds.includes(kind))
}

export function isMetricAggregation(value: unknown): value is MetricAggregation {
  return METRIC_AGGREGATIONS.includes(value as MetricAggregation)
}

/** A stored fact operand, every field narrowed. */
export interface FactOperandConfig {
  factTableId: string | null
  aggregation: MetricAggregation
  measureColumn: string | null
  distinctColumn: string | null
  /** Named row filters, with a legacy single `row_filter` folded in. */
  rowFilters: string[]
  conditions: FactConditionConfig[]
  filterSql: string | null
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null
}

function isScalar(value: unknown): value is FactConditionScalar {
  return typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'
}

/**
 * Narrow a stored `conditions` array. An entry without a column, or with an
 * operator the backend does not define, is dropped rather than coerced: the
 * form used to rewrite an unknown operator to `=`, which changes what the
 * metric counts on the next save.
 */
export function readConditionsConfig(raw: unknown): FactConditionConfig[] {
  if (!Array.isArray(raw)) return []
  const out: FactConditionConfig[] = []
  for (const entry of raw) {
    if (!entry || typeof entry !== 'object') continue
    const record = entry as Record<string, unknown>
    const column = nonEmptyString(record.column)
    if (!column || !isFactConditionOperator(record.operator)) continue
    const value = record.value
    const condition: FactConditionConfig = { column, operator: record.operator }
    if (Array.isArray(value)) condition.value = value.filter(isScalar)
    else if (isScalar(value)) condition.value = value
    out.push(condition)
  }
  return out
}

/** Named row filters: `row_filters` plus a folded legacy single `row_filter`. */
export function readRowFiltersConfig(record: Record<string, unknown>): string[] {
  const names = Array.isArray(record.row_filters)
    ? record.row_filters.filter((name): name is string => typeof name === 'string' && !!name)
    : []
  const legacy = nonEmptyString(record.row_filter)
  return legacy && !names.includes(legacy) ? [...names, legacy] : names
}

/**
 * Read one fact operand out of untyped config JSON: a ratio's
 * `numerator` / `denominator` block, or the top-level config of a single fact
 * metric (pass the metric's own `fact_table_id` / `aggregation` as
 * `overrides`, since those live on the definition row rather than in config).
 */
export function readFactOperandConfig(
  raw: unknown,
  overrides: { factTableId?: string | null; aggregation?: MetricAggregation | null } = {},
): FactOperandConfig {
  const record = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  const aggregation = overrides.aggregation ?? record.aggregation
  return {
    factTableId:
      overrides.factTableId !== undefined
        ? overrides.factTableId || null
        : nonEmptyString(record.fact_table_id),
    aggregation: isMetricAggregation(aggregation) ? aggregation : 'count',
    measureColumn: nonEmptyString(record.measure_column),
    distinctColumn: nonEmptyString(record.distinct_column),
    rowFilters: readRowFiltersConfig(record),
    conditions: readConditionsConfig(record.conditions),
    filterSql: nonEmptyString(record.filter_sql),
  }
}

/** Serialise a narrowed operand back to the create/update contract. */
export function factOperandConfigToPayload(operand: FactOperandConfig): FactOperandPayload {
  return {
    fact_table_id: operand.factTableId ?? '',
    aggregation: operand.aggregation,
    measure_column: operand.measureColumn,
    distinct_column: operand.distinctColumn,
    row_filters: operand.rowFilters,
    filter_sql: operand.filterSql,
    conditions: operand.conditions.map(condition =>
      condition.value === undefined || condition.value === null
        ? { column: condition.column, operator: condition.operator }
        : { column: condition.column, operator: condition.operator, value: condition.value },
    ),
  }
}
