import { describe, expect, it } from 'vitest'
import { invalidValuesFor, splitValueList, valueRuleFor } from './variableValueValidation'

describe('variableValueValidation (PLAN-24)', () => {
  it('checks each typed value against its type', () => {
    expect(invalidValuesFor('number', ['1', '2.5', 'abc', ''])).toEqual(['abc', ''])
    expect(invalidValuesFor('boolean', ['true', 'false', 'yes'])).toEqual(['yes'])
    expect(invalidValuesFor('date', ['2026-09-25', '2026-02-30', 'tomorrow'])).toEqual(['2026-02-30', 'tomorrow'])
    expect(invalidValuesFor('datetime', ['2026-09-25T14:30:00Z', '2026-09-25 14:30', 'noon'])).toEqual(['noon'])
    expect(invalidValuesFor('json', ['{"a": 1}', '"text"', '{a}'])).toEqual(['{a}'])
    expect(invalidValuesFor('number_array', ['3', 'x'])).toEqual(['x'])
  })

  it('lets any value through for string types', () => {
    expect(invalidValuesFor('string', ['anything'])).toEqual([])
    expect(invalidValuesFor('string_array', ['anything'])).toEqual([])
    expect(valueRuleFor('string')).toEqual({})
    expect(valueRuleFor('number').validate?.('7')).toBe(true)
  })
})

describe('splitValueList', () => {
  it('still splits plain scalars on commas, trimming and dropping blanks', () => {
    expect(splitValueList('a, b,c')).toEqual(['a', 'b', 'c'])
    expect(splitValueList(' 1 ,, 2 , ')).toEqual(['1', '2'])
  })

  it('keeps a JSON object or array with commas in it whole', () => {
    expect(splitValueList('{"a": 1, "b": [2, 3]}')).toEqual(['{"a": 1, "b": [2, 3]}'])
    expect(splitValueList('[1, 2], {"k": "x,y"}, plain')).toEqual(['[1, 2]', '{"k": "x,y"}', 'plain'])
  })

  it('keeps a quoted string whole, escaped quotes included', () => {
    expect(splitValueList('"a, b", "say \\"hi, there\\""')).toEqual(['"a, b"', '"say \\"hi, there\\""'])
  })

  it('does not let a stray closer swallow later commas', () => {
    expect(splitValueList('a], b')).toEqual(['a]', 'b'])
  })
})
