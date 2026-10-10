/**
 * Who answers for an event: one rule for every surface that names an owner,
 * the same one the health score counts (website/docs/use/health-score.md).
 * The event's own owner when it has one; without one, the owners of its event
 * type answer for it, and the surface says that is where they come from.
 * Event-type owners are a fact about main's type, so a branch copy of a type
 * has none, and on a branch an event without its own owner has no owner.
 */
import { pluralize } from '@/lib/plural'
import type { EventTypeOwner } from '@/types'

type OwnerName = Pick<EventTypeOwner, 'user_name' | 'user_email'>

/** One event type's owners out of the project's list, in the order it gives them. */
export function ownersOfType<T extends Pick<EventTypeOwner, 'event_type_id'>>(
  owners: readonly T[],
  eventTypeId: string,
): T[] {
  return owners.filter((owner) => owner.event_type_id === eventTypeId)
}

/** "Audit Owner", "Audit Owner, Jane Doe": each owner by name, else by email. */
export function ownerNames(owners: readonly OwnerName[]): string {
  return owners.map((owner) => owner.user_name || owner.user_email).join(', ')
}

/** Where the names on an event without its own owner come from. */
export function typeOwnerSource(count: number): string {
  return pluralize(count, 'event type owner', 'event type owners')
}

/**
 * The Owner hint of the event form (`plural` for the form that adds many).
 * With no owner picked and an event type that has owners, it says who answers
 * for the event instead, so "No owner" never stands unexplained next to a
 * health score that counts the type's owners.
 */
export function ownerFieldHint(
  ownerId: string,
  typeName: string | null | undefined,
  typeOwners: readonly OwnerName[] | undefined,
  { plural = false }: { plural?: boolean } = {},
): string {
  const hint = plural ? 'Who answers for these events.' : 'Who answers for this event.'
  if (ownerId || !typeOwners || typeOwners.length === 0) return hint
  const type = typeName ? `the ${typeName} event type's owners` : "the event type's owners"
  return `${hint} Without one, ${type} do: ${ownerNames(typeOwners)}.`
}
