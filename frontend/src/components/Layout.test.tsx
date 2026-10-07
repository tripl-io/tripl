import type { ReactNode } from 'react'
import { projectsKey, projectsQueryOptions } from '@/lib/queryKeys'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryCache, QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { alertingApi } from '@/api/alerting'
import { eventMetricsApi } from '@/api/eventMetrics'
import { projectsApi } from '@/api/projects'
import { ApiError } from '@/api/client'
import type { Project } from '@/types'
import NotFoundPage from '@/pages/NotFoundPage'
import ProjectsPage from '@/pages/ProjectsPage'
import { AuthContext, type AuthContextValue } from './auth-context'
import Layout from './Layout'
import { expectNoAxeViolations } from '@/test/axe'
import { toast } from 'sonner'
import { surfaceQueryError } from '@/lib/errorFeedback'
import { editPageTitle, usePageTitle } from './shell-chrome-context'
import { at } from '@/test/at'

vi.mock('@/api/alerting', () => ({
  alertingApi: { listDeliveries: vi.fn() },
}))

vi.mock('@/api/eventMetrics', () => ({
  eventMetricsApi: { getActiveSignals: vi.fn() },
}))

vi.mock('@/api/projects', () => ({
  // `get` is the confirmation call the shell makes for a slug the list does not
  // know; it must reject the way a real 404 does, not blow up as undefined.
  projectsApi: { list: vi.fn(), get: vi.fn() },
}))

vi.mock('@/components/activity-panel', () => ({
  ActivityPanel: ({ open, slug }: { open: boolean; slug?: string }) =>
    open ? <aside data-testid="activity-panel">Now {slug}</aside> : null,
}))

vi.mock('@/components/app-sidebar', () => ({
  AppSidebar: () => (
    <nav aria-label="sidebar">
      <Link to="/p/demo/other">Other page</Link>
    </nav>
  ),
}))

vi.mock('@/components/command-palette', () => ({
  CommandPaletteProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}))

vi.mock('@/components/tweaks-panel', () => ({
  TweaksPanelProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}))

// The demo chrome, stood in for by the controls that matter to the bypass block:
// the real components need mutations, a tour dialog and scenario polling, none of
// which decides where in the DOM the shell puts them.
// Lets a test make the demo chrome fail the way a missing chunk does.
const demoChrome = vi.hoisted(() => ({ fail: false }))

vi.mock('@/demo/DemoBanner', () => ({
  // Renders its `scenario` slot, as the real banner does inside its row.
  DemoBanner: ({ scenario }: { scenario?: ReactNode }) => {
    if (demoChrome.fail) {
      throw new TypeError('Failed to fetch dynamically imported module: /assets/DemoBanner-abc.js')
    }
    return (
    <div>
      <button type="button">What’s simulated</button>
      {scenario}
      <button type="button">Delete</button>
    </div>
    )
  },
}))

vi.mock('@/demo/DemoScenarioStrip', () => ({
  DemoScenarioStrip: () => <button type="button">Dismiss</button>,
}))

// Portalled to <body>, so it puts nothing between the skip link and the page;
// stood in for so its chunk lands as fast as the rest of the demo chrome.
vi.mock('@/demo/DemoGuideHost', () => ({
  DemoGuideHost: () => null,
}))

vi.mock('@/demo/LazyDemoScenarioProvider', () => ({
  LazyDemoScenarioProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}))

