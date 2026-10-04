import { eventNameLabel } from '@/lib/eventName'
import { DEFAULT_ENTITY_COLOR, type EventType, type EventTypeBrief } from '@/types'

/** What one side of an event-composition metric points at. */
export interface EventRef {
  eventId: string
  eventTypeId: string
}

/** One pickable row of the event combobox. */
export interface EventRefOption {
  /** Unique across both groups: `event:<id>` or `type:<id>`. */
  key: string
  group: 'events' | 'types'
  ref: EventRef
  /** The row's main text. */
  label: string
  /** The muted chip beside an event's name; null on an event-type row. */
  typeName: string | null
  /** The event type's colour, for the dot. */
  color: string
}

type TypeLookup = ReadonlyMap<string, Pick<EventTypeBrief, 'display_name' | 'color'>>

/** An event as the roster (no nested type) or the detail endpoint (nested type) returns it. */
interface EventLike {
  id: string
  name: string
  event_type_id?: string | null
  event_type?: EventTypeBrief | null
}

export function typeLookup(eventTypes: readonly EventType[]): TypeLookup {
  return new Map(eventTypes.map(type => [type.id, type]))
}

function typeOf(event: EventLike, types: TypeLookup) {
  return (event.event_type_id ? types.get(event.event_type_id) : undefined) ?? event.event_type ?? null
}

export function eventOption(event: EventLike, types: TypeLookup): EventRefOption {
  const type = typeOf(event, types)
  return {
    key: `event:${event.id}`,
    group: 'events',
    ref: { eventId: event.id, eventTypeId: '' },
    label: eventNameLabel(event.name),
    typeName: type?.display_name ?? null,
    color: type?.color || DEFAULT_ENTITY_COLOR,
  }
}

export function typeOption(type: Pick<EventType, 'id' | 'display_name' | 'color'>): EventRefOption {
  return {
    key: `type:${type.id}`,
    group: 'types',
    ref: { eventId: '', eventTypeId: type.id },
    label: `Every ${type.display_name} event`,
    typeName: null,
    color: type.color || DEFAULT_ENTITY_COLOR,
  }
}

/**
 * The event-type group for `needle`, matched on display name and slug. A type
 * that is no longer in the project still gets a row while it is the value, so
 * the field never paints a stored reference as unset.
 */
export function typeOptions(
  eventTypes: readonly EventType[],
  needle: string,
  selectedTypeId: string,
): EventRefOption[] {
  const lower = needle.trim().toLowerCase()
  const matching = eventTypes
    .filter(
      type =>
        !lower
        || type.display_name.toLowerCase().includes(lower)
        || type.name.toLowerCase().includes(lower),
    )
    .map(typeOption)
  const known = eventTypes.some(type => type.id === selectedTypeId)
  if (!selectedTypeId || known || lower) return matching
  return [
    {
      ...typeOption({ id: selectedTypeId, display_name: '', color: '' }),
      label: `Every event of type ${selectedTypeId.slice(0, 8)}`,
    },
    ...matching,
  ]
}

/**
 * The field's text while the list is closed: the event's name with its type,
 * so two same-named events in different types read apart.
 */
export function refText(option: EventRefOption | null): string {
  if (!option) return ''
  return option.typeName ? `${option.label} · ${option.typeName}` : option.label
}

/**
 * The search an edit to the closed field starts. The field then shows the
 * pick (`shown`), so the browser hands over `shown` with the keystroke
 * applied: text typed at the end is kept on its own, Backspace or Delete
 * inside the pick starts empty, and anything else (the pick selected and
 * typed over) is taken as typed.
 */
export function freshSearch(shown: string, edited: string): string {
  if (!shown) return edited
  if (edited.startsWith(shown)) return edited.slice(shown.length)
  if (isOneDeletion(shown, edited)) return ''
  return edited
}

function isOneDeletion(shown: string, edited: string): boolean {
  if (edited.length !== shown.length - 1) return false
  let i = 0
  while (i < edited.length && edited[i] === shown[i]) i++
  return edited.slice(i) === shown.slice(i + 1)
}

/** "N more — keep typing" for a capped roster; null when nothing is hidden. */
export function moreText(total: number, shown: number): string | null {
  const hidden = Math.max(0, total - shown)
  return hidden > 0 ? `${hidden} more — keep typing` : null
}
