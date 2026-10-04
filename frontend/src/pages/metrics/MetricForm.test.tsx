import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'
import { AuthContext } from '@/components/auth-context'
import { authAs } from '@/test/auth'
import { ApiError } from '@/api/client'
import { expectNoAxeViolations } from '@/test/axe'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DataSource, EventListItem, MetricDefinitionDetailResponse } from '@/types'
import MetricEditPage, { MetricForm } from './MetricForm'

vi.mock('@/api/metricsCatalog', () => ({
  metricsCatalogApi: {
    create: vi.fn().mockResolvedValue({ id: 'created' }),
    update: vi.fn().mockResolvedValue({ id: 'updated' }),
    get: vi.fn(),
    preview: vi.fn(),
    previewFactOperand: vi.fn(),
  },
}))

// The event pickers search the server; `list` answers from EVENT_CATALOG the
// way the endpoint does (ILIKE on the name, `limit` rows, the true `total`).
vi.mock('@/api/events', () => ({
  eventsApi: { list: vi.fn(), get: vi.fn() },
}))
vi.mock('@/api/eventTypes', () => ({
  eventTypesApi: { list: vi.fn() },
}))
vi.mock('@/api/dataSources', () => ({
  dataSourcesApi: { list: vi.fn() },
}))
// The Owner picker lists the workspace roster.
vi.mock('@/api/users', () => ({
  usersApi: {
    list: vi.fn(async () => [
      { id: 'user-1', email: 'ana@example.com', name: 'Ana', role: 'editor', created_at: '2026-01-01T00:00:00Z' },
    ]),
  },
}))

vi.mock('@/api/factTables', () => ({
  factTablesApi: {
    list: vi.fn(),
    get: vi.fn(),
  },
}))

// The fact / event series dry run.
vi.mock('./catalogRequests', () => ({
  previewMetricSeries: vi.fn(),
}))

// CodeMirror needs real layout measurement jsdom can't provide; stub it with a
// plain textarea that forwards value/onChange/placeholder and the aria-label so
// the SQL editor stays queryable by accessible name.
vi.mock('@uiw/react-codemirror', () => ({
  default: ({
    value,
    onChange,
    placeholder,
    readOnly,
    'aria-label': ariaLabel,
    onCreateEditor,
  }: {
    value: string
    onChange: (v: string) => void
    placeholder?: string
    readOnly?: boolean
    'aria-label'?: string
    onCreateEditor?: (view: { contentDOM: HTMLElement; dom: HTMLElement }) => void
  }) => (
    <textarea
      // SqlEditor puts its id and ARIA on CodeMirror's contenteditable through
      // the editor view; the textarea stands in for that element.
      ref={el => {
        if (el) onCreateEditor?.({ contentDOM: el, dom: el })
      }}
      aria-label={ariaLabel}
      readOnly={readOnly}
      value={value}
      placeholder={placeholder}
      onChange={e => onChange(e.target.value)}
    />
  ),
}))

// The SQL editor and the column-suggestion inputs fetch the data-source schema;
// stub the hook so the form test never reaches the network. The default
// implementation (set in beforeEach) returns DS_SCHEMA once a source is picked
// and nothing otherwise, mirroring the real hook's `enabled: Boolean(dsId)`.
const { useDataSourceSchemaMock } = vi.hoisted(() => ({
  useDataSourceSchemaMock: vi.fn<(dsId?: string) => { data: unknown }>(),
}))
vi.mock('@/hooks/useDataSourceSchema', () => ({
  useDataSourceSchema: useDataSourceSchemaMock,
}))

import { metricsCatalogApi } from '@/api/metricsCatalog'
import { factTablesApi } from '@/api/factTables'
import { eventsApi } from '@/api/events'
import { eventTypesApi } from '@/api/eventTypes'
import { dataSourcesApi } from '@/api/dataSources'
import { previewMetricSeries } from './catalogRequests'
import { at } from '@/test/at'
import { personaAuth } from '@/test/persona'
import { SessionProject } from '@/test/PersonaProject'

// Radix drives the dropdown through pointer-capture APIs jsdom omits.
beforeAll(() => {
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.setPointerCapture = vi.fn()
  Element.prototype.releasePointerCapture = vi.fn()
})

const DATA_SOURCES = [
  { id: 'ds-1', name: 'Warehouse' },
] as unknown as DataSource[]

const DS_SCHEMA = {
  tables: [
    {
      name: 'events',
      columns: [
        { name: 'bucket', data_type: 'timestamptz' },
        { name: 'value', data_type: 'numeric' },
        { name: 'platform', data_type: 'text' },
        { name: 'country', data_type: 'text' },
        { name: 'app_version', data_type: 'text' },
      ],
    },
  ],
}

const EVENTS = [
  { id: 'ev-1', name: 'checkout:start' },
  { id: 'ev-2', name: 'checkout:done' },
] as unknown as EventListItem[]

// A catalog larger than any one page, so "beyond the first 200" is reachable.
let EVENT_CATALOG: EventListItem[] = EVENTS

const EVENT_TYPES = [
  { id: 'event-type-1', name: 'signup', display_name: 'Signup' },
  { id: 'event-type-2', name: 'purchase', display_name: 'Purchase' },
]

const FACT_TABLES = {
  total: 2,
  items: [
    { id: 'ft-1', display_name: 'Orders', name: 'orders' },
    { id: 'ft-2', display_name: 'Sessions', name: 'sessions' },
  ],
}

const FACT_TABLE_DETAIL = {
  id: 'ft-1',
  name: 'orders',
  display_name: 'Orders',
  columns: [
    { name: 'amount', type: 'number' },
    { name: 'user_id', type: 'string' },
  ],
  identifier_columns: ['user_id'],
  row_filters: [{ name: 'completed', sql: 'status = $1' }],
}

const EDIT_METRIC = {
  id: 'metric-1',
  project_id: 'p-1',
  kind: 'sql',
  name: 'order_count',
  display_name: 'Order count',
  description: 'Orders per hour',
  status: 'active',
  unit: null,
  color: '#6366f1',
  anomaly_detection_enabled: true,
  breakdown_columns: [],
  app_version_column: null,
  platform_column: null,
  data_source_id: 'ds-1',
  interval: '1h',
  replay_chunk_interval: '1h',
  aggregation: 'count',
  composition: null,
  numerator_event_id: null,
  denominator_event_id: null,
  reviewed: false,
  order: 0,
  config: {
    metric_sql: 'SELECT bucket, count(*) AS value FROM events GROUP BY 1',
    time_column: 'bucket',
  },
  created_at: '2026-06-01T00:00:00Z',
  updated_at: '2026-06-20T00:00:00Z',
} as unknown as MetricDefinitionDetailResponse

let queryClient: QueryClient

function wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: queryClient }, children)
}

/** The bare form outside a page: it links out (e.g. "Add events" when the
 * project tracks none), so it needs the router the app always gives it. */
function formWrapper({ children }: { children: ReactNode }) {
  return wrapper({ children: createElement(MemoryRouter, null, children) })
}

/**
 * Render the form. A new metric starts on "From tracked events"; most
 * of these cases exercise the SQL kind, so a create form switches to Custom
 * SQL first unless `pickSql` is false (which keeps the form pristine).
 */
function renderForm(
  metric: MetricDefinitionDetailResponse | null = null,
  dataSources: DataSource[] = DATA_SOURCES,
  { pickSql = true }: { pickSql?: boolean } = {},
) {
  const onClose = vi.fn()
  render(
    createElement(MetricForm, {
      slug: 'demo',
      metric,
      dataSources,
      onClose,
    }),
    { wrapper: formWrapper },
  )
  if (!metric && pickSql) fireEvent.click(screen.getByRole('radio', { name: /Custom SQL/ }))
  return { onClose }
}

/** Wait until a picker offers `value`, then pick it. */
async function pickOption(selectId: string, value: string) {
  await waitFor(() =>
    expect(document.querySelector(`#${selectId} option[value="${value}"]`)).not.toBeNull(),
  )
  fireEvent.change(document.getElementById(selectId)!, { target: { value } })
}

/** The option list an event combobox (EventRefPicker) controls, once it is open. */
async function eventListbox(input: HTMLElement) {
  return waitFor(() => {
    const listbox = document.getElementById(input.getAttribute('aria-controls') ?? '')
    expect(listbox).not.toBeNull()
    return listbox!
  })
}

/** Open an event combobox, wait until it offers `name`, then click that row. */
async function pickEvent(inputId: string, name: string | RegExp) {
  const input = document.getElementById(inputId)!
  act(() => input.focus())
  // Focus opens it the first time; after an earlier pick focus stays put, and a click reopens it.
  fireEvent.click(input)
  const listbox = await eventListbox(input)
  fireEvent.click(await within(listbox).findByRole('option', { name }))
}

async function openAddFilterMenu() {
  fireEvent.keyDown(screen.getByRole('button', { name: 'Add filter' }), { key: 'Enter' })
  return screen.findByRole('menu')
}

/** Open the collapsed "Breakdowns and dimensions" section. */
function showDimensions() {
  const trigger = screen.getByRole('button', { name: /Breakdowns and dimensions/ })
  if (trigger.getAttribute('aria-expanded') !== 'true') fireEvent.click(trigger)
}

function submit() {
  fireEvent.click(screen.getByRole('button', { name: /Create and start collecting|Save metric/ }))
}

/** Confirm the "hasn't previewed" ask a new SQL metric gets on create. */
async function createAnyway() {
  const dialog = await screen.findByRole('alertdialog')
  fireEvent.click(within(dialog).getByRole('button', { name: 'Create anyway' }))
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  useDataSourceSchemaMock.mockReset()
  useDataSourceSchemaMock.mockImplementation((dsId?: string) => ({
    data: dsId ? DS_SCHEMA : undefined,
  }))
  vi.mocked(metricsCatalogApi.create).mockClear()
  vi.mocked(metricsCatalogApi.update).mockClear()
  vi.mocked(metricsCatalogApi.preview).mockReset()
  vi.mocked(previewMetricSeries).mockReset()
  vi.mocked(factTablesApi.list).mockReset()
  vi.mocked(factTablesApi.get).mockReset()
  vi.mocked(factTablesApi.list).mockResolvedValue(
    FACT_TABLES as unknown as Awaited<ReturnType<typeof factTablesApi.list>>,
  )
  vi.mocked(factTablesApi.get).mockResolvedValue(
    FACT_TABLE_DETAIL as unknown as Awaited<ReturnType<typeof factTablesApi.get>>,
  )
  EVENT_CATALOG = EVENTS
  vi.mocked(eventsApi.list).mockReset()
  vi.mocked(eventsApi.list).mockImplementation(async (_slug, params) => {
    const needle = (params?.search ?? '').toLowerCase()
    const matches = EVENT_CATALOG.filter(event => event.name.toLowerCase().includes(needle))
    return {
      items: matches.slice(0, params?.limit ?? 200),
      total: matches.length,
    } as unknown as Awaited<ReturnType<typeof eventsApi.list>>
  })
  vi.mocked(eventsApi.get).mockReset()
  vi.mocked(eventsApi.get).mockImplementation(async (_slug, id) => {
    const event = EVENT_CATALOG.find(candidate => candidate.id === id)
    if (!event) throw new Error('Event not found')
    return event as unknown as Awaited<ReturnType<typeof eventsApi.get>>
  })
  vi.mocked(eventTypesApi.list).mockReset()
  vi.mocked(eventTypesApi.list).mockResolvedValue(
    EVENT_TYPES as unknown as Awaited<ReturnType<typeof eventTypesApi.list>>,
  )
})

