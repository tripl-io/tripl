import { Panel, Field, NativeSelect } from '@/components/settings/kit'
import { useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Check,
  Pencil,
  Plus,
  Save,
  Shapes,
  Trash2,
  X,
} from 'lucide-react'
import { eventTypeOwnersApi } from '@/api/eventTypeOwners'
import { eventTypesApi } from '@/api/eventTypes'
import { fieldsApi } from '@/api/fields'
import { usersApi } from '@/api/users'
import { useActiveBranchId } from '@/hooks/useBranch'
import type { EventType, EventTypeOwner, FieldDefinition, Sensitivity } from '@/types'
import { DEFAULT_ENTITY_COLOR, SENSITIVITY_OPTIONS } from '@/types'
import { useConfirm } from '@/hooks/useConfirm'
import { useUnsavedChangesGuard } from '@/hooks/useUnsavedChangesGuard'
import { Button } from '@/components/ui/button'
import { IconButton } from '@/components/ui/icon-button'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import { Switch } from '@/components/ui/switch'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import {
  useFieldControlId,
} from '@/components/settings/field-control-id'
import { Chip } from '@/components/primitives/chip'
import { PageContainer } from '@/components/primitives/page-container'
import { PageHeader } from '@/components/primitives/page-header'
import { FieldError } from '@/components/forms/FieldError'
import { SaveBar } from '@/components/forms/SaveBar'
import { REQUIRED_MESSAGE, attentionSummary, focusFirstInvalid } from '@/components/forms/validation'
import { SensitivityChip } from '@/components/primitives/sensitivity-chip'
import { countOf } from '@/lib/plural'
import { cn, getErrorMessage } from '@/lib/utils'
import {
  eventTypeOwnersKey,
  eventTypesKey,
  projectEventTypeOwnersKey,
  projectEventTypesKey,
  projectKey,
  projectMembersQueryOptions,
  usersKey,
} from '@/lib/queryKeys'
import { TEXT_INPUT_CLASS } from '@/pages/events/eventFormLayout'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useCanWriteProject } from '@/lib/permissions'
import { projectCandidates, type ProjectCandidate } from '@/lib/projectCandidates'
import { ReadOnlyNotice } from '@/components/states'
import {
  parseContract,
  regexNotice,
  validateContract,
  type ContractDraft,
  type ContractErrors,
} from './fieldContract'
import { describedByIds, SFieldHintContext, useSFieldHintId } from './sFieldContext'
import { UserAvatar } from '@/components/ui/user-avatar'

const FIELD_TYPES = ['string', 'number', 'boolean', 'json', 'enum', 'url']


/**
 * The field's contract rules in words, one per rule: the Contract cell shows
 * their count and lists them on hover, where a bare "1" said nothing (AU-12).
 */
function fieldContractRules(field: FieldDefinition): string[] {
  const rules: string[] = []
  if (field.is_required) rules.push('Required')
  const enumCount = field.field_type === 'enum' ? (field.enum_options?.length ?? 0) : 0
  if (enumCount > 0) rules.push(`One of ${countOf(enumCount, 'option', 'options')}`)
  if (field.contract_regex) rules.push(`Regex ${field.contract_regex}`)
  if (field.contract_min_value != null || field.contract_max_value != null) {
    rules.push(
      [
        field.contract_min_value != null ? `Min ${field.contract_min_value}` : null,
        field.contract_max_value != null ? `Max ${field.contract_max_value}` : null,
      ].filter(Boolean).join(' · '),
    )
  }
  return rules
}

