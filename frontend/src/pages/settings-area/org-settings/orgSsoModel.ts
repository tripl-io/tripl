import {
  SAML_NAME_ID_EMAIL,
  domainVerified,
  type SsoConfig,
  type SsoConfigUpdate,
  type SsoDomain,
  type SsoProtocol,
} from '@/api/sso'

/**
 * Organization › Single sign-on (F20): the pure half of the page. The draft of
 * the provider fields (OpenID Connect or SAML 2.0), what a save sends, and
 * when SSO may be turned on.
 */

export const ORG_SSO_PATH = 'organization/sso'

export const DEFAULT_SSO_SCOPES = 'openid email profile'

/** The OpenID Connect fields the owner edits and saves together. */
export type OidcTextField = 'issuer' | 'client_id' | 'client_secret' | 'scopes'

/** The SAML 2.0 fields the owner edits and saves together. */
export type SamlTextField =
  | 'saml_idp_entity_id'
  | 'saml_idp_sso_url'
  | 'saml_idp_certs'
  | 'saml_email_attribute'
  | 'saml_name_id_format'

export type SsoTextField = OidcTextField | SamlTextField

export const OIDC_FIELDS: readonly OidcTextField[] = ['issuer', 'client_id', 'client_secret', 'scopes']
export const SAML_FIELDS: readonly SamlTextField[] = [
  'saml_idp_entity_id',
  'saml_idp_sso_url',
  'saml_idp_certs',
  'saml_email_attribute',
  'saml_name_id_format',
]

/** Edits not yet saved. A field left out is the saved value. */
export type SsoDraft = Partial<Record<SsoTextField, string>> & { protocol?: SsoProtocol }

export type SsoDraftField = SsoTextField | 'protocol'

/** The NameID formats the page offers; the email one is the default. */
export const SAML_NAME_ID_FORMATS: readonly { value: string; label: string }[] = [
  { value: SAML_NAME_ID_EMAIL, label: 'Email address' },
  { value: 'urn:oasis:names:tc:SAML:2.0:nameid-format:persistent', label: 'Persistent' },
  { value: 'urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified', label: 'Unspecified' },
]

export function savedProtocol(config: SsoConfig): SsoProtocol {
  return config.protocol === 'saml' ? 'saml' : 'oidc'
}

/** The protocol the page edits: the draft's choice, else the saved one. */
export function draftProtocol(config: SsoConfig, draft: SsoDraft): SsoProtocol {
  return draft.protocol ?? savedProtocol(config)
}

function savedText(config: SsoConfig, field: SsoTextField): string {
  if (field === 'client_secret') return ''
  if (field === 'scopes') return config.scopes || DEFAULT_SSO_SCOPES
  if (field === 'saml_name_id_format') return config.saml_name_id_format || SAML_NAME_ID_EMAIL
  return config[field] ?? ''
}

/** The value a field shows: the draft's, else the saved one. The secret never has a saved one. */
export function ssoDisplayValue(config: SsoConfig, draft: SsoDraft, field: SsoTextField): string {
  if (field in draft) return draft[field] ?? ''
  return savedText(config, field)
}

function normalizeScopes(value: string): string {
  return value.trim().split(/\s+/).join(' ')
}

/** PEM text with unified line endings, no trailing spaces and no blank edges. */
export function normalizePem(value: string): string {
  return value
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .map((line) => line.trim())
    .join('\n')
    .trim()
}

function normalized(field: SsoTextField, value: string): string {
  if (field === 'scopes') return normalizeScopes(value)
  if (field === 'saml_idp_certs') return normalizePem(value)
  return value.trim()
}

/** The fields of `protocol` the owner edits. */
export function protocolFields(protocol: SsoProtocol): readonly SsoTextField[] {
  return protocol === 'saml' ? SAML_FIELDS : OIDC_FIELDS
}

/**
 * What the draft changes from the saved configuration: the protocol, and the
 * fields of the protocol being edited (the secret when one was typed). Edits
 * to the other protocol's fields are not kept by a save, so they do not count.
 */
