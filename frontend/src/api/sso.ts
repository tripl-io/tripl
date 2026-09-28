import { api } from './client'
import type { AuthUser } from '@/types'

/**
 * Per-organization single sign-on over OpenID Connect (F20,
 * backend/src/tripl/api/v1/org_sso.py and the `/auth/sso/*` routes).
 *
 * Hand-written rather than read from `api.gen.ts`: the configuration and the
 * login flow are small, and the browser half of the flow is a full-page
 * navigation (`start` answers a 302 to the identity provider), not a fetch.
 * `/orgs/...` paths are never rewritten by the client: the organization is
 * named in the path.
 */

/** One email domain an organization claims for its single sign-on. */
export interface SsoDomain {
  id: string
  /** Lowercase, e.g. `example.com`. */
  domain: string
  /** Proves ownership once published as `tripl-verification=<token>`. */
  verification_token?: string
  /** Set once the DNS TXT record was found; `null` while unverified. */
  verified_at: string | null
  verified?: boolean
  /** The record to publish, spelled out by the server (`_tripl-verification.<domain>`). */
  txt_record_name?: string
  /** Its value (`tripl-verification=<token>`). */
  txt_record_value?: string
}

/** An organization's OpenID Connect provider. The client secret is write-only. */
export interface SsoConfig {
  /** Whether a provider has been saved at all. */
  configured?: boolean
  issuer: string
  client_id: string
  /** Whether a client secret is stored; the secret itself is never returned. */
  client_secret_configured: boolean
  scopes: string
  enabled: boolean
  sso_required: boolean
  /** The claimed domains, when the server includes them; the page also lists them itself. */
  domains?: SsoDomain[]
  /** What to register at the identity provider, built from `APP_BASE_URL`. */
  redirect_uri?: string
  /** Where members start signing in. */
  login_url?: string
  /** API keys revoked by the save that turned `sso_required` on (absent otherwise). */
  revoked_api_keys?: number
}

/**
 * A save: the whole configuration. `client_secret` is sent only when the
 * owner typed a new one; left out, the stored one is kept (there is no way to
 * read it back).
 */
export interface SsoConfigUpdate {
  issuer: string
  client_id: string
  client_secret?: string
  scopes: string
  enabled: boolean
  sso_required: boolean
}

/** The discovery probe: whether the issuer answers and names itself. */
export interface SsoTestResponse {
  ok: boolean
  message: string
  error_code?: string | null
  authorization_endpoint?: string | null
  token_endpoint?: string | null
  jwks_uri?: string | null
}

/** An organization that signs this address in through its identity provider. */
export interface SsoDiscoveredOrg {
  slug: string
  name: string
  login_url?: string
}

export interface SsoDiscoverResponse {
  orgs: SsoDiscoveredOrg[]
}

/**
 * What a link ticket would do, shown before the user confirms it. Optional on
 * the server: the page falls back to generic words without it.
 */
export interface SsoLinkPreview {
  email: string
  org_slug: string
  org_name: string
  expires_at?: string
  /**
   * This browser must sign in to the account (with its password, or another
   * way) before it can confirm: the identity provider's sign-in alone does not
   * prove the account is yours.
   */
  sign_in_required?: boolean
}

/** A confirmed link: the new session's account, and where the sign-in was headed. */
export interface SsoLinkResult {
  next?: string
  user?: AuthUser
}

/** The TXT record an owner publishes to prove a domain. */
export const SSO_TXT_PREFIX = '_tripl-verification.'
export const SSO_TXT_VALUE_PREFIX = 'tripl-verification='

export function ssoTxtName(domain: string): string {
  return `${SSO_TXT_PREFIX}${domain}`
}

export function ssoTxtValue(token: string): string {
  return `${SSO_TXT_VALUE_PREFIX}${token}`
}

/** The record name for `domain`: the server's spelling, else built from the domain. */
export function domainTxtName(domain: SsoDomain): string {
  return domain.txt_record_name ?? ssoTxtName(domain.domain)
}

/** The record value for `domain`: the server's spelling, else built from its token. */
export function domainTxtValue(domain: SsoDomain): string {
  return domain.txt_record_value ?? ssoTxtValue(domain.verification_token ?? '')
}

export function domainVerified(domain: SsoDomain): boolean {
  return domain.verified_at !== null && domain.verified_at !== undefined
}

function base(org: string): string {
  return `/orgs/${encodeURIComponent(org)}/sso`
}

