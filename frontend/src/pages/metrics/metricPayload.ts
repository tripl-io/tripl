/**
 * Pure mappers from the editor's {@link MetricDraft} to the backend contract.
 *
 * The create body is exactly presentation + definition, so it is built as one
 * spread of the other two. The form used to write each kind's payload twice
 * (create and update) plus an inline copy of the operand mapper, and the copies
 * had drifted: event-type refs were sent on update but never on create (MET-42).
 */

import type {
  MetricCreate,
  MetricDefinitionConfigUpdate,
  MetricDefinitionUpdate,
} from '@/types'
import type { FactTableColumn } from '@/types/factTables'
import type { FactOperandPayload } from '@/lib/factOperandConfig'
import { filtersToPayload } from './factFilters'
import {
  needsDistinct,
  needsMeasure,
  type FactOperandState,
  type MetricDraft,
} from './metricDraft'

/**
 * Columns of each operand's loaded fact table, which decide how a condition's
 * typed value is serialised (`3` vs `'3'`). Omitted while loading.
 */
export interface OperandColumns {
  numerator?: readonly FactTableColumn[]
  denominator?: readonly FactTableColumn[]
}

/**
 * The operand as the API takes it.
 *
 * A column the aggregation does not read is sent as the draft carries it rather
 * than nulled. The form hides that field, so the draft only carries one the
 * stored metric already had — the API accepts, say, a `count` with a
 * `measure_column` — and nulling it made an untouched save change the stored
 * definition, which deletes the metric's history. Changing the
 * aggregation or the fact table in the form clears the column it hid
 * (`withAggregation`, `withFactTable`), so a stale choice is not carried along
 * instead. And a hidden column the loaded fact table no longer has is dropped:
 * the backend would refuse it with a 422 the user could not clear, since the
 * form does not show the field. While the columns are still loading
 * (`conditionColumns` empty) it is kept — nothing says it is gone.
 */
export function toOperandPayload(
  operand: FactOperandState,
  conditionColumns: readonly FactTableColumn[] = [],
): FactOperandPayload {
  return {
    fact_table_id: operand.factTableId,
    aggregation: operand.aggregation,
    measure_column: columnToSend(
      operand.measureColumn,
      needsMeasure(operand.aggregation),
      conditionColumns,
    ),
    distinct_column: columnToSend(
      operand.distinctColumn,
      needsDistinct(operand.aggregation),
      conditionColumns,
    ),
    ...filtersToPayload(operand.filters, conditionColumns),
  }
}

/** A column as sent: a visible one verbatim, a hidden one only while it still exists. */
function columnToSend(
  column: string,
  shown: boolean,
  tableColumns: readonly FactTableColumn[],
): string | null {
  if (!column) return null
  if (shown || tableColumns.length === 0) return column
  return tableColumns.some(candidate => candidate.name === column) ? column : null
}

/** The column fields `aggregation` does not read, cleared: the form hides them. */
function withoutHiddenColumns(operand: FactOperandState): FactOperandState {
  return {
    ...operand,
    measureColumn: needsMeasure(operand.aggregation) ? operand.measureColumn : '',
    distinctColumn: needsDistinct(operand.aggregation) ? operand.distinctColumn : '',
  }
}

/**
 * `operand` pointed at another fact table: a column the form hides belonged to
 * the old table, and the user could neither see nor clear it on the new one.
 */
export function withFactTable(operand: FactOperandState, factTableId: string): FactOperandState {
  if (factTableId === operand.factTableId) return operand
  return withoutHiddenColumns({ ...operand, factTableId })
}

/**
 * `operand` with a new aggregation chosen in the form: the column fields the
 * new aggregation does not read are cleared, since the form stops showing them.
 */
export function withAggregation(
  operand: FactOperandState,
  aggregation: FactOperandState['aggregation'],
): FactOperandState {
  if (aggregation === operand.aggregation) return operand
  return withoutHiddenColumns({ ...operand, aggregation })
}

/** The `definition` block: kind + collection config, create and update alike. */
export function buildDefinitionPayload(
  draft: MetricDraft,
  columns: OperandColumns = {},
): MetricDefinitionConfigUpdate {
  const { kind } = draft
  if (kind === 'sql') {
    return {
      kind: 'sql',
      interval: draft.interval,
      data_source_id: draft.dataSourceId,
      config: {
        metric_sql: draft.metricSql,
        time_column: draft.sqlTimeColumn.trim(),
        value_column: draft.sqlValueColumn.trim() || null,
      },
      replay_chunk_interval: draft.replayChunkInterval,
    }
  }
  if (kind === 'fact') {
    if (draft.factComposition === 'ratio') {
      return {
        kind: 'fact',
        composition: 'ratio',
        interval: draft.interval,
        numerator: toOperandPayload(draft.numeratorOp, columns.numerator),
        denominator: toOperandPayload(draft.denominatorOp, columns.denominator),
        replay_chunk_interval: draft.replayChunkInterval,
      }
    }
    // A single fact metric carries its one operand at the top level.
    return {
      kind: 'fact',
      composition: 'single',
      interval: draft.interval,
      replay_chunk_interval: draft.replayChunkInterval,
      ...toOperandPayload(draft.numeratorOp, columns.numerator),
    }
  }
  if (kind === 'event_composition') {
    const isRatio = draft.composition === 'ratio'
    return {
      kind: 'event_composition',
      composition: draft.composition,
      numerator_event_id: draft.numeratorEventId || null,
      numerator_event_type_id: draft.numeratorEventId ? null : draft.numeratorEventTypeId || null,
      denominator_event_id: isRatio ? draft.denominatorEventId || null : null,
      denominator_event_type_id:
        isRatio && !draft.denominatorEventId ? draft.denominatorEventTypeId || null : null,
      user_id_column:
        draft.composition === 'per_distinct_user' ? draft.userIdColumn.trim() || null : null,
    }
  }
  // Exhaustiveness guard: a future MetricKind must add a branch above. Missing
  // one fails at compile time (the `never` assignment) and loudly at runtime,
  // rather than silently producing a wrong-kind payload.
  const _exhaustive: never = kind
  throw new Error(`unsupported metric kind: ${String(_exhaustive)}`)
}

/** Presentation, lifecycle and dimension fields — everything but the definition. */
export function buildPresentationPayload(draft: MetricDraft) {
  return {
    display_name: draft.displayName.trim(),
    description: draft.description,
    status: draft.status,
    unit: draft.unit.trim() || null,
    color: draft.color,
    anomaly_detection_enabled: draft.anomalyDetection,
    // Chips are trimmed/deduped as they are added, so the state array is
    // already the exact string[] the payload expects.
    breakdown_columns: draft.breakdownColumns,
    app_version_column: draft.appVersionColumn.trim() || null,
    platform_column: draft.platformColumn.trim() || null,
  }
}

export function buildCreatePayload(
  draft: MetricDraft,
  columns: OperandColumns = {},
  reviewed = false,
): MetricCreate {
  return {
    ...buildPresentationPayload(draft),
    name: draft.name.trim(),
    reviewed,
    ...buildDefinitionPayload(draft, columns),
  }
}

export function buildUpdatePayload(
  draft: MetricDraft,
  columns: OperandColumns = {},
): MetricDefinitionUpdate {
  return {
    ...buildPresentationPayload(draft),
    definition: buildDefinitionPayload(draft, columns),
  }
}
