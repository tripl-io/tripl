import { useId, useState } from "react"
import { Link } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ArrowRight, Link2, Pencil, Plus, Trash2 } from "lucide-react"
import { eventTypesApi } from "@/api/eventTypes"
import { relationsApi } from "@/api/relations"
import { useActiveBranchId } from "@/hooks/useBranch"
import { useDirtySinceOpen } from "@/hooks/useUnsavedChangesGuard"
import type { EventType, EventTypeRelation } from "@/types"
import { useConfirm } from "@/hooks/useConfirm"
import { Button } from "@/components/ui/button"
import { IconButton } from "@/components/ui/icon-button"
import { Dialog, DialogBody, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { PageContainer } from "@/components/primitives/page-container"
import { PageHeader } from "@/components/primitives/page-header"
import { Label } from "@/components/ui/label"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Skeleton } from "@/components/ui/skeleton"
import { EmptyState } from "@/components/empty-state"
import { ErrorState } from "@/components/error-state"
import { NativeSelect, Panel } from "@/components/settings/kit"
import { cn, getErrorMessage } from '@/lib/utils'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { eventTypesKey, relationsKey } from '@/lib/queryKeys'
import { useCanWriteProject } from '@/lib/permissions'
import { DisabledReason, ReadOnlyNotice, disabledReasonAria } from '@/components/states'
import { currentOrgSlug, projectPath } from '@/lib/navigation'
import { STICKY_ACTIONS_CLASS } from './stickyActions'
import { countOf } from '@/lib/plural'

/** A stored relation type in words: `belongs_to` -> "belongs to". */
const relationTypeLabel = (relationType: string) => relationType.replace(/_/g, ' ')

/** Why New relation is off: each end of a join is a field of an event type. */
const NEW_RELATION_BLOCKER = 'Create an event type with at least one field first.'

