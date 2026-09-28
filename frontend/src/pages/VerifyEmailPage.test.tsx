import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { postLoginDestination } from '@/lib/authRedirect'
import { authAs } from '@/test/auth'
import VerifyEmailPage from './VerifyEmailPage'

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function urlOf(input: RequestInfo | URL) {
  return typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
}

const ANONYMOUS: AuthContextValue = {
  user: null,
  status: 'anonymous',
  error: null,
  isLoggingOut: false,
  logout: async () => {},
  refresh: () => {},
}

function unverified(): AuthContextValue {
  const auth = authAs('owner')
  return { ...auth, user: auth.user ? { ...auth.user, email_verified: false } : null }
}

/** Stand-in for AuthPage: shows where a sign-in would send the visitor. */
function AuthDestination() {
  const location = useLocation()
  return <p>after sign-in: {postLoginDestination(location.state)}</p>
}

function renderPage(entry: string, auth: AuthContextValue = ANONYMOUS) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <MemoryRouter initialEntries={[entry]}>
          <Routes>
            <Route path="/verify-email" element={<VerifyEmailPage />} />
            <Route path="/auth" element={<AuthDestination />} />
          </Routes>
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('VerifyEmailPage', () => {
  it('redeems the token once and offers the way on', async () => {
    const bodies: unknown[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input)
      if (url.endsWith('/api/v1/auth/verify-email/confirm')) {
        bodies.push(JSON.parse(String(init?.body ?? '{}')))
        return Promise.resolve(new Response(null, { status: 204 }))
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })
    // The API redeems a token only for the signed-in account it was sent to.
    renderPage('/verify-email?token=tok-1', unverified())

    expect(
      await screen.findByRole('heading', { name: 'Your email address is verified' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Continue to tripl' })).toHaveAttribute('href', '/')
    expect(bodies).toEqual([{ token: 'tok-1' }])
  })

  it('sends a visitor without a session to sign in and back to the same link', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.endsWith('/api/v1/auth/verify-email/confirm')) {
        return Promise.resolve(
          jsonResponse({ detail: 'Sign in to confirm your email address.' }, 401),
        )
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })
    renderPage('/verify-email?token=tok-2')

    expect(
      await screen.findByRole('heading', { name: 'Sign in to confirm your email address' }),
    ).toBeInTheDocument()
    // Not a dead link: nothing says it expired, and no resend is offered.
    expect(screen.queryByText(/expire after 24 hours/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Send a new link' })).toBeNull()

    const signIn = screen.getByRole('link', { name: 'Sign in' })
    expect(signIn).toHaveAttribute('href', '/auth')
    fireEvent.click(signIn)
    expect(
      await screen.findByText('after sign-in: /verify-email?token=tok-2'),
    ).toBeInTheDocument()
  })

  it('reads a refused link as invalid or meant for another account', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      Promise.resolve(jsonResponse({ detail: 'This verification link is invalid, expired, or already used.' }, 400)),
    )
    // Signed in and already verified: the link was for someone else.
    renderPage('/verify-email?token=theirs', authAs('owner'))

    expect(
      await screen.findByRole('heading', { name: 'This verification link does not work' }),
    ).toBeInTheDocument()
    expect(screen.getByText(/or it was sent to another account/)).toBeInTheDocument()
    expect(screen.getByText(/sign in as that account and open it again/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Send a new link' })).toBeNull()
  })

  it('shows the neutral error for a dead link and lets the signed-in account resend', async () => {
    const calls: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      calls.push(url)
      if (url.endsWith('/api/v1/auth/verify-email/confirm')) {
        return Promise.resolve(
          jsonResponse({ detail: 'This verification link is invalid, expired, or already used.' }, 400),
        )
      }
      if (url.endsWith('/api/v1/auth/verify-email/request')) {
        return Promise.resolve(new Response(null, { status: 204 }))
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })
    renderPage('/verify-email?token=old', unverified())

    expect(
      await screen.findByText(/This verification link is invalid, expired or already used/),
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Send a new link' }))

    expect(await screen.findByText(/A new link is on its way/)).toBeInTheDocument()
    await waitFor(() =>
      expect(calls.filter((url) => url.endsWith('/verify-email/request'))).toHaveLength(1),
    )
  })

  it('does not offer a resend to a visitor who is not signed in', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      Promise.resolve(jsonResponse({ detail: 'This verification link is invalid, expired, or already used.' }, 400)),
    )
    renderPage('/verify-email?token=old')

    expect(await screen.findByText(/Sign in to send yourself a new one/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Send a new link' })).toBeNull()
  })

  it('calls a link without a token dead without asking the API', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    renderPage('/verify-email')

    expect(screen.getByText('This link is missing its verification code.')).toBeInTheDocument()
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})
