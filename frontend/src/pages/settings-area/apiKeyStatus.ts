import { formatIsoDate } from '@/lib/datetime'
import type { ApiKey } from '@/types'

/**
 * Credential-status helpers for the API keys card. Kept out of the section
 * component so the .tsx stays component-only (react-refresh) and the counting
 * rule is unit-testable on its own.
 */

/**
 * A key that can no longer authenticate — revoked, or past its expiry.
 *
 * The expiry comparison is inclusive to match the backend, which rejects a
 * token once `expires_at <= now` (services/api_key_service.py:125). A strict
 * `<` here would report a key as active for the instant the backend already
 * refuses it.
 */
export function isKeyInactive(key: Pick<ApiKey, 'revoked_at' | 'expires_at'>, now = new Date()): boolean {
  if (key.revoked_at != null) return true
  return key.expires_at != null && new Date(key.expires_at) <= now
}

/**
 * Card heading for the key list, e.g. "7 active · 3 revoked or expired".
 * The card used to read "Active keys · 10 keys" off the unfiltered list, so
 * dead tokens were counted as live ones on a credentials surface.
 */
export function describeKeyCounts(active: number, inactive: number): string {
  const activeLabel = `${active} active`
  return inactive > 0 ? `${activeLabel} · ${inactive} revoked or expired` : activeLabel
}

/**
 * The line under the reveal-once dialog's title that says which key the token
 * belongs to, e.g. "claude-agent · read-only · All projects · no expiry". The
 * row it came from is hidden behind the overlay (ST-21).
 */
export function describeRevealedKey(
  key: Pick<ApiKey, 'name' | 'scope' | 'project_id' | 'expires_at'>,
  projectName?: string,
): string {
  const scope = key.scope === 'write' ? 'read & write' : 'read-only'
  const project = key.project_id ? (projectName ?? 'one project') : 'All projects'
  const expiry = key.expires_at ? `expires ${formatIsoDate(key.expires_at)}` : 'no expiry'
  return `${key.name} · ${scope} · ${project} · ${expiry}`
}
