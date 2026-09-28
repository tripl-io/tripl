import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { platformApi, type PlatformOrg } from '@/api/platform'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { authAs } from '@/test/auth'
import PlatformOrgsSection from './PlatformOrgsSection'

/**
 * Platform › Organizations (F20): the list with its search and status filter,
 * suspension with a mandatory reason, reinstatement after a confirm, and the
 * read-only step-in with a reason and a length.
 */

const STAMP = '2026-09-28T00:00:00Z'

function org(overrides: Partial<PlatformOrg> & { slug: string; name: string }): PlatformOrg {
  return {
    id: `id-${overrides.slug}`,
    status: 'active',
    created_at: STAMP,
    suspended_at: null,
    suspended_reason: null,
    member_count: 3,
    project_count: 2,
    owner_emails: [`owner@${overrides.slug}.example.com`],
    ...overrides,
  }
}

const ACME = org({ slug: 'acme', name: 'Acme' })
const GLOBEX = org({
  slug: 'globex',
  name: 'Globex',
  status: 'suspended',
  suspended_at: STAMP,
  suspended_reason: 'Unpaid invoice',
  member_count: 1,
  project_count: 1,
})

function platformAuth(): AuthContextValue {
  const value = authAs('owner')
  return { ...value, refresh: vi.fn(), user: value.user && { ...value.user, is_platform_admin: true } }
}

