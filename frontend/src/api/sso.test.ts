import { describe, expect, it } from 'vitest'
import { safeNextPath, ssoErrorMessage, ssoStartUrl, ssoTxtName, ssoTxtValue } from './sso'

describe('single sign-on client helpers (F20)', () => {
  it('keeps only same-origin relative paths as the place to come back to', () => {
    expect(safeNextPath('/o/acme/p/web')).toBe('/o/acme/p/web')
    expect(safeNextPath('//evil.example.com/x')).toBeNull()
    expect(safeNextPath('/\\evil.example.com')).toBeNull()
    expect(safeNextPath('https://evil.example.com')).toBeNull()
    expect(safeNextPath('relative')).toBeNull()
    expect(safeNextPath(null)).toBeNull()
  })

  it('builds the start URL with next only when it is worth sending', () => {
    expect(ssoStartUrl('acme')).toBe('/api/v1/auth/sso/acme/start')
    expect(ssoStartUrl('acme', '/')).toBe('/api/v1/auth/sso/acme/start')
    expect(ssoStartUrl('acme', '/o/acme/p/web?tab=1')).toBe(
      '/api/v1/auth/sso/acme/start?next=%2Fo%2Facme%2Fp%2Fweb%3Ftab%3D1',
    )
    expect(ssoStartUrl('acme', '//evil.example.com')).toBe('/api/v1/auth/sso/acme/start')
  })

  it('names the DNS TXT record a domain is proved with', () => {
    expect(ssoTxtName('example.com')).toBe('_tripl-verification.example.com')
    expect(ssoTxtValue('abc123')).toBe('tripl-verification=abc123')
  })

  it('words every callback error code, and an unknown one generically', () => {
    // The backend's codes (sso_login_service.py), every one worded.
    for (const code of [
      'sso_unavailable',
      'invalid_state',
      'idp_error',
      'idp_denied',
      'invalid_token',
      'email_missing',
      'email_not_verified',
      'email_domain_not_allowed',
      'membership_removed',
      'rate_limited',
      'sso_failed',
    ]) {
      expect(ssoErrorMessage(code)).not.toMatch(/did not complete\. Try again, or sign in another way/)
    }
    expect(ssoErrorMessage('<script>')).toBe('Single sign-on did not complete. Try again, or sign in another way.')
  })
})
