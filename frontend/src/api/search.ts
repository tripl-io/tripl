import { api, withBranch } from './client'
import type { SearchEntityType, SearchResponse } from '../types'

type SearchParams = {
  q: string
  types?: SearchEntityType[]
  include_archived?: boolean
  limit?: number
  /** `false` skips the embedding leg: a keyword-only answer, much sooner. */
  semantic?: boolean
  /**
   * Fold events that differ only in one naming-rule placeholder into their
   * best-ranked member, which then carries `variant_group` (#238).
   */
  group_variants?: boolean
}

export const searchApi = {
  search: (
    slug: string,
    params: SearchParams,
    branchId?: string | null,
    signal?: AbortSignal,
  ) => {
    const sp = new URLSearchParams()
    sp.set('q', params.q)
    params.types?.forEach(type => sp.append('types', type))
    if (params.include_archived !== undefined) {
      sp.set('include_archived', String(params.include_archived))
    }
    if (params.limit !== undefined) sp.set('limit', String(params.limit))
    if (params.semantic !== undefined) sp.set('semantic', String(params.semantic))
    if (params.group_variants !== undefined) {
      sp.set('group_variants', String(params.group_variants))
    }
    return api.get<SearchResponse>(
      withBranch(`/projects/${slug}/search?${sp.toString()}`, branchId),
      signal,
    )
  },
  reindex: (slug: string, branchId?: string | null) =>
    api.post<{ documents_indexed: number; embeddings_scheduled: boolean }>(
      withBranch(`/projects/${slug}/search/reindex`, branchId),
    ),
}
