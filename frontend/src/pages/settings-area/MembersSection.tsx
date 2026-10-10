import { Suspense } from 'react'
import { Link } from 'react-router-dom'
import { UserPlus } from 'lucide-react'
import { lazyWithReload } from '@/lib/lazyWithReload'
import { settingsPath } from '@/lib/activeOrg'
import { useIsOwner } from '@/lib/permissions'
import { SHeader } from '@/components/settings/kit'
import { SectionSkeleton } from '@/components/states'
import { Button } from '@/components/ui/button'

const UsersPage = lazyWithReload(() => import('@/pages/UsersPage'))

/**
 * Organization · Members. Reuses the existing UsersPage wiring (it self-fetches the
 * roster and owner-gates role changes) under the takeover section header.
 * Membership here is the organization's roster only; which projects a person
 * sees is set per project (Project settings › Access).
 *
 * Owners and admins get an "Invite people" button: this is where a new owner
 * looks for one, and on a phone the settings rail with Invitations in it is
 * behind the menu button. It is the rail's rule (Invitations is owner-only),
 * and it lands on the invite form with the email field focused (`?invite=1`).
 */
export default function MembersSection() {
  const canInvite = useIsOwner()
  return (
    <div>
      <SHeader
        title="Members"
        description="People in this organization. Joining it does not open any project: add each person to the projects they need from Project settings › Access. Owners and admins see every project."
        actions={
          canInvite && (
            <Button asChild size="sm">
              <Link to={settingsPath('/settings/invitations?invite=1')}>
                <UserPlus className="h-3.5 w-3.5" aria-hidden="true" />
                Invite people
              </Link>
            </Button>
          )
        }
      />
      <Suspense
        // The roster's shape under the header, not a 14px "Loading…" (#237).
        fallback={<SectionSkeleton variant="list" label="Loading members…" />}
      >
        <UsersPage />
      </Suspense>
    </div>
  )
}
