import { useMutation, useQuery } from '@tanstack/react-query'
import { Mail } from 'lucide-react'
import { Link } from 'react-router-dom'
import { authApi } from '@/api/auth'
import { useAuth } from '@/components/auth-context'
import { SCard, SHeader } from '@/components/settings/kit'
import { DisabledReason, disabledReasonAria } from '@/components/states'
import { Button } from '@/components/ui/button'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { isPlatformAdmin } from '@/lib/permissions'
import { authStatusKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import { ComingLaterCard } from './ComingLaterCard'

const UNBUILT = [
  {
    title: 'Changing your password here',
    detail: 'with your current one, without the email round trip.',
  },
  {
    title: 'Two-factor authentication',
    detail: 'a code from an authenticator app at sign-in, and single-use recovery codes.',
  },
  {
    title: 'Signed-in devices',
    detail: 'a list of where the account is signed in, and signing out the others. tripl keeps no per-device record yet.',
  },
] as const

/**
 * Account · Password & sessions (was "Security"; renamed apart from Instance ·
 * Security & access, #238 JR-26).
 *
 * There is no signed-in change-password endpoint, but the email reset flow
 * exists and works, so the password card runs it for the signed-in address in
 * one click instead of telling the reader to sign out and find "Forgot your
 * password?" (WS-37). It used to hold two inputs and an "Update password"
 * button that did nothing at all, so people walked away believing their
 * password had rotated (tripl-2o74), and then the same controls disabled.
 *
 * Two-factor and session management are not built; they share one "Coming
 * later" card instead of a page of switches nobody can move (tripl-91j6).
 */
export default function SecuritySection() {
  const { user } = useAuth()
  const email = user?.email ?? ''
  // Who can fix email: a platform admin. One SMTP relay carries every
  // organization's mail, so its settings are operator-only.
  const canSetUpEmail = isPlatformAdmin(user)
  const resetMut = useMutation({
    // The outcome renders under the button.
    meta: SILENT_ERROR_META,
    mutationFn: () => authApi.requestPasswordReset({ email }),
  })
  // Everyone learns that email is off BEFORE pressing a button that cannot
  // work (ST-24). The unauthenticated instance probe carries the same
  // `email_can_send` answer the reset endpoint uses (host AND From: address),
  // so it is right for owners and non-owners alike. An owner gets the way to
  // fix it; anyone else is told to ask one. Only a definite `false` disables
  // the button: while the probe is in flight, or if it failed, the request's
  // own answer still says whether a link went out.
  const statusQuery = useQuery({
    queryKey: authStatusKey(),
    queryFn: authApi.status,
    meta: SILENT_ERROR_META,
  })
  const emailOff = statusQuery.data?.email_configured === false
  const setUpEmail = (
    <Link to="/settings/instance/email" className="font-medium text-accent no-underline hover:underline">
      Set up email
    </Link>
  )
  const blocker = emailOff ? "This instance can't send email yet." : null

  return (
    <div>
      <SHeader
        title="Password & sessions"
        description="Your password and the places you are signed in."
      />

      <SCard
        title="Password"
        description="Get a reset link by email and choose a new password from it. Your current password keeps working until you do."
      >
        <div className="flex flex-col gap-2 px-4 py-[14px]">
          <div>
            <Button
              variant="outline"
              size="sm"
              disabled={!email || resetMut.isPending || emailOff}
              onClick={() => resetMut.mutate()}
              {...disabledReasonAria('password-reset', blocker)}
            >
              <Mail className="h-3 w-3" />
              {resetMut.isPending ? 'Sending…' : 'Email me a reset link'}
            </Button>
          </div>
          {emailOff && (
            <div className="flex flex-wrap items-baseline gap-x-2">
              <DisabledReason id="password-reset" reason={blocker} />
              <span className="text-caption">{canSetUpEmail ? setUpEmail : 'Ask a platform admin to set it up.'}</span>
            </div>
          )}
          <div aria-live="polite" className="text-body-sm leading-[1.45]">
            {resetMut.isSuccess && resetMut.data.email_configured && (
              <span className="text-success">
                Sent — check {email} for a link to choose a new password.
              </span>
            )}
            {resetMut.isSuccess && !resetMut.data.email_configured && (
              <span className="text-warning">
                This instance can't send email, so no link went out.{' '}
                {canSetUpEmail ? setUpEmail : 'Ask a platform admin to set it up.'}
              </span>
            )}
          </div>
          {resetMut.isError && (
            <p role="alert" className="m-0 text-body-sm text-danger">
              Could not request a reset link: {getErrorMessage(resetMut.error)}
            </p>
          )}
        </div>
      </SCard>

      <ComingLaterCard items={UNBUILT} />
    </div>
  )
}
