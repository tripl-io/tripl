import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import type { DocFileResponse, DocWriteResponse } from '@/types/docs'
import { DocEditor } from './DocEditor'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), info: vi.fn(), error: vi.fn() },
}))

// CodeMirror needs layout jsdom does not have; a textarea stands in for it.
vi.mock('@uiw/react-codemirror', () => ({
  default: ({
    value,
    onChange,
    'aria-label': ariaLabel,
  }: {
    value: string
    onChange: (v: string) => void
    'aria-label'?: string
  }) => <textarea aria-label={ariaLabel} value={value} onChange={e => onChange(e.target.value)} />,
  EditorView: { lineWrapping: [] },
}))
vi.mock('@codemirror/lang-markdown', () => ({ markdown: () => [] }))

vi.mock('@/api/docs', () => ({
  docsApi: { write: vi.fn(), read: vi.fn(), links: vi.fn() },
}))

import { docsApi } from '@/api/docs'

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
    updated_by_name: 'Editor',
    content: '---\ntitle: Setup\n---\n# Setup\n',
    body: '# Setup\n',
    extra_frontmatter: {},
    links: [],
    created_at: '2026-09-01T00:00:00Z',
    created_by_name: 'Editor',
    ...overrides,
  }
}

function saved(overrides: Partial<DocWriteResponse> = {}): DocWriteResponse {
  return { ...doc({ revision: 4 }), created: false, changed: true, warnings: [], ...overrides }
}

function renderEditor(onDone = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DocEditor slug="demo" doc={doc()} maxBytes={262144} onDone={onDone} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return onDone
}

function type(text: string) {
  fireEvent.change(screen.getByLabelText('Markdown source'), { target: { value: text } })
}

describe('DocEditor (F22)', () => {
  beforeEach(() => {
    vi.mocked(docsApi.write).mockReset()
    vi.mocked(docsApi.read).mockReset()
    vi.mocked(docsApi.links).mockReset()
    vi.mocked(docsApi.links).mockResolvedValue([])
  })

  it('saves against the revision it started from, with the message', async () => {
    vi.mocked(docsApi.write).mockResolvedValue(saved())
    const onDone = renderEditor()
    type('# Setup\n\nNew text\n')
    fireEvent.change(screen.getByLabelText('Revision message'), { target: { value: 'Explain setup' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onDone).toHaveBeenCalledWith(expect.objectContaining({ revision: 4 })))
    expect(docsApi.write).toHaveBeenCalledWith('demo', 'project', 'guides/setup.md', {
      content: '# Setup\n\nNew text\n',
      base_revision: 3,
      message: 'Explain setup',
    })
  })

  it('offers reload or overwrite on a 409, and overwrite saves on the latest revision', async () => {
    vi.mocked(docsApi.write)
      .mockRejectedValueOnce(new ApiError('Doc changed since revision 3', 409))
      .mockResolvedValueOnce(saved({ revision: 6 }))
    vi.mocked(docsApi.read).mockResolvedValue(doc({ revision: 5, content: 'theirs' }))
    const onDone = renderEditor()
    type('mine')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByRole('heading', { name: 'This note changed while you were editing' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Overwrite with mine' }))

    await waitFor(() => expect(onDone).toHaveBeenCalledWith(expect.objectContaining({ revision: 6 })))
    expect(vi.mocked(docsApi.write).mock.calls[1]?.[3]).toEqual({ content: 'mine', base_revision: 5, message: '' })
  })

  it('ignores Ctrl+S while the conflict dialog asks for a choice', async () => {
    vi.mocked(docsApi.write).mockRejectedValueOnce(new ApiError('Doc changed since revision 3', 409))
    renderEditor()
    type('mine')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('heading', { name: 'This note changed while you were editing' })).toBeInTheDocument()

    fireEvent.keyDown(window, { key: 's', ctrlKey: true })
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(docsApi.write).toHaveBeenCalledTimes(1)
  })

  it('loads their revision and drops the draft on "Load theirs"', async () => {
    vi.mocked(docsApi.write).mockRejectedValueOnce(new ApiError('Doc changed since revision 3', 409))
    vi.mocked(docsApi.read).mockResolvedValue(doc({ revision: 5, content: 'theirs' }))
    renderEditor()
    type('mine')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Load theirs' }))

    await waitFor(() => expect(screen.getByLabelText('Markdown source')).toHaveValue('theirs'))
  })

  it('shows a validation error in place', async () => {
    vi.mocked(docsApi.write).mockRejectedValue(new ApiError('Invalid frontmatter: audience must be human, agent or both', 422))
    renderEditor()
    type('---\naudience: robots\n---\n')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('audience must be human, agent or both')
  })

  it('previews the body without frontmatter and flags broken links', async () => {
    // Broken only for the draft's ref: the editor first resolves the loaded
    // note's refs (none) and the draft's only after the debounce, so a blanket
    // mock would show the warning before the draft was ever sent.
    const broken = {
      kind: 'event' as const,
      target: 'gone',
      qualifier: null,
      raw: '[[event:gone]]',
      status: 'broken' as const,
      route_path: null,
      entity_id: null,
      candidates: 0,
    }
    vi.mocked(docsApi.links).mockImplementation(async (_slug, refs) =>
      refs.includes('event:gone') ? [broken] : [],
    )
    renderEditor()
    type('---\ntitle: Hidden\n---\n# Visible\n\nSee [[event:gone]].\n')

    expect(await screen.findByText('1 link does not resolve')).toBeInTheDocument()
    expect(docsApi.links).toHaveBeenLastCalledWith('demo', ['event:gone'], expect.anything())
    const preview = screen.getByLabelText('Preview')
    expect(within(preview).getByRole('heading', { name: 'Visible' })).toBeInTheDocument()
    expect(within(preview).queryByText(/title: Hidden/)).toBeNull()
  })

  it('will not save a note over the size limit', () => {
    renderEditor()
    type('x'.repeat(262145))
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(screen.getByText(/too large to save/)).toBeInTheDocument()
  })
})