/** "screen_view" -> "Screen View": the display name a type gets by default. */
function displayNameFrom(name: string): string {
  return name
    .trim()
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

// Which contract inputs a field type can use (AU-16): bounds only mean
// something for numbers, a pattern only for text. An input with a saved value
// stays on screen whatever the type, so no rule is ever kept out of sight.
const RANGE_FIELD_TYPES = new Set(['number'])
const PATTERN_FIELD_TYPES = new Set(['string', 'url'])

function sensitiveFieldCount(eventType: EventType): number {
  return eventType.field_definitions.filter((f) => f.sensitivity !== 'none').length
}

function requiredFieldCount(eventType: EventType): number {
  return eventType.field_definitions.filter((f) => f.is_required).length
}

// ─────────────────────────── Event types list ───────────────────────────

export function EventTypesTab({ slug }: { slug: string }) {
  const branchId = useActiveBranchId()
  const canWrite = useCanWriteProject()
  const [creating, setCreating] = useState(false)

  const typesQuery = useQuery({
    queryKey: eventTypesKey(slug, branchId),
    queryFn: () => eventTypesApi.list(slug, branchId),
    // Rendered in the panel below, with a retry.
    meta: SILENT_ERROR_META,
  })
  const eventTypes = typesQuery.data ?? []

  // Owners drive the list's Owner column and the derived merge Status (no
  // owners ⇒ anyone can merge ⇒ "ungated"; owners present ⇒ "gated"). Owners
  // are a main-plan fact keyed by MAIN's type ids. A branch lists its own
  // deep-copied ids, so asking for their owners was one 404 per type on every
  // visit in branch context, for a column the page then hid anyway
  // (tripl-kjhi.11). The editor for owners is likewise main-only.
  //
  // One request for the whole project, grouped here: the list used to fire one
  // /owners request per type on every visit (PLAN-42). The key is a prefix of
  // each type's own owners key, so an owner change invalidates both.
  const onMain = branchId === null
  const ownersQuery = useQuery({
    queryKey: projectEventTypeOwnersKey(slug),
    queryFn: () => eventTypeOwnersApi.listForProject(slug),
    enabled: onMain,
    // An unanswered owners request makes the Status cell say "—" rather than
    // guess, so a toast would only repeat it.
    meta: SILENT_ERROR_META,
  })
  // `undefined` = not known (still loading, or the request failed), which is
  // NOT the same as "no owners" and must not be rendered as "ungated". Once the
  // project's owners have answered, a type with no rows has none: [].
  const ownersByType = new Map<string, EventTypeOwner[] | undefined>()
  const projectOwners = ownersQuery.data
  eventTypes.forEach((et) => {
    ownersByType.set(
      et.id,
      projectOwners === undefined ? undefined : projectOwners.filter((o) => o.event_type_id === et.id),
    )
  })

  if (creating) {
    return <CreateEventTypeView slug={slug} branchId={branchId} onDone={() => setCreating(false)} />
  }

  const sorted = [...eventTypes].sort((a, b) => a.order - b.order)
  // Hide columns that are empty for every visible row — a wall of em-dashes
  // (no sensitive fields / no owners anywhere) is noise, not information.
  const showSensitive = sorted.some((et) => sensitiveFieldCount(et) > 0)
  const showOwner = sorted.some((et) => (ownersByType.get(et.id) ?? []).length > 0)
  // Owners live on main, so a branch cannot know whether a type is gated. The
  // column used to say "ungated" for every row there — wrong for exactly the
  // types whose owners will block this branch's merge (PLAN-40).
  const showStatus = onMain

  return (
    <PageContainer>
      {/* The shared page header (DS-1): a real h1 under the Plan eyebrow, in
          place of an 18px h2 with a 14px description. */}
      <PageHeader
        eyebrow="Plan"
        title="Event types"
        description="Categories that group your events and define their shared schema, ownership and naming. Settings here apply to every event of that type."
        actions={
          canWrite && (
            <Button size="sm" onClick={() => setCreating(true)}>
              <Plus className="size-3.5" />
              New type
            </Button>
          )
        }
      />
      {!canWrite && <ReadOnlyNotice />}

      <Panel
        title="All types"
        subtitle={typesQuery.isPending ? 'Loading…' : countOf(sorted.length, 'type', 'types')}
      >
        {typesQuery.isError && typesQuery.data !== undefined && (
          // A failed REFRESH keeps the rows on screen: replacing them with an
          // error would unmount whatever is being edited (review 204).
          <p role="alert" className="px-4 py-2 text-body-sm text-destructive">
            Couldn't refresh event types: {getErrorMessage(typesQuery.error)}
          </p>
        )}
        {typesQuery.isPending ? (
          // A pending list is not an empty one: "No event types yet" used to
          // flash on every cold load and stay up on a 500 (PLAN-41).
          <div className="space-y-2 px-4 py-4" aria-busy="true" aria-label="Loading event types">
            {Array.from({ length: 3 }, (_, index) => (
              <Skeleton key={index} className="h-10 w-full" />
            ))}
          </div>
        ) : typesQuery.isError && typesQuery.data === undefined ? (
          <div className="p-4">
            <ErrorState
              compact
              title="Couldn't load event types"
              error={typesQuery.error}
              onRetry={() => { void typesQuery.refetch() }}
              retryLabel="Retry"
            />
          </div>
        ) : sorted.length === 0 ? (
          // The first thing a new project creates, so the empty state teaches
          // what a type is for and offers the step (AU-34).
          <div className="px-4 py-8">
            <EmptyState
              icon={Shapes}
              title="No event types yet"
              description="A type holds the fields its events share, e.g. Screen View carries screen_name. Create one to categorize your events."
              action={canWrite ? (
                <Button type="button" size="sm" onClick={() => setCreating(true)}>
                  <Plus className="size-3.5" />
                  Create your first event type
                </Button>
              ) : undefined}
            />
          </div>
        ) : (
          // The shared table. `scroll={false}`: the panel body is already the
          // sideways-scrolling region, and a second one inside it would be a
          // nested scroller. Required, Sensitive and Owner are the columns a
          // phone can do without; they come back from `md` up.
          <Table scroll={false} aria-label="Event types">
            <TableHeader>
              <TableRow style={{ background: 'var(--bg-sunken)' }}>
                <Th>Type</Th>
                <Th>Fields</Th>
                <Th align="right" wideOnly>Required</Th>
                {showSensitive && <Th wideOnly>Sensitive</Th>}
                {showOwner && <Th wideOnly>Owner</Th>}
                {/* About who must approve a merge, not the type's state: the
                    column read "Status: gated" (AU-12). */}
                {showStatus && <Th>Merge approval</Th>}
                <Th style={{ width: 40 }}><span className="sr-only">Details</span></Th>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sorted.map((et) => (
                <ListRow key={et.id}>
                  <Td>
                    <div className="flex items-center gap-2.5">
                      <span
                        className="size-[9px] shrink-0 rounded-sm"
                        style={{ background: et.color || DEFAULT_ENTITY_COLOR }}
                        aria-hidden="true"
                      />
                      <div className="min-w-0">
                        {/* A real link, not a `role="button"` row: the row keeps
                            its cell semantics, so a screen reader still reads the
                            column headers (PLAN-39). */}
                        <Link
                          to={`/p/${slug}/event-types/${et.id}`}
                          className="text-body font-semibold hover:underline text-fg"
                        >
                          {et.display_name}
                        </Link>
                        <div className="mono text-caption text-fg-tertiary">
                          {et.name}_*
                        </div>
                      </div>
                    </div>
                  </Td>
                  {/* Counts are figures: sans + tabular digits (DS-17). */}
                  <Td>
                    <span className="tnum">{et.field_definitions.length}</span>
                  </Td>
                  <Td align="right" wideOnly>
                    <span className="tnum text-fg-secondary">
                      {requiredFieldCount(et)}
                    </span>
                  </Td>
                  {showSensitive && (
                    <Td wideOnly>
                      {sensitiveFieldCount(et) > 0 ? (
                        <Chip tone="warning" size="xs">
                          {sensitiveFieldCount(et)}
                        </Chip>
                      ) : (
                        <span className="text-fg-tertiary">—</span>
                      )}
                    </Td>
                  )}
                  {showOwner && (
                    <Td wideOnly>
                      {(() => {
                        const owners = ownersByType.get(et.id) ?? []
                        const [firstOwner] = owners
                        if (!firstOwner) {
                          return <span className="text-fg-tertiary">—</span>
                        }
                        return (
                          <span className="text-body-sm text-fg-tertiary">
                            {firstOwner.user_name || firstOwner.user_email}
                            {owners.length > 1 ? ` +${owners.length - 1}` : ''}
                          </span>
                        )
                      })()}
                    </Td>
                  )}
                  {showStatus && (
                    <Td>
                      {(() => {
                        const owners = ownersByType.get(et.id)
                        if (owners === undefined) {
                          return (
                            <span className="text-fg-tertiary" title="Owners not known yet">
                              —
                            </span>
                          )
                        }
                        return owners.length > 0 ? (
                          <Chip
                            tone="accent"
                            size="xs"
                            title="Has owners — a branch that edits this type needs an owner's approval to merge"
                          >
                            Owner approval
                          </Chip>
                        ) : (
                          <Chip
                            tone="neutral"
                            size="xs"
                            title="No owners — anyone can merge changes to this type"
                          >
                            {/* The detail header's words for the same state (AU-12). */}
                            Open to merge
                          </Chip>
                        )
                      })()}
                    </Td>
                  )}
                  <Td>
                    <ChevronRight
                      className="size-3.5 text-fg-tertiary"
                      aria-hidden="true"
                    />
                  </Td>
                </ListRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Panel>
    </PageContainer>
  )
}

// ─────────────────────── New event type (page-style) ───────────────────────

interface CreateEventTypeViewProps {
  slug: string
  branchId: string | null
  onDone: () => void
}

function CreateEventTypeView({ slug, branchId, onDone }: CreateEventTypeViewProps) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [description, setDescription] = useState('')
  const [color, setColor] = useState(DEFAULT_ENTITY_COLOR)
  // Shown after a Create with no name: the submit used to do nothing at all.
  const [nameError, setNameError] = useState<string | null>(null)
  const nameId = useId()

  const createMut = useMutation({
    // Its error is rendered under the form.
    meta: SILENT_ERROR_META,
    mutationFn: () =>
      eventTypesApi.create(
        slug,
        { name: name.trim(), display_name: displayName.trim() || displayNameFrom(name), description, color },
        branchId,
      ),
    onSuccess: (created) => {
      qc.invalidateQueries({ queryKey: projectEventTypesKey(slug) })
      // The sidebar's event-type count reads the project summary (AU-32).
      qc.invalidateQueries({ queryKey: projectKey(slug) })
      // A type is useful once it has fields, so it opens where they are
      // added rather than back on the list (AU-36).
      if (created?.id) navigate(`/p/${slug}/event-types/${created.id}?tab=settings`)
      else onDone()
    },
  })

  return (
    <PageContainer width="narrow" className="space-y-[18px]">
      <PageHeader
        back={<BackLink label="Event types" onClick={onDone} />}
        eyebrow="Plan · Event type"
        title="New event type"
      />
      {/* noValidate + an inline "Required" (AU-4): one validation pattern. */}
      <form
        noValidate
        onSubmit={(e) => {
          e.preventDefault()
          if (!name.trim()) {
            setNameError(REQUIRED_MESSAGE)
            const form = e.currentTarget
            requestAnimationFrame(() => focusFirstInvalid(form))
            return
          }
          createMut.mutate()
        }}
      >
        <Panel
          className="mb-3"
          title="General"
          footer={
            <SaveFooter onCancel={onDone} pending={createMut.isPending} submitLabel="Create type" />
          }
        >
          <SField
            label="Name"
            required
            hint="Lowercase letters, digits and _, used in queries and ingestion. Can't be changed later."
          >
            <SInput
              id={nameId}
              required
              value={name}
              onChange={(v) => {
                setName(v)
                if (v.trim()) setNameError(null)
              }}
              mono
              placeholder="e.g. checkout"
              invalid={!!nameError}
              describedBy={nameError ? `${nameId}-error` : undefined}
            />
            <FieldError inputId={nameId} message={nameError} />
          </SField>
          {/* Left empty, the type is shown under a name made from Name, and the
              placeholder says which (AU-36). */}
          <SField label="Display name">
            <SInput
              value={displayName}
              onChange={setDisplayName}
              placeholder={name.trim() ? displayNameFrom(name) : 'e.g. Checkout'}
            />
          </SField>
          <SField label="Description">
            <STextarea value={description} onChange={setDescription} />
          </SField>
          <SField label="Color" last>
            <ColorPicker value={color} onChange={setColor} />
          </SField>
        </Panel>
        <p className="text-body-sm text-fg-tertiary">
          Next: add the fields every event of this type carries. The type opens on its settings once
          created.
        </p>
        {createMut.isError && (
          <p className="mt-2 text-body text-danger">
            {getErrorMessage(createMut.error)}
          </p>
        )}
      </form>
    </PageContainer>
  )
}

export function ColorPicker({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  // Takes the enclosing SField's label when there is one.
  const id = useFieldControlId()
  return (
    <div className="flex items-center gap-2.5">
      <input
        id={id}
        type="color"
        value={value || DEFAULT_ENTITY_COLOR}
        onChange={(e) => onChange(e.target.value)}
        aria-label="Color"
        className="cursor-pointer rounded-control p-0.5"
        style={{ width: 40, height: 34, border: '1px solid var(--border)', background: 'none' }}
      />
      <span className="mono text-body-sm text-fg-secondary">
        {value || DEFAULT_ENTITY_COLOR}
      </span>
    </div>
  )
}

// ─────────────────────────── Fields editor ───────────────────────────

interface FieldDraft extends ContractDraft {
  name: string
  display_name: string
  field_type: string
  is_required: boolean
  description: string
  enum_options: string[]
  sensitivity: Sensitivity
}

function emptyDraft(): FieldDraft {
  return {
    name: '',
    display_name: '',
    field_type: 'string',
    is_required: false,
    description: '',
    enum_options: [],
    sensitivity: 'none',
    contract_max_bad_rate: '0',
    contract_required_max_null_rate: '',
    contract_regex: '',
    contract_min_value: '',
    contract_max_value: '',
  }
}

function draftFromField(f: FieldDefinition): FieldDraft {
  return {
    name: f.name,
    display_name: f.display_name,
    field_type: f.field_type,
    is_required: f.is_required,
    description: f.description,
    enum_options: f.enum_options ?? [],
    sensitivity: f.sensitivity,
    contract_max_bad_rate: String(f.contract_max_bad_rate ?? 0),
    contract_required_max_null_rate:
      f.contract_required_max_null_rate == null ? '' : String(f.contract_required_max_null_rate),
    contract_regex: f.contract_regex ?? '',
    contract_min_value: f.contract_min_value == null ? '' : String(f.contract_min_value),
    contract_max_value: f.contract_max_value == null ? '' : String(f.contract_max_value),
  }
}

// Only ever called on a draft FieldEditPage has validated (validateContract):
// an input that does not parse is an error on the form, never a dropped or
// tightened rule (PLAN-38).
function draftContract(draft: FieldDraft) {
  return parseContract(draft)
}

export function FieldsEditor({
  slug,
  eventType,
  branchId,
  onEditingChange,
}: {
  slug: string
  eventType: EventType
  branchId: string | null
  /**
   * Told when the field page opens and closes, so the page around it can put
   * its other cards away: one title and one Save in view (AU-15).
   */
  onEditingChange?: (editing: boolean) => void
}) {
  const qc = useQueryClient()
  const canWrite = useCanWriteProject()
  // editing view-state: null = list, 'new' = add subpage, field = edit subpage.
  const [editing, setEditing] = useState<FieldDefinition | 'new' | null>(null)
  const isEditing = editing !== null
  useEffect(() => {
    onEditingChange?.(isEditing)
  }, [isEditing, onEditingChange])
  const { confirm, dialog } = useConfirm()

  const sortedFields = [...eventType.field_definitions].sort((a, b) => a.order - b.order)
  const invalidate = () => qc.invalidateQueries({ queryKey: projectEventTypesKey(slug) })

  // Create and update render their error on the field page (`error` below).
  const createMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (draft: FieldDraft) =>
      fieldsApi.create(
        slug,
        eventType.id,
        {
          name: draft.name.trim(),
          display_name: draft.display_name.trim() || draft.name.trim(),
          field_type: draft.field_type,
          is_required: draft.is_required,
          description: draft.description,
          ...(draft.field_type === 'enum' && draft.enum_options.length > 0
            ? { enum_options: draft.enum_options }
            : {}),
          order: sortedFields.length,
          sensitivity: draft.sensitivity,
          ...draftContract(draft),
        },
        branchId,
      ),
    onSuccess: () => {
      invalidate()
      setEditing(null)
    },
  })

  const updateMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: ({ id, draft }: { id: string; draft: FieldDraft }) =>
      fieldsApi.update(
        slug,
        eventType.id,
        id,
        {
          display_name: draft.display_name.trim() || draft.name,
          field_type: draft.field_type as FieldDefinition['field_type'],
          is_required: draft.is_required,
          description: draft.description,
          enum_options: draft.field_type === 'enum' ? draft.enum_options : null,
          sensitivity: draft.sensitivity,
          ...draftContract(draft),
        },
        branchId,
      ),
    onSuccess: () => {
      invalidate()
      setEditing(null)
    },
  })

  // Applied to the cached list at once, so the row moves with the click instead
  // of after a full refetch (PLAN-37); a refusal puts the server's order back.
  const reorderMut = useMutation({
    // Its error is rendered above the table.
    meta: SILENT_ERROR_META,
    mutationFn: (fieldIds: string[]) => fieldsApi.reorder(slug, eventType.id, fieldIds, branchId),
    onMutate: async (fieldIds: string[]) => {
      const key = eventTypesKey(slug, branchId)
      await qc.cancelQueries({ queryKey: key })
      const orderOf = new Map(fieldIds.map((id, order) => [id, order]))
      qc.setQueryData<EventType[]>(key, (types) =>
        types?.map((et) =>
          et.id !== eventType.id
            ? et
            : {
                ...et,
                field_definitions: et.field_definitions.map((f) => ({
                  ...f,
                  order: orderOf.get(f.id) ?? f.order,
                })),
              },
        ),
      )
    },
    onSettled: invalidate,
  })
  // Said aloud after a move, because the row jumping is only visible (PLAN-37).
  const [moveAnnouncement, setMoveAnnouncement] = useState('')
  // After a move to the top or bottom the pressed button turns disabled and
  // focus would fall to <body>; this names the button that takes it instead.
  const [focusRequest, setFocusRequest] = useState<{ fieldId: string; button: 'up' | 'down' } | null>(null)

  const deleteMut = useMutation({
    mutationFn: (id: string) => fieldsApi.del(slug, eventType.id, id, branchId),
    onSuccess: invalidate,
  })

  const handleDelete = async (f: FieldDefinition) => {
    // Drop the previous failure first: a stale 409 next to a different field's
    // confirmation reads as if THAT delete had failed.
    deleteMut.reset()
    const ok = await confirm({
      title: 'Delete field',
      message: `Delete "${f.display_name}" from ${eventType.display_name}?`,
      confirmLabel: 'Delete',
      variant: 'danger',
    })
    if (ok) deleteMut.mutate(f.id)
  }

  const moveField = (idx: number, direction: -1 | 1) => {
    // Ignored rather than disabled while a move is in flight: disabling every
    // move button took focus away from the one just pressed.
    if (reorderMut.isPending) return
    const newIdx = idx + direction
    if (newIdx < 0 || newIdx >= sortedFields.length) return
    const reordered = [...sortedFields]
    const [moved] = reordered.splice(idx, 1)
    if (!moved) return
    reordered.splice(newIdx, 0, moved)
    reorderMut.reset()
    reorderMut.mutate(reordered.map((f) => f.id))
    setMoveAnnouncement(`${moved.name} moved to position ${newIdx + 1} of ${sortedFields.length}`)
    if (newIdx === 0) setFocusRequest({ fieldId: moved.id, button: 'down' })
    else if (newIdx === sortedFields.length - 1) setFocusRequest({ fieldId: moved.id, button: 'up' })
    else setFocusRequest(null)
  }

  if (editing) {
    const field = editing === 'new' ? null : editing
    const pending = createMut.isPending || updateMut.isPending
    const error = createMut.isError
      ? getErrorMessage(createMut.error)
      : updateMut.isError
        ? getErrorMessage(updateMut.error)
        : null
    return (
      <FieldEditPage
        field={field}
        pending={pending}
        error={error}
        onCancel={() => setEditing(null)}
        onSubmit={(draft) => {
          if (field) updateMut.mutate({ id: field.id, draft })
          else createMut.mutate(draft)
        }}
      />
    )
  }

  return (
    <Panel
      className="mb-3"
      title="Fields"
      subtitle={`${sortedFields.length} field definitions applied to every ${eventType.display_name.toLowerCase()} event.`}
      right={
        canWrite && (
          <Button variant="outline" size="sm" onClick={() => setEditing('new')}>
            <Plus className="size-3" />
            Add field
          </Button>
        )
      }
    >
      {dialog}
      {/* Without this the backend's 409 (deleting a field a scan's event name
          format builds event names from) is invisible: the row simply stays and
          the operator has no idea why (tripl-3mmh). The backend sends a plain
          string detail, which api/client.ts puts straight into ApiError.message,
          so it renders verbatim — it already names the scan and the one edit
          that unblocks the delete.

          Verbatim deliberately (tripl-24i0): rewriting the wording here means
          matching backend prose, which fails OPEN the first time the backend
          rewords — the old string on screen and nothing to notice. The web UI's
          nouns are enforced where the sentence is written instead, by backend
          test_name_format_conflict_vocabulary, since scan-docs-agreement.test.ts
          reads frontend sources and cannot see a string built in Python. */}
      {deleteMut.isError && (
        <div role="alert" className="px-4 py-2 text-body-sm text-destructive">
          {getErrorMessage(deleteMut.error)}
        </div>
      )}
      {reorderMut.isError && (
        <div role="alert" className="px-4 py-2 text-body-sm text-destructive">
          Could not reorder fields: {getErrorMessage(reorderMut.error)}
        </div>
      )}
      <p aria-live="polite" className="sr-only">
        {moveAnnouncement}
      </p>
      {sortedFields.length === 0 ? (
        <p className="px-4 py-3.5 text-body-sm text-fg-tertiary">
          No fields defined yet.
        </p>
      ) : (
        // Display, Sensitivity and Contract are hidden below `md`: on a phone the
        // name, type and required flag are what identify a field, and the
        // row's actions must stay reachable.
        <Table scroll={false} aria-label={`${eventType.display_name} fields`}>
          <TableHeader>
            <TableRow style={{ background: 'var(--bg-sunken)' }}>
              <Th style={{ width: 34 }}><span className="sr-only">Order</span></Th>
              <Th>Name</Th>
              <Th wideOnly>Display</Th>
              <Th>Type</Th>
              <Th wideOnly>Sensitivity</Th>
              <Th>Required</Th>
              <Th wideOnly>Contract</Th>
              <Th style={{ width: 66 }}><span className="sr-only">Actions</span></Th>
            </TableRow>
          </TableHeader>
          <TableBody>
            {sortedFields.map((f, idx) => (
              <FieldRow
                key={f.id}
                field={f}
                isFirst={idx === 0}
                isLast={idx === sortedFields.length - 1}
                focusButton={focusRequest?.fieldId === f.id ? focusRequest.button : null}
                onFocused={() => setFocusRequest(null)}
                canWrite={canWrite}
                onMoveUp={() => moveField(idx, -1)}
                onMoveDown={() => moveField(idx, 1)}
                onEdit={() => setEditing(f)}
                onDelete={() => handleDelete(f)}
              />
            ))}
          </TableBody>
        </Table>
      )}
    </Panel>
  )
}

interface FieldRowProps {
  field: FieldDefinition
  isFirst: boolean
  isLast: boolean
  /** Which move button should take focus after this row moved, if any. */
  focusButton: 'up' | 'down' | null
  onFocused: () => void
  /** False for a read-only visitor: the row is information, not a way in. */
  canWrite: boolean
  onMoveUp: () => void
  onMoveDown: () => void
  onEdit: () => void
  onDelete: () => void
}

function FieldRow({
  field,
  isFirst,
  isLast,
  focusButton,
  onFocused,
  canWrite,
  onMoveUp,
  onMoveDown,
  onEdit,
  onDelete,
}: FieldRowProps) {
  const contractRules = fieldContractRules(field)
  const upRef = useRef<HTMLButtonElement>(null)
  const downRef = useRef<HTMLButtonElement>(null)
  // The move just put this row at an edge, so the button that was pressed is
  // now disabled; hand focus to the one that still works (PLAN-37).
  useEffect(() => {
    if (!focusButton) return
    const target = focusButton === 'up' ? upRef.current : downRef.current
    if (target && !target.disabled) {
      target.focus()
      onFocused()
    }
  }, [focusButton, isFirst, isLast, onFocused])
  return (
    <ListRow>
      <Td className="pr-0">
        {canWrite && <div className="flex flex-col gap-px">
          <IconButton ref={upRef} label={`Move ${field.name} up`} className={ROW_ICON_CLASS} disabled={isFirst} onClick={onMoveUp}>
            <ChevronUp className="size-3" />
          </IconButton>
          <IconButton ref={downRef} label={`Move ${field.name} down`} className={ROW_ICON_CLASS} disabled={isLast} onClick={onMoveDown}>
            <ChevronDown className="size-3" />
          </IconButton>
        </div>}
      </Td>
      <Td>
        {/* The way into the editor is this button, not a `role="button"` row
            wrapped around the move, edit and delete buttons (PLAN-39). */}
        {canWrite ? (
          <button
            type="button"
            onClick={onEdit}
            className="mono text-left text-body-sm hover:underline"
          >
            {field.name}
          </button>
        ) : (
          <span className="mono text-body-sm">{field.name}</span>
        )}
      </Td>
      <Td wideOnly>
        <span className="text-body-sm text-fg-secondary">
          {field.display_name}
        </span>
      </Td>
      <Td>
        <Chip variant="outline" size="xs">
          {field.field_type}
        </Chip>
        {field.field_type === 'enum' && field.enum_options && (
          <span className="ml-1 text-micro text-fg-tertiary">
            ({field.enum_options.length})
          </span>
        )}
      </Td>
      <Td wideOnly>
        <SensitivityChip value={field.sensitivity} />
      </Td>
      <Td>
        {field.is_required ? (
          <Check className="size-3.5 text-success" />
        ) : (
          <span className="text-fg-tertiary">—</span>
        )}
      </Td>
      <Td wideOnly>
        {contractRules.length > 0 ? (
          <Chip variant="outline" size="xs" title={contractRules.join('\n')}>
            {countOf(contractRules.length, 'rule', 'rules')}
          </Chip>
        ) : (
          <span className="text-fg-tertiary">—</span>
        )}
      </Td>
      <Td>
        {canWrite && <div className="flex justify-end gap-0.5">
          <IconButton label={`Edit field ${field.name}`} tooltip="Edit field" className={ROW_ICON_CLASS} onClick={onEdit}>
            <Pencil className="size-3.5" />
          </IconButton>
          <IconButton label={`Delete field ${field.name}`} tooltip="Delete field" className={ROW_ICON_DANGER_CLASS} onClick={onDelete}>
            <Trash2 className="size-3.5" />
          </IconButton>
        </div>}
      </Td>
    </ListRow>
  )
}

// ───────────────────── Field edit subpage (in-place view) ─────────────────────

interface FieldEditPageProps {
  field: FieldDefinition | null
  pending: boolean
  error: string | null
  onCancel: () => void
  onSubmit: (draft: FieldDraft) => void
}

function FieldEditPage({ field, pending, error, onCancel, onSubmit }: FieldEditPageProps) {
  const isEdit = !!field
  const [initialDraft] = useState<FieldDraft>(() => (field ? draftFromField(field) : emptyDraft()))
  const [draft, setDraft] = useState<FieldDraft>(initialDraft)
  const [enumInput, setEnumInput] = useState('')
  // Shown once a create was attempted with no name: Save used to do nothing at
  // all, with no word as to why (PLAN-46).
  const [nameMissing, setNameMissing] = useState(false)
  // Cancel and "← Fields" threw a half-filled contract away without asking.
  // The page guard also covers leaving through the app (the sidebar, Back) and
  // reload; a successful save unmounts this page, so it needs no release.
  // A read-only visitor has nothing to lose, so the guard never arms for one.
  const canWrite = useCanWriteProject()
  const unsaved = useUnsavedChangesGuard(
    canWrite
      && (JSON.stringify(draft) !== JSON.stringify(initialDraft) || enumInput.trim() !== ''),
  )
  const cancel = () => unsaved.requestLeave(onCancel)
  const set = <K extends keyof FieldDraft>(key: K, value: FieldDraft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }))

  const addEnum = () => {
    const v = enumInput.trim()
    if (v && !draft.enum_options.includes(v)) set('enum_options', [...draft.enum_options, v])
    setEnumInput('')
  }

  // Contract errors show as soon as something invalid is typed, and a required
  // one left blank shows once Save was tried (PLAN-38).
  const [submitAttempted, setSubmitAttempted] = useState(false)
  const contractErrors = validateContract(draft)
  const contractError = (key: keyof ContractErrors): string | undefined =>
    submitAttempted || draft[key].trim() !== '' ? contractErrors[key] : undefined
  const requiredId = useId()
  const enumInputId = useId()
  const errorIdBase = useId()
  const errorId = (key: keyof ContractErrors) => `${errorIdBase}-${key}`

  // Only the contract inputs this type can use, plus any holding a value
  // (AU-16); see RANGE_FIELD_TYPES.
  const showRegex = PATTERN_FIELD_TYPES.has(draft.field_type) || draft.contract_regex.trim() !== ''
  const showRange =
    RANGE_FIELD_TYPES.has(draft.field_type)
    || draft.contract_min_value.trim() !== ''
    || draft.contract_max_value.trim() !== ''

  const formRef = useRef<HTMLFormElement>(null)
  const submit = () => {
    setSubmitAttempted(true)
    const nameProblem = !isEdit && !draft.name.trim()
    if (nameProblem) setNameMissing(true)
    if (nameProblem || Object.keys(contractErrors).length > 0) {
      // After the render that marks them (AU-4).
      requestAnimationFrame(() => {
        if (formRef.current) focusFirstInvalid(formRef.current)
      })
      return
    }
    onSubmit(draft)
  }
  // What blocks Save, in red next to it (AU-6 / AU-5); the sticky bar keeps it
  // on screen from anywhere in the form.
  const blockingCount = submitAttempted
    ? Object.keys(contractErrors).length + (nameMissing ? 1 : 0)
    : 0

  const contractInput = (
    key: keyof ContractErrors,
    props: { placeholder?: string; decimal?: boolean } = {},
  ) => {
    const error = contractError(key)
    return (
      <>
        <SInput
          value={draft[key]}
          onChange={(v) => set(key, v)}
          // Mono only for the pattern: the rates and bounds are numbers (DS-17).
          mono={!props.decimal}
          placeholder={props.placeholder}
          inputMode={props.decimal ? 'decimal' : undefined}
          invalid={!!error}
          describedBy={error ? errorId(key) : undefined}
        />
        <FieldError id={errorId(key)} message={error} />
      </>
    )
  }

  return (
    // A real form, so Enter in any input saves, as it does everywhere else.
    <form
      ref={formRef}
      className="max-w-[880px]"
      noValidate
      onSubmit={(e) => {
        e.preventDefault()
        if (!pending) submit()
      }}
    >
      {unsaved.dialog}
      <BackLink label="Fields" onClick={cancel} />
      {/* A section of the event type page, under its h1: a heading-size h2,
          not a second 19px page title (DS-1). */}
      <h2 className="mb-[18px] text-heading font-semibold">
        {isEdit ? `Edit field · ${field.name}` : 'New field'}
      </h2>

      <Panel className="mb-3" title="Field" headingLevel={3}>
        {!isEdit && (
          <SField label="Name" hint="Matches the query column the scan populates.">
            <SInput
              value={draft.name}
              onChange={(v) => {
                set('name', v)
                if (v.trim()) setNameMissing(false)
              }}
              mono
              placeholder="e.g. order_id"
              invalid={nameMissing}
              describedBy={nameMissing ? 'field-name-error' : undefined}
            />
            <FieldError
              id="field-name-error"
              message={nameMissing ? 'A new field needs a name.' : null}
              announce
            />
          </SField>
        )}
        <SField label="Display name">
          <SInput
            value={draft.display_name}
            onChange={(v) => set('display_name', v)}
            placeholder="Optional"
          />
        </SField>
        <SField label="Type">
          <SSelect
            value={draft.field_type}
            onChange={(v) => set('field_type', v)}
            options={FIELD_TYPES.map((t) => ({ value: t, label: t }))}
          />
        </SField>
        <SField label="Sensitivity">
          <SSelect
            value={draft.sensitivity}
            onChange={(v) => set('sensitivity', v as Sensitivity)}
            options={SENSITIVITY_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
          />
        </SField>
        <SField label="Required" htmlFor={requiredId}>
          <div className="flex items-center gap-2.5">
            <Switch
              id={requiredId}
              checked={draft.is_required}
              onCheckedChange={(c) => set('is_required', c)}
            />
            <span className="text-body-sm text-fg-secondary">
              Must be present on every event
            </span>
          </div>
        </SField>
        <SField label="Description" last={draft.field_type !== 'enum'}>
          <SInput
            value={draft.description}
            onChange={(v) => set('description', v)}
            placeholder="Optional"
          />
        </SField>
        {draft.field_type === 'enum' && (
          <SField label="Enum options" last htmlFor={enumInputId}>
            <div className="flex flex-col gap-2">
              <div className="flex max-w-[360px] gap-2">
                <Input
                  id={enumInputId}
                  className="mono"
                  value={enumInput}
                  placeholder="Type option, press Enter"
                  onChange={(e) => setEnumInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault()
                      addEnum()
                    }
                  }}
                />
                <Button type="button" variant="outline" size="sm" onClick={addEnum}>
                  Add
                </Button>
              </div>
              {draft.enum_options.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {draft.enum_options.map((opt) => (
                    <span
                      key={opt}
                      className="mono inline-flex items-center gap-1.5 rounded-full pr-1.5 pl-2.5 text-caption"
                      style={{ height: 22, background: 'var(--surface-hover)' }}
                    >
                      {opt}
                      <IconButton
                        label={`Remove option ${opt}`}
                        tooltip="Remove option"
                        className={ROW_ICON_DANGER_CLASS}
                        onClick={() =>
                          set(
                            'enum_options',
                            draft.enum_options.filter((o) => o !== opt),
                          )
                        }
                      >
                        <X className="size-3" />
                      </IconButton>
                    </span>
                  ))}
                </div>
              )}
            </div>
          </SField>
        )}
      </Panel>

      <Panel
        className="mb-3"
        title="Data contract"
        subtitle="Quality rules tripl checks on every scan of this field."
        headingLevel={3}
      >
        <SField
          label="Max invalid share"
          hint="Share of values (0–1) allowed to break the rules below before the field is flagged."
        >
          {contractInput('contract_max_bad_rate', { decimal: true })}
        </SField>
        <SField
          label="Null share"
          hint="Max fraction allowed to be null (0–1). Leave empty for no rule."
          last={!showRegex && !showRange && draft.field_type !== 'enum'}
        >
          {contractInput('contract_required_max_null_rate', { decimal: true, placeholder: '—' })}
        </SField>
        {showRegex && (
          <SField label="Regex" hint="Values must match this pattern." last={!showRange}>
            {contractInput('contract_regex', { placeholder: 'e.g. ^[a-z0-9_]+$' })}
            {draft.contract_regex !== initialDraft.contract_regex && regexNotice(draft.contract_regex) && (
              <p className="mt-1 text-body-sm text-fg-tertiary">
                {regexNotice(draft.contract_regex)}
              </p>
            )}
          </SField>
        )}
        {showRange && (
          <>
            <SField label="Min">
              {contractInput('contract_min_value', { decimal: true, placeholder: '—' })}
            </SField>
            <SField label="Max" last>
              {contractInput('contract_max_value', { decimal: true, placeholder: '—' })}
            </SField>
          </>
        )}
        {draft.field_type === 'enum' && !showRegex && !showRange && (
          <p className="px-4 py-3 text-body-sm text-fg-tertiary">
            Values outside the enum options count as invalid.
          </p>
        )}
      </Panel>

      {/* Sticky, so Save and what blocks it stay in reach from the top of
          the form (AU-6). */}
      <SaveBar
        status={attentionSummary(blockingCount)}
        statusTone="danger"
        onStatusClick={
          blockingCount > 0
            ? () => {
                if (formRef.current) focusFirstInvalid(formRef.current)
              }
            : undefined
        }
        error={error}
      >
        <Button type="button" variant="outline" size="sm" onClick={cancel}>
          Cancel
        </Button>
        <Button type="submit" size="sm" disabled={pending}>
          {isEdit ? <Save className="size-3.5" /> : <Plus className="size-3.5" />}
          {isEdit ? 'Save field' : 'Add field'}
        </Button>
      </SaveBar>
    </form>
  )
}

