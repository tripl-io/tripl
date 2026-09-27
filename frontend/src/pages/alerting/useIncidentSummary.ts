import { useCallback, useEffect } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { incidentSummaryApi, type IncidentSummaryResponse } from '@/api/incidentSummary'
import { useAiStatus } from '@/hooks/useAiStatus'

/** Local on purpose: the summary is read and written only through this hook. */
export function incidentSummaryKey(slug: string, correlationGroupId: string) {
  return ['incidentSummary', slug, correlationGroupId] as const
}

/**
 * The facts hash `ensure` was last fired for, kept in the query cache so it
 * outlives the component: a remount (a filter change, paging, a revisit of the
 * signal page) does not pay for another generation of the same facts. The
 * entry has no observer, so it is dropped after the cache's gc time and a
 * later visit may try once more.
 */
function incidentSummaryEnsuredKey(slug: string, correlationGroupId: string) {
  return ['incidentSummaryEnsured', slug, correlationGroupId] as const
}

/**
 * The summary of one incident (F14, #267).
 *
 * GET never calls the LLM. When it answers "missing" or "stale", this fires
 * `ensure` ONCE per facts hash — the attempted hash lives in the query cache
 * (`incidentSummaryEnsuredKey`), so a re-render, a refetch or a remount of the
 * same state does not pay for a second generation — and writes the result into
 * the query cache. Once that attempt has settled without a body,
 * `ensureAttempted` is true and the panel offers Retry instead of waiting.
 *
 * `isDemo` turns everything off: a demo project never sends data to a model.
 *
 * `active` gates the fetch: the inbox card passes it only once its disclosure
 * is open, so a page of 20 cards never asks for 20 summaries.
 *
 * A generation that fails ("failed", or a transport error) never replaces a
 * body already on screen: the stale or previous summary stays, and
 * `updateFailed` says the refresh did not happen.
 */
export function useIncidentSummary({
  slug,
  correlationGroupId,
  active,
  isDemo = false,
}: {
  slug: string
  correlationGroupId: string
  active: boolean
  isDemo?: boolean
}) {
  const aiEnabled = useAiStatus(slug) && !isDemo
  const queryClient = useQueryClient()
  const queryKey = incidentSummaryKey(slug, correlationGroupId)

  const query = useQuery({
    queryKey,
    queryFn: () => incidentSummaryApi.get(slug, correlationGroupId),
    enabled: aiEnabled && active && !!slug && !!correlationGroupId,
    staleTime: 60 * 1000,
    retry: 1,
  })

  const store = useCallback(
    (result: IncidentSummaryResponse) => {
      queryClient.setQueryData<IncidentSummaryResponse>(
        incidentSummaryKey(slug, correlationGroupId),
        previous =>
          // Keep a body the reader already has over a failure that has none.
          result.state === 'failed' && previous?.summary ? previous : result,
      )
    },
    [queryClient, slug, correlationGroupId],
  )

  const ensure = useMutation({
    mutationFn: () => incidentSummaryApi.ensure(slug, correlationGroupId),
    onSuccess: store,
  })
  const regenerate = useMutation({
    mutationFn: () => incidentSummaryApi.regenerate(slug, correlationGroupId),
    onSuccess: store,
  })

  const data = query.data
  const needsEnsure =
    aiEnabled && active && (data?.state === 'missing' || data?.state === 'stale')
  const hash = data?.current_facts_hash ?? ''
  const { mutate: ensureMutate } = ensure
  useEffect(() => {
    if (!needsEnsure) return
    const ensuredKey = incidentSummaryEnsuredKey(slug, correlationGroupId)
    if (queryClient.getQueryData<string>(ensuredKey) === hash) return
    queryClient.setQueryData<string>(ensuredKey, hash)
    ensureMutate()
  }, [needsEnsure, hash, ensureMutate, queryClient, slug, correlationGroupId])
  const ensureAttempted =
    !ensure.isPending &&
    queryClient.getQueryData<string>(incidentSummaryEnsuredKey(slug, correlationGroupId)) === hash

  const lastUpdate = regenerate.isIdle ? ensure : regenerate
  const updateFailed =
    lastUpdate.isError || (lastUpdate.data?.state === 'failed' && !!data?.summary)

  return {
    aiEnabled,
    data,
    isLoading: query.isLoading,
    isError: query.isError,
    error: query.error,
    refetch: query.refetch,
    isEnsuring: ensure.isPending,
    ensureError: ensure.error,
    ensureAttempted,
    retry: () => ensure.mutate(),
    regenerate: () => regenerate.mutate(),
    isRegenerating: regenerate.isPending,
    updateFailed,
  }
}
