import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { toast } from 'sonner'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DocFileResponse, DocLinkResolution } from '@/types/docs'
import { BrokenLinksBanner, DocView } from './DocView'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), info: vi.fn(), error: vi.fn() },
}))

function doc(overrides: Partial<DocFileResponse> = {}): DocFileResponse {
  return {
    id: 'd-1',
    scope: 'project',
    path: 'guides/setup.md',
    title: 'Setup',
    description: '',
    tags: [],
    audience: 'both',
    revision: 3,
    size_bytes: 20,
    updated_at: '2026-09-01T00:00:00Z',
    updated_by_name: null,
    visibility: 'level',
    my_permission: 'edit',
    shared: false,
    content: '# Setup\n',
    body: 'Body text\n',
    extra_frontmatter: {},
    links: [],
    created_at: '2026-09-01T00:00:00Z',
    created_by_name: null,
    ...overrides,
  }
}

function renderView(d: DocFileResponse, canEdit = true) {
  const handlers = { onEdit: vi.fn(), onHistory: vi.fn(), onMove: vi.fn(), onDelete: vi.fn() }
  render(
    <MemoryRouter>
      <DocView slug="demo" doc={d} canEdit={canEdit} {...handlers} />
    </MemoryRouter>,
  )
  return handlers
}

const writeText = vi.fn()

beforeEach(() => {
  vi.mocked(toast.success).mockReset()
  writeText.mockReset()
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
})

afterEach(() => {
  Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
})

describe('DocView', () => {
  it('labels an organization note for people, with description and plural extra keys', () => {
    renderView(
      doc({
        scope: 'organization',
        audience: 'human',
        description: 'How to set up',
        extra_frontmatter: { a: 1, b: 2 },
      }),
    )
    expect(screen.getByText('Organization note')).toBeInTheDocument()
    expect(screen.getByText('For people')).toBeInTheDocument()
    expect(screen.getByText('How to set up')).toBeInTheDocument()
    expect(screen.getByText('Other frontmatter (2 keys)')).toBeInTheDocument()
    expect(screen.getByText(/Revision 3/)).not.toHaveTextContent(' by ')
  })

  it('wires the write actions for an editor', () => {
    const h = renderView(doc({ audience: 'both' }))
    expect(screen.getByText('For people and agents')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'History' }))
    fireEvent.click(screen.getByRole('button', { name: 'Rename or move' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete note' }))
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    expect(h.onHistory).toHaveBeenCalled()
    expect(h.onMove).toHaveBeenCalled()
    expect(h.onDelete).toHaveBeenCalled()
    expect(h.onEdit).toHaveBeenCalled()
  })

  it('copies the path and confirms it', async () => {
    writeText.mockResolvedValue(undefined)
    renderView(doc())
    fireEvent.click(screen.getByRole('button', { name: 'Copy path' }))
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Path copied'))
    expect(writeText).toHaveBeenCalledWith('guides/setup.md')
  })

  it('says nothing when the clipboard refuses', async () => {
    writeText.mockRejectedValue(new Error('denied'))
    renderView(doc())
    fireEvent.click(screen.getByRole('button', { name: 'Copy path' }))
    await waitFor(() => expect(writeText).toHaveBeenCalled())
    await Promise.resolve()
    expect(toast.success).not.toHaveBeenCalled()
  })

  it('lists ambiguous links as not resolving too', () => {
    const ambiguous: DocLinkResolution = {
      kind: 'event',
      target: 'dup',
      qualifier: null,
      raw: '[[event:dup]]',
      status: 'ambiguous',
      route_path: '/p/demo/monitoring/event/e-1',
      entity_id: null,
      candidates: 2,
    }
    renderView(doc({ links: [ambiguous, { ...ambiguous, target: 'dup2', raw: '[[event:dup2]]' }] }))
    expect(screen.getByText('2 links do not resolve')).toBeInTheDocument()
  })
})

describe('BrokenLinksBanner', () => {
  it('shows the first 20 and counts the rest', () => {
    render(<BrokenLinksBanner items={Array.from({ length: 23 }, (_, i) => `link ${i}`)} />)
    expect(screen.getByText('23 links do not resolve')).toBeInTheDocument()
    expect(screen.getByText('link 19')).toBeInTheDocument()
    expect(screen.queryByText('link 20')).toBeNull()
    expect(screen.getByText('…and 3 more')).toBeInTheDocument()
  })
})

describe('DocView sharing (F24)', () => {
  it('says nothing for a note everyone in the project reads', () => {
    renderView(doc())
    expect(screen.queryByText(/Only the author/)).toBeNull()
    expect(screen.queryByText(/Shared with specific people/)).toBeNull()
    expect(screen.queryByText('View only')).toBeNull()
  })

  it('marks a private note and a shared view-only note', () => {
    renderView(doc({ visibility: 'private' }))
    expect(screen.getByText('Only the author')).toBeInTheDocument()
  })

  it('marks a note shared with the reader to view only', () => {
    renderView(doc({ visibility: 'restricted', shared: true, my_permission: 'view' }), false)
    expect(screen.getByText('Shared with specific people · view only')).toBeInTheDocument()
  })

  it('tells an org admin that a break-glass read was audited', () => {
    renderView(doc({ visibility: 'private', my_permission: 'view', break_glass: true }), false)
    expect(screen.getByText('This note is not shared with you')).toBeInTheDocument()
    expect(screen.getByText(/recorded in the audit log/)).toBeInTheDocument()
  })

  it('shows no break-glass notice on an ordinary read', () => {
    renderView(doc())
    expect(screen.queryByText('This note is not shared with you')).not.toBeInTheDocument()
  })

  it('opens the Share dialog', () => {
    const onShare = vi.fn()
    render(
      <MemoryRouter>
        <DocView
          slug="demo"
          doc={doc()}
          canEdit={false}
          onEdit={vi.fn()}
          onHistory={vi.fn()}
          onMove={vi.fn()}
          onDelete={vi.fn()}
          onShare={onShare}
        />
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    expect(onShare).toHaveBeenCalled()
  })
})
