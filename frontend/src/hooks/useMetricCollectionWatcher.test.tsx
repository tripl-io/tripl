import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
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
  useMetricCollectionWatcher,
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

function Harness({
  onSettled,
}: {
  onSettled?: (metricId: string, status: 'success' | 'error') => void
}) {
  const watcher = useMetricCollectionWatcher(onSettled, { pollIntervalMs: 10 })
  return (
    <button
      type="button"
      onClick={() =>
        watcher.watch({ slug: 'demo', metricId: 'm-1', displayName: 'Checkout errors' })
      }
    >
      {watcher.isWatching ? 'watching' : 'idle'}
    </button>
  )
}

function renderHarness(onSettled?: (metricId: string, status: 'success' | 'error') => void) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <Harness onSettled={onSettled} />
    </QueryClientProvider>,
  )
}

// Mirrors MonitoringDetailPage: the spinner is keyed to the *watched* metric
// (not a raw `isWatching`), and the watch captures the current route's ids at
// collect-start so a completion can invalidate the metric it actually collected
// — even after an `:id`-only navigation that does NOT remount the page.
type NavRoute = { slug: string; scope: string; scopeId: string }

function NavHarness({
  route,
  onInvalidate,
}: {
  route: NavRoute
  onInvalidate: (slug: string, scope: string, scopeId: string) => void
}) {
  const watcher = useMetricCollectionWatcher<NavRoute>(
    (_metricId, status, context) => {
      if (status !== 'success' || !context) return
      onInvalidate(context.slug, context.scope, context.scopeId)
    },
    { pollIntervalMs: 10 },
  )
  const isCollecting = watcher.watchingMetricId === route.scopeId
  return (
    <button
      type="button"
      onClick={() =>
        watcher.watch({
          slug: route.slug,
          metricId: route.scopeId,
          displayName: `Metric ${route.scopeId}`,
          context: route,
        })
      }
    >
      {isCollecting ? 'collecting' : 'idle'}
    </button>
  )
}

