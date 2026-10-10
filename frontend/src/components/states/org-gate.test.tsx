import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { authAs } from '@/test/auth'
import { OrgGateScreen, type OrgGateScreenProps } from './org-gate'

/** The shared frame of a full-screen organization gate (suspended; SSO in Enterprise). */

function renderGate(props: Partial<OrgGateScreenProps> = {}, auth: AuthContextValue | null = authAs('member')) {
  const gate = (
    <MemoryRouter>
      <OrgGateScreen icon={<svg data-testid="gate-icon" />} title="Gate title" otherOrgs={[]} {...props}>
        Why the organization is closed.
      </OrgGateScreen>
    </MemoryRouter>
  )
  render(auth ? <AuthContext.Provider value={auth}>{gate}</AuthContext.Provider> : gate)
}

describe('OrgGateScreen', () => {
  it('is the page’s main landmark, with the heading, the reason and the way out', () => {
    renderGate()
    const main = screen.getByRole('main')
    expect(within(main).getByRole('heading', { level: 1, name: 'Gate title' })).toBeInTheDocument()
    expect(within(main).getByText('Why the organization is closed.')).toBeInTheDocument()
    expect(within(main).getByTestId('gate-icon')).toBeInTheDocument()
    expect(within(main).getByRole('button', { name: 'Sign out' })).toBeInTheDocument()
    // No other organizations: no empty list.
    expect(screen.queryByRole('navigation', { name: 'Your other organizations' })).toBeNull()
  })

  it('puts the way in above the other organizations, and footer actions beside Sign out', () => {
    renderGate({
      primaryAction: <button type="button">Way in</button>,
      footerActions: <button type="button">Console</button>,
      otherOrgs: [{ slug: 'globex', name: 'Globex', role: 'member' }],
    })
    const wayIn = screen.getByRole('button', { name: 'Way in' })
    const nav = screen.getByRole('navigation', { name: 'Your other organizations' })
    const consoleButton = screen.getByRole('button', { name: 'Console' })
    const signOut = screen.getByRole('button', { name: 'Sign out' })

    expect(within(nav).getByRole('link', { name: 'Globex' })).toHaveAttribute('href', '/o/globex')
    expect(wayIn.compareDocumentPosition(nav) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(nav.compareDocumentPosition(consoleButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(consoleButton.parentElement).toBe(signOut.parentElement)
  })

  it('offers no Sign out without a session to end', () => {
    renderGate({}, null)
    expect(screen.queryByRole('button', { name: 'Sign out' })).toBeNull()
  })
})