const ownerAuth: AuthContextValue = {
  user: {
    id: 'owner-1',
    email: 'owner@example.com',
    name: 'owner',
    role: 'owner',
    is_platform_admin: false,
    orgs: [],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
  status: 'authenticated',
  error: null,
  isLoggingOut: false,
  logout: async () => {},
  refresh: () => {},
}

interface RenderLayoutOptions {
  /** Turns on the shell's demo chrome (banner + coach strip). */
  isDemo?: boolean
  /** Route element, when the test needs the page to own a control. */
  page?: ReactNode
  /** Overrides the default API mocks, applied before the first render. */
  mocks?: () => void
  /** Seeds the query cache before the first render (e.g. an already-loaded list). */
  seed?: (queryClient: QueryClient) => void
  /** The app's cache, when a test needs its global error backstop. */
  queryCache?: QueryCache
}

function makeProject(isDemo = false): Project {
  return {
    id: 'project-1',
    name: 'Demo',
    slug: 'demo',
    is_demo: isDemo,
    description: '',
    app_version_keep_releases: 5,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    summary: {
      event_type_count: 0,
      event_count: 0,
      active_event_count: 0,
      implemented_event_count: 0,
      review_pending_event_count: 0,
      archived_event_count: 0,
      variable_count: 0,
      scan_count: 0,
      firing_monitor_count: 0,
      open_incident_count: 0,
      alert_destination_count: 0,
      alert_rule_count: 0,
      monitoring_signal_count: 0,
      failing_scan_config_count: 0,
      latest_scan_job: null,
      latest_signal: null,
    },
  }
}

function renderLayout(
  path: string,
  routePath = '/p/:slug/monitoring/:scope/:id',
  pageLabel = 'Monitoring detail',
  options: RenderLayoutOptions = {},
) {
  vi.mocked(projectsApi.list).mockResolvedValue([makeProject(options.isDemo)])
  vi.mocked(projectsApi.get).mockRejectedValue(new ApiError('Not found', 404))
  vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([])
  vi.mocked(alertingApi.listDeliveries).mockResolvedValue({ items: [], total: 0, next_cursor: null })
  options.mocks?.()

  const queryClient = new QueryClient({
    queryCache: options.queryCache,
    defaultOptions: { queries: { retry: false } },
  })
  options.seed?.(queryClient)
  const page = options.page ?? <div>{pageLabel}</div>
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path={routePath} element={<Layout />}>
            <Route index element={page} />
            {/* A splat `routePath` consumes the trailing segments, so the index
                child never matches — give those cases a child that does. */}
            <Route path="*" element={page} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

// jsdom has no matchMedia; the rail's inline-vs-drawer choice reads it. Default
// to the narrow branch (no match) so a test opts into wide mode explicitly.
function mockMatchMedia(matches: boolean) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: (query: string) => ({
      matches,
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
      onchange: null,
    }),
  })
}

beforeEach(() => {
  mockMatchMedia(false)
  if (!globalThis.localStorage) {
    Object.defineProperty(globalThis, 'localStorage', {
      value: {
        store: {} as Record<string, string>,
        getItem(key: string) { return this.store[key] ?? null },
        setItem(key: string, value: string) { this.store[key] = value },
        removeItem(key: string) { delete this.store[key] },
        clear() { this.store = {} },
      },
      configurable: true,
      writable: true,
    })
  }
  localStorage.clear()
  localStorage.setItem('tripl-activity-open', '0')
})

afterEach(() => {
  demoChrome.fail = false
  vi.restoreAllMocks()
  // defineProperty isn't undone by restoreAllMocks — drop the matchMedia stub so
  // it can't leak into other suites under full-suite concurrency.
  delete (window as { matchMedia?: unknown }).matchMedia
})

describe('Layout bypass block', () => {
  it('offers a skip link as the first focusable element, targeting main content', async () => {
    const { container } = renderLayout('/p/demo/monitoring/event/event-1')
    await screen.findByText('Monitoring detail')

    const skipLink = screen.getByRole('link', { name: 'Skip to main content' })
    expect(skipLink).toHaveAttribute('href', '#main-content')

    // It must come before the sidebar so the very first Tab reaches it.
    const focusable = container.querySelectorAll('a[href], button, input, [tabindex]')
    expect(focusable[0]).toBe(skipLink)

    // …and the target has to be focusable, or the jump goes nowhere.
    const target = container.querySelector('#main-content')
    expect(target).not.toBeNull()
    expect(target).toHaveAttribute('tabindex', '-1')
  })

  it('lands past the demo chrome, not on it', async () => {
    const { container } = renderLayout('/p/demo/events', '/p/:slug/events', 'Events body', {
      isDemo: true,
      page: (
        <div>
          <p>Events body</p>
          <button type="button">New event</button>
        </div>
      ),
    })
    await screen.findByText('Events body')

    const target = container.querySelector('#main-content')
    if (!target) throw new Error('the skip link has no target to land on')

    // The demo banner and the coach strip are shell furniture: they belong on
    // the page, but INSIDE the skip target they made the user Tab through the
    // demo's own controls — the DESTRUCTIVE Delete among them — before reaching
    // the page they had asked to be taken to.
    // The demo chrome is a lazy chunk, so it can land after the page.
    const deleteButton = await screen.findByRole('button', { name: 'Delete' })
    const dismissButton = await screen.findByRole('button', { name: 'Dismiss' })
    expect(target.contains(deleteButton)).toBe(false)
    expect(target.contains(dismissButton)).toBe(false)

    // So the first stop after the jump is the page's own first control.
    const insideTarget = target.querySelectorAll('a[href], button, input, [tabindex]')
    expect(insideTarget[0]).toBe(screen.getByRole('button', { name: 'New event' }))
  })
})

