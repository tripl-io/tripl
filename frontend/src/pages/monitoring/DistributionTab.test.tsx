import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import { DistributionTab } from './DistributionTab'

vi.mock('@/api/eventMetrics', () => ({
  eventMetricsApi: {
    getDistributionDrifts: vi.fn().mockResolvedValue({
      scope: 'event_type',
      scan_config_id: 'scan-1',
      event_type_id: 'et-1',
      fields: ['platform'],
      data: [
        {
          id: 'drift-1',
          scan_config_id: 'scan-1',
          event_type_id: 'et-1',
          field_name: 'platform',
          bucket: '2026-09-25T00:00:00Z',
          psi: 0.274,
          band: 'significant',
          baseline_total: 1200,
          current_total: 1350,
          top_movers: [],
        },
      ],
    }),
  },
}))

describe('DistributionTab', () => {
  it('lays the four stats out as a 2×2 grid on a phone', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <DistributionTab
          slug="demo"
          distributionScope={{ scope_type: 'event_type', scope_ref: 'et-1' }}
          rangeDays={7}
          timeRange={{ from: '2026-09-19T00:00:00Z', to: '2026-09-26T00:00:00Z' }}
          refetchInterval={false}
          selectedField="platform"
          onSelectedFieldChange={() => {}}
        />
      </QueryClientProvider>,
    )

    // The PSI prints in the stat strip and again in the per-bucket table.
    const psi = await screen.findAllByText('0.274')
    expect(psi.some((node) => node.closest('[data-phone-grid]') !== null)).toBe(true)
  })
})
