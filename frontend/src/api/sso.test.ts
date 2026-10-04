import { describe, expect, it } from 'vitest'
import { ssoStartUrl, ssoTxtName, ssoTxtValue } from './sso'

describe('single sign-on client helpers (F20)', () => {
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

})
