import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { formatUtcOffset } from '@/lib/datetime'
import type { DataSource, MonitoringSignal, ScanConfig } from '@/types'
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

const SIGNAL: MonitoringSignal = {
  scan_config_id: 'scan-1',
  scope_type: 'event',
  scope_ref: 'ev-1',
  state: 'latest_scan',
  event_id: 'ev-1',
  event_type_id: null,
  bucket: '2026-07-01T12:00:00Z',
  actual_count: 7173,
  expected_count: 2403,
  stddev: 5,
  z_score: 8,
  direction: 'spike',
  scope_name: 'Home Screen View',
  incident_child: false,
  muted: false,
  expected: false,
  hidden: false,
  attribution_status: 'not_computed',
  unit: null,
  detected_at: '2026-07-01T13:05:00Z',
}

function renderAnomalies() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/p/demo/anomalies']}>
        <Routes>
          <Route path="/p/:slug/anomalies" element={<AnomaliesPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

async function renderAndFindRow(): Promise<HTMLElement> {
  renderAnomalies()
  const link = await screen.findByRole('link', { name: 'Spike on Event · Home Screen View' })
  return link.closest('[role="row"]') as HTMLElement
}

beforeEach(() => {
  vi.mocked(eventMetricsApi.getActiveSignals).mockReset()
  vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([SIGNAL])
  vi.mocked(eventMetricsApi.getSignalSeries).mockReset()
  vi.mocked(eventMetricsApi.getSignalSeries).mockResolvedValue([])
  vi.mocked(scansApi.list).mockReset()
  vi.mocked(scansApi.list).mockResolvedValue([] as ScanConfig[])
  vi.mocked(sourceFreshnessApi.list).mockReset()
  vi.mocked(sourceFreshnessApi.list).mockResolvedValue([])
  vi.mocked(dataSourcesApi.list).mockReset()
  vi.mocked(dataSourcesApi.list).mockResolvedValue([] as DataSource[])
})

// At 390px a row read "Event · Home Sc…", an unlabelled "7,173 vs 2,403", and
// two times with no zone: the column headers are sm+ only, and the zone lived
// in a tooltip a phone cannot show.
describe('AnomaliesPage — a row on a phone', () => {
  it('wraps the scope name to two lines instead of cutting it to a few letters', async () => {
    const row = await renderAndFindRow()
    const label = row.querySelector('[data-anomaly-label]') as HTMLElement

    expect(label).toHaveClass('line-clamp-2', 'sm:line-clamp-1')
    expect(label).not.toHaveClass('truncate')
  })

  it('names the expected figure where the column header is hidden', async () => {
    const row = await renderAndFindRow()

    expect(row).toHaveTextContent('7,173 vs 2,403 expected')
    expect(screen.getByText('expected', { selector: 'span.sm\\:hidden' })).toBeInTheDocument()
  })

  it('names the zone of the bucket time and puts the times on a line of their own', async () => {
    const row = await renderAndFindRow()
    const bucket = row.querySelector('time[datetime="2026-07-01T12:00:00Z"]') as HTMLElement
    const cell = bucket.closest('[role="cell"]') as HTMLElement
    const zone = formatUtcOffset(new Date(SIGNAL.bucket))

    // Outside the <time>, so the element still holds the bucket time alone.
    expect(bucket.textContent).not.toContain(zone)
    expect(cell).toHaveTextContent(zone)
    expect(cell).toHaveClass('max-sm:col-span-2')
    // "found …" follows the bucket on the phone's line and sits under it from sm.
    const found = row.querySelector('time[datetime="2026-07-01T13:05:00Z"]') as HTMLElement
    expect(found).toHaveTextContent(/^found /)
    expect(found).toHaveClass('sm:block')
    expect(found).not.toHaveClass('block')
  })
})