export function changedSsoFields(config: SsoConfig, draft: SsoDraft): SsoDraftField[] {
  const protocol = draftProtocol(config, draft)
  const changed: SsoDraftField[] = []
  if (protocol !== savedProtocol(config)) changed.push('protocol')
  for (const field of protocolFields(protocol)) {
    const value = draft[field]
    if (value === undefined) continue
    if (field === 'client_secret') {
      if (value) changed.push(field)
      continue
    }
    if (normalized(field, value) !== normalized(field, savedText(config, field))) changed.push(field)
  }
  return changed
}

function orNull(value: string): string | null {
  return value ? value : null
}

/**
 * What a save sends: the whole configuration. The protocol being edited takes
 * the draft over its saved values; the other protocol keeps what is saved, so
 * switching back and forth loses nothing. `switches` go over the saved
 * switches. A secret goes only when one was typed; left out, the stored one is
 * kept (it cannot be read back).
 */
export function buildSsoUpdate(
  config: SsoConfig,
  draft: SsoDraft,
  switches: Partial<Pick<SsoConfigUpdate, 'enabled' | 'sso_required'>> = {},
): SsoConfigUpdate {
  const protocol = draftProtocol(config, draft)
  const own = protocolFields(protocol)
  const value = (field: SsoTextField): string =>
    normalized(field, own.includes(field) && draft[field] !== undefined ? (draft[field] ?? '') : savedText(config, field))
  const update: SsoConfigUpdate = {
    protocol,
    issuer: orNull(value('issuer')),
    client_id: orNull(value('client_id')),
    scopes: value('scopes'),
    saml_idp_entity_id: orNull(value('saml_idp_entity_id')),
    saml_idp_sso_url: orNull(value('saml_idp_sso_url')),
    saml_idp_certs: orNull(value('saml_idp_certs')),
    saml_name_id_format: value('saml_name_id_format'),
    saml_email_attribute: orNull(value('saml_email_attribute')),
    enabled: switches.enabled ?? config.enabled,
    sso_required: switches.sso_required ?? config.sso_required,
  }
  if (protocol === 'oidc' && draft.client_secret) update.client_secret = draft.client_secret
  return update
}

/**
 * A provider that was never saved has nothing to keep: its required fields
 * must be typed before the first save (issuer and client ID for OpenID
 * Connect; entity ID, SSO URL and a certificate for SAML).
 */
export function firstSaveMissing(config: SsoConfig, draft: SsoDraft): boolean {
  const current = (field: SsoTextField) => ssoDisplayValue(config, draft, field).trim()
  if (draftProtocol(config, draft) === 'saml') {
    return !current('saml_idp_entity_id') || !current('saml_idp_sso_url') || !current('saml_idp_certs')
  }
  return !current('issuer') || !current('client_id')
}

/** What the save bar says while `firstSaveMissing` holds. */
export function firstSaveMessage(config: SsoConfig, draft: SsoDraft): string {
  return draftProtocol(config, draft) === 'saml'
    ? 'Enter the IdP entity ID, SSO URL and signing certificate to save.'
    : 'Enter the issuer URL and client ID to save.'
}

function httpsUrlError(value: string, what: string, example: string): string | null {
  let url: URL
  try {
    url = new URL(value.trim())
  } catch {
    return `Enter a full URL, e.g. ${example}.`
  }
  if (url.protocol !== 'https:') return `${what} must use https.`
  return null
}

/** The issuer must be an https URL; the server checks it again (and its host). */
export function issuerError(value: string): string | null {
  if (!value.trim()) return 'Enter the issuer URL your identity provider publishes.'
  return httpsUrlError(value, 'The issuer', 'https://idp.example.com')
}

export function scopesError(value: string): string | null {
  const scopes = value.trim().split(/\s+/)
  return scopes.includes('openid') ? null : 'Scopes must include openid.'
}

export function samlSsoUrlError(value: string): string | null {
  if (!value.trim()) return 'Enter the single sign-on URL your identity provider publishes.'
  return httpsUrlError(value, 'The SSO URL', 'https://idp.example.com/sso/saml')
}

const PEM_BLOCK = /-----BEGIN CERTIFICATE-----\s*([A-Za-z0-9+/=\s]+?)\s*-----END CERTIFICATE-----/g

/** How many PEM certificate blocks the text holds. */
export function countPemCertificates(value: string): number {
  return [...value.matchAll(PEM_BLOCK)].length
}

/**
 * The certificates must be PEM blocks, one or more, and nothing else. Whether
 * each one parses and is current is the server's check.
 */
