import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { BranchContext } from '@/components/branch-context-internal'
import { planBranchesKey } from '@/lib/queryKeys'
import { EntityBranchBanner } from './EntityBranchBanner'

vi.mock('@/api/planBranches', () => ({
  planBranchesApi: {
    list: vi.fn(async () => ({
      total: 2,
      items: [
        { id: 'main-id', project_id: 'p', name: 'main', kind: 'main', status: 'merged' },
        { id: 'proj-4770', project_id: 'p', name: 'PROJ-4770', kind: 'working', status: 'ready_for_review' },
      ],
    })),
  },
}))

function renderBanner(activeBranchId: string | null, rowBranchId: string, mainPath?: string) {
  const setBranchId = vi.fn()
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <BranchContext.Provider value={{ branchId: activeBranchId, setBranchId, slug: 'demo' }}>
          <EntityBranchBanner
            slug="demo"
            rowBranchId={rowBranchId}
            path="/p/demo/monitoring/event/e1"
            mainPath={mainPath}
          />
        </BranchContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { setBranchId, queryClient }
}

describe('EntityBranchBanner (tripl-kjhi.7)', () => {
  it('names the branch a row lives on and offers main when the reader is on that branch', async () => {
    const { setBranchId } = renderBanner('proj-4770', 'proj-4770', '/p/demo/events')
    const banner = await screen.findByTestId('entity-branch-banner')
    expect(banner.textContent).toContain('PROJ-4770')
    // The status in the words every branch surface uses, not the raw enum (PL-3).
    expect(banner.textContent).toContain('Ready for review')
    expect(screen.queryByRole('alert')).toBeNull()
    const link = screen.getByRole('link', { name: 'View main plan' })
    expect(link).toHaveAttribute('href', '/p/demo/events')
    fireEvent.click(link)
    expect(setBranchId).toHaveBeenCalledWith(null, { updateUrl: false })
  })

  it("never points main at the branch row's own address (EVT-42)", async () => {
    // Reads are lenient: main would render the same branch row under a
    // mismatch warning, and its Save would 404. Without somewhere on main to go,
    // the banner names the branch and offers no link.
    renderBanner('proj-4770', 'proj-4770')
    const banner = await screen.findByTestId('entity-branch-banner')
    expect(banner.textContent).toContain('PROJ-4770')
    expect(screen.queryByRole('link', { name: 'View main plan' })).toBeNull()
  })

  it('offers the switch when a pasted branch link is opened with main active', async () => {
    const { setBranchId } = renderBanner(null, 'proj-4770')
    const link = await screen.findByRole('link', { name: 'Switch to PROJ-4770' })
    expect(link).toHaveAttribute('href', '/p/demo/monitoring/event/e1?branch=proj-4770')
    fireEvent.click(link)
    // The link carries the branch; the entry being left keeps its own address.
    expect(setBranchId).toHaveBeenCalledWith('proj-4770', { updateUrl: false })
    expect(screen.getByTestId('entity-branch-banner').textContent).toContain('you are viewing main')
    // A mismatch is announced: the page below it cannot save there (PL-2).
    expect(screen.getByRole('alert')).toBe(screen.getByTestId('entity-branch-banner'))
  })

  it('says nothing for a main row read with main active', async () => {
    const { queryClient } = renderBanner(null, 'main-id')
    // Wait for the branches query to settle, so "nothing" is the answer to the
    // loaded list rather than to the loading state.
    await waitFor(() =>
      expect(queryClient.getQueryState(planBranchesKey('demo'))?.status).toBe('success'),
    )
    expect(screen.queryByTestId('entity-branch-banner')).toBeNull()
  })

  it('points a main row back to main when another branch is active', async () => {
    const { setBranchId } = renderBanner('proj-4770', 'main-id')
    const link = await screen.findByRole('link', { name: 'Switch to main' })
    expect(link).toHaveAttribute('href', '/p/demo/monitoring/event/e1')
    fireEvent.click(link)
    expect(setBranchId).toHaveBeenCalledWith(null, { updateUrl: false })
  })
})
