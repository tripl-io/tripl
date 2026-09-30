import type { ScimToken } from '@/api/scim'

/**
 * Organization › Provisioning (SCIM) (F20): the pure half of the page.
 */

export const ORG_SCIM_PATH = 'organization/scim'

/**
 * The SCIM base URL to give the identity provider. The server's answer (built
 * from `APP_BASE_URL`) wins; without one, the page's own origin stands in.
 */
export function scimBaseUrl(origin: string, org: string, serverBaseUrl?: string | null): string {
  if (serverBaseUrl) return serverBaseUrl
  return `${origin.replace(/\/+$/, '')}/scim/v2/${encodeURIComponent(org)}`
}

/** Active tokens first (newest first), then revoked ones (most recently revoked first). */
export function sortScimTokens(tokens: readonly ScimToken[]): ScimToken[] {
  const active = tokens.filter((t) => !t.revoked_at)
  const revoked = tokens.filter((t) => t.revoked_at)
  const byCreated = (a: ScimToken, b: ScimToken) => b.created_at.localeCompare(a.created_at)
  const byRevoked = (a: ScimToken, b: ScimToken) => (b.revoked_at ?? '').localeCompare(a.revoked_at ?? '')
  return [...[...active].sort(byCreated), ...[...revoked].sort(byRevoked)]
}

/** A token's display form: its prefix, visibly cut. */
export function scimTokenLabel(token: Pick<ScimToken, 'prefix'>): string {
  return `${token.prefix}…`
}
