import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext, type AuthContextValue } from './auth-context'
import { AppSidebar } from './app-sidebar'
import { BranchContext } from './branch-context-internal'

function mockJsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function makeAuth(role: 'owner' | 'admin' | 'member' = 'owner'): AuthContextValue {
  return {
    user: {
      id: 'user-1',
      email: `${role}@example.com`,
      name: 'Owner',
      role,
      is_platform_admin: false,
      orgs: [],
      created_at: '2026-04-18T10:00:00Z',
      updated_at: '2026-04-18T10:00:00Z',
    },
    status: 'authenticated',
    error: null,
    isLoggingOut: false,
    logout: async () => {},
    refresh: () => {},
  }
}

function mockProjectsFetch() {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input)
    if (url.endsWith('/api/v1/projects')) {
      return mockJsonResponse([
        {
          id: 'project-1',
          name: 'Demo',
          slug: 'demo',
          description: '',
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          summary: {
            event_type_count: 2,
            event_count: 13,
            active_event_count: 10,
            implemented_event_count: 7,
            review_pending_event_count: 3,
            archived_event_count: 2,
            variable_count: 5,
            scan_count: 4,
            alert_destination_count: 1,
            // Deliberately different from alert_destination_count: the Alerting
            // badge used to count DESTINATIONS, so it read "1" while 52
            // incidents sat open. Two distinct values are what
            // let the assertion below tell the two apart.
            open_incident_count: 7,
            alert_rule_count: 0,
            // H1: monitoring_signal_count is the OPEN-SIGNAL population (anomalies
            // across project_total + event_type + event scope). It must NOT drive
            // the "Monitors" nav badge, which counts MONITOR configs needing
            // attention. firing_monitor_count is the canonical source for that badge.
            monitoring_signal_count: 9,
            firing_monitor_count: 3,
            latest_scan_job: null,
            latest_signal: null,
          },
        },
      ])
    }
    if (url.endsWith('/api/v1/projects/demo/event-types')) {
      return mockJsonResponse([
        {
          id: 'event-type-1',
          project_id: 'project-1',
          name: 'page_view',
          display_name: 'Page view',
          description: '',
          color: '#3b82f6',
          order: 0,
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          field_definitions: [],
        },
        {
          id: 'event-type-2',
          project_id: 'project-1',
          name: 'track_click',
          display_name: 'Track click',
          description: '',
          color: '#f97316',
          order: 1,
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          field_definitions: [],
        },
      ])
    }
    if (url.endsWith('/api/v1/projects/demo/branches')) {
      return mockJsonResponse({
        items: [
          {
            id: 'branch-main',
            project_id: 'project-1',
            name: 'main',
            kind: 'main',
            status: 'merged',
            description: '',
            base_revision_id: null,
            created_by: null,
            merged_at: null,
            merged_by: null,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
          },
        ],
        total: 1,
      })
    }
    throw new Error(`Unhandled fetch: ${url}`)
  })
}

