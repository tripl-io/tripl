import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import type { DocWriteResponse } from '@/types/docs'
import { MoveDocDialog, NewDocDialog, type MoveRequest, type NewDocRequest } from './DocFileDialogs'

vi.mock('@/api/docs', () => ({
  docsApi: { write: vi.fn(), move: vi.fn() },
}))

import { docsApi } from '@/api/docs'

function wrap(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  render(<QueryClientProvider client={client}>{children}</QueryClientProvider>)
  return { invalidate }
}

function created(path: string): DocWriteResponse {
  return {
    id: 'd-1',
    scope: 'project',
    path,
    title: 'x',
    description: '',
    tags: [],
    audience: 'both',
    revision: 1,
    size_bytes: 1,
    updated_at: '2026-09-01T00:00:00Z',
    updated_by_name: null,
    content: '',
    body: '',
    extra_frontmatter: {},
    links: [],
    created_at: '2026-09-01T00:00:00Z',
    created_by_name: null,
    created: true,
    changed: true,
    warnings: [],
  }
}

function renderNew(request: NewDocRequest | null) {
  const onClose = vi.fn()
  const onCreated = vi.fn()
  const { invalidate } = wrap(
    <NewDocDialog slug="demo" request={request} organizationName="Acme" onClose={onClose} onCreated={onCreated} />,
  )
  return { onClose, onCreated, invalidate }
}

function renderMove(request: MoveRequest | null) {
  const onClose = vi.fn()
  const onMoved = vi.fn()
  const { invalidate } = wrap(<MoveDocDialog slug="demo" request={request} onClose={onClose} onMoved={onMoved} />)
  return { onClose, onMoved, invalidate }
}

const createButton = () => screen.getByRole('button', { name: 'Create' })
const moveButton = () => screen.getByRole('button', { name: 'Move' })

beforeEach(() => {
  vi.mocked(docsApi.write).mockReset()
  vi.mocked(docsApi.move).mockReset()
})

describe('NewDocDialog', () => {
  it('renders nothing without a request', () => {
    renderNew(null)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('starts in the given folder, adds .md and creates the note create-only', async () => {
    vi.mocked(docsApi.write).mockResolvedValue(created('guides/setup.md'))
    const { onCreated, invalidate } = renderNew({ scope: 'project', folder: 'guides/' })
    const path = screen.getByLabelText('Path')
    expect(path).toHaveValue('guides/')
    // A folder alone is not a file name yet.
    expect(createButton()).toBeDisabled()
    fireEvent.change(path, { target: { value: 'guides/setup' } })
    expect(screen.getByText('guides/setup.md')).toBeInTheDocument()
    fireEvent.click(createButton())

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created('guides/setup.md')))
    const [slug, scope, notePath, body] = vi.mocked(docsApi.write).mock.calls[0]!
    expect([slug, scope, notePath]).toEqual(['demo', 'project', 'guides/setup.md'])
    expect(body).toMatchObject({ create_only: true, message: 'Created' })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['docs', 'demo'] })
  })

  it('creates in the organization root when that scope is picked, carrying the body', async () => {
    vi.mocked(docsApi.write).mockResolvedValue(created('a.md'))
    const { onCreated } = renderNew({ scope: 'project', folder: '', body: '[[event:signup]]' })
    fireEvent.click(within(screen.getByRole('group', { name: 'Scope' })).getByRole('button', { name: 'Organization · Acme' }))
    fireEvent.change(screen.getByLabelText('Path'), { target: { value: 'a' } })
    fireEvent.click(createButton())
    await waitFor(() => expect(onCreated).toHaveBeenCalled())
    const [, scope, , body] = vi.mocked(docsApi.write).mock.calls[0]!
    expect(scope).toBe('organization')
    expect(body.content).toContain('[[event:signup]]')
  })

  it('hints at the extension while empty and explains an invalid path', () => {
    renderNew({ scope: 'project', folder: '' })
    expect(screen.getByText('.md is added when missing.')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Path'), { target: { value: '/abs' } })
    expect(screen.getByText('Paths are relative: drop the leading "/".')).toBeInTheDocument()
    expect(createButton()).toBeDisabled()
  })

  it('shows the error on submit of an invalid path (Enter in the field)', () => {
    renderNew({ scope: 'project', folder: '' })
    const path = screen.getByLabelText('Path')
    fireEvent.change(path, { target: { value: 'a\\b' } })
    fireEvent.submit(path.closest('form')!)
    expect(screen.getByRole('alert')).toHaveTextContent('Use "/" to separate folders')
    expect(path).toHaveAttribute('aria-invalid', 'true')
    expect(docsApi.write).not.toHaveBeenCalled()
  })

  it('shows a taken path (409) in place and keeps the dialog open', async () => {
    vi.mocked(docsApi.write).mockRejectedValue(new ApiError('A note already exists at guides/Setup.md', 409))
    const { onCreated } = renderNew({ scope: 'project', folder: '' })
    fireEvent.change(screen.getByLabelText('Path'), { target: { value: 'guides/setup' } })
    fireEvent.click(createButton())
    expect(await screen.findByRole('alert')).toHaveTextContent('A note already exists at guides/Setup.md')
    expect(onCreated).not.toHaveBeenCalled()
  })

  it('cancels', () => {
    const { onClose } = renderNew({ scope: 'project', folder: '' })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalled()
  })

  it('closes on Escape', () => {
    const { onClose } = renderNew({ scope: 'project', folder: '' })
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })
})

