import { useSyncExternalStore } from 'react'

/**
 * Whether charts draw release and API annotations (#256). One preference for
 * every chart, kept in localStorage like the other view preferences, so
 * hiding them on one chart hides them on the next page too. Manual
 * annotations are never affected.
 */
export const SHOW_RELEASES_STORAGE_KEY = 'tripl.chartShowReleases'

const listeners = new Set<() => void>()
// Where the choice lives when storage is unavailable (private mode, quota).
let memoryValue = true
// Set when the last write failed while reads may still work (a full quota):
// storage then holds a stale value, so the in-memory choice has to win or the
// checkbox snaps back to what was stored before.
let writeFailed = false

function readShowReleases(): boolean {
  if (writeFailed) return memoryValue
  try {
    // Anything but an explicit "false" is the default: on.
    return localStorage.getItem(SHOW_RELEASES_STORAGE_KEY) !== 'false'
  } catch {
    return memoryValue
  }
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  // Another tab changed it.
  const onStorage = (event: StorageEvent) => {
    if (event.key === SHOW_RELEASES_STORAGE_KEY || event.key === null) {
      // The other tab's write landed, so storage is current again.
      writeFailed = false
      listener()
    }
  }
  window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(listener)
    window.removeEventListener('storage', onStorage)
  }
}

/** Store the choice and redraw every chart that reads it. */
export function setShowReleases(show: boolean): void {
  memoryValue = show
  try {
    localStorage.setItem(SHOW_RELEASES_STORAGE_KEY, show ? 'true' : 'false')
    writeFailed = false
  } catch {
    // Storage refused the write; reads serve the in-memory value from now on.
    writeFailed = true
  }
  listeners.forEach(listener => listener())
}

/** The "Show releases" preference (default on) and its setter. */
export function useShowReleases(): [boolean, (show: boolean) => void] {
  const show = useSyncExternalStore(subscribe, readShowReleases, () => true)
  return [show, setShowReleases]
}
