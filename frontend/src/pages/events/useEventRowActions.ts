import { useCallback, useEffect, useRef } from 'react'
import type { DragEndEvent } from '@dnd-kit/core'

import type { EventListItem } from '@/types'

import type { RowAction } from './EventRow'
import type { EventMutations } from './useEventMutations'
import { changedSlice, reorderWithSelection } from './utils'

/**
 * Bundles the row-level dispatch + drag-end handler for the events table.
 * Uses a ref so the resulting callbacks have stable identity across parent
 * re-renders, which keeps `React.memo` on EventRow effective.
 */
export function useEventRowActions({
  openEvent,
  mutations,
  visibleEventIds,
  selectedSet,
  canReorder,
}: {
  openEvent: (ev: EventListItem) => void
  mutations: EventMutations
  visibleEventIds: string[]
  selectedSet: Set<string>
  /** False while the rows are not in catalog order (see EventsPage). */
  canReorder: boolean
}) {
  const rowCtxRef = useRef({
    openEvent,
    mutations,
    visibleEventIds,
    selectedSet,
    canReorder,
  })
  useEffect(() => {
    rowCtxRef.current = {
      openEvent,
      mutations,
      visibleEventIds,
      selectedSet,
      canReorder,
    }
  })

  const handleDragEnd = useCallback(
    (event: DragEndEvent) => {
      const { active, over } = event
      if (!over) return
      const ctx = rowCtxRef.current
      if (!ctx.canReorder) return
      // Multi-select drag moves the whole selection as a block; a single row
      // moves on its own. `reorderWithSelection` returns null when there is
      // nothing to apply.
      const next = reorderWithSelection(
        ctx.visibleEventIds,
        ctx.selectedSet,
        String(active.id),
        String(over.id),
      )
      if (!next) return
      // Only the rows whose position changed. The server hands the existing
      // order slots of the ids it is sent back out in the order sent, so the
      // span between the first and last moved row is a complete answer — and
      // one drag no longer posts every loaded id.
      const slice = changedSlice(ctx.visibleEventIds, next)
      if (slice.length > 1) ctx.mutations.reorderEventsMut.mutate(slice)
    },
    [],
  )

  const onRowAction = useCallback((action: RowAction, ev: EventListItem) => {
    if (action === 'edit') rowCtxRef.current.openEvent(ev)
  }, [])

  return { handleDragEnd, onRowAction }
}
