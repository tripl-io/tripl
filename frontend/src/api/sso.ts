import { api } from './client'
import { safeNextPath } from './signIn'
import type { AuthUser } from '@/types'

/**
 * Per-organization single sign-on over OpenID Connect or SAML 2.0 (F20,
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

/** How an organization's identity provider signs people in. */
export type SsoProtocol = 'oidc' | 'saml'

/** The SAML NameID format tripl asks for unless the owner picks another. */
export const SAML_NAME_ID_EMAIL = 'urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress'

/** One saved IdP signing certificate, as the server read it. */
export interface SamlCertInfo {
  /** SHA-256 over the DER bytes, as the server spells it (hex, colon-separated or not). */
  fingerprint_sha256: string
  /** ISO timestamp of the certificate's expiry. */
  not_after: string
  /** The certificate's subject, e.g. `CN=idp.example.com`. */
  subject: string
}

/**
 * An organization's identity provider. The OIDC client secret is write-only;
 * SAML certificates are public and come back in full.
 */
export interface SsoConfig {
  /** Whether a provider has been saved at all. */
  configured?: boolean
  /** Absent from an older server: OpenID Connect. */
  protocol?: SsoProtocol
  // OpenID Connect. Empty or null while the organization uses SAML.
  issuer: string | null
  client_id: string | null
  /** Whether a client secret is stored; the secret itself is never returned. */
  client_secret_configured: boolean
  scopes: string | null
  // SAML 2.0. Null while the organization uses OpenID Connect.
  saml_idp_entity_id?: string | null
  /** The IdP's HTTP-Redirect single sign-on URL (https). */
  saml_idp_sso_url?: string | null
  /** One or more PEM certificates, several while the IdP rotates its key. */
  saml_idp_certs?: string | null
  saml_name_id_format?: string | null
  /** The attribute carrying the email; blank means the NameID is the email. */
  saml_email_attribute?: string | null
  /** What to register at a SAML IdP, built from `APP_BASE_URL` (read-only). */
  saml_sp_entity_id?: string | null
  saml_acs_url?: string | null
  saml_metadata_url?: string | null
  /** The saved certificates, parsed (read-only). */
  saml_cert_info?: SamlCertInfo[] | null
  enabled: boolean
  sso_required: boolean
  /** The claimed domains, when the server includes them; the page also lists them itself. */
  domains?: SsoDomain[]
  /** What to register at an OpenID Connect provider, built from `APP_BASE_URL`. */
  redirect_uri?: string
  /** Where members start signing in. */
  login_url?: string
  /** API keys revoked by the save that turned `sso_required` on (absent otherwise). */
  revoked_api_keys?: number
}

/**
 * A save: the whole configuration, both protocols' fields (the one not in use
 * keeps its saved values, `null` where it has none). `client_secret` is sent
 * only when the owner typed a new one; left out, the stored one is kept
 * (there is no way to read it back).
 */
export interface SsoConfigUpdate {
  protocol: SsoProtocol
  issuer: string | null
  client_id: string | null
  client_secret?: string
  scopes: string | null
  saml_idp_entity_id: string | null
  saml_idp_sso_url: string | null
  saml_idp_certs: string | null
  saml_name_id_format: string
  saml_email_attribute: string | null
  enabled: boolean
  sso_required: boolean
}

/** What pasted IdP metadata named: the values to fill in, not yet saved. */
export interface SamlMetadataImport {
  saml_idp_entity_id: string
  saml_idp_sso_url: string
  saml_idp_certs: string
}

/**
 * The configuration check: for OpenID Connect, whether the issuer answers and
 * names itself; for SAML, whether the certificates parse and are current and
 * the SSO URL is https.
 */
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
  /** Reads pasted IdP metadata XML; nothing is fetched and nothing is saved. */
  importSamlMetadata: (org: string, xml: string) =>
    api.post<SamlMetadataImport>(`${base(org)}/saml/metadata-import`, { xml }),
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
