import type { ReactNode } from 'react'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { AuthContext, type AuthContextValue } from './auth-context'
import { ErrorState } from './error-state'

function withAuth(ui: ReactNode, sessionExpired: boolean) {
  const auth: AuthContextValue = {
    user: null,
    status: 'authenticated',
    error: null,
    isLoggingOut: false,
    logout: async () => {},
    refresh: () => {},
    sessionExpired,
  }
  return <AuthContext.Provider value={auth}>{ui}</AuthContext.Provider>
}

describe('ErrorState', () => {
  it('titles itself with an h2 by default', () => {
    render(<ErrorState title="Couldn't load events" error={new Error('boom')} />)
    expect(screen.getByRole('heading', { level: 2, name: "Couldn't load events" })).toBeInTheDocument()
  })

  // inside a card that already has an h2, two sibling h2s flatten the
  // outline.
  it('takes a lower heading level when nested under a card title', () => {
    render(<ErrorState title="Couldn't load events" error={new Error('boom')} headingLevel={3} />)
    expect(screen.getByRole('heading', { level: 3, name: "Couldn't load events" })).toBeInTheDocument()
  })

  it('offers a retry button named by its label', () => {
    const onRetry = vi.fn()
    render(<ErrorState title="Failed" error={new Error('boom')} onRetry={onRetry} retryLabel="Retry" />)
    screen.getByRole('button', { name: 'Retry' }).click()
    expect(onRetry).toHaveBeenCalledTimes(1)
  })
})

describe('ErrorState during an expired session', () => {
  it('shows a quiet waiting note, not a red alert, for a 401 while the sign-in dialog is open', async () => {
    const { ApiError } = await import('@/api/client')
    render(
      withAuth(
        <ErrorState
          title="Couldn't load events"
          error={new ApiError('Authentication required', 401)}
          onRetry={vi.fn()}
        />,
        true,
      ),
    )
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByText('Authentication required')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
    expect(screen.getByRole('status')).toHaveTextContent('Waiting for you to sign in again')
  })

  it('keeps the error card for a 401 with no sign-in dialog to wait on', async () => {
    const { ApiError } = await import('@/api/client')
    render(
      withAuth(
        <ErrorState title="Couldn't load events" error={new ApiError('Authentication required', 401)} />,
        false,
      ),
    )
    expect(screen.getByRole('alert')).toHaveTextContent('Authentication required')
    expect(screen.queryByText(/Waiting for you to sign in again/)).toBeNull()
  })

  it('keeps the error card for a 403', async () => {
    const { ApiError } = await import('@/api/client')
    render(withAuth(<ErrorState title="Couldn't load events" error={new ApiError('Forbidden', 403)} />, true))
    expect(screen.getByRole('alert')).toHaveTextContent('Forbidden')
  })
})
