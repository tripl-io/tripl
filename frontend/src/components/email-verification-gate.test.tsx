import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AuthStatusResponse } from '@/api/auth'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { authStatusKey } from '@/lib/queryKeys'
import { authAs } from '@/test/auth'
import { EmailVerificationGate } from './email-verification-gate'

const SELF_HOSTED: AuthStatusResponse = {
  has_users: true,
  registration_enabled: true,
  email_configured: true,
  deployment_mode: 'self_hosted',
  email_verification_required: false,
}

const HOSTED: AuthStatusResponse = {
  ...SELF_HOSTED,
  deployment_mode: 'hosted',
  email_verification_required: true,
}

function session(emailVerified: boolean, overrides: Partial<AuthContextValue> = {}): AuthContextValue {
  const auth = authAs('owner')
  return {
    ...auth,
    ...overrides,
    user: auth.user ? { ...auth.user, email_verified: emailVerified } : null,
  }
}

function renderGate(auth: AuthContextValue, status: AuthStatusResponse | null) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (status) queryClient.setQueryData(authStatusKey(), status)
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <EmailVerificationGate>
          <div>The app</div>
        </EmailVerificationGate>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('EmailVerificationGate', () => {
  it('renders the app for a verified account without asking the instance', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    renderGate(session(true), null)
    expect(screen.getByText('The app')).toBeInTheDocument()
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('never gates a self-hosted instance, even for an unverified account', () => {
    renderGate(session(false), SELF_HOSTED)
    expect(screen.getByText('The app')).toBeInTheDocument()
  })

  it('shows "Check your inbox" to an unverified account in hosted mode', () => {
    renderGate(session(false), HOSTED)
    expect(screen.getByRole('heading', { name: 'Check your inbox' })).toBeInTheDocument()
    expect(screen.getByText('owner@example.com')).toBeInTheDocument()
    expect(screen.queryByText('The app')).toBeNull()
  })

  it('resends the link and holds the button for a cooldown', async () => {
    const calls: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      calls.push(url)
      if (url.endsWith('/api/v1/auth/verify-email/request')) {
        return Promise.resolve(new Response(null, { status: 204 }))
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })
    renderGate(session(false), HOSTED)

    fireEvent.click(screen.getByRole('button', { name: 'Resend email' }))

    expect(await screen.findByText(/A new link is on its way/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Resend email' })).toBeDisabled()
    expect(calls).toEqual(['/api/v1/auth/verify-email/request'])
  })

  it('shows why a resend failed', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ detail: 'Email delivery is not configured' }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )
    renderGate(session(false), HOSTED)

    fireEvent.click(screen.getByRole('button', { name: 'Resend email' }))

    expect(await screen.findByText('Email delivery is not configured')).toBeInTheDocument()
  })

  it('asks again and signs out on request', () => {
    const refresh = vi.fn()
    const logout = vi.fn(async () => {})
    renderGate(session(false, { refresh, logout }), HOSTED)

    fireEvent.click(screen.getByRole('button', { name: 'I have verified it' }))
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))

    expect(refresh).toHaveBeenCalledTimes(1)
    expect(logout).toHaveBeenCalledTimes(1)
  })
})
