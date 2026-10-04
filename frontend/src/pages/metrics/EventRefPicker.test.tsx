import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { expectNoAxeViolations } from '@/test/axe'
import type { EventListItem, EventType } from '@/types'
import { EventRefPicker, type EventRef } from './EventRefPicker'
import { freshSearch, moreText, refText, typeOptions } from './eventRefOptions'

vi.mock('@/api/events', () => ({
  eventsApi: { list: vi.fn(), get: vi.fn() },
}))

import { eventsApi } from '@/api/events'

const EVENT_TYPES = [
  { id: 'et-web', name: 'web', display_name: 'Web', color: '#ff0000' },
  { id: 'et-app', name: 'app', display_name: 'App', color: '#00ff00' },
] as unknown as EventType[]

function events(count: number): EventListItem[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `ev-${i}`,
    name: `event_${String(i).padStart(3, '0')}`,
    event_type_id: i % 2 === 0 ? 'et-web' : 'et-app',
  })) as unknown as EventListItem[]
}

/** Answers the way the endpoint does: ILIKE on the name, `limit` rows, the true `total`. */
function serve(catalog: EventListItem[]) {
  vi.mocked(eventsApi.list).mockImplementation(async (_slug, params) => {
    const search = params?.search?.toLowerCase()
    const matching = search ? catalog.filter(e => e.name.toLowerCase().includes(search)) : catalog
    return { items: matching.slice(0, params?.limit ?? 200), total: matching.length }
  })
}

