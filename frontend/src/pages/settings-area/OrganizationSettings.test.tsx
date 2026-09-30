import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ActiveOrgProvider } from '@/components/active-org-provider'
import { AuthContext } from '@/components/auth-context'
import { authAs } from '@/test/auth'
import type { DefaultProjectRole, OrgMembership, Role } from '@/types'
import UsersPage from '@/pages/UsersPage'
import InvitationsSection from './InvitationsSection'
import OrganizationGeneralSection from './OrganizationGeneralSection'

/**
 * Organization settings (F20 PR7): Details (rename, read-only slug, delete with
 * a typed slug), Members (role, remove, transfer) and Invitations, all against
 * `/api/v1/orgs/{org}/…` for the organization in the URL.
 */

type Call = { method: string; url: string; body?: string }

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })
}

const MEMBERS = [
  { id: 'me', email: 'me@example.com', name: 'Me', role: 'owner', created_at: '2026-01-01T00:00:00Z' },
  { id: 'ad-1', email: 'ad@example.com', name: 'Ada', role: 'admin', created_at: '2026-01-02T00:00:00Z' },
  { id: 'mb-1', email: 'mb@example.com', name: 'Mo', role: 'member', created_at: '2026-01-03T00:00:00Z' },
]

function orgResponse(
  slug: string,
  role: Role,
  name = 'Acme',
  defaultProjectRole: DefaultProjectRole = 'none',
) {
  return {
    id: `org-${slug}`,
    slug,
    name,
    role,
    status: 'active',
    is_default: slug === 'default',
    default_project_role: defaultProjectRole,
    created_at: '2026-01-01T00:00:00Z',
  }
}

function mockApi(org = 'acme', role: Role = 'owner') {
  const calls: Call[] = []
  // The server's copy: a PATCH lands in it, so the refetch after a save reads it back.
  let stored = { name: 'Acme', default_project_role: 'none' as DefaultProjectRole }
  vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
    const method = (init?.method ?? 'GET').toUpperCase()
    const body = init?.body ? String(init.body) : undefined
    calls.push({ method, url, body })
    const base = `/api/v1/orgs/${org}`
    if (url === base && method === 'GET') {
      return Promise.resolve(jsonResponse(orgResponse(org, role, stored.name, stored.default_project_role)))
    }
    if (url === base && method === 'PATCH') {
      const patch = JSON.parse(body ?? '{}') as { name?: string; default_project_role?: DefaultProjectRole }
      stored = { ...stored, ...patch }
      return Promise.resolve(jsonResponse(orgResponse(org, role, stored.name, stored.default_project_role)))
    }
    if (url === base && method === 'DELETE') {
      return Promise.resolve(jsonResponse({ ...orgResponse(org, role), status: 'deleting' }, 202))
    }
    if (url === '/api/v1/orgs' && method === 'POST') {
      const created = JSON.parse(body ?? '{}') as { slug: string; name: string }
      return Promise.resolve(jsonResponse(orgResponse(created.slug, 'owner', created.name), 201))
    }
    if (url === `${base}/members`) return Promise.resolve(jsonResponse(MEMBERS))
    if (url.startsWith(`${base}/members/`) && method === 'PATCH') {
      const { role: next } = JSON.parse(body ?? '{}') as { role: Role }
      const id = url.slice(`${base}/members/`.length)
      return Promise.resolve(jsonResponse({ ...MEMBERS.find((m) => m.id === id), role: next }))
    }
    if (url.startsWith(`${base}/members/`) && method === 'DELETE') {
      return Promise.resolve(
        jsonResponse({
          user_id: 'mb-1',
          project_memberships_removed: 2,
          api_keys_revoked: 1,
          invitations_revoked: 0,
          group_memberships_removed: 3,
        }),
      )
    }
    if (url === `${base}/transfer-ownership`) return Promise.resolve(jsonResponse({ ...MEMBERS[2], role: 'owner' }))
    if (url === `${base}/users/invitations` && method === 'GET') return Promise.resolve(jsonResponse([]))
    if (url === `${base}/users/invitations` && method === 'POST') {
      const { email, role: invited } = JSON.parse(body ?? '{}') as { email: string; role: Role }
      return Promise.resolve(
        jsonResponse({
          invitation: {
            id: 'inv-1', email, role: invited, invited_by_user_id: 'me',
            expires_at: '2026-10-01T00:00:00Z', created_at: '2026-09-28T00:00:00Z', is_expired: false,
          },
          accept_path: '/invite/tok-1',
          expires_at: '2026-10-01T00:00:00Z',
        }),
      )
    }
    if (url === '/api/v1/auth/me') return Promise.resolve(jsonResponse({}))
    return Promise.reject(new Error(`Unexpected request: ${method} ${url}`))
  })
  return calls
}

