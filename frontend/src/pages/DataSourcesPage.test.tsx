import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MutationCache, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { toast } from 'sonner'
import { surfaceMutationError } from '@/lib/errorFeedback'
import { MemoryRouter, Route, RouterProvider, Routes, createMemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { SettingsLayout } from '@/components/settings/SettingsLayout'
import DataSourcesPage from './DataSourcesPage'
import type { DataSource, Role } from '@/types'

const DATA_SOURCE: DataSource = {
  id: 'ds-1',
  name: 'Warehouse',
  db_type: 'clickhouse',
  is_synthetic: false,
  host: 'localhost',
  port: 8123,
  database_name: 'analytics',
  username: 'default',
  password_set: false,
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
  },
  last_test_at: null,
  last_test_status: null,
  last_test_message: null,
  created_at: '2026-04-01T09:00:00Z',
  updated_at: '2026-04-10T09:00:00Z',
}

const BIGQUERY_SOURCE: DataSource = {
  ...DATA_SOURCE,
  id: 'ds-bq',
  name: 'Warehouse BQ',
  db_type: 'bigquery',
  // A BigQuery source stores the GCP project in `host` and the default dataset
  // in `database_name`; `port`/`username` are meaningless (the adapter deletes
  // them) but a row still carries whatever the create call defaulted them to.
  host: 'my-gcp-project',
  port: 8123,
  database_name: 'analytics',
  username: '',
  password_set: true,
  connection_settings: {
    ...DATA_SOURCE.connection_settings,
    location: 'EU',
    maximum_bytes_billed: 5_000_000,
    dataset_allowlist: ['analytics', 'marts'],
  },
}

const POSTGRES_SOURCE: DataSource = {
  ...DATA_SOURCE,
  id: 'ds-pg',
  name: 'Warehouse PG',
  db_type: 'postgres',
  host: 'pg.example.com',
  port: 5432,
  username: 'reader',
  password_set: true,
  timeout_seconds: 90,
  connection_settings: {
    ...DATA_SOURCE.connection_settings,
    sslmode: 'verify-full',
    sslrootcert: '-----BEGIN CERTIFICATE-----ca',
    sslcert: '-----BEGIN CERTIFICATE-----client',
    search_path: 'public, analytics',
    sslkey_set: true,
  },
}

const SYNTHETIC_SOURCE: DataSource = {
  ...DATA_SOURCE,
  id: 'ds-demo',
  name: 'Demo source',
  db_type: 'synthetic',
  is_synthetic: true,
  host: '',
  port: 0,
  database_name: '',
  username: '',
}

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function listFetchMock(sources: DataSource[]) {
  return (input: RequestInfo | URL) => {
    const url =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url

    if (url.endsWith('/api/v1/data-sources')) {
      return Promise.resolve(jsonResponse(sources))
    }

    return Promise.reject(new Error(`Unexpected request: ${url}`))
  }
}

/** Lists `source`, and captures the body of the PATCH the edit dialog sends for it. */
function editFetchMock(source: DataSource, onPatch: (payload: Record<string, unknown>) => void) {
  return (input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url

    if (url.endsWith('/api/v1/data-sources') && !init?.method) {
      return Promise.resolve(jsonResponse([source]))
    }

    if (url.endsWith(`/api/v1/data-sources/${source.id}`) && init?.method === 'PATCH') {
      onPatch(JSON.parse(String(init.body)) as Record<string, unknown>)
      return Promise.resolve(jsonResponse(source))
    }

    return Promise.reject(new Error(`Unexpected request: ${url}`))
  }
}

const DAY_MS = 24 * 60 * 60 * 1000

function LocationProbe() {
  const location = useLocation()
  return <span data-testid="location">{location.pathname}</span>
}

function authValue(role: Role): AuthContextValue {
  return {
    user: {
      id: `${role}-1`,
      email: `${role}@example.com`,
      name: role,
      role,
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
}

function renderDataSourcesPage(
  path = '/settings/data-sources/ds-1',
  role: Role = 'owner',
  queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  }),
) {

  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={authValue(role)}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route
              path="/settings/data-sources"
              element={(
                <>
                  <DataSourcesPage />
                  <LocationProbe />
                </>
              )}
            />
            <Route
              path="/settings/data-sources/:dsId"
              element={(
                <>
                  <DataSourcesPage />
                  <LocationProbe />
                </>
              )}
            />
          </Routes>
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})


// The summary strip (a <dl>) uses the same Title Case words as the per-card
// health chip, so card assertions skip the strip.
function healthLabels(label: string): HTMLElement[] {
  return screen.queryAllByText(label).filter((el) => el.closest('dl') === null)
}

