import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { DocScope, DocSummary, DocTreeResponse } from '@/types/docs'
import { DocsTree, type FolderActions } from './DocsTree'

function summary(path: string, title: string, scope: DocScope = 'project'): DocSummary {
  return {
    scope,
    path,
    title,
    description: '',
    tags: [],
    audience: 'both',
    revision: 1,
    size_bytes: 10,
    updated_at: '2026-09-01T00:00:00Z',
    updated_by_name: null,
    visibility: 'level',
    my_permission: 'edit',
    shared: false,
  }
}

function tree(overrides: Partial<DocTreeResponse> = {}): DocTreeResponse {
  return {
    project: { slug: 'demo', name: 'Demo' },
    organization: { id: 'o-1', slug: 'acme', name: 'Acme' },
    project_docs: [
      summary('SKILL.md', 'Checkout skill'),
      summary('references/queries.md', 'Event query recipes'),
      summary('references/deep/notes.md', ''),
    ],
    organization_docs: [summary('warehouse/gotchas.md', 'Warehouse gotchas', 'organization')],
    limits: { max_file_bytes: 1, max_files_per_scope: 1, max_bundle_files: 1, max_bundle_bytes: 1 },
    ...overrides,
  }
}

function actions(canEdit: (scope: DocScope) => boolean = () => true): FolderActions {
  return {
    onNewInFolder: vi.fn(),
    onMoveFolder: vi.fn(),
    onDeleteFolder: vi.fn(),
    canEditScope: vi.fn(canEdit),
  }
}

function renderTree(props: Partial<Parameters<typeof DocsTree>[0]> = {}) {
  const view = render(
    <MemoryRouter>
      <DocsTree slug="demo" tree={tree()} active={null} {...props} />
    </MemoryRouter>,
  )
  const rerender = (next: Partial<Parameters<typeof DocsTree>[0]>) =>
    view.rerender(
      <MemoryRouter>
        <DocsTree slug="demo" tree={tree()} active={null} {...props} {...next} />
      </MemoryRouter>,
    )
  return { rerender }
}

const projectRoot = () => screen.getByRole('region', { name: 'Project notes' })
const folder = (name: RegExp) => within(projectRoot()).getByRole('button', { name })

