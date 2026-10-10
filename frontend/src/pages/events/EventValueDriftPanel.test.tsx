import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { variableDriftsApi } from '@/api/variableDrifts'
import { formatDateTime } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import {
  branchPropertyEntriesKey,
  branchVariableDriftsKey,
  branchVariableOverridesKey,
  variablesKey,
} from '@/lib/queryKeys'
import { EventValueDriftPanel } from './EventValueDriftPanel'

vi.mock('@/api/variableDrifts', () => ({
  variableDriftsApi: {
    list: vi.fn(),
    action: vi.fn(),
  },
}))

function renderPanel({ canWrite = true }: { canWrite?: boolean } = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={queryClient}>
      <EventValueDriftPanel slug="demo" eventId="ev-1" canWrite={canWrite} />
    </QueryClientProvider>,
  )
  return { ...view, queryClient }
}

afterEach(() => {
  vi.clearAllMocks()
})

const DRIFT = {
  id: 'drift-1',
  variable_id: 'var-1',
  variable_name: 'variant',
  event_id: 'ev-1',
  event_name: 'Onboarding',
  scan_config_id: null,
  observed_values: ['x', 'y'],
  status: 'open' as const,
  resolution_note: null,
  snoozed_until: null,
  resolved_at: null,
  resolved_by: null,
  detected_at: '2026-07-09T00:00:00Z',
}

