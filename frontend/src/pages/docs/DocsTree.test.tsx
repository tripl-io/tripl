import { fireEvent, render, screen, within } from '@testing-library/react'
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

function actions(canDelete: (scope: DocScope) => boolean = () => true): FolderActions {
  return {
    onNewInFolder: vi.fn(),
    onMoveFolder: vi.fn(),
    onDeleteFolder: vi.fn(),
    canDeleteFolder: vi.fn(canDelete),
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

  it('hides folder delete where the user may not bulk-delete', () => {
    renderTree({ actions: actions(scope => scope === 'project') })
    expect(screen.getByRole('button', { name: 'Delete references/' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete warehouse/' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Rename or move warehouse/' })).toBeInTheDocument()
  })
})
