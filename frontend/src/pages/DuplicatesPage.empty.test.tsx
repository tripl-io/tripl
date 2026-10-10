import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { duplicatesApi } from '@/api/duplicates'
import { projectsApi } from '@/api/projects'
import { BranchContext } from '@/components/branch-context-internal'
import type { Project } from '@/types'
import DuplicatesPage from './DuplicatesPage'

vi.mock('@/api/duplicates', () => ({
  MAX_DUPLICATE_CANDIDATES: 500,
  duplicatesApi: { check: vi.fn(), clusters: vi.fn(), dismiss: vi.fn() },
}))
vi.mock('@/api/projects', () => ({
  projectsApi: { get: vi.fn() },
}))

function withEvents(eventCount: number): Project {
  return { summary: { event_count: eventCount } } as Project
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <BranchContext.Provider value={{ branchId: null, setBranchId: () => {}, slug: 'demo' }}>
        <MemoryRouter initialEntries={['/p/demo/duplicates']}>
          <Routes>
            <Route path="/p/:slug/duplicates" element={<DuplicatesPage />} />
          </Routes>
        </MemoryRouter>
      </BranchContext.Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(duplicatesApi.clusters).mockResolvedValue({
    items: [],
    next_cursor: null,
    total: 0,
    threshold: 0.88,
  })
})

describe('DuplicatesPage — nothing to show', () => {
  // "No likely duplicates" on a project with no events read as a check that
  // came back clean.
  it('says there is nothing to compare yet in a plan with no events', async () => {
    vi.mocked(projectsApi.get).mockResolvedValue(withEvents(0))
    renderPage()

    expect(await screen.findByRole('heading', { name: 'No events to compare yet' })).toBeInTheDocument()
    expect(screen.queryByText('No likely duplicates')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Go to Events' })).toHaveAttribute('href', '/p/demo/events')
  })

  it('keeps the all-clear for a plan with events, in the status names the app uses', async () => {
    vi.mocked(projectsApi.get).mockResolvedValue(withEvents(12))
    renderPage()

    expect(await screen.findByRole('heading', { name: 'No likely duplicates' })).toBeInTheDocument()
    expect(
      screen.getByText(/No two Live, Implemented or Ready for dev events of the same event type/),
    ).toBeInTheDocument()
    const docs = screen.getByRole('link', { name: 'How duplicates are found' })
    expect(docs).toHaveAttribute(
      'href',
      'https://docs.tripl.io/use/duplicates-and-naming#the-duplicates-view',
    )
    expect(docs).toHaveAttribute('target', '_blank')
  })

  it('draws the header link to Reconciliation as Coverage draws it', async () => {
    vi.mocked(projectsApi.get).mockResolvedValue(withEvents(12))
    renderPage()

    const link = await screen.findByRole('link', { name: 'Reconciliation' })
    expect(link).toHaveAttribute('href', '/p/demo/reconciliation')
    expect(link.querySelector('svg')).not.toBeNull()
  })
})
