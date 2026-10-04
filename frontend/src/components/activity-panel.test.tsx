import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ActivityPanel } from './activity-panel'

function implementedEvent(name: string, occurredAt: string) {
  return {
    id: `event:${name}`,
    project_id: 'project-1',
    project_slug: 'demo',
    project_name: 'Demo',
    type: 'event',
    severity: 'low',
    title: `Event implemented: ${name}`,
    detail: 'Signup',
    occurred_at: occurredAt,
    target_path: `/p/demo/monitoring/event/${name}`,
  }
}

/** One RUN of a scan, as `_scan_job_items` emits it. */
function completedRun(id: string, scanName: string, occurredAt: string) {
  return {
    id: `scan-job:${id}`,
    project_id: 'project-1',
    project_slug: 'demo',
    project_name: 'Demo',
    type: 'scan',
    severity: 'low',
    title: `Scan completed: ${scanName}`,
    detail: 'no new events discovered · 512 column combinations',
    occurred_at: occurredAt,
    target_path: '/p/demo/scans',
  }
}

function mockJsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function renderActivityPanel(slug?: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ActivityPanel open slug={slug} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ActivityPanel', () => {
  it('keeps the loaded feed when a later refresh fails (SHELL-40)', async () => {
    let fail = false
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/activity/projects/demo?limit=20')) {
        if (fail) return new Response(JSON.stringify({ detail: 'boom' }), { status: 500 })
        return mockJsonResponse([implementedEvent('Signup', new Date().toISOString())])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderActivityPanel('demo')
    expect(await screen.findByText('Event implemented: Signup')).toBeInTheDocument()

    fail = true
    fireEvent.click(screen.getByRole('button', { name: 'Refresh activity' }))

    expect(
      await screen.findByText('Could not refresh; showing the last loaded items.'),
    ).toBeInTheDocument()
    expect(screen.getByText('Event implemented: Signup')).toBeInTheDocument()
    expect(screen.queryByText('Activity unavailable')).toBeNull()
  })

  it('keeps relative times counting without a refetch (SHELL-40)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ['Date', 'setInterval', 'clearInterval'] })
    try {
      vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
        const url = String(input)
        if (url.endsWith('/api/v1/activity/projects/demo?limit=20')) {
          return mockJsonResponse([implementedEvent('Signup', new Date().toISOString())])
        }
        throw new Error(`Unhandled fetch: ${url}`)
      })
      renderActivityPanel('demo')
      expect(await screen.findByText('just now')).toBeInTheDocument()

      await act(async () => {
        vi.advanceTimersByTime(5 * 60_000)
      })
      expect(screen.queryByText('just now')).toBeNull()
      expect(screen.getByText('5m ago')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('loads project activity from the backend', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/activity/projects/demo?limit=20')) {
        return mockJsonResponse([
          {
            id: 'anomaly:1',
            project_id: 'project-1',
            project_slug: 'demo',
            project_name: 'Demo',
            type: 'anomaly',
            severity: 'high',
            title: 'Spike on Page View',
            detail: '42 actual vs 21 expected · z=7.0',
            occurred_at: new Date().toISOString(),
            target_path: '/p/demo/monitoring/event-type/type-1',
          },
        ])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    const { container } = renderActivityPanel('demo')

    expect(await screen.findByText('Spike on Page View')).toBeInTheDocument()
    expect(screen.getByText('42 actual vs 21 expected · z=7.0')).toBeInTheDocument()
    expect(container.querySelector('a[href="/p/demo/monitoring/event-type/type-1"]')).toBeInTheDocument()
  })

  it('loads workspace activity when no project slug is present', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/activity?limit=20')) {
        return mockJsonResponse([])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderActivityPanel()

    expect(await screen.findByText('No recent activity')).toBeInTheDocument()
  })

  it('labels the rail as activity rather than a live stream', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/activity/projects/demo?limit=20')) {
        return mockJsonResponse([
          {
            id: 'anomaly:1',
            project_id: 'project-1',
            project_slug: 'demo',
            project_name: 'Demo',
            type: 'anomaly',
            severity: 'high',
            title: 'Spike on Page View',
            detail: '42 actual vs 21 expected · z=7.0',
            occurred_at: new Date().toISOString(),
            target_path: '/p/demo/monitoring/event-type/type-1',
          },
        ])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderActivityPanel('demo')

    // Named as the top-bar toggle names it (#238 SH-8).
    expect(await screen.findByText('Activity')).toBeInTheDocument()
    // The old copy sold the rail as a live/streaming feed; make sure it is gone.
    expect(screen.queryByText('live')).not.toBeInTheDocument()
    expect(screen.queryByText(/streaming/i)).not.toBeInTheDocument()
    // Item age is surfaced prominently on each row.
    expect(await screen.findByText('just now')).toBeInTheDocument()
  })

  it('collapses a scan burst of implemented events into one expandable summary', async () => {
    // One scan implements every discovered event at the same instant.
    const stamp = new Date().toISOString()
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/activity/projects/demo?limit=20')) {
        return mockJsonResponse([
          implementedEvent('checkout_started', stamp),
          implementedEvent('page_view', stamp),
          implementedEvent('cart_viewed', stamp),
          implementedEvent('signup', stamp),
          implementedEvent('purchase', stamp),
        ])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderActivityPanel('demo')

    // The burst is one summary row, not five near-identical "Event implemented" rows.
    const summary = await screen.findByRole('button', { name: /5 events implemented/ })
    expect(screen.queryByText('Event implemented: checkout_started')).not.toBeInTheDocument()

    // Expanding reveals the individual items it stands in for.
    fireEvent.click(summary)
    expect(await screen.findByText('Event implemented: checkout_started')).toBeInTheDocument()
    expect(screen.getByText('Event implemented: purchase')).toBeInTheDocument()

    // Collapsing hides them again.
    fireEvent.click(summary)
    expect(screen.queryByText('Event implemented: checkout_started')).not.toBeInTheDocument()
  })

  it('keeps a sub-threshold run of events as standalone rows', async () => {
    const stamp = new Date().toISOString()
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/activity/projects/demo?limit=20')) {
        return mockJsonResponse([
          implementedEvent('checkout_started', stamp),
          implementedEvent('page_view', stamp),
        ])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderActivityPanel('demo')

    expect(await screen.findByText('Event implemented: checkout_started')).toBeInTheDocument()
    expect(screen.getByText('Event implemented: page_view')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /events implemented/ })).not.toBeInTheDocument()
  })

  it('counts a burst of scan items as runs, not as scans', async () => {
    // Three RUNS of ONE nightly scan, retried in quick succession. The old
    // summary read "3 scans completed", so a project with a single scan appeared
    // to have three — a run counted as a scan, the noun the epic settled.
    const stamp = new Date().toISOString()
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/activity/projects/demo?limit=20')) {
        return mockJsonResponse([
          completedRun('3', 'Nightly scan', stamp),
          completedRun('2', 'Nightly scan', stamp),
          completedRun('1', 'Nightly scan', stamp),
        ])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderActivityPanel('demo')

    expect(await screen.findByRole('button', { name: /3 runs completed/ })).toBeInTheDocument()
    expect(screen.queryByText(/scans completed/)).not.toBeInTheDocument()
  })

  it('narrows the rail and drops its footer when there is no activity', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/api/v1/activity?limit=20')) {
        return mockJsonResponse([])
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderActivityPanel()

    expect(await screen.findByText('No recent activity')).toBeInTheDocument()
    const rail = screen.getByRole('complementary', { name: 'Activity feed' })
    expect(rail.className).toContain('w-[220px]')
    expect(rail.className).not.toContain('w-[304px]')
    // Empty chrome is trimmed: no "last 7 days · 0 items" footer line.
    expect(screen.queryByText(/last 7 days/)).not.toBeInTheDocument()
  })
})


