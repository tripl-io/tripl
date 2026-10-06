import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { eventsApi } from '@/api/events'
import { propertyEntriesApi, type EventPropertyEntry } from '@/api/propertyEntries'
import type { EventFieldValue, Variable } from '@/types'
import { EventPropertiesGrid } from './EventPropertiesGrid'

vi.mock('@/api/propertyEntries', () => ({
  propertyEntriesApi: { forEvent: vi.fn(), set: vi.fn(), remove: vi.fn() },
}))
vi.mock('@/api/events', () => ({ eventsApi: { update: vi.fn() } }))

function entry(overrides: Partial<EventPropertyEntry> & { variable_id: string; name: string }): EventPropertyEntry {
  return {
    id: `entry-${overrides.variable_id}`,
    variable_type: 'string',
    json_schema: null,
    description: '',
    required: false,
    values: null,
    effective_values: [],
    presence_rate: null,
    suggested_required: null,
    ...overrides,
  }
}

const ENTRIES: EventPropertyEntry[] = [
  entry({
    variable_id: 'var-cart',
    name: 'cart',
    variable_type: 'json',
    json_schema: { type: 'object', properties: { id: { type: 'string' }, total: { type: 'number' } } },
    presence_rate: 1,
    suggested_required: true,
  }),
  entry({
    variable_id: 'var-currency',
    name: 'currency',
    required: true,
    values: ['USD'],
    effective_values: ['USD'],
    presence_rate: 0.5,
    suggested_required: false,
  }),
  entry({
    variable_id: 'var-price',
    name: 'price',
    variable_type: 'number',
    json_schema: { type: 'integer', minimum: 0 },
  }),
]

const PROJECT_VARIABLES = [
  { id: 'var-cart', name: 'cart' },
  { id: 'var-currency', name: 'currency' },
  { id: 'var-coupon', name: 'coupon' },
] as Variable[]

