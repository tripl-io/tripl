import { useContext } from 'react'
import { Link } from 'react-router-dom'
import { CirclePause } from 'lucide-react'
import { AuthContext } from '@/components/auth-context'
import { Button } from '@/components/ui/button'
import { orgHomePath } from '@/lib/activeOrg'
import { isPlatformAdmin } from '@/lib/permissions'
import type { OrgMembership } from '@/types'

/**
 * A suspended organization (F20), in place of the app shell: every request
 * inside it answers 403 "This organization is suspended" until an operator
 * reinstates it, so a shell full of failing panels would say nothing useful.
 * Offers the user's other organizations, and the way out.
 */
export function OrgSuspendedState({
  orgName,
  otherOrgs,
}: {
  orgName: string
  otherOrgs: readonly OrgMembership[]
}) {
  const auth = useContext(AuthContext)
  return (
    <div
      role="main"
      className="flex h-screen flex-col items-center justify-center-safe overflow-y-auto px-6 py-8 supports-[height:100dvh]:h-dvh bg-background"
    >
      <div
        className="w-full max-w-md space-y-4 rounded-card border p-6"
        style={{ borderColor: 'var(--border)', background: 'var(--surface)' }}
      >
        <CirclePause aria-hidden="true" className="size-5 text-fg-tertiary" />
        <h1 className="m-0 text-heading font-semibold">This organization is suspended</h1>
        <p className="m-0 text-body" style={{ color: 'var(--fg-muted)' }}>
          <strong>{orgName}</strong> has been suspended by the platform operator. Its projects and
          data are kept, but nobody can use them until it is reinstated. Contact the operator of
          this instance to find out more.
        </p>
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
        <div className="flex flex-wrap gap-2">
          {isPlatformAdmin(auth?.user) && (
            <Button asChild variant="outline">
              <Link to="/settings/platform/orgs">Open the platform console</Link>
            </Button>
          )}
          {auth && (
            <Button type="button" variant="outline" onClick={() => void auth.logout()} disabled={auth.isLoggingOut}>
              {auth.isLoggingOut ? 'Signing out…' : 'Sign out'}
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}
