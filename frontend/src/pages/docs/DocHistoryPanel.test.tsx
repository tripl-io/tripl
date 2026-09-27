import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { toast } from 'sonner'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import type { DocRevisionDetail, DocRevisionListResponse, DocRevisionSummary } from '@/types/docs'
import { DiffView, DocHistoryPanel } from './DocHistoryPanel'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), info: vi.fn(), error: vi.fn() },
}))
vi.mock('@/api/docs', () => ({
  docsApi: { revisions: vi.fn(), revision: vi.fn(), restore: vi.fn() },
}))

import { docsApi } from '@/api/docs'

function rev(overrides: Partial<DocRevisionSummary> = {}): DocRevisionSummary {
  return {
    id: 'r-1',
    number: 1,
    action: 'create',
    path: 'guides/setup.md',
    message: '',
    author_name: 'Ann',
    created_at: '2026-09-01T10:00:00Z',
    content_sha256: 'abc',
    size_bytes: 2048,
    restored_from_number: null,
    ...overrides,
  }
}

function list(items: DocRevisionSummary[], current = 3): DocRevisionListResponse {
  return { scope: 'project', path: 'guides/setup.md', current_revision: current, items }
}

function detail(overrides: Partial<DocRevisionDetail> = {}): DocRevisionDetail {
  return { ...rev(), content: '# Setup\n', diff: '', diff_truncated: false, ...overrides }
}

const ITEMS = [
  rev({ id: 'r-3', number: 3, action: 'restore', restored_from_number: 1, message: 'Restored revision 1' }),
  rev({ id: 'r-2', number: 2, action: 'move', path: 'guides/install.md', author_name: null }),
  rev({ id: 'r-1', number: 1 }),
]

function renderPanel({ canEdit = true, open = true } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  const onOpenChange = vi.fn()
  render(
    <QueryClientProvider client={client}>
      <DocHistoryPanel
        slug="demo"
        scope="project"
        path="guides/setup.md"
        open={open}
        onOpenChange={onOpenChange}
        canEdit={canEdit}
      />
    </QueryClientProvider>,
  )
  return { onOpenChange, invalidate }
}

beforeEach(() => {
  vi.mocked(docsApi.revisions).mockReset().mockResolvedValue(list(ITEMS))
  vi.mocked(docsApi.revision).mockReset()
  vi.mocked(docsApi.restore).mockReset()
  vi.mocked(toast.success).mockReset()
})

