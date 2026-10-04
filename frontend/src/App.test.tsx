import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'

// App routes its pages through `lazy(() => import(...))`, so the first render of a
// route pays to transform and evaluate that page's whole dependency tree — seconds
// of it on a busy machine, and all of it inside whatever `findBy*` timeout the
// assertion happens to have. Importing the pages this file asserts
// text against warms Vite's module cache during collection, so the lazy routes
// resolve from memory instead of racing a timer. The routes stay lazy in the app;
// only the test stops paying the import cost in the middle of an assertion.
import './pages/ProjectsPage'
import './pages/AuthPage'
import './pages/NotFoundPage'

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function urlOf(input: RequestInfo | URL) {
  return typeof input === 'string'
    ? input
    : input instanceof URL
      ? input.toString()
      : input.url
}

const OWNER = {
  id: 'user-1',
  email: 'owner@example.com',
  name: 'Owner',
  role: 'owner',
  created_at: '2026-04-18T10:00:00Z',
  updated_at: '2026-04-18T10:00:00Z',
}

function makeProject(slug: string, name: string) {
  return {
    id: `proj-${slug}`,
    name,
    slug,
    description: '',
    created_at: '2026-04-18T10:00:00Z',
    updated_at: '2026-04-18T10:00:00Z',
    summary: {
      event_type_count: 0,
      event_count: 0,
      active_event_count: 0,
      implemented_event_count: 0,
      review_pending_event_count: 0,
      archived_event_count: 0,
      variable_count: 0,
      scan_count: 0,
      alert_destination_count: 0,
      alert_rule_count: 0,
      monitoring_signal_count: 0,
      firing_monitor_count: 0,
      latest_scan_job: null,
      latest_signal: null,
    },
  }
}

function renderApp(path = '/') {
  window.history.pushState({}, '', path)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>,
  )
}

function authenticatedFetch(input: RequestInfo | URL) {
  const url =
    typeof input === 'string'
      ? input
      : input instanceof URL
        ? input.toString()
        : input.url

  if (url.endsWith('/api/v1/auth/me')) {
    return Promise.resolve(jsonResponse({
      id: 'user-1',
      email: 'owner@example.com',
      name: 'Owner',
      role: 'owner',
      created_at: '2026-04-18T10:00:00Z',
      updated_at: '2026-04-18T10:00:00Z',
    }))
  }

  if (url.endsWith('/api/v1/projects')) {
    return Promise.resolve(jsonResponse([]))
  }

  return Promise.reject(new Error(`Unexpected request: ${url}`))
}

