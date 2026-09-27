import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import UsersPage from './UsersPage'

/**
 * Role changes and invite links on Members (#207): owner grants and demotions
 * confirm first, a failure lands on the row it belongs to, and the show-once
 * invite link can be dismissed without being silently replaced.
 */

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

const USERS = [
  { id: 'owner-1', email: 'owner@example.com', name: 'Owner', role: 'owner', created_at: '2026-01-01T00:00:00Z' },
  { id: 'ed-1', email: 'ed@example.com', name: 'Ed', role: 'admin', created_at: '2026-01-02T00:00:00Z' },
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

type Call = { method: string; url: string; body?: string }

function mockApi(options: { patchStatus?: number } = {}) {
  const calls: Call[] = []
  let minted = 0
  vi.spyOn(globalThis, 'fetch').mockImplementation(
    (input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      const body = init?.body ? String(init.body) : undefined
      calls.push({ method, url, body })
      if (method === 'PATCH') {
        return Promise.resolve(
          options.patchStatus
            ? jsonResponse({ detail: 'Role change refused' }, options.patchStatus)
            : jsonResponse({ ...USERS[1], ...(JSON.parse(body ?? '{}') as object) }),
        )
      }
      if (method === 'POST' && url.endsWith('/api/v1/users/invitations')) {
        minted += 1
        const { email, role } = JSON.parse(body ?? '{}') as { email: string; role: string }
        return Promise.resolve(
          jsonResponse({
            invitation: {
              id: `inv-${minted}`,
              email,
              role,
              expires_at: '2026-09-01T00:00:00Z',
              is_expired: false,
            },
            accept_path: `/invite/tok-${minted}`,
            expires_at: '2026-09-01T00:00:00Z',
          }),
        )
      }
      if (url.endsWith('/api/v1/users/invitations')) return Promise.resolve(jsonResponse([]))
      if (url.endsWith('/api/v1/users')) return Promise.resolve(jsonResponse(USERS))
      return Promise.reject(new Error(`Unexpected request: ${method} ${url}`))
    },
  )
  return calls
}

function renderUsersPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={OWNER}>
        <MemoryRouter>
          <UsersPage />
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

const patches = (calls: Call[]) => calls.filter((call) => call.method === 'PATCH')

afterEach(() => {
  vi.restoreAllMocks()
})

describe('UsersPage — role changes (WS-19)', () => {
  it('asks before granting Owner, and does nothing when cancelled', async () => {
    const calls = mockApi()
    renderUsersPage()

    fireEvent.change(await screen.findByLabelText('Role for Ed'), { target: { value: 'owner' } })

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Make Ed an owner?')
    expect(dialog).toHaveTextContent(/data sources and secrets/)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(patches(calls)).toHaveLength(0)

    fireEvent.change(screen.getByLabelText('Role for Ed'), { target: { value: 'owner' } })
    fireEvent.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Make owner' }),
    )
    await waitFor(() => expect(patches(calls)).toHaveLength(1))
    expect(patches(calls)[0]?.url).toMatch(/\/users\/ed-1$/)
    expect(JSON.parse(patches(calls)[0]?.body ?? '{}')).toEqual({ role: 'owner' })
  })

  it('asks before a demotion', async () => {
    const calls = mockApi()
    renderUsersPage()

    fireEvent.change(await screen.findByLabelText('Role for Ed'), { target: { value: 'member' } })

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Change Ed to Member?')
    expect(patches(calls)).toHaveLength(0)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Change to Member' }))
    await waitFor(() => expect(patches(calls)).toHaveLength(1))
  })

  it('applies a promotion short of Owner directly', async () => {
    const calls = mockApi()
    renderUsersPage()

    fireEvent.change(await screen.findByLabelText('Role for Vi'), { target: { value: 'admin' } })

    await waitFor(() => expect(patches(calls)).toHaveLength(1))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  // ST-3: an instant-apply control says it applied, on the row.
  it('confirms an applied role change on the row', async () => {
    mockApi()
    renderUsersPage()

    fireEvent.change(await screen.findByLabelText('Role for Vi'), { target: { value: 'admin' } })

    expect(await screen.findByText('Role updated')).toHaveAttribute('role', 'status')
  })

  it('keeps the row status region mounted, empty, before any change', async () => {
    mockApi()
    renderUsersPage()

    await screen.findByLabelText('Role for Vi')
    const statuses = screen.getAllByRole('status')
    expect(statuses.length).toBeGreaterThan(0)
    expect(screen.queryByText('Role updated')).not.toBeInTheDocument()
  })

  it('shows a failed change on the row of the person it was for', async () => {
    mockApi({ patchStatus: 403 })
    renderUsersPage()

    fireEvent.change(await screen.findByLabelText('Role for Vi'), { target: { value: 'admin' } })

    expect(await screen.findByRole('alert')).toHaveTextContent(/Could not change the role of Vi/)
  })
})

describe('UsersPage — invite links (WS-21, WS-22)', () => {
  async function mint(email: string) {
    fireEvent.change(await screen.findByLabelText('Email'), { target: { value: email } })
    fireEvent.click(screen.getByRole('button', { name: 'Create invite link' }))
  }

  // AU-4: the email rule is said inline, not by the browser's bubble.
  it('says an address is malformed inline and sends nothing', async () => {
    const calls = mockApi()
    renderUsersPage()

    await mint('not-an-address')

    const email = screen.getByLabelText('Email')
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter an email address')
    expect(email).toHaveAttribute('aria-invalid', 'true')
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })

  it('names the role on the minted link and lets it be dismissed', async () => {
    mockApi()
    renderUsersPage()

    await mint('newcomer@example.com')

    expect(
      await screen.findByText(/Member invite link for newcomer@example.com/),
    ).toBeInTheDocument()
    // Nobody copied it, so dismissing asks first (the show-once link is lost).
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Discard link' }))
    await waitFor(() =>
      expect(screen.queryByRole('textbox', { name: 'Invite link' })).not.toBeInTheDocument(),
    )
  })

  it('warns before dismissing a link nobody copied', async () => {
    mockApi()
    renderUsersPage()

    await mint('first@example.com')
    const link = await screen.findByRole('textbox', { name: 'Invite link' })
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/first@example.com has not been copied/)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())

    // The link is still on the page.
    expect(link).toBeInTheDocument()
    expect(link).toHaveValue(`${window.location.origin}/invite/tok-1`)
  })

  it('counts a manual Ctrl/Cmd+C of the link as copied', async () => {
    const calls = mockApi()
    renderUsersPage()

    await mint('first@example.com')
    const link = await screen.findByRole('textbox', { name: 'Invite link' })
    // The no-clipboard path: the user copies the selected link by hand.
    fireEvent.copy(link)

    await mint('second@example.com')
    await waitFor(() => expect(calls.filter((call) => call.method === 'POST')).toHaveLength(2))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Invite link' })).toHaveValue(
        `${window.location.origin}/invite/tok-2`,
      ),
    )

    fireEvent.copy(screen.getByRole('textbox', { name: 'Invite link' }))
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Invite link' })).not.toBeInTheDocument()
  })

  it('warns before replacing a link nobody copied', async () => {
    const calls = mockApi()
    renderUsersPage()

    await mint('first@example.com')
    await screen.findByRole('textbox', { name: 'Invite link' })
    await mint('second@example.com')

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/first@example.com has not been copied/)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())

    // The first link is still there, and no second invite was minted.
    expect(screen.getByRole('textbox', { name: 'Invite link' })).toHaveValue(
      `${window.location.origin}/invite/tok-1`,
    )
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(1)
  })

  it('announces a copy and resets the button afterwards', async () => {
    const clipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      configurable: true,
    })
    mockApi()
    try {
      renderUsersPage()
      await mint('newcomer@example.com')
      fireEvent.click(await screen.findByRole('button', { name: 'Copy' }))

      expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument()
      expect(screen.getByText('Invite link copied to the clipboard.')).toHaveAttribute('role', 'status')
      await waitFor(
        () => expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument(),
        { timeout: 3000 },
      )
    } finally {
      if (clipboard) Object.defineProperty(navigator, 'clipboard', clipboard)
      else Reflect.deleteProperty(navigator, 'clipboard')
    }
  })

  it('says what Owner grants, and confirms an owner invite', async () => {
    const calls = mockApi()
    renderUsersPage()

    const role = await screen.findByLabelText('Role')
    fireEvent.change(role, { target: { value: 'owner' } })
    expect(role).toHaveAccessibleDescription(/administer the whole organization/)

    await mint('boss@example.com')
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Invite as Owner?')
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create owner invite' }))
    await waitFor(() => expect(calls.filter((call) => call.method === 'POST')).toHaveLength(1))
  })
})
