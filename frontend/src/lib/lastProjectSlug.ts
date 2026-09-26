/**
 * The last project the user opened, remembered across reloads. The sidebar
 * writes it on every project route; Settings falls back to it for a bare
 * address.
 */
export const LAST_PROJECT_SLUG_KEY = 'tripl-last-project-slug'

/**
 * Forget the remembered project when it is `slug`. A project the server
 * answers 404 for — deleted, or one the user was removed from (non-members do
 * not see a project at all, tripl-vefw) — must not keep being offered as
 * "where you left off". A different remembered slug is left alone.
 */
export function forgetLastProjectSlug(slug: string): void {
  try {
    if (localStorage.getItem(LAST_PROJECT_SLUG_KEY) === slug) {
      localStorage.removeItem(LAST_PROJECT_SLUG_KEY)
    }
  } catch {
    /* storage unavailable: nothing is remembered, so nothing to forget */
  }
}
