import type { ReactNode } from 'react'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { TooltipProvider } from '@/components/ui/tooltip'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import EventsPage from './EventsPage'
import EventEditPage from './events/EventForm'

vi.mock('recharts', async () => {
  const actual = await vi.importActual<typeof import('recharts')>('recharts')
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  }
})

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

import { toast } from 'sonner'
import { at } from '@/test/at'
import { personaAuth } from '@/test/persona'
import { SessionProject } from '@/test/PersonaProject'

/**
 * Assert an accessible control is absent — searching the whole DOM, not just the
 * accessibility tree.
 *
 * `queryByRole` defaults to `hidden: false`, which only considers elements exposed to
 * the a11y tree. For an *absence* assertion that is the wrong default: a control still
 * present in the DOM but hidden from a11y satisfies `not.toBeInTheDocument()`, so the
 * assertion can pass for the wrong reason. Searching everything cannot.
 *
 * This is a correctness argument, not a speed one. `hidden: true` is only ~1ms cheaper
 * per query in steady state (6.7ms -> 5.5ms measured); it is the *first* role query in
 * a file that costs ~87ms, once, warming dom-accessibility-api. See tripl-mwv3.
 */
function expectAbsent(role: Parameters<typeof screen.queryByRole>[0], name: string) {
  expect(screen.queryByRole(role, { name, hidden: true })).not.toBeInTheDocument()
}

function mockJsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function makeEvent(overrides: Record<string, unknown> = {}) {
  return {
    id: 'event-1',
    project_id: 'project-1',
    event_type_id: 'type-1',
    event_type: { id: 'type-1', name: 'page', display_name: 'Page', color: '#0ea5e9' },
    name: 'Homepage View',
    description: '',
    order: 0,
    status: 'live',
    sunset_at: null,
    tags: [],
    field_values: [],
    meta_values: [],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

/** Reads back where the router ended up, so a redirect can be asserted. */
function LocationProbe() {
  const location = useLocation()
  return (
    <span data-testid="location" hidden>
      {location.pathname}
      {location.search}
    </span>
  )
}

function viewerAuth(): AuthContextValue {
  return personaAuth('viewer')
}

function renderEventsPage(
  initialEntries: string[] = ['/p/demo/events'],
  auth: AuthContextValue | null = null,
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  // The app mounts one TooltipProvider in main.tsx; the page no longer brings
  // its own (DS-12), so the test stands in for main.tsx.
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <SessionProject session={auth}>
          <TooltipProvider delayDuration={300}>
          <MemoryRouter initialEntries={initialEntries}>
            <LocationProbe />
            <Routes>
              <Route path="/p/:slug/events" element={<EventsPage />} />
              <Route path="/p/:slug/events/:tab/new" element={<EventEditPage />} />
              <Route path="/p/:slug/events/:tab/:eventId/edit" element={<EventEditPage />} />
              <Route path="/p/:slug/events/:tab" element={<EventsPage />} />
              <Route path="/p/:slug/events/:tab/:eventId" element={<EventsPage />} />
              <Route path="/p/:slug/monitoring/event/:eventId" element={<span>Event detail</span>} />
            </Routes>
          </MemoryRouter>
          </TooltipProvider>
        </SessionProject>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('EventsPage', () => {
  it('says on the edit form which branch the event lives on', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/api/v1/projects/demo/branches')) {
        return mockJsonResponse({
          items: [
            { id: 'main-1', name: 'main', kind: 'main', status: 'merged' },
            { id: 'feat-1', name: 'checkout-v2', kind: 'working', status: 'draft' },
          ],
          total: 2,
        })
      }
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/meta-fields')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/scans')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/users')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/variables')) return mockJsonResponse({ items: [], total: 0 })
      // Before the event itself: `includes` would otherwise answer the
      // discussion request with an event object.
      if (url.endsWith('/api/v1/projects/demo/events/ev-1/comments')) {
        return mockJsonResponse([])
      }
      if (url.includes('/api/v1/projects/demo/events/ev-1')) {
        return mockJsonResponse(
          makeEvent({ id: 'ev-1', name: 'checkout_started', branch_id: 'feat-1' }),
        )
      }
      if (url.includes('/api/v1/projects/demo/events')) return mockJsonResponse({ items: [], total: 0 })
      return mockJsonResponse({})
    })

    // No BranchProvider here, so the app reads as being on main while the row
    // says otherwise — the mismatch the form used to hide until Save 404d.
    renderEventsPage(['/p/demo/events/all/ev-1/edit'])

    const banner = await screen.findByTestId('entity-branch-banner')
    expect(banner).toHaveTextContent(/lives on branch checkout-v2/i)
    expect(within(banner).getByRole('link', { name: /Switch to checkout-v2/i })).toHaveAttribute(
      'href',
      '/p/demo/events/all/ev-1/edit?branch=feat-1',
    )
  })

  it('carries ?branch= through the redirect into the editor', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/meta-fields')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/variables')) return mockJsonResponse({ items: [], total: 0 })
      if (url.endsWith('/api/v1/projects/demo/events/tags')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events')) return mockJsonResponse({ items: [], total: 0 })
      return mockJsonResponse({})
    })

    renderEventsPage(['/p/demo/events/all/ev-1?branch=feat-1'])

    // Dropping the param here turns a shared branch-diff link into a main-plan
    // edit, which renders normally and 404s at Save (tripl-h2sx.2).
    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent(
        '/p/demo/events/all/ev-1/edit?branch=feat-1',
      ),
    )
  })

  it('sends a viewer on an event link to its read view, not the editor (EV-34)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => mockJsonResponse({ items: [], total: 0 }))

    renderEventsPage(['/p/demo/events/all/ev-1?branch=feat-1'], viewerAuth())

    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent(
        '/p/demo/monitoring/event/ev-1?branch=feat-1',
      ),
    )
    expect(screen.getByText('Event detail')).toBeInTheDocument()
  })

  it('renders monitoring signal links for active view and rows', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)

      if (url.endsWith('/api/v1/projects/demo/event-types')) {
        return mockJsonResponse([
          {
            id: 'type-1',
            project_id: 'project-1',
            name: 'page',
            display_name: 'Page',
            description: '',
            color: '#0ea5e9',
            order: 0,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
            field_definitions: [],
          },
        ])
      }
      if (url.endsWith('/api/v1/projects/demo/meta-fields')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/variables')) return mockJsonResponse({ items: [], total: 0 })
      if (url.endsWith('/api/v1/projects/demo/events/tags')) return mockJsonResponse([])
      // unreviewedCount query: exactly status=in_review with limit=1
      if (url.includes('/api/v1/projects/demo/events') && url.includes('status=in_review') && url.includes('limit=1')) {
        return mockJsonResponse({ items: [], total: 0 })
      }
      if (url.includes('/api/v1/projects/demo/events-metrics')) {
        return mockJsonResponse({
          scope: 'events_total',
          scan_config_id: null,
          event_id: null,
          event_type_id: null,
          interval: '1h',
          latest_signal: null,
          data: [],
        })
      }
      if (url.endsWith('/api/v1/projects/demo/events/window-metrics') && init?.method === 'POST') {
        return mockJsonResponse([
          {
            event_id: 'event-1',
            scan_config_id: 'scan-1',
            interval: '1h',
            total_count: 1200,
            data: [
              {
                bucket: '2026-01-01T00:00:00Z',
                count: 500,
                expected_count: null,
                is_anomaly: false,
                anomaly_direction: null,
                z_score: null,
              },
              {
                bucket: '2026-01-01T12:00:00Z',
                count: 700,
                expected_count: null,
                is_anomaly: false,
                anomaly_direction: null,
                z_score: null,
              },
            ],
          },
        ])
      }
      if (url.includes('/api/v1/projects/demo/anomalies/signals')) {
        return mockJsonResponse([
          {
            scan_config_id: 'scan-1',
            scope_type: 'project_total',
            scope_ref: 'scan-1',
            state: 'latest_scan',
            event_id: null,
            event_type_id: null,
            bucket: '2026-01-02T00:00:00Z',
            actual_count: 0,
            expected_count: 15,
            stddev: 0,
            z_score: -15,
            direction: 'drop',
          },
          {
            scan_config_id: 'scan-1',
            scope_type: 'event_type',
            scope_ref: 'type-1',
            state: 'recent',
            event_id: null,
            event_type_id: 'type-1',
            bucket: '2026-01-02T00:00:00Z',
            actual_count: 0,
            expected_count: 15,
            stddev: 0,
            z_score: -15,
            direction: 'drop',
          },
          {
            scan_config_id: 'scan-1',
            scope_type: 'event',
            scope_ref: 'event-1',
            state: 'recent',
            event_id: 'event-1',
            event_type_id: null,
            bucket: '2026-01-02T00:00:00Z',
            actual_count: 0,
            expected_count: 10,
            stddev: 0,
            z_score: -10,
            direction: 'drop',
          },
        ])
      }
      if (url.includes('/api/v1/projects/demo/events')) {
        return mockJsonResponse({
          items: [makeEvent()],
          total: 1,
        })
      }

      throw new Error(`Unhandled fetch: ${url}`)
    })

    const { container } = renderEventsPage()

    expect(await screen.findByText('Homepage View')).toBeInTheDocument()

    // One traversal for every column instead of a `getByRole` per header. This is a
    // clarity win — the ordering assertion now reads off the header list directly,
    // rather than through compareDocumentPosition — and only a marginal speed one.
    // See tripl-mwv3: role queries cost ~6.7ms each in steady state here, so the
    // thirteen in this test are not what makes it slow.
    const headers = screen.getAllByRole('columnheader').map((h) => h.textContent?.trim())
    expect(headers).toContain('Event')
    // The volume and trend lead; Type follows (EV-12).
    expect(headers.indexOf('48h')).toBeLessThan(headers.indexOf('Type'))
    // tripl-jfm3.4: the signal-state column is headed "Signal", not "Monitor" —
    // its cells report detection output, which exists without any monitor, and
    // heading it "Monitor" contradicted the Monitors page's "No monitors yet".
    expect(headers).toContain('Signal')
    expect(headers).not.toContain('Monitor')
    // The trailing sticky "Actions" column and its hover cluster were removed;
    // reordering is now drag-handle only.
    expect(headers).not.toContain('Actions')
    expect(screen.getByText('48h')).toBeInTheDocument()
    // The volume chart starts collapsed (EV-21); opening it shows its controls.
    fireEvent.click(screen.getByRole('button', { name: /Show chart/ }))
    expect(await screen.findByRole('button', { name: '7d' })).toBeInTheDocument()
    // The fixture's series is empty: the card stays a header that says so,
    // without a bucket select for a chart that is not there (EV-16).
    expect(await screen.findByText(/No volume in the last 7 days/)).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Time granularity' })).not.toBeInTheDocument()
    // An image, not a button: pressing it did nothing, so it was a dead tab
    // stop on every row (EVT-46).
    const metricsButton = await screen.findByRole('img', { name: /Homepage View metrics: 1K events in last 48 hours/ })
    expect(metricsButton).toBeInTheDocument()
    // The row exposes no inline action buttons — Edit/Metrics/Archive/Delete and
    // move/status now live on the event detail page, not on the row.
    // (`expectAbsent` searches the whole DOM, not just the a11y tree — see its docstring.)
    expectAbsent('button', 'Edit event')
    // The toolbar's own "More actions" overflow (tripl-7l83.9) lives above the
    // grid; scope this row-cleanliness check to the events table so it verifies
    // rows carry no per-row action menu, not the toolbar affordance.
    const eventsGrid = container.querySelector('table')
    expect(eventsGrid?.querySelector('button[aria-label="More actions"]')).toBeNull()
    // tripl-dmch.12 dropped the per-row SignalLink arrow anchors (one incident =
    // one saturated indicator, the Monitor-cell chip). The only surviving
    // monitoring anchor here is the open tab volume card's "View signal" link
    // for the active tab (project_total); the row-level event/event-type anchors
    // and the "Open recent anomaly" affordance are gone.
    expect(container.querySelector('a[href="/p/demo/monitoring/project-total/scan-1"]')).toBeInTheDocument()
    expect(container.querySelector('a[href="/p/demo/monitoring/event-type/type-1"]')).not.toBeInTheDocument()
    // tripl-fa8l made the event NAME the row's monitoring anchor, so this href
    // is expected again — what tripl-dmch.12 removed was the separate SignalLink
    // arrow, which the "Open recent anomaly" assertion below still guards.
    expect(screen.getByRole('link', { name: 'Homepage View' })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/event/event-1',
    )
    expect(screen.queryByLabelText('Open recent anomaly')).not.toBeInTheDocument()

    fireEvent.mouseOver(metricsButton)
    fireEvent.focus(metricsButton)
    expect((await screen.findAllByText('Last 48 hours')).length).toBeGreaterThan(0)
    expect(screen.getAllByText('1K events').length).toBeGreaterThan(0)

    // The hover action cluster was removed entirely — no move up/down buttons
    // and no per-row status select.
    expectAbsent('button', 'Move event up')
    expectAbsent('button', 'Move event down')
    expectAbsent('combobox', 'Set event status')
    expectAbsent('link', 'View metrics')
    expectAbsent('button', 'Archive event')
    expectAbsent('button', 'Delete event')

    // Opened above, the toggle reads "Hide chart"; the signal link sits in the
    // card header either way.
    expect(screen.getByRole('button', { name: /Hide chart/ })).toBeInTheDocument()
    expect(await screen.findByText('View signal')).toBeInTheDocument()

    // Header stat scope. This is a component-level assertion over a synthetic
    // payload: the stat counts only the aggregate series this page charts, so
    // the fixture's project_total + event_type rows give 2 and its event row is
    // ignored. The label must not read as a project-wide anomaly count, which is
    // what the sidebar "Anomalies" badge reports on a different basis.
    const chartSignalsStat = screen.getByText('Open signals').closest('dl')
    expect(chartSignalsStat).not.toBeNull()
    expect(chartSignalsStat).toHaveTextContent('2')
    expect(chartSignalsStat).toHaveTextContent('open')
    expect(screen.queryByText('Active signals')).not.toBeInTheDocument()
    // The scope note is a focusable button, not hover-only chrome.
    expect(
      screen.getByRole('button', { name: /Open anomalies on the volume the chart shows/ }),
    ).toBeInTheDocument()
  }, 10_000)

  it('renders active event-type anomaly link for sidebar-selected view', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)

      if (url.endsWith('/api/v1/projects/demo/event-types')) {
        return mockJsonResponse([
          {
            id: 'type-1',
            project_id: 'project-1',
            name: 'page',
            display_name: 'Page',
            description: '',
            color: '#ec4899',
            order: 0,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
            field_definitions: [],
          },
        ])
      }
      if (url.endsWith('/api/v1/projects/demo/meta-fields')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/variables')) return mockJsonResponse({ items: [], total: 0 })
      if (url.endsWith('/api/v1/projects/demo/events/tags')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events') && url.includes('status=in_review') && url.includes('limit=1')) {
        return mockJsonResponse({ items: [], total: 0 })
      }
      if (url.includes('/api/v1/projects/demo/events-metrics')) {
        return mockJsonResponse({
          scope: 'events_total',
          scan_config_id: null,
          event_id: null,
          event_type_id: null,
          interval: '1h',
          latest_signal: null,
          data: [],
        })
      }
      if (url.endsWith('/api/v1/projects/demo/events/window-metrics') && init?.method === 'POST') {
        return mockJsonResponse([])
      }
      if (url.endsWith('/api/v1/projects/demo/anomalies/signals')) {
        return mockJsonResponse([
          {
            scan_config_id: 'scan-1',
            scope_type: 'project_total',
            scope_ref: 'scan-1',
            state: 'latest_scan',
            event_id: null,
            event_type_id: null,
            bucket: '2026-01-02T00:00:00Z',
            actual_count: 0,
            expected_count: 20,
            stddev: 0,
            z_score: -20,
            direction: 'drop',
          },
          {
            scan_config_id: 'scan-1',
            scope_type: 'event_type',
            scope_ref: 'type-1',
            state: 'recent',
            event_id: null,
            event_type_id: 'type-1',
            bucket: '2026-01-02T00:00:00Z',
            actual_count: 0,
            expected_count: 12,
            stddev: 0,
            z_score: -12,
            direction: 'drop',
          },
        ])
      }
      if (url.endsWith('/api/v1/projects/demo/anomalies/signals/query') && init?.method === 'POST') {
        return mockJsonResponse([])
      }
      if (url.includes('/api/v1/projects/demo/events')) {
        return mockJsonResponse({
          items: [makeEvent({ id: 'active-event-1', name: 'Active Signup', event_type: { id: 'type-1', name: 'page', display_name: 'Page', color: '#ec4899' } })],
          total: 1,
        })
      }

      throw new Error(`Unhandled fetch: ${url}`)
    })

    const { container } = renderEventsPage(['/p/demo/events/page'])

    expect(await screen.findByText('Active Signup')).toBeInTheDocument()
    expect(screen.getByText('Page volume')).toBeInTheDocument()
    await waitFor(() => {
      expect(container.querySelector('a[href="/p/demo/monitoring/event-type/type-1"]')).toBeInTheDocument()
    })
    expect(container.querySelector('a[href="/p/demo/monitoring/project-total/scan-1"]')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'All' })).not.toBeInTheDocument()
  })

  it('offers a viewer no create, select, reorder or edit controls (EVT-9)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/meta-fields')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/variables')) return mockJsonResponse({ items: [], total: 0 })
      if (url.endsWith('/api/v1/projects/demo/events/tags')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/users')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events-metrics')) {
        return mockJsonResponse({
          scope: 'events_total',
          scan_config_id: null,
          event_id: null,
          event_type_id: null,
          interval: '1h',
          latest_signal: null,
          data: [],
        })
      }
      if (url.endsWith('/api/v1/projects/demo/events/window-metrics') && init?.method === 'POST') {
        return mockJsonResponse([])
      }
      if (url.includes('/api/v1/projects/demo/anomalies/signals')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events')) {
        return mockJsonResponse({
          items: [makeEvent({ id: 'event-1', name: 'Homepage View', status: 'live' })],
          total: 1,
        })
      }
      return mockJsonResponse({})
    })

    renderEventsPage(['/p/demo/events'], viewerAuth())

    expect(await screen.findByText('Homepage View')).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent(/viewer role/)
    expectAbsent('button', 'New event')
    expectAbsent('checkbox', 'Select Homepage View')
    expectAbsent('checkbox', 'Select all visible events')
    // With no select-all, the phone header bar would be empty: it is hidden
    // below md for a viewer (EV-28).
    const headerRow = screen.getByRole('columnheader', { name: 'Event' }).closest('tr') as HTMLElement
    expect(headerRow.className).toContain('max-md:hidden')
    expectAbsent('button', 'Drag to reorder Homepage View')
    expectAbsent('button', 'Edit Homepage View')
  })

  it('sends a viewer on the edit URL to the event page, not a disabled form (EV-34 / AU-33)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/api/v1/projects/demo/branches')) return mockJsonResponse({ items: [], total: 0 })
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/meta-fields')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/scans')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/users')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/variables')) return mockJsonResponse({ items: [], total: 0 })
      if (url.endsWith('/api/v1/projects/demo/events/ev-1/comments')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events/ev-1')) {
        return mockJsonResponse(makeEvent({ id: 'ev-1', name: 'checkout_started' }))
      }
      if (url.includes('/api/v1/projects/demo/events')) return mockJsonResponse({ items: [], total: 0 })
      return mockJsonResponse({})
    })

    renderEventsPage(['/p/demo/events/all/ev-1/edit'], viewerAuth())

    // The page built for reading: the event's detail page. No form is drawn
    // on the way, disabled or not.
    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent('/p/demo/monitoring/event/ev-1'),
    )
    expect(screen.getByText('Event detail')).toBeInTheDocument()
    expect(document.querySelector('fieldset')).toBeNull()
    expectAbsent('button', 'Save event')
  })

  it('supports selecting multiple events and bulk deleting them', async () => {
    const bulkDeleteBodies: unknown[] = []

    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)

      if (url.endsWith('/api/v1/projects/demo/event-types')) {
        return mockJsonResponse([
          {
            id: 'type-1',
            project_id: 'project-1',
            name: 'page',
            display_name: 'Page',
            description: '',
            color: '#0ea5e9',
            order: 0,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
            field_definitions: [],
          },
        ])
      }
      if (url.endsWith('/api/v1/projects/demo/meta-fields')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/variables')) return mockJsonResponse({ items: [], total: 0 })
      if (url.endsWith('/api/v1/projects/demo/events/tags')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events') && url.includes('status=in_review') && url.includes('limit=1')) return mockJsonResponse({ items: [], total: 0 })
      if (url.includes('/api/v1/projects/demo/events-metrics')) {
        return mockJsonResponse({
          scope: 'events_total',
          scan_config_id: null,
          event_id: null,
          event_type_id: null,
          interval: '1h',
          latest_signal: null,
          data: [],
        })
      }
      if (url.endsWith('/api/v1/projects/demo/events/window-metrics') && init?.method === 'POST') {
        return mockJsonResponse([])
      }
      if (url.includes('/api/v1/projects/demo/anomalies/signals')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/events/bulk-delete') && init?.method === 'POST') {
        bulkDeleteBodies.push(JSON.parse(String(init.body)))
        return new Response(null, { status: 204 })
      }
      if (url.includes('/api/v1/projects/demo/events')) {
        return mockJsonResponse({
          items: [
            makeEvent({ id: 'event-1', name: 'Homepage View', status: 'live' }),
            makeEvent({ id: 'event-2', name: 'Settings View', order: 1, status: 'draft' }),
          ],
          total: 2,
        })
      }

      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderEventsPage()

    expect(await screen.findByText('Homepage View')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Select Homepage View'))
    // One of two selected: the header box reads "mixed", and a click from
    // there clears the selection rather than selecting everything (EV-26).
    const selectAll = screen.getByRole('checkbox', { name: 'Select all visible events' })
    expect(selectAll).toHaveAttribute('aria-checked', 'mixed')
    fireEvent.click(selectAll)
    expect(screen.getByLabelText('Select Homepage View')).toHaveAttribute('aria-checked', 'false')

    fireEvent.click(screen.getByLabelText('Select Homepage View'))
    fireEvent.click(screen.getByLabelText('Select Settings View'))

    expect(
      screen.getByText((_, node) => node?.textContent === '2 selected' && node.tagName === 'SPAN'),
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Delete selected' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))

    await waitFor(() => {
      expect(bulkDeleteBodies).toContainEqual({ event_ids: ['event-1', 'event-2'] })
    })
  })

  it('supports bulk status transitions for selected events', async () => {
    const bulkUpdateBodies: unknown[] = []

    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)

      if (url.endsWith('/api/v1/projects/demo/event-types')) {
        return mockJsonResponse([
          {
            id: 'type-1',
            project_id: 'project-1',
            name: 'page',
            display_name: 'Page',
            description: '',
            color: '#0ea5e9',
            order: 0,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
            field_definitions: [],
          },
        ])
      }
      if (url.endsWith('/api/v1/projects/demo/meta-fields')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/variables')) return mockJsonResponse({ items: [], total: 0 })
      if (url.endsWith('/api/v1/projects/demo/events/tags')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events') && url.includes('status=in_review') && url.includes('limit=1')) return mockJsonResponse({ items: [], total: 0 })
      if (url.includes('/api/v1/projects/demo/events-metrics')) {
        return mockJsonResponse({
          scope: 'events_total',
          scan_config_id: null,
          event_id: null,
          event_type_id: null,
          interval: '1h',
          latest_signal: null,
          data: [],
        })
      }
      if (url.endsWith('/api/v1/projects/demo/events/window-metrics') && init?.method === 'POST') {
        return mockJsonResponse([])
      }
      if (url.includes('/api/v1/projects/demo/anomalies/signals')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/events/bulk-update') && init?.method === 'POST') {
        bulkUpdateBodies.push(JSON.parse(String(init.body)))
        return new Response(null, { status: 204 })
      }
      if (url.includes('/api/v1/projects/demo/events')) {
        return mockJsonResponse({
          items: [
            makeEvent({ id: 'event-1', name: 'Homepage View', status: 'live' }),
            makeEvent({ id: 'event-2', name: 'Settings View', order: 1, status: 'draft' }),
          ],
          total: 2,
        })
      }

      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderEventsPage()

    expect(await screen.findByText('Homepage View')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Select Homepage View'))
    fireEvent.click(screen.getByLabelText('Select Settings View'))

    // BulkActionBar shows a "Set status…" select
    expect(screen.getByRole('combobox', { name: 'Set status' })).toBeInTheDocument()
  })

  it('creates an event with selected event-level metric breakdowns', async () => {
    const eventCreateBodies: unknown[] = []

    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)

      if (url.endsWith('/api/v1/projects/demo/event-types')) {
        return mockJsonResponse([
          {
            id: 'type-1',
            project_id: 'project-1',
            name: 'page',
            display_name: 'Page',
            description: '',
            color: '#0ea5e9',
            order: 0,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
            field_definitions: [
              {
                id: 'field-country',
                event_type_id: 'type-1',
                name: 'country',
                display_name: 'Country',
                field_type: 'string',
                is_required: false,
                enum_options: null,
                description: '',
                order: 0,
                sensitivity: 'none',
              },
              {
                id: 'field-payload',
                event_type_id: 'type-1',
                name: 'payload',
                display_name: 'Payload',
                field_type: 'json',
                is_required: false,
                enum_options: null,
                description: '',
                order: 1,
                sensitivity: 'none',
              },
            ],
          },
        ])
      }
      if (url.endsWith('/api/v1/projects/demo/meta-fields')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/variables')) return mockJsonResponse({ items: [], total: 0 })
      if (url.endsWith('/api/v1/projects/demo/events/tags')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events') && url.includes('status=in_review') && url.includes('limit=1')) return mockJsonResponse({ items: [], total: 0 })
      if (url.includes('/api/v1/projects/demo/events-metrics')) {
        return mockJsonResponse({
          scope: 'events_total',
          scan_config_id: null,
          event_id: null,
          event_type_id: null,
          interval: '1h',
          latest_signal: null,
          data: [],
        })
      }
      if (url.endsWith('/api/v1/projects/demo/events/window-metrics') && init?.method === 'POST') {
        return mockJsonResponse([])
      }
      if (url.includes('/api/v1/projects/demo/anomalies/signals')) return mockJsonResponse([])
      // The form asks the scans which columns the project can collect, and for
      // the rule that names events of this type. None here: the breakdown chips
      // then come from the event type's own scalar fields.
      if (url.includes('/api/v1/projects/demo/scans')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/events') && init?.method === 'POST') {
        const body = JSON.parse(String(init.body))
        eventCreateBodies.push(body)
        return mockJsonResponse({
          ...makeEvent({ name: body.name, status: body.status ?? 'draft', metric_breakdown_columns: body.metric_breakdown_columns }),
          event_type_id: body.event_type_id,
        })
      }
      if (url.includes('/api/v1/projects/demo/events')) {
        return mockJsonResponse({ items: [], total: 0 })
      }

      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderEventsPage()

    // "New event" navigates to the page-based editor (no Sheet/dialog).
    // Two on an empty project: the toolbar's and the empty state's.
    fireEvent.click((await screen.findAllByRole('button', { name: 'New event' }))[0]!)
    expect(await screen.findByRole('heading', { name: 'New event' })).toBeInTheDocument()

    fireEvent.change(at(screen.getAllByRole('combobox'), 0), { target: { value: 'type-1' } })
    fireEvent.change(screen.getByPlaceholderText('The exact name the app sends'), {
      target: { value: 'Homepage View' },
    })
    // Breakdown options come from the type's scalar fields and the project's
    // scans — 'country' is a field on type-1, so it is offered as a chip. A
    // column neither knows about is typed in, which is the half of the picker
    // the redesign dropped and the docs never stopped describing (tripl-u2h9.6).
    fireEvent.click(screen.getByRole('button', { name: 'country' }))
    const breakdownInput = screen.getByLabelText(/Metric breakdowns/)
    fireEvent.change(breakdownInput, { target: { value: 'platform' } })
    fireEvent.keyDown(breakdownInput, { key: 'Enter' })
    fireEvent.click(screen.getByRole('button', { name: 'Create event' }))

    await waitFor(() => {
      expect(eventCreateBodies).toContainEqual(
        expect.objectContaining({
          event_type_id: 'type-1',
          name: 'Homepage View',
          description: '',
          status: 'draft',
          metric_breakdown_columns: ['country', 'platform'],
          tags: [],
          field_values: [],
          meta_values: [],
        }),
      )
    })
  })

  it('collapses the toolbar and hides the metrics chart until the project has events', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)

      if (url.endsWith('/api/v1/projects/demo/event-types')) {
        return mockJsonResponse([
          {
            id: 'type-1',
            project_id: 'project-1',
            name: 'page',
            display_name: 'Page',
            description: '',
            color: '#0ea5e9',
            order: 0,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
            field_definitions: [],
          },
        ])
      }
      if (url.endsWith('/api/v1/projects/demo/meta-fields')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/variables')) return mockJsonResponse({ items: [], total: 0 })
      if (url.endsWith('/api/v1/projects/demo/events/tags')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events') && url.includes('status=in_review') && url.includes('limit=1')) {
        return mockJsonResponse({ items: [], total: 0 })
      }
      if (url.includes('/api/v1/projects/demo/events-metrics')) {
        return mockJsonResponse({
          scope: 'events_total',
          scan_config_id: null,
          event_id: null,
          event_type_id: null,
          interval: '1h',
          latest_signal: null,
          data: [],
        })
      }
      if (url.endsWith('/api/v1/projects/demo/events/window-metrics') && init?.method === 'POST') {
        return mockJsonResponse([])
      }
      if (url.includes('/api/v1/projects/demo/anomalies/signals')) return mockJsonResponse([])
      // The project has zero events.
      if (url.includes('/api/v1/projects/demo/events')) {
        return mockJsonResponse({ items: [], total: 0 })
      }

      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderEventsPage()

    // The empty state renders and the primary "New event" action stays reachable.
    expect(await screen.findByText('No events yet')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'New event' }).length).toBeGreaterThan(0)

    // The toolbar collapses only once the events query has SETTLED — not during the
    // initial load, so a populated project never flashes the minimal bar
    // (tripl-yfsj.12). Wait for the search field to disappear before the synchronous
    // checks below.
    await waitFor(() =>
      expect(
        screen.queryByRole('searchbox', {
          name: 'Search events',
          hidden: true,
        }),
      ).not.toBeInTheDocument(),
    )

    // The rest of the filter toolbar (Status/Activity/Sort/Views/Columns/More) is
    // gone too — nothing to act on. (`expectAbsent` searches the whole DOM.)
    expectAbsent('button', 'Status filter')
    expectAbsent('combobox', 'Activity filter')
    expectAbsent('combobox', 'Sort order')
    expectAbsent('button', 'More actions')

    // The empty "Event volume" chart card is gone until events exist.
    expect(screen.queryByText('Event volume')).not.toBeInTheDocument()
    expect(screen.queryByText('No recent volume to chart')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Show chart|Hide chart/ })).not.toBeInTheDocument()
  })
})

