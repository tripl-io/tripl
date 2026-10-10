import { countOf, pluralize } from '@/lib/plural'
import type { DataSource } from '@/types'

/** Past this many scans the confirm counts them rather than naming them. */
const NAMED_SCANS_MAX = 3

/**
 * The scans the confirm names, or null to count them instead: only when the
 * list holds every scan (a capped list, or one missing scans in projects the
 * reader cannot see, would name some and hide the rest) and is short.
 */
function namedScans(ds: DataSource): string | null {
  const scans = ds.scans
  if (!scans || scans.length === 0 || scans.length !== ds.scan_count) return null
  if (scans.length > NAMED_SCANS_MAX) return null
  const names = scans.map(scan => scan.name)
  const last = names.pop()
  return names.length > 0 ? `${names.join(', ')} and ${last}` : (last ?? null)
}

/**
 * The delete confirm's sentence: counted from the list's usage fields when the
 * server sent them, with the scans named when there are few, and named
 * without numbers when it sent none.
 */
export function dataSourceDeleteMessage(ds: DataSource): string {
  if (ds.scan_count === undefined) {
    return `Delete "${ds.name}"? All associated scans and their runs will be removed.`
  }
  if (ds.scan_count === 0) {
    return `Delete "${ds.name}"? No scan reads it, so nothing else is removed.`
  }
  const scans = countOf(ds.scan_count, 'scan', 'scans')
  const runs = countOf(ds.scan_run_count ?? 0, 'run', 'runs')
  const names = namedScans(ds)
  if (names) {
    const their = pluralize(ds.scan_count, 'its', 'their')
    return `Delete "${ds.name}"? ${scans} (${names}) and ${their} ${runs} will be removed with it.`
  }
  return `Delete "${ds.name}"? ${scans} and ${runs} will be removed with it.`
}

/**
 * A source that scans read must be deleted deliberately: the confirm arms only
 * once its name is typed. A source nothing reads keeps the plain confirm.
 */
export function dataSourceDeleteRequireText(ds: DataSource): string | undefined {
  return (ds.scan_count ?? 0) > 0 ? ds.name : undefined
}
