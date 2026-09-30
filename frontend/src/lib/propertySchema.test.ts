import { describe, expect, it } from 'vitest'
import {
  canonicalSchema,
  defaultSchemaFor,
  describeConstraints,
  nodeOfType,
  pruneSchema,
  rootTypeOptions,
  sameSchema,
  schemaForType,
  schemaMatchesType,
  schemaProblems,
  schemaToSave,
  summariseType,
} from './propertySchema'

describe('the type <-> schema mapping (mirrors core/property_schema.py)', () => {
  it('agrees with the backend on what each type admits', () => {
    expect(schemaMatchesType('number', { type: 'integer' })).toBe(true)
    expect(schemaMatchesType('string', { type: 'string', format: 'date' })).toBe(false)
    expect(schemaMatchesType('date', { type: 'string', format: 'date' })).toBe(true)
    expect(schemaMatchesType('datetime', { type: 'string', format: 'date' })).toBe(false)
    expect(schemaMatchesType('json', { type: 'array' })).toBe(true)
    expect(schemaMatchesType('string_array', { type: 'array', items: { type: 'number' } })).toBe(false)
    expect(schemaMatchesType('number_array', { type: 'array', items: { type: 'integer' } })).toBe(true)
    expect(schemaMatchesType('boolean', null)).toBe(true)
  })

  it('keeps a schema that still agrees after a type change, else starts from the default', () => {
    expect(schemaForType('number', { type: 'integer', minimum: 1 })).toEqual({ type: 'integer', minimum: 1 })
    expect(schemaForType('string', { type: 'integer' })).toEqual({ type: 'string' })
    expect(schemaForType('datetime', null)).toEqual({ type: 'string', format: 'date-time' })
  })

  it('saves nothing for a schema that only restates the type, except for JSON', () => {
    expect(schemaToSave('string', { type: 'string', pattern: '' })).toBeNull()
    expect(schemaToSave('string_array', defaultSchemaFor('string_array'))).toBeNull()
    expect(schemaToSave('json', { type: 'object', properties: {}, required: [] })).toEqual({ type: 'object' })
    expect(schemaToSave('number', { type: 'integer' })).toEqual({ type: 'integer' })
  })

  it('reads a null schema and its default as the same', () => {
    expect(sameSchema('string', null, { type: 'string' })).toBe(true)
    expect(sameSchema('number', { maximum: 3, type: 'number' }, { type: 'number', maximum: 3 })).toBe(true)
    expect(sameSchema('number', null, { type: 'integer' })).toBe(false)
  })
})

describe('summaries', () => {
  it('says the type in a few words, schema first', () => {
    expect(summariseType('number', { type: 'integer' })).toBe('Integer')
    expect(summariseType('string', { type: 'string', format: 'email' })).toBe('String (email)')
    expect(summariseType('number_array', { type: 'array', items: { type: 'integer' } })).toBe('Integer[]')
    expect(summariseType('json', { type: 'array', items: { type: 'object' } })).toBe('Object[]')
    expect(
      summariseType('json', {
        type: 'object',
        properties: { a: { type: 'string' }, b: { type: 'string' }, c: { type: 'string' }, d: { type: 'string' } },
      }),
    ).toBe('Object {a, b, c, +1}')
    expect(summariseType('date', null)).toBe('Date')
    // A stored schema that contradicts the type is not believed.
    expect(summariseType('string', { type: 'integer' })).toBe('String')
  })

  it('names the constraints', () => {
    expect(describeConstraints({ type: 'number', minimum: 0, maximum: 9 })).toEqual(['≥ 0', '≤ 9'])
    expect(describeConstraints({ type: 'object', required: ['id'] })).toEqual(['requires id'])
  })
})

describe('schemaProblems', () => {
  it('names contradicting bounds, a bad regex and a nameless nested property, by path', () => {
    expect(
      schemaProblems({
        type: 'object',
        properties: {
          price: { type: 'number', minimum: 5, maximum: 1 },
          code: { type: 'string', pattern: '(' },
          '': { type: 'string' },
        },
      }),
    ).toEqual([
      'The property: every nested property needs a name.',
      'price: minimum is greater than maximum.',
      'code: the pattern is not a valid regular expression.',
    ])
  })
})

