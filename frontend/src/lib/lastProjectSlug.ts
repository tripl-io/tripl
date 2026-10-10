/**
 * The last project the user opened, remembered across reloads. The sidebar
 * writes it on every project route; Settings falls back to it for a bare
 * address.
 */

import { LAST_PROJECT_SLUG_KEY, orgStorageKey } from '@/lib/activeOrg'

/**
 * Forget the remembered project when it is `slug`. A project the server
 * answers 404 for — deleted, or one the user was removed from (non-members do
 * not see a project at all) — must not keep being offered as
 * "where you left off". A different remembered slug is left alone.
 */
export function forgetLastProjectSlug(slug: string): void {
  try {
    const key = orgStorageKey(LAST_PROJECT_SLUG_KEY)
    if (localStorage.getItem(key) === slug) {
      localStorage.removeItem(key)
    }
  } catch {
    /* storage unavailable: nothing is remembered, so nothing to forget */
  }
}
