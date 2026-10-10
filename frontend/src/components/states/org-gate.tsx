import { useContext, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { AuthContext } from '@/components/auth-context'
import { Button } from '@/components/ui/button'
import { orgHomePath } from '@/lib/activeOrg'
import type { OrgMembership } from '@/types'
import { GateCard, ShellStandIn } from './shell-stand-in'

export interface OrgGateScreenProps {
  /** A `size-5 text-fg-tertiary` icon, `aria-hidden`. */
  icon: ReactNode
  title: ReactNode
  /** What happened to the organization, one paragraph. */
  children: ReactNode
  /** The way in, under the paragraph (Enterprise: "Sign in with SSO"). */
  primaryAction?: ReactNode
  /** Beside Sign out (a platform admin's "Open the platform console"). */
  footerActions?: ReactNode
  /** The user's other organizations they can open instead; none hides the list. */
  otherOrgs: readonly OrgMembership[]
}

/**
 * An organization the user cannot use right now, in place of the app shell:
 * every request inside it is refused, so a shell full of failing panels would
 * say nothing useful. Under the reason come the way in when there is one, the
 * user's other organizations, and the way out. Community's suspended
 * organization renders it, and so can an extension's shell gate.
 */
export function OrgGateScreen({
  icon,
  title,
  children,
  primaryAction,
  footerActions,
  otherOrgs,
}: OrgGateScreenProps) {
  const auth = useContext(AuthContext)
  return (
    <ShellStandIn>
      <GateCard icon={icon} title={title} body={children}>
        {primaryAction}
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
        {(footerActions || auth) && (
          <div className="flex flex-wrap gap-2">
            {footerActions}
            {auth && (
              <Button type="button" variant="outline" onClick={() => void auth.logout()} disabled={auth.isLoggingOut}>
                {auth.isLoggingOut ? 'Signing out…' : 'Sign out'}
              </Button>
            )}
          </div>
        )}
      </GateCard>
    </ShellStandIn>
  )
}