function renderGrid(
  canWrite = true,
  threshold: number | null = null,
  extra: { fieldValues?: EventFieldValue[]; projectVariables?: Variable[] } = {},
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <EventPropertiesGrid
          slug="demo"
          branchId="branch-1"
          eventId="ev-1"
          threshold={threshold}
          canWrite={canWrite}
          projectVariables={extra.projectVariables ?? PROJECT_VARIABLES}
          fieldValues={extra.fieldValues}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(propertyEntriesApi.forEvent).mockResolvedValue(ENTRIES)
  vi.mocked(propertyEntriesApi.set).mockResolvedValue({})
  vi.mocked(propertyEntriesApi.remove).mockResolvedValue(undefined as never)
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('EventPropertiesGrid (F23)', () => {
  it('lists each property with its type from the schema, required, values and presence', async () => {
    renderGrid()

    const cart = await screen.findByRole('link', { name: 'cart' })
    expect(cart).toHaveAttribute('href', '/p/demo/variables/var-cart')
    expect(screen.getByText('Object {id, total}')).toBeInTheDocument()
    expect(screen.getByText('Integer')).toHaveAttribute('title', '≥ 0')
    expect(screen.getByRole('switch', { name: 'currency is required' })).toBeChecked()
    expect(screen.getByText('This event')).toBeInTheDocument()
    expect(screen.getByText('50%')).toBeInTheDocument()
    expect(screen.getByText('below threshold')).toBeInTheDocument()
    expect(screen.getByText('looks required')).toBeInTheDocument()
    expect(screen.getByText('3 on this event · 1 required')).toBeInTheDocument()
  })

  it('toggles required in place, on the branch being edited', async () => {
    renderGrid()
    fireEvent.click(await screen.findByRole('switch', { name: 'cart is required' }))
    await waitFor(() =>
      expect(propertyEntriesApi.set).toHaveBeenCalledWith('demo', 'var-cart', 'ev-1', { required: true }, 'branch-1'),
    )
  })

  it('edits the allowed values for this event, and can hand back to the documented list', async () => {
    renderGrid()
    fireEvent.click(await screen.findByRole('button', { name: 'Edit allowed values of currency for this event' }))
    const input = screen.getByLabelText('Add allowed value of currency for this event')
    fireEvent.change(input, { target: { value: 'EUR' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.click(screen.getByRole('button', { name: 'Save values' }))
    await waitFor(() =>
      expect(propertyEntriesApi.set).toHaveBeenCalledWith('demo', 'var-currency', 'ev-1', { values: ['USD', 'EUR'] }, 'branch-1'),
    )

    fireEvent.click(screen.getByRole('button', { name: 'Edit allowed values of currency for this event' }))
    fireEvent.click(screen.getByRole('button', { name: 'Use documented list' }))
    await waitFor(() =>
      expect(propertyEntriesApi.set).toHaveBeenLastCalledWith('demo', 'var-currency', 'ev-1', { values: null }, 'branch-1'),
    )
  })

  it('adds a property from the project, offering only those not on the list', async () => {
    renderGrid()
    await screen.findByRole('link', { name: 'cart' })
    fireEvent.change(screen.getByLabelText('Add property'), { target: { value: 'c' } })
    const matches = screen.getByRole('list', { name: 'Matching properties' })
    expect(within(matches).getAllByRole('button').map(b => b.textContent)).toEqual(['coupon'])
    fireEvent.click(screen.getByRole('switch', { name: 'Add as required' }))
    fireEvent.click(within(matches).getByRole('button', { name: 'coupon' }))
    await waitFor(() =>
      expect(propertyEntriesApi.set).toHaveBeenCalledWith('demo', 'var-coupon', 'ev-1', { required: true }, 'branch-1'),
    )
  })

  it('removes a property after asking', async () => {
    renderGrid()
    fireEvent.click(await screen.findByRole('button', { name: 'Remove currency from this event' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/per-event values go with it/)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }))
    await waitFor(() =>
      expect(propertyEntriesApi.remove).toHaveBeenCalledWith('demo', 'var-currency', 'ev-1', 'branch-1'),
    )
  })

  it("sets the event's own required threshold, and resets it to the default", async () => {
    vi.mocked(eventsApi.update).mockResolvedValue({} as never)
    renderGrid(true, 0.8)
    const input = await screen.findByLabelText('Required at')
    expect(input).toHaveValue(80)
    fireEvent.change(input, { target: { value: '90' } })
    fireEvent.blur(input)
    await waitFor(() =>
      expect(eventsApi.update).toHaveBeenCalledWith('demo', 'ev-1', { required_presence_threshold: 0.9 }, 'branch-1'),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Reset to 95%' }))
    await waitFor(() =>
      expect(eventsApi.update).toHaveBeenLastCalledWith('demo', 'ev-1', { required_presence_threshold: null }, 'branch-1'),
    )
  })

  it('is read-only for a viewer', async () => {
    renderGrid(false)
    expect(await screen.findByRole('link', { name: 'cart' })).toBeInTheDocument()
    expect(screen.queryByRole('switch')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.queryByLabelText('Add property')).toBeNull()
    expect(screen.getByText('Required at 95% presence (default)')).toBeInTheDocument()
  })
})

// tripl-4zzc.6: field values name properties the list does not carry yet.
const TOKEN_VARIABLES = [
  { id: 'var-spot', name: 'spot_id', source_name: 'property.spot_id', bindings: [] },
  { id: 'var-type', name: 'type', source_name: null, bindings: [] },
  { id: 'var-rank', name: 'details_rank', source_name: null, bindings: [] },
  { id: 'var-cart', name: 'cart', source_name: null, bindings: [] },
] as unknown as Variable[]

const TOKEN_FIELD_VALUES: EventFieldValue[] = [
  {
    id: 'fv-1',
    field_definition_id: 'fd-1',
    value: '{"spot_id":"${property.spot_id}","type":"${type}","details_rank":"${details_rank}","x":"${nope}"}',
  },
]

function renderWithTokens(canWrite = true) {
  return renderGrid(canWrite, null, { fieldValues: TOKEN_FIELD_VALUES, projectVariables: TOKEN_VARIABLES })
}

describe('EventPropertiesGrid: properties the field values use', () => {
  it('offers the referenced properties of an empty list, not the unknown token', async () => {
    vi.mocked(propertyEntriesApi.forEvent).mockResolvedValue([])
    renderWithTokens()

    expect(await screen.findByText('None listed yet · field values use 3')).toBeInTheDocument()
    expect(screen.queryByText('No properties on this event yet.')).toBeNull()
    const list = screen.getByRole('list', { name: 'Properties the field values use' })
    expect(within(list).getAllByRole('link').map(l => l.textContent)).toEqual(['spot_id', 'type', 'details_rank'])
    expect(within(list).getByText('${property.spot_id}')).toHaveAttribute('title', '${property.spot_id}')
    expect(list).not.toHaveTextContent('nope')
  })

  it('adds one, optional by default, on the branch being edited', async () => {
    vi.mocked(propertyEntriesApi.forEvent).mockResolvedValue([])
    renderWithTokens()
    fireEvent.click(await screen.findByRole('button', { name: 'Add type to this event' }))
    await waitFor(() =>
      expect(propertyEntriesApi.set).toHaveBeenCalledWith('demo', 'var-type', 'ev-1', { required: false }, 'branch-1'),
    )
  })

  it('adds all of them one after another, required when its own switch is on', async () => {
    vi.mocked(propertyEntriesApi.forEvent).mockResolvedValue([])
    renderWithTokens()
    await screen.findByRole('button', { name: 'Add all (3)' })
    fireEvent.click(screen.getByRole('switch', { name: 'Add required' }))
    // The search's own switch is left alone.
    expect(screen.getByRole('switch', { name: 'Add as required' })).not.toBeChecked()
    fireEvent.click(screen.getByRole('button', { name: 'Add all (3)' }))
    await waitFor(() => expect(propertyEntriesApi.set).toHaveBeenCalledTimes(3))
    expect(vi.mocked(propertyEntriesApi.set).mock.calls).toEqual([
      ['demo', 'var-spot', 'ev-1', { required: true }, 'branch-1'],
      ['demo', 'var-type', 'ev-1', { required: true }, 'branch-1'],
      ['demo', 'var-rank', 'ev-1', { required: true }, 'branch-1'],
    ])
  })

  it('says how far Add all got when a PUT fails, and stops there', async () => {
    vi.mocked(propertyEntriesApi.forEvent).mockResolvedValue([])
    vi.mocked(propertyEntriesApi.set)
      .mockResolvedValueOnce({})
      .mockRejectedValueOnce(new Error('Server unavailable'))
    renderWithTokens()
    fireEvent.click(await screen.findByRole('button', { name: 'Add all (3)' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Added 1 of 3: Server unavailable')
    expect(propertyEntriesApi.set).toHaveBeenCalledTimes(2)

    // A later single Add clears it.
    fireEvent.click(screen.getByRole('button', { name: 'Add details_rank to this event' }))
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
  })

  it('counts only those off the list when some are on it', async () => {
    vi.mocked(propertyEntriesApi.forEvent).mockResolvedValue([
      entry({ variable_id: 'var-spot', name: 'spot_id', required: true }),
    ])
    renderWithTokens()
    expect(await screen.findByText('1 on this event · 1 required · 2 more used in field values')).toBeInTheDocument()
    const list = screen.getByRole('list', { name: 'Properties the field values use' })
    expect(within(list).getAllByRole('link').map(l => l.textContent)).toEqual(['type', 'details_rank'])
    expect(screen.getByRole('button', { name: 'Add all (2)' })).toBeInTheDocument()
  })

  it('offers nothing while the list is loading, or when it failed to load', async () => {
    vi.mocked(propertyEntriesApi.forEvent).mockReturnValue(new Promise(() => {}))
    const { unmount } = renderWithTokens()
    expect(screen.getByText('Loading…')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Properties the field values use' })).toBeNull()
    unmount()

    vi.mocked(propertyEntriesApi.forEvent).mockRejectedValue(new Error('boom'))
    renderWithTokens()
    expect(await screen.findByText("Couldn't load this event's properties")).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Properties the field values use' })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Add all/ })).toBeNull()
  })

  it('read-only: no list or buttons, only an empty state that names the count', async () => {
    vi.mocked(propertyEntriesApi.forEvent).mockResolvedValue([])
    renderWithTokens(false)
    expect(await screen.findByText('None listed yet · field values use 3')).toBeInTheDocument()
    expect(
      screen.getByText(/The field values use 3 properties not on this event's property list yet/),
    ).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Properties the field values use' })).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('keeps the old copy when the field values reference nothing', async () => {
    vi.mocked(propertyEntriesApi.forEvent).mockResolvedValue([])
    renderGrid(true, null, {
      fieldValues: [{ id: 'fv-1', field_definition_id: 'fd-1', value: 'static ${nope}' }],
      projectVariables: TOKEN_VARIABLES,
    })
    expect(await screen.findByText('No properties on this event yet.')).toBeInTheDocument()
    expect(screen.queryByText('Used in field values')).toBeNull()
  })
})
