import { APP_LOCALE } from '@/lib/format'
import { ApiError } from '@/api/client'
import type { ActiveStepIn, AuthUser, OrgMembership } from '@/types'

/**
 * The server's words for the two platform-console refusals (F20). Kept equal to
 * the backend's `detail` strings: the shell tells them apart from any other 403
 * by the text, because both arrive on requests that would otherwise succeed.
 */
export const ORG_SUSPENDED_DETAIL = 'This organization is suspended'
export const STEP_IN_READ_ONLY_DETAIL = 'Step-in is read-only'

/** A request refused because its organization is suspended. */
export function isOrgSuspendedError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403 && error.message === ORG_SUSPENDED_DETAIL
}

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
    error.ssoStart ??
    (nested && typeof nested === 'object' && 'sso_start' in nested
      ? (nested as { sso_start?: unknown }).sso_start
      : undefined)
  if (typeof candidate !== 'string') return null
  return candidate.startsWith('/api/v1/auth/sso/') ? candidate : null
}

/** Is the organization on screen suspended, by its membership or by a refusal? */
export function orgIsSuspended(
  membership: Pick<OrgMembership, 'status'> | null | undefined,
  ...errors: unknown[]
): boolean {
  return membership?.status === 'suspended' || errors.some(isOrgSuspendedError)
}

/**
 * The caller's unexpired step-in to `org`, if any. `now` is injectable for
 * tests; an entry past its `expires_at` is ignored even before the session
 * refreshes.
 */
export function activeStepInFor(
  user: Pick<AuthUser, 'active_step_ins'> | null | undefined,
  org: string | null,
  now: number = Date.now(),
): ActiveStepIn | null {
  if (!org) return null
  return (
    user?.active_step_ins?.find(
      (stepIn) => stepIn.org_slug === org && new Date(stepIn.expires_at).getTime() > now,
    ) ?? null
  )
}

/** "14:05": the step-in's end in the viewer's local zone. */
export function stepInEndTime(expiresAt: string): string {
  const date = new Date(expiresAt)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString(APP_LOCALE, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
}

/** Milliseconds from now until `iso`; NaN for an unparseable stamp. */
export function msUntil(iso: string): number {
  return new Date(iso).getTime() - Date.now()
}
