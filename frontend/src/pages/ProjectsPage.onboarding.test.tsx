/**
 * What a newcomer reads on All projects: the access note an empty list gives a
 * member who sees only the projects they are added to, and the Data sources
 * tile that says when a source is a demo's synthetic warehouse.
 */

import { render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ActiveOrgContext } from '@/components/active-org-context'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import type { DefaultProjectRole, Role } from '@/types'
import ProjectsPage from './ProjectsPage'

const ORG = 'acme'
const ACCESS_NOTE = /You see a project here once you are added to it/

function authValue(role: Role): AuthContextValue {
  return {
    user: {
      id: `${role}-1`,
      email: `${role}@example.com`,
      name: role,
      role,
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
}

function jsonResponse(data: unknown) {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const PROJECT = {
  id: 'proj-1',
  name: 'Alpha',
  slug: 'alpha',
  description: '',
  created_at: '2026-05-01T09:00:00Z',
  updated_at: '2026-05-10T09:00:00Z',
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
    open_incident_count: 0,
    failing_scan_config_count: 0,
    latest_scan_job: null,
    latest_signal: null,
  },
}

interface Backend {
  projects?: unknown[]
  dataSources?: unknown[]
  defaultProjectRole?: DefaultProjectRole
}

/** Answers the page's reads; returns the organization reads it was sent. */
function mockBackend({ projects = [], dataSources = [], defaultProjectRole = 'none' }: Backend) {
  const orgReads: string[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
    const url =
      typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
    if (url.endsWith('/api/v1/projects')) return Promise.resolve(jsonResponse(projects))
    if (url.endsWith('/api/v1/data-sources')) return Promise.resolve(jsonResponse(dataSources))
    if (url.endsWith(`/api/v1/orgs/${ORG}`)) {
      orgReads.push(url)
      return Promise.resolve(
        jsonResponse({ slug: ORG, name: 'Acme', default_project_role: defaultProjectRole }),
      )
    }
    return Promise.reject(new Error(`Unexpected request: ${url}`))
  })
  return { orgReads }
}

function renderPage(role: Role) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={authValue(role)}>
        <ActiveOrgContext.Provider
          value={{ slug: ORG, membership: { slug: ORG, name: 'Acme', role }, orgs: [] }}
        >
          <MemoryRouter>
            <ProjectsPage />
          </MemoryRouter>
        </ActiveOrgContext.Provider>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ProjectsPage — an empty list for a member', () => {
  it('says the team’s projects are shared one by one where the default access is none', async () => {
    const { orgReads } = mockBackend({ defaultProjectRole: 'none' })

    renderPage('member')

    expect(await screen.findByText('Keep your product analytics honest')).toBeInTheDocument()
    await waitFor(() => expect(orgReads).toHaveLength(1))
    expect(screen.getByText(ACCESS_NOTE)).toBeInTheDocument()
    // The note sits beside the ways in, not in place of them.
    expect(screen.getByRole('button', { name: /Generate demo project/i })).toBeInTheDocument()
  })

  it('says nothing where the default gives every member the projects', async () => {
    const { orgReads } = mockBackend({ defaultProjectRole: 'viewer' })

    renderPage('member')

    expect(await screen.findByText('Keep your product analytics honest')).toBeInTheDocument()
    await waitFor(() => expect(orgReads).toHaveLength(1))
    await waitFor(() => expect(screen.queryByText(ACCESS_NOTE)).toBeNull())
  })

  it('says nothing to an owner or admin, who sees every project, and asks nothing', async () => {
    const { orgReads } = mockBackend({ defaultProjectRole: 'none' })

    renderPage('admin')

    expect(await screen.findByText('Keep your product analytics honest')).toBeInTheDocument()
    expect(screen.queryByText(ACCESS_NOTE)).toBeNull()
    expect(orgReads).toHaveLength(0)
  })

  it('asks nothing once the member has a project to see', async () => {
    const { orgReads } = mockBackend({ projects: [PROJECT] })

    renderPage('member')

    expect(await screen.findByText('Alpha')).toBeInTheDocument()
    expect(screen.queryByText(ACCESS_NOTE)).toBeNull()
    expect(orgReads).toHaveLength(0)
  })
})

describe('ProjectsPage — the Data sources tile', () => {
  /** The tile's caption-and-figure pair (a `<dl>`). */
  function dataSourcesTile(): HTMLElement {
    const tile = screen.getByText('Data sources').closest('dl')
    if (!(tile instanceof HTMLElement)) throw new Error('no Data sources tile')
    return tile
  }

  it('says how many of the sources are a demo’s synthetic warehouse', async () => {
    mockBackend({
      projects: [PROJECT],
      dataSources: [
        { id: 'ds-demo', name: 'Demo warehouse', is_synthetic: true },
        { id: 'ds-real', name: 'Warehouse', is_synthetic: false },
      ],
    })

    renderPage('owner')

    await screen.findByText('Alpha')
    // Both sources, as the Data sources page lists them, and which one is not real.
    await waitFor(() =>
      expect(within(dataSourcesTile()).getByText('1 synthetic')).toBeInTheDocument(),
    )
    expect(within(dataSourcesTile()).getByText('2')).toBeInTheDocument()
  })

  it('qualifies nothing when every source is real', async () => {
    mockBackend({
      projects: [PROJECT],
      dataSources: [{ id: 'ds-real', name: 'Warehouse', is_synthetic: false }],
    })

    renderPage('owner')

    await screen.findByText('Alpha')
    await waitFor(() =>
      expect(dataSourcesTile().querySelector('dd')).toHaveTextContent(/^1$/),
    )
    expect(within(dataSourcesTile()).queryByText(/synthetic/)).toBeNull()
  })

  it('counts the projects once, in the Projects tile, not again beside the list', async () => {
    mockBackend({ projects: [PROJECT] })

    renderPage('owner')

    expect(await screen.findByText('Alpha')).toBeInTheDocument()
    expect(screen.queryByText(/\d+ tracked/)).toBeNull()
  })
})
