import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useParams } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext } from '@/components/auth-context'
import { ApiError } from '@/api/client'
import { eventsApi } from '@/api/events'
import { propertyEntriesApi, type PropertyEventEntry } from '@/api/propertyEntries'
import { variablesApi } from '@/api/variables'
import { variableDriftsApi } from '@/api/variableDrifts'
import { variableOverridesApi } from '@/api/variableOverrides'
import type { Variable } from '@/types'
import { personaAuth, type Persona } from '@/test/persona'
import { PersonaProject } from '@/test/PersonaProject'
import { VariableDetailPage } from './VariableDetailPage'

vi.mock('@/api/variables', () => ({
  variablesApi: { list: vi.fn(), update: vi.fn(), values: vi.fn(), clearValues: vi.fn() },
}))
vi.mock('@/api/variableDrifts', () => ({
  variableDriftsApi: { list: vi.fn(), action: vi.fn() },
}))
vi.mock('@/api/variableOverrides', () => ({
  variableOverridesApi: { list: vi.fn(), upsert: vi.fn(), del: vi.fn() },
}))
vi.mock('@/api/events', () => ({ eventsApi: { list: vi.fn() } }))
vi.mock('@/api/propertyEntries', () => ({
  PROPERTY_BULK_EVENT_LIMIT: 5000,
  propertyEntriesApi: { forProperty: vi.fn(), bulkSet: vi.fn(), bulkRemove: vi.fn() },
}))
vi.mock('@/api/docs', () => ({
  docsApi: {
    backlinks: vi.fn(() => Promise.resolve({ kind: 'variable', name: '', qualifier: null, items: [] })),
  },
}))

function makeVariable(overrides: Partial<Variable> & { id: string; name: string }): Variable {
  return {
    project_id: 'project-1',
    source_name: null,
    variable_type: 'string',
    allowed_values: [],
    bindings: [],
    description: '',
    ...overrides,
  }
}

function entry(overrides: Partial<PropertyEventEntry> & { event_id: string; event_name: string }): PropertyEventEntry {
  return {
    event_type_id: 'et-1',
    status: 'active',
    required: false,
    values: null,
    effective_values: ['USD', 'EUR'],
    presence_rate: null,
    required_presence_threshold: null,
    suggested_required: null,
    ...overrides,
  }
}

function PageRoute() {
  const { slug, id } = useParams<{ slug: string; id: string }>()
  return <VariableDetailPage slug={slug!} variableId={id!} />
}

function renderPage(path: string, role: Persona = 'owner') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={personaAuth(role)}>
        <PersonaProject persona={role}>
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route path="/p/:slug/variables/:id" element={<PageRoute />} />
            </Routes>
          </MemoryRouter>
        </PersonaProject>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

const CURRENCY = makeVariable({ id: 'var-1', name: 'currency', allowed_values: ['USD', 'EUR'] })
const PRICE = makeVariable({ id: 'var-2', name: 'price', variable_type: 'number' })
const SEEN_ON = makeVariable({ id: 'var-4', name: 'seen_on' })
const CART = makeVariable({
  id: 'var-3',
  name: 'cart',
  variable_type: 'json',
  json_schema: {
    type: 'object',
    properties: { id: { type: 'string' }, total: { type: 'number', minimum: 0 } },
    required: ['id'],
  },
})

