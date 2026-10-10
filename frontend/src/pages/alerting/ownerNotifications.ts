import type { ChipTone } from '@/components/primitives/chip'
import type {
  AlertOwnerNotification,
  AlertOwnerNotificationStatus,
  MetricScopeType,
  SignalOwnerRef,
} from '@/types'
import { pluralize } from '@/lib/plural'

/**
 * Owner routing (F07, #260): the pure wording the incident card, the signal
 * card and the delivery detail share. Kept out of the component file so
 * react-refresh sees components only there.
 */

/**
 * Whose owners an alert names, as owner routing resolves them
 * (alert_owner_routing.py): a catalog metric's own owner, or, for anything
 * about an event or an event type (drift, release and lifecycle signals
 * included), that event type's owners. An event's own owner is never one of
 * them, so the line must not read as if it were.
 */
export type OwnerSource = 'metric' | 'event_type'

export function ownerSourceOf(scopeType: MetricScopeType): OwnerSource {
  return scopeType === 'metric' ? 'metric' : 'event_type'
}

/**
 * "Event type owners: @anna, @oleg" ("Event type owner: @anna" for one), or
 * "Owner: @anna" for a metric; null when there is nobody to name.
 */
export function ownersLabel(
  owners: readonly SignalOwnerRef[] | null | undefined,
  source: OwnerSource,
): string | null {
  if (!owners || owners.length === 0) return null
  const noun = source === 'metric' ? 'Owner' : 'Event type owner'
  return `${pluralize(owners.length, noun, `${noun}s`)}: ${owners.map(owner => `@${owner.name}`).join(', ')}`
}

const STATUS_LABEL: Record<AlertOwnerNotificationStatus, string> = {
  pending: 'sending',
  sent: 'emailed',
  failed: 'failed',
  skipped: 'skipped',
}

export function ownerNotificationStatusLabel(status: AlertOwnerNotificationStatus): string {
  return STATUS_LABEL[status]
}

export function ownerNotificationTone(
  status: AlertOwnerNotificationStatus,
): ChipTone {
  if (status === 'sent') return 'success'
  if (status === 'failed') return 'danger'
  return 'neutral'
}

/** Who a notification row names: the user's name, else the address it went to. */
export function ownerNotificationWho(row: AlertOwnerNotification): string {
  return row.name?.trim() || row.email
}

/**
 * Why a row did not send, for a reader: the server's reason when it gave one
 * (e.g. the manual cooldown's "notified 4 minutes ago"), else the status word.
 */
export function ownerNotificationReason(row: AlertOwnerNotification): string {
  return row.error?.trim() || ownerNotificationStatusLabel(row.status)
}

/**
 * One sentence for what a "Notify owners" click did. A click that emailed
 * nobody must say so — "Done" over a skipped owner reads as delivered. A
 * `skipped` row carries the server's reason, so an owner held back by the
 * manual cooldown reads "Not sent to Oleg (notified 4 minutes ago)".
 */
export function notifyOwnersResultSummary(rows: readonly AlertOwnerNotification[]): string {
  if (rows.length === 0) return 'No owners to notify: none is a project member with an email.'
  const sent = rows.filter(row => row.status === 'sent').map(ownerNotificationWho)
  const sending = rows.filter(row => row.status === 'pending').map(ownerNotificationWho)
  const notSent = rows.filter(row => row.status === 'failed' || row.status === 'skipped')
  const parts: string[] = []
  if (sent.length > 0) parts.push(`Emailed ${sent.join(', ')}.`)
  if (sending.length > 0) parts.push(`Still sending to ${sending.join(', ')}.`)
  if (notSent.length > 0) {
    parts.push(
      `Not sent to ${notSent
        .map(row => `${ownerNotificationWho(row)} (${ownerNotificationReason(row)})`)
        .join(', ')}.`,
    )
  }
  return parts.join(' ')
}