describe('MoveDocDialog', () => {
  it('renders nothing without a request', () => {
    renderMove(null)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('renames a note and reports what moved', async () => {
    const moved = [{ from_path: 'guides/setup.md', to_path: 'guides/install.md' }]
    vi.mocked(docsApi.move).mockResolvedValue({ moved })
    const request: MoveRequest = { scope: 'project', from: 'guides/setup.md', folder: false }
    const { onMoved, invalidate } = renderMove(request)
    expect(screen.getByRole('heading', { name: 'Rename or move note' })).toBeInTheDocument()
    expect(screen.getByText(/relative links from other notes/)).toBeInTheDocument()
    const input = screen.getByLabelText('New path')
    // Unchanged: nothing to do.
    expect(moveButton()).toBeDisabled()
    fireEvent.change(input, { target: { value: 'guides/install' } })
    fireEvent.click(moveButton())
    await waitFor(() => expect(onMoved).toHaveBeenCalledWith(moved, request))
    expect(docsApi.move).toHaveBeenCalledWith('demo', {
      scope: 'project',
      from_path: 'guides/setup.md',
      to_path: 'guides/install.md',
      folder: false,
    })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['docs', 'demo'] })
  })

  it('moves a folder as a prefix', async () => {
    vi.mocked(docsApi.move).mockResolvedValue({ moved: [] })
    const { onMoved } = renderMove({ scope: 'organization', from: 'guides/', folder: true })
    expect(screen.getByRole('heading', { name: 'Rename or move folder' })).toBeInTheDocument()
    expect(screen.getByText(/Every note under this folder moves with it/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('New folder path'), { target: { value: 'handbook' } })
    fireEvent.click(moveButton())
    await waitFor(() => expect(onMoved).toHaveBeenCalled())
    expect(docsApi.move).toHaveBeenCalledWith('demo', {
      scope: 'organization',
      from_path: 'guides/',
      to_path: 'handbook/',
      folder: true,
    })
  })

  it('explains an invalid target and blocks Move', () => {
    renderMove({ scope: 'project', from: 'guides/', folder: true })
    const input = screen.getByLabelText('New folder path')
    fireEvent.change(input, { target: { value: '' } })
    expect(screen.getByText('Enter a folder name.')).toBeInTheDocument()
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(moveButton()).toBeDisabled()
    // Submitting anyway does nothing.
    fireEvent.submit(input.closest('form')!)
    expect(docsApi.move).not.toHaveBeenCalled()
  })

  it('names the colliding paths of a 409', async () => {
    const err = new ApiError('Conflict', 409)
    err.detail = { message: 'Target paths are taken', collisions: ['b/one.md', 'b/two.md'] }
    vi.mocked(docsApi.move).mockRejectedValue(err)
    renderMove({ scope: 'project', from: 'a/', folder: true })
    fireEvent.change(screen.getByLabelText('New folder path'), { target: { value: 'b' } })
    fireEvent.click(moveButton())
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toBe('Target paths are taken:\nb/one.md\nb/two.md')
  })

  it('reads 409 collisions from `paths` with a default lead', async () => {
    const err = new ApiError('Conflict', 409)
    err.detail = { paths: ['x.md'] }
    vi.mocked(docsApi.move).mockRejectedValue(err)
    renderMove({ scope: 'project', from: 'a.md', folder: false })
    fireEvent.change(screen.getByLabelText('New path'), { target: { value: 'x' } })
    fireEvent.click(moveButton())
    expect((await screen.findByRole('alert')).textContent).toBe('These paths are already taken:\nx.md')
  })

  it('falls back to the plain message for a 409 without paths and for other errors', async () => {
    const conflict = new ApiError('Already there', 409)
    conflict.detail = { message: 'ignored' }
    vi.mocked(docsApi.move).mockRejectedValueOnce(conflict).mockRejectedValueOnce(new ApiError('Forbidden', 403))
    renderMove({ scope: 'project', from: 'a.md', folder: false })
    fireEvent.change(screen.getByLabelText('New path'), { target: { value: 'b' } })
    fireEvent.click(moveButton())
    expect(await screen.findByRole('alert')).toHaveTextContent('Already there')
    fireEvent.click(moveButton())
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Forbidden'))
  })

  it('cancels', () => {
    const { onClose } = renderMove({ scope: 'project', from: 'a.md', folder: false })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalled()
  })
})
