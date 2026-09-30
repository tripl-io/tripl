import { useId, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Pencil, Plus, Trash2 } from 'lucide-react'
import { eventsApi } from '@/api/events'
import { propertyEntriesApi, type EventPropertyEntry, type PropertyEntriesBulkPatch } from '@/api/propertyEntries'
import { ChipListInput } from '@/components/chip-list-input'
import { ErrorState } from '@/components/error-state'
import { Chip } from '@/components/primitives/chip'
import { CodeToken } from '@/components/primitives/code-token'
import { Panel } from '@/components/settings/kit'
import { Button } from '@/components/ui/button'
import { IconButton } from '@/components/ui/icon-button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useConfirm } from '@/hooks/useConfirm'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import {
  DEFAULT_REQUIRED_PRESENCE,
  formatPresence,
  formatThreshold,
  invalidatePropertyEntries,
} from '@/lib/propertyEntries'
import { describeConstraints, schemaMatchesType, summariseType } from '@/lib/propertySchema'
import { eventKey, eventPropertiesKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type { Variable } from '@/types'
import { variableDetailPath } from '@/pages/settings/variable-detail/variableDetailPath'
import { invalidValuesFor, valueRuleFor } from '@/pages/settings/variableValueValidation'

const ADD_SUGGESTION_LIMIT = 8

/** The type cell: the schema in a few words, its constraints on hover. */
function TypeCell({ entry }: { entry: EventPropertyEntry }) {
  const schema = entry.json_schema && schemaMatchesType(entry.variable_type, entry.json_schema) ? entry.json_schema : null
  const constraints = describeConstraints(schema)
  return (
    <span className="text-body-sm text-fg-muted" title={constraints.length ? constraints.join(', ') : undefined}>
      {summariseType(entry.variable_type, entry.json_schema)}
      {constraints.length > 0 && <span className="sr-only">, {constraints.join(', ')}</span>}
    </span>
  )
}

function ValuesCell({ entry }: { entry: EventPropertyEntry }) {
  if (entry.effective_values.length === 0) {
    return (
      <span className="text-caption text-fg-tertiary">
        {entry.values === null ? 'Any value' : 'No values allowed'}
      </span>
    )
  }
  return (
    <span className="flex flex-wrap items-center gap-1">
      {entry.effective_values.map((value) => <CodeToken key={value} title={value}>{value}</CodeToken>)}
      {entry.values !== null && (
        <Chip size="xs" variant="outline" title="This event's own list, replacing the property's documented values.">
          This event
        </Chip>
      )}
    </span>
  )
}

function PresenceCell({ entry, threshold }: { entry: EventPropertyEntry; threshold: number }) {
  const below = entry.required && entry.suggested_required === false
  const looksRequired = !entry.required && entry.suggested_required === true
  return (
    <span
      className="tabular-nums"
      title={
        entry.presence_rate === null
          ? 'No scan has measured this yet.'
          : `Carried by ${formatPresence(entry.presence_rate)} of the event's rows at the last scan; the required threshold is ${formatThreshold(threshold)}.`
      }
    >
      {formatPresence(entry.presence_rate)}
      {below && <span className="ml-1.5 text-caption text-warning">below threshold</span>}
      {looksRequired && <span className="ml-1.5 text-caption text-fg-tertiary">looks required</span>}
    </span>
  )
}

/** The per-event required threshold: its own number, or the default. */
function ThresholdControl({
  slug,
  branchId,
  eventId,
  threshold,
  canWrite,
}: {
  slug: string
  branchId: string | null
  eventId: string
  threshold: number | null
  canWrite: boolean
}) {
  const qc = useQueryClient()
  const id = useId()
  const [draft, setDraft] = useState<string | null>(null)
  const shown = draft ?? String(Math.round((threshold ?? DEFAULT_REQUIRED_PRESENCE) * 1000) / 10)
  const mut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (next: number | null) =>
      eventsApi.update(slug, eventId, { required_presence_threshold: next }, branchId),
    onSuccess: () => {
      setDraft(null)
      void qc.invalidateQueries({ queryKey: eventKey(slug, branchId, eventId) })
      invalidatePropertyEntries(qc, slug, branchId)
    },
  })
  if (!canWrite) {
    return (
      <span className="text-caption text-fg-muted">
        Required at {formatThreshold(threshold)} presence{threshold === null ? ' (default)' : ''}
      </span>
    )
  }
  const parsed = Number(shown)
  const valid = shown.trim() !== '' && Number.isFinite(parsed) && parsed > 0 && parsed <= 100
  const commit = () => {
    if (draft === null) return
    if (!valid) return
    const next = Math.round(parsed * 10) / 1000
    if (next === (threshold ?? DEFAULT_REQUIRED_PRESENCE)) return setDraft(null)
    mut.mutate(next === DEFAULT_REQUIRED_PRESENCE ? null : next)
  }
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-caption text-fg-muted">
      <label htmlFor={id}>Required at</label>
      <Input
        id={id}
        type="number"
        inputMode="decimal"
        min={0.1}
        max={100}
        step="any"
        className="h-7 w-16 text-right"
        value={shown}
        aria-invalid={!valid || undefined}
        aria-describedby={mut.isError ? `${id}-error` : undefined}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            commit()
          }
        }}
      />
      <span>% presence</span>
      {threshold !== null && (
        <Button type="button" variant="ghost" size="xs" disabled={mut.isPending} onClick={() => mut.mutate(null)}>
          Reset to {formatThreshold(null)}
        </Button>
      )}
      {mut.isError && (
        <span id={`${id}-error`} role="alert" className="text-destructive">{getErrorMessage(mut.error)}</span>
      )}
    </div>
  )
}

