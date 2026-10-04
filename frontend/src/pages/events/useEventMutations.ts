import { useCallback, useMemo } from 'react'
import {
  useMutation,
  useQueryClient,
  type InfiniteData,
  type QueryKey,
} from '@tanstack/react-query'

import { eventsApi } from '@/api/events'
import { refreshEventsLists } from '@/lib/eventsListCache'
import type { EventStatus } from '@/lib/eventStatus'
import type { EventListItem, EventListResponse } from '@/types'
import { branchEventsKey, projectHealthRootKey } from '@/lib/queryKeys'

// Both shapes coexist under the `['events', slug, ...]` prefix: the main
// table uses `useInfiniteQuery` (InfiniteData), and in-review-count / alerting
// pages use a flat `EventListResponse`. Mutations need to update every cache
// they touch so the optimistic patch stays consistent.
type EventsQueryData = EventListResponse | InfiniteData<EventListResponse>
type Snapshot = readonly [QueryKey, EventsQueryData | undefined]

export type EventMutations = ReturnType<typeof useEventMutations>

/** The fields a bulk update can set, as the table's bar sends them. */
export type BulkUpdatePatch = {
  status?: EventStatus
  sunset_at?: string | null
  reviewed?: boolean
  owner_id?: string | null
}

type BulkUpdateVars = { eventIds: string[] } & BulkUpdatePatch

type PreviousValues = Map<string, Pick<EventListItem, 'status' | 'sunset_at' | 'reviewed' | 'owner_id'>>

/**
 * What each of `eventIds` holds now among `items`, for a bulk update's Undo.
 * Read from the list the table renders, not from whichever cached list holds
 * the row first: another tab's inactive list can be minutes stale, and an
 * Undo built from it restored values the operator never saw.
 */
export function bulkPreviousValues(items: EventListItem[], eventIds: string[]): PreviousValues {
  const idSet = new Set(eventIds)
  const previous: PreviousValues = new Map()
  for (const e of items) {
    if (!idSet.has(e.id)) continue
    previous.set(e.id, {
      status: e.status,
      sunset_at: e.sunset_at,
      reviewed: e.reviewed,
      owner_id: e.owner_id,
    })
  }
  return previous
}

/**
 * `eventIds` moved into the order given, within `items`: the rows among them
 * keep the positions they held, handed out in the new order. Rows not in
 * `eventIds` stay put.
 */
export function permuteEvents(items: EventListItem[], eventIds: string[]): EventListItem[] {
  const indexById = new Map(eventIds.map((id, i) => [id, i]))
  const moved = items
    .filter((event) => indexById.has(event.id))
    .sort((left, right) => indexById.get(left.id)! - indexById.get(right.id)!)
  let pointer = 0
  return items.map((event) => (indexById.has(event.id) ? (moved[pointer++] ?? event) : event))
}

/**
 * `permuteEvents` over an infinite list as ONE list. A drag across a page
 * boundary moves rows between pages; permuting each page on its own left them
 * in the wrong order. The result is re-split into the original page sizes.
 */
export function permuteInfinitePages(
  data: InfiniteData<EventListResponse>,
  eventIds: string[],
): InfiniteData<EventListResponse> {
  const flat = permuteEvents(data.pages.flatMap((page) => page.items), eventIds)
  let offset = 0
  return {
    ...data,
    pages: data.pages.map((page) => {
      const items = flat.slice(offset, offset + page.items.length)
      offset += page.items.length
      return { ...page, items }
    }),
  }
}

/**
 * The bulk updates that put `eventIds` back the way they were before `patch`,
 * grouped by previous value (the endpoint sets one value across its ids). Null
 * when any id's previous value is unknown — a "select all N matching" sweep
 * covers rows that were never loaded, and a partial undo would be worse than
 * none.
 */
export function buildBulkUndo(
  eventIds: string[],
  patch: BulkUpdatePatch,
  previous: PreviousValues,
): BulkUpdateVars[] | null {
  const keys = (Object.keys(patch) as (keyof BulkUpdatePatch)[])
    .filter(key => patch[key] !== undefined)
  if (keys.length === 0 || eventIds.length === 0) return null
  const groups = new Map<string, BulkUpdateVars>()
  for (const id of eventIds) {
    const prev = previous.get(id)
    if (!prev) return null
    const restore: BulkUpdatePatch = {}
    for (const key of keys) {
      Object.assign(restore, { [key]: prev[key] ?? (key === 'reviewed' ? false : null) })
    }
    const groupKey = JSON.stringify(restore)
    const group = groups.get(groupKey)
    if (group) group.eventIds.push(id)
    else groups.set(groupKey, { eventIds: [id], ...restore })
  }
  return [...groups.values()]
}

