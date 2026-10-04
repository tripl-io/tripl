import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SecuritySection from './SecuritySection'

const mockAuth = vi.hoisted(() => ({ role: 'member', platformAdmin: false }))

vi.mock('@/components/auth-context', () => ({
  useAuth: () => ({
    user: {
      id: 'u1',
      email: 'ada@example.com',
      name: 'Ada Lovelace',
      role: mockAuth.role,
      is_platform_admin: mockAuth.platformAdmin,
      orgs: [],
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    status: 'authenticated',
    error: null,
    isLoggingOut: false,
    logout: vi.fn(),
    refresh: vi.fn(),
  }),
}))

// A factory, not a Response: a body can be read only once.
function jsonResponse(body: unknown, status = 200) {
  return () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    })
}

/** Routes the instance probe and the reset request apart. `emailConfigured`
 *  undefined leaves the flag out of the probe, as an older API would. */
function mockApi({
  emailConfigured,
  reset = jsonResponse({ message: 'ok', email_configured: true }),
}: {
  emailConfigured?: boolean
  reset?: () => Response
} = {}) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    if (String(input).endsWith('/auth/status')) {
      return jsonResponse({
        has_users: true,
        registration_enabled: false,
        ...(emailConfigured === undefined ? {} : { email_configured: emailConfigured }),
      })()
    }
    return reset()
  })
}

function renderSection() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SecuritySection />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  mockAuth.role = 'member'
  mockAuth.platformAdmin = false
})

describe('Account · Security', () => {
  /**
   * The old card held an "Update password" button that did nothing
   * and then told the reader to sign out and find "Forgot your password?".
   * The reset flow works, so the card runs it for the signed-in address.
   */
  it('emails a reset link to the signed-in address', async () => {
    const fetchSpy = mockApi({ emailConfigured: true })
    renderSection()

    expect(screen.queryByRole('button', { name: 'Update password' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Email me a reset link' }))

    expect(await screen.findByText(/check ada@example.com for a link/i)).toBeInTheDocument()
    const call = fetchSpy.mock.calls.find(([input]) => String(input).includes('/password-reset/'))
    const [url, init] = call!
    expect(String(url)).toMatch(/\/auth\/password-reset\/request$/)
    expect(JSON.parse(String(init?.body))).toEqual({ email: 'ada@example.com' })
  })

  it('says plainly when the instance cannot send the email', async () => {
    // The probe does not say (older API): the request's answer still does.
    mockApi({ reset: jsonResponse({ message: 'ok', email_configured: false }) })
    renderSection()

    fireEvent.click(screen.getByRole('button', { name: 'Email me a reset link' }))

    expect(await screen.findByText(/can't send email, so no link went out/i)).toBeInTheDocument()
    // Not "ask a workspace owner… or to reset the password for you": there is
    // no such action, and the reader may be the owner.
    expect(screen.getByText(/Ask a platform admin to set it up/)).toBeInTheDocument()
    expect(screen.queryByText(/reset the password for you/i)).toBeNull()
  })

  it('tells a platform admin up front that email is off, with the way to set it up', async () => {
    // SMTP is operator-only: one relay carries every organization's mail.
    mockAuth.platformAdmin = true
    mockApi({ emailConfigured: false })
    renderSection()

    expect(await screen.findByText("This instance can't send email yet.")).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Set up email' })).toHaveAttribute(
      'href',
      '/settings/instance/email',
    )
    expect(screen.getByRole('button', { name: 'Email me a reset link' })).toBeDisabled()
  })

  it('tells anyone else up front too, and to ask a platform admin', async () => {
    mockApi({ emailConfigured: false })
    renderSection()

    expect(await screen.findByText("This instance can't send email yet.")).toBeInTheDocument()
    expect(screen.getByText('Ask a platform admin to set it up.')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Set up email' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Email me a reset link' })).toBeDisabled()
  })

  it('shows a failed request in place', async () => {
    mockApi({ reset: jsonResponse({ detail: 'Too many requests' }, 429) })
    renderSection()

    fireEvent.click(screen.getByRole('button', { name: 'Email me a reset link' }))

    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(/Could not request a reset link/),
    )
  })

  /**
   * Two-factor and sessions were cards of switches nobody could move, one of
   * them "Strongly recommended for owners", and a fabricated
   * device row. One card now names them, with nothing to click.
   */
  it('collapses the unbuilt protections into one card with no controls', () => {
    mockApi({ emailConfigured: true })
    renderSection()

    expect(screen.getByText('Coming later')).toBeInTheDocument()
    expect(screen.getByText('Two-factor authentication')).toBeInTheDocument()
    expect(screen.queryAllByRole('switch')).toHaveLength(0)
    expect(screen.getAllByRole('button')).toHaveLength(1)
    expect(screen.queryByText(/recommended/i)).toBeNull()
    expect(screen.queryByText(/Current location/i)).toBeNull()
  })
})
