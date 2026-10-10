import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Pencil, Trash2 } from 'lucide-react'
import { type VariableEventOverride, variableOverridesApi } from '@/api/variableOverrides'
import { ChipListInput } from '@/components/chip-list-input'
import { CodeToken } from '@/components/primitives/code-token'
import { NativeSelect } from '@/components/settings/kit'
import { Button } from '@/components/ui/button'
import { IconButton } from '@/components/ui/icon-button'
import { Input } from '@/components/ui/input'
import { useConfirm } from '@/hooks/useConfirm'
import { useEventRoster } from '@/hooks/useEventRoster'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { eventNameLabel } from '@/lib/eventName'
import { countOf } from '@/lib/plural'
import { variableOverridesKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type { Variable, VariableType } from '@/types'
import { TYPE_LABELS } from '../variablesShared'
import { invalidValuesFor, valueRuleFor } from '../variableValueValidation'

/** A property entry that carries its own allowed values. */
type Override = VariableEventOverride & { values: string[] }

/**
 * Per-event value overrides of ONE variable: the list, and the picker that
 * adds or edits one. Save override applies at once.
 *
 * `variableType` checks the override values: the dialog passes the type being
 * edited, so a type change warns about the values it would strand; the page
 * passes the saved type. `note` is the second sentence of the intro, which
 * differs by where the section sits.
 */
export function VariableOverridesSection({
  slug,
  branchId,
  variable,
  variableType,
  canWrite,
  note,
}: {
  slug: string
  branchId: string | null
  variable: Variable
  variableType: VariableType
  canWrite: boolean
  note: string
}) {
  const qc = useQueryClient()
  const { confirm, dialog } = useConfirm()
  // The picked event, NOT a bare id. The roster is one searched page of a
  // catalog that can run to thousands, so an id alone is not enough to render
  // the selection: Edit on an override whose event sits outside the page set an
  // id no <option> carried and the select painted BLANK while Save stayed
  // enabled. Carrying the name the event was picked under — from
  // the override row, or from the roster option — means the picker can always
  // show what is selected, whatever the search is currently narrowed to. The
  // name is stored RAW; eventNameLabel is applied where it is painted, so a
  // blank-named event still reads "(unnamed event)".
  const [overrideEvent, setOverrideEvent] = useState<{ id: string; name: string } | null>(null)
  const [overrideEventSearch, setOverrideEventSearch] = useState('')
  const [overrideValues, setOverrideValues] = useState<string[]>([])
  // The roster is only fetched once someone reaches for the override picker.
  // Opening a variable to fix a description used to pull 100 full event rows
  // first.
  const [pickerActive, setPickerActive] = useState(false)
  const valueRule = valueRuleFor(variableType)

  const { data: entries = [] } = useQuery({
    queryKey: variableOverridesKey(slug, branchId, variable.id),
    queryFn: () => variableOverridesApi.list(slug, variable.id, branchId),
  })
  // The endpoint lists every property entry for this variable (F23); only the
  // ones with their own values are overrides. The rest use the documented list.
  const overrides = useMemo(
    () => entries.filter((entry): entry is Override => entry.values !== null),
    [entries],
  )
  const invalidEditedOverrideValues = invalidValuesFor(variableType, overrideValues)

  // Searched SERVER-side, like every event picker (`useEventRoster`): the
  // backend matches name, description and source_name with an ILIKE, so any
  // event in the catalog is reachable by typing part of its name. Narrowing
  // here instead would only re-filter the page the server already truncated,
  // which is the defect. `hiddenEventCount` is what the search did not return:
  // the variables table prints exactly this note for its own truncation, so an
  // operator can tell a short list from a complete one.
  const { events: rosterEvents, hiddenCount: hiddenEventCount } = useEventRoster({
    slug,
    branchId,
    search: overrideEventSearch,
    enabled: pickerActive && canWrite,
  })
  // The selected event is prepended when the search does not hold it, so Edit on
  // an out-of-roster override shows that event rather than a blank select — and
  // a selection survives retyping the search.
  const pickerEvents = useMemo<{ id: string; name: string }[]>(() => {
    const roster = rosterEvents.map(event => ({ id: event.id, name: event.name }))
    if (!overrideEvent || roster.some(event => event.id === overrideEvent.id)) return roster
    return [overrideEvent, ...roster]
  }, [overrideEvent, rosterEvents])

  const overrideUpsertMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: ({ eventId, values }: { eventId: string; values: string[] }) =>
      variableOverridesApi.upsert(slug, variable.id, eventId, values, branchId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: variableOverridesKey(slug, branchId, variable.id) })
      setOverrideEvent(null); setOverrideValues([])
    },
  })

  const overrideDeleteMut = useMutation({
    meta: SILENT_ERROR_META,
    // The row is also the event's property entry (F23): only the override
    // goes, and the property stays on the event's list.
    mutationFn: (eventId: string) =>
      variableOverridesApi.clearValues(slug, variable.id, eventId, branchId),
    onSuccess: () => qc.invalidateQueries({ queryKey: variableOverridesKey(slug, branchId, variable.id) }),
  })

  // An override can hold many hand-curated values and has no undo, and the
  // trash icon deleted it on one click, pending or not.
  const handleOverrideDelete = async (override: Override) => {
    const ok = await confirm({
      title: 'Delete override',
      message: `Delete the override for ${eventNameLabel(override.event_name)}? Its ${countOf(override.values.length, 'value', 'values')} go with it, and the event falls back to the documented list.`,
      confirmLabel: 'Delete',
      variant: 'danger',
    })
    if (ok) overrideDeleteMut.mutate(override.event_id)
  }

  const overrideError = [overrideUpsertMut, overrideDeleteMut].find(mutation => mutation.isError)?.error

  return (
    <div className="rounded-md border bg-muted/30 p-3">
      {dialog}
      <div className="mb-1 text-body-sm font-semibold uppercase tracking-wide text-fg-tertiary">
        Per-event value overrides
      </div>
      <p className="mb-2 text-caption text-fg-tertiary">
        An override replaces the documented list for that specific event.
        {canWrite ? ` ${note}` : null}
      </p>
      {overrides.length === 0 && !canWrite && (
        <p className="text-body-sm text-fg-tertiary">No overrides: every event uses the documented list.</p>
      )}
      {overrides.length > 0 && (
        <ul className="mb-2 space-y-1">
          {overrides.map(override => (
            <li key={override.id} className="flex items-start justify-between gap-2 rounded-sm border bg-background px-2 py-1.5">
              <div className="min-w-0">
                <div className="text-body-sm font-medium">{eventNameLabel(override.event_name)}</div>
                <div className="mt-0.5 flex flex-wrap gap-1">
                  {override.values.map(value => (
                    <CodeToken key={value} title={value}>{value}</CodeToken>
                  ))}
                </div>
              </div>
              {canWrite && (
                <div className="flex shrink-0 gap-1">
                  {/* Without the placeholder these read "Edit override for " and
                      "Delete override for " — a trailing space and nothing else,
                      the same defect EventRow fixed on the events list.

                      Edit hands the picker the event NAME as well as the id,
                      both straight off this override row. The event is often
                      absent from the roster page below — an override outlives
                      whatever the picker is searched to — and a bare id left
                      the select blank with Save still enabled. */}
                  <IconButton
                    type="button"
                    variant="ghost"
                    className="h-6 w-6"
                    label={`Edit override for ${eventNameLabel(override.event_name)}`}
                    onClick={() => {
                      setPickerActive(true)
                      setOverrideEvent({ id: override.event_id, name: override.event_name })
                      setOverrideValues(override.values)
                    }}
                  >
                    <Pencil className="h-3 w-3" aria-hidden="true" />
                  </IconButton>
                  <IconButton
                    type="button"
                    variant="ghost"
                    className="h-6 w-6 text-fg-tertiary hover:text-destructive"
                    label={`Delete override for ${eventNameLabel(override.event_name)}`}
                    disabled={overrideDeleteMut.isPending}
                    onClick={() => { void handleOverrideDelete(override) }}
                  >
                    <Trash2 className="h-3 w-3" aria-hidden="true" />
                  </IconButton>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      {canWrite && (
        <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)_auto] sm:items-start">
          {/* Focus anywhere in the picker is what fetches its roster. */}
          <div className="grid gap-1" onFocus={() => setPickerActive(true)}>
            <Input
              aria-label="Search events"
              className="h-8 text-body"
              placeholder="Search events…"
              value={overrideEventSearch}
              onChange={e => setOverrideEventSearch(e.target.value)}
              // Enter is the universal gesture in a search field, and in the
              // edit dialog this one used to sit inside the definition's
              // <form>, one `type="submit"` Save away from HTML's implicit
              // submission: pressing it PATCHed the variable and closed the
              // dialog, destroying the override being written.
              // Kept as a guard wherever the section is mounted. Nothing runs
              // in its place: the search is debounced and applies as you type.
              onKeyDown={e => { if (e.key === 'Enter') e.preventDefault() }}
            />
            {/* A native <option> takes its accessible name from its text
                content, so a blank-named event was a selectable row with no
                name at all — indistinguishable from a rendering glitch in the
                list, and announced as nothing. */}
            <NativeSelect
              aria-label="Override event"
              value={overrideEvent?.id ?? ''}
              onChange={value => {
                const picked = pickerEvents.find(event => event.id === value)
                setOverrideEvent(picked ?? null)
              }}
              options={[
                { value: '', label: 'Select event…' },
                ...pickerEvents.map(event => ({ value: event.id, label: eventNameLabel(event.name) })),
              ]}
            />
            {hiddenEventCount > 0 && (
              // Say what is missing rather than presenting a truncated roster
              // as the whole catalog — the same note the
              // variables table prints for its own truncation.
              <p className="text-caption text-fg-tertiary">
                {hiddenEventCount} more not listed — search to narrow.
              </p>
            )}
          </div>
          <div className="grid gap-1">
            <ChipListInput values={overrideValues} onChange={setOverrideValues} placeholder="Values for this event" ariaLabel="Add override value" {...valueRule} />
            {/* The chip input checks only NEW chips, so values loaded by Edit
                on an override are checked here. */}
            {invalidEditedOverrideValues.length > 0 && (
              <p className="text-caption text-warning">
                Not valid for {TYPE_LABELS[variableType]}: {invalidEditedOverrideValues.join(', ')}.
              </p>
            )}
          </div>
          <Button type="button" size="sm" disabled={!overrideEvent || overrideUpsertMut.isPending} onClick={() => { if (overrideEvent) overrideUpsertMut.mutate({ eventId: overrideEvent.id, values: overrideValues }) }}>
            Save override
          </Button>
        </div>
      )}
      {overrideError !== undefined && (
        <p role="alert" className="mt-2 text-body text-destructive">{getErrorMessage(overrideError)}</p>
      )}
    </div>
  )
}
