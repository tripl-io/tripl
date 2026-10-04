import { useState } from 'react'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { planBranchesApi } from '@/api/planBranches'
import type { PlanBranchSummary } from '@/types'
import { useUnsavedChangesGuard } from '@/hooks/useUnsavedChangesGuard'
import { BranchSwitcher } from './branch-switcher'

const setBranchId = vi.fn()
const branchState: { id: string | null } = { id: null }

vi.mock('@/api/planBranches', () => ({
  planBranchesApi: { list: vi.fn() },
}))

vi.mock('@/hooks/useBranch', () => ({
  useBranchContext: () => ({ branchId: branchState.id, setBranchId, slug: 'demo' }),
  useActiveBranchId: () => branchState.id,
}))

function makeBranch(overrides: Partial<PlanBranchSummary>): PlanBranchSummary {
  return {
    id: 'b-1',
    project_id: 'p-1',
    name: 'main',
    kind: 'main',
    status: 'merged',
    description: '',
    base_revision_id: null,
    created_by: null,
    merged_at: null,
    merged_by: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

const MAIN = makeBranch({ id: 'main-1', name: 'main', kind: 'main', status: 'merged' })
const FEATURE = makeBranch({
  id: 'feat-1',
  name: 'checkout-v2',
  kind: 'working',
  status: 'approved',
})

/** A page form holding the page guard, the way EventForm does. */
function GuardedForm() {
  const [value, setValue] = useState('')
  const guard = useUnsavedChangesGuard(value !== '')
  return (
    <>
      {guard.dialog}
      <label>
        Draft
        <input value={value} onChange={e => setValue(e.target.value)} />
      </label>
    </>
  )
}

function renderSwitcher({ withForm = false } = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <BranchSwitcher slug="demo" />
        {withForm && <GuardedForm />}
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

afterEach(() => {
  vi.clearAllMocks()
  branchState.id = null
})

describe('BranchSwitcher', () => {
  it('shows the active branch label on the trigger (defaults to main)', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })

    renderSwitcher()

    const trigger = await screen.findByTitle(/^Switch branch/)
    expect(within(trigger).getByText('main')).toBeInTheDocument()
  })

  it('reads "loading" rather than "main" while a selected branch is resolving', async () => {
    branchState.id = FEATURE.id
    let resolve: (value: { items: PlanBranchSummary[]; total: number }) => void = () => {}
    vi.mocked(planBranchesApi.list).mockReturnValue(new Promise((r) => { resolve = r }))

    renderSwitcher()

    const trigger = await screen.findByTitle(/^Switch branch/)
    expect(within(trigger).getByText('loading…')).toBeInTheDocument()
    expect(within(trigger).queryByText('main')).not.toBeInTheDocument()

    resolve({ items: [MAIN, FEATURE], total: 2 })
    await waitFor(() => expect(within(trigger).getByText('checkout-v2')).toBeInTheDocument())
  })

  it('opens a Plan branches dropdown listing main and feature branches with a New branch action', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })

    renderSwitcher()

    fireEvent.click(await screen.findByTitle(/^Switch branch/))

    expect(await screen.findByText('Plan branches')).toBeInTheDocument()
    expect(screen.getByText('checkout-v2')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /New branch from main/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Manage branches/i })).toBeInTheDocument()
    // Each working branch says where its review stands.
    expect(screen.getByText('Approved')).toBeInTheDocument()
  })

  it('shows the active branch status, not a "feature" chip, and names it in full', async () => {
    branchState.id = FEATURE.id
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })

    renderSwitcher()

    const trigger = await screen.findByTitle('Switch branch (current: checkout-v2)')
    expect(within(trigger).getAllByText('Approved')).toHaveLength(1)
    expect(within(trigger).queryByText('feature')).not.toBeInTheDocument()
  })

  it('selects a feature branch via setBranchId', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })

    renderSwitcher()

    fireEvent.click(await screen.findByTitle(/^Switch branch/))
    fireEvent.click(await screen.findByText('checkout-v2'))

    expect(setBranchId).toHaveBeenCalledWith('feat-1')
  })

  it('excludes merged and closed branches from the working list', async () => {
    const merged = makeBranch({ id: 'm', name: 'merged-branch', kind: 'working', status: 'merged' })
    const closed = makeBranch({ id: 'c', name: 'closed-branch', kind: 'working', status: 'closed' })
    vi.mocked(planBranchesApi.list).mockResolvedValue({
      items: [MAIN, FEATURE, merged, closed],
      total: 4,
    })

    renderSwitcher()

    fireEvent.click(await screen.findByTitle(/^Switch branch/))
    await screen.findByText('Plan branches')

    expect(screen.getByText('checkout-v2')).toBeInTheDocument()
    expect(screen.queryByText('merged-branch')).not.toBeInTheDocument()
    expect(screen.queryByText('closed-branch')).not.toBeInTheDocument()
  })

  it('asks the page guard before switching away from a dirty form', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })

    renderSwitcher({ withForm: true })
    fireEvent.change(screen.getByLabelText('Draft'), { target: { value: 'draft' } })

    fireEvent.click(await screen.findByTitle(/^Switch branch/))
    fireEvent.click(await screen.findByText('checkout-v2'))

    await screen.findByRole('alertdialog', { name: 'Leave without saving?' })
    expect(setBranchId).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(setBranchId).not.toHaveBeenCalled()

    fireEvent.click(screen.getByTitle(/^Switch branch/))
    fireEvent.click(await screen.findByText('checkout-v2'))
    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(setBranchId).toHaveBeenCalledWith('feat-1'))
  })

  it('switches at once when the guarded form is clean', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({ items: [MAIN, FEATURE], total: 2 })

    renderSwitcher({ withForm: true })
    fireEvent.click(await screen.findByTitle(/^Switch branch/))
    fireEvent.click(await screen.findByText('checkout-v2'))

    expect(setBranchId).toHaveBeenCalledWith('feat-1')
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })
})