describe('DocHistoryPanel', () => {
  it('does not fetch while closed', () => {
    renderPanel({ open: false })
    expect(docsApi.revisions).not.toHaveBeenCalled()
  })

  it('lists every revision with its action, author, size and the current marker', async () => {
    renderPanel()
    expect(screen.getByText('Loading history…')).toBeInTheDocument()
    const rows = await screen.findAllByRole('listitem')
    expect(rows).toHaveLength(3)
    expect(docsApi.revisions).toHaveBeenCalledWith('demo', 'project', 'guides/setup.md', expect.anything())
    expect(rows[0]).toHaveTextContent('#3')
    expect(rows[0]).toHaveTextContent('Restored')
    expect(rows[0]).toHaveTextContent('from #1')
    expect(rows[0]).toHaveTextContent('Current')
    expect(rows[0]).toHaveTextContent('Restored revision 1')
    expect(rows[1]).toHaveTextContent('Moved')
    expect(rows[1]).toHaveTextContent('Unknown')
    expect(within(rows[1]!).getByText('guides/install.md')).toBeInTheDocument()
    expect(rows[1]).not.toHaveTextContent('Current')
    expect(rows[2]).toHaveTextContent('Created')
    expect(rows[2]).toHaveTextContent('2.0 KB')
  })

  it('shows a retryable error when the list fails', async () => {
    vi.mocked(docsApi.revisions).mockRejectedValueOnce(new ApiError('boom', 500))
    renderPanel()
    expect(await screen.findByText("Couldn't load the history")).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /try again|retry/i }))
    expect(await screen.findAllByRole('listitem')).toHaveLength(3)
  })

  it('opens a revision with its diff, tints the lines and goes back', async () => {
    vi.mocked(docsApi.revision).mockResolvedValue(
      detail({
        id: 'r-2',
        number: 2,
        action: 'update',
        message: 'Tweak',
        diff: '--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n context',
        diff_truncated: true,
      }),
    )
    renderPanel()
    fireEvent.click((await screen.findAllByRole('button', { name: /#2/ }))[0]!)
    expect(await screen.findByText('Changes from the previous revision')).toBeInTheDocument()
    expect(docsApi.revision).toHaveBeenCalledWith('demo', 'r-2', expect.anything())
    expect(screen.getByText('#2 · Edited')).toBeInTheDocument()
    expect(screen.getByText('Tweak')).toBeInTheDocument()
    expect(screen.getByText('The diff was too long and is cut short.')).toBeInTheDocument()
    const diff = screen.getByLabelText('Unified diff')
    expect(within(diff).getByText('+new')).toHaveClass('text-success')
    expect(within(diff).getByText('-old')).toHaveClass('text-danger')
    expect(within(diff).getByText('+++ b')).not.toHaveClass('text-success')
    expect(within(diff).getByText('@@ -1 +1 @@')).toHaveClass('text-fg-tertiary')

    fireEvent.click(screen.getByRole('button', { name: 'All revisions' }))
    expect(await screen.findAllByRole('listitem')).toHaveLength(3)
  })

  it('shows the full content of the first revision (no diff) and no Restore on the current one', async () => {
    vi.mocked(docsApi.revision).mockResolvedValue(detail({ id: 'r-3', number: 3, content: '# Setup body', author_name: null }))
    renderPanel()
    fireEvent.click((await screen.findAllByRole('button', { name: /#3/ }))[0]!)
    expect(await screen.findByText('Content')).toBeInTheDocument()
    expect(screen.getByText('# Setup body')).toBeInTheDocument()
    expect(screen.getByText(/by Unknown/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Restore' })).toBeNull()
  })

  it('shows an error when a revision cannot load', async () => {
    vi.mocked(docsApi.revision).mockRejectedValue(new ApiError('gone', 404))
    renderPanel()
    fireEvent.click((await screen.findAllByRole('button', { name: /#1/ }))[0]!)
    expect(await screen.findByText("Couldn't load this revision")).toBeInTheDocument()
  })

  it('hides Restore from a viewer', async () => {
    vi.mocked(docsApi.revision).mockResolvedValue(detail())
    renderPanel({ canEdit: false })
    fireEvent.click((await screen.findAllByRole('button', { name: /#1/ }))[0]!)
    await screen.findByText('Content')
    expect(screen.queryByRole('button', { name: 'Restore' })).toBeNull()
  })

  it('restores an older revision after confirmation and closes', async () => {
    vi.mocked(docsApi.revision).mockResolvedValue(detail())
    vi.mocked(docsApi.restore).mockResolvedValue({} as never)
    const { onOpenChange, invalidate } = renderPanel()
    fireEvent.click((await screen.findAllByRole('button', { name: /#1/ }))[0]!)
    fireEvent.click(await screen.findByRole('button', { name: 'Restore' }))
    const confirm = await screen.findByRole('alertdialog')
    expect(within(confirm).getByText('Restore revision #1?')).toBeInTheDocument()
    fireEvent.click(within(confirm).getByRole('button', { name: 'Restore' }))
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
    expect(docsApi.restore).toHaveBeenCalledWith('demo', 'r-1', 'Restored revision 1')
    expect(toast.success).toHaveBeenCalledWith('Restored revision #1')
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['docs', 'demo'] })
  })

  it('keeps the drawer open when the restore is cancelled', async () => {
    vi.mocked(docsApi.revision).mockResolvedValue(detail())
    const { onOpenChange } = renderPanel()
    fireEvent.click((await screen.findAllByRole('button', { name: /#1/ }))[0]!)
    fireEvent.click(await screen.findByRole('button', { name: 'Restore' }))
    const confirm = await screen.findByRole('alertdialog')
    fireEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(docsApi.restore).not.toHaveBeenCalled()
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('reports closing to the owner', async () => {
    const { onOpenChange } = renderPanel()
    await screen.findAllByRole('listitem')
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })
})

describe('DiffView', () => {
  it('keeps blank lines as rows', () => {
    render(<DiffView diff={'+a\n\n-b'} />)
    expect(screen.getByLabelText('Unified diff').querySelectorAll('span')).toHaveLength(3)
  })
})