describe('App', () => {
  beforeEach(() => {
    if (!window.localStorage) {
      Object.defineProperty(window, 'localStorage', {
        value: {
          store: {} as Record<string, string>,
          getItem(key: string) { return this.store[key] ?? null },
          setItem(key: string, value: string) { this.store[key] = value },
          removeItem(key: string) { delete this.store[key] },
          clear() { this.store = {} },
        },
        writable: true,
      })
    }
  })

  afterEach(() => {
    vi.restoreAllMocks()
    window.history.pushState({}, '', '/')
  })

  it('renders the tripl brand for authenticated users', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(authenticatedFetch)

    renderApp()

    expect(await screen.findByText('tripl')).toBeInTheDocument()
  })

  it('renders the sidebar navigation and signed-in user', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(authenticatedFetch)

    renderApp()

    // No projects in this fixture, so the workspace opens on the welcome hero
    // while the footer still renders the signed-in user and sign-out action.
    expect(await screen.findByText('Keep your product analytics honest')).toBeInTheDocument()
    expect(screen.getAllByText('Owner').length).toBeGreaterThan(0)
    // Sign out lives in the account menu the user row opens (#238 SH-39).
    fireEvent.keyDown(screen.getByRole('button', { name: /^Account menu/ }), { key: 'Enter' })
    expect(await screen.findByRole('menuitem', { name: 'Sign out' })).toBeInTheDocument()
  })

  it('redirects anonymous users to the auth page', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/auth/me')) {
        return Promise.resolve(jsonResponse({ detail: 'Authentication required' }, 401))
      }

      if (url.endsWith('/api/v1/auth/status')) {
        return Promise.resolve(jsonResponse({ has_users: true }))
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderApp()

    expect(await screen.findByText('Sign in to tripl')).toBeInTheDocument()
    // The register tab ("Create account") is distinct from the submit button.
    expect(screen.getByText('Create account')).toBeInTheDocument()
  })

  it('redirects the legacy event-detail URL to the canonical monitoring route', async () => {
    // B1: the legacy `/events/detail/:eventId` route used to mount the detail
    // page with no `:scope` and crash. It now redirects to the canonical URL.
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.endsWith('/api/v1/auth/me')) {
        return Promise.resolve(jsonResponse({
          id: 'user-1',
          email: 'owner@example.com',
          name: 'Owner',
          role: 'owner',
          created_at: '2026-04-18T10:00:00Z',
          updated_at: '2026-04-18T10:00:00Z',
        }))
      }
      // The shell resolves `:slug` against this list before it mounts anything
      // project-scoped, so the project the URL names has to
      // exist for the routed redirect below it to run at all.
      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([makeProject('demo', 'Demo')]))
      }
      // Detail-page data is irrelevant to the redirect assertion; a 404 makes
      // the page render its ErrorState gracefully rather than crash.
      return Promise.resolve(jsonResponse({ detail: 'Not found' }, 404))
    })

    renderApp('/p/demo/events/detail/event-1')

    await waitFor(() => {
      expect(window.location.pathname).toBe('/p/demo/monitoring/event/event-1')
    })
  })

  it('redirects the legacy /p/:slug/settings/scans URL to the top-level Scans surface', async () => {
    // Scans moved out of the settings tab strip to `/p/:slug/scans`. The legacy
    // path must NOT fall through to ProjectSettingsPage: `scans` is no longer a
    // functional tab there, so that page would bounce the user to
    // `/p/demo/events` and the bookmark would land on the wrong surface.
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.endsWith('/api/v1/auth/me')) return Promise.resolve(jsonResponse(OWNER))
      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([makeProject('demo', 'Demo')]))
      }
      // The Scans surface itself needs a config list and its activity summary
      // (an object, not a list); everything else may 404.
      if (url.includes('/scans/activity')) {
        return Promise.resolve(
          jsonResponse({ window_from: '2026-01-01T00:00:00Z', window_to: '2026-01-02T00:00:00Z', items: [] }),
        )
      }
      if (url.includes('/scans')) return Promise.resolve(jsonResponse([]))
      return Promise.resolve(jsonResponse({ detail: 'Not found' }, 404))
    })

    renderApp('/p/demo/settings/scans')

    await waitFor(() => {
      expect(window.location.pathname).toBe('/p/demo/scans')
    })
    // …and the Scans surface really mounted, rather than the redirect merely
    // rewriting the URL under a different page.
    // Exactly one heading names the page (the PageHeader h1); the list panel
    // below it is "All scans".
    expect(await screen.findByRole('heading', { level: 1, name: 'Scans' })).toBeInTheDocument()
  })

  it('redirects the legacy /p/:slug/settings/scans/:id URL and keeps the scan id', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.endsWith('/api/v1/auth/me')) return Promise.resolve(jsonResponse(OWNER))
      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([makeProject('demo', 'Demo')]))
      }
      return Promise.resolve(jsonResponse({ detail: 'Not found' }, 404))
    })

    renderApp('/p/demo/settings/scans/scan-1')

    await waitFor(() => {
      expect(window.location.pathname).toBe('/p/demo/scans/scan-1')
    })
  })

  it.each([
    ['/p/demo/settings/alerting', '/p/demo/alerting', ''],
    // An alert message sent before the move: the delivery id and both anchors
    // must survive the hop, or the link opens on the page index.
    [
      '/p/demo/settings/alerting/dlv-1?item=event:e1&incident=g1',
      '/p/demo/alerting/dlv-1',
      '?item=event:e1&incident=g1',
    ],
    ['/p/demo/settings/event-types/et-1?tab=settings', '/p/demo/event-types/et-1', '?tab=settings'],
    ['/p/demo/settings/variables/var-how', '/p/demo/variables/var-how', ''],
    ['/p/demo/settings/branches/br-1', '/p/demo/branches/br-1', ''],
    ['/p/demo/settings/history', '/p/demo/history', ''],
    ['/p/demo/settings/audit', '/p/demo/audit', ''],
  ])('redirects the old %s address to its top-level route (JR-25)', async (from, pathname, search) => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.endsWith('/api/v1/auth/me')) return Promise.resolve(jsonResponse(OWNER))
      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([makeProject('demo', 'Demo')]))
      }
      return Promise.resolve(jsonResponse({ detail: 'Not found' }, 404))
    })

    renderApp(from)

    await waitFor(() => {
      expect(window.location.pathname).toBe(pathname)
    })
    expect(decodeURIComponent(window.location.search)).toBe(search)
  })

  it('serves /p/:slug/variables/:id as the property page, not a redirect', async () => {
    // #245 JR-10 kept this address alive as a redirect into /settings; it is
    // the canonical variable page now (JR-25).
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.endsWith('/api/v1/auth/me')) return Promise.resolve(jsonResponse(OWNER))
      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([makeProject('demo', 'Demo')]))
      }
      return Promise.resolve(jsonResponse({ detail: 'Not found' }, 404))
    })

    renderApp('/p/demo/variables/var-how')

    // The variable page itself has mounted (its fetch 404s here, so it shows its
    // own error state), which means any redirect would already have committed.
    expect(await screen.findByRole('heading', { name: /load this property/ })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/p/demo/variables/var-how')
  })

  it('sends the bare /p/:slug to the project home', async () => {
    // #250 JR-1: the bare project URL used to render Events, not the Overview
    // every in-app project entry opens.
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.endsWith('/api/v1/auth/me')) return Promise.resolve(jsonResponse(OWNER))
      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([makeProject('demo', 'Demo')]))
      }
      return Promise.resolve(jsonResponse({ detail: 'Not found' }, 404))
    })

    renderApp('/p/demo')

    await waitFor(() => {
      expect(window.location.pathname).toBe('/p/demo/overview')
    })
  })

  it('redirects "/" into the single project when exactly one exists', async () => {
    // UX-11 / UX-25: one project ⇒ "/" is a redundant hop, so land directly in
    // that project's overview. Post-redirect page data is irrelevant to the
    // assertion; a 404 lets the overview render its ErrorState rather than crash.
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.endsWith('/api/v1/auth/me')) return Promise.resolve(jsonResponse(OWNER))
      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([makeProject('demo', 'Demo')]))
      }
      return Promise.resolve(jsonResponse({ detail: 'Not found' }, 404))
    })

    renderApp('/')

    await waitFor(() => {
      expect(window.location.pathname).toBe('/p/demo/overview')
    })
  })

  it('keeps "/" on the workspace dashboard when multiple projects exist', async () => {
    // 2+ projects ⇒ the portfolio view is the right landing; no redirect.
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.endsWith('/api/v1/auth/me')) return Promise.resolve(jsonResponse(OWNER))
      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([makeProject('alpha', 'Alpha'), makeProject('beta', 'Beta')]))
      }
      if (url.endsWith('/api/v1/data-sources')) return Promise.resolve(jsonResponse([]))
      return Promise.resolve(jsonResponse({ detail: 'Not found' }, 404))
    })

    renderApp('/')

    // The dashboard's always-present header renders, and the URL never bounces.
    expect(await screen.findByRole('heading', { level: 1, name: 'All projects' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/')
  })

  it('redirects "/projects" to the workspace portfolio', async () => {
    // The sidebar/back-links route to the portfolio; "/projects" is a friendly
    // alias that lands on the same all-projects workspace (never bounced).
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.endsWith('/api/v1/auth/me')) return Promise.resolve(jsonResponse(OWNER))
      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([makeProject('alpha', 'Alpha'), makeProject('beta', 'Beta')]))
      }
      if (url.endsWith('/api/v1/data-sources')) return Promise.resolve(jsonResponse([]))
      return Promise.resolve(jsonResponse({ detail: 'Not found' }, 404))
    })

    renderApp('/projects')

    expect(await screen.findByRole('heading', { level: 1, name: 'All projects' })).toBeInTheDocument()
    await waitFor(() => {
      expect(window.location.pathname).toBe('/workspace')
    })
  })

  it('renders the not-found page for an unknown authed route', async () => {
    // No catch-all used to leave unmatched paths on a blank screen; the catch-all
    // now renders the app shell + a not-found state with a way back to the portfolio.
    vi.spyOn(globalThis, 'fetch').mockImplementation(authenticatedFetch)

    renderApp('/totally-unknown')

    expect(await screen.findByText('Page not found')).toBeInTheDocument()
    expect(screen.getByText('Back to all projects')).toBeInTheDocument()
    await waitFor(() => {
      expect(document.title).toBe('Page not found · tripl')
    })
  })

  it('keeps the project shell on an unmatched path under a real project', async () => {
    // `/p/demo/<no-such-page>` used to fall through to the GLOBAL catch-all,
    // which declares no `:slug` — so the shell collapsed to "No project
    // selected" and the tab kept the previous page's title.
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.endsWith('/api/v1/auth/me')) return Promise.resolve(jsonResponse(OWNER))
      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([makeProject('demo', 'Demo')]))
      }
      return Promise.resolve(jsonResponse({ detail: 'Not found' }, 404))
    })

    renderApp('/p/demo/this-route-does-not-exist')

    expect(await screen.findByText('Page not found')).toBeInTheDocument()
    // The project is still in scope: its name heads the breadcrumb trail.
    expect(screen.getAllByText('Demo').length).toBeGreaterThan(0)
    await waitFor(() => {
      expect(document.title).toBe('Page not found · demo · tripl')
    })
  })

  it('shows a not-found state for an unknown project slug without fanning out', async () => {
    // An invented slug used to render a complete, working-looking project shell
    // backed by a dozen 404ing project-scoped requests, with the bogus slug
    // echoed into the document title.
    const requested: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      requested.push(url)
      if (url.endsWith('/api/v1/auth/me')) return Promise.resolve(jsonResponse(OWNER))
      if (url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse([makeProject('demo', 'Demo')]))
      }
      return Promise.resolve(jsonResponse({ detail: 'Not found' }, 404))
    })

    renderApp('/p/no-such-project-xyz/overview')

    expect(await screen.findByText('Project not found')).toBeInTheDocument()
    // Exactly ONE request names the bogus slug — the confirmation that decides
    // the project does not exist. No event-types / signals / branches / stream /
    // activity fan-out behind a shell that was never going to work.
    const scoped = [...new Set(requested.filter((u) => u.includes('no-such-project-xyz')))]
    expect(scoped).toEqual(['/api/v1/projects/no-such-project-xyz'])
    // …and the invented slug is not echoed back as if it named a workspace.
    await waitFor(() => {
      expect(document.title).toBe('Page not found · tripl')
    })
  })
})

