import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, RouterProvider, Routes, createMemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import { AuthContext } from '@/components/auth-context'
import { PersonaProject } from '@/test/PersonaProject'
import { personaAuth, type Persona } from '@/test/persona'
import type { DocFileResponse, DocSummary, DocTreeResponse } from '@/types/docs'
import DocsPage from './DocsPage'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), info: vi.fn(), error: vi.fn() },
}))
vi.mock('@uiw/react-codemirror', () => ({
  default: ({
    value,
    onChange,
    'aria-label': ariaLabel,
  }: {
    value: string
    onChange?: (next: string) => void
    'aria-label'?: string
  }) => <textarea aria-label={ariaLabel} value={value} onChange={e => onChange?.(e.target.value)} />,
  EditorView: { lineWrapping: [] },
}))
vi.mock('@codemirror/lang-markdown', () => ({ markdown: () => [] }))
vi.mock('@/api/docs', () => ({
  docsApi: {
    tree: vi.fn(),
    read: vi.fn(),
    write: vi.fn(),
    links: vi.fn(),
    search: vi.fn(),
    revisions: vi.fn(),
    revision: vi.fn(),
    remove: vi.fn(),
    removeFolder: vi.fn(),
    move: vi.fn(),
  },
}))

import { docsApi } from '@/api/docs'

function summary(overrides: Partial<DocSummary>): DocSummary {
  return {
    scope: 'project',
    path: 'guides/setup.md',
    title: 'Setup',
    description: '',
    tags: [],
    audience: 'both',
    revision: 1,
    size_bytes: 10,
    updated_at: '2026-09-01T00:00:00Z',
    updated_by_name: 'Editor',
    visibility: 'level',
    my_permission: 'edit',
    shared: false,
    ...overrides,
  }
}

function tree(overrides: Partial<DocTreeResponse> = {}): DocTreeResponse {
  return {
    project: { slug: 'demo', name: 'Demo' },
    organization: { id: 'o-1', slug: 'acme', name: 'Acme' },
    project_docs: [
      summary({ path: 'SKILL.md', title: 'Checkout skill', updated_at: '2026-09-03T00:00:00Z' }),
      summary({ path: 'references/queries.md', title: 'Event query recipes', updated_at: '2026-09-02T00:00:00Z' }),
    ],
    organization_docs: [
      summary({ scope: 'organization', path: 'warehouse/gotchas.md', title: 'Warehouse gotchas' }),
    ],
    limits: { max_file_bytes: 262144, max_files_per_scope: 5000, max_bundle_files: 2000, max_bundle_bytes: 20971520 },
    ...overrides,
  }
}

function file(overrides: Partial<DocFileResponse> = {}): DocFileResponse {
  return {
    ...summary({ path: 'references/queries.md', title: 'Event query recipes', tags: ['sql', 'funnel'], audience: 'agent' }),
    id: 'd-1',
    content: '---\ntitle: Event query recipes\n---\n# Recipes\n\nCount [[event:checkout_started]] and [[event:gone]].\n',
    body: '# Recipes\n\nCount [[event:checkout_started]] and [[event:gone]].\n',
    extra_frontmatter: { 'allowed-tools': ['Read'] },
    links: [
      {
        kind: 'event',
        target: 'checkout_started',
        qualifier: null,
        raw: '[[event:checkout_started]]',
        status: 'resolved',
        route_path: '/p/demo/monitoring/event/e-1',
        entity_id: 'e-1',
        candidates: 1,
      },
      {
        kind: 'event',
        target: 'gone',
        qualifier: null,
        raw: '[[event:gone]]',
        status: 'broken',
        route_path: null,
        entity_id: null,
        candidates: 0,
      },
    ],
    created_at: '2026-09-01T00:00:00Z',
    created_by_name: 'Editor',
    ...overrides,
  }
}

function LocationProbe() {
  const location = useLocation()
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>
}

function renderPage(url: string, persona: Persona = 'member') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <AuthContext.Provider value={personaAuth(persona)}>
        <PersonaProject persona={persona}>
          <MemoryRouter initialEntries={[url]}>
            <Routes>
              <Route path="/p/:slug/docs/:scope/*" element={<DocsPage />} />
              <Route path="/p/:slug/docs" element={<DocsPage />} />
            </Routes>
            <LocationProbe />
          </MemoryRouter>
        </PersonaProject>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