describe('DataSourcesPage', () => {
  it('shows skeletons, not the empty state, while the list is in flight', async () => {
    // An unsettled list query used to fall through to `dataSources.length === 0`
    // and tell an operator who has connections that they have none, with an
    // "Add connection" CTA — the false-empty class ScanDetail already fixed.
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => new Promise(() => {}))

    renderDataSourcesPage('/settings/data-sources')

    expect(await screen.findByLabelText('Loading data sources')).toBeInTheDocument()
    expect(screen.queryByText('No data sources')).not.toBeInTheDocument()
    // Nor may the header claim measurements it does not have yet.
    expect(screen.queryByText('0')).not.toBeInTheDocument()
  })

  it('closes a directly opened edit dialog on cancel', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/data-sources')) {
        return Promise.resolve(jsonResponse([DATA_SOURCE]))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderDataSourcesPage()

    expect(await screen.findByRole('dialog', { name: 'Edit data source' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: 'Edit data source' })).not.toBeInTheDocument()
    })
    expect(screen.getByTestId('location')).toHaveTextContent('/settings/data-sources')
  })

  it('closes a directly opened edit dialog after save', async () => {
    let patchPayload: Record<string, unknown> | undefined

    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/data-sources') && !init?.method) {
        return Promise.resolve(jsonResponse([DATA_SOURCE]))
      }

      if (url.endsWith('/api/v1/data-sources/ds-1') && init?.method === 'PATCH') {
        patchPayload = JSON.parse(String(init.body)) as Record<string, unknown>
        return Promise.resolve(jsonResponse(DATA_SOURCE))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderDataSourcesPage()

    expect(await screen.findByRole('dialog', { name: 'Edit data source' })).toBeInTheDocument()
    fireEvent.change(screen.getByPlaceholderText('Default'), { target: { value: '120' } })

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: 'Edit data source' })).not.toBeInTheDocument()
    })
    expect(screen.getByTestId('location')).toHaveTextContent('/settings/data-sources')
    expect(patchPayload?.timeout_seconds).toBe(120)
  })

  it('keeps data source management controls owner-only', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/data-sources')) {
        return Promise.resolve(jsonResponse([DATA_SOURCE]))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderDataSourcesPage('/settings/data-sources/ds-1', 'member')

    expect(await screen.findByText('Warehouse')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByTestId('location')).toHaveTextContent('/settings/data-sources')
    })
    expect(screen.queryByRole('button', { name: 'Add connection' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Test' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
  })

  it('shows a failed delete on the card it failed for, and keeps the app-wide toast quiet', async () => {
    const OTHER_SOURCE: DataSource = { ...DATA_SOURCE, id: 'ds-2', name: 'Replica' }
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/data-sources') && !init?.method) {
        return Promise.resolve(jsonResponse([DATA_SOURCE, OTHER_SOURCE]))
      }
      if (url.endsWith('/api/v1/data-sources/ds-1') && init?.method === 'DELETE') {
        return Promise.resolve(jsonResponse({ detail: 'Data source is in use' }, 409))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })
    // The backstop main.tsx registers, so the test sees what the app does.
    const queryClient = new QueryClient({
      mutationCache: new MutationCache({ onError: surfaceMutationError }),
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    const toastError = vi.spyOn(toast, 'error')

    renderDataSourcesPage('/settings/data-sources', 'owner', queryClient)

    fireEvent.click(await screen.findByRole('button', { name: 'Delete data source Warehouse' }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Delete data source' })
    fireEvent.click(within(confirm).getByRole('button', { name: 'Delete' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Could not delete Warehouse: Data source is in use')
    // Only the card that failed says so.
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(toastError).not.toHaveBeenCalled()
  })

  it('shows what reads a source, and asks for its name before deleting one in use', async () => {
    const usedSource: DataSource = { ...DATA_SOURCE, scan_count: 2, scan_run_count: 5 }
    const deleted = vi.fn()
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/data-sources') && !init?.method) {
        return Promise.resolve(jsonResponse([usedSource]))
      }
      if (url.endsWith('/api/v1/data-sources/ds-1') && init?.method === 'DELETE') {
        deleted()
        return Promise.resolve(new Response(null, { status: 204 }))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderDataSourcesPage('/settings/data-sources', 'owner')

    expect(await screen.findByText('Used by 2 scans')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Delete data source Warehouse' }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Delete data source' })
    expect(confirm).toHaveTextContent('2 scans and 5 runs will be removed with it.')
    const deleteButton = within(confirm).getByRole('button', { name: 'Delete' })
    expect(deleteButton).toBeDisabled()

    fireEvent.change(within(confirm).getByLabelText(/type warehouse to confirm/i), {
      target: { value: 'Warehouse' },
    })
    expect(deleteButton).toBeEnabled()
    fireEvent.click(deleteButton)

    await waitFor(() => expect(deleted).toHaveBeenCalledTimes(1))
  })

  it('links each scan that reads a source to its scan page', async () => {
    const usedSource: DataSource = {
      ...DATA_SOURCE,
      scan_count: 3,
      scan_run_count: 9,
      scans: [
        { id: 'scan-a', name: 'App events', project_slug: 'app', project_name: 'App' },
        { id: 'scan-b', name: 'Web events', project_slug: 'web', project_name: 'Web' },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(listFetchMock([usedSource]))

    renderDataSourcesPage('/settings/data-sources', 'owner')

    const appLink = await screen.findByRole('link', { name: 'App events' })
    expect(appLink).toHaveAttribute('href', '/p/app/scans/scan-a')
    expect(screen.getByRole('link', { name: 'Web events' })).toHaveAttribute('href', '/p/web/scans/scan-b')
    // Two projects, so each link names its own; the third scan is past the cap.
    expect(screen.getByText('(Web)')).toBeInTheDocument()
    expect(screen.getByText(/Used by 3 scans/)).toHaveTextContent(
      'Used by 3 scans: App events (App), Web events (Web) and 1 more',
    )
  })

  it('flags an old successful health check as stale instead of confident "healthy"', async () => {
    const staleSource: DataSource = {
      ...DATA_SOURCE,
      last_test_status: 'success',
      last_test_message: 'Connection successful',
      last_test_at: new Date(Date.now() - 60 * DAY_MS).toISOString(),
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(listFetchMock([staleSource]))

    renderDataSourcesPage('/settings/data-sources', 'owner')

    expect(await screen.findByText('Warehouse')).toBeInTheDocument()
    expect(healthLabels('Stale')).toHaveLength(1)
    expect(healthLabels('Healthy')).toHaveLength(0)
    expect(screen.getByText(/Last checked/)).toBeInTheDocument()
    expect(screen.getByText('re-test to confirm')).toBeInTheDocument()
  })

  it('hides the connection line entirely when the server redacted it', async () => {
    // A non-owner gets host/port/database_name blanked by the API.
    // The card used to render that as a bare ":0/"
    // — it must show nothing instead, while still identifying
    // the source by name, type and health.
    const redacted: DataSource = {
      ...DATA_SOURCE,
      host: '',
      port: 0,
      database_name: '',
      username: '',
      password_set: false,
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(listFetchMock([redacted]))

    renderDataSourcesPage('/settings/data-sources', 'member')

    expect(await screen.findByText('Warehouse')).toBeInTheDocument()
    expect(screen.queryByText(/:0\//)).not.toBeInTheDocument()
    expect(screen.getByText('clickhouse')).toBeInTheDocument()
  })

  it('presents a recent successful health check as healthy', async () => {
    const freshSource: DataSource = {
      ...DATA_SOURCE,
      last_test_status: 'success',
      last_test_message: 'Connection successful',
      last_test_at: new Date(Date.now() - 60 * 1000).toISOString(),
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(listFetchMock([freshSource]))

    renderDataSourcesPage('/settings/data-sources', 'owner')

    expect(await screen.findByText('Warehouse')).toBeInTheDocument()
    expect(healthLabels('Healthy')).toHaveLength(1)
    expect(healthLabels('Stale')).toHaveLength(0)
    expect(screen.getByText('Connection successful')).toBeInTheDocument()
    // A confidently healthy source does not need recovery affordances.
    expect(screen.queryByRole('button', { name: 'Re-test connection' })).not.toBeInTheDocument()
  })

  it('surfaces inline recovery actions on a failed source and re-tests on click', async () => {
    const failedSource: DataSource = {
      ...DATA_SOURCE,
      last_test_status: 'failed',
      last_test_message: 'Connection refused',
      last_test_at: new Date(Date.now() - 60 * 1000).toISOString(),
    }
    const recovered: DataSource = {
      ...failedSource,
      last_test_status: 'success',
      last_test_message: 'Connection successful',
      last_test_at: new Date().toISOString(),
    }
    let testCalls = 0

    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/data-sources') && !init?.method) {
        return Promise.resolve(jsonResponse([failedSource]))
      }

      if (url.endsWith('/api/v1/data-sources/ds-1/test') && init?.method === 'POST') {
        testCalls += 1
        return Promise.resolve(
          jsonResponse({
            success: true,
            message: 'Connection successful',
            tested_at: recovered.last_test_at,
            data_source: recovered,
          }),
        )
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderDataSourcesPage('/settings/data-sources', 'owner')

    expect(await screen.findByText('Connection refused')).toBeInTheDocument()
    const retest = screen.getByRole('button', { name: 'Re-test connection' })
    expect(retest).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit connection' })).toBeInTheDocument()

    fireEvent.click(retest)

    await waitFor(() => {
      expect(testCalls).toBe(1)
    })
    expect(await screen.findByText('Connection successful')).toBeInTheDocument()
    // Once recovered, the inline re-test affordance is no longer needed.
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Re-test connection' })).not.toBeInTheDocument()
    })
  })

  it('surfaces a re-test recovery action on a stale source', async () => {
    const staleSource: DataSource = {
      ...DATA_SOURCE,
      last_test_status: 'success',
      last_test_message: 'Connection successful',
      last_test_at: new Date(Date.now() - 60 * DAY_MS).toISOString(),
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(listFetchMock([staleSource]))

    renderDataSourcesPage('/settings/data-sources', 'owner')

    expect(await screen.findByText('Warehouse')).toBeInTheDocument()
    expect(healthLabels('Stale')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Re-test connection' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit connection' })).toBeInTheDocument()
  })

  it('renders the JSON path discovery select for a ClickHouse create form and submits its value', async () => {
    let postPayload: Record<string, unknown> | undefined

    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/data-sources') && !init?.method) {
        return Promise.resolve(jsonResponse([DATA_SOURCE]))
      }

      if (url.endsWith('/api/v1/data-sources') && init?.method === 'POST') {
        postPayload = JSON.parse(String(init.body)) as Record<string, unknown>
        return Promise.resolve(jsonResponse(DATA_SOURCE))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderDataSourcesPage('/settings/data-sources', 'owner')

    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }))

    // ClickHouse is the default db type, so the CH-only discovery select shows.
    const discovery = await screen.findByLabelText('JSON path discovery')
    expect(discovery).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Prod CH' } })
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'ch.example.com' } })
    fireEvent.change(screen.getByLabelText('Database'), { target: { value: 'analytics' } })
    fireEvent.change(discovery, { target: { value: 'all' } })

    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => {
      expect(postPayload?.json_path_discovery).toBe('all')
    })
  })

  it('defaults the create payload json_path_discovery to "dynamic"', async () => {
    let postPayload: Record<string, unknown> | undefined

    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/data-sources') && !init?.method) {
        return Promise.resolve(jsonResponse([DATA_SOURCE]))
      }

      if (url.endsWith('/api/v1/data-sources') && init?.method === 'POST') {
        postPayload = JSON.parse(String(init.body)) as Record<string, unknown>
        return Promise.resolve(jsonResponse(DATA_SOURCE))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderDataSourcesPage('/settings/data-sources', 'owner')

    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }))
    await screen.findByLabelText('JSON path discovery')

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Prod CH' } })
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'ch.example.com' } })
    fireEvent.change(screen.getByLabelText('Database'), { target: { value: 'analytics' } })

    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => {
      expect(postPayload?.json_path_discovery).toBe('dynamic')
    })
  })

  it('includes json_path_discovery in the edit/update payload for a ClickHouse source', async () => {
    let patchPayload: Record<string, unknown> | undefined

    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/data-sources') && !init?.method) {
        return Promise.resolve(jsonResponse([DATA_SOURCE]))
      }

      if (url.endsWith('/api/v1/data-sources/ds-1') && init?.method === 'PATCH') {
        patchPayload = JSON.parse(String(init.body)) as Record<string, unknown>
        return Promise.resolve(jsonResponse(DATA_SOURCE))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderDataSourcesPage()

    expect(await screen.findByRole('dialog', { name: 'Edit data source' })).toBeInTheDocument()
    const discovery = screen.getByLabelText('JSON path discovery')
    expect(discovery).toBeInTheDocument()
    fireEvent.change(discovery, { target: { value: 'all' } })

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(patchPayload?.json_path_discovery).toBe('all')
    })
  })

  it('omits json_path_discovery from a BigQuery create payload', async () => {
    let postPayload: Record<string, unknown> | undefined

    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/data-sources') && !init?.method) {
        return Promise.resolve(jsonResponse([DATA_SOURCE]))
      }

      if (url.endsWith('/api/v1/data-sources') && init?.method === 'POST') {
        postPayload = JSON.parse(String(init.body)) as Record<string, unknown>
        return Promise.resolve(jsonResponse(DATA_SOURCE))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderDataSourcesPage('/settings/data-sources', 'owner')

    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }))
    fireEvent.change(await screen.findByLabelText('Type'), { target: { value: 'bigquery' } })

    // The ClickHouse-only discovery control must disappear for BigQuery.
    expect(screen.queryByLabelText('JSON path discovery')).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'BQ' } })
    fireEvent.change(screen.getByLabelText('Project ID'), { target: { value: 'gcp-proj' } })
    fireEvent.change(screen.getByLabelText('Default dataset'), { target: { value: 'analytics' } })
    fireEvent.change(screen.getByLabelText('Service account JSON'), {
      target: { value: '{"type":"service_account"}' },
    })

    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => {
      expect(postPayload).toBeDefined()
    })
    expect(postPayload).not.toHaveProperty('json_path_discovery')
  })

  it('edits a BigQuery source as project / dataset / key — never as host, port or username', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(listFetchMock([BIGQUERY_SOURCE]))

    renderDataSourcesPage('/settings/data-sources/ds-bq', 'owner')

    expect(await screen.findByRole('dialog', { name: 'Edit data source' })).toBeInTheDocument()

    // The BigQuery vocabulary, matching the create dialog.
    expect(screen.getByLabelText('Project ID')).toHaveValue('my-gcp-project')
    expect(screen.getByLabelText('Default dataset')).toHaveValue('analytics')

    // The service-account key is a JSON document, so it needs a textarea — not
    // the single-line type=password input the edit dialog used to cram it into.
    const key = screen.getByLabelText('Service account JSON')
    expect(key.tagName).toBe('TEXTAREA')

    // None of these exist for BigQuery: the adapter deletes port and username.
    expect(screen.queryByLabelText('Host')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Port')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Username')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
    // ClickHouse-only control.
    expect(screen.queryByLabelText('JSON path discovery')).not.toBeInTheDocument()

    // The BigQuery connection settings and the (universal) timeout are still there.
    expect(screen.getByLabelText('Location')).toHaveValue('EU')
    expect(screen.getByLabelText('Max billed bytes')).toHaveValue(5_000_000)
    expect(screen.getByLabelText('Dataset allowlist')).toHaveValue('analytics, marts')
    expect(screen.getByLabelText('Timeout (seconds)')).toBeInTheDocument()
  })

  it('never pre-fills the BigQuery key and keeps the stored one when the form is untouched', async () => {
    let patchPayload: Record<string, unknown> | undefined

    vi.spyOn(globalThis, 'fetch').mockImplementation(
      editFetchMock(BIGQUERY_SOURCE, (payload) => { patchPayload = payload }),
    )

    renderDataSourcesPage('/settings/data-sources/ds-bq', 'owner')

    expect(await screen.findByRole('dialog', { name: 'Edit data source' })).toBeInTheDocument()

    // The API returns `password_set`, never the key itself — so the field is
    // empty and the form says so rather than echoing a fake secret.
    expect(screen.getByLabelText('Service account JSON')).toHaveValue('')
    expect(screen.getByText(/Service account key: set/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(patchPayload).toBeDefined()
    })
    // An untouched secret is not sent at all — an omitted password keeps the stored one.
    expect(patchPayload).not.toHaveProperty('password')
    // And the fields BigQuery has no concept of are not written back either.
    expect(patchPayload).not.toHaveProperty('port')
    expect(patchPayload).not.toHaveProperty('username')
    expect(patchPayload?.host).toBe('my-gcp-project')
    expect(patchPayload?.database_name).toBe('analytics')
  })

  it('renames a synthetic source without sending locked connection fields', async () => {
    let patchPayload: Record<string, unknown> | undefined
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      editFetchMock(SYNTHETIC_SOURCE, (payload) => { patchPayload = payload }),
    )

    renderDataSourcesPage('/settings/data-sources/ds-demo', 'owner')

    expect(await screen.findByRole('dialog', { name: 'Edit data source' })).toBeInTheDocument()
    expect(screen.queryByLabelText('Host')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Port')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Renamed demo' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(patchPayload).toBeDefined())
    expect(patchPayload).toEqual({ name: 'Renamed demo', timeout_seconds: null })
  })

  it('sends a new BigQuery service account key only once the operator types one', async () => {
    let patchPayload: Record<string, unknown> | undefined

    vi.spyOn(globalThis, 'fetch').mockImplementation(
      editFetchMock(BIGQUERY_SOURCE, (payload) => { patchPayload = payload }),
    )

    renderDataSourcesPage('/settings/data-sources/ds-bq', 'owner')

    expect(await screen.findByRole('dialog', { name: 'Edit data source' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Service account JSON'), {
      target: { value: '{"type":"service_account","private_key":"rotated"}' },
    })

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(patchPayload?.password).toBe('{"type":"service_account","private_key":"rotated"}')
    })
  })

  it('shows a PostgreSQL source its TLS settings, search path and timeout on edit', async () => {
    let patchPayload: Record<string, unknown> | undefined

    vi.spyOn(globalThis, 'fetch').mockImplementation(
      editFetchMock(POSTGRES_SOURCE, (payload) => { patchPayload = payload }),
    )

    renderDataSourcesPage('/settings/data-sources/ds-pg', 'owner')

    expect(await screen.findByRole('dialog', { name: 'Edit data source' })).toBeInTheDocument()

    // PostgreSQL keeps the host/port/username triple — it really has one.
    expect(screen.getByLabelText('Host')).toHaveValue('pg.example.com')
    expect(screen.getByLabelText('Port')).toHaveValue(5432)
    expect(screen.getByLabelText('Username')).toHaveValue('reader')
    expect(screen.getByLabelText('Timeout (seconds)')).toHaveValue(90)
    expect(screen.queryByLabelText('JSON path discovery')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Project ID')).not.toBeInTheDocument()

    // The TLS settings, which the edit dialog must show for PostgreSQL.
    expect(screen.getByLabelText('SSL mode')).toHaveValue('verify-full')
    expect(screen.getByLabelText('CA certificate')).toHaveValue('-----BEGIN CERTIFICATE-----ca')
    expect(screen.getByLabelText('Client certificate')).toHaveValue(
      '-----BEGIN CERTIFICATE-----client',
    )
    expect(screen.getByLabelText('Search path')).toHaveValue('public, analytics')

    // Both write-only secrets come back empty, with a set/not-set indicator.
    expect(screen.getByLabelText('Password')).toHaveValue('')
    expect(screen.getByText(/Password: set/)).toBeInTheDocument()
    expect(screen.getByLabelText('Client private key')).toHaveValue('')
    expect(screen.getByText('Remove the stored client private key')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(patchPayload).toBeDefined()
    })
    // Neither secret is echoed back when it was left untouched.
    expect(patchPayload).not.toHaveProperty('password')
    expect(patchPayload?.connection_settings).not.toHaveProperty('sslkey')
    expect(patchPayload?.port).toBe(5432)
    expect(patchPayload?.connection_settings).toMatchObject({
      sslmode: 'verify-full',
      search_path: 'public, analytics',
    })
  })

  it('sends a new PostgreSQL password only once the operator types one', async () => {
    let patchPayload: Record<string, unknown> | undefined

    vi.spyOn(globalThis, 'fetch').mockImplementation(
      editFetchMock(POSTGRES_SOURCE, (payload) => { patchPayload = payload }),
    )

    renderDataSourcesPage('/settings/data-sources/ds-pg', 'owner')

    expect(await screen.findByRole('dialog', { name: 'Edit data source' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'rotated-secret' } })

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(patchPayload?.password).toBe('rotated-secret')
    })
  })

  it('summarises a BigQuery source as project/dataset, without a meaningless port', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(listFetchMock([BIGQUERY_SOURCE]))

    renderDataSourcesPage('/settings/data-sources', 'owner')

    expect(await screen.findByText('Warehouse BQ')).toBeInTheDocument()
    expect(screen.getByText('my-gcp-project/analytics')).toBeInTheDocument()
    expect(screen.queryByText('my-gcp-project:8123/analytics')).not.toBeInTheDocument()
  })

  it('hides inline recovery actions from members on a failed source', async () => {
    const failedSource: DataSource = {
      ...DATA_SOURCE,
      last_test_status: 'failed',
      last_test_message: 'Connection refused',
      last_test_at: new Date(Date.now() - 60 * 1000).toISOString(),
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(listFetchMock([failedSource]))

    renderDataSourcesPage('/settings/data-sources', 'member')

    expect(await screen.findByText('Connection refused')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Re-test connection' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit connection' })).not.toBeInTheDocument()
  })

  it('asks before Escape throws away a typed-in create form, and keeps it on Cancel', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      Promise.resolve(jsonResponse([DATA_SOURCE])),
    )
    renderDataSourcesPage('/settings/data-sources', 'owner')

    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }))
    const dialog = await screen.findByRole('dialog', { name: 'New data source' })
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Prod CH' } })

    fireEvent.keyDown(dialog, { key: 'Escape' })
    const confirm = await screen.findByRole('alertdialog', { name: 'Leave without saving?' })
    fireEvent.click(within(confirm).getByRole('button', { name: 'Keep editing' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(screen.getByRole('dialog', { name: 'New data source' })).toBeInTheDocument()
    expect(screen.getByLabelText('Name')).toHaveValue('Prod CH')
  })

  it('closes an untouched create form on Escape without asking', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      Promise.resolve(jsonResponse([DATA_SOURCE])),
    )
    renderDataSourcesPage('/settings/data-sources', 'owner')

    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }))
    const dialog = await screen.findByRole('dialog', { name: 'New data source' })
    fireEvent.keyDown(dialog, { key: 'Escape' })

    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'New data source' })).not.toBeInTheDocument(),
    )
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  // a stale "healthy" check renders amber on its card, so the header
  // must not read "Warnings 0" above it.
  it('counts stale health checks as warnings', async () => {
    const staleSource: DataSource = {
      ...DATA_SOURCE,
      last_test_status: 'success',
      last_test_message: 'Connection successful',
      last_test_at: new Date(Date.now() - 60 * DAY_MS).toISOString(),
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(listFetchMock([staleSource]))

    renderDataSourcesPage('/settings/data-sources', 'owner')

    expect(await screen.findByText('Warehouse')).toBeInTheDocument()
    const warnings = screen.getByText('Warnings').closest('dl')
    expect(warnings).toHaveTextContent('Warnings1')
    const healthy = screen.getByText('Healthy').closest('dl')
    expect(healthy).toHaveTextContent('Healthy0')
    expect(screen.getByRole('button', { name: 'Add connection' })).toBeInTheDocument()
  })

  // one health indicator and one type marker per card.
  it('shows one health marker and one type marker per card', async () => {
    const freshSynthetic: DataSource = {
      ...SYNTHETIC_SOURCE,
      last_test_status: 'success',
      last_test_message: 'Synthetic warehouse (demo)',
      last_test_at: new Date(Date.now() - 60 * 1000).toISOString(),
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(listFetchMock([freshSynthetic]))

    renderDataSourcesPage('/settings/data-sources', 'owner')

    expect(await screen.findByText('Demo source')).toBeInTheDocument()
    expect(healthLabels('Healthy')).toHaveLength(1)
    expect(screen.getByText('Synthetic')).toBeInTheDocument()
    expect(screen.queryByText('synthetic')).not.toBeInTheDocument()
  })

  it('still names the health of a source that was never tested', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(listFetchMock([DATA_SOURCE]))

    renderDataSourcesPage('/settings/data-sources', 'owner')

    expect(await screen.findByText('Warehouse')).toBeInTheDocument()
    expect(screen.getByText('Untested')).toBeInTheDocument()
    expect(screen.getByText('clickhouse')).toBeInTheDocument()
  })

  // The dialog must not read as a login form, and no secret
  // may go through the browser's spell checker.
  it('keeps browsers from autofilling or spell-checking the credentials', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(listFetchMock([DATA_SOURCE]))

    renderDataSourcesPage('/settings/data-sources', 'owner')

    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }))
    const username = await screen.findByLabelText('Username')
    const password = screen.getByLabelText('Password')
    expect(username).toHaveAttribute('autocomplete', 'off')
    expect(password).toHaveAttribute('autocomplete', 'new-password')
    expect(password).toHaveAttribute('data-1p-ignore')
    expect(password).toHaveAttribute('data-lpignore', 'true')

    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'postgres' } })
    for (const label of ['CA certificate', 'Client certificate', 'Client private key']) {
      expect(screen.getByLabelText(label)).toHaveAttribute('spellcheck', 'false')
    }

    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'bigquery' } })
    const key = screen.getByLabelText('Service account JSON')
    expect(key).toHaveAttribute('spellcheck', 'false')
    expect(key).toHaveAttribute('autocomplete', 'off')
  })

  it('rejects a malformed service-account key inline instead of saving it', async () => {
    let posted = false
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v1/data-sources') && init?.method === 'POST') {
        posted = true
        return Promise.resolve(jsonResponse(DATA_SOURCE))
      }
      if (url.endsWith('/api/v1/data-sources')) return Promise.resolve(jsonResponse([DATA_SOURCE]))
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderDataSourcesPage('/settings/data-sources', 'owner')

    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }))
    fireEvent.change(await screen.findByLabelText('Type'), { target: { value: 'bigquery' } })
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'BQ' } })
    fireEvent.change(screen.getByLabelText('Project ID'), { target: { value: 'gcp-proj' } })
    fireEvent.change(screen.getByLabelText('Default dataset'), { target: { value: 'analytics' } })
    const key = screen.getByLabelText('Service account JSON')

    // A partial paste.
    fireEvent.change(key, { target: { value: '{"type":"service_account","private' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/not valid JSON/)
    expect(key).toHaveAttribute('aria-invalid', 'true')

    // Valid JSON, but not a service-account key.
    fireEvent.change(key, { target: { value: '{"type":"authorized_user"}' } })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/service_account/)
    expect(posted).toBe(false)
  })

  it('rejects a PEM field that holds a path instead of the certificate', async () => {
    let posted = false
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v1/data-sources') && init?.method === 'POST') {
        posted = true
        return Promise.resolve(jsonResponse(DATA_SOURCE))
      }
      if (url.endsWith('/api/v1/data-sources')) return Promise.resolve(jsonResponse([DATA_SOURCE]))
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderDataSourcesPage('/settings/data-sources', 'owner')

    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }))
    fireEvent.change(await screen.findByLabelText('Type'), { target: { value: 'postgres' } })
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'PG' } })
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'pg.example.com' } })
    fireEvent.change(screen.getByLabelText('Database'), { target: { value: 'analytics' } })
    fireEvent.change(screen.getByLabelText('CA certificate'), {
      target: { value: '/etc/ssl/certs/ca.pem' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/BEGIN CERTIFICATE/)
    expect(screen.getByLabelText('CA certificate')).toHaveAttribute('aria-invalid', 'true')
    expect(posted).toBe(false)
  })

  it('tests an unsaved connection before Create, storing nothing', async () => {
    const bodies: unknown[] = []
    const posts: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST') posts.push(url)
      if (url.endsWith('/api/v1/data-sources/test') && init?.method === 'POST') {
        bodies.push(JSON.parse(String(init.body)))
        return Promise.resolve(
          jsonResponse({
            success: false,
            message: 'Connection test failed: could not reach the data source',
            tested_at: '2026-04-10T09:00:00Z',
          }),
        )
      }
      if (url.endsWith('/api/v1/data-sources')) return Promise.resolve(jsonResponse([DATA_SOURCE]))
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderDataSourcesPage('/settings/data-sources', 'owner')

    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Prod CH' } })
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'ch.exmaple.com' } })
    fireEvent.change(screen.getByLabelText('Database'), { target: { value: 'analytics' } })
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }))

    expect(await screen.findByText(/could not reach the data source/)).toHaveAttribute('role', 'status')
    expect(bodies).toHaveLength(1)
    expect(bodies[0]).toMatchObject({ db_type: 'clickhouse', host: 'ch.exmaple.com', database_name: 'analytics' })
    // Nothing was created.
    expect(posts.every(url => url.endsWith('/api/v1/data-sources/test'))).toBe(true)

    // The answer is for the inputs it was run with.
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'ch.example.com' } })
    expect(screen.queryByText(/could not reach the data source/)).toBeNull()
  })

  // a new source is also tested the moment it is saved, so its card
  // shows health right away instead of sitting "untested".
  it('tests a new connection as soon as it is created', async () => {
    const created: DataSource = { ...DATA_SOURCE, id: 'ds-new', name: 'Prod CH' }
    let sources: DataSource[] = [DATA_SOURCE]
    const tested: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v1/data-sources') && init?.method === 'POST') {
        sources = [DATA_SOURCE, created]
        return Promise.resolve(jsonResponse(created))
      }
      if (url.endsWith('/api/v1/data-sources')) return Promise.resolve(jsonResponse(sources))
      const test = /\/data-sources\/([^/]+)\/test$/.exec(url)
      if (test?.[1] && init?.method === 'POST') {
        tested.push(test[1])
        const result: DataSource = {
          ...created,
          last_test_status: 'failed',
          last_test_message: 'Host not found',
          last_test_at: new Date().toISOString(),
        }
        return Promise.resolve(
          jsonResponse({
            success: false,
            message: 'Host not found',
            tested_at: result.last_test_at,
            data_source: result,
          }),
        )
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderDataSourcesPage('/settings/data-sources', 'owner')

    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Prod CH' } })
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'ch.exmaple.com' } })
    fireEvent.change(screen.getByLabelText('Database'), { target: { value: 'analytics' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(tested).toEqual(['ds-new']))
    expect(await screen.findByText('Host not found')).toBeInTheDocument()
  })

  it('re-tests an edited source only when its connection changed', async () => {
    const tested: string[] = []
    const patchFetch = editFetchMock(DATA_SOURCE, () => {})
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v1/data-sources/ds-1/test') && init?.method === 'POST') {
        tested.push('ds-1')
        return Promise.resolve(
          jsonResponse({
            success: true,
            message: 'Connection successful',
            tested_at: new Date().toISOString(),
            data_source: DATA_SOURCE,
          }),
        )
      }
      return patchFetch(input, init)
    })

    renderDataSourcesPage()

    // A rename alone is not a connection change.
    expect(await screen.findByRole('dialog', { name: 'Edit data source' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Renamed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Edit data source' })).not.toBeInTheDocument(),
    )
    expect(tested).toEqual([])

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    fireEvent.change(await screen.findByLabelText('Host'), { target: { value: 'ch2.example.com' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(tested).toEqual(['ds-1']))
  })

  // resetForm/closeEdit never reset the mutations, so a reopened
  // dialog greeted the user with the previous attempt's error.
  it('does not show a stale create error when the dialog is reopened', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v1/data-sources') && init?.method === 'POST') {
        return Promise.resolve(jsonResponse({ detail: 'Host is unreachable' }, 400))
      }
      if (url.endsWith('/api/v1/data-sources')) return Promise.resolve(jsonResponse([DATA_SOURCE]))
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderDataSourcesPage('/settings/data-sources', 'owner')

    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Prod CH' } })
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'ch.example.com' } })
    fireEvent.change(screen.getByLabelText('Database'), { target: { value: 'analytics' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(await screen.findByText('Host is unreachable')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'New data source' })).not.toBeInTheDocument(),
    )

    fireEvent.click(screen.getByRole('button', { name: 'Add connection' }))
    expect(await screen.findByRole('dialog', { name: 'New data source' })).toBeInTheDocument()
    expect(screen.queryByText('Host is unreachable')).not.toBeInTheDocument()
  })

  // the edit name had no `required`; clearing it came back as a raw 422.
  it('stops an edit with a blank name inline, without a request', async () => {
    let patched = false
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      editFetchMock(DATA_SOURCE, () => { patched = true }),
    )

    renderDataSourcesPage()

    expect(await screen.findByRole('dialog', { name: 'Edit data source' })).toBeInTheDocument()
    const nameInput = screen.getByLabelText('Name')
    expect(nameInput).toBeRequired()
    fireEvent.change(nameInput, { target: { value: '   ' } })
    fireEvent.submit(nameInput.closest('form') as HTMLFormElement)

    expect(await screen.findByRole('alert')).toHaveTextContent('Enter a name.')
    expect(nameInput).toHaveAttribute('aria-invalid', 'true')
    expect(patched).toBe(false)
  })

  // one shared `testingId` re-enabled A's button when B started, and
  // A's finish re-enabled B's while B was still running.
  it('tracks each running connection test separately', async () => {
    const second: DataSource = { ...DATA_SOURCE, id: 'ds-2', name: 'Replica' }
    const pending = new Map<string, (response: Response) => void>()
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v1/data-sources') && !init?.method) {
        return Promise.resolve(jsonResponse([DATA_SOURCE, second]))
      }
      const test = /\/data-sources\/([^/]+)\/test$/.exec(url)
      if (test?.[1]) {
        const id = test[1]
        return new Promise<Response>((resolve) => pending.set(id, resolve))
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderDataSourcesPage('/settings/data-sources', 'owner')

    expect(await screen.findByText('Replica')).toBeInTheDocument()
    const [testA, testB] = screen.getAllByRole('button', { name: 'Test' })
    fireEvent.click(testA as HTMLElement)
    fireEvent.click(testB as HTMLElement)

    await waitFor(() => expect(pending.size).toBe(2))
    expect(screen.getAllByRole('button', { name: 'Testing…' })).toHaveLength(2)

    const done = (source: DataSource) =>
      jsonResponse({
        success: true,
        message: 'Connection successful',
        tested_at: new Date().toISOString(),
        data_source: {
          ...source,
          last_test_status: 'success',
          last_test_message: 'Connection successful',
          last_test_at: new Date().toISOString(),
        },
      })
    await act(async () => {
      pending.get('ds-1')?.(done(DATA_SOURCE))
    })

    // A finished; B is still running and stays disabled.
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Testing…' })).toHaveLength(1))
    expect(screen.getByRole('button', { name: 'Testing…' })).toBeDisabled()

    await act(async () => {
      pending.get('ds-2')?.(done(second))
    })
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Testing…' })).not.toBeInTheDocument(),
    )
  })

  describe('inside the settings takeover', () => {
    // A DATA router and the real shell: browser Back is only interceptable by
    // the shell's blocker, which is what the edit dialog registers with.
    function renderInTakeover() {
      const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
      })
      const page = (
        <SettingsLayout activePath="data-sources" backHref="/">
          <DataSourcesPage />
          <LocationProbe />
        </SettingsLayout>
      )
      const router = createMemoryRouter(
        [
          { path: '/settings/data-sources', element: page },
          { path: '/settings/data-sources/:dsId', element: page },
        ],
        { initialEntries: ['/settings/data-sources', '/settings/data-sources/ds-1'], initialIndex: 1 },
      )
      vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
        Promise.resolve(jsonResponse([DATA_SOURCE])),
      )
      render(
        <QueryClientProvider client={queryClient}>
          <AuthContext.Provider value={authValue('owner')}>
            <RouterProvider router={router} />
          </AuthContext.Provider>
        </QueryClientProvider>,
      )
      return router
    }

    it('asks before browser Back throws away a dirty edit, and keeps it on Cancel', async () => {
      const router = renderInTakeover()
      await screen.findByRole('dialog', { name: 'Edit data source' })
      fireEvent.change(screen.getByPlaceholderText('Default'), { target: { value: '120' } })

      await act(() => router.navigate(-1))
      const confirm = await screen.findByRole('alertdialog', { name: 'Leave without saving?' })
      fireEvent.click(within(confirm).getByRole('button', { name: 'Keep editing' }))

      await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
      expect(router.state.location.pathname).toBe('/settings/data-sources/ds-1')
      expect(screen.getByPlaceholderText('Default')).toHaveValue(120)
    })

    it("asks once, not twice, when the dialog's own Cancel discards the edit", async () => {
      const router = renderInTakeover()
      await screen.findByRole('dialog', { name: 'Edit data source' })
      fireEvent.change(screen.getByPlaceholderText('Default'), { target: { value: '120' } })

      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
      fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }))

      await waitFor(() => expect(router.state.location.pathname).toBe('/settings/data-sources'))
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
      expect(screen.queryByRole('dialog', { name: 'Edit data source' })).not.toBeInTheDocument()
    })
  })
})

