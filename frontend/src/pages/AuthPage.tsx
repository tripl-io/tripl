import { Suspense, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { ArrowRight, LockKeyhole, LogIn, Radar, UserPlus } from 'lucide-react'
import { authApi } from '@/api/auth'
import { googleStartUrl, oidcStartUrl, signInErrorMessage } from '@/api/signIn'
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
import { projectsKey } from '@/lib/queryKeys'
import { authStatusQueryOptions } from '@/lib/deploymentMode'
import { AUTH_QUERY_KEY } from '@/components/auth-context'
import { extensionAuthPanels } from '@/extensions'

type CoreMode = 'login' | 'register' | 'forgot' | 'reset'
/** A core form, or an extension's sign-in panel by id (single sign-on). */
type AuthMode = CoreMode | `panel:${string}`

const CARD_COPY: Record<CoreMode, { title: string; description: string }> = {
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

/** Sign-in on the public demo, where Google is the only way in. */
const DEMO_CARD_COPY = {
  title: 'Try the tripl demo',
  description: 'Sign in with Google and get a demo workspace of your own.',
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
/** Google's "G", in its own colours as its sign-in guidelines ask. */
function GoogleMark() {
  return (
    <svg className="h-4 w-4" viewBox="0 0 48 48" aria-hidden="true">
      <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z" />
      <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z" />
      <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z" />
      <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z" />
    </svg>
  )
}

function describedBy(...ids: Array<string | false | null | undefined>): string | undefined {
  const list = ids.filter(Boolean)
  return list.length > 0 ? list.join(' ') : undefined
}

/**
 * A refused submit: the errors render on this pass, so focus the first
 * invalid control on the next frame.
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
  // A failed single sign-on comes back to /auth?sso_error=<code>. Only the
  // code travels, never the identity provider's own words.
  const ssoError = searchParams.get('sso_error')
  // The session-expiry dialog links to /auth?mode=forgot, so that link opens
  // the reset-request form rather than sign-in.
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
  // marked from then on, not while the reader is still typing.
  const [submittedMode, setSubmittedMode] = useState<AuthMode | null>(null)

  const destination = postLoginDestination(location.state)

  // Unauthenticated instance probe: drives the "first account becomes owner"
  // note and whether a sign-up form is worth offering at all. Fetched at boot
  // (main.tsx), so the public demo's Google-only card is usually decided
  // before this page first paints.
  const statusQuery = useQuery(authStatusQueryOptions())
  const isFreshInstance = statusQuery.data?.has_users === false
  // Only a definite `false` closes the door in the UI. While the probe is in
  // flight (or if it failed) we keep offering sign-up — the server is the real
  // gate and still answers 403; guessing "closed" here would hide the form on
  // an open instance every time the page loads.
  // A public demo signs visitors up with Google only.
  const registrationClosed =
    statusQuery.data?.registration_enabled === false || statusQuery.data?.public_demo === true
  const googleSignIn = statusQuery.data?.google_sign_in === true
  const oidcSignIn = statusQuery.data?.oidc_sign_in === true
  const oidcLabel = statusQuery.data?.oidc_button_label || 'Sign in with OpenID Connect'
  // Same rule: only a definite `false` says so before the request. The
  // form stays usable — the server's answer is the same neutral one either way.
  const emailOff = statusQuery.data?.email_configured === false
  // Hosted mode (F20): sign-up creates an organization, and the new account
  // verifies its address before it can use the app.
  const hosted = statusQuery.data?.deployment_mode === 'hosted'
  // The public demo signs visitors in with Google and nothing else. The email
  // form, single sign-on and "Forgot your password?" were all for accounts a
  // visitor does not have, and the closed-sign-up note sent them to ask an
  // owner who does not exist. Its operators still sign in with a password, at
  // /auth?mode=password. A demo without Google configured keeps the form —
  // there would be no way in at all.
  const publicDemo = statusQuery.data?.public_demo === true
  const googleOnly = publicDemo && googleSignIn && searchParams.get('mode') !== 'password'
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
    // the user back into a stale reset form; a single sign-on failure has been
    // read once the user moves on.
    if ((next !== 'reset' && searchParams.has('reset_token')) || searchParams.has('sso_error')) {
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
  // An extension's sign-in panel, when one is open.
  const panel = mode.startsWith('panel:')
    ? extensionAuthPanels.find((candidate) => `panel:${candidate.id}` === mode)
    : undefined
  const { title: cardTitle, description: cardDescription } =
    panel ?? (googleOnly && mode === 'login' ? DEMO_CARD_COPY : CARD_COPY[mode as CoreMode])

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
    // outside the contrast checks every other screen passes.
    <div
      className="min-h-screen text-fg"
      style={{
        background:
          'radial-gradient(circle at top left, var(--accent-soft), transparent 32%), var(--bg)',
      }}
    >
      {/* Top-aligned, not centred: centring re-placed the card every time a
          mode changed its height, so the tabs just clicked moved out from
          under the pointer. */}
      <div className="mx-auto grid min-h-screen max-w-6xl content-start items-start gap-8 px-6 py-10 lg:grid-cols-[1.15fr_0.85fr] lg:pt-[12vh]">
        {/* The product's mark, as the sidebar draws it: above the card on a
            phone, top-left of the page from lg. */}
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
            sign-in card about a screen and a half down on a phone. */}
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
              {publicDemo
                ? 'Walk a ready-made workspace — a tracking plan, live scans, anomalies and alerts on a synthetic warehouse — with a guide that shows you where to click.'
                : 'Sign in to manage catalog coverage, scan production data, review anomalies, and route alerts without losing the operational context of the workspace.'}
            </p>
          </div>
          <div className="hidden gap-4 sm:grid sm:grid-cols-3">
            <FeatureCard
              eyebrow="Catalog"
              title="Track intent"
              description="Keep event definitions, properties, and metadata aligned with the real implementation surface."
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
                ) : panel ? (
                  <panel.icon className="h-4 w-4" />
                ) : (
                  <LockKeyhole className="h-4 w-4" />
                )}
              </div>
            </div>
          </CardHeader>
          <CardContent className="space-y-6 px-6 py-6">
            {ssoError && (mode === 'login' || panel) && (
              <div
                role="alert"
                className="rounded-lg border border-danger/25 bg-danger-soft px-3 py-2 text-body text-danger"
              >
                {signInErrorMessage(ssoError)}
              </div>
            )}

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

            {googleOnly && isAuthTab && (
              <div className="space-y-4">
                <Button asChild size="lg" variant="outline" className="w-full justify-center">
                  <a href={googleStartUrl(destination)}>
                    <GoogleMark />
                    Continue with Google
                  </a>
                </Button>
                <p className="text-body leading-6 text-fg-subtle">
                  You get a generated project on a synthetic warehouse, and a guide
                  that shows you around. Nothing of yours is connected, and a
                  workspace nobody opens for a while is deleted.
                </p>
              </div>
            )}

            {!googleOnly && isAuthTab && (googleSignIn || oidcSignIn) && (
              <div className="space-y-4">
                {/* Navigations, not fetches: the server answers with a
                    redirect to the provider. */}
                {oidcSignIn && (
                  <Button asChild size="lg" variant="outline" className="w-full justify-center">
                    <a href={oidcStartUrl(destination)}>
                      <LogIn aria-hidden="true" />
                      {oidcLabel}
                    </a>
                  </Button>
                )}
                {googleSignIn && (
                  <Button asChild size="lg" variant="outline" className="w-full justify-center">
                    <a href={googleStartUrl(destination)}>
                      <GoogleMark />
                      Continue with Google
                    </a>
                  </Button>
                )}
                <div className="flex items-center gap-3 text-body-sm text-fg-subtle" aria-hidden="true">
                  <span className="h-px flex-1 bg-border" />
                  or with email
                  <span className="h-px flex-1 bg-border" />
                </div>
              </div>
            )}

            {isAuthTab && !googleOnly && (
              <form
                className="space-y-4"
                // Validated here, not by the browser's one-field-at-a-time
                // bubbles.
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

            {mode === 'login' && !googleOnly && extensionAuthPanels.length > 0 && (
              <div className="space-y-4">
                <div className="flex items-center gap-3 text-body-sm text-fg-subtle" aria-hidden="true">
                  <span className="h-px flex-1 bg-border" />
                  or
                  <span className="h-px flex-1 bg-border" />
                </div>
                {extensionAuthPanels.map((candidate) => (
                  <Button
                    key={candidate.id}
                    type="button"
                    variant="outline"
                    size="lg"
                    className="w-full justify-center"
                    onClick={() => switchMode(`panel:${candidate.id}`)}
                  >
                    <candidate.icon className="h-4 w-4" aria-hidden="true" />
                    {candidate.buttonLabel}
                  </Button>
                ))}
              </div>
            )}

            {panel && (
              <Suspense fallback={null}>
                <panel.Component
                  email={email}
                  onEmailChange={setEmail}
                  next={destination}
                  onBack={() => switchMode('login')}
                />
              </Suspense>
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

            {/* Not on the public demo: sign-up there is open, through Google. */}
            {mode === 'login' && registrationClosed && !publicDemo && (
              <p
                role="status"
                className="rounded-lg border border-border bg-bg-sunken px-3 py-2 text-body leading-6 text-fg-muted"
              >
                Sign-ups are closed on this instance. Ask an owner to reopen registration
                under Settings → Instance → Security &amp; access so you can sign up.
              </p>
            )}

            {mode === 'login' && !googleOnly && (
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