describe('DocsTree', () => {
  it('builds folders from the flat paths and falls back to the file name without a title', () => {
    renderTree()
    expect(folder(/^references/)).toHaveAttribute('aria-expanded', 'true')
    expect(within(projectRoot()).getByRole('link', { name: 'notes.md' })).toHaveAttribute(
      'href',
      '/p/demo/docs/project/references/deep/notes.md',
    )
    // A reader gets no folder actions.
    expect(screen.queryByRole('button', { name: 'New project note' })).toBeNull()
  })

  it('collapses and re-expands a folder', () => {
    renderTree()
    fireEvent.click(folder(/^references/))
    expect(folder(/^references/)).toHaveAttribute('aria-expanded', 'false')
    expect(within(projectRoot()).queryByRole('link', { name: 'Event query recipes' })).toBeNull()
    fireEvent.click(folder(/^references/))
    expect(within(projectRoot()).getByRole('link', { name: 'Event query recipes' })).toBeInTheDocument()
  })

  it('opens the folders of a newly opened note that the user had collapsed', () => {
    const { rerender } = renderTree()
    fireEvent.click(folder(/^references/))
    expect(folder(/^references/)).toHaveAttribute('aria-expanded', 'false')
    rerender({ active: { scope: 'project', path: 'references/queries.md' } })
    expect(folder(/^references/)).toHaveAttribute('aria-expanded', 'true')
    // The open-note marker matches case-insensitively (paths are).
    rerender({ active: { scope: 'project', path: 'References/Queries.md' } })
    expect(within(projectRoot()).getByRole('link', { name: 'Event query recipes' })).toHaveAttribute(
      'aria-current',
      'page',
    )
  })

  it('filters by title or path, keeps folders open and says when nothing matches', () => {
    renderTree()
    fireEvent.click(folder(/^references/))
    fireEvent.change(screen.getByLabelText('Filter notes'), { target: { value: 'recipes' } })
    const refs = folder(/^references/)
    expect(refs).toHaveAttribute('aria-expanded', 'true')
    expect(refs).toHaveAttribute('aria-disabled', 'true')
    expect(refs).toHaveAttribute('title', 'Clear the filter to collapse folders')
    // Locked while filtering.
    fireEvent.click(refs)
    expect(folder(/^references/)).toHaveAttribute('aria-expanded', 'true')
    expect(within(projectRoot()).queryByRole('link', { name: 'Checkout skill' })).toBeNull()
    expect(within(screen.getByRole('region', { name: 'Organization notes · Acme' })).getByText('No match.')).toBeInTheDocument()
  })

  it('says an empty root has no notes yet', () => {
    renderTree({ tree: tree({ organization_docs: [] }) })
    expect(within(screen.getByRole('region', { name: 'Organization notes · Acme' })).getByText('No notes yet.')).toBeInTheDocument()
  })

  it('gives an editor root and folder actions', () => {
    const a = actions()
    renderTree({ actions: a })
    fireEvent.click(screen.getByRole('button', { name: 'New project note' }))
    expect(a.onNewInFolder).toHaveBeenCalledWith('project', '')
    fireEvent.click(screen.getByRole('button', { name: 'New organization note' }))
    expect(a.onNewInFolder).toHaveBeenCalledWith('organization', '')
    fireEvent.click(screen.getByRole('button', { name: 'New note in references/' }))
    expect(a.onNewInFolder).toHaveBeenCalledWith('project', 'references/')
    fireEvent.click(screen.getByRole('button', { name: 'Rename or move references/' }))
    expect(a.onMoveFolder).toHaveBeenCalledWith('project', 'references/')
    fireEvent.click(screen.getByRole('button', { name: 'Delete references/' }))
    expect(a.onDeleteFolder).toHaveBeenCalledWith('project', 'references/', 2)
  })

  it('hides organization folder actions from a project editor who is not an org owner or admin', () => {
    renderTree({ actions: actions(scope => scope === 'project') })
    expect(screen.getByRole('button', { name: 'Delete references/' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New project note' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete warehouse/' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Rename or move warehouse/' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'New organization note' })).toBeNull()
  })
})

describe('DocsTree sharing (F24)', () => {
  it('marks private and shared notes, and nothing for the default', () => {
    render(
      <MemoryRouter>
        <DocsTree
          slug="demo"
          tree={tree({
            project_docs: [
              { ...summary('mine.md', 'Mine'), visibility: 'private' },
              { ...summary('team.md', 'Team'), visibility: 'restricted', shared: true },
              summary('open.md', 'Open'),
            ],
          })}
          active={null}
        />
      </MemoryRouter>,
    )
    const root = projectRoot()
    expect(within(root).getByRole('link', { name: 'Mine (only the author)' })).toBeInTheDocument()
    expect(within(root).getByRole('link', { name: 'Team (shared with specific people)' })).toBeInTheDocument()
    expect(within(root).getByRole('link', { name: 'Open' })).toBeInTheDocument()
  })

  it('offers folder sharing to an editor when the page wires it', () => {
    const a = { ...actions(), onShareFolder: vi.fn() }
    renderTree({ actions: a })
    fireEvent.click(within(projectRoot()).getByRole('button', { name: 'Share references/' }))
    expect(a.onShareFolder).toHaveBeenCalledWith('project', 'references/')
  })

  it('hides folder sharing when the page does not wire it', () => {
    renderTree({ actions: actions() })
    expect(screen.queryByRole('button', { name: 'Share references/' })).toBeNull()
  })
})

describe('DocsTree drag and drop', () => {
  // jsdom lays nothing out: give the drop targets boxes, nested like the real
  // tree (the project root holds `references/`, which holds `deep/`), and drive
  // dnd-kit's pointer sensor with coordinates.
  function layout() {
    const box = (el: Element, top: number, height: number) =>
      vi.spyOn(el, 'getBoundingClientRect').mockReturnValue({
        x: 0, y: top, top, left: 0, width: 200, height, right: 200, bottom: top + height, toJSON: () => ({}),
      } as DOMRect)
    box(projectRoot(), 0, 400)
    box(folder(/^references/).closest('li')!, 40, 200)
    box(folder(/^deep/).closest('li')!, 80, 60)
    box(screen.getByRole('region', { name: /Organization notes/ }), 500, 100)
  }

  async function drag(source: Element, to: { x: number; y: number }) {
    fireEvent.pointerDown(source, { clientX: 5, clientY: 5, button: 0, isPrimary: true })
    fireEvent.pointerMove(document, { clientX: to.x, clientY: to.y })
    fireEvent.pointerMove(document, { clientX: to.x + 1, clientY: to.y + 1 })
    fireEvent.pointerUp(document, { clientX: to.x + 1, clientY: to.y + 1 })
    // The drag overlay settles on the next frame.
    await act(() => new Promise(resolve => setTimeout(resolve, 20)))
  }

  it('moves a note into the innermost folder under the pointer', async () => {
    const onDropMove = vi.fn()
    renderTree({ actions: { ...actions(), onDropMove } })
    layout()
    await drag(within(projectRoot()).getByRole('link', { name: 'Checkout skill' }), { x: 50, y: 100 })
    expect(onDropMove).toHaveBeenCalledWith({ scope: 'project', from: 'SKILL.md', to: 'references/deep/SKILL.md', folder: false })
  })

  it('moves a folder to the top level', async () => {
    const onDropMove = vi.fn()
    renderTree({ actions: { ...actions(), onDropMove } })
    layout()
    await drag(folder(/^deep/), { x: 50, y: 300 })
    expect(onDropMove).toHaveBeenCalledWith({ scope: 'project', from: 'references/deep/', to: 'deep/', folder: true })
  })

  it('ignores a drop into another scope, into itself, or where the item already is', async () => {
    const onDropMove = vi.fn()
    renderTree({ actions: { ...actions(), onDropMove } })
    layout()
    await drag(within(projectRoot()).getByRole('link', { name: 'Checkout skill' }), { x: 50, y: 550 })
    await drag(folder(/^references/), { x: 50, y: 100 })
    await drag(within(projectRoot()).getByRole('link', { name: 'Event query recipes' }), { x: 50, y: 50 })
    expect(onDropMove).not.toHaveBeenCalled()
  })

  it('does not drag for a scope the viewer cannot edit', async () => {
    const onDropMove = vi.fn()
    renderTree({ actions: { ...actions(scope => scope === 'organization'), onDropMove } })
    layout()
    await drag(within(projectRoot()).getByRole('link', { name: 'Checkout skill' }), { x: 50, y: 100 })
    expect(onDropMove).not.toHaveBeenCalled()
  })
})
