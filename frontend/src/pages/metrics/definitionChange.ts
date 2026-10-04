/**
 * Whether saving a definition payload would make the backend delete a metric's
 * collected history.
 *
 * The backend (`_definition_values_changed` in metric_definition_service.py)
 * re-reads the STORED definition through the same model an incoming one goes
 * through, takes `to_definition_values()` of both, and compares them after
 * turning enums and UUIDs into strings and sorting dict keys. Any difference
 * deletes the metric's values, breakdowns and anomalies.
 *
 * So this compares the payload the form is about to send with the definition
 * stored on the metric, both reduced the way the backend reduces them. It used
 * to compare two drafts hydrated from the same metric, which cannot see a
 * lossy load/save round trip: a stored `filter_sql` or condition that loading
 * rewrote was a history wipe with no warning.
 *
 * Where the backend would reject the stored row outright (an unknown condition
 * operator, a key its model forbids) it compares raw columns instead, which
 * all but always differ. The reduction here keeps such content as it is, so
 * the answer errs towards "changed": a needless warning, never a missed one.
 */

import type { MetricDefinitionConfigUpdate, MetricDefinitionDetailResponse } from '@/types'
import { VALUELESS_CONDITION_OPERATORS } from '@/lib/factOperandConfig'

type JsonRecord = Record<string, unknown>

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as JsonRecord) : {}
}

/** Keys of `record` outside `known`, kept so a stray stored key reads as a change. */
function extraKeys(record: JsonRecord, known: readonly string[]): JsonRecord {
  return Object.fromEntries(Object.entries(record).filter(([key]) => !known.includes(key)))
}

/** `_fold_row_filters`: `row_filters` plus a legacy `row_filter`, deduped in order. */
function foldRowFilters(rowFilters: unknown, rowFilter: unknown): unknown {
  if (rowFilters !== undefined && rowFilters !== null && !Array.isArray(rowFilters)) {
    return rowFilters
  }
  const names: unknown[] = []
  for (const name of [...(rowFilters ?? []) as unknown[], ...(rowFilter ? [rowFilter] : [])]) {
    if (!names.includes(name)) names.push(name)
  }
  return names
}

/** `filter_sql` is stored stripped of surrounding whitespace. */
function normaliseFilterSql(value: unknown): unknown {
  return typeof value === 'string' ? value.trim() : (value ?? null)
}

/** `FactCondition.to_config`: `None` dropped, no `value` on a valueless operator. */
function normaliseConditions(value: unknown): unknown {
  if (!Array.isArray(value)) return value ?? []
  return value.map(entry => {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return entry
    const { value: operand, ...rest } = entry as JsonRecord
    const valueless = VALUELESS_CONDITION_OPERATORS.has(rest.operator as never)
    return valueless || operand === undefined || operand === null ? rest : { ...rest, value: operand }
  })
}

const OPERAND_KEYS = [
  'fact_table_id',
  'aggregation',
  'measure_column',
  'distinct_column',
  'row_filter',
  'row_filters',
  'filter_sql',
  'conditions',
] as const

/** The filter/column part of a fact operand's config, as `to_config` writes it. */
function operandFilterConfig(operand: JsonRecord): JsonRecord {
  return {
    measure_column: operand.measure_column ?? null,
    distinct_column: operand.distinct_column ?? null,
    row_filters: foldRowFilters(operand.row_filters, operand.row_filter),
    filter_sql: normaliseFilterSql(operand.filter_sql),
    conditions: normaliseConditions(operand.conditions),
  }
}

/** `FactOperand.to_config` for a ratio side. */
function ratioOperandConfig(raw: unknown): JsonRecord {
  const operand = asRecord(raw)
  return {
    ...extraKeys(operand, OPERAND_KEYS),
    fact_table_id: operand.fact_table_id ?? null,
    aggregation: operand.aggregation ?? null,
    ...operandFilterConfig(operand),
  }
}

const NO_REFS = {
  numerator_event_id: null,
  numerator_event_type_id: null,
  denominator_event_id: null,
  denominator_event_type_id: null,
}

/**
 * `to_definition_values()` of a definition in the create/update shape: every
 * definition column, defaults filled in. Works on either side of the
 * comparison, so it reads fields loosely.
 */
