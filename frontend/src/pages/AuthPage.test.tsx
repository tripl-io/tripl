import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import AuthPage from './AuthPage'

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function urlOf(input: RequestInfo | URL) {
  return typeof input === 'string'
    ? input
    : input instanceof URL
      ? input.toString()
      : input.url
}

// Default the unauthenticated /auth/status probe to a provisioned instance with
// registration open, so the owner note stays hidden and the sign-up tab stays
// visible unless a test opts into another instance shape.
function mockStatus(hasUsers: boolean, registrationEnabled = true) {
  vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
    const url = urlOf(input)
    if (url.endsWith('/api/v1/auth/status')) {
      return Promise.resolve(
        jsonResponse({ has_users: hasUsers, registration_enabled: registrationEnabled }),
      )
    }
    return Promise.reject(new Error(`Unexpected request: ${url}`))
  })
}

// Broader router that also answers the password-reset endpoints. Re-implements
// the (already-installed) fetch spy so tests can opt into the reset flows.
function mockAuthFetch(options: { emailConfigured?: boolean } = {}) {
  const emailConfigured = options.emailConfigured ?? true
  vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
    const url = urlOf(input)
    if (url.endsWith('/api/v1/auth/status')) {
      return Promise.resolve(jsonResponse({ has_users: true, registration_enabled: true }))
    }
    if (url.endsWith('/api/v1/auth/password-reset/request')) {
      return Promise.resolve(
        jsonResponse({ message: 'neutral', email_configured: emailConfigured }),
      )
    }
    if (url.endsWith('/api/v1/auth/password-reset/confirm')) {
      return Promise.resolve(jsonResponse({ message: 'done' }))
    }
    return Promise.reject(new Error(`Unexpected request: ${url}`))
  })
}

