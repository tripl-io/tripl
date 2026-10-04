import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'
import { focusManager, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { planBranchesApi } from '@/api/planBranches'
import { useBranchContext } from '@/hooks/useBranch'
import { useUnsavedChangesGuard } from '@/hooks/useUnsavedChangesGuard'
import { planBranchesKey } from '@/lib/queryKeys'
import type { PlanBranchSummary } from '@/types'
import { BranchProvider } from './branch-context'

vi.mock('@/api/planBranches', () => ({
  planBranchesApi: { list: vi.fn() },
}))
vi.mock('sonner', () => ({ toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() } }))

function branch(overrides: Partial<PlanBranchSummary>): PlanBranchSummary {
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

const MAIN = branch({ id: 'main-1', name: 'main', kind: 'main' })
const FEATURE = branch({ id: 'feat-1', name: 'checkout-v2', kind: 'working', status: 'draft' })

function listReturns(items: PlanBranchSummary[]) {
  vi.mocked(planBranchesApi.list).mockResolvedValue({ items, total: items.length })
}

function Probe() {
  const { branchId, setBranchId } = useBranchContext()
  const location = useLocation()
  const navigate = useNavigate()
  return (
    <>
      <output aria-label="branch">{branchId ?? 'main'}</output>
      <output aria-label="search">{location.search}</output>
      <button type="button" onClick={() => setBranchId(null)}>
        Switch to main
      </button>
      <button type="button" onClick={() => setBranchId(FEATURE.id)}>
        Switch to feature
      </button>
      <button type="button" onClick={() => void navigate(`/p/demo/metrics?branch=${FEATURE.id}`)}>
        Open branch link
      </button>
      <button type="button" onClick={() => void navigate('/p/demo/overview')}>
        Open plain link
      </button>
      <button type="button" onClick={() => void navigate(-1)}>
        Back
      </button>
    </>
  )
}

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

function renderProvider(
  initialEntry: string | string[] = '/p/demo/events',
  { client = new QueryClient({ defaultOptions: { queries: { retry: false } } }), withForm = false } = {},
) {
  const entries = Array.isArray(initialEntry) ? initialEntry : [initialEntry]
  return render(
    <MemoryRouter initialEntries={entries} initialIndex={entries.length - 1}>
      <QueryClientProvider client={client}>
        <BranchProvider slug="demo">
          <Probe />
          {withForm && <GuardedForm />}
        </BranchProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

// `hidden`: a modal confirm hides the page from the accessibility tree.
const branchShown = () => screen.getByRole('status', { name: 'branch', hidden: true }).textContent
const searchShown = () => screen.getByRole('status', { name: 'search', hidden: true }).textContent
/**
 * Let the list request that was just made answer and reach the provider:
 * react-query hands results to observers on a macrotask, not a microtask.
 */
const settle = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 20)))