afterEach(() => {
  queryClient.clear()
})

describe('MetricForm validation', () => {
  it('rejects a SQL metric that is missing required identity/config', async () => {
    renderForm()

    // SQL form with no display name, data source, or query.
    submit()

    // Each message renders both inline (under its field) and in the summary list.
    expect((await screen.findAllByText('Display name is required.')).length).toBeGreaterThan(0)
    expect(screen.getAllByText('A data source is required for a SQL metric.').length).toBeGreaterThan(0)
    expect(screen.getAllByText('The metric SQL query is required.').length).toBeGreaterThan(0)
    expect(metricsCatalogApi.create).not.toHaveBeenCalled()
  })

  it('requires a time column for a SQL metric', async () => {
    renderForm()

    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Total revenue' },
    })
    fireEvent.change(screen.getByLabelText('Internal name', { exact: false }), {
      target: { value: 'total_revenue' },
    })
    fireEvent.change(document.getElementById('metric-sql-data-source')!, { target: { value: 'ds-1' } })
    fireEvent.change(screen.getByLabelText('Metric SQL'), {
      target: { value: 'SELECT 1 AS value' },
    })

    submit()

    expect(
      (await screen.findAllByText('A time column is required for a SQL metric.')).length,
    ).toBeGreaterThan(0)
    expect(metricsCatalogApi.create).not.toHaveBeenCalled()
  })

  it('creates a valid SQL metric', async () => {
    const { onClose } = renderForm()

    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Order count' },
    })
    fireEvent.change(screen.getByLabelText('Internal name', { exact: false }), {
      target: { value: 'order_count' },
    })
    fireEvent.change(document.getElementById('metric-sql-data-source')!, { target: { value: 'ds-1' } })
    fireEvent.change(screen.getByLabelText('Metric SQL'), {
      target: { value: 'SELECT bucket, count(*) AS value FROM events GROUP BY 1' },
    })
    fireEvent.change(document.getElementById('metric-sql-time')!, { target: { value: 'bucket' } })

    submit()
    // Never previewed, so the create asks first.
    await createAnyway()

    await waitFor(() => expect(metricsCatalogApi.create).toHaveBeenCalledTimes(1))
    expect(metricsCatalogApi.create).toHaveBeenCalledWith(
      'demo',
      expect.objectContaining({
        kind: 'sql',
        data_source_id: 'ds-1',
        name: 'order_count',
        display_name: 'Order count',
      }),
    )
    // No `order`: the contract lists it optional and the backend appends a
    // metric that names no position.
    const payload = at(vi.mocked(metricsCatalogApi.create).mock.calls, 0)[1]
    expect(payload).not.toHaveProperty('order')
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('pre-fills the internal name from the display name in snake_case until edited', () => {
    renderForm()

    const display = screen.getByLabelText('Display name', { exact: false })
    fireEvent.change(display, { target: { value: 'Checkout Conversion!' } })
    const internal = screen.getByLabelText('Internal name', { exact: false }) as HTMLInputElement
    expect(internal.value).toBe('checkout_conversion')

    // Editing the internal name directly stops further auto-derivation.
    fireEvent.change(internal, { target: { value: 'my_metric' } })
    fireEvent.change(display, { target: { value: 'Something Else' } })
    expect(
      (screen.getByLabelText('Internal name', { exact: false }) as HTMLInputElement).value,
    ).toBe('my_metric')
  })

  it('keeps the internal name read-only and renders editable kind/config in edit mode', () => {
    renderForm(EDIT_METRIC)

    expect(screen.getByRole('heading', { name: 'Edit · Order count' })).toBeInTheDocument()
    // Internal name is shown as text, not an editable input. Queried by role
    // because the row itself is now named "Internal name" — it holds no control,
    // so it is a labelled group rather than a <label> pointing at nothing.
    expect(screen.queryByRole('textbox', { name: /Internal name/ })).toBeNull()
    expect(document.getElementById('metric-name')).toBeNull()
    expect(screen.getByText('order_count')).toBeInTheDocument()
    // The kind-specific config is editable after creation.
    expect(document.getElementById('metric-sql-data-source')).not.toBeNull()
    expect(screen.getByLabelText('Metric SQL')).toBeInTheDocument()
    expect(document.getElementById('metric-sql-time')).not.toBeNull()
  })

  it('saves presentation edits together with the current definition', async () => {
    const { onClose } = renderForm(EDIT_METRIC)

    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Orders / hour' },
    })

    submit()

    await waitFor(() => expect(metricsCatalogApi.update).toHaveBeenCalledTimes(1))
    expect(metricsCatalogApi.update).toHaveBeenCalledWith(
      'demo',
      'metric-1',
      expect.objectContaining({
        display_name: 'Orders / hour',
        definition: expect.objectContaining({
          kind: 'sql',
          data_source_id: 'ds-1',
          interval: '1h',
          replay_chunk_interval: '1h',
          config: {
            metric_sql: 'SELECT bucket, count(*) AS value FROM events GROUP BY 1',
            time_column: 'bucket',
            value_column: null,
          },
        }),
      }),
    )
    expect(screen.queryByText('The metric SQL query is required.')).toBeNull()
    expect(metricsCatalogApi.create).not.toHaveBeenCalled()
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('round-trips an existing fact ratio definition without dropping numerator config', async () => {
    const metric = {
      ...EDIT_METRIC,
      kind: 'fact',
      name: 'revenue_per_session',
      display_name: 'Revenue / session',
      data_source_id: null,
      interval: '1h',
      replay_chunk_interval: '15m',
      fact_table_id: 'ft-1',
      aggregation: 'sum',
      composition: 'ratio',
      config: {
        numerator: {
          fact_table_id: 'ft-1',
          aggregation: 'sum',
          measure_column: 'amount',
          row_filters: ['completed'],
          filter_sql: 'amount > 0',
        },
        denominator: {
          fact_table_id: 'ft-2',
          aggregation: 'count_distinct',
          distinct_column: 'session_id',
        },
      },
    } as unknown as MetricDefinitionDetailResponse

    renderForm(metric)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save metric' })).toBeEnabled())
    submit()

    await waitFor(() => expect(metricsCatalogApi.update).toHaveBeenCalledTimes(1))
    expect(metricsCatalogApi.update).toHaveBeenCalledWith(
      'demo',
      'metric-1',
      expect.objectContaining({
        definition: expect.objectContaining({
          kind: 'fact',
          composition: 'ratio',
          replay_chunk_interval: '15m',
          numerator: expect.objectContaining({
            fact_table_id: 'ft-1',
            aggregation: 'sum',
            measure_column: 'amount',
            row_filters: ['completed'],
            // Verbatim: an untouched load→save must not add parens.
            filter_sql: 'amount > 0',
          }),
          denominator: expect.objectContaining({
            fact_table_id: 'ft-2',
            aggregation: 'count_distinct',
            distinct_column: 'session_id',
          }),
        }),
      }),
    )
  })

  it('preserves event-type refs for existing event-composition metrics', async () => {
    const metric = {
      ...EDIT_METRIC,
      kind: 'event_composition',
      name: 'signup_type_count',
      display_name: 'Signup type count',
      data_source_id: null,
      interval: null,
      replay_chunk_interval: null,
      aggregation: null,
      composition: 'single',
      numerator_event_id: null,
      numerator_event_type_id: 'event-type-1',
      config: {},
    } as unknown as MetricDefinitionDetailResponse

    renderForm(metric)
    submit()

    await waitFor(() => expect(metricsCatalogApi.update).toHaveBeenCalledTimes(1))
    expect(metricsCatalogApi.update).toHaveBeenCalledWith(
      'demo',
      'metric-1',
      expect.objectContaining({
        definition: expect.objectContaining({
          kind: 'event_composition',
          composition: 'single',
          numerator_event_id: null,
          numerator_event_type_id: 'event-type-1',
          denominator_event_id: null,
          denominator_event_type_id: null,
        }),
      }),
    )
  })

  it('validates editable SQL config in edit mode', async () => {
    renderForm(EDIT_METRIC)

    fireEvent.change(screen.getByLabelText('Metric SQL'), { target: { value: '   ' } })

    submit()

    expect(
      (await screen.findAllByText('The metric SQL query is required.')).length,
    ).toBeGreaterThan(0)
    expect(metricsCatalogApi.update).not.toHaveBeenCalled()
  })

  it('updates an existing SQL metric into a single fact metric definition', async () => {
    renderForm(EDIT_METRIC)

    // Switching kind applies at once; the history-loss confirm comes at save.
    fireEvent.click(screen.getByRole('radio', { name: /From a fact table/ }))
    await waitFor(() =>
      expect(document.querySelector('#metric-fact-table option[value="ft-1"]')).not.toBeNull(),
    )
    fireEvent.change(document.getElementById('metric-fact-table')!, { target: { value: 'ft-1' } })
    fireEvent.change(document.getElementById('metric-fact-aggregation')!, { target: { value: 'sum' } })
    await waitFor(() =>
      expect(document.querySelector('#metric-fact-measure option[value="amount"]')).not.toBeNull(),
    )
    fireEvent.change(document.getElementById('metric-fact-measure')!, { target: { value: 'amount' } })

    submit()
    fireEvent.click(await screen.findByRole('button', { name: 'Save and delete history' }))

    await waitFor(() => expect(metricsCatalogApi.update).toHaveBeenCalledTimes(1))
    expect(metricsCatalogApi.update).toHaveBeenCalledWith(
      'demo',
      'metric-1',
      expect.objectContaining({
        definition: expect.objectContaining({
          kind: 'fact',
          composition: 'single',
          interval: '1h',
          fact_table_id: 'ft-1',
          aggregation: 'sum',
          measure_column: 'amount',
          distinct_column: null,
          row_filters: [],
          filter_sql: null,
        }),
      }),
    )
  })

  it('still requires a display name when editing', async () => {
    renderForm(EDIT_METRIC)

    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: '   ' },
    })

    submit()

    expect((await screen.findAllByText('Display name is required.')).length).toBeGreaterThan(0)
    expect(metricsCatalogApi.update).not.toHaveBeenCalled()
  })

  it('requires a denominator for a ratio event_composition, then accepts one', async () => {
    renderForm()

    // Switch to event composition.
    fireEvent.click(screen.getByRole('radio', { name: /From tracked events/ }))
    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Checkout ratio' },
    })
    fireEvent.change(screen.getByLabelText('Internal name', { exact: false }), {
      target: { value: 'checkout_ratio' },
    })
    fireEvent.change(document.getElementById('metric-composition')!, { target: { value: 'ratio' } })
    await pickEvent('metric-numerator', 'checkout:done')

    submit()

    expect(
      (await screen.findAllByText('A denominator event is required for a ratio metric.')).length,
    ).toBeGreaterThan(0)
    expect(metricsCatalogApi.create).not.toHaveBeenCalled()

    // Provide the denominator and resubmit.
    await pickEvent('metric-denominator', 'checkout:start')
    submit()

    await waitFor(() => expect(metricsCatalogApi.create).toHaveBeenCalledTimes(1))
    expect(metricsCatalogApi.create).toHaveBeenCalledWith(
      'demo',
      expect.objectContaining({
        kind: 'event_composition',
        composition: 'ratio',
        numerator_event_id: 'ev-2',
        denominator_event_id: 'ev-1',
      }),
    )
  })

  function fillFactIdentity(displayName: string, name: string) {
    fireEvent.click(screen.getByRole('radio', { name: /From a fact table/ }))
    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: displayName },
    })
    fireEvent.change(screen.getByLabelText('Internal name', { exact: false }), {
      target: { value: name },
    })
  }

  it('builds a single fact metric with a sum over a measure column', async () => {
    renderForm()
    fillFactIdentity('Total revenue', 'total_revenue')

    // Pick the fact table once the list (and its options) has loaded; its detail
    // (columns) then loads asynchronously.
    await waitFor(() =>
      expect(document.querySelector('#metric-fact-table option[value="ft-1"]')).not.toBeNull(),
    )
    fireEvent.change(document.getElementById('metric-fact-table')!, { target: { value: 'ft-1' } })
    await waitFor(() => expect(factTablesApi.get).toHaveBeenCalledWith('demo', 'ft-1'))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Check filters/i })).toBeEnabled(),
    )

    // Sum requires a measure column; the dropdown appears and fills from columns.
    fireEvent.change(document.getElementById('metric-fact-aggregation')!, { target: { value: 'sum' } })
    await waitFor(() =>
      expect(document.querySelector('#metric-fact-measure option[value="amount"]')).not.toBeNull(),
    )
    fireEvent.change(document.getElementById('metric-fact-measure')!, { target: { value: 'amount' } })

    submit()

    await waitFor(() => expect(metricsCatalogApi.create).toHaveBeenCalledTimes(1))
    expect(metricsCatalogApi.create).toHaveBeenCalledWith(
      'demo',
      expect.objectContaining({
        kind: 'fact',
        composition: 'single',
        fact_table_id: 'ft-1',
        aggregation: 'sum',
        measure_column: 'amount',
        distinct_column: null,
        row_filters: [],
        filter_sql: null,
      }),
    )
  })

  it('combines a named filter with a free-text SQL filter via Add filter', async () => {
    renderForm()
    fillFactIdentity('Completed revenue', 'completed_revenue')

    await waitFor(() =>
      expect(document.querySelector('#metric-fact-table option[value="ft-1"]')).not.toBeNull(),
    )
    fireEvent.change(document.getElementById('metric-fact-table')!, { target: { value: 'ft-1' } })
    await waitFor(() => expect(factTablesApi.get).toHaveBeenCalledWith('demo', 'ft-1'))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Check filters/i })).toBeEnabled(),
    )

    // Add a named filter (the "Named filter" option appears once the fact
    // table's named filters have loaded).
    fireEvent.click(within(await openAddFilterMenu()).getByRole('menuitem', { name: 'Named filter' }))
    fireEvent.change(screen.getByLabelText('Filter 1 named filter'), {
      target: { value: 'completed' },
    })

    // Add a free-text SQL filter.
    fireEvent.click(within(await openAddFilterMenu()).getByRole('menuitem', { name: 'SQL filter' }))
    fireEvent.change(screen.getByLabelText('Filter 2 SQL'), { target: { value: 'amount > 0' } })

    submit()

    await waitFor(() => expect(metricsCatalogApi.create).toHaveBeenCalledTimes(1))
    expect(metricsCatalogApi.create).toHaveBeenCalledWith(
      'demo',
      expect.objectContaining({
        kind: 'fact',
        composition: 'single',
        fact_table_id: 'ft-1',
        row_filters: ['completed'],
        // A single SQL fragment is stored verbatim; the collector wraps it.
        filter_sql: 'amount > 0',
      }),
    )
  })

  it('submits a structured condition filter for fact metrics', async () => {
    renderForm()
    fillFactIdentity('Qualified orders', 'qualified_orders')

    await waitFor(() =>
      expect(document.querySelector('#metric-fact-table option[value="ft-1"]')).not.toBeNull(),
    )
    fireEvent.change(document.getElementById('metric-fact-table')!, { target: { value: 'ft-1' } })
    await waitFor(() => expect(factTablesApi.get).toHaveBeenCalledWith('demo', 'ft-1'))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Check filters/i })).toBeEnabled(),
    )

    fireEvent.click(within(await openAddFilterMenu()).getByRole('menuitem', { name: 'Condition' }))
    fireEvent.change(screen.getByLabelText('Filter 1 condition column'), {
      target: { value: 'amount' },
    })
    fireEvent.change(screen.getByLabelText('Filter 1 condition operator'), {
      target: { value: 'gt' },
    })
    fireEvent.change(screen.getByLabelText('Filter 1 condition value'), {
      target: { value: '3' },
    })

    submit()

    await waitFor(() => expect(metricsCatalogApi.create).toHaveBeenCalledTimes(1))
    expect(metricsCatalogApi.create).toHaveBeenCalledWith(
      'demo',
      expect.objectContaining({
        kind: 'fact',
        composition: 'single',
        fact_table_id: 'ft-1',
        conditions: [{ column: 'amount', operator: 'gt', value: 3 }],
      }),
    )
  })

  it('blocks filter checks and save until fact-table column types load', async () => {
    let resolveDetail!: (value: Awaited<ReturnType<typeof factTablesApi.get>>) => void
    vi.mocked(factTablesApi.get).mockReturnValue(
      new Promise<Awaited<ReturnType<typeof factTablesApi.get>>>(resolve => {
        resolveDetail = resolve
      }),
    )
    renderForm()
    fillFactIdentity('Qualified orders', 'qualified_orders')

    await waitFor(() =>
      expect(document.querySelector('#metric-fact-table option[value="ft-1"]')).not.toBeNull(),
    )
    fireEvent.change(document.getElementById('metric-fact-table')!, { target: { value: 'ft-1' } })

    expect(await screen.findByRole('button', { name: /Check filters/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Create and start collecting' })).toBeDisabled()

    await act(async () => {
      resolveDetail(
        FACT_TABLE_DETAIL as unknown as Awaited<ReturnType<typeof factTablesApi.get>>,
      )
    })

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Check filters/i })).toBeEnabled()
      expect(screen.getByRole('button', { name: 'Create and start collecting' })).toBeEnabled()
    })
  })

  it('surfaces a fact-table detail failure and keeps type-dependent actions disabled', async () => {
    vi.mocked(factTablesApi.get).mockRejectedValue(new Error('Fact table details unavailable'))
    renderForm()
    fillFactIdentity('Qualified orders', 'qualified_orders')

    await waitFor(() =>
      expect(document.querySelector('#metric-fact-table option[value="ft-1"]')).not.toBeNull(),
    )
    fireEvent.change(document.getElementById('metric-fact-table')!, { target: { value: 'ft-1' } })

    expect(await screen.findByText('Could not load fact table details')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Check filters/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Create and start collecting' })).toBeDisabled()
    expect(metricsCatalogApi.create).not.toHaveBeenCalled()
  })

  it('rejects a sum fact metric with no measure column', async () => {
    renderForm()
    fillFactIdentity('Total revenue', 'total_revenue')

    fireEvent.change(document.getElementById('metric-fact-table')!, { target: { value: 'ft-1' } })
    fireEvent.change(document.getElementById('metric-fact-aggregation')!, { target: { value: 'sum' } })

    submit()

    expect(
      (await screen.findAllByText('A measure column is required for the sum aggregation.')).length,
    ).toBeGreaterThan(0)
    expect(metricsCatalogApi.create).not.toHaveBeenCalled()
  })

  it('builds a ratio fact metric with numerator and denominator operands', async () => {
    renderForm()
    fillFactIdentity('Revenue per session', 'revenue_per_session')

    fireEvent.change(document.getElementById('metric-fact-composition')!, { target: { value: 'ratio' } })

    // Both operands use count, so no measure/distinct column is required.
    await waitFor(() =>
      expect(document.querySelector('#metric-fact-num-table option[value="ft-1"]')).not.toBeNull(),
    )
    fireEvent.change(document.getElementById('metric-fact-num-table')!, { target: { value: 'ft-1' } })
    fireEvent.change(document.getElementById('metric-fact-den-table')!, { target: { value: 'ft-2' } })

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Create and start collecting' })).toBeEnabled(),
    )
    submit()

    await waitFor(() => expect(metricsCatalogApi.create).toHaveBeenCalledTimes(1))
    expect(metricsCatalogApi.create).toHaveBeenCalledWith(
      'demo',
      expect.objectContaining({
        kind: 'fact',
        composition: 'ratio',
        numerator: expect.objectContaining({ fact_table_id: 'ft-1', aggregation: 'count' }),
        denominator: expect.objectContaining({ fact_table_id: 'ft-2', aggregation: 'count' }),
      }),
    )
  })

  it('renders an inline error under the display name field on submit', async () => {
    renderForm()

    submit()

    // The message appears twice: once inline under the field, once in the summary.
    const matches = await screen.findAllByText('Display name is required.')
    const alert = screen.getByRole('alert')
    const inline = matches.filter(node => !alert.contains(node))
    expect(inline).toHaveLength(1)
  })

  it('labels the event select "Event" and hides the denominator for single composition', () => {
    renderForm()

    fireEvent.click(screen.getByRole('radio', { name: /From tracked events/ }))

    // Default composition is single: the event select is labelled "Event".
    expect(screen.getByLabelText('Event').id).toBe('metric-numerator')
    expect(screen.queryByLabelText('Numerator event')).toBeNull()
    // No denominator select is rendered for single composition.
    expect(document.getElementById('metric-denominator')).toBeNull()
  })

  it('hides warehouse monitoring fields for an event-composition metric', () => {
    renderForm()

    fireEvent.click(screen.getByRole('radio', { name: /From tracked events/ }))

    expect(document.getElementById('metric-breakdowns')).toBeNull()
    expect(document.getElementById('metric-app-version')).toBeNull()
    expect(document.getElementById('metric-platform')).toBeNull()
    // Anomaly detection stays available for every kind.
    expect(screen.getByRole('switch', { name: 'Anomaly detection' })).toBeInTheDocument()
  })

  // Fill the three inputs the SQL preview needs (data source + SQL + time column).
  function fillSqlPreviewPrerequisites() {
    fireEvent.change(document.getElementById('metric-sql-data-source')!, { target: { value: 'ds-1' } })
    fireEvent.change(screen.getByLabelText('Metric SQL'), {
      target: { value: 'SELECT bucket, count(*) AS value FROM events GROUP BY 1' },
    })
    fireEvent.change(document.getElementById('metric-sql-time')!, { target: { value: 'bucket' } })
  }

  it('enables Preview only once data source, SQL, and time column are set', () => {
    renderForm()

    const previewButton = screen.getByRole('button', { name: 'Preview' })
    expect(previewButton).toBeDisabled()

    fireEvent.change(document.getElementById('metric-sql-data-source')!, { target: { value: 'ds-1' } })
    fireEvent.change(screen.getByLabelText('Metric SQL'), {
      target: { value: 'SELECT bucket, count(*) AS value FROM events GROUP BY 1' },
    })
    // Still missing the time column.
    expect(previewButton).toBeDisabled()

    fireEvent.change(document.getElementById('metric-sql-time')!, { target: { value: 'bucket' } })
    expect(previewButton).toBeEnabled()
  })

  it('runs a preview and renders the bucket summary for a successful dry-run', async () => {
    vi.mocked(metricsCatalogApi.preview).mockResolvedValue({
      columns: ['bucket', 'value'],
      points: [
        { bucket: '2026-07-01T00:00:00Z', value: 1 },
        { bucket: '2026-07-01T01:00:00Z', value: 4 },
        { bucket: '2026-07-01T02:00:00Z', value: 2 },
      ],
      point_count: 3,
      truncated: false,
      error: null,
    })
    renderForm()
    fillSqlPreviewPrerequisites()

    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))

    expect(await screen.findByText('3 buckets · columns: bucket, value')).toBeInTheDocument()
    expect(metricsCatalogApi.preview).toHaveBeenCalledWith('demo', {
      data_source_id: 'ds-1',
      sql: 'SELECT bucket, count(*) AS value FROM events GROUP BY 1',
      time_column: 'bucket',
      value_column: null,
      interval: '1h',
    })
    // No save call happens as part of previewing.
    expect(metricsCatalogApi.create).not.toHaveBeenCalled()
  })

  it('surfaces a warehouse/SQL error returned by the preview', async () => {
    vi.mocked(metricsCatalogApi.preview).mockResolvedValue({
      columns: [],
      points: [],
      point_count: 0,
      truncated: false,
      error: 'relation "evnts" does not exist',
    })
    renderForm()
    fillSqlPreviewPrerequisites()

    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))

    expect(await screen.findByText('relation "evnts" does not exist')).toBeInTheDocument()

    // Editing the SQL invalidates the transient result panel.
    fireEvent.change(screen.getByLabelText('Metric SQL'), {
      target: { value: 'SELECT bucket, count(*) AS value FROM events GROUP BY 1 -- fixed' },
    })
    expect(screen.queryByText('relation "evnts" does not exist')).toBeNull()
  })

  it('switches kind on edit without a confirm; the history warning moves to save', async () => {
    renderForm(EDIT_METRIC)

    fireEvent.click(screen.getByRole('radio', { name: /From a fact table/ }))

    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(screen.getByRole('radio', { name: /From a fact table/ })).toHaveAttribute('aria-checked', 'true')
    // The consequence is on screen before Save, not only in the confirm.
    expect(screen.getByText(/Saving deletes its collected values/)).toBeInTheDocument()
  })

  it('suggests schema columns for the time column and fills on pick', async () => {
    renderForm()

    fireEvent.change(document.getElementById('metric-sql-data-source')!, { target: { value: 'ds-1' } })
    // Suggestions come from the tables the query names.
    fireEvent.change(screen.getByLabelText('Metric SQL'), {
      target: { value: 'SELECT bucket, count(*) AS value FROM events GROUP BY 1' },
    })
    const timeInput = document.getElementById('metric-sql-time') as HTMLInputElement
    fireEvent.change(timeInput, { target: { value: 'buck' } })

    const listbox = await screen.findByRole('listbox', { name: 'Column suggestions' })
    fireEvent.click(within(listbox).getByRole('option', { name: 'bucket' }))

    expect(timeInput.value).toBe('bucket')
    expect(screen.queryByRole('listbox', { name: 'Column suggestions' })).toBeNull()
  })

  it('ticks breakdown columns and submits them in the payload', async () => {
    renderForm()

    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Order count' },
    })
    fireEvent.change(document.getElementById('metric-sql-data-source')!, { target: { value: 'ds-1' } })
    fireEvent.change(screen.getByLabelText('Metric SQL'), {
      target: { value: 'SELECT bucket, count(*) AS value FROM events GROUP BY 1' },
    })
    fireEvent.change(document.getElementById('metric-sql-time')!, { target: { value: 'bucket' } })

    // The picker offers the data-source schema columns as checkboxes; tick two.
    showDimensions()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Break down by platform' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Break down by country' }))
    expect(screen.getByRole('checkbox', { name: 'Break down by platform' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Break down by country' })).toBeChecked()

    submit()
    // Never previewed, so the create asks first.
    await createAnyway()

    await waitFor(() => expect(metricsCatalogApi.create).toHaveBeenCalledTimes(1))
    expect(metricsCatalogApi.create).toHaveBeenCalledWith(
      'demo',
      expect.objectContaining({ breakdown_columns: ['platform', 'country'] }),
    )
  })

  it('offers no free-text add input in the breakdown picker and unticks cleanly', async () => {
    renderForm()

    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Order count' },
    })
    fireEvent.change(document.getElementById('metric-sql-data-source')!, { target: { value: 'ds-1' } })
    fireEvent.change(screen.getByLabelText('Metric SQL'), {
      target: { value: 'SELECT bucket, count(*) AS value FROM events GROUP BY 1' },
    })
    fireEvent.change(document.getElementById('metric-sql-time')!, { target: { value: 'bucket' } })

    // Checkbox-only picker: the embedded add-column combobox that duplicated
    // the checkbox list was removed.
    showDimensions()
    const group = document.getElementById('metric-breakdowns')!
    expect(within(group).queryByRole('combobox')).toBeNull()

    const platform = screen.getByRole('checkbox', { name: 'Break down by platform' })
    fireEvent.click(platform)
    expect(platform).toBeChecked()
    fireEvent.click(platform)
    expect(platform).not.toBeChecked()

    submit()
    // Never previewed, so the create asks first.
    await createAnyway()

    await waitFor(() => expect(metricsCatalogApi.create).toHaveBeenCalledTimes(1))
    expect(metricsCatalogApi.create).toHaveBeenCalledWith(
      'demo',
      expect.objectContaining({ breakdown_columns: [] }),
    )
  })

  it('renders a saved breakdown column missing from the schema as a checked box', () => {
    // A custom column saved before the add-input was removed (or added via the
    // API) must stay visible and removable via the options union.
    renderForm({
      ...EDIT_METRIC,
      breakdown_columns: ['custom_dim'],
    } as unknown as MetricDefinitionDetailResponse)

    expect(screen.getByRole('checkbox', { name: 'Break down by custom_dim' })).toBeChecked()
  })

  it('keeps column inputs plain when no data source is selected', () => {
    renderForm()

    const timeInput = document.getElementById('metric-sql-time') as HTMLInputElement
    fireEvent.change(timeInput, { target: { value: 'buck' } })

    // No schema loaded -> no suggestion listbox; the field is a plain input.
    expect(screen.queryByRole('listbox', { name: 'Column suggestions' })).toBeNull()
    expect(timeInput.value).toBe('buck')
  })
})

