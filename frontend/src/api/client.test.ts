// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, AUTH_UNAUTHORIZED_EVENT } from './client'

// Exercise the REAL client (no vi.mock of './client'); only global fetch is
// stubbed with crafted Response objects so error mapping is fully covered.

interface ResponseOptions {
  status: number
  statusText?: string
  body?: unknown
  headers?: Record<string, string>
}

function makeResponse({ status, statusText = '', body = {}, headers = {} }: ResponseOptions): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText,
    headers: new Headers(headers),
    json: async () => body,
  } as unknown as Response
}

function mockFetchOnce(options: ResponseOptions) {
  vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(makeResponse(options))
}


afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('api request success', () => {
  it('returns parsed JSON on 200', async () => {
    mockFetchOnce({ status: 200, body: { id: 'x' } })
    await expect(api.get<{ id: string }>('/things')).resolves.toEqual({ id: 'x' })
  })

  it('returns undefined on 204 No Content', async () => {
    mockFetchOnce({ status: 204 })
    await expect(api.del('/things/1')).resolves.toBeUndefined()
  })
})

describe('outside a secure context', () => {
  it('still sends a request id when crypto.randomUUID is missing (plain HTTP)', async () => {
    const real = globalThis.crypto
    vi.stubGlobal('crypto', { getRandomValues: real.getRandomValues.bind(real) })
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      makeResponse({ status: 200, body: { ok: true } }),
    )

    await expect(api.get('/auth/me')).resolves.toEqual({ ok: true })
    const headers = fetchSpy.mock.calls[0]![1]!.headers as Headers
    expect(headers.get('X-Request-ID')).toMatch(/^[0-9a-f-]{36}$/)
  })
})

describe('cancellation', () => {
  it('forwards the caller\'s AbortSignal to fetch', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      makeResponse({ status: 200, body: {} }),
    )
    const controller = new AbortController()

    await api.get('/things', controller.signal)
    expect(fetchSpy.mock.calls[0]![1]!.signal).toBe(controller.signal)
  })
})

describe('422 validation mapping', () => {
  it('maps a FastAPI detail array to a readable message and exposes fields', async () => {
    const detail = [
      { loc: ['body', 'email'], msg: 'value is not a valid email address', type: 'value_error' },
      { loc: ['body', 'name'], msg: 'field required', type: 'missing' },
    ]
    mockFetchOnce({ status: 422, body: { detail } })

    const err = await api.post('/auth/register', {}).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    const apiErr = err as ApiError
    expect(apiErr.status).toBe(422)
    // "body" segment is dropped; entries joined with "; ".
    expect(apiErr.message).toBe(
      'email: value is not a valid email address; name: field required',
    )
    expect(apiErr.fields).toEqual(detail)
  })

  it('drops body/query segments and keeps msg when path is empty', async () => {
    const detail = [{ loc: ['body'], msg: 'invalid payload', type: 'value_error' }]
    mockFetchOnce({ status: 422, body: { detail } })

    const err = (await api.post('/things', {}).catch((e: unknown) => e)) as ApiError
    expect(err.message).toBe('invalid payload')
  })
})

describe('401 vs 403 distinction', () => {
  it('emits AUTH_UNAUTHORIZED_EVENT on a 401 for a non-/auth path', async () => {
    const handler = vi.fn()
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, handler)
    mockFetchOnce({ status: 401, statusText: 'Unauthorized', body: {} })

    const err = (await api.get('/projects').catch((e: unknown) => e)) as ApiError
    expect(err.status).toBe(401)
    expect(handler).toHaveBeenCalledTimes(1)
    window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, handler)
  })

  it('does NOT emit the unauthorized event on a 401 for an /auth path', async () => {
    const handler = vi.fn()
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, handler)
    mockFetchOnce({ status: 401, statusText: 'Unauthorized', body: {} })

    await api.get('/auth/me').catch(() => undefined)
    expect(handler).not.toHaveBeenCalled()
    window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, handler)
  })

  it('does NOT emit the unauthorized event on a 403 (authorization failure)', async () => {
    const handler = vi.fn()
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, handler)
    mockFetchOnce({ status: 403, statusText: 'Forbidden', body: { detail: 'forbidden' } })

    const err = (await api.get('/projects').catch((e: unknown) => e)) as ApiError
    expect(err.status).toBe(403)
    expect(err.message).toBe('forbidden')
    expect(handler).not.toHaveBeenCalled()
    window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, handler)
  })
})

