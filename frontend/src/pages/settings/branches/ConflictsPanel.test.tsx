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
    saveResolutions: vi.fn(),
  },
}))
const permissions = vi.hoisted(() => ({ canWrite: true }))
vi.mock('@/lib/permissions', () => ({ useCanWriteProject: () => permissions.canWrite }))

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

afterEach(() => {
  vi.resetAllMocks()
  permissions.canWrite = true
})

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

  it('holds one field to one save at a time, so two cannot race the same row', async () => {
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue(CONFLICTS)
    vi.mocked(planBranchesApi.saveResolution).mockReturnValue(new Promise(() => {}))
    renderPanel()
    await screen.findByText('2 unresolved')

    const home = within(card('home'))
    fireEvent.click(home.getByRole('button', { name: 'Take main' }))
    await home.findByText("Resolved: main's value")
    // A double-click, and a quick change of side, while the first save runs.
    fireEvent.click(home.getByRole('button', { name: 'Take main' }))
    fireEvent.click(home.getByRole('button', { name: 'Keep this branch' }))

    expect(home.getByRole('button', { name: 'Keep this branch' })).toBeDisabled()
    expect(planBranchesApi.saveResolution).toHaveBeenCalledTimes(1)
    // Another row is not held up.
    fireEvent.click(within(card('paywall')).getByRole('button', { name: 'Keep this branch' }))
    await waitFor(() => expect(planBranchesApi.saveResolution).toHaveBeenCalledTimes(2))
  })

  it('puts the field back when the save fails, and says so', async () => {
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue(CONFLICTS)
    vi.mocked(planBranchesApi.saveResolution).mockRejectedValue(new Error('boom'))
    renderPanel()
    await screen.findByText('2 unresolved')

    fireEvent.click(within(card('home')).getByRole('button', { name: 'Take main' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not save the choice for home')
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

/** Two value rows each on two events — one both sides added — and a deletion. */
const BULK: PlanBranchConflicts = {
  entities: [
    {
      entity_type: 'event',
      name: 'home',
      parent: 'screen_view',
      label: 'home',
      added_on_both: true,
      fields: [
        { field: 'title', base: null, ours: '', theirs: 'Home', choice: null, dependents: 0 },
        { field: 'description', base: null, ours: 'main', theirs: '', choice: null, dependents: 0 },
      ],
    },
    {
      entity_type: 'event',
      name: 'paywall',
      parent: 'screen_view',
      label: 'paywall',
      fields: [
        { field: 'title', base: 'a', ours: 'b', theirs: 'c', choice: null, dependents: 0 },
        { field: 'tags', base: [], ours: ['x'], theirs: ['y'], choice: null, dependents: 0 },
      ],
    },
    {
      entity_type: 'event',
      name: 'legacy',
      parent: 'screen_view',
      label: 'legacy',
      fields: [
        {
          field: '@presence',
          base: 'present',
          ours: 'absent',
          theirs: 'present',
          choice: null,
          dependents: 0,
        },
      ],
    },
  ],
  unresolved_count: 5,
  behind: true,
  overlap_count: 3,
  merge_blocked: true,
}

const everyConflict = () => within(screen.getByRole('group', { name: 'Every conflict' }))

describe('ConflictsPanel bulk choices', () => {
  it('settles the whole list in one request, at the click, and leaves deletions alone', async () => {
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue(BULK)
    vi.mocked(planBranchesApi.saveResolutions).mockReturnValue(new Promise(() => {}))
    renderPanel()
    await screen.findByText('5 unresolved')
    expect(screen.getByTestId('deletions-left')).toHaveTextContent('1 deletion still needs a choice')

    fireEvent.click(everyConflict().getByRole('button', { name: 'Keep whichever is filled in' }))

    expect(await screen.findByText('1 unresolved')).toBeInTheDocument()
    expect(planBranchesApi.saveResolutions).toHaveBeenCalledTimes(1)
    expect(planBranchesApi.saveResolutions).toHaveBeenCalledWith('acme', 'feat-1', {
      resolutions: [
        { entity_type: 'event', entity_name: 'home', field_name: 'title', choice: 'theirs' },
        { entity_type: 'event', entity_name: 'home', field_name: 'description', choice: 'ours' },
        { entity_type: 'event', entity_name: 'paywall', field_name: 'title', choice: 'theirs' },
        { entity_type: 'event', entity_name: 'paywall', field_name: 'tags', choice: 'theirs' },
      ],
    })
    expect(planBranchesApi.saveResolution).not.toHaveBeenCalled()
    // The batched rows wait for their save; the deletion is still the user's.
    // Each card carries its own bulk row beside the per-field picks. On the
    // batched card the other side of each saving pick waits (title went to
    // the branch, so its "Take main" is off); the deletion's own pick (the
    // last one on its card) is still free.
    const homeTakeMain = within(card('home')).getAllByRole('button', { name: 'Take main' })
    expect(homeTakeMain.some((button) => button.hasAttribute('disabled'))).toBe(true)
    expect(within(card('legacy')).getAllByRole('button', { name: 'Take main' }).at(-1)).toBeEnabled()
  })

  it('touches only its own entity, and suggests "filled in" where both sides added it', async () => {
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue(BULK)
    vi.mocked(planBranchesApi.saveResolutions).mockReturnValue(new Promise(() => {}))
    renderPanel()
    await screen.findByText('5 unresolved')

    const homeActions = within(screen.getByRole('group', { name: 'All fields of home' }))
    const [first] = homeActions.getAllByRole('button')
    expect(first).toHaveTextContent('Keep whichever is filled in')
    expect(within(card('home')).getByText(/Added on both sides/)).toBeInTheDocument()
    expect(within(card('paywall')).queryByText(/Added on both sides/)).not.toBeInTheDocument()

    fireEvent.click(homeActions.getByRole('button', { name: 'Take main for all' }))

    expect(await screen.findByText('3 unresolved')).toBeInTheDocument()
    const [, , data] = vi.mocked(planBranchesApi.saveResolutions).mock.calls[0]!
    expect(data.resolutions.map((r) => [r.entity_name, r.field_name, r.choice])).toEqual([
      ['home', 'title', 'ours'],
      ['home', 'description', 'ours'],
    ])
  })

  it('puts back only the batched fields when the batch fails, and says how many', async () => {
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue(BULK)
    vi.mocked(planBranchesApi.saveResolution).mockReturnValue(new Promise(() => {}))
    let fail: (error: Error) => void = () => {}
    vi.mocked(planBranchesApi.saveResolutions).mockReturnValue(
      new Promise((_resolve, reject) => {
        fail = reject
      }),
    )
    renderPanel()
    await screen.findByText('5 unresolved')

    fireEvent.click(
      within(screen.getByRole('group', { name: 'All fields of paywall' })).getByRole('button', {
        name: 'Keep this branch for all',
      }),
    )
    await screen.findByText('3 unresolved')
    // A single pick elsewhere while the batch is out.
    fireEvent.click(within(card('legacy')).getByRole('button', { name: 'Keep this branch' }))
    await screen.findByText('2 unresolved')
    fail(new Error('boom'))

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not save 2 choices: boom')
    await waitFor(() => expect(screen.getByText('4 unresolved')).toBeInTheDocument())
    expect(within(card('legacy')).getByText("Resolved: this branch's value")).toBeInTheDocument()
  })

  it('shows a viewer no bulk actions', async () => {
    permissions.canWrite = false
    vi.mocked(planBranchesApi.getConflicts).mockResolvedValue(BULK)
    renderPanel()
    await screen.findByText('5 unresolved')

    expect(screen.queryByRole('group', { name: 'Every conflict' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Take main for all' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('deletions-left')).not.toBeInTheDocument()
  })
})