function renderPicker(initial: EventRef = { eventId: '', eventTypeId: '' }, onChange = vi.fn()) {
  function Harness() {
    const [value, setValue] = useState(initial)
    return (
      <>
        <label htmlFor="metric-numerator">Event</label>
        <EventRefPicker
          slug="demo"
          id="metric-numerator"
          label="events"
          value={value}
          onChange={next => {
            setValue(next)
            onChange(next)
          }}
          eventTypes={EVENT_TYPES}
        />
      </>
    )
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Harness />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { input: screen.getByRole('combobox', { name: 'Event' }), onChange }
}

afterEach(() => vi.clearAllMocks())

describe('EventRefPicker combobox', () => {
  it('is one labelled combobox, not a search box over a select', async () => {
    serve(events(3))
    const { input } = renderPicker()
    expect(input.id).toBe('metric-numerator')
    expect(screen.queryByRole('searchbox')).toBeNull()
    expect(document.querySelector('select')).toBeNull()
    expect(input).toHaveAttribute('placeholder', 'Select event…')
    await waitFor(() => expect(eventsApi.list).toHaveBeenCalled())
  })

  it('groups events and event types, each row with its type chip and dot', async () => {
    serve(events(3))
    const { input } = renderPicker()
    act(() => input.focus())

    const listbox = await screen.findByRole('listbox', { name: 'Events' })
    // The groups render once the roster has loaded.
    const eventsGroup = await within(listbox).findByRole('group', { name: 'Events' })
    const typesGroup = within(listbox).getByRole('group', { name: 'All events of a type' })

    const first = await within(eventsGroup).findByRole('option', { name: /event_000/ })
    expect(first).toHaveTextContent('Web')
    expect(within(first).getByTestId('event-type-dot')).toHaveStyle({ backgroundColor: '#ff0000' })
    expect(within(eventsGroup).getByRole('option', { name: /event_001/ })).toHaveTextContent('App')
    expect(within(typesGroup).getByRole('option', { name: 'Every Web event' })).toBeInTheDocument()
    expect(within(typesGroup).getByRole('option', { name: 'Every App event' })).toBeInTheDocument()
    expect(input).toHaveAttribute('aria-controls', listbox.id)
  })

  it('filters as you type: events on the server, types locally', async () => {
    serve(events(3))
    const { input } = renderPicker()
    act(() => input.focus())
    fireEvent.change(input, { target: { value: 'app' } })

    // Types filter on the keystroke, without waiting for the debounce.
    const listbox = screen.getByRole('listbox')
    expect(within(listbox).getByRole('option', { name: 'Every App event' })).toBeInTheDocument()
    expect(within(listbox).queryByRole('option', { name: 'Every Web event' })).toBeNull()
    await waitFor(() =>
      expect(eventsApi.list).toHaveBeenCalledWith('demo', expect.objectContaining({ search: 'app' })),
    )
  })

  it('picks by click and by keyboard, and Enter never submits the form', async () => {
    serve(events(3))
    const onSubmit = vi.fn((e: { preventDefault: () => void }) => e.preventDefault())
    const onChange = vi.fn()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    function Harness() {
      const [value, setValue] = useState<EventRef>({ eventId: '', eventTypeId: '' })
      return (
        <form onSubmit={onSubmit}>
          <EventRefPicker
            slug="demo"
            id="pick"
            label="events"
            value={value}
            onChange={next => {
              setValue(next)
              onChange(next)
            }}
            eventTypes={EVENT_TYPES}
          />
        </form>
      )
    }
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Harness />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    const input = screen.getByRole('combobox')
    act(() => input.focus())
    fireEvent.click(await screen.findByRole('option', { name: /event_001/ }))
    expect(onChange).toHaveBeenLastCalledWith({ eventId: 'ev-1', eventTypeId: '' })
    expect(input).toHaveValue('event_001 · App')
    expect(input).toHaveAttribute('aria-expanded', 'false')
    expect(input).toHaveFocus()

    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(input).toHaveAttribute('aria-expanded', 'true')
    // Past the three events to the first type.
    for (let i = 0; i < 3; i++) fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(input).toHaveAttribute(
      'aria-activedescendant',
      screen.getByRole('option', { name: 'Every Web event' }).id,
    )
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).toHaveBeenLastCalledWith({ eventId: '', eventTypeId: 'et-web' })
    expect(input).toHaveValue('Every Web event')
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('starts a fresh search when typing after a pick, not "name · Type" plus the key', async () => {
    serve(events(3))
    const { input } = renderPicker()
    act(() => input.focus())
    fireEvent.click(await screen.findByRole('option', { name: /event_001/ }))
    expect(input).toHaveValue('event_001 · App')
    expect(input).toHaveAttribute('aria-expanded', 'false')

    // The browser appends the first key to the shown pick; the search is that key alone.
    fireEvent.change(input, { target: { value: 'event_001 · Appe' } })
    expect(input).toHaveAttribute('aria-expanded', 'true')
    expect(input).toHaveValue('e')
    fireEvent.change(input, { target: { value: 'event_00' } })

    const listbox = screen.getByRole('listbox')
    expect(await within(listbox).findByRole('option', { name: /event_000/ })).toBeInTheDocument()
    expect(within(listbox).getByRole('option', { name: /event_002/ })).toBeInTheDocument()
    await waitFor(() =>
      expect(eventsApi.list).toHaveBeenCalledWith('demo', expect.objectContaining({ search: 'event_00' })),
    )
  })

  it('starts empty on Backspace after Escape closes the list over a pick', async () => {
    serve(events(3))
    const { input } = renderPicker({ eventId: '', eventTypeId: 'et-web' })
    act(() => input.focus())
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(input).toHaveAttribute('aria-expanded', 'false')
    expect(input).toHaveValue('Every Web event')

    fireEvent.change(input, { target: { value: 'Every Web even' } })
    expect(input).toHaveAttribute('aria-expanded', 'true')
    expect(input).toHaveValue('')
    expect(await screen.findByRole('option', { name: /event_000/ })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Every App event' })).toBeInTheDocument()
  })

  it('ends a capped list with "N more — keep typing"', async () => {
    serve(events(150))
    const { input } = renderPicker()
    act(() => input.focus())
    expect(await screen.findByRole('option', { name: '50 more — keep typing' })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
  })

  it('shows a stored event beyond the first page, resolved by id', async () => {
    const catalog = events(250)
    serve(catalog)
    vi.mocked(eventsApi.get).mockResolvedValue({
      ...catalog[240],
      event_type: { id: 'et-web', name: 'web', display_name: 'Web', color: '#ff0000' },
    } as never)
    const { input } = renderPicker({ eventId: 'ev-240', eventTypeId: '' })

    await waitFor(() => expect(input).toHaveValue('event_240 · Web'))
    expect(eventsApi.get).toHaveBeenCalledWith('demo', 'ev-240')
    act(() => input.focus())
    const current = await screen.findByRole('option', { name: /event_240/ })
    expect(current).toHaveAttribute('aria-current', 'true')
  })

  it('shows a stored event-type reference and clears it', async () => {
    serve(events(3))
    const { input, onChange } = renderPicker({ eventId: '', eventTypeId: 'et-app' })
    expect(input).toHaveValue('Every App event')

    fireEvent.click(screen.getByRole('button', { name: 'Clear events' }))
    expect(onChange).toHaveBeenLastCalledWith({ eventId: '', eventTypeId: '' })
    expect(input).toHaveValue('')
  })

  it('keeps an unknown stored type visible instead of painting it unset', () => {
    serve([])
    const { input } = renderPicker({ eventId: '', eventTypeId: 'gone-type-id' })
    expect(input).toHaveValue('Every event of type gone-typ')
  })

  it('says so when a project has no events', async () => {
    serve([])
    renderPicker()
    expect(await screen.findByText(/No events in this project yet/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Add events' })).toHaveAttribute('href', '/p/demo/events')
  })

  it('has no axe violations with the list open', async () => {
    serve(events(150))
    const { input } = renderPicker({ eventId: 'ev-1', eventTypeId: '' })
    act(() => input.focus())
    await screen.findByRole('option', { name: /more — keep typing/ })
    await expectNoAxeViolations(document.body)
  })
})

describe('eventRefOptions helpers', () => {
  it('counts what a capped roster hides', () => {
    expect(moreText(150, 100)).toBe('50 more — keep typing')
    expect(moreText(100, 100)).toBeNull()
  })

  it('names an event with its type, and a type row on its own', () => {
    const [web] = typeOptions(EVENT_TYPES, 'web', '')
    expect(web && refText(web)).toBe('Every Web event')
    expect(refText(null)).toBe('')
  })

  it('seeds a search from an edit to the shown pick', () => {
    const shown = 'event_001 · App'
    expect(freshSearch(shown, `${shown}x`)).toBe('x')
    expect(freshSearch(shown, 'event_001 · Ap')).toBe('')
    expect(freshSearch(shown, 'event_01 · App')).toBe('')
    expect(freshSearch(shown, 'x')).toBe('x')
    expect(freshSearch(shown, '')).toBe('')
    expect(freshSearch('', 'ab')).toBe('ab')
  })
})