// ─────────────────────────── Owners editor ───────────────────────────

export function OwnersEditor({ slug, eventType }: { slug: string; eventType: EventType }) {
  const qc = useQueryClient()
  const canWrite = useCanWriteProject()
  const [selectedUserId, setSelectedUserId] = useState('')
  const { confirm, dialog } = useConfirm()
  const ownerSelectId = useId()

  const { data: owners = [] } = useQuery({
    queryKey: eventTypeOwnersKey(slug, eventType.id),
    queryFn: () => eventTypeOwnersApi.list(slug, eventType.id),
  })
  // Only the project's members, and the instance owners who see every project
  // without a member row, can own its event types: anyone else cannot see the
  // project, and the server refuses them (tripl-vefw).
  const { data: members } = useQuery(projectMembersQueryOptions(slug))
  const { data: users } = useQuery({ queryKey: usersKey(), queryFn: () => usersApi.list() })

  // Both errors render in the card: an editor hitting the owner-only endpoint
  // used to get nothing at all (PLAN-42).
  const addMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (userId: string) => eventTypeOwnersApi.add(slug, eventType.id, userId),
    onSuccess: () => {
      // The project prefix: this type's owners AND the list's project-wide
      // owners, which the Status column reads.
      qc.invalidateQueries({ queryKey: projectEventTypeOwnersKey(slug) })
      setSelectedUserId('')
    },
  })

  const removeMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (ownerId: string) => eventTypeOwnersApi.remove(slug, eventType.id, ownerId),
    onSuccess: () => qc.invalidateQueries({ queryKey: projectEventTypeOwnersKey(slug) }),
  })

  // Removing an owner changes who has to approve a merge, and the X sits 12px
  // from the email it belongs to — one mis-hit was enough (PLAN-42).
  const handleRemove = async (owner: EventTypeOwner) => {
    addMut.reset()
    removeMut.reset()
    const who = owner.user_name || owner.user_email
    const remaining = owners.length - 1
    const ok = await confirm({
      title: 'Remove owner',
      message:
        remaining > 0
          ? `Remove ${who} as an owner of ${eventType.display_name}? Merges touching this type will no longer need their approval.`
          : `Remove ${who} as an owner of ${eventType.display_name}? It has no other owner, so anyone will be able to merge changes to this type.`,
      confirmLabel: 'Remove',
      variant: 'danger',
    })
    if (ok) removeMut.mutate(owner.id)
  }

  const ownerUserIds = new Set(owners.map((o: EventTypeOwner) => o.user_id))
  const availableUsers = projectCandidates(members, users).filter((m) => !ownerUserIds.has(m.user_id))
  const ownerError = addMut.isError ? addMut.error : removeMut.isError ? removeMut.error : null

  return (
    <Panel
      className="mb-3"
      title="Owners"
      subtitle="Owners gate branch merges that touch this event type."
      right={
        <Chip tone="accent" size="xs">
          gates merge
        </Chip>
      }
    >
      {dialog}
      <div className="flex flex-col gap-3 px-4 py-3.5">
        {owners.length === 0 ? (
          <p className="m-0 text-body-sm text-fg-tertiary">
            No owners — anyone can merge a branch touching this type.
          </p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {owners.map((owner: EventTypeOwner) => (
              <span
                key={owner.id}
                className="inline-flex items-center gap-1.5 rounded-lg px-2 py-1 border border-border bg-background"
              >
                <UserAvatar name={owner.user_name || owner.user_email} size={18} />
                <span className="text-body-sm font-medium">{owner.user_name || owner.user_email}</span>
                <span className="mono text-micro text-fg-tertiary">
                  {owner.user_email}
                </span>
                {canWrite && (
                  <IconButton
                    label={`Remove owner ${owner.user_name || owner.user_email}`}
                    tooltip="Remove owner"
                    className={ROW_ICON_DANGER_CLASS}
                    disabled={removeMut.isPending}
                    onClick={() => { void handleRemove(owner) }}
                  >
                    <X className="size-3" />
                  </IconButton>
                )}
              </span>
            ))}
          </div>
        )}
        {canWrite && availableUsers.length > 0 && (
          <div className="flex flex-wrap gap-2">
            <div className="max-w-[320px] min-w-0 flex-1">
              <SSelect
                id={ownerSelectId}
                ariaLabel="New owner"
                value={selectedUserId}
                onChange={setSelectedUserId}
                options={[
                  { value: '', label: 'Select user…' },
                  ...availableUsers.map((m: ProjectCandidate) => ({
                    value: m.user_id,
                    label: `${m.name || m.email} · ${m.email}`,
                  })),
                ]}
              />
            </div>
            <Button
              variant="outline"
              size="sm"
              disabled={!selectedUserId || addMut.isPending}
              onClick={() => {
                removeMut.reset()
                addMut.mutate(selectedUserId)
              }}
            >
              <Plus className="size-3" />
              Add owner
            </Button>
          </div>
        )}
        {ownerError && (
          <p role="alert" className="m-0 text-body-sm text-destructive">
            {addMut.isError ? 'Could not add the owner' : 'Could not remove the owner'}:{' '}
            {getErrorMessage(ownerError)}
          </p>
        )}
      </div>
    </Panel>
  )
}

