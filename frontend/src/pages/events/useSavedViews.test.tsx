import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { loadEventsSavedViews, saveEventsSavedView } from './savedViews'
import { useSavedViews } from './useSavedViews'

const wrapper = ({ children }: { children: ReactNode }) => (
  <MemoryRouter initialEntries={['/p/demo/events']}>{children}</MemoryRouter>
)

afterEach(() => {
  localStorage.clear()
})

describe('useSavedViews delete', () => {
  it('asks before deleting and keeps the view when the answer is no', async () => {
    saveEventsSavedView('demo', { name: 'Checkout', tab: 'all', params: 'q=checkout' })
    const confirm = vi.fn().mockResolvedValue(false)
    const { result } = renderHook(() => useSavedViews({ slug: 'demo', activeTab: 'all', confirm }), { wrapper })

    await act(async () => {
      await result.current.deleteSavedView('Checkout')
    })

    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ title: 'Delete saved view', variant: 'danger' }))
    expect(result.current.savedViews.map(view => view.name)).toEqual(['Checkout'])
    expect(loadEventsSavedViews('demo')).toHaveLength(1)
  })

  it('deletes the view once confirmed', async () => {
    saveEventsSavedView('demo', { name: 'Checkout', tab: 'all', params: 'q=checkout' })
    const confirm = vi.fn().mockResolvedValue(true)
    const { result } = renderHook(() => useSavedViews({ slug: 'demo', activeTab: 'all', confirm }), { wrapper })

    await act(async () => {
      await result.current.deleteSavedView('Checkout')
    })

    expect(result.current.savedViews).toEqual([])
    expect(loadEventsSavedViews('demo')).toEqual([])
  })
})
