import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { projectMembersApi } from '@/api/projectMembers'
import { projectsApi } from '@/api/projects'
import { usersApi } from '@/api/users'
import { AuthContext } from '@/components/auth-context'
import { authAs } from '@/test/auth'
import type { Project, ProjectMember, Role, UserListItem } from '@/types'
import ProjectMembersSection from './ProjectMembersSection'

vi.mock('@/api/projects', () => ({
  projectsApi: { get: vi.fn(), list: vi.fn() },
}))
vi.mock('@/api/projectMembers', () => ({
  projectMembersApi: { list: vi.fn(), add: vi.fn(), updateRole: vi.fn(), remove: vi.fn() },
}))
vi.mock('@/api/users', () => ({
  usersApi: { list: vi.fn(), updateRole: vi.fn() },
}))

const CREATOR_ID = 'creator-1'

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'project-1',
    name: 'Demo',
    slug: 'demo',
    description: '',
    app_version_keep_releases: 5,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    created_by_user_id: CREATOR_ID,
    my_role: 'editor',
    ...overrides,
  } as Project
}

function member(overrides: Partial<ProjectMember>): ProjectMember {
  return {
    user_id: 'u-1',
    name: 'Ada',
    email: 'ada@example.com',
    role: 'editor',
    added_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function user(overrides: Partial<UserListItem>): UserListItem {
  return {
    id: 'u-1',
    name: 'Ada',
    email: 'ada@example.com',
    role: 'member',
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

const MEMBERS = [
  member({ user_id: 'u-ada', name: 'Ada', email: 'ada@example.com', role: 'editor' }),
  member({ user_id: 'u-grace', name: 'Grace', email: 'grace@example.com', role: 'viewer' }),
]

const ROSTER = [
  user({ id: 'u-ada', name: 'Ada', email: 'ada@example.com' }),
  user({ id: 'u-grace', name: 'Grace', email: 'grace@example.com' }),
  user({ id: 'u-linus', name: 'Linus', email: 'linus@example.com' }),
  user({ id: 'u-boss', name: 'Boss', email: 'boss@example.com', role: 'owner' }),
  user({ id: 'u-root', name: 'Root', email: 'root@example.com', role: 'admin' }),
]

function renderSection(role: Role, id: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <AuthContext.Provider value={authAs(role, id)}>
        <MemoryRouter>
          <ProjectMembersSection slug="demo" />
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(projectsApi.get).mockResolvedValue(makeProject())
  vi.mocked(projectMembersApi.list).mockResolvedValue(MEMBERS)
  vi.mocked(usersApi.list).mockResolvedValue(ROSTER)
  vi.mocked(projectMembersApi.add).mockResolvedValue(member({ user_id: 'u-linus' }))
  vi.mocked(projectMembersApi.updateRole).mockResolvedValue(member({ user_id: 'u-ada' }))
  vi.mocked(projectMembersApi.remove).mockResolvedValue(undefined as never)
})

describe('Project · Access', () => {
  it('lists the members with role chips and no controls for a plain member', async () => {
    renderSection('member', 'u-ada')

    expect(await screen.findByText('grace@example.com')).toBeInTheDocument()
    expect(screen.getByText('2 people')).toBeInTheDocument()
    expect(
      await screen.findByText(/Only an organization owner or admin, or the person who created this project/),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add member' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Remove Ada' })).toBeNull()
    expect(screen.queryByLabelText('Role for Ada')).toBeNull()
    // Readers never fetch the roster: it is only needed to add someone.
    expect(usersApi.list).not.toHaveBeenCalled()
  })

  it('lets the project creator add someone from the roster, leaving out members, owners and admins', async () => {
    renderSection('member', CREATOR_ID)

    const person = await screen.findByLabelText('Person')
    await waitFor(() =>
      expect(within(person).getByRole('option', { name: 'Linus · linus@example.com' })).toBeInTheDocument(),
    )
    expect(within(person).queryByRole('option', { name: /Ada/ })).toBeNull()
    expect(within(person).queryByRole('option', { name: /Boss/ })).toBeNull()
    expect(within(person).queryByRole('option', { name: /Root/ })).toBeNull()

    fireEvent.change(person, { target: { value: 'u-linus' } })
    fireEvent.change(screen.getByLabelText('Role'), { target: { value: 'viewer' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add member' }))

    await waitFor(() =>
      expect(projectMembersApi.add).toHaveBeenCalledWith('demo', 'u-linus', 'viewer'),
    )
  })

  it('gives the instance owner the controls on a project someone else created', async () => {
    renderSection('owner', 'boss-1')

    expect(await screen.findByLabelText('Role for Ada')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove Grace' })).toBeInTheDocument()
  })

  it('asks before a demotion, then changes the role', async () => {
    renderSection('owner', 'boss-1')

    fireEvent.change(await screen.findByLabelText('Role for Ada'), { target: { value: 'viewer' } })
    const confirm = await screen.findByRole('alertdialog', { name: 'Change Ada to Viewer?' })
    expect(projectMembersApi.updateRole).not.toHaveBeenCalled()
    fireEvent.click(within(confirm).getByRole('button', { name: 'Change to Viewer' }))

    await waitFor(() =>
      expect(projectMembersApi.updateRole).toHaveBeenCalledWith('demo', 'u-ada', 'viewer'),
    )
  })

  it('promotes without asking', async () => {
    renderSection('owner', 'boss-1')

    fireEvent.change(await screen.findByLabelText('Role for Grace'), { target: { value: 'editor' } })

    await waitFor(() =>
      expect(projectMembersApi.updateRole).toHaveBeenCalledWith('demo', 'u-grace', 'editor'),
    )
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  it('confirms a removal and shows a refusal on the card', async () => {
    vi.mocked(projectMembersApi.remove).mockRejectedValue(new Error('Not allowed'))
    renderSection('owner', 'boss-1')

    fireEvent.click(await screen.findByRole('button', { name: 'Remove Grace' }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Remove member' })
    expect(within(confirm).getByText(/Remove Grace from this project\?/)).toBeInTheDocument()
    expect(projectMembersApi.remove).not.toHaveBeenCalled()
    fireEvent.click(within(confirm).getByRole('button', { name: 'Remove' }))

    await waitFor(() => expect(projectMembersApi.remove).toHaveBeenCalledWith('demo', 'u-grace'))
    expect(await screen.findByText(/Could not remove the member: Not allowed/)).toBeInTheDocument()
  })

  it('adds any organization member at the chosen role, with no org-level read-only cap (F20 PR4)', async () => {
    renderSection('owner', 'boss-1')

    const person = await screen.findByLabelText('Person')
    await waitFor(() =>
      expect(within(person).getByRole('option', { name: 'Linus · linus@example.com' })).toBeInTheDocument(),
    )
    fireEvent.change(person, { target: { value: 'u-linus' } })

    expect(screen.queryByText(/can only read this project/)).toBeNull()
  })

  // F20 PR15: a 'none' row opts an organization member out of the project the
  // organization's default access would otherwise give them.
  it('shows a No access row as such to a reader', async () => {
    vi.mocked(projectMembersApi.list).mockResolvedValue([
      ...MEMBERS,
      member({ user_id: 'u-linus', name: 'Linus', email: 'linus@example.com', role: 'none' }),
    ])
    renderSection('member', 'u-ada')

    expect(await screen.findByText('linus@example.com')).toBeInTheDocument()
    expect(screen.getByText('No access')).toHaveAttribute('data-slot', 'chip')
  })

  it('adds an organization member at No access', async () => {
    renderSection('owner', 'boss-1')

    const person = await screen.findByLabelText('Person')
    await waitFor(() =>
      expect(within(person).getByRole('option', { name: 'Linus · linus@example.com' })).toBeInTheDocument(),
    )
    const role = screen.getByLabelText('Role')
    expect(within(role).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'No access',
      'Viewer',
      'Editor',
    ])
    fireEvent.change(person, { target: { value: 'u-linus' } })
    fireEvent.change(role, { target: { value: 'none' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add member' }))

    await waitFor(() => expect(projectMembersApi.add).toHaveBeenCalledWith('demo', 'u-linus', 'none'))
  })

  it('asks before taking a member to No access', async () => {
    renderSection('owner', 'boss-1')

    fireEvent.change(await screen.findByLabelText('Role for Grace'), { target: { value: 'none' } })
    const confirm = await screen.findByRole('alertdialog', { name: 'Change Grace to No access?' })
    expect(within(confirm).getByText(/Grace loses this project/)).toBeInTheDocument()
    expect(projectMembersApi.updateRole).not.toHaveBeenCalled()
    fireEvent.click(within(confirm).getByRole('button', { name: 'Change to No access' }))

    await waitFor(() =>
      expect(projectMembersApi.updateRole).toHaveBeenCalledWith('demo', 'u-grace', 'none'),
    )
  })

  it('gives a No access member Viewer back without asking', async () => {
    vi.mocked(projectMembersApi.list).mockResolvedValue([
      member({ user_id: 'u-linus', name: 'Linus', email: 'linus@example.com', role: 'none' }),
    ])
    renderSection('owner', 'boss-1')

    fireEvent.change(await screen.findByLabelText('Role for Linus'), { target: { value: 'viewer' } })

    await waitFor(() =>
      expect(projectMembersApi.updateRole).toHaveBeenCalledWith('demo', 'u-linus', 'viewer'),
    )
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  // Owners/admins always have access; the backend refuses a 'none' row for them
  // (422), so their row never offers it.
  it('leaves No access out of the row select of an org owner or admin', async () => {
    vi.mocked(projectMembersApi.list).mockResolvedValue([
      member({ user_id: 'u-root', name: 'Root', email: 'root@example.com', role: 'editor' }),
      ...MEMBERS,
    ])
    renderSection('owner', 'boss-1')

    const rootRole = await screen.findByLabelText('Role for Root')
    await waitFor(() =>
      expect(within(rootRole).getAllByRole('option').map((o) => o.textContent)).toEqual([
        'Viewer',
        'Editor',
      ]),
    )
    const graceRole = screen.getByLabelText('Role for Grace')
    expect(within(graceRole).getAllByRole('option').map((o) => o.textContent)).toContain('No access')
  })

  it('shows a leftover No access row of a since-promoted admin as always having access', async () => {
    vi.mocked(projectMembersApi.list).mockResolvedValue([
      member({ user_id: 'u-root', name: 'Root', email: 'root@example.com', role: 'none' }),
    ])
    renderSection('owner', 'boss-1')

    expect(await screen.findByText('Owner/admin · always has access')).toBeInTheDocument()
    expect(screen.queryByLabelText('Role for Root')).toBeNull()
    expect(screen.getByRole('button', { name: 'Remove Root' })).toBeInTheDocument()
  })
})
