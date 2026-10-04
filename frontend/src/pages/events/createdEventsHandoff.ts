/**
 * The events a form has just created, handed to the Events list so it can
 * scroll to them and mark them.
 *
 * Session storage, not router state: the single-event form closes with
 * `navigate(-1)` when there is somewhere in-app to go back to, and a history
 * step carries no new state. The list takes the ids once and forgets them, so a
 * later visit does not flash rows created long ago. Scoped by project: a list
 * of another project has no use for them.
 */

const KEY_PREFIX = 'tripl:created-events:'

/** Older than this, the ids belong to a list the reader has moved on from. */
const HANDOFF_TTL_MS = 60_000

interface StoredHandoff {
  ids: string[]
  at: number
}

function keyFor(slug: string): string {
  return `${KEY_PREFIX}${slug}`
}

/**
 * Remember the ids a create has just made. Added to a handoff the list has not
 * taken yet, so a run of "Save and add another" marks every event of the run.
 */
export function rememberCreatedEvents(slug: string, ids: readonly string[], now = Date.now()): void {
  if (ids.length === 0) return
  const merged = [...new Set([...readCreatedEvents(slug, now), ...ids])]
  try {
    sessionStorage.setItem(keyFor(slug), JSON.stringify({ ids: merged, at: now } satisfies StoredHandoff))
  } catch {
    // Storage off or full: the toast still says what was created.
  }
}

/** The ids handed over and still fresh, without consuming them. */
export function readCreatedEvents(slug: string | undefined, now = Date.now()): string[] {
  if (!slug) return []
  try {
    const raw = sessionStorage.getItem(keyFor(slug))
    if (!raw) return []
    const parsed = JSON.parse(raw) as Partial<StoredHandoff> | null
    if (!parsed || !Array.isArray(parsed.ids) || typeof parsed.at !== 'number') return []
    if (now - parsed.at > HANDOFF_TTL_MS || parsed.at > now) return []
    return parsed.ids.filter((id): id is string => typeof id === 'string')
  } catch {
    return []
  }
}

/** Drop the handoff once the list has taken it. */
export function forgetCreatedEvents(slug: string | undefined): void {
  if (!slug) return
  try {
    sessionStorage.removeItem(keyFor(slug))
  } catch {
    // Nothing to clean up when storage is unavailable.
  }
}
