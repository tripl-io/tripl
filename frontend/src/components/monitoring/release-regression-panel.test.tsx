import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { eventMetricsApi } from '@/api/eventMetrics'
import type { ReleaseComparabilityItem, ReleaseRegressionItem } from '@/types'

import { ReleaseRegressionPanel } from './release-regression-panel'

vi.mock('@/api/eventMetrics', () => ({
  eventMetricsApi: { getReleaseRegressions: vi.fn() },
}))

function verdict(overrides: Partial<ReleaseComparabilityItem> = {}): ReleaseComparabilityItem {
  return {
    scope_type: 'event',
    comparable: true,
    reason: 'comparable',
    version: '2.1.0',
    previous_version: '2.0.0',
    emerging_share: 0.02,
    max_emerging_share: 0.25,
    ...overrides,
  }
}

function regression(overrides: Partial<ReleaseRegressionItem>): ReleaseRegressionItem {
  return {
    scope_type: 'event',
    scope_ref: 'e1',
    scope_name: 'Login',
    event_id: 'e1',
    event_type_id: null,
    kind: 'missing',
    version: '2.1.0',
    previous_version: '2.0.0',
    observed_count: 0,
    expected_count: 200,
    ratio: 0,
    share_prev: 0.1,
    share_new: 0,
    release_share: 0.33,
    window_from: '2026-01-01T10:00:00',
    window_to: '2026-01-01T11:00:00',
    ...overrides,
  }
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ReleaseRegressionPanel slug="demo" scanConfigId="scan-1" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ReleaseRegressionPanel', () => {
  it('lists regressed events with kind and version context', async () => {
    vi.mocked(eventMetricsApi.getReleaseRegressions).mockResolvedValue({
      scan_config_id: 'scan-1',
      app_version_column: 'app_version',
      latest_version: '2.1.0',
      comparability: [verdict()],
      items: [
        regression({ scope_ref: 'e1', scope_name: 'Login', kind: 'missing', ratio: 0 }),
        regression({
          scope_ref: 'e2',
          scope_name: 'Purchase',
          kind: 'volume_drop',
          observed_count: 40,
          expected_count: 100,
          ratio: 0.4,
        }),
      ],
    })

    renderPanel()

    expect(await screen.findByText('Login')).toBeInTheDocument()
    expect(screen.getByText('missing')).toBeInTheDocument()
    expect(screen.getByText('Purchase')).toBeInTheDocument()
    expect(screen.getByText('-60%')).toBeInTheDocument()
  })

  it('shows an empty state when nothing regressed', async () => {
    vi.mocked(eventMetricsApi.getReleaseRegressions).mockResolvedValue({
      scan_config_id: 'scan-1',
      app_version_column: 'app_version',
      latest_version: '2.1.0',
      // The affirmative is only earned by a verdict that says the comparison
      // was actually made.
      comparability: [verdict()],
      items: [],
    })

    renderPanel()

    expect(await screen.findByText(/No events regressed/i)).toBeInTheDocument()
    expect(screen.queryByText(/Cannot be judged yet/i)).not.toBeInTheDocument()
  })

  it('says a withheld comparison cannot be judged instead of affirming health', async () => {
    vi.mocked(eventMetricsApi.getReleaseRegressions).mockResolvedValue({
      scan_config_id: 'scan-1',
      app_version_column: 'app_version',
      latest_version: '2.1.0',
      comparability: [
        verdict({ comparable: false, reason: 'population_mismatch', emerging_share: 0.66 }),
      ],
      items: [],
    })

    renderPanel()

    expect(await screen.findByText(/Cannot be judged yet/i)).toBeInTheDocument()
    expect(screen.getByText(/66\.0% of its volume/)).toBeInTheDocument()
    expect(screen.getByText(/2\.1\.0 vs 2\.0\.0/)).toBeInTheDocument()
    expect(screen.queryByText(/No events regressed/i)).not.toBeInTheDocument()
  })

  it('shows the withheld notice alongside the missing rows suppression keeps', async () => {
    vi.mocked(eventMetricsApi.getReleaseRegressions).mockResolvedValue({
      scan_config_id: 'scan-1',
      app_version_column: 'app_version',
      latest_version: '2.1.0',
      comparability: [verdict({ comparable: false, reason: 'population_mismatch' })],
      items: [regression({ scope_name: 'Login', kind: 'missing' })],
    })

    renderPanel()

    expect(await screen.findByText(/Cannot be judged yet/i)).toBeInTheDocument()
    expect(screen.getByText('Login')).toBeInTheDocument()
  })

  it('names the reason when no baseline release exists to compare against', async () => {
    vi.mocked(eventMetricsApi.getReleaseRegressions).mockResolvedValue({
      scan_config_id: 'scan-1',
      app_version_column: 'app_version',
      latest_version: null,
      comparability: [
        verdict({
          comparable: false,
          reason: 'no_baseline',
          version: null,
          previous_version: null,
          emerging_share: 0,
        }),
      ],
      items: [],
    })

    renderPanel()

    expect(await screen.findByText(/Fewer than two released versions/i)).toBeInTheDocument()
  })

  it('does not affirm health when detection has never run for the scan', async () => {
    vi.mocked(eventMetricsApi.getReleaseRegressions).mockResolvedValue({
      scan_config_id: 'scan-1',
      app_version_column: 'app_version',
      latest_version: null,
      comparability: [],
      items: [],
    })

    renderPanel()

    expect(await screen.findByText(/No release comparison has run/i)).toBeInTheDocument()
    expect(screen.queryByText(/No events regressed/i)).not.toBeInTheDocument()
  })

  it('says the list is scan-wide, links each row and labels the count', async () => {
    vi.mocked(eventMetricsApi.getReleaseRegressions).mockResolvedValue({
      scan_config_id: 'scan-1',
      app_version_column: 'app_version',
      latest_version: '2.1.0',
      comparability: [verdict()],
      items: [
        regression({ scope_ref: 'e1', event_id: 'e1', scope_name: 'Login' }),
        regression({
          scope_type: 'event_type',
          scope_ref: 't1',
          event_id: null,
          event_type_id: 't1',
          scope_name: 'Checkout',
        }),
      ],
    })

    renderPanel()

    expect(await screen.findByRole('heading', { name: /Release regressions · whole scan/ }))
      .toBeInTheDocument()
    expect(await screen.findByRole('link', { name: 'Login' }))
      .toHaveAttribute('href', '/p/demo/monitoring/event/e1')
    expect(screen.getByRole('link', { name: 'Checkout' }))
      .toHaveAttribute('href', '/p/demo/monitoring/event-type/t1')
    expect(screen.getByText('2 regressions')).toBeInTheDocument()
  })

  it('lists every distinct withheld reason, not only the first', async () => {
    vi.mocked(eventMetricsApi.getReleaseRegressions).mockResolvedValue({
      scan_config_id: 'scan-1',
      app_version_column: 'app_version',
      latest_version: '2.1.0',
      comparability: [
        verdict({ comparable: false, reason: 'no_baseline' }),
        verdict({ scope_type: 'event_type', comparable: false, reason: 'baseline_no_volume' }),
      ],
      items: [],
    })

    renderPanel()

    expect(await screen.findByText(/Fewer than two released versions/i)).toBeInTheDocument()
    expect(screen.getByText(/baseline release has no volume/i)).toBeInTheDocument()
  })
  it('reports a failed request as an error, not as "no comparison has run"', async () => {
    const fetchRegressions = vi.mocked(eventMetricsApi.getReleaseRegressions)
    fetchRegressions.mockReset()
    fetchRegressions.mockRejectedValueOnce(new Error('upstream timeout'))
    fetchRegressions.mockResolvedValue({
      scan_config_id: 'scan-1',
      app_version_column: 'app_version',
      latest_version: '2.1.0',
      items: [regression({})],
      comparability: [verdict()],
    })
    renderPanel()

    expect(await screen.findByText('Release regressions unavailable')).toBeInTheDocument()
    expect(screen.queryByText(/No release comparison has run/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Login')).toBeInTheDocument()
  })
})
