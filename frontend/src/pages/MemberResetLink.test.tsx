import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ActiveOrgContext } from '@/components/active-org-context'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import type { Role } from '@/types'
import UsersPage from './UsersPage'

/**
 * Members › Create reset link: without email, "Forgot your password?" sends
 * nothing, so an owner or admin creates a single-use link for a member and
 * hands it over (POST /orgs/{org}/members/{id}/password-reset-link).
 */

function authAs(role: Role): AuthContextValue {
  return {
    user: {
      id: 'me-1',
      email: 'me@example.com',
      name: 'Me',
      role,
      is_platform_admin: false,
      orgs: [{ slug: 'acme', name: 'Acme', role }],
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

const MEMBERS = [
  { id: 'me-1', email: 'me@example.com', name: 'Me', role: 'owner', created_at: '2026-01-01T00:00:00Z' },
  { id: 'own-2', email: 'olga@example.com', name: 'Olga', role: 'owner', created_at: '2026-01-02T00:00:00Z' },
  { id: 'vi-1', email: 'vi@example.com', name: 'Vi', role: 'member', created_at: '2026-01-03T00:00:00Z' },
]

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function urlOf(input: RequestInfo | URL): string {
  return typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
}

const RESET_URL = /\/api\/v1\/orgs\/acme\/members\/([^/]+)\/password-reset-link$/

function mockApi(options: { refuse?: string } = {}) {
  const posts: string[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(
    (input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      if (method === 'POST' && RESET_URL.test(url)) {
        posts.push(url)
        const userId = RESET_URL.exec(url)?.[1] ?? ''
        if (options.refuse) {
          return Promise.resolve(jsonResponse({ detail: options.refuse }, 403))
        }
        return Promise.resolve(
          jsonResponse(
            {
              user_id: userId,
              email: 'vi@example.com',
              reset_path: '/auth?reset_token=tok-1',
              expires_at: '2026-10-10T13:00:00Z',
            },
            201,
          ),
        )
      }
      // The roster is read in pages: `/members?limit=…&offset=…`.
      if (url.startsWith('/api/v1/orgs/acme/members?')) return Promise.resolve(jsonResponse(MEMBERS))
      if (url.endsWith('/api/v1/auth/status')) {
        return Promise.resolve(jsonResponse({ has_users: true, registration_enabled: true }))
      }
      return Promise.reject(new Error(`Unexpected request: ${method} ${url}`))
    },
  )
  return posts
}

function renderMembers(role: Role = 'owner') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const membership = { slug: 'acme', name: 'Acme', role }
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={authAs(role)}>
        <ActiveOrgContext.Provider value={{ slug: 'acme', membership, orgs: [membership] }}>
          <MemoryRouter>
            <UsersPage />
          </MemoryRouter>
        </ActiveOrgContext.Provider>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Members › Create reset link', () => {
  it('asks first, then shows the link once under the member’s row', async () => {
    const posts = mockApi()
    renderMembers()

    fireEvent.click(await screen.findByRole('button', { name: 'Create reset link for Vi' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Create a password reset link for Vi?')
    expect(dialog).toHaveTextContent(/give it to Vi and no one else/)
    expect(posts).toHaveLength(0)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create link' }))

    const link = await screen.findByRole('textbox', { name: 'Password reset link' })
    expect(link).toHaveValue(`${window.location.origin}/auth?reset_token=tok-1`)
    expect(posts).toEqual(['/api/v1/orgs/acme/members/vi-1/password-reset-link'])
    expect(screen.getByText(/Password reset link for Vi/)).toBeInTheDocument()
    expect(screen.getByText(/signs them out everywhere/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss the reset link for Vi' }))
    await waitFor(() =>
      expect(screen.queryByRole('textbox', { name: 'Password reset link' })).toBeNull(),
    )
  })

  it('creates nothing when the question is cancelled', async () => {
    const posts = mockApi()
    renderMembers()

    fireEvent.click(await screen.findByRole('button', { name: 'Create reset link for Vi' }))
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(posts).toHaveLength(0)
    expect(screen.queryByRole('textbox', { name: 'Password reset link' })).toBeNull()
  })

  it('shows a refusal on the row it was for', async () => {
    mockApi({ refuse: 'Only a platform admin can create a password reset link for a platform admin.' })
    renderMembers()

    fireEvent.click(await screen.findByRole('button', { name: 'Create reset link for Vi' }))
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Create link' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /Could not create a reset link for Vi: Only a platform admin/,
    )
  })

  it('offers it for every other member, never for oneself', async () => {
    mockApi()
    renderMembers()

    expect(await screen.findByRole('button', { name: 'Create reset link for Olga' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create reset link for Vi' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create reset link for Me' })).toBeNull()
  })

  it('does not offer an admin a link for an owner', async () => {
    mockApi()
    renderMembers('admin')

    expect(await screen.findByRole('button', { name: 'Create reset link for Vi' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create reset link for Olga' })).toBeNull()
  })
})
