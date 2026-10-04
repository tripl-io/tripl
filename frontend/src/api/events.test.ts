import { afterEach, describe, expect, it, vi } from 'vitest'
import { eventListSearchParams, eventsApi } from './events'
import { at } from '@/test/at'

// The real client is exercised; only global fetch is stubbed, so the URL
// asserted here is the one the list request actually sends.

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('eventsApi.list', () => {
  it('sends the open-questions filter to the server', async () => {
    // The hand-written params type had no `has_open_questions`, so "Questions →
    // Open questions" reached the URL and saved views and never the request.
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse({ items: [], total: 0 }))

    await eventsApi.list('demo', { has_open_questions: true, reviewed: false }, 'branch-1')

    const url = new URL(String(at(fetchSpy.mock.calls, 0)[0]), 'http://localhost')
    expect(url.pathname).toBe('/api/v1/projects/demo/events')
    expect(url.searchParams.get('has_open_questions')).toBe('true')
    expect(url.searchParams.get('reviewed')).toBe('false')
    expect(url.searchParams.get('branch')).toBe('branch-1')
  })

  it('passes the caller\'s abort signal to the request', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse({ items: [], total: 0 }))
    const controller = new AbortController()

    await eventsApi.list('demo', { search: 'signup' }, null, controller.signal)

    const init = at(fetchSpy.mock.calls, 0)[1]
    expect(init?.signal).toBe(controller.signal)
  })
})

describe('eventListSearchParams', () => {
  it('serializes every parameter it is given, arrays as repeated keys', () => {
    const sp = eventListSearchParams({
      event_type_id: 'et-1',
      search: 'checkout',
      status: ['draft', 'live'],
      tag: 'web',
      silent_since_days: 0,
      reviewed: true,
      has_open_questions: false,
      field_value: 'x',
      meta_value: 'y',
      order_by: 'volume',
      offset: 0,
      limit: 200,
    })

    expect(sp.getAll('status')).toEqual(['draft', 'live'])
    expect(sp.get('silent_since_days')).toBe('0')
    expect(sp.get('has_open_questions')).toBe('false')
    expect(sp.get('offset')).toBe('0')
    expect(sp.get('field_value')).toBe('x')
    expect(sp.get('meta_value')).toBe('y')
    expect(sp.get('order_by')).toBe('volume')
  })

  it('treats empty strings and empty arrays as "no filter"', () => {
    expect(eventListSearchParams({ search: '', status: [], tag: undefined }).toString()).toBe('')
  })
})
