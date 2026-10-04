import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api/client'
import { SSO_REQUIRED_DETAIL, findSsoRequiredError, isSsoRequiredError, ssoStartFromError } from './ssoRefusal'

function refusal(): ApiError {
  const error = new ApiError(SSO_REQUIRED_DETAIL, 403)
  error.extra = { sso_start: '/api/v1/auth/sso/acme/start' }
  return error
}

describe('single sign-on refusals (F20)', () => {
  it('tells the SSO refusal apart from any other 403', () => {
    expect(isSsoRequiredError(refusal())).toBe(true)
    expect(isSsoRequiredError(new ApiError('Forbidden', 403))).toBe(false)
    expect(isSsoRequiredError(new ApiError(SSO_REQUIRED_DETAIL, 401))).toBe(false)
    expect(findSsoRequiredError(null, new Error('x'), refusal())).not.toBeNull()
  })

  it('reads a refusal whose detail is nested one level down', () => {
    const nested = new ApiError('403 Forbidden', 403)
    nested.detail = { detail: SSO_REQUIRED_DETAIL, sso_start: '/api/v1/auth/sso/acme/start' }
    expect(isSsoRequiredError(nested)).toBe(true)
    expect(ssoStartFromError(nested)).toBe('/api/v1/auth/sso/acme/start')
  })

  it('follows only a start path on this origin', () => {
    expect(ssoStartFromError(refusal())).toBe('/api/v1/auth/sso/acme/start')
    const foreign = refusal()
    foreign.extra = { sso_start: 'https://evil.example.com/api/v1/auth/sso/acme/start' }
    expect(ssoStartFromError(foreign)).toBeNull()
  })
})
