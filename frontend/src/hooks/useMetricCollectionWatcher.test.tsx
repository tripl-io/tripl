import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MetricDefinitionDetailResponse } from '@/types'

vi.mock('@/api/metricsCatalog', () => ({
  metricsCatalogApi: { get: vi.fn() },
}))
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

import { toast } from 'sonner'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { ApiError, AUTH_SIGNED_OUT_EVENT, AUTH_UNAUTHORIZED_EVENT } from '@/api/client'
import {
  startMetricCollectionWatch,
  stopAllMetricCollectionWatches,
  useIsMetricCollectionWatched,
} from './useMetricCollectionWatcher'

// The watcher only reads id / last_collection_status / last_collection_error;
// a minimal shape keeps the fixtures focused (mirrors MetricsPage.test.tsx's
// `as unknown as` casts for partial API payloads).
function definitionWith(
  status: string | null,
  error: string | null = null,
): MetricDefinitionDetailResponse {
  return {
    id: 'm-1',
    last_collection_status: status,
    last_collection_error: error,
  } as unknown as MetricDefinitionDetailResponse
}

beforeEach(() => {
  vi.clearAllMocks()
})

function WatchedBadge({ metricId }: { metricId: string }) {
  return <span>{useIsMetricCollectionWatched('demo', metricId) ? 'watching' : 'idle'}</span>
}

