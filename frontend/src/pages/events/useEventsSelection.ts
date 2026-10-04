import { useCallback, useMemo, useState } from 'react'

import type { EventListItem } from '@/types'

export function useEventsSelection({
  events,
  scopeKey,
}: {
  events: EventListItem[]
  /**
   * Identifies the result set the selection was made in (tab, branch, server
   * filters). When it changes the selection is dropped: 20 rows ticked on
   * Review and then "Set status" on Archived changed 20 events the operator
   * could no longer see. Sort order is not part of it — it reorders
   * the same set. Client-side column filters are not part of it either; the
   * bar and the confirmations name off-screen rows instead.
   */
  scopeKey?: string
}) {
  const [selectedEventIds, setSelectedEventIds] = useState<string[]>([])
  const [selectionScope, setSelectionScope] = useState(scopeKey)
  // Reset during render (React-recommended) rather than in an effect, so no
  // frame ever pairs the new scope with the old selection.
  if (selectionScope !== scopeKey) {
    setSelectionScope(scopeKey)
    setSelectedEventIds([])
  }

  const visibleEventIds = useMemo(
    () => events.map(event => event.id),
    [events],
  )
  const visibleIndexById = useMemo(() => {
    const map = new Map<string, number>()
    visibleEventIds.forEach((id, i) => map.set(id, i))
    return map
  }, [visibleEventIds])
  const visibleEventIdsSet = useMemo(() => new Set(visibleEventIds), [visibleEventIds])
  const selectedVisibleEventIds = useMemo(
    () => selectedEventIds.filter(eventId => visibleEventIdsSet.has(eventId)),
    [selectedEventIds, visibleEventIdsSet],
  )
  const allVisibleSelected = events.length > 0 && selectedVisibleEventIds.length === events.length
  const someVisibleSelected = selectedVisibleEventIds.length > 0
  const selectedSet = useMemo(() => new Set(selectedEventIds), [selectedEventIds])

  const toggleEventSelected = useCallback((eventId: string, checked: boolean) => {
    setSelectedEventIds(current => (
      checked
        ? (current.includes(eventId) ? current : [...current, eventId])
        : current.filter(id => id !== eventId)
    ))
  }, [])

  const toggleAllVisibleSelected = useCallback((checked: boolean) => {
    setSelectedEventIds(current => {
      if (!checked) {
        return current.filter(id => !visibleEventIdsSet.has(id))
      }
      const next = new Set(current)
      visibleEventIds.forEach(id => next.add(id))
      return Array.from(next)
    })
  }, [visibleEventIds, visibleEventIdsSet])

  // Select an explicit id set that may include events not currently loaded
  // (used by "select all N matching" to sweep the whole filtered result).
  const selectAll = useCallback((ids: string[]) => {
    setSelectedEventIds(ids)
  }, [])

  // Add many ids in one update. Ticking them one by one through
  // `toggleEventSelected` was quadratic (an `includes` per id) for a large
  // name cluster.
  const selectMany = useCallback((ids: string[]) => {
    setSelectedEventIds(current => {
      const next = new Set(current)
      for (const id of ids) next.add(id)
      return next.size === current.length ? current : Array.from(next)
    })
  }, [])

  const clearSelection = useCallback(() => {
    setSelectedEventIds([])
  }, [])

  return {
    selectedEventIds,
    selectedCount: selectedEventIds.length,
    selectedVisibleEventIds,
    allVisibleSelected,
    someVisibleSelected,
    selectedSet,
    visibleEventIds,
    visibleIndexById,
    toggleEventSelected,
    toggleAllVisibleSelected,
    selectAll,
    selectMany,
    clearSelection,
  }
}