// The top bar repeats the project name on a phone-only line under the title
// (#238); skip it so crumb assertions still find exactly the crumb.
const CRUMB_IGNORE = 'script, style, [data-testid="topbar-project"]'

describe('Layout breadcrumbs', () => {
  it('renders no root crumb on the workspace surface', async () => {
    renderLayout('/workspace', '/workspace', 'Workspace dashboard')
    await screen.findByText('Workspace dashboard')

    // The placeholder the crumb resolver used to emit when no project was in
    // scope. It read as an untranslated template leaking into production.
    expect(screen.queryByText('project')).toBeNull()
    // Named as the sidebar and the page's own heading name it.
    expect(screen.getByRole('banner')).toHaveTextContent('All projects')
  })

  it('names the Concepts surface instead of claiming to be Overview', async () => {
    renderLayout('/p/demo/concepts', '/p/:slug/concepts', 'Concepts body')
    await screen.findByText('Concepts body')

    // Concepts sits outside the grouped nav, so it used to fall through to the
    // catch-all and render "Demo › Overview" — a trail pointing at a page the
    // user is not on. (The phone-only project line under the title also reads
    // "Demo"; the crumb is the one asserted here.)
    expect(screen.getByText('Demo', { ignore: CRUMB_IGNORE })).toBeInTheDocument()
    expect(screen.getByText('Help & reference')).toBeInTheDocument()
    expect(screen.getByText('Concepts')).toBeInTheDocument()
    expect(screen.queryByText('Overview')).toBeNull()
  })

  it('keeps the project crumb but stops claiming "Overview" on an unmatched path', async () => {
    renderLayout('/p/demo/this-route-does-not-exist', '/p/:slug/*', 'Page not found')
    await screen.findByText('Page not found')

    // The slug is valid, so the trail still names the project …
    expect(screen.getByText('Demo', { ignore: CRUMB_IGNORE })).toBeInTheDocument()
    // … but the page half must not name a real surface the user is not on.
    expect(screen.queryByText('Overview')).toBeNull()
    expect(screen.getByText('Not found')).toBeInTheDocument()
  })
})

describe('Layout suspended organization (F20)', () => {
  it('shows the suspended state instead of the shell when the organization refuses', async () => {
    renderLayout('/workspace', '/workspace', 'Workspace page', {
      mocks: () => {
        vi.mocked(projectsApi.list).mockRejectedValue(new ApiError('This organization is suspended', 403))
      },
    })
    expect(
      await screen.findByRole('heading', { level: 1, name: 'This organization is suspended' }),
    ).toBeInTheDocument()
    expect(screen.queryByText('Workspace page')).toBeNull()
    expect(screen.queryByRole('navigation', { name: 'sidebar' })).toBeNull()
  })
})

