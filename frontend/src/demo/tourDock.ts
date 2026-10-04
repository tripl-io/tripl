/**
 * Which tour step is docked on the page, per project (#251).
 *
 * "Open X" in the tour navigates to X, and the tour then stays on screen as a
 * small docked card ("Step 4 of 11 · Next") instead of disappearing. The two
 * ends live in different subtrees — the tour is a dialog opened from the demo
 * banner or the Overview's welcome panel, the dock is mounted by the banner on
 * every demo surface — so, like the welcome dismissal, this is a subscribable
 * store rather than component state. sessionStorage: a docked tour lasts the
 * browser session, and a reload on the step's page keeps it.
 */

import { useSyncExternalStore } from 'react'
import { TOUR_DOCK_PREFIX } from './demoLocalState'

const listeners = new Set<() => void>()

function dockKey(slug: string): string {
  return `${TOUR_DOCK_PREFIX}${slug}`
}

/** The docked step's index, or null when the tour is not docked. */
export function readTourDock(slug: string): number | null {
  try {
    const raw = window.sessionStorage.getItem(dockKey(slug))
    if (raw === null) return null
    const parsed = Number.parseInt(raw, 10)
    return Number.isInteger(parsed) && parsed >= 0 ? parsed : null
  } catch {
    return null
  }
}

/** Dock the tour on `step`, or take it off the page with null. */
export function setTourDock(slug: string, step: number | null): void {
  try {
    if (step === null) window.sessionStorage.removeItem(dockKey(slug))
    else window.sessionStorage.setItem(dockKey(slug), String(step))
  } catch {
    /* private mode: the dock just won't survive a reload */
  }
  for (const listener of listeners) listener()
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function useTourDock(slug: string): number | null {
  return useSyncExternalStore(
    subscribe,
    () => readTourDock(slug),
    () => null,
  )
}
