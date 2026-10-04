import { useCallback, useSyncExternalStore } from 'react'

/**
 * The unsent note on each incident card, held outside React state.
 *
 * The drafts used to be a `Record` in ProjectAlertingTab's state, so every
 * keystroke in any card re-rendered the whole page and all 50–250 cards under
 * it. They still have to be owned by the page — a draft outlives a section
 * switch, and the action mutation that sends it lives there — but nothing on
 * the page needs to re-render when one changes except the card it belongs to.
 * A tiny store with per-card subscriptions gives exactly that: the page holds
 * the store (one stable object), and each card subscribes to its own key.
 */
export interface NoteDraftStore {
  get: (correlationGroupId: string) => string
  set: (correlationGroupId: string, value: string) => void
  /**
   * Drop a draft once it has been saved — but only if it still says what was
   * sent. A reader who kept typing while the request was in flight has written
   * something the server has not seen, and wiping it would lose their words.
   */
  clearIfUnchanged: (correlationGroupId: string, sent: string) => void
  subscribe: (correlationGroupId: string, listener: () => void) => () => void
}

export function createNoteDraftStore(initial: Record<string, string> = {}): NoteDraftStore {
  const drafts = new Map<string, string>(Object.entries(initial))
  const listeners = new Map<string, Set<() => void>>()

  const notify = (correlationGroupId: string) => {
    for (const listener of listeners.get(correlationGroupId) ?? []) listener()
  }

  return {
    get: correlationGroupId => drafts.get(correlationGroupId) ?? '',
    set: (correlationGroupId, value) => {
      if ((drafts.get(correlationGroupId) ?? '') === value) return
      if (value) drafts.set(correlationGroupId, value)
      else drafts.delete(correlationGroupId)
      notify(correlationGroupId)
    },
    clearIfUnchanged: (correlationGroupId, sent) => {
      const current = drafts.get(correlationGroupId)
      if (current === undefined || current.trim() !== sent.trim()) return
      drafts.delete(correlationGroupId)
      notify(correlationGroupId)
    },
    subscribe: (correlationGroupId, listener) => {
      let bucket = listeners.get(correlationGroupId)
      if (!bucket) {
        bucket = new Set()
        listeners.set(correlationGroupId, bucket)
      }
      bucket.add(listener)
      return () => {
        bucket.delete(listener)
        if (bucket.size === 0) listeners.delete(correlationGroupId)
      }
    },
  }
}

/** One card's draft, re-rendering that card — and only that card — when it changes. */
export function useNoteDraft(store: NoteDraftStore, correlationGroupId: string): string {
  const subscribe = useCallback(
    (listener: () => void) => store.subscribe(correlationGroupId, listener),
    [store, correlationGroupId],
  )
  const getSnapshot = () => store.get(correlationGroupId)
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}
