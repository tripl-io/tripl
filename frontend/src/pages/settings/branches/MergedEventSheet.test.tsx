import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import type { MergePreviewTarget, MergedEventPreview, MergedValue } from '@/types'
import { MergedEventSheet } from './MergedEventSheet'

vi.mock('@/api/planBranches', () => ({
  planBranchesApi: { mergePreview: vi.fn() },
}))

import { planBranchesApi } from '@/api/planBranches'

function value(overrides: Partial<MergedValue>): MergedValue {
  return { key: 'title', value: 'x', previous: 'x', state: 'unchanged', main_moved: false, ...overrides }
}

function preview(overrides: Partial<MergedEventPreview> = {}): MergedEventPreview {
  return {
    event_id: 'b-1',
    main_event_id: 'm-1',
    ref_id: 'b-1',
    event_type_name: 'track',
    name: 'checkout_started',
    outcome: 'changed',
    behind_base: false,
    blocked: false,
    branch_merge_blocked: false,
    other_blocking_count: 0,
    attributes: [
      value({ key: 'title', value: 'Start checkout', previous: 'checkout started', state: 'changed' }),
      value({
        key: 'description',
        value: 'Main wrote this later',
        previous: 'Main wrote this later',
        main_moved: true,
      }),
    ],
    field_values: [value({ key: 'name', value: 'Checkout', previous: 'Checkout' })],
    tags: [value({ key: 'funnel', value: 'funnel', previous: null, state: 'added' })],
    properties: [
      {
        name: 'plan',
        variable_type: 'string',
        required: false,
        previous_required: false,
        override: true,
        values: [
          { value: 'free', state: 'unchanged' },
          { value: 'pro', state: 'added' },
        ],
        state: 'changed',
        variable_state: 'unchanged',
        main_moved: false,
      },
    ],
    ...overrides,
  }
}

function renderSheet(
  target: MergePreviewTarget | null = { eventId: 'b-1' },
  props: Partial<Parameters<typeof MergedEventSheet>[0]> = {},
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MergedEventSheet slug="demo" branchId="br-1" target={target} onClose={vi.fn()} {...props} />
    </QueryClientProvider>,
  )
}

async function dialog() {
  return screen.findByRole('dialog', { name: 'Event after merge: checkout_started' })
}

