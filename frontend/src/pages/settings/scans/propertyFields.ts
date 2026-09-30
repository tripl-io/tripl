import type { ScanConfigPreview } from '@/types'
import { splitFullJsonPath } from './scanUtils'

/**
 * Properties (`<json_column>.<path>`, F23 #306) as metric breakdowns and
 * distribution-drift fields. Mirrors `tripl.json_paths` on the backend: the
 * same grammar and the same cap, so the form refuses what the save would.
 */

/** Per list: at most this many property entries (`MAX_PROPERTY_FIELDS`). */
export const MAX_PROPERTY_FIELDS = 10

const SEGMENT = /^[A-Za-z_][A-Za-z0-9_]*$/

export function isPropertyField(entry: string): boolean {
  return entry.includes('.')
}

/** Whether `entry` is a well-formed `<json_column>.<path>`: identifier segments only. */
export function isValidPropertyField(entry: string): boolean {
  if (!isPropertyField(entry)) return false
  return entry.split('.').every(segment => SEGMENT.test(segment))
}

export function countPropertyFields(entries: string[]): number {
  return entries.filter(isPropertyField).length
}

export interface PropertyOption {
  fullPath: string
  column: string
  path: string
  sampleValues: string[]
}

/**
 * The properties a picker offers: every discovered path of the preview's JSON
 * columns whose name the grammar admits, plus the already-selected entries
 * (a saved config can list a path the current preview has not sampled).
 */
export function propertyOptions(
  jsonColumns: ScanConfigPreview['json_columns'],
  selected: string[],
): PropertyOption[] {
  const byPath = new Map<string, PropertyOption>()
  for (const jsonColumn of jsonColumns) {
    for (const path of jsonColumn.paths) {
      if (!isValidPropertyField(path.full_path)) continue
      byPath.set(path.full_path, {
        fullPath: path.full_path,
        column: jsonColumn.column,
        path: path.path,
        sampleValues: path.sample_values,
      })
    }
  }
  for (const entry of selected) {
    if (!isPropertyField(entry) || byPath.has(entry)) continue
    const parsed = splitFullJsonPath(entry)
    if (!parsed) continue
    byPath.set(entry, { fullPath: entry, column: parsed.column, path: parsed.path, sampleValues: [] })
  }
  return [...byPath.values()].sort((a, b) => a.fullPath.localeCompare(b.fullPath))
}
