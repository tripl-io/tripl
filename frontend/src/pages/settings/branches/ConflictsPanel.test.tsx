import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { planBranchesApi } from '@/api/planBranches'
import type { PlanBranchConflicts, PlanBranchMergeResolution, PlanBranchSummary } from '@/types'
import { ConflictsPanel } from './ConflictsPanel'
import { withChoice } from './conflictModel'

vi.mock('@/api/planBranches', () => ({
  planBranchesApi: {
    getConflicts: vi.fn(),
    saveResolution: vi.fn(),
  },
}))
vi.mock('@/lib/permissions', () => ({ useCanWriteProject: () => true }))

const BRANCH: PlanBranchSummary = {
  id: 'feat-1',
  project_id: 'p-1',
  name: 'checkout-v2',
  kind: 'working',
  status: 'draft',
  description: '',
  base_revision_id: 'rev-1',
  created_by: null,
  merged_at: null,
  merged_by: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const CONFLICTS: PlanBranchConflicts = {
  entities: ['home', 'paywall'].map((name) => ({
    entity_type: 'event',
    name,
    parent: 'screen_view',
    label: name,
    fields: [
      { field: 'description', base: 'old', ours: 'main', theirs: 'branch', choice: null, dependents: 0 },
    ],
  })),
  unresolved_count: 2,
  behind: true,
  overlap_count: 2,
  merge_blocked: false,
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <ConflictsPanel slug="acme" branch={BRANCH} />
    </QueryClientProvider>,
  )
}

function card(name: string) {
  return screen.getByText(name).closest('.rounded-card') as HTMLElement
}

afterEach(() => vi.resetAllMocks())

describe('withChoice', () => {
  it('sets one field and recounts, leaving the input untouched', () => {
    const next = withChoice(
      CONFLICTS,
      { entity_type: 'event', entity_name: 'home', field: 'description' },
      'ours',
    )
    expect(next.entities[0]?.fields[0]?.choice).toBe('ours')
    expect(next.entities[1]?.fields[0]?.choice).toBeNull()
    expect(next.unresolved_count).toBe(1)
    expect(CONFLICTS.entities[0]?.fields[0]?.choice).toBeNull()
  })
})

describe('ConflictsPanel', () => {
  it('shows the choice at the click, before the save answers, and keeps other rows clickable', async () => {
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue(CONFLICTS)
    // A save that never answers: whatever shows, shows without it.
    vi.mocked(planBranchesApi.saveResolution).mockReturnValue(new Promise(() => {}))
    renderPanel()
    await screen.findByText('2 unresolved')

    fireEvent.click(within(card('home')).getByRole('button', { name: 'Take main' }))

    expect(await within(card('home')).findByText("Resolved: main's value")).toBeInTheDocument()
    expect(screen.getByText('1 unresolved')).toBeInTheDocument()
    expect(within(card('paywall')).getByRole('button', { name: 'Keep this branch' })).toBeEnabled()
  })

  it('puts the field back when the save fails, and says so', async () => {
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue(CONFLICTS)
    vi.mocked(planBranchesApi.saveResolution).mockRejectedValue(new Error('boom'))
    renderPanel()
    await screen.findByText('2 unresolved')

    fireEvent.click(within(card('home')).getByRole('button', { name: 'Take main' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not save the choice')
    await waitFor(() => expect(within(card('home')).getByText('Unresolved')).toBeInTheDocument())
  })

  it('re-reads the conflicts once the save lands', async () => {
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue(CONFLICTS)
    vi.mocked(planBranchesApi.saveResolution).mockResolvedValue({} as PlanBranchMergeResolution)
    renderPanel()
    await screen.findByText('2 unresolved')

    fireEvent.click(within(card('home')).getByRole('button', { name: 'Take main' }))

    await waitFor(() => expect(planBranchesApi.getConflicts).toHaveBeenCalledTimes(2))
  })
})
