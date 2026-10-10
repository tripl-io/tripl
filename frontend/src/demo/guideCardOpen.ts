/**
 * Whether a demo guide card is open on the page, for the scenario strip. The
 * card says the step's instruction in full, and the strip's cut-short copy of
 * the same sentence sat right above it. While a card is open the strip keeps
 * its copy for screen readers only.
 *
 * A count, not a flag: nothing stops two guides being mounted at once, and
 * one folding must not uncover the strip's copy while the other is open.
 */

import { useSyncExternalStore } from 'react'

let openCards = 0
const listeners = new Set<() => void>()

function emit(): void {
  for (const listener of listeners) listener()
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

/** Counts one open card until the returned release is called. */
export function holdGuideCardOpen(): () => void {
  openCards += 1
  emit()
  let released = false
  return () => {
    if (released) return
    released = true
    openCards -= 1
    emit()
  }
}

export function useGuideCardOpen(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => openCards > 0,
    () => false,
  )
}