describe('editing helpers', () => {
  it('has a default for every type and matches it', () => {
    for (const type of ['string', 'number', 'boolean', 'date', 'datetime', 'json', 'string_array', 'number_array'] as const) {
      expect(schemaMatchesType(type, defaultSchemaFor(type))).toBe(true)
    }
    expect(schemaMatchesType('boolean', { type: 'string' })).toBe(false)
  })

  it('offers the root types each property type allows', () => {
    expect(rootTypeOptions('number')).toEqual(['number', 'integer'])
    expect(rootTypeOptions('json')).toEqual(['object', 'array'])
    expect(rootTypeOptions('boolean')).toEqual(['boolean'])
    expect(rootTypeOptions('string_array')).toEqual(['array'])
    expect(rootTypeOptions('number_array')).toEqual(['array'])
    expect(rootTypeOptions('date')).toEqual(['string'])
  })

  it('keeps what still applies when a node changes type', () => {
    const same = { type: 'string' as const, pattern: 'x' }
    expect(nodeOfType('string', same)).toBe(same)
    expect(nodeOfType('array', { type: 'string', description: 'd' })).toEqual({
      type: 'array', items: { type: 'string' }, description: 'd',
    })
    expect(nodeOfType('integer', { type: 'number', minimum: 1 })).toEqual({ type: 'integer', minimum: 1 })
    expect(nodeOfType('boolean', { type: 'number', minimum: 1 })).toEqual({ type: 'boolean' })
    expect(nodeOfType('object')).toEqual({ type: 'object' })
  })

  it('prunes empty keys at every depth', () => {
    expect(
      pruneSchema({
        type: 'object',
        required: [],
        properties: {
          list: { type: 'array', items: { type: 'string', pattern: '' } },
          empty: { type: 'object', properties: {} },
        },
      }),
    ).toEqual({
      type: 'object',
      properties: { list: { type: 'array', items: { type: 'string' } }, empty: { type: 'object' } },
    })
  })

  it('writes the same text for the same fragment in any key order', () => {
    expect(canonicalSchema({ b: [1, { d: 2, c: undefined }], a: 'x' })).toBe('{"a":"x","b":[1,{"d":2}]}')
    expect(canonicalSchema({ b: 1, a: 2 })).toBe(canonicalSchema({ a: 2, b: 1 }))
  })

  it('checks every bound pair and the items of an array', () => {
    expect(
      schemaProblems({
        type: 'array', minItems: 3, maxItems: 1, items: { type: 'string', minLength: 4, maxLength: 2 },
      }),
    ).toEqual([
      'The property: minItems is greater than maxItems.',
      '[]: minLength is greater than maxLength.',
    ])
    expect(
      schemaProblems({ type: 'object', properties: { a: { type: 'object', properties: { b: { type: 'string', pattern: '[' } } } } }),
    ).toEqual(['a.b: the pattern is not a valid regular expression.'])
  })

  it('summarises dates, a bare object, arrays without items and plain types', () => {
    expect(summariseType('date', { type: 'string', format: 'date' })).toBe('Date')
    expect(summariseType('datetime', { type: 'string', format: 'date-time' })).toBe('Datetime')
    expect(summariseType('json', { type: 'object' })).toBe('Object')
    expect(summariseType('json', { type: 'array' })).toBe('Any[]')
    expect(summariseType('boolean', { type: 'boolean' })).toBe('Boolean')
  })

  it('names every constraint, and none for no node', () => {
    expect(describeConstraints(null)).toEqual([])
    expect(
      describeConstraints({
        type: 'string', pattern: '^a', minLength: 1, maxLength: 5,
      }),
    ).toEqual(['matches /^a/', 'at least 1 characters', 'at most 5 characters'])
    expect(describeConstraints({ type: 'array', minItems: 1, maxItems: 2 })).toEqual([
      'at least 1 items', 'at most 2 items',
    ])
  })
})
