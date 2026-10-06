import { act, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import type { PlanBranchDiffSummary, PlanDiffEntry } from '@/types'
import { ChangeRow } from './ChangeRow'
import { TransferBar } from './TransferBar'
import { useTransferSelection } from './useTransferSelection'

const ADDED: PlanDiffEntry = {
  entity_type: 'event',
  kind: 'added',
  name: 'signup',
  parent: 'track',
  entity_id: 'e-1',
  changes: [],
  field_changes: [],
}

const RENAMED_FROM: PlanDiffEntry = {
  entity_type: 'variable',
  kind: 'removed',
  name: 'currency',
  parent: null,
  entity_id: 'm-1',
  changes: [],
  field_changes: [],
  before: { source_name: 'S_CUR' },
}

const RENAMED_TO: PlanDiffEntry = {
  entity_type: 'variable',
  kind: 'added',
  name: 'currency_code',
  parent: null,
  entity_id: 'v-1',
  changes: [],
  field_changes: [],
  after: { source_name: 'S_CUR' },
}

function diffOf(entries: PlanDiffEntry[]): PlanBranchDiffSummary {
  return {
    entries,
    summary: { added: 0, removed: 0, changed: 0 },
    behind_base: false,
  } as PlanBranchDiffSummary
}

describe('ChangeRow selection', () => {
  it('offers a checkbox only when selectable, and ticking does not expand the row', () => {
    const onToggleSelect = vi.fn()
    const { rerender } = render(
      <MemoryRouter>
        <ChangeRow slug="demo" branchId="br-1" entry={ADDED} editable reverting={false} />
      </MemoryRouter>,
    )
    expect(screen.queryByRole('checkbox', { name: 'Select signup' })).toBeNull()

    rerender(
      <MemoryRouter>
        <ChangeRow
          slug="demo"
          branchId="br-1"
          entry={ADDED}
          editable
          reverting={false}
          selectable
          selected={false}
          onToggleSelect={onToggleSelect}
        />
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select signup' }))
    expect(onToggleSelect).toHaveBeenCalledWith(ADDED)
    expect(screen.getByRole('button', { name: /signup/ })).toHaveAttribute(
      'aria-expanded',
      'false',
    )
  })
})

describe('useTransferSelection', () => {
  it('sends both halves of a rename and drops rows the diff no longer has', () => {
    const { result, rerender } = renderHook(({ diff }) => useTransferSelection(diff), {
      initialProps: { diff: diffOf([ADDED, RENAMED_FROM, RENAMED_TO]) },
    })
    act(() => result.current.toggle(RENAMED_FROM))
    expect(result.current.count).toBe(1)
    expect(result.current.entries).toEqual([RENAMED_FROM, RENAMED_TO])

    rerender({ diff: diffOf([ADDED]) })
    expect(result.current.count).toBe(0)
    expect(result.current.entries).toEqual([])
  })
})

describe('TransferBar', () => {
  it('reads "N selected" and offers move only where moving is allowed', () => {
    const onTransfer = vi.fn()
    const { rerender } = render(
      <TransferBar count={2} canMove onTransfer={onTransfer} onClear={vi.fn()} />,
    )
    expect(screen.getByText('2 selected')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Move to branch…' }))
    expect(onTransfer).toHaveBeenCalledWith('move')

    rerender(<TransferBar count={2} canMove={false} onTransfer={onTransfer} onClear={vi.fn()} />)
    expect(screen.queryByRole('button', { name: 'Move to branch…' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Copy to branch…' }))
    expect(onTransfer).toHaveBeenCalledWith('copy')

    rerender(<TransferBar count={0} canMove onTransfer={onTransfer} onClear={vi.fn()} />)
    expect(screen.queryByRole('toolbar')).toBeNull()
  })
})
