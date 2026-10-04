import { describe, expect, it } from 'vitest'
import { safeNextPath, signInErrorMessage } from './signIn'

describe('browser sign-in helpers', () => {
  it('keeps only same-origin relative paths as the place to come back to', () => {
    expect(safeNextPath('/o/acme/p/web')).toBe('/o/acme/p/web')
    expect(safeNextPath('//evil.example.com/x')).toBeNull()
    expect(safeNextPath('/\\evil.example.com')).toBeNull()
    expect(safeNextPath('https://evil.example.com')).toBeNull()
    expect(safeNextPath('relative')).toBeNull()
    expect(safeNextPath(null)).toBeNull()
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
      'saml_invalid',
      'saml_signature_invalid',
      'saml_replay',
      'saml_unsolicited',
      'encrypted_assertion_unsupported',
    ]) {
      expect(signInErrorMessage(code)).not.toMatch(/did not complete\. Try again, or sign in another way/)
    }
    expect(signInErrorMessage('<script>')).toBe('Single sign-on did not complete. Try again, or sign in another way.')
  })
})
