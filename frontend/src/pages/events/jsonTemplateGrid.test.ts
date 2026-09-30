import { describe, expect, it } from 'vitest'
import { validateJsonWithVars } from './jsonTemplate'
import {
  cellText,
  cellToRaw,
  parseTemplateRows,
  rowProblem,
  serializeTemplateRows,
  tokenOf,
} from './jsonTemplateGrid'

describe('parseTemplateRows', () => {
  it('reads a flat object of references and literals, keeping each value as written', () => {
    const rows = parseTemplateRows('{"currency":"${currency}","price":${price},"source":"cta","n":1}')
    expect(rows).toEqual([
      { key: 'currency', raw: '"${currency}"' },
      { key: 'price', raw: '${price}' },
      { key: 'source', raw: '"cta"' },
      { key: 'n', raw: '1' },
    ])
  })

  it('keeps tokens inside a nested value', () => {
    const [row] = parseTemplateRows('{"cart":{"id":"${cart_id}"}}')!
    expect(row!.raw).toBe('{\n  "id": "${cart_id}"\n}')
  })

  it('reads an empty value as no rows, and refuses what is not one object', () => {
    expect(parseTemplateRows('')).toEqual([])
    expect(parseTemplateRows('[1, 2]')).toBeNull()
    expect(parseTemplateRows('42')).toBeNull()
    expect(parseTemplateRows('{"a":')).toBeNull()
    // Two equal keys: the grid would silently drop one.
    expect(parseTemplateRows('{"a":1,"a":2}')).toBeNull()
    // Also beside a nested object, whose own keys are not the top level's.
    expect(parseTemplateRows('{"a":1,"cart":{"id":"x","n":2},"a":2}')).toBeNull()
    expect(parseTemplateRows('{"a":"x:\\"y","cart":[{"id":1}]}')).toHaveLength(2)
  })
})

describe('serializeTemplateRows', () => {
  it('round-trips through the parser and stays a valid template', () => {
    const text = '{"currency":"${currency}","price":${price},"cart":{"id":"${cart_id}"},"ok":true}'
    const rows = parseTemplateRows(text)!
    const out = serializeTemplateRows(rows)
    expect(validateJsonWithVars(out)).toBeNull()
    expect(parseTemplateRows(out)).toEqual(rows)
    expect(out).toContain('"price": ${price}')
  })

  it('writes nothing for no rows, so an emptied grid clears the field', () => {
    expect(serializeTemplateRows([])).toBe('')
  })
})

describe('value cells', () => {
  it('shows a reference as ${name} and writes it quoted', () => {
    expect(cellText('"${currency}"')).toBe('${currency}')
    expect(cellText('${price}')).toBe('${price}')
    expect(cellToRaw('${currency}')).toBe('"${currency}"')
    expect(tokenOf('"${a.b}"')).toBe('a.b')
  })

  it('reads JSON as JSON and anything else as a string', () => {
    expect(cellToRaw('42')).toBe('42')
    expect(cellToRaw('true')).toBe('true')
    expect(cellToRaw('{"a":1}')).toBe('{"a":1}')
    expect(cellToRaw('checkout')).toBe('"checkout"')
    expect(cellToRaw('1.')).toBe('"1."')
    expect(cellToRaw('')).toBe('""')
  })

  it('quotes a string only where bare text would read back as something else', () => {
    expect(cellText('"checkout"')).toBe('checkout')
    expect(cellText('"42"')).toBe('"42"')
    expect(cellText('"true"')).toBe('"true"')
    for (const typed of ['checkout', '42', '"42"', 'with space ', '${x}', '-']) {
      expect(cellText(cellToRaw(typed))).toBe(typed)
    }
  })
})

describe('rowProblem', () => {
  it('names an empty or repeated key', () => {
    const rows = [{ key: 'a', raw: '1' }, { key: '', raw: '2' }, { key: 'a', raw: '3' }]
    expect(rowProblem(rows, 0)).toBeNull()
    expect(rowProblem(rows, 1)).toBe('Name the key.')
    expect(rowProblem(rows, 2)).toBe('a is already a key.')
  })
})
