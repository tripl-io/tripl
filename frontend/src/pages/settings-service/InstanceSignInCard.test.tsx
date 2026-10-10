import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { authApi, type AuthStatusResponse } from '@/api/auth'
import { InstanceSignInCard } from './InstanceSignInCard'

function renderCard(status: Partial<AuthStatusResponse> | Error) {
  const spy = vi.spyOn(authApi, 'status')
  if (status instanceof Error) spy.mockRejectedValue(status)
  else spy.mockResolvedValue({ has_users: true, registration_enabled: true, ...status })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <InstanceSignInCard />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('InstanceSignInCard', () => {
  it('shows the OpenID Connect button the sign-in page offers', async () => {
    renderCard({ oidc_sign_in: true, oidc_button_label: 'Sign in with Okta', google_sign_in: false })

    expect(await screen.findByText('On: the sign-in page offers “Sign in with Okta”')).toBeInTheDocument()
    expect(screen.getByText('Not configured')).toBeInTheDocument()
  })

  it('says neither provider is configured, and where the setup is', async () => {
    renderCard({ oidc_sign_in: false, google_sign_in: false })

    expect(await screen.findAllByText('Not configured')).toHaveLength(2)
    const link = screen.getByRole('link', { name: /How to set it up/ })
    expect(link).toHaveAttribute('href', 'https://docs.tripl.io/administer/admin-guide#instance-sign-in')
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('reports Google sign-in', async () => {
    renderCard({ google_sign_in: true })

    expect(await screen.findByText('On: the sign-in page offers “Continue with Google”')).toBeInTheDocument()
  })

  it('keeps the setup link when the status cannot be read', async () => {
    renderCard(new Error('offline'))

    expect(await screen.findByText('Unknown (the status could not be read)')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /How to set it up/ })).toBeInTheDocument()
  })
})