describe('startMetricCollectionWatch', () => {
  afterEach(() => {
    stopAllMetricCollectionWatches()
  })

  it('reports the outcome with no component mounted at all', async () => {
    vi.mocked(metricsCatalogApi.get)
      .mockResolvedValueOnce(definitionWith('running'))
      .mockResolvedValue(definitionWith('success'))
    const onSettled = vi.fn()

    startMetricCollectionWatch(
      { slug: 'demo', metricId: 'm-1', displayName: 'Checkout errors' },
      { onSettled, pollIntervalMs: 10 },
    )

    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        '"Checkout errors" collected — the chart is up to date.',
      ),
    )
    expect(onSettled).toHaveBeenCalledWith('m-1', 'success')
  })

  it('keeps polling while the run is still running, then settles', async () => {
    vi.mocked(metricsCatalogApi.get)
      .mockResolvedValueOnce(definitionWith('running'))
      .mockResolvedValueOnce(definitionWith('running'))
      .mockResolvedValue(definitionWith('success'))
    const onSettled = vi.fn()

    startMetricCollectionWatch(
      { slug: 'demo', metricId: 'm-1', displayName: 'Checkout errors' },
      { onSettled, pollIntervalMs: 10 },
    )

    await waitFor(() => expect(toast.success).toHaveBeenCalled())
    expect(metricsCatalogApi.get).toHaveBeenCalledTimes(3)
    expect(metricsCatalogApi.get).toHaveBeenCalledWith('demo', 'm-1')
    expect(onSettled).toHaveBeenCalledTimes(1)
    expect(onSettled).toHaveBeenCalledWith('m-1', 'success')
  })

  it('settles on a failed run too, so the caller can refresh the list', async () => {
    vi.mocked(metricsCatalogApi.get).mockResolvedValue(definitionWith('error', 'boom'))
    const onSettled = vi.fn()

    startMetricCollectionWatch(
      { slug: 'demo', metricId: 'm-1', displayName: 'Checkout errors' },
      { onSettled, pollIntervalMs: 10 },
    )

    await waitFor(() => expect(onSettled).toHaveBeenCalledWith('m-1', 'error'))
    expect(toast.error).toHaveBeenCalledWith('Collection failed: boom')
  })

  it('falls back to a generic failure message when no reason was persisted', async () => {
    vi.mocked(metricsCatalogApi.get).mockResolvedValue(definitionWith('error'))

    startMetricCollectionWatch(
      { slug: 'demo', metricId: 'm-1', displayName: 'Checkout errors' },
      { pollIntervalMs: 10 },
    )

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Collection failed.'))
  })

  it('says "no longer exists" when the metric was deleted mid-watch, and stops there', async () => {
    vi.mocked(metricsCatalogApi.get).mockRejectedValue(new ApiError('Not found', 404))
    const onSettled = vi.fn()
    render(<WatchedBadge metricId="m-1" />)

    act(() => {
      startMetricCollectionWatch(
        { slug: 'demo', metricId: 'm-1', displayName: 'Checkout errors' },
        { onSettled, pollIntervalMs: 10 },
      )
    })

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        '"Checkout errors" no longer exists — it was deleted while collecting.',
      ),
    )
    expect(await screen.findByText('idle')).toBeInTheDocument()
    expect(metricsCatalogApi.get).toHaveBeenCalledTimes(1)
    expect(onSettled).not.toHaveBeenCalled()
  })

  it('times out on schedule even while every poll fails', async () => {
    let now = 1_000_000
    const clock = vi.spyOn(Date, 'now').mockImplementation(() => now)
    vi.mocked(metricsCatalogApi.get).mockImplementation(async () => {
      // Five minutes pass during the first (failing) poll.
      now += 5 * 60_000
      throw new ApiError('Gateway timeout', 504)
    })

    startMetricCollectionWatch(
      { slug: 'demo', metricId: 'm-1', displayName: 'Checkout errors' },
      { pollIntervalMs: 10 },
    )

    await waitFor(() => expect(toast.info).toHaveBeenCalledTimes(1))
    expect(vi.mocked(toast.info).mock.calls[0]![0]).toMatch(/is still collecting/)
    expect(metricsCatalogApi.get).toHaveBeenCalledTimes(1)
    clock.mockRestore()
  })

  it('tells a mounted component while the watch runs, and after it ends', async () => {
    vi.mocked(metricsCatalogApi.get).mockResolvedValue(definitionWith('running'))
    render(<WatchedBadge metricId="m-1" />)
    expect(screen.getByText('idle')).toBeInTheDocument()

    // Starting a watch notifies mounted subscribers synchronously; in the app
    // that happens inside a click handler, here it needs act().
    act(() => {
      startMetricCollectionWatch(
        { slug: 'demo', metricId: 'm-1', displayName: 'Checkout errors' },
        { pollIntervalMs: 10 },
      )
    })
    expect(await screen.findByText('watching')).toBeInTheDocument()

    vi.mocked(metricsCatalogApi.get).mockResolvedValue(definitionWith('success'))
    expect(await screen.findByText('idle')).toBeInTheDocument()
  })

  it('gives up after repeated failed polls', async () => {
    vi.mocked(metricsCatalogApi.get).mockRejectedValue(new ApiError('Bad gateway', 502))

    startMetricCollectionWatch(
      { slug: 'demo', metricId: 'm-1', displayName: 'Checkout errors' },
      { pollIntervalMs: 10 },
    )

    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith(
        'Lost track of "Checkout errors" — the server stopped answering. Reload the page to see whether it finished.',
      ),
    )
    expect(metricsCatalogApi.get).toHaveBeenCalledTimes(3)
  })

  it.each([401, 403])('ends silently when a poll gets %i (the session is gone)', async (status) => {
    vi.mocked(metricsCatalogApi.get).mockRejectedValue(new ApiError('Denied', status))
    const onSettled = vi.fn()
    render(<WatchedBadge metricId="m-1" />)

    act(() => {
      startMetricCollectionWatch(
        { slug: 'demo', metricId: 'm-1', displayName: 'Checkout errors' },
        { onSettled, pollIntervalMs: 10 },
      )
    })

    expect(screen.getByText('watching')).toBeInTheDocument()
    expect(await screen.findByText('idle')).toBeInTheDocument()
    await new Promise(resolve => setTimeout(resolve, 50))
    // One poll, no retries, and nothing said on the login screen.
    expect(metricsCatalogApi.get).toHaveBeenCalledTimes(1)
    expect(toast.info).not.toHaveBeenCalled()
    expect(toast.error).not.toHaveBeenCalled()
    expect(onSettled).not.toHaveBeenCalled()
  })

  it.each([AUTH_UNAUTHORIZED_EVENT, AUTH_SIGNED_OUT_EVENT])('stops every watch when the app signals %s', async (event) => {
    vi.mocked(metricsCatalogApi.get).mockResolvedValue(definitionWith('running'))
    render(<WatchedBadge metricId="m-1" />)
    act(() => {
      startMetricCollectionWatch(
        { slug: 'demo', metricId: 'm-1', displayName: 'Checkout errors' },
        { pollIntervalMs: 10 },
      )
    })
    expect(await screen.findByText('watching')).toBeInTheDocument()

    act(() => {
      window.dispatchEvent(new Event(event))
    })

    expect(screen.getByText('idle')).toBeInTheDocument()
    const polls = vi.mocked(metricsCatalogApi.get).mock.calls.length
    vi.mocked(metricsCatalogApi.get).mockResolvedValue(definitionWith('success'))
    await new Promise(resolve => setTimeout(resolve, 50))
    expect(metricsCatalogApi.get).toHaveBeenCalledTimes(polls)
    expect(toast.success).not.toHaveBeenCalled()
  })
})
