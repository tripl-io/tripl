/**
 * Turning a pasted block into a list of events to create.
 *
 * Kept pure and separate from the page because everything interesting here is a
 * rule, not a rendering: which columns a line has to supply, what the resulting
 * event is called, and whether that name is free. The page renders what this
 * returns.
 */
import type { FieldDefinition } from '@/types'
import { applyEventNameFormat } from './utils'

/** Why a parsed line cannot be created, or `ready` when it can. */
export type BulkRowStatus = 'ready' | 'incomplete' | 'invalid' | 'duplicate' | 'exists'

/**
 * A required field the name is not built from, carried as a column of its own
 * after the identity columns (tripl-hhw3 / AU-19). Without it a type with one
 * such field could not be pasted at all: every event of it would be refused.
 */
export interface BulkExtraColumn {
  /** The field's name, which is also what the hint calls the column. */
  name: string
  /** An enum field's allowed values; a pasted value outside them is refused here, not by the server. */
  enumOptions?: readonly string[] | null
}

export interface BulkRow {
  /** 1-based position in the pasted text, blank lines included, so the reader can find it. */
  line: number
  /** Values in naming-column order — or the single typed name when no rule applies. */
  values: string[]
  /** Values for the extra (required, non-naming) columns, in their order; '' where the line gave none. */
  extras: string[]
  /** The event that would be created. */
  name: string
  /** The human label that followed the identity columns; empty when the line gave none. */
  title: string
  status: BulkRowStatus
  /** Columns this line left empty; only set when `status` is `incomplete`. */
  missing: string[]
  /** What is wrong with a value the line gave; only set when `status` is `invalid`. */
  problems: string[]
}

/**
 * Split one line into the values its columns need, and the title after them.
 *
 * Whatever follows the last column is the title, kept whole with its own
 * delimiters — `weather_alert,show,widget,Weather alert widget shown` is three
 * columns and a label (tripl-kjhi.3). Extra columns (required fields the name
 * is not built from) sit between the identity and the title. Past one identity
 * column, a tab wins over a comma — a paste out of a spreadsheet is
 * tab-separated, and its cells may themselves contain commas.
 *
 * A single identity column (a one-column format, or a free-text name) is only
 * ever split on a TAB, extra columns or not: `{page}` names events after paths
 * like `/buoy/2758a8b1.../Tregde+A`, which carry commas, and a comma there
 * would tear the identity apart to make a title nobody asked for.
 */
function splitLine(
  line: string,
  columnCount: number,
  extraCount: number,
): { values: string[]; extras: string[]; title: string } {
  const identityCount = Math.max(columnCount, 1)
  const delimiter = line.includes('\t') ? '\t' : identityCount > 1 ? ',' : null
  if (delimiter === null) {
    return { values: [line.trim()], extras: Array.from({ length: extraCount }, () => ''), title: '' }
  }
  const parts = line.split(delimiter)
  const extraEnd = identityCount + extraCount
  return {
    values: parts.slice(0, identityCount).map(part => part.trim()),
    extras: Array.from({ length: extraCount }, (_, i) => (parts[identityCount + i] ?? '').trim()),
    title: parts.slice(extraEnd).join(delimiter).trim(),
  }
}

export interface ParseBulkDraftOptions {
  /** Columns the name format reads, in the order it reads them. Empty when unruled. */
  columns: string[]
  /** The governing `event_name_format`, or null when the user names events freely. */
  nameFormat: string | null
  /**
   * Identities already in the catalog — `source_name` where an event has one,
   * its `name` otherwise, which is what the next scan would adopt. Matching the
   * server's own rule (`_event_holding_scan_identity`) is the point: a name is
   * not free just because no event DISPLAYS it.
   */
  taken?: ReadonlySet<string>
  /** Required fields outside the name, read after the identity columns. */
  extraColumns?: readonly BulkExtraColumn[]
}

/** What an extra column's value gets wrong, or null when it is fine. */
function extraProblem(column: BulkExtraColumn, value: string): string | null {
  const options = column.enumOptions
  if (!options || options.length === 0 || options.includes(value)) return null
  return `${column.name} must be one of ${options.join(', ')}`
}

