import { useState, type ReactNode } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { MailCheck } from 'lucide-react'
import { authApi } from '@/api/auth'
import { useAuth } from '@/components/auth-context'
import { TrifoldMark } from '@/components/states/brand-mark'
import { Button } from '@/components/ui/button'
import { authStatusQueryOptions, requiresEmailVerification } from '@/lib/deploymentMode'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { getErrorMessage } from '@/lib/utils'

/** How long Resend stays disabled after a link went out. */
export const RESEND_COOLDOWN_MS = 60_000

/**
 * The app behind a verified address (F20 hosted mode). In hosted mode the API
 * refuses an unverified session everywhere but `/auth/*`, so rendering the
 * shell would only draw a screen of 403s; this draws "Check your inbox"
 * instead. Self-hosted never gates: the instance says
 * `email_verification_required: false`, and the probe is not even asked for an
 * account that is verified (every account that existed before F20 is).
 */
export function EmailVerificationGate({ children }: { children: ReactNode }) {
  const auth = useAuth()
  // Only a definite `false`: a fixture or an older answer without the field
  // reads as verified, like the backend's backfill.
  const unverified = auth.user?.email_verified === false
  const statusQuery = useQuery({ ...authStatusQueryOptions(), enabled: unverified })

  if (!unverified) return <>{children}</>
  if (statusQuery.isPending) {
    return (
      <div
        role="status"
        aria-live="polite"
        className="flex min-h-screen items-center justify-center bg-background px-6 text-body text-fg-tertiary"
      >
        Checking session…
      </div>
    )
  }
  // A failed probe falls through to the app: the backend is the real gate, and
  // a self-hosted instance must never be locked out by a flaky status call.
  if (!requiresEmailVerification(statusQuery.data)) return <>{children}</>
  return <CheckInboxScreen email={auth.user?.email ?? ''} />
}

function CheckInboxScreen({ email }: { email: string }) {
  const auth = useAuth()
  const [coolingDown, setCoolingDown] = useState(false)

  const resendMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: authApi.verifyEmailRequest,
    onSuccess: () => {
      setCoolingDown(true)
      // A timer that outlives the screen (the user verified and moved on)
      // only sets state on an unmounted component, which React ignores.
      window.setTimeout(() => setCoolingDown(false), RESEND_COOLDOWN_MS)
    },
  })

  return (
    <div
      className="min-h-screen text-fg"
      style={{
        background:
          'radial-gradient(circle at top left, var(--accent-soft), transparent 32%), var(--bg)',
      }}
    >
      <div className="mx-auto flex min-h-screen max-w-md flex-col gap-6 px-6 py-10 sm:pt-[12vh]">
        <div className="flex items-center gap-2">
          <TrifoldMark size={24} />
          <span
            className="font-bold leading-none tracking-[-0.045em]"
            style={{ color: 'var(--fg)', fontSize: 18 }}
          >
            tripl
          </span>
        </div>
        <div className="space-y-4 rounded-card border border-border bg-bg-elevated p-6 shadow-lg">
          <div className="flex items-center gap-3">
            <div className="rounded-full border border-accent/30 bg-accent-soft p-2 text-accent">
              <MailCheck className="h-4 w-4" aria-hidden="true" />
            </div>
            <h1 className="text-title font-semibold text-fg">Check your inbox</h1>
          </div>
          <p className="text-body leading-6 text-fg-muted">
            We sent a verification link to <strong className="text-fg">{email}</strong>. Open it to
            finish setting up your account. The link expires in 24 hours.
          </p>

          {resendMut.isSuccess && coolingDown && (
            <p
              role="status"
              className="rounded-lg border border-accent/25 bg-accent-soft px-3 py-2 text-body leading-6 text-fg"
            >
              A new link is on its way. You can ask for another one in a minute.
            </p>
          )}
          {resendMut.isError && (
            <p
              role="alert"
              className="rounded-lg border border-danger/25 bg-danger-soft px-3 py-2 text-body text-danger"
            >
              {getErrorMessage(resendMut.error)}
            </p>
          )}

          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              onClick={() => resendMut.mutate()}
              disabled={resendMut.isPending || coolingDown}
            >
              {resendMut.isPending ? 'Sending…' : 'Resend email'}
            </Button>
            {/* Verified in another tab or on another device: ask again. */}
            <Button type="button" variant="outline" onClick={auth.refresh}>
              I have verified it
            </Button>
            <Button
              type="button"
              variant="ghost"
              onClick={() => void auth.logout()}
              disabled={auth.isLoggingOut}
            >
              {auth.isLoggingOut ? 'Signing out…' : 'Sign out'}
            </Button>
          </div>
        </div>
      </div>
    </div>
  )
}
