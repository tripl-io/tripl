import { useEffect, useRef, useState, type RefObject } from 'react'
import type { EventListItem } from '@/types'
import { forgetCreatedEvents, readCreatedEvents } from './createdEventsHandoff'

/** How long a found row stays marked. */
export const CREATED_HIGHLIGHT_MS = 6_000
/**
 * How long the list waits for a created row to turn up. The list renders its
 * cached page first and refetches after the create invalidated it, so the row
 * can arrive a moment late — but a row that is pages away is left to the
 * toast's Open action rather than yanking the scroll position later on.
 */
export const CREATED_FIND_WINDOW_MS = 5_000

const NONE: ReadonlySet<string> = new Set()

/**
 * The rows a form has just created: read once from the
 * handoff the form left, scrolled to once, and marked for a few seconds.
 *
 * "Create event" used to step back to a 170-row list with nothing to find the
 * new row by. A virtualized list may not have rendered the row at all, so the
 * scroll goes through the virtualizer's `scrollToIndex`; a short list has every
 * row in the DOM and scrolls the first marked one into view.
 */
export function useCreatedEventsHighlight({
  slug,
  events,
  virtualize,
  scrollToIndex,
  scrollRef,
}: {
  slug: string | undefined
  events: readonly (EventListItem | undefined)[]
  virtualize: boolean
  scrollToIndex: (index: number, options?: { align?: 'start' | 'center' | 'end' | 'auto' }) => void
  scrollRef: RefObject<HTMLElement | null>
}): ReadonlySet<string> {
  // Read in the initializer, cleared in an effect: a Strict Mode double call of
  // the initializer must not find the handoff already consumed.
  const [createdIds, setCreatedIds] = useState<ReadonlySet<string>>(() => {
    const ids = readCreatedEvents(slug)
    return ids.length > 0 ? new Set(ids) : NONE
  })
  useEffect(() => {
    forgetCreatedEvents(slug)
  }, [slug])

  const targetIndex =
    createdIds.size === 0 ? -1 : events.findIndex(ev => !!ev && createdIds.has(ev.id))
  const found = targetIndex >= 0

  const scrolled = useRef(false)
  useEffect(() => {
    if (scrolled.current || targetIndex < 0) return
    scrolled.current = true
    if (virtualize) {
      scrollToIndex(targetIndex, { align: 'center' })
      return
    }
    scrollRef.current?.querySelector('[data-created]')?.scrollIntoView({ block: 'center' })
  }, [targetIndex, virtualize, scrollToIndex, scrollRef])

  // The mark fades once seen; a row that never turned up stops being looked for.
  useEffect(() => {
    if (createdIds.size === 0) return
    const timer = window.setTimeout(
      () => setCreatedIds(NONE),
      found ? CREATED_HIGHLIGHT_MS : CREATED_FIND_WINDOW_MS,
    )
    return () => window.clearTimeout(timer)
  }, [createdIds, found])

  return createdIds
}
