import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import type { DocSharing } from '@/types/docs'
import { DocShareDialog } from './DocShareDialog'
import type { DocSharingTarget } from './useDocs'

vi.mock('@/api/docs', () => ({
  docsApi: {
    fileSharing: vi.fn(),
    updateFileSharing: vi.fn(),
    folderSharing: vi.fn(),
    updateFolderSharing: vi.fn(),
  },
}))
vi.mock('@/api/orgs', () => ({ orgsApi: { members: vi.fn() } }))
vi.mock('@/api/orgGroups', async importOriginal => ({
  ...(await importOriginal<typeof import('@/api/orgGroups')>()),
  orgGroupsApi: { list: vi.fn() },
}))

import { docsApi } from '@/api/docs'
import { orgGroupsApi } from '@/api/orgGroups'
import { orgsApi } from '@/api/orgs'

const FILE: DocSharingTarget = { kind: 'file', scope: 'project', path: 'guides/setup.md' }
const FOLDER: DocSharingTarget = { kind: 'folder', scope: 'organization', path: 'warehouse/' }

function sharing(overrides: Partial<DocSharing> = {}): DocSharing {
  return { visibility: 'level', inherited: false, inherited_from: null, shares: [], ...overrides }
}

function renderDialog(target: DocSharingTarget | null = FILE, canManage = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  const onClose = vi.fn()
  render(
    <QueryClientProvider client={client}>
      <DocShareDialog
        slug="demo"
        target={target}
        organizationSlug="acme"
        organizationName="Acme"
        canManage={canManage}
        onClose={onClose}
      />
    </QueryClientProvider>,
  )
  return { onClose, invalidate }
}

const radio = (name: RegExp) => screen.getByRole('radio', { name })
const save = () => screen.getByRole('button', { name: 'Save' })

beforeEach(() => {
  vi.mocked(docsApi.fileSharing).mockReset()
  vi.mocked(docsApi.updateFileSharing).mockReset()
  vi.mocked(docsApi.folderSharing).mockReset()
  vi.mocked(docsApi.updateFolderSharing).mockReset()
  vi.mocked(orgsApi.members).mockReset().mockResolvedValue([
    { id: 'u-1', email: 'alice@example.test', name: 'Alice Example', role: 'member', created_at: '2026-09-01T00:00:00Z' },
    { id: 'u-2', email: 'bob@example.test', name: null, role: 'member', created_at: '2026-09-01T00:00:00Z' },
  ])
  vi.mocked(orgGroupsApi.list).mockReset().mockResolvedValue([
    { id: 'g-1', name: 'Analysts', description: '', created_at: '2026-09-01T00:00:00Z', member_count: 2, managed_by_scim: false } as never,
  ])
})