function renderNavHarness(
  route: NavRoute,
  onInvalidate: (slug: string, scope: string, scopeId: string) => void,
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const ui = (nextRoute: NavRoute) => (
    <QueryClientProvider client={queryClient}>
      <NavHarness route={nextRoute} onInvalidate={onInvalidate} />
    </QueryClientProvider>
  )
  const view = render(ui(route))
  // Simulates navigating to another metric (or project) without unmounting.
  const navigateTo = (nextRoute: NavRoute) => view.rerender(ui(nextRoute))
  return { ...view, navigateTo }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('useMetricCollectionWatcher', () => {
  it('does not poll until a watch starts', () => {
    renderHarness()

    expect(screen.getByRole('button')).toHaveTextContent('idle')
    expect(metricsCatalogApi.get).not.toHaveBeenCalled()
  })

  it('toasts success and settles when the run reports success', async () => {
    vi.mocked(metricsCatalogApi.get).mockResolvedValue(definitionWith('success'))
    const onSettled = vi.fn()
    renderHarness(onSettled)

    fireEvent.click(screen.getByRole('button'))

    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        '"Checkout errors" collected — the chart is up to date.',
      ),
    )
    expect(metricsCatalogApi.get).toHaveBeenCalledWith('demo', 'm-1')
    // No context captured by this harness, so the third arg is undefined.
    expect(onSettled).toHaveBeenCalledWith('m-1', 'success', undefined)
    // The watch stops once terminal.
    expect(screen.getByRole('button')).toHaveTextContent('idle')
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('toasts the persisted failure reason when the run reports error', async () => {
    vi.mocked(metricsCatalogApi.get).mockResolvedValue(
      definitionWith('error', 'warehouse query timed out'),
    )
    const onSettled = vi.fn()
    renderHarness(onSettled)

    fireEvent.click(screen.getByRole('button'))

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Collection failed: warehouse query timed out'),
    )
    expect(onSettled).toHaveBeenCalledWith('m-1', 'error', undefined)
    expect(screen.getByRole('button')).toHaveTextContent('idle')
    expect(toast.success).not.toHaveBeenCalled()
  })

  it('falls back to a generic failure message when no reason was persisted', async () => {
    vi.mocked(metricsCatalogApi.get).mockResolvedValue(definitionWith('error'))
    renderHarness()

    fireEvent.click(screen.getByRole('button'))

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Collection failed.'))
  })

  // a failing poll used to error the query, which kept refetching on
  // the interval — a toast every 3 s and a spinner that never stopped.
  it('settles with "no longer exists" when the metric was deleted mid-watch', async () => {
    vi.mocked(metricsCatalogApi.get).mockRejectedValue(new ApiError('Not found', 404))
    const onSettled = vi.fn()
    renderHarness(onSettled)

    fireEvent.click(screen.getByRole('button'))

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        '"Checkout errors" no longer exists — it was deleted while collecting.',
      ),
    )
    expect(screen.getByRole('button')).toHaveTextContent('idle')
    expect(metricsCatalogApi.get).toHaveBeenCalledTimes(1)
    expect(onSettled).not.toHaveBeenCalled()
  })

  it('gives up after repeated failed polls instead of spinning forever', async () => {
    vi.mocked(metricsCatalogApi.get).mockRejectedValue(new ApiError('Backend is unavailable.', 503))
    renderHarness()

    fireEvent.click(screen.getByRole('button'))

    await waitFor(() => expect(screen.getByRole('button')).toHaveTextContent('idle'))
    expect(metricsCatalogApi.get).toHaveBeenCalledTimes(3)
    expect(toast.info).toHaveBeenCalledTimes(1)
    expect(vi.mocked(toast.info).mock.calls[0]![0]).toMatch(/Lost track of "Checkout errors"/)
  })

  it('times out on schedule even while every poll fails', async () => {
    let now = 1_000_000
    vi.spyOn(Date, 'now').mockImplementation(() => now)
    vi.mocked(metricsCatalogApi.get).mockImplementation(async () => {
      // Five minutes pass during the first (failing) poll.
      now += 5 * 60_000
      throw new ApiError('Gateway timeout', 504)
    })
    renderHarness()

    fireEvent.click(screen.getByRole('button'))

    await waitFor(() => expect(screen.getByRole('button')).toHaveTextContent('idle'))
    expect(vi.mocked(toast.info).mock.calls[0]![0]).toMatch(/is still collecting/)
    expect(metricsCatalogApi.get).toHaveBeenCalledTimes(1)
  })

  it('keeps polling while the run is still running, then settles', async () => {
    vi.mocked(metricsCatalogApi.get)
      .mockResolvedValueOnce(definitionWith('running'))
      .mockResolvedValueOnce(definitionWith('running'))
      .mockResolvedValue(definitionWith('success'))
    const onSettled = vi.fn()
    renderHarness(onSettled)

    fireEvent.click(screen.getByRole('button'))

    // Still watching after the first "running" response — no toast yet.
    await waitFor(() => expect(metricsCatalogApi.get).toHaveBeenCalled())
    expect(screen.getByRole('button')).toHaveTextContent('watching')
    expect(toast.success).not.toHaveBeenCalled()

    // The 10ms poll interval re-fetches until the terminal status lands.
    await waitFor(() => expect(toast.success).toHaveBeenCalled())
    expect(vi.mocked(metricsCatalogApi.get).mock.calls.length).toBeGreaterThanOrEqual(3)
    expect(onSettled).toHaveBeenCalledWith('m-1', 'success', undefined)
  })

  it('does not show a metric navigated to mid-watch as collecting', async () => {
    // The watch stays in flight (never leaves "running") across the navigation.
    vi.mocked(metricsCatalogApi.get).mockResolvedValue(definitionWith('running'))
    const onInvalidate = vi.fn()
    const { navigateTo } = renderNavHarness(
      { slug: 'demo', scope: 'metric', scopeId: 'A' },
      onInvalidate,
    )

    // Start collecting metric A.
    fireEvent.click(screen.getByRole('button'))
    await waitFor(() => expect(metricsCatalogApi.get).toHaveBeenCalledWith('demo', 'A'))
    expect(screen.getByRole('button')).toHaveTextContent('collecting')

    // Navigate to metric B while A is still collecting — same page, no remount.
    navigateTo({ slug: 'demo', scope: 'metric', scopeId: 'B' })

    // B must NOT inherit A's in-flight watch state.
    expect(screen.getByRole('button')).toHaveTextContent('idle')
  })

  it("invalidates the metric captured at collect-start, not the one navigated to", async () => {
    vi.mocked(metricsCatalogApi.get).mockResolvedValue(definitionWith('running'))
    const onInvalidate = vi.fn()
    const { navigateTo } = renderNavHarness(
      { slug: 'demo', scope: 'metric', scopeId: 'A' },
      onInvalidate,
    )

    // Start collecting metric A (captures { scope: 'metric', scopeId: 'A' }).
    fireEvent.click(screen.getByRole('button'))
    await waitFor(() => expect(metricsCatalogApi.get).toHaveBeenCalledWith('demo', 'A'))

    // Navigate to metric B while A's collection is still running.
    navigateTo({ slug: 'demo', scope: 'metric', scopeId: 'B' })

    // A's run now settles successfully.
    vi.mocked(metricsCatalogApi.get).mockResolvedValue(definitionWith('success'))

    await waitFor(() => expect(onInvalidate).toHaveBeenCalled())
    // Completion invalidates A's captured scope — never the navigated-to B.
    expect(onInvalidate).toHaveBeenCalledWith('demo', 'metric', 'A')
    expect(onInvalidate).not.toHaveBeenCalledWith('demo', 'metric', 'B')
  })

  it('keeps polling the originating project after a cross-project move', async () => {
    vi.mocked(metricsCatalogApi.get).mockResolvedValue(definitionWith('running'))
    const onInvalidate = vi.fn()
    const { navigateTo } = renderNavHarness(
      { slug: 'project-a', scope: 'metric', scopeId: 'A' },
      onInvalidate,
    )

    fireEvent.click(screen.getByRole('button'))
    await waitFor(() => expect(metricsCatalogApi.get).toHaveBeenCalledWith('project-a', 'A'))

    // Leave for another project entirely while A is still collecting. The poll
    // used to read the slug live, so it started asking project-b for metric A —
    // an id that does not exist there.
    navigateTo({ slug: 'project-b', scope: 'metric', scopeId: 'B' })
    vi.mocked(metricsCatalogApi.get).mockResolvedValue(definitionWith('success'))

    await waitFor(() => expect(onInvalidate).toHaveBeenCalled())
    expect(metricsCatalogApi.get).not.toHaveBeenCalledWith('project-b', 'A')
    // The settle invalidates the project the collect was fired against.
    expect(onInvalidate).toHaveBeenCalledWith('project-a', 'metric', 'A')
  })
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