describe('MetricForm templates', () => {
  const TEMPLATE_EDIT_METRIC = {
    id: 'metric-1',
    project_id: 'p-1',
    kind: 'sql',
    name: 'order_count',
    display_name: 'Order count',
    description: '',
    status: 'active',
    unit: null,
    color: '#6366f1',
    anomaly_detection_enabled: true,
    breakdown_columns: [],
    app_version_column: null,
    platform_column: null,
    data_source_id: 'ds-1',
    interval: '1h',
    replay_chunk_interval: '1h',
    aggregation: 'count',
    composition: null,
    numerator_event_id: null,
    denominator_event_id: null,
    reviewed: false,
    order: 0,
    config: {
      metric_sql: 'SELECT bucket, count(*) AS value FROM events GROUP BY 1',
      time_column: 'bucket',
    },
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-20T00:00:00Z',
  } as unknown as MetricDefinitionDetailResponse

  it('shows the starter template gallery in create mode', () => {
    renderForm()

    expect(screen.getByText('Start from a template')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Daily active users/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Conversion A→B/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Start from scratch' })).toBeInTheDocument()
  })

  it('seeds kind=event_composition and unit=% from the "Conversion A→B" template', () => {
    renderForm()

    fireEvent.click(screen.getByRole('button', { name: /Conversion A→B/ }))

    // The event-composition kind radio is now checked...
    expect(screen.getByRole('radio', { name: /From tracked events/ })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    // ...and the unit input carries the seeded '%'.
    expect((document.getElementById('metric-unit') as HTMLInputElement).value).toBe('%')
    // Picking a template dismisses the gallery, leaving the prefilled form.
    expect(screen.queryByText('Start from a template')).toBeNull()
  })

  it('dismisses the gallery and leaves an empty form on "Start from scratch"', () => {
    renderForm(null, DATA_SOURCES, { pickSql: false })

    fireEvent.click(screen.getByRole('button', { name: 'Start from scratch' }))

    expect(screen.queryByText('Start from a template')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Start from scratch' })).toBeNull()
    // The form is present and pristine: the no-SQL kind, empty display name.
    expect(
      (screen.getByLabelText('Display name', { exact: false }) as HTMLInputElement).value,
    ).toBe('')
    expect(screen.getByRole('radio', { name: /From tracked events/ })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    expect(screen.getByRole('radio', { name: /Custom SQL/ })).toHaveAttribute('aria-checked', 'false')
  })

  it('brings the gallery back after "Start from scratch"', () => {
    renderForm(null, DATA_SOURCES, { pickSql: false })
    fireEvent.click(screen.getByRole('button', { name: 'Start from scratch' }))

    fireEvent.click(screen.getByRole('button', { name: 'Browse templates' }))

    expect(screen.getByText('Start from a template')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Conversion A→B/ })).toBeInTheDocument()
  })

  it('lists the kinds most approachable first and says which the project cannot use yet', async () => {
    vi.mocked(factTablesApi.list).mockResolvedValue(
      { total: 0, items: [] } as unknown as Awaited<ReturnType<typeof factTablesApi.list>>,
    )
    EVENT_CATALOG = []
    renderForm(null, DATA_SOURCES, { pickSql: false })

    const kinds = within(screen.getByRole('radiogroup', { name: 'Metric kind' })).getAllByRole('radio')
    expect(kinds.map(kind => kind.textContent)).toEqual([
      expect.stringContaining('From tracked events'),
      expect.stringContaining('From a fact table'),
      expect.stringContaining('Custom SQL'),
    ])
    expect(await screen.findByText(/No events are tracked in this project yet/)).toBeInTheDocument()
    expect(await screen.findByText(/This project has no fact tables yet/)).toBeInTheDocument()
    // Faded, not disabled: the author may still pick them and fill them in later.
    const [events, facts, sql] = within(screen.getByRole('radiogroup', { name: 'Metric kind' })).getAllByRole('radio')
    for (const kind of [events, facts]) {
      expect(kind).toHaveAttribute('data-dimmed', 'true')
      expect(kind).toBeEnabled()
    }
    expect(sql).not.toHaveAttribute('data-dimmed')
  })

  it('never shows the gallery in edit mode', () => {
    renderForm(TEMPLATE_EDIT_METRIC)

    expect(screen.queryByText('Start from a template')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Start from scratch' })).toBeNull()
    // The form renders directly.
    expect(screen.getByRole('heading', { name: 'Edit · Order count' })).toBeInTheDocument()
  })
})

describe('MetricForm starter SQL follows the selected warehouse', () => {
  // One source per engine: the starter query is dialect-specific, so which one is
  // selected decides which SQL can even run.
  const MULTI_DB_SOURCES = [
    { id: 'ds-ch', name: 'ClickHouse', db_type: 'clickhouse' },
    { id: 'ds-pg', name: 'Postgres', db_type: 'postgres' },
    { id: 'ds-bq', name: 'BigQuery', db_type: 'bigquery' },
  ] as unknown as DataSource[]

  const sqlEditor = () => screen.getByLabelText('Metric SQL') as HTMLTextAreaElement
  const pickSource = (id: string) =>
    fireEvent.change(document.getElementById('metric-sql-data-source')!, { target: { value: id } })
  const pickDauTemplate = () =>
    fireEvent.click(screen.getByRole('button', { name: /Daily active users/ }))

  it('renders the template for the source selected AFTER the template was picked', () => {
    renderForm(null, MULTI_DB_SOURCES)
    pickDauTemplate()

    pickSource('ds-bq')

    // GoogleSQL has no date_trunc('day', ts) — the form must not hand the user a
    // starter query that cannot run on the warehouse they just chose.
    expect(sqlEditor().value).toContain("TIMESTAMP_TRUNC(created_at, DAY, 'UTC')")
    expect(sqlEditor().value).not.toMatch(/date_trunc/i)
  })

  it('re-renders the template when the data source changes engine', () => {
    renderForm(null, MULTI_DB_SOURCES)
    pickDauTemplate()

    pickSource('ds-pg')
    expect(sqlEditor().value).toContain('date_bin(')

    pickSource('ds-ch')
    expect(sqlEditor().value).toContain('toStartOfInterval(')
    expect(sqlEditor().value).not.toContain('date_bin(')

    pickSource('ds-bq')
    expect(sqlEditor().value).toContain('TIMESTAMP_TRUNC(')
    expect(sqlEditor().value).not.toContain('toStartOfInterval(')
  })

  it('NEVER clobbers SQL the user has edited', () => {
    renderForm(null, MULTI_DB_SOURCES)
    pickDauTemplate()
    pickSource('ds-pg')

    const edited = 'SELECT bucket, count(*) AS value FROM my_own_table GROUP BY 1'
    fireEvent.change(sqlEditor(), { target: { value: edited } })

    // Switching the warehouse must leave the user's own query completely alone,
    // even though it is now (probably) wrong for the new engine — silently
    // rewriting someone's SQL is far worse than letting preview flag it.
    pickSource('ds-bq')
    expect(sqlEditor().value).toBe(edited)

    pickSource('ds-ch')
    expect(sqlEditor().value).toBe(edited)
  })

  it('leaves hand-written SQL alone when no template was ever picked', () => {
    renderForm(null, MULTI_DB_SOURCES)
    fireEvent.click(screen.getByRole('button', { name: 'Start from scratch' }))

    const handWritten = "SELECT date_trunc('day', ts) AS bucket, 1 AS value FROM t"
    fireEvent.change(sqlEditor(), { target: { value: handWritten } })

    pickSource('ds-bq')
    expect(sqlEditor().value).toBe(handWritten)
  })

  it('keeps re-rendering after the user switches back and forth without editing', () => {
    renderForm(null, MULTI_DB_SOURCES)
    pickDauTemplate()

    pickSource('ds-bq')
    pickSource('ds-pg')
    pickSource('ds-bq')

    // Still pristine template output, so still valid for the current engine.
    expect(sqlEditor().value).toContain("TIMESTAMP_TRUNC(created_at, DAY, 'UTC')")
  })

  it('renders the hourly event-volume template for the selected engine', () => {
    renderForm(null, MULTI_DB_SOURCES)
    fireEvent.click(screen.getByRole('button', { name: /Event volume/ }))

    pickSource('ds-bq')
    expect(sqlEditor().value).toContain("TIMESTAMP_TRUNC(created_at, HOUR, 'UTC')")
  })
})

describe('MetricForm field labels', () => {
  // The same label bug reached this form too: the settings kit's Field generates an id
  // and points its <label htmlFor> at it, but only the kit's own controls claim
  // that id. A row wrapping anything else — the read-only name, the checkbox
  // grid, the filter editor — was left with a label addressing an element that
  // did not exist, so clicking it focused nothing.
  const danglingLabels = (): string[] =>
    Array.from(document.querySelectorAll<HTMLLabelElement>('label[for]'))
      .filter(label => document.getElementById(label.htmlFor) === null)
      .map(label => label.textContent ?? '')

  it('gives every field on the SQL create form a label that resolves', () => {
    renderForm()
    showDimensions()

    expect(danglingLabels()).toEqual([])
    for (const field of [
      'Display name',
      'Internal name',
      'Description',
      'Unit',
      'Color',
      'Data source',
      'Collection interval',
      'Time column',
      'Value column',
      'App version column',
      'Platform column',
    ]) {
      expect(screen.getByLabelText(field)).toBeInTheDocument()
    }
  })

  it('names the read-only internal name row as a group instead of dangling its label', () => {
    renderForm(EDIT_METRIC)

    expect(danglingLabels()).toEqual([])
    expect(screen.getByRole('group', { name: 'Internal name' })).toBeInTheDocument()
  })

  // The filter row holds a list and two buttons, the breakdown row a grid of
  // individually-labelled checkboxes: neither has one control a <label> can
  // name, and on a fresh fact metric the filter list is empty, so the id the
  // row's first control would otherwise claim was on nothing at all.
  it('names the filter and breakdown rows as groups on a fact metric', async () => {
    renderForm()
    fireEvent.click(screen.getByRole('radio', { name: /From a fact table/ }))
    await waitFor(() =>
      expect(document.querySelector('#metric-fact-table option[value="ft-1"]')).not.toBeNull(),
    )

    expect(danglingLabels()).toEqual([])
    expect(screen.getByRole('group', { name: 'Filters' })).toBeInTheDocument()
    showDimensions()
    expect(screen.getByRole('group', { name: 'Breakdown columns' })).toBeInTheDocument()
  })
})

/** Whether a reload/tab-close right now would get the browser's prompt. */
function reloadIsGuarded(): boolean {
  const event = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(event)
  return event.defaultPrevented
}

describe('MetricForm unsaved-changes guard', () => {
  it('arms the reload prompt only once something was typed', () => {
    renderForm(null, DATA_SOURCES, { pickSql: false })
    expect(reloadIsGuarded()).toBe(false)

    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Orders' },
    })
    expect(reloadIsGuarded()).toBe(true)
  })
})

const VIEWER = personaAuth('viewer')

describe('MetricForm for a viewer', () => {
  it('shows the SQL read-only and never asks for the editor-only schema', () => {
    render(
      createElement(
        AuthContext.Provider,
        { value: VIEWER },
        createElement(SessionProject, { session: VIEWER }, createElement(MetricForm, {
          slug: 'demo',
          metric: EDIT_METRIC,
          dataSources: DATA_SOURCES,
          onClose: vi.fn(),
        })),
      ),
      { wrapper: formWrapper },
    )

    expect(screen.getByLabelText('Metric SQL')).toHaveAttribute('readonly')
    expect(useDataSourceSchemaMock).toHaveBeenCalled()
    expect(useDataSourceSchemaMock.mock.calls.every(([dsId]) => dsId === undefined)).toBe(true)
  })

  it('asks an editor for the schema of the chosen source', () => {
    renderForm(EDIT_METRIC)

    expect(screen.getByLabelText('Metric SQL')).not.toHaveAttribute('readonly')
    expect(useDataSourceSchemaMock).toHaveBeenCalledWith('ds-1')
  })
})

const EVENT_METRIC = {
  ...EDIT_METRIC,
  kind: 'event_composition',
  name: 'late_signups',
  display_name: 'Late signups',
  data_source_id: null,
  interval: null,
  replay_chunk_interval: null,
  aggregation: null,
  composition: 'single',
  config: {},
} as unknown as MetricDefinitionDetailResponse

describe('MetricForm history-loss confirm', () => {
  it('asks before saving any change of meaning and names the history loss', async () => {
    renderForm(EDIT_METRIC)

    fireEvent.change(screen.getByLabelText('Metric SQL'), {
      target: { value: 'SELECT bucket, count(*) AS value FROM events WHERE ok GROUP BY 1' },
    })
    submit()

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/deletes its collected values, breakdowns and anomalies/)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(metricsCatalogApi.update).not.toHaveBeenCalled()

    submit()
    fireEvent.click(await screen.findByRole('button', { name: 'Save and delete history' }))
    await waitFor(() => expect(metricsCatalogApi.update).toHaveBeenCalledTimes(1))
  })

  it('asks for an interval change too, not only a kind change', async () => {
    renderForm(EDIT_METRIC)
    fireEvent.change(document.getElementById('metric-sql-interval')!, { target: { value: '1d' } })
    submit()
    expect(await screen.findByRole('alertdialog')).toBeInTheDocument()
  })

  it('saves presentation-only edits without asking', async () => {
    renderForm(EDIT_METRIC)
    fireEvent.change(document.getElementById('metric-unit')!, { target: { value: 'ms' } })
    expect(screen.queryByText(/Saving deletes its collected values/)).toBeNull()
    submit()
    await waitFor(() => expect(metricsCatalogApi.update).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  const factMetric = (config: Record<string, unknown>) =>
    ({
      ...EDIT_METRIC,
      kind: 'fact',
      data_source_id: null,
      fact_table_id: 'ft-1',
      aggregation: 'count',
      composition: 'single',
      replay_chunk_interval: null,
      config,
    }) as unknown as MetricDefinitionDetailResponse

  it('sends an untouched fact definition back exactly as stored, without asking', async () => {
    const config = {
      filter_sql: '(amount > 0) and (user_id is not null)',
      conditions: [
        { column: 'user_id', operator: 'in', value: 'u-1' },
        { column: 'amount', operator: 'gt', value: '3' },
      ],
    }
    renderForm(factMetric(config))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save metric' })).toBeEnabled())
    expect(screen.queryByText(/Saving deletes its collected values/)).toBeNull()

    fireEvent.change(document.getElementById('metric-unit')!, { target: { value: 'ms' } })
    submit()
    await waitFor(() => expect(metricsCatalogApi.update).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    // Rewriting either (`AND`, `['u-1']`, `3`) would have deleted the history.
    expect(at(vi.mocked(metricsCatalogApi.update).mock.calls, 0)[2].definition).toMatchObject({
      filter_sql: config.filter_sql,
      conditions: config.conditions,
    })
  })

  it('warns from the start when the stored definition cannot be sent back unchanged', async () => {
    renderForm(factMetric({ conditions: [{ column: 'amount', operator: 'between', value: [1, 2] }] }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save metric' })).toBeEnabled())
    expect(screen.getByText(/Saving deletes its collected values/)).toBeInTheDocument()
    submit()
    expect(await screen.findByRole('alertdialog')).toBeInTheDocument()
    expect(metricsCatalogApi.update).not.toHaveBeenCalled()
  })
})

describe('MetricForm event picker', () => {
  const bigCatalog = Array.from({ length: 250 }, (_, i) => ({
    id: `ev-${i}`,
    name: `event_${String(i).padStart(3, '0')}`,
  })) as unknown as EventListItem[]

  it('shows an event beyond the first page and searches the server for more', async () => {
    EVENT_CATALOG = bigCatalog
    renderForm({ ...EVENT_METRIC, numerator_event_id: 'ev-240' } as MetricDefinitionDetailResponse)

    // The stored event is resolved by id and selected, not painted as unset.
    const input = document.getElementById('metric-numerator') as HTMLInputElement
    await waitFor(() => expect(input.value).toBe('event_240'))
    expect(eventsApi.get).toHaveBeenCalledWith('demo', 'ev-240')

    // The capped roster says so as the last row of the open list.
    act(() => input.focus())
    const listbox = await eventListbox(input)
    expect(
      await within(listbox).findByRole('option', { name: '150 more — keep typing' }),
    ).toHaveAttribute('aria-disabled', 'true')

    fireEvent.change(input, { target: { value: 'event_24' } })
    await waitFor(() =>
      expect(eventsApi.list).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({ search: 'event_24', limit: 100 }),
      ),
    )
    await pickEvent('metric-numerator', 'event_245')
    submit()
    fireEvent.click(await screen.findByRole('button', { name: 'Save and delete history' }))
    await waitFor(() => expect(metricsCatalogApi.update).toHaveBeenCalledTimes(1))
    expect(at(vi.mocked(metricsCatalogApi.update).mock.calls, 0)[2].definition).toMatchObject({
      numerator_event_id: 'ev-245',
      numerator_event_type_id: null,
    })
  })

  it('shows an event-type reference and lets it be changed to an event', async () => {
    renderForm({ ...EVENT_METRIC, numerator_event_type_id: 'event-type-1' } as MetricDefinitionDetailResponse)

    const input = document.getElementById('metric-numerator') as HTMLInputElement
    await waitFor(() => expect(input.value).toBe('Every Signup event'))

    await pickEvent('metric-numerator', 'checkout:start')
    expect(input.value).toBe('checkout:start')
    fireEvent.click(screen.getByRole('button', { name: 'Clear events' }))
    expect(input.value).toBe('')
    submit()
    // Clearing the pick is now possible, so validation catches it.
    expect((await screen.findAllByText('An event is required.')).length).toBeGreaterThan(0)
  })
})

describe('MetricForm filter validation', () => {
  it('blocks save on an incomplete filter instead of dropping it', async () => {
    renderForm()
    fireEvent.click(screen.getByRole('radio', { name: /From a fact table/ }))
    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Orders' },
    })
    await pickOption('metric-fact-table', 'ft-1')
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Create and start collecting' })).toBeEnabled(),
    )

    fireEvent.click(within(await openAddFilterMenu()).getByRole('menuitem', { name: 'Condition' }))
    fireEvent.change(screen.getByLabelText('Filter 1 condition column'), {
      target: { value: 'user_id' },
    })
    submit()

    const valueError = 'Filter 1: Enter a value for this condition.'
    expect((await screen.findAllByText(valueError)).length).toBe(2)
    expect(metricsCatalogApi.create).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Filter 1 condition column')).toHaveAttribute('aria-invalid', 'true')

    // Fixing it clears the message without another submit.
    fireEvent.change(screen.getByLabelText('Filter 1 condition value'), { target: { value: 'u1' } })
    expect(screen.queryByText(valueError)).toBeNull()
  })

  it('keeps Check filters disabled until the operand is complete', async () => {
    renderForm()
    fireEvent.click(screen.getByRole('radio', { name: /From a fact table/ }))
    await pickOption('metric-fact-table', 'ft-1')
    fireEvent.change(document.getElementById('metric-fact-aggregation')!, { target: { value: 'sum' } })

    const check = await screen.findByRole('button', { name: /Check filters/i })
    await waitFor(() => expect(check).toHaveAccessibleDescription(/measure column is required/))
    expect(check).toBeDisabled()
    await pickOption('metric-fact-measure', 'amount')
    expect(check).toBeEnabled()
  })
})

describe('MetricForm SQL preview', () => {
  it('never paints a result for SQL that was edited while it ran', async () => {
    let resolve!: (value: Awaited<ReturnType<typeof metricsCatalogApi.preview>>) => void
    vi.mocked(metricsCatalogApi.preview).mockReturnValue(
      new Promise(r => {
        resolve = r
      }),
    )
    renderForm()
    fireEvent.change(document.getElementById('metric-sql-data-source')!, { target: { value: 'ds-1' } })
    fireEvent.change(screen.getByLabelText('Metric SQL'), { target: { value: 'SELECT 1 FROM events' } })
    fireEvent.change(document.getElementById('metric-sql-time')!, { target: { value: 'bucket' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))

    fireEvent.change(screen.getByLabelText('Metric SQL'), { target: { value: 'SELECT 2 FROM events' } })
    await act(async () => {
      resolve({ columns: ['bucket', 'value'], points: [], point_count: 7, truncated: false, error: null })
    })
    expect(screen.queryByText(/7 buckets/)).toBeNull()
  })

  it('clears the preview when the interval changes', async () => {
    vi.mocked(metricsCatalogApi.preview).mockResolvedValue({
      columns: ['bucket', 'value'],
      points: [],
      point_count: 0,
      truncated: false,
      error: null,
    })
    renderForm()
    fireEvent.change(document.getElementById('metric-sql-data-source')!, { target: { value: 'ds-1' } })
    fireEvent.change(screen.getByLabelText('Metric SQL'), { target: { value: 'SELECT 1 FROM events' } })
    fireEvent.change(document.getElementById('metric-sql-time')!, { target: { value: 'bucket' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))

    // An empty result says what it most likely means.
    expect(await screen.findByText(/returned no rows in the preview window/)).toBeInTheDocument()
    fireEvent.change(document.getElementById('metric-sql-interval')!, { target: { value: '1d' } })
    expect(screen.queryByText(/returned no rows/)).toBeNull()
  })

  it('offers the columns the preview returned as breakdowns', async () => {
    vi.mocked(metricsCatalogApi.preview).mockResolvedValue({
      columns: ['bucket', 'value', 'region'],
      points: [{ bucket: '2026-07-01T00:00:00Z', value: 5 }],
      point_count: 1,
      truncated: false,
      error: null,
    })
    renderForm()
    fireEvent.change(document.getElementById('metric-sql-data-source')!, { target: { value: 'ds-1' } })
    fireEvent.change(screen.getByLabelText('Metric SQL'), { target: { value: 'SELECT 1 FROM events' } })
    fireEvent.change(document.getElementById('metric-sql-time')!, { target: { value: 'bucket' } })
    // Before a preview: only the named table's columns.
    showDimensions()
    expect(screen.getByRole('checkbox', { name: 'Break down by country' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    expect(await screen.findByText(/Only one bucket came back/)).toBeInTheDocument()
    expect(screen.getByText('min 5 · max 5 · last 5')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Break down by region' })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'Break down by country' })).toBeNull()

    // Editing the SQL invalidates the preview and the columns it returned.
    fireEvent.change(screen.getByLabelText('Metric SQL'), {
      target: { value: 'SELECT 2 FROM events' },
    })
    expect(screen.queryByRole('checkbox', { name: 'Break down by region' })).toBeNull()
    expect(screen.getByRole('checkbox', { name: 'Break down by country' })).toBeInTheDocument()
  })
})

describe('MetricForm replay chunk', () => {
  it('drops a stored replay chunk finer than a new interval, and says so', async () => {
    renderForm({ ...EDIT_METRIC, replay_chunk_interval: '1d' } as MetricDefinitionDetailResponse)
    expect(screen.getByText(/Backfills replay in daily chunks/)).toBeInTheDocument()

    fireEvent.change(document.getElementById('metric-sql-interval')!, { target: { value: '1w' } })
    expect(screen.getByText(/will be cleared on save/)).toBeInTheDocument()

    submit()
    fireEvent.click(await screen.findByRole('button', { name: 'Save and delete history' }))
    await waitFor(() => expect(metricsCatalogApi.update).toHaveBeenCalledTimes(1))
    expect(at(vi.mocked(metricsCatalogApi.update).mock.calls, 0)[2].definition).toMatchObject({
      interval: '1w',
      replay_chunk_interval: null,
    })
  })
})

describe('MetricForm replay chunk restored', () => {
  it('brings the stored chunk back when the interval returns below it', async () => {
    renderForm({ ...EDIT_METRIC, replay_chunk_interval: '1d' } as MetricDefinitionDetailResponse)
    const interval = document.getElementById('metric-sql-interval')!

    fireEvent.change(interval, { target: { value: '1w' } })
    expect(screen.getByText(/will be cleared on save/)).toBeInTheDocument()
    fireEvent.change(interval, { target: { value: '1h' } })
    expect(screen.queryByText(/will be cleared on save/)).toBeNull()
    expect(screen.getByText(/Backfills replay in daily chunks/)).toBeInTheDocument()

    // Nothing of meaning changed, so nothing asks and the chunk is re-sent.
    submit()
    await waitFor(() => expect(metricsCatalogApi.update).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(at(vi.mocked(metricsCatalogApi.update).mock.calls, 0)[2].definition).toMatchObject({
      interval: '1h',
      replay_chunk_interval: '1d',
    })
  })
})

describe('MetricForm validation accessibility', () => {
  it('links each invalid field to its message and focuses fields from the summary', async () => {
    renderForm()
    submit()

    const display = screen.getByLabelText('Display name', { exact: false })
    await waitFor(() => expect(display).toHaveAttribute('aria-invalid', 'true'))
    expect(display).toHaveAccessibleDescription('Display name is required.')
    // Focus lands on the first invalid field: the definition comes before the
    // name card now, so that is the data source.
    expect(document.getElementById('metric-sql-data-source')).toHaveFocus()

    const summary = screen.getByRole('alert')
    fireEvent.click(within(summary).getByRole('button', { name: 'The metric SQL query is required.' }))
    // The SQL editor's id sits on a wrapper; focus goes to its editable surface.
    expect(screen.getByLabelText('Metric SQL')).toHaveFocus()
  })

  it('has no axe violations with errors on screen, event pickers included', async () => {
    renderForm()
    fireEvent.click(screen.getByRole('radio', { name: /From tracked events/ }))
    fireEvent.change(document.getElementById('metric-composition')!, { target: { value: 'ratio' } })
    submit()
    await screen.findAllByText('A denominator event is required for a ratio metric.')
    // Wait for both pickers' rosters, so the axe pass sees them settled.
    await waitFor(() => expect(eventsApi.list).toHaveBeenCalled())
    await act(async () => {})
    await expectNoAxeViolations(document.body)
  })

  it('drops an error once its field is fixed or no longer rendered', async () => {
    renderForm()
    fireEvent.click(screen.getByRole('radio', { name: /From tracked events/ }))
    fireEvent.change(document.getElementById('metric-composition')!, { target: { value: 'ratio' } })
    submit()
    expect((await screen.findAllByText('A denominator event is required for a ratio metric.')).length)
      .toBeGreaterThan(0)

    fireEvent.change(document.getElementById('metric-composition')!, { target: { value: 'single' } })
    expect(screen.queryByText('A denominator event is required for a ratio metric.')).toBeNull()
  })
})

describe('MetricForm kind switch', () => {
  it('does not carry dimension columns across kinds', async () => {
    renderForm({
      ...EDIT_METRIC,
      breakdown_columns: ['platform'],
      platform_column: 'platform',
    } as unknown as MetricDefinitionDetailResponse)

    fireEvent.click(screen.getByRole('radio', { name: /From a fact table/ }))
    await pickOption('metric-fact-table', 'ft-1')
    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: 'Break down by amount' })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('checkbox', { name: 'Break down by platform' })).toBeNull()
    expect(document.getElementById('metric-platform')).toHaveValue('')

    // Back to the saved kind restores what was saved for it.
    fireEvent.click(screen.getByRole('radio', { name: /Custom SQL/ }))
    expect(screen.getByRole('checkbox', { name: 'Break down by platform' })).toBeChecked()
  })

  it('gives a kind back the dimensions it had earlier in the session', async () => {
    renderForm(EDIT_METRIC)
    showDimensions()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Break down by country' }))

    fireEvent.click(screen.getByRole('radio', { name: /From a fact table/ }))
    await pickOption('metric-fact-table', 'ft-1')
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Break down by amount' }))

    fireEvent.click(screen.getByRole('radio', { name: /Custom SQL/ }))
    expect(screen.getByRole('checkbox', { name: 'Break down by country' })).toBeChecked()
    fireEvent.click(screen.getByRole('radio', { name: /From a fact table/ }))
    expect(await screen.findByRole('checkbox', { name: 'Break down by amount' })).toBeChecked()
  })
})

describe('MetricForm internal name', () => {
  it('derives a Latin identifier from a Cyrillic display name', () => {
    renderForm()
    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Конверсия оплаты' },
    })
    expect(screen.getByLabelText('Internal name', { exact: false })).toHaveValue('konversiya_oplaty')
  })
})

describe('MetricForm after a save', () => {
  it('refreshes the drilldown caches of a redefined metric and reports the save', async () => {
    const onSaved = vi.fn()
    render(
      createElement(MetricForm, {
        slug: 'demo',
        metric: EDIT_METRIC,
        dataSources: DATA_SOURCES,
        onClose: vi.fn(),
        onSaved,
      }),
      { wrapper },
    )
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    fireEvent.change(document.getElementById('metric-sql-value')!, { target: { value: 'total' } })
    submit()
    fireEvent.click(await screen.findByRole('button', { name: 'Save and delete history' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('updated', false))
    const keys = invalidate.mock.calls.map(([filters]) => filters?.queryKey)
    expect(keys).toContainEqual(['monitoringMetrics', 'demo', 'metric', 'metric-1'])
    expect(keys).toContainEqual(['eventMetricBreakdowns', 'demo', 'metric', 'metric-1'])
    expect(keys).toContainEqual(['appVersionSeries', 'demo', 'metric', 'metric-1'])
  })
})

describe('MetricEditPage', () => {
  function BackButton() {
    const navigate = useNavigate()
    return createElement('button', { type: 'button', onClick: () => navigate(-1) }, 'Go back')
  }

  function renderPage(path: string, history: string[] = [], auth = authAs('member')) {
    render(
      createElement(
        AuthContext.Provider,
        { value: auth },
        createElement(SessionProject, { session: auth }, createElement(
          MemoryRouter,
          { initialEntries: [...history, path], initialIndex: history.length },
          createElement(
            Routes,
            null,
            createElement(Route, { path: '/p/:slug/metrics', element: createElement('p', null, 'catalog') }),
            createElement(Route, { path: '/p/:slug/metrics/new', element: createElement(MetricEditPage) }),
            createElement(Route, {
              path: '/p/:slug/metrics/:metricId/edit',
              element: createElement(MetricEditPage),
            }),
            createElement(Route, {
              path: '/p/:slug/monitoring/metric/:id',
              element: createElement('div', null, createElement('p', null, 'drilldown'), createElement(BackButton)),
            }),
          ),
        )),
      ),
      { wrapper },
    )
  }

  it('sends a viewer to the metric’s read view instead of a disabled form (#237)', async () => {
    vi.mocked(dataSourcesApi.list).mockResolvedValue(DATA_SOURCES)
    vi.mocked(metricsCatalogApi.get).mockResolvedValue(EDIT_METRIC)
    renderPage('/p/demo/metrics/metric-1/edit', [], personaAuth('viewer'))

    expect(await screen.findByText('drilldown')).toBeInTheDocument()
    expect(screen.queryByRole('group')).toBeNull()
  })

  it('sends a viewer away from "New metric" to the catalog', async () => {
    renderPage('/p/demo/metrics/new', [], personaAuth('viewer'))

    expect(await screen.findByText('catalog')).toBeInTheDocument()
  })

  it('says a missing metric is not found, with the way back and no retry (#237)', async () => {
    vi.mocked(dataSourcesApi.list).mockResolvedValue(DATA_SOURCES)
    vi.mocked(metricsCatalogApi.get).mockRejectedValue(new ApiError('Not found', 404))
    renderPage('/p/demo/metrics/gone/edit')

    expect(await screen.findByRole('heading', { name: 'Metric not found' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to metrics' })).toHaveAttribute('href', '/p/demo/metrics')
    expect(screen.queryByRole('button', { name: /try again|retry/i })).toBeNull()
    vi.mocked(metricsCatalogApi.get).mockReset()
  })

  it('opens the editor when data sources fail, and shows the failure in the SQL card', async () => {
    vi.mocked(dataSourcesApi.list).mockRejectedValue(new Error('warehouse list down'))
    renderPage('/p/demo/metrics/new')

    fireEvent.click(await screen.findByRole('radio', { name: /Custom SQL/ }))
    expect(await screen.findByText('Could not load data sources')).toBeInTheDocument()
    // A fact or event metric can still be written.
    fireEvent.click(screen.getByRole('radio', { name: /From tracked events/ }))
    expect(screen.queryByText('Could not load data sources')).toBeNull()
  })

  it('lands on the new metric’s drilldown after create', async () => {
    vi.mocked(dataSourcesApi.list).mockResolvedValue(DATA_SOURCES)
    renderPage('/p/demo/metrics/new')

    fireEvent.click(await screen.findByRole('radio', { name: /From tracked events/ }))
    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Checkouts' },
    })
    await pickEvent('metric-numerator', 'checkout:start')
    submit()

    expect(await screen.findByText('drilldown')).toBeInTheDocument()
  })

  it('replaces the create form in history, so Back skips the empty form', async () => {
    vi.mocked(dataSourcesApi.list).mockResolvedValue(DATA_SOURCES)
    renderPage('/p/demo/metrics/new', ['/p/demo/metrics'])

    fireEvent.click(await screen.findByRole('radio', { name: /From tracked events/ }))
    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Checkouts' },
    })
    await pickEvent('metric-numerator', 'checkout:start')
    submit()

    fireEvent.click(await screen.findByRole('button', { name: 'Go back' }))
    expect(await screen.findByText('catalog')).toBeInTheDocument()
  })
})

describe('MetricEditPage header link', () => {
  it('names the catalog it leads to, even when opened from the drilldown', async () => {
    vi.mocked(dataSourcesApi.list).mockResolvedValue(DATA_SOURCES)
    vi.mocked(metricsCatalogApi.get).mockResolvedValue(EDIT_METRIC)
    render(
      createElement(
        AuthContext.Provider,
        { value: authAs('member') },
        createElement(SessionProject, { session: authAs('member') }, createElement(
          MemoryRouter,
          { initialEntries: ['/p/demo/monitoring/metric/metric-1', '/p/demo/metrics/metric-1/edit'], initialIndex: 1 },
          createElement(
            Routes,
            null,
            createElement(Route, { path: '/p/:slug/metrics', element: createElement('p', null, 'catalog') }),
            createElement(Route, { path: '/p/:slug/metrics/:metricId/edit', element: createElement(MetricEditPage) }),
            createElement(Route, {
              path: '/p/:slug/monitoring/metric/:id',
              element: createElement('p', null, 'drilldown'),
            }),
          ),
        )),
      ),
      { wrapper },
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Metrics' }))
    expect(await screen.findByText('catalog')).toBeInTheDocument()
    vi.mocked(metricsCatalogApi.get).mockReset()
  })
})

describe('MetricForm owner', () => {
  it('sends the picked owner with a new metric', async () => {
    renderForm(null, DATA_SOURCES, { pickSql: false })
    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Checkouts' },
    })
    await pickEvent('metric-numerator', 'checkout:start')
    await pickOption('metric-owner', 'user-1')
    submit()

    await waitFor(() => expect(metricsCatalogApi.create).toHaveBeenCalledTimes(1))
    expect(at(vi.mocked(metricsCatalogApi.create).mock.calls, 0)[1]).toMatchObject({
      owner_id: 'user-1',
    })
  })

  it('keeps a stored owner on save, and clears it only when asked', async () => {
    renderForm({ ...EDIT_METRIC, owner_id: 'user-1' } as MetricDefinitionDetailResponse)
    await waitFor(() =>
      expect((document.getElementById('metric-owner') as HTMLSelectElement).value).toBe('user-1'),
    )
    fireEvent.change(document.getElementById('metric-owner')!, { target: { value: '' } })
    submit()

    await waitFor(() => expect(metricsCatalogApi.update).toHaveBeenCalledTimes(1))
    expect(at(vi.mocked(metricsCatalogApi.update).mock.calls, 0)[2]).toMatchObject({ owner_id: null })
  })
})

describe('MetricForm create status', () => {
  async function fillEventMetric() {
    fireEvent.click(screen.getByRole('radio', { name: /From tracked events/ }))
    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Checkouts' },
    })
    await pickEvent('metric-numerator', 'checkout:start')
  }

  it('creates an active metric from the primary button, with no Status select', async () => {
    renderForm()
    expect(document.getElementById('metric-status')).toBeNull()

    await fillEventMetric()
    fireEvent.click(screen.getByRole('button', { name: 'Create and start collecting' }))

    await waitFor(() => expect(metricsCatalogApi.create).toHaveBeenCalledTimes(1))
    expect(metricsCatalogApi.create).toHaveBeenCalledWith(
      'demo',
      expect.objectContaining({ status: 'active' }),
    )
  })

  it('parks the metric as a draft from "Save as draft"', async () => {
    renderForm()
    await fillEventMetric()
    fireEvent.click(screen.getByRole('button', { name: 'Save as draft' }))

    await waitFor(() => expect(metricsCatalogApi.create).toHaveBeenCalledTimes(1))
    expect(metricsCatalogApi.create).toHaveBeenCalledWith(
      'demo',
      expect.objectContaining({ status: 'draft' }),
    )
  })

  it('keeps Status on edit and says only active metrics are collected', () => {
    renderForm(EDIT_METRIC)

    expect(screen.getByLabelText('Status')).toBeInTheDocument()
    expect(
      screen.getByText(/Only active metrics are collected on schedule and monitored/),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save as draft' })).toBeNull()
  })
})

describe('MetricForm unit, colour and template', () => {
  it('fills in % when an event metric becomes a ratio with no unit', () => {
    renderForm()
    fireEvent.click(screen.getByRole('radio', { name: /From tracked events/ }))
    fireEvent.change(document.getElementById('metric-composition')!, { target: { value: 'ratio' } })

    expect(document.getElementById('metric-unit')).toHaveValue('%')
    expect(screen.getByText(/Set to % for a ratio/)).toBeInTheDocument()
  })

  it('keeps a unit the author already set when the metric becomes a ratio', () => {
    renderForm()
    fireEvent.click(screen.getByRole('radio', { name: /From tracked events/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Use unit users' }))
    fireEvent.change(document.getElementById('metric-composition')!, { target: { value: 'ratio' } })

    expect(document.getElementById('metric-unit')).toHaveValue('users')
  })

  it('picks a colour from the swatches', () => {
    renderForm()
    // Colour sits in the "More options" fold.
    fireEvent.click(screen.getByRole('button', { name: /More options/ }))
    const sky = screen.getByRole('button', { name: 'Sky' })
    expect(sky).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(sky)
    expect(sky).toHaveAttribute('aria-pressed', 'true')
    expect(document.getElementById('metric-color')).toHaveValue('#0ea5e9')
  })

  it('names the template that seeded the form and can reopen the gallery', () => {
    renderForm()
    fireEvent.click(screen.getByRole('button', { name: /Conversion A→B/ }))

    expect(screen.getByText(/Started from/)).toHaveTextContent('Started from Conversion A→B')
    fireEvent.click(screen.getByRole('button', { name: 'Change template' }))
    expect(screen.getByText('Start from a template')).toBeInTheDocument()
  })
})

describe('MetricForm SQL preview guard', () => {
  it('says what Preview still needs', () => {
    renderForm()
    const preview = screen.getByRole('button', { name: 'Preview' })
    expect(preview).toBeDisabled()
    expect(preview).toHaveAccessibleDescription('Pick a data source to preview.')
  })

  it('asks before creating a metric whose last preview failed', async () => {
    vi.mocked(metricsCatalogApi.preview).mockResolvedValue({
      columns: [],
      points: [],
      point_count: 0,
      truncated: false,
      error: 'Unknown column bucket',
    })
    renderForm()
    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Order count' },
    })
    fireEvent.change(document.getElementById('metric-sql-data-source')!, { target: { value: 'ds-1' } })
    fireEvent.change(screen.getByLabelText('Metric SQL'), {
      target: { value: 'SELECT bucket, count(*) AS value FROM events GROUP BY 1' },
    })
    fireEvent.change(document.getElementById('metric-sql-time')!, { target: { value: 'bucket' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    expect(await screen.findByText('Unknown column bucket')).toBeInTheDocument()

    submit()
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/hasn't previewed successfully/)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create anyway' }))
    await waitFor(() => expect(metricsCatalogApi.create).toHaveBeenCalledTimes(1))
  })

  it('asks before creating a metric that was never previewed, and Cancel keeps the form', async () => {
    renderForm()
    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Order count' },
    })
    fireEvent.change(document.getElementById('metric-sql-data-source')!, { target: { value: 'ds-1' } })
    fireEvent.change(screen.getByLabelText('Metric SQL'), { target: { value: 'SELECT 1 FROM events' } })
    fireEvent.change(document.getElementById('metric-sql-time')!, { target: { value: 'bucket' } })

    submit()
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Create a metric that hasn’t previewed?')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(metricsCatalogApi.create).not.toHaveBeenCalled()
  })

  it('creates without asking once the current query has previewed cleanly', async () => {
    vi.mocked(metricsCatalogApi.preview).mockResolvedValue({
      columns: ['bucket', 'value'],
      points: [{ bucket: '2026-07-01T00:00:00Z', value: 5 }],
      point_count: 1,
      truncated: false,
      error: null,
    })
    renderForm()
    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Order count' },
    })
    fireEvent.change(document.getElementById('metric-sql-data-source')!, { target: { value: 'ds-1' } })
    fireEvent.change(screen.getByLabelText('Metric SQL'), { target: { value: 'SELECT 1 FROM events' } })
    fireEvent.change(document.getElementById('metric-sql-time')!, { target: { value: 'bucket' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    expect(await screen.findByText(/Only one bucket came back/)).toBeInTheDocument()

    submit()
    await waitFor(() => expect(metricsCatalogApi.create).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })
})

describe('MetricForm follow-ups', () => {
  it('folds the internal name, description and colour behind More options', async () => {
    renderForm(null, DATA_SOURCES, { pickSql: false })
    const more = screen.getByRole('button', { name: /More options/ })
    expect(more).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('textbox', { name: 'Description' })).toBeNull()

    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Checkout conversion' },
    })
    // The derived internal name stays readable while its input is folded.
    expect(screen.getByText('checkout_conversion')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Change' }))
    expect(more).toHaveAttribute('aria-expanded', 'true')
    await waitFor(() => expect(document.getElementById('metric-name')).toHaveFocus())
    expect(screen.getByRole('textbox', { name: 'Description' })).toBeInTheDocument()
  })

  it('opens an edited metric with a description with the fold open', () => {
    renderForm({ ...EDIT_METRIC, description: 'Orders per day' })
    expect(screen.getByRole('button', { name: /More options/ })).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('group', { name: 'Internal name' })).toBeInTheDocument()
  })

  it('starts on a fact table when the project tracks no events', async () => {
    EVENT_CATALOG = []
    renderForm(null, DATA_SOURCES, { pickSql: false })

    await waitFor(() =>
      expect(screen.getByRole('radio', { name: /From a fact table/ })).toHaveAttribute(
        'aria-checked',
        'true',
      ),
    )
    expect(screen.getByRole('link', { name: 'Add events' })).toHaveAttribute('href', '/p/demo/events')
  })

  it('starts on SQL, and links to a new fact table, when there are neither', async () => {
    EVENT_CATALOG = []
    vi.mocked(factTablesApi.list).mockResolvedValue(
      { total: 0, items: [] } as unknown as Awaited<ReturnType<typeof factTablesApi.list>>,
    )
    renderForm(null, DATA_SOURCES, { pickSql: false })

    await waitFor(() =>
      expect(screen.getByRole('radio', { name: /Custom SQL/ })).toHaveAttribute('aria-checked', 'true'),
    )
    expect(screen.getByRole('link', { name: 'Create one' })).toHaveAttribute(
      'href',
      '/p/demo/metrics/fact-tables/new',
    )
  })

  it('keeps the kind the author picked once the roster says there are no events', async () => {
    EVENT_CATALOG = []
    renderForm()
    await screen.findByRole('link', { name: 'Add events' })
    expect(screen.getByRole('radio', { name: /Custom SQL/ })).toHaveAttribute('aria-checked', 'true')
  })

  it('previews an event metric\'s series from the definition a save would send', async () => {
    vi.mocked(previewMetricSeries).mockResolvedValue({
      columns: [],
      points: [
        { bucket: '2026-09-01T10:00:00Z', value: 3 },
        { bucket: '2026-09-01T11:00:00Z', value: 4 },
      ],
      point_count: 2,
      truncated: false,
      error: null,
    })
    renderForm(null, DATA_SOURCES, { pickSql: false })
    const preview = screen.getByRole('button', { name: 'Preview' })
    expect(preview).toBeDisabled()
    expect(preview).toHaveAccessibleDescription('Pick an event to preview.')

    await pickEvent('metric-numerator', 'checkout:start')
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))

    await waitFor(() =>
      expect(previewMetricSeries).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({ kind: 'event_composition', numerator_event_id: 'ev-1' }),
      ),
    )
    expect(await screen.findByText('2 buckets')).toBeInTheDocument()
  })

  it('shows a server-reported preview problem inline', async () => {
    vi.mocked(previewMetricSeries).mockResolvedValue({
      columns: [],
      points: [],
      point_count: 0,
      truncated: false,
      error: 'No counts have been collected for this event yet.',
    })
    renderForm(null, DATA_SOURCES, { pickSql: false })
    await pickEvent('metric-numerator', 'checkout:start')
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))

    expect(await screen.findByText(/No counts have been collected/)).toBeInTheDocument()
  })
})
