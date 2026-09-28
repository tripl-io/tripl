import { domainVerified, type SsoConfig, type SsoConfigUpdate, type SsoDomain } from '@/api/sso'

/**
 * Organization › Single sign-on (F20): the pure half of the page. The draft of
 * the provider fields, what a save sends, and when SSO may be turned on.
 */

export const ORG_SSO_PATH = 'organization/sso'

export const DEFAULT_SSO_SCOPES = 'openid email profile'

/** The provider fields the owner edits and saves together. */
export type SsoTextField = 'issuer' | 'client_id' | 'client_secret' | 'scopes'

/** Edits not yet saved. A field left out is the saved value. */
export type SsoDraft = Partial<Record<SsoTextField, string>>

/** The value a field shows: the draft's, else the saved one. The secret never has a saved one. */
export function ssoDisplayValue(config: SsoConfig, draft: SsoDraft, field: SsoTextField): string {
  if (field in draft) return draft[field] ?? ''
  if (field === 'client_secret') return ''
  if (field === 'scopes') return config.scopes || DEFAULT_SSO_SCOPES
  return config[field] ?? ''
}

/** The provider fields the draft changes from the saved ones (the secret when one was typed). */
export function changedSsoFields(config: SsoConfig, draft: SsoDraft): SsoTextField[] {
  const changed: SsoTextField[] = []
  if (draft.issuer !== undefined && draft.issuer.trim() !== (config.issuer ?? '')) changed.push('issuer')
  if (draft.client_id !== undefined && draft.client_id.trim() !== (config.client_id ?? '')) {
    changed.push('client_id')
  }
  if (draft.scopes !== undefined && normalizeScopes(draft.scopes) !== normalizeScopes(config.scopes || DEFAULT_SSO_SCOPES)) {
    changed.push('scopes')
  }
  if (draft.client_secret) changed.push('client_secret')
  return changed
}

function normalizeScopes(value: string): string {
  return value.trim().split(/\s+/).join(' ')
}

/**
 * What a save sends: the whole configuration, the draft over the saved values,
 * and `switches` over the saved switches. A secret goes only when one was
 * typed; left out, the stored one is kept (it cannot be read back).
 */
export function buildSsoUpdate(
  config: SsoConfig,
  draft: SsoDraft,
  switches: Partial<Pick<SsoConfigUpdate, 'enabled' | 'sso_required'>> = {},
): SsoConfigUpdate {
  const update: SsoConfigUpdate = {
    issuer: (draft.issuer ?? config.issuer ?? '').trim(),
    client_id: (draft.client_id ?? config.client_id ?? '').trim(),
    scopes: normalizeScopes(draft.scopes ?? (config.scopes || DEFAULT_SSO_SCOPES)),
    enabled: switches.enabled ?? config.enabled,
    sso_required: switches.sso_required ?? config.sso_required,
  }
  if (draft.client_secret) update.client_secret = draft.client_secret
  return update
}

/**
 * A provider that was never saved has nothing to keep: the issuer and client
 * ID must be typed before the first save.
 */
export function firstSaveMissing(config: SsoConfig, draft: SsoDraft): boolean {
  const issuer = (draft.issuer ?? config.issuer ?? '').trim()
  const clientId = (draft.client_id ?? config.client_id ?? '').trim()
  return !issuer || !clientId
}

/** The issuer must be an https URL; the server checks it again (and its host). */
export function issuerError(value: string): string | null {
  const trimmed = value.trim()
  if (!trimmed) return 'Enter the issuer URL your identity provider publishes.'
  let url: URL
  try {
    url = new URL(trimmed)
  } catch {
    return 'Enter a full URL, e.g. https://idp.example.com.'
  }
  if (url.protocol !== 'https:') return 'The issuer must use https.'
  return null
}

export function scopesError(value: string): string | null {
  const scopes = value.trim().split(/\s+/)
  return scopes.includes('openid') ? null : 'Scopes must include openid.'
}

/** Per-field problems of the draft, for the fields it touches. */
export function ssoFieldError(field: SsoTextField, draft: SsoDraft): string | null {
  const value = draft[field]
  if (value === undefined) return null
  if (field === 'issuer') return issuerError(value)
  if (field === 'client_id') return value.trim() ? null : 'Enter the client ID.'
  if (field === 'scopes') return scopesError(value)
  return null
}

export function ssoDraftInvalid(draft: SsoDraft): boolean {
  return (['issuer', 'client_id', 'scopes'] as const).some((field) => ssoFieldError(field, draft) !== null)
}

/** Is the provider saved in full: issuer, client ID and a stored secret? */
export function providerConfigured(config: SsoConfig): boolean {
  return Boolean(config.issuer && config.client_id && config.client_secret_configured)
}

export function verifiedDomains(domains: readonly SsoDomain[]): SsoDomain[] {
  return domains.filter(domainVerified)
}

/**
 * Why SSO cannot be turned on yet, or `null` when it can. The server refuses
 * the same cases; this says so before the click.
 */
export function enableBlockedReason(config: SsoConfig, domains: readonly SsoDomain[]): string | null {
  if (!providerConfigured(config)) return 'Save the issuer, client ID and client secret first.'
  if (verifiedDomains(domains).length === 0) return 'Verify at least one email domain first.'
  return null
}

// Lowercase labels of letters, digits and hyphens, at least one dot.
const DOMAIN_SHAPE = /^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$/

export function normalizeDomain(value: string): string {
  return value.trim().toLowerCase().replace(/\.$/, '')
}

export function domainError(value: string): string | null {
  const domain = normalizeDomain(value)
  if (!domain) return 'Enter a domain, e.g. example.com.'
  if (domain.includes('@')) return 'Enter only the domain, without a name and @.'
  if (!DOMAIN_SHAPE.test(domain)) return 'Enter a domain such as example.com.'
  return null
}

/**
 * The redirect URI to register at the identity provider. The server builds it
 * from its public address (`APP_BASE_URL`), which is normally the address this
 * page is served from.
 */
export function ssoRedirectUri(origin: string, org: string, fromServer?: string): string {
  if (fromServer) return fromServer
  return `${origin.replace(/\/+$/, '')}/api/v1/auth/sso/${encodeURIComponent(org)}/callback`
}
