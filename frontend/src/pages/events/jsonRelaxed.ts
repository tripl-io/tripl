/**
 * A tolerant reader for the JSON an analyst actually types into a property
 * field, used only after strict `JSON.parse` has already refused.
 *
 * It never runs on valid JSON, so it cannot change the meaning of anything that
 * already parses. What it accepts, in the order people hit it: a missing outer
 * `{}`, unquoted keys, single-quoted strings, trailing commas, a newline where
 * a comma belongs, an unclosed brace at the end, and bare `${token}` values —
 * which are a syntax error to every off-the-shelf JSON or JSON5 parser but are
 * legal here.
 *
 * A bare dotted value becomes a variable template, not a literal string:
 * `from_profile: property.forecast_profile` reads as
 * `{"from_profile": "${property.forecast_profile}"}`. That is what the product
 * itself writes — `build_json_value` defaults every kept path to
 * `${column.path}` — and it is the reading the analyst who asked for this
 * confirmed. It is a guess, so every repair is reported and undoable.
 */

import { BARE_WORD_VARIABLE_PATTERN } from './jsonTemplate'

const NUMBER_PATTERN = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/
const VALUE_TERMINATORS = new Set([',', '}', ']', '\n'])

export interface RelaxedResult {
  /** Strict JSON, with `${token}` templates kept as quoted values. */
  text: string
  /** Human-readable list of what was changed, for the editor to show. */
  fixes: string[]
}

type Node =
  | { kind: 'raw'; text: string }
  | { kind: 'string'; value: string }
  | { kind: 'template'; token: string }
  | { kind: 'array'; items: Node[] }
  | { kind: 'object'; entries: { key: string; value: Node }[] }

interface Repairs {
  wrapped: boolean
  closed: boolean
  keysQuoted: number
  valuesQuoted: number
  singleQuotes: number
  trailingCommas: number
  templates: number
}

class RelaxedParser {
  private i = 0

  private readonly src: string
  private readonly known: ReadonlySet<string>
  private readonly repairs: Repairs

  constructor(src: string, known: ReadonlySet<string>, repairs: Repairs) {
    this.src = src
    this.known = known
    this.repairs = repairs
  }

  get done(): boolean {
    return this.i >= this.src.length
  }

  skipBlank(): void {
    while (this.i < this.src.length && /\s/.test(this.src.charAt(this.i))) this.i += 1
  }

  /** Skip whitespace and any run of separators, counting the ones that were redundant. */
  skipSeparators(): void {
    let seen = 0
    for (;;) {
      this.skipBlank()
      if (this.src[this.i] !== ',') break
      seen += 1
      this.i += 1
    }
    if (seen > 1) this.repairs.trailingCommas += seen - 1
  }

  peek(): string | undefined {
    return this.src[this.i]
  }

  parseQuoted(quote: string): string {
    this.i += 1 // opening quote
    let out = ''
    while (this.i < this.src.length) {
      const ch = this.src[this.i]
      if (ch === '\\') {
        const next = this.src[this.i + 1]
        if (next === undefined) break
        // A backslash before the *other* quote character is an escape the
        // author added for their quote style; strict JSON does not want it.
        out += next === quote && quote === "'" ? "'" : `\\${next}`
        this.i += 2
        continue
      }
      if (ch === quote) {
        this.i += 1
        if (quote === "'") this.repairs.singleQuotes += 1
        return out
      }
      out += ch
      this.i += 1
    }
    throw new Error('unterminated string')
  }

  /** Read an unquoted run up to the next structural character. */
  readBare(stopAtColon: boolean): string {
    const start = this.i
    while (this.i < this.src.length) {
      const ch = this.src.charAt(this.i)
      // A ${token} closes with the same brace that ends an object, so step
      // over it whole or every bare template would be cut in half.
      if (ch === '$' && this.src[this.i + 1] === '{') {
        const close = this.src.indexOf('}', this.i + 2)
        if (close !== -1) {
          this.i = close + 1
          continue
        }
      }
      if (VALUE_TERMINATORS.has(ch)) break
      if (stopAtColon && ch === ':') break
      this.i += 1
    }
    return this.src.slice(start, this.i).trim()
  }

