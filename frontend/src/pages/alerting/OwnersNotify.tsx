import { useMutation } from '@tanstack/react-query'
import { Mail } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { getErrorMessage } from '@/lib/utils'
import type { AlertOwnerNotification, SignalOwnerRef } from '@/types'

import { notifyOwnersResultSummary, ownersLabel } from './ownerNotifications'

/**
 * "Owners: @anna, @oleg" and, for an editor, a "Notify owners" button that
 * emails them once, now (F07, #260). Shared by the incident card and the
 * drilldown's Signal card.
 *
 * Renders nothing when the payload names no owners: an unowned type behaves
 * exactly as before, and a server that predates owner routing omits the field.
 * The result of a click is said in a live region under the button — who was
 * emailed and who was not, and why — because a skipped owner (not a member any
 * more, no email, SMTP off) looks like success unless it is spelled out.
 */
export function OwnersNotify({
  owners,
  canNotify,
  notify,
  target,
  className,
}: {
  owners: readonly SignalOwnerRef[] | null | undefined
  /** False hides the button (a viewer, or a signal its incident owns). */
  canNotify: boolean
  notify: () => Promise<AlertOwnerNotification[]>
  /** What the button acts on, for its accessible name ("… for checkout_completed"). */
  target: string
  className?: string
}) {
  const mutation = useMutation({ mutationFn: notify })
  const label = ownersLabel(owners)
  if (!label) return null

  return (
    <div className={className} data-testid="owners-notify">
      <div className="flex flex-wrap items-center gap-2 text-body-sm">
        <span className="text-fg-secondary">{label}</span>
        {canNotify && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={mutation.isPending}
            aria-label={`Notify owners of ${target} by email`}
            onClick={() => mutation.mutate()}
          >
            <Mail aria-hidden="true" />
            {mutation.isPending ? 'Notifying…' : 'Notify owners'}
          </Button>
        )}
      </div>
      <p role="status" className="m-0 mt-1 text-body-sm text-fg-tertiary empty:hidden">
        {mutation.isSuccess ? notifyOwnersResultSummary(mutation.data) : ''}
      </p>
      {mutation.isError && (
        <p role="alert" className="m-0 mt-1 text-body-sm text-destructive">
          Could not notify owners: {getErrorMessage(mutation.error)}
        </p>
      )}
    </div>
  )
}
