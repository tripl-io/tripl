import { render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import type { Event as TEvent, EventChange } from '@/types'
import { EventSideColumn } from './EventSideColumn'

function json(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function event(overrides: Partial<TEvent> = {}): TEvent {
  return {
    id: 'event-1',
    project_id: 'project-1',
    event_type_id: 'type-1',
    event_type: { id: 'type-1', name: 'screen', display_name: 'Screen View', color: '#0ea5e9' },
    name: 'Home Screen View',
    source_name: 'Home Screen View',
    description: 'User lands on the home screen.',
    order: 0,
    status: 'live',
    owner_id: null,
    sunset_at: null,
    superseded_by_event_id: null,
    last_seen_at: '2026-10-08T00:00:00Z',
    metric_breakdown_columns: [],
    drift_count: 0,
    tags: [],
    field_values: [],
    meta_values: [],
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-08T00:00:00Z',
    ...overrides,
  } as unknown as TEvent
}

function change(field: string, newValue: string | null, overrides: Partial<EventChange> = {}): EventChange {
  return {
    id: `ch-${field}`,
    event_id: 'event-1',
    user_id: 'u-ana',
    user_email: 'ana@example.com',
    author_label: null,
    field,
    old_value: null,
    new_value: newValue,
    created_at: '2026-10-08T00:00:00Z',
    ...overrides,
  }
}

const ANA = { id: 'u-ana', name: 'Ana Ortiz', email: 'ana@example.com' }

function installFetch(opts: { typeOwners?: unknown[]; users?: unknown[]; successor?: unknown } = {}) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
    const url = String(input)
    if (url.endsWith('/api/v1/projects/demo/event-type-owners')) return json(opts.typeOwners ?? [])
    // The roster is read page by page (`?limit=…&offset=…`).
    if (url.includes('/api/v1/users?')) return json(opts.users ?? [])
    if (url.includes('/api/v1/projects/demo/events/event-1/implementation-tickets')) return json([])
    if (url.endsWith('/api/v1/projects/demo/events/event-2')) return json(opts.successor ?? {})
    throw new Error(`Unhandled fetch: ${url}`)
  })
}

function renderColumn(ev: TEvent, history: EventChange[] = []) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <EventSideColumn
          slug="demo"
          event={ev}
          eventType={undefined}
          history={history}
          metaFieldMap={new Map()}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function ownerRow(): HTMLElement {
  const details = screen.getByRole('table', { name: 'Details' })
  return within(details).getByText('Owner').closest('[role="row"]') as HTMLElement
}

describe('EventSideColumn owner', () => {
  it("names the event type's owners, as the type's, when the event has no owner of its own", async () => {
    installFetch({
      typeOwners: [
        { id: 'o-1', event_type_id: 'type-1', user_id: 'u-1', user_email: 'audit@example.com', user_name: 'Audit Owner', granted_by: null, created_at: '2026-10-01T00:00:00Z' },
        { id: 'o-2', event_type_id: 'type-2', user_id: 'u-2', user_email: 'other@example.com', user_name: 'Someone Else', granted_by: null, created_at: '2026-10-01T00:00:00Z' },
      ],
    })
    renderColumn(event())

    expect(await within(ownerRow()).findByText('Audit Owner')).toBeInTheDocument()
    expect(ownerRow()).toHaveTextContent('Audit Owner (event type owner)')
    expect(ownerRow()).not.toHaveTextContent('Someone Else')
  })

  it('reads "—" only when neither the event nor its type has an owner', async () => {
    installFetch()
    renderColumn(event())

    expect(await within(ownerRow()).findByText('—')).toBeInTheDocument()
  })

  it("puts the event's own owner first and does not ask for the type's", async () => {
    const fetchSpy = installFetch({ users: [ANA] })
    renderColumn(event({ owner_id: 'u-ana' }))

    expect(await within(ownerRow()).findByText('Ana Ortiz')).toBeInTheDocument()
    expect(fetchSpy.mock.calls.map(([input]) => String(input))).not.toContainEqual(
      expect.stringContaining('/event-type-owners'),
    )
  })

  it('titles the card "Details", apart from the page\'s Properties list', () => {
    installFetch()
    renderColumn(event())

    expect(screen.getByRole('heading', { name: 'Details' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Properties' })).toBeNull()
  })
})

function activity(): HTMLElement {
  return screen.getByRole('heading', { name: 'Recent activity' }).parentElement as HTMLElement
}

describe('EventSideColumn recent activity', () => {
  it('reads each change as a person would, and credits people by name', async () => {
    installFetch({ users: [ANA] })
    renderColumn(event(), [
      change('status', 'live'),
      change('name', 'Home Screen View'),
      change('required_presence_threshold', '0.8'),
      change('created', 'Home View', { id: 'ch-created' }),
    ])

    const card = activity()
    expect(within(card).getByText('Status')).toBeInTheDocument()
    expect(within(card).getByText('→ Live')).toBeInTheDocument()
    expect(within(card).getByText('Name')).toBeInTheDocument()
    expect(within(card).getByText('Required presence')).toBeInTheDocument()
    expect(within(card).getByText('→ 80%')).toBeInTheDocument()
    expect(within(card).getByText('as Home View')).toBeInTheDocument()
    expect(within(card).queryByText('status')).toBeNull()

    expect(await within(card).findAllByText('· Ana Ortiz')).toHaveLength(4)
    expect(within(card).queryByText(/ana@example\.com/)).toBeNull()
  })

  it('names the current successor and not an id', async () => {
    installFetch({ successor: { ...event(), id: 'event-2', name: 'Home Screen Opened' } })
    renderColumn(event({ superseded_by_event_id: 'event-2' }), [
      change('superseded_by_event_id', 'event-2', { author_label: 'tripl (scan)', user_id: null }),
    ])

    const card = activity()
    expect(await within(card).findByText('→ Home Screen Opened')).toBeInTheDocument()
    expect(within(card).getByText('Replaced by')).toBeInTheDocument()
    expect(within(card).queryByText(/event-2/)).toBeNull()
  })

  it('names an attribute the way the Details card beside it does', () => {
    installFetch()
    renderColumn(event({ status: 'deprecated', sunset_at: '2026-12-31T09:00:00Z' }), [
      change('sunset_at', '2026-12-31 09:00:00+00:00', { author_label: 'tripl (scan)', user_id: null }),
    ])

    const details = screen.getByRole('table', { name: 'Details' })
    expect(within(details).getByText('Sunset date')).toBeInTheDocument()
    expect(within(activity()).getByText('Sunset date')).toBeInTheDocument()
    expect(screen.queryByText('Sunset')).toBeNull()
  })
})
