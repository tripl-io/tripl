import { useAuth } from '@/components/auth-context'
import { InfoRow, SCard, SHeader } from '@/components/settings/kit'
import { RoleChip } from '@/components/settings/role-chip'
import { UserAvatar } from '@/components/ui/user-avatar'
import { ComingLaterCard } from './ComingLaterCard'
import { NotificationPrefsCard } from './NotificationPrefsCard'
import { ReadOnlyNotice } from '@/components/states'

/**
 * Timestamps render in *your browser's* timezone, so that is what this page
 * shows. It used to render a hardcoded "Europe/Berlin" from a five-city list,
 * which a reader in Tokyo could only read as their account being set wrong
 * (tripl-hmlx).
 */
function browserTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
}


const UNBUILT = [
  { title: 'Avatar and name', detail: 'uploading a picture and editing the name set when the account was created.' },
  {
    title: 'Display preferences',
    detail: 'date format and start of week. Timestamps are relative, in your browser’s timezone, for everyone.',
  },
] as const

/**
 * Account · Profile: what the account really holds — name, email, role — and
 * one card for what is not built (WS-37).
 *
 * The preference and notification controls have no backend and nothing reads
 * them. They were first six live controls that persisted nowhere (tripl-z9ot),
 * then the same controls disabled: honest, but a page of controls that do
 * nothing. "Weekly digest" could never have been a per-person switch at all —
 * the digest is fanned out per project alert destination.
 */
export default function ProfileSection() {
  const { user } = useAuth()

  return (
    <div>
      <SHeader title="Profile" description="Your personal details across every project you belong to." />
      {/* Nothing on this page is editable; say so once, the way every other
          read-only section does, rather than leave a page of values that look
          like they should be (#237 ST-17). */}
      <ReadOnlyNotice className="mb-5">
        Your details can't be changed here yet. A workspace owner sets your role.
      </ReadOnlyNotice>

      {/* Read-only values in read-only rows (ST-23): editable-form Field rows
          top-aligned each value about 6px off its label and made four facts
          380px tall. The avatar and name head the card; the rest are InfoRows,
          in the body font — mono is for machine identifiers. */}
      <SCard title="Your details">
        <div
          className="flex items-center gap-3 px-4 py-[13px] border-b border-b-border-subtle"
        >
          {/* The shared avatar on --avatar-bg, the colour the sidebar shows for
              the same account. A hand-picked lighter blue here fell below AA
              for the white initials and read as a second identity (WS-38). */}
          <UserAvatar name={user?.name || user?.email} size={40} />
          <div className="min-w-0">
            <div className="truncate text-body font-medium">{user?.name || '—'}</div>
            <div className="text-caption text-fg-tertiary">
              Set when the account was created.
            </div>
          </div>
        </div>
        <InfoRow label="Email" value={user?.email ?? '—'} mono={false} />
        <InfoRow label="Role" value={user?.role ? <RoleChip role={user.role} /> : '—'} mono={false} />
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

      {/* The one editable card: how your own notifications reach you (#259). */}
      <NotificationPrefsCard />

      <ComingLaterCard items={UNBUILT} />
    </div>
  )
}
