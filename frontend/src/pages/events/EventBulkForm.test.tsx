import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { EventType } from '@/types'
import { eventsApi } from '@/api/events'
import { eventTypesApi } from '@/api/eventTypes'
import EventBulkForm from './EventBulkForm'
import { readCreatedEvents } from './createdEventsHandoff'

vi.mock('@/api/events', () => ({
  eventsApi: {
    list: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    byNames: vi.fn().mockResolvedValue({ items: [] }),
    bulkCreate: vi.fn().mockResolvedValue([]),
  },
}))
vi.mock('@/api/eventTypes', () => ({ eventTypesApi: { list: vi.fn() } }))

// Duplicate detection (F12, #265) asks the catalog as the name is typed. No
// matches by default, so every other test stays on the quiet path.
vi.mock('@/api/duplicates', () => ({
  MAX_DUPLICATE_CANDIDATES: 500,
  duplicatesApi: {
    check: vi.fn().mockResolvedValue({ items: [], threshold: 0.88 }),
    clusters: vi.fn(),
    dismiss: vi.fn(),
  },
}))
vi.mock('@/api/users', () => ({
  usersApi: {
    list: vi.fn().mockResolvedValue([{ id: 'u-1', name: 'Ann Analyst', email: 'ann@example.com' }]),
  },
}))

// The rule arrives ON the type, resolved by the server — not read off the scan
// list by event_type_id, which a branch copy of the type never matches.
const SE_TYPE = {
  id: 'et-se',
  name: 'se',
  display_name: 'Structured Event',
  event_name_format: '{category}:{action}:{label}',
  field_definitions: [
    { id: 'f-category', name: 'category', display_name: 'Category', field_type: 'string', is_required: false, order: 0 },
    { id: 'f-action', name: 'action', display_name: 'Action', field_type: 'string', is_required: false, order: 1 },
    { id: 'f-label', name: 'label', display_name: 'Label', field_type: 'string', is_required: false, order: 2 },
  ],
} as unknown as EventType

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
        // Where a successful save navigates.
        createElement(Route, { path: '/p/:slug/events', element: null }),
      ),
    ),
  )
}

/** The blocking-reason line of the sticky action bar. */
function saveBarStatus() {
  return document.querySelector('[data-slot="save-bar"] [role="status"]')
}

async function chooseType(id = 'et-se') {
  // Wait for the OPTION, not just the select: a controlled <select> ignores a
  // value it has no option for, so firing the change before the types resolve
  // silently leaves the form on "Select type…".
  await screen.findByRole('option', { name: 'Structured Event' })
  fireEvent.change(screen.getByLabelText(/Event type/), { target: { value: id } })
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.mocked(eventTypesApi.list).mockResolvedValue([SE_TYPE])
  vi.mocked(eventsApi.list).mockResolvedValue({ items: [], total: 0 } as never)
  vi.mocked(eventsApi.byNames).mockResolvedValue({ items: [] })
  vi.mocked(eventsApi.bulkCreate).mockResolvedValue([] as never)
})

