import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { subscriptionsApi } from '@/api/notifications'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { keySegment, projectSubscriptionsKey, subscriptionKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type { SubscriptionEntityType, SubscriptionState } from '@/types'

/** What the buttons read: the answer, if it has the state's shape. */
export interface WatchState {
  watching: boolean
  muted: boolean
  reasons: SubscriptionState['reasons']
}

function asState(data: unknown): WatchState | undefined {
  if (!data || typeof data !== 'object') return undefined
  const row = data as Partial<SubscriptionState>
  if (typeof row.watching !== 'boolean') return undefined
  return {
    watching: row.watching,
    muted: row.muted === true,
    reasons: Array.isArray(row.reasons) ? row.reasons : [],
  }
}

/**
 * The reader's watch state on one entity, and the three things they can do
 * with it: watch, unwatch, mute (#259). Each write answers with the new state,
 * which replaces the cache.
 */
export function useSubscription(
  slug: string | undefined,
  entityType: SubscriptionEntityType,
  entityId: string | undefined,
) {
  const qc = useQueryClient()
  const key = subscriptionKey(slug, entityType, entityId)
  const enabled = !!slug && !!entityId
  const query = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => subscriptionsApi.get(slug as string, entityType, entityId as string, signal),
    enabled,
    meta: SILENT_ERROR_META,
    staleTime: 30_000,
  })

  // The answer replaces this cache; the rest of the project's states are
  // refetched, since a branch copy's thread and its main twin's page read the
  // same watch under two ids.
  const store = (next: SubscriptionState) => {
    qc.setQueryData(key, next)
    void qc.invalidateQueries({
      queryKey: projectSubscriptionsKey(slug),
      predicate: query =>
        keySegment(query.queryKey, 2) !== entityType || keySegment(query.queryKey, 3) !== entityId,
    })
  }

  const watchMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (watch: boolean) =>
      watch
        ? subscriptionsApi.watch(slug as string, entityType, entityId as string)
        : subscriptionsApi.unwatch(slug as string, entityType, entityId as string),
    onSuccess: store,
    onError: (error, watch) =>
      toast.error(`Could not ${watch ? 'watch' : 'unwatch'} — ${getErrorMessage(error)}`),
  })

  const muteMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (muted: boolean) =>
      subscriptionsApi.setMuted(slug as string, entityType, entityId as string, muted),
    onSuccess: store,
    onError: (error, muted) =>
      toast.error(`Could not ${muted ? 'mute' : 'unmute'} the thread — ${getErrorMessage(error)}`),
  })

  return {
    state: asState(query.data),
    isLoading: enabled && query.isPending,
    isError: query.isError,
    isPending: watchMut.isPending || muteMut.isPending,
    setWatching: (watch: boolean) => watchMut.mutate(watch),
    setMuted: (muted: boolean) => muteMut.mutate(muted),
  }
}
