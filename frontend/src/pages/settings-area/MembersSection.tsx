import { Suspense } from 'react'
import { lazyWithReload } from '@/lib/lazyWithReload'
import { SHeader } from '@/components/settings/kit'
import { SectionSkeleton } from '@/components/states'

const UsersPage = lazyWithReload(() => import('@/pages/UsersPage'))

/**
 * Workspace · Members. Reuses the existing UsersPage wiring (it self-fetches the
 * roster and owner-gates role changes) under the takeover section header.
 * Membership here is the instance roster only; which projects a person sees is
 * set per project (Project settings › Access, tripl-vefw).
 */
export default function MembersSection() {
  return (
    <div>
      <SHeader
        title="Members"
        description="People who can sign in to this tripl workspace. Joining it does not open any project: add each person to the projects they need from Project settings › Access. Owners see every project."
      />
      <Suspense
        // The roster's shape under the header, not a 14px "Loading…" (#237 ST-35).
        fallback={<SectionSkeleton variant="list" label="Loading members…" />}
      >
        <UsersPage />
      </Suspense>
    </div>
  )
}
