import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { eventsApi } from '@/api/events'
import { eventTypesApi } from '@/api/eventTypes'
import { factTablesApi } from '@/api/factTables'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { AuthContext } from '@/components/auth-context'
import type { FactTableListResponse, MetricDefinitionDetailResponse } from '@/types'

import { MetricDefinitionCard } from './metric-definition-card'
import { personaAuth, type Persona } from '@/test/persona'
import { PersonaProject } from '@/test/PersonaProject'

vi.mock('@/api/factTables', () => ({
  factTablesApi: { list: vi.fn(), get: vi.fn() },
}))
vi.mock('@/api/events', () => ({
  eventsApi: { get: vi.fn() },
}))
vi.mock('@/api/eventTypes', () => ({
  eventTypesApi: { list: vi.fn() },
}))
vi.mock('@/api/dataSources', () => ({
  dataSourcesApi: { list: vi.fn() },
}))
vi.mock('@/api/metricsCatalog', () => ({
  metricsCatalogApi: { getGeneratedSql: vi.fn() },
}))
vi.mock('@uiw/react-codemirror', () => ({
  default: ({
    value,
    readOnly,
    'aria-label': ariaLabel,
  }: {
    value: string
    readOnly?: boolean
    'aria-label'?: string
  }) => <textarea aria-label={ariaLabel} value={value} readOnly={readOnly} onChange={() => {}} />,
}))

const FACT_TABLE_ID = '11111111-1111-1111-1111-111111111111'
const OTHER_FACT_TABLE_ID = '22222222-2222-2222-2222-222222222222'

function factDefinition(
  overrides: Partial<MetricDefinitionDetailResponse> & {
    next_collection_at?: string | null
    collection_due?: boolean
  },
): MetricDefinitionDetailResponse {
  return {
    id: 'metric-1',
    project_id: 'project-1',
    name: 'purchases',
    display_name: 'Purchases',
    description: '',
    color: '#6366f1',
    order: 0,
    unit: null,
    status: 'active',
    owner_id: null,
    reviewed: false,
    kind: 'fact',
    aggregation: 'count',
    composition: 'single',
    config: {},
    fact_table_id: FACT_TABLE_ID,
    breakdown_columns: [],
    breakdown_values_limit: null,
    app_version_column: null,
    platform_column: null,
    data_source_id: null,
    interval: '1h',
    replay_chunk_interval: null,
    numerator_event_id: null,
    numerator_event_type_id: null,
    denominator_event_id: null,
    denominator_event_type_id: null,
    anomaly_detection_enabled: true,
    last_collected_at: null,
    last_collection_status: null,
    last_collection_error: null,
    collection_due: false,
    next_collection_at: null,
    created_at: '2026-01-01T00:00:00',
    updated_at: '2026-01-01T00:00:00',
    ...overrides,
  }
}

