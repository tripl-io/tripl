import { useQuery } from '@tanstack/react-query'
import { dependenciesApi } from '@/api/dependencies'
import { useActiveBranchId } from '@/hooks/useBranch'
import { IMPACT_MAX_CHANGES, impactChangesId } from '@/lib/dependencies'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { branchImpactKey, entityDependenciesKey, impactKey } from '@/lib/queryKeys'
import type { DependencyEntityKind, ImpactChange } from '@/types'

/**
 * One entity's dependencies on the branch on screen (main when none). A
 * failure is said by the section itself, so the global error toast stays
 * quiet: a "Used by" list is an aid, not the page.
 */
export function useEntityDependencies(
  slug: string | undefined,
  entity: { kind: DependencyEntityKind; id: string } | null,
  options: { depth?: 1 | 2; branchId?: string | null; enabled?: boolean } = {},
) {
  const activeBranchId = useActiveBranchId()
  const branchId = options.branchId === undefined ? activeBranchId : options.branchId
  const depth = options.depth ?? 1
  const entityId = entity ? `${entity.kind}:${entity.id}` : ''
  return useQuery({
    queryKey: entityDependenciesKey(slug, branchId, entityId, depth),
    queryFn: ({ signal }) => dependenciesApi.get(slug!, entity!, { depth, branchId }, signal),
    enabled: !!slug && !!entity && (options.enabled ?? true),
    meta: SILENT_ERROR_META,
    staleTime: 30_000,
  })
}

/**
 * What a planned change set touches (POST /impact). Capped at
 * {@link IMPACT_MAX_CHANGES} ids; `truncated` says whether the cap cut it.
 */
export function useImpact(
  slug: string | undefined,
  changes: readonly ImpactChange[],
  options: { branchId?: string | null; enabled?: boolean } = {},
) {
  const activeBranchId = useActiveBranchId()
  const branchId = options.branchId === undefined ? activeBranchId : options.branchId
  const sent = changes.slice(0, IMPACT_MAX_CHANGES)
  const query = useQuery({
    queryKey: impactKey(slug, branchId, impactChangesId(sent)),
    queryFn: ({ signal }) => dependenciesApi.impact(slug!, sent, branchId, signal),
    enabled: !!slug && sent.length > 0 && (options.enabled ?? true),
    meta: SILENT_ERROR_META,
    staleTime: 15_000,
  })
  return { ...query, truncated: changes.length > sent.length, checkedCount: sent.length }
}

/** A branch's impact computed from its diff (GET /branches/{id}/impact). */
export function useBranchImpact(slug: string, branchId: string) {
  return useQuery({
    queryKey: branchImpactKey(slug, branchId),
    queryFn: ({ signal }) => dependenciesApi.branchImpact(slug, branchId, signal),
    meta: SILENT_ERROR_META,
  })
}
