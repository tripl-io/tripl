import { render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import { at } from '@/test/at'
import type { PlannedEvent } from '@/types'
import { PlannedEventsCard } from './PlannedEventsCard'
import type { usePlannedEvents } from './usePlannedEvents'

const BASE: PlannedEvent = {
  id: 'pe',
  project_id: 'p-1',
  label: 'label',
  description: null,
  starts_at: '2026-05-02T00:00:00Z',
  ends_at: '2026-05-04T00:00:00Z',
  direction: 'spike',
  scope_type: 'event',
  scope_ref: 'e-1',
  created_by_user_id: null,
  created_at: '2026-05-01T00:00:00Z',
  updated_at: '2026-05-01T00:00:00Z',
}

function renderCard(events: PlannedEvent[], canWrite = false) {
  const query = {
    data: events,
    isError: false,
    error: null,
    refetch: () => Promise.resolve(),
  } as unknown as ReturnType<typeof usePlannedEvents>
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <PlannedEventsCard slug="demo" scope="event" scopeId="e-1" canWrite={canWrite} query={query} />
    </QueryClientProvider>,
  )
}

describe('PlannedEventsCard (F18)', () => {
  it('lists each window with what it expects and flags project-wide ones', () => {
    renderCard([
      { ...BASE, id: 'a', label: 'Black Friday' },
      { ...BASE, id: 'b', label: 'Maintenance', direction: 'drop', scope_type: null, scope_ref: null },
    ])

    expect(screen.getByText('(2)')).toBeInTheDocument()
    const items = screen.getAllByRole('listitem')
    const first = at(items, 0)
    const second = at(items, 1)
    expect(within(first).getByText('Black Friday')).toBeInTheDocument()
    expect(within(first).getByText('Expected rise')).toBeInTheDocument()
    expect(within(first).queryByText('project-wide')).not.toBeInTheDocument()
    expect(within(second).getByText('Expected drop')).toBeInTheDocument()
    expect(within(second).getByText('project-wide')).toBeInTheDocument()
  })

  it('offers no form or delete to a viewer', () => {
    renderCard([{ ...BASE, label: 'Promo' }])
    expect(screen.queryByRole('button', { name: 'Add planned event' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Delete planned event/ })).not.toBeInTheDocument()
    expect(screen.getByText(/up to an editor or owner/)).toBeInTheDocument()
  })

  it('lets an editor add once a label is typed', () => {
    renderCard([{ ...BASE, label: 'Promo' }], true)
    expect(screen.getByRole('button', { name: 'Add planned event' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Delete planned event Promo' })).toBeInTheDocument()
  })
})
