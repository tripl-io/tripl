import type { Page, Request, Route } from '@playwright/test'

type Fulfill = Parameters<Route['fulfill']>[0]

/**
 * What a fully mocked spec answers for one API call, keyed by
 * `"<METHOD> <path>"`. The path drops `/api/v1` and an `/orgs/{org}` prefix, so
 * `GET /projects/demo/health` matches the org-scoped request the app sends as
 * well as the org-less one; the query string is not part of the key.
 */
export type ApiMocks = Record<string, (request: Request) => Fulfill | Promise<Fulfill>>

/** `/api/v1/orgs/acme/projects/x?y=1` -> `/projects/x`. */
export function mockKeyPath(url: string): string {
  const path = new URL(url).pathname.replace(/^\/api\/v1/, '')
  return path.replace(/^\/orgs\/[^/]+(?=\/)/, '')
}

/**
 * Answer every `/api/v1` call from `mocks`, and refuse anything else loudly.
 *
 * A catch-all that answers `[]` (or 404) to every call it does not know hides
 * what the page really asked for: the public-demo invitation spec fed `[]` to
 * the Overview's plan-health panel, which reads an object, and the page crashed
 * for a reason the spec did not show. Here an unmocked call gets a 501 whose
 * body names it, and is recorded; call the returned `assertAllMocked` at the
 * end of the test so a new request the page starts making fails the spec with
 * its method and URL instead of being papered over.
 */
export async function mockApi(page: Page, mocks: ApiMocks): Promise<{ assertAllMocked: () => void }> {
  const unexpected: string[] = []
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const key = `${request.method()} ${mockKeyPath(request.url())}`
    const answer = mocks[key]
    if (answer) return route.fulfill(await answer(request))
    const call = `${request.method()} ${new URL(request.url()).pathname}${new URL(request.url()).search}`
    unexpected.push(call)
    return route.fulfill({ status: 501, json: { detail: `Unmocked API call in an e2e spec: ${call}` } })
  })
  return {
    assertAllMocked: () => {
      if (unexpected.length > 0) {
        throw new Error(`Unmocked API calls (add an explicit mock for each):\n${[...new Set(unexpected)].join('\n')}`)
      }
    },
  }
}