/**
 * Fetch mock for the export tests. Everything the page loads answers at once;
 * only the events LIST is steerable — `listGate` holds it open (the cold-load
 * window, where `total` is still 0) and `failExportSweep` fails the
 * `limit=10000` request the export pages the whole match set with.
 *
 * The sweep branch must be tested BEFORE the in-review count branch: the sweep
 * URL carries `status=in_review` too, and `limit=10000` contains `limit=1`.
 */
function mockExportFetch({
  listGate,
  failExportSweep = false,
}: { listGate?: Promise<void>; failExportSweep?: boolean } = {}) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input)

    if (url.endsWith('/api/v1/projects/demo/event-types')) {
      return mockJsonResponse([
        {
          id: 'type-1',
          project_id: 'project-1',
          name: 'page',
          display_name: 'Page',
          description: '',
          color: '#0ea5e9',
          order: 0,
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          field_definitions: [],
        },
      ])
    }
    if (url.endsWith('/api/v1/projects/demo/meta-fields')) return mockJsonResponse([])
    if (url.includes('/api/v1/projects/demo/variables')) return mockJsonResponse({ items: [], total: 0 })
    if (url.endsWith('/api/v1/projects/demo/events/tags')) return mockJsonResponse([])
    if (url.endsWith('/api/v1/projects/demo/events/window-metrics') && init?.method === 'POST') {
      return mockJsonResponse([])
    }
    if (url.includes('/api/v1/projects/demo/events-metrics')) {
      return mockJsonResponse({
        scope: 'events_total',
        scan_config_id: null,
        event_id: null,
        event_type_id: null,
        interval: '1h',
        latest_signal: null,
        data: [],
      })
    }
    if (url.includes('/api/v1/projects/demo/anomalies/signals')) return mockJsonResponse([])
    if (url.includes('/api/v1/projects/demo/events') && url.includes('limit=10000')) {
      if (failExportSweep) return new Response('boom', { status: 500 })
      return mockJsonResponse({ items: [makeEvent()], total: 1 })
    }
    if (url.includes('/api/v1/projects/demo/events') && url.includes('status=in_review') && url.includes('limit=1')) {
      return mockJsonResponse({ items: [], total: 0 })
    }
    if (url.includes('/api/v1/projects/demo/events')) {
      if (listGate) await listGate
      return mockJsonResponse({ items: [makeEvent()], total: 1 })
    }

    throw new Error(`Unhandled fetch: ${url}`)
  })
}