beforeEach(() => {
  vi.mocked(variablesApi.list).mockResolvedValue([CURRENCY, PRICE, CART, SEEN_ON])
  vi.mocked(variablesApi.values).mockResolvedValue([])
  vi.mocked(variableOverridesApi.list).mockResolvedValue([])
  vi.mocked(variableDriftsApi.list).mockResolvedValue({ items: [], total: 0 })
  vi.mocked(eventsApi.list).mockResolvedValue({ items: [] as never, total: 0 })
  vi.mocked(propertyEntriesApi.forProperty).mockResolvedValue([])
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('Property schema editor (F23)', () => {
  it('narrows a number to integer with bounds, and saves the fragment with the type', async () => {
    vi.mocked(variablesApi.update).mockResolvedValue(PRICE)
    renderPage('/p/demo/variables/var-2')

    const schemaType = await screen.findByLabelText('Schema type of the property')
    fireEvent.change(schemaType, { target: { value: 'integer' } })
    fireEvent.change(screen.getByLabelText('Minimum'), { target: { value: '0' } })
    fireEvent.change(screen.getByLabelText('Maximum'), { target: { value: '100' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() =>
      expect(variablesApi.update).toHaveBeenCalledWith(
        'demo',
        'var-2',
        expect.objectContaining({
          variable_type: 'number',
          json_schema: { type: 'integer', minimum: 0, maximum: 100 },
        }),
        null,
      ),
    )
  })

  it('holds Save while a bound contradicts another, and names it', async () => {
    renderPage('/p/demo/variables/var-2')

    fireEvent.change(await screen.findByLabelText('Minimum'), { target: { value: '10' } })
    fireEvent.change(screen.getByLabelText('Maximum'), { target: { value: '1' } })

    expect(screen.getByText('The property: minimum is greater than maximum.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled()
    expect(screen.getByText('Fix the schema')).toBeInTheDocument()
  })

  it('edits nested properties of an object, and carries required through a rename', async () => {
    vi.mocked(variablesApi.update).mockResolvedValue(CART)
    renderPage('/p/demo/variables/var-3')

    const name = await screen.findByLabelText('Name of nested property id')
    fireEvent.change(name, { target: { value: 'cart_id' } })
    fireEvent.blur(name)
    fireEvent.click(screen.getByRole('button', { name: 'Add nested property' }))
    fireEvent.change(screen.getByLabelText('Schema type of property_3'), { target: { value: 'array' } })
    fireEvent.change(screen.getByLabelText('Schema type of items of property_3'), { target: { value: 'integer' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(variablesApi.update).toHaveBeenCalled())
    const [, , body] = vi.mocked(variablesApi.update).mock.calls[0]!
    expect(body.json_schema).toEqual({
      type: 'object',
      properties: {
        cart_id: { type: 'string' },
        total: { type: 'number', minimum: 0 },
        property_3: { type: 'array', items: { type: 'integer' } },
      },
      required: ['cart_id'],
    })
  })

  it('refuses a nested name that is already taken', async () => {
    renderPage('/p/demo/variables/var-3')
    const name = await screen.findByLabelText('Name of nested property id')
    fireEvent.change(name, { target: { value: 'total' } })
    fireEvent.blur(name)
    expect(screen.getByText('total is already a property here.')).toBeInTheDocument()
    expect(name).toHaveValue('id')
  })

  it("shows the server's refusal of the schema under the schema editor", async () => {
    vi.mocked(variablesApi.update).mockRejectedValue(
      new ApiError("json_schema.properties.total: 'minimum' must be a number", 422),
    )
    renderPage('/p/demo/variables/var-2')

    fireEvent.change(await screen.findByLabelText('Schema type of the property'), { target: { value: 'integer' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    const schemaGroup = screen.getByRole('group', { name: 'Schema' })
    expect(await within(schemaGroup).findByText(/minimum' must be a number/)).toBeInTheDocument()
  })

  it('pins the format of a date property, and resets the schema when the type changes', async () => {
    vi.mocked(variablesApi.update).mockResolvedValue(SEEN_ON)
    renderPage('/p/demo/variables/var-4')

    fireEvent.change(await screen.findByLabelText('Pattern (regex)'), { target: { value: '^[A-Z]{3}$' } })
    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'date' } })
    expect(screen.getByLabelText('Format of the property')).toBeDisabled()
    expect(screen.getByLabelText('Format of the property')).toHaveValue('date')
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() =>
      expect(variablesApi.update).toHaveBeenCalledWith(
        'demo',
        'var-4',
        // The date type says it all: no fragment of its own.
        expect.objectContaining({ variable_type: 'date', json_schema: null }),
        null,
      ),
    )
  })

  it('shows a viewer the schema in words', async () => {
    renderPage('/p/demo/variables/var-3', 'viewer')
    await screen.findByRole('heading', { level: 1, name: '${cart}' })
    expect(screen.getByText('Schema').tagName).toBe('DT')
    expect(screen.getByText('(required)')).toBeInTheDocument()
    expect(screen.getByText(/≥ 0/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Schema type of the property')).not.toBeInTheDocument()
  })
})

describe('Property catalog: events tab (F23)', () => {
  const ENTRIES = [
    entry({ event_id: 'ev-1', event_name: 'checkout:start', required: true, presence_rate: 0.5, suggested_required: false }),
    entry({ event_id: 'ev-2', event_name: 'checkout:success', values: ['USD'], effective_values: ['USD'], presence_rate: 0.99, suggested_required: true, required_presence_threshold: 0.9 }),
  ]

  it('lists the events with required, values and presence, and links to the filtered events list', async () => {
    vi.mocked(propertyEntriesApi.forProperty).mockResolvedValue(ENTRIES)
    renderPage('/p/demo/variables/var-1?tab=events')

    const panel = await screen.findByRole('tabpanel')
    expect(await within(panel).findByText('checkout:start')).toBeInTheDocument()
    expect(within(panel).getByText('On 2 events · 1 required · 1 override')).toBeInTheDocument()
    expect(within(panel).getByText('Documented list')).toBeInTheDocument()
    expect(within(panel).getByText('50%')).toBeInTheDocument()
    expect(within(panel).getByText('· below 95%')).toBeInTheDocument()
    expect(within(panel).getByText('90%')).toBeInTheDocument()
    expect(within(panel).getByText('· looks required')).toBeInTheDocument()
    expect(within(panel).getByRole('link', { name: /Show in the events list/ })).toHaveAttribute(
      'href',
      '/p/demo/events?property=currency',
    )
    expect(screen.getByRole('tab', { name: /Events/ })).toHaveTextContent('2')
  })

  it('marks the selected events required in one request', async () => {
    vi.mocked(propertyEntriesApi.forProperty).mockResolvedValue(ENTRIES)
    vi.mocked(propertyEntriesApi.bulkSet).mockResolvedValue({ created: 0, updated: 2, removed: 0 })
    renderPage('/p/demo/variables/var-1?tab=events')

    fireEvent.click(await screen.findByRole('checkbox', { name: 'Select every event' }))
    const toolbar = screen.getByRole('toolbar', { name: 'Edit the selected events' })
    expect(toolbar).toHaveTextContent('2 events selected')
    fireEvent.click(within(toolbar).getByRole('button', { name: 'Mark required' }))

    await waitFor(() =>
      expect(propertyEntriesApi.bulkSet).toHaveBeenCalledWith('demo', 'var-1', ['ev-1', 'ev-2'], { required: true }, null),
    )
  })

  it('sets an allowed-values override on the selection', async () => {
    vi.mocked(propertyEntriesApi.forProperty).mockResolvedValue(ENTRIES)
    vi.mocked(propertyEntriesApi.bulkSet).mockResolvedValue({ created: 0, updated: 1, removed: 0 })
    renderPage('/p/demo/variables/var-1?tab=events')

    fireEvent.click(await screen.findByRole('checkbox', { name: 'Select checkout:start' }))
    fireEvent.click(screen.getByRole('button', { name: 'Set allowed values…' }))
    const input = screen.getByLabelText('Add allowed value for the selected events')
    fireEvent.change(input, { target: { value: 'GBP' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.click(screen.getByRole('button', { name: 'Apply to 1 event' }))

    await waitFor(() =>
      expect(propertyEntriesApi.bulkSet).toHaveBeenCalledWith(
        'demo', 'var-1', ['ev-1'], { values: ['USD', 'EUR', 'GBP'] }, null,
      ),
    )
  })

  it('removes the property from the selection after asking', async () => {
    vi.mocked(propertyEntriesApi.forProperty).mockResolvedValue(ENTRIES)
    vi.mocked(propertyEntriesApi.bulkRemove).mockResolvedValue({ created: 0, updated: 0, removed: 1 })
    renderPage('/p/demo/variables/var-1?tab=events')

    fireEvent.click(await screen.findByRole('checkbox', { name: 'Select checkout:success' }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove from events' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }))

    await waitFor(() =>
      expect(propertyEntriesApi.bulkRemove).toHaveBeenCalledWith('demo', 'var-1', ['ev-2'], null),
    )
  })

  it('adds the property to events picked from a search', async () => {
    vi.mocked(propertyEntriesApi.forProperty).mockResolvedValue(ENTRIES)
    vi.mocked(propertyEntriesApi.bulkSet).mockResolvedValue({ created: 1, updated: 0, removed: 0 })
    vi.mocked(eventsApi.list).mockResolvedValue({
      items: [
        { id: 'ev-1', name: 'checkout:start' },
        { id: 'ev-3', name: 'home:view' },
      ] as never,
      total: 2,
    })
    renderPage('/p/demo/variables/var-1?tab=events')

    fireEvent.focus(await screen.findByLabelText('Search events to add the property to'))
    const list = await screen.findByRole('list', { name: 'Events to add the property to' })
    // An event already carrying it is not offered again.
    expect(await within(list).findByText('home:view')).toBeInTheDocument()
    expect(within(list).queryByText('checkout:start')).toBeNull()
    fireEvent.click(within(list).getByRole('checkbox'))
    fireEvent.click(screen.getByRole('switch', { name: 'Add as required' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add to 1 event' }))

    await waitFor(() =>
      expect(propertyEntriesApi.bulkSet).toHaveBeenCalledWith('demo', 'var-1', ['ev-3'], { required: true }, null),
    )
  })

  it('is read-only for a viewer', async () => {
    vi.mocked(propertyEntriesApi.forProperty).mockResolvedValue(ENTRIES)
    renderPage('/p/demo/variables/var-1?tab=events', 'viewer')

    const panel = await screen.findByRole('tabpanel')
    expect(await within(panel).findByText('checkout:start')).toBeInTheDocument()
    // The column header and the one required row.
    expect(within(panel).getAllByText('Required')).toHaveLength(2)
    expect(within(panel).queryByRole('checkbox')).toBeNull()
    expect(within(panel).queryByRole('switch')).toBeNull()
    expect(screen.queryByText('Add to events')).toBeNull()
  })
})