describe('DocShareDialog', () => {
  it('renders nothing without a target', () => {
    renderDialog(null)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(docsApi.fileSharing).not.toHaveBeenCalled()
  })

  it('shows the default and saves a switch to "Only me"', async () => {
    vi.mocked(docsApi.fileSharing).mockResolvedValue(sharing())
    vi.mocked(docsApi.updateFileSharing).mockResolvedValue(sharing({ visibility: 'private' }))
    const { onClose, invalidate } = renderDialog()

    expect(await screen.findByText('Everyone in this project can read this note.')).toBeInTheDocument()
    expect(docsApi.fileSharing).toHaveBeenCalledWith('demo', 'project', 'guides/setup.md', expect.anything())
    expect(radio(/Everyone in this project/)).toBeChecked()
    // Its own setting: the "follow the folder" toggle is offered, off.
    expect(screen.getByRole('checkbox', { name: 'Follow the folder setting' })).not.toBeChecked()

    fireEvent.click(radio(/Only me/))
    expect(screen.getByText('Only the author can read this note.')).toBeInTheDocument()
    fireEvent.click(save())

    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(docsApi.updateFileSharing).toHaveBeenCalledWith('demo', 'project', 'guides/setup.md', {
      visibility: 'private',
      inherited: false,
      shares: [],
    })
    expect(invalidate).toHaveBeenCalled()
  })

  it('edits people and groups: add, change permission, remove — and sends no names', async () => {
    vi.mocked(docsApi.fileSharing).mockResolvedValue(
      sharing({
        visibility: 'restricted',
        shares: [{ principal_type: 'user', principal_id: 'u-1', name: 'Alice Example', permission: 'view' }],
      }),
    )
    vi.mocked(docsApi.updateFileSharing).mockResolvedValue(sharing())
    renderDialog()

    expect(await screen.findByText('The author and 1 person can read this note.')).toBeInTheDocument()
    const list = screen.getByRole('region', { name: 'Shared with' })
    // Alice is already shared, so only the group and Bob (by email) are offered.
    const suggestions = await screen.findByRole('list', { name: 'Suggestions' })
    expect(within(suggestions).queryByRole('button', { name: 'Add Alice Example' })).toBeNull()
    fireEvent.change(screen.getByRole('searchbox', { name: 'Add people or groups' }), { target: { value: 'analy' } })
    fireEvent.click(within(screen.getByRole('list', { name: 'Suggestions' })).getByRole('button', { name: 'Add Analysts' }))
    expect(screen.getByText('The author and 1 person and 1 group can read this note.')).toBeInTheDocument()

    const group = within(list).getByRole('group', { name: 'Permission for Analysts' })
    fireEvent.click(within(group).getByRole('button', { name: 'Can edit' }))
    fireEvent.click(within(list).getByRole('button', { name: 'Remove Alice Example' }))
    fireEvent.click(save())

    await waitFor(() => expect(docsApi.updateFileSharing).toHaveBeenCalled())
    expect(vi.mocked(docsApi.updateFileSharing).mock.calls[0]?.[3]).toEqual({
      visibility: 'restricted',
      inherited: false,
      shares: [{ principal_type: 'group', principal_id: 'g-1', permission: 'edit' }],
    })
  })

  it('offers members by name or email', async () => {
    vi.mocked(docsApi.fileSharing).mockResolvedValue(sharing({ visibility: 'restricted' }))
    renderDialog()
    const suggestions = await screen.findByRole('list', { name: 'Suggestions' })
    await within(suggestions).findByRole('button', { name: 'Add bob@example.test' })
    expect(within(suggestions).getByRole('button', { name: 'Add Alice Example' })).toBeInTheDocument()
  })

  it('locks the choice while the note follows its folder, and unlocks it on override', async () => {
    vi.mocked(docsApi.folderSharing).mockResolvedValue(
      sharing({ visibility: 'private', inherited: true, inherited_from: 'warehouse/' }),
    )
    vi.mocked(docsApi.updateFolderSharing).mockResolvedValue(sharing())
    renderDialog(FOLDER)

    expect(await screen.findByText("Only each note's own author can read the notes in this folder.")).toBeInTheDocument()
    // A folder has no single author, so "private" is not offered as "Only me".
    expect(radio(/Only each note's author/)).toBeInTheDocument()
    expect(screen.queryByRole('radio', { name: /Only me/ })).toBeNull()
    expect(screen.getByText((_, el) => el?.textContent === 'Inherited from warehouse/.')).toBeInTheDocument()
    const follow = screen.getByRole('checkbox', { name: 'Follow the folder setting' })
    expect(follow).toBeChecked()
    expect(radio(/Everyone in Acme/)).toBeDisabled()

    fireEvent.click(follow)
    expect(radio(/Everyone in Acme/)).toBeEnabled()
    fireEvent.click(radio(/Everyone in Acme/))
    fireEvent.click(save())
    await waitFor(() =>
      expect(docsApi.updateFolderSharing).toHaveBeenCalledWith('demo', 'organization', 'warehouse/', {
        visibility: 'level',
        inherited: false,
        shares: [],
      }),
    )
  })

  it('is read-only for someone who may not change it, and loads no member list', async () => {
    vi.mocked(docsApi.fileSharing).mockResolvedValue(
      sharing({
        visibility: 'restricted',
        shares: [{ principal_type: 'group', principal_id: 'g-1', name: 'Analysts', permission: 'view' }],
      }),
    )
    renderDialog(FILE, false)
    expect(await screen.findByText('The author and 1 group can read this note.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Done' })).toBeInTheDocument()
    expect(radio(/Only me/)).toBeDisabled()
    expect(screen.queryByRole('searchbox', { name: 'Add people or groups' })).toBeNull()
    expect(orgsApi.members).not.toHaveBeenCalled()
    expect(screen.getByText(/Only the author or an organization owner or admin changes/)).toBeInTheDocument()
  })

  it('lets the server\'s can_manage overrule the page\'s guess', async () => {
    vi.mocked(docsApi.fileSharing).mockResolvedValue(sharing({ can_manage: false }))
    renderDialog(FILE, true)
    await screen.findByText('Everyone in this project can read this note.')
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('shows a refusal in place and stays open', async () => {
    vi.mocked(docsApi.fileSharing).mockResolvedValue(sharing())
    vi.mocked(docsApi.updateFileSharing).mockRejectedValue(
      new ApiError('Only the author or an organization owner or admin can change sharing', 403),
    )
    const { onClose } = renderDialog()
    await screen.findByText('Everyone in this project can read this note.')
    fireEvent.click(radio(/Only me/))
    fireEvent.click(save())
    expect(await screen.findByRole('alert')).toHaveTextContent('Only the author or an organization owner or admin')
    expect(onClose).not.toHaveBeenCalled()
  })

  it('shows a load error with a retry', async () => {
    vi.mocked(docsApi.fileSharing).mockRejectedValue(new ApiError('Not found', 404))
    renderDialog()
    expect(await screen.findByText("Couldn't load the sharing settings")).toBeInTheDocument()
  })

  it('mentions the audited break-glass read', async () => {
    vi.mocked(docsApi.fileSharing).mockResolvedValue(sharing())
    renderDialog()
    expect(await screen.findByText(/every such read is recorded in the audit log/)).toBeInTheDocument()
  })
})

describe('DocShareDialog — unsaved sharing (prelaunch)', () => {
  it('asks before Cancel drops a changed setting, and closes an untouched one at once', async () => {
    vi.mocked(docsApi.fileSharing).mockResolvedValue(sharing())
    const { onClose } = renderDialog()
    await screen.findByText('Everyone in this project can read this note.')

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalledTimes(1)

    fireEvent.click(radio(/Only me/))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    fireEvent.click(await screen.findByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(radio(/Only me/)).toBeChecked()
    expect(docsApi.updateFileSharing).not.toHaveBeenCalled()
  })
})
