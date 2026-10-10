/**
 * A property's type as JSON Schema (F23): the coarse `variable_type` and the
 * `json_schema` fragment refining it, kept in step the way the backend does
 * (backend/src/tripl/core/property_schema.py `check_schema_matches_type`):
 *
 *   string        type string, any format but a date one
 *   date/datetime type string with format date / date-time
 *   number        type number or integer
 *   boolean       type boolean
 *   json          type object (nested properties) or array
 *   string_array  type array, items of type string
 *   number_array  type array, items of type number or integer
 *
 * Pure: the schema editor, the event property grid and the catalog read it.
 */
import type { PropertySchema, VariableType } from '@/types'

export type SchemaNodeType = PropertySchema['type']

export const SCHEMA_NODE_TYPES: SchemaNodeType[] = ['string', 'number', 'integer', 'boolean', 'array', 'object']

export const SCHEMA_NODE_TYPE_LABELS: Record<SchemaNodeType, string> = {
  string: 'String',
  number: 'Number',
  integer: 'Integer',
  boolean: 'Boolean',
  array: 'Array',
  object: 'Object',
}

/** The formats the backend accepts on a string node (`STRING_FORMATS`). */
export const STRING_FORMATS = [
  'date', 'date-time', 'time', 'duration', 'email', 'uri', 'uuid', 'hostname', 'ipv4', 'ipv6',
] as const

export type StringFormat = (typeof STRING_FORMATS)[number]

/** What a reader sees for each format; the stored value stays the JSON Schema id. */
export const STRING_FORMAT_LABELS: Record<StringFormat, string> = {
  date: 'Date (YYYY-MM-DD)',
  'date-time': 'Date and time (ISO 8601)',
  time: 'Time',
  duration: 'Duration (ISO 8601)',
  email: 'Email address',
  uri: 'URL',
  uuid: 'UUID',
  hostname: 'Hostname',
  ipv4: 'IPv4 address',
  ipv6: 'IPv6 address',
}

/** A format's label; one this client does not know reads as its id. */
export function stringFormatLabel(format: string): string {
  return Object.hasOwn(STRING_FORMAT_LABELS, format) ? STRING_FORMAT_LABELS[format as StringFormat] : format
}

const DATE_FORMATS: Partial<Record<VariableType, string>> = { date: 'date', datetime: 'date-time' }

/** The smallest fragment that says exactly what `variableType` says. */
export function defaultSchemaFor(variableType: VariableType): PropertySchema {
  switch (variableType) {
    case 'string': return { type: 'string' }
    case 'number': return { type: 'number' }
    case 'boolean': return { type: 'boolean' }
    case 'date': return { type: 'string', format: 'date' }
    case 'datetime': return { type: 'string', format: 'date-time' }
    case 'json': return { type: 'object' }
    case 'string_array': return { type: 'array', items: { type: 'string' } }
    case 'number_array': return { type: 'array', items: { type: 'number' } }
  }
}

/** Whether the fragment agrees with the type, by the backend's rule. */
export function schemaMatchesType(variableType: VariableType, schema: PropertySchema | null | undefined): boolean {
  if (!schema) return true
  const itemType = schema.items?.type
  switch (variableType) {
    case 'string': return schema.type === 'string' && schema.format !== 'date' && schema.format !== 'date-time'
    case 'date':
    case 'datetime': return schema.type === 'string' && schema.format === DATE_FORMATS[variableType]
    case 'number': return schema.type === 'number' || schema.type === 'integer'
    case 'boolean': return schema.type === 'boolean'
    case 'json': return schema.type === 'object' || schema.type === 'array'
    case 'string_array': return schema.type === 'array' && itemType === 'string'
    case 'number_array': return schema.type === 'array' && (itemType === 'number' || itemType === 'integer')
  }
}

/** The fragment to edit for `variableType`: the stored one when it agrees, else the default. */
export function schemaForType(variableType: VariableType, schema: PropertySchema | null | undefined): PropertySchema {
  return schema && schemaMatchesType(variableType, schema) ? schema : defaultSchemaFor(variableType)
}

/** The node types the root may take under `variableType`. */
export function rootTypeOptions(variableType: VariableType): SchemaNodeType[] {
  switch (variableType) {
    case 'number': return ['number', 'integer']
    case 'json': return ['object', 'array']
    case 'boolean': return ['boolean']
    case 'string_array':
    case 'number_array': return ['array']
    default: return ['string']
  }
}

/** A fresh node of `type`, keeping what still applies from `previous`. */
export function nodeOfType(type: SchemaNodeType, previous?: PropertySchema): PropertySchema {
  if (previous?.type === type) return previous
  const description = previous?.description ? { description: previous.description } : {}
  if (type === 'array') return { type, items: { type: 'string' }, ...description }
  // number <-> integer keep their bounds: the keywords are the same.
  if ((type === 'number' || type === 'integer') && (previous?.type === 'number' || previous?.type === 'integer')) {
    return { ...previous, type }
  }
  return { type, ...description }
}

const isEmpty = (value: unknown) =>
  value === undefined
  || value === ''
  || (Array.isArray(value) && value.length === 0)

