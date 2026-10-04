/**
 * Paging for the two infinite alerting lists — the Inbox and an incident's
 * deliveries.
 *
 * Both endpoints return a keyset `next_cursor`: the next page starts strictly
 * after the last row already held, so a row that sorts down past the seam
 * between two requests (an incident acknowledged on page 1 under the 60 s
 * refetch) is still served instead of skipped, which offset paging could not
 * promise. The first page is still requested by offset 0; a response without
 * the field (an older server, or a fixture) falls back to offset paging.
 */

/** A page param: an offset, or a cursor from the page before. */
export type ListPageParam = number | string

export function listPageRequest(param: ListPageParam): { offset?: number; cursor?: string } {
  return typeof param === 'string' ? { cursor: param } : { offset: param }
}

interface PagedList {
  items: readonly unknown[]
  total: number
  next_cursor?: string | null
}

export function nextListPageParam(
  lastPage: PagedList,
  allPages: readonly PagedList[],
): ListPageParam | undefined {
  if (typeof lastPage.next_cursor === 'string') return lastPage.next_cursor
  // `null` is the server saying "last page" — trust it over a total that a
  // concurrent insert may have moved.
  if (lastPage.next_cursor === null) return undefined
  const loaded = allPages.reduce((sum, page) => sum + page.items.length, 0)
  return lastPage.items.length > 0 && loaded < lastPage.total ? loaded : undefined
}
