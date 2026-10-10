import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { orgGroupsApi, type OrgGroup, type OrgGroupDetail } from '@/api/orgGroups'
import { orgsApi } from '@/api/orgs'
import { ActiveOrgContext } from '@/components/active-org-context'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { authAs } from '@/test/auth'
import type { Role, UserListItem } from '@/types'
import OrgGroupsSection from './OrgGroupsSection'

/**
 * Organization › Groups (F20): any member reads the groups; owners and admins
 * create, rename, delete them (after a confirm) and pick members from the
 * organization's members.
 */

const STAMP = '2026-09-28T00:00:00Z'

function auth(role: Role): AuthContextValue {
  const value = authAs(role)
  return { ...value, user: value.user && { ...value.user, orgs: [] } }
}

function group(overrides: Partial<OrgGroup> & { id: string; name: string }): OrgGroup {
  return {
    description: '',
    member_count: 0,
    created_at: STAMP,
    updated_at: STAMP,
    managed_by_scim: false,
    ...overrides,
  }
}

function detail(base: OrgGroup, members: OrgGroupDetail['members'] = []): OrgGroupDetail {
  return { ...base, member_count: members.length, members }
}

function user(id: string, name: string): UserListItem {
  return { id, name, email: `${name.toLowerCase()}@example.com`, role: 'member', created_at: STAMP }
}

const ANALYSTS = group({ id: 'g1', name: 'Analysts', description: 'Data people', member_count: 1 })
const ALICE = { user_id: 'u-alice', name: 'Alice', email: 'alice@example.com', added_at: STAMP }

