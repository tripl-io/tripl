import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { ArrowRight, LockKeyhole, Radar, UserPlus } from 'lucide-react'
import { authApi } from '@/api/auth'
import { FieldError } from '@/components/forms/FieldError'
import { REQUIRED_MESSAGE, focusFirstInvalid, invalidAria } from '@/components/forms/validation'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { TrifoldMark } from '@/components/states/brand-mark'
import { PasswordInput } from '@/components/ui/password-input'
import { cn } from '@/lib/utils'
import { postLoginDestination } from '@/lib/authRedirect'
import { PASSWORD_MIN_LENGTH, PASSWORD_POLICY_HINT } from '@/lib/passwordPolicy'
import { SLUG_ERROR, foldSlug, isValidSlug } from '@/lib/slug'
import type { AuthUser } from '@/types'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { authStatusKey, projectsKey } from '@/lib/queryKeys'
import { AUTH_QUERY_KEY } from '@/components/auth-context'

type AuthMode = 'login' | 'register' | 'forgot' | 'reset'

const CARD_COPY: Record<AuthMode, { title: string; description: string }> = {
  login: {
    title: 'Sign in to tripl',
    description: 'Use your account to access the workspace and monitoring tools.',
  },
  register: {
    title: 'Create your tripl account',
    description:
      'Set up your account to start tracking coverage, monitoring drift, and routing alerts.',
  },
  forgot: {
    title: 'Reset your password',
    description: 'Enter your account email and we will send you a reset link.',
  },
  reset: {
    title: 'Choose a new password',
    description: 'Set a new password to finish resetting your account.',
  },
}

// Just enough to catch a missing @ before the server's 422 would; the backend
// still decides what an acceptable address is.
const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+$/

function emailError(value: string): string | null {
  if (!value.trim()) return REQUIRED_MESSAGE
  if (!EMAIL_SHAPE.test(value.trim())) return 'Enter an email address, e.g. you@company.com.'
  return null
}

function passwordError(value: string, minLength: number): string | null {
  if (!value) return REQUIRED_MESSAGE
  if (value.length < minLength) return `Use at least ${minLength} characters.`
  return null
}

function orgNameError(value: string): string | null {
  return value.trim() ? null : REQUIRED_MESSAGE
}

/** The backend's `OrgCreate.slug` shape; reserved words are left to its 422. */
function orgSlugError(value: string): string | null {
  if (!value.trim()) return REQUIRED_MESSAGE
  if (!isValidSlug(value.trim())) return SLUG_ERROR
  return null
}

/** `aria-describedby` for a control with a standing hint and a possible error. */
function describedBy(...ids: Array<string | false | null | undefined>): string | undefined {
  const list = ids.filter(Boolean)
  return list.length > 0 ? list.join(' ') : undefined
}

/**
 * A refused submit: the errors render on this pass, so focus the first
 * invalid control on the next frame (AU-4).
 */
function focusFirstInvalidSoon(form: HTMLFormElement) {
  requestAnimationFrame(() => focusFirstInvalid(form))
}

