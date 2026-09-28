import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import { MailCheck } from 'lucide-react'

import { authApi } from '@/api/auth'
import { ApiError } from '@/api/client'
import { AUTH_QUERY_KEY, useAuth } from '@/components/auth-context'
import { PageHeader } from '@/components/primitives/page-header'
import { TrifoldMark } from '@/components/states/brand-mark'
import { Button } from '@/components/ui/button'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { verifyEmailKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'

/**
 * The API's answer to a token it will not redeem — unknown, expired, used, or
 * issued to an account other than the one signed in, alike on purpose — is one
 * 400. Anything else (a network drop, a rate limit) says nothing about the link.
 */
function isDeadLinkError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 400
}

/**
 * Confirming needs the signed-in session of the account the link was sent to:
 * holding the link alone proves nothing. Without a session the API answers 401
 * and the token stays unused.
 */
function isSignInNeededError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401
}

const DEAD_LINK_MESSAGE =
  'This verification link is invalid, expired or already used, or it was sent to another account.'

/**
 * `/verify-email?token=…`, the link in the verification email (F20).
 *
 * The route is public, but the API redeems the token only for a browser signed
 * in as the account it was sent to. A visitor without a session is sent to
 * sign in and brought back here (RequireAuth's `state.from` round trip, which
 * AuthPage already honours), where the token is tried again. The token is
 * redeemed once, on arrival; a query rather than an effect-fired mutation, so
 * a re-render or StrictMode's double mount cannot spend the single-use token
 * twice, and it is never refetched while this page stays mounted.
 */
export default function VerifyEmailPage() {
  const [searchParams] = useSearchParams()
  const token = searchParams.get('token') ?? ''
  const queryClient = useQueryClient()
  const auth = useAuth()
  const location = useLocation()

  const confirmQuery = useQuery({
    meta: SILENT_ERROR_META,
    queryKey: verifyEmailKey(token),
    queryFn: async () => {
      await authApi.verifyEmailConfirm({ token })
      // A signed-in tab lifts its "Check your inbox" screen.
      void queryClient.invalidateQueries({ queryKey: AUTH_QUERY_KEY })
      return true
    },
    enabled: token !== '',
    retry: false,
    staleTime: Infinity,
    gcTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })

  const resendMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: authApi.verifyEmailRequest,
  })

  const signedIn = auth.status === 'authenticated'
  // A dead link can be replaced from here only by the account it was for.
  const canResend = signedIn && auth.user?.email_verified === false
  const needsSignIn = confirmQuery.isError && isSignInNeededError(confirmQuery.error)
  const linkIsDead = token === '' || (confirmQuery.isError && isDeadLinkError(confirmQuery.error))
  const checkFailed = confirmQuery.isError && !linkIsDead && !needsSignIn
  const title = confirmQuery.isSuccess
    ? 'Your email address is verified'
    : needsSignIn
      ? 'Sign in to confirm your email address'
      : linkIsDead
        ? 'This verification link does not work'
        : checkFailed
          ? 'Could not verify your email address'
          : 'Verifying your email address…'
  // Back here, token and all, once signed in: the same `state.from` a
  // RequireAuth bounce carries, so AuthPage needs no extra redirect parameter.
  const signInState = {
    from: { pathname: location.pathname, search: location.search, hash: location.hash },
  }

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
          <PageHeader title={title} />

          {confirmQuery.isSuccess && (
            <div className="space-y-4">
              <p role="status" className="flex items-start gap-2 text-body text-fg-muted">
                <MailCheck className="mt-0.5 h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
                Thanks for confirming your address. Your account is ready to use.
              </p>
              <Button asChild size="lg" className="w-full justify-center">
                <Link to={signedIn ? '/' : '/auth'}>{signedIn ? 'Continue to tripl' : 'Sign in'}</Link>
              </Button>
            </div>
          )}

          {needsSignIn && (
            <div className="space-y-4">
              <p role="alert" className="text-body text-fg-muted">
                Sign in to confirm your email address. Sign in as the account this
                link was sent to; you come straight back here afterwards.
              </p>
              <Button asChild size="lg" className="w-full justify-center">
                <Link to="/auth" state={signInState}>
                  Sign in
                </Link>
              </Button>
            </div>
          )}

          {(linkIsDead || checkFailed) && (
            <div className="space-y-4">
              <div className="space-y-1">
                <p role="alert" className="text-body text-destructive">
                  {token === ''
                    ? 'This link is missing its verification code.'
                    : linkIsDead
                      ? DEAD_LINK_MESSAGE
                      : getErrorMessage(confirmQuery.error)}
                </p>
                <p className="text-body-sm text-fg-tertiary">
                  {checkFailed
                    ? 'Your link may still be fine. Try again in a moment.'
                    : canResend
                      ? 'Links expire after 24 hours and work once. If this one was for your account, send yourself a new one.'
                      : signedIn
                        ? 'Links expire after 24 hours and work once. If it was sent to another account, sign in as that account and open it again.'
                        : 'Links expire after 24 hours and work once. Sign in to send yourself a new one.'}
                </p>
              </div>

              {resendMut.isSuccess && (
                <p role="status" className="text-body-sm text-fg-muted">
                  A new link is on its way to {auth.user?.email}.
                </p>
              )}
              {resendMut.isError && (
                <p role="alert" className="text-body-sm text-destructive">
                  {getErrorMessage(resendMut.error)}
                </p>
              )}

              {checkFailed && (
                <Button
                  type="button"
                  size="lg"
                  className="w-full justify-center"
                  disabled={confirmQuery.isFetching}
                  onClick={() => void confirmQuery.refetch()}
                >
                  {confirmQuery.isFetching ? 'Checking…' : 'Try again'}
                </Button>
              )}
              {canResend && !checkFailed && (
                <Button
                  type="button"
                  size="lg"
                  className="w-full justify-center"
                  disabled={resendMut.isPending || resendMut.isSuccess}
                  onClick={() => resendMut.mutate()}
                >
                  {resendMut.isPending ? 'Sending…' : 'Send a new link'}
                </Button>
              )}
              <Button asChild variant="outline" size="lg" className="w-full justify-center">
                <Link to={signedIn ? '/' : '/auth'}>{signedIn ? 'Back to tripl' : 'Go to sign in'}</Link>
              </Button>
            </div>
          )}

          {!confirmQuery.isSuccess && !linkIsDead && !checkFailed && !needsSignIn && (
            <p role="status" className="text-body text-fg-tertiary">
              Checking your link…
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
