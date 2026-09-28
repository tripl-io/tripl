import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { LAST_ORG_STORAGE_KEY, setCurrentOrgSlug } from './lib/activeOrg'

/**
 * The organization addressing of the real route tree (F20 PR7): what a unit
 * test of one route cannot see, because the shell (Layout) around it decides
 * things first.
 */

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })
}

function urlOf(input: RequestInfo | URL) {
  return typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
}

const ME = {
  id: 'user-1',
  email: 'owner@example.com',
  name: 'Owner',
  role: 'owner',
  created_at: '2026-04-18T10:00:00Z',
  updated_at: '2026-04-18T10:00:00Z',
  is_platform_admin: false,
  orgs: [
    { slug: 'default', name: 'Default', role: 'owner' },
    { slug: 'acme', name: 'Acme', role: 'owner' },
  ],
}

/** `default` holds no `web`; `acme` does. Anything else is not asserted on. */
function orgFetch(input: RequestInfo | URL) {
  const url = urlOf(input)
  if (url.endsWith('/api/v1/auth/me')) return Promise.resolve(jsonResponse(ME))
  if (url.endsWith('/api/v1/orgs/default/projects')) return Promise.resolve(jsonResponse([]))
  if (url.endsWith('/api/v1/orgs/acme/projects')) {
    return Promise.resolve(jsonResponse([{ id: 'proj-web', slug: 'web', name: 'Web' }]))
  }
  if (url.includes('/api/v1/orgs/default/projects/web')) {
    return Promise.resolve(jsonResponse({ detail: 'Project not found' }, 404))
  }
  return Promise.resolve(jsonResponse({ detail: 'not mocked' }, 404))
}

function renderApp(path: string) {
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

describe('App organization addressing', () => {
  beforeEach(() => {
    // The last organization used is `default`, the one WITHOUT the project.
    localStorage.setItem(LAST_ORG_STORAGE_KEY, 'default')
    sessionStorage.setItem(LAST_ORG_STORAGE_KEY, 'default')
  })

  afterEach(() => {
    vi.restoreAllMocks()
    localStorage.removeItem(LAST_ORG_STORAGE_KEY)
    sessionStorage.removeItem(LAST_ORG_STORAGE_KEY)
    setCurrentOrgSlug(null)
    window.history.pushState({}, '', '/')
  })

  it('moves a legacy /p/ link to the organization holding the project, not "Project not found" in the active one', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(orgFetch)

    renderApp('/p/web/events?status=live#row-3')

    await waitFor(() => expect(window.location.pathname).toBe('/o/acme/p/web/events'))
    expect(window.location.search).toBe('?status=live')
    expect(window.location.hash).toBe('#row-3')
    expect(screen.queryByText(/Project not found/i)).toBeNull()
  })

  it('opens /o/:org/settings/* for a member organization as the takeover bound to it', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(orgFetch)

    renderApp('/o/acme/settings/members')

    await waitFor(() => expect(window.location.pathname).toBe('/settings/members'))
    expect(new URLSearchParams(window.location.search).get('org')).toBe('acme')
  })

  it('answers "Organization not found" for /o/:org/settings/* of an organization the user is not in', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(orgFetch)

    renderApp('/o/elsewhere/settings/members')

    expect(await screen.findByRole('heading', { name: 'Organization not found' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/o/elsewhere/settings/members')
  })
})