function renderAuth(initialEntry = '/auth') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <AuthPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AuthPage', () => {
  beforeEach(() => {
    mockStatus(true)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows sign-in copy in the card header by default (login mode)', () => {
    renderAuth()

    expect(
      screen.getByRole('heading', { name: 'Sign in to tripl' }),
    ).toBeInTheDocument()
    expect(
      screen.getByText('Use your account to access the workspace and monitoring tools.'),
    ).toBeInTheDocument()
  })

  it('updates the card title and subtitle to registration copy when the register tab is active (UX-22)', () => {
    renderAuth()

    // The register tab is the only "Create account" control while login mode is active.
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))

    expect(
      screen.getByRole('heading', { name: 'Create your tripl account' }),
    ).toBeInTheDocument()
    expect(
      screen.getByText(
        'Set up your account to start tracking coverage, monitoring drift, and routing alerts.',
      ),
    ).toBeInTheDocument()
    // Login-only copy is gone once registration mode is active.
    expect(
      screen.queryByRole('heading', { name: 'Sign in to tripl' }),
    ).not.toBeInTheDocument()
  })

  it('restores the sign-in copy when switching back to Existing account', () => {
    renderAuth()

    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))
    fireEvent.click(screen.getByRole('button', { name: 'Existing account' }))

    expect(
      screen.getByRole('heading', { name: 'Sign in to tripl' }),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('heading', { name: 'Create your tripl account' }),
    ).not.toBeInTheDocument()
  })

  it('gives the register tab and submit button distinct accessible names (UX .23)', () => {
    renderAuth()

    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))

    // Tab and submit no longer collide on the same accessible name.
    expect(screen.getByRole('button', { name: 'Create account' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create your account' })).toBeInTheDocument()
  })

  it('advertises the unified password policy on the register form (UX .11)', () => {
    renderAuth()

    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))

    const password = screen.getByLabelText('Password') as HTMLInputElement
    expect(password.minLength).toBe(12)
    expect(
      screen.getByText('At least 12 characters, with a number and symbol.'),
    ).toBeInTheDocument()
  })

  it('exposes a forgot-password entry point in the login footer (UX .13)', () => {
    renderAuth()

    expect(
      screen.getByRole('button', { name: 'Forgot your password?' }),
    ).toBeInTheDocument()
    // The old static "contact your owner" copy is gone from the default footer —
    // it now only appears as a fallback after a request on an email-less instance.
    expect(
      screen.queryByText(/Contact your instance owner to reset/),
    ).not.toBeInTheDocument()
  })

  it('sends a reset request and shows a neutral confirmation when email is configured', async () => {
    mockAuthFetch({ emailConfigured: true })
    renderAuth()

    fireEvent.click(screen.getByRole('button', { name: 'Forgot your password?' }))
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'user@example.com' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Send reset link' }))

    expect(
      await screen.findByText(/a password reset link is on its way/),
    ).toBeInTheDocument()
    // Neutral: it never states whether the account exists.
    expect(screen.queryByText(/no account/i)).not.toBeInTheDocument()
  })

  it('opens the reset-request form from ?mode=forgot, the session dialog link (SH-35)', () => {
    mockAuthFetch({ emailConfigured: true })
    renderAuth('/auth?mode=forgot')

    expect(screen.getByRole('button', { name: 'Send reset link' })).toBeInTheDocument()
  })

  it('says before the request when the instance cannot send email (ST-24)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      if (String(input).endsWith('/api/v1/auth/status')) {
        return Promise.resolve(
          jsonResponse({ has_users: true, registration_enabled: true, email_configured: false }),
        )
      }
      return Promise.reject(new Error(`Unhandled fetch: ${String(input)}`))
    })
    renderAuth('/auth?mode=forgot')

    expect(
      await screen.findByText(/This instance can't send email, so no reset link will arrive/),
    ).toBeInTheDocument()
    // Still a form: the server's answer is the same neutral one either way.
    expect(screen.getByRole('button', { name: 'Send reset link' })).toBeEnabled()
  })

  it('falls back to the contact-owner copy when the instance has no email configured', async () => {
    mockAuthFetch({ emailConfigured: false })
    renderAuth()

    fireEvent.click(screen.getByRole('button', { name: 'Forgot your password?' }))
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'user@example.com' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Send reset link' }))

    expect(
      await screen.findByText(/Contact your instance owner to reset your password/),
    ).toBeInTheDocument()
  })

  it('enters reset mode from an emailed ?reset_token link and confirms a new password', async () => {
    mockAuthFetch()
    renderAuth('/auth?reset_token=tok-123')

    // The token in the URL switches the card straight into reset mode.
    expect(
      screen.getByRole('heading', { name: 'Choose a new password' }),
    ).toBeInTheDocument()

    const newPassword = screen.getByLabelText('New password') as HTMLInputElement
    expect(newPassword.minLength).toBe(12)

    fireEvent.change(newPassword, { target: { value: 'BrandNewPass9!' } })
    fireEvent.click(screen.getByRole('button', { name: 'Set new password' }))

    expect(
      await screen.findByText(/Your password has been reset/),
    ).toBeInTheDocument()
  })

  it('shows the first-account owner note only on a fresh instance in register mode (UX .13)', async () => {
    mockStatus(false)
    renderAuth()

    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))

    expect(
      await screen.findByText(/The first account on a new instance becomes the owner/),
    ).toBeInTheDocument()

    // The note is register-only: it disappears back in login mode.
    fireEvent.click(screen.getByRole('button', { name: 'Existing account' }))
    expect(
      screen.queryByText(/The first account on a new instance becomes the owner/),
    ).not.toBeInTheDocument()
  })

  it('hides the owner note on a provisioned instance (UX .13)', async () => {
    renderAuth()

    // Let the /auth/status query settle (defaults to has_users: true).
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))

    expect(
      screen.queryByText(/The first account on a new instance becomes the owner/),
    ).not.toBeInTheDocument()
  })

  it('offers no sign-up form when the instance has registration closed (tripl-jfm3.79)', async () => {
    mockStatus(true, false)
    renderAuth()

    // The policy is stated up front instead of being discovered from a 403 after
    // the visitor has filled in the form.
    expect(
      await screen.findByText(/Sign-ups are closed on this instance/),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create account' })).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Create your account' }),
    ).not.toBeInTheDocument()
    // Signing in still works — only the sign-up half is withdrawn.
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('keeps the sign-up tab on an instance with registration open', async () => {
    renderAuth()

    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled())

    expect(
      await screen.findByRole('button', { name: 'Create account' }),
    ).toBeInTheDocument()
    expect(screen.queryByText(/Sign-ups are closed on this instance/)).not.toBeInTheDocument()
  })
  it('marks missing fields inline instead of a browser bubble, and sends nothing (AU-4)', async () => {
    renderAuth()
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    const email = screen.getByLabelText('Email')
    const password = screen.getByLabelText('Password')
    expect(email).toHaveAttribute('aria-invalid', 'true')
    expect(email).toHaveAccessibleDescription('Required')
    expect(password).toHaveAttribute('aria-invalid', 'true')
    expect(email.closest('form')).toHaveAttribute('novalidate')
    // The refused submit never reached the API: only the status probe ran.
    expect(globalThis.fetch).toHaveBeenCalledTimes(1)
    // The first invalid control takes focus on the next frame.
    await waitFor(() => expect(email).toHaveFocus())
  })

  it('names the password rule under the field on a short register password', async () => {
    renderAuth()

    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'new@example.com' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'short' } })
    const submit = screen.getByRole('button', { name: 'Create your account' })
    await waitFor(() => expect(submit).toBeEnabled())
    fireEvent.click(submit)

    const password = screen.getByLabelText('Password')
    expect(password).toHaveAttribute('aria-invalid', 'true')
    expect(await screen.findByText('Use at least 12 characters.')).toBeInTheDocument()
    expect(screen.getByLabelText('Email')).not.toHaveAttribute('aria-invalid')
  })

  it('holds the sign-up submit until the instance probe settles (F20)', async () => {
    let answerStatus: (response: Response) => void = () => {}
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.endsWith('/api/v1/auth/status')) {
        return new Promise<Response>((resolve) => {
          answerStatus = resolve
        })
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })
    renderAuth()

    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))
    const submit = screen.getByRole('button', { name: 'Create your account' })
    // Hosted or not is still unknown, so the organization fields may be missing.
    expect(submit).toBeDisabled()
    // Signing in never depends on the probe.
    fireEvent.click(screen.getByRole('button', { name: 'Existing account' }))
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))

    answerStatus(jsonResponse({ has_users: true, registration_enabled: true }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Create your account' })).toBeEnabled(),
    )
  })

  it('shows the product mark, a plain name placeholder and a password reveal (SH-31)', () => {
    mockStatus(true)
    renderAuth()

    expect(screen.getByText('tripl')).toBeInTheDocument()

    const password = screen.getByLabelText('Password')
    expect(password).toHaveAttribute('type', 'password')
    fireEvent.click(screen.getByRole('button', { name: 'Show password' }))
    expect(password).toHaveAttribute('type', 'text')

    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))
    expect(screen.getByLabelText('Name')).toHaveAttribute('placeholder', 'Your name')
  })

  describe('hosted mode (F20)', () => {
    const HOSTED_STATUS = {
      has_users: true,
      registration_enabled: true,
      email_configured: true,
      deployment_mode: 'hosted',
      email_verification_required: true,
    }

    function mockHosted() {
      const bodies: unknown[] = []
      vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
        const url = urlOf(input)
        if (url.endsWith('/api/v1/auth/status')) return Promise.resolve(jsonResponse(HOSTED_STATUS))
        if (url.endsWith('/api/v1/auth/register')) {
          bodies.push(JSON.parse(String(init?.body ?? '{}')))
          return Promise.resolve(
            jsonResponse(
              {
                id: 'user-9',
                email: 'founder@example.com',
                name: null,
                role: 'owner',
                is_platform_admin: false,
                email_verified: false,
                orgs: [{ slug: 'acme-labs', name: 'Acme Labs', role: 'owner' }],
                created_at: '2026-09-28T00:00:00Z',
                updated_at: '2026-09-28T00:00:00Z',
              },
              201,
            ),
          )
        }
        return Promise.reject(new Error(`Unexpected request: ${url}`))
      })
      return bodies
    }

    it('asks for the organization and derives its slug from the name until edited', async () => {
      mockHosted()
      renderAuth()
      fireEvent.click(screen.getByRole('button', { name: 'Create account' }))

      const orgName = await screen.findByLabelText('Organization name')
      const orgSlug = screen.getByLabelText('Organization URL slug')
      fireEvent.change(orgName, { target: { value: 'Acme Labs' } })
      expect(orgSlug).toHaveValue('acme-labs')
      expect(screen.getByText('/o/acme-labs')).toBeInTheDocument()

      fireEvent.change(orgSlug, { target: { value: 'acme' } })
      fireEvent.change(orgName, { target: { value: 'Acme Laboratories' } })
      expect(orgSlug).toHaveValue('acme')
      expect(screen.getByText('/o/acme')).toBeInTheDocument()
      // No first-account note in hosted mode.
      expect(screen.queryByText(/The first account on a new instance/)).toBeNull()
    })

    it('refuses a malformed slug inline and sends nothing', async () => {
      const bodies = mockHosted()
      renderAuth()
      fireEvent.click(screen.getByRole('button', { name: 'Create account' }))

      fireEvent.change(await screen.findByLabelText('Organization name'), { target: { value: 'Acme' } })
      fireEvent.change(screen.getByLabelText('Organization URL slug'), { target: { value: 'Acme Labs' } })
      fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'founder@example.com' } })
      fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'a-long-enough-password' } })
      fireEvent.click(screen.getByRole('button', { name: 'Create your account' }))

      expect(screen.getByLabelText('Organization URL slug')).toHaveAttribute('aria-invalid', 'true')
      expect(bodies).toHaveLength(0)
    })

    it('sends the organization with the sign-up', async () => {
      const bodies = mockHosted()
      renderAuth()
      fireEvent.click(screen.getByRole('button', { name: 'Create account' }))

      fireEvent.change(await screen.findByLabelText('Organization name'), { target: { value: 'Acme Labs' } })
      fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'founder@example.com' } })
      fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'a-long-enough-password' } })
      fireEvent.click(screen.getByRole('button', { name: 'Create your account' }))

      await waitFor(() => expect(bodies).toHaveLength(1))
      expect(bodies[0]).toEqual({
        email: 'founder@example.com',
        password: 'a-long-enough-password',
        org_name: 'Acme Labs',
        org_slug: 'acme-labs',
      })
    })
  })

  it('keeps the self-hosted sign-up form free of organization fields', async () => {
    renderAuth()
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))
    expect(screen.queryByLabelText('Organization name')).toBeNull()
    expect(screen.queryByLabelText('Organization URL slug')).toBeNull()
  })
})
