import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { eventsApi } from '@/api/events'
import { useEventRoster } from '@/hooks/useEventRoster'
import { EVENT_ATTRIBUTE_LABEL } from '@/lib/eventAttributes'
import { eventKey } from '@/lib/queryKeys'
import type { EventType } from '@/types'
import { EvField, EvInput, SelectControl } from './eventFormLayout'
import { disambiguate, type SuccessorCandidate } from './successorLabels'

const NO_TYPES: readonly EventType[] = []

/**
 * "Replaced by" on a deprecated event: a server-side search and the pick.
 *
 * Mounted only while the field is on screen — an event that is not being
 * retired asks nothing of the catalog. Split out of `EventForm`.
 */
export function SuccessorPicker({
  slug,
  branchId,
  eventId,
  value,
  onChange,
  eventTypes = NO_TYPES,
}: {
  slug: string
  branchId: string | null
  /** The event being edited, which cannot replace itself. */
  eventId: string
  value: string
  onChange: (value: string) => void
  /** The project's types, to name each option's type. */
  eventTypes?: readonly EventType[]
}) {
  const [search, setSearch] = useState('')
  // The successor roster, searched SERVER-side for the reason the variables tab
  // states at length: /events returns full list rows, so pulling a
  // whole catalog into a <select> to spare the user typing is the wrong trade,
  // and narrowing a page the server already truncated is the defect itself.
  // What the search did not return is printed rather than hidden — a short
  // list and a complete one are otherwise indistinguishable.
  const { events: roster, hiddenCount } = useEventRoster({ slug, branchId, search, debounceMs: 350 })
  // Same key shape as the detail page's own event query, so the successor is
  // read from cache when it has already been opened.
  const { data: successor } = useQuery({
    queryKey: eventKey(slug, branchId, value),
    queryFn: () => eventsApi.get(slug, value, branchId),
    enabled: !!value,
  })
  const options = useMemo(() => {
    // "name · type": options showed the name alone, so two events of one name
    // under different types could not be told apart. Two namesakes of
    // one type — the duplicate case — share that label too, so those
    // alone also carry their status and the day they were added, and the id's
    // head when even that matches.
    const typeNames = new Map(eventTypes.map(et => [et.id, et.display_name]))
    const base = (item: SuccessorCandidate) => {
      const typeName = item.event_type_id ? typeNames.get(item.event_type_id) : undefined
      return typeName ? `${item.name} · ${typeName}` : item.name
    }
    const items: SuccessorCandidate[] = roster
      // An event cannot replace itself; the server answers 400, but offering it
      // at all invites the trip.
      .filter(item => item.id !== eventId)
    // The current choice is prepended when the search does not hold it, so
    // opening a retired event shows what replaced it rather than a blank select,
    // and a selection survives retyping the search.
    const candidates = !successor || items.some(item => item.id === successor.id)
      ? items
      : [successor, ...items]
    return disambiguate(candidates, base)
  }, [roster, successor, eventId, eventTypes])

  return (
    <EvField
      label={EVENT_ATTRIBUTE_LABEL.superseded_by}
      htmlFor="form-superseded"
      hint="What to send instead. Documentation only: nothing is matched, collected or counted through it."
      last
    >
      <div className="flex flex-col gap-[6px]">
        <EvInput
          type="search"
          width="half"
          placeholder="Search events…"
          aria-label="Search for the replacement event"
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
        <SelectControl id="form-superseded" value={value} onChange={onChange}>
          <option value="">Nothing replaces it</option>
          {options.map(option => (
            <option key={option.id} value={option.id}>{option.name}</option>
          ))}
        </SelectControl>
        {hiddenCount > 0 && (
          <p className="text-caption text-fg-tertiary">
            {hiddenCount} more not shown — narrow the search.
          </p>
        )}
      </div>
    </EvField>
  )
}