function renderSection(role: Role = 'owner', groups: OrgGroup[] = [ANALYSTS]) {
  const list = vi.spyOn(orgGroupsApi, 'list').mockResolvedValue(groups)
  const get = vi.spyOn(orgGroupsApi, 'get').mockResolvedValue(detail(ANALYSTS, [ALICE]))
  const members = vi
    .spyOn(orgsApi, 'members')
    .mockResolvedValue([user('u-alice', 'Alice'), user('u-bob', 'Bob')])
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth(role)}>
        <ActiveOrgContext.Provider value={{ slug: 'acme', membership: null, orgs: [] }}>
          <MemoryRouter>
            <OrgGroupsSection />
          </MemoryRouter>
        </ActiveOrgContext.Provider>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
  return { list, get, members }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Organization › Groups', () => {
  it("lists the active organization's groups", async () => {
    const { list } = renderSection()
    expect(await screen.findByText('Analysts')).toBeInTheDocument()
    expect(list).toHaveBeenCalledWith('acme')
    expect(screen.getByText('Data people')).toBeInTheDocument()
    expect(screen.getByText('1 member')).toBeInTheDocument()
  })

  it('creates a group', async () => {
    const created = group({ id: 'g2', name: 'Ops' })
    const create = vi.spyOn(orgGroupsApi, 'create').mockResolvedValue(detail(created))
    renderSection('admin')
    await screen.findByText('Analysts')

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: '  Ops ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create group' }))

    await waitFor(() => expect(create).toHaveBeenCalledWith('acme', { name: 'Ops', description: '' }))
  })

  it('renames a group, sending only what changed', async () => {
    const update = vi
      .spyOn(orgGroupsApi, 'update')
      .mockResolvedValue(detail({ ...ANALYSTS, name: 'Analytics' }, [ALICE]))
    renderSection()
    fireEvent.click(await screen.findByRole('button', { name: 'Manage Analysts' }))

    const input = await screen.findByDisplayValue('Analysts')
    fireEvent.change(input, { target: { value: 'Analytics' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(update).toHaveBeenCalledWith('acme', 'g1', { name: 'Analytics' }))
  })

  it('deletes a group only after the confirm', async () => {
    const remove = vi.spyOn(orgGroupsApi, 'delete').mockResolvedValue(undefined)
    renderSection()
    fireEvent.click(await screen.findByRole('button', { name: 'Delete Analysts' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/Its members stay in the organization/)
    expect(remove).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete group' }))

    await waitFor(() => expect(remove).toHaveBeenCalledWith('acme', 'g1'))
  })

  it('adds an organization member who is not in the group yet, and removes one', async () => {
    const add = vi.spyOn(orgGroupsApi, 'addMember').mockResolvedValue({
      user_id: 'u-bob',
      name: 'Bob',
      email: 'bob@example.com',
      added_at: STAMP,
    })
    const drop = vi.spyOn(orgGroupsApi, 'removeMember').mockResolvedValue(undefined)
    renderSection()
    fireEvent.click(await screen.findByRole('button', { name: 'Manage Analysts' }))
    expect(await screen.findByText('alice@example.com')).toBeInTheDocument()

    const picker = screen.getByLabelText('Organization member to add')
    await waitFor(() => expect(within(picker).getByRole('option', { name: /Bob/ })).toBeInTheDocument())
    // Alice is already in the group, so she is not offered.
    expect(within(picker).queryByRole('option', { name: /Alice/ })).toBeNull()
    fireEvent.change(picker, { target: { value: 'u-bob' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add to group' }))
    await waitFor(() => expect(add).toHaveBeenCalledWith('acme', 'g1', 'u-bob'))

    fireEvent.click(screen.getByRole('button', { name: 'Remove Alice from Analysts' }))
    await waitFor(() => expect(drop).toHaveBeenCalledWith('acme', 'g1', 'u-alice'))
  })

  it('shows a member the groups read-only', async () => {
    const { members } = renderSection('member')
    fireEvent.click(await screen.findByRole('button', { name: 'View Analysts' }))

    expect(await screen.findByText('alice@example.com')).toBeInTheDocument()
    expect(screen.getByText(/Owners and admins of the organization manage its groups/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create group' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Delete Analysts' })).toBeNull()
    expect(screen.queryByRole('button', { name: /Remove Alice/ })).toBeNull()
    expect(screen.queryByLabelText('Organization member to add')).toBeNull()
    expect(members).not.toHaveBeenCalled()
  })

  it('marks a SCIM-managed group and offers no manual rename or member edit', async () => {
    const managed = group({ id: 'g1', name: 'Analysts', member_count: 1, managed_by_scim: true })
    const { get, members } = renderSection('owner', [managed])
    get.mockResolvedValue(detail(managed, [ALICE]))

    expect(await screen.findByText('Managed by SCIM')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Manage Analysts' }))

    expect(await screen.findByText('alice@example.com')).toBeInTheDocument()
    expect(screen.getByText(/is managed by SCIM\. Change its name and members, or delete it, in your identity provider/)).toBeInTheDocument()
    expect(await screen.findByDisplayValue('Analysts')).toBeDisabled()
    expect(screen.queryByRole('button', { name: /Remove Alice/ })).toBeNull()
    expect(screen.queryByLabelText('Organization member to add')).toBeNull()
    expect(members).not.toHaveBeenCalled()
  })

  it('offers no Delete for a SCIM-managed group (the API refuses it with 409)', async () => {
    const managed = group({ id: 'g1', name: 'Analysts', managed_by_scim: true })
    renderSection('owner', [managed])
    expect(await screen.findByText('Managed by SCIM')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete Analysts' })).toBeNull()
  })

  it("keeps a SCIM-managed group's description editable", async () => {
    const managed = group({ id: 'g1', name: 'Analysts', description: 'Data people', managed_by_scim: true })
    const { get } = renderSection('owner', [managed])
    get.mockResolvedValue(detail(managed, [ALICE]))
    const update = vi
      .spyOn(orgGroupsApi, 'update')
      .mockResolvedValue(detail({ ...managed, description: 'Analytics team' }, [ALICE]))

    fireEvent.click(await screen.findByRole('button', { name: 'Manage Analysts' }))
    const description = await screen.findByDisplayValue('Data people')
    fireEvent.change(description, { target: { value: 'Analytics team' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(update).toHaveBeenCalledWith('acme', 'g1', { description: 'Analytics team' }))
  })

  // A new owner had no reason to make one: the page says what a group is for.
  it('says what a group is for, and where to use one when there are none', async () => {
    renderSection('owner', [])
    expect(
      await screen.findByText('No groups yet. Create one above, then choose it when you share a note in Docs.'),
    ).toBeInTheDocument()
    expect(screen.getByText(/Share a Docs note with a group instead of person by person/)).toBeInTheDocument()
  })

  it('does not tell a member to create the group they cannot', async () => {
    renderSection('member', [])
    expect(await screen.findByText('No groups yet.')).toBeInTheDocument()
    expect(screen.queryByText(/Create one above/)).toBeNull()
  })
})