describe('Layout backend unavailable', () => {
  it('shows the card once and no toast on top of it when the project list fails', async () => {
    const toastError = vi.spyOn(toast, 'error')
    // A second reader of the list, as the sidebar and the palette are in the
    // real shell: every one of them must leave the failure to the card.
    function ProjectsReader() {
      const { data } = useQuery(projectsQueryOptions())
      return <div>Workspace dashboard {data?.length ?? 0}</div>
    }
    renderLayout('/workspace', '/workspace', 'Workspace dashboard', {
      page: <ProjectsReader />,
      queryCache: new QueryCache({ onError: surfaceQueryError }),
      mocks: () => {
        vi.mocked(projectsApi.list).mockRejectedValue(new ApiError('Service unavailable', 503))
      },
    })

    expect(await screen.findAllByRole('heading', { name: 'Backend is unavailable' })).toHaveLength(1)
    expect(toastError).not.toHaveBeenCalled()
  })

  it('reports a failed list once on the real workspace page, not once per surface', async () => {
    // The page used to add its own "Failed to load projects" card under the
    // shell's, each with its own Try again.
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'))
    const toastError = vi.spyOn(toast, 'error')
    renderLayout('/workspace', '/workspace', 'Workspace dashboard', {
      page: (
        <AuthContext.Provider value={ownerAuth}>
          <ProjectsPage />
        </AuthContext.Provider>
      ),
      queryCache: new QueryCache({ onError: surfaceQueryError }),
      mocks: () => {
        vi.mocked(projectsApi.list).mockRejectedValue(new ApiError('Service unavailable', 503))
      },
    })

    await screen.findByRole('heading', { name: 'Backend is unavailable' })
    await waitFor(() => expect(document.querySelector('[data-slot="skeleton"]')).toBeNull())
    const alerts = screen.getAllByRole('alert')
    expect(alerts).toHaveLength(1)
    expect(within(at(alerts, 0)).getAllByRole('button', { name: 'Try again' })).toHaveLength(1)
    expect(screen.queryByText('Failed to load projects')).toBeNull()
    expect(toastError).not.toHaveBeenCalledWith(expect.stringContaining('Service unavailable'), expect.anything())
  })
})

describe('Layout page title', () => {
  function NamedDetail({ name }: { name?: string }) {
    usePageTitle(name)
    return <div>Detail body</div>
  }

  it('names the entity a detail page shows instead of "Detail"', async () => {
    renderLayout('/p/demo/monitoring/metric/m-1', undefined, undefined, {
      page: <NamedDetail name="Checkout conversion" />,
    })
    await screen.findByText('Detail body')

    const banner = screen.getByRole('banner')
    expect(within(banner).getByText('Checkout conversion')).toBeInTheDocument()
    expect(within(banner).queryByText('Detail')).toBeNull()
  })

  it('leaves the entity crumb blank until the entity has loaded', async () => {
    renderLayout('/p/demo/monitoring/metric/m-1', undefined, undefined, {
      page: <NamedDetail />,
    })
    await screen.findByText('Detail body')

    // No generic "Detail" flashes in the top bar before the name arrives.
    const banner = screen.getByRole('banner')
    expect(within(banner).queryByText('Detail')).toBeNull()
    // The area's page stands in as the title, so the header never ends in a
    // bare chevron and still names the page on phones.
    const titleEl = within(banner).getByText('Metrics')
    expect(titleEl).toHaveClass('font-semibold')
  })
})

describe('Layout detail crumbs from the entity', () => {
  function NamedDetail({ name }: { name?: string }) {
    usePageTitle(name)
    return <div>Detail body</div>
  }

  it('files an alert rule under Observe › Alerting › Rules', async () => {
    renderLayout('/p/demo/monitors/rule-1', '/p/:slug/monitors/:id', undefined, {
      page: <NamedDetail name="Checkout drop" />,
    })
    await screen.findByText('Detail body')

    const banner = screen.getByRole('banner')
    expect(within(banner).getByText('Alerting')).toBeInTheDocument()
    expect(within(banner).getByText('Rules')).toBeInTheDocument()
    expect(within(banner).getByText('Checkout drop')).toBeInTheDocument()
    // The surfaces are links back to their lists; the nav group is not a page.
    expect(within(banner).getByRole('link', { name: 'Alerting' })).toHaveAttribute(
      'href',
      '/p/demo/alerting',
    )
    expect(within(banner).getByRole('link', { name: 'Rules' })).toHaveAttribute(
      'href',
      '/p/demo/alerting?section=monitors',
    )
    expect(within(banner).queryByRole('link', { name: 'Observe' })).toBeNull()
  })

  it('files an event type\'s volume under Event types, not Anomalies', async () => {
    renderLayout('/p/demo/monitoring/event-type/et-1', undefined, undefined, {
      page: <NamedDetail name="Screen View" />,
    })
    await screen.findByText('Detail body')

    const banner = screen.getByRole('banner')
    expect(within(banner).getByText('Event types')).toBeInTheDocument()
    // The same group the nav files Event types under, not Observe.
    expect(within(banner).getAllByText('Plan').length).toBeGreaterThan(0)
    expect(within(banner).queryByText('Observe')).toBeNull()
    expect(within(banner).queryByText('Anomalies')).toBeNull()
  })
})