describe('DataSourcesPage design review (#248)', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  // BigQuery has no port, so Postgres → BigQuery → ClickHouse used to
  // keep 5432 and the connection failed like a network problem.
  it('resets an untouched port to the new type default across BigQuery', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(listFetchMock([DATA_SOURCE]))
    renderDataSourcesPage('/settings/data-sources', 'owner')

    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }))
    const type = await screen.findByLabelText('Type')
    fireEvent.change(type, { target: { value: 'postgres' } })
    expect(screen.getByLabelText('Port')).toHaveValue(5432)
    fireEvent.change(type, { target: { value: 'bigquery' } })
    fireEvent.change(type, { target: { value: 'clickhouse' } })
    expect(screen.getByLabelText('Port')).toHaveValue(8123)

    // A port the user typed is theirs to keep.
    fireEvent.change(screen.getByLabelText('Port'), { target: { value: '9440' } })
    fireEvent.change(type, { target: { value: 'postgres' } })
    expect(screen.getByLabelText('Port')).toHaveValue(9440)
  })

  // a 422 used to land as "host: String should …" at the foot of the
  // dialog with nothing marked; it now sits under the control it names.
  it('pins a server field refusal under its control', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v1/data-sources') && init?.method === 'POST') {
        return Promise.resolve(
          jsonResponse(
            {
              detail: [
                { loc: ['body', 'host'], msg: 'Host must not include a scheme', type: 'value_error' },
              ],
            },
            422,
          ),
        )
      }
      if (url.endsWith('/api/v1/data-sources')) return Promise.resolve(jsonResponse([DATA_SOURCE]))
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })
    renderDataSourcesPage('/settings/data-sources', 'owner')

    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Prod CH' } })
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'https://ch.example.com' } })
    fireEvent.change(screen.getByLabelText('Database'), { target: { value: 'analytics' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    expect(await screen.findByText('Host must not include a scheme')).toBeInTheDocument()
    expect(screen.getByLabelText('Host')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.queryByText(/host: Host must not/)).not.toBeInTheDocument()

    // The refusal is for the value it was sent with.
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'ch.example.com' } })
    expect(screen.queryByText('Host must not include a scheme')).not.toBeInTheDocument()
  })

  // the card's two relative times are labelled, and the demo warehouse
  // shows no fake "synthetic:0/synthetic" address.
  it('labels the card times and hides the synthetic address', async () => {
    const tested: DataSource = {
      ...DATA_SOURCE,
      last_test_status: 'success',
      last_test_message: 'Connection successful',
      last_test_at: new Date(Date.now() - 60 * 60 * 1000).toISOString(),
    }
    const synthetic: DataSource = {
      ...SYNTHETIC_SOURCE,
      host: 'synthetic',
      database_name: 'synthetic',
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(listFetchMock([tested, synthetic]))
    renderDataSourcesPage('/settings/data-sources', 'owner')

    expect(await screen.findByText('Tested 1h ago')).toBeInTheDocument()
    expect(screen.getAllByText(/^Edited /)).toHaveLength(2)
    expect(screen.queryByText(/^synthetic:/)).not.toBeInTheDocument()
  })
})