// ─────────────────── Shared page-style UI primitives ───────────────────
// Composed from design tokens; exported for EventTypeDetailView to reuse so the
// settings surface stays visually consistent without a separate shared module.
// Section cards are the kit `Panel` (DS-4): the local `SCard` copy is gone.

/**
 * The Cancel / submit pair of a short event-type form, for a `Panel`'s
 * `footer` slot (which draws the sunken bar around it).
 */
export function SaveFooter({
  onCancel,
  pending,
  disabled,
  status,
  submitLabel = 'Save changes',
}: {
  onCancel?: () => void
  pending?: boolean
  /** Nothing to save yet (e.g. the form is unchanged). */
  disabled?: boolean
  /** A short outcome line beside the button, such as "Saved". */
  status?: string
  submitLabel?: string
}) {
  return (
    <>
      <span role="status" className="mr-auto text-body-sm text-fg-tertiary">
        {status}
      </span>
      {onCancel && (
        <Button type="button" variant="outline" size="sm" onClick={onCancel}>
          Cancel
        </Button>
      )}
      <Button type="submit" size="sm" disabled={pending || disabled}>
        {submitLabel}
      </Button>
    </>
  )
}

/**
 * A labelled row of the event-type forms.
 *
 * The caption is a real `<label>` pointing at the row's control, and the hint
 * describes it. Both used to be plain text beside it, so every input here was
 * announced as an unlabeled "edit text" and clicking a label focused nothing
 * (PLAN-36). The id travels the way the settings kit's `Field` sends it: the
 * S* controls below (and ColorPicker) claim it through `useFieldControlId`, and
 * a row holding any other control passes `htmlFor` naming that control's id,
 * or `false` for a row with no single control, which is then named as a group.
 */
