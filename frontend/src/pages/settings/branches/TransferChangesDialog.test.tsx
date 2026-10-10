import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import { planBranchesApi, type BranchTransferResult } from '@/api/planBranches'
import type { PlanBranchSummary, PlanDiffEntry } from '@/types'
import { TransferChangesDialog } from './TransferChangesDialog'

vi.mock('@/api/planBranches', () => ({
  planBranchesApi: {
    list: vi.fn(),
    transfer: vi.fn(),
    create: vi.fn(),
  },
}))

function branchRow(overrides: Partial<PlanBranchSummary>): PlanBranchSummary {
  return {
    id: 'b-src',
    project_id: 'p-1',
    name: 'TASK-1',
    kind: 'working',
    status: 'draft',
    description: '',
    base_revision_id: 'rev-1',
    created_by: null,
    merged_at: null,
    merged_by: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

const SOURCE = branchRow({})
const TASK_2 = branchRow({ id: 'b-2', name: 'TASK-2' })
const LANDED = branchRow({ id: 'b-3', name: 'TASK-3', status: 'merged' })

const SIGNUP = {
  entity_type: 'event',
  kind: 'added',
  name: 'signup',
  parent: 'track',
  entity_id: 'e-1',
  changes: [],
  field_changes: [],
  before: null,
  after: {},
  warnings: [],
  housekeeping: null,
} as unknown as PlanDiffEntry

function result(overrides: Partial<BranchTransferResult> = {}): BranchTransferResult {
  return {
    mode: 'move',
    dry_run: true,
    target_branch_id: 'b-2',
    target_branch_name: 'TASK-2',
    applied: [
      {
        entity_type: 'event',
        name: 'signup',
        parent: 'track',
        entity_id: 'e-1',
        kind: 'added',
        needed_by: null,
      },
    ],
    carried: [
      {
        entity_type: 'field_definition',
        name: 'plan',
        parent: 'track',
        entity_id: 'f-1',
        kind: 'added',
        needed_by: 'signup',
      },
    ],
    skipped: [],
    warnings: [],
    target_counts: [],
    source_diff: null,
    ...overrides,
  }
}

function refusal(detail: Record<string, unknown>): ApiError {
  const error = new ApiError('409 Conflict', 409)
  error.detail = detail
  return error
}

function renderDialog(branch: PlanBranchSummary = SOURCE, mode: 'move' | 'copy' = 'move') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onDone = vi.fn()
  const onOpenChange = vi.fn()
  render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <TransferChangesDialog
          slug="demo"
          branch={branch}
          entries={[SIGNUP]}
          renamePairs={[]}
          mode={mode}
          open
          onOpenChange={onOpenChange}
          onDone={onDone}
        />
      </QueryClientProvider>
    </MemoryRouter>,
  )
  return { onDone, onOpenChange }
}

