import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { toast } from 'sonner'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import { planBranchesApi } from '@/api/planBranches'
import type {
  PlanBranchConflicts,
  PlanBranchSummary,
  UpdateFromMainPreview,
  UpdateFromMainResult,
} from '@/types'
import { UpdateFromMainDialog } from './UpdateFromMainDialog'

vi.mock('@/api/planBranches', () => ({
  planBranchesApi: {
    getUpdatePreview: vi.fn(),
    updateFromMain: vi.fn(),
  },
}))

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

const OVERLAPS: PlanBranchConflicts = {
  entities: [
    {
      entity_type: 'variable',
      name: 'plan',
      parent: null,
      label: 'plan',
      fields: [
        {
          field: 'description',
          base: 'old',
          ours: 'main edit',
          theirs: 'branch edit',
          choice: null,
          dependents: 0,
        },
      ],
    },
    {
      entity_type: 'event',
      name: 'checkout.paid',
      parent: 'checkout',
      label: 'paid',
      fields: [
        {
          field: '@presence',
          base: 'present',
          ours: 'absent',
          theirs: 'present',
          choice: 'theirs',
          dependents: 0,
        },
      ],
    },
  ],
  unresolved_count: 1,
  behind: true,
  overlap_count: 2,
  merge_blocked: true,
}

function preview(overrides: Partial<UpdateFromMainPreview> = {}): UpdateFromMainPreview {
  return {
    behind: true,
    updatable: true,
    blockers: [],
    base_revision_id: 'rev-1',
    main_hash: 'hash-1',
    main_changes: [
      { entity_type: 'event', added: 1, changed: 3, removed: 0, renamed: 0 },
      { entity_type: 'variable', added: 0, changed: 1, removed: 0, renamed: 0 },
    ],
    conflicts: OVERLAPS,
    ...overrides,
  }
}

function result(overrides: Partial<UpdateFromMainResult> = {}): UpdateFromMainResult {
  return {
    updated: true,
    branch: { ...BRANCH, reviewers: [], approvals: [] },
    applied: [{ entity_type: 'event', added: 1, changed: 3, removed: 0, renamed: 0 }],
    previous_base_revision_id: 'rev-1',
    base_revision_id: 'rev-2',
    ...overrides,
  }
}

function refusal(detail: Record<string, unknown>): ApiError {
  const error = new ApiError('409 Conflict', 409)
  error.detail = detail
  return error
}

function renderDialog(branch: PlanBranchSummary = BRANCH) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const invalidate = vi.spyOn(qc, 'invalidateQueries')
  const onOpenChange = vi.fn()
  render(
    <QueryClientProvider client={qc}>
      <UpdateFromMainDialog slug="demo" branch={branch} open onOpenChange={onOpenChange} />
    </QueryClientProvider>,
  )
  return { invalidate, onOpenChange }
}

const updateButton = () => screen.getByRole('button', { name: 'Update branch' })

