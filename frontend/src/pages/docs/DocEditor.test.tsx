import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
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

// The picker's CodeMirror half is replaced: the test drives the bridge the
// editor hands it, the way the real extension would on typing and keys.
const pickerBridge = vi.hoisted(() => ({ get: null as null | (() => LinkPickerBridge) }))
vi.mock('./docLinkEditorExtension', async importOriginal => ({
  LinkPickerBridgeBox: (await importOriginal<typeof import('./docLinkEditorExtension')>())
    .LinkPickerBridgeBox,
  docLinkPickerExtension: (box: { get: () => LinkPickerBridge }) => {
    pickerBridge.get = () => box.get()
    return []
  },
  insertPick: vi.fn(),
  measureTrigger: vi.fn(),
}))

vi.mock('@/api/docs', () => ({
  docsApi: { write: vi.fn(), writeTranslation: vi.fn(), read: vi.fn(), links: vi.fn(), linkSuggestions: vi.fn() },
}))

import { docsApi } from '@/api/docs'
import type { LinkTrigger } from '@/lib/docLinkTrigger'
import type { LinkPickerBridge } from './docLinkEditorExtension'

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
    visibility: 'level',
    my_permission: 'edit',
    shared: false,
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
    vi.mocked(docsApi.writeTranslation).mockReset()
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

  it('edits a translation: saves it against its own revision, without a message', async () => {
    vi.mocked(docsApi.writeTranslation)
      .mockRejectedValueOnce(new ApiError('The translation changed concurrently', 409))
      .mockResolvedValueOnce({ lang: 'de', revision: 5 } as never)
    vi.mocked(docsApi.read).mockResolvedValue(
      doc({ revision: 9, lang: 'de', content: 'theirs', translations: [{ lang: 'de', revision: 4 } as never] }),
    )
    const onDone = vi.fn()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <DocEditor
            slug="demo"
            doc={doc({ lang: 'de', content: '# Einrichtung\n' })}
            maxBytes={262144}
            onDone={onDone}
            translation={{ lang: 'de', revision: 2 }}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(screen.getByRole('heading', { name: /Editing the German translation of/ })).toBeInTheDocument()
    expect(screen.queryByLabelText('Revision message')).toBeNull()
    type('mine')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('heading', { name: 'This translation changed while you were editing' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Overwrite with mine' }))

    await waitFor(() => expect(onDone).toHaveBeenCalledWith(null))
    expect(docsApi.read).toHaveBeenCalledWith('demo', 'project', 'guides/setup.md', 'de')
    expect(vi.mocked(docsApi.writeTranslation).mock.calls[1]?.[1]).toEqual({
      scope: 'project',
      path: 'guides/setup.md',
      lang: 'de',
      content: 'mine',
      base_revision: 4,
    })
    expect(docsApi.write).not.toHaveBeenCalled()
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

  it('saves on Ctrl+S and passes on the save warnings', async () => {
    vi.mocked(toast.warning).mockReset()
    vi.mocked(docsApi.write).mockResolvedValue(saved({ warnings: ['[[event:gone]] does not resolve'] }))
    const onDone = renderEditor()
    type('changed')
    fireEvent.keyDown(window, { key: 'S', metaKey: true })
    await waitFor(() => expect(onDone).toHaveBeenCalled())
    expect(toast.success).toHaveBeenCalledWith('Saved revision 4')
    expect(toast.warning).toHaveBeenCalledWith('[[event:gone]] does not resolve')
  })

  it('counts several warnings and says when nothing changed', async () => {
    vi.mocked(toast.warning).mockReset()
    vi.mocked(docsApi.write).mockResolvedValue(saved({ changed: false, warnings: ['a', 'b'] }))
    const onDone = renderEditor()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onDone).toHaveBeenCalled())
    expect(toast.success).toHaveBeenCalledWith('No changes to save')
    expect(toast.warning).toHaveBeenCalledWith('2 links do not resolve')
  })

  it('cancels a clean draft at once', () => {
    const onDone = renderEditor()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onDone).toHaveBeenCalledWith(null)
  })

  it('keeps the draft when the conflict dialog is dismissed', async () => {
    vi.mocked(docsApi.write).mockRejectedValueOnce(new ApiError('Doc changed', 409))
    renderEditor()
    type('mine')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Keep editing' }))
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'This note changed while you were editing' })).toBeNull(),
    )
    expect(screen.getByLabelText('Markdown source')).toHaveValue('mine')
  })

  it('reports a failed reload of their revision', async () => {
    vi.mocked(docsApi.write).mockRejectedValueOnce(new ApiError('Doc changed', 409))
    vi.mocked(docsApi.read).mockRejectedValue(new ApiError('Network down', 503))
    renderEditor()
    type('mine')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Load theirs' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Network down')
    expect(screen.getByLabelText('Markdown source')).toHaveValue('mine')
  })

  it('reports a failed overwrite without saving', async () => {
    vi.mocked(docsApi.write).mockRejectedValueOnce(new ApiError('Doc changed', 409))
    vi.mocked(docsApi.read).mockRejectedValue(new ApiError('Doc not found', 404))
    renderEditor()
    type('mine')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Overwrite with mine' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Doc not found')
    expect(docsApi.write).toHaveBeenCalledTimes(1)
  })
})