describe('TransferChangesDialog', () => {
  beforeEach(() => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({
      items: [SOURCE, TASK_2, LANDED],
      total: 3,
    })
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('offers open branches only and lists what the dry run carries', async () => {
    vi.mocked(planBranchesApi.transfer).mockResolvedValue(result())
    renderDialog()

    fireEvent.click(await screen.findByRole('radio', { name: 'TASK-2' }))
    expect(screen.queryByRole('radio', { name: 'TASK-3' })).not.toBeInTheDocument()
    expect(screen.queryByRole('radio', { name: 'TASK-1' })).not.toBeInTheDocument()

    expect(await screen.findByText('Field plan (needed by signup)')).toBeInTheDocument()
    expect(screen.getByText('Event signup')).toBeInTheDocument()
    expect(planBranchesApi.transfer).toHaveBeenCalledWith('demo', 'b-src', {
      target_branch_id: 'b-2',
      mode: 'move',
      entries: [{ entity_type: 'event', name: 'signup', parent: 'track', entity_id: 'e-1' }],
      dry_run: true,
    })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Move changes' })).toBeEnabled(),
    )
  })

  it('keeps confirm off while a row is refused', async () => {
    vi.mocked(planBranchesApi.transfer).mockRejectedValue(
      refusal({
        transfer_conflicts: [
          {
            entity_type: 'variable',
            name: 'currency',
            parent: null,
            field: 'description',
            reason: 'target_changed',
            message: "'currency' changed 'description' on 'TASK-2' too.",
          },
        ],
        message: "'currency' changed 'description' on 'TASK-2' too.",
      }),
    )
    renderDialog()

    fireEvent.click(await screen.findByRole('radio', { name: 'TASK-2' }))
    expect(
      await screen.findByText(/Property currency · description: 'currency' changed/),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Move changes' })).toBeDisabled()
  })

  it('previews a new branch before creating it, then creates and transfers', async () => {
    vi.mocked(planBranchesApi.transfer).mockImplementation(async (_slug, _id, body) =>
      result({ dry_run: body.dry_run ?? false, target_branch_id: body.target_branch_id }),
    )
    vi.mocked(planBranchesApi.create).mockResolvedValue(branchRow({ id: 'b-new', name: 'TASK-9' }))
    const { onDone } = renderDialog()

    fireEvent.click(await screen.findByRole('radio', { name: 'New branch…' }))
    fireEvent.change(screen.getByLabelText('Branch name'), { target: { value: 'TASK-9' } })
    await screen.findByText('Field plan (needed by signup)')
    expect(planBranchesApi.transfer).toHaveBeenCalledWith(
      'demo',
      'b-src',
      expect.objectContaining({ target_branch_id: null, dry_run: true }),
    )
    expect(planBranchesApi.create).not.toHaveBeenCalled()

    const confirm = screen.getByRole('button', { name: 'Move changes' })
    await waitFor(() => expect(confirm).toBeEnabled())
    fireEvent.click(confirm)

    await waitFor(() => expect(onDone).toHaveBeenCalled())
    expect(planBranchesApi.create).toHaveBeenCalledWith('demo', { name: 'TASK-9', description: '' })
    expect(planBranchesApi.transfer).toHaveBeenCalledWith('demo', 'b-src', {
      target_branch_id: 'b-new',
      mode: 'move',
      entries: [{ entity_type: 'event', name: 'signup', parent: 'track', entity_id: 'e-1' }],
    })
  })

  it('creates no branch when the new-branch preview is refused', async () => {
    vi.mocked(planBranchesApi.transfer).mockRejectedValue(
      refusal({
        transfer_base_mismatch: true,
        message: "'TASK-1' was cut from an older main. Run Update from main on 'TASK-1', then retry.",
        behind_branch_ids: ['b-src'],
      }),
    )
    renderDialog()

    fireEvent.click(await screen.findByRole('radio', { name: 'New branch…' }))
    fireEvent.change(screen.getByLabelText('Branch name'), { target: { value: 'TASK-9' } })
    expect(await screen.findByText(/Run Update from main on 'TASK-1'/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open TASK-1 to update it' })).toBeInTheDocument()
    const confirm = screen.getByRole('button', { name: 'Move changes' })
    expect(confirm).toBeDisabled()
    fireEvent.click(confirm)
    expect(planBranchesApi.create).not.toHaveBeenCalled()
  })

  it('offers only copy off a closed branch', async () => {
    vi.mocked(planBranchesApi.transfer).mockResolvedValue(result({ mode: 'copy' }))
    renderDialog(branchRow({ status: 'closed' }), 'move')

    expect(screen.getByRole('button', { name: 'Move' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Copy' })).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(await screen.findByRole('radio', { name: 'TASK-2' }))
    expect(await screen.findByRole('button', { name: 'Copy changes' })).toBeInTheDocument()
    expect(planBranchesApi.transfer).toHaveBeenCalledWith(
      'demo',
      'b-src',
      expect.objectContaining({ mode: 'copy' }),
    )
  })

  it('asks before Escape drops a new branch being written', async () => {
    vi.mocked(planBranchesApi.transfer).mockResolvedValue(result())
    const { onOpenChange } = renderDialog()

    fireEvent.click(await screen.findByRole('radio', { name: 'New branch…' }))
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'Signup rework' } })
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })

    expect(await screen.findByRole('alertdialog')).toHaveTextContent('Leave without saving?')
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
  })

  it('closes from Cancel at once when nothing was picked', async () => {
    const { onOpenChange } = renderDialog()
    await screen.findByRole('radio', { name: 'TASK-2' })

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(onOpenChange).toHaveBeenCalledWith(false)
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })
})