describe('Layout metric editor crumbs', () => {
  it('names the new-metric editor under Metrics', async () => {
    renderLayout('/p/demo/metrics/new', '/p/:slug/metrics/new', 'Metric form body')
    await screen.findByText('Metric form body')

    const banner = screen.getByRole('banner')
    expect(within(banner).getByText('Metrics')).toBeInTheDocument()
    expect(within(banner).getByText('New metric')).toBeInTheDocument()
  })

  it('files a fact table editor under Metrics › Fact tables', async () => {
    renderLayout('/p/demo/metrics/fact-tables/ft-1/edit', '/p/:slug/metrics/fact-tables/:id/edit', 'Fact form body')
    await screen.findByText('Fact form body')

    const banner = screen.getByRole('banner')
    expect(within(banner).getByText('Fact tables')).toBeInTheDocument()
    expect(within(banner).getByText('Edit fact table')).toBeInTheDocument()
  })

  it('reads Metrics › <metric> › Edit once the editor names its metric', async () => {
    function NamedEditor() {
      usePageTitle(editPageTitle('Active Sessions'))
      return <div>Metric form body</div>
    }
    renderLayout('/p/demo/metrics/m-1/edit', '/p/:slug/metrics/:id/edit', undefined, {
      page: <NamedEditor />,
    })
    await screen.findByText('Metric form body')

    const banner = screen.getByRole('banner')
    expect(within(banner).getByText('Metrics')).toBeInTheDocument()
    expect(within(banner).getByText('Active Sessions')).toBeInTheDocument()
    expect(within(banner).getByText('Edit')).toHaveClass('font-semibold')
    expect(within(banner).queryByText('Edit · Active Sessions')).toBeNull()
  })
})

