import { api, withBranch } from './client'
import type {
  DuplicateCandidate,
  DuplicateCheckResponse,
  DuplicateClustersResponse,
  DuplicateDismissBody,
  DuplicateDismissResponse,
} from '../types'

/** The most candidates one duplicate-check request takes (the route's cap). */
export const MAX_DUPLICATE_CANDIDATES = 500

/**
 * Duplicate detection and naming-convention lint (F12, #265). The check is a
 * read that happens to be a POST (the candidates do not fit a query string):
 * any project member may ask, a viewer included. Resolved on the branch
 * passed (`?branch=`), main otherwise.
 *
 * There is no merge endpoint on purpose: "merge" is the existing event update
 * (successor + deprecate), so it goes through `eventsApi.update`.
 */
export const duplicatesApi = {
  check: (
    slug: string,
    candidates: readonly DuplicateCandidate[],
    branchId?: string | null,
    signal?: AbortSignal,
  ): Promise<DuplicateCheckResponse> =>
    api.post<DuplicateCheckResponse>(
      withBranch(`/projects/${slug}/events/duplicate-check`, branchId),
      { candidates: candidates.slice(0, MAX_DUPLICATE_CANDIDATES) },
      signal,
    ),

  clusters: (
    slug: string,
    cursor?: string | null,
    branchId?: string | null,
    signal?: AbortSignal,
  ): Promise<DuplicateClustersResponse> => {
    const qs = cursor ? `?${new URLSearchParams({ cursor }).toString()}` : ''
    return api.get<DuplicateClustersResponse>(
      withBranch(`/projects/${slug}/duplicates${qs}`, branchId),
      signal,
    )
  },

  /** "Not a duplicate": the pair stops being reported. Editor action. */
  dismiss: (
    slug: string,
    body: DuplicateDismissBody,
    branchId?: string | null,
  ): Promise<DuplicateDismissResponse> =>
    api.post<DuplicateDismissResponse>(withBranch(`/projects/${slug}/duplicates/dismiss`, branchId), body),
}