describe('DocEditor links and mentions (F24)', () => {
  const fakeView = {} as Parameters<LinkPickerBridge['onTrigger']>[1]

  function openPicker(trigger: LinkTrigger) {
    act(() => {
      pickerBridge.get?.().onTrigger(trigger, fakeView)
    })
  }

  function press(key: string): boolean {
    let used = false
    act(() => {
      used = pickerBridge.get?.().onKey(key) ?? false
    })
    return used
  }

  beforeEach(() => {
    vi.mocked(docsApi.links).mockReset()
    vi.mocked(docsApi.links).mockResolvedValue([])
    vi.mocked(docsApi.linkSuggestions).mockReset()
    vi.mocked(docsApi.linkSuggestions).mockResolvedValue({
      items: [
        { kind: 'metric', id: 'm-1', label: 'signup_rate', detail: 'Signup rate', insert: '[[metric:signup_rate]]' },
        { kind: 'metric', id: 'm-2', label: 'signup_count', detail: '', insert: '[[metric:signup_count]]' },
      ],
    })
  })

  it('opens the picker on [[kind:, narrows the request and inserts the picked reference', async () => {
    renderEditor()
    type('See [[metric:sig]]')
    openPicker({ mode: 'link', from: 4, to: 16, kind: 'metric', query: 'sig' })

    const listbox = await screen.findByRole('listbox', { name: 'Link to a metric' })
    await waitFor(() => expect(within(listbox).getAllByRole('option')).toHaveLength(2))
    // Debounced: the typed query goes out once typing pauses.
    await waitFor(() =>
      expect(docsApi.linkSuggestions).toHaveBeenLastCalledWith(
        'demo',
        { q: 'sig', kind: 'metric', limit: 8 },
        expect.anything(),
      ),
    )
    expect(within(listbox).getAllByRole('option')[0]).toHaveAttribute('aria-selected', 'true')

    expect(press('ArrowDown')).toBe(true)
    expect(within(listbox).getAllByRole('option')[1]).toHaveAttribute('aria-selected', 'true')
    expect(press('Enter')).toBe(true)

    // The auto-closed `]]` after the cursor is swallowed, not doubled.
    expect(screen.getByLabelText('Markdown source')).toHaveValue('See [[metric:signup_count]]')
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('picks with the mouse too', async () => {
    renderEditor()
    type('[[sig')
    openPicker({ mode: 'link', from: 0, to: 5, kind: null, query: 'sig' })
    fireEvent.mouseDown(await screen.findByRole('option', { name: /signup_rate/ }))
    expect(screen.getByLabelText('Markdown source')).toHaveValue('[[metric:signup_rate]]')
  })

  it('closes on Escape and stays closed for the same [[', async () => {
    renderEditor()
    type('[[sig')
    openPicker({ mode: 'link', from: 0, to: 5, kind: null, query: 'sig' })
    await screen.findByRole('listbox')
    expect(press('Escape')).toBe(true)
    expect(screen.queryByRole('listbox')).toBeNull()

    openPicker({ mode: 'link', from: 0, to: 6, kind: null, query: 'sign' })
    expect(screen.queryByRole('listbox')).toBeNull()
    // Keys go back to the editor while it is closed.
    expect(press('Enter')).toBe(false)
  })

  it('never offers the previous query rows while the narrowed query loads', async () => {
    vi.mocked(docsApi.linkSuggestions).mockResolvedValue({
      items: [{ kind: 'doc', id: 'n-1', label: 'Setup guide', detail: 'guides/setup.md', insert: '[[doc:n-1]]' }],
    })
    renderEditor()
    type('[[set')
    openPicker({ mode: 'link', from: 0, to: 5, kind: null, query: 'set' })
    await screen.findByRole('option', { name: /Setup guide/ })

    // The metric lookup never answers: the note row must not stay pickable.
    vi.mocked(docsApi.linkSuggestions).mockReturnValue(new Promise(() => {}))
    type('[[metric:rev')
    openPicker({ mode: 'link', from: 0, to: 12, kind: 'metric', query: 'rev' })
    expect(screen.queryByRole('option', { name: /Setup guide/ })).toBeNull()
    expect(press('Enter')).toBe(false)
    expect(screen.getByLabelText('Markdown source')).toHaveValue('[[metric:rev')
  })

  it('opens the people picker on @ and inserts a user mention', async () => {
    vi.mocked(docsApi.linkSuggestions).mockResolvedValue({
      items: [{ kind: 'user', id: 'u-1', label: 'Ada Example', detail: 'ada@example.com', insert: '[[user:u-1]]' }],
    })
    renderEditor()
    type('Thanks @ad')
    openPicker({ mode: 'mention', from: 7, to: 10, kind: 'user', query: 'ad' })
    const option = await screen.findByRole('option', { name: /@Ada Example/ })
    expect(screen.getByRole('listbox', { name: 'Mention a person' })).toBeInTheDocument()
    expect(option).toHaveTextContent('ada@example.com')
    press('Tab')
    expect(screen.getByLabelText('Markdown source')).toHaveValue('Thanks [[user:u-1]]')
  })

  it('re-points a broken link at a suggested name from the banner', async () => {
    vi.mocked(docsApi.links).mockResolvedValue([
      {
        kind: 'metric',
        target: 'signup_rte',
        qualifier: null,
        raw: '[[metric:signup_rte]]',
        status: 'broken',
        route_path: null,
        entity_id: null,
        candidates: 0,
        suggestions: ['signup_rate'],
      },
    ])
    renderEditor()
    type('Rate: [[metric:signup_rte|the rate]]')
    fireEvent.click(await screen.findByRole('button', { name: 'Relink [[metric:signup_rte]] to signup_rate' }))
    expect(screen.getByLabelText('Markdown source')).toHaveValue('Rate: [[metric:signup_rate|the rate]]')
  })
})
