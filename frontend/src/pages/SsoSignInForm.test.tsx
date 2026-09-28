import { useState } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ssoApi } from '@/api/sso'
import { SsoSignInForm } from './SsoSignInForm'

const { navigateToSso } = vi.hoisted(() => ({ navigateToSso: vi.fn() }))
vi.mock('@/lib/ssoNavigation', () => ({ navigateToSso }))

/** "Sign in with SSO" on the sign-in card (F20): email -> discover -> start. */

afterEach(() => {
  vi.restoreAllMocks()
  navigateToSso.mockReset()
})

function Harness({ onBack = () => {} }: { onBack?: () => void }) {
  const [email, setEmail] = useState('')
  return <SsoSignInForm email={email} onEmailChange={setEmail} next="/o/acme/p/web" onBack={onBack} />
}

function renderForm(onBack?: () => void) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <Harness onBack={onBack} />
    </QueryClientProvider>,
  )
}

function submit(email: string) {
  fireEvent.change(screen.getByLabelText('Work email'), { target: { value: email } })
  fireEvent.click(screen.getByRole('button', { name: /Continue/ }))
}

describe('SsoSignInForm', () => {
  it('goes straight to the one organization that signs the address in', async () => {
    const discover = vi
      .spyOn(ssoApi, 'discover')
      .mockResolvedValue({ orgs: [{ slug: 'acme', name: 'Acme' }] })
    const go = navigateToSso
    renderForm()

    submit('jane@example.com')

    await waitFor(() =>
      expect(go).toHaveBeenCalledWith('/api/v1/auth/sso/acme/start?next=%2Fo%2Facme%2Fp%2Fweb'),
    )
    expect(discover).toHaveBeenCalledWith('jane@example.com')
  })

  it('asks which organization when several sign the address in', async () => {
    vi.spyOn(ssoApi, 'discover').mockResolvedValue({
      orgs: [
        { slug: 'acme', name: 'Acme' },
        { slug: 'acme-labs', name: 'Acme Labs' },
      ],
    })
    const go = navigateToSso
    renderForm()

    submit('jane@example.com')

    fireEvent.click(await screen.findByRole('button', { name: /Acme Labs/ }))
    expect(go).toHaveBeenCalledWith('/api/v1/auth/sso/acme-labs/start?next=%2Fo%2Facme%2Fp%2Fweb')
  })

  it('says so when no organization signs the address in', async () => {
    vi.spyOn(ssoApi, 'discover').mockResolvedValue({ orgs: [] })
    const go = navigateToSso
    renderForm()

    submit('jane@example.com')

    expect(await screen.findByText(/No organization signs this address in/)).toBeInTheDocument()
    expect(go).not.toHaveBeenCalled()
  })

  it('checks the address before asking the server', () => {
    const discover = vi.spyOn(ssoApi, 'discover')
    renderForm()

    submit('not-an-address')

    expect(screen.getByText(/Enter an email address/)).toBeInTheDocument()
    expect(discover).not.toHaveBeenCalled()
  })

  it('leads back to the password form', () => {
    const onBack = vi.fn()
    renderForm(onBack)
    fireEvent.click(screen.getByRole('button', { name: 'Sign in with a password instead' }))
    expect(onBack).toHaveBeenCalled()
  })
})
