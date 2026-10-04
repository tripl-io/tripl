/**
 * The getting-started checklist's "dismissed" flag, per project.
 *
 * Keyed on the project id when the caller has it: a slug can be renamed, and
 * the old slug key then no longer matched, so a dismissed checklist came back.
 * The slug key is still read, so a dismissal made before this change
 * holds, and is what callers without an id use.
 */

import { orgStorageKey } from '@/lib/activeOrg'
const STORAGE_PREFIX = 'tripl-onboarding-dismissed:'

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
      return
    }
    for (const key of keys) localStorage.removeItem(key)
  } catch {
    // Private-mode / storage-disabled: the choice just won't persist.
  }
}
