import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AuthContext } from '@/components/auth-context'
import { authAs } from '@/test/auth'
import { SsoRequiredState } from './sso-required'

const { navigateToSso } = vi.hoisted(() => ({ navigateToSso: vi.fn() }))
vi.mock('@/lib/ssoNavigation', () => ({ navigateToSso }))

/** An organization that requires single sign-on (F20): the full-page prompt. */

afterEach(() => {
  vi.restoreAllMocks()
  navigateToSso.mockReset()
})

function renderState(props: Partial<Parameters<typeof SsoRequiredState>[0]> = {}) {
  render(
    <AuthContext.Provider value={authAs('member')}>
      <MemoryRouter>
        <SsoRequiredState
          orgName="Acme"
          orgSlug="acme"
          serverStart="/api/v1/auth/sso/acme/start"
          returnTo="/o/acme/p/web/events"
          otherOrgs={[{ slug: 'globex', name: 'Globex', role: 'member' }]}
          {...props}
        />
      </MemoryRouter>
    </AuthContext.Provider>,
  )
}

describe('SsoRequiredState', () => {
  it('sends the user to the organization sign-in, coming back to the same page', () => {
    const go = navigateToSso
    renderState()

    expect(
      screen.getByRole('heading', { level: 1, name: 'This organization requires single sign-on' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Globex' })).toHaveAttribute('href', '/o/globex')
    fireEvent.click(screen.getByRole('button', { name: 'Sign in with SSO' }))
    expect(go).toHaveBeenCalledWith(
      '/api/v1/auth/sso/acme/start?next=%2Fo%2Facme%2Fp%2Fweb%2Fevents',
    )
  })

  it("falls back to the refusal's own start path without a slug", () => {
    const go = navigateToSso
    renderState({ orgSlug: null })

    fireEvent.click(screen.getByRole('button', { name: 'Sign in with SSO' }))
    expect(go).toHaveBeenCalledWith('/api/v1/auth/sso/acme/start')
  })
})