function renderSidebar(initialEntry = '/p/demo/events', role: 'owner' | 'admin' | 'member' = 'owner') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={makeAuth(role)}>
        <MemoryRouter initialEntries={[initialEntry]}>
          <Routes>
            <Route path="/p/:slug/events" element={<AppSidebar />} />
            <Route path="/p/:slug/events/:tab" element={<AppSidebar />} />
            <Route path="/p/:slug/events/:tab/:eventId" element={<AppSidebar />} />
            <Route path="/p/:slug/settings" element={<AppSidebar />} />
            <Route path="/p/:slug/settings/:tab" element={<AppSidebar />} />
            {/* Global/workspace route: no `:slug`, so the sidebar must render
                its workspace-scoped nav rather than the last project's nav. */}
            <Route path="/workspace" element={<AppSidebar />} />
          </Routes>
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  try {
    localStorage.clear()
  } catch {
    /* ignore */
  }
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('AppSidebar', () => {
  it('exposes the site nav as a navigation landmark, not a complementary one', async () => {
    mockProjectsFetch()

    renderSidebar('/p/demo/events')
    await screen.findByText('Events')

    // <aside aria-label="Main navigation"> announced as "complementary", so the
    // nav rotor never listed it.
    expect(screen.getByRole('navigation', { name: 'Main navigation' })).toBeInTheDocument()
    expect(screen.queryByRole('complementary', { name: 'Main navigation' })).toBeNull()
  })

  it('renders the job-based navigation groups for the active project', async () => {
    mockProjectsFetch()

    renderSidebar('/p/demo/events')

    // Await the projects query so the active project (and its counts) resolve.
    expect(await screen.findByText('Events')).toBeInTheDocument()
    expect(await screen.findByText('Page view')).toBeInTheDocument()

    for (const group of ['Plan', 'Observe', 'Govern']) {
      expect(screen.getByText(group)).toBeInTheDocument()
    }
    for (const item of [
      'Events',
      'Event types',
      'Page view',
      'Track click',
      'Meta fields',
      'Plan branches',
      'Overview',
      'Anomalies',
      'Alerting',
      'Reconciliation',
      'Coverage',
      'Scans',
      'Audit log',
    ]) {
      expect(screen.getByText(item)).toBeInTheDocument()
    }
  })

  it('points each nav item at its first-class route', async () => {
    mockProjectsFetch()

    const { container } = renderSidebar('/p/demo/events')
    await screen.findByText('Events')
    await screen.findByText('Page view')

    const expected: Record<string, string> = {
      Events: '/p/demo/events',
      Overview: '/p/demo/overview',
      'Meta fields': '/p/demo/meta-fields',
      'Plan branches': '/p/demo/branches',
      Anomalies: '/p/demo/anomalies',
      Alerting: '/p/demo/alerting',
      Reconciliation: '/p/demo/reconciliation',
      Coverage: '/p/demo/coverage',
      Scans: '/p/demo/scans',
      'Audit log': '/p/demo/audit',
    }
    for (const [label, href] of Object.entries(expected)) {
      expect(screen.getByRole('link', { name: new RegExp(label) })).toHaveAttribute('href', href)
    }
    expect(screen.getByRole('link', { name: 'Page view' })).toHaveAttribute('href', '/p/demo/events/page_view')
    expect(screen.getByRole('link', { name: 'Track click' })).toHaveAttribute('href', '/p/demo/events/track_click')
    // Event types is a plain leaf to its page, with no gear (#238).
    const eventTypes = screen.getByRole('link', { name: /^Event types/ })
    expect(eventTypes).toHaveAttribute('href', '/p/demo/event-types')
    expect(eventTypes.querySelectorAll('svg')).toHaveLength(1)
    expect(container).toBeInTheDocument()
    // Footer: project settings point at the full-takeover area and name THIS
    // project in the address.
    expect(screen.getByRole('link', { name: 'Project settings' })).toHaveAttribute(
      'href',
      '/settings/project/general?project=demo',
    )
  })

  it('hides the owner-only Audit log from a member', async () => {
    mockProjectsFetch()

    renderSidebar('/p/demo/events', 'member')
    await screen.findByText('Events')

    // The endpoint behind it is owner-only, so the link would only walk an
    // editor into a wall.
    expect(screen.queryByRole('link', { name: /Audit log/ })).toBeNull()
    // The rest of Govern is unchanged — this hides one item, not the group.
    expect(screen.getByText('Govern')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Reconciliation/ })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Coverage/ })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Scans/ })).toBeInTheDocument()
  })

  it('marks the active event type link based on the current route', async () => {
    mockProjectsFetch()

    renderSidebar('/p/demo/events/page_view')
    const eventTypeLink = await screen.findByRole('link', { name: 'Page view' })
    expect(eventTypeLink).toHaveClass('bg-sidebar-active')
    expect(eventTypeLink).toHaveAttribute('aria-current', 'page')
    // Events matches the same /events prefix but is not the page;
    // it stays lit as the section the type filter belongs to (#238).
    const events = screen.getByRole('link', { name: /^Events/ })
    expect(events).not.toHaveAttribute('aria-current')
    expect(events.querySelector('svg')).toHaveStyle({ color: 'var(--accent)' })
  })

  it('announces the current page with aria-current', async () => {
    mockProjectsFetch()

    renderSidebar('/p/demo/events')
    const events = await screen.findByRole('link', { name: /^Events/ })
    expect(events).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: /Anomalies/ })).not.toHaveAttribute('aria-current')
  })

  it('keeps the switchers, project links and account menu when collapsed', async () => {
    mockProjectsFetch()
    localStorage.setItem('tripl-sidebar-collapsed', '1')

    renderSidebar('/p/demo/events')

    expect(
      await screen.findByRole('button', { name: 'Switch project (current: Demo)' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Switch branch/ })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Project settings' })).toHaveAttribute(
      'href',
      '/settings/project/general?project=demo',
    )
    expect(screen.getByRole('link', { name: 'Concepts' })).toHaveAttribute('href', '/p/demo/concepts')
    expect(screen.getByRole('link', { name: 'Events' })).toHaveAttribute('aria-current', 'page')

    fireEvent.keyDown(screen.getByRole('button', { name: /^Account menu/ }), { key: 'Enter' })
    expect(await screen.findByRole('menuitem', { name: 'Sign out' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Settings' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Appearance' })).toBeInTheDocument()
  })

  it('drops the Plan counts, which are main\'s, while a branch is active', async () => {
    mockProjectsFetch()
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={makeAuth('owner')}>
          <BranchContext.Provider value={{ branchId: 'branch-1', setBranchId: () => {}, slug: 'demo' }}>
            <MemoryRouter initialEntries={['/p/demo/events']}>
              <Routes>
                <Route path="/p/:slug/events" element={<AppSidebar />} />
              </Routes>
            </MemoryRouter>
          </BranchContext.Provider>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )

    // Observe counts are not plan content and stay (Alerting's 7 open incidents).
    const alertingLink = await screen.findByRole('link', { name: /Alerting/ })
    await waitFor(() => expect(alertingLink).toHaveTextContent('7'))
    // Events (10) and Event types (2) describe main, not the branch on screen.
    expect(screen.getByRole('link', { name: /^Events/ })).not.toHaveTextContent('10')
    expect(screen.getByRole('link', { name: /^Event types/ })).not.toHaveTextContent('2')
  })

  it('surfaces project-summary counts, and badges Observe exactly twice', async () => {
    mockProjectsFetch()

    renderSidebar('/p/demo/events')
    // The nav labels render from the URL slug immediately; counts only appear
    // once the projects query resolves the active project's summary.
    await screen.findByText('10')

    // Events active count (10) and event-type count (2) are unambiguous
    // project-summary stats and always render as nav badges.
    expect(screen.getByText('10')).toBeInTheDocument()
    expect(screen.getByText('2')).toBeInTheDocument()

    // Alerting badges OPEN INCIDENTS (7), not the destination count (1) it used
    // to show — a number that said "1" while the Inbox held 52 open incidents,
    // sitting directly under Anomalies, which does badge a real backlog.
    const alertingLink = screen.getByRole('link', { name: /Alerting/ })
    expect(alertingLink).toHaveTextContent('7')
    expect(alertingLink).not.toHaveTextContent('1')

    // Anomalies badges the open-signal population (monitoring_signal_count === 9).
    const anomaliesLink = screen.getByRole('link', { name: /Anomalies/ })
    expect(anomaliesLink).toHaveTextContent('9')

    // And nothing badges firing_monitor_count (3) any more. It belonged to the
    // Monitors item, which is gone: a firing rule already reaches
    // the sidebar as the incident it opens, and three danger badges in one group
    // for one event is what the merge set out to fix. The firing count is on the
    // Monitors section's own rollup.
    expect(screen.queryByRole('link', { name: /Monitors/ })).toBeNull()
    expect(anomaliesLink).not.toHaveTextContent('3')
    expect(alertingLink).not.toHaveTextContent('3')
  })

  it('surfaces Properties and Relations as discoverable Plan nav items (M6)', async () => {
    mockProjectsFetch()

    renderSidebar('/p/demo/events')
    await screen.findByText('Events')

    // M6: Variables and Relations must be reachable from the sidebar (not only
    // via the command palette), pointing at their project-scoped routes.
    const variables = await screen.findByRole('link', { name: /Properties/ })
    expect(variables).toHaveAttribute('href', '/p/demo/variables')
    const relations = screen.getByRole('link', { name: /Relations/ })
    expect(relations).toHaveAttribute('href', '/p/demo/relations')
  })

  it('marks the active surface based on the current route', async () => {
    mockProjectsFetch()

    // Detection settings highlight Anomalies — they decide what gets flagged,
    // and notify nobody. They used to highlight Monitors, a list of alert rules
    // they have no bearing on.
    renderSidebar('/p/demo/settings/monitoring')
    const anomalies = await screen.findByRole('link', { name: /Anomalies/ })
    expect(anomalies).toHaveClass('bg-sidebar-active')
  })

  it('keeps nav icons neutral and paints only the open-incident count red', async () => {
    mockProjectsFetch()

    renderSidebar('/p/demo/events')
    await screen.findByText('10')

    const anomalies = screen.getByRole('link', { name: /Anomalies/ })
    const alerting = screen.getByRole('link', { name: /Alerting/ })
    // The icon names the section, not its status.
    expect(anomalies.querySelector('svg')).toHaveStyle({ color: 'var(--fg-subtle)' })
    // Open signals are a count: neutral. Open incidents are unacknowledged
    // alerts: the one red pill.
    const anomalyCount = anomalies.querySelector('[data-slot="count-badge"]')
    const incidentCount = alerting.querySelector('[data-slot="count-badge"]')
    // Not --surface-active: that is the light sidebar's hover fill.
    expect(anomalyCount).toHaveClass('bg-surface')
    expect(anomalyCount).not.toHaveClass('bg-surface-active')
    expect(anomalyCount).not.toHaveClass('bg-destructive')
    expect(incidentCount).toHaveClass('bg-destructive')
  })

  it('renders a workspace-scoped nav on /workspace instead of the last project', async () => {
    mockProjectsFetch()
    // Even with a persisted last project, /workspace must NOT resurrect that
    // project's Plan/Observe/Govern groups — it is a portfolio route.
    try {
      localStorage.setItem('tripl-last-project-slug', 'demo')
    } catch {
      /* ignore */
    }

    renderSidebar('/workspace')

    // The workspace section renders.
    expect(await screen.findByRole('link', { name: 'All projects' })).toHaveAttribute(
      'href',
      '/workspace',
    )
    expect(screen.getByText('Workspace')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Data sources' })).toHaveAttribute(
      'href',
      '/settings/data-sources',
    )

    // The per-project job groups and their contents do NOT render.
    for (const absent of ['Plan', 'Observe', 'Govern', 'Reconciliation', 'Page view']) {
      expect(screen.queryByText(absent)).not.toBeInTheDocument()
    }
    // The project-scoped footer affordances are suppressed too.
    expect(screen.queryByText('Concepts')).not.toBeInTheDocument()

    // The project switcher shows the neutral "Choose a project" placeholder and
    // must NOT show a real project (projects[0]) under it as if it were picked.
    // findByText waits for the projects query to resolve (loading -> Select).
    expect(await screen.findByText('Choose a project')).toBeInTheDocument()
    expect(screen.getByText('1 project')).toBeInTheDocument()
    expect(screen.queryByText('demo')).not.toBeInTheDocument()
  })
})