describe('App auth links', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    window.history.pushState({}, '', '/')
  })

  const owner = {
    id: 'user-1',
    email: 'owner@example.com',
    name: 'Owner',
    role: 'owner',
    created_at: '2026-04-18T10:00:00Z',
    updated_at: '2026-04-18T10:00:00Z',
  }

  it('returns to the full deep link, query and fragment included, after signing in (SHELL-14)', async () => {
    let signedIn = false
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.endsWith('/api/v1/auth/me')) {
        return Promise.resolve(
          signedIn ? jsonResponse(owner) : jsonResponse({ detail: 'Authentication required' }, 401),
        )
      }
      if (url.endsWith('/api/v1/auth/status')) {
        return Promise.resolve(jsonResponse({ has_users: true, registration_enabled: true }))
      }
      if (url.endsWith('/api/v1/auth/login')) {
        signedIn = true
        return Promise.resolve(jsonResponse(owner))
      }
      if (url.endsWith('/api/v1/projects')) return Promise.resolve(jsonResponse([]))
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderApp('/workspace?incident=inc-2#card')

    fireEvent.change(await screen.findByLabelText('Email'), {
      target: { value: 'owner@example.com' },
    })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'secret' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    await waitFor(() =>
      expect(`${window.location.pathname}${window.location.search}${window.location.hash}`).toBe(
        '/workspace?incident=inc-2#card',
      ),
    )
  })

  it('tells a signed-in visitor on an invitation link who they are, and keeps the link (SHELL-16)', async () => {
    let signedIn = true
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.endsWith('/api/v1/auth/me')) {
        return Promise.resolve(
          signedIn ? jsonResponse(owner) : jsonResponse({ detail: 'Authentication required' }, 401),
        )
      }
      if (url.endsWith('/api/v1/auth/logout')) {
        signedIn = false
        return Promise.resolve(new Response(null, { status: 204 }))
      }
      if (url.endsWith('/api/v1/auth/invitations/tok-1')) {
        return Promise.resolve(
          jsonResponse({ email: 'new@example.com', role: 'editor', expires_at: '2026-12-01T00:00:00Z' }),
        )
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderApp('/invite/tok-1')

    // F20: signed in, the invitation can join THIS account; the page says who
    // that is and offers a sign-out that keeps the link.
    expect(await screen.findByRole('button', { name: 'Accept with this account' })).toBeInTheDocument()
    expect(screen.getByText('owner@example.com')).toBeInTheDocument()
    expect(window.location.pathname).toBe('/invite/tok-1')

    fireEvent.click(screen.getByRole('button', { name: 'Sign out and use another account' }))

    expect(await screen.findByLabelText('Password')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Join this tripl workspace' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/invite/tok-1')
  })
})
