import { describe, expect, it } from 'vitest'
import { relaxedToJson } from './jsonRelaxed'

const KNOWN = ['variant', 'mode']

function repaired(input: string, known: string[] = KNOWN): string | null {
  const result = relaxedToJson(input, known)
  return result ? result.text : null
}

describe('relaxedToJson', () => {
  it('wraps bare key: value pairs and reads dotted values as properties', () => {
    expect(repaired('from_profile: property.forecast_profile, mode: property.mode')).toBe(
      '{"from_profile":"${property.forecast_profile}","mode":"${property.mode}"}',
    )
  })

  it('treats a newline as a separator', () => {
    expect(repaired('a: 1\nb: 2')).toBe('{"a":1,"b":2}')
  })

  it('quotes unquoted keys inside braces the author did write', () => {
    expect(repaired('{spot_id: 12, live: true}')).toBe('{"spot_id":12,"live":true}')
  })

  it('accepts single quotes and drops a trailing comma', () => {
    expect(repaired("{'name': 'Aalter', 'n': 3,}")).toBe('{"name":"Aalter","n":3}')
  })

  it('closes a brace the author left open', () => {
    const result = relaxedToJson('{spot_id: 12', KNOWN)
    expect(result?.text).toBe('{"spot_id":12}')
    expect(result?.fixes).toContain('closed a missing brace')
  })

  it('normalises a bare ${token} value to the quoted form while repairing its neighbours', () => {
    expect(repaired('{a: ${variant}}')).toBe('{"a":"${variant}"}')
  })

  it('stands aside when only the bare token is unusual: the strict formatter already handles that', () => {
    expect(relaxedToJson('{"a": ${variant}}', KNOWN)).toBeNull()
  })

  it('leaves a single bare word as a string unless the project knows the name', () => {
    expect(repaired('theme: dark')).toBe('{"theme":"dark"}')
    expect(repaired('theme: variant')).toBe('{"theme":"${variant}"}')
  })

  it('handles nested objects and arrays', () => {
    expect(repaired('{outer: {inner: property.x}, list: [1, two, property.z]}')).toBe(
      '{"outer":{"inner":"${property.x}"},"list":[1,"two","${property.z}"]}',
    )
  })

  it('reports every repair it made', () => {
    const result = relaxedToJson("from_profile: property.forecast_profile, label: 'go'", KNOWN)
    expect(result?.fixes).toEqual([
      'wrapped it in { }',
      'quoted 2 keys',
      'switched single quotes to double',
      'read 1 value as a property',
    ])
  })

  it('returns null when there was nothing to repair, so strict output is never rewritten', () => {
    expect(relaxedToJson('{"a": 1}', KNOWN)).toBeNull()
  })

  it('returns null on input it cannot make sense of', () => {
    expect(relaxedToJson('', KNOWN)).toBeNull()
    expect(relaxedToJson('just some prose', KNOWN)).toBeNull()
    expect(relaxedToJson('{a: }', KNOWN)).toBeNull()
    expect(relaxedToJson('}{', KNOWN)).toBeNull()
  })

  it('produces output that strict JSON.parse accepts once the templates are stubbed', () => {
    const result = relaxedToJson('a: property.x, b: [1, 2], c: "plain"', KNOWN)
    expect(result).not.toBeNull()
    const stubbed = result!.text.replace(/"\$\{[^}]*\}"/g, '"__var__"')
    expect(() => JSON.parse(stubbed)).not.toThrow()
  })
})
