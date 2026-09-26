import { api } from './client'
import type { SourceFreshnessItem } from '../types'

/**
 * `GET /projects/{slug}/source-freshness` (F16, #269): every scan's source
 * freshness, one request for the Overview's source health, the data source
 * cards and the "drop signals held" notices.
 *
 * Its own module rather than a `scansApi` method so the pages that mock
 * `@/api/scans` wholesale in their tests do not lose this call to the mock.
 * Accepts a bare list or an `{ items }` envelope: the reader only needs the
 * rows.
 */
export const sourceFreshnessApi = {
  list: async (slug: string, signal?: AbortSignal): Promise<SourceFreshnessItem[]> => {
    const response = await api.get<SourceFreshnessItem[] | { items: SourceFreshnessItem[] }>(
      `/projects/${slug}/source-freshness`,
      signal,
    )
    return Array.isArray(response) ? response : (response.items ?? [])
  },
}
