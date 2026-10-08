import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { plannedEventsApi } from '@/api/plannedEvents'
import { usePlannedEvents } from './usePlannedEvents'

vi.mock('@/api/plannedEvents', () => ({ plannedEventsApi: { list: vi.fn().mockResolvedValue([]) } }))

describe('usePlannedEvents', () => {
  it('lists windows from the range start on, upcoming ones included', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    )

    const { result } = renderHook(
      () =>
        usePlannedEvents({
          slug: 'demo',
          scope: 'metric',
          scopeId: 'm1',
          rangeDays: 7,
          timeRange: { from: '2026-10-01T00:00:00Z', to: '2026-10-08T00:00:00Z' },
        }),
      { wrapper },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    // No upper bound: a sale planned for next month stays in the card after Add.
    expect(plannedEventsApi.list).toHaveBeenCalledWith('demo', {
      scope_type: 'metric',
      scope_ref: 'm1',
      from: '2026-10-01T00:00:00Z',
    })
  })
})
