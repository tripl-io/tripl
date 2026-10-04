import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext } from './auth-context'
import { CommentThread, type ThreadComment } from './comment-thread'
import { at } from '@/test/at'
import { authAs } from '@/test/auth'
import { personaAuth } from '@/test/persona'
import { PersonaProject } from '@/test/PersonaProject'

const { listMembers, listUsers } = vi.hoisted(() => ({ listMembers: vi.fn(), listUsers: vi.fn() }))
vi.mock('@/api/projectMembers', () => ({ projectMembersApi: { list: listMembers } }))
vi.mock('@/api/users', () => ({ usersApi: { list: listUsers } }))

function comment(overrides: Partial<ThreadComment> & { id: string }): ThreadComment {
  return {
    parent_id: null,
    body: 'a comment',
    created_at: '2026-09-08T10:00:00Z',
    ...overrides,
  }
}

let queryClient: QueryClient

function wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: queryClient }, children)
}

function renderThread(
  rows: ThreadComment[],
  handlers: Partial<{
    create: (body: string, parentId: string | null) => Promise<unknown>
    remove: (commentId: string) => Promise<unknown>
    authorName: (comment: ThreadComment) => string
    onCreated: () => void
    onAction: (
      commentId: string,
      action: 'resolve' | 'snooze' | 'reopen',
      snoozedUntil?: string,
    ) => Promise<unknown>
  }> = {},
) {
  const create = handlers.create ?? vi.fn().mockResolvedValue({})
  const remove = handlers.remove ?? vi.fn().mockResolvedValue(undefined)
  const view = render(
    createElement(CommentThread, {
      queryKey: ['thread', 'demo'],
      list: () => Promise.resolve(rows),
      create,
      remove,
      authorName: handlers.authorName,
      onCreated: handlers.onCreated,
      onAction: handlers.onAction,
    }),
    { wrapper },
  )
  return { ...view, create, remove }
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  listUsers.mockResolvedValue([])
  listMembers.mockResolvedValue([])
})

afterEach(() => {
  queryClient.clear()
  vi.clearAllMocks()
})