export function useEventMutations({
  slug,
  branchId,
  onBulkDeleteSuccess,
  onBulkUpdateSuccess,
}: {
  slug: string | undefined
  branchId: string | null
  /**
   * Run on SUCCESS, not in `onMutate`: clearing the selection optimistically
   * lost it on a 4xx/5xx, so a "select all 2,400" sweep had to be redone before
   * it could be retried.
   */
  onBulkDeleteSuccess?: () => void
  onBulkUpdateSuccess?: () => void
}) {
  const qc = useQueryClient()
  // Mutations and cache patches scope to `['events', slug, branchId]` so editing
  // the active branch never touches another branch's cache.
  const eventsKey = useMemo(() => branchEventsKey(slug, branchId), [slug, branchId])

  const applyToEventsCaches = useCallback(
    (transform: (items: EventListItem[]) => EventListItem[]): Snapshot[] => {
      const snapshots = qc.getQueriesData<EventsQueryData>({ queryKey: eventsKey })
      qc.setQueriesData<EventsQueryData>({ queryKey: eventsKey }, (data) => {
        if (!data) return data
        if ('pages' in data) {
          return {
            ...data,
            pages: data.pages.map((page) => ({ ...page, items: transform(page.items) })),
          }
        }
        return { ...data, items: transform(data.items) }
      })
      return snapshots
    },
    [qc, eventsKey],
  )

  const rollbackEventsCaches = useCallback((snapshots: Snapshot[] | undefined) => {
    if (!snapshots) return
    for (const [key, data] of snapshots) {
      qc.setQueryData(key, data)
    }
  }, [qc])

  // Reconcile with the server after a mutation, without re-requesting every
  // loaded page when nobody would see the difference.
  // A bulk status, archive, owner or delete change on main also moves health
  // scores, which live under their own key rather than the list's.
  const refreshEventsCaches = useCallback(() => {
    if (branchId === null) void qc.invalidateQueries({ queryKey: projectHealthRootKey(slug) })
    return refreshEventsLists(qc, eventsKey)
  }, [qc, eventsKey, branchId, slug])

  const applyBulkPatch = useCallback(
    (eventIds: string[], patch: BulkUpdatePatch) => {
      const idSet = new Set(eventIds)
      return applyToEventsCaches((items) =>
        items.map((e) => (idSet.has(e.id) ? { ...e, ...patch } : e)),
      )
    },
    [applyToEventsCaches],
  )

  const bulkDeleteMut = useMutation({
    mutationFn: (eventIds: string[]) => eventsApi.bulkDelete(slug!, eventIds, branchId),
    onMutate: async (eventIds) => {
      await qc.cancelQueries({ queryKey: eventsKey })
      const idSet = new Set(eventIds)
      const snapshots = applyToEventsCaches((items) => items.filter((e) => !idSet.has(e.id)))
      return { snapshots }
    },
    onSuccess: () => onBulkDeleteSuccess?.(),
    onError: (_e, _v, ctx) => rollbackEventsCaches(ctx?.snapshots),
    onSettled: () => refreshEventsCaches(),
  })

  const bulkUpdateMut = useMutation({
    mutationFn: ({ eventIds, ...patch }: BulkUpdateVars) =>
      eventsApi.bulkUpdate(slug!, eventIds, patch, branchId),
    onMutate: async ({ eventIds, ...patch }) => {
      await qc.cancelQueries({ queryKey: eventsKey })
      return { snapshots: applyBulkPatch(eventIds, patch) }
    },
    onSuccess: () => onBulkUpdateSuccess?.(),
    onError: (_e, _v, ctx) => rollbackEventsCaches(ctx?.snapshots),
    onSettled: () => refreshEventsCaches(),
  })

  // A bulk update's Undo: the groups `buildBulkUndo` made, sent in turn as ONE
  // mutation. Through `bulkUpdateMut` each group cleared whatever the operator
  // had selected since and refreshed the lists once per group.
  const bulkUndoMut = useMutation({
    mutationFn: async (groups: BulkUpdateVars[]) => {
      for (const { eventIds, ...patch } of groups) {
        await eventsApi.bulkUpdate(slug!, eventIds, patch, branchId)
      }
    },
    onMutate: async (groups) => {
      await qc.cancelQueries({ queryKey: eventsKey })
      let snapshots: Snapshot[] | undefined
      for (const { eventIds, ...patch } of groups) {
        const taken = applyBulkPatch(eventIds, patch)
        // The first group's snapshots hold the lists as they were before any.
        snapshots ??= taken
      }
      return { snapshots }
    },
    onError: (_e, _v, ctx) => rollbackEventsCaches(ctx?.snapshots),
    onSettled: () => refreshEventsCaches(),
  })

  const reorderEventsMut = useMutation({
    mutationFn: (eventIds: string[]) => eventsApi.reorder(slug!, eventIds, branchId),
    onMutate: async (eventIds) => {
      await qc.cancelQueries({ queryKey: eventsKey })
      const snapshots = qc.getQueriesData<EventsQueryData>({ queryKey: eventsKey })
      qc.setQueriesData<EventsQueryData>({ queryKey: eventsKey }, (data) => {
        if (!data) return data
        if ('pages' in data) return permuteInfinitePages(data, eventIds)
        return { ...data, items: permuteEvents(data.items, eventIds) }
      })
      return { snapshots }
    },
    // The server hands the sent rows' existing order slots back out in the
    // order sent. In a catalog-ordered list holding all of those rows — the
    // table the drag happened in — that is the same permutation as the one
    // applied above across the whole list, so success only marks the lists
    // stale and nothing is re-requested for a drag. Lists in another order, or
    // holding only some of the rows, catch up when they are next read.
    onSuccess: () => qc.invalidateQueries({ queryKey: eventsKey, refetchType: 'none' }),
    onError: (_error, _vars, ctx) => {
      rollbackEventsCaches(ctx?.snapshots)
      void refreshEventsCaches()
    },
  })

  return {
    bulkDeleteMut,
    bulkUpdateMut,
    bulkUndoMut,
    reorderEventsMut,
  }
}