async function openExportItem() {
  // Radix opens the menu from pointer/keyboard events jsdom does not synthesise
  // from a bare click — keyboard is the reliable path here.
  fireEvent.keyDown(await screen.findByRole('button', { name: 'More actions' }), { key: 'Enter' })
  return screen.findByRole('menuitem', { name: /Export CSV/ })
}

describe('EventsPage CSV export', () => {
  it('withholds the export until the loaded page belongs to the current filters', async () => {
    let releaseList = () => {}
    const listGate = new Promise<void>((resolve) => {
      releaseList = resolve
    })
    mockExportFetch({ listGate })

    renderEventsPage()

    // Cold load: `total` is 0 and the sweep short-circuits on it, so exporting
    // here downloaded a header-only file indistinguishable from "nothing matched".
    expect(await openExportItem()).toHaveAttribute('aria-disabled', 'true')

    releaseList()

    await waitFor(() =>
      expect(screen.getByRole('menuitem', { name: /Export CSV/ })).not.toHaveAttribute(
        'aria-disabled',
        'true',
      ),
    )
  })

  it('reports a failed export instead of leaving the menu item to flip back silently', async () => {
    vi.mocked(toast.error).mockClear()
    mockExportFetch({ failExportSweep: true })

    renderEventsPage()

    expect(await screen.findByText('Homepage View')).toBeInTheDocument()
    fireEvent.click(await openExportItem())

    // The sweep is a bare awaited request: no query cache, no error boundary, so
    // without the catch the only trace was an unhandled rejection in the console.
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(expect.stringContaining('Could not export CSV')),
    )
  })
})