function Where() {
  return <div data-testid="where">{useLocation().pathname}</div>
}

function renderSection(
  section: React.ReactNode,
  {
    org = 'acme',
    role = 'owner' as Role,
    platformAdmin = false,
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } }),
  } = {},
) {
  const orgs: OrgMembership[] =
    org === 'default'
      ? [{ slug: 'default', name: 'Default', role }]
      : [
          { slug: 'default', name: 'Default', role: 'member' },
          { slug: org, name: 'Acme', role },
        ]
  const auth = authAs('member', 'me')
  const value = auth.user ? { ...auth, user: { ...auth.user, orgs, is_platform_admin: platformAdmin } } : auth
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={value}>
        <MemoryRouter initialEntries={[`/o/${org}/settings`]}>
          <ActiveOrgProvider>
            <Routes>
              <Route path="/o/:org/settings" element={section} />
              <Route path="*" element={<Where />} />
            </Routes>
          </ActiveOrgProvider>
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Organization › Details', () => {
  it('renames the organization and shows the slug read-only', async () => {
    const calls = mockApi()
    renderSection(<OrganizationGeneralSection />)

    const name = await screen.findByLabelText('Name')
    const slug = screen.getByLabelText('Slug')
    expect(slug).toHaveValue('acme')
    expect(slug).toHaveAttribute('readonly')
    expect(screen.getByText(/cannot be changed/)).toBeInTheDocument()

    fireEvent.change(name, { target: { value: 'Acme Labs' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(calls.find((c) => c.method === 'PATCH')).toEqual(
        expect.objectContaining({ url: '/api/v1/orgs/acme', body: JSON.stringify({ name: 'Acme Labs' }) }),
      )
    })
    expect(await screen.findByText('Saved')).toBeInTheDocument()
  })

  it('deletes only after the slug is typed, then leaves the organization', async () => {
    const calls = mockApi()
    renderSection(<OrganizationGeneralSection />)

    fireEvent.click(await screen.findByRole('button', { name: 'Delete organization' }))
    const dialog = await screen.findByRole('alertdialog')
    const confirmButton = within(dialog).getByRole('button', { name: 'Delete organization' })
    expect(confirmButton).toBeDisabled()

    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: 'acm' } })
    expect(confirmButton).toBeDisabled()
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: 'acme' } })
    fireEvent.click(confirmButton)

    await waitFor(() => {
      expect(calls.find((c) => c.method === 'DELETE')).toEqual(
        expect.objectContaining({ url: '/api/v1/orgs/acme', body: JSON.stringify({ confirm_slug: 'acme' }) }),
      )
    })
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(/^\/$/))
  })

  it('offers no danger zone to an admin, or for the default organization', async () => {
    mockApi('acme', 'admin')
    const { unmount } = renderSection(<OrganizationGeneralSection />, { role: 'admin' })
    expect(await screen.findByLabelText('Name')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete organization' })).toBeNull()
    unmount()
    vi.restoreAllMocks()

    mockApi('default', 'owner')
    renderSection(<OrganizationGeneralSection />, { org: 'default', role: 'owner' })
    expect(await screen.findByLabelText('Name')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete organization' })).toBeNull()
  })

  it('shows a member the name without an editor', async () => {
    mockApi('acme', 'member')
    renderSection(<OrganizationGeneralSection />, { role: 'member' })
    expect(await screen.findByText('Acme')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('sets the default access to projects, with what it means (F20 PR15)', async () => {
    const calls = mockApi()
    renderSection(<OrganizationGeneralSection />)

    const select = await screen.findByLabelText('Default access')
    expect(select).toHaveValue('none')
    expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'No access',
      'Viewer',
      'Editor',
    ])
    expect(screen.getByText(/Projects are invite-only/)).toBeInTheDocument()
    const save = screen.getByRole('button', { name: 'Save default access' })
    expect(save).toBeDisabled()

    fireEvent.change(select, { target: { value: 'viewer' } })
    expect(screen.getByText(/Every member can open every project and read it/)).toBeInTheDocument()
    fireEvent.click(save)

    await waitFor(() => {
      expect(calls.find((c) => c.method === 'PATCH')).toEqual(
        expect.objectContaining({
          url: '/api/v1/orgs/acme',
          body: JSON.stringify({ default_project_role: 'viewer' }),
        }),
      )
    })
    expect(await screen.findByText('Saved')).toBeInTheDocument()
    expect(screen.getByLabelText('Default access')).toHaveValue('viewer')
  })

  it('lets an admin set the default access, and shows a member it read-only', async () => {
    mockApi('acme', 'admin')
    const { unmount } = renderSection(<OrganizationGeneralSection />, { role: 'admin' })
    expect(await screen.findByLabelText('Default access')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save default access' })).toBeInTheDocument()
    unmount()
    vi.restoreAllMocks()

    mockApi('acme', 'member')
    renderSection(<OrganizationGeneralSection />, { role: 'member' })
    expect(await screen.findByText('Default access')).toBeInTheDocument()
    expect(screen.getByText('No access')).toBeInTheDocument()
    expect(screen.queryByLabelText('Default access')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save default access' })).toBeNull()
  })

  it('lets a platform admin create an organization and opens it', async () => {
    const calls = mockApi()
    renderSection(<OrganizationGeneralSection />, { platformAdmin: true })

    const card = (await screen.findByRole('heading', { name: 'Create organization' })).closest('section') ?? document.body
    fireEvent.change(within(card as HTMLElement).getByLabelText('Name'), { target: { value: 'Beta' } })
    fireEvent.change(within(card as HTMLElement).getByLabelText('Slug'), { target: { value: 'beta' } })
    fireEvent.click(within(card as HTMLElement).getByRole('button', { name: 'Create organization' }))

    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('/o/beta'))
    expect(calls.find((c) => c.method === 'POST' && c.url === '/api/v1/orgs')?.body).toBe(
      JSON.stringify({ name: 'Beta', slug: 'beta' }),
    )
  })

  it('hides "Create organization" from everyone but a platform admin', async () => {
    mockApi()
    renderSection(<OrganizationGeneralSection />)
    expect(await screen.findByLabelText('Name')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Create organization' })).toBeNull()
  })

  it('offers "Create organization" to every account in hosted mode', async () => {
    mockApi()
    const answer = vi.mocked(globalThis.fetch).getMockImplementation()!
    vi.mocked(globalThis.fetch).mockImplementation((input, init) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      if (url === '/api/v1/auth/status') {
        return Promise.resolve(
          jsonResponse({
            has_users: true,
            registration_enabled: true,
            email_configured: true,
            deployment_mode: 'hosted',
            email_verification_required: true,
          }),
        )
      }
      return answer(input, init)
    })
    renderSection(<OrganizationGeneralSection />)
    expect(await screen.findByRole('heading', { name: 'Create organization' })).toBeInTheDocument()
  })
})

describe('Organization › Members', () => {
  it('changes a role through the organization API', async () => {
    const calls = mockApi()
    renderSection(<UsersPage />)

    fireEvent.change(await screen.findByLabelText('Role for Mo'), { target: { value: 'admin' } })

    await waitFor(() => {
      expect(calls.find((c) => c.method === 'PATCH')).toEqual(
        expect.objectContaining({ url: '/api/v1/orgs/acme/members/mb-1', body: JSON.stringify({ role: 'admin' }) }),
      )
    })
  })

  it('removes a member after a confirmation and says what went with them', async () => {
    const calls = mockApi()
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    renderSection(<UsersPage />, { queryClient })

    // Each row's button names its person, not a list of identical "Remove"s.
    fireEvent.click(await screen.findByRole('button', { name: 'Remove Mo' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Remove Mo?')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove member' }))

    await waitFor(() => {
      expect(calls.find((c) => c.method === 'DELETE')?.url).toBe('/api/v1/orgs/acme/members/mb-1')
    })
    expect(
      await screen.findByText('Removed Mo, with 2 project memberships, 1 API key, 3 group memberships.'),
    ).toBeInTheDocument()
    // The Groups page's copies of the roster go stale with it.
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['acme', 'orgGroups'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['acme', 'orgMembers'] })
  })

  it('transfers ownership after a confirmation', async () => {
    const calls = mockApi()
    renderSection(<UsersPage />)

    fireEvent.click(await screen.findByRole('button', { name: 'Transfer ownership to Mo' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Transfer ownership' }))

    await waitFor(() => {
      expect(calls.find((c) => c.url.endsWith('/transfer-ownership'))?.body).toBe(JSON.stringify({ user_id: 'mb-1' }))
    })
  })

  it('offers an admin no removal of an owner and no transfer', async () => {
    mockApi('acme', 'admin')
    renderSection(<UsersPage />, { role: 'admin' })

    await screen.findByText('Mo')
    expect(screen.queryByRole('button', { name: /^Transfer ownership/ })).toBeNull()
    // Ada (admin) and Mo (member) can be removed; the owner and the reader cannot.
    expect(screen.getAllByRole('button', { name: /^Remove / })).toHaveLength(2)
  })

  it("shows a failed transfer's own error, not an earlier failed removal's", async () => {
    mockApi()
    const answer = vi.mocked(globalThis.fetch).getMockImplementation()!
    vi.mocked(globalThis.fetch).mockImplementation((input, init) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      const method = (init?.method ?? 'GET').toUpperCase()
      if (method === 'DELETE') return Promise.resolve(jsonResponse({ detail: 'Removal refused' }, 409))
      if (url.endsWith('/transfer-ownership')) return Promise.resolve(jsonResponse({ detail: 'Transfer refused' }, 409))
      return answer(input, init)
    })
    renderSection(<UsersPage />)

    fireEvent.click(await screen.findByRole('button', { name: 'Remove Mo' }))
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Remove member' }))
    expect(await screen.findByText('Removal refused')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Transfer ownership to Mo' }))
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Transfer ownership' }))
    expect(await screen.findByText('Transfer refused')).toBeInTheDocument()
    expect(screen.queryByText('Removal refused')).toBeNull()
  })
})

describe('Organization › Invitations', () => {
  it('invites into the organization in the URL at an organization role', async () => {
    const calls = mockApi()
    renderSection(<InvitationsSection />)

    fireEvent.change(await screen.findByLabelText('Email'), { target: { value: 'new@example.com' } })
    fireEvent.change(screen.getByLabelText('Role'), { target: { value: 'admin' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create invite link' }))

    await waitFor(() => {
      expect(calls.find((c) => c.method === 'POST')).toEqual(
        expect.objectContaining({
          url: '/api/v1/orgs/acme/users/invitations',
          body: JSON.stringify({ email: 'new@example.com', role: 'admin' }),
        }),
      )
    })
  })

  it('tells a member that only owners and admins invite', () => {
    mockApi('acme', 'member')
    renderSection(<InvitationsSection />, { role: 'member' })
    expect(screen.getByText('Only owners and admins can invite people.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create invite link' })).toBeNull()
  })
})