describe('EventBulkForm', () => {
  it('previews the name the scan rule will give each line', async () => {
    render(createElement(EventBulkForm), { wrapper })
    await chooseType()

    fireEvent.change(await screen.findByLabelText('Events to create'), {
      target: { value: 'settings\tunit_change\twind_speed\nspot\topen\tmodels' },
    })

    expect(await screen.findByText('settings:unit_change:wind_speed')).toBeInTheDocument()
    expect(screen.getByText('spot:open:models')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Create 2 events' })).not.toBeDisabled())
  })

  it('sends the field values the name was built from, not just the name', async () => {
    render(createElement(EventBulkForm), { wrapper })
    await chooseType()
    fireEvent.change(await screen.findByLabelText('Events to create'), {
      target: { value: 'settings\tunit_change\twind_speed' },
    })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Create 1 event' })).not.toBeDisabled())
    fireEvent.click(screen.getByRole('button', { name: 'Create 1 event' }))

    // An event carrying the name and none of the values behind it would show an
    // empty Field values card and drift from its scanned counterpart.
    await waitFor(() =>
      expect(eventsApi.bulkCreate).toHaveBeenCalledWith(
        'demo',
        [
          {
            event_type_id: 'et-se',
            name: 'settings:unit_change:wind_speed',
            status: 'draft',
            field_values: [
              { field_definition_id: 'f-category', value: 'settings' },
              { field_definition_id: 'f-action', value: 'unit_change' },
              { field_definition_id: 'f-label', value: 'wind_speed' },
            ],
          },
        ],
        null,
      ),
    )
  })

  it('carries a title given after the identity columns, and previews it', async () => {
    render(createElement(EventBulkForm), { wrapper })
    await chooseType()
    fireEvent.change(await screen.findByLabelText('Events to create'), {
      target: { value: 'weather_alert,show,widget,Weather alert widget shown' },
    })

    // The label is visible before it is stored, next to the identity it never
    // becomes part of.
    expect(await screen.findByText('weather_alert:show:widget')).toBeInTheDocument()
    expect(screen.getByText('Weather alert widget shown')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Create 1 event' })).not.toBeDisabled())
    fireEvent.click(screen.getByRole('button', { name: 'Create 1 event' }))

    await waitFor(() =>
      expect(eventsApi.bulkCreate).toHaveBeenCalledWith(
        'demo',
        [
          expect.objectContaining({
            name: 'weather_alert:show:widget',
            title: 'Weather alert widget shown',
            field_values: [
              { field_definition_id: 'f-category', value: 'weather_alert' },
              { field_definition_id: 'f-action', value: 'show' },
              { field_definition_id: 'f-label', value: 'widget' },
            ],
          }),
        ],
        null,
      ),
    )
  })

  it('takes free names from a type no rule governs', async () => {
    vi.mocked(eventTypesApi.list).mockResolvedValue([
      { ...SE_TYPE, event_name_format: null, field_definitions: [] } as unknown as EventType,
    ])
    render(createElement(EventBulkForm), { wrapper })
    await chooseType()
    fireEvent.change(await screen.findByLabelText('Events to create'), {
      target: { value: 'checkout:started\tCheckout started\ncheckout:completed' },
    })

    expect(await screen.findByText('checkout:started')).toBeInTheDocument()
    expect(screen.getByText('Checkout started')).toBeInTheDocument()
    expect(screen.getByText('checkout:completed')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Create 2 events' })).not.toBeDisabled())
  })

  it('leaves out the lines it cannot create, and says why', async () => {
    vi.mocked(eventsApi.byNames).mockResolvedValue({
      items: [
        { identity: 'spot:open:models', event_id: 'ev-1', name: 'spot:open:models', source_name: 'spot:open:models' },
      ],
    })
    render(createElement(EventBulkForm), { wrapper })
    await chooseType()

    fireEvent.change(await screen.findByLabelText('Events to create'), {
      target: {
        value: [
          'settings\tunit_change\twind_speed',
          'settings\tunit_change\twind_speed',
          'spot\topen\tmodels',
          'settings\tunit_change',
        ].join('\n'),
      },
    })

    expect(await screen.findByText('repeated above')).toBeInTheDocument()
    // The catalog is asked once the paste settles (debounced), in one lookup.
    expect(await screen.findByText('already in the catalog')).toBeInTheDocument()
    expect(screen.getByText('missing label')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create 1 event' })).toBeInTheDocument()
  })

  it('carries a required field the name is not built from as a column of its own', async () => {
    vi.mocked(eventTypesApi.list).mockResolvedValue([
      {
        ...SE_TYPE,
        field_definitions: [
          ...SE_TYPE.field_definitions,
          { id: 'f-plat', name: 'platform', display_name: 'Platform', field_type: 'string', is_required: true, order: 3 },
        ],
      } as unknown as EventType,
    ])
    render(createElement(EventBulkForm), { wrapper })
    await chooseType()

    // The column order is stated, as the naming-rule path states its own.
    expect(
      await screen.findByText(/category, then action, then label, then platform, separated by a tab or a comma/),
    ).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
    fireEvent.change(screen.getByLabelText('Events to create'), {
      target: { value: 'settings,unit_change,wind_speed,web,Wind unit changed\nspot,open,models' },
    })

    expect(await screen.findByText('missing platform')).toBeInTheDocument()
    expect(screen.getByText('platform: web')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Create 1 event' })).not.toBeDisabled())
    fireEvent.click(screen.getByRole('button', { name: 'Create 1 event' }))

    await waitFor(() =>
      expect(eventsApi.bulkCreate).toHaveBeenCalledWith(
        'demo',
        [
          expect.objectContaining({
            name: 'settings:unit_change:wind_speed',
            title: 'Wind unit changed',
            field_values: [
              { field_definition_id: 'f-category', value: 'settings' },
              { field_definition_id: 'f-action', value: 'unit_change' },
              { field_definition_id: 'f-label', value: 'wind_speed' },
              { field_definition_id: 'f-plat', value: 'web' },
            ],
          }),
        ],
        null,
      ),
    )
  })

  it('refuses a type whose required JSON field a pasted list cannot fill', async () => {
    vi.mocked(eventTypesApi.list).mockResolvedValue([
      {
        ...SE_TYPE,
        field_definitions: [
          ...SE_TYPE.field_definitions,
          { id: 'f-ctx', name: 'context', display_name: 'Context', field_type: 'json', is_required: true, order: 3 },
        ],
      } as unknown as EventType,
    ])
    render(createElement(EventBulkForm), { wrapper })
    await chooseType()

    expect(await screen.findByRole('alert')).toHaveTextContent(/needs context, a JSON value/)
    expect(screen.queryByLabelText('Events to create')).not.toBeInTheDocument()
  })

  it('offers the two ways out of a type it cannot fill, and no dead Create', async () => {
    vi.mocked(eventTypesApi.list).mockResolvedValue([
      {
        ...SE_TYPE,
        field_definitions: [
          ...SE_TYPE.field_definitions,
          { id: 'f-ctx', name: 'context', display_name: 'Context', field_type: 'json', is_required: true, order: 3 },
        ],
      } as unknown as EventType,
    ])
    render(createElement(EventBulkForm), { wrapper })
    await chooseType()

    const callout = await screen.findByRole('alert')
    expect(within(callout).getByRole('link', { name: 'Add one at a time' })).toHaveAttribute(
      'href',
      '/p/demo/events/all/new',
    )
    expect(within(callout).getByRole('link', { name: 'Edit Structured Event fields' })).toHaveAttribute(
      'href',
      '/p/demo/event-types/et-se',
    )
    expect(screen.queryByRole('button', { name: /^Create/ })).toBeNull()
  })

  it('hands the created events to the list, which scrolls to and marks them', async () => {
    vi.mocked(eventsApi.bulkCreate).mockResolvedValue([{ id: 'ev-new-1' }, { id: 'ev-new-2' }] as never)
    render(createElement(EventBulkForm), { wrapper })
    await chooseType()
    fireEvent.change(await screen.findByLabelText('Events to create'), {
      target: { value: 'settings\tunit_change\twind_speed\nspot\topen\tmodels' },
    })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Create 2 events' })).not.toBeDisabled())
    fireEvent.click(screen.getByRole('button', { name: 'Create 2 events' }))

    await waitFor(() => expect(readCreatedEvents('demo')).toEqual(['ev-new-1', 'ev-new-2']))
  })

  it('sets the owner on every event of the batch when one is picked', async () => {
    render(createElement(EventBulkForm), { wrapper })
    await chooseType()
    await screen.findByRole('option', { name: 'Ann Analyst' })
    fireEvent.change(screen.getByLabelText('Owner'), { target: { value: 'u-1' } })
    fireEvent.change(await screen.findByLabelText('Events to create'), {
      target: { value: 'settings\tunit_change\twind_speed' },
    })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Create 1 event' })).not.toBeDisabled())
    fireEvent.click(screen.getByRole('button', { name: 'Create 1 event' }))

    await waitFor(() =>
      expect(eventsApi.bulkCreate).toHaveBeenCalledWith(
        'demo',
        [expect.objectContaining({ name: 'settings:unit_change:wind_speed', owner_id: 'u-1' })],
        null,
      ),
    )
  })

  it('preselects the type the route names, once', async () => {
    render(createElement(EventBulkForm), {
      wrapper: ({ children }: { children: ReactNode }) =>
        createElement(
          QueryClientProvider,
          { client: queryClient },
          createElement(
            MemoryRouter,
            { initialEntries: ['/p/demo/events/se/bulk'] },
            createElement(
              Routes,
              null,
              createElement(Route, { path: '/p/:slug/events/:tab/bulk', element: children }),
            ),
          ),
        ),
    })

    await screen.findByRole('option', { name: 'Structured Event' })
    await waitFor(() => expect(screen.getByLabelText(/Event type/)).toHaveValue('et-se'))
    // The rule's columns appear without a click, as on the single-event form.
    expect(await screen.findByLabelText('Events to create')).toBeInTheDocument()

    // A reader who clears the choice is not overruled.
    fireEvent.change(screen.getByLabelText(/Event type/), { target: { value: '' } })
    await waitFor(() => expect(screen.getByLabelText(/Event type/)).toHaveValue(''))
  })
})

/** Whether a reload/tab-close right now would get the browser's prompt. */
function reloadIsGuarded(): boolean {
  const event = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(event)
  return event.defaultPrevented
}

describe('EventBulkForm unsaved-changes guard', () => {
  it('arms the reload prompt while a pasted list is on the page', async () => {
    render(createElement(EventBulkForm), { wrapper })
    await chooseType()
    expect(reloadIsGuarded()).toBe(false)

    fireEvent.change(await screen.findByLabelText('Events to create'), {
      target: { value: 'settings\tunit_change\twind_speed' },
    })
    expect(reloadIsGuarded()).toBe(true)
  })
})

describe('EventBulkForm duplicate check', () => {
  it('asks the catalog about the pasted names only, in one exact-name lookup', async () => {
    vi.mocked(eventsApi.list).mockClear()
    vi.mocked(eventsApi.byNames).mockClear()
    render(createElement(EventBulkForm), { wrapper })
    await chooseType()
    fireEvent.change(await screen.findByLabelText('Events to create'), {
      target: { value: 'settings\tunit_change\twind_speed\nspot\topen\tmodels' },
    })

    await waitFor(() =>
      expect(eventsApi.byNames).toHaveBeenCalledWith(
        'demo',
        'et-se',
        ['settings:unit_change:wind_speed', 'spot:open:models'],
        null,
        expect.anything(),
      ),
    )
    expect(eventsApi.byNames).toHaveBeenCalledTimes(1)
    // Neither the old whole-catalog read nor a substring search per name.
    expect(eventsApi.list).not.toHaveBeenCalled()
  })

  it('holds Create while the names are still being checked', async () => {
    let answer: (value: { items: [] }) => void = () => {}
    vi.mocked(eventsApi.byNames).mockImplementation(
      () => new Promise(resolve => { answer = resolve }),
    )
    render(createElement(EventBulkForm), { wrapper })
    await chooseType()
    fireEvent.change(await screen.findByLabelText('Events to create'), {
      target: { value: 'spot\topen\tmodels' },
    })

    // Before the debounce and while the lookup is out, nothing says the line
    // is free — the name may be taken, and the server would refuse the batch.
    expect(await screen.findByText('checking…')).toBeInTheDocument()
    expect(screen.queryByText('will be created')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create 1 event' })).toBeDisabled()
    expect(saveBarStatus()).toHaveTextContent('Checking the names…')
    await waitFor(() => expect(eventsApi.byNames).toHaveBeenCalled())
    expect(screen.getByRole('button', { name: 'Create 1 event' })).toBeDisabled()

    answer({ items: [] })
    expect(await screen.findByText('will be created')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create 1 event' })).not.toBeDisabled()
  })

  it('says on the action bar why Create is held', async () => {
    render(createElement(EventBulkForm), { wrapper })
    await waitFor(() => expect(saveBarStatus()).toHaveTextContent('Pick an event type'))
    await chooseType()
    await waitFor(() => expect(saveBarStatus()).toHaveTextContent('Paste at least one event name'))
    expect(screen.getByRole('button', { name: 'Create 0 events' })).toBeDisabled()
  })

  it('reads a failed lookup as unchecked, not as a free name', async () => {
    vi.mocked(eventsApi.byNames).mockRejectedValue(new Error('network down'))
    render(createElement(EventBulkForm), { wrapper })
    await chooseType()
    fireEvent.change(await screen.findByLabelText('Events to create'), {
      target: { value: 'spot\topen\tmodels' },
    })

    expect(await screen.findByText('will be created, not checked')).toBeInTheDocument()
    expect(screen.getByText(/1 name could not be checked against the catalog/)).toBeInTheDocument()
  })

  it('does not ask anything before a line is pasted', async () => {
    vi.mocked(eventsApi.byNames).mockClear()
    render(createElement(EventBulkForm), { wrapper })
    await chooseType()
    await screen.findByLabelText('Events to create')
    expect(eventsApi.byNames).not.toHaveBeenCalled()
  })
})

function ListLocation() {
  const location = useLocation()
  return createElement('div', { 'data-testid': 'list-location' }, `${location.pathname}${location.search}`)
}

describe('EventBulkForm exits', () => {
  it("returns to the list with the list's filters and branch", async () => {
    render(createElement(EventBulkForm), {
      wrapper: ({ children }: { children: ReactNode }) =>
        createElement(
          QueryClientProvider,
          { client: queryClient },
          createElement(
            MemoryRouter,
            { initialEntries: ['/p/demo/events/all/bulk?branch=b-1&status=draft'] },
            createElement(
              Routes,
              null,
              createElement(Route, { path: '/p/:slug/events/:tab/bulk', element: children }),
              createElement(Route, { path: '/p/:slug/events', element: createElement(ListLocation) }),
            ),
          ),
        ),
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(await screen.findByTestId('list-location')).toHaveTextContent(
      '/p/demo/events?branch=b-1&status=draft',
    )
  })
})
