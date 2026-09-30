import { useContext } from 'react'
import { Link } from 'react-router-dom'
import { KeyRound } from 'lucide-react'
import { ssoStartUrl } from '@/api/sso'
import { AuthContext } from '@/components/auth-context'
import { Button } from '@/components/ui/button'
import { orgHomePath } from '@/lib/activeOrg'
import { navigateToSso } from '@/lib/ssoNavigation'
import type { OrgMembership } from '@/types'

/**
 * An organization that requires single sign-on (F20), in place of the app
 * shell: every request inside it answers 403 "This organization requires
 * single sign-on" for a session that did not come through its identity
 * provider, so a shell full of failing panels would say nothing useful. Offers
 * the sign-in that would be accepted, the user's other organizations, and the
 * way out.
 */
export function SsoRequiredState({
  orgName,
  orgSlug,
  serverStart,
  returnTo,
  otherOrgs,
}: {
  orgName: string
  /** The organization in the address; builds the start URL with `next`. */
  orgSlug: string | null
  /** The refusal's own `sso_start`, used when the slug is not known. */
  serverStart: string | null
  /** Where to come back to after signing in (a same-origin path). */
  returnTo: string
  otherOrgs: readonly OrgMembership[]
}) {
  const auth = useContext(AuthContext)
  const startUrl = orgSlug ? ssoStartUrl(orgSlug, returnTo) : serverStart
  return (
    <div
      role="main"
      className="flex h-screen flex-col items-center justify-center-safe overflow-y-auto px-6 py-8 supports-[height:100dvh]:h-dvh bg-background"
    >
      <div
        className="w-full max-w-md space-y-4 rounded-card border p-6"
        style={{ borderColor: 'var(--border)', background: 'var(--surface)' }}
      >
        <KeyRound aria-hidden="true" className="size-5 text-fg-tertiary" />
        <h1 className="m-0 text-heading font-semibold">This organization requires single sign-on</h1>
        <p className="m-0 text-body" style={{ color: 'var(--fg-muted)' }}>
          <strong>{orgName}</strong> only admits sessions that signed in through its identity
          provider. Sign in with SSO to continue; you come back to this page afterwards.
        </p>
        {startUrl && (
          <Button type="button" size="lg" className="w-full justify-center" onClick={() => navigateToSso(startUrl)}>
            Sign in with SSO
          </Button>
        )}
        {otherOrgs.length > 0 && (
          <nav aria-label="Your other organizations">
            <p className="m-0 mb-2 text-body-sm text-fg-tertiary">Your other organizations</p>
            <ul className="m-0 list-none space-y-1 p-0">
              {otherOrgs.map((org) => (
                <li key={org.slug}>
                  <Link to={orgHomePath(org.slug)} className="text-body text-accent no-underline hover:underline">
                    {org.name}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        )}
        {auth && (
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" onClick={() => void auth.logout()} disabled={auth.isLoggingOut}>
              {auth.isLoggingOut ? 'Signing out…' : 'Sign out'}
            </Button>
          </div>
        )}
      </div>
    </div>
  )
}
