import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DocScope, DocSearchHit, DocSummary } from '@/types/docs'
import { DocQuickOpen } from './DocQuickOpen'

vi.mock('@/api/docs', () => ({
  docsApi: { search: vi.fn() },
}))

import { docsApi } from '@/api/docs'

function summary(path: string, title: string, updated_at: string, scope: DocScope = 'project'): DocSummary {
  return {
    scope,
    path,
    title,
    description: '',
    tags: [],
    audience: 'both',
    revision: 1,
    size_bytes: 1,
    updated_at,
    updated_by_name: null,
  }
}

function hit(path: string, title: string, snippet = ''): DocSearchHit {
  return { scope: 'project', path, title, description: '', tags: [], audience: 'both', snippet, score: 1, confidence: 1 }
}

const PROJECT = [
  summary('checkout.md', 'Checkout funnel', '2026-09-01T00:00:00Z'),
  summary('guides/setup.md', 'Setup', '2026-09-03T00:00:00Z'),
]
const ORG = [summary('warehouse/gotchas.md', 'Warehouse gotchas', '2026-09-02T00:00:00Z', 'organization')]

function LocationProbe() {
  const location = useLocation()
  return <output data-testid="location">{location.pathname}</output>
}

function renderQuickOpen(open = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onOpenChange = vi.fn()
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/p/demo/docs']}>
        <DocQuickOpen slug="demo" open={open} onOpenChange={onOpenChange} projectDocs={PROJECT} organizationDocs={ORG} />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { onOpenChange }
}

const input = () => screen.getByPlaceholderText('Open a note by title or path…')

beforeEach(() => {
  vi.mocked(docsApi.search).mockReset().mockResolvedValue({ items: [], total: 0, truncated: false, semantic_used: false })
})

describe('DocQuickOpen', () => {
  it('renders nothing while closed', () => {
    renderQuickOpen(false)
    expect(screen.queryByPlaceholderText('Open a note by title or path…')).toBeNull()
  })

  it('lists the most recently updated notes first before any query', () => {
    renderQuickOpen()
    expect(screen.getByText('Recently updated')).toBeInTheDocument()
    const titles = screen.getAllByRole('option').map(o => o.textContent)
    expect(titles[0]).toContain('Setup')
    expect(titles[1]).toContain('Warehouse gotchas')
    expect(titles[2]).toContain('Checkout funnel')
  })

  it('fuzzy-matches titles and paths across both scopes', () => {
    renderQuickOpen()
    fireEvent.change(input(), { target: { value: 'ckout' } })
    expect(screen.getByText('Notes')).toBeInTheDocument()
    expect(screen.getAllByRole('option')).toHaveLength(1)
    expect(screen.getByRole('option', { name: /Checkout funnel/ })).toBeInTheDocument()
  })

  it('adds body-text hits it has not already listed, and opens one', async () => {
    vi.mocked(docsApi.search).mockResolvedValue({
      items: [hit('checkout.md', 'Checkout funnel'), hit('sql/recipes.md', 'Recipes', '…zzqq appears here…'), hit('bare.md', 'Bare')],
      total: 3,
      truncated: false,
      semantic_used: false,
    })
    const { onOpenChange } = renderQuickOpen()
    fireEvent.change(input(), { target: { value: 'zzqq' } })
    expect(await screen.findByText('In note text', {}, { timeout: 3000 })).toBeInTheDocument()
    expect(docsApi.search).toHaveBeenCalledWith('demo', { q: 'zzqq', limit: 10 }, expect.anything())
    expect(screen.getByText('…zzqq appears here…')).toBeInTheDocument()
    // Only the body hits: nothing matched by title, and no duplicates.
    expect(screen.getAllByRole('option')).toHaveLength(3)
    fireEvent.click(screen.getByRole('option', { name: /Recipes/ }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/p/demo/docs/project/sql/recipes.md'))
  })

  it('says when nothing matches', async () => {
    renderQuickOpen()
    fireEvent.change(input(), { target: { value: 'zzzzqqqq' } })
    expect(screen.getByText('No note matches.')).toBeInTheDocument()
    await waitFor(() => expect(docsApi.search).toHaveBeenCalled(), { timeout: 3000 })
    expect(screen.getByText('No note matches.')).toBeInTheDocument()
  })

  it('does not search the body for a one-letter query', async () => {
    renderQuickOpen()
    fireEvent.change(input(), { target: { value: 's' } })
    // Past the debounce: the settled query is still too short to search.
    await act(() => new Promise(resolve => setTimeout(resolve, 400)))
    expect(docsApi.search).not.toHaveBeenCalled()
  })
})
