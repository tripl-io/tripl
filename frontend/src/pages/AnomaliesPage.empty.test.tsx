import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import type { DataSource, Role, ScanConfig } from '@/types'
import AnomaliesPage from './AnomaliesPage'

vi.mock('@/api/eventMetrics', () => ({
  eventMetricsApi: { getActiveSignals: vi.fn(), getSignalSeries: vi.fn() },
}))
vi.mock('@/api/scans', () => ({
  scansApi: { list: vi.fn() },
}))
vi.mock('@/api/sourceFreshness', () => ({
  sourceFreshnessApi: { list: vi.fn() },
}))
vi.mock('@/api/dataSources', () => ({
  dataSourcesApi: { list: vi.fn() },
}))

import { eventMetricsApi } from '@/api/eventMetrics'
import { scansApi } from '@/api/scans'
import { sourceFreshnessApi } from '@/api/sourceFreshness'
import { dataSourcesApi } from '@/api/dataSources'

function authAs(role: Role): AuthContextValue {
  return {
    user: {
      id: 'user-1',
      email: 'someone@example.com',
      name: 'Someone',
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

function renderAnomalies(role: Role) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={authAs(role)}>
        <MemoryRouter initialEntries={['/p/demo/anomalies']}>
          <Routes>
            <Route path="/p/:slug/anomalies" element={<AnomaliesPage />} />
          </Routes>
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(eventMetricsApi.getActiveSignals).mockReset()
  vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([])
  vi.mocked(eventMetricsApi.getSignalSeries).mockReset()
  vi.mocked(eventMetricsApi.getSignalSeries).mockResolvedValue([])
  // No scan collects volume: monitoring is off.
  vi.mocked(scansApi.list).mockReset()
  vi.mocked(scansApi.list).mockResolvedValue([] as ScanConfig[])
  vi.mocked(sourceFreshnessApi.list).mockReset()
  vi.mocked(sourceFreshnessApi.list).mockResolvedValue([])
  // ...and the project has no data source to scan from.
  vi.mocked(dataSourcesApi.list).mockReset()
  vi.mocked(dataSourcesApi.list).mockResolvedValue([] as DataSource[])
})

// "Run a scan" opened a Scans page with nothing to scan from: with no source,
// connecting one is the step, and it is an owner's.
describe('AnomaliesPage — monitoring off in a project with no data source', () => {
  it('sends an owner to connect a data source instead of running a scan', async () => {
    renderAnomalies('owner')

    expect(
      await screen.findByRole('link', { name: 'Connect a data source' }),
    ).toHaveAttribute('href', '/settings/data-sources')
    expect(screen.queryByRole('link', { name: 'Go to Scans' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Monitoring isn’t running yet' })).toBeInTheDocument()
  })

  it('tells anyone else an owner has to connect one, with no dead-end button', async () => {
    renderAnomalies('member')

    expect(
      await screen.findByText(/An owner has to connect a data source first\./),
    ).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByRole('link', { name: 'Go to Scans' })).not.toBeInTheDocument(),
    )
    expect(screen.queryByRole('link', { name: 'Connect a data source' })).not.toBeInTheDocument()
  })
})
