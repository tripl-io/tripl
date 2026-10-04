import type { InfiniteData, QueryClient, QueryKey } from '@tanstack/react-query'

/**
 * Query `meta` key the events table sets on its infinite list: a function that
 * says whether the rows on screen all come from the list's first page.
 */
export const FIRST_PAGE_IN_VIEW_META = 'isFirstPageInView'

function isInfiniteData(data: unknown): data is InfiniteData<unknown> {
  return typeof data === 'object' && data !== null && 'pages' in data && 'pageParams' in data
}

/**
 * Reconcile the events lists under `queryKey` with the server.
 *
 * Invalidating an infinite query re-requests EVERY loaded page, one after
 * another, so after scrolling 12 pages each bulk action fired 12 sequential
 * 200-row requests. A list is cut back to its first page first when
 * nobody would see the cut: no mounted table shows it, or the table's viewport
 * sits inside page 0 — the rest refill on demand as it scrolls, exactly as they
 * loaded the first time. A table scrolled past page 0 refetches its pages in
 * place instead; cutting there blanked the rows on screen until each page came
 * back in turn, and under a column filter shrank the spacer and jumped the
 * scroll position.
 *
 * Mutations and the realtime stream both go through here, so the two agree.
 */
export function refreshEventsLists(qc: QueryClient, queryKey: QueryKey): Promise<void> {
  for (const query of qc.getQueryCache().findAll({ queryKey })) {
    const data = query.state.data
    if (!isInfiniteData(data) || data.pages.length <= 1) continue
    const firstPageInView = query.meta?.[FIRST_PAGE_IN_VIEW_META]
    const unseen =
      !query.isActive()
      || (typeof firstPageInView === 'function' && firstPageInView() === true)
    if (!unseen) continue
    qc.setQueryData<InfiniteData<unknown>>(query.queryKey, {
      pages: data.pages.slice(0, 1),
      pageParams: data.pageParams.slice(0, 1),
    })
  }
  return qc.invalidateQueries({ queryKey })
}
