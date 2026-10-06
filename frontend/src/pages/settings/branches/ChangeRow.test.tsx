import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import type { PlanDiffEntry } from '@/types'
import { ChangeRow } from './ChangeRow'

function eventEntry(overrides: Partial<PlanDiffEntry> = {}): PlanDiffEntry {
  return {
    entity_type: 'event',
    kind: 'changed',
    name: 'checkout_started',
    parent: 'track',
    entity_id: 'b-1',
    changes: ['title'],
    field_changes: [{ field: 'title', before: 'a', after: 'b' }],
    ...overrides,
  }
}

const variableEntry: PlanDiffEntry = {
  entity_type: 'variable',
  kind: 'changed',
  name: 'plan',
  parent: null,
  entity_id: 'bv-1',
  changes: ['event_value_overrides'],
  field_changes: [
    {
      field: 'event_value_overrides',
      before: [],
      after: [],
      items: [{ key: 'track.checkout_done', kind: 'changed', before: ['free'], after: ['free', 'pro'] }],
    },
  ],
  before: {
    event_value_overrides: [{ event_type_name: 'track', event_name: 'checkout_done', values: ['free'] }],
  },
  after: {
    event_value_overrides: [
      { event_type_name: 'track', event_name: 'checkout_done', values: ['free', 'pro'] },
    ],
  },
}

function renderRow(props: Partial<Parameters<typeof ChangeRow>[0]> & { entry: PlanDiffEntry }) {
  return render(
    <MemoryRouter>
      <ChangeRow slug="demo" branchId="br-1" editable reverting={false} {...props} />
    </MemoryRouter>,
  )
}

describe('ChangeRow "As merged"', () => {
  it('offers it on an event row with the branch-side id', () => {
    const onPreviewMerged = vi.fn()
    renderRow({ entry: eventEntry(), onPreviewMerged })
    fireEvent.click(screen.getByRole('button', { name: 'As merged: checkout_started' }))
    expect(onPreviewMerged).toHaveBeenCalledWith({ eventId: 'b-1' })
  })

  it('passes the paired addition id for a rename', () => {
    const onPreviewMerged = vi.fn()
    renderRow({
      entry: eventEntry({ kind: 'removed', entity_id: 'm-1' }),
      renamedTo: 'checkout_begun',
      renamedEntityId: 'b-9',
      onPreviewMerged,
    })
    fireEvent.click(screen.getByRole('button', { name: 'As merged: checkout_begun' }))
    expect(onPreviewMerged).toHaveBeenCalledWith({ eventId: 'b-9' })
  })

  it('links each per-event override item of a variable row by type and name', () => {
    const onPreviewMerged = vi.fn()
    renderRow({ entry: variableEntry, onPreviewMerged })
    // The variable row itself is no event: no row-level button.
    expect(screen.queryByRole('button', { name: 'As merged: plan' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /plan/ }))
    fireEvent.click(screen.getByRole('button', { name: 'As merged: track.checkout_done' }))
    expect(onPreviewMerged).toHaveBeenCalledWith({
      eventType: 'track',
      eventName: 'checkout_done',
    })
  })

  it('offers nothing without the callback', () => {
    renderRow({ entry: eventEntry() })
    expect(screen.queryByRole('button', { name: /^As merged/ })).toBeNull()
    renderRow({ entry: variableEntry })
    fireEvent.click(screen.getAllByRole('button', { name: /plan/ })[0]!)
    expect(screen.queryByRole('button', { name: /^As merged/ })).toBeNull()
  })
})
