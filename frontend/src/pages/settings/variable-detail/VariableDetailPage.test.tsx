import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation, useParams } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext } from '@/components/auth-context'
import { eventsApi } from '@/api/events'
import { variablesApi } from '@/api/variables'
import { variableDriftsApi } from '@/api/variableDrifts'
import { variableOverridesApi } from '@/api/variableOverrides'
import type { Variable } from '@/types'
import { VariableDetailPage } from './VariableDetailPage'
import { variableDetailPath, variableListPath } from './variableDetailPath'
import { personaAuth, type Persona } from '@/test/persona'
import { PersonaProject } from '@/test/PersonaProject'

vi.mock('@/api/variables', () => ({
  variablesApi: {
    list: vi.fn(),
    update: vi.fn(),
    values: vi.fn(),
    clearValues: vi.fn(),
  },
}))

vi.mock('@/api/variableDrifts', () => ({
  variableDriftsApi: { list: vi.fn(), action: vi.fn() },
}))

vi.mock('@/api/variableOverrides', () => ({
  variableOverridesApi: { list: vi.fn(), upsert: vi.fn(), del: vi.fn() },
}))

vi.mock('@/api/events', () => ({
  eventsApi: { list: vi.fn() },
}))

// The Notes card (F24) asks which docs link to the variable.
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

function PageRoute() {
  const { slug, id } = useParams<{ slug: string; id: string }>()
  return <VariableDetailPage slug={slug!} variableId={id!} />
}

function ListProbe() {
  const location = useLocation()
  return <p>list at {location.search}</p>
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
              <Route path="/p/:slug/variables" element={<ListProbe />} />
            </Routes>
          </MemoryRouter>
        </PersonaProject>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

const VARIANT = makeVariable({
  id: 'var-1',
  name: 'variant',
  description: 'Experiment arm',
  allowed_values: ['a', 'b'],
  bindings: ['page_data.variant'],
  open_drift_count: 1,
  context_count: 2,
})

beforeEach(() => {
  vi.mocked(variablesApi.list).mockResolvedValue([VARIANT])
  vi.mocked(variablesApi.values).mockResolvedValue([])
  vi.mocked(variableOverridesApi.list).mockResolvedValue([])
  vi.mocked(variableDriftsApi.list).mockResolvedValue({ items: [], total: 0 })
  vi.mocked(eventsApi.list).mockResolvedValue({ items: [] as never, total: 0 })
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('variableDetailPath', () => {
  it('builds the page, tab and back-to-list addresses', () => {
    expect(variableDetailPath('demo', 'var-1')).toBe('/p/demo/variables/var-1')
    expect(variableDetailPath('demo', 'var-1', 'definition')).toBe('/p/demo/variables/var-1')
    expect(variableDetailPath('demo', 'var-1', 'drift')).toBe('/p/demo/variables/var-1?tab=drift')
    expect(variableListPath('demo', 'var-1')).toBe('/p/demo/variables?focus=var-1')
  })
})

describe('VariableDetailPage (AU-26)', () => {
  it('titles the page after the variable and opens on its definition', async () => {
    renderPage('/p/demo/variables/var-1')

    expect(await screen.findByRole('heading', { level: 1, name: '${variant}' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Definition' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByLabelText('Name')).toHaveValue('variant')
    // Nothing to save until something changes.
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled()
    expect(screen.getByText('No changes')).toBeInTheDocument()
  })

  it('saves the definition and stays on the page', async () => {
    vi.mocked(variablesApi.update).mockResolvedValue(VARIANT)
    renderPage('/p/demo/variables/var-1')

    fireEvent.change(await screen.findByLabelText('Description'), { target: { value: 'Arm of the test' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() =>
      expect(variablesApi.update).toHaveBeenCalledWith(
        'demo',
        'var-1',
        {
          name: 'variant',
          variable_type: 'string',
          description: 'Arm of the test',
          allowed_values: ['a', 'b'],
          bindings: ['page_data.variant'],
        },
        null,
      ),
    )
    expect(screen.getByRole('heading', { level: 1, name: '${variant}' })).toBeInTheDocument()
  })

  it('opens the tab named in the URL and says when there is no drift', async () => {
    renderPage('/p/demo/variables/var-1?tab=drift')

    expect(await screen.findByText('No value drift')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /Drift/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('shows a viewer the definition as a read view, with nothing that writes', async () => {
    renderPage('/p/demo/variables/var-1', 'viewer')

    await screen.findByRole('heading', { level: 1, name: '${variant}' })
    expect(screen.getByRole('note')).toHaveTextContent(/viewer role/)
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.getByText('Name').tagName).toBe('DT')
    expect(screen.queryByRole('button', { name: 'Save changes' })).not.toBeInTheDocument()
  })

  it('offers the way back to the list, with the row focused', async () => {
    renderPage('/p/demo/variables/var-1')

    fireEvent.click(await screen.findByRole('link', { name: 'Variables' }))
    expect(await screen.findByText('list at ?focus=var-1')).toBeInTheDocument()
  })

  it('says the variable is gone, with the way back, when the list has no such id', async () => {
    renderPage('/p/demo/variables/var-missing')

    expect(await screen.findByText('Variable not found')).toBeInTheDocument()
    const back = screen.getByRole('link', { name: 'Back to variables' })
    expect(back).toHaveAttribute('href', '/p/demo/variables')
  })

  it('lists overrides on their own tab, read-only for a viewer', async () => {
    vi.mocked(variableOverridesApi.list).mockResolvedValue([
      {
        id: 'ov-1',
        variable_id: 'var-1',
        event_id: 'ev-1',
        event_name: 'checkout',
        values: ['c'],
      } as never,
    ])
    renderPage('/p/demo/variables/var-1?tab=overrides', 'viewer')

    const panel = await screen.findByRole('tabpanel')
    expect(await within(panel).findByText('checkout')).toBeInTheDocument()
    expect(within(panel).queryByRole('button', { name: 'Save override' })).not.toBeInTheDocument()
    expect(within(panel).queryByRole('button', { name: /Delete override/ })).not.toBeInTheDocument()
  })
})
