import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { BulkActionBar } from './BulkActionBar'

function renderBar(overrides: Partial<Parameters<typeof BulkActionBar>[0]> = {}) {
  const props = {
    selectedCount: 5,
    isDeleting: false,
    isUpdating: false,
    onSetStatus: vi.fn(),
    onMarkReviewed: vi.fn(),
    onAssignOwner: vi.fn(),
    owners: [],
    onDelete: vi.fn(),
    onClear: vi.fn(),
    ...overrides,
  }
  render(<BulkActionBar {...props} />)
  return props
}

describe('BulkActionBar select-all-matching', () => {
  it('offers to widen the selection when more events match than are selected', () => {
    const onSelectAllMatching = vi.fn()
    renderBar({ selectedCount: 5, matchingTotal: 499, onSelectAllMatching })

    const button = screen.getByRole('button', { name: 'Select all 499' })
    fireEvent.click(button)
    expect(onSelectAllMatching).toHaveBeenCalledTimes(1)
  })

  it('offers "all matching" without a count when the count is not known', () => {
    // A client-side column filter narrows rows the server total still counts,
    // so "Select all 5000" over 12 visible rows was the wrong number.
    const onSelectAllMatching = vi.fn()
    renderBar({ selectedCount: 5, matchingTotal: null, onSelectAllMatching })

    fireEvent.click(screen.getByRole('button', { name: 'Select all matching' }))
    expect(onSelectAllMatching).toHaveBeenCalledTimes(1)
  })

  it('formats a large match count', () => {
    renderBar({ selectedCount: 5, matchingTotal: 12000, onSelectAllMatching: vi.fn() })

    expect(
      screen.getByRole('button', { name: 'Select all 12,000' }),
    ).toBeInTheDocument()
  })

  it('hides the affordance once the whole matching set is selected', () => {
    renderBar({ selectedCount: 499, matchingTotal: 499, onSelectAllMatching: vi.fn() })
    expect(screen.queryByRole('button', { name: /Select all/ })).toBeNull()
  })

  it('shows a pending label and disables actions while selecting all', () => {
    renderBar({
      selectedCount: 5,
      matchingTotal: 499,
      onSelectAllMatching: vi.fn(),
      isSelectingAll: true,
    })
    expect(screen.getByRole('button', { name: 'Selecting…' })).toBeDisabled()
    expect(screen.getByRole('button', { name: /Mark as verified/ })).toBeDisabled()
  })

  it('renders nothing when the selection is empty', () => {
    const { container } = render(
      <BulkActionBar
        selectedCount={0}
        isDeleting={false}
        isUpdating={false}
        onSetStatus={vi.fn()}
        onMarkReviewed={vi.fn()}
        onAssignOwner={vi.fn()}
        owners={[]}
        onDelete={vi.fn()}
        onClear={vi.fn()}
      />,
    )
    expect(container).toBeEmptyDOMElement()
  })
})

describe('BulkActionBar bulk unassign', () => {
  it('offers Unassign and reports it as a null owner', async () => {
    // `POST .../events/bulk-update` keys off which fields were SENT, so
    // `owner_id: null` is the selection-wide unassign and an omitted `owner_id`
    // is "leave it alone". The picker offered assignments only, so the one bulk
    // owner change the API has was reachable from the API and MCP alone.
    //
    // RED on a revert: drop the `Unassign` SelectItem from BulkActionBar and
    // `findByRole` throws; keep it but drop the `UNASSIGN_VALUE` mapping in
    // `onValueChange` and the callback gets the sentinel string, not `null`.
    const { onAssignOwner } = renderBar({
      owners: [{ id: 'user-1', name: 'Ada', email: 'ada@example.com' }],
    })

    fireEvent.keyDown(screen.getByRole('combobox', { name: 'Assign owner' }), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('option', { name: 'Unassign' }))

    expect(onAssignOwner).toHaveBeenCalledWith(null)
  })

  it('still reports a real owner by id', async () => {
    const { onAssignOwner } = renderBar({
      owners: [{ id: 'user-1', name: 'Ada', email: 'ada@example.com' }],
    })

    fireEvent.keyDown(screen.getByRole('combobox', { name: 'Assign owner' }), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('option', { name: 'Ada' }))

    expect(onAssignOwner).toHaveBeenCalledWith('user-1')
  })
})

describe('BulkActionBar stale-selection disclosure', () => {
  it('says how much of the selection is still on screen when they diverge', () => {
    renderBar({ selectedCount: 20, selectedVisibleCount: 3, matchingTotal: 3 })

    expect(screen.getByText('20')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(screen.getByText(/on screen/)).toBeInTheDocument()
  })

  it('stays a bare count while every selected row is on screen', () => {
    renderBar({ selectedCount: 2, selectedVisibleCount: 2, matchingTotal: 2 })

    expect(screen.queryByText(/on screen/)).toBeNull()
  })
})
