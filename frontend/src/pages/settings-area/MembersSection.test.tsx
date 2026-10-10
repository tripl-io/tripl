import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import type { Role } from '@/types'
import MembersSection from './MembersSection'

function signedInAs(role: Role): AuthContextValue {
  return {
    user: {
      id: 'me-1',
      email: 'me@example.com',
      name: 'Me',
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

function renderMembers(role: Role) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={signedInAs(role)}>
        <MemoryRouter initialEntries={['/settings/members']}>
          <MembersSection />
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
    Promise.resolve(
      new Response('[]', { status: 200, headers: { 'Content-Type': 'application/json' } }),
    ),
  )
})

afterEach(() => {
  vi.restoreAllMocks()
})

// The page a new owner opens to add teammates said "Invite people from
// Invitations." in plain text, and on a phone the rail holding Invitations is
// behind the menu button.
describe('MembersSection', () => {
  it.each<Role>(['owner', 'admin'])('gives an %s a way to invite people from here', (role) => {
    renderMembers(role)

    const invite = screen.getByRole('link', { name: 'Invite people' })
    // Straight to the form, with the email field focused.
    expect(invite).toHaveAttribute('href', '/settings/invitations?invite=1')
    expect(screen.queryByText(/Invite people from Invitations/)).not.toBeInTheDocument()
  })

  it('offers a member no invite button, as the rail offers them no Invitations', () => {
    renderMembers('member')

    expect(screen.getByRole('heading', { level: 1, name: 'Members' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Invite people' })).not.toBeInTheDocument()
  })
})
