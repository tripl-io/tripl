/**
 * Plan history: an empty history, a phone-width layout, paging that cannot
 * skip a page, and timestamps that name their zone.
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { PlanRevisionList, PlanRevisionSummary } from '@/types'
import { HistoryTab } from './HistoryTab'

vi.mock('@/api/planRevisions', () => ({
  planRevisionsApi: { list: vi.fn(), create: vi.fn(), get: vi.fn(), diff: vi.fn() },
}))
vi.mock('@/api/planBranches', () => ({
  planBranchesApi: { list: vi.fn(async () => ({ items: [], total: 0 })) },
}))
vi.mock('@/api/users', () => ({
  usersApi: { list: vi.fn(async () => []) },
}))
vi.mock('@/api/planExport', () => ({
  planExportApi: { jsonSchema: vi.fn() },
}))

import { planRevisionsApi } from '@/api/planRevisions'
import { formatDateTime } from '@/lib/datetime'

const SLUG = 'demo'

function makeRevision(id: string): PlanRevisionSummary {
  return {
    id,
    project_id: 'p-1',
    summary: `Snapshot ${id}`,
    created_at: '2026-10-09T11:35:00Z',
    created_by: null,
    kind: 'snapshot',
    branch_id: null,
    entity_counts: { event_types: 3, fields: 10, events: 18, variables: 0, meta_fields: 0, relations: 0 },
  }
}

/** The page the endpoint answers for `offset`: `PAGE_SIZE + 1` rows at most. */
function pageAt(offset: number, total: number): PlanRevisionList {
  const count = Math.max(0, Math.min(51, total - offset))
  return {
    items: Array.from({ length: count }, (_, index) => makeRevision(`rev-${offset + index}`)),
    total,
  }
}

function renderHistory() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <HistoryTab slug={SLUG} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(planRevisionsApi.list).mockReset()
  vi.mocked(planRevisionsApi.diff).mockResolvedValue({
    revision_id: 'x',
    compare_to: 'y',
    entries: [],
    summary: { added: 0, removed: 0, changed: 0 },
  })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('HistoryTab — an empty history', () => {
  it('shows one empty state that points at branches, not a prompt to pick nothing', async () => {
    vi.mocked(planRevisionsApi.list).mockResolvedValue({ items: [], total: 0 })
    renderHistory()

    expect(await screen.findByRole('heading', { name: 'No revisions yet' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open plan branches' })).toHaveAttribute(
      'href',
      '/p/demo/branches',
    )
    expect(screen.queryByText('Pick a revision to view its diff.')).toBeNull()
    expect(screen.queryByText(/Select a revision/)).toBeNull()
    // "Snapshot now" stays where it was, in the header.
    expect(screen.getByRole('button', { name: /Snapshot now/ })).toBeInTheDocument()
  })
})

describe('HistoryTab — a phone-width layout', () => {
  it('holds the single column to the screen, so a long meta line truncates instead of widening it', async () => {
    vi.mocked(planRevisionsApi.list).mockResolvedValue(pageAt(0, 1))
    renderHistory()

    const row = await screen.findByText('Snapshot rev-0')
    // jsdom does no layout; the class is the fix. Without a base template the
    // implicit `auto` track took the meta line's nowrap width.
    const grid = row.closest('.grid')
    expect(grid?.className).toMatch(/(^|\s)grid-cols-1(\s|$)/)
  })
})

describe('HistoryTab — paging', () => {
  it('keeps the page on screen and refuses a second step until the next one lands', async () => {
    // Page 2 answers only when the test lets it.
    const gate = { release: () => {} }
    vi.mocked(planRevisionsApi.list).mockImplementation(async (_slug, params) => {
      const offset = params?.offset ?? 0
      if (offset === 0) return pageAt(0, 120)
      await new Promise<void>((resolve) => {
        gate.release = () => resolve()
      })
      return pageAt(offset, 120)
    })
    renderHistory()

    expect(await screen.findByText('Showing 1–50 of 120 revisions.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Older' }))

    // The rows and the caption still describe page 1, and Older is shut: a
    // second click used to move on to 100 and drop 51–100 unrendered.
    expect(await screen.findByText('Updating…')).toBeInTheDocument()
    expect(screen.getByText('Snapshot rev-0')).toBeInTheDocument()
    expect(screen.getByText('Showing 1–50 of 120 revisions.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Older' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Older' }))
    expect(planRevisionsApi.list).not.toHaveBeenCalledWith(SLUG, { offset: 100, limit: 51 })

    await waitFor(() =>
      expect(planRevisionsApi.list).toHaveBeenCalledWith(SLUG, { offset: 50, limit: 51 }),
    )
    await act(async () => {
      gate.release()
    })
    expect(await screen.findByText('Showing 51–100 of 120 revisions.')).toBeInTheDocument()
    expect(screen.queryByText('Updating…')).toBeNull()
  })
})

describe('HistoryTab — times name their zone', () => {
  it('titles the selected revision’s time with the zone it is shown in', async () => {
    vi.mocked(planRevisionsApi.list).mockResolvedValue(pageAt(0, 2))
    renderHistory()

    await screen.findByText('Snapshot rev-0')
    const shown = formatDateTime('2026-10-09T11:35:00Z')
    // Exact text: the header's timestamp, not the list row's meta line, which
    // carries the same time inside a longer string.
    const title = screen.getByText(shown).getAttribute('title') ?? ''
    expect(title.startsWith(`${shown} `)).toBe(true)
    expect(title.length).toBeGreaterThan(shown.length + 1)
  })
})
