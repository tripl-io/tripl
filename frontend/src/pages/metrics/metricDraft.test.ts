import { describe, expect, it } from 'vitest'
import { operandErrors, type FactOperandState } from './metricDraft'

const EMPTY_OPERAND: FactOperandState = {
  factTableId: '',
  aggregation: 'count',
  measureColumn: '',
  distinctColumn: '',
  filters: [],
}

describe('operandErrors', () => {
  it('names the aggregation the way the select shows it, not by its wire code', () => {
    const errors = operandErrors(
      { ...EMPTY_OPERAND, factTableId: 'ft-1', aggregation: 'avg' },
      'metric-fact',
      '',
    )
    expect(errors['metric-fact-measure']).toBe('A measure column is required for Average.')
  })

  it('says "Count distinct" for a missing distinct column, with the ratio side', () => {
    const errors = operandErrors(
      { ...EMPTY_OPERAND, factTableId: 'ft-1', aggregation: 'count_distinct' },
      'metric-num',
      'numerator',
    )
    expect(errors['metric-num-distinct']).toBe(
      'A numerator distinct column is required for Count distinct.',
    )
  })
})
