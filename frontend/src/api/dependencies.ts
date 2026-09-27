import { api, withBranch } from './client'
import type {
  DependenciesResponse,
  DependencyEntityKind,
  ImpactChange,
  ImpactResponse,
} from '../types'

/**
 * Dependency graph and impact analysis (F04, #257). Read-only: nothing here
 * changes what a delete, deprecate or rename does server-side — the answers
 * only feed the "Used by" sections, the confirm dialogs and a branch's Impact
 * panel. Resolved on the branch passed (`?branch=`), main otherwise.
 */
export const dependenciesApi = {
  get: (
    slug: string,
    entity: { kind: DependencyEntityKind; id: string },
    options: { depth?: 1 | 2; branchId?: string | null } = {},
    signal?: AbortSignal,
  ): Promise<DependenciesResponse> => {
    const sp = new URLSearchParams({ entity: `${entity.kind}:${entity.id}` })
    if (options.depth) sp.set('depth', String(options.depth))
    return api.get<DependenciesResponse>(
      withBranch(`/projects/${slug}/dependencies?${sp.toString()}`, options.branchId),
      signal,
    )
  },

  impact: (
    slug: string,
    changes: readonly ImpactChange[],
    branchId?: string | null,
    signal?: AbortSignal,
  ): Promise<ImpactResponse> =>
    api.post<ImpactResponse>(
      withBranch(`/projects/${slug}/impact`, branchId),
      { changes },
      signal,
    ),

  branchImpact: (slug: string, branchId: string, signal?: AbortSignal): Promise<ImpactResponse> =>
    api.get<ImpactResponse>(`/projects/${slug}/branches/${branchId}/impact`, signal),
}
