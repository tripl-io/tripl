import { describe, expect, it } from 'vitest'
import {
  conditionOperatorLabel,
  conditionOperatorsFor,
  factOperandConfigToPayload,
  readFactOperandConfig,
} from './factOperandConfig'

describe('readFactOperandConfig', () => {
  it('narrows every field of untrusted config JSON', () => {
    expect(
      readFactOperandConfig({
        fact_table_id: 'ft-1',
        aggregation: 'median', // not a MetricAggregation
        measure_column: 42,
        row_filters: ['a', 7, '', 'b'],
        conditions: [
          { column: 'amount', operator: 'gt', value: 3 },
          { column: 'plan', operator: 'in', value: ['pro', { nested: true }] },
          { column: 'x', operator: 'approximately', value: 1 }, // unknown operator
          { operator: 'eq', value: 'no column' },
          'garbage',
        ],
        filter_sql: '',
      }),
    ).toEqual({
      factTableId: 'ft-1',
      aggregation: 'count',
      measureColumn: null,
      distinctColumn: null,
      rowFilters: ['a', 'b'],
      conditions: [
        { column: 'amount', operator: 'gt', value: 3 },
        { column: 'plan', operator: 'in', value: ['pro'] },
      ],
      filterSql: null,
    })
  })

  it('folds a legacy single row_filter into the named filters', () => {
    expect(readFactOperandConfig({ row_filter: 'legacy' }).rowFilters).toEqual(['legacy'])
    expect(readFactOperandConfig({ row_filters: ['legacy'], row_filter: 'legacy' }).rowFilters)
      .toEqual(['legacy'])
  })

  it('takes a single metric’s table and aggregation from the definition row', () => {
    const operand = readFactOperandConfig(
      { measure_column: 'amount' },
      { factTableId: 'ft-9', aggregation: 'sum' },
    )
    expect(operand).toMatchObject({ factTableId: 'ft-9', aggregation: 'sum', measureColumn: 'amount' })
  })

  it('serialises back to the operand contract', () => {
    const payload = factOperandConfigToPayload(
      readFactOperandConfig({
        fact_table_id: 'ft-1',
        aggregation: 'count_distinct',
        distinct_column: 'user_id',
        row_filter: 'completed',
        conditions: [{ column: 'user_id', operator: 'is_not_null', value: null }],
      }),
    )
    expect(payload).toEqual({
      fact_table_id: 'ft-1',
      aggregation: 'count_distinct',
      measure_column: null,
      distinct_column: 'user_id',
      row_filters: ['completed'],
      filter_sql: null,
      conditions: [{ column: 'user_id', operator: 'is_not_null' }],
    })
  })
})

describe('condition operator metadata', () => {
  it('narrows operators by column value kind', () => {
    const labels = (kind: Parameters<typeof conditionOperatorsFor>[0]) =>
      conditionOperatorsFor(kind).map(meta => meta.value)
    expect(labels('number')).not.toContain('contains')
    expect(labels('number')).not.toContain('is_true')
    expect(labels('boolean')).toEqual(['eq', 'ne', 'in', 'not_in', 'is_null', 'is_not_null', 'is_true', 'is_false'])
    expect(labels('string')).toContain('like')
    expect(labels(null)).toHaveLength(16)
  })

  it('labels known operators and passes unknown ones through', () => {
    expect(conditionOperatorLabel('gte')).toBe('>=')
    expect(conditionOperatorLabel('mystery')).toBe('mystery')
  })
})