describe('MergedEventSheet', () => {
  beforeEach(() => {
    vi.mocked(planBranchesApi.mergePreview).mockReset()
  })

  it('renders each section with the old value inline', async () => {
    vi.mocked(planBranchesApi.mergePreview).mockResolvedValue(preview())
    renderSheet()
    const sheet = await dialog()
    expect(await within(sheet).findByRole('region', { name: 'Attributes' })).toBeInTheDocument()
    expect(within(sheet).getByRole('region', { name: 'Field values' })).toBeInTheDocument()
    expect(within(sheet).getByRole('region', { name: 'Tags' })).toBeInTheDocument()
    expect(within(sheet).getByRole('region', { name: 'Properties' })).toBeInTheDocument()
    expect(within(sheet).getByText('Start checkout')).toBeInTheDocument()
    // Main's value, struck through beside the new one.
    expect(within(sheet).getByText('checkout started').closest('del')).not.toBeNull()
    expect(within(sheet).getAllByText('Changed').length).toBeGreaterThan(0)
    const values = within(sheet).getByRole('list', { name: 'plan values' })
    expect(within(values).getByText('pro')).toBeInTheDocument()
    expect(within(values).getByText('Added')).toBeInTheDocument()
  })

  it('names attributes and sections as the event form and page do', async () => {
    vi.mocked(planBranchesApi.mergePreview).mockResolvedValue(
      preview({
        attributes: [
          value({ key: 'reviewed', value: true, previous: false, state: 'changed' }),
          value({ key: 'sunset_at', value: '2026-12-31', previous: null, state: 'added' }),
          value({ key: 'superseded_by', value: 'checkout_v2', previous: null, state: 'added' }),
          value({ key: 'source_name', value: 'checkout_started', previous: 'checkout_started' }),
        ],
        meta_values: [value({ key: 'team', value: 'growth', previous: 'growth' })],
      }),
    )
    renderSheet()
    const sheet = await dialog()
    const attributes = await within(sheet).findByRole('region', { name: 'Attributes' })
    for (const label of ['Verified', 'Sunset date', 'Replaced by', 'Scan identity']) {
      expect(within(attributes).getByText(label)).toBeInTheDocument()
    }
    expect(within(sheet).getByRole('region', { name: 'Meta fields' })).toBeInTheDocument()
    expect(within(sheet).queryByRole('region', { name: 'Meta values' })).toBeNull()
  })

  it('marks a value main changed after the cut', async () => {
    vi.mocked(planBranchesApi.mergePreview).mockResolvedValue(preview())
    renderSheet()
    const sheet = await dialog()
    expect(await within(sheet).findByText('changed on main since this branch')).toBeInTheDocument()
  })

  it('marks a property whose variable alone conflicts', async () => {
    const plan = {
      ...preview().properties![0]!,
      state: 'unchanged' as const,
      variable_state: 'conflict' as const,
    }
    vi.mocked(planBranchesApi.mergePreview).mockResolvedValue(
      preview({ blocked: true, properties: [plan] }),
    )
    renderSheet()
    const sheet = await dialog()
    expect(await within(sheet).findByText('property conflicts with main')).toBeInTheDocument()
  })

  it('shows both sides of a conflict and the blocked banner', async () => {
    const onShowConflicts = vi.fn()
    vi.mocked(planBranchesApi.mergePreview).mockResolvedValue(
      preview({
        blocked: true,
        branch_merge_blocked: true,
        attributes: [
          value({ key: 'title', value: null, previous: 'Main', branch_value: 'Branch', state: 'conflict' }),
        ],
      }),
    )
    renderSheet({ eventId: 'b-1' }, { onShowConflicts, onUpdateFromMain: vi.fn() })
    const sheet = await dialog()
    expect(await within(sheet).findByText('main now')).toBeInTheDocument()
    expect(within(sheet).getByText('this branch')).toBeInTheDocument()
    expect(within(sheet).getByText('Main')).toBeInTheDocument()
    expect(within(sheet).getByText('Branch')).toBeInTheDocument()
    const link = within(sheet).getByRole('link', { name: 'See the conflicts' })
    expect(link).toHaveAttribute('href', '#branch-conflicts')
    fireEvent.click(link)
    expect(onShowConflicts).toHaveBeenCalled()
    expect(within(sheet).getByRole('button', { name: 'Update from main' })).toBeInTheDocument()
  })

  it('says a clean event is still held up by the rest of the branch', async () => {
    vi.mocked(planBranchesApi.mergePreview).mockResolvedValue(
      preview({ blocked: false, branch_merge_blocked: true, other_blocking_count: 2 }),
    )
    renderSheet()
    const sheet = await dialog()
    expect(
      await within(sheet).findByText(/This event is clean, but 2 other conflicts block the whole merge/),
    ).toBeInTheDocument()
  })

  it('lists the notes', async () => {
    vi.mocked(planBranchesApi.mergePreview).mockResolvedValue(
      preview({ notes: ['Photos changed on both sides; the merge refuses until that is settled.'] }),
    )
    renderSheet()
    const sheet = await dialog()
    expect(await within(sheet).findByText(/Photos changed on both sides/)).toBeInTheDocument()
  })

  it('hides unchanged rows by default when there are many, and shows them on request', async () => {
    const many = Array.from({ length: 13 }, (_, i) =>
      value({ key: `f${i}`, value: `v${i}`, previous: `v${i}` }),
    )
    vi.mocked(planBranchesApi.mergePreview).mockResolvedValue(preview({ field_values: many }))
    renderSheet()
    const sheet = await dialog()
    const toggle = await within(sheet).findByRole('checkbox', { name: /Hide unchanged/ })
    expect(toggle).toBeChecked()
    expect(within(sheet).queryByText('v3')).toBeNull()
    fireEvent.click(toggle)
    expect(within(sheet).getByText('v3')).toBeInTheDocument()
  })

  it('shows the refusal message of a 409 instead of the sections', async () => {
    const error = new ApiError('409 Conflict', 409)
    error.detail = {
      incomplete_base_snapshot: true,
      message: 'This branch predates the complete merge baseline.',
    }
    vi.mocked(planBranchesApi.mergePreview).mockRejectedValue(error)
    renderSheet()
    expect(
      await screen.findByText('This branch predates the complete merge baseline.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Attributes' })).toBeNull()
  })

  it('hands back the event id when opened by type and name', async () => {
    const onResolved = vi.fn()
    vi.mocked(planBranchesApi.mergePreview).mockResolvedValue(preview({ ref_id: 'b-7' }))
    renderSheet({ eventType: 'track', eventName: 'checkout_started' }, { onResolved })
    await dialog()
    await waitFor(() => expect(onResolved).toHaveBeenCalledWith('b-7'))
    expect(onResolved).toHaveBeenCalledTimes(1)
    expect(planBranchesApi.mergePreview).toHaveBeenCalledWith('demo', 'br-1', {
      eventType: 'track',
      eventName: 'checkout_started',
    })
  })

  it('hands back the event id again when the same key target is reopened', async () => {
    const onResolved = vi.fn()
    vi.mocked(planBranchesApi.mergePreview).mockResolvedValue(preview({ ref_id: 'b-7' }))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const target: MergePreviewTarget = { eventType: 'track', eventName: 'checkout_started' }
    const ui = (open: MergePreviewTarget | null) => (
      <QueryClientProvider client={client}>
        <MergedEventSheet
          slug="demo"
          branchId="br-1"
          target={open}
          onClose={vi.fn()}
          onResolved={onResolved}
        />
      </QueryClientProvider>
    )
    const { rerender } = render(ui(target))
    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1))
    // Closed (the sheet stays mounted), then the same link again.
    rerender(ui(null))
    rerender(ui({ ...target }))
    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(2))
    expect(onResolved).toHaveBeenLastCalledWith('b-7')
  })

  it('renders nothing while closed', () => {
    renderSheet(null)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(planBranchesApi.mergePreview).not.toHaveBeenCalled()
  })
})