describe('Layout unknown project', () => {
  it('renders a not-found state instead of the project shell for an unknown slug', async () => {
    renderLayout('/p/no-such-project-xyz/overview', '/p/:slug/overview', 'Live activity body')

    expect(await screen.findByText('Project not found')).toBeInTheDocument()
    // No shell, so nothing below it can fan out project-scoped requests.
    expect(screen.queryByText('Live activity body')).toBeNull()
    expect(screen.queryByRole('navigation', { name: 'sidebar' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Toggle activity feed' })).toBeNull()
    // The invented slug is not echoed back as if it named a workspace.
    expect(screen.getByText(/no project with the address/i)).toBeInTheDocument()
  })

  it('forgets the remembered last project when it is not found', async () => {
    localStorage.setItem('tripl-last-project-slug', 'no-such-project-xyz')
    renderLayout('/p/no-such-project-xyz/overview', '/p/:slug/overview', 'Live activity body')

    expect(await screen.findByText('Project not found')).toBeInTheDocument()
    await waitFor(() => expect(localStorage.getItem('tripl-last-project-slug')).toBeNull())
  })

  it('offers a retry, not a 404, when the server cannot confirm the slug', async () => {
    renderLayout('/p/seeding-demo/overview', '/p/:slug/overview', 'Live activity body', {
      // A 503 says nothing about whether the project exists.
      mocks: () =>
        vi.mocked(projectsApi.get).mockRejectedValue(new ApiError('Backend is unavailable.', 503)),
    })

    expect(
      await screen.findByRole('heading', { name: 'Could not open this project' }),
    ).toBeInTheDocument()
    expect(screen.queryByText('Project not found')).toBeNull()

    vi.mocked(projectsApi.get).mockResolvedValue({ ...makeProject(), slug: 'seeding-demo' })
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByText('Live activity body')).toBeInTheDocument()
  })

  it('renders the shell from the project endpoint without waiting for the list', async () => {
    renderLayout('/p/demo/overview', '/p/:slug/overview', 'Live activity body', {
      mocks: () => {
        // The list (with its summaries) never answers; the project endpoint does.
        vi.mocked(projectsApi.list).mockReturnValue(new Promise(() => {}))
        vi.mocked(projectsApi.get).mockResolvedValue(makeProject())
      },
    })

    expect(await screen.findByText('Live activity body')).toBeInTheDocument()
  })

  it('takes the demo chrome from the project endpoint when the list has not answered', async () => {
    renderLayout('/p/demo/events', '/p/:slug/events', 'Events body', {
      mocks: () => {
        vi.mocked(projectsApi.list).mockReturnValue(new Promise(() => {}))
        vi.mocked(projectsApi.get).mockResolvedValue(makeProject(true))
      },
    })

    expect(await screen.findByText('Events body')).toBeInTheDocument()
    // The same project ActiveProjectContext hands the page — not the list row
    // alone, which a deep link does not have yet.
    expect(await screen.findByRole('button', { name: 'Dismiss' })).toBeInTheDocument()
  })

  it('does not re-confirm a project the loaded list already names', async () => {
    vi.mocked(projectsApi.get).mockClear()
    renderLayout('/p/demo/overview', '/p/:slug/overview', 'Live activity body', {
      seed: (queryClient) => queryClient.setQueryData(projectsKey(), [makeProject()]),
    })

    expect(await screen.findByText('Live activity body')).toBeInTheDocument()
    expect(projectsApi.get).not.toHaveBeenCalled()
  })

  it('renders the full shell once the slug is confirmed to exist', async () => {
    renderLayout('/p/demo/overview', '/p/:slug/overview', 'Live activity body')

    expect(await screen.findByText('Live activity body')).toBeInTheDocument()
    expect(screen.queryByText('Project not found')).toBeNull()
  })
})

describe('Layout activity panel', () => {
  it('opens Now activity from monitoring detail routes', async () => {
    renderLayout('/p/demo/monitoring/event/event-1')

    expect(await screen.findByText('Monitoring detail')).toBeInTheDocument()
    expect(screen.queryByTestId('activity-panel')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Toggle activity feed' }))

    expect(await screen.findByTestId('activity-panel')).toHaveTextContent('Now demo')
  })

  it('opens the rail as a dismissible drawer on narrow viewports', async () => {
    mockMatchMedia(false)
    renderLayout('/p/demo/monitoring/event/event-1')
    await screen.findByText('Monitoring detail')

    fireEvent.click(screen.getByRole('button', { name: 'Toggle activity feed' }))
    // Below the breakpoint the rail is an off-canvas drawer with a backdrop.
    expect(await screen.findByTestId('activity-panel')).toBeInTheDocument()
    const backdrop = screen.getByRole('button', { name: 'Close activity feed' })

    fireEvent.click(backdrop)
    expect(screen.queryByTestId('activity-panel')).toBeNull()
  })

  it('renders the rail inline without a backdrop on wide viewports', async () => {
    mockMatchMedia(true)
    renderLayout('/p/demo/monitoring/event/event-1')
    await screen.findByText('Monitoring detail')

    fireEvent.click(screen.getByRole('button', { name: 'Toggle activity feed' }))
    expect(await screen.findByTestId('activity-panel')).toBeInTheDocument()
    // Inline mode has no drawer backdrop.
    expect(screen.queryByRole('button', { name: 'Close activity feed' })).toBeNull()
  })
})

describe("Layout after a demo is deleted", () => {
  it.each(['/p/demo-gone/events', '/p/demo-gone/anomalies', '/p/demo-gone/overview'])(
    'answers %s with the not-found page and a way out, on every route',
    async (path) => {
      // A deleted demo's URL used to render project chrome, stale event-type
      // navigation and a strip of zeroed stats over "Project not found", plus a
      // pile of raw "Reference: <uuid>" toasts from every failing child query.
      renderLayout(path, '/p/:slug/*', 'Live activity body')

      expect(await screen.findByText('Project not found')).toBeInTheDocument()
      expect(screen.getByRole('link', { name: /back to all projects/i })).toBeInTheDocument()
      expect(screen.queryByText('Live activity body')).toBeNull()
      expect(screen.queryByRole('navigation', { name: 'sidebar' })).toBeNull()
    },
  )
})

describe('Layout demo chrome failure', () => {
  it('keeps the page when the demo chrome cannot load', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    demoChrome.fail = true
    renderLayout('/p/demo/events', '/p/:slug/events', 'Events body', { isDemo: true })

    // Outside the route boundary, a failing banner chunk used to reach the
    // app-level boundary and take the sidebar, top bar and page with it.
    expect(await screen.findByText('Events body')).toBeInTheDocument()
    await vi.waitFor(() => expect(console.error).toHaveBeenCalled())
    expect(screen.getByText('Events body')).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'sidebar' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull()
  })
})

describe('Layout mobile navigation drawer', () => {
  it('keeps the off-canvas sidebar out of the tab order until it is opened', async () => {
    mockMatchMedia(false)
    const { container } = renderLayout('/p/demo/events', '/p/:slug/events', 'Events body')
    await screen.findByText('Events body')

    const drawer = container.querySelector('#app-sidebar')
    expect(drawer).toHaveAttribute('inert')

    const trigger = screen.getByRole('button', { name: 'Open navigation' })
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    trigger.focus()
    fireEvent.click(trigger)

    expect(drawer).not.toHaveAttribute('inert')
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    // Focus moves into the drawer, and the page behind it goes inert.
    expect(screen.getByRole('link', { name: 'Other page' })).toHaveFocus()
    expect(screen.getByRole('main').closest('[inert]')).not.toBeNull()

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(drawer).toHaveAttribute('inert')
    expect(screen.getByRole('main').closest('[inert]')).toBeNull()
    expect(trigger).toHaveFocus()
  })

  it('leaves the pinned sidebar alone on wide viewports', async () => {
    mockMatchMedia(true)
    const { container } = renderLayout('/p/demo/events', '/p/:slug/events', 'Events body')
    await screen.findByText('Events body')

    expect(container.querySelector('#app-sidebar')).not.toHaveAttribute('inert')
  })

  it('closes the activity drawer on Escape and hands focus back', async () => {
    mockMatchMedia(false)
    renderLayout('/p/demo/events', '/p/:slug/events', 'Events body')
    await screen.findByText('Events body')

    const toggle = screen.getByRole('button', { name: 'Toggle activity feed' })
    toggle.focus()
    fireEvent.click(toggle)
    expect(await screen.findByTestId('activity-panel')).toBeInTheDocument()

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByTestId('activity-panel')).toBeNull()
    expect(toggle).toHaveFocus()
  })
})

describe('Layout landmarks and route changes', () => {
  it('puts the top bar in a banner outside <main>', async () => {
    renderLayout('/p/demo/events', '/p/:slug/events', 'Events body')
    await screen.findByText('Events body')

    const main = screen.getByRole('main')
    expect(main).toHaveAttribute('id', 'main-content')
    expect(main).toHaveTextContent('Events body')
    expect(main.contains(screen.getByRole('banner'))).toBe(false)
  })

  it('moves focus to the content after navigating from the sidebar', async () => {
    mockMatchMedia(true)
    renderLayout('/p/demo/events', '/p/:slug/*', 'Page body')
    await screen.findByText('Page body')

    const link = screen.getByRole('link', { name: 'Other page' })
    link.focus()
    fireEvent.click(link)

    await vi.waitFor(() => expect(screen.getByRole('main')).toHaveFocus())
  })

  it('hides the activity rail on the not-found page', async () => {
    mockMatchMedia(true)
    localStorage.setItem('tripl-activity-open', '1')
    renderLayout('/p/demo/nowhere', '/p/:slug/*', '', { page: <NotFoundPage /> })
    await screen.findByText('Page not found')

    expect(screen.queryByTestId('activity-panel')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Toggle activity feed' })).toBeNull()
  })
})

describe('Layout accessibility', () => {
  it('has no axe violations with the demo banner and scenario strip', async () => {
    renderLayout('/p/demo/events', '/p/:slug/events', 'Events body', { isDemo: true })
    await screen.findByText('Events body')
    // The axe pass is "with the demo chrome": wait for its lazy chunk.
    await screen.findByRole('button', { name: 'Dismiss' })
    await expectNoAxeViolations(document.body, { page: true })
  })
})