describe('EventValueDriftPanel', () => {
  it('renders nothing when the event has no active drift', async () => {
    vi.mocked(variableDriftsApi.list).mockResolvedValue({ items: [], total: 0 })
    const { container } = renderPanel()
    await waitFor(() => expect(variableDriftsApi.list).toHaveBeenCalledWith('demo', { eventId: 'ev-1' }, null))
    expect(container).toBeEmptyDOMElement()
  })

  it('lists novel values and accepts them for the event', async () => {
    vi.mocked(variableDriftsApi.list).mockResolvedValue({ items: [DRIFT], total: 1 })
    vi.mocked(variableDriftsApi.action).mockResolvedValue({ ...DRIFT, status: 'accepted' })

    renderPanel()
    expect(await screen.findByText('${variant}')).toBeInTheDocument()
    expect(screen.getByText('x')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Accept for event' }))
    await waitFor(() =>
      expect(variableDriftsApi.action).toHaveBeenCalledWith(
        'demo',
        'drift-1',
        { action: 'accept', scope: 'event', snoozed_until: undefined },
        null,
      ),
    )
  })

  it('reveals an accepted drift behind the resolved toggle and reopens it', async () => {
    const accepted = {
      ...DRIFT,
      id: 'drift-2',
      status: 'accepted' as const,
      observed_values: ['q'],
    }
    vi.mocked(variableDriftsApi.list).mockResolvedValue({ items: [accepted], total: 1 })
    vi.mocked(variableDriftsApi.action).mockResolvedValue({ ...accepted, status: 'open' })

    renderPanel()

    // Resolved rows are collapsed, not hidden: the panel still renders.
    fireEvent.click(await screen.findByRole('button', { name: 'Show 1 resolved' }))
    expect(await screen.findByText('q')).toBeInTheDocument()
    expect(screen.getByText('accepted')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Reopen' }))
    await waitFor(() =>
      expect(variableDriftsApi.action).toHaveBeenCalledWith(
        'demo',
        'drift-2',
        { action: 'reopen', scope: undefined, snoozed_until: undefined },
        null,
      ),
    )
  })

  it('collapses a drift snoozed into the future instead of calling it active', async () => {
    // The backend drops a future-snoozed row from `get_open_drift_counts`, so
    // the variables table badge reads zero for it. This panel used to keep the
    // very same row in the warning-toned active list, with the full action row
    // and no toggle that could ever collapse it.
    const snoozedUntil = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000).toISOString()
    const snoozed = {
      ...DRIFT,
      id: 'drift-3',
      status: 'snoozed' as const,
      snoozed_until: snoozedUntil,
    }
    vi.mocked(variableDriftsApi.list).mockResolvedValue({ items: [snoozed], total: 1 })
    vi.mocked(variableDriftsApi.action).mockResolvedValue({ ...snoozed, status: 'open' })

    renderPanel()

    // Nothing is open, so the block is muted rather than warning-toned, and the
    // row is behind the collapse toggle — named for what it is, not "resolved".
    expect(await screen.findByText(/value drift — observed values outside/i)).toHaveClass(
      'text-fg-tertiary',
    )
    expect(screen.queryByRole('button', { name: 'Accept for event' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Show 1 snoozed' }))
    // And it says when it comes back: `snoozed_until` was fetched and rendered
    // nowhere at all.
    expect(
      await screen.findByText(`snoozed until ${formatDateTime(snoozedUntil)}`),
    ).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Un-snooze' }))
    await waitFor(() =>
      expect(variableDriftsApi.action).toHaveBeenCalledWith(
        'demo',
        'drift-3',
        { action: 'reopen', scope: undefined, snoozed_until: undefined },
        null,
      ),
    )
  })

  it('lets a snooze lapse on a panel left open, without a remount', async () => {
    // The panel's clock was `useState(() => Date.now())`, and a lazy initializer
    // runs once per mount. So a snooze that ran out while the panel sat open
    // went on reading as snoozed here — collapsed, with only Un-snooze on it —
    // while the variables table badge, which the backend recomputes per request,
    // had already counted the row as open. That is exactly the badge/panel
    // disagreement this ticket exists to remove, re-entering through the clock.
    vi.useFakeTimers()
    try {
      const snoozedUntil = new Date(Date.now() + 60_000).toISOString()
      vi.mocked(variableDriftsApi.list).mockResolvedValue({
        items: [
          { ...DRIFT, id: 'drift-4', status: 'snoozed' as const, snoozed_until: snoozedUntil },
        ],
        total: 1,
      })

      renderPanel()

      // `waitFor` cannot drive vitest's fake clock (it only detects jest's), so
      // the timers are advanced explicitly and the assertions are synchronous.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10)
      })
      expect(screen.getByRole('button', { name: 'Show 1 snoozed' })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Snooze 7d' })).not.toBeInTheDocument()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000)
      })

      // Nothing was clicked, nothing remounted, and the list was never asked
      // again — only the deadline passed.
      expect(screen.queryByRole('button', { name: 'Show 1 snoozed' })).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Snooze 7d' })).toBeInTheDocument()
      expect(variableDriftsApi.list).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows a viewer the drift without the review buttons', async () => {
    // The route behind every button takes an editor, so a viewer's click only
    // ever ended in a 403. The property's page already left them out; the
    // event's page now reads the same shared review.
    const accepted = { ...DRIFT, id: 'drift-5', status: 'accepted' as const, observed_values: ['q'] }
    vi.mocked(variableDriftsApi.list).mockResolvedValue({ items: [DRIFT, accepted], total: 2 })

    renderPanel({ canWrite: false })

    expect(await screen.findByText('${variant}')).toBeInTheDocument()
    expect(screen.getByText('x')).toBeInTheDocument()
    for (const name of ['Accept', 'Accept for event', 'Snooze 7d', 'False positive']) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
    }
    expect(screen.queryByText('Each action applies at once.')).not.toBeInTheDocument()

    // The collapsed rows stay readable, without the button that reopens them.
    fireEvent.click(screen.getByRole('button', { name: 'Show 1 resolved' }))
    expect(await screen.findByText('q')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reopen' })).not.toBeInTheDocument()
  })

  it('refreshes both drift lists, the badge and the property entries after a verdict', async () => {
    // Accept for event writes the event's own property entry, so the event's
    // property grid and the property's overrides list are stale after it; the
    // event copy of the review used to refresh only the drift and variable lists.
    vi.mocked(variableDriftsApi.list).mockResolvedValue({ items: [DRIFT], total: 1 })
    vi.mocked(variableDriftsApi.action).mockResolvedValue({ ...DRIFT, status: 'accepted' })

    const { queryClient } = renderPanel()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    fireEvent.click(await screen.findByRole('button', { name: 'Accept for event' }))

    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: branchPropertyEntriesKey('demo', null) }),
    )
    expect(invalidate).toHaveBeenCalledWith({ queryKey: branchVariableDriftsKey('demo', null) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: variablesKey('demo', null) })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: branchVariableOverridesKey('demo', null) })
  })

  it('reports a failed verdict once, inline', async () => {
    // Without the silent meta the global toast said it as well as the line here.
    vi.mocked(variableDriftsApi.list).mockResolvedValue({ items: [DRIFT], total: 1 })
    vi.mocked(variableDriftsApi.action).mockRejectedValue(new Error('Drift already resolved'))

    const { queryClient } = renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: 'False positive' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Drift already resolved')
    expect(queryClient.getMutationCache().getAll()[0]?.options.meta).toEqual(SILENT_ERROR_META)
  })
})