function renderCard(definition: MetricDefinitionDetailResponse, role: Persona = 'member') {
  const factTables: FactTableListResponse = {
    items: [
      {
        id: FACT_TABLE_ID,
        project_id: 'project-1',
        name: 'orders',
        display_name: 'Orders',
        description: '',
        color: '#6366f1',
        order: 0,
        data_source_id: null,
        timestamp_column: 'created_at',
        metric_count: 0,
        column_count: 0,
        identifier_count: 0,
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
      },
      {
        id: OTHER_FACT_TABLE_ID,
        project_id: 'project-1',
        name: 'sessions',
        display_name: 'Sessions',
        description: '',
        color: '#14b8a6',
        order: 1,
        data_source_id: null,
        timestamp_column: 'created_at',
        metric_count: 0,
        column_count: 0,
        identifier_count: 0,
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
      },
    ],
    total: 2,
  }
  vi.mocked(factTablesApi.list).mockResolvedValue(factTables)
  vi.mocked(factTablesApi.get).mockImplementation(async (_slug, id) => {
    const item = factTables.items.find(table => table.id === id)
    if (!item) throw new Error('Fact table not found')
    return {
      ...item,
      sql: `SELECT * FROM ${item.name}`,
      columns: id === FACT_TABLE_ID
        ? [
            { name: 'days_available', type: 'number' },
            { name: 'platform', type: 'string' },
            { name: 'plan', type: 'string' },
            { name: 'deleted_at', type: 'timestamp' },
          ]
        : [{ name: 'created_at', type: 'timestamp' }],
      identifier_columns: [],
      row_filters: [],
    }
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <AuthContext.Provider value={personaAuth(role)}>
      <PersonaProject persona={role}>
        <QueryClientProvider client={client}>
          <MemoryRouter>
            <MetricDefinitionCard slug="demo" definition={definition} />
          </MemoryRouter>
        </QueryClientProvider>
      </PersonaProject>
    </AuthContext.Provider>,
  )
}

describe('MetricDefinitionCard filters', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(metricsCatalogApi.getGeneratedSql).mockResolvedValue({
      queries: [],
      breakdown_queries_omitted: true,
    })
  })

  it('renders named row filters, conditions, and filter SQL for a single fact metric', async () => {
    renderCard(
      factDefinition({
        config: {
          measure_column: null,
          distinct_column: null,
          row_filters: ['Paying users'],
          filter_sql: "country != 'RU'",
          conditions: [
            { column: 'platform', operator: 'eq', value: 'ios' },
            { column: 'plan', operator: 'in', value: ['pro', 'team'] },
            { column: 'deleted_at', operator: 'is_null' },
            { column: 'days_available', operator: 'gt', value: '1' },
          ],
        },
      }),
    )

    expect(screen.getByText('Paying users')).toBeInTheDocument()
    expect(screen.getByText("platform = 'ios'")).toBeInTheDocument()
    expect(screen.getByText("plan in ('pro', 'team')")).toBeInTheDocument()
    expect(screen.getByText('deleted_at is null')).toBeInTheDocument()
    expect(await screen.findByText('days_available > 1')).toBeInTheDocument()
    expect(screen.queryByText("days_available > '1'")).not.toBeInTheDocument()
    expect(screen.getByText("country != 'RU'")).toBeInTheDocument()
    expect(screen.getAllByText('where')).toHaveLength(1)
    expect(screen.getAllByText('and')).toHaveLength(4)
  })

  it('links each fact-table name to its editor', async () => {
    renderCard(factDefinition({}))

    expect(await screen.findByRole('link', { name: 'Orders' })).toHaveAttribute(
      'href',
      `/p/demo/metrics/fact-tables/${FACT_TABLE_ID}/edit`,
    )
  })

  it('folds a legacy single row_filter name into the filter list', () => {
    renderCard(
      factDefinition({
        config: { row_filter: 'Active sessions' },
      }),
    )

    expect(screen.getByText('Active sessions')).toBeInTheDocument()
  })

  it('renders each ratio operand with its own filters', () => {
    renderCard(
      factDefinition({
        composition: 'ratio',
        aggregation: 'count',
        fact_table_id: FACT_TABLE_ID,
        config: {
          numerator: {
            fact_table_id: FACT_TABLE_ID,
            aggregation: 'count',
            row_filters: [],
            filter_sql: null,
            conditions: [{ column: 'status', operator: 'eq', value: 'paid' }],
          },
          denominator: {
            fact_table_id: OTHER_FACT_TABLE_ID,
            aggregation: 'count',
            row_filters: ['All sessions'],
            filter_sql: null,
            conditions: [],
          },
        },
      }),
    )

    expect(screen.getByText("status = 'paid'")).toBeInTheDocument()
    expect(screen.getByText('All sessions')).toBeInTheDocument()
  })

  it('omits the filter rows entirely when the metric has no filters', () => {
    renderCard(
      factDefinition({
        config: {
          measure_column: null,
          distinct_column: null,
          row_filters: [],
          filter_sql: null,
          conditions: [],
        },
      }),
    )

    expect(screen.queryByText(/where/)).not.toBeInTheDocument()
    expect(screen.queryByText(/filter ·/)).not.toBeInTheDocument()
  })

  it('loads canonical generated SQL when its collapsed disclosure opens', async () => {
    vi.mocked(metricsCatalogApi.getGeneratedSql).mockResolvedValue({
      queries: [
        {
          role: 'primary',
          label: 'Orders · 1d',
          fact_table_id: FACT_TABLE_ID,
          fact_table_name: 'Orders',
          interval: '1d',
          window_from: '2026-07-01T00:00:00Z',
          window_to: '2026-07-02T00:00:00Z',
          metric_ids: ['metric-1', 'metric-2'],
          sql: 'SELECT * FROM (SELECT * FROM orders) AS _filtered WHERE amount > 1',
        },
      ],
      breakdown_queries_omitted: true,
    })
    renderCard(factDefinition({}))

    expect(metricsCatalogApi.getGeneratedSql).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Generated batch SQL'))

    const editor = await screen.findByRole('textbox', { name: 'Generated batch SQL' })
    expect(editor).toHaveValue(
      'SELECT * FROM (SELECT * FROM orders) AS _filtered WHERE amount > 1',
    )
    expect(editor).toHaveAttribute('readonly')
    expect(screen.getByText(/2 metrics/)).toBeInTheDocument()
    expect(screen.getByText(/Breakdown queries are generated separately/)).toBeInTheDocument()
    await waitFor(() => {
      expect(metricsCatalogApi.getGeneratedSql).toHaveBeenCalledWith('demo', 'metric-1')
    })
  })

  it('offers the generated SQL to a viewer, who can read the metric config it is built from', async () => {
    vi.mocked(metricsCatalogApi.getGeneratedSql).mockResolvedValue({
      queries: [
        {
          role: 'primary',
          label: 'Orders · 1d',
          fact_table_id: FACT_TABLE_ID,
          fact_table_name: 'Orders',
          interval: '1d',
          window_from: '2026-07-01T00:00:00Z',
          window_to: '2026-07-02T00:00:00Z',
          metric_ids: ['metric-1'],
          sql: 'SELECT count() FROM orders',
        },
      ],
      breakdown_queries_omitted: true,
    })
    renderCard(factDefinition({}), 'viewer')

    fireEvent.click(screen.getByText('Generated batch SQL'))

    const editor = await screen.findByRole('textbox', { name: 'Generated batch SQL' })
    expect(editor).toHaveValue('SELECT count() FROM orders')
  })

  it('shows an explicit next update for active scheduled metrics', () => {
    renderCard(
      factDefinition({
        next_collection_at: '2026-07-15T12:30:00Z',
        collection_due: false,
      }),
    )

    expect(screen.getByText(/Next update/)).toHaveTextContent('2026')
  })

  it('shows due and unscheduled states explicitly', () => {
    const { rerender } = renderCard(
      factDefinition({ next_collection_at: null, collection_due: true }),
    )
    expect(screen.getByText('Due now')).toBeInTheDocument()

    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <MetricDefinitionCard
            slug="demo"
            definition={factDefinition({ status: 'draft', collection_due: false })}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(screen.getByText('Not collected while draft')).toBeInTheDocument()

    // An active metric with no interval leads to the editor (#246).
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <MetricDefinitionCard
            slug="demo"
            definition={factDefinition({ status: 'active', interval: null, collection_due: false })}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(screen.getByRole('link', { name: 'Set a schedule' })).toHaveAttribute(
      'href',
      expect.stringMatching(/^\/p\/demo\/metrics\/[^/]+\/edit$/),
    )
  })
})

