import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { MetricDefinitionDetailResponse } from '@/types'
import { MetricHeaderActions } from './MetricHeaderActions'

vi.mock('@/api/metricsCatalog', () => ({
  metricsCatalogApi: { update: vi.fn(), del: vi.fn() },
}))
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))

import { metricsCatalogApi } from '@/api/metricsCatalog'
import { toast } from 'sonner'

function renderActions(status: 'draft' | 'active') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const metric = {
    id: 'm-1',
    kind: 'sql',
    status,
    display_name: 'Signups',
  } as unknown as MetricDefinitionDetailResponse
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <MetricHeaderActions
          slug="demo"
          scopeId="m-1"
          metricDefinition={metric}
          editPath="/p/demo/metrics/m-1/edit"
          collect={{ start: vi.fn(), isCollecting: false }}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('MetricHeaderActions — a draft can be activated where it is read', () => {
  it('activates a draft metric in one click', async () => {
    vi.mocked(metricsCatalogApi.update).mockResolvedValue(
      {} as Awaited<ReturnType<typeof metricsCatalogApi.update>>,
    )
    renderActions('draft')

    fireEvent.click(screen.getByRole('button', { name: 'Activate' }))

    await waitFor(() =>
      expect(metricsCatalogApi.update).toHaveBeenCalledWith('demo', 'm-1', { status: 'active' }),
    )
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        'Metric activated. Collection starts on the next scheduled run.',
      ),
    )
  })

  it('offers no Activate on an active metric', () => {
    renderActions('active')

    expect(screen.queryByRole('button', { name: 'Activate' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Collect now' })).toBeInTheDocument()
  })
})