export function SField({
  label,
  hint,
  last,
  htmlFor,
  required,
  children,
}: {
  label: string
  hint?: string
  last?: boolean
  htmlFor?: string | false
  /** The kit's required mark beside the label; the control sets its own `aria-required`. */
  required?: boolean
  children: ReactNode
}) {
  const hintId = useId()
  // The kit Field row (DS-17): one implementation of the caption, the label
  // association, the phone stacking and the required / error wiring. This only
  // keeps the event-type forms' narrower caption column, and hands the hint's
  // id to the S* controls so they are described by it.
  return (
    <Field
      label={label}
      hint={hint ? <span id={hintId}>{hint}</span> : undefined}
      last={last}
      htmlFor={htmlFor}
      required={required}
      labelWidth={180}
    >
      <SFieldHintContext.Provider value={hint ? hintId : undefined}>{children}</SFieldHintContext.Provider>
    </Field>
  )
}

export function SInput({
  value,
  onChange,
  mono,
  placeholder,
  disabled,
  ariaLabel,
  invalid,
  describedBy,
  inputMode,
  id,
  required,
}: {
  value: string
  onChange: (v: string) => void
  mono?: boolean
  placeholder?: string
  disabled?: boolean
  ariaLabel?: string
  invalid?: boolean
  describedBy?: string
  inputMode?: React.HTMLAttributes<HTMLInputElement>['inputMode']
  id?: string
  required?: boolean
}) {
  const controlId = useFieldControlId(id)
  const hintId = useSFieldHintId()
  return (
    // The event form's control (AU-35): its edge, focus accent, faint
    // placeholder and 16px-on-phones text, so a type's settings and an
    // event's fields read as one form.
    <input
      id={controlId}
      className={cn(TEXT_INPUT_CLASS, 'max-w-[420px]', mono && 'mono')}
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      inputMode={inputMode}
      aria-required={required || undefined}
      aria-label={ariaLabel}
      aria-invalid={invalid || undefined}
      aria-describedby={describedByIds(describedBy, hintId)}
      onChange={(e) => onChange(e.target.value)}
    />
  )
}