describe('DataSourcesPage — source freshness on the card (F16, #269)', () => {
  it("shows the worst freshness of the scans reading the source, from each scan's project", async () => {
    const usedSource: DataSource = {
      ...DATA_SOURCE,
      scan_count: 2,
      scan_run_count: 4,
      scans: [
        { id: 'scan-a', name: 'App events', project_slug: 'app', project_name: 'App' },
        { id: 'scan-b', name: 'Web events', project_slug: 'web', project_name: 'Web' },
      ],
    }
    const freshnessRow = (id: string, name: string, status: string, lag: number) => ({
      id,
      name,
      data_source_id: DATA_SOURCE.id,
      freshness: {
        status,
        lag_seconds: lag,
        last_event_at: '2026-09-26T03:00:00Z',
        last_collection_at: '2026-09-26T09:55:00Z',
        expected_by: '2026-09-26T06:00:00Z',
      },
    })
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      if (url.endsWith('/api/v1/data-sources')) return Promise.resolve(jsonResponse([usedSource]))
      if (url.endsWith('/projects/app/source-freshness')) {
        return Promise.resolve(jsonResponse([freshnessRow('scan-a', 'App events', 'fresh', 600)]))
      }
      if (url.endsWith('/projects/web/source-freshness')) {
        return Promise.resolve(jsonResponse([freshnessRow('scan-b', 'Web events', 'late', 7 * 3600)]))
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderDataSourcesPage('/settings/data-sources', 'owner')

    const chip = await screen.findByText('Data late · 7h')
    expect(chip).toHaveAttribute('data-tone', 'warning')
    expect(chip.getAttribute('title')).toMatch(/^Web events: /)
  })
})