const SCREEN_FIELD = {
  id: 'fd-screen',
  event_type_id: 'type-1',
  name: 'screen',
  display_name: 'Screen',
  field_type: 'string',
  is_required: false,
  enum_options: null,
  order: 0,
}

/**
 * One fetch stub for the catalog tests below: a single event type with a
 * `screen` field, the list answering with `events`, and every side query the
 * page makes answered with a valid empty payload. Records the list URLs and
 * the bulk request bodies it sees.
 */
function mockCatalogFetch({
  events,
  listGate,
  withFieldlessType = false,
}: {
  events: ReturnType<typeof makeEvent>[]
  listGate?: Promise<void>
  /** Adds a second type without the `screen` field, so on the All tab
   *  `screen` is a type-specific column that starts hidden (EV-11). */
  withFieldlessType?: boolean
}) {
  const listUrls: string[] = []
  const bulkDeleteBodies: unknown[] = []
  const bulkUpdateBodies: unknown[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input)
    if (url.endsWith('/api/v1/projects/demo/event-types')) {
      return mockJsonResponse([
        {
          id: 'type-1',
          project_id: 'project-1',
          name: 'page',
          display_name: 'Page',
          description: '',
          color: '#0ea5e9',
          order: 0,
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          field_definitions: [SCREEN_FIELD],
        },
        ...(withFieldlessType
          ? [
              {
                id: 'type-2',
                project_id: 'project-1',
                name: 'action',
                display_name: 'Action',
                description: '',
                color: '#f97316',
                order: 1,
                created_at: '2026-01-01T00:00:00Z',
                updated_at: '2026-01-01T00:00:00Z',
                field_definitions: [],
              },
            ]
          : []),
      ])
    }
    if (url.endsWith('/api/v1/projects/demo/meta-fields')) return mockJsonResponse([])
    if (url.includes('/api/v1/projects/demo/variables')) return mockJsonResponse({ items: [], total: 0 })
    if (url.endsWith('/api/v1/projects/demo/events/tags')) return mockJsonResponse([])
    if (url.endsWith('/api/v1/users')) return mockJsonResponse([])
    if (url.endsWith('/api/v1/projects/demo/events/window-metrics') && init?.method === 'POST') {
      return mockJsonResponse([])
    }
    if (url.includes('/api/v1/projects/demo/events-metrics')) {
      return mockJsonResponse({
        scope: 'events_total',
        scan_config_id: null,
        event_id: null,
        event_type_id: null,
        interval: '1h',
        latest_signal: null,
        data: [],
      })
    }
    if (url.includes('/api/v1/projects/demo/anomalies/signals')) return mockJsonResponse([])
    if (url.endsWith('/api/v1/projects/demo/events/bulk-delete') && init?.method === 'POST') {
      bulkDeleteBodies.push(JSON.parse(String(init.body)))
      return new Response(null, { status: 204 })
    }
    if (url.endsWith('/api/v1/projects/demo/events/bulk-update') && init?.method === 'POST') {
      bulkUpdateBodies.push(JSON.parse(String(init.body)))
      return new Response(null, { status: 204 })
    }
    if (url.includes('/api/v1/projects/demo/events') && url.includes('status=in_review') && url.includes('limit=1')) {
      return mockJsonResponse({ items: [], total: 0 })
    }
    if (url.includes('/api/v1/projects/demo/events')) {
      listUrls.push(url)
      if (listGate) await listGate
      return mockJsonResponse({ items: events, total: events.length })
    }
    return mockJsonResponse({})
  })
  return { listUrls, bulkDeleteBodies, bulkUpdateBodies }
}

