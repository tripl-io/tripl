import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { invitationsApi } from '@/api/invitations'
import { authApi } from '@/api/auth'
import { ApiError } from '@/api/client'
import { authStatusKey } from '@/lib/queryKeys'
import { InviteMemberCard } from './UsersPage'
import InvitePage from './InvitePage'

function AuthDestination() {
  const location = useLocation()
  return <div>{JSON.stringify(location.state)}</div>
}

function renderDemo(element: React.ReactNode, publicDemo: boolean | null = true) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (publicDemo !== null) qc.setQueryData(authStatusKey(), { public_demo: publicDemo })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/invite/demo-token']}>
        <Routes>
          <Route path="/invite/:token" element={element} />
          <Route path="/auth" element={<AuthDestination />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => vi.restoreAllMocks())

describe('public demo invitations', () => {
  it('creates a member link, explains no email is sent and limits the role options', async () => {
    vi.spyOn(invitationsApi, 'list').mockResolvedValue([])
    const create = vi.spyOn(invitationsApi, 'create').mockResolvedValue({
      invitation: {
        id: 'invite', email: 'colleague@example.com', role: 'member',
        invited_by_user_id: null, created_at: '2026-10-01T00:00:00Z',
        expires_at: '2026-10-10T00:00:00Z', is_expired: false,
      },
      accept_path: '/invite/demo-token', expires_at: '2026-10-10T00:00:00Z',
    })
    renderDemo(<InviteMemberCard actorIsOrgOwner />)
    expect(screen.getByText(/No email is sent/)).toBeInTheDocument()
    expect(within(screen.getByLabelText('Role')).getAllByRole('option')).toHaveLength(1)
    expect(screen.getByRole('option', { name: 'Member' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'colleague@example.com' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create invite link' }))
    await waitFor(() => expect(create).toHaveBeenCalledWith('colleague@example.com', 'member'))
    expect(await screen.findByLabelText('Invite link')).toHaveValue(`${window.location.origin}/invite/demo-token`)
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('requires Google sign-in and preserves the invitation return path', async () => {
    vi.spyOn(invitationsApi, 'preview').mockResolvedValue({
      email: 'colleague@example.com', role: 'member', expires_at: '2026-10-10T00:00:00Z',
    })
    renderDemo(<InvitePage />)
    const signIn = await screen.findByRole('button', { name: 'Sign in with Google' })
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Your name')).not.toBeInTheDocument()
    expect(screen.getByText(/viewer access to the demo projects of this organization/)).toBeInTheDocument()
    fireEvent.click(signIn)
    expect(await screen.findByText('{"from":{"pathname":"/invite/demo-token"}}')).toBeInTheDocument()
  })

  it('preserves password acceptance outside the public demo', async () => {
    vi.spyOn(invitationsApi, 'preview').mockResolvedValue({
      email: 'colleague@example.com', role: 'member', expires_at: '2026-10-10T00:00:00Z',
    })
    renderDemo(<InvitePage />, false)
    expect(await screen.findByLabelText('Password')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in with Google' })).not.toBeInTheDocument()
  })

  it('keeps a valid normal invitation usable when the status probe fails', async () => {
    vi.spyOn(authApi, 'status').mockRejectedValue(new Error('Status unavailable'))
    vi.spyOn(invitationsApi, 'preview').mockResolvedValue({
      email: 'colleague@example.com', role: 'member', expires_at: '2026-10-10T00:00:00Z',
    })
    renderDemo(<InvitePage />, null)
    expect(await screen.findByLabelText('Password')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in with Google' })).not.toBeInTheDocument()
  })

  it('does not offer password creation while the demo status is still being checked', async () => {
    let resolveStatus!: (status: Awaited<ReturnType<typeof authApi.status>>) => void
    vi.spyOn(authApi, 'status').mockImplementation(() => new Promise((resolve) => { resolveStatus = resolve }))
    vi.spyOn(invitationsApi, 'preview').mockResolvedValue({
      email: 'colleague@example.com', role: 'member', expires_at: '2026-10-10T00:00:00Z',
    })
    renderDemo(<InvitePage />, null)
    expect(await screen.findByText('Checking sign-in options…')).toBeInTheDocument()
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
    resolveStatus({ public_demo: true, has_users: true, registration_enabled: false })
    expect(await screen.findByRole('button', { name: 'Sign in with Google' })).toBeInTheDocument()
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
  })

  it('keeps the invitation open when the signed-in Google address does not match', async () => {
    vi.spyOn(invitationsApi, 'preview').mockResolvedValue({
      email: 'colleague@example.com', role: 'member', expires_at: '2026-10-10T00:00:00Z',
    })
    const accept = vi.spyOn(invitationsApi, 'acceptSignedIn').mockRejectedValue(
      new ApiError('This invitation is for another email address.', 403),
    )
    renderDemo(<InvitePage signedIn={{ email: 'another@example.com', isSigningOut: false, signOut: vi.fn() }} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Accept with this account' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('This invitation is for another email address.')
    expect(accept).toHaveBeenCalledWith('demo-token')
    expect(screen.getByRole('button', { name: 'Sign out and use another account' })).toBeInTheDocument()
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
  })
})
