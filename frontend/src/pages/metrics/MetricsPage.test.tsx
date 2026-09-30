import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useParams } from 'react-router-dom'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  DataSource,
  FactTableListItem,
  FactTableListResponse,
  MetricCollectNowResponse,
  MetricDefinitionDetailResponse,
  MetricDefinitionListItem,
  MetricDefinitionListResponse,
} from '@/types'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import MetricsPage, { type MetricsTab } from './MetricsPage'

/** The catalog's filters are FilterSelect chips (DS-15), not native selects. */
async function pickFilter(label: string, option: string) {
  fireEvent.click(screen.getByRole('combobox', { name: new RegExp(`^${label} filter`) }))
  fireEvent.click(await screen.findByRole('option', { name: option }))
}

vi.mock('@/api/metricsCatalog', () => ({
  metricsCatalogApi: {
    list: vi.fn(),
    get: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    collect: vi.fn(),
    bulkUpdate: vi.fn(),
    reorder: vi.fn(),
  },
}))
vi.mock('@/api/factTables', () => ({
  factTablesApi: { list: vi.fn() },
}))
vi.mock('@/api/dataSources', () => ({
  dataSourcesApi: { list: vi.fn() },
}))
// Mocked so collect-now toasts are observable (no <Toaster> mounts in tests).
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

import { toast } from 'sonner'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { factTablesApi } from '@/api/factTables'
import { dataSourcesApi } from '@/api/dataSources'
import { personaAuth } from '@/test/persona'
import { SessionProject } from '@/test/PersonaProject'

function makeItem(overrides: Partial<MetricDefinitionListItem>): MetricDefinitionListItem {
  return {
    id: 'm-1',
    project_id: 'p-1',
    name: 'checkout_conversion',
    display_name: 'Checkout conversion',
    description: '',
    kind: 'sql',
    status: 'active',
    aggregation: null,
    composition: null,
    interval: '1h',
    color: '#6366f1',
    unit: '%',
    anomaly_detection_enabled: true,
    reviewed: false,
    owner_id: null,
    order: 0,
    spark: [1, 2, 3, 4, 5],
    // Percent-unit metrics store fractions; 0.42 renders as '42%' (tripl-nxk2.1).
    latest_value: 0.42,
    latest_bucket: null,
    latest_signal: null,
    last_collected_at: null,
    last_collection_status: null,
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-20T00:00:00Z',
    ...overrides,
  }
}

// Full definition returned by `metricsCatalogApi.get` — what "Duplicate as
// draft" reads to build the create payload. Defaults to a SQL metric.
function makeDefinition(
  overrides: Partial<MetricDefinitionDetailResponse>,
): MetricDefinitionDetailResponse {
  return {
    id: 'm-1',
    project_id: 'p-1',
    name: 'checkout_conversion',
    display_name: 'Checkout conversion',
    description: '',
    color: '#6366f1',
    order: 0,
    unit: '%',
    status: 'active',
    owner_id: null,
    reviewed: false,
    kind: 'sql',
    aggregation: null,
    composition: null,
    config: { metric_sql: 'SELECT 1', time_column: 'bucket', value_column: null },
    fact_table_id: null,
    breakdown_columns: [],
    breakdown_values_limit: null,
    app_version_column: null,
    platform_column: null,
    data_source_id: 'ds-1',
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
    next_collection_at: null,
    collection_due: false,
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-20T00:00:00Z',
    ...overrides,
  }
}

function makeFactTable(overrides: Partial<FactTableListItem>): FactTableListItem {
  return {
    id: 'ft-1',
    project_id: 'p-1',
    name: 'orders',
    display_name: 'Orders',
    description: '',
    color: '#6366f1',
    order: 0,
    data_source_id: 'ds-1',
    timestamp_column: 'created_at',
    metric_count: 0,
    column_count: 0,
    identifier_count: 0,
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-20T00:00:00Z',
    ...overrides,
  }
}

