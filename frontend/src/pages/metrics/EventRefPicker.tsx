import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Check, ChevronDown, X } from 'lucide-react'
import { eventsApi } from '@/api/events'
import { INPUT_BASE, INPUT_CLASS, INPUT_DISABLED } from '@/components/settings/input-style'
import { AnchoredListbox } from '@/components/ui/anchored-listbox'
import { useDebouncedValue } from '@/hooks/useDebouncedValue'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { DEFAULT_ENTITY_COLOR, type EventType } from '@/types'
import { eventKey } from '@/lib/queryKeys'
import { eventRosterQuery } from './eventRoster'
import {
  eventOption,
  moreText,
  freshSearch,
  refText,
  typeLookup,
  typeOptions,
  type EventRef,
  type EventRefOption,
} from './eventRefOptions'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

export type { EventRef }

interface EventRefPickerProps {
  slug: string
  id: string
  /** Names the option list and the clear button ("Clear <label>"). */
  label: string
  value: EventRef
  onChange: (next: EventRef) => void
  eventTypes: readonly EventType[]
  disabled?: boolean
  'aria-invalid'?: boolean
  'aria-describedby'?: string
}

/**
 * Pick the event — or the whole event type — one side of an event-composition
 * metric counts.
 *
 * The native select this replaces was fed `eventsApi.list(slug)` with no
 * limit, i.e. the endpoint's default first 200 events: anything later could
 * not be picked, and editing a metric already on one painted "Select event…"
 * as if it were unset. The roster is now searched on the server, and
 * the selected event is fetched by id and always offered.
 *
 * Event-type references are valid on the backend and preserved by the form,
 * but the old select had no option for them, so such a metric also read
 * "Select event…" while validation passed on hidden state, and nothing could
 * clear or change the type. They are a second option group here.
 *
 * One combobox, not a search box stacked over a select: the pair read as two
 * fields, the select could not show whose type an event was beyond a text
 * suffix, and a capped roster said so only in a caption under both.
 * Typing filters; each row carries its type's dot and a muted type chip.
 */