/** The fragment without empty keys (an unset bound, an empty `required`), nested too. */
export function pruneSchema(node: PropertySchema): PropertySchema {
  const out: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(node)) {
    if (isEmpty(value)) continue
    if (key === 'items' && value) out.items = pruneSchema(value as PropertySchema)
    else if (key === 'properties' && value) {
      if (Object.keys(value as object).length === 0) continue
      out.properties = Object.fromEntries(
        Object.entries(value as Record<string, PropertySchema>).map(([name, sub]) => [name, pruneSchema(sub)]),
      )
    } else out[key] = value
  }
  return out as unknown as PropertySchema
}

/** A stable text form, for comparing two fragments regardless of key order. */
export function canonicalSchema(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalSchema).join(',')}]`
  if (value && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, v]) => v !== undefined)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonicalSchema(v)}`).join(',')}}`
  }
  return JSON.stringify(value)
}

/**
 * What to send as `json_schema`: null when the fragment says no more than the
 * type does, so a property nobody refined keeps no schema of its own. A JSON
 * property keeps even a bare `{type: object}`: object and array are two
 * different answers to the same type.
 */
export function schemaToSave(variableType: VariableType, schema: PropertySchema): PropertySchema | null {
  const pruned = pruneSchema(schema)
  if (variableType !== 'json' && canonicalSchema(pruned) === canonicalSchema(defaultSchemaFor(variableType))) {
    return null
  }
  return pruned
}

/** Whether two (type, schema) pairs describe the same thing once normalised. */
export function sameSchema(
  variableType: VariableType,
  a: PropertySchema | null | undefined,
  b: PropertySchema | null | undefined,
): boolean {
  const norm = (s: PropertySchema | null | undefined) =>
    canonicalSchema(schemaToSave(variableType, schemaForType(variableType, s)))
  return norm(a) === norm(b)
}

/** Problems the editor can see before the server does; one line each, keyed by path. */
export function schemaProblems(node: PropertySchema, path = ''): string[] {
  const problems: string[] = []
  const at = path || 'The property'
  const pair = (low: keyof PropertySchema, high: keyof PropertySchema) => {
    const lo = node[low]
    const hi = node[high]
    if (typeof lo === 'number' && typeof hi === 'number' && lo > hi) {
      problems.push(`${at}: ${String(low)} is greater than ${String(high)}.`)
    }
  }
  pair('minimum', 'maximum')
  pair('minLength', 'maxLength')
  pair('minItems', 'maxItems')
  if (node.pattern) {
    try {
      new RegExp(node.pattern)
    } catch {
      problems.push(`${at}: the pattern is not a valid regular expression.`)
    }
  }
  if (node.items) problems.push(...schemaProblems(node.items, `${path || ''}[]`))
  if (node.properties) {
    const names = Object.keys(node.properties)
    if (names.some(name => !name.trim())) problems.push(`${at}: every nested property needs a name.`)
    for (const [name, sub] of Object.entries(node.properties)) {
      problems.push(...schemaProblems(sub, path ? `${path}.${name}` : name))
    }
  }
  return problems
}

function nodeLabel(node: PropertySchema): string {
  if (node.type === 'array') return `${node.items ? nodeLabel(node.items) : 'Any'}[]`
  if (node.type === 'string' && node.format) {
    if (node.format === 'date') return 'Date'
    if (node.format === 'date-time') return 'Datetime'
    return `String (${node.format})`
  }
  return SCHEMA_NODE_TYPE_LABELS[node.type]
}

const TYPE_ONLY_LABELS: Record<VariableType, string> = {
  string: 'String', number: 'Number', boolean: 'Boolean', date: 'Date',
  datetime: 'Datetime', json: 'JSON', string_array: 'String[]', number_array: 'Number[]',
}

/**
 * The type in a few words, schema first: "Integer", "String (email)",
 * "Object {id, price, +2}", "Object[]". Falls back to the coarse type.
 */
export function summariseType(variableType: VariableType, schema: PropertySchema | null | undefined): string {
  if (!schema || !schemaMatchesType(variableType, schema)) return TYPE_ONLY_LABELS[variableType]
  if (schema.type === 'object') {
    const keys = Object.keys(schema.properties ?? {})
    if (keys.length === 0) return 'Object'
    const shown = keys.slice(0, 3).join(', ')
    return `Object {${shown}${keys.length > 3 ? `, +${keys.length - 3}` : ''}}`
  }
  return nodeLabel(schema)
}

/** The constraints of a node in words, for a tooltip or a read-only view. */
export function describeConstraints(node: PropertySchema | null | undefined): string[] {
  if (!node) return []
  const out: string[] = []
  if (node.pattern) out.push(`matches /${node.pattern}/`)
  if (node.minimum !== undefined) out.push(`≥ ${node.minimum}`)
  if (node.maximum !== undefined) out.push(`≤ ${node.maximum}`)
  if (node.minLength !== undefined) out.push(`at least ${node.minLength} characters`)
  if (node.maxLength !== undefined) out.push(`at most ${node.maxLength} characters`)
  if (node.minItems !== undefined) out.push(`at least ${node.minItems} items`)
  if (node.maxItems !== undefined) out.push(`at most ${node.maxItems} items`)
  if (node.type === 'object' && node.required?.length) out.push(`requires ${node.required.join(', ')}`)
  return out
}
