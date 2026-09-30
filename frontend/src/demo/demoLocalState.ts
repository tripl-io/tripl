/**
 * Every piece of browser storage the demo keeps per project, named in one place.
 *
 * Demo slugs are random (`demo-xxxxxx`) and a reset or delete retires them, so
 * keys written against a slug outlive the project unless something removes them
 * (DEMO-17). Keeping the prefixes here lets the delete path clear exactly what
 * the tour, the scenario, the welcome panel and the hint toggle wrote.
 */

import { orgStorageKey } from '@/lib/activeOrg'

/** localStorage: the product tour's step position. */
export const TOUR_STORAGE_PREFIX = 'tripl-tour:'
/** localStorage: the coached scenario's chapter progress. */
export const SCENARIO_STORAGE_PREFIX = 'tripl-demo-scenario:'
/** localStorage: the welcome panel was put away. */
export const WELCOME_DISMISS_PREFIX = 'tripl-demo-welcome-dismissed:'
/** sessionStorage: "Hide hints" — scoped to the browser session, per project. */
export const HINTS_MUTED_PREFIX = 'tripl-demo-hints-muted:'
/** sessionStorage: the tour step shown in the docked card (tourDock.ts). */
export const TOUR_DOCK_PREFIX = 'tripl-tour-dock:'

/** Drop everything the demo remembered about `slug`. Best effort, never throws. */
export function forgetDemoLocalState(slug: string): void {
  try {
    window.localStorage.removeItem(orgStorageKey(`${TOUR_STORAGE_PREFIX}${slug}`))
    window.localStorage.removeItem(orgStorageKey(`${SCENARIO_STORAGE_PREFIX}${slug}`))
    window.localStorage.removeItem(orgStorageKey(`${WELCOME_DISMISS_PREFIX}${slug}`))
  } catch {
    /* storage unavailable: there is nothing to clean up either */
  }
  try {
    window.sessionStorage.removeItem(`${HINTS_MUTED_PREFIX}${slug}`)
    window.sessionStorage.removeItem(`${TOUR_DOCK_PREFIX}${slug}`)
  } catch {
    /* as above */
  }
}

/**
 * Drop what the demo remembered about every project that is no longer in
 * `liveSlugs` (DEMO-17). The delete paths forget their own project, but a demo
 * deleted from another browser, reset (a re-seed keeps the slug, so that one is
 * fine) or removed by an owner elsewhere left its keys behind for good.
 * Best effort, never throws.
 */
export function sweepOrphanedDemoLocalState(liveSlugs: Iterable<string>): void {
  const live = new Set(liveSlugs)
  const sweep = (storage: Storage, prefixes: readonly string[]) => {
    const orphaned: string[] = []
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index)
      if (!key) continue
      const prefix = prefixes.find((candidate) => key.startsWith(candidate))
      if (prefix && !live.has(key.slice(prefix.length))) orphaned.push(key)
    }
    for (const key of orphaned) storage.removeItem(key)
  }
  try {
    // The active organization's keys only: `liveSlugs` are its projects.
    sweep(
      window.localStorage,
      [TOUR_STORAGE_PREFIX, SCENARIO_STORAGE_PREFIX, WELCOME_DISMISS_PREFIX].map((prefix) => orgStorageKey(prefix)),
    )
  } catch {
    /* storage unavailable: nothing was written to it either */
  }
  try {
    sweep(window.sessionStorage, [HINTS_MUTED_PREFIX, TOUR_DOCK_PREFIX])
  } catch {
    /* as above */
  }
}

/** Whether "Hide hints" is on for this project in this browser session (DEMO-15). */
export function readHintsMuted(slug: string | undefined): boolean {
  if (!slug) return false
  try {
    return window.sessionStorage.getItem(`${HINTS_MUTED_PREFIX}${slug}`) === '1'
  } catch {
    return false
  }
}

export function writeHintsMuted(slug: string | undefined, muted: boolean): void {
  if (!slug) return
  try {
    if (muted) window.sessionStorage.setItem(`${HINTS_MUTED_PREFIX}${slug}`, '1')
    else window.sessionStorage.removeItem(`${HINTS_MUTED_PREFIX}${slug}`)
  } catch {
    /* private mode: the toggle just lasts until the next reload */
  }
}
