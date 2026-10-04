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
