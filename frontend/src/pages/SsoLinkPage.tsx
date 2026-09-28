import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { Link2 } from 'lucide-react'

import { ApiError } from '@/api/client'
import { safeNextPath, ssoApi } from '@/api/sso'
import { AUTH_QUERY_KEY } from '@/components/auth-context'
import { PageHeader } from '@/components/primitives/page-header'
import { TrifoldMark } from '@/components/states/brand-mark'
import { Button } from '@/components/ui/button'
import { orgHomePath } from '@/lib/activeOrg'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { projectsKey, ssoLinkPreviewKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'

/** An unknown, expired or used ticket: one neutral 400/404 from the API. */
function isDeadTicketError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 400 || error.status === 404)
}

/** 401: the browser holds no session of the account the ticket names. */
function isSignInRequiredError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401
}

/**
 * `/sso/link?ticket=…` (F20). A single sign-on came back with the address of
 * an account that already exists, and tripl does not attach an identity
 * provider to an existing account on its own: the person confirms it here
 * first. Confirming links the identity, adds the organization membership if
 * it is missing, marks the address verified and signs in. Cancelling leaves
 * the account as it was; the ticket expires on its own after ten minutes.
 *
 * Signing in at the identity provider does not prove the account is yours
 * (whoever runs the provider can name any address of its domain), so the
 * confirmation needs a session OF THE ACCOUNT: when the preview says
 * `sign_in_required` (or the confirm answers 401), the page sends the person
 * to sign in first and brings them back here with the same ticket.
 *
 * The preview (`GET /auth/sso/link?ticket=`) is optional: without it the
 * question is asked in general words.
 */
export default function SsoLinkPage() {
  const [searchParams] = useSearchParams()
  const ticket = searchParams.get('ticket') ?? ''
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  const previewQuery = useQuery({
    meta: SILENT_ERROR_META,
    queryKey: ssoLinkPreviewKey(ticket),
    queryFn: () => ssoApi.linkPreview(ticket),
    enabled: ticket !== '',
    retry: false,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  })

  const confirmMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => ssoApi.confirmLink(ticket),
    onSuccess: async (result) => {
      // The response starts the new session; /auth/me reads it back whole.
      await queryClient.invalidateQueries({ queryKey: AUTH_QUERY_KEY })
      await queryClient.invalidateQueries({ queryKey: projectsKey() })
      // Where the sign-in was headed, when that is a path on this origin;
      // otherwise the organization it was for.
      const next = safeNextPath(result?.next)
      const org = previewQuery.data?.org_slug
      navigate(next && next !== '/' ? next : org ? orgHomePath(org) : '/', { replace: true })
    },
  })

  const preview = previewQuery.data
  const ticketIsDead =
    ticket === '' ||
    (previewQuery.isError && previewQuery.error instanceof ApiError && previewQuery.error.status === 400) ||
    (confirmMut.isError && isDeadTicketError(confirmMut.error))
  const signInRequired =
    preview?.sign_in_required === true ||
    (confirmMut.isError && isSignInRequiredError(confirmMut.error))
  // After signing in, AuthPage returns to `state.from`: this page, same ticket.
  const signInState = {
    from: { pathname: '/sso/link', search: `?ticket=${encodeURIComponent(ticket)}` },
  }
  const title = ticketIsDead ? 'This link request does not work' : 'Link your account to single sign-on'

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

          {ticketIsDead ? (
            <div className="space-y-4">
              <p role="alert" className="text-body text-destructive">
                {ticket === ''
                  ? 'This link is missing its ticket.'
                  : 'This request expired or was already used. Requests last ten minutes and work once.'}
              </p>
              <Button asChild size="lg" className="w-full justify-center">
                <Link to="/auth">Go to sign in</Link>
              </Button>
            </div>
          ) : previewQuery.isPending ? (
            <p role="status" className="text-body text-fg-tertiary">
              Checking the request…
            </p>
          ) : (
            <div className="space-y-4">
              <p className="flex items-start gap-2 text-body text-fg-muted">
                <Link2 className="mt-0.5 h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
                {preview ? (
                  <span>
                    Link your account <strong className="text-fg">{preview.email}</strong> to{' '}
                    <strong className="text-fg">{preview.org_name}</strong> single sign-on?
                  </span>
                ) : (
                  <span>
                    Link your existing tripl account to your organization&rsquo;s single sign-on?
                  </span>
                )}
              </p>
              <p className="text-body-sm text-fg-tertiary">
                You signed in through the organization&rsquo;s identity provider with the address
                of an account that already exists. Once linked, that provider signs you in to this
                account, and you become a member of the organization if you are not one yet. Your
                password keeps working where the organization allows it.
              </p>

              {signInRequired ? (
                <p role="status" className="text-body-sm text-fg">
                  To confirm, first sign in to{' '}
                  {preview ? <strong>{preview.email}</strong> : 'that account'} the way you
                  usually do. You will come back here to finish linking.
                </p>
              ) : (
                confirmMut.isError && (
                  <p role="alert" className="text-body-sm text-destructive">
                    {getErrorMessage(confirmMut.error)}
                  </p>
                )
              )}

              <div className="flex flex-col gap-2 sm:flex-row">
                {signInRequired ? (
                  <Button asChild size="lg" className="flex-1 justify-center">
                    <Link to="/auth" state={signInState}>
                      Sign in to confirm
                    </Link>
                  </Button>
                ) : (
                  <Button
                    type="button"
                    size="lg"
                    className="flex-1 justify-center"
                    disabled={confirmMut.isPending || confirmMut.isSuccess}
                    onClick={() => confirmMut.mutate()}
                  >
                    {confirmMut.isPending ? 'Linking…' : 'Confirm'}
                  </Button>
                )}
                <Button asChild variant="outline" size="lg" className="flex-1 justify-center">
                  <Link to="/auth">Cancel</Link>
                </Button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