/** A two-row textarea that takes its SField's label and hint. */
export function STextarea({
  value,
  onChange,
}: {
  value: string
  onChange: (v: string) => void
}) {
  const controlId = useFieldControlId()
  const hintId = useSFieldHintId()
  return (
    <Textarea
      id={controlId}
      value={value}
      rows={2}
      aria-describedby={hintId}
      onChange={(e) => onChange(e.target.value)}
    />
  )
}

export function SSelect({
  value,
  onChange,
  options,
  id,
  ariaLabel,
}: {
  value: string
  onChange: (v: string) => void
  options: { value: string; label: string }[]
  id?: string
  ariaLabel?: string
}) {
  const hintId = useSFieldHintId()
  // The kit's select (DS-9), capped at the text inputs' 420px so the column
  // keeps one right edge.
  return (
    <div className="max-w-[420px]">
      <NativeSelect
        id={id}
        width="fill"
        value={value}
        aria-label={ariaLabel}
        aria-describedby={hintId}
        onChange={onChange}
        options={options}
      />
    </div>
  )
}

function BackLink({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="mb-3.5 inline-flex items-center gap-1 text-caption transition-colors hover:text-[var(--fg)]"
      style={{ color: 'var(--fg-muted)' }}
    >
      <ArrowLeft className="size-3" />
      {label}
    </button>
  )
}

