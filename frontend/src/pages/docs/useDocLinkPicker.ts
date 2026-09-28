import { useCallback, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { LinkTrigger } from '@/lib/docLinkTrigger'
import type { DocLinkSuggestion } from '@/types/docs'
import { useLinkSuggestions } from './useDocs'

/**
 * State of the note editor's `[[` / `@` link picker (F24, GH #308). The editor
 * reports the trigger under the cursor on every change ({@link update}) and
 * forwards the navigation keys ({@link handleKey}), which answers whether it
 * used the key. A pick hands the trigger and the row to `onPick`, which writes
 * the canonical reference into the editor.
 *
 * Escape closes the picker for the `[[` or `@` it was open for; it opens again
 * at the next one the cursor reaches.
 */
export interface DocLinkPicker {
  trigger: LinkTrigger | null
  items: readonly DocLinkSuggestion[]
  loading: boolean
  /** The highlighted row, -1 when there is none. */
  active: number
  listId: string
  optionId: (index: number) => string
  update: (trigger: LinkTrigger | null) => void
  handleKey: (key: string) => boolean
  select: (index: number) => void
  setActive: (index: number) => void
  close: () => void
}

function sameTrigger(a: LinkTrigger | null, b: LinkTrigger | null): boolean {
  if (a === null || b === null) return a === b
  return a.mode === b.mode && a.from === b.from && a.to === b.to && a.kind === b.kind && a.query === b.query
}

export function useDocLinkPicker(
  slug: string,
  onPick: (trigger: LinkTrigger, item: DocLinkSuggestion) => void,
): DocLinkPicker {
  const [trigger, setTrigger] = useState<LinkTrigger | null>(null)
  const [activeRaw, setActiveRaw] = useState(0)
  const dismissedFrom = useRef<number | null>(null)
  const listId = useId()

  const suggestions = useLinkSuggestions(slug, trigger?.query ?? '', trigger?.kind ?? null, trigger !== null)
  // Only rows that answer the trigger as typed now: never a previous query's
  // rows (another kind) left on screen while the next ones load, so a fast
  // `[[metric:rev` + Enter cannot insert a note or a person.
  const { current, data } = suggestions
  const items = useMemo(() => {
    if (!trigger || !current) return []
    const rows = data?.items ?? []
    return trigger.kind ? rows.filter(row => row.kind === trigger.kind) : rows
  }, [trigger, current, data])
  const active = items.length === 0 ? -1 : Math.min(activeRaw, items.length - 1)

  // The editor's key handler runs outside React: it reads the latest state
  // through this ref, synced after every commit.
  const live = useRef({ trigger, items, active, onPick })
  useLayoutEffect(() => {
    live.current = { trigger, items, active, onPick }
  })

  const update = useCallback((next: LinkTrigger | null) => {
    if (next === null) dismissedFrom.current = null
    const shown = next !== null && next.from === dismissedFrom.current ? null : next
    const prev = live.current.trigger
    if (sameTrigger(prev, shown)) return
    if (!prev || !shown || prev.query !== shown.query || prev.kind !== shown.kind) setActiveRaw(0)
    live.current = { ...live.current, trigger: shown }
    setTrigger(shown)
  }, [])

  const close = useCallback(() => {
    const open = live.current.trigger
    if (open) dismissedFrom.current = open.from
    setTrigger(null)
  }, [])

  const select = useCallback((index: number) => {
    const { trigger: open, items: rows, onPick: pick } = live.current
    const item = rows[index]
    if (!open || !item) return
    dismissedFrom.current = null
    setTrigger(null)
    pick(open, item)
  }, [])

  const handleKey = useCallback(
    (key: string): boolean => {
      const { trigger: open, items: rows, active: current } = live.current
      if (!open) return false
      switch (key) {
        case 'ArrowDown':
          if (rows.length === 0) return false
          setActiveRaw((current + 1) % rows.length)
          return true
        case 'ArrowUp':
          if (rows.length === 0) return false
          setActiveRaw((current - 1 + rows.length) % rows.length)
          return true
        case 'Enter':
        case 'Tab':
          if (rows.length === 0 || current < 0) return false
          select(current)
          return true
        case 'Escape':
          close()
          return true
        default:
          return false
      }
    },
    [close, select],
  )

  const optionId = useCallback((index: number) => `${listId}-option-${index}`, [listId])

  return {
    trigger,
    items,
    loading: trigger !== null && (suggestions.isFetching || !current),
    active,
    listId,
    optionId,
    update,
    handleKey,
    select,
    setActive: setActiveRaw,
    close,
  }
}
