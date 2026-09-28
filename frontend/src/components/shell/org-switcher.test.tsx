import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it } from 'vitest'
import { ActiveOrgProvider } from '@/components/active-org-provider'
import { AuthContext, useAuth } from '@/components/auth-context'
import { currentOrgSlug } from '@/lib/activeOrg'
import { authAs } from '@/test/auth'
import type { OrgMembership } from '@/types'
import { OrgSwitcher } from './org-switcher'
import { shouldShowOrgSwitcher } from './org-switcher-model'

const DEFAULT: OrgMembership = { slug: 'default', name: 'Default', role: 'owner' }
const ACME: OrgMembership = { slug: 'acme', name: 'Acme', role: 'member' }

function Where() {
  const location = useLocation()
  const auth = useAuth()
  return (
    <>
      <div data-testid="where">{location.pathname}</div>
      <div data-testid="role">{auth.user?.role ?? 'none'}</div>
    </>
  )
}

function renderSwitcher(orgs: OrgMembership[], entry = '/o/acme', platformAdmin = false) {
  const auth = authAs('owner')
  const value = auth.user
    ? { ...auth, user: { ...auth.user, orgs, is_platform_admin: platformAdmin } }
    : auth
  return render(
    <AuthContext.Provider value={value}>
      <MemoryRouter initialEntries={[entry]}>
        <ActiveOrgProvider>
          <OrgSwitcher />
          <Routes>
            <Route path="*" element={<Where />} />
          </Routes>
        </ActiveOrgProvider>
      </MemoryRouter>
    </AuthContext.Provider>,
  )
}

afterEach(() => {
  localStorage.removeItem('tripl-last-org')
  sessionStorage.removeItem('tripl-last-org')
})

describe('OrgSwitcher', () => {
  it('is drawn only for someone in more than one organization', () => {
    expect(shouldShowOrgSwitcher(0)).toBe(false)
    expect(shouldShowOrgSwitcher(1)).toBe(false)
    expect(shouldShowOrgSwitcher(2)).toBe(true)
  })

  it('renders nothing for a user in one organization', () => {
    renderSwitcher([ACME])
    expect(screen.queryByRole('button', { name: /Switch organization/ })).toBeNull()
  })

  it('names the organization in the URL and switches to another one’s workspace', async () => {
    renderSwitcher([DEFAULT, ACME], '/o/acme/p/web/events')

    const trigger = screen.getByRole('button', { name: 'Switch organization (current: Acme)' })
    fireEvent.keyDown(trigger, { key: 'Enter' })
    fireEvent.click(await screen.findByText('Default'))

    expect(screen.getByTestId('where')).toHaveTextContent(/^\/o\/default$/)
    expect(currentOrgSlug()).toBe('default')
  })

  it('offers "Create organization" to a platform admin only', async () => {
    renderSwitcher([DEFAULT, ACME], '/o/acme', true)
    fireEvent.keyDown(screen.getByRole('button', { name: /Switch organization/ }), { key: 'Enter' })
    expect(await screen.findByText('Create organization')).toBeInTheDocument()
    expect(screen.getByText('Organization settings')).toBeInTheDocument()
  })

  it('announces the current organization and binds its settings link to it', async () => {
    renderSwitcher([DEFAULT, ACME], '/o/acme/p/web')
    fireEvent.keyDown(screen.getByRole('button', { name: /Switch organization/ }), { key: 'Enter' })

    const current = await screen.findByRole('menuitem', { name: /^Acme,? ?current/ })
    expect(current).toHaveAttribute('aria-current', 'true')
    expect(screen.getByRole('menuitem', { name: /^Default/ })).not.toHaveAttribute('aria-current')
    expect(screen.getByRole('menuitem', { name: 'Organization settings' })).toHaveAttribute(
      'href',
      '/settings/organization/general?org=acme',
    )
  })

  it('hides "Create organization" from everyone else', async () => {
    renderSwitcher([DEFAULT, ACME], '/o/acme')
    fireEvent.keyDown(screen.getByRole('button', { name: /Switch organization/ }), { key: 'Enter' })
    expect(await screen.findByText('Organization settings')).toBeInTheDocument()
    expect(screen.queryByText('Create organization')).toBeNull()
  })
})

describe('ActiveOrgProvider', () => {
  it("hands the tree the role the user holds in the URL's organization", () => {
    renderSwitcher([DEFAULT, ACME], '/o/acme/p/web')
    // Owner of `default`, a member of `acme`: the page in acme reads member.
    expect(screen.getByTestId('role')).toHaveTextContent('member')
  })

  it('falls back to the last organization used, then the first', () => {
    localStorage.setItem('tripl-last-org', 'acme')
    const { unmount } = renderSwitcher([DEFAULT, ACME], '/settings/members')
    expect(currentOrgSlug()).toBe('acme')
    unmount()

    localStorage.removeItem('tripl-last-org')
    sessionStorage.removeItem('tripl-last-org')
    renderSwitcher([DEFAULT, ACME], '/settings/members')
    expect(currentOrgSlug()).toBe('default')
    expect(screen.getByTestId('role')).toHaveTextContent('owner')
  })

  it('remembers an organization opened by address', () => {
    renderSwitcher([DEFAULT, ACME], '/o/acme')
    expect(localStorage.getItem('tripl-last-org')).toBe('acme')
  })

  it("acts in the settings address's ?org= over another tab's last organization", () => {
    sessionStorage.setItem('tripl-last-org', 'default')
    localStorage.setItem('tripl-last-org', 'default')
    renderSwitcher([DEFAULT, ACME], '/settings/members?org=acme')
    expect(currentOrgSlug()).toBe('acme')
    expect(screen.getByTestId('role')).toHaveTextContent('member')
    expect(sessionStorage.getItem('tripl-last-org')).toBe('acme')
  })

  it("keeps this tab's organization when another tab opened a different one", () => {
    sessionStorage.setItem('tripl-last-org', 'default')
    // Written by a tab now on /o/acme.
    localStorage.setItem('tripl-last-org', 'acme')
    renderSwitcher([DEFAULT, ACME], '/settings/project/general?project=web')
    expect(currentOrgSlug()).toBe('default')
  })

  it('gives no role in an organization the user is not in', () => {
    renderSwitcher([DEFAULT, ACME], '/o/elsewhere')
    expect(screen.getByTestId('role')).toHaveTextContent('none')
  })
})
