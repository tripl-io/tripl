import { countOf } from '@/lib/plural'
import type { DataSource } from '@/types'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

export interface UsedByLink {
  id: string
  name: string
  href: string
  /** Set only when the scans span more than one project. */
  projectName: string | null
}

export interface UsedBy {
  /** "Used by 2 scans" / "Not used by any scan". */
  label: string
  links: UsedByLink[]
  /** Scans counted but not listed: the server caps the list. */
  more: number
}

/**
 * What the card says about the scans reading a source. Null when the
 * server sent no count at all. The project is named only when the scans sit in
 * more than one: on a one-project workspace it would repeat on every link.
 */
export function dataSourceUsedBy(ds: DataSource): UsedBy | null {
  if (ds.scan_count === undefined) return null
  if (ds.scan_count === 0) return { label: 'Not used by any scan', links: [], more: 0 }
  const label = `Used by ${countOf(ds.scan_count, 'scan', 'scans')}`
  // No list at all (an older server): the count alone, not "and 2 more".
  if (ds.scans === undefined) return { label, links: [], more: 0 }
  const scans = ds.scans
  const manyProjects = new Set(scans.map(scan => scan.project_slug)).size > 1
  return {
    label,
    links: scans.map(scan => ({
      id: scan.id,
      name: scan.name,
      href: projectPath(currentOrgSlug(), scan.project_slug, `/scans/${scan.id}`),
      projectName: manyProjects ? scan.project_name : null,
    })),
    more: Math.max(0, ds.scan_count - scans.length),
  }
}