export function definitionValues(definition: JsonRecord): JsonRecord {
  const kind = definition.kind
  if (kind === 'fact') {
    const composition = definition.composition ?? 'single'
    const common = {
      kind,
      composition,
      data_source_id: null,
      interval: definition.interval ?? null,
      replay_chunk_interval: definition.replay_chunk_interval ?? null,
      ...NO_REFS,
    }
    if (composition === 'ratio') {
      const numerator = ratioOperandConfig(definition.numerator)
      return {
        ...common,
        fact_table_id: numerator.fact_table_id,
        aggregation: numerator.aggregation,
        config: { numerator, denominator: ratioOperandConfig(definition.denominator) },
      }
    }
    return {
      ...common,
      fact_table_id: definition.fact_table_id ?? null,
      aggregation: definition.aggregation ?? null,
      config: operandFilterConfig(definition),
    }
  }
  if (kind === 'sql') {
    const config = asRecord(definition.config)
    return {
      kind,
      aggregation: null,
      composition: null,
      config: {
        ...extraKeys(config, ['metric_sql', 'time_column', 'value_column']),
        metric_sql: config.metric_sql ?? null,
        time_column: config.time_column ?? null,
        value_column: config.value_column ?? null,
      },
      fact_table_id: null,
      data_source_id: definition.data_source_id ?? null,
      interval: definition.interval ?? null,
      replay_chunk_interval: definition.replay_chunk_interval ?? null,
      ...NO_REFS,
    }
  }
  const userIdColumn = definition.user_id_column ?? null
  return {
    kind,
    aggregation: null,
    composition: definition.composition ?? null,
    config: userIdColumn === null ? {} : { user_id_column: userIdColumn },
    fact_table_id: null,
    data_source_id: null,
    interval: null,
    replay_chunk_interval: null,
    numerator_event_id: definition.numerator_event_id ?? null,
    numerator_event_type_id: definition.numerator_event_type_id ?? null,
    denominator_event_id: definition.denominator_event_id ?? null,
    denominator_event_type_id: definition.denominator_event_type_id ?? null,
  }
}

/**
 * The stored definition in the create/update shape, the way
 * `_stored_definition_values` feeds it back through the schema: the columns of
 * the row plus the keys each kind reads out of `config`.
 */
export function storedDefinition(metric: MetricDefinitionDetailResponse): JsonRecord {
  const config = asRecord(metric.config)
  if (metric.kind === 'fact') {
    const composition = metric.composition ?? 'single'
    const common = {
      kind: metric.kind,
      composition,
      interval: metric.interval,
      replay_chunk_interval: metric.replay_chunk_interval,
    }
    if (composition === 'ratio') {
      return { ...common, numerator: config.numerator, denominator: config.denominator }
    }
    return {
      ...common,
      fact_table_id: metric.fact_table_id,
      aggregation: metric.aggregation,
      measure_column: config.measure_column,
      distinct_column: config.distinct_column,
      row_filter: config.row_filter,
      row_filters: config.row_filters,
      filter_sql: config.filter_sql,
      conditions: config.conditions,
    }
  }
  if (metric.kind === 'sql') {
    return {
      kind: metric.kind,
      interval: metric.interval,
      replay_chunk_interval: metric.replay_chunk_interval,
      config,
      data_source_id: metric.data_source_id,
    }
  }
  return {
    kind: metric.kind,
    composition: metric.composition,
    numerator_event_id: metric.numerator_event_id,
    numerator_event_type_id: metric.numerator_event_type_id,
    denominator_event_id: metric.denominator_event_id,
    denominator_event_type_id: metric.denominator_event_type_id,
    user_id_column: config.user_id_column,
  }
}

/** JSON with every object's keys sorted, so key order never counts as a change. */
function canonicalJson(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) =>
    item && typeof item === 'object' && !Array.isArray(item)
      ? Object.fromEntries(
          Object.entries(item as JsonRecord).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)),
        )
      : item,
  )
}

/**
 * True when saving `definition` would change what `metric` has stored — and so
 * delete its collected values, breakdowns and anomalies.
 */
export function definitionDiffersFromStored(
  metric: MetricDefinitionDetailResponse,
  definition: MetricDefinitionConfigUpdate,
): boolean {
  return (
    canonicalJson(definitionValues(storedDefinition(metric)))
    !== canonicalJson(definitionValues(definition as unknown as JsonRecord))
  )
}
