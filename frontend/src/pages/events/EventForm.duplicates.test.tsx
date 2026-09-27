import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import type { EventType } from '@/types'
import { duplicatesApi } from '@/api/duplicates'
import { eventsApi } from '@/api/events'
import { EventForm } from './EventForm'

vi.mock('@/api/duplicates', () => ({
  MAX_DUPLICATE_CANDIDATES: 500,
  duplicatesApi: { check: vi.fn(), clusters: vi.fn(), dismiss: vi.fn() },
}))
vi.mock('@/api/events', () => ({
  eventsApi: {
    create: vi.fn(),
    update: vi.fn(),
    byNames: vi.fn().mockResolvedValue({ items: [] }),
    list: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    get: vi.fn().mockResolvedValue({}),
  },
}))
vi.mock('@/api/dependencies', () => ({
  dependenciesApi: { impact: vi.fn().mockResolvedValue({ items: [] }), get: vi.fn() },
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/api/users', () => ({ usersApi: { list: vi.fn().mockResolvedValue([]) } }))
vi.mock('@/api/planBranches', () => ({
  planBranchesApi: { list: vi.fn().mockResolvedValue({ items: [], total: 0 }) },
}))
vi.mock('@/api/scans', () => ({ scansApi: { list: vi.fn().mockResolvedValue([]) } }))
vi.mock('@/api/ai', () => ({
  aiApi: { status: vi.fn().mockResolvedValue({ enabled: false }), describeEvent: vi.fn() },
}))

const SCREEN_TYPE = {
  id: 'et-screen',
  name: 'screen',
  display_name: 'Screen',
  field_definitions: [],
} as unknown as EventType

let queryClient: QueryClient

function wrapper({ children }: { children: ReactNode }) {
  return createElement(
    QueryClientProvider,
    { client: queryClient },
    createElement(MemoryRouter, null, children),
  )
}

const CLICK_TYPE = {
  id: 'et-click',
  name: 'click',
  display_name: 'Click',
  field_definitions: [],
} as unknown as EventType

function renderNewForm(eventTypes: EventType[] = [SCREEN_TYPE], defaultEventTypeId?: string) {
  return render(
    createElement(EventForm, {
      slug: 'demo',
      eventTypes,
      ...(defaultEventTypeId ? { defaultEventTypeId } : {}),
      metaFields: [],
      projectVariables: [],
      event: null,
      onClose: () => {},
    }),
    { wrapper },
  )
}

const LOOKALIKE = {
  duplicates: [
    {
      event_id: 'ev-paywall',
      name: 'Paywall View',
      event_type_id: 'et-screen',
      status: 'live' as const,
      score: 0.94,
      reasons: ['similar name'],
    },
  ],
  lint: [
    {
      code: 'case' as const,
      message: 'Screen events use snake_case.',
      suggestion: 'paywall_screen_view',
    },
  ],
  suggestion: 'paywall_screen_view',
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.mocked(duplicatesApi.check).mockResolvedValue({ items: [LOOKALIKE], threshold: 0.88 })
})

afterEach(() => {
  queryClient.clear()
  vi.clearAllMocks()
})

describe('EventForm duplicate hints (F12, #265)', () => {
  it('warns about a look-alike with a link to it, once typing pauses', async () => {
    renderNewForm()
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'Paywall Screen View' } })

    expect(await screen.findByText(/Looks like/, {}, { timeout: 2000 })).toHaveTextContent(
      'Looks like Paywall View (94%)',
    )
    expect(screen.getByRole('link', { name: /Open Paywall View/ })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/event/ev-paywall',
    )
    expect(duplicatesApi.check).toHaveBeenCalledWith(
      'demo',
      [{ name: 'Paywall Screen View', event_type_id: 'et-screen', field_values: [] }],
      null,
      expect.anything(),
    )
    // Advisory: Create stays available.
    expect(screen.getByRole('button', { name: /Create event/i })).not.toBeDisabled()
  })

  it('sends one request for the settled name, not one per keystroke', async () => {
    renderNewForm()
    const input = screen.getByLabelText(/Name/)
    fireEvent.change(input, { target: { value: 'Pay' } })
    fireEvent.change(input, { target: { value: 'Paywall' } })
    fireEvent.change(input, { target: { value: 'Paywall Screen View' } })

    await screen.findByText(/Looks like/, {}, { timeout: 2000 })
    expect(duplicatesApi.check).toHaveBeenCalledTimes(1)
  })

  it('rewrites the name to the suggestion', async () => {
    renderNewForm()
    const input = screen.getByLabelText(/Name/)
    fireEvent.change(input, { target: { value: 'Paywall Screen View' } })

    expect(await screen.findByText('Screen events use snake_case.', {}, { timeout: 2000 })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Use suggested name' }))
    expect(input).toHaveValue('paywall_screen_view')
  })

  it('deprecates the marked event with the new one as its successor after create', async () => {
    vi.mocked(eventsApi.create).mockResolvedValue({
      id: 'ev-new',
      name: 'Paywall Screen View',
      event_type_id: 'et-screen',
    } as never)
    vi.mocked(eventsApi.update).mockResolvedValue({} as never)
    renderNewForm()
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'Paywall Screen View' } })

    fireEvent.click(await screen.findByRole('button', { name: 'Mark as replacement' }, { timeout: 2000 }))
    expect(screen.getByText(/Replaces “Paywall View”/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Create event/i }))

    await waitFor(() =>
      expect(eventsApi.update).toHaveBeenCalledWith(
        'demo',
        'ev-paywall',
        { status: 'deprecated', superseded_by_event_id: 'ev-new' },
        null,
      ),
    )
    // The create itself carries no successor: EventCreate does not take one.
    expect(vi.mocked(eventsApi.create).mock.calls[0]?.[1]).not.toHaveProperty('superseded_by_event_id')
  })

  it('names the event a save will deprecate beside the Create button', async () => {
    renderNewForm()
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'Paywall Screen View' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Mark as replacement' }, { timeout: 2000 }))

    expect(screen.getByTestId('replaces-confirm')).toHaveTextContent(
      'Creating this event also deprecates Paywall View, with this event as its successor.',
    )
  })

  it('drops the mark once the latest answer no longer lists the marked event', async () => {
    vi.mocked(duplicatesApi.check)
      .mockResolvedValue({ items: [{ duplicates: [], lint: [] }], threshold: 0.88 })
      .mockResolvedValueOnce({ items: [LOOKALIKE], threshold: 0.88 })
    renderNewForm()
    const input = screen.getByLabelText(/Name/)
    fireEvent.change(input, { target: { value: 'Paywall Screen View' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Mark as replacement' }, { timeout: 2000 }))
    expect(screen.getByText(/Replaces “Paywall View”/)).toBeInTheDocument()

    fireEvent.change(input, { target: { value: 'Checkout Started' } })
    await waitFor(() => expect(duplicatesApi.check).toHaveBeenCalledTimes(2), { timeout: 2000 })
    await waitFor(() => expect(screen.queryByText(/Replaces “Paywall View”/)).not.toBeInTheDocument())
    expect(screen.queryByTestId('replaces-confirm')).not.toBeInTheDocument()
  })

  it('drops the mark when the event type changes', async () => {
    renderNewForm([SCREEN_TYPE, CLICK_TYPE], 'et-screen')
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'Paywall Screen View' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Mark as replacement' }, { timeout: 2000 }))
    expect(screen.getByTestId('replaces-confirm')).toBeInTheDocument()

    fireEvent.change(document.getElementById('form-event-type')!, { target: { value: 'et-click' } })
    await waitFor(() => expect(screen.queryByTestId('replaces-confirm')).not.toBeInTheDocument())
    expect(screen.queryByText(/Replaces “Paywall View”/)).not.toBeInTheDocument()
  })

  it('summarises the matches in one polite live region; the rows are plain text', async () => {
    renderNewForm()
    const region = screen.getByTestId('duplicate-live-region')
    expect(region).toHaveAttribute('aria-live', 'polite')
    expect(region).toHaveTextContent('')

    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'Paywall Screen View' } })
    const row = await screen.findByText(/Looks like/, {}, { timeout: 2000 })

    expect(screen.getByTestId('duplicate-live-region')).toBe(region)
    expect(region).toHaveTextContent('1 possible duplicate')
    expect(row.closest('[role="status"], [aria-live]')).toBeNull()
  })

  it('stays quiet when the check fails', async () => {
    vi.mocked(duplicatesApi.check).mockRejectedValue(new Error('offline'))
    renderNewForm()
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: 'Paywall Screen View' } })

    await waitFor(() => expect(duplicatesApi.check).toHaveBeenCalled(), { timeout: 2000 })
    expect(screen.queryByText(/Looks like/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Create event/i })).not.toBeDisabled()
  })
})
