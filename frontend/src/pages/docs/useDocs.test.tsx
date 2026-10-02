import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import {
  useDeleteDoc,
  TRANSLATION_POLL_MS,
  translationPollInterval,
  useDeleteDocFolder,
  useDocFile,
  useDocRevision,
  useDocRevisions,
  useDocTree,
  useMoveDoc,
  useRestoreDocRevision,
  useWriteDoc,
} from './useDocs'

vi.mock('@/api/docs', () => ({
  docsApi: {
    tree: vi.fn(),
    read: vi.fn(),
    write: vi.fn(),
    remove: vi.fn(),
    removeFolder: vi.fn(),
    move: vi.fn(),
    revisions: vi.fn(),
    revision: vi.fn(),
    restore: vi.fn(),
  },
}))

import { docsApi } from '@/api/docs'

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  return { client, invalidate, wrapper }
}

beforeEach(() => {
  for (const fn of Object.values(docsApi)) vi.mocked(fn).mockReset()
})

describe('docs queries', () => {
  it('loads the tree, and waits for a slug', async () => {
    vi.mocked(docsApi.tree).mockResolvedValue({ project_docs: [] } as never)
    const { wrapper } = setup()
    const idle = renderHook(() => useDocTree(''), { wrapper })
    expect(idle.result.current.fetchStatus).toBe('idle')
    const { result } = renderHook(() => useDocTree('demo'), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(docsApi.tree).toHaveBeenCalledTimes(1)
    expect(docsApi.tree).toHaveBeenCalledWith('demo', expect.anything())
  })

  it('reads a file only with a scope, a path and a language, and does not retry a failure', async () => {
    vi.mocked(docsApi.read).mockRejectedValue(new ApiError('Doc not found', 404))
    const { wrapper } = setup()
    const idle = renderHook(() => useDocFile('demo', null, 'a.md', 'original'), { wrapper })
    expect(idle.result.current.fetchStatus).toBe('idle')
    // The page does not know the language yet (the tree is loading): wait.
    const waiting = renderHook(() => useDocFile('demo', 'organization', 'a.md', null), { wrapper })
    expect(waiting.result.current.fetchStatus).toBe('idle')
    const { result } = renderHook(() => useDocFile('demo', 'organization', 'a.md', 'de'), { wrapper })
    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(docsApi.read).toHaveBeenCalledTimes(1)
    expect(docsApi.read).toHaveBeenCalledWith('demo', 'organization', 'a.md', 'de', expect.anything())
  })

  it('re-reads the note only while one of its translations is being made', () => {
    const doc = (status: string) => ({ translations: [{ lang: 'de', status }] }) as never
    expect(translationPollInterval(doc('pending'))).toBe(TRANSLATION_POLL_MS)
    expect(translationPollInterval(doc('ready'))).toBe(false)
    expect(translationPollInterval(undefined)).toBe(false)
  })

  it('lists revisions only while enabled and reads one by id', async () => {
    vi.mocked(docsApi.revisions).mockResolvedValue({ items: [] } as never)
    vi.mocked(docsApi.revision).mockResolvedValue({ id: 'r-1' } as never)
    const { wrapper } = setup()
    renderHook(() => useDocRevisions('demo', 'project', 'a.md', false), { wrapper })
    expect(docsApi.revisions).not.toHaveBeenCalled()
    const list = renderHook(() => useDocRevisions('demo', 'project', 'a.md', true), { wrapper })
    await waitFor(() => expect(list.result.current.isSuccess).toBe(true))
    renderHook(() => useDocRevision('demo', null), { wrapper })
    expect(docsApi.revision).not.toHaveBeenCalled()
    const one = renderHook(() => useDocRevision('demo', 'r-1'), { wrapper })
    await waitFor(() => expect(one.result.current.data).toEqual({ id: 'r-1' }))
  })
})

/** A mutation hook reduced to "call it with these vars", so one table covers all. */
function runner<V>(use: () => { mutateAsync: (vars: V) => Promise<unknown> }) {
  return () => {
    const { mutateAsync } = use()
    return (vars: unknown) => mutateAsync(vars as V)
  }
}

describe('docs mutations', () => {
  it.each([
    ['write', runner(() => useWriteDoc('demo')), { scope: 'project', path: 'a.md', body: { content: 'x' } }, () =>
      expect(docsApi.write).toHaveBeenCalledWith('demo', 'project', 'a.md', { content: 'x' })],
    ['delete', runner(() => useDeleteDoc('demo')), { scope: 'project', path: 'a.md' }, () =>
      expect(docsApi.remove).toHaveBeenCalledWith('demo', 'project', 'a.md')],
    ['delete folder', runner(() => useDeleteDocFolder('demo')), { scope: 'organization', prefix: 'g/' }, () =>
      expect(docsApi.removeFolder).toHaveBeenCalledWith('demo', 'organization', 'g/')],
    ['move', runner(() => useMoveDoc('demo')), { scope: 'project', from_path: 'a.md', to_path: 'b.md' }, () =>
      expect(docsApi.move).toHaveBeenCalledWith('demo', { scope: 'project', from_path: 'a.md', to_path: 'b.md' })],
    ['restore', runner(() => useRestoreDocRevision('demo')), { revisionId: 'r-1' }, () =>
      expect(docsApi.restore).toHaveBeenCalledWith('demo', 'r-1', '')],
  ] as const)('%s calls the API and refreshes the whole docs family', async (_name, hook, vars, check) => {
    for (const fn of Object.values(docsApi)) vi.mocked(fn).mockResolvedValue({} as never)
    const { wrapper, invalidate } = setup()
    const { result } = renderHook(hook, { wrapper })
    await act(() => result.current(vars))
    check()
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['docs', 'demo'] })
  })

  it('passes a restore message through', async () => {
    vi.mocked(docsApi.restore).mockResolvedValue({} as never)
    const { wrapper } = setup()
    const { result } = renderHook(() => useRestoreDocRevision('demo'), { wrapper })
    await act(() => result.current.mutateAsync({ revisionId: 'r-2', message: 'back' }))
    expect(docsApi.restore).toHaveBeenCalledWith('demo', 'r-2', 'back')
  })

  it('does not refresh anything when a write fails', async () => {
    vi.mocked(docsApi.write).mockRejectedValue(new ApiError('Conflict', 409))
    const { wrapper, invalidate } = setup()
    const { result } = renderHook(() => useWriteDoc('demo'), { wrapper })
    await act(async () => {
      await expect(
        result.current.mutateAsync({ scope: 'project', path: 'a.md', body: { content: 'x' } }),
      ).rejects.toThrow('Conflict')
    })
    expect(invalidate).not.toHaveBeenCalled()
  })
})
