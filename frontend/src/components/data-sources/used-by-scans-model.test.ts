import { describe, expect, it } from 'vitest'
import type { DataSource, DataSourceScanRef } from '@/types'
import { dataSourceUsedBy } from './used-by-scans-model'

const SOURCE = { id: 'ds-1', name: 'Warehouse' } as DataSource

const ref = (id: string, project: string): DataSourceScanRef => ({
  id,
  name: `Scan ${id}`,
  project_slug: project,
  project_name: project.toUpperCase(),
})

describe('dataSourceUsedBy', () => {
  it('says nothing when the server sent no count', () => {
    expect(dataSourceUsedBy(SOURCE)).toBeNull()
  })

  it('says a source nothing reads is unused', () => {
    expect(dataSourceUsedBy({ ...SOURCE, scan_count: 0, scans: [] })).toEqual({
      label: 'Not used by any scan',
      links: [],
      more: 0,
    })
  })

  it('keeps the bare count when the server listed no scans', () => {
    expect(dataSourceUsedBy({ ...SOURCE, scan_count: 2 })).toEqual({
      label: 'Used by 2 scans',
      links: [],
      more: 0,
    })
  })

  it('links each scan and leaves the project out when there is one', () => {
    const usedBy = dataSourceUsedBy({ ...SOURCE, scan_count: 1, scans: [ref('a', 'app')] })
    expect(usedBy?.label).toBe('Used by 1 scan')
    expect(usedBy?.links).toEqual([
      { id: 'a', name: 'Scan a', href: '/p/app/scans/a', projectName: null },
    ])
    expect(usedBy?.more).toBe(0)
  })

  it('names the project when scans span several, and counts the ones past the cap', () => {
    const usedBy = dataSourceUsedBy({
      ...SOURCE,
      scan_count: 25,
      scans: [ref('a', 'app'), ref('b', 'web')],
    })
    expect(usedBy?.links.map(link => link.projectName)).toEqual(['APP', 'WEB'])
    expect(usedBy?.more).toBe(23)
  })
})
