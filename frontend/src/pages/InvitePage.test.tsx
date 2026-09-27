import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import InvitePage from './InvitePage'
import { at } from '@/test/at'


function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function urlOf(input: RequestInfo | URL) {
  return typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
}

const TOKEN = 'invite-token-abc'

function renderInvitePage(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/invite/${TOKEN}`]}>
        <Routes>
          <Route path="/invite/:token" element={<InvitePage />} />
          <Route path="/" element={<div>Signed in home</div>} />
          <Route path="/auth" element={<div>Sign in screen</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('InvitePage', () => {
  it('shows who the invitation is for and the role it grants', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.includes(`/auth/invitations/${TOKEN}`)) {
        return Promise.resolve(
          jsonResponse({
            email: 'invitee@example.com',
            role: 'member',
            expires_at: '2026-08-01T00:00:00Z',
          }),
        )
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderInvitePage()

    expect(await screen.findByText('invitee@example.com')).toBeInTheDocument()
    // The invitation grants an organization role (F20 PR4: owner | admin |
    // member); what a member may do in each project lives on the project row.
    expect(screen.getByText('Member')).toBeInTheDocument()
    // The role is explained, not just named (SH-32).
    expect(
      screen.getByText(
        'Member sees the projects they are added to, as an editor or a viewer of each.',
      ),
    ).toBeInTheDocument()
    // The address is fixed by the invitation, so there must be no way to
    // redirect it to a different identity.
    expect(screen.queryByLabelText(/email/i)).not.toBeInTheDocument()
  })

  it('surfaces the single neutral error for an unusable link without guessing why', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.includes('/auth/invitations/')) {
        return Promise.resolve(
          jsonResponse({ detail: 'This invitation link is invalid, expired, or already used.' }, 400),
        )
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderInvitePage()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/invalid, expired, or already used/i)
    // The title stops inviting, and the way out is a real button (SH-32).
    expect(
      screen.getByRole('heading', { level: 1, name: 'This invite link no longer works' }),
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Go to sign in' }))
    expect(await screen.findByText('Sign in screen')).toBeInTheDocument()
    // No password form for a link that cannot be redeemed.
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
  })

  it('does not call a link dead when the check itself failed, and offers a retry', async () => {
    // A rate limit (or a 5xx, or no network) says nothing about the link. Telling
    // a valid invitee it "no longer works" sent them away from a good invite.
    let calls = 0
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.includes(`/auth/invitations/${TOKEN}`)) {
        calls += 1
        if (calls === 1) {
          return Promise.resolve(jsonResponse({ detail: 'Too many requests.' }, 429))
        }
        return Promise.resolve(
          jsonResponse({
            email: 'invitee@example.com',
            role: 'member',
            expires_at: '2026-08-01T00:00:00Z',
          }),
        )
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderInvitePage()

    expect(
      await screen.findByRole('heading', { level: 1, name: 'Could not check this invitation' }),
    ).toBeInTheDocument()
    expect(screen.queryByText('This invite link no longer works')).not.toBeInTheDocument()
    expect(screen.queryByText('Ask whoever invited you to send a new link.')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('invitee@example.com')).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { level: 1, name: 'Join this tripl workspace' }),
    ).toBeInTheDocument()
  })

  it('accepts the invitation and lands the new user in the app', async () => {
    const accepted = vi.fn()
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url = urlOf(input)
        if (url.includes(`/auth/invitations/${TOKEN}/accept`)) {
          accepted(JSON.parse(String(init?.body)))
          return Promise.resolve(
            jsonResponse({ id: 'u1', email: 'invitee@example.com', role: 'member' }, 201),
          )
        }
        if (url.includes(`/auth/invitations/${TOKEN}`)) {
          return Promise.resolve(
            jsonResponse({
              email: 'invitee@example.com',
              role: 'member',
              expires_at: '2026-08-01T00:00:00Z',
            }),
          )
        }
        // No /auth/me: the accept response IS the session (SHELL-17).
        return Promise.reject(new Error(`Unexpected request: ${url}`))
      },
    )

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderInvitePage(qc)

    // The password policy is stated up front, not learned from a 422.
    const password = await screen.findByLabelText('Password')
    expect(password).toHaveAccessibleDescription('At least 12 characters, with a number and symbol.')
    expect(password).toHaveAttribute('autocomplete', 'new-password')

    fireEvent.change(password, {
      target: { value: 'Password123!' },
    })
    fireEvent.change(screen.getByLabelText(/Your name/i), { target: { value: 'New Person' } })
    fireEvent.click(screen.getByRole('button', { name: /accept invitation/i }))

    await waitFor(() => expect(accepted).toHaveBeenCalled())
    // Only a password and a display name are ever submitted — never an address
    // or a role, which the server takes from the invitation.
    expect(at(accepted.mock.calls, 0)[0]).toEqual({ password: 'Password123!', name: 'New Person' })
    expect(await screen.findByText('Signed in home')).toBeInTheDocument()
    expect(qc.getQueryData(['auth', 'me'])).toEqual({
      id: 'u1',
      email: 'invitee@example.com',
      role: 'member',
    })
  })
  it('marks a missing password under the field instead of a browser bubble (AU-4)', async () => {
    const accepted = vi.fn()
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.includes(`/auth/invitations/${TOKEN}/accept`)) {
        accepted()
        return Promise.reject(new Error('should not submit'))
      }
      if (url.includes(`/auth/invitations/${TOKEN}`)) {
        return Promise.resolve(
          jsonResponse({ email: 'invitee@example.com', role: 'member', expires_at: '2026-08-01T00:00:00Z' }),
        )
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderInvitePage()

    const password = await screen.findByLabelText('Password')
    // Accept stays pressable, so it can say what is missing.
    fireEvent.click(screen.getByRole('button', { name: /accept invitation/i }))

    expect(password).toHaveAttribute('aria-invalid', 'true')
    expect(password).toHaveAccessibleDescription(/Required/)
    await waitFor(() => expect(password).toHaveFocus())
    expect(accepted).not.toHaveBeenCalled()
  })

  it('lets the new user check the password they typed (SH-31)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.includes(`/auth/invitations/${TOKEN}`)) {
        return Promise.resolve(
          jsonResponse({ email: 'invitee@example.com', role: 'member', expires_at: '2026-08-01T00:00:00Z' }),
        )
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })

    renderInvitePage()

    const password = await screen.findByLabelText('Password')
    expect(password).toHaveAttribute('type', 'password')
    const toggle = screen.getByRole('button', { name: 'Show password' })
    fireEvent.click(toggle)
    expect(password).toHaveAttribute('type', 'text')
    expect(toggle).toHaveAttribute('aria-pressed', 'true')
  })
})
