import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import DataSourcesPage from './DataSourcesPage'
import type { DataSource } from '@/types'

// A Trino source stored with a password and then switched to plain HTTP: the
// adapter never sends a password over HTTP, so every test and scan fails, and
// the only way out used to be deleting the source.
const TRINO_SOURCE: DataSource = {
  id: 'ds-trino',
  name: 'Lake',
  db_type: 'trino',
  is_synthetic: false,
  host: 'trino.internal',
  port: 8080,
  database_name: 'hive',
  username: 'tripl',
  password_set: true,
  timeout_seconds: null,
  json_path_discovery: null,
  connection_settings: {
    location: null,
    maximum_bytes_billed: null,
    dataset_allowlist: null,
    sslmode: null,
    sslrootcert: null,
    sslcert: null,
    search_path: null,
    sslkey_set: false,
    http_scheme: 'http',
  },
  last_test_at: null,
  last_test_status: null,
  last_test_message: null,
  created_at: '2026-10-01T09:00:00Z',
  updated_at: '2026-10-01T09:00:00Z',
}

const OWNER: AuthContextValue = {
  user: {
    id: 'owner-1',
    email: 'owner@example.com',
    name: 'owner',
    role: 'owner',
    is_platform_admin: false,
    orgs: [],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
  status: 'authenticated',
  error: null,
  isLoggingOut: false,
  logout: async () => {},
  refresh: () => {},
}

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** Lists `source`; records each create and update body; answers a test as passed. */
function mockApi(source: DataSource) {
  const sent: { method: string; body: Record<string, unknown> }[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST' && url.endsWith('/test')) {
      return Promise.resolve(
        jsonResponse({
          success: true,
          message: 'Connection successful',
          tested_at: '2026-10-09T09:00:00Z',
          data_source: source,
        }),
      )
    }
    if (init?.method === 'POST' || init?.method === 'PATCH') {
      sent.push({ method: init.method, body: JSON.parse(String(init.body)) as Record<string, unknown> })
      return Promise.resolve(jsonResponse(source))
    }
    if (url.endsWith('/api/v1/data-sources')) return Promise.resolve(jsonResponse([source]))
    return Promise.reject(new Error(`Unexpected request: ${url}`))
  })
  return sent
}

function renderPage(path: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={OWNER}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/settings/data-sources" element={<DataSourcesPage />} />
            <Route path="/settings/data-sources/:dsId" element={<DataSourcesPage />} />
          </Routes>
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('DataSourcesPage sign-in checks', () => {
  it('refuses a stored Trino password over HTTP until it is removed, then sends its removal', async () => {
    const sent = mockApi(TRINO_SOURCE)
    renderPage('/settings/data-sources/ds-trino')

    expect(await screen.findByRole('dialog', { name: 'Edit data source' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    const password = screen.getByLabelText('Password')
    await waitFor(() => expect(password).toHaveAttribute('aria-invalid', 'true'))
    expect(password).toHaveAccessibleDescription(/only sent over HTTPS/)
    expect(sent).toEqual([])

    fireEvent.click(screen.getByLabelText('Remove the stored password'))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(sent).toHaveLength(1))
    expect(sent[0]).toMatchObject({ method: 'PATCH', body: { password: '' } })
  })

  it('creates a Trino source only once its user name is filled in', async () => {
    const sent = mockApi(TRINO_SOURCE)
    renderPage('/settings/data-sources')

    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }))
    fireEvent.change(await screen.findByLabelText('Type'), { target: { value: 'trino' } })
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Lake' } })
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'trino.internal' } })
    fireEvent.change(screen.getByLabelText('Catalog'), { target: { value: 'hive' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() =>
      expect(screen.getByLabelText('Username')).toHaveAttribute('aria-invalid', 'true'),
    )
    expect(sent).toEqual([])

    fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'tripl' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(sent).toHaveLength(1))
    expect(sent[0]).toMatchObject({ method: 'POST', body: { db_type: 'trino', username: 'tripl' } })
  })

  it('flags a qualified default schema under its field instead of sending it', async () => {
    const sent = mockApi(TRINO_SOURCE)
    renderPage('/settings/data-sources/ds-trino')

    expect(await screen.findByRole('dialog', { name: 'Edit data source' })).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Remove the stored password'))
    fireEvent.change(screen.getByLabelText('Default schema'), { target: { value: 'hive.events' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    const schema = screen.getByLabelText('Default schema')
    await waitFor(() => expect(schema).toHaveAttribute('aria-invalid', 'true'))
    expect(schema).toHaveAccessibleDescription('Use the name alone, like events, not hive.events.')
    expect(sent).toEqual([])
  })
})
