import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { platformApi } from '@/api/platform'
import { ActiveOrgContext } from '@/components/active-org-context'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { authAs } from '@/test/auth'
import type { ActiveStepIn } from '@/types'
import { stepInEndTime } from '@/lib/orgStatus'
import { StepInBanner } from './step-in-banner'

/**
 * The read-only step-in banner (F20): shown while the platform admin has an
 * unexpired step-in to the organization on screen, and ends it on request.
 */

const IN_AN_HOUR = new Date(Date.now() + 60 * 60 * 1000).toISOString()
const AN_HOUR_AGO = new Date(Date.now() - 60 * 60 * 1000).toISOString()

function auth(stepIns: ActiveStepIn[]): AuthContextValue {
  const value = authAs('member')
  return {
    ...value,
    refresh: vi.fn(),
    user: value.user && { ...value.user, is_platform_admin: true, orgs: [], active_step_ins: stepIns },
  }
}

function renderBanner(value: AuthContextValue, org = 'acme') {
  render(
    <AuthContext.Provider value={value}>
      <ActiveOrgContext.Provider value={{ slug: org, membership: null, orgs: [] }}>
        <MemoryRouter initialEntries={[`/o/${org}`]}>
          <Routes>
            <Route path="/o/:org" element={<StepInBanner />} />
            <Route path="/settings/platform/orgs" element={<p>Platform console</p>} />
          </Routes>
        </MemoryRouter>
      </ActiveOrgContext.Provider>
    </AuthContext.Provider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('StepInBanner', () => {
  it('says the step-in is read-only and when it ends', () => {
    renderBanner(auth([{ org_slug: 'acme', expires_at: IN_AN_HOUR }]))
    const banner = screen.getByTestId('step-in-banner')
    expect(banner).toHaveTextContent('Read-only step-in to acme')
    expect(banner).toHaveTextContent(`ends at ${stepInEndTime(IN_AN_HOUR)}`)
    expect(stepInEndTime(IN_AN_HOUR)).toMatch(/^\d{2}:\d{2}$/)
  })

  it('renders nothing in another organization', () => {
    renderBanner(auth([{ org_slug: 'globex', expires_at: IN_AN_HOUR }]))
    expect(screen.queryByTestId('step-in-banner')).toBeNull()
  })

  it('renders nothing for an expired step-in', () => {
    renderBanner(auth([{ org_slug: 'acme', expires_at: AN_HOUR_AGO }]))
    expect(screen.queryByTestId('step-in-banner')).toBeNull()
  })

  it('ends the step-in and returns to the console', async () => {
    const list = vi
      .spyOn(platformApi, 'listStepIns')
      .mockResolvedValue([{ id: 's-9', org_slug: 'acme', expires_at: IN_AN_HOUR }])
    const end = vi.spyOn(platformApi, 'endStepIn').mockResolvedValue(undefined)
    const value = auth([{ org_slug: 'acme', expires_at: IN_AN_HOUR }])
    renderBanner(value)
    fireEvent.click(screen.getByRole('button', { name: 'End now' }))
    await waitFor(() => expect(end).toHaveBeenCalledWith('s-9'))
    expect(list).toHaveBeenCalledWith(true)
    expect(value.refresh).toHaveBeenCalled()
    expect(await screen.findByText('Platform console')).toBeInTheDocument()
  })

  it('ends it by the id the session carries, without a lookup', async () => {
    const list = vi.spyOn(platformApi, 'listStepIns')
    const end = vi.spyOn(platformApi, 'endStepIn').mockResolvedValue(undefined)
    renderBanner(auth([{ id: 's-1', org_slug: 'acme', expires_at: IN_AN_HOUR }]))
    fireEvent.click(screen.getByRole('button', { name: 'End now' }))
    await waitFor(() => expect(end).toHaveBeenCalledWith('s-1'))
    expect(list).not.toHaveBeenCalled()
  })

  it('keeps the banner with the error when ending fails', async () => {
    vi.spyOn(platformApi, 'listStepIns').mockRejectedValue(new Error('Network down'))
    renderBanner(auth([{ org_slug: 'acme', expires_at: IN_AN_HOUR }]))
    fireEvent.click(screen.getByRole('button', { name: 'End now' }))
    expect(await screen.findByText('Network down')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'End now' })).toBeEnabled()
  })
})