// The shared table's cells, in this page's denser type. `wideOnly` hides a
// low-value column below `md`, header and cells alike.
function Th({
  children,
  align,
  style,
  wideOnly,
}: {
  children?: ReactNode
  align?: 'right'
  style?: CSSProperties
  wideOnly?: boolean
}) {
  return (
    <TableHead
      scope="col"
      className={cn(
        'h-8 px-3.5 micro-label',
        align === 'right' && 'text-right',
        wideOnly && 'hidden md:table-cell',
      )}
      style={{ color: 'var(--fg-subtle)', ...style }}
    >
      {children}
    </TableHead>
  )
}

function Td({
  children,
  align,
  className,
  wideOnly,
}: {
  children?: ReactNode
  align?: 'right'
  className?: string
  wideOnly?: boolean
}) {
  return (
    <TableCell
      className={cn(
        'px-3.5 text-body-sm',
        align === 'right' && 'text-right',
        wideOnly && 'hidden md:table-cell',
        className,
      )}
    >
      {children}
    </TableCell>
  )
}

// A plain row. It used to be `<tr role="button">` wrapped around its own move,
// edit and delete buttons, which stripped the row and cell semantics (no column
// headers read out) and nested interactive content; the way in is now a link or
// button in the name cell (PLAN-39).
function ListRow({ children }: { children: ReactNode }) {
  return (
    <TableRow
      className="border-t border-b-0 hover:bg-[var(--surface-hover)] border-border-subtle"
    >
      {children}
    </TableRow>
  )
}

// The row actions' look on the shared IconButton (DS-12): small and quiet,
// darkening on hover AND keyboard focus — the old local button swapped its
// colour in JS `onMouseEnter`, so focus got nothing.
const ROW_ICON_CLASS =
  'size-6 text-[var(--fg-subtle)] hover:bg-transparent hover:text-[var(--fg)] focus-visible:text-[var(--fg)]'
const ROW_ICON_DANGER_CLASS =
  'size-6 text-[var(--fg-subtle)] hover:bg-transparent hover:text-[var(--danger)] focus-visible:text-[var(--danger)]'

