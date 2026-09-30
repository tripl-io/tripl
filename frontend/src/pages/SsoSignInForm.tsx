import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { ArrowRight } from 'lucide-react'
import { ssoApi, ssoStartUrl, type SsoDiscoveredOrg } from '@/api/sso'
import { FieldError } from '@/components/forms/FieldError'
import { REQUIRED_MESSAGE, focusFirstInvalid, invalidAria } from '@/components/forms/validation'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { navigateToSso } from '@/lib/ssoNavigation'

const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+$/

function emailError(value: string): string | null {
  if (!value.trim()) return REQUIRED_MESSAGE
  if (!EMAIL_SHAPE.test(value.trim())) return 'Enter an email address, e.g. you@example.com.'
  return null
}

/**
 * "Sign in with SSO" on the sign-in card (F20): the work email names the
 * organizations whose identity provider signs that domain in. One answer goes
 * straight to that provider; several ask which organization; none says so,
 * and the password form stays one click away.
 */
export function SsoSignInForm({
  email,
  onEmailChange,
  next,
  onBack,
}: {
  email: string
  onEmailChange: (value: string) => void
  /** Where the app goes once signed in (a same-origin path). */
  next: string
  onBack: () => void
}) {
  const [submitted, setSubmitted] = useState(false)
  const discoverMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (address: string) => ssoApi.discover(address),
    onSuccess: (data) => {
      const [only] = data.orgs
      if (data.orgs.length === 1 && only) navigateToSso(ssoStartUrl(only.slug, next))
    },
  })
  const error = submitted ? emailError(email) : null
  const orgs: SsoDiscoveredOrg[] = discoverMut.data?.orgs ?? []
  const redirecting = discoverMut.isSuccess && orgs.length === 1

  return (
    <div className="space-y-4">
      <form
        className="space-y-4"
        noValidate
        onSubmit={(event) => {
          event.preventDefault()
          setSubmitted(true)
          if (emailError(email)) {
            const form = event.currentTarget
            requestAnimationFrame(() => focusFirstInvalid(form))
            return
          }
          discoverMut.mutate(email.trim())
        }}
      >
        <div className="space-y-2">
          <Label htmlFor="sso-email">Work email</Label>
          <Input
            id="sso-email"
            type="email"
            autoComplete="email"
            value={email}
            onChange={(event) => {
              onEmailChange(event.target.value)
              if (discoverMut.data || discoverMut.error) discoverMut.reset()
            }}
            placeholder="you@example.com"
            aria-required
            {...invalidAria('sso-email', error)}
          />
          <FieldError inputId="sso-email" message={error} className="mt-0" />
        </div>

        {discoverMut.isError && (
          <div
            role="alert"
            className="rounded-lg border border-danger/25 bg-danger-soft px-3 py-2 text-body text-danger"
          >
            {discoverMut.error.message}
          </div>
        )}

        {discoverMut.isSuccess && orgs.length === 0 && (
          <p
            role="status"
            className="rounded-lg border border-border bg-bg-sunken px-3 py-2 text-body leading-6 text-fg-muted"
          >
            No organization signs this address in with single sign-on. Sign in with your password
            instead, or check the address with your administrator.
          </p>
        )}

        {redirecting && (
          <p role="status" className="text-body text-fg-tertiary">
            Taking you to {orgs[0]?.name ?? 'your organization'}&rsquo;s sign-in…
          </p>
        )}

        {orgs.length < 2 && (
          <Button
            type="submit"
            size="lg"
            className="w-full justify-center"
            disabled={discoverMut.isPending || redirecting}
          >
            {discoverMut.isPending ? 'Working…' : 'Continue'}
            {!discoverMut.isPending && <ArrowRight className="h-4 w-4" />}
          </Button>
        )}
      </form>

      {orgs.length > 1 && (
        <div className="space-y-2">
          <p id="sso-org-choice" className="m-0 text-body text-fg-muted">
            Several organizations sign this address in. Choose the one to continue with:
          </p>
          <ul aria-labelledby="sso-org-choice" className="m-0 list-none space-y-2 p-0">
            {orgs.map((org) => (
              <li key={org.slug}>
                <Button
                  type="button"
                  variant="outline"
                  size="lg"
                  className="w-full justify-between"
                  onClick={() => navigateToSso(ssoStartUrl(org.slug, next))}
                >
                  <span className="truncate">{org.name}</span>
                  <span className="font-mono text-caption text-fg-tertiary">{org.slug}</span>
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <button
        type="button"
        onClick={onBack}
        className="text-body font-medium text-accent underline-offset-4 hover:underline"
      >
        Sign in with a password instead
      </button>
    </div>
  )
}
