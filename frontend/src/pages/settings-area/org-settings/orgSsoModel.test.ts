import { describe, expect, it } from 'vitest'
import { domainTxtName, domainTxtValue, type SsoConfig, type SsoDomain } from '@/api/sso'
import {
  buildSsoUpdate,
  changedSsoFields,
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
      issuer: 'https://idp.example.com',
      client_id: 'other',
      scopes: 'openid email profile',
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