describe('CommentThread', () => {
  it('lets a viewer read the thread but offers no write', async () => {
    const viewer = personaAuth('viewer')
    render(
      createElement(
        AuthContext.Provider,
        { value: viewer },
        createElement(
          PersonaProject,
          { persona: 'viewer' },
          createElement(CommentThread, {
            queryKey: ['thread', 'demo'],
            list: () => Promise.resolve([comment({ id: 'c1', body: 'is this still sent?' })]),
            create: vi.fn(),
            remove: vi.fn(),
            onAction: vi.fn(),
          }),
        ),
      ),
      { wrapper },
    )

    expect(await screen.findByText('is this still sent?')).toBeInTheDocument()
    expect(screen.getByText('Only editors and owners can comment.')).toBeInTheDocument()
    expect(screen.queryByLabelText('Write a comment')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete comment' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'resolve' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'reply' })).not.toBeInTheDocument()
  })

  it('says the thread is empty rather than showing nothing', async () => {
    renderThread([])
    expect(await screen.findByText('No comments yet. Start the thread.')).toBeInTheDocument()
    expect(screen.getByText('(0)')).toBeInTheDocument()
  })

  it('nests replies under the comment they answer', async () => {
    renderThread([
      comment({ id: 'c1', body: 'should this fire on cancel?' }),
      comment({ id: 'c2', parent_id: 'c1', body: 'no, cancel has its own event' }),
    ])
    const top = await screen.findByText('should this fire on cancel?')
    expect(screen.getByText('(2)')).toBeInTheDocument()
    // The reply renders inside the top-level comment's block, not beside it.
    const block = top.closest('.space-y-2')
    expect(block?.textContent).toContain('no, cancel has its own event')
  })

  it('posts a reply against the comment being answered', async () => {
    const { create } = renderThread([comment({ id: 'c1' })])
    fireEvent.click(await screen.findByRole('button', { name: 'reply' }))

    fireEvent.change(screen.getByLabelText('Write a comment'), {
      target: { value: 'answering' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Reply' }))

    await waitFor(() => expect(create).toHaveBeenCalledWith('answering', 'c1'))
  })

  it('composes in a div, so a thread can live inside another form', async () => {
    // A nested <form> is invalid HTML: the browser drops the inner one, and the
    // composer's submit would save the surrounding event instead of posting.
    const { container, create } = renderThread([])
    await screen.findByText('No comments yet. Start the thread.')
    expect(container.querySelector('form')).toBeNull()

    const box = screen.getByLabelText('Write a comment')
    fireEvent.change(box, { target: { value: 'raised for discussion' } })
    // The shortcut every chat box uses; plain Enter belongs to the text.
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(create).not.toHaveBeenCalled()

    fireEvent.keyDown(box, { key: 'Enter', metaKey: true })
    await waitFor(() => expect(create).toHaveBeenCalledWith('raised for discussion', null))
  })

  it('will not post an empty comment', async () => {
    const { create } = renderThread([])
    await screen.findByText('No comments yet. Start the thread.')
    fireEvent.change(screen.getByLabelText('Write a comment'), { target: { value: '   ' } })
    expect(screen.getByRole('button', { name: 'Comment' })).toBeDisabled()
    expect(create).not.toHaveBeenCalled()
  })

  it('deletes the comment whose control was used', async () => {
    const { remove } = renderThread([
      comment({ id: 'c1', body: 'first' }),
      comment({ id: 'c2', body: 'second' }),
    ])
    await screen.findByText('second')
    const controls = screen.getAllByRole('button', { name: 'Delete comment' })
    fireEvent.click(at(controls, 1))
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(remove).toHaveBeenCalledWith('c2'))
  })
})

function renderAs(
  role: 'owner' | 'member',
  rows: ThreadComment[],
  extra: Partial<{ onAction: () => Promise<unknown>; create: () => Promise<unknown> }> = {},
) {
  const remove = vi.fn().mockResolvedValue(undefined)
  const create = extra.create ?? vi.fn().mockResolvedValue({})
  render(
    createElement(
      AuthContext.Provider,
      { value: authAs(role, 'me') },
      createElement(CommentThread, {
        queryKey: ['thread', 'demo'],
        list: () => Promise.resolve(rows),
        create,
        remove,
        onAction: extra.onAction,
      }),
    ),
    { wrapper },
  )
  return { remove, create }
}

describe('CommentThread delete', () => {
  it('asks first, and a cancelled delete deletes nothing', async () => {
    const { remove } = renderThread([comment({ id: 'c1', body: 'first' })])
    fireEvent.click(await screen.findByRole('button', { name: 'Delete comment' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Delete this comment? This cannot be undone.')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(remove).not.toHaveBeenCalled()
  })

  it('says a parent takes its replies with it', async () => {
    renderThread([
      comment({ id: 'c1', body: 'question' }),
      comment({ id: 'c2', parent_id: 'c1', body: 'answer one' }),
      comment({ id: 'c3', parent_id: 'c1', body: 'answer two' }),
    ])
    await screen.findByText('answer two')
    fireEvent.click(at(screen.getAllByRole('button', { name: 'Delete comment' }), 0))

    expect(await screen.findByRole('alertdialog')).toHaveTextContent(
      'Delete this comment and its 2 replies? This cannot be undone.',
    )
  })

  it("offers a member delete on their own comment only", async () => {
    renderAs('member', [
      comment({ id: 'c1', body: 'mine', user_id: 'me' }),
      comment({ id: 'c2', body: 'theirs', user_id: 'someone-else' }),
    ])
    await screen.findByText('theirs')
    expect(screen.getAllByRole('button', { name: 'Delete comment' })).toHaveLength(1)
  })

  it('lets the owner delete any comment', async () => {
    renderAs('owner', [
      comment({ id: 'c1', body: 'mine', user_id: 'me' }),
      comment({ id: 'c2', body: 'theirs', user_id: 'someone-else' }),
      comment({ id: 'c3', body: 'gone', user_id: null }),
    ])
    await screen.findByText('gone')
    expect(screen.getAllByRole('button', { name: 'Delete comment' })).toHaveLength(3)
  })
})

describe('CommentThread catalog counts', () => {
  it("refreshes the catalog's open-question count when a new question is posted", async () => {
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    renderAs('member', [], { onAction: vi.fn().mockResolvedValue({}) })

    fireEvent.change(await screen.findByLabelText('Write a comment'), {
      target: { value: 'does this fire on cancel?' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Comment' }))

    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['events'] }),
    )
  })

  it('leaves the catalog alone for a thread with no resolution state', async () => {
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    const { create } = renderAs('member', [])

    fireEvent.change(await screen.findByLabelText('Write a comment'), {
      target: { value: 'a branch note' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Comment' }))

    await waitFor(() => expect(create).toHaveBeenCalled())
    await waitFor(() => expect(invalidate).toHaveBeenCalled())
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ['events'] })
  })
})

describe('CommentThread authors', () => {
  it('names the author on a comment and on its reply', async () => {
    renderThread(
      [
        comment({ id: 'c-1', body: 'Is this still shipping?', user_id: 'u-1' }),
        comment({ id: 'c-2', parent_id: 'c-1', body: 'Yes, in March.', user_id: 'u-2' }),
      ],
      { authorName: c => (c.user_id === 'u-1' ? 'Ada' : 'Grace') },
    )

    expect(await screen.findByText(/Ada ·/)).toBeInTheDocument()
    expect(screen.getByText(/Grace ·/)).toBeInTheDocument()
  })

  it('stays anonymous when no resolver is given', async () => {
    renderThread([comment({ id: 'c-1', body: 'hi', user_id: 'u-1' })])

    expect(await screen.findByText('hi')).toBeInTheDocument()
    expect(screen.queryByText(/ · /)).not.toBeInTheDocument()
  })

  it('fires onCreated after a post — the demo scenario hangs off it', async () => {
    const onCreated = vi.fn()
    renderThread([], { onCreated })

    fireEvent.change(await screen.findByLabelText('Write a comment'), {
      target: { value: 'first' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Comment' }))

    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1))
  })
})


describe('CommentThread resolution', () => {
  it('shows no resolution controls when the caller offers no action', async () => {
    // The branch-review thread's table has no resolution columns, so the same
    // component must render exactly as it did before.
    renderThread([comment({ id: 'c-1' })])
    await screen.findByText('a comment')

    expect(screen.queryByRole('button', { name: 'resolve' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'snooze' })).toBeNull()
  })

  it('resolves a thread, and snoozes it with a date the caller never has to pick', async () => {
    const onAction = vi.fn().mockResolvedValue({})
    renderThread([comment({ id: 'c-1', status: 'open' })], { onAction })
    await screen.findByText('a comment')

    fireEvent.click(screen.getByRole('button', { name: 'resolve' }))
    await waitFor(() => expect(onAction).toHaveBeenCalledWith('c-1', 'resolve', undefined))

    fireEvent.click(screen.getByRole('button', { name: 'snooze' }))
    await waitFor(() => expect(onAction).toHaveBeenCalledTimes(2))
    const [, action, snoozedUntil] = at(onAction.mock.calls, 1)
    expect(action).toBe('snooze')
    // The API requires a date on a snooze, so the control has to supply one.
    expect(new Date(snoozedUntil as string).getTime()).toBeGreaterThan(Date.now())
  })

  it('offers reopen — and only reopen — once the thread is answered', async () => {
    const onAction = vi.fn().mockResolvedValue({})
    renderThread([comment({ id: 'c-1', status: 'resolved' })], { onAction })
    await screen.findByText('a comment')

    expect(screen.getByText('resolved')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'resolve' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'reopen' }))
    await waitFor(() => expect(onAction).toHaveBeenCalledWith('c-1', 'reopen', undefined))
  })

  it('treats a lapsed snooze as unanswered again', async () => {
    // Derived, not stored: a written-back flag is wrong for as long as it takes
    // a sweeper to run, and the point of a snooze is that nobody is watching.
    renderThread([comment({ id: 'c-1', status: 'snoozed', snoozed_until: '2000-01-01T00:00:00Z' })], {
      onAction: vi.fn().mockResolvedValue({}),
    })
    await screen.findByText('a comment')

    expect(screen.getByRole('button', { name: 'resolve' })).toBeInTheDocument()
    expect(screen.queryByText('snoozed')).toBeNull()
  })

  it('leaves a live snooze parked', async () => {
    renderThread([comment({ id: 'c-1', status: 'snoozed', snoozed_until: '2999-01-01T00:00:00Z' })], {
      onAction: vi.fn().mockResolvedValue({}),
    })
    await screen.findByText('a comment')

    expect(screen.getByText('snoozed')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'reopen' })).toBeInTheDocument()
  })

  it('does not offer a reply its own resolution state', async () => {
    // The thread is the unit that gets answered; the server refuses an action on
    // a reply, so the UI must not offer one.
    renderThread(
      [comment({ id: 'c-1' }), comment({ id: 'c-2', parent_id: 'c-1', body: 'a reply' })],
      { onAction: vi.fn().mockResolvedValue({}) },
    )
    await screen.findByText('a reply')

    expect(screen.getAllByRole('button', { name: 'resolve' })).toHaveLength(1)
  })
})

describe('CommentThread mentions (#259)', () => {
  const ADA = '11111111-2222-3333-4444-555555555555'
  const GRACE = '22222222-3333-4444-5555-666666666666'
  const LINUS = '33333333-4444-5555-6666-777777777777'

  function renderMentionThread(rows: ThreadComment[]) {
    return render(
      createElement(
        AuthContext.Provider,
        { value: authAs('member') },
        createElement(CommentThread, {
          queryKey: ['thread', 'mentions', rows.length],
          list: () => Promise.resolve(rows),
          create: vi.fn().mockResolvedValue({}),
          remove: vi.fn(),
          mentionSlug: 'demo',
        }),
      ),
      { wrapper },
    )
  }

  it('draws a stored mention as a chip, not as its token', async () => {
    renderThread([comment({ id: 'c-1', body: `ask @[Ada Lovelace](${ADA}) about it` })])

    const chip = await screen.findByText('@Ada Lovelace')
    expect(chip).toHaveAttribute('data-slot', 'chip')
    expect(chip).toHaveAttribute('data-mention-user-id', ADA)
    expect(screen.queryByText(new RegExp(ADA.slice(0, 8)))).toBeNull()
  })

  it('offers project members after @ and inserts the token on pick', async () => {
    listMembers.mockResolvedValue([
      { user_id: ADA, name: 'Ada Lovelace', email: 'ada@example.com', role: 'editor', added_at: '2026-01-01T00:00:00Z' },
    ])
    const create = vi.fn().mockResolvedValue({})
    render(
      createElement(
        AuthContext.Provider,
        { value: authAs('member') },
        createElement(CommentThread, {
          queryKey: ['thread', 'mentions'],
          list: () => Promise.resolve([]),
          create,
          remove: vi.fn(),
          mentionSlug: 'demo',
        }),
      ),
      { wrapper },
    )
    await waitFor(() => expect(listMembers).toHaveBeenCalledWith('demo', expect.anything()))
    const box = (await screen.findByLabelText('Write a comment')) as HTMLTextAreaElement

    fireEvent.change(box, { target: { value: 'cc @ad', selectionStart: 6, selectionEnd: 6 } })
    const option = await screen.findByRole('option', { name: /Ada Lovelace/ })
    fireEvent.mouseDown(option)

    expect(box.value).toBe(`cc @[Ada Lovelace](${ADA}) `)
    fireEvent.click(screen.getByRole('button', { name: 'Comment' }))
    await waitFor(() => expect(create).toHaveBeenCalledWith(`cc @[Ada Lovelace](${ADA})`, null))
  })

  it('is an autocompleting textbox that owns its list and says how many members match', async () => {
    listMembers.mockResolvedValue([
      { user_id: ADA, name: 'Ada Lovelace', email: 'ada@example.com', role: 'editor', added_at: '2026-01-01T00:00:00Z' },
    ])
    renderMentionThread([])
    await waitFor(() => expect(listMembers).toHaveBeenCalled())
    // A <textarea> may carry no role (ARIA in HTML), so it stays a textbox.
    const box = await screen.findByRole('textbox', { name: 'Write a comment' })
    expect(box).toHaveAttribute('aria-haspopup', 'listbox')
    expect(box).toHaveAttribute('aria-autocomplete', 'list')
    expect(box.getAttribute('aria-controls')).toBeTruthy()
    expect(box).not.toHaveAttribute('aria-activedescendant')

    fireEvent.change(box, { target: { value: '@ad', selectionStart: 3, selectionEnd: 3 } })
    const option = await screen.findByRole('option', { name: /Ada Lovelace/ })
    expect(box).toHaveAttribute('aria-activedescendant', option.id)
    expect(screen.getByText('1 member')).toHaveAttribute('aria-live', 'polite')
  })

  it('offers instance owners who hold no member row, once', async () => {
    listMembers.mockResolvedValue([
      { user_id: ADA, name: 'Ada Lovelace', email: 'ada@example.com', role: 'editor', added_at: '2026-01-01T00:00:00Z' },
    ])
    listUsers.mockResolvedValue([
      { id: ADA, name: 'Ada Lovelace', email: 'ada@example.com', role: 'owner' },
      { id: GRACE, name: 'Grace Hopper', email: 'grace@example.com', role: 'owner' },
      { id: LINUS, name: 'Linus', email: 'linus@example.com', role: 'member' },
    ])
    renderMentionThread([])
    await waitFor(() => expect(listUsers).toHaveBeenCalled())
    const box = (await screen.findByLabelText('Write a comment')) as HTMLTextAreaElement

    fireEvent.change(box, { target: { value: '@', selectionStart: 1, selectionEnd: 1 } })
    await screen.findByRole('option', { name: /Grace Hopper/ })
    expect(screen.getAllByRole('option', { name: /Ada Lovelace/ })).toHaveLength(1)
    expect(screen.queryByRole('option', { name: /Linus/ })).toBeNull()
  })

  it('draws a chip under the member\'s current name, with their email as its title', async () => {
    listUsers.mockResolvedValue([
      { id: ADA, name: 'Ada King', email: 'ada@example.com', role: 'member' },
    ])
    renderMentionThread([comment({ id: 'c-1', body: `ask @[Ada Lovelace](${ADA}) and @[Gone](${GRACE})` })])

    const chip = await screen.findByText('@Ada King')
    expect(chip).toHaveAttribute('title', 'ada@example.com')
    expect(screen.getByText('@Gone')).not.toHaveAttribute('title')
  })
})