describe('MetricDefinitionCard event composition', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(eventsApi.get).mockImplementation(async (_slug, id) => ({
      id,
      name: id === 'ev-late' ? 'checkout:completed' : 'checkout:started',
    }) as unknown as Awaited<ReturnType<typeof eventsApi.get>>)
    vi.mocked(eventTypesApi.list).mockResolvedValue([
      { id: 'type-1', name: 'signup', display_name: 'Signup' },
    ] as unknown as Awaited<ReturnType<typeof eventTypesApi.list>>)
  })

  it('names an event by id, wherever it sits in the catalog', async () => {
    renderCard(
      factDefinition({
        kind: 'event_composition',
        composition: 'single',
        fact_table_id: null,
        aggregation: null,
        numerator_event_id: 'ev-late',
      }),
    )
    expect(await screen.findByText('checkout:completed')).toBeInTheDocument()
    expect(eventsApi.get).toHaveBeenCalledWith('demo', 'ev-late')
  })

  it('names an event-type side instead of painting a dash', async () => {
    renderCard(
      factDefinition({
        kind: 'event_composition',
        composition: 'ratio',
        fact_table_id: null,
        aggregation: null,
        numerator_event_type_id: 'type-1',
        denominator_event_id: 'ev-early',
      }),
    )
    expect(await screen.findByText('type · Signup')).toBeInTheDocument()
    expect(await screen.findByText('checkout:started')).toBeInTheDocument()
    expect(screen.queryByText('—')).toBeNull()
  })

  // a SQL metric has no value column; it showed an empty "–" token.
  it('leaves out a column the SQL metric does not use', () => {
    renderCard(factDefinition({
      kind: 'sql',
      config: { metric_sql: 'select 1', time_column: 'ts' },
    }))
    expect(screen.getByText('time')).toBeInTheDocument()
    expect(screen.getByText('ts')).toBeInTheDocument()
    expect(screen.queryByText('value')).toBeNull()
    expect(screen.queryByText('—')).toBeNull()
  })

  it('labels the collection interval the way the rest of the UI does', () => {
    renderCard(factDefinition({ interval: '1h' }))
    expect(screen.getByText('Hourly')).toBeInTheDocument()
    expect(screen.queryByText('every 1h')).toBeNull()
  })
})
