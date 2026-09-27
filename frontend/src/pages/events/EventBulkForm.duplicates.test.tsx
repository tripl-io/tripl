import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DuplicateCheckResult, EventType } from '@/types'
import { duplicatesApi } from '@/api/duplicates'
import { eventsApi } from '@/api/events'
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
  duplicatesApi: { check: vi.fn(), clusters: vi.fn(), dismiss: vi.fn() },
}))
vi.mock('@/api/users', () => ({ usersApi: { list: vi.fn().mockResolvedValue([]) } }))

// No naming rule: one free name per line.
const SCREEN_TYPE = {
  id: 'et-screen',
  name: 'screen',
  display_name: 'Screen',
  event_name_format: null,
  field_definitions: [],
} as unknown as EventType

const QUIET: DuplicateCheckResult = { duplicates: [], lint: [], suggestion: null }
const LOOKALIKE: DuplicateCheckResult = {
  duplicates: [
    {
      event_id: 'ev-paywall',
      name: 'paywall_view',
      event_type_id: 'et-screen',
      status: 'live',
      score: 0.94,
      reasons: [],
    },
  ],
  lint: [{ code: 'case', message: 'Screen events use snake_case.', suggestion: 'paywall_screen_view' }],
  suggestion: 'paywall_screen_view',
}

let queryClient: QueryClient

function wrapper({ children }: { children: ReactNode }) {
  return createElement(
    QueryClientProvider,
    { client: queryClient },
    createElement(
      MemoryRouter,
      { initialEntries: ['/p/demo/events/all/bulk'] },
      createElement(
        Routes,
        null,
        createElement(Route, { path: '/p/:slug/events/:tab/bulk', element: children }),
        createElement(Route, { path: '/p/:slug/events', element: null }),
      ),
    ),
  )
}

async function pasteLines(text: string) {
  render(createElement(EventBulkForm), { wrapper })
  await screen.findByRole('option', { name: 'Screen' })
  fireEvent.change(screen.getByLabelText(/Event type/), { target: { value: 'et-screen' } })
  fireEvent.change(await screen.findByLabelText('Events to create'), { target: { value: text } })
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.mocked(eventTypesApi.list).mockResolvedValue([SCREEN_TYPE])
  vi.mocked(eventsApi.byNames).mockResolvedValue({ items: [] })
  vi.mocked(duplicatesApi.check).mockReset()
})

describe('EventBulkForm duplicate hints (F12, #265)', () => {
  it('checks every line in one request and flags the look-alike on its row', async () => {
    vi.mocked(duplicatesApi.check).mockResolvedValue({ items: [QUIET, LOOKALIKE], threshold: 0.88 })
    await pasteLines('signup_started\nPaywallScreenView')

    expect(await screen.findByText(/Looks like/, {}, { timeout: 2000 })).toHaveTextContent(
      'Looks like paywall_view (94%)',
    )
    expect(duplicatesApi.check).toHaveBeenCalledTimes(1)
    expect(vi.mocked(duplicatesApi.check).mock.calls[0]?.[1]).toEqual([
      { name: 'signup_started', event_type_id: 'et-screen', field_values: [] },
      { name: 'PaywallScreenView', event_type_id: 'et-screen', field_values: [] },
    ])
    // A warning, not a refusal: both lines are still created.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Create 2 events' })).not.toBeDisabled(),
    )
  })

  it('announces the table once through one polite region; the rows stay plain text', async () => {
    vi.mocked(duplicatesApi.check).mockResolvedValue({
      items: [LOOKALIKE, QUIET, LOOKALIKE],
      threshold: 0.88,
    })
    await pasteLines('PaywallScreenView\nsignup_started\nPaywallShown')

    const rows = await screen.findAllByText(/Looks like/, {}, { timeout: 2000 })
    expect(rows).toHaveLength(2)
    for (const row of rows) expect(row.closest('[role="status"], [aria-live]')).toBeNull()
    const regions = screen.getAllByTestId('duplicate-live-region')
    expect(regions).toHaveLength(1)
    expect(regions[0]).toHaveAttribute('aria-live', 'polite')
    expect(regions[0]).toHaveTextContent('2 possible duplicates')
  })

  it('"Use suggested name" rewrites that line of the paste', async () => {
    vi.mocked(duplicatesApi.check).mockResolvedValue({ items: [QUIET, LOOKALIKE], threshold: 0.88 })
    await pasteLines('signup_started\nPaywallScreenView\tPaywall shown')

    fireEvent.click(
      await screen.findByRole('button', { name: 'Use suggested name' }, { timeout: 2000 }),
    )
    expect(screen.getByRole('textbox', { name: 'Events to create' })).toHaveValue(
      'signup_started\npaywall_screen_view\tPaywall shown',
    )
  })
})
