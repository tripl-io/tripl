import { describe, expect, it } from 'vitest'
import { SAML_NAME_ID_EMAIL, domainTxtName, domainTxtValue, type SsoConfig, type SsoDomain } from '@/api/sso'
import {
  buildSsoUpdate,
  certExpiryState,
  changedSsoFields,
  countPemCertificates,
  formatFingerprint,
  samlCertsError,
  samlSpValues,
  firstSaveMissing,
  domainError,
  enableBlockedReason,
  issuerError,
  normalizeDomain,
  ssoDisplayValue,
  ssoDraftInvalid,
  ssoRedirectUri,
} from './orgSsoModel'

function config(overrides: Partial<SsoConfig> = {}): SsoConfig {
  return {
    issuer: 'https://idp.example.com',
    client_id: 'tripl',
    client_secret_configured: true,
    scopes: 'openid email profile',
    enabled: false,
    sso_required: false,
    domains: [],
    ...overrides,
  }
}

const verified: SsoDomain = {
  id: 'd1',
  domain: 'example.com',
  verification_token: 'tok',
  verified_at: '2026-09-01T00:00:00Z',
}
const pending: SsoDomain = { ...verified, id: 'd2', domain: 'example.org', verified_at: null }

describe('Organization › Single sign-on model (F20)', () => {
  it('never shows a stored secret, and sends one only when typed', () => {
    expect(ssoDisplayValue(config(), {}, 'client_secret')).toBe('')
    expect(buildSsoUpdate(config(), { client_secret: '' })).not.toHaveProperty('client_secret')
    expect(buildSsoUpdate(config(), { client_secret: 's3cret' }).client_secret).toBe('s3cret')
  })

  it('counts as changed only what differs from the saved values', () => {
    expect(changedSsoFields(config(), { issuer: ' https://idp.example.com ' })).toEqual([])
    expect(changedSsoFields(config(), { scopes: 'openid  email profile' })).toEqual([])
    expect(changedSsoFields(config(), { client_id: ' other ', client_secret: 'x' })).toEqual([
      'client_id',
      'client_secret',
    ])
  })

  it('saves the whole configuration, the draft and switches over the saved values', () => {
    expect(buildSsoUpdate(config({ enabled: true }), { client_id: ' other ' }, { sso_required: true })).toEqual({
      protocol: 'oidc',
      issuer: 'https://idp.example.com',
      client_id: 'other',
      scopes: 'openid email profile',
      saml_idp_entity_id: null,
      saml_idp_sso_url: null,
      saml_idp_certs: null,
      saml_name_id_format: SAML_NAME_ID_EMAIL,
      saml_email_attribute: null,
      enabled: true,
      sso_required: true,
    })
  })

  it('needs an issuer and client ID before the first save', () => {
    const empty = config({ issuer: '', client_id: '' })
    expect(firstSaveMissing(empty, { issuer: 'https://idp.example.com' })).toBe(true)
    expect(firstSaveMissing(empty, { issuer: 'https://idp.example.com', client_id: 'tripl' })).toBe(false)
  })

  it('reads the TXT record from the server when it spells one out', () => {
    expect(domainTxtName({ ...pending, txt_record_name: '_tripl-verification.srv.example' })).toBe(
      '_tripl-verification.srv.example',
    )
    expect(domainTxtValue(pending)).toBe('tripl-verification=tok')
  })

  it('wants an https issuer and openid among the scopes', () => {
    expect(issuerError('http://idp.example.com')).toMatch(/https/)
    expect(issuerError('idp.example.com')).toMatch(/full URL/)
    expect(issuerError('https://idp.example.com')).toBeNull()
    expect(ssoDraftInvalid({ scopes: 'email profile' })).toBe(true)
    expect(ssoDraftInvalid({ scopes: 'openid email' })).toBe(false)
  })

  it('turns SSO on only with a saved provider and a verified domain', () => {
    expect(enableBlockedReason(config({ client_secret_configured: false }), [verified])).toMatch(/client secret/)
    expect(enableBlockedReason(config(), [pending])).toMatch(/Verify at least one/)
    expect(enableBlockedReason(config(), [pending, verified])).toBeNull()
  })

  it('checks and normalises a domain to add', () => {
    expect(normalizeDomain(' Example.COM. ')).toBe('example.com')
    expect(domainError('someone@example.com')).toMatch(/without a name/)
    expect(domainError('localhost')).toMatch(/such as/)
    expect(domainError('-bad.example.com')).toMatch(/such as/)
    expect(domainError('sub.example.com')).toBeNull()
  })

  it('shows the redirect URI to register at the provider', () => {
    expect(ssoRedirectUri('https://tripl.example.com/', 'acme')).toBe(
      'https://tripl.example.com/api/v1/auth/sso/acme/callback',
    )
  })
})

const PEM_A = '-----BEGIN CERTIFICATE-----\nQUFBQQ==\n-----END CERTIFICATE-----'
const PEM_B = '-----BEGIN CERTIFICATE-----\nQkJCQg==\n-----END CERTIFICATE-----'

