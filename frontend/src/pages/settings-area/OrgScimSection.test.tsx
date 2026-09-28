import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import { orgGroupsApi, orgMembersKey, type OrgGroup } from '@/api/orgGroups'
import { scimApi, type ScimConfig, type ScimToken } from '@/api/scim'
import { ActiveOrgContext } from '@/components/active-org-context'
import { usersKey } from '@/lib/queryKeys'
import OrgScimSection from './OrgScimSection'

/**
 * Organization › Provisioning (SCIM) (F20): the base URL to give the identity
 * provider, bearer tokens shown once and revocable, and the admin-group mapping.
 */

const STAMP = '2026-09-28T00:00:00Z'

const ACTIVE: ScimToken = {
  id: 't1',
  prefix: 'tripl_scim_ab12',
  created_at: '2026-09-20T10:00:00Z',
  last_used_at: '2026-09-27T10:00:00Z',
  revoked_at: null,
}
const REVOKED: ScimToken = {
  id: 't0',
  prefix: 'tripl_scim_zz99',
  created_at: '2026-09-01T10:00:00Z',
  last_used_at: null,
  revoked_at: '2026-09-10T10:00:00Z',
}

function group(id: string, name: string, managed = false): OrgGroup {
  return { id, name, description: '', member_count: 0, created_at: STAMP, updated_at: STAMP, managed_by_scim: managed }
}

function renderSection(
  config: ScimConfig = { base_url: 'https://tripl.example.com/scim/v2/acme', admin_group_id: null },
  tokens: ScimToken[] = [ACTIVE, REVOKED],
  groups: OrgGroup[] = [group('g1', 'Admins', true), group('g2', 'Analysts')],
) {
  const getConfig = vi.spyOn(scimApi, 'getConfig').mockResolvedValue(config)
  const listTokens = vi.spyOn(scimApi, 'listTokens').mockResolvedValue(tokens)
  const listGroups = vi.spyOn(orgGroupsApi, 'list').mockResolvedValue(groups)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <ActiveOrgContext.Provider value={{ slug: 'acme', membership: null, orgs: [] }}>
        <MemoryRouter>
          <OrgScimSection />
        </MemoryRouter>
      </ActiveOrgContext.Provider>
    </QueryClientProvider>,
  )
  return { getConfig, listTokens, listGroups, queryClient }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Organization › Provisioning (SCIM)', () => {
  it('shows the base URL to give the identity provider', async () => {
    const { getConfig, listTokens } = renderSection()

    expect(await screen.findByText('https://tripl.example.com/scim/v2/acme')).toBeInTheDocument()
    expect(getConfig).toHaveBeenCalledWith('acme')
    expect(listTokens).toHaveBeenCalledWith('acme')
  })

  it('lists tokens by prefix only, with revoked ones marked and not revocable', async () => {
    renderSection()

    expect(await screen.findByText('tripl_scim_ab12…')).toBeInTheDocument()
    expect(screen.getByText('tripl_scim_zz99…')).toBeInTheDocument()
    expect(screen.getByText(/Revoked 2026-09-10/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Revoke tripl_scim_ab12…' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Revoke tripl_scim_zz99…' })).toBeNull()
    expect(screen.getByText(/never used/)).toBeInTheDocument()
  })

  it('creates a token and shows it once, closable only from its own button', async () => {
    renderSection()
    const create = vi.spyOn(scimApi, 'createToken').mockResolvedValue({
      id: 't2',
      prefix: 'tripl_scim_cd34',
      token: 'tripl_scim_cd34-full-secret',
      created_at: STAMP,
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Create token' }))

    await waitFor(() => expect(create).toHaveBeenCalledWith('acme'))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByLabelText('SCIM token')).toHaveValue('tripl_scim_cd34-full-secret')
    expect(within(dialog).getByText(/shown only once/)).toBeInTheDocument()

    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    fireEvent.click(within(dialog).getByRole('button', { name: 'I’ve saved it' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.queryByDisplayValue('tripl_scim_cd34-full-secret')).toBeNull()
  })

  it('shows why a token could not be created', async () => {
    renderSection()
    vi.spyOn(scimApi, 'createToken').mockRejectedValue(new ApiError('Too many tokens', 409))

    fireEvent.click(await screen.findByRole('button', { name: 'Create token' }))

    expect(await screen.findByText('Too many tokens')).toBeInTheDocument()
  })

  it('revokes a token only after the confirm', async () => {
    renderSection()
    const revoke = vi.spyOn(scimApi, 'revokeToken').mockResolvedValue(undefined)

    fireEvent.click(await screen.findByRole('button', { name: 'Revoke tripl_scim_ab12…' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(revoke).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Revoke token' }))

    await waitFor(() => expect(revoke).toHaveBeenCalledWith('acme', 't1'))
  })

  it('maps an admin group from the organization groups', async () => {
    renderSection()
    const update = vi
      .spyOn(scimApi, 'updateConfig')
      .mockResolvedValue({ base_url: 'https://tripl.example.com/scim/v2/acme', admin_group_id: 'g1' })

    const select = await screen.findByLabelText('Group')
    await waitFor(() => expect(within(select).getByRole('option', { name: 'Admins (SCIM)' })).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()

    fireEvent.change(select, { target: { value: 'g1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(update).toHaveBeenCalledWith('acme', { admin_group_id: 'g1' }))
    expect(await screen.findByText('Saved')).toBeInTheDocument()
  })

  it('refreshes the member rosters after the mapping changes roles', async () => {
    const { queryClient } = renderSection()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    vi.spyOn(scimApi, 'updateConfig').mockResolvedValue({
      base_url: 'https://tripl.example.com/scim/v2/acme',
      admin_group_id: 'g1',
    })

    const select = await screen.findByLabelText('Group')
    await waitFor(() => expect(within(select).getByRole('option', { name: 'Admins (SCIM)' })).toBeInTheDocument())
    fireEvent.change(select, { target: { value: 'g1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: orgMembersKey('acme') }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: usersKey() })
  })

  it('clears the admin group mapping', async () => {
    renderSection({ base_url: 'https://tripl.example.com/scim/v2/acme', admin_group_id: 'g1' })
    const update = vi
      .spyOn(scimApi, 'updateConfig')
      .mockResolvedValue({ base_url: 'https://tripl.example.com/scim/v2/acme', admin_group_id: null })

    const select = await screen.findByLabelText('Group')
    await waitFor(() => expect(select).toHaveValue('g1'))
    fireEvent.change(select, { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(update).toHaveBeenCalledWith('acme', { admin_group_id: null }))
  })

  it('offers a retry when the settings fail to load', async () => {
    vi.spyOn(scimApi, 'getConfig').mockRejectedValue(new ApiError('Boom', 500))
    vi.spyOn(scimApi, 'listTokens').mockResolvedValue([])
    vi.spyOn(orgGroupsApi, 'list').mockResolvedValue([])
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <ActiveOrgContext.Provider value={{ slug: 'acme', membership: null, orgs: [] }}>
          <MemoryRouter>
            <OrgScimSection />
          </MemoryRouter>
        </ActiveOrgContext.Provider>
      </QueryClientProvider>,
    )

    expect(await screen.findByText("Couldn't load provisioning settings")).toBeInTheDocument()
  })
})