beforeEach(() => {
  localStorage.clear()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('BranchProvider', () => {
  it('drops a stored branch that has since been merged, and says so', async () => {
    localStorage.setItem('tripl-branch:demo', FEATURE.id)
    listReturns([MAIN, { ...FEATURE, status: 'merged' }])
    renderProvider()

    await waitFor(() => expect(branchShown()).toBe('main'))
    expect(toast.info).toHaveBeenCalledWith(
      'Branch checkout-v2 was merged; switched to main.',
      expect.anything(),
    )
    expect(localStorage.getItem('tripl-branch:demo')).toBeNull()
  })

  it('keeps a merged branch that a link opens deliberately, as a read-only view', async () => {
    // A merged branch's diff links here with ?branch= (BranchesTab, the entity
    // banner's "Switch to"); dropping it would make those links lie.
    listReturns([MAIN, { ...FEATURE, status: 'merged' }])
    renderProvider(`/p/demo/events?branch=${FEATURE.id}&q=x`)

    await waitFor(() => expect(planBranchesApi.list).toHaveBeenCalled())
    await settle()
    expect(branchShown()).toBe(FEATURE.id)
    expect(searchShown()).toBe(`?branch=${FEATURE.id}&q=x`)
    expect(toast.info).not.toHaveBeenCalled()
  })

  it('drops a branch the URL names once it is closed while selected, and strips it from the address', async () => {
    listReturns([MAIN, FEATURE])
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderProvider(`/p/demo/events?branch=${FEATURE.id}&q=x`, { client })
    await waitFor(() => expect(planBranchesApi.list).toHaveBeenCalled())
    await settle()

    listReturns([MAIN, { ...FEATURE, status: 'closed' }])
    await act(async () => {
      await client.invalidateQueries({ queryKey: planBranchesKey('demo') })
    })
    await waitFor(() => expect(branchShown()).toBe('main'))
    await waitFor(() => expect(searchShown()).toBe('?q=x'))
  })

  it('notices a branch merged in another session when the window regains focus', async () => {
    localStorage.setItem('tripl-branch:demo', FEATURE.id)
    listReturns([MAIN, FEATURE])
    // The app's own defaults (main.tsx): no refetch on focus, fresh for 60 s.
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false, staleTime: 60_000 } },
    })
    vi.useFakeTimers({ toFake: ['Date'] })
    try {
      renderProvider('/p/demo/events', { client })
      await waitFor(() => expect(planBranchesApi.list).toHaveBeenCalledTimes(1))
      await settle()
      expect(branchShown()).toBe(FEATURE.id)

      // Merged elsewhere; the reader comes back to this tab a while later.
      listReturns([MAIN, { ...FEATURE, status: 'merged' }])
      vi.setSystemTime(Date.now() + 45_000)
      act(() => focusManager.setFocused(false))
      act(() => focusManager.setFocused(true))
      await waitFor(() => expect(branchShown()).toBe('main'))
    } finally {
      focusManager.setFocused(undefined)
      vi.useRealTimers()
    }
  })

  it('does not call a branch missing from a list that is being refreshed', async () => {
    // Created a moment ago: the cached list predates it, the refetch has it.
    const created = { ...FEATURE, id: 'new-1', name: 'fresh' }
    localStorage.setItem('tripl-branch:demo', created.id)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(planBranchesKey('demo'), { items: [MAIN], total: 1 }, { updatedAt: 0 })
    let answer: (value: { items: PlanBranchSummary[]; total: number }) => void = () => {}
    vi.mocked(planBranchesApi.list).mockReturnValue(new Promise((resolve) => { answer = resolve }))
    renderProvider('/p/demo/events', { client })

    await waitFor(() => expect(planBranchesApi.list).toHaveBeenCalled())
    expect(branchShown()).toBe(created.id)
    await act(async () => answer({ items: [MAIN, created], total: 2 }))
    await settle()
    expect(branchShown()).toBe(created.id)
    expect(toast.info).not.toHaveBeenCalled()
  })

  it('asks the page guard before Back re-adopts an older ?branch= on the same page', async () => {
    listReturns([MAIN, FEATURE])
    renderProvider([`/p/demo/events?branch=${FEATURE.id}`, `/p/demo/events?branch=${FEATURE.id}&q=x`], {
      withForm: true,
    })
    fireEvent.click(screen.getByRole('button', { name: 'Switch to main' }))
    expect(searchShown()).toBe('?q=x')
    fireEvent.change(screen.getByLabelText('Draft'), { target: { value: 'draft' } })

    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    await screen.findByRole('alertdialog', { name: 'Leave without saving?' })
    expect(branchShown()).toBe('main')

    // Keeping the draft keeps its branch, and the address says so again.
    fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(searchShown()).toBe(''))
    expect(branchShown()).toBe('main')
    expect(screen.getByLabelText('Draft')).toHaveValue('draft')
  })

  it('adopts the older ?branch= once the draft is discarded', async () => {
    listReturns([MAIN, FEATURE])
    renderProvider([`/p/demo/events?branch=${FEATURE.id}`, `/p/demo/events?branch=${FEATURE.id}&q=x`], {
      withForm: true,
    })
    fireEvent.click(screen.getByRole('button', { name: 'Switch to main' }))
    fireEvent.change(screen.getByLabelText('Draft'), { target: { value: 'draft' } })

    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(branchShown()).toBe(FEATURE.id))
    expect(searchShown()).toBe(`?branch=${FEATURE.id}`)
  })

  it('drops a branch that no longer exists', async () => {
    localStorage.setItem('tripl-branch:demo', 'deleted-branch')
    listReturns([MAIN, FEATURE])
    renderProvider()

    await waitFor(() => expect(branchShown()).toBe('main'))
    expect(toast.info).toHaveBeenCalledWith(
      'The branch you were working in no longer exists; switched to main.',
      expect.anything(),
    )
  })

  it('keeps a live branch', async () => {
    localStorage.setItem('tripl-branch:demo', FEATURE.id)
    listReturns([MAIN, FEATURE])
    renderProvider()

    await waitFor(() => expect(planBranchesApi.list).toHaveBeenCalled())
    expect(branchShown()).toBe(FEATURE.id)
    expect(toast.info).not.toHaveBeenCalled()
  })

  it('resets when the active branch is merged while the page is open', async () => {
    listReturns([MAIN, FEATURE])
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderProvider(`/p/demo/events?branch=${FEATURE.id}`, { client })
    await waitFor(() => expect(planBranchesApi.list).toHaveBeenCalled())
    await settle()
    expect(branchShown()).toBe(FEATURE.id)

    // The merge invalidates the list; the next answer reports the branch merged.
    listReturns([MAIN, { ...FEATURE, status: 'merged' }])
    await act(async () => {
      await client.invalidateQueries({ queryKey: planBranchesKey('demo') })
    })
    await waitFor(() => expect(branchShown()).toBe('main'))
  })

  it('writes a manual switch into the address, so a reload keeps it', async () => {
    listReturns([MAIN, FEATURE])
    renderProvider(`/p/demo/events?branch=${FEATURE.id}`)
    await waitFor(() => expect(planBranchesApi.list).toHaveBeenCalled())

    fireEvent.click(screen.getByRole('button', { name: 'Switch to main' }))
    expect(branchShown()).toBe('main')
    expect(searchShown()).toBe('')

    fireEvent.click(screen.getByRole('button', { name: 'Switch to feature' }))
    expect(searchShown()).toBe(`?branch=${FEATURE.id}`)
  })

  it('follows ?branch= when navigation changes it', async () => {
    listReturns([MAIN, FEATURE])
    renderProvider('/p/demo/events')
    expect(branchShown()).toBe('main')

    fireEvent.click(screen.getByRole('button', { name: 'Open branch link' }))
    expect(branchShown()).toBe(FEATURE.id)

    // An address without the param keeps the selection.
    fireEvent.click(screen.getByRole('button', { name: 'Open plain link' }))
    expect(branchShown()).toBe(FEATURE.id)
    await waitFor(() => expect(planBranchesApi.list).toHaveBeenCalled())
  })
})