export function EventRefPicker({
  slug,
  id,
  label,
  value,
  onChange,
  eventTypes,
  disabled,
  'aria-invalid': ariaInvalid,
  'aria-describedby': ariaDescribedBy,
}: EventRefPickerProps) {
  const uid = useId()
  const listboxId = `event-ref-listbox-${uid}`
  const wrapperRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [highlight, setHighlight] = useState(0)
  const debouncedSearch = useDebouncedValue(search.trim())

  const types = useMemo(() => typeLookup(eventTypes), [eventTypes])
  const rosterQuery = useQuery(eventRosterQuery(slug, debouncedSearch))
  const roster = useMemo(() => rosterQuery.data?.items ?? [], [rosterQuery.data])
  const selectedInRoster = !!value.eventId && roster.some(event => event.id === value.eventId)
  // Same key shape as the event detail page, so an opened event is read from cache.
  const selectedEventQuery = useQuery({
    queryKey: eventKey(slug, null, value.eventId),
    queryFn: () => eventsApi.get(slug, value.eventId),
    enabled: !!value.eventId && !selectedInRoster,
    meta: SILENT_ERROR_META,
  })

  const selected = useMemo((): EventRefOption | null => {
    if (value.eventId) {
      const inRoster = roster.find(event => event.id === value.eventId)
      if (inRoster) return eventOption(inRoster, types)
      if (selectedEventQuery.data) return eventOption(selectedEventQuery.data, types)
      return {
        key: `event:${value.eventId}`,
        group: 'events',
        ref: { eventId: value.eventId, eventTypeId: '' },
        label: selectedEventQuery.isError
          ? `Unknown event (${value.eventId.slice(0, 8)})`
          : 'Loading selected event…',
        typeName: null,
        color: DEFAULT_ENTITY_COLOR,
      }
    }
    if (value.eventTypeId) {
      return typeOptions(eventTypes, '', value.eventTypeId)
        .find(option => option.ref.eventTypeId === value.eventTypeId) ?? null
    }
    return null
  }, [value, roster, types, eventTypes, selectedEventQuery.data, selectedEventQuery.isError])

  const needle = search.trim()
  const eventRows = useMemo(() => {
    const rows = roster.map(event => eventOption(event, types))
    // The stored event outside the current page is still offered while
    // nothing is typed, so it can be seen and re-picked.
    if (selected?.group === 'events' && !selectedInRoster && !needle) return [selected, ...rows]
    return rows
  }, [roster, types, selected, selectedInRoster, needle])
  // Local and small: filtered on every keystroke, not debounced.
  const typeRows = useMemo(
    () => typeOptions(eventTypes, needle, value.eventTypeId),
    [eventTypes, needle, value.eventTypeId],
  )
  const rows = useMemo(() => [...eventRows, ...typeRows], [eventRows, typeRows])
  const more = moreText(rosterQuery.data?.total ?? 0, roster.length)

  const expanded = open && !disabled
  // The highlight can outlive a shrinking list; clamp rather than reset.
  const activeIdx = Math.min(highlight, rows.length - 1)
  const optionId = (i: number) => `${listboxId}-opt-${i}`

  // Keep the highlighted row in view as the arrow keys move it.
  // Optional call — jsdom has no scrollIntoView.
  useEffect(() => {
    if (!expanded || activeIdx < 0) return
    document.getElementById(`${listboxId}-opt-${activeIdx}`)?.scrollIntoView?.({ block: 'nearest' })
  }, [expanded, activeIdx, listboxId])

  // A project with no events at all: an empty list is a dead end, so the
  // picker says so and links to where events come from.
  const noEvents =
    rosterQuery.isSuccess && !debouncedSearch && roster.length === 0 && !value.eventId

  const close = () => {
    setOpen(false)
    setSearch('')
    setHighlight(0)
  }

  const pick = (option: EventRefOption) => {
    onChange(option.ref)
    close()
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    // The field sits inside the metric <form>: Enter would otherwise submit
    // the whole metric.
    if (e.key === 'Enter') e.preventDefault()
    if (!expanded) {
      if (e.key === 'ArrowDown' || e.key === 'Enter') {
        e.preventDefault()
        setOpen(true)
        setHighlight(0)
      }
      return
    }
    if (e.key === 'Enter') {
      const choice = rows[activeIdx]
      if (choice) pick(choice)
    } else if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHighlight(Math.min(activeIdx + 1, rows.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlight(Math.max(activeIdx - 1, 0))
    } else if (e.key === 'Escape') {
      e.preventDefault()
      close()
    } else if (e.key === 'Tab') {
      close()
    }
  }

  const renderRow = (option: EventRefOption, i: number) => {
    const current = option.key === selected?.key
    return (
      <button
        key={option.key}
        id={optionId(i)}
        type="button"
        role="option"
        tabIndex={-1}
        aria-selected={i === activeIdx}
        aria-current={current || undefined}
        onMouseDown={e => e.preventDefault()}
        onClick={() => pick(option)}
        onMouseEnter={() => setHighlight(i)}
        className="flex w-full items-center gap-2 rounded-control px-2 py-[5px] text-left text-body-sm"
        style={{
          background: i === activeIdx ? 'var(--surface-hover)' : 'transparent',
          color: 'var(--fg)',
        }}
      >
        <span
          aria-hidden="true"
          data-testid="event-type-dot"
          className="inline-block size-2 shrink-0 rounded-full"
          style={{ backgroundColor: option.color }}
        />
        <span className="min-w-0 flex-1 truncate">{option.label}</span>
        {option.typeName && (
          <span className="max-w-[45%] shrink-0 truncate rounded-sm bg-surface-hover px-1.5 text-caption text-fg-tertiary">
            {option.typeName}
          </span>
        )}
        <Check
          aria-hidden="true"
          className="size-3.5 shrink-0"
          style={{ visibility: current ? 'visible' : 'hidden' }}
        />
      </button>
    )
  }

  const groupHead = 'px-2 pb-0.5 pt-1.5 text-micro font-medium uppercase tracking-wide text-fg-tertiary'
  const statusRow = 'px-2 py-[5px] text-caption text-fg-tertiary'

  return (
    // 280px like every other select on the form.
    <div className="flex flex-col gap-1.5" style={{ maxWidth: 280 }}>
      <div ref={wrapperRef} className="relative">
        <input
          ref={inputRef}
          id={id}
          type="text"
          role="combobox"
          aria-expanded={expanded}
          aria-haspopup="listbox"
          aria-autocomplete="list"
          aria-controls={listboxId}
          aria-activedescendant={expanded && activeIdx >= 0 ? optionId(activeIdx) : undefined}
          aria-required
          aria-invalid={ariaInvalid || undefined}
          aria-describedby={ariaDescribedBy}
          autoComplete="off"
          // Closed, the field shows the pick; open, it is the search, with the
          // pick as its placeholder so what is being replaced stays in view.
          value={expanded ? search : refText(selected)}
          placeholder={expanded ? refText(selected) || 'Search events…' : 'Select event…'}
          disabled={disabled}
          onFocus={() => setOpen(true)}
          // A pick closes the list but keeps focus here; a click reopens it.
          onClick={() => setOpen(true)}
          onChange={e => {
            // Closed with focus kept (after a pick or Escape), the field holds
            // the pick's display text; an edit then starts a fresh search from
            // what was just typed rather than "name · Type" plus a character.
            setSearch(expanded ? e.target.value : freshSearch(refText(selected), e.target.value))
            setOpen(true)
            setHighlight(0)
          }}
          onKeyDown={handleKeyDown}
          className={INPUT_CLASS}
          style={{ ...INPUT_BASE, paddingRight: 30, ...(disabled ? INPUT_DISABLED : {}) }}
        />
        {selected && !disabled ? (
          <button
            type="button"
            aria-label={`Clear ${label}`}
            onMouseDown={e => e.preventDefault()}
            onClick={() => {
              onChange({ eventId: '', eventTypeId: '' })
              close()
            }}
            className="absolute right-1.5 top-1/2 flex size-5 -translate-y-1/2 items-center justify-center rounded-sm text-fg-tertiary hover:text-fg"
          >
            <X aria-hidden="true" className="size-3.5" />
          </button>
        ) : (
          <ChevronDown
            aria-hidden="true"
            className="pointer-events-none absolute right-2 top-1/2 size-3.5 -translate-y-1/2 text-fg-tertiary"
          />
        )}
      </div>
      <AnchoredListbox
        id={listboxId}
        open={expanded}
        anchorRef={wrapperRef}
        onDismiss={close}
        ariaLabel={`${label[0]?.toUpperCase() ?? ''}${label.slice(1)}`}
        className="max-h-[280px] rounded-control bg-surface border-border"
      >
        {eventRows.length > 0 && (
          <div role="group" aria-label="Events">
            <div aria-hidden="true" className={groupHead}>
              Events
            </div>
            {eventRows.map((option, i) => renderRow(option, i))}
          </div>
        )}
        {typeRows.length > 0 && (
          <div role="group" aria-label="All events of a type">
            <div aria-hidden="true" className={groupHead}>
              All events of a type
            </div>
            {typeRows.map((option, i) => renderRow(option, eventRows.length + i))}
          </div>
        )}
        {/* Status lines are disabled options: a listbox may only own options
            and groups, and these must still be read out. */}
        {rows.length === 0 && (
          <div role="option" aria-disabled="true" aria-selected={false} className={statusRow}>
            {rosterQuery.isPending ? 'Loading events…' : 'No events match'}
          </div>
        )}
        {more && (
          <div role="option" aria-disabled="true" aria-selected={false} className={statusRow}>
            {more}
          </div>
        )}
      </AnchoredListbox>
      {noEvents && (
        <p className="text-caption text-fg-tertiary">
          No events in this project yet.{' '}
          <Link to={projectPath(currentOrgSlug(), slug, '/events')} className="underline underline-offset-2 text-fg">
            Add events
          </Link>
        </p>
      )}
      {rosterQuery.isError && (
        <p role="alert" className="text-caption text-danger">
          Could not load events.{' '}
          <button
            type="button"
            onClick={() => void rosterQuery.refetch()}
            className="underline underline-offset-2"
          >
            Retry
          </button>
        </p>
      )}
    </div>
  )
}