describe('DocsPage (F22)', () => {
  beforeEach(() => {
    vi.mocked(docsApi.tree).mockReset().mockResolvedValue(tree())
    vi.mocked(docsApi.read).mockReset().mockResolvedValue(file())
    vi.mocked(docsApi.write).mockReset()
    vi.mocked(docsApi.links).mockReset().mockResolvedValue([])
    vi.mocked(docsApi.search).mockReset().mockResolvedValue({ items: [], total: 0, truncated: false, semantic_used: false })
  })

  it('shows both roots in the tree and the recently updated notes', async () => {
    renderPage('/p/demo/docs')
    const nav = await screen.findByRole('navigation', { name: 'Docs' })
    expect(within(nav).getByRole('heading', { name: 'Project notes' })).toBeInTheDocument()
    expect(within(nav).getByRole('heading', { name: 'Organization notes · Acme' })).toBeInTheDocument()
    // Anchored: an editor also gets "New note in references/" and its siblings.
    const projectRoot = within(nav).getByRole('region', { name: 'Project notes' })
    expect(within(projectRoot).getByRole('button', { name: /^references/ })).toHaveAttribute('aria-expanded', 'true')
    expect(within(nav).getByRole('link', { name: 'Warehouse gotchas' })).toHaveAttribute(
      'href',
      '/p/demo/docs/organization/warehouse/gotchas.md',
    )
    const recent = screen.getByRole('region', { name: 'Recently updated' })
    expect(within(recent).getAllByRole('link')[0]).toHaveTextContent('Checkout skill')
    expect(screen.getByRole('button', { name: 'New note' })).toBeInTheDocument()
  })

  it('hides every write control from a viewer', async () => {
    renderPage('/p/demo/docs/project/references/queries.md', 'viewer')
    expect(await screen.findByRole('heading', { name: 'Event query recipes', level: 2 })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New note' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Delete note' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Export' })).toBeInTheDocument()
  })

  it('renders a note: chips, resolved links and the broken-link warning', async () => {
    renderPage('/p/demo/docs/project/references/queries.md')
    expect(await screen.findByRole('heading', { name: 'Event query recipes', level: 2 })).toBeInTheDocument()
    expect(docsApi.read).toHaveBeenCalledWith('demo', 'project', 'references/queries.md', expect.anything())
    expect(screen.getByText('For agents')).toBeInTheDocument()
    expect(screen.getByText('#sql')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'checkout_started' })).toHaveAttribute('href', '/p/demo/monitoring/event/e-1')
    expect(screen.getByText('1 link does not resolve')).toBeInTheDocument()
    expect(screen.getByText(/Other frontmatter \(1 key\)/)).toBeInTheDocument()
    // The open note is marked in the tree.
    const nav = screen.getByRole('navigation', { name: 'Docs' })
    expect(within(nav).getByRole('link', { name: 'Event query recipes' })).toHaveAttribute('aria-current', 'page')
  })

  it('says a note is gone rather than showing an error', async () => {
    vi.mocked(docsApi.read).mockRejectedValue(new ApiError('Doc not found', 404))
    renderPage('/p/demo/docs/project/missing.md')
    expect(await screen.findByText('Note not found')).toBeInTheDocument()
  })

  it('opens the editor on Edit', async () => {
    renderPage('/p/demo/docs/project/references/queries.md')
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    expect(await screen.findByLabelText('Markdown source')).toHaveValue(file().content)
    expect(screen.getByTestId('location')).toHaveTextContent('?edit=1')
  })

  it('opens quick open on Ctrl+P and goes to the picked note', async () => {
    renderPage('/p/demo/docs')
    await screen.findByRole('navigation', { name: 'Docs' })
    fireEvent.keyDown(window, { key: 'p', ctrlKey: true })
    const input = await screen.findByPlaceholderText('Open a note by title or path…')
    fireEvent.change(input, { target: { value: 'gotch' } })
    const option = await screen.findByRole('option', { name: /Warehouse gotchas/ })
    fireEvent.click(option)
    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent('/p/demo/docs/organization/warehouse/gotchas.md'),
    )
  })

  it('pre-fills a new note from "New note about this" and creates it create-only', async () => {
    vi.mocked(docsApi.write).mockResolvedValue({
      ...file({ path: 'checkout-notes.md', title: 'Checkout notes', revision: 1 }),
      created: true,
      changed: true,
      warnings: [],
    })
    renderPage(`/p/demo/docs?new=1&link=${encodeURIComponent('[[event:checkout_started]]')}`)
    const path = await screen.findByLabelText('Path')
    fireEvent.change(path, { target: { value: 'checkout-notes' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(docsApi.write).toHaveBeenCalled())
    const [slug, scope, notePath, body] = vi.mocked(docsApi.write).mock.calls[0]!
    expect([slug, scope, notePath]).toEqual(['demo', 'project', 'checkout-notes.md'])
    expect(body.create_only).toBe(true)
    expect(body.content).toContain('[[event:checkout_started]]')
    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent('/p/demo/docs/project/checkout-notes.md?edit=1'),
    )
  })

  it('asks before a click on the open note drops an unsaved draft (same path, no ?edit)', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const page = (
      <>
        <DocsPage />
        <LocationProbe />
      </>
    )
    const router = createMemoryRouter(
      [
        { path: '/p/:slug/docs/:scope/*', element: page },
        { path: '/p/:slug/docs', element: page },
      ],
      { initialEntries: ['/p/demo/docs/project/references/queries.md?edit=1'] },
    )
    render(
      <QueryClientProvider client={client}>
        <AuthContext.Provider value={personaAuth('member')}>
          <RouterProvider router={router} />
        </AuthContext.Provider>
      </QueryClientProvider>,
    )
    const source = await screen.findByLabelText('Markdown source')
    fireEvent.change(source, { target: { value: 'my unsaved draft' } })
    const nav = screen.getByRole('navigation', { name: 'Docs' })
    fireEvent.click(within(nav).getByRole('link', { name: 'Event query recipes' }))

    expect(await screen.findByText('Leave without saving?')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(screen.queryByText('Leave without saving?')).toBeNull())
    expect(screen.getByLabelText('Markdown source')).toHaveValue('my unsaved draft')
    expect(screen.getByTestId('location')).toHaveTextContent('?edit=1')
  })
})

