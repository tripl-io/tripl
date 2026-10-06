import { PageHeader } from '@/components/primitives/page-header'
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'

import { ApiError } from '@/api/client'
import { invitationsApi } from '@/api/invitations'
import { FieldError } from '@/components/forms/FieldError'
import { REQUIRED_MESSAGE, focusFirstInvalid } from '@/components/forms/validation'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { TrifoldMark } from '@/components/states/brand-mark'
import { ROLE_OPTIONS, type AuthUser, type Role } from '@/types'
import { getErrorMessage } from '@/lib/utils'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { PASSWORD_MIN_LENGTH, PASSWORD_POLICY_HINT } from '@/lib/passwordPolicy'
import { invitationPreviewKey } from '@/lib/queryKeys'
import { AUTH_QUERY_KEY } from '@/components/auth-context'
import { orgHomePath } from '@/lib/activeOrg'
import { PasswordInput } from '@/components/ui/password-input'
import { authStatusQueryOptions, isPublicDemoStatus } from '@/lib/deploymentMode'

/**
 * What each ORGANIZATION role can do, in the words of the Concepts page's Roles
 * section (F20 PR4).
 */
const ROLE_BLURB: Readonly<Record<Role, string>> = {
  owner: 'has full control of the organization, including every project, data sources, members and other owners.',
  admin: 'administers the organization — every project, data sources, scans and members — except managing owners.',
  member: 'sees the projects they are added to, as an editor or a viewer of each.',
}

/**
 * Statuses that mean the link itself is spent. The API answers an unknown,
 * expired or used token with one neutral 400; 404 and 410 are read the same way
 * so a future route change cannot turn a dead link into a "try again".
 */
const DEAD_LINK_STATUSES: ReadonlySet<number> = new Set([400, 404, 410])

function isDeadLinkError(error: unknown): boolean {
  return error instanceof ApiError && DEAD_LINK_STATUSES.has(error.status)
}

/** The signed-in account a visitor arrived with, for accepting into it. */
export interface InviteSignedInAccount {
  email: string
  isSigningOut: boolean
  signOut: () => void
}

/**
 * The organization an acceptance added: in the account's list now, not before.
 * `null` when it cannot tell, and the app's home picks one.
 */
function joinedOrgSlug(before: AuthUser | null | undefined, after: AuthUser): string | null {
  const known = new Set((before?.orgs ?? []).map((org) => org.slug))
  return after.orgs.find((org) => !known.has(org.slug))?.slug ?? null
}

/**
 * Redeem an invitation into an account.
 *
 * Reachable without a session by design — the whole point is that this person
 * cannot sign in yet. The address and role come from the invitation, so this
 * form only ever asks for a password and a display name; there is no field that
 * could redirect the invite to a different identity.
 *
 * Unknown, expired and already-used links are indistinguishable here because
 * the API answers all three identically, and this screen must not undo that by
 * guessing at a friendlier explanation.
 *
 * `signedIn` (F20): the visitor arrived signed in. The invitation then joins
 * THAT account to the organization — no password — or they sign out to redeem
 * it into another one. What the API refuses (the invitation names another
 * address; hosted mode and this address is not verified yet) is shown in its
 * own words.
 */