  classifyBare(text: string): Node {
    if (text === 'true' || text === 'false' || text === 'null') return { kind: 'raw', text }
    if (NUMBER_PATTERN.test(text)) return { kind: 'raw', text }
    const template = /^\$\{([^}]*)\}$/.exec(text)
    if (template) return { kind: 'template', token: template[1] ?? '' }
    // Only a dotted path or a name the project already knows is read as a
    // variable. A bare single word stays a string, so `mode: dark` is not
    // silently turned into a reference to something that does not exist.
    if (BARE_WORD_VARIABLE_PATTERN.test(text) && (text.includes('.') || this.known.has(text))) {
      this.repairs.templates += 1
      return { kind: 'template', token: text }
    }
    this.repairs.valuesQuoted += 1
    return { kind: 'string', value: text }
  }

  parseValue(): Node {
    this.skipBlank()
    const ch = this.peek()
    if (ch === undefined) throw new Error('unexpected end of input')
    if (ch === '{') return this.parseObject(true)
    if (ch === '[') return this.parseArray()
    if (ch === '"' || ch === "'") {
      const value = this.parseQuoted(ch)
      const template = /^\$\{([^}]*)\}$/.exec(value)
      return template ? { kind: 'template', token: template[1] ?? '' } : { kind: 'string', value }
    }
    const bare = this.readBare(false)
    if (!bare) throw new Error('empty value')
    return this.classifyBare(bare)
  }

  parseArray(): Node {
    this.i += 1 // '['
    const items: Node[] = []
    for (;;) {
      this.skipSeparators()
      if (this.done) {
        this.repairs.closed = true
        return { kind: 'array', items }
      }
      if (this.peek() === ']') {
        this.i += 1
        return { kind: 'array', items }
      }
      const before = this.i
      items.push(this.parseValue())
      if (this.i === before) throw new Error('made no progress')
    }
  }

  parseObject(braced: boolean): Node {
    if (braced) this.i += 1 // '{'
    const entries: { key: string; value: Node }[] = []
    for (;;) {
      this.skipSeparators()
      if (this.done) {
        if (braced) this.repairs.closed = true
        return { kind: 'object', entries }
      }
      if (this.peek() === '}') {
        if (!braced) throw new Error('unbalanced brace')
        this.i += 1
        return { kind: 'object', entries }
      }

      const quote = this.peek()
      let key: string
      if (quote === '"' || quote === "'") {
        key = this.parseQuoted(quote)
      } else {
        key = this.readBare(true)
        if (!key) throw new Error('empty key')
        this.repairs.keysQuoted += 1
      }

      this.skipBlank()
      if (this.peek() !== ':') throw new Error('missing colon')
      this.i += 1

      const before = this.i
      entries.push({ key, value: this.parseValue() })
      if (this.i === before) throw new Error('made no progress')
    }
  }
}

function emit(node: Node): string {
  switch (node.kind) {
    case 'raw':
      return node.text
    case 'string':
      return JSON.stringify(node.value)
    case 'template':
      return `"\${${node.token}}"`
    case 'array':
      return `[${node.items.map(emit).join(',')}]`
    case 'object':
      return `{${node.entries.map(e => `${JSON.stringify(e.key)}:${emit(e.value)}`).join(',')}}`
  }
}

function describe(repairs: Repairs): string[] {
  const fixes: string[] = []
  if (repairs.wrapped) fixes.push('wrapped it in { }')
  else if (repairs.closed) fixes.push('closed a missing brace')
  if (repairs.keysQuoted) fixes.push(`quoted ${repairs.keysQuoted} key${repairs.keysQuoted > 1 ? 's' : ''}`)
  if (repairs.singleQuotes) fixes.push('switched single quotes to double')
  if (repairs.valuesQuoted) fixes.push(`quoted ${repairs.valuesQuoted} value${repairs.valuesQuoted > 1 ? 's' : ''}`)
  if (repairs.templates) {
    fixes.push(
      repairs.templates > 1
        ? `read ${repairs.templates} values as properties`
        : 'read 1 value as a property',
    )
  }
  if (repairs.trailingCommas) fixes.push('dropped a stray comma')
  return fixes
}

export function relaxedToJson(input: string, knownVariables: readonly string[] = []): RelaxedResult | null {
  const src = input.trim()
  if (!src) return null

  const repairs: Repairs = {
    wrapped: false,
    closed: false,
    keysQuoted: 0,
    valuesQuoted: 0,
    singleQuotes: 0,
    trailingCommas: 0,
    templates: 0,
  }
  const parser = new RelaxedParser(src, new Set(knownVariables), repairs)

  let root: Node
  try {
    parser.skipBlank()
    const first = parser.peek()
    if (first === '{') {
      root = parser.parseObject(true)
    } else if (first === '[') {
      root = parser.parseArray()
    } else {
      // Bare `key: value` pairs with no braces around them.
      repairs.wrapped = true
      root = parser.parseObject(false)
    }
    parser.skipSeparators()
    if (!parser.done) return null
  } catch {
    return null
  }

  if (root.kind === 'object' && root.entries.length === 0) return null

  const fixes = describe(repairs)
  if (fixes.length === 0) return null
  return { text: emit(root), fixes }
}
