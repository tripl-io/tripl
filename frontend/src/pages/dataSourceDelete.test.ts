import { describe, expect, it } from 'vitest'

import type { DataSource } from '@/types'
import {
  dataSourceDeleteMessage,
  dataSourceDeleteRequireText,
} from './dataSourceDelete'

const SOURCE = { name: 'Warehouse' } as DataSource

describe('dataSourceDeleteMessage', () => {
  it('counts the scans and runs that go with the source', () => {
    expect(dataSourceDeleteMessage({ ...SOURCE, scan_count: 1, scan_run_count: 12 })).toBe(
      'Delete "Warehouse"? 1 scan and 12 runs will be removed with it.',
    )
  })

  it('says nothing else goes when no scan reads it', () => {
    expect(dataSourceDeleteMessage({ ...SOURCE, scan_count: 0, scan_run_count: 0 })).toBe(
      'Delete "Warehouse"? No scan reads it, so nothing else is removed.',
    )
  })

  it('names the loss without numbers when the server sent none', () => {
    expect(dataSourceDeleteMessage(SOURCE)).toBe(
      'Delete "Warehouse"? All associated scans and their runs will be removed.',
    )
  })
})

describe('dataSourceDeleteMessage names a few scans', () => {
  const scan = (id: string, name: string) => ({ id, name, project_slug: 'app', project_name: 'App' })

  it('names the one scan and its runs', () => {
    expect(
      dataSourceDeleteMessage({ ...SOURCE, scan_count: 1, scan_run_count: 42, scans: [scan('s1', 'Demo scan')] }),
    ).toBe('Delete "Warehouse"? 1 scan (Demo scan) and its 42 runs will be removed with it.')
  })

  it('names several scans and their runs', () => {
    expect(
      dataSourceDeleteMessage({
        ...SOURCE,
        scan_count: 2,
        scan_run_count: 5,
        scans: [scan('s1', 'App events'), scan('s2', 'Web events')],
      }),
    ).toBe('Delete "Warehouse"? 2 scans (App events and Web events) and their 5 runs will be removed with it.')
  })

  it('only counts when the list does not hold every scan', () => {
    expect(
      dataSourceDeleteMessage({ ...SOURCE, scan_count: 3, scan_run_count: 5, scans: [scan('s1', 'App events')] }),
    ).toBe('Delete "Warehouse"? 3 scans and 5 runs will be removed with it.')
  })
})

describe('dataSourceDeleteRequireText', () => {
  it('asks for the name when scans read the source', () => {
    expect(dataSourceDeleteRequireText({ ...SOURCE, scan_count: 2 })).toBe('Warehouse')
  })

  it('keeps the plain confirm when nothing reads it or the count is unknown', () => {
    expect(dataSourceDeleteRequireText({ ...SOURCE, scan_count: 0 })).toBeUndefined()
    expect(dataSourceDeleteRequireText(SOURCE)).toBeUndefined()
  })
})
