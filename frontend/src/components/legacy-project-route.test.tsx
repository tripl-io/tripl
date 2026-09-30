import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { authAs } from '@/test/auth'
import { AuthContext } from '@/components/auth-context'
import type { OrgMembership } from '@/types'
import { LegacyProjectRoute } from './legacy-project-route'

/**
 * `/p/:slug/…` moves to `/o/{org}/p/:slug/…` (F20 PR7): the one organization
 * of the user's that holds the slug, else the default organization, else their
 * only one — with the query string and hash kept.
 */

function Where() {
  const location = useLocation()
  return <div data-testid="where">{`${location.pathname}${location.search}${location.hash}`}</div>
}

function jsonResponse(data: unknown) {
  return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } })
}

/** `/orgs/{org}/projects` answers with the slugs each organization holds. */
function mockProjects(holdings: Record<string, string[]>) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
    const org = /\/api\/v1\/orgs\/([^/]+)\/projects$/.exec(url)?.[1]
    if (org && org in holdings) {
      return Promise.resolve(jsonResponse(holdings[org]!.map((slug) => ({ id: `${org}-${slug}`, slug, name: slug }))))
    }
    return Promise.reject(new Error(`Unexpected request: ${url}`))
  })
}

function renderAt(entry: string, orgs: OrgMembership[]) {
  const auth = authAs('owner')
  const value = { ...auth, user: auth.user && { ...auth.user, orgs } }
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={value}>
        <MemoryRouter initialEntries={[entry]}>
          <Routes>
            <Route path="/p/:slug" element={<LegacyProjectRoute />}>
              <Route path="*" element={<div data-testid="legacy-page">legacy page</div>} />
            </Route>
            <Route path="/o/*" element={<Where />} />
          </Routes>
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

const DEFAULT: OrgMembership = { slug: 'default', name: 'Default', role: 'owner' }
const ACME: OrgMembership = { slug: 'acme', name: 'Acme', role: 'member' }
const BETA: OrgMembership = { slug: 'beta', name: 'Beta', role: 'admin' }

afterEach(() => {
  vi.restoreAllMocks()
})

describe('LegacyProjectRoute', () => {
  it('goes to the one organization that holds the slug, keeping the query and hash', async () => {
    mockProjects({ default: ['api'], acme: ['web'] })
    renderAt('/p/web/events/all?status=live#row-3', [DEFAULT, ACME])

    expect(await screen.findByTestId('where')).toHaveTextContent('/o/acme/p/web/events/all?status=live#row-3')
  })

  it('falls back to the default organization when no organization holds the slug', async () => {
    mockProjects({ default: [], acme: [] })
    renderAt('/p/web/overview', [ACME, DEFAULT])

    expect(await screen.findByTestId('where')).toHaveTextContent('/o/default/p/web/overview')
  })

  it('falls back to the default organization when several hold the slug', async () => {
    mockProjects({ default: ['web'], acme: ['web'] })
    renderAt('/p/web/overview', [ACME, DEFAULT])

    expect(await screen.findByTestId('where')).toHaveTextContent('/o/default/p/web/overview')
  })

  it("uses the user's only organization without asking for projects", async () => {
    const fetchSpy = mockProjects({})
    renderAt('/p/web/scans?x=1', [ACME])

    expect(await screen.findByTestId('where')).toHaveTextContent('/o/acme/p/web/scans?x=1')
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('picks the first organization when the user is outside the default one and none holds it', async () => {
    mockProjects({ acme: [], beta: [] })
    renderAt('/p/web', [BETA, ACME])

    expect(await screen.findByTestId('where')).toHaveTextContent('/o/beta/p/web')
  })

  it('renders the legacy page for a session in no organization', () => {
    const fetchSpy = mockProjects({})
    renderAt('/p/web/events', [])

    expect(screen.getByTestId('legacy-page')).toBeInTheDocument()
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})