describe('DocsPage (F22) states and file actions', () => {
  beforeEach(() => {
    vi.mocked(docsApi.tree).mockReset().mockResolvedValue(tree())
    vi.mocked(docsApi.read).mockReset().mockResolvedValue(file())
    vi.mocked(docsApi.links).mockReset().mockResolvedValue([])
    vi.mocked(docsApi.revisions).mockReset().mockResolvedValue({
      scope: 'project',
      path: 'references/queries.md',
      current_revision: 1,
      items: [],
    })
    vi.mocked(docsApi.remove).mockReset()
    vi.mocked(docsApi.removeFolder).mockReset()
    vi.mocked(docsApi.move).mockReset()
    vi.mocked(toast.success).mockReset()
  })

  it('offers a retry when the tree fails to load', async () => {
    vi.mocked(docsApi.tree).mockRejectedValueOnce(new ApiError('boom', 500))
    renderPage('/p/demo/docs')
    expect(await screen.findByText("Couldn't load the docs")).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('navigation', { name: 'Docs' })).toBeInTheDocument()
  })

  it('invites an editor to write or import the first note', async () => {
    vi.mocked(docsApi.tree).mockResolvedValue(tree({ project_docs: [], organization_docs: [] }))
    renderPage('/p/demo/docs')
    expect(await screen.findByText('No notes yet')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Import a folder' }))
    expect(await screen.findByRole('dialog', { name: 'Import and export notes' })).toBeInTheDocument()
  })

  it('shows a reader the empty state without write actions', async () => {
    vi.mocked(docsApi.tree).mockResolvedValue(tree({ project_docs: [], organization_docs: [] }))
    renderPage('/p/demo/docs', 'viewer')
    expect(await screen.findByText('No notes yet')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Import a folder' })).toBeNull()
  })

  it('opens the new-note dialog from the empty state', async () => {
    vi.mocked(docsApi.tree).mockResolvedValue(tree({ project_docs: [], organization_docs: [] }))
    renderPage('/p/demo/docs')
    await screen.findByText('No notes yet')
    const newButtons = screen.getAllByRole('button', { name: 'New note' })
    fireEvent.click(newButtons[newButtons.length - 1]!)
    expect(await screen.findByLabelText('Path')).toHaveValue('')
  })

  it('lists recent notes with their organization, author and description', async () => {
    vi.mocked(docsApi.tree).mockResolvedValue(
      tree({
        project_docs: [summary({ path: 'a.md', title: 'Alpha', description: 'What alpha is', updated_by_name: null })],
      }),
    )
    renderPage('/p/demo/docs')
    const recent = await screen.findByRole('region', { name: 'Recently updated' })
    expect(within(recent).getByText('What alpha is')).toBeInTheDocument()
    expect(within(recent).getByText(/^Acme · warehouse\/gotchas\.md/)).toHaveTextContent('· Editor')
  })

  it('says an unknown scope in the URL is not a notes root', async () => {
    renderPage('/p/demo/docs/team/a.md')
    expect(await screen.findByText('Unknown notes scope')).toBeInTheDocument()
  })

  it('names the organization in a missing organization note', async () => {
    vi.mocked(docsApi.read).mockRejectedValue(new ApiError('Doc not found', 404))
    renderPage('/p/demo/docs/organization/missing.md')
    expect(await screen.findByText(/no organization note at missing\.md/)).toBeInTheDocument()
  })

  it('offers a retry when a note fails for another reason', async () => {
    vi.mocked(docsApi.read).mockRejectedValueOnce(new ApiError('boom', 500))
    renderPage('/p/demo/docs/project/references/queries.md')
    expect(await screen.findByText("Couldn't load this note")).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('heading', { name: 'Event query recipes', level: 2 })).toBeInTheDocument()
  })

  it('deletes the open note after confirmation and returns to the index', async () => {
    vi.mocked(docsApi.remove).mockResolvedValue(undefined)
    renderPage('/p/demo/docs/project/references/queries.md')
    fireEvent.click(await screen.findByRole('button', { name: 'Delete note' }))
    const confirm = await screen.findByRole('alertdialog')
    expect(within(confirm).getByText('Delete this note?')).toBeInTheDocument()
    fireEvent.click(within(confirm).getByRole('button', { name: 'Delete note' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/p\/demo\/docs$/))
    expect(docsApi.remove).toHaveBeenCalledWith('demo', 'project', 'references/queries.md')
    expect(toast.success).toHaveBeenCalledWith('Deleted references/queries.md')
  })

  it('deletes the folder holding the open note and returns to the index', async () => {
    vi.mocked(docsApi.removeFolder).mockResolvedValue({ deleted: ['references/queries.md'] })
    renderPage('/p/demo/docs/project/references/queries.md')
    await screen.findByRole('heading', { name: 'Event query recipes', level: 2 })
    fireEvent.click(screen.getByRole('button', { name: 'Delete references/' }))
    const confirm = await screen.findByRole('alertdialog')
    expect(within(confirm).getByText('All 1 note under this folder are deleted with their history.')).toBeInTheDocument()
    fireEvent.click(within(confirm).getByRole('button', { name: 'Delete folder' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/p\/demo\/docs$/))
    expect(docsApi.removeFolder).toHaveBeenCalledWith('demo', 'project', 'references/')
    expect(toast.success).toHaveBeenCalledWith('Deleted references/')
  })

  it('makes the user type the folder name before deleting a large folder', async () => {
    const many = Array.from({ length: 6 }, (_, i) => summary({ path: `big/n${i}.md`, title: `N${i}` }))
    vi.mocked(docsApi.tree).mockResolvedValue(tree({ project_docs: many }))
    vi.mocked(docsApi.removeFolder).mockResolvedValue({ deleted: [] })
    renderPage('/p/demo/docs')
    fireEvent.click(await screen.findByRole('button', { name: 'Delete big/' }))
    const confirm = await screen.findByRole('alertdialog')
    const submit = within(confirm).getByRole('button', { name: 'Delete folder' })
    expect(submit).toBeDisabled()
    fireEvent.change(within(confirm).getByLabelText(/to confirm/), { target: { value: 'big/' } })
    fireEvent.click(submit)
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Deleted big/'))
    // Not under the open location: the page stays put.
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/p\/demo\/docs$/)
  })

  it('lets only an organization owner or admin change organization notes', async () => {
    renderPage('/p/demo/docs')
    await screen.findByRole('navigation', { name: 'Docs' })
    // A project editor who is a plain organization member: project folders only.
    expect(screen.getByRole('button', { name: 'Delete references/' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete warehouse/' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Rename or move warehouse/' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'New organization note' })).toBeNull()
  })

  it('hides Edit on an organization note from a member and shows it to an admin', async () => {
    vi.mocked(docsApi.read).mockResolvedValue(
      file({ scope: 'organization', path: 'warehouse/gotchas.md', title: 'Warehouse gotchas' }),
    )
    const member = renderPage('/p/demo/docs/organization/warehouse/gotchas.md')
    expect(await screen.findByRole('heading', { name: 'Warehouse gotchas', level: 2 })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
    member.unmount()

    renderPage('/p/demo/docs/organization/warehouse/gotchas.md', 'admin')
    expect(await screen.findByRole('heading', { name: 'Warehouse gotchas', level: 2 })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete warehouse/' })).toBeInTheDocument()
  })

  it('renames the open note and follows it', async () => {
    vi.mocked(docsApi.move).mockResolvedValue({
      moved: [{ from_path: 'references/queries.md', to_path: 'references/recipes.md' }],
    })
    renderPage('/p/demo/docs/project/references/queries.md')
    fireEvent.click(await screen.findByRole('button', { name: 'Rename or move' }))
    fireEvent.change(await screen.findByLabelText('New path'), { target: { value: 'references/recipes' } })
    fireEvent.click(screen.getByRole('button', { name: 'Move' }))
    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent('/p/demo/docs/project/references/recipes.md'),
    )
    expect(toast.success).toHaveBeenCalledWith('Moved to references/recipes.md')
  })

  it('moves a folder that does not hold the open note without navigating', async () => {
    vi.mocked(docsApi.move).mockResolvedValue({
      moved: [
        { from_path: 'warehouse/gotchas.md', to_path: 'wh/gotchas.md' },
        { from_path: 'warehouse/b.md', to_path: 'wh/b.md' },
      ],
    })
    renderPage('/p/demo/docs/project/references/queries.md', 'owner')
    await screen.findByRole('heading', { name: 'Event query recipes', level: 2 })
    fireEvent.click(screen.getByRole('button', { name: 'Rename or move warehouse/' }))
    fireEvent.change(await screen.findByLabelText('New folder path'), { target: { value: 'wh' } })
    fireEvent.click(screen.getByRole('button', { name: 'Move' }))
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Moved 2 notes'))
    expect(screen.getByTestId('location')).toHaveTextContent('/p/demo/docs/project/references/queries.md')
  })

  it('opens the history drawer for the open note', async () => {
    renderPage('/p/demo/docs/project/references/queries.md')
    fireEvent.click(await screen.findByRole('button', { name: 'History' }))
    expect(await screen.findByRole('dialog', { name: 'History' })).toBeInTheDocument()
    await waitFor(() => expect(docsApi.revisions).toHaveBeenCalled())
  })

  it('starts a new note in a folder from the tree', async () => {
    renderPage('/p/demo/docs')
    fireEvent.click(await screen.findByRole('button', { name: 'New note in references/' }))
    expect(await screen.findByLabelText('Path')).toHaveValue('references/')
  })

  it('drops ?new and ?link when the linked new-note dialog is cancelled', async () => {
    renderPage(`/p/demo/docs?new=1&link=${encodeURIComponent('[[event:a]]')}`)
    await screen.findByLabelText('Path')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/p\/demo\/docs$/))
  })

  it('opens quick open from the header button', async () => {
    renderPage('/p/demo/docs')
    fireEvent.click(await screen.findByRole('button', { name: /Open note/ }))
    expect(await screen.findByPlaceholderText('Open a note by title or path…')).toBeInTheDocument()
  })
})
