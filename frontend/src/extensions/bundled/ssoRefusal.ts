import { ApiError } from '@/api/client'

/**
 * Recognising an organization's "single sign-on required" refusal (F20): the
 * bundled enterprise extension's shell gate turns it into the sign-in screen.
 */

/**
 * The server's words when an organization that requires single sign-on (F20)
 * refuses a session that did not come through its identity provider.
 */
export const SSO_REQUIRED_DETAIL = 'This organization requires single sign-on'

/** The `detail` text of a refusal, whether flat or nested one level down. */
function refusalDetail(error: ApiError): string | undefined {
  if (error.message === SSO_REQUIRED_DETAIL) return error.message
  const nested = error.detail
  if (nested && typeof nested === 'object' && 'detail' in nested) {
    const text = (nested as { detail?: unknown }).detail
    return typeof text === 'string' ? text : undefined
  }
  return undefined
}

/** A request refused because the organization requires single sign-on. */
export function isSsoRequiredError(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 403 && refusalDetail(error) === SSO_REQUIRED_DETAIL
}

/** The first of `errors` that is a single-sign-on refusal, if any. */
export function findSsoRequiredError(...errors: unknown[]): ApiError | null {
  for (const error of errors) {
    if (isSsoRequiredError(error)) return error
  }
  return null
}

/**
 * Where the refusal says to begin single sign-on: its `sso_start`, which the
 * server builds for the organization. Only a path on this origin under
 * `/api/v1/auth/sso/` is followed; anything else is ignored for the fallback.
 */
export function ssoStartFromError(error: ApiError): string | null {
  const nested = error.detail
  const candidate =
    error.extra?.sso_start ??
    (nested && typeof nested === 'object' && 'sso_start' in nested
      ? (nested as { sso_start?: unknown }).sso_start
      : undefined)
  if (typeof candidate !== 'string') return null
  return candidate.startsWith('/api/v1/auth/sso/') ? candidate : null
}