function renderSection(orgs: PlatformOrg[] = [ACME, GLOBEX], auth = platformAuth()) {
  const list = vi.spyOn(platformApi, 'listOrgs').mockResolvedValue({ items: orgs, total: orgs.length })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <MemoryRouter initialEntries={['/settings/platform/orgs']}>
          <Routes>
            <Route path="/settings/platform/orgs" element={<PlatformOrgsSection />} />
            <Route path="/o/:org" element={<p>Organization home</p>} />
          </Routes>
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
  return { list, auth }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Platform › Organizations', () => {
  it('lists organizations with their status, counts and owners', async () => {
    renderSection()
    const acme = (await screen.findByRole('link', { name: 'Acme' })).closest('tr')!
    expect(acme).toHaveTextContent('Active')
    expect(acme).toHaveTextContent('owner@acme.example.com')
    expect(within(acme).getByRole('link', { name: 'Acme' })).toHaveAttribute(
      'href',
      '/settings/platform/orgs/acme',
    )
    const globex = screen.getByRole('link', { name: 'Globex' }).closest('tr')!
    expect(globex).toHaveTextContent('Suspended')
    expect(globex).toHaveTextContent('Unpaid invoice')
    expect(screen.getByText('Showing 1–2 of 2')).toBeInTheDocument()
  })

  it('offers suspend on an active organization and unsuspend on a suspended one', async () => {
    renderSection()
    await screen.findByRole('link', { name: 'Acme' })
    expect(screen.getByRole('button', { name: 'Suspend Acme' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Unsuspend Acme' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Unsuspend Globex' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Suspend Globex' })).toBeNull()
    // Suspension blocks members and keys, but an operator may still step in to investigate.
    expect(screen.getByRole('button', { name: 'Step in to Globex (read-only)' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Step in to Acme (read-only)' })).toBeInTheDocument()
  })

  it('searches and filters by status', async () => {
    const { list } = renderSection()
    await screen.findByRole('link', { name: 'Acme' })
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search organizations' }), {
      target: { value: 'glo' },
    })
    await waitFor(() =>
      expect(list).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'glo', offset: 0 }), expect.anything()),
    )
    fireEvent.change(screen.getByRole('combobox', { name: 'Status' }), { target: { value: 'suspended' } })
    await waitFor(() =>
      expect(list).toHaveBeenLastCalledWith(
        expect.objectContaining({ q: 'glo', status: 'suspended' }),
        expect.anything(),
      ),
    )
  })

  it('suspends only with a reason', async () => {
    const suspend = vi.spyOn(platformApi, 'suspendOrg').mockResolvedValue({ ...ACME, status: 'suspended' })
    renderSection()
    fireEvent.click(await screen.findByRole('button', { name: 'Suspend Acme' }))
    const dialog = await screen.findByRole('dialog', { name: 'Suspend Acme' })

    fireEvent.click(within(dialog).getByRole('button', { name: 'Suspend organization' }))
    expect(await within(dialog).findByText('Give a reason. It is recorded in the audit log.')).toBeInTheDocument()
    expect(suspend).not.toHaveBeenCalled()

    fireEvent.change(within(dialog).getByLabelText('Reason'), { target: { value: '  Abuse report 42  ' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Suspend organization' }))
    await waitFor(() => expect(suspend).toHaveBeenCalledWith('acme', 'Abuse report 42'))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Suspend Acme' })).toBeNull())
  })

  it('shows the server refusal inside the suspend dialog', async () => {
    const { ApiError } = await import('@/api/client')
    vi.spyOn(platformApi, 'suspendOrg').mockRejectedValue(
      new ApiError('The default organization cannot be suspended', 409),
    )
    renderSection()
    fireEvent.click(await screen.findByRole('button', { name: 'Suspend Acme' }))
    const dialog = await screen.findByRole('dialog', { name: 'Suspend Acme' })
    fireEvent.change(within(dialog).getByLabelText('Reason'), { target: { value: 'Test' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Suspend organization' }))
    expect(
      await within(dialog).findByText(/The default organization cannot be suspended/),
    ).toBeInTheDocument()
  })

  it('unsuspends after a confirm', async () => {
    const unsuspend = vi.spyOn(platformApi, 'unsuspendOrg').mockResolvedValue({ ...GLOBEX, status: 'active' })
    renderSection()
    fireEvent.click(await screen.findByRole('button', { name: 'Unsuspend Globex' }))
    const confirm = await screen.findByRole('alertdialog')
    expect(confirm).toHaveTextContent('Reinstate Globex?')
    fireEvent.click(within(confirm).getByRole('button', { name: 'Unsuspend' }))
    await waitFor(() => expect(unsuspend).toHaveBeenCalledWith('globex'))
  })

  it('steps in read-only with a reason and a length, then opens the organization', async () => {
    const stepIn = vi
      .spyOn(platformApi, 'stepIn')
      .mockResolvedValue({ id: 's1', org_slug: 'acme', expires_at: '2026-09-28T01:00:00Z' })
    const { auth } = renderSection()
    fireEvent.click(await screen.findByRole('button', { name: 'Step in to Acme (read-only)' }))
    const dialog = await screen.findByRole('dialog', { name: 'Step in to Acme (read-only)' })
    const ttl = within(dialog).getByLabelText('Length (minutes)')
    expect(ttl).toHaveValue(60)

    fireEvent.change(ttl, { target: { value: '500' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Step in (read-only)' }))
    expect(await within(dialog).findByText('Give a reason. It is recorded in the audit log.')).toBeInTheDocument()
    expect(within(dialog).getByText('Between 5 and 240 minutes.')).toBeInTheDocument()
    expect(stepIn).not.toHaveBeenCalled()

    fireEvent.change(within(dialog).getByLabelText('Reason'), { target: { value: 'Ticket 1234' } })
    fireEvent.change(ttl, { target: { value: '30' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Step in (read-only)' }))
    await waitFor(() =>
      expect(stepIn).toHaveBeenCalledWith('acme', { reason: 'Ticket 1234', ttl_minutes: 30 }),
    )
    expect(await screen.findByText('Organization home')).toBeInTheDocument()
    expect(auth.refresh).toHaveBeenCalled()
  })

  it('says so when nothing matches', async () => {
    renderSection([])
    expect(await screen.findByText('There are no organizations yet.')).toBeInTheDocument()
    expect(screen.getByText('No results')).toBeInTheDocument()
  })
})