// active_total is server-side (tripl-jfm3.109); default it so every existing
// fixture keeps expressing only what its own assertion is about.
function mockList(body: Omit<MetricDefinitionListResponse, 'active_total'> & { active_total?: number }) {
  vi.mocked(metricsCatalogApi.list).mockResolvedValue({
    active_total: body.items.filter(m => m.status === 'active').length,
    ...body,
  })
}

function mockFactTables(body: FactTableListResponse) {
  vi.mocked(factTablesApi.list).mockResolvedValue(body)
}

// The row only reads `state` + `direction`; a minimal shape keeps the test
// focused (mirrors the existing `as unknown as` casts above).
function makeSignal(
  state: string,
  direction: 'spike' | 'drop' = 'spike',
): NonNullable<MetricDefinitionListItem['latest_signal']> {
  return { state, direction } as unknown as NonNullable<
    MetricDefinitionListItem['latest_signal']
  >
}

// Probe standing in for the metric edit page, so a row-menu navigation to the
// edit route is observable (the real editor is a separate route/module).
function EditRouteProbe() {
  const { metricId } = useParams<{ metricId: string }>()
  return <div data-testid="edit-route">{metricId}</div>
}

function renderMetrics(
  tab: MetricsTab = 'catalog',
  pathOverride?: string,
  auth: AuthContextValue | null = null,
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const path =
    pathOverride ?? (tab === 'fact-tables' ? '/p/demo/metrics/fact-tables' : '/p/demo/metrics')
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <SessionProject session={auth}>
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route path="/p/:slug/metrics" element={<MetricsPage tab="catalog" />} />
              <Route path="/p/:slug/metrics/fact-tables" element={<MetricsPage tab="fact-tables" />} />
              <Route path="/p/:slug/metrics/:metricId/edit" element={<EditRouteProbe />} />
            </Routes>
          </MemoryRouter>
        </SessionProject>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

// Radix DropdownMenu drives open/close through pointer-capture APIs that jsdom
// omits; stub them so the trigger opens under test.
beforeAll(() => {
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.setPointerCapture = vi.fn()
  Element.prototype.releasePointerCapture = vi.fn()
})

beforeEach(() => {
  vi.mocked(metricsCatalogApi.list).mockReset()
  vi.mocked(factTablesApi.list).mockReset()
  vi.mocked(dataSourcesApi.list).mockReset()
  vi.mocked(dataSourcesApi.list).mockResolvedValue([
    { id: 'ds-1', name: 'Warehouse' },
  ] as unknown as DataSource[])
})

afterEach(() => {
  vi.restoreAllMocks()
})

function viewerAuth(): AuthContextValue {
  return personaAuth('viewer')
}

describe('MetricsPage — a viewer reads the catalog without write controls (MET-6)', () => {
  it('hides New, the select boxes, the reorder handles and the row menu', async () => {
    mockList({
      items: [
        makeItem({ id: 'm-1', display_name: 'Checkout conversion' }),
        makeItem({ id: 'm-2', display_name: 'Signups', name: 'signups' }),
      ],
      total: 2,
    })
    renderMetrics('catalog', undefined, viewerAuth())

    expect(await screen.findByText('Checkout conversion')).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent(/viewer role/)
    expect(screen.queryByRole('link', { name: /New metric/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'Select all metrics' })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'Select Checkout conversion' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reorder Checkout conversion' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Actions for Checkout conversion' })).not.toBeInTheDocument()
    // Nor the empty tracks those controls sat in (MT-29).
    expect(screen.queryByRole('columnheader', { name: 'Reorder' })).not.toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Actions' })).not.toBeInTheDocument()
  })
})

