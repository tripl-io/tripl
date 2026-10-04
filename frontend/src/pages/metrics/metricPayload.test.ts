import { describe, expect, it } from 'vitest'
import { makeConditionFilter, makeNamedFilter } from './factFilters'
import {
  columnsOfReferencedTables,
  draftFromMetric,
  validateDraft,
  type MetricDraft,
} from './metricDraft'
import {
  buildCreatePayload,
  buildDefinitionPayload,
  buildUpdatePayload,
} from './metricPayload'

const base = (patch: Partial<MetricDraft>): MetricDraft => ({
  ...draftFromMetric(null),
  displayName: 'Signups',
  name: 'signups',
  ...patch,
})

describe('buildCreatePayload', () => {
  it('is presentation plus the definition, so create and update cannot drift', () => {
    const draft = base({
      kind: 'event_composition',
      composition: 'ratio',
      numeratorEventTypeId: 'type-1',
      denominatorEventId: 'ev-2',
    })
    const create = buildCreatePayload(draft)
    const update = buildUpdatePayload(draft)
    // Event-type refs used to be sent on update only.
    expect(create).toMatchObject({
      kind: 'event_composition',
      numerator_event_id: null,
      numerator_event_type_id: 'type-1',
      denominator_event_id: 'ev-2',
      denominator_event_type_id: null,
      name: 'signups',
      reviewed: false,
    })
    expect(update.definition).toEqual(buildDefinitionPayload(draft))
    const { definition, ...presentation } = update
    expect(create).toEqual({ ...presentation, name: 'signups', reviewed: false, ...definition })
  })

  it('types condition values from the loaded columns', () => {
    const draft = base({
      kind: 'fact',
      numeratorOp: {
        factTableId: 'ft-1',
        aggregation: 'count',
        measureColumn: '',
        distinctColumn: '',
        filters: [makeConditionFilter('amount', 'gt', '3')],
      },
    })
    expect(buildDefinitionPayload(draft, { numerator: [{ name: 'amount', type: 'number' }] }))
      .toMatchObject({ conditions: [{ column: 'amount', operator: 'gt', value: 3 }] })
  })
})

describe('validateDraft', () => {
  it('blocks a fact operand with an incomplete filter row', () => {
    const named = makeNamedFilter()
    const errors = validateDraft(
      base({
        kind: 'fact',
        numeratorOp: {
          factTableId: 'ft-1',
          aggregation: 'count',
          measureColumn: '',
          distinctColumn: '',
          filters: [named],
        },
      }),
      true,
    )
    expect(errors).toEqual({
      [`metric-fact-filter-${named.id}`]: 'Filter 1: Pick a named filter, or remove this row.',
    })
  })
})

describe('columnsOfReferencedTables', () => {
  const tables = [
    { name: 'events', columns: [{ name: 'bucket' }, { name: 'platform' }] },
    { name: 'events_daily', columns: [{ name: 'day' }] },
    { name: 'analytics.orders', columns: [{ name: 'amount' }, { name: 'platform' }] },
    { name: 'users', columns: [{ name: 'email' }] },
  ]

  it('offers only the columns of tables the query names', () => {
    expect(columnsOfReferencedTables(tables, 'SELECT * FROM events e JOIN analytics.orders o ON 1=1'))
      .toEqual(['bucket', 'platform', 'amount'])
    expect(columnsOfReferencedTables(tables, '')).toEqual([])
  })

  it('caps the list', () => {
    expect(columnsOfReferencedTables(tables, 'from events, orders', 2)).toEqual(['bucket', 'platform'])
  })
})
