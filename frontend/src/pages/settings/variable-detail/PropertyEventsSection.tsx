import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ListFilter } from 'lucide-react'
import { toast } from 'sonner'
import { eventsApi } from '@/api/events'
import {
  PROPERTY_BULK_EVENT_LIMIT,
  propertyEntriesApi,
  type PropertyEntriesBulkPatch,
  type PropertyEventEntry,
} from '@/api/propertyEntries'
import { ChipListInput } from '@/components/chip-list-input'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { CodeToken } from '@/components/primitives/code-token'
import { Panel } from '@/components/settings/kit'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useBranchLinkProps } from '@/hooks/useBranch'
import { useConfirm } from '@/hooks/useConfirm'
import { useDebouncedValue } from '@/hooks/useDebouncedValue'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { eventNameLabel } from '@/lib/eventName'
import { currentOrgSlug, projectPath } from '@/lib/navigation'
import { countOf } from '@/lib/plural'
import { formatPresence, formatThreshold, invalidatePropertyEntries } from '@/lib/propertyEntries'
import { eventsPickerKey, propertyEventsKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type { Variable } from '@/types'
import { TYPE_LABELS } from '../variablesShared'
import { invalidValuesFor, valueRuleFor } from '../variableValueValidation'
import { eventsWithPropertyPath } from './variableDetailPath'

const ADD_PICKER_PAGE_SIZE = 50

function PresenceCell({ entry }: { entry: PropertyEventEntry }) {
  const hint =
    entry.suggested_required === null
      ? 'No scan has measured this yet.'
      : `${entry.suggested_required ? 'At or above' : 'Below'} the event's required threshold of ${formatThreshold(entry.required_presence_threshold)}.`
  return (
    <span className="tabular-nums" title={hint}>
      {formatPresence(entry.presence_rate)}
      {entry.suggested_required === true && !entry.required ? (
        <span className="ml-1.5 text-caption text-fg-tertiary">· looks required</span>
      ) : null}
      {entry.required && entry.suggested_required === false ? (
        <span className="ml-1.5 text-caption text-warning">· below {formatThreshold(entry.required_presence_threshold)}</span>
      ) : null}
    </span>
  )
}

/**
 * The events a property is on (F23): the catalog's answer to "where is this
 * property used, and how", with each event's entry — required, allowed values,
 * presence — and edits across many events at once.
 */
export function PropertyEventsSection({
  slug,
  branchId,
  variable,
  canWrite,
}: {
  slug: string
  branchId: string | null
  variable: Variable
  canWrite: boolean
}) {
  const qc = useQueryClient()
  const branchLink = useBranchLinkProps()
  const { confirm, dialog } = useConfirm()
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const [valuesDraft, setValuesDraft] = useState<string[] | null>(null)
  const valueRule = valueRuleFor(variable.variable_type)

  const query = useQuery({
    queryKey: propertyEventsKey(slug, branchId, variable.id),
    queryFn: () => propertyEntriesApi.forProperty(slug, variable.id, branchId),
  })
  const entries = useMemo(() => query.data ?? [], [query.data])
  // What is selected and still listed: a row taken off by another tab drops out.
  const selectedIds = useMemo(
    () => entries.filter(entry => selected.has(entry.event_id)).map(entry => entry.event_id),
    [entries, selected],
  )
  const requiredCount = entries.filter(entry => entry.required).length
  const overrideCount = entries.filter(entry => entry.values !== null).length

  const done = () => invalidatePropertyEntries(qc, slug, branchId)

  const bulkMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: ({ eventIds, patch }: { eventIds: string[]; patch: PropertyEntriesBulkPatch }) =>
      propertyEntriesApi.bulkSet(slug, variable.id, eventIds, patch, branchId),
    onSuccess: (result, { eventIds }) => {
      done()
      setValuesDraft(null)
      toast.success(
        result.created > 0
          ? `Added ${variable.name} to ${countOf(result.created, 'event', 'events')}`
          : `Updated ${variable.name} on ${countOf(eventIds.length, 'event', 'events')}`,
      )
    },
  })
  const removeMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (eventIds: string[]) => propertyEntriesApi.bulkRemove(slug, variable.id, eventIds, branchId),
    onSuccess: (result) => {
      done()
      setSelected(new Set())
      toast.success(`Took ${variable.name} off ${countOf(result.removed, 'event', 'events')}`)
    },
  })
  const pending = bulkMut.isPending || removeMut.isPending
  const mutationError = [bulkMut, removeMut].find(m => m.isError)?.error

  const toggle = (eventId: string, on: boolean) =>
    setSelected(prev => {
      const next = new Set(prev)
      if (on) next.add(eventId)
      else next.delete(eventId)
      return next
    })
  const allSelected = entries.length > 0 && selectedIds.length === entries.length
  const someSelected = selectedIds.length > 0 && !allSelected

  const handleRemove = async () => {
    const ok = await confirm({
      title: 'Remove from events',
      message: `Take ${variable.name} off ${countOf(selectedIds.length, 'event', 'events')}? Their required flags and per-event values go with it.`,
      confirmLabel: 'Remove',
      variant: 'danger',
    })
    if (ok) removeMut.mutate(selectedIds)
  }

  const invalidDraftValues = valuesDraft ? invalidValuesFor(variable.variable_type, valuesDraft) : []

  if (query.isError && !query.data) {
    return (
      <ErrorState
        compact
        title="Couldn't load the events this property is on"
        error={query.error}
        onRetry={() => { void query.refetch() }}
      />
    )
  }

  return (
    <div className="grid gap-3">
      {dialog}
      <Panel
        title="Events"
        subtitle={
          query.isPending
            ? 'Loading…'
            : `On ${countOf(entries.length, 'event', 'events')} · ${requiredCount} required · ${countOf(overrideCount, 'override', 'overrides')}`
        }
        right={
          entries.length > 0 ? (
            <Button asChild variant="outline" size="sm">
              <Link {...branchLink(eventsWithPropertyPath(slug, variable.name), branchId)}>
                <ListFilter className="size-3.5" aria-hidden="true" />
                Show in the events list
              </Link>
            </Button>
          ) : undefined
        }
      >
        {canWrite && selectedIds.length > 0 && (
          <div
            role="toolbar"
            aria-label="Edit the selected events"
            className="flex flex-wrap items-center gap-2 border-b bg-accent-soft/40 px-4 py-2"
          >
            <span className="text-body-sm font-medium" aria-live="polite">
              {countOf(selectedIds.length, 'event', 'events')} selected
            </span>
            <Button size="sm" variant="outline" disabled={pending} onClick={() => bulkMut.mutate({ eventIds: selectedIds, patch: { required: true } })}>
              Mark required
            </Button>
            <Button size="sm" variant="outline" disabled={pending} onClick={() => bulkMut.mutate({ eventIds: selectedIds, patch: { required: false } })}>
              Mark optional
            </Button>
            <Button size="sm" variant="outline" disabled={pending} onClick={() => setValuesDraft(valuesDraft ? null : [...variable.allowed_values])}>
              Set allowed values…
            </Button>
            <Button size="sm" variant="outline" disabled={pending} onClick={() => bulkMut.mutate({ eventIds: selectedIds, patch: { values: null } })}>
              Use documented values
            </Button>
            <Button size="sm" variant="ghost" className="text-destructive" disabled={pending} onClick={() => { void handleRemove() }}>
              Remove from events
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>
              Clear selection
            </Button>
          </div>
        )}
        {canWrite && valuesDraft && selectedIds.length > 0 && (
          <div className="grid gap-2 border-b px-4 py-3">
            <p className="text-caption text-fg-tertiary">
              These values replace the documented list on the {countOf(selectedIds.length, 'selected event', 'selected events')}.
            </p>
            <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start">
              <ChipListInput
                values={valuesDraft}
                onChange={setValuesDraft}
                placeholder="Type a value, press Enter"
                ariaLabel="Add allowed value for the selected events"
                {...valueRule}
              />
              <div className="flex gap-2">
                <Button
                  size="sm"
                  disabled={pending}
                  onClick={() => bulkMut.mutate({ eventIds: selectedIds, patch: { values: valuesDraft } })}
                >
                  Apply to {countOf(selectedIds.length, 'event', 'events')}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setValuesDraft(null)}>Cancel</Button>
              </div>
            </div>
            {invalidDraftValues.length > 0 && (
              <p className="text-caption text-warning">
                Not valid for {TYPE_LABELS[variable.variable_type]}: {invalidDraftValues.join(', ')}.
              </p>
            )}
          </div>
        )}
        {query.isPending ? null : entries.length === 0 ? (
          <div className="p-4">
            <EmptyState
              size="sm"
              title="On no event's property list"
              description={
                canWrite
                  ? 'Add it to the events that carry it below, or accept a new-property drift after a scan.'
                  : 'No event lists this property yet.'
              }
            />
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                {canWrite && (
                  <TableHead className="w-8">
                    <Checkbox
                      aria-label="Select every event"
                      checked={allSelected ? true : someSelected ? 'indeterminate' : false}
                      onCheckedChange={(checked) =>
                        setSelected(checked === true ? new Set(entries.map(e => e.event_id)) : new Set())
                      }
                    />
                  </TableHead>
                )}
                <TableHead>Event</TableHead>
                <TableHead>Required</TableHead>
                <TableHead>Allowed values</TableHead>
                <TableHead className="text-right">Presence</TableHead>
                <TableHead className="text-right">Threshold</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {entries.map(entry => {
                const label = eventNameLabel(entry.event_name)
                return (
                  <TableRow key={entry.event_id} data-state={selected.has(entry.event_id) ? 'selected' : undefined}>
                    {canWrite && (
                      <TableCell>
                        <Checkbox
                          aria-label={`Select ${label}`}
                          checked={selected.has(entry.event_id)}
                          onCheckedChange={(checked) => toggle(entry.event_id, checked === true)}
                        />
                      </TableCell>
                    )}
                    <TableCell className="font-medium">
                      <Link
                        className="hover:underline"
                        {...branchLink(projectPath(currentOrgSlug(), slug, `/events/all/${entry.event_id}`), branchId)}
                      >
                        {label}
                      </Link>
                      {entry.status !== 'active' && (
                        <span className="ml-1.5 text-caption text-fg-tertiary">{entry.status.replace('_', ' ')}</span>
                      )}
                    </TableCell>
                    <TableCell>
                      {canWrite ? (
                        <Switch
                          aria-label={`${variable.name} is required on ${label}`}
                          checked={entry.required}
                          disabled={pending}
                          onCheckedChange={(checked) =>
                            bulkMut.mutate({ eventIds: [entry.event_id], patch: { required: checked } })
                          }
                        />
                      ) : (
                        entry.required ? 'Required' : 'Optional'
                      )}
                    </TableCell>
                    <TableCell>
                      {entry.values === null ? (
                        <span className="text-caption text-fg-tertiary">Documented list</span>
                      ) : entry.values.length === 0 ? (
                        <span className="text-caption text-fg-tertiary">No values allowed</span>
                      ) : (
                        <span className="flex flex-wrap gap-1">
                          {entry.values.map(value => <CodeToken key={value} title={value}>{value}</CodeToken>)}
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="text-right"><PresenceCell entry={entry} /></TableCell>
                    <TableCell className="text-right tabular-nums text-fg-muted">
                      {formatThreshold(entry.required_presence_threshold)}
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}
        {mutationError !== undefined && (
          <p role="alert" className="px-4 py-2 text-body text-destructive">{getErrorMessage(mutationError)}</p>
        )}
      </Panel>
      {canWrite && (
        <AddToEventsPanel
          slug={slug}
          branchId={branchId}
          variable={variable}
          listed={entries}
          pending={pending}
          onAdd={(eventIds, required) => bulkMut.mutate({ eventIds, patch: required ? { required: true } : {} })}
        />
      )}
    </div>
  )
}

/** Search events and add the property to the picked ones in one request. */
function AddToEventsPanel({
  slug,
  branchId,
  variable,
  listed,
  pending,
  onAdd,
}: {
  slug: string
  branchId: string | null
  variable: Variable
  listed: PropertyEventEntry[]
  pending: boolean
  onAdd: (eventIds: string[], required: boolean) => void
}) {
  const [search, setSearch] = useState('')
  const [picked, setPicked] = useState<Set<string>>(() => new Set())
  const [required, setRequired] = useState(false)
  const [active, setActive] = useState(false)
  const debounced = useDebouncedValue(search)
  const { data } = useQuery({
    queryKey: eventsPickerKey(slug, branchId, 'property-picker', debounced),
    queryFn: () => eventsApi.list(slug, { search: debounced || undefined, limit: ADD_PICKER_PAGE_SIZE, offset: 0 }, branchId),
    enabled: active,
    placeholderData: keepPreviousData,
  })
  const onList = useMemo(() => new Set(listed.map(entry => entry.event_id)), [listed])
  const candidates = (data?.items ?? []).filter(event => !onList.has(event.id))
  const hidden = Math.max(0, (data?.total ?? 0) - (data?.items.length ?? 0))
  const pickedIds = [...picked].slice(0, PROPERTY_BULK_EVENT_LIMIT)

  return (
    <Panel
      title="Add to events"
      subtitle={`Put ${variable.name} on the property list of more events.`}
      bodyClassName="grid gap-2 p-4"
    >
      <Input
        aria-label="Search events to add the property to"
        placeholder="Search events…"
        className="h-8"
        value={search}
        onFocus={() => setActive(true)}
        onChange={e => setSearch(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter') e.preventDefault() }}
      />
      {active && (
        <ul className="grid max-h-64 gap-0.5 overflow-y-auto" aria-label="Events to add the property to">
          {candidates.length === 0 && (
            <li className="text-caption text-fg-tertiary">No event without this property matches.</li>
          )}
          {candidates.map(event => (
            <li key={event.id}>
              <label className="flex items-center gap-2 rounded-sm px-1 py-1 text-body-sm hover:bg-muted">
                <Checkbox
                  checked={picked.has(event.id)}
                  onCheckedChange={(checked) =>
                    setPicked(prev => {
                      const next = new Set(prev)
                      if (checked === true) next.add(event.id)
                      else next.delete(event.id)
                      return next
                    })
                  }
                />
                {eventNameLabel(event.name)}
              </label>
            </li>
          ))}
        </ul>
      )}
      {active && hidden > 0 && (
        <p className="text-caption text-fg-tertiary">{hidden} more not listed — search to narrow.</p>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-body-sm">
          <Switch checked={required} onCheckedChange={setRequired} aria-label="Add as required" />
          Required
        </label>
        <Button
          size="sm"
          disabled={pickedIds.length === 0 || pending}
          onClick={() => {
            onAdd(pickedIds, required)
            setPicked(new Set())
          }}
        >
          Add to {countOf(pickedIds.length, 'event', 'events')}
        </Button>
      </div>
    </Panel>
  )
}
