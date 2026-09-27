import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { duplicatesApi, MAX_DUPLICATE_CANDIDATES } from '@/api/duplicates'
import { useDebouncedValue } from '@/hooks/useDebouncedValue'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { duplicateCheckKey } from '@/lib/queryKeys'
import type { DuplicateCandidate, DuplicateCheckResult } from '@/types'

/** How long typing has to pause before the catalog is asked (F12, #265). */
export const DUPLICATE_CHECK_DEBOUNCE_MS = 400

const NO_CANDIDATES = '[]'

export interface DuplicateCheckState {
  /**
   * One result per candidate sent, in order — undefined until the answer for
   * what is on screen NOW has arrived, so a hint about the previous name is
   * never shown under the next one.
   */
  results: DuplicateCheckResult[] | undefined
  /** Typing has not paused yet, or the request is in flight. */
  checking: boolean
  /** Advisory only: a failure hides the hints and never blocks a save. */
  failed: boolean
}

/**
 * Ask whether would-be events look like ones the catalog already has, and how
 * their names depart from its convention. Debounced; a request whose key has
 * moved on is cancelled (react-query aborts a query that loses its last
 * observer once its `signal` has been read), so a fast typist leaves no trail
 * of stale answers racing the current one.
 *
 * Candidates without a name or a type are not sent; `results` still lines up
 * with the list passed in, holding an empty result at their positions.
 */
export function useDuplicateCheck({
  slug,
  branchId,
  candidates,
  enabled = true,
  debounceMs = DUPLICATE_CHECK_DEBOUNCE_MS,
}: {
  slug: string | undefined
  branchId: string | null | undefined
  candidates: readonly DuplicateCandidate[]
  enabled?: boolean
  debounceMs?: number
}): DuplicateCheckState {
  // Which positions go out, and the body they make, as one comparable string.
  const { payload, positions } = useMemo(() => {
    const sent: DuplicateCandidate[] = []
    const at: number[] = []
    candidates.forEach((candidate, index) => {
      if (sent.length >= MAX_DUPLICATE_CANDIDATES) return
      if (!candidate.name.trim() || !candidate.event_type_id) return
      sent.push(candidate)
      at.push(index)
    })
    return { payload: JSON.stringify(sent), positions: at }
  }, [candidates])
  const debounced = useDebouncedValue(payload, debounceMs)
  const settled = debounced === payload
  const active = enabled && !!slug && debounced !== NO_CANDIDATES

  const query = useQuery({
    queryKey: duplicateCheckKey(slug, branchId, debounced),
    queryFn: ({ signal }) =>
      duplicatesApi.check(slug!, JSON.parse(debounced) as DuplicateCandidate[], branchId, signal),
    enabled: active,
    staleTime: 30_000,
    retry: false,
    meta: SILENT_ERROR_META,
  })

  const results = useMemo(() => {
    if (!enabled || !settled || !query.data) return undefined
    const out: DuplicateCheckResult[] = candidates.map(() => ({ duplicates: [], lint: [] }))
    positions.forEach((index, i) => {
      const result = query.data.items[i]
      if (result) out[index] = result
    })
    return out
  }, [enabled, settled, query.data, candidates, positions])

  return {
    results,
    checking: enabled && payload !== NO_CANDIDATES && (!settled || (active && query.isFetching)),
    failed: active && settled && query.isError,
  }
}