export default function InvitePage({ signedIn }: { signedIn?: InviteSignedInAccount } = {}) {
  const authStatusQuery = useQuery(authStatusQueryOptions())
  const publicDemo = isPublicDemoStatus(authStatusQuery.data)
  const { token = '' } = useParams<{ token: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  // Problems are marked once Accept was pressed, not while typing.
  const [submitted, setSubmitted] = useState(false)

  const previewQuery = useQuery({
    meta: SILENT_ERROR_META,
    queryKey: invitationPreviewKey(token),
    queryFn: () => invitationsApi.preview(token),
    retry: false,
  })

  const joinMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => invitationsApi.acceptSignedIn(token),
    onSuccess: (user: AuthUser) => {
      const joined = joinedOrgSlug(queryClient.getQueryData<AuthUser | null>(AUTH_QUERY_KEY), user)
      queryClient.setQueryData<AuthUser | null>(AUTH_QUERY_KEY, user)
      void navigate(joined ? orgHomePath(joined) : '/', { replace: true })
    },
  })

  const acceptMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => invitationsApi.accept(token, password, name.trim() || undefined),
    onSuccess: (user: AuthUser) => {
      // The API already set the session cookie and answered with the account.
      // Writing it straight into the session query is what lands the user in
      // the app: a refetch left the session "anonymous" until /auth/me came
      // back, long enough for the sign-in screen to flash.
      queryClient.setQueryData<AuthUser | null>(AUTH_QUERY_KEY, user)
      void navigate('/', { replace: true })
    },
  })

  const preview = previewQuery.data
  const passwordProblem = !password
    ? REQUIRED_MESSAGE
    : password.length < PASSWORD_MIN_LENGTH
      ? `Use at least ${PASSWORD_MIN_LENGTH} characters.`
      : null
  const passwordError = submitted ? passwordProblem : null

  const roleLabel = preview
    ? ROLE_OPTIONS.find((r) => r.value === preview.role)?.label ?? preview.role
    : null
  const roleBlurb = publicDemo
    ? 'receives viewer access to the demo projects of this workspace, including ones generated later. Use a verified Google account matching the invited email address.'
    : preview ? ROLE_BLURB[preview.role] : undefined
  // Only the API's invalid-token answer means the link is dead. A network
  // failure, a 5xx or a rate limit says nothing about the link, and telling a
  // valid invitee to ask for a new one sent them away from a working invite.
  const linkIsDead = previewQuery.isError && isDeadLinkError(previewQuery.error)
  const checkFailed = previewQuery.isError && !linkIsDead
  const title = linkIsDead
    ? 'This invite link no longer works'
    : checkFailed
      ? 'Could not check this invitation'
      : 'Join this tripl workspace'

  return (
    // The sign-in page's shell — accent wash, the product mark, one card — so
    // the first screen a teammate ever sees is recognisably tripl.
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
          {/* A logo, not UI text: drawn at the sidebar wordmark's fixed 18px. */}
          <span
            className="font-bold leading-none tracking-[-0.045em]"
            style={{ color: 'var(--fg)', fontSize: 18 }}
          >
            tripl
          </span>
        </div>
        <div
          className="space-y-4 rounded-card border p-6 shadow-lg border-border bg-bg-elevated"
        >
          <PageHeader title={title} />

          {previewQuery.isLoading && (
            <p className="text-body text-fg-tertiary">
              Checking your invitation…
            </p>
          )}

          {linkIsDead && (
            <div className="space-y-4">
              <div className="space-y-1">
                <p role="alert" className="text-body text-destructive">
                  {getErrorMessage(previewQuery.error)}
                </p>
                <p className="text-body-sm text-fg-tertiary">
                  Ask whoever invited you to send a new link.
                </p>
              </div>
              <Button
                type="button"
                variant="outline"
                size="lg"
                className="w-full justify-center"
                onClick={() => void navigate('/auth')}
              >
                Go to sign in
              </Button>
            </div>
          )}

          {checkFailed && (
            <div className="space-y-4">
              <div className="space-y-1">
                <p role="alert" className="text-body text-destructive">
                  {getErrorMessage(previewQuery.error)}
                </p>
                <p className="text-body-sm text-fg-tertiary">
                  Your link may still be fine. Try again in a moment.
                </p>
              </div>
              <Button
                type="button"
                size="lg"
                className="w-full justify-center"
                disabled={previewQuery.isFetching}
                onClick={() => void previewQuery.refetch()}
              >
                {previewQuery.isFetching ? 'Checking…' : 'Try again'}
              </Button>
            </div>
          )}

          {preview && signedIn && (
            <div className="space-y-4">
              <div className="space-y-1">
                <p className="text-body text-fg-tertiary">
                  You were invited as <strong>{preview.email}</strong>, joining as{' '}
                  <strong>{roleLabel}</strong>. You are signed in as{' '}
                  <strong>{signedIn.email}</strong>.
                </p>
                {roleBlurb && (
                  <p className="text-body-sm text-fg-tertiary">
                    {`${roleLabel} ${roleBlurb}`}
                  </p>
                )}
              </div>

              {joinMut.isError && (
                <p role="alert" className="text-body-sm text-destructive">
                  {getErrorMessage(joinMut.error)}
                </p>
              )}

              <Button
                type="button"
                size="lg"
                className="w-full justify-center"
                disabled={joinMut.isPending}
                onClick={() => joinMut.mutate()}
              >
                {joinMut.isPending ? 'Joining…' : 'Accept with this account'}
              </Button>
              <Button
                type="button"
                variant="outline"
                size="lg"
                className="w-full justify-center"
                disabled={signedIn.isSigningOut}
                onClick={signedIn.signOut}
              >
                {signedIn.isSigningOut ? 'Signing out…' : 'Sign out and use another account'}
              </Button>
            </div>
          )}

          {preview && !signedIn && authStatusQuery.isPending && (
            <p className="text-body text-fg-tertiary">Checking sign-in options…</p>
          )}

          {preview && !signedIn && !authStatusQuery.isPending && publicDemo && (
            <div className="space-y-4">
              <p className="text-body text-fg-tertiary">
                You were invited as <strong>{preview.email}</strong>. Sign in with Google using
                this email address to accept the invitation and receive viewer access to the demo projects of this workspace, including ones generated later.
              </p>
              <Button
                type="button"
                size="lg"
                className="w-full justify-center"
                onClick={() => void navigate('/auth', {
                  state: { from: { pathname: `/invite/${token}` } },
                })}
              >
                Sign in with Google
              </Button>
            </div>
          )}

          {preview && !signedIn && !authStatusQuery.isPending && !publicDemo && (
            <>
              <div className="space-y-1">
                <p className="text-body text-fg-tertiary">
                  You were invited as <strong>{preview.email}</strong>, joining as{' '}
                  <strong>{roleLabel}</strong>. Set a password to finish.
                </p>
                {/* What the role means, since "Editor" alone does not say
                    (website/docs/use/concepts.md, Roles). */}
                {roleBlurb && (
                  <p className="text-body-sm text-fg-tertiary">
                    {`${roleLabel} ${roleBlurb}`}
                  </p>
                )}
              </div>
              <form
                className="space-y-3"
                // Checked here and marked under the field, not by a browser
                // bubble; Accept stays pressable so it can say what is missing.
                noValidate
                onSubmit={(e) => {
                  e.preventDefault()
                  setSubmitted(true)
                  if (passwordProblem) {
                    const form = e.currentTarget
                    requestAnimationFrame(() => focusFirstInvalid(form))
                    return
                  }
                  acceptMut.mutate()
                }}
              >
                <div className="space-y-1.5">
                  <Label htmlFor="invite-name" optional>
                    Your name
                  </Label>
                  <Input
                    id="invite-name"
                    autoComplete="name"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="invite-password">Password</Label>
                  {/* The policy up front, as on sign-up, rather than learned from
                      a 422 after submitting. */}
                  <PasswordInput
                    id="invite-password"
                    autoComplete="new-password"
                    aria-required
                    minLength={PASSWORD_MIN_LENGTH}
                    aria-invalid={passwordError ? true : undefined}
                    aria-describedby={
                      passwordError ? 'invite-password-hint invite-password-error' : 'invite-password-hint'
                    }
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                  <p id="invite-password-hint" className="text-body-sm text-fg-tertiary">
                    {PASSWORD_POLICY_HINT}
                  </p>
                  <FieldError inputId="invite-password" message={passwordError} className="mt-0" />
                </div>

                {acceptMut.isError && (
                  <p role="alert" className="text-body-sm text-destructive">
                    {getErrorMessage(acceptMut.error)}
                  </p>
                )}

                <Button type="submit" size="lg" className="w-full justify-center" disabled={acceptMut.isPending}>
                  {acceptMut.isPending ? 'Creating your account…' : 'Accept invitation'}
                </Button>
              </form>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
