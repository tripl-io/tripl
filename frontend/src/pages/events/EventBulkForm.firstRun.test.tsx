import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { EventType } from '@/types'
import { eventTypesApi } from '@/api/eventTypes'
import EventBulkForm from './EventBulkForm'

vi.mock('@/api/events', () => ({
  eventsApi: {
    list: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    byNames: vi.fn().mockResolvedValue({ items: [] }),
    bulkCreate: vi.fn().mockResolvedValue([]),
  },
}))
vi.mock('@/api/eventTypes', () => ({ eventTypesApi: { list: vi.fn() } }))
vi.mock('@/api/duplicates', () => ({
  MAX_DUPLICATE_CANDIDATES: 500,
  duplicatesApi: {
    check: vi.fn().mockResolvedValue({ items: [], threshold: 0.88 }),
    clusters: vi.fn(),
    dismiss: vi.fn(),
  },
}))
vi.mock('@/api/users', () => ({ usersApi: { list: vi.fn().mockResolvedValue([]) } }))

const PLAIN_TYPE = {
  id: 'et-plain',
  name: 'plain',
  display_name: 'Plain',
  event_name_format: null,
  field_definitions: [],
} as unknown as EventType

let queryClient: QueryClient

function wrapper({ children }: { children: ReactNode }) {
  return createElement(
    QueryClientProvider,
    { client: queryClient },
    createElement(
      MemoryRouter,
      { initialEntries: ['/p/demo/events/all/bulk'] },
      createElement(Routes, null, createElement(Route, { path: '/p/:slug/events/:tab/bulk', element: children })),
    ),
  )
}

function saveBarStatus() {
  return document.querySelector('[data-slot="save-bar"] [role="status"]')
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

describe('EventBulkForm before anything is chosen', () => {
  it('opens on a muted next step and shows where the events will go', async () => {
    vi.mocked(eventTypesApi.list).mockResolvedValue([PLAIN_TYPE])
    render(createElement(EventBulkForm), { wrapper })

    await waitFor(() =>
      expect(saveBarStatus()).toHaveTextContent('Pick an event type, then paste one event per line'),
    )
    // Not the red of a blocking error: nothing has been done wrong yet.
    expect(saveBarStatus()).toHaveClass('text-(--fg-subtle)')
    expect(saveBarStatus()).not.toHaveClass('text-(--danger)')
    const draft = screen.getByLabelText('Events to create')
    expect(draft).toBeDisabled()
    expect(draft).toHaveAttribute('placeholder', 'Pick an event type above first')

    fireEvent.change(screen.getByLabelText(/Event type/), { target: { value: 'et-plain' } })
    expect(screen.getByLabelText('Events to create')).toBeEnabled()
  })

  it('points a project with no event types at making one, with no dead Create', async () => {
    vi.mocked(eventTypesApi.list).mockResolvedValue([])
    render(createElement(EventBulkForm), { wrapper })

    expect(await screen.findByRole('link', { name: 'Create an event type' })).toHaveAttribute(
      'href',
      '/p/demo/event-types',
    )
    expect(saveBarStatus()).toHaveTextContent('Create an event type first')
    expect(screen.queryByRole('combobox', { name: /Event type/ })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Events to create')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Create \d+ events?$/ })).not.toBeInTheDocument()
  })
})