function screenEvent(id: string, name: string, screen: string) {
  return makeEvent({
    id,
    name,
    drift_count: 0,
    field_values: [{ id: `fv-${id}`, field_definition_id: SCREEN_FIELD.id, value: screen }],
  })
}

describe('EventsPage current view', () => {
  it('keeps a filtered type-specific column visible on the All tab (EV-11)', async () => {
    // A link or saved view carrying `f.screen` narrows the rows; hiding the
    // Screen header by default would hide where that filter is shown.
    mockCatalogFetch({
      events: [screenEvent('event-1', 'checkout_view', 'checkout')],
      withFieldlessType: true,
    })

    renderEventsPage(['/p/demo/events?f.screen=checkout'])

    expect(await screen.findByText('checkout_view')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Filter Screen' })).toBeInTheDocument()
  })

  it('starts an unfiltered type-specific column hidden on the All tab (EV-11)', async () => {
    mockCatalogFetch({
      events: [screenEvent('event-1', 'checkout_view', 'checkout')],
      withFieldlessType: true,
    })

    renderEventsPage()

    expect(await screen.findByText('checkout_view')).toBeInTheDocument()
    expectAbsent('button', 'Filter Screen')
  })

  it('selects only the rows a column filter leaves when selecting all matching (EVT-2)', async () => {
    // The column filter showed 12 rows; "Select all" took the server's 5,000
    // and "Delete selected" deleted them all.
    const { bulkDeleteBodies } = mockCatalogFetch({
      events: [
        screenEvent('event-1', 'checkout_view', 'checkout'),
        screenEvent('event-2', 'home_view', 'home'),
        screenEvent('event-3', 'cart_view', 'cart'),
      ],
    })

    renderEventsPage(['/p/demo/events?f.screen=checkout'])

    expect(await screen.findByText('checkout_view')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText('home_view')).not.toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Select checkout_view'))
    fireEvent.click(screen.getByRole('button', { name: 'Select all matching' }))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Selecting…' })).not.toBeInTheDocument(),
    )

    fireEvent.click(screen.getByRole('button', { name: 'Delete selected' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))

    await waitFor(() => expect(bulkDeleteBodies).toEqual([{ event_ids: ['event-1'] }]))
  })

  it('offers no drag-reorder while sorted busiest first (EVT-3)', async () => {
    mockCatalogFetch({ events: [screenEvent('event-1', 'checkout_view', 'checkout')] })

    renderEventsPage(['/p/demo/events?sort=volume'])

    expect(await screen.findByText('checkout_view')).toBeInTheDocument()
    expect(screen.getByLabelText('Select checkout_view')).toBeInTheDocument()
    expectAbsent('button', 'Drag to reorder checkout_view')
  })

  it('says it is loading, not "No events yet", during the cold load (EVT-14)', async () => {
    let releaseList = () => {}
    const listGate = new Promise<void>((resolve) => {
      releaseList = resolve
    })
    mockCatalogFetch({ events: [screenEvent('event-1', 'checkout_view', 'checkout')], listGate })

    renderEventsPage()

    expect(await screen.findByText('Loading events…')).toBeInTheDocument()
    expect(screen.queryByText('No events yet')).not.toBeInTheDocument()

    releaseList()
    expect(await screen.findByText('checkout_view')).toBeInTheDocument()
  })

  it('keeps the full toolbar on an empty Review tab (EVT-15)', async () => {
    mockCatalogFetch({ events: [] })

    renderEventsPage(['/p/demo/events/review'])

    expect(await screen.findByText('Nothing waiting for review')).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Sort order' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'More actions' })).toBeInTheDocument()
  })

  it('redirects an event link to the editor without loading the list first (EVT-50)', async () => {
    const { listUrls } = mockCatalogFetch({ events: [] })

    renderEventsPage(['/p/demo/events/all/ev-1'])

    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent('/p/demo/events/all/ev-1/edit'),
    )
    expect(listUrls.filter((url) => url.includes('limit=200'))).toEqual([])
  })

  it('reports a bulk status change and offers to undo it (EVT-11)', async () => {
    vi.mocked(toast.success).mockClear()
    const { bulkUpdateBodies } = mockCatalogFetch({
      events: [
        screenEvent('event-1', 'checkout_view', 'checkout'),
        { ...screenEvent('event-2', 'home_view', 'home'), status: 'draft' },
      ],
    })

    renderEventsPage()

    expect(await screen.findByText('checkout_view')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Select checkout_view'))
    fireEvent.click(screen.getByLabelText('Select home_view'))
    fireEvent.keyDown(screen.getByRole('combobox', { name: 'Set status' }), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('option', { name: 'Implemented' }))

    await waitFor(() =>
      expect(bulkUpdateBodies).toEqual([
        { event_ids: ['event-1', 'event-2'], status: 'implemented' },
      ]),
    )
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        'Set 2 events to Implemented',
        expect.objectContaining({ action: expect.objectContaining({ label: 'Undo' }) }),
      ),
    )
    // Success, not the optimistic moment, clears the selection.
    expect(screen.queryByRole('combobox', { name: 'Set status' })).not.toBeInTheDocument()
  })

  it('undoes to what the table showed, not a stale list in another cache (EVT-11)', async () => {
    vi.mocked(toast.success).mockClear()
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    // Another tab's list, cached before this one and minutes out of date.
    queryClient.setQueryData(['events', 'demo', null, 'stale-tab'], {
      items: [{ ...screenEvent('event-1', 'checkout_view', 'checkout'), status: 'archived' }],
      total: 1,
    })
    const { bulkUpdateBodies } = mockCatalogFetch({
      events: [
        screenEvent('event-1', 'checkout_view', 'checkout'),
        { ...screenEvent('event-2', 'home_view', 'home'), status: 'draft' },
      ],
    })

    renderEventsPage(['/p/demo/events'], null, queryClient)

    expect(await screen.findByText('checkout_view')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Select checkout_view'))
    fireEvent.click(screen.getByLabelText('Select home_view'))
    fireEvent.keyDown(screen.getByRole('combobox', { name: 'Set status' }), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('option', { name: 'Implemented' }))
    await waitFor(() => expect(toast.success).toHaveBeenCalled())

    // A newer selection, made before Undo, is the operator's; Undo keeps it.
    fireEvent.click(screen.getByLabelText('Select home_view'))
    const [, options] = at(vi.mocked(toast.success).mock.calls, 0)
    const action = (options as unknown as { action: { onClick: () => void } }).action
    action.onClick()

    await waitFor(() =>
      expect(bulkUpdateBodies.slice(1)).toEqual([
        { event_ids: ['event-1'], status: 'live' },
        { event_ids: ['event-2'], status: 'draft' },
      ]),
    )
    await waitFor(() => expect(screen.getByLabelText('Select home_view')).toBeChecked())
    expect(screen.getByRole('combobox', { name: 'Set status' })).toBeInTheDocument()
  })

  it('keeps the selection when only the sort order changes', async () => {
    // Sorting reorders the same set; the selection belongs to the set.
    mockCatalogFetch({
      events: [
        screenEvent('event-1', 'checkout_view', 'checkout'),
        screenEvent('event-2', 'home_view', 'home'),
      ],
    })

    renderEventsPage()

    expect(await screen.findByText('checkout_view')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Select checkout_view'))
    fireEvent.keyDown(screen.getByRole('combobox', { name: 'Sort order' }), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('option', { name: 'Busiest first' }))

    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent('sort=volume'),
    )
    expect(screen.getByLabelText('Select checkout_view')).toBeChecked()
    expect(screen.getByRole('combobox', { name: 'Set status' })).toBeInTheDocument()
  })

  it("shows the type's schema drift in the embedded table, which has no header (EVT-33)", async () => {
    mockCatalogFetch({
      events: [makeEvent({ ...screenEvent('event-1', 'checkout_view', 'checkout'), drift_count: 2 })],
    })

    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <TooltipProvider>
        <MemoryRouter initialEntries={['/p/demo/event-types/type-1']}>
          <Routes>
            <Route
              path="/p/:slug/event-types/:id"
              element={<EventsPage lockType="page" embedded />}
            />
          </Routes>
        </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>,
    )

    expect(await screen.findByText('checkout_view')).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: '2 schema drifts on this event type' }),
    ).toBeInTheDocument()
  })
})