function saml(overrides: Partial<SsoConfig> = {}): SsoConfig {
  return config({
    protocol: 'saml',
    issuer: null,
    client_id: null,
    client_secret_configured: false,
    scopes: null,
    saml_idp_entity_id: 'https://idp.example.com/saml',
    saml_idp_sso_url: 'https://idp.example.com/sso',
    saml_idp_certs: PEM_A,
    ...overrides,
  })
}

describe('Organization › Single sign-on model › SAML 2.0 (F20)', () => {
  it('saves the SAML draft and keeps the saved OpenID Connect fields', () => {
    const update = buildSsoUpdate(config(), {
      protocol: 'saml',
      saml_idp_entity_id: ' https://idp.example.com/saml ',
      saml_idp_sso_url: 'https://idp.example.com/sso',
      saml_idp_certs: `${PEM_A}\r\n${PEM_B}\r\n`,
      issuer: 'https://ignored.example.com',
      client_secret: 'ignored',
    })
    expect(update).toMatchObject({
      protocol: 'saml',
      issuer: 'https://idp.example.com',
      client_id: 'tripl',
      saml_idp_entity_id: 'https://idp.example.com/saml',
      saml_idp_certs: `${PEM_A}\n${PEM_B}`,
      saml_email_attribute: null,
    })
    expect(update).not.toHaveProperty('client_secret')
  })

  it('counts the protocol switch and only the edited protocol\'s fields as changes', () => {
    expect(changedSsoFields(config(), { protocol: 'saml' })).toEqual(['protocol'])
    expect(changedSsoFields(config(), { protocol: 'oidc' })).toEqual([])
    expect(changedSsoFields(saml(), { issuer: 'https://other.example.com' })).toEqual([])
    expect(changedSsoFields(saml(), { saml_idp_certs: `${PEM_A}\n` })).toEqual([])
    expect(changedSsoFields(saml(), { saml_idp_certs: PEM_B })).toEqual(['saml_idp_certs'])
  })

  it('needs the entity ID, SSO URL and a certificate before the first SAML save', () => {
    const empty = config({ issuer: '', client_id: '' })
    expect(firstSaveMissing(empty, { protocol: 'saml', saml_idp_entity_id: 'x', saml_idp_sso_url: 'https://idp.example.com' })).toBe(true)
    expect(
      firstSaveMissing(empty, {
        protocol: 'saml',
        saml_idp_entity_id: 'x',
        saml_idp_sso_url: 'https://idp.example.com',
        saml_idp_certs: PEM_A,
      }),
    ).toBe(false)
  })

  it('wants an https SSO URL and certificates in PEM only', () => {
    expect(ssoDraftInvalid({ saml_idp_sso_url: 'http://idp.example.com' }, 'saml')).toBe(true)
    expect(ssoDraftInvalid({ saml_idp_sso_url: 'http://idp.example.com' }, 'oidc')).toBe(false)
    expect(samlCertsError('')).toMatch(/Paste the certificate/)
    expect(samlCertsError('QUFBQQ==')).toMatch(/PEM form/)
    expect(samlCertsError(`junk\n${PEM_A}`)).toMatch(/only certificates/)
    expect(samlCertsError(`${PEM_A}\n\n${PEM_B}`)).toBeNull()
    expect(countPemCertificates(`${PEM_A}\n${PEM_B}`)).toBe(2)
    expect(ssoDraftInvalid({ saml_email_attribute: 'e mail' }, 'saml')).toBe(true)
    expect(ssoDraftInvalid({ saml_email_attribute: '' }, 'saml')).toBe(false)
  })

  it('turns SAML on with a saved IdP, no client secret needed', () => {
    expect(enableBlockedReason(saml(), [verified])).toBeNull()
    expect(enableBlockedReason(saml({ saml_idp_certs: null }), [verified])).toMatch(/signing certificate/)
  })

  it("shows the server's SP values, else builds them from this address", () => {
    expect(samlSpValues('https://tripl.example.com/', 'acme')).toEqual({
      entityId: 'https://tripl.example.com/api/v1/auth/sso/acme/saml/metadata',
      acsUrl: 'https://tripl.example.com/api/v1/auth/sso/acme/saml/acs',
      metadataUrl: 'https://tripl.example.com/api/v1/auth/sso/acme/saml/metadata',
    })
    expect(samlSpValues('https://x.example.com', 'acme', saml({ saml_acs_url: 'https://srv.example.com/acs' })).acsUrl).toBe(
      'https://srv.example.com/acs',
    )
  })

  it('flags expired and soon-expiring certificates and spells fingerprints one way', () => {
    const now = new Date('2026-09-01T00:00:00Z')
    expect(certExpiryState('2026-08-31T00:00:00Z', now)).toBe('expired')
    expect(certExpiryState('2026-09-20T00:00:00Z', now)).toBe('expiring')
    expect(certExpiryState('2027-09-01T00:00:00Z', now)).toBe('valid')
    expect(certExpiryState('not a date', now)).toBe('unknown')
    expect(formatFingerprint('ab01cd')).toBe('AB:01:CD')
    expect(formatFingerprint('AB:01:CD')).toBe('AB:01:CD')
  })
})
