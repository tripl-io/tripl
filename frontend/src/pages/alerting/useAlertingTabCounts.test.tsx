import type { ReactNode } from 'react'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { alertingApi } from '@/api/alerting'
import { alertDeliveriesKey, alertInboxKey } from '@/lib/queryKeys'
import type { AlertDeliveryListResponse, AlertInboxListResponse } from '@/types'

import { invalidateAlertingConfig } from './alertingCache'
import { failedDeliveryCountKey, openIncidentCountKey, useAlertingTabCounts } from './useAlertingTabCounts'

function wrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>
  }
}

function newClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('useAlertingTabCounts', () => {
  it('asks for one open incident and one failed delivery, and reads their totals', async () => {
    const listInbox = vi
      .spyOn(alertingApi, 'listInbox')
      .mockResolvedValue({ items: [], total: 3, window_truncated_at: null } as unknown as AlertInboxListResponse)
    const listDeliveries = vi
      .spyOn(alertingApi, 'listDeliveries')
      .mockResolvedValue({ items: [], total: 2 } as unknown as AlertDeliveryListResponse)

    const { result } = renderHook(
      () => useAlertingTabCounts('demo', { enabled: true, refetchInterval: false }),
      { wrapper: wrapper(newClient()) },
    )

    await waitFor(() => expect(result.current).toEqual({ openIncidents: 3, failedDeliveries: 2 }))
    // A page of one: the count is the server's `total`, not a list to fetch.
    expect(listInbox).toHaveBeenCalledWith('demo', { status: 'open', limit: 1 })
    expect(listDeliveries).toHaveBeenCalledWith('demo', { status: 'failed', limit: 1 })
  })

  it('asks nothing while disabled', () => {
    const listInbox = vi.spyOn(alertingApi, 'listInbox')
    const listDeliveries = vi.spyOn(alertingApi, 'listDeliveries')

    const { result } = renderHook(
      () => useAlertingTabCounts('demo', { enabled: false, refetchInterval: false }),
      { wrapper: wrapper(newClient()) },
    )

    expect(result.current).toEqual({ openIncidents: undefined, failedDeliveries: undefined })
    expect(listInbox).not.toHaveBeenCalled()
    expect(listDeliveries).not.toHaveBeenCalled()
  })

  it('keys both counts under the lists they count, so every alerting write refreshes them', () => {
    // An Ack that left "Inbox 3" on the tab would make the strip untrustworthy.
    expect(openIncidentCountKey('demo').slice(0, 2)).toEqual([...alertInboxKey('demo')])
    expect(failedDeliveryCountKey('demo').slice(0, 2)).toEqual([...alertDeliveriesKey('demo')])

    const client = newClient()
    client.setQueryData(openIncidentCountKey('demo'), { items: [], total: 3 })
    client.setQueryData(failedDeliveryCountKey('demo'), { items: [], total: 2 })
    invalidateAlertingConfig(client, 'demo')
    expect(client.getQueryState(openIncidentCountKey('demo'))?.isInvalidated).toBe(true)
    expect(client.getQueryState(failedDeliveryCountKey('demo'))?.isInvalidated).toBe(true)
  })
})
