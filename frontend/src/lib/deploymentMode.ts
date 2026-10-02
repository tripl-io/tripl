import { queryOptions, useQuery } from '@tanstack/react-query'
import { authApi, type AuthStatusResponse } from '@/api/auth'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useIsPlatformAdmin } from '@/lib/permissions'
import { authStatusKey } from '@/lib/queryKeys'

/**
 * `GET /auth/status` for code that only needs the instance's deployment mode.
 * The mode is set from the environment and cannot change while the page is
 * open, so one answer per few minutes is plenty; a failed probe is silent and
 * reads as self-hosted (the backend is the real gate either way).
 */
export function authStatusQueryOptions() {
  return queryOptions({
    queryKey: authStatusKey(),
    queryFn: authApi.status,
    staleTime: 5 * 60_000,
    meta: SILENT_ERROR_META,
  })
}

/** Hosted mode (F20): public sign-up creates an organization; addresses must be verified. */
export function isHostedStatus(status: AuthStatusResponse | null | undefined): boolean {
  return status?.deployment_mode === 'hosted'
}

/** Whether an unverified account is locked out of the app until it verifies. */
export function requiresEmailVerification(status: AuthStatusResponse | null | undefined): boolean {
  return status?.email_verification_required === true
}

/** A public demo (tripl-sav5): the server refuses whatever would reach outside it. */
export function isPublicDemoStatus(status: AuthStatusResponse | null | undefined): boolean {
  return status?.public_demo === true
}

/**
 * Is this instance a public demo? It runs on demo projects only: no blank
 * projects, no connections of one's own, no further organizations — the
 * server refuses them, so the app does not offer them.
 */
export function usePublicDemo(): boolean {
  const { data } = useQuery(authStatusQueryOptions())
  return isPublicDemoStatus(data)
}

/**
 * May the signed-in user create an organization? A platform admin always may;
 * in hosted mode every signed-in (verified) account may too — `POST /orgs`
 * answers the same. Nobody may on a public demo.
 */
export function useCanCreateOrg(): boolean {
  const platformAdmin = useIsPlatformAdmin()
  const { data } = useQuery(authStatusQueryOptions())
  return !isPublicDemoStatus(data) && (platformAdmin || isHostedStatus(data))
}