export function samlCertsError(value: string): string | null {
  const text = normalizePem(value)
  if (!text) return 'Paste the certificate your identity provider signs assertions with.'
  const count = countPemCertificates(text)
  if (count === 0) {
    return 'Paste the certificate in PEM form, from -----BEGIN CERTIFICATE----- to -----END CERTIFICATE-----.'
  }
  if (text.replace(PEM_BLOCK, '').trim()) {
    return 'Paste only certificates: text outside the BEGIN/END CERTIFICATE lines is not allowed.'
  }
  return null
}

/** An attribute name is optional, but a blank-looking one or one with spaces is a typo. */
export function samlEmailAttributeError(value: string): string | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  if (/\s/.test(trimmed)) return 'An attribute name has no spaces.'
  if (trimmed.length > 255) return 'Keep the attribute name under 256 characters.'
  return null
}

/** Per-field problems of the draft, for the fields it touches. */
export function ssoFieldError(field: SsoTextField, draft: SsoDraft): string | null {
  const value = draft[field]
  if (value === undefined) return null
  switch (field) {
    case 'issuer':
      return issuerError(value)
    case 'client_id':
      return value.trim() ? null : 'Enter the client ID.'
    case 'scopes':
      return scopesError(value)
    case 'saml_idp_entity_id':
      return value.trim() ? null : 'Enter the entity ID (issuer) your identity provider publishes.'
    case 'saml_idp_sso_url':
      return samlSsoUrlError(value)
    case 'saml_idp_certs':
      return samlCertsError(value)
    case 'saml_email_attribute':
      return samlEmailAttributeError(value)
    default:
      return null
  }
}

/** Whether a field of the protocol being edited has a problem. */
export function ssoDraftInvalid(draft: SsoDraft, protocol: SsoProtocol = draft.protocol ?? 'oidc'): boolean {
  return protocolFields(protocol).some((field) => ssoFieldError(field, draft) !== null)
}

/**
 * Is the saved provider complete: issuer, client ID and a stored secret for
 * OpenID Connect; entity ID, SSO URL and certificates for SAML.
 */
export function providerConfigured(config: SsoConfig): boolean {
  if (savedProtocol(config) === 'saml') {
    return Boolean(config.saml_idp_entity_id && config.saml_idp_sso_url && config.saml_idp_certs)
  }
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
  if (!providerConfigured(config)) {
    return savedProtocol(config) === 'saml'
      ? 'Save the IdP entity ID, SSO URL and signing certificate first.'
      : 'Save the issuer, client ID and client secret first.'
  }
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

/** The service-provider values a SAML IdP is configured with. */
export interface SamlSpValues {
  entityId: string
  acsUrl: string
  metadataUrl: string
}

/**
 * The server's spelling when it sends one (built from `APP_BASE_URL`), else
 * built from the address this page is served from.
 */
export function samlSpValues(origin: string, org: string, config?: SsoConfig): SamlSpValues {
  const root = `${origin.replace(/\/+$/, '')}/api/v1/auth/sso/${encodeURIComponent(org)}/saml`
  return {
    entityId: config?.saml_sp_entity_id || `${root}/metadata`,
    acsUrl: config?.saml_acs_url || `${root}/acs`,
    metadataUrl: config?.saml_metadata_url || `${root}/metadata`,
  }
}

/** Within this many days of its expiry a certificate is flagged. */
export const CERT_EXPIRY_WARNING_DAYS = 30

export type CertExpiryState = 'expired' | 'expiring' | 'valid' | 'unknown'

export function certExpiryState(notAfter: string, now: Date = new Date()): CertExpiryState {
  const expiry = Date.parse(notAfter)
  if (Number.isNaN(expiry)) return 'unknown'
  const left = expiry - now.getTime()
  if (left <= 0) return 'expired'
  if (left <= CERT_EXPIRY_WARNING_DAYS * 24 * 60 * 60 * 1000) return 'expiring'
  return 'valid'
}

/** A fingerprint as colon-separated uppercase byte pairs, however the server spelled it. */
export function formatFingerprint(fingerprint: string): string {
  const hex = fingerprint.replace(/[^0-9a-fA-F]/g, '').toUpperCase()
  if (!hex) return fingerprint
  return hex.match(/.{1,2}/g)?.join(':') ?? hex
}