/**
 * An event's property list as a grid (F23): each property's type from its
 * schema, whether the event must carry it, the values allowed here, and how
 * often the last scan saw it. Editors change an entry in place; every change
 * saves at once, on the branch being edited, apart from the event form's Save.
 */
export function EventPropertiesGrid({
  slug,
  branchId,
  eventId,
  threshold,
  canWrite,
  projectVariables = [],
  className,
}: {
  slug: string
  branchId: string | null
  eventId: string
  /** The event's own required threshold; null: the default. */
  threshold: number | null
  canWrite: boolean
  /** The project's properties, for "Add property". */
  projectVariables?: Variable[]
  className?: string
}) {
  const qc = useQueryClient()
  const { confirm, dialog } = useConfirm()
  const [editing, setEditing] = useState<{ variableId: string; values: string[] } | null>(null)
  const [addSearch, setAddSearch] = useState('')
  const [addRequired, setAddRequired] = useState(false)
  const addId = useId()

  const query = useQuery({
    queryKey: eventPropertiesKey(slug, branchId, eventId),
    queryFn: () => propertyEntriesApi.forEvent(slug, eventId, branchId),
  })
  const entries = useMemo(() => query.data ?? [], [query.data])
  const effectiveThreshold = threshold ?? DEFAULT_REQUIRED_PRESENCE

  const setMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: ({ variableId, patch }: { variableId: string; patch: PropertyEntriesBulkPatch }) =>
      propertyEntriesApi.set(slug, variableId, eventId, patch, branchId),
    onSuccess: () => {
      setEditing(null)
      invalidatePropertyEntries(qc, slug, branchId)
    },
  })
  const removeMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (variableId: string) => propertyEntriesApi.remove(slug, variableId, eventId, branchId),
    onSuccess: () => invalidatePropertyEntries(qc, slug, branchId),
  })
  const pending = setMut.isPending || removeMut.isPending
  const error = [setMut, removeMut].find((m) => m.isError)?.error

  const onList = useMemo(() => new Set(entries.map((entry) => entry.variable_id)), [entries])
  const suggestions = useMemo(() => {
    const needle = addSearch.trim().toLowerCase()
    if (!needle) return []
    return projectVariables
      .filter((v) => !onList.has(v.id) && v.name.toLowerCase().includes(needle))
      .slice(0, ADD_SUGGESTION_LIMIT)
  }, [addSearch, onList, projectVariables])

  const handleRemove = async (entry: EventPropertyEntry) => {
    const ok = await confirm({
      title: 'Remove property',
      message: `Take ${entry.name} off this event's property list?${entry.values !== null ? ' Its per-event values go with it.' : ''}`,
      confirmLabel: 'Remove',
      variant: 'danger',
    })
    if (ok) removeMut.mutate(entry.variable_id)
  }

  const editedEntry = editing ? entries.find((entry) => entry.variable_id === editing.variableId) : undefined
  const invalidEdited = editing && editedEntry ? invalidValuesFor(editedEntry.variable_type, editing.values) : []
  const requiredCount = entries.filter((entry) => entry.required).length

  return (
    <Panel
      className={className}
      title="Properties"
      subtitle={
        query.isPending
          ? 'Loading…'
          : entries.length === 0
            ? 'No properties on this event yet.'
            : `${entries.length} on this event · ${requiredCount} required`
      }
      right={
        <ThresholdControl
          slug={slug}
          branchId={branchId}
          eventId={eventId}
          threshold={threshold}
          canWrite={canWrite}
        />
      }
    >
      {dialog}
      {query.isError && !query.data ? (
        <div className="p-4">
          <ErrorState
            compact
            title="Couldn't load this event's properties"
            error={query.error}
            onRetry={() => { void query.refetch() }}
          />
        </div>
      ) : entries.length > 0 ? (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Property</TableHead>
              <TableHead>Type</TableHead>
              <TableHead>Required</TableHead>
              <TableHead>Allowed values</TableHead>
              <TableHead className="text-right">Presence</TableHead>
              {canWrite && <TableHead className="w-16"><span className="sr-only">Actions</span></TableHead>}
            </TableRow>
          </TableHeader>
          <TableBody>
            {entries.map((entry) => {
              const isEditing = editing?.variableId === entry.variable_id
              return (
                <TableRow key={entry.id}>
                  <TableCell>
                    <Link
                      to={variableDetailPath(slug, entry.variable_id)}
                      className="mono text-body-sm font-medium hover:underline"
                      title={entry.description || undefined}
                    >
                      {entry.name}
                    </Link>
                  </TableCell>
                  <TableCell><TypeCell entry={entry} /></TableCell>
                  <TableCell>
                    {canWrite ? (
                      <Switch
                        aria-label={`${entry.name} is required`}
                        checked={entry.required}
                        disabled={pending}
                        onCheckedChange={(checked) =>
                          setMut.mutate({ variableId: entry.variable_id, patch: { required: checked } })
                        }
                      />
                    ) : (
                      <span className="text-body-sm">{entry.required ? 'Required' : 'Optional'}</span>
                    )}
                  </TableCell>
                  <TableCell className="min-w-56">
                    {isEditing && editing ? (
                      <div className="grid gap-1.5">
                        <ChipListInput
                          values={editing.values}
                          onChange={(values) => setEditing({ variableId: entry.variable_id, values })}
                          placeholder="Type a value, press Enter"
                          ariaLabel={`Add allowed value of ${entry.name} for this event`}
                          {...valueRuleFor(entry.variable_type)}
                        />
                        {invalidEdited.length > 0 && (
                          <p className="text-caption text-warning">Not valid for the type: {invalidEdited.join(', ')}.</p>
                        )}
                        <div className="flex flex-wrap gap-1.5">
                          <Button
                            type="button"
                            size="xs"
                            disabled={pending}
                            onClick={() => setMut.mutate({ variableId: entry.variable_id, patch: { values: editing.values } })}
                          >
                            Save values
                          </Button>
                          {entry.values !== null && (
                            <Button
                              type="button"
                              size="xs"
                              variant="outline"
                              disabled={pending}
                              onClick={() => setMut.mutate({ variableId: entry.variable_id, patch: { values: null } })}
                            >
                              Use documented list
                            </Button>
                          )}
                          <Button type="button" size="xs" variant="ghost" onClick={() => setEditing(null)}>
                            Cancel
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <ValuesCell entry={entry} />
                    )}
                  </TableCell>
                  <TableCell className="text-right">
                    <PresenceCell entry={entry} threshold={effectiveThreshold} />
                  </TableCell>
                  {canWrite && (
                    <TableCell>
                      <div className="flex justify-end gap-0.5">
                        <IconButton
                          type="button"
                          variant="ghost"
                          className="size-7"
                          label={`Edit allowed values of ${entry.name} for this event`}
                          disabled={pending || isEditing}
                          onClick={() => setEditing({ variableId: entry.variable_id, values: [...entry.effective_values] })}
                        >
                          <Pencil className="size-3.5" aria-hidden="true" />
                        </IconButton>
                        <IconButton
                          type="button"
                          variant="ghost"
                          className="size-7 text-fg-tertiary hover:text-destructive"
                          label={`Remove ${entry.name} from this event`}
                          disabled={pending}
                          onClick={() => { void handleRemove(entry) }}
                        >
                          <Trash2 className="size-3.5" aria-hidden="true" />
                        </IconButton>
                      </div>
                    </TableCell>
                  )}
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      ) : !query.isPending ? (
        <p className="px-4 py-3 text-body-sm text-fg-tertiary">
          {canWrite
            ? 'List the properties this event carries: add them below, or accept the new-property drifts a scan reports.'
            : 'This event lists no properties yet.'}
        </p>
      ) : null}
      {canWrite && (
        <div className="grid gap-2 border-t px-4 py-3">
          <div className="flex flex-wrap items-center gap-3">
            <label htmlFor={addId} className="text-body-sm font-medium">Add property</label>
            <Input
              id={addId}
              className="h-8 max-w-64"
              placeholder="Search properties…"
              value={addSearch}
              aria-controls={`${addId}-list`}
              onChange={(e) => setAddSearch(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') e.preventDefault() }}
            />
            <label className="flex items-center gap-2 text-body-sm">
              <Switch checked={addRequired} onCheckedChange={setAddRequired} aria-label="Add as required" />
              Required
            </label>
          </div>
          {suggestions.length > 0 && (
            <ul id={`${addId}-list`} className="flex flex-wrap gap-1.5" aria-label="Matching properties">
              {suggestions.map((variable) => (
                <li key={variable.id}>
                  <Button
                    type="button"
                    size="xs"
                    variant="outline"
                    disabled={pending}
                    onClick={() => {
                      setMut.mutate({ variableId: variable.id, patch: { required: addRequired } })
                      setAddSearch('')
                    }}
                  >
                    <Plus className="size-3" aria-hidden="true" />
                    <span className="mono">{variable.name}</span>
                  </Button>
                </li>
              ))}
            </ul>
          )}
          {addSearch.trim() !== '' && suggestions.length === 0 && (
            <p className="text-caption text-fg-tertiary">No property off this event's list matches.</p>
          )}
        </div>
      )}
      {error !== undefined && (
        <p role="alert" className="px-4 pb-3 text-body text-destructive">{getErrorMessage(error)}</p>
      )}
    </Panel>
  )
}
