/**
 * A JSON field value as rows of key → value, for the property grid that edits
 * the common case (F23): a flat object whose values are property references
 * (`${currency}`) or literals. The text stays the model — the grid reads rows
 * from it and writes it back — so the "Edit JSON" view and the grid can never
 * disagree about what is saved.
 *
 * Each row keeps its value as the JSON text it had (`raw`), so a value nobody
 * touched is written back byte for byte: a bare `${price}` stays bare, a nested
 * object keeps its tokens.
 */

// Same grammar as jsonTemplate.ts: a token occupying a whole JSON value.
// eslint-disable-next-line no-control-regex -- the C0 range is excluded, never matched (see jsonTemplate.ts)
const TEMPLATE_VALUE_PATTERN = /"\$\{[^"\\}\x00-\x1f]+\}"|\$\{[^"\\}\x00-\x1f]+\}/g
// eslint-disable-next-line no-control-regex -- same exclusion
const WHOLE_TOKEN = /^"?\$\{([^"\\}\x00-\x1f]+)\}"?$/

export interface TemplateRow {
  key: string
  /** The value as JSON text, `${token}`s included. */
  raw: string
}

function stash(text: string): { safe: string; restore: (s: string) => string } | null {
  const placeholders = new Map<string, string>()
  let prefix = '__TRIPL_GRID_'
  while (text.includes(prefix)) prefix = `_${prefix}`
  const safe = text.replace(TEMPLATE_VALUE_PATTERN, match => {
    const sentinel = `${prefix}${placeholders.size}__`
    placeholders.set(sentinel, match)
    return `"${sentinel}"`
  })
  const restore = (s: string) => {
    let out = s
    placeholders.forEach((original, sentinel) => {
      out = out.split(`"${sentinel}"`).join(original)
    })
    return out
  }
  return { safe, restore }
}

/**
 * The rows of a JSON object template, or null when the text is not one the
 * grid can show (invalid JSON, an array or scalar at the top, a duplicated
 * key). An empty value is an object with no rows yet.
 */
export function parseTemplateRows(text: string): TemplateRow[] | null {
  if (!text.trim()) return []
  const stashed = stash(text)
  if (!stashed) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(stashed.safe)
  } catch {
    return null
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
  const rows = Object.entries(parsed as Record<string, unknown>).map(([key, value]) => ({
    key,
    raw: stashed.restore(JSON.stringify(value, null, 2)),
  }))
  // JSON.parse keeps the last of two equal keys; the grid would silently drop one.
  if (topLevelKeyCount(stashed.safe) !== rows.length) return null
  return rows
}

/** The keys written at the top level of valid JSON object text, repeats included. */
function topLevelKeyCount(text: string): number {
  const colon = /\s*:/y
  let depth = 0
  let count = 0
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (ch === '"') {
      let end = i + 1
      while (end < text.length && text[end] !== '"') end += text[end] === '\\' ? 2 : 1
      colon.lastIndex = end + 1
      if (depth === 1 && colon.test(text)) count++
      i = end
    } else if (ch === '{' || ch === '[') depth++
    else if (ch === '}' || ch === ']') depth--
  }
  return count
}

/** The template text for `rows`, two-space indented like Format writes it. */
export function serializeTemplateRows(rows: TemplateRow[]): string {
  if (rows.length === 0) return ''
  const lines = rows.map(({ key, raw }) => {
    const value = (raw.trim() || '""').replace(/\n/g, '\n  ')
    return `  ${JSON.stringify(key)}: ${value}`
  })
  return `{\n${lines.join(',\n')}\n}`
}

/** The property a value refers to, when the whole value is one `${token}`. */
export function tokenOf(raw: string): string | null {
  const match = WHOLE_TOKEN.exec(raw.trim())
  return match ? match[1]! : null
}

/**
 * What a value cell shows: `${name}` for a reference, the text of a string
 * (quoted only where the bare text would read back as something else), and
 * any other JSON as written.
 */
export function cellText(raw: string): string {
  const token = tokenOf(raw)
  if (token !== null) return `\${${token}}`
  try {
    const value: unknown = JSON.parse(raw)
    if (typeof value === 'string') return cellToRaw(value) === raw ? value : raw
  } catch {
    // A nested value with tokens inside: shown as written.
  }
  return raw
}

/**
 * The JSON text for what was typed in a value cell: a `${name}` reference,
 * JSON when it reads as JSON (numbers, true, null, an object…), else a string.
 */
export function cellToRaw(text: string): string {
  const trimmed = text.trim()
  const token = tokenOf(trimmed)
  if (token !== null && trimmed.startsWith('$')) return `"\${${token}}"`
  if (trimmed === '') return '""'
  if (/^[[{"\d-]|^(true|false|null)$/.test(trimmed)) {
    const stashed = stash(trimmed)
    try {
      if (stashed) JSON.parse(stashed.safe)
      return trimmed
    } catch {
      // Not JSON after all: a string that merely starts like it.
    }
  }
  return JSON.stringify(text)
}

/** A problem with one row the grid can name, else null. */
export function rowProblem(rows: TemplateRow[], index: number): string | null {
  const row = rows[index]
  if (!row) return null
  if (!row.key.trim()) return 'Name the key.'
  if (rows.findIndex(other => other.key === row.key) !== index) return `${row.key} is already a key.`
  return null
}