describe('UpdateFromMainDialog', () => {
  beforeEach(() => {
    vi.mocked(planBranchesApi.getUpdatePreview).mockResolvedValue(preview())
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('says what main brings, one line per entity type', async () => {
    renderDialog()

    expect(await screen.findByText('Events: 3 changed, 1 added')).toBeInTheDocument()
    expect(screen.getByText('Properties: 1 changed')).toBeInTheDocument()
    expect(planBranchesApi.getUpdatePreview).toHaveBeenCalledWith('demo', 'feat-1')
  })

  it('renders every entity type, grouped, with presence rows in words', async () => {
    renderDialog()

    // The group heading; the row inside it repeats the type before the name.
    expect(await screen.findByRole('heading', { level: 3, name: 'Property' })).toBeInTheDocument()
    expect(screen.getByText('Event in checkout')).toBeInTheDocument()
    expect(screen.getByText(/Deleted on main · edited here/)).toBeInTheDocument()
    expect(screen.getByText(/Take main deletes this event on the branch/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Keep this branch' })).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: 'Take main' })).toHaveLength(2)
    // No git words on screen.
    expect(screen.queryByText(/\bours\b|\btheirs\b/)).not.toBeInTheDocument()
  })

  it('holds the update until every overlap has a side, then sends the choices and the hash', async () => {
    vi.mocked(planBranchesApi.updateFromMain).mockResolvedValue(result())
    renderDialog()

    await screen.findByText('Events: 3 changed, 1 added')
    expect(screen.getByText('1 left to choose')).toBeInTheDocument()
    expect(updateButton()).toBeDisabled()

    const [takeMain] = screen.getAllByRole('button', { name: 'Take main' })
    fireEvent.click(takeMain!)

    expect(screen.queryByText(/left to choose/)).not.toBeInTheDocument()
    expect(updateButton()).toBeEnabled()
    fireEvent.click(updateButton())

    await waitFor(() =>
      expect(planBranchesApi.updateFromMain).toHaveBeenCalledWith('demo', 'feat-1', {
        expected_main_hash: 'hash-1',
        resolutions: [
          { entity_type: 'variable', entity_name: 'plan', field_name: 'description', choice: 'ours' },
          // The stored choice goes too, so one call carries every decision.
          { entity_type: 'event', entity_name: 'checkout.paid', field_name: '@presence', choice: 'theirs' },
        ],
      }),
    )
  })

  it('toasts, invalidates the branch caches and closes on success', async () => {
    const success = vi.spyOn(toast, 'success')
    vi.mocked(planBranchesApi.getUpdatePreview).mockResolvedValue(
      preview({ conflicts: { entities: [], unresolved_count: 0, behind: true } }),
    )
    vi.mocked(planBranchesApi.updateFromMain).mockResolvedValue(result())
    const { invalidate, onOpenChange } = renderDialog()

    await screen.findByText('Events: 3 changed, 1 added')
    fireEvent.click(updateButton())

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
    expect(success).toHaveBeenCalledWith('Branch updated from main — 4 changes brought in')
    const keys = invalidate.mock.calls.map(([filters]) => filters?.queryKey?.[0])
    for (const key of [
      'planBranchDiff',
      'planBranchDetail',
      'planBranchConflicts',
      'planBranchCounts',
      'planBranches',
      'planBranchUpdatePreview',
    ]) {
      expect(keys).toContain(key)
    }
  })

  it('re-renders from the refusal when the server finds an overlap unresolved', async () => {
    vi.mocked(planBranchesApi.getUpdatePreview).mockResolvedValue(
      preview({ conflicts: { entities: [], unresolved_count: 0, behind: true } }),
    )
    vi.mocked(planBranchesApi.updateFromMain).mockRejectedValue(
      refusal({
        unresolved_conflicts: [{ entity_type: 'variable', name: 'plan', field: 'description' }],
        conflicts: { ...OVERLAPS, entities: [OVERLAPS.entities[0]!] },
      }),
    )
    renderDialog()

    await screen.findByText('Events: 3 changed, 1 added')
    fireEvent.click(updateButton())

    expect(await screen.findByText('1 left to choose')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'Property' })).toBeInTheDocument()
    expect(updateButton()).toBeDisabled()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('refetches the preview and says so when main moved in between', async () => {
    vi.mocked(planBranchesApi.getUpdatePreview).mockResolvedValue(
      preview({ conflicts: { entities: [], unresolved_count: 0, behind: true } }),
    )
    vi.mocked(planBranchesApi.updateFromMain).mockRejectedValue(refusal({ main_moved: true }))
    renderDialog()

    await screen.findByText('Events: 3 changed, 1 added')
    fireEvent.click(updateButton())

    expect(await screen.findByText('Main changed again — review the new changes.')).toBeInTheDocument()
    await waitFor(() => expect(planBranchesApi.getUpdatePreview).toHaveBeenCalledTimes(2))
  })

  it('asks again for every pick once main moved, and refreshes the header', async () => {
    vi.mocked(planBranchesApi.updateFromMain).mockRejectedValue(refusal({ main_moved: true }))
    const { invalidate } = renderDialog()

    await screen.findByText('Events: 3 changed, 1 added')
    const [takeMain] = screen.getAllByRole('button', { name: 'Take main' })
    fireEvent.click(takeMain!)
    expect(updateButton()).toBeEnabled()
    fireEvent.click(updateButton())

    expect(await screen.findByText('Main changed again — review the new changes.')).toBeInTheDocument()
    // The pick was made against main as it was: the row is open again.
    await waitFor(() => expect(screen.getByText('1 left to choose')).toBeInTheDocument())
    expect(updateButton()).toBeDisabled()
    const keys = invalidate.mock.calls.map(([filters]) => filters?.queryKey?.[0])
    expect(keys).toContain('planBranchConflicts')
  })

  it('refreshes the header overlaps on an unresolved refusal too', async () => {
    vi.mocked(planBranchesApi.getUpdatePreview).mockResolvedValue(
      preview({ conflicts: { entities: [], unresolved_count: 0, behind: true } }),
    )
    vi.mocked(planBranchesApi.updateFromMain).mockRejectedValue(
      refusal({
        unresolved_conflicts: [{ entity_type: 'variable', name: 'plan', field: 'description' }],
        conflicts: { ...OVERLAPS, entities: [OVERLAPS.entities[0]!] },
      }),
    )
    const { invalidate } = renderDialog()

    await screen.findByText('Events: 3 changed, 1 added')
    fireEvent.click(updateButton())

    expect(await screen.findByText('1 left to choose')).toBeInTheDocument()
    const keys = invalidate.mock.calls.map(([filters]) => filters?.queryKey?.[0])
    expect(keys).toContain('planBranchConflicts')
  })

  it('names what stops the update and keeps it disabled', async () => {
    vi.mocked(planBranchesApi.getUpdatePreview).mockResolvedValue(
      preview({
        updatable: false,
        blockers: [
          {
            kind: 'identity_clash',
            entity_type: 'variable',
            name: 'money',
            message: "Rename the branch's one, then update.",
          },
        ],
        conflicts: { entities: [], unresolved_count: 0, behind: true },
      }),
    )
    renderDialog()

    expect(await screen.findByText("Rename the branch's one, then update.")).toBeInTheDocument()
    expect(updateButton()).toBeDisabled()
  })

  it('counts what taking main removes under a deleted parent', async () => {
    vi.mocked(planBranchesApi.getUpdatePreview).mockResolvedValue(
      preview({
        conflicts: {
          entities: [
            {
              entity_type: 'event_type',
              name: 'checkout',
              parent: null,
              label: 'checkout',
              fields: [
                {
                  field: '@presence',
                  base: 'present',
                  ours: 'absent',
                  theirs: 'present',
                  choice: null,
                  dependents: 3,
                },
              ],
            },
          ],
          unresolved_count: 1,
          behind: true,
        },
      }),
    )
    renderDialog()

    expect(
      await screen.findByText(/Taking main also removes 3 entities this branch added or edited under it/),
    ).toBeInTheDocument()
  })

  it('words any other refusal inline', async () => {
    vi.mocked(planBranchesApi.getUpdatePreview).mockResolvedValue(
      preview({ conflicts: { entities: [], unresolved_count: 0, behind: true } }),
    )
    vi.mocked(planBranchesApi.updateFromMain).mockRejectedValue(
      refusal({ incomplete_base_snapshot: true }),
    )
    renderDialog()

    await screen.findByText('Events: 3 changed, 1 added')
    fireEvent.click(updateButton())

    expect(await screen.findByRole('alert')).toHaveTextContent(/Copy your changes to a new branch/)
  })

  it('has nothing to do on a branch that already has everything on main', async () => {
    vi.mocked(planBranchesApi.getUpdatePreview).mockResolvedValue(
      preview({ behind: false, main_changes: [], conflicts: { entities: [], unresolved_count: 0 } }),
    )
    renderDialog()

    expect(await screen.findByText('This branch already has everything on main.')).toBeInTheDocument()
    expect(updateButton()).toBeDisabled()
  })

  it('warns an approved branch that its approvals will need renewing', async () => {
    renderDialog({ ...BRANCH, status: 'approved' })

    expect(await screen.findByText('Existing approvals will need renewing.')).toBeInTheDocument()
  })
})
