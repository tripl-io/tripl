/**
 * The getting-started checklist's "dismissed" flag, per project.
 *
 * Keyed on the project id when the caller has it: a slug can be renamed, and
 * the old slug key then no longer matched, so a dismissed checklist came back.
 * The slug key is still read, so a dismissal made before this change
 * holds, and is what callers without an id use.
 *
 * Every write tells the components reading it through
 * `useOnboardingDismissed`, so the checklist's own dismiss, its toast's Undo
 * and the palette's "Show getting started" all redraw whatever shows it.
 */

import { useSyncExternalStore } from 'react'
import { orgStorageKey } from '@/lib/activeOrg'

const STORAGE_PREFIX = 'tripl-onboarding-dismissed:'

const listeners = new Set<() => void>()

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  // Another tab dismissed or brought back a checklist.
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key.includes(STORAGE_PREFIX)) listener()
  }
  window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(listener)
    window.removeEventListener('storage', onStorage)
  }
}

function keysFor(slug: string, projectId?: string): string[] {
  // Inside the active organization: a slug names a project only there.
  return projectId
    ? [orgStorageKey(`${STORAGE_PREFIX}${projectId}`), orgStorageKey(`${STORAGE_PREFIX}${slug}`)]
    : [orgStorageKey(`${STORAGE_PREFIX}${slug}`)]
}

export function isOnboardingDismissed(slug: string, projectId?: string): boolean {
  try {
    return keysFor(slug, projectId).some((key) => localStorage.getItem(key) === '1')
  } catch {
    return false
  }
}

/** Dismiss, or bring back, the checklist for one project. */
export function setOnboardingDismissed(
  slug: string,
  projectId: string | undefined,
  dismissed: boolean,
): void {
  const keys = keysFor(slug, projectId)
  try {
    if (dismissed) {
      localStorage.setItem(keys[0]!, '1')
    } else {
      for (const key of keys) localStorage.removeItem(key)
    }
  } catch {
    // Private-mode / storage-disabled: the choice just won't persist.
  }
  listeners.forEach((listener) => listener())
}

/**
 * Whether the checklist of a project is dismissed, kept current across every
 * `setOnboardingDismissed` (this tab) and storage write (another tab). False
 * while there is no slug yet.
 */
export function useOnboardingDismissed(slug: string | undefined, projectId?: string): boolean {
  return useSyncExternalStore(subscribe, () => (slug ? isOnboardingDismissed(slug, projectId) : false))
}