export function parseBulkDraft(text: string, options: ParseBulkDraftOptions): BulkRow[] {
  const { columns, nameFormat, taken, extraColumns = [] } = options
  const rows: BulkRow[] = []
  const seen = new Set<string>()

  const classify = (name: string): BulkRowStatus => {
    if (seen.has(name)) return 'duplicate'
    if (taken?.has(name)) return 'exists'
    return 'ready'
  }

  text.split('\n').forEach((rawLine, index) => {
    const line = index + 1
    if (rawLine.trim() === '') return

    const { values, extras, title } = splitLine(rawLine, columns.length, extraColumns.length)
    const base = { line, values, extras, title }

    // An empty extra is the same gap as an empty naming column: the server
    // refuses the event, so the line says which column it left out.
    const missingExtras = extraColumns.filter((_, i) => !extras[i]).map(column => column.name)

    let name: string
    let missing: string[]
    if (nameFormat === null) {
      name = values[0] ?? ''
      missing = missingExtras
    } else {
      const valuesByColumn: Record<string, string> = {}
      columns.forEach((column, position) => {
        const value = values[position]
        if (value) valuesByColumn[column] = value
      })
      const applied = applyEventNameFormat(nameFormat, valuesByColumn)
      name = applied.name
      missing = [...applied.missing, ...missingExtras]
    }

    if (missing.length > 0) {
      rows.push({ ...base, name, status: 'incomplete', missing, problems: [] })
      return
    }
    const problems = extraColumns.flatMap((column, i) => extraProblem(column, extras[i] ?? '') ?? [])
    if (problems.length > 0) {
      rows.push({ ...base, name, status: 'invalid', missing: [], problems })
      return
    }
    const status = classify(name)
    if (status === 'ready') seen.add(name)
    rows.push({ ...base, name, status, missing: [], problems: [] })
  })

  return rows
}

/**
 * The required fields a paste has to carry as columns of their own: required,
 * and not already a naming column. Field order, which is the form's order.
 */
export function bulkExtraColumns(
  fields: readonly Pick<FieldDefinition, 'name' | 'field_type' | 'is_required' | 'enum_options' | 'order'>[],
  namingColumns: readonly string[],
): BulkExtraColumn[] {
  return [...fields]
    .filter(field => field.is_required && field.field_type !== 'json' && !namingColumns.includes(field.name))
    .sort((a, b) => a.order - b.order)
    .map(field => ({
      name: field.name,
      enumOptions: field.field_type === 'enum' ? (field.enum_options ?? null) : null,
    }))
}

/**
 * Why this event type cannot be filled from a pasted block, or null when it can.
 *
 * Both refusals are narrow and checkable, and saying so beats accepting a paste
 * the server will reject item by item. A required field the name is not built
 * from is no longer one of them — the paste carries it as an extra column —
 * unless it holds JSON, which a cell of a pasted line cannot.
 */
export function bulkUnsupportedReason(options: {
  nameFormat: string | null
  namingColumns: string[]
  /** Required JSON fields that are not naming columns. */
  requiredJsonFields: string[]
}): string | null {
  const { nameFormat, namingColumns, requiredJsonFields } = options
  if (nameFormat !== null && nameFormat.includes('.')) {
    return (
      'This event type names its events from a value inside a JSON field, which a '
      + 'pasted list cannot fill. Add these events one at a time.'
    )
  }
  const unfillable = requiredJsonFields.filter(field => !namingColumns.includes(field))
  if (unfillable.length > 0) {
    return (
      `Every event of this type needs ${unfillable.join(', ')}, a JSON value a pasted list `
      + 'cannot carry. Add these events one at a time, or make the fields optional.'
    )
  }
  return null
}

/**
 * The paste with one line's name swapped for `name` — "Use suggested name" on
 * the bulk preview (F12, #265). Only for a type without a naming rule, where
 * the name is the line's first cell: the part before the first tab, or the
 * whole line when it has none. Anything after the tab (the title) is kept.
 * `line` is 1-based, as {@link BulkRow.line}; an out-of-range line changes
 * nothing.
 */
export function replaceBulkLineName(text: string, line: number, name: string): string {
  const lines = text.split('\n')
  const target = lines[line - 1]
  if (target === undefined) return text
  const tab = target.indexOf('\t')
  lines[line - 1] = tab === -1 ? name : `${name}${target.slice(tab)}`
  return lines.join('\n')
}
