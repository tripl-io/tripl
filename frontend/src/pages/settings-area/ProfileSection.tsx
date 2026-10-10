import { useAuth } from '@/components/auth-context'
import { InfoRow, SCard, SHeader } from '@/components/settings/kit'
import { RoleChip } from '@/components/settings/role-chip'
import { UserAvatar } from '@/components/ui/user-avatar'
import { activeOrgRole } from '@/lib/permissions'
import { ComingLaterCard } from './ComingLaterCard'
import { NotificationPrefsCard } from './NotificationPrefsCard'
import { currentZoneName } from './timeZones'

/**
 * Timestamps render in *your browser's* timezone, so that is what this page
 * shows. It used to render a hardcoded "Europe/Berlin" from a five-city list,
 * which a reader in Tokyo could only read as their account being set wrong.
 * Under its current name: the browser may report `Asia/Calcutta` for Kolkata.
 */
function browserTimezone(): string {
  return currentZoneName(Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC')
}


const UNBUILT = [
  { title: 'Avatar and name', detail: 'uploading a picture and editing the name set when the account was created.' },
  {
    title: 'Display preferences',
    detail: 'date format and start of week. Timestamps are relative, in your browser’s timezone, for everyone.',
  },
] as const

/**
 * Account · Profile: what the account really holds — name, email, role — the
 * one editable card, Notifications (#259), and one card for what is not built.
 *
 * The details are read-only: there is no endpoint to rename yourself, and the
 * organization role is set on Members. The card says so itself; a lock banner
 * over the whole page used to say nothing here could change, above the
 * Notifications card, which can. The display preferences have no backend;
 * they were first live controls that persisted nowhere, then the same
 * controls disabled, and are now named in the Coming later card.
 */
export default function ProfileSection() {
  const { user } = useAuth()
  // The role in the organization the app acts in, the one Members shows:
  // `user.role` is the default organization's.
  const role = activeOrgRole(user)
  // An owner is who sets roles; anyone else is told who sets theirs.
  const detailsNote =
    role === 'owner'
      ? "Your name and email can't be changed here yet."
      : "Your name and email can't be changed here yet. An organization owner or admin sets your role."

  return (
    <div>
      <SHeader title="Profile" description="Your personal details across every project you belong to." />

      {/* Read-only values in read-only rows: editable-form Field rows
          top-aligned each value about 6px off its label and made four facts
          380px tall. The avatar and name head the card; the rest are InfoRows,
          in the body font — mono is for machine identifiers. */}
      <SCard title="Your details" description={detailsNote}>
        <div
          className="flex items-center gap-3 px-4 py-[13px] border-b border-b-border-subtle"
        >
          {/* The shared avatar on --avatar-bg, the colour the sidebar shows for
              the same account. A hand-picked lighter blue here fell below AA
              for the white initials and read as a second identity. */}
          <UserAvatar name={user?.name || user?.email} size={40} />
          <div className="min-w-0">
            <div className="truncate text-body font-medium">{user?.name || '—'}</div>
            <div className="text-caption text-fg-tertiary">
              Set when the account was created.
            </div>
          </div>
        </div>
        <InfoRow label="Email" value={user?.email ?? '—'} mono={false} />
        <InfoRow label="Role" value={role ? <RoleChip role={role} /> : '—'} mono={false} />
        <InfoRow
          label="Timezone"
          value={
            <>
              <span>{browserTimezone()}</span>
              <span className="text-fg-tertiary"> · from this browser</span>
            </>
          }
          mono={false}
          last
        />
      </SCard>

      <NotificationPrefsCard />

      <ComingLaterCard items={UNBUILT} />
    </div>
  )
}
