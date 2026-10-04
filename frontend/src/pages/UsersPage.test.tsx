import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import InvitationsSection from './settings-area/InvitationsSection'
import UsersPage from './UsersPage'

const OWNER: AuthContextValue = {
  user: {
    id: 'owner-1',
    email: 'owner@example.com',
    name: 'Owner',
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

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const INVITATION = {
  id: 'inv-1',
  email: 'newcomer@example.com',
  role: 'editor',
  expires_at: '2026-09-01T00:00:00Z',
  is_expired: false,
}

function renderUsersPage(auth: AuthContextValue = OWNER, entry = '/settings/members') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })

  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <MemoryRouter initialEntries={[entry]}>
          {/* Members and Invitations are two sections since F20 PR7; the
              invite flows here are the Invitations section's. */}
          <InvitationsSection />
          <UsersPage />
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

function urlOf(input: RequestInfo | URL): string {
  return typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('UsersPage', () => {
  it('tells a non-owner once, in the shared read-only notice, why roles are locked (#237)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve(jsonResponse([])))

    renderUsersPage({ ...OWNER, user: OWNER.user && { ...OWNER.user, role: 'member' } })

    // One notice per section: Members' says why roles are locked, and the
    // Invitations section beside it says the same once for itself.
    const notes = await screen.findAllByRole('note')
    expect(notes.filter((note) => note.textContent?.includes('change roles'))).toHaveLength(1)
    expect(notes.find((note) => note.textContent?.includes('change roles'))).toHaveTextContent(
      'Only owners and admins can change roles or invite people.',
    )
  })

  it("focuses the invite email field when the palette's Invite member lands on ?invite=1", async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.resolve(jsonResponse([])))

    renderUsersPage(OWNER, '/settings/invitations?invite=1')

    await waitFor(() => expect(screen.getByLabelText('Email')).toHaveFocus())
  })

  it('surfaces a failed users fetch instead of claiming the instance is empty', async () => {
    // A page that by definition contains at least its reader used to answer a
    // failed fetch with "No users yet." — no error, no retry, nowhere to go.
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      if (urlOf(input).endsWith('/api/v1/users')) {
        return Promise.resolve(jsonResponse({ detail: 'boom' }, 500))
      }
      return Promise.resolve(jsonResponse([]))
    })

    renderUsersPage()

    expect(await screen.findByText("Couldn't load users")).toBeInTheDocument()
    expect(screen.queryByText('No users yet.')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /retry|try again/i })).toBeInTheDocument()
  })

  it('asks before revoking an invitation, because the link cannot be reissued', async () => {
    const revoked: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url = urlOf(input)
        if (url.endsWith('/api/v1/users')) return Promise.resolve(jsonResponse([]))
        if (url.endsWith('/api/v1/users/invitations') && !init?.method) {
          return Promise.resolve(jsonResponse([INVITATION]))
        }
        if (init?.method === 'DELETE') {
          revoked.push(url)
          return Promise.resolve(new Response(null, { status: 204 }))
        }
        return Promise.reject(new Error(`Unexpected request: ${url}`))
      },
    )

    renderUsersPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))

    expect(await screen.findByText('Revoke invitation')).toBeInTheDocument()
    expect(revoked).toEqual([])

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => {
      expect(screen.queryByText('Revoke invitation')).not.toBeInTheDocument()
    })
    expect(revoked).toEqual([])
  })

  it('splits the page into titled settings cards with counts', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.endsWith('/api/v1/users')) {
        return Promise.resolve(
          jsonResponse([
            { ...OWNER.user, created_at: '2026-01-02T12:00:00Z' },
            {
              id: 'u2',
              email: 'ed@example.com',
              name: 'Ed',
              role: 'editor',
              created_at: '2026-01-02T12:00:00Z',
              updated_at: '2026-01-02T12:00:00Z',
            },
          ]),
        )
      }
      if (url.endsWith('/api/v1/users/invitations')) return Promise.resolve(jsonResponse([INVITATION]))
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderUsersPage()

    expect(screen.getByRole('heading', { name: 'Invite a member' })).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'Pending invitations' })).toBeInTheDocument()
    expect(screen.getByText('1 pending')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Members' })).toBeInTheDocument()
    expect(await screen.findByText('2 people')).toBeInTheDocument()
    // The page's main action is the primary button, and the
    // instance-registration aside is gone.
    expect(screen.getByRole('button', { name: 'Create invite link' })).toHaveClass('bg-accent-solid')
    expect(screen.queryByText(/self-service registration/)).toBeNull()
  })

  it('selects the one-time invite link when the clipboard is unavailable', async () => {
    const clipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url = urlOf(input)
        if (url.endsWith('/api/v1/users/invitations') && init?.method === 'POST') {
          return Promise.resolve(
            jsonResponse({
              invitation: INVITATION,
              accept_path: '/invite/tok-123',
              expires_at: '2026-09-01T00:00:00Z',
            }),
          )
        }
        return Promise.resolve(jsonResponse([]))
      },
    )
    try {
      renderUsersPage()

      fireEvent.change(await screen.findByLabelText('Email'), {
        target: { value: 'newcomer@example.com' },
      })
      fireEvent.click(screen.getByRole('button', { name: 'Create invite link' }))

      const link = await screen.findByRole('textbox', { name: 'Invite link' })
      fireEvent.click(screen.getByRole('button', { name: 'Copy' }))

      expect(await screen.findByRole('alert')).toHaveTextContent(/Couldn’t reach the clipboard/)
      expect(link).toHaveFocus()
    } finally {
      if (clipboard) Object.defineProperty(navigator, 'clipboard', clipboard)
      else Reflect.deleteProperty(navigator, 'clipboard')
    }
  })
})