export default function AuthPage() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const location = useLocation()
  const [searchParams, setSearchParams] = useSearchParams()

  // A reset link lands on /auth?reset_token=... (the SPA has no dedicated reset
  // route), so an incoming token puts the page straight into reset mode.
  const resetToken = searchParams.get('reset_token') ?? ''
  // The session-expiry dialog links to /auth?mode=forgot, so that link opens
  // the reset-request form rather than sign-in (SH-35).
  const [chosenMode, setChosenMode] = useState<AuthMode>(() =>
    searchParams.get('mode') === 'forgot' ? 'forgot' : 'login',
  )
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  // Hosted sign-up names the organization the new account creates and owns.
  // The slug follows the name until the user types one of their own.
  const [orgName, setOrgName] = useState('')
  const [editedOrgSlug, setEditedOrgSlug] = useState<string | null>(null)
  const orgSlug = editedOrgSlug ?? foldSlug(orgName)
  // The form whose Submit was pressed: its missing or malformed fields are
  // marked from then on, not while the reader is still typing (AU-4).
  const [submittedMode, setSubmittedMode] = useState<AuthMode | null>(null)

  const destination = postLoginDestination(location.state)

  // Unauthenticated instance probe: drives the "first account becomes owner"
  // note and whether a sign-up form is worth offering at all.
  const statusQuery = useQuery({
    queryKey: authStatusKey(),
    queryFn: authApi.status,
  })
  const isFreshInstance = statusQuery.data?.has_users === false
  // Only a definite `false` closes the door in the UI. While the probe is in
  // flight (or if it failed) we keep offering sign-up — the server is the real
  // gate and still answers 403; guessing "closed" here would hide the form on
  // an open instance every time the page loads.
  const registrationClosed = statusQuery.data?.registration_enabled === false
  // Same rule: only a definite `false` says so before the request (ST-24). The
  // form stays usable — the server's answer is the same neutral one either way.
  const emailOff = statusQuery.data?.email_configured === false
  // Hosted mode (F20): sign-up creates an organization, and the new account
  // verifies its address before it can use the app.
  const hosted = statusQuery.data?.deployment_mode === 'hosted'
  // A live reset token always forces reset mode: a reset link must show the reset
  // form even when /auth was ALREADY mounted (same route, new ?reset_token=, no
  // remount). Deriving `mode` — rather than syncing it in an effect — means the
  // token can never be missed and avoids set-state-in-effect. The same derivation
  // falls back to login if the probe resolves "closed" while register mode is
  // already showing (the tab is hidden, but the mode is state that predates it).
  const mode: AuthMode = resetToken
    ? 'reset'
    : registrationClosed && chosenMode === 'register'
      ? 'login'
      : chosenMode

  const authMutation = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () =>
      mode === 'login'
        ? authApi.login({ email, password })
        : authApi.register({
            email,
            password,
            ...(name.trim() ? { name: name.trim() } : {}),
            ...(hosted ? { org_name: orgName.trim(), org_slug: orgSlug.trim() } : {}),
          }),
    onSuccess: async (user: AuthUser) => {
      queryClient.setQueryData<AuthUser | null>(AUTH_QUERY_KEY, user)
      await queryClient.invalidateQueries({ queryKey: projectsKey() })
      navigate(destination, { replace: true })
    },
  })

  const forgotMutation = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => authApi.requestPasswordReset({ email }),
  })

  const resetMutation = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () =>
      authApi.confirmPasswordReset({ token: resetToken, new_password: newPassword }),
  })

  function switchMode(next: AuthMode) {
    setChosenMode(next)
    setSubmittedMode(null)
    authMutation.reset()
    forgotMutation.reset()
    resetMutation.reset()
    // Drop any ?reset_token= when leaving reset mode so a refresh doesn't drop
    // the user back into a stale reset form.
    if (next !== 'reset' && searchParams.has('reset_token')) {
      setSearchParams({}, { replace: true })
    }
  }

  const isAuthTab = mode === 'login' || mode === 'register'
  const submitLabel =
    mode === 'login'
      ? 'Sign in'
      : mode === 'register'
        ? 'Create your account'
        : mode === 'forgot'
          ? 'Send reset link'
          : 'Set new password'
  const { title: cardTitle, description: cardDescription } = CARD_COPY[mode]

  const submitted = submittedMode === mode
  // Register enforces the shared policy; login stays lenient so pre-policy
  // accounts can still sign in.
  const authErrors = {
    email: submitted ? emailError(email) : null,
    password: submitted ? passwordError(password, mode === 'register' ? PASSWORD_MIN_LENGTH : 1) : null,
  }
  const askOrg = mode === 'register' && hosted
  // Until the probe settles the page cannot know whether this is a hosted
  // instance, so it cannot know whether the organization fields belong on the
  // form: a sign-up sent now would go without them and bounce off the server.
  const registerWaitingForStatus = mode === 'register' && statusQuery.isPending
  const orgErrors = {
    name: submitted && askOrg ? orgNameError(orgName) : null,
    slug: submitted && askOrg ? orgSlugError(orgSlug) : null,
  }
  const forgotEmailError = submitted ? emailError(email) : null
  const newPasswordError = submitted ? passwordError(newPassword, PASSWORD_MIN_LENGTH) : null

  return (
    // Theme tokens throughout: the page used to be hard-coded slate and teal,
    // so a light-theme user with a violet accent landed on a dark teal splash,
    // outside the contrast checks every other screen passes (DS-46).
    <div
      className="min-h-screen text-fg"
      style={{
        background:
          'radial-gradient(circle at top left, var(--accent-soft), transparent 32%), var(--bg)',
      }}
    >
      {/* Top-aligned, not centred: centring re-placed the card every time a
          mode changed its height, so the tabs just clicked moved out from
          under the pointer (SH-30). */}
      <div className="mx-auto grid min-h-screen max-w-6xl content-start items-start gap-8 px-6 py-10 lg:grid-cols-[1.15fr_0.85fr] lg:pt-[12vh]">
        {/* The product's mark, as the sidebar draws it: above the card on a
            phone, top-left of the page from lg (SH-31). */}
        <div className="order-first flex items-center gap-2 lg:col-span-2 lg:order-none">
          <TrifoldMark size={24} />
          {/* A logo, not UI text: drawn at the sidebar wordmark's fixed 18px. */}
          <span
            className="font-bold leading-none tracking-[-0.045em]"
            style={{ color: 'var(--fg)', fontSize: 18 }}
          >
            tripl
          </span>
        </div>
        {/* Below lg the form comes first: the pitch stacked above it put the
            sign-in card about a screen and a half down on a phone (SHELL-43). */}
        <section className="order-last space-y-8 lg:order-none">
          <div className="inline-flex items-center gap-2 rounded-full border border-border bg-surface px-3 py-1 text-body-sm uppercase tracking-[0.28em] text-accent">
            <Radar className="h-3.5 w-3.5" aria-hidden="true" />
            Tracking operations
          </div>
          <div className="max-w-2xl space-y-4">
            <h1 className="text-4xl font-semibold tracking-tight text-fg sm:text-5xl">
              Operate the tracking plan before the data drifts.
            </h1>
            <p className="max-w-xl text-heading leading-7 text-fg-muted">
              Sign in to manage catalog coverage, scan production data, review anomalies,
              and route alerts without losing the operational context of the workspace.
            </p>
          </div>
          <div className="hidden gap-4 sm:grid sm:grid-cols-3">
            <FeatureCard
              eyebrow="Catalog"
              title="Track intent"
              description="Keep event definitions, variables, and metadata aligned with the real implementation surface."
            />
            <FeatureCard
              eyebrow="Monitoring"
              title="Catch drift"
              description="Surface the latest scan outcomes and anomaly signals as soon as collection diverges."
            />
            <FeatureCard
              eyebrow="Alerting"
              title="Route action"
              description="Move from suspicious metrics to Slack and Telegram delivery without leaving the product."
            />
          </div>
        </section>

        <Card className="order-first border-border bg-bg-elevated shadow-lg lg:order-none">
          <CardHeader className="border-b border-border px-6 py-6">
            <div className="flex items-center justify-between gap-3">
              <div>
                <CardTitle as="h2" className="text-title text-fg">{cardTitle}</CardTitle>
                <CardDescription className="mt-2 text-fg-subtle">
                  {cardDescription}
                </CardDescription>
              </div>
              <div className="rounded-full border border-accent/30 bg-accent-soft p-2 text-accent">
                {mode === 'register' ? (
                  <UserPlus className="h-4 w-4" />
                ) : (
                  <LockKeyhole className="h-4 w-4" />
                )}
              </div>
            </div>
          </CardHeader>
          <CardContent className="space-y-6 px-6 py-6">
            {isAuthTab && !registrationClosed && (
              <div className="grid grid-cols-2 gap-2 rounded-xl border border-border bg-bg-sunken p-1">
                <button
                  type="button"
                  aria-pressed={mode === 'login'}
                  className={cn(
                    'rounded-lg px-3 py-2 text-body font-medium transition-colors',
                    mode === 'login'
                      ? 'bg-surface text-fg shadow-sm'
                      : 'text-fg-muted hover:text-fg',
                  )}
                  onClick={() => switchMode('login')}
                >
                  Existing account
                </button>
                <button
                  type="button"
                  aria-pressed={mode === 'register'}
                  className={cn(
                    'rounded-lg px-3 py-2 text-body font-medium transition-colors',
                    mode === 'register'
                      ? 'bg-surface text-fg shadow-sm'
                      : 'text-fg-muted hover:text-fg',
                  )}
                  onClick={() => switchMode('register')}
                >
                  Create account
                </button>
              </div>
            )}

            {isAuthTab && (
              <form
                className="space-y-4"
                // Validated here, not by the browser's one-field-at-a-time
                // bubbles (AU-4).
                noValidate
                onSubmit={(event) => {
                  event.preventDefault()
                  setSubmittedMode(mode)
                  const minLength = mode === 'register' ? PASSWORD_MIN_LENGTH : 1
                  const orgInvalid = askOrg && (orgNameError(orgName) || orgSlugError(orgSlug))
                  if (emailError(email) || passwordError(password, minLength) || orgInvalid) {
                    focusFirstInvalidSoon(event.currentTarget)
                    return
                  }
                  authMutation.mutate()
                }}
              >
                {mode === 'register' && (
                  <div className="space-y-2">
                    <Label htmlFor="auth-name">
                      Name
                    </Label>
                    <Input
                      id="auth-name"
                      value={name}
                      onChange={event => setName(event.target.value)}
                      placeholder="Your name"
                      autoComplete="name"
                    />
                  </div>
                )}

                <div className="space-y-2">
                  <Label htmlFor="auth-email">
                    Email
                  </Label>
                  <Input
                    id="auth-email"
                    type="email"
                    autoComplete="email"
                    value={email}
                    onChange={event => setEmail(event.target.value)}
                    placeholder="you@company.com"
                    aria-required
                    {...invalidAria('auth-email', authErrors.email)}
                  />
                  <FieldError inputId="auth-email" message={authErrors.email} className="mt-0" />
                </div>

                <div className="space-y-2">
                  <Label htmlFor="auth-password">
                    Password
                  </Label>
                  <PasswordInput
                    id="auth-password"
                    autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                    value={password}
                    onChange={event => setPassword(event.target.value)}
                    placeholder={mode === 'register' ? PASSWORD_POLICY_HINT : 'Enter your password'}
                    aria-required
                    // Kept for password managers; the form checks it itself.
                    minLength={mode === 'register' ? PASSWORD_MIN_LENGTH : 1}
                    aria-invalid={authErrors.password ? true : undefined}
                    aria-describedby={describedBy(
                      mode === 'register' && 'auth-password-hint',
                      authErrors.password && 'auth-password-error',
                    )}
                  />
                  {mode === 'register' && (
                    <p id="auth-password-hint" className="text-body-sm leading-5 text-fg-subtle">
                      {PASSWORD_POLICY_HINT}
                    </p>
                  )}
                  <FieldError inputId="auth-password" message={authErrors.password} className="mt-0" />
                </div>

                {askOrg && (
                  <>
                    <div className="space-y-2">
                      <Label htmlFor="auth-org-name">
                        Organization name
                      </Label>
                      <Input
                        id="auth-org-name"
                        value={orgName}
                        onChange={event => setOrgName(event.target.value)}
                        placeholder="e.g. Acme Labs"
                        autoComplete="organization"
                        aria-required
                        {...invalidAria('auth-org-name', orgErrors.name)}
                      />
                      <FieldError inputId="auth-org-name" message={orgErrors.name} className="mt-0" />
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="auth-org-slug">
                        Organization URL slug
                      </Label>
                      <Input
                        id="auth-org-slug"
                        className="font-mono"
                        value={orgSlug}
                        onChange={event => setEditedOrgSlug(event.target.value)}
                        placeholder="acme-labs"
                        autoCapitalize="none"
                        autoCorrect="off"
                        spellCheck={false}
                        aria-required
                        aria-invalid={orgErrors.slug ? true : undefined}
                        aria-describedby={describedBy(
                          'auth-org-slug-hint',
                          orgErrors.slug && 'auth-org-slug-error',
                        )}
                      />
                      <p id="auth-org-slug-hint" className="text-body-sm leading-5 text-fg-subtle">
                        Your organization's address,{' '}
                        <span className="font-mono text-fg-muted">/o/{orgSlug.trim() || '<slug>'}</span>.
                        Lowercase letters, digits and single hyphens. It cannot be changed later.
                      </p>
                      <FieldError inputId="auth-org-slug" message={orgErrors.slug} className="mt-0" />
                    </div>
                  </>
                )}

                {mode === 'register' && !hosted && isFreshInstance && (
                  <p className="rounded-lg border border-accent/25 bg-accent-soft px-3 py-2 text-body leading-6 text-fg">
                    The first account on a new instance becomes the owner and can manage
                    members and instance settings.
                  </p>
                )}

                {authMutation.isError && (
                  <div
                    role="alert"
                    className="rounded-lg border border-danger/25 bg-danger-soft px-3 py-2 text-body text-danger"
                  >
                    {authMutation.error.message}
                  </div>
                )}

                <Button
                  type="submit"
                  size="lg"
                  className="w-full justify-center"
                  disabled={authMutation.isPending || registerWaitingForStatus}
                >
                  {authMutation.isPending ? 'Working…' : submitLabel}
                  {!authMutation.isPending && <ArrowRight className="h-4 w-4" />}
                </Button>
              </form>
            )}

            {mode === 'forgot' &&
              (forgotMutation.isSuccess ? (
                <div className="space-y-4">
                  <div
                    role="status"
                    className="rounded-lg border border-accent/25 bg-accent-soft px-3 py-3 text-body leading-6 text-fg"
                  >
                    {forgotMutation.data?.email_configured
                      ? 'If an account exists for that email, a password reset link is on its way. The link expires in one hour.'
                      : 'Self-service password reset is not available on this instance. Contact your instance owner to reset your password.'}
                  </div>
                  <button
                    type="button"
                    onClick={() => switchMode('login')}
                    className="text-body font-medium text-accent underline-offset-4 hover:underline"
                  >
                    Back to sign in
                  </button>
                </div>
              ) : (
                <form
                  className="space-y-4"
                  noValidate
                  onSubmit={(event) => {
                    event.preventDefault()
                    setSubmittedMode(mode)
                    if (emailError(email)) {
                      focusFirstInvalidSoon(event.currentTarget)
                      return
                    }
                    forgotMutation.mutate()
                  }}
                >
                  <div className="space-y-2">
                    <Label htmlFor="forgot-email">
                      Email
                    </Label>
                    <Input
                      id="forgot-email"
                      type="email"
                      autoComplete="email"
                      value={email}
                      onChange={event => setEmail(event.target.value)}
                      placeholder="you@company.com"
                      aria-required
                      {...invalidAria('forgot-email', forgotEmailError)}
                      />
                    <FieldError inputId="forgot-email" message={forgotEmailError} className="mt-0" />
                  </div>

                  {emailOff && (
                    <p className="rounded-lg border border-warning/25 bg-warning-soft px-3 py-2 text-body leading-6 text-fg">
                      This instance can't send email, so no reset link will arrive. Ask your
                      instance owner to reset your password.
                    </p>
                  )}

                  {forgotMutation.isError && (
                    <div
                      role="alert"
                      className="rounded-lg border border-danger/25 bg-danger-soft px-3 py-2 text-body text-danger"
                    >
                      {forgotMutation.error.message}
                    </div>
                  )}

                  <Button
                    type="submit"
                    size="lg"
                    className="w-full justify-center"
                    disabled={forgotMutation.isPending}
                  >
                    {forgotMutation.isPending ? 'Working…' : submitLabel}
                    {!forgotMutation.isPending && <ArrowRight className="h-4 w-4" />}
                  </Button>

                  <button
                    type="button"
                    onClick={() => switchMode('login')}
                    className="text-body font-medium text-accent underline-offset-4 hover:underline"
                  >
                    Back to sign in
                  </button>
                </form>
              ))}

            {mode === 'reset' &&
              (resetMutation.isSuccess ? (
                <div className="space-y-4">
                  <div
                    role="status"
                    className="rounded-lg border border-accent/25 bg-accent-soft px-3 py-3 text-body leading-6 text-fg"
                  >
                    Your password has been reset. Sign in with your new password to continue.
                  </div>
                  <Button
                    type="button"
                    size="lg"
                    className="w-full justify-center"
                    onClick={() => switchMode('login')}
                  >
                    Back to sign in
                    <ArrowRight className="h-4 w-4" />
                  </Button>
                </div>
              ) : (
                <form
                  className="space-y-4"
                  noValidate
                  onSubmit={(event) => {
                    event.preventDefault()
                    setSubmittedMode(mode)
                    if (passwordError(newPassword, PASSWORD_MIN_LENGTH)) {
                      focusFirstInvalidSoon(event.currentTarget)
                      return
                    }
                    resetMutation.mutate()
                  }}
                >
                  <div className="space-y-2">
                    <Label htmlFor="reset-password">
                      New password
                    </Label>
                    <PasswordInput
                      id="reset-password"
                      autoComplete="new-password"
                      value={newPassword}
                      onChange={event => setNewPassword(event.target.value)}
                      placeholder={PASSWORD_POLICY_HINT}
                      aria-required
                      minLength={PASSWORD_MIN_LENGTH}
                      aria-invalid={newPasswordError ? true : undefined}
                      aria-describedby={describedBy(
                        'reset-password-hint',
                        newPasswordError && 'reset-password-error',
                      )}
                      />
                    <p id="reset-password-hint" className="text-body-sm leading-5 text-fg-subtle">
                      {PASSWORD_POLICY_HINT}
                    </p>
                    <FieldError inputId="reset-password" message={newPasswordError} className="mt-0" />
                  </div>

                  {resetMutation.isError && (
                    <div
                      role="alert"
                      className="rounded-lg border border-danger/25 bg-danger-soft px-3 py-2 text-body text-danger"
                    >
                      {resetMutation.error.message}
                    </div>
                  )}

                  <Button
                    type="submit"
                    size="lg"
                    className="w-full justify-center"
                    disabled={resetMutation.isPending}
                  >
                    {resetMutation.isPending ? 'Working…' : submitLabel}
                    {!resetMutation.isPending && <ArrowRight className="h-4 w-4" />}
                  </Button>

                  <button
                    type="button"
                    onClick={() => switchMode('login')}
                    className="text-body font-medium text-accent underline-offset-4 hover:underline"
                  >
                    Back to sign in
                  </button>
                </form>
              ))}

            {mode === 'login' && registrationClosed && (
              <p
                role="status"
                className="rounded-lg border border-border bg-bg-sunken px-3 py-2 text-body leading-6 text-fg-muted"
              >
                Sign-ups are closed on this instance. Ask an owner to reopen registration
                under Settings → Instance → Security &amp; access so you can sign up.
              </p>
            )}

            {mode === 'login' && (
              <div className="space-y-2 text-body leading-6 text-fg-subtle">
                <p>Use the same account across catalog, monitoring, and alerting workflows.</p>
                <button
                  type="button"
                  onClick={() => switchMode('forgot')}
                  className="font-medium text-accent underline-offset-4 hover:underline"
                >
                  Forgot your password?
                </button>
              </div>
            )}

            {mode === 'register' && (
              <p className="text-body leading-6 text-fg-subtle">
                {hosted
                  ? 'You become the owner of the new organization. We will email you a link to verify your address before you can start.'
                  : 'New accounts are created inside this tripl workspace and receive access immediately.'}
              </p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

function FeatureCard({
  eyebrow,
  title,
  description,
}: {
  eyebrow: string
  title: string
  description: string
}) {
  return (
    <div className="rounded-2xl border border-border bg-surface p-4">
      <div className="text-caption font-semibold uppercase tracking-[0.22em] text-accent">
        {eyebrow}
      </div>
      <div className="mt-3 text-heading font-semibold text-fg">{title}</div>
      <p className="mt-2 text-body leading-6 text-fg-muted">{description}</p>
    </div>
  )
}
