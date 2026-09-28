import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import {
  ORG_SUSPENDED_DETAIL,
  STEP_IN_READ_ONLY_DETAIL,
  activeStepInFor,
  isOrgSuspendedError,
  orgIsSuspended,
} from '@/lib/orgStatus'
import { authAs } from '@/test/auth'
import { OrgSuspendedState } from './org-suspended'

/** A suspended organization (F20): the full-page state, and how it is detected. */

function renderState(value: AuthContextValue = authAs('member')) {
  render(
    <AuthContext.Provider value={value}>
      <MemoryRouter>
        <OrgSuspendedState
          orgName="Acme"
          otherOrgs={[{ slug: 'globex', name: 'Globex', role: 'member' }]}
        />
      </MemoryRouter>
    </AuthContext.Provider>,
  )
}

describe('OrgSuspendedState', () => {
  it('says the organization is suspended and offers the others and a way out', () => {
    const logout = vi.fn(async () => {})
    renderState({ ...authAs('member'), logout })
    expect(screen.getByRole('heading', { level: 1, name: 'This organization is suspended' })).toBeInTheDocument()
    expect(screen.getByText('Acme')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Globex' })).toHaveAttribute('href', '/o/globex')
    expect(screen.queryByRole('link', { name: 'Open the platform console' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    expect(logout).toHaveBeenCalled()
  })

  it('offers a platform admin the console', () => {
    const value = authAs('member')
    renderState({ ...value, user: value.user && { ...value.user, is_platform_admin: true } })
    expect(screen.getByRole('link', { name: 'Open the platform console' })).toHaveAttribute(
      'href',
      '/settings/platform/orgs',
    )
  })
})

describe('orgStatus', () => {
  it('recognises the suspension refusal by its status and words', () => {
    expect(isOrgSuspendedError(new ApiError(ORG_SUSPENDED_DETAIL, 403))).toBe(true)
    expect(isOrgSuspendedError(new ApiError(STEP_IN_READ_ONLY_DETAIL, 403))).toBe(false)
    expect(isOrgSuspendedError(new ApiError(ORG_SUSPENDED_DETAIL, 404))).toBe(false)
    expect(isOrgSuspendedError(new Error(ORG_SUSPENDED_DETAIL))).toBe(false)
  })

  it('reads suspension from the membership or from a refusal', () => {
    expect(orgIsSuspended({ status: 'suspended' })).toBe(true)
    expect(orgIsSuspended({ status: 'active' })).toBe(false)
    expect(orgIsSuspended(null, null, new ApiError(ORG_SUSPENDED_DETAIL, 403))).toBe(true)
    expect(orgIsSuspended(undefined, new ApiError('Forbidden', 403))).toBe(false)
  })

  it('finds the unexpired step-in to one organization', () => {
    const now = Date.parse('2026-09-28T12:00:00Z')
    const user = {
      active_step_ins: [
        { org_slug: 'acme', expires_at: '2026-09-28T11:00:00Z' },
        { org_slug: 'globex', expires_at: '2026-09-28T13:00:00Z' },
      ],
    }
    expect(activeStepInFor(user, 'acme', now)).toBeNull()
    expect(activeStepInFor(user, 'globex', now)?.expires_at).toBe('2026-09-28T13:00:00Z')
    expect(activeStepInFor(user, null, now)).toBeNull()
    expect(activeStepInFor({}, 'globex', now)).toBeNull()
  })
})
