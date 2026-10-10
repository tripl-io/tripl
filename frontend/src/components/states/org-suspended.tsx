import { useContext } from 'react'
import { Link } from 'react-router-dom'
import { CirclePause } from 'lucide-react'
import { AuthContext } from '@/components/auth-context'
import { Button } from '@/components/ui/button'
import { isPlatformAdmin } from '@/lib/permissions'
import type { OrgMembership } from '@/types'
import { OrgGateScreen } from './org-gate'

/**
 * A suspended organization (F20), in place of the app shell: every request
 * inside it answers 403 "This organization is suspended" until an operator
 * reinstates it. Offers the user's other organizations, the platform console
 * to a platform admin, and the way out.
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
    <OrgGateScreen
      icon={<CirclePause aria-hidden="true" className="size-5 text-fg-tertiary" />}
      title="This organization is suspended"
      otherOrgs={otherOrgs}
      footerActions={
        isPlatformAdmin(auth?.user) && (
          <Button asChild variant="outline">
            <Link to="/settings/platform/orgs">Open the platform console</Link>
          </Button>
        )
      }
    >
      <strong>{orgName}</strong> has been suspended by the platform operator. Its projects and
      data are kept, but nobody can use them until it is reinstated. Contact the operator of
      this instance to find out more.
    </OrgGateScreen>
  )
}
