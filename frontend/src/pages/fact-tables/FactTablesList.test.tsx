import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DataSource, FactTableListItem, FactTableListResponse } from '@/types'
import { FactTablesList } from './FactTablesList'

vi.mock('@/api/factTables', () => ({
  factTablesApi: { list: vi.fn(), remove: vi.fn(), get: vi.fn(), create: vi.fn() },
}))
vi.mock('@/api/dataSources', () => ({
  dataSourcesApi: { list: vi.fn() },
}))

import { factTablesApi } from '@/api/factTables'
import { dataSourcesApi } from '@/api/dataSources'

function makeItem(overrides: Partial<FactTableListItem>): FactTableListItem {
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

function mockList(body: FactTableListResponse) {
  vi.mocked(factTablesApi.list).mockResolvedValue(body)
}

// Mount the list body the same way the Fact tables tab does: inside a project
// route that supplies the :slug param.
function renderList() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/p/demo/metrics/fact-tables']}>
        <Routes>
          <Route path="/p/:slug/metrics/fact-tables" element={<FactTablesListHarness />} />
          <Route path="/p/:slug/metrics/fact-tables/:id/edit" element={<div>fact table editor</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

// Tiny harness that reads :slug from the route and feeds it to the list, since
// FactTablesList now takes slug as a prop (the parent MetricsPage resolves it).
function FactTablesListHarness() {
  return <FactTablesList slug="demo" />
}

beforeEach(() => {
  vi.mocked(factTablesApi.list).mockReset()
  vi.mocked(dataSourcesApi.list).mockReset()
  vi.mocked(dataSourcesApi.list).mockResolvedValue([
    { id: 'ds-1', name: 'Warehouse' },
  ] as unknown as DataSource[])
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('FactTablesList', () => {
  it('renders a fact table row with its source and timestamp column', async () => {
    mockList({
      items: [makeItem({ id: 'ft-1', display_name: 'Orders', timestamp_column: 'created_at' })],
      total: 1,
    })

    renderList()

    const cell = await screen.findByText('Orders')
    const row = cell.closest('[role="row"]') as HTMLElement
    expect(row).not.toBeNull()
    expect(within(row).getByText('orders')).toBeInTheDocument()
    expect(within(row).getByText('Warehouse')).toBeInTheDocument()
    expect(within(row).getByText('created_at')).toBeInTheDocument()
  })

  it('titles the list panel for what it lists, not for the tab next to it', async () => {
    mockList({ items: [makeItem({})], total: 1 })

    renderList()

    const table = await screen.findByRole('table', { name: 'Fact tables' })
    const panel = table.closest('section') as HTMLElement
    expect(panel).not.toBeNull()
    const header = panel.querySelector('header') as HTMLElement
    expect(header).not.toBeNull()
    // The header used to read "Catalog / 1 total": the same hardcoded title as
    // the panel on the Catalog tab. Only the title was wrong. The
    // "N total" caption is the shape both tabs' list panels share, and dropping
    // it here left Fact tables the one bare header on the page.
    expect(header.textContent).toBe('Fact tables1 total')
    expect(screen.queryByText('Catalog')).toBeNull()
  })

  // The count is the server's `total`, not `items.length`: a truncated first
  // page would otherwise caption the panel with the page size.
  it('captions the panel with the server total, not the row count', async () => {
    mockList({ items: [makeItem({ id: 'ft-1' })], total: 12 })

    renderList()

    const table = await screen.findByRole('table', { name: 'Fact tables' })
    const header = (table.closest('section') as HTMLElement).querySelector('header') as HTMLElement
    expect(header.textContent).toBe('Fact tables12 total')
  })

  it('links each row to its edit route under Metrics', async () => {
    mockList({ items: [makeItem({ id: 'abc-123', display_name: 'Sessions' })], total: 1 })

    renderList()

    const link = await screen.findByRole('link', { name: 'Sessions' })
    expect(link).toHaveAttribute('href', '/p/demo/metrics/fact-tables/abc-123/edit')
  })

  it('shows an empty state with a create CTA pointing under Metrics', async () => {
    mockList({ items: [], total: 0 })

    renderList()

    expect(await screen.findByText('No fact tables yet')).toBeInTheDocument()
    const links = await screen.findAllByRole('link', { name: /New fact table/ })
    expect(links[0]).toHaveAttribute('href', '/p/demo/metrics/fact-tables/new')
  })
})

describe('FactTablesList data source column (MET-37)', () => {
  it('shows a placeholder, not a dash, while source names load', async () => {
    vi.mocked(dataSourcesApi.list).mockImplementation(() => new Promise(() => {}))
    mockList({ items: [makeItem({ id: 'ft-1', display_name: 'Orders' })], total: 1 })

    renderList()

    const row = (await screen.findByText('Orders')).closest('[role="row"]') as HTMLElement
    expect(within(row).getByText('Loading data source')).toBeInTheDocument()
    expect(within(row).queryByText('Missing source')).toBeNull()
  })

  it('flags a fact table whose source was deleted (data_source_id set to null)', async () => {
    // The FK is ON DELETE SET NULL, so this is what a deleted source looks like.
    mockList({
      items: [makeItem({ id: 'ft-1', display_name: 'Orders', data_source_id: null })],
      total: 1,
    })

    renderList()

    const row = (await screen.findByText('Orders')).closest('[role="row"]') as HTMLElement
    expect(await within(row).findByText('Missing source')).toBeInTheDocument()
    expect(within(row).queryByText('—')).toBeNull()
  })

  it('flags a fact table whose source id is not in the source list', async () => {
    mockList({
      items: [makeItem({ id: 'ft-1', display_name: 'Orders', data_source_id: 'ds-gone' })],
      total: 1,
    })

    renderList()

    const row = (await screen.findByText('Orders')).closest('[role="row"]') as HTMLElement
    expect(await within(row).findByText('Missing source')).toBeInTheDocument()
  })

  it('says the names failed to load and offers a retry', async () => {
    vi.mocked(dataSourcesApi.list).mockRejectedValue(new Error('boom'))
    mockList({ items: [makeItem({ id: 'ft-1', display_name: 'Orders' })], total: 1 })

    renderList()

    expect(await screen.findByText('Data source names could not be loaded.')).toBeInTheDocument()
    const row = screen.getByText('Orders').closest('[role="row"]') as HTMLElement
    expect(within(row).getByText('Unavailable')).toBeInTheDocument()
    expect(within(row).queryByText('Missing source')).toBeNull()

    vi.mocked(dataSourcesApi.list).mockResolvedValue([
      { id: 'ds-1', name: 'Warehouse' },
    ] as unknown as DataSource[])
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await within(row).findByText('Warehouse')).toBeInTheDocument()
  })
})

describe('FactTablesList rows (MT-30)', () => {
  it('says how many metrics use each table, and counts the tables in use', async () => {
    mockList({
      items: [
        makeItem({ id: 'ft-1', display_name: 'Orders', metric_count: 3, column_count: 5, identifier_count: 1 }),
        makeItem({ id: 'ft-2', name: 'refunds', display_name: 'Refunds', metric_count: 0 }),
      ],
      total: 2,
    })
    renderList()

    const orders = (await screen.findByRole('link', { name: 'Orders' })).closest('[role="row"]')!
    expect(within(orders as HTMLElement).getByText('3 metrics')).toHaveAttribute(
      'title',
      '5 columns, 1 identifier',
    )
    const refunds = screen.getByRole('link', { name: 'Refunds' }).closest('[role="row"]')!
    expect(within(refunds as HTMLElement).getByText('No metrics')).toBeInTheDocument()
    // Tables, not a sum of metric_count: a cross-table ratio counts in both
    // tables' metric_count, so summing them double-counted it.
    expect(screen.getByText('Tables in use').parentElement?.textContent).toBe('Tables in use1')
  })

  it('deletes a table from its row menu after a confirm, without opening it', async () => {
    mockList({ items: [makeItem({ id: 'ft-1', display_name: 'Orders' })], total: 1 })
    vi.mocked(factTablesApi.remove).mockResolvedValue(undefined)
    renderList()

    const trigger = await screen.findByRole('button', { name: 'Actions for Orders' })
    fireEvent.keyDown(trigger, { key: 'Enter' })
    expect(await screen.findByRole('menuitem', { name: 'Edit' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete' }))

    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete fact table' }))
    await waitFor(() => expect(factTablesApi.remove).toHaveBeenCalledWith('demo', 'ft-1'))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    // The menu's clicks do not fall through to the row's open-on-click.
    expect(screen.queryByText('fact table editor')).toBeNull()
  })

  it('opens the fact table from anywhere on its row', async () => {
    mockList({ items: [makeItem({ id: 'ft-1', display_name: 'Orders' })], total: 1 })
    renderList()

    const row = (await screen.findByRole('link', { name: 'Orders' })).closest('[role="row"]')!
    fireEvent.click(within(row as HTMLElement).getByText('created_at'))

    expect(await screen.findByText('fact table editor')).toBeInTheDocument()
  })
})

describe('FactTablesList follow-ups (F7)', () => {
  it('links a Used-by count to the catalog narrowed to that table', async () => {
    mockList({ items: [makeItem({ id: 'ft-1', display_name: 'Orders', metric_count: 3 })], total: 1 })
    renderList()

    const link = await screen.findByRole('link', { name: '3 metrics' })
    expect(link).toHaveAttribute('href', '/p/demo/metrics?fact_table=ft-1')
  })

  it('duplicates a table under a free name and opens the copy', async () => {
    mockList({
      items: [
        makeItem({ id: 'ft-1', name: 'orders', display_name: 'Orders' }),
        makeItem({ id: 'ft-2', name: 'orders_copy', display_name: 'Orders (copy)' }),
      ],
      total: 2,
    })
    vi.mocked(factTablesApi.get).mockResolvedValue({
      ...makeItem({ id: 'ft-1', name: 'orders', display_name: 'Orders' }),
      sql: 'SELECT id, created_at FROM orders',
      columns: [{ name: 'id', type: 'bigint' }],
      identifier_columns: ['id'],
      row_filters: [{ name: 'big', sql: 'amount > 100' }],
    } as unknown as Awaited<ReturnType<typeof factTablesApi.get>>)
    vi.mocked(factTablesApi.create).mockResolvedValue({
      id: 'ft-3',
    } as unknown as Awaited<ReturnType<typeof factTablesApi.create>>)
    renderList()

    const trigger = await screen.findByRole('button', { name: 'Actions for Orders' })
    fireEvent.keyDown(trigger, { key: 'Enter' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Duplicate' }))

    await waitFor(() =>
      expect(factTablesApi.create).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({
          name: 'orders_copy_2',
          display_name: 'Orders (copy)',
          sql: 'SELECT id, created_at FROM orders',
          identifier_columns: ['id'],
          row_filters: [{ name: 'big', sql: 'amount > 100' }],
        }),
      ),
    )
    expect(await screen.findByText('fact table editor')).toBeInTheDocument()
  })
})
