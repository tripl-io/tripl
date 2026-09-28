import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import { platformApi, type PlatformUser } from '@/api/platform'
import { AuthContext } from '@/components/auth-context'
import { authAs } from '@/test/auth'
import PlatformUsersSection from './PlatformUsersSection'

/**
 * Platform › User accounts (F20): every account, with the platform admin flag
 * granted and revoked after a confirm; your own flag cannot be revoked here.
 */

const STAMP = '2026-09-28T00:00:00Z'

function user(overrides: Partial<PlatformUser> & { id: string; email: string }): PlatformUser {
  return {
    name: null,
    is_platform_admin: false,
    email_verified: true,
    created_at: STAMP,
    org_count: 1,
    ...overrides,
  }
}

const SELF_ID = 'owner-1'
const ME = user({ id: SELF_ID, email: 'owner@example.com', name: 'Olivia', is_platform_admin: true })
const OPS = user({ id: 'u-ops', email: 'ops@example.com', is_platform_admin: true })
const ALICE = user({ id: 'u-alice', email: 'alice@example.com', name: 'Alice', email_verified: false, org_count: 2 })

function renderSection(users: PlatformUser[] = [ME, OPS, ALICE]) {
  const list = vi.spyOn(platformApi, 'listUsers').mockResolvedValue({ items: users, total: users.length })
  const auth = authAs('owner', SELF_ID)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={{ ...auth, user: auth.user && { ...auth.user, is_platform_admin: true } }}>
        <MemoryRouter>
          <PlatformUsersSection />
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
  return { list }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Platform › User accounts', () => {
  it('lists accounts with their verification and platform admin flag', async () => {
    renderSection()
    const alice = (await screen.findByText('Alice')).closest('tr')!
    expect(alice).toHaveTextContent('alice@example.com')
    expect(alice).toHaveTextContent('Unverified')
    expect(within(alice).getByRole('button', { name: 'Make alice@example.com a platform admin' })).toBeInTheDocument()
    const ops = screen.getByText('ops@example.com').closest('tr')!
    expect(ops).toHaveTextContent('Platform admin')
  })

  it('never offers to revoke your own flag', async () => {
    renderSection()
    const me = (await screen.findByText('Olivia')).closest('tr')!
    expect(within(me).getByText('You')).toBeInTheDocument()
    expect(within(me).queryByRole('button')).toBeNull()
  })

  it('searches by email or name', async () => {
    const { list } = renderSection()
    await screen.findByText('Alice')
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search users' }), { target: { value: 'ali' } })
    await waitFor(() =>
      expect(list).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'ali', offset: 0 }), expect.anything()),
    )
  })

  it('grants platform admin after a confirm', async () => {
    const grant = vi.spyOn(platformApi, 'setPlatformAdmin').mockResolvedValue({ ...ALICE, is_platform_admin: true })
    renderSection()
    fireEvent.click(await screen.findByRole('button', { name: 'Make alice@example.com a platform admin' }))
    const confirm = await screen.findByRole('alertdialog')
    expect(confirm).toHaveTextContent('Make platform admin?')
    fireEvent.click(within(confirm).getByRole('button', { name: 'Make platform admin' }))
    await waitFor(() => expect(grant).toHaveBeenCalledWith('u-alice', true))
  })

  it('keeps the confirm open with the refusal to revoke the last admin', async () => {
    vi.spyOn(platformApi, 'setPlatformAdmin').mockRejectedValue(
      new ApiError('Cannot revoke the last platform admin', 409),
    )
    renderSection()
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke platform admin from ops@example.com' }))
    const confirm = await screen.findByRole('alertdialog')
    fireEvent.click(within(confirm).getByRole('button', { name: 'Revoke' }))
    expect(await within(confirm).findByText(/Cannot revoke the last platform admin/)).toBeInTheDocument()
  })
})
