import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ActiveProjectContext } from '@/components/active-project-context'
import { AuthContext } from '@/components/auth-context'
import { authAs } from '@/test/auth'
import type { DataSource, Project } from '@/types'
import FactTableEditPage from './FactTableForm'

vi.mock('@/api/dataSources', () => ({
  dataSourcesApi: { list: vi.fn() },
}))
vi.mock('@/api/factTables', () => ({
  factTablesApi: { get: vi.fn(), create: vi.fn(), update: vi.fn(), preview: vi.fn(), remove: vi.fn() },
}))
// CodeMirror needs layout jsdom lacks; a textarea stands in for it.
vi.mock('@uiw/react-codemirror', () => ({
  default: ({ value, 'aria-label': ariaLabel }: { value: string; 'aria-label'?: string }) => (
    <textarea aria-label={ariaLabel} value={value} readOnly />
  ),
}))
vi.mock('@/hooks/useDataSourceSchema', () => ({
  useDataSourceSchema: () => ({ data: undefined }),
}))

import { dataSourcesApi } from '@/api/dataSources'

// A brand-new project beside a demo one: the demo's synthetic warehouse is
// owned by the demo project, and the server refuses a fact table bound to it.
const PROJECT = { id: 'p-blank', slug: 'blank', can_mutate: true } as Project

let queryClient: QueryClient

function renderNewFactTable(auth = authAs('member')) {
  render(
    createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(
        AuthContext.Provider,
        { value: auth },
        createElement(
          ActiveProjectContext.Provider,
          { value: PROJECT },
          createElement(
            MemoryRouter,
            { initialEntries: ['/p/blank/metrics/fact-tables/new'] },
            createElement(
              Routes,
              null,
              createElement(Route, {
                path: '/p/:slug/metrics/fact-tables/new',
                element: createElement(FactTableEditPage),
              }),
            ),
          ),
        ),
      ),
    ),
  )
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.mocked(dataSourcesApi.list).mockReset()
})

afterEach(() => {
  queryClient.clear()
})

describe('FactTableEditPage data sources of this project', () => {
  it("does not offer another project's warehouse", async () => {
    vi.mocked(dataSourcesApi.list).mockResolvedValue([
      { id: 'ds-demo', name: 'Demo warehouse demo-0793b8', project_id: 'p-demo' },
      { id: 'ds-shared', name: 'Shared warehouse', project_id: null },
      { id: 'ds-own', name: 'Own warehouse', project_id: 'p-blank' },
    ] as unknown as DataSource[])
    renderNewFactTable()

    await screen.findByRole('heading', { name: 'New fact table' })
    const select = document.getElementById('fact-data-source')!
    expect([...select.querySelectorAll('option')].map(option => option.textContent)).toEqual([
      'Select data source…',
      'Shared warehouse',
      'Own warehouse',
    ])
  })

  it('says the project has none in place of the select, and still points the required message at it', async () => {
    vi.mocked(dataSourcesApi.list).mockResolvedValue([
      { id: 'ds-demo', name: 'Demo warehouse demo-0793b8', project_id: 'p-demo' },
    ] as unknown as DataSource[])
    renderNewFactTable()

    await screen.findByRole('heading', { name: 'New fact table' })
    expect(screen.getByText(/No data source in this project yet/)).toBeInTheDocument()
    expect(screen.getByText(/An owner has to connect one first/)).toBeInTheDocument()
    expect(document.querySelector('select#fact-data-source')).toBeNull()

    // With the name filled in, the data source is the first problem a save
    // finds, and focus goes to the line that stands in for its select.
    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Orders' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Create fact table' }))

    expect(await screen.findByText('A data source is required.', { selector: 'p' })).toBeInTheDocument()
    expect(document.getElementById('fact-data-source')).toHaveFocus()
  })

  it('gives an owner the way to connect one', async () => {
    vi.mocked(dataSourcesApi.list).mockResolvedValue([])
    renderNewFactTable(authAs('owner'))

    expect(await screen.findByRole('link', { name: 'Connect one' })).toHaveAttribute(
      'href',
      expect.stringContaining('/settings/data-sources'),
    )
  })
})