describe('MetricsPage — the kind filter is deep-linkable (tripl-2su6.19)', () => {
  it('opens the catalog already filtered when ?kind= is present', async () => {
    // The demo's metric building blocks link straight to a kind. That only
    // discovers anything if the catalog honours the param — it used to keep the
    // filter in component state, so every block landed on the same unfiltered page.
    mockList({ items: [], total: 0 })
    renderMetrics('catalog', '/p/demo/metrics?kind=fact')

    await waitFor(() =>
      expect(metricsCatalogApi.list).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({ kind: 'fact' }),
      ),
    )
  })

  it('ignores an unknown kind rather than querying it', async () => {
    mockList({ items: [], total: 0 })
    renderMetrics('catalog', '/p/demo/metrics?kind=not-a-kind')

    await waitFor(() => expect(metricsCatalogApi.list).toHaveBeenCalled())
    expect(metricsCatalogApi.list).toHaveBeenCalledWith(
      'demo',
      expect.objectContaining({ kind: undefined }),
    )
  })
})

describe('MetricsPage', () => {
  it('renders the rollup and a metric row from the mocked api', async () => {
    mockList({
      items: [makeItem({ id: 'm-1', display_name: 'Checkout conversion', kind: 'sql', latest_value: 0.42 })],
      total: 1,
    })

    renderMetrics()

    expect(await screen.findByRole('heading', { name: 'Metrics' })).toBeInTheDocument()
    const cell = await screen.findByText('Checkout conversion')
    // Scope kind/status assertions to the row — those labels also appear in the
    // filter <select> options and the rollup MiniStats.
    const row = cell.closest('[role="row"]') as HTMLElement
    expect(row).not.toBeNull()
    expect(within(row).getByText('SQL')).toBeInTheDocument()
    expect(within(row).getByText('42%')).toBeInTheDocument()
    expect(within(row).getByText('Active')).toBeInTheDocument()
  })

  it('renders both Catalog and Fact tables tabs as deep-linkable routes', async () => {
    mockList({ items: [], total: 0 })

    renderMetrics()

    // Route links in a named nav, not a tablist that never behaved like one
    // (DS-35 / MET-38).
    const sections = await screen.findByRole('navigation', { name: 'Metrics sections' })
    expect(screen.queryByRole('tablist')).toBeNull()
    const catalogTab = within(sections).getByRole('link', { name: 'Catalog' })
    const factTablesTab = within(sections).getByRole('link', { name: 'Fact tables' })
    expect(catalogTab).toHaveAttribute('href', '/p/demo/metrics')
    expect(factTablesTab).toHaveAttribute('href', '/p/demo/metrics/fact-tables')
    // On the Catalog route, the Catalog link is the current page.
    expect(catalogTab).toHaveAttribute('aria-current', 'page')
    expect(factTablesTab).not.toHaveAttribute('aria-current')
  })

  it('shows the New metric action on the Catalog tab', async () => {
    mockList({ items: [makeItem({ id: 'm-1' })], total: 1 })

    renderMetrics()

    const links = await screen.findAllByRole('link', { name: /New metric/ })
    expect(links[0]).toHaveAttribute('href', '/p/demo/metrics/new')
  })

  it('links each metric row to its monitoring drilldown route', async () => {
    mockList({ items: [makeItem({ id: 'abc-123', display_name: 'Revenue' })], total: 1 })

    renderMetrics()

    const link = await screen.findByRole('link', { name: 'Revenue' })
    expect(link).toHaveAttribute('href', '/p/demo/monitoring/metric/abc-123')
  })

  it('shows active-anomaly visuals for a latest-scan signal', async () => {
    mockList({
      items: [
        makeItem({
          id: 'm-firing',
          display_name: 'Firing metric',
          latest_signal: makeSignal('latest_scan', 'spike'),
        }),
      ],
      total: 1,
    })

    renderMetrics()

    const cell = await screen.findByText('Firing metric')
    const row = cell.closest('[role="row"]') as HTMLElement
    expect(row).not.toBeNull()
    // Pulsing dot + sparkline anomaly marker on the latest (current) point.
    // Scope to the sparkline's danger-filled marker so the row-action kebab
    // icon's own <circle> dots don't satisfy the assertion.
    expect(row.querySelector('.pulse-dot')).not.toBeNull()
    expect(row.querySelector('circle[fill="var(--danger)"]')).not.toBeNull()
  })

  it('suppresses active-anomaly visuals for a recent (already-cleared) signal', async () => {
    mockList({
      items: [
        makeItem({
          id: 'm-recent',
          display_name: 'Recent metric',
          latest_signal: makeSignal('recent', 'spike'),
        }),
      ],
      total: 1,
    })

    renderMetrics()

    const cell = await screen.findByText('Recent metric')
    const row = cell.closest('[role="row"]') as HTMLElement
    expect(row).not.toBeNull()
    // The most recent scan was clean: no pulse, no anomaly marker. Scope to the
    // sparkline's danger-filled marker so the row-action kebab icon's own
    // <circle> dots don't trip the assertion.
    expect(row.querySelector('.pulse-dot')).toBeNull()
    expect(row.querySelector('circle[fill="var(--danger)"]')).toBeNull()
  })

  it('shows an empty state with a create CTA when the catalog is empty', async () => {
    mockList({ items: [], total: 0 })

    renderMetrics()

    expect(await screen.findByText('No metrics yet')).toBeInTheDocument()
    const links = await screen.findAllByRole('link', { name: /New metric/ })
    expect(links[0]).toHaveAttribute('href', '/p/demo/metrics/new')
  })

  describe('bulk actions and reorder (tripl-57o8)', () => {
    it('selects rows and applies a bulk status change', async () => {
      mockList({
        items: [
          makeItem({ id: 'm-1', display_name: 'Checkout conversion' }),
          makeItem({ id: 'm-2', display_name: 'Revenue', name: 'revenue' }),
        ],
        total: 2,
      })
      vi.mocked(metricsCatalogApi.bulkUpdate).mockResolvedValue(undefined)

      renderMetrics()

      fireEvent.click(await screen.findByRole('checkbox', { name: 'Select Checkout conversion' }))
      expect(await screen.findByText('1 selected')).toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: 'Set archived' }))
      await waitFor(() =>
        expect(metricsCatalogApi.bulkUpdate).toHaveBeenCalledWith('demo', {
          metric_ids: ['m-1'],
          status: 'archived',
        }),
      )
    })

    it('select-all covers every visible row', async () => {
      mockList({
        items: [
          makeItem({ id: 'm-1', display_name: 'Checkout conversion' }),
          makeItem({ id: 'm-2', display_name: 'Revenue', name: 'revenue' }),
        ],
        total: 2,
      })

      renderMetrics()

      fireEvent.click(await screen.findByRole('checkbox', { name: 'Select all metrics' }))
      expect(await screen.findByText('2 selected')).toBeInTheDocument()
    })

    it('shows a partial selection as mixed, and clears it from there (EV-26)', async () => {
      mockList({
        items: [
          makeItem({ id: 'm-1', display_name: 'Checkout conversion' }),
          makeItem({ id: 'm-2', display_name: 'Revenue', name: 'revenue' }),
        ],
        total: 2,
      })

      renderMetrics()

      fireEvent.click(await screen.findByRole('checkbox', { name: 'Select Checkout conversion' }))
      const header = screen.getByRole('checkbox', { name: 'Select all metrics' })
      expect(header).toHaveAttribute('aria-checked', 'mixed')

      fireEvent.click(header)
      await waitFor(() => expect(screen.queryByText('1 selected')).not.toBeInTheDocument())
      expect(screen.getByRole('checkbox', { name: 'Select Checkout conversion' })).not.toBeChecked()
    })

    it('clears the selection when the filter view changes', async () => {
      mockList({
        items: [
          makeItem({ id: 'm-1', display_name: 'Checkout conversion' }),
          makeItem({ id: 'm-2', display_name: 'Revenue', name: 'revenue' }),
        ],
        total: 2,
      })

      renderMetrics()

      fireEvent.click(await screen.findByRole('checkbox', { name: 'Select Checkout conversion' }))
      expect(await screen.findByText('1 selected')).toBeInTheDocument()

      await pickFilter('Status', 'Active')
      await waitFor(() => expect(screen.queryByText('1 selected')).not.toBeInTheDocument())
    })

    it('shows drag handles only when the full unfiltered catalog is listed', async () => {
      mockList({
        items: [
          makeItem({ id: 'm-1', display_name: 'Checkout conversion' }),
          makeItem({ id: 'm-2', display_name: 'Revenue', name: 'revenue' }),
        ],
        total: 2,
      })

      renderMetrics()

      expect(
        await screen.findByRole('button', { name: 'Reorder Checkout conversion' }),
      ).toBeInTheDocument()

      // A status filter narrows the list — partial orders can't be persisted,
      // so the handles disappear.
      await pickFilter('Status', 'Active')
      await waitFor(() =>
        expect(
          screen.queryByRole('button', { name: 'Reorder Checkout conversion' }),
        ).not.toBeInTheDocument(),
      )
    })
  })

  describe('row actions menu (tripl-nxk2.9)', () => {
    async function openRowMenu(name: string) {
      const trigger = await screen.findByRole('button', { name: `Actions for ${name}` })
      fireEvent.keyDown(trigger, { key: 'Enter' })
    }

    it('duplicates a metric as a draft and navigates to its edit page', async () => {
      mockList({
        items: [
          makeItem({ id: 'm-1', name: 'checkout_conversion', display_name: 'Checkout conversion' }),
        ],
        total: 1,
      })
      vi.mocked(metricsCatalogApi.get).mockResolvedValue(
        makeDefinition({
          id: 'm-1',
          name: 'checkout_conversion',
          display_name: 'Checkout conversion',
        }),
      )
      vi.mocked(metricsCatalogApi.create).mockResolvedValue(
        makeDefinition({
          id: 'm-copy',
          name: 'checkout_conversion_copy',
          display_name: 'Checkout conversion (copy)',
          status: 'draft',
        }),
      )

      renderMetrics()

      await openRowMenu('Checkout conversion')
      fireEvent.click(await screen.findByRole('menuitem', { name: 'Duplicate as draft' }))

      await waitFor(() =>
        expect(metricsCatalogApi.create).toHaveBeenCalledWith(
          'demo',
          expect.objectContaining({
            kind: 'sql',
            status: 'draft',
            display_name: 'Checkout conversion (copy)',
            name: 'checkout_conversion_copy',
          }),
        ),
      )
      // Lands on the freshly created draft's edit route.
      expect(await screen.findByTestId('edit-route')).toHaveTextContent('m-copy')
    })

    it('duplicates a legacy fact metric with its named filter and a narrowed config (MET-43)', async () => {
      mockList({
        items: [makeItem({ id: 'm-1', name: 'orders', display_name: 'Orders' })],
        total: 1,
      })
      vi.mocked(metricsCatalogApi.get).mockResolvedValue(
        makeDefinition({
          id: 'm-1',
          name: 'orders',
          display_name: 'Orders',
          kind: 'fact',
          composition: 'single',
          aggregation: 'count',
          fact_table_id: 'ft-1',
          data_source_id: null,
          config: {
            row_filter: 'completed',
            row_filters: ['paid', 42],
            conditions: [{ column: 'amount', operator: 'gt', value: 3 }],
          },
        }),
      )
      vi.mocked(metricsCatalogApi.create).mockResolvedValue(
        makeDefinition({ id: 'm-copy', name: 'orders_copy', status: 'draft' }),
      )

      renderMetrics()

      await openRowMenu('Orders')
      fireEvent.click(await screen.findByRole('menuitem', { name: 'Duplicate as draft' }))

      await waitFor(() =>
        expect(metricsCatalogApi.create).toHaveBeenCalledWith(
          'demo',
          expect.objectContaining({
            kind: 'fact',
            fact_table_id: 'ft-1',
            aggregation: 'count',
            row_filters: ['paid', 'completed'],
            conditions: [{ column: 'amount', operator: 'gt', value: 3 }],
          }),
        ),
      )
    })

    it('archives an active metric from the row menu', async () => {
      mockList({
        items: [makeItem({ id: 'm-1', display_name: 'Checkout conversion', status: 'active' })],
        total: 1,
      })
      vi.mocked(metricsCatalogApi.update).mockResolvedValue(
        makeDefinition({ id: 'm-1', status: 'archived' }),
      )

      renderMetrics()

      await openRowMenu('Checkout conversion')
      fireEvent.click(await screen.findByRole('menuitem', { name: 'Archive' }))

      await waitFor(() =>
        expect(metricsCatalogApi.update).toHaveBeenCalledWith('demo', 'm-1', {
          status: 'archived',
        }),
      )
    })

    // Manual collect feedback (tripl-4mju): the row action must confirm the run
    // started, then watch the persisted last_collection_status and report the
    // terminal outcome — success, or the failure reason the worker stamped.
    describe('collect now feedback (tripl-4mju)', () => {
      function mockCollectQueued() {
        vi.mocked(metricsCatalogApi.collect).mockResolvedValue({
          metric_id: 'm-1',
          status: 'queued',
          window_from: null,
          window_to: null,
          task_id: 'task-1',
        } as unknown as MetricCollectNowResponse)
      }

      it('confirms the start and toasts success once the run settles', async () => {
        mockList({
          items: [makeItem({ id: 'm-1', display_name: 'Checkout conversion' })],
          total: 1,
        })
        mockCollectQueued()
        vi.mocked(metricsCatalogApi.get).mockResolvedValue(
          makeDefinition({ id: 'm-1', last_collection_status: 'success' }),
        )

        renderMetrics()

        await openRowMenu('Checkout conversion')
        fireEvent.click(await screen.findByRole('menuitem', { name: 'Collect now' }))

        await waitFor(() =>
          expect(metricsCatalogApi.collect).toHaveBeenCalledWith('demo', 'm-1'),
        )
        // Immediate confirmation the run started…
        await waitFor(() =>
          expect(toast.success).toHaveBeenCalledWith(
            'Collection started — you will be notified when it finishes.',
          ),
        )
        // …then the terminal outcome from the status watch.
        await waitFor(() =>
          expect(toast.success).toHaveBeenCalledWith(
            '"Checkout conversion" collected — the chart is up to date.',
          ),
        )
        expect(toast.error).not.toHaveBeenCalled()
      })

      it('surfaces the worker-persisted failure reason', async () => {
        mockList({
          items: [makeItem({ id: 'm-1', display_name: 'Checkout conversion' })],
          total: 1,
        })
        mockCollectQueued()
        vi.mocked(metricsCatalogApi.get).mockResolvedValue(
          makeDefinition({
            id: 'm-1',
            last_collection_status: 'error',
            last_collection_error: 'SQL syntax error near SELECT',
          }),
        )

        renderMetrics()

        await openRowMenu('Checkout conversion')
        fireEvent.click(await screen.findByRole('menuitem', { name: 'Collect now' }))

        await waitFor(() =>
          expect(toast.error).toHaveBeenCalledWith(
            'Collection failed: SQL syntax error near SELECT',
          ),
        )
      })

      it('toasts an error when the trigger request itself fails', async () => {
        mockList({
          items: [makeItem({ id: 'm-1', display_name: 'Checkout conversion' })],
          total: 1,
        })
        vi.mocked(metricsCatalogApi.collect).mockRejectedValue(new Error('503'))
        // Drop call history leaked from earlier tests in this file (module-factory
        // vi.fn()s survive restoreAllMocks) so the "never polled" check is real.
        vi.mocked(metricsCatalogApi.get).mockClear()

        renderMetrics()

        await openRowMenu('Checkout conversion')
        fireEvent.click(await screen.findByRole('menuitem', { name: 'Collect now' }))

        await waitFor(() =>
          expect(toast.error).toHaveBeenCalledWith('Could not start collection — 503'),
        )
        // No watch starts, so the status endpoint is never polled.
        expect(metricsCatalogApi.get).not.toHaveBeenCalled()
      })
    })
  })

  describe('operational stat bar and column context (tripl-nxk2.10 / tripl-nxk2.11)', () => {
    function firingAndQuiet(): MetricDefinitionListResponse {
      return {
        items: [
          makeItem({
            id: 'm-firing',
            name: 'firing',
            display_name: 'Firing metric',
            latest_signal: makeSignal('latest_scan', 'spike'),
          }),
          makeItem({ id: 'm-quiet', name: 'quiet', display_name: 'Quiet metric', latest_signal: null }),
        ],
        total: 2,
        active_total: 2,
      }
    }

    it('counts active anomalies and filters the table to them on click', async () => {
      mockList(firingAndQuiet())

      renderMetrics()

      // Wait for the data to load: both rows visible before filtering.
      expect(await screen.findByText('Firing metric')).toBeInTheDocument()
      expect(screen.getByText('Quiet metric')).toBeInTheDocument()
      const anomaliesFilter = screen.getByRole('button', { name: /^With anomalies\b/ })
      // One of the two loaded metrics is firing on the latest scan.
      expect(within(anomaliesFilter).getByText('1')).toBeInTheDocument()

      fireEvent.click(anomaliesFilter)

      // Filtered down to just the firing metric.
      expect(screen.getByText('Firing metric')).toBeInTheDocument()
      expect(screen.queryByText('Quiet metric')).not.toBeInTheDocument()
      expect(anomaliesFilter).toHaveAttribute('aria-pressed', 'true')
    })

    it('restores the full list when the anomalies filter is toggled off', async () => {
      mockList(firingAndQuiet())

      renderMetrics()

      expect(await screen.findByText('Quiet metric')).toBeInTheDocument()
      const anomaliesFilter = screen.getByRole('button', { name: /^With anomalies\b/ })
      fireEvent.click(anomaliesFilter)
      expect(screen.queryByText('Quiet metric')).not.toBeInTheDocument()

      // Clicking the active stat again clears the client-side filter.
      fireEvent.click(anomaliesFilter)
      expect(await screen.findByText('Quiet metric')).toBeInTheDocument()
      expect(anomaliesFilter).toHaveAttribute('aria-pressed', 'false')
    })

    it('clears the anomalies filter when a server-side filter changes', async () => {
      mockList(firingAndQuiet())

      renderMetrics()

      expect(await screen.findByText('Quiet metric')).toBeInTheDocument()
      const anomaliesFilter = screen.getByRole('button', { name: /^With anomalies\b/ })
      fireEvent.click(anomaliesFilter)
      expect(screen.queryByText('Quiet metric')).not.toBeInTheDocument()

      // Changing the status filter swaps the loaded set, so the signal filter is
      // dropped and both rows return (the mock ignores server-side filter args).
      await pickFilter('Status', 'Active')
      expect(await screen.findByText('Quiet metric')).toBeInTheDocument()
      expect(anomaliesFilter).toHaveAttribute('aria-pressed', 'false')
    })

    // A content assertion, not a styling one: the tile counts catalog metrics
    // carrying a latest-scan signal, while the sidebar badge counts every
    // significant open signal in the project. Captioned "Anomalies" the tile
    // read as the project-wide number and contradicted the nav in the same
    // viewport (tripl-vsw2).
    it('captions the anomaly stat with its metric-catalog scope, not the bare word', async () => {
      mockList(firingAndQuiet())

      renderMetrics()

      await screen.findByText('Firing metric')
      expect(screen.getByText('With anomalies')).toBeInTheDocument()
      expect(screen.queryByText('Anomalies')).not.toBeInTheDocument()
    })

    // The name is the only cell a reader cannot reconstruct from the rest of the
    // row, and a wide kind chip can clip it (tripl-862w).
    it('titles the metric name with its full display name so a clipped name survives', async () => {
      mockList({
        items: [
          makeItem({
            id: 'm-1',
            name: 'purchase_conversion',
            display_name: 'Purchase conversion',
            kind: 'event_composition',
          }),
        ],
        total: 1,
      })

      renderMetrics()

      const link = await screen.findByRole('link', { name: 'Purchase conversion' })
      expect(link).toHaveAttribute('title', 'Purchase conversion')
    })

    it('titles the Trend column header with the real collection count (MT-34)', async () => {
      mockList({ items: [makeItem({ id: 'm-1', spark: [1, 2, 3, 4, 5, 6, 7, 8] })], total: 1 })

      renderMetrics()

      expect(await screen.findByRole('columnheader', { name: 'Trend' })).toHaveAttribute(
        'title',
        'Last 8 collections',
      )
    })

    it('titles the Latest cell with the latest bucket time when available', async () => {
      mockList({ items: [makeItem({ id: 'm-1', latest_bucket: '2026-06-20T00:00:00Z' })], total: 1 })

      renderMetrics()

      const cell = await screen.findByText('42%')
      expect(cell.getAttribute('title')).toMatch(/^Latest point:/)
    })

    it('falls back to the collection interval as the Latest-cell title', async () => {
      mockList({
        items: [makeItem({ id: 'm-1', latest_bucket: null, latest_signal: null, interval: '1h' })],
        total: 1,
      })

      renderMetrics()

      const cell = await screen.findByText('42%')
      expect(cell).toHaveAttribute('title', 'Collected hourly')
    })
  })

  describe('Fact tables tab', () => {
    it('keeps the Metrics heading and lists fact tables on the Fact tables tab', async () => {
      mockFactTables({
        items: [makeFactTable({ id: 'ft-1', display_name: 'Orders', timestamp_column: 'created_at' })],
        total: 1,
      })

      renderMetrics('fact-tables')

      // Same page chrome (H1 "Metrics", area "Observe"), Fact tables tab selected.
      expect(await screen.findByRole('heading', { name: 'Metrics' })).toBeInTheDocument()
      expect(screen.getByRole('link', { name: 'Fact tables' })).toHaveAttribute(
        'aria-current',
        'page',
      )

      const cell = await screen.findByText('Orders')
      const row = cell.closest('[role="row"]') as HTMLElement
      expect(row).not.toBeNull()
      expect(within(row).getByText('created_at')).toBeInTheDocument()
      // Row edit link points at the fact-tables tab edit route.
      const link = within(row).getByRole('link', { name: 'Orders' })
      expect(link).toHaveAttribute('href', '/p/demo/metrics/fact-tables/ft-1/edit')
    })

    it('shows the contextual New fact table action on the Fact tables tab', async () => {
      mockFactTables({ items: [], total: 0 })

      renderMetrics('fact-tables')

      const links = await screen.findAllByRole('link', { name: /New fact table/ })
      expect(links[0]).toHaveAttribute('href', '/p/demo/metrics/fact-tables/new')
    })

    it('hints what a fact table is beside the header only on the Fact tables tab (JR-31)', async () => {
      mockFactTables({ items: [], total: 0 })

      renderMetrics('fact-tables')

      expect(
        await screen.findByRole('link', { name: 'What is Fact tables? Open in Concepts' }),
      ).toHaveAttribute('href', '/p/demo/concepts#term-fact-tables')
    })
  })
})