export function RelationsTab({ slug }: { slug: string }) {
  const qc = useQueryClient()
  const canWrite = useCanWriteProject()
  const branchId = useActiveBranchId()
  const [showForm, setShowForm] = useState(false)
  // The relation the dialog edits; null while it creates one.
  const [editing, setEditing] = useState<EventTypeRelation | null>(null)
  const [srcEtId, setSrcEtId] = useState('')
  const [tgtEtId, setTgtEtId] = useState('')
  const [srcFieldId, setSrcFieldId] = useState('')
  const [tgtFieldId, setTgtFieldId] = useState('')
  const { confirm, dialog } = useConfirm()

  const srcEtLabelId = useId()
  const tgtEtLabelId = useId()
  const srcFieldLabelId = useId()
  const tgtFieldLabelId = useId()

  const typesQuery = useQuery({
    queryKey: eventTypesKey(slug, branchId),
    queryFn: () => eventTypesApi.list(slug, branchId),
  })
  const relationsQuery = useQuery({
    queryKey: relationsKey(slug, branchId),
    queryFn: () => relationsApi.list(slug, branchId),
    // Rendered in the panel below, with a retry.
    meta: SILENT_ERROR_META,
  })
  const eventTypes = typesQuery.data ?? []
  const relations = relationsQuery.data ?? []

  // A relation joins two fields, and the backend allows both on one type: the
  // one prerequisite is a type with a field. Without one the dialog's selects
  // held nothing to pick, so New relation is off and the empty state points at
  // the page that fixes it.
  const canJoin = eventTypes.some((et: EventType) => et.field_definitions.length > 0)
  const joinBlocker = typesQuery.isSuccess && !canJoin ? NEW_RELATION_BLOCKER : null

  const srcEt = eventTypes.find((e: EventType) => e.id === srcEtId)
  const tgtEt = eventTypes.find((e: EventType) => e.id === tgtEtId)

  const closeForm = () => {
    setShowForm(false); setEditing(null)
    setSrcEtId(''); setTgtEtId(''); setSrcFieldId(''); setTgtFieldId('')
  }

  const saveMut = useMutation({
    // Its error is rendered in the dialog.
    meta: SILENT_ERROR_META,
    mutationFn: () => {
      const ends = {
        source_event_type_id: srcEtId, target_event_type_id: tgtEtId,
        source_field_id: srcFieldId, target_field_id: tgtFieldId,
      }
      return editing
        ? relationsApi.update(slug, editing.id, ends, branchId)
        : relationsApi.create(slug, ends, branchId)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: relationsKey(slug, branchId) })
      closeForm()
    },
  })

  const openCreate = () => {
    saveMut.reset()
    setEditing(null)
    setSrcEtId(''); setTgtEtId(''); setSrcFieldId(''); setTgtFieldId('')
    setShowForm(true)
  }

  // Edit in place: the same From/To dialog, seeded with the relation's ends,
  // so fixing a wrong field no longer means delete and re-create.
  const openEdit = (r: EventTypeRelation) => {
    saveMut.reset()
    setEditing(r)
    setSrcEtId(r.source_event_type_id); setSrcFieldId(r.source_field_id)
    setTgtEtId(r.target_event_type_id); setTgtFieldId(r.target_field_id)
    setShowForm(true)
  }

  // Picked ends ask before Escape or an outside click drops them.
  const dirty = useDirtySinceOpen(showForm, { srcEtId, tgtEtId, srcFieldId, tgtFieldId })

  const unchanged = editing !== null
    && editing.source_event_type_id === srcEtId && editing.source_field_id === srcFieldId
    && editing.target_event_type_id === tgtEtId && editing.target_field_id === tgtFieldId

  const deleteMut = useMutation({
    // Its error is rendered under the table.
    meta: SILENT_ERROR_META,
    mutationFn: (id: string) => relationsApi.del(slug, id, branchId),
    onSuccess: () => qc.invalidateQueries({ queryKey: relationsKey(slug, branchId) }),
  })

  const etMap: Record<string, EventType | undefined> = Object.fromEntries(
    eventTypes.map((e: EventType) => [e.id, e]),
  )
  // A relation IS its two fields: two relations between the same pair of types
  // differ only there, so a row (and a delete confirm) naming only the types
  // could not say which one it meant.
  const endpoint = (typeId: string, fieldId: string) => {
    const et = etMap[typeId]
    const fieldName = et?.field_definitions.find(f => f.id === fieldId)?.name
    return `${et?.name ?? '?'}.${fieldName ?? '?'}`
  }

  const handleDelete = async (r: EventTypeRelation) => {
    deleteMut.reset()
    const source = endpoint(r.source_event_type_id, r.source_field_id)
    const target = endpoint(r.target_event_type_id, r.target_field_id)
    const ok = await confirm({
      title: 'Delete relation',
      message: `Remove the relation ${source} → ${target}?`,
      confirmLabel: 'Delete',
      variant: 'danger',
    })
    if (ok) deleteMut.mutate(r.id)
  }

  return (
    <PageContainer className="space-y-4">
      {dialog}
      {/* The shared page header: the page had no title of its own,
          only the Panel's. "New relation" matches the dialog. */}
      <PageHeader
        eyebrow="Plan"
        title="Relations"
        description={<>Declare that a field on one event type refers to a field on another, so drift and coverage can follow the join (e.g. <span className="font-mono">click.screen_name → screen_view.screen_name</span>).</>}
        actions={
          canWrite && (
            // The reason New relation is off is a caption under it, not a
            // `title` a disabled button never shows (#237).
            <div className="flex flex-col items-end gap-1">
              <Button
                size="sm"
                disabled={!!joinBlocker}
                {...disabledReasonAria('new-relation', joinBlocker)}
                onClick={openCreate}
              >
                <Plus className="size-3.5" />New relation
              </Button>
              <DisabledReason id="new-relation" reason={joinBlocker} />
            </div>
          )
        }
      />
      {!canWrite && <ReadOnlyNotice />}

      {/* Create / edit dialog. Each end of the join is one group — its type, then
          its field — read top to bottom, with a live preview of the join
          instead of a 2x2 grid read diagonally. */}
      <Dialog open={showForm} dirty={dirty} onOpenChange={open => { if (!open) closeForm() }}>
        <DialogContent>
          <form className="flex min-h-0 flex-col gap-4" onSubmit={e => { e.preventDefault(); saveMut.mutate() }}>
            <DialogHeader>
              <DialogTitle>{editing ? 'Edit relation' : 'New relation'}</DialogTitle>
              <DialogDescription>
                {editing
                  ? 'Move either end of the join to another event type or field.'
                  : 'Say that a field on one event type holds the same value as a field on another, so drift and coverage can follow the join.'}
              </DialogDescription>
            </DialogHeader>
            <DialogBody className="grid gap-4">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <RelationEndpointFields
                  legend="From"
                  typeId={srcEtLabelId}
                  fieldId={srcFieldLabelId}
                  eventTypes={eventTypes}
                  selectedType={srcEt}
                  typeValue={srcEtId}
                  fieldValue={srcFieldId}
                  onTypeChange={value => { setSrcEtId(value); setSrcFieldId('') }}
                  onFieldChange={setSrcFieldId}
                />
                <RelationEndpointFields
                  legend="To"
                  typeId={tgtEtLabelId}
                  fieldId={tgtFieldLabelId}
                  eventTypes={eventTypes}
                  selectedType={tgtEt}
                  typeValue={tgtEtId}
                  fieldValue={tgtFieldId}
                  onTypeChange={value => { setTgtEtId(value); setTgtFieldId('') }}
                  onFieldChange={setTgtFieldId}
                />
              </div>
              <p className="text-body-sm text-fg-tertiary" aria-live="polite">
                {srcFieldId && tgtFieldId ? (
                  <>
                    Joins{' '}
                    <span className="font-mono text-foreground">{endpoint(srcEtId, srcFieldId)}</span>
                    {' → '}
                    <span className="font-mono text-foreground">{endpoint(tgtEtId, tgtFieldId)}</span>
                  </>
                ) : (
                  'Pick a field on each side to preview the join.'
                )}
              </p>
              {saveMut.isError && <p role="alert" className="text-body text-destructive">{getErrorMessage(saveMut.error)}</p>}
            </DialogBody>
            <DialogFooter>
              <DialogClose asChild>
                <Button type="button" variant="outline">Cancel</Button>
              </DialogClose>
              <Button type="submit" disabled={!srcFieldId || !tgtFieldId || unchanged || saveMut.isPending}>
                {editing ? 'Save' : 'Create'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Panel
        title="All relations"
        subtitle={relationsQuery.isPending
          ? 'Loading…'
          : countOf(relations.length, 'relation', 'relations')}
      >
        {relationsQuery.isError && relationsQuery.data !== undefined && (
          // A failed REFRESH keeps the rows on screen: replacing them with an
          // error would unmount whatever is being edited.
          <p role="alert" className="px-4 py-2 text-body-sm text-destructive">
            Couldn't refresh relations: {getErrorMessage(relationsQuery.error)}
          </p>
        )}
        {relationsQuery.isPending ? (
          // A pending list is not an empty one: "No relations yet" used to flash on
          // every cold load.
          <div className="space-y-2 px-4 py-4" aria-busy="true" aria-label="Loading relations">
            {Array.from({ length: 3 }, (_, index) => (
              <Skeleton key={index} className="h-9 w-full" />
            ))}
          </div>
        ) : relationsQuery.isError && relationsQuery.data === undefined ? (
          <div className="p-4">
            <ErrorState
              compact
              title="Couldn't load relations"
              error={relationsQuery.error}
              onRetry={() => { void relationsQuery.refetch() }}
              retryLabel="Retry"
            />
          </div>
        ) : relations.length > 0 ? (
          <>
            <Table>
              <TableHeader>
                <TableRow>
                  {/* One cell per join, so source and target read as one
                      thing instead of floating apart across a wide arrow
                      column. */}
                  <TableHead>Join</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead className={cn(STICKY_ACTIONS_CLASS, 'w-20')}><span className="sr-only">Actions</span></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {relations.map((r: EventTypeRelation) => {
                  const source = endpoint(r.source_event_type_id, r.source_field_id)
                  const target = endpoint(r.target_event_type_id, r.target_field_id)
                  return (
                    <TableRow key={r.id}>
                      {/* The join wraps rather than running under the pinned
                          actions: on a phone the target takes a line of its
                          own, and a long name breaks where it must. */}
                      <TableCell className="font-mono text-body-sm wrap-anywhere">
                        <span>{source}</span>
                        <span className="px-1.5 text-fg-tertiary" aria-hidden="true">→</span>
                        <span className="sr-only"> to </span>
                        <span className="max-sm:block">{target}</span>
                      </TableCell>
                      {/* The stored key, in words: "belongs_to" was a value
                          nobody chose in the dialog. */}
                      <TableCell className="text-fg-tertiary text-body-sm">{relationTypeLabel(r.relation_type)}</TableCell>
                      <TableCell className={STICKY_ACTIONS_CLASS}>
                        {canWrite && (
                          <div className="flex items-center justify-end gap-0.5">
                            <IconButton
                              variant="ghost"
                              className="h-7 w-7 text-fg-tertiary"
                              label={`Edit relation between ${source} and ${target}`}
                              onClick={() => openEdit(r)}
                            >
                              <Pencil className="h-3 w-3" aria-hidden="true" />
                            </IconButton>
                            <IconButton
                              variant="ghost"
                              className="h-7 w-7 text-fg-tertiary hover:text-destructive"
                              label={`Delete relation between ${source} and ${target}`}
                              disabled={deleteMut.isPending}
                              onClick={() => handleDelete(r)}
                            >
                              <Trash2 className="h-3 w-3" aria-hidden="true" />
                            </IconButton>
                          </div>
                        )}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
            {deleteMut.isError && (
              <p role="alert" className="px-4 py-2 text-body text-destructive">
                Could not delete the relation: {getErrorMessage(deleteMut.error)}
              </p>
            )}
          </>
        ) : (
          <div className="px-4 py-8">
            {/* Without a type that has a field there is nothing to join, so
                the empty state sends the reader to the page that fixes that
                instead of a dialog whose selects hold nothing. */}
            {joinBlocker ? (
              <EmptyState
                icon={Link2}
                title="No relations yet"
                description={`A relation joins a field on one event type to a field on another. ${NEW_RELATION_BLOCKER}`}
                action={canWrite ? (
                  <Button asChild size="sm">
                    <Link to={projectPath(currentOrgSlug(), slug, '/event-types')}>
                      Go to event types
                      <ArrowRight className="size-3.5" aria-hidden="true" />
                    </Link>
                  </Button>
                ) : undefined}
              />
            ) : (
              <EmptyState
                icon={Link2}
                title="No relations yet"
                description="A relation joins a field on one event type to a field on another, so drift and coverage can follow the join."
                action={canWrite ? (
                  <Button type="button" size="sm" onClick={openCreate}>
                    <Plus className="size-3.5" />Create your first relation
                  </Button>
                ) : undefined}
              />
            )}
          </div>
        )}
      </Panel>
    </PageContainer>
  )
}

/** One end of a relation: its event type, then a field of that type. */
function RelationEndpointFields({
  legend,
  typeId,
  fieldId,
  eventTypes,
  selectedType,
  typeValue,
  fieldValue,
  onTypeChange,
  onFieldChange,
}: {
  legend: 'From' | 'To'
  typeId: string
  fieldId: string
  eventTypes: EventType[]
  selectedType: EventType | undefined
  typeValue: string
  fieldValue: string
  onTypeChange: (value: string) => void
  onFieldChange: (value: string) => void
}) {
  return (
    <fieldset className="grid min-w-0 gap-2 rounded-card border border-border-subtle p-3">
      <legend className="px-1 text-body-sm font-semibold">{legend}</legend>
      <Label htmlFor={typeId}>Event type</Label>
      <NativeSelect
        id={typeId}
        aria-label={`${legend} event type`}
        value={typeValue}
        onChange={onTypeChange}
        options={[
          { value: '', label: 'Pick an event type' },
          ...eventTypes.map(et => ({ value: et.id, label: et.display_name })),
        ]}
      />
      <Label htmlFor={fieldId}>Field</Label>
      {/* Disabled until its type is set: enabled and empty, it offered a
          list with nothing in it. */}
      <NativeSelect
        id={fieldId}
        aria-label={`${legend} field`}
        value={fieldValue}
        disabled={!selectedType}
        onChange={onFieldChange}
        options={[
          { value: '', label: selectedType ? 'Pick a field' : 'Pick a type first' },
          ...(selectedType?.field_definitions ?? []).map(f => ({ value: f.id, label: f.display_name })),
        ]}
      />
    </fieldset>
  )
}