/**
 * Only a same-origin relative path is a place to come back to: it starts with
 * one `/`, never `//` (another host) or `/\` (which some browsers read as one).
 * The server checks it again; this keeps the SPA from asking for a refusal.
 */
export function safeNextPath(next: string | null | undefined): string | null {
  if (!next || !next.startsWith('/') || next.startsWith('//') || next.startsWith('/\\')) return null
  return next
}

/**
 * The address that begins a sign-in through `org`'s identity provider. A
 * browser navigation target, not a fetch: the server answers with a redirect
 * to the provider, which comes back to the callback with the session cookie.
 */
export function ssoStartUrl(org: string, next?: string | null): string {
  const path = `/api/v1/auth/sso/${encodeURIComponent(org)}/start`
  const safe = safeNextPath(next)
  return safe && safe !== '/' ? `${path}?next=${encodeURIComponent(safe)}` : path
}

export const ssoApi = {
  // Configuration: organization OWNERS only, from a signed-in session.
  get: (org: string) => api.get<SsoConfig>(base(org)),
  update: (org: string, data: SsoConfigUpdate) => api.put<SsoConfig>(base(org), data),
  test: (org: string) => api.post<SsoTestResponse>(`${base(org)}/test`),
  listDomains: (org: string) => api.get<SsoDomain[]>(`${base(org)}/domains`),
  addDomain: (org: string, domain: string) =>
    api.post<SsoDomain>(`${base(org)}/domains`, { domain }),
  deleteDomain: (org: string, id: string) =>
    api.del<void>(`${base(org)}/domains/${encodeURIComponent(id)}`),
  verifyDomain: (org: string, id: string) =>
    api.post<SsoDomain>(`${base(org)}/domains/${encodeURIComponent(id)}/verify`),

  // Sign-in: public.
  discover: (email: string) =>
    api.get<SsoDiscoverResponse>(`/auth/sso/discover?email=${encodeURIComponent(email)}`),
  linkPreview: (ticket: string) =>
    api.get<SsoLinkPreview>(`/auth/sso/link?ticket=${encodeURIComponent(ticket)}`),
  /**
   * Confirms linking the signed-in-at-the-IdP identity to the existing account.
   * Needs a session of that account (401 otherwise); 403 for an account
   * removed from the organization.
   */
  confirmLink: (ticket: string) => api.post<SsoLinkResult>('/auth/sso/link', { ticket }),
}

/**
 * The codes the callback puts on `/auth?sso_error=`; never the provider's own
 * text. Exactly the constants of `backend/src/tripl/services/sso_login_service.py`.
 */
export type SsoErrorCode =
  | 'sso_unavailable'
  | 'invalid_state'
  | 'idp_error'
  | 'idp_denied'
  | 'invalid_token'
  | 'email_missing'
  | 'email_not_verified'
  | 'email_domain_not_allowed'
  | 'membership_removed'
  | 'rate_limited'
  | 'sso_failed'

const SSO_ERROR_MESSAGES: Record<SsoErrorCode, string> = {
  sso_unavailable: 'Single sign-on is not turned on for this organization.',
  invalid_state:
    'That single sign-on attempt expired or was already used. Start signing in again.',
  idp_error:
    'Your identity provider did not complete the sign-in. Try again, or ask your administrator to check the single sign-on setup.',
  idp_denied: 'The sign-in was cancelled or refused at your identity provider.',
  invalid_token:
    'The sign-in answer from your identity provider could not be verified. Try again, or ask your administrator to check the single sign-on setup.',
  email_missing:
    'Your identity provider did not send an email address, so tripl cannot sign you in with it.',
  email_not_verified:
    'Your identity provider did not confirm your email address, so tripl cannot sign you in with it.',
  email_domain_not_allowed:
    "Your email address's domain is not one this organization signs in with single sign-on.",
  membership_removed:
    'You were removed from this organization. Ask an administrator to invite you again.',
  rate_limited: 'Too many sign-in attempts. Wait a minute, then try again.',
  sso_failed: 'Single sign-on could not finish. Try again.',
}

/** Words for a `sso_error` code; an unknown code still says the sign-in failed. */
export function ssoErrorMessage(code: string): string {
  return (
    SSO_ERROR_MESSAGES[code as SsoErrorCode] ??
    'Single sign-on did not complete. Try again, or sign in another way.'
  )
}
