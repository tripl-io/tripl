import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import type { AuthUser } from '@/types'
import { SessionExpiredDialog } from './session-expired-dialog'

function renderDialog() {
  const user = { id: 'u1', email: 'ada@example.com', name: 'Ada' } as AuthUser
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <SessionExpiredDialog user={user} onSignedIn={vi.fn()} onSignOut={vi.fn()} />
    </QueryClientProvider>,
  )
}

describe('SessionExpiredDialog', () => {
  it('offers password recovery in a new tab, keeping the draft on this page', () => {
    renderDialog()
    const link = screen.getByRole('link', { name: 'Forgot password?' })
    expect(link).toHaveAttribute('href', '/auth?mode=forgot')
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('keeps Sign in as the primary action while it waits for a password', () => {
    renderDialog()
    const signIn = screen.getByRole('button', { name: 'Sign in' })
    expect(signIn).toBeDisabled()
    expect(signIn.className).toContain('disabled:bg-accent-solid')
  })
})