describe('request_id surfacing on 5xx', () => {
  it('prefers the X-Request-ID response header', async () => {
    mockFetchOnce({
      status: 500,
      statusText: 'Internal Server Error',
      headers: { 'X-Request-ID': 'req-from-header' },
      body: { request_id: 'req-from-body' },
    })

    const err = (await api.get('/projects').catch((e: unknown) => e)) as ApiError
    expect(err.status).toBe(500)
    expect(err.requestId).toBe('req-from-header')
  })

  it('falls back to request_id in the 500 body when no header is present', async () => {
    mockFetchOnce({ status: 500, statusText: 'Internal Server Error', body: { request_id: 'req-99' } })

    const err = (await api.get('/projects').catch((e: unknown) => e)) as ApiError
    expect(err.requestId).toBe('req-99')
    // No body.detail -> "<status> <statusText>" message.
    expect(err.message).toBe('500 Internal Server Error')
  })
})

describe('502/503/504 gateway handling', () => {
  it('uses the default backend-unavailable message when no detail is given (503)', async () => {
    mockFetchOnce({ status: 503, statusText: 'Service Unavailable', body: {} })

    const err = (await api.get('/projects').catch((e: unknown) => e)) as ApiError
    expect(err.status).toBe(503)
    expect(err.message).toContain('Backend is unavailable')
  })

  it('uses a meaningful body.detail when the gateway supplies one (502)', async () => {
    mockFetchOnce({ status: 502, statusText: 'Bad Gateway', body: { detail: 'upstream exploded' } })

    const err = (await api.get('/projects').catch((e: unknown) => e)) as ApiError
    expect(err.status).toBe(502)
    expect(err.message).toBe('upstream exploded')
  })

  it('does NOT emit the unauthorized event for gateway errors', async () => {
    const handler = vi.fn()
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, handler)
    mockFetchOnce({ status: 503, statusText: 'Service Unavailable', body: {} })

    await api.get('/projects').catch(() => undefined)
    expect(handler).not.toHaveBeenCalled()
    window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, handler)
  })
})

describe('network and timeout failures', () => {
  it('maps a generic fetch rejection to a 503 backend-unavailable ApiError', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValueOnce(new TypeError('Failed to fetch'))

    const err = (await api.get('/projects').catch((e: unknown) => e)) as ApiError
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(503)
    expect(err.message).toContain('Backend is unavailable')
  })

  it('maps an AbortError to a 408 timeout ApiError', async () => {
    const abort = new Error('aborted')
    abort.name = 'AbortError'
    vi.spyOn(globalThis, 'fetch').mockRejectedValueOnce(abort)

    const err = (await api.get('/projects').catch((e: unknown) => e)) as ApiError
    expect(err.status).toBe(408)
    expect(err.message).toContain('timed out')
  })
})

describe('a gate refusal', () => {
  it('keeps the fields beside detail, which say how to get through', async () => {
    mockFetchOnce({
      status: 403,
      body: { detail: 'This organization requires single sign-on', sso_start: '/api/v1/auth/sso/acme/start' },
    })

    const err = (await api.get('/projects').catch((e: unknown) => e)) as ApiError
    expect(err.message).toBe('This organization requires single sign-on')
    expect(err.extra).toEqual({ sso_start: '/api/v1/auth/sso/acme/start' })
  })

  it('has no extra fields when the body carries only detail', async () => {
    mockFetchOnce({ status: 403, body: { detail: 'forbidden' } })

    const err = (await api.get('/projects').catch((e: unknown) => e)) as ApiError
    expect(err.extra).toBeUndefined()
  })
})
