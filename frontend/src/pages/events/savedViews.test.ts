import { describe, expect, it } from 'vitest'
import {
  applyViewParams,
  deleteEventsSavedView,
  viewParamsOf,
  loadEventsSavedViews,
  saveEventsSavedView,
} from './savedViews'

describe('events saved views storage', () => {
  it('stores saved filter snapshots per project slug', () => {
    saveEventsSavedView('demo-saved-a', {
      name: 'Needs review',
      tab: 'review',
      params: 'q=checkout&implemented=false',
    })
    saveEventsSavedView('other-saved-a', {
      name: 'Archived',
      tab: 'archived',
      params: 'tag=legacy',
    })

    expect(loadEventsSavedViews('demo-saved-a')).toMatchObject([
      {
        name: 'Needs review',
        tab: 'review',
        params: 'q=checkout&implemented=false',
      },
    ])
    expect(loadEventsSavedViews('other-saved-a')).toMatchObject([
      {
        name: 'Archived',
        tab: 'archived',
        params: 'tag=legacy',
      },
    ])
  })

  it('overwrites and deletes saved views by name', () => {
    saveEventsSavedView('demo-saved-b', { name: 'Default', tab: 'all', params: 'q=old' })
    saveEventsSavedView('demo-saved-b', { name: 'Default', tab: 'all', params: 'q=new' })

    expect(loadEventsSavedViews('demo-saved-b')).toMatchObject([
      { name: 'Default', tab: 'all', params: 'q=new' },
    ])

    expect(deleteEventsSavedView('demo-saved-b', 'Default')).toEqual([])
    expect(loadEventsSavedViews('demo-saved-b')).toEqual([])
  })
})

describe('saved view params', () => {
  it('keeps only filter keys, sorted, and never the branch', () => {
    expect(viewParamsOf('branch=b1&tag=web&q=checkout&f.screen=home&utm=x&status=live&status=draft'))
      .toBe('f.screen=home&q=checkout&status=draft&status=live&tag=web')
  })

  it('matches the same filters in another order', () => {
    expect(viewParamsOf('tag=web&q=a')).toBe(viewParamsOf('q=a&tag=web'))
  })

  it('swaps the filters and leaves the rest of the URL alone when applied', () => {
    const next = applyViewParams(new URLSearchParams('branch=b2&q=old&m.owner=x'), 'q=new&branch=b1')

    expect(next.get('branch')).toBe('b2')
    expect(next.get('q')).toBe('new')
    expect(next.has('m.owner')).toBe(false)
  })
})
