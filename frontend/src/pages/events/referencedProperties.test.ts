import { describe, expect, it } from 'vitest'
import type { EventFieldValue, Variable } from '@/types'
import { referencedProperties } from './referencedProperties'

function variable(id: string, name: string, extra: Partial<Variable> = {}): Variable {
  return { id, name, source_name: null, bindings: [], ...extra } as Variable
}

function fieldValue(value: string, id = 'fv-1'): EventFieldValue {
  return { id, field_definition_id: `fd-${id}`, value }
}

const VARIABLES = [
  variable('var-spot', 'spot_id', { source_name: 'property.spot_id' }),
  variable('var-type', 'type', { bindings: ['props.kind'] }),
  variable('var-rank', 'details_rank'),
]

const NONE = new Set<string>()

describe('referencedProperties', () => {
  it('resolves a token by name, by source name and by binding', () => {
    const result = referencedProperties(
      [fieldValue('{"spot_id":"${property.spot_id}","type":"${props.kind}","details_rank":"${details_rank}"}')],
      VARIABLES,
      NONE,
    )
    expect(result).toEqual([
      { variableId: 'var-spot', name: 'spot_id', tokens: ['${property.spot_id}'] },
      { variableId: 'var-type', name: 'type', tokens: ['${props.kind}'] },
      { variableId: 'var-rank', name: 'details_rank', tokens: ['${details_rank}'] },
    ])
  })

  it('leaves out a token no property answers to, as the form warns about it', () => {
    expect(referencedProperties([fieldValue('${nope} and ${property.missing}')], VARIABLES, NONE)).toEqual([])
  })

  it('does not read a scan context: a token only its source_column names stays unknown', () => {
    const scanned: EventFieldValue = {
      ...fieldValue('${property.ghost}'),
      variable_values: [
        {
          id: 'vv-1',
          variable_id: 'var-rank',
          variable_name: 'details_rank',
          source_column: 'property.ghost',
          value_kind: 'low',
          observed_count: 3,
          values: ['a'],
        },
      ],
    }
    expect(referencedProperties([scanned], VARIABLES, NONE)).toEqual([])
  })

  it('lists a property named from two fields once, with each distinct token', () => {
    const result = referencedProperties(
      [fieldValue('${spot_id}', 'a'), fieldValue('${property.spot_id} ${spot_id}', 'b')],
      VARIABLES,
      NONE,
    )
    expect(result).toEqual([
      { variableId: 'var-spot', name: 'spot_id', tokens: ['${spot_id}', '${property.spot_id}'] },
    ])
  })

  it('drops properties already on the list', () => {
    const result = referencedProperties(
      [fieldValue('${spot_id} ${type} ${details_rank}')],
      VARIABLES,
      new Set(['var-spot', 'var-rank']),
    )
    expect(result.map(r => r.name)).toEqual(['type'])
  })

  it('keeps the order of first appearance across fields', () => {
    const result = referencedProperties(
      [fieldValue('${details_rank}', 'a'), fieldValue('${spot_id} ${details_rank}', 'b')],
      VARIABLES,
      NONE,
    )
    expect(result.map(r => r.name)).toEqual(['details_rank', 'spot_id'])
  })

  it('returns nothing for values without a token, or with a malformed one', () => {
    expect(referencedProperties([fieldValue('plain text')], VARIABLES, NONE)).toEqual([])
    // The token grammar is ${[^}{]*}: a brace inside is not a token.
    expect(referencedProperties([fieldValue('${spot{id}')], VARIABLES, NONE)).toEqual([])
    expect(referencedProperties([], VARIABLES, NONE)).toEqual([])
  })
})
