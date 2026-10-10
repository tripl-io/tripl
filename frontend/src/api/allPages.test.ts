import { afterEach, describe, expect, it, vi } from 'vitest'
import { getAllPages } from './allPages'
import { orgsApi } from './orgs'
import { ROSTER_PAGE_LIMIT, usersApi } from './users'

function jsonResponse(data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function urlOf(input: RequestInfo | URL): string {
  return typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
}

/** A list route of `total` rows that honours `limit` and `offset`, like the server's. */
function serveRows(total: number) {
  const requested: string[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
    const url = urlOf(input)
    requested.push(url)
    const params = new URL(url, 'http://localhost').searchParams
    const limit = Number(params.get('limit'))
    const offset = Number(params.get('offset'))
    const size = Math.max(0, Math.min(limit, total - offset))
    return Promise.resolve(jsonResponse(Array.from({ length: size }, (_, i) => offset + i)))
  })
  return requested
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('getAllPages', () => {
  it('keeps reading past a full page until a short one', async () => {
    const requested = serveRows(5)

    await expect(getAllPages<number>('/things', 2)).resolves.toEqual([0, 1, 2, 3, 4])
    expect(requested).toEqual([
      '/api/v1/things?limit=2&offset=0',
      '/api/v1/things?limit=2&offset=2',
      '/api/v1/things?limit=2&offset=4',
    ])
  })

  it('asks once more after an exactly full last page, and stops on the empty one', async () => {
    const requested = serveRows(4)

    await expect(getAllPages<number>('/things', 2)).resolves.toEqual([0, 1, 2, 3])
    expect(requested).toHaveLength(3)
  })

  it('adds its parameters to a path that already has a query', async () => {
    const requested = serveRows(1)

    await getAllPages<number>('/things?kind=a', 10)
    expect(requested).toEqual(['/api/v1/things?kind=a&limit=10&offset=0'])
  })

  it('fails as a whole when a later page fails, rather than returning part of the list', async () => {
    let calls = 0
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => {
      calls += 1
      return Promise.resolve(
        calls === 1
          ? jsonResponse([1, 2])
          : new Response(JSON.stringify({ detail: 'boom' }), { status: 500 }),
      )
    })

    await expect(getAllPages<number>('/things', 2)).rejects.toThrow()
  })
})

// The roster stopped at the route's default 200 rows: member 201 had no name,
// no row on the Members page and no place in any member picker.
describe('the member roster', () => {
  it('reads every member of the default organization, a page at a time', async () => {
    const requested = serveRows(ROSTER_PAGE_LIMIT + 1)

    const members = await usersApi.list()
    expect(members).toHaveLength(ROSTER_PAGE_LIMIT + 1)
    expect(requested).toEqual([
      `/api/v1/users?limit=${ROSTER_PAGE_LIMIT}&offset=0`,
      `/api/v1/users?limit=${ROSTER_PAGE_LIMIT}&offset=${ROSTER_PAGE_LIMIT}`,
    ])
  })

  it("reads every member of a named organization the same way", async () => {
    const requested = serveRows(3)

    await expect(orgsApi.members('acme')).resolves.toHaveLength(3)
    expect(requested).toEqual([`/api/v1/orgs/acme/members?limit=${ROSTER_PAGE_LIMIT}&offset=0`])
  })
})
