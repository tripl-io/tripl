import { SHeader } from '@/components/settings/kit'
import { ReadOnlyNotice } from '@/components/states'
import { useIsOrgOwner, useIsOwner } from '@/lib/permissions'
import { InviteMemberCard } from '@/pages/UsersPage'

/**
 * Organization › Invitations (F20 PR7): invite someone into the active
 * organization at an organization role, and list or revoke the pending
 * invitations. `/users/invitations` goes out as `/orgs/{org}/users/invitations`,
 * so both belong to the organization on screen. Owners and admins only; only an
 * owner can invite an owner.
 */
export default function InvitationsSection() {
  const isOwner = useIsOwner()
  const actorIsOrgOwner = useIsOrgOwner()
  return (
    <div>
      <SHeader
        title="Invitations"
        description="Invite people into this organization. An invitation grants an organization role; which projects they see is set per project under Project settings › Access."
      />
      {isOwner ? (
        <InviteMemberCard actorIsOrgOwner={actorIsOrgOwner} />
      ) : (
        <ReadOnlyNotice className="mb-5">Only owners and admins can invite people.</ReadOnlyNotice>
      )}
    </div>
  )
}