describe('AppSidebar shell review (#238)', () => {
  it('opens the account menu from the user row, with Profile and Sign out', async () => {
    mockProjectsFetch()
    renderSidebar('/p/demo/events')
    await screen.findByText('Events')

    // No loose gear / sign-out icons beside the user any more.
    expect(screen.queryByRole('button', { name: 'Sign out' })).toBeNull()
    expect(screen.queryByRole('link', { name: 'Settings' })).toBeNull()
    // Appearance stays one click away.
    expect(screen.getByRole('button', { name: 'Appearance' })).toBeInTheDocument()

    fireEvent.keyDown(screen.getByRole('button', { name: /^Account menu/ }), { key: 'Enter' })
    expect(await screen.findByRole('menuitem', { name: 'Profile' })).toHaveAttribute(
      'href',
      '/settings/profile',
    )
    expect(screen.getByRole('menuitem', { name: 'Settings' })).toHaveAttribute('href', '/settings')
    // The docs site, from every page: the Concepts glossary is project-scoped
    // and has no link out to it.
    const docs = screen.getByRole('menuitem', { name: 'Documentation (opens in a new tab)' })
    expect(docs).toHaveAttribute('href', 'https://docs.tripl.io')
    expect(docs).toHaveAttribute('target', '_blank')
    expect(screen.getByRole('menuitem', { name: 'Sign out' })).toBeInTheDocument()
  })

  it('pins Project settings in the footer, outside the scrolling nav', async () => {
    mockProjectsFetch()
    renderSidebar('/p/demo/events')
    await screen.findByText('Events')
    const settings = screen.getByRole('link', { name: 'Project settings' })
    const concepts = screen.getByRole('link', { name: 'Concepts' })
    expect(settings.parentElement).toBe(concepts.parentElement)
  })

  it('closes, rather than collapses, when rendered as the drawer', async () => {
    mockProjectsFetch()
    const onClose = vi.fn()
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    localStorage.setItem('tripl-sidebar-collapsed', '1')
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={makeAuth('owner')}>
          <MemoryRouter initialEntries={['/p/demo/events']}>
            <Routes>
              <Route path="/p/:slug/events" element={<AppSidebar drawer onCloseDrawer={onClose} />} />
            </Routes>
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )
    // The persisted collapse is ignored in the drawer: the full nav renders.
    expect(await screen.findByText('Plan')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Collapse sidebar' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Close navigation' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('offers New project in the switcher to someone who can create one', async () => {
    mockProjectsFetch()
    renderSidebar('/p/demo/events')
    await screen.findByText('Events')
    fireEvent.keyDown(await screen.findByRole('button', { name: /Demo/ }), { key: 'Enter' })
    expect(await screen.findByRole('menuitem', { name: 'New project' })).toHaveAttribute(
      'href',
      '/workspace?new=1',
    )
  })
})