describe('ActivityPanel copy and controls (#238 SH-22)', () => {
  function eventItem(id: string, title: string, occurredAt: string) {
    return { ...implementedEvent(id, occurredAt), id: `event:${id}`, title }
  }

  function renderWith(items: unknown[], props: { slug?: string; onClose?: () => void } = {}) {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => mockJsonResponse(items))
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ActivityPanel open slug={props.slug ?? 'demo'} onClose={props.onClose} />
        </MemoryRouter>
      </QueryClientProvider>,
    )
  }

  it('pluralises the verb of a collapsed burst', async () => {
    const at = new Date().toISOString()
    renderWith([
      eventItem('a', 'Event needs review: A', at),
      eventItem('b', 'Event needs review: B', at),
      eventItem('c', 'Event needs review: C', at),
    ])
    expect(await screen.findByText('3 events need review')).toBeInTheDocument()
  })

  it('shows a scan signature by its values, not raw key=value pairs', async () => {
    renderWith([
      eventItem('x', 'Event implemented: event_name=Home Screen View | screen_name=Home', new Date().toISOString()),
    ])
    expect(await screen.findByText('Event implemented: Home Screen View · Home')).toBeInTheDocument()
    expect(screen.queryByText(/event_name=/)).toBeNull()
  })

  it('offers a Close button in drawer mode', async () => {
    const onClose = vi.fn()
    renderWith([], { onClose })
    fireEvent.click(await screen.findByRole('button', { name: 'Close activity' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
