// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, AUTH_UNAUTHORIZED_EVENT } from './client'
import { docsApi, linkRefQuery } from './docs'
import { at } from '@/test/at'

// The real client is exercised; only global fetch is stubbed, so the URL and
// body asserted here are what the docs requests actually send.

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}

function stubFetch(response: Response | (() => Response)) {
  return vi
    .spyOn(globalThis, 'fetch')
    .mockImplementation(() => Promise.resolve(typeof response === 'function' ? response() : response))
}

function call(spy: ReturnType<typeof stubFetch>, index = 0) {
  const [input, init] = at(spy.mock.calls, index)
  const url = new URL(String(input), 'http://localhost')
  return { url, init: init ?? {}, body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('docsApi JSON requests', () => {
  it('reads the tree with the caller\'s signal', async () => {
    const spy = stubFetch(jsonResponse({ project_docs: [] }))
    const controller = new AbortController()
    await docsApi.tree('my proj', controller.signal)
    const { url, init } = call(spy)
    expect(url.pathname).toBe('/api/v1/projects/my%20proj/docs')
    expect(init.signal).toBe(controller.signal)
  })

  it('reads, writes and deletes a file by scope and path', async () => {
    const spy = stubFetch(() => jsonResponse({}))
    await docsApi.read('demo', 'organization', 'a b/c.md')
    await docsApi.write('demo', 'project', 'c.md', { content: '# C', base_revision: 2 })
    await docsApi.remove('demo', 'project', 'c.md')
    await docsApi.removeFolder('demo', 'organization', 'guides/')

    const read = call(spy, 0)
    expect(read.url.pathname).toBe('/api/v1/projects/demo/docs/file')
    expect(Object.fromEntries(read.url.searchParams)).toEqual({ scope: 'organization', path: 'a b/c.md' })

    const write = call(spy, 1)
    expect(write.init.method).toBe('PUT')
    expect(write.body).toEqual({ content: '# C', base_revision: 2 })
    expect(write.url.searchParams.get('path')).toBe('c.md')

    expect(call(spy, 2).init.method).toBe('DELETE')

    const folder = call(spy, 3)
    expect(folder.url.pathname).toBe('/api/v1/projects/demo/docs/folder')
    expect(folder.url.searchParams.get('path')).toBe('guides/')
  })

  it('moves, lists revisions, reads one and restores it', async () => {
    const spy = stubFetch(() => jsonResponse({}))
    await docsApi.move('demo', { scope: 'project', from_path: 'a.md', to_path: 'b.md' })
    await docsApi.revisions('demo', 'project', 'b.md')
    await docsApi.revision('demo', 'r/1')
    await docsApi.restore('demo', 'r/1')
    await docsApi.restore('demo', 'r-2', 'why')

    expect(call(spy, 0).init.method).toBe('POST')
    expect(call(spy, 0).body).toEqual({ scope: 'project', from_path: 'a.md', to_path: 'b.md' })
    expect(call(spy, 1).url.pathname).toBe('/api/v1/projects/demo/docs/revisions')
    expect(call(spy, 2).url.pathname).toBe('/api/v1/projects/demo/docs/revisions/r%2F1')
    expect(call(spy, 3).url.pathname).toBe('/api/v1/projects/demo/docs/revisions/r%2F1/restore')
    expect(call(spy, 3).body).toEqual({ message: '' })
    expect(call(spy, 4).body).toEqual({ message: 'why' })
  })

  it('drops unset search and backlink params', async () => {
    const spy = stubFetch(() => jsonResponse({ items: [] }))
    await docsApi.search('demo', { q: 'funnel', limit: 5 })
    await docsApi.backlinks('demo', { kind: 'event', name: 'signup', qualifier: null })
    await docsApi.backlinks('demo', { kind: 'field', name: 'plan', qualifier: 'checkout' })

    expect(Object.fromEntries(call(spy, 0).url.searchParams)).toEqual({ q: 'funnel', limit: '5' })
    expect(Object.fromEntries(call(spy, 1).url.searchParams)).toEqual({ kind: 'event', name: 'signup' })
    expect(call(spy, 2).url.searchParams.get('qualifier')).toBe('checkout')
  })

  it('resolves link refs as repeated params, and skips the request for none', async () => {
    const spy = stubFetch(() => jsonResponse([]))
    await expect(docsApi.links('demo', [])).resolves.toEqual([])
    expect(spy).not.toHaveBeenCalled()
    await docsApi.links('demo', ['event:a', 'field:b&c'])
    expect(call(spy).url.searchParams.getAll('ref')).toEqual(['event:a', 'field:b&c'])
  })

  it('exports and imports a JSON bundle', async () => {
    const spy = stubFetch(() => jsonResponse({}))
    await docsApi.exportJson('demo', 'project')
    await docsApi.importJson('demo', { scope: 'organization', mode: 'mirror', dryRun: true }, {
      format: 'tripl-docs/v1',
      files: [],
    })
    expect(Object.fromEntries(call(spy, 0).url.searchParams)).toEqual({ scope: 'project', format: 'json' })
    const imp = call(spy, 1)
    expect(imp.url.pathname).toBe('/api/v1/projects/demo/docs/import')
    expect(Object.fromEntries(imp.url.searchParams)).toEqual({ scope: 'organization', mode: 'mirror', dry_run: 'true' })
    expect(imp.body).toEqual({ format: 'tripl-docs/v1', files: [] })
  })
})

describe('linkRefQuery', () => {
  it('serialises refs as repeated ref params', () => {
    expect(linkRefQuery(['event:a', 'event_type:b'])).toBe('ref=event%3Aa&ref=event_type%3Ab')
    expect(linkRefQuery([])).toBe('')
  })
})

describe('docsApi raw requests (zip)', () => {
  it('downloads a zip under the Content-Disposition filename with a request id', async () => {
    const spy = stubFetch(
      new Response('PK', { status: 200, headers: { 'Content-Disposition': 'attachment; filename="acme-docs.zip"' } }),
    )
    const { blob, filename } = await docsApi.exportZip('demo', 'organization')
    expect(filename).toBe('acme-docs.zip')
    expect(await blob.text()).toBe('PK')
    const { url, init } = call(spy)
    expect(Object.fromEntries(url.searchParams)).toEqual({ scope: 'organization', format: 'zip' })
    expect(init.credentials).toBe('include')
    expect(new Headers(init.headers).get('X-Request-ID')).toBeTruthy()
  })

  it('falls back to <slug>-docs.zip without a Content-Disposition', async () => {
    stubFetch(new Response('PK', { status: 200 }))
    expect((await docsApi.exportZip('demo', 'project')).filename).toBe('demo-docs.zip')
  })

  it('uploads a zip as multipart with every import flag', async () => {
    const spy = stubFetch(jsonResponse({ created: ['SKILL.md'] }))
    const file = new Blob(['PK'])
    const result = await docsApi.importZip(
      'demo',
      { scope: 'project', mode: 'merge', dryRun: false, keepRoot: true },
      file,
    )
    expect(result).toEqual({ created: ['SKILL.md'] })
    const { url, init } = call(spy)
    expect(url.pathname).toBe('/api/v1/projects/demo/docs/import/zip')
    expect(Object.fromEntries(url.searchParams)).toEqual({
      scope: 'project',
      mode: 'merge',
      dry_run: 'false',
      keep_root: 'true',
    })
    expect(init.method).toBe('POST')
    expect(init.body).toBeInstanceOf(FormData)
    expect((init.body as FormData).get('file')).toBeTruthy()
  })

  it('raises an ApiError with the string detail and request id', async () => {
    stubFetch(jsonResponse({ detail: 'Scope is empty' }, 404, { 'X-Request-ID': 'req-9' }))
    const err = await docsApi.exportZip('demo', 'project').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err).toMatchObject({ message: 'Scope is empty', status: 404, requestId: 'req-9' })
  })

  it('keeps a structured detail for the import error report', async () => {
    const detail = { errors: [{ path: 'a.md', detail: 'too big' }] }
    stubFetch(jsonResponse({ detail }, 422))
    const err = (await docsApi
      .importZip('demo', { scope: 'project', mode: 'merge', dryRun: true, keepRoot: false }, new Blob(['PK']))
      .catch((e: unknown) => e)) as ApiError
    expect(err.status).toBe(422)
    expect(err.message).toBe('422 ')
    expect(err.detail).toEqual(detail)
  })

  it('uses the status line when the error body is not JSON', async () => {
    stubFetch(new Response('<html>', { status: 502, statusText: 'Bad Gateway' }))
    const err = (await docsApi.exportZip('demo', 'project').catch((e: unknown) => e)) as ApiError
    expect(err.message).toBe('502 Bad Gateway')
    expect(err.detail).toBeUndefined()
  })

  it('asks the user to sign in again on a 401', async () => {
    stubFetch(jsonResponse({ detail: 'Not signed in' }, 401))
    const onUnauthorized = vi.fn()
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, onUnauthorized)
    await expect(docsApi.exportZip('demo', 'project')).rejects.toThrow('Not signed in')
    window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, onUnauthorized)
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('reports an unreachable backend as a 503', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'))
    const err = (await docsApi.exportZip('demo', 'project').catch((e: unknown) => e)) as ApiError
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(503)
    expect(err.message).toMatch(/Backend is unavailable/)
  })
})
