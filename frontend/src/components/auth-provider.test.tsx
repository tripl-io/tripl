import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, AUTH_SIGNED_OUT_EVENT, AUTH_UNAUTHORIZED_EVENT } from '@/api/client'
import type { AuthUser } from '@/types'
import { AuthProvider } from './auth-provider'
import { useAuth } from './auth-context'

// Mock only the auth API (the thin wrapper over the client); the provider's
// reaction to the real AUTH_UNAUTHORIZED_EVENT is what we exercise here.
vi.mock('@/api/auth', () => ({
  authApi: {
    me: vi.fn(),
    login: vi.fn(),
    logout: vi.fn(),
  },
}))

import { authApi } from '@/api/auth'

const meMock = vi.mocked(authApi.me)
const logoutMock = vi.mocked(authApi.logout)
const loginMock = vi.mocked(authApi.login)

function makeUser(): AuthUser {
  return { id: 'u-1', email: 'a@b.com', name: 'Ada' } as AuthUser
}

function Probe() {
  const { status, user, logout } = useAuth()
  return (
    <div>
      <span data-testid="status">{status}</span>
      <span data-testid="email">{user?.email ?? 'none'}</span>
      <button type="button" onClick={() => void logout()}>
        Log out
      </button>
    </div>
  )
}

function renderProvider() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <Probe />
      </AuthProvider>
    </QueryClientProvider>,
  )
  return queryClient
}

beforeEach(() => {
  meMock.mockReset()
  logoutMock.mockReset()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('AuthProvider session status', () => {
  it('exposes authenticated status when /auth/me returns a user', async () => {
    meMock.mockResolvedValue(makeUser())
    renderProvider()
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))
    expect(screen.getByTestId('email').textContent).toBe('a@b.com')
  })

  it('treats a 401 from /auth/me as anonymous (not error)', async () => {
    meMock.mockRejectedValue(new ApiError('Unauthorized', 401))
    renderProvider()
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('anonymous'))
  })

  it('treats a non-401 failure as an error status', async () => {
    meMock.mockRejectedValue(new ApiError('Boom', 500))
    renderProvider()
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('error'))
  })
})

describe('AuthProvider /auth/me refetch failures', () => {
  it('keeps a signed-in user signed in when a refetch fails with a non-401', async () => {
    meMock.mockResolvedValueOnce(makeUser()).mockRejectedValue(new ApiError('Bad gateway', 502))
    const queryClient = renderProvider()
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))

    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ['auth', 'me'] })
    })

    await waitFor(() => expect(meMock).toHaveBeenCalledTimes(2))
    // A network blip says nothing about the session: no error screen, no
    // sign-in dialog, the same user.
    expect(screen.getByTestId('status').textContent).toBe('authenticated')
    expect(screen.getByTestId('email').textContent).toBe('a@b.com')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})

describe('AuthProvider unauthorized event cycle', () => {
  it('keeps the page mounted under a sign-in dialog when the session expires', async () => {
    meMock.mockResolvedValue(makeUser())
    loginMock.mockResolvedValue(makeUser())
    renderProvider()
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))

    // Simulate a 401 on a non-/auth path emitting the global event.
    act(() => {
      window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT))
    })

    const dialog = await screen.findByRole('dialog', { name: 'Your session has expired' })
    // Still signed in as far as the routes are concerned: nothing unmounts.
    expect(screen.getByTestId('status').textContent).toBe('authenticated')
    expect(screen.getByTestId('email').textContent).toBe('a@b.com')

    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'secret' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    await waitFor(() => expect(dialog).not.toBeInTheDocument())
    expect(loginMock).toHaveBeenCalledWith({ email: 'a@b.com', password: 'secret' })
    expect(screen.getByTestId('status').textContent).toBe('authenticated')
  })

  it('signs out from the expired-session dialog', async () => {
    meMock.mockResolvedValueOnce(makeUser()).mockRejectedValue(new ApiError('Unauthorized', 401))
    renderProvider()
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))

    act(() => {
      window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT))
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }))

    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('anonymous'))
    expect(screen.getByTestId('email').textContent).toBe('none')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('clears the session when a 401 arrives with no signed-in user', async () => {
    meMock.mockRejectedValue(new ApiError('Unauthorized', 401))
    renderProvider()
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('anonymous'))

    act(() => {
      window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT))
    })

    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('anonymous'))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('removes the event listener on unmount (no leak)', () => {
    meMock.mockRejectedValue(new ApiError('Unauthorized', 401))
    const removeSpy = vi.spyOn(window, 'removeEventListener')
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { unmount } = render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <Probe />
        </AuthProvider>
      </QueryClientProvider>,
    )
    unmount()
    expect(removeSpy).toHaveBeenCalledWith(AUTH_UNAUTHORIZED_EVENT, expect.any(Function))
    removeSpy.mockRestore()
  })

  it('treats a 401 on the /auth/me refetch of a known user as an expiry, not a sign-out', async () => {
    // /auth paths never raise the unauthorized event (a reconnect after sleep
    // refetches /auth/me first), so the query's own 401 must hold the page.
    meMock.mockResolvedValueOnce(makeUser()).mockRejectedValue(new ApiError('Unauthorized', 401))
    loginMock.mockResolvedValue(makeUser())
    const queryClient = renderProvider()
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))

    await act(async () => {
      await queryClient.refetchQueries({ queryKey: ['auth', 'me'] })
    })

    const dialog = await screen.findByRole('dialog', { name: 'Your session has expired' })
    expect(screen.getByTestId('status').textContent).toBe('authenticated')
    expect(screen.getByTestId('email').textContent).toBe('a@b.com')

    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'secret' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(dialog).not.toBeInTheDocument())
    expect(screen.getByTestId('status').textContent).toBe('authenticated')
  })

  it('ignores a 401 that races a sign-out, leaving no dialog behind', async () => {
    meMock.mockResolvedValueOnce(makeUser()).mockRejectedValue(new ApiError('Unauthorized', 401))
    let finishLogout: () => void = () => {}
    logoutMock.mockReturnValue(new Promise<void>((resolve) => { finishLogout = resolve }))
    renderProvider()
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))

    fireEvent.click(screen.getByRole('button', { name: 'Log out' }))
    await waitFor(() => expect(logoutMock).toHaveBeenCalled())
    // A request still in flight answers 401 once the cookie is gone.
    act(() => {
      window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT))
    })
    await act(async () => finishLogout())

    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('anonymous'))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})

describe('AuthProvider sign-out', () => {
  // Detached collect watches are module state; the provider's job is only to
  // announce the sign-out that ends them (the watcher listens for it).
  it('announces the sign-out that ends every metric collection watch', async () => {
    const signedOut = vi.fn()
    window.addEventListener(AUTH_SIGNED_OUT_EVENT, signedOut)
    meMock.mockResolvedValueOnce(makeUser()).mockRejectedValue(new ApiError('Unauthorized', 401))
    logoutMock.mockResolvedValue(undefined)
    renderProvider()
    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))
    expect(signedOut).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Log out' }))

    await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('anonymous'))
    expect(signedOut).toHaveBeenCalledTimes(1)
    window.removeEventListener(AUTH_SIGNED_OUT_EVENT, signedOut)
  })
})
