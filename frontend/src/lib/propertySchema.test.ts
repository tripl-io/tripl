import { describe, expect, it } from 'vitest'
import {
  defaultSchemaFor,
  describeConstraints,
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
