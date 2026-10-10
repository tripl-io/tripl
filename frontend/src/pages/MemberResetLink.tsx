import type { MemberPasswordResetLink } from '@/api/orgs'
import { OneTimeSecretField } from '@/components/settings/one-time-secret'
import { Button } from '@/components/ui/button'
import { formatDateTime } from '@/lib/datetime'

/**
 * The password reset link just created for a member, under their row on
 * Members. For an instance that cannot email one: the owner or admin hands it
 * over themselves. It is shown this once, but nothing is lost by dismissing
 * it: a new link can be created at any time and replaces this one.
 */
export function MemberResetLinkPanel({
  who,
  link,
  onDismiss,
}: {
  who: string
  link: MemberPasswordResetLink
  onDismiss: () => void
}) {
  const url = `${window.location.origin}${link.reset_path}`
  return (
    <div className="mt-2 space-y-1.5 rounded-md border border-border-subtle px-3 py-2.5">
      <div className="flex items-start justify-between gap-2">
        <p className="m-0 text-body-sm font-medium">Password reset link for {who} — copy it now</p>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="max-md:min-h-10"
          onClick={onDismiss}
          aria-label={`Dismiss the reset link for ${who}`}
        >
          Dismiss
        </Button>
      </div>
      <p className="text-caption text-fg-tertiary">
        Give it to {who} yourself: whoever opens it can choose a new password for {link.email}. It
        works once, until {formatDateTime(link.expires_at)}, and is not shown again. Their current
        password keeps working until the link is used; using it signs them out everywhere and
        revokes their API keys.
      </p>
      <OneTimeSecretField key={url} value={url} label="Password reset link" noun="link" />
    </div>
  )
}
