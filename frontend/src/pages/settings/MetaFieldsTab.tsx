import { useId, useState } from "react"
import { Link } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { List, Pencil, Plus, Trash2, X } from "lucide-react"
import { metaFieldsApi } from "@/api/metaFields"
import { metaFieldDeleteMessage } from "./metaFieldDelete"
import { useActiveBranchId } from "@/hooks/useBranch"
import type { MetaFieldDefinition, MetaFieldUsage, Sensitivity } from "@/types"
import { SENSITIVITY_OPTIONS } from "@/types"
import { SensitivityChip } from "@/components/primitives/sensitivity-chip"
import { useConfirm } from "@/hooks/useConfirm"
import { Chip } from "@/components/primitives/chip"
import { PageContainer } from "@/components/primitives/page-container"
import { PageHeader } from "@/components/primitives/page-header"
import { FieldError } from "@/components/forms/FieldError"
import { examplePlaceholder } from "@/components/forms/placeholders"
import { REQUIRED_MESSAGE, focusFirstInvalid, invalidAria } from "@/components/forms/validation"
import { Button } from "@/components/ui/button"
import { IconButton } from "@/components/ui/icon-button"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Skeleton } from "@/components/ui/skeleton"
import { EmptyState } from "@/components/empty-state"
import { ErrorState } from "@/components/error-state"
import { NativeSelect, Panel } from "@/components/settings/kit"
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import {
  META_FIELD_LINK_EXAMPLE_KEY,
  META_FIELD_LINK_PLACEHOLDER,
  MULTI_VALUE_META_FIELD_TYPES,
  metaFieldLinkExample,
} from "@/lib/metaFields"
import { getErrorMessage } from '@/lib/utils'
import { useCanWriteProject } from '@/lib/permissions'
import { ReadOnlyNotice } from '@/components/states'
import { metaFieldsKey, projectMetaFieldsKey } from '@/lib/queryKeys'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

/**
 * A link template as it will be saved. `{value}` without the dollar sign is
 * what people type from memory (the seeded Jira template had it), and it
 * silently never resolved; it is taken to mean `${value}` (#244).
 */
function normalizeLinkTemplate(template: string): string {
  return template.trim().replace(/(?<!\$)\{value\}/g, META_FIELD_LINK_PLACEHOLDER)
}

const LINK_TEMPLATE_MISSING_VALUE = `Add ${META_FIELD_LINK_PLACEHOLDER} where the key goes, e.g. https://jira.example.com/browse/${META_FIELD_LINK_PLACEHOLDER}.`

/** Why a link template cannot be saved, or null. */
function linkTemplateError(enabled: boolean, template: string): string | null {
  if (!enabled) return null
  if (!template.trim()) return REQUIRED_MESSAGE
  if (!normalizeLinkTemplate(template).includes(META_FIELD_LINK_PLACEHOLDER)) {
    return LINK_TEMPLATE_MISSING_VALUE
  }
  return null
}

/**
 * The one helper line under a link template, the same in both dialogs: a live
 * preview of the link once the template can build one, the rule until then.
 */
function LinkTemplateHint({ id, template }: { id: string; template: string }) {
  const example = metaFieldLinkExample(normalizeLinkTemplate(template))
  return (
    <p id={id} className="min-w-0 break-words text-body-sm text-fg-tertiary">
      {example ? (
        <>
          Opens <span className="font-mono">{example}</span> for a stored value like{' '}
          <span className="font-mono">{META_FIELD_LINK_EXAMPLE_KEY}</span>.
        </>
      ) : (
        <>
          Put <span className="font-mono">{META_FIELD_LINK_PLACEHOLDER}</span> where the stored value
          goes. Stored values stay short, for example{' '}
          <span className="font-mono">{META_FIELD_LINK_EXAMPLE_KEY}</span>.
        </>
      )}
    </p>
  )
}

export function MetaFieldsTab({ slug }: { slug: string }) {
  const qc = useQueryClient()
  const canWrite = useCanWriteProject()
  const branchId = useActiveBranchId()
  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [fieldType, setFieldType] = useState('string')
  const [isRequired, setIsRequired] = useState(false)
  const [allowMultiple, setAllowMultiple] = useState(false)
  const [enumOptions, setEnumOptions] = useState<string[]>([])
  const [enumInput, setEnumInput] = useState('')
  const [defaultValue, setDefaultValue] = useState('')
  const [displayAsLink, setDisplayAsLink] = useState(false)
  const [linkTemplate, setLinkTemplate] = useState('')
  const [sensitivity, setSensitivity] = useState<Sensitivity>('none')
  const [editingMf, setEditingMf] = useState<MetaFieldDefinition | null>(null)
  const [editDisplayName, setEditDisplayName] = useState('')
  const [editFieldType, setEditFieldType] = useState('')
  const [editIsRequired, setEditIsRequired] = useState(false)
  const [editAllowMultiple, setEditAllowMultiple] = useState(false)
  const [editEnumOptions, setEditEnumOptions] = useState<string[]>([])
  const [editEnumInput, setEditEnumInput] = useState('')
  const [editDefaultValue, setEditDefaultValue] = useState('')
  const [editDisplayAsLink, setEditDisplayAsLink] = useState(false)
  const [editLinkTemplate, setEditLinkTemplate] = useState('')
  const [editSensitivity, setEditSensitivity] = useState<Sensitivity>('none')
  const { confirm, dialog } = useConfirm()
  // Inline validation shown once Create / Save was pressed: the forms
  // are noValidate, so an empty required field is flagged under itself
  // instead of by the browser's bubble on the first one only.
  const [createSubmitted, setCreateSubmitted] = useState(false)
  const [editSubmitted, setEditSubmitted] = useState(false)
  const createErrors = createSubmitted
    ? {
        name: name.trim() ? null : REQUIRED_MESSAGE,
        displayName: displayName.trim() ? null : REQUIRED_MESSAGE,
        linkTemplate: linkTemplateError(displayAsLink, linkTemplate),
      }
    : { name: null, displayName: null, linkTemplate: null }
  const editLinkError = editSubmitted ? linkTemplateError(editDisplayAsLink, editLinkTemplate) : null

  // IDs for create dialog form controls
  const createNameId = useId()
  const createDisplayNameId = useId()
  const createTypeId = useId()
  const createSensitivityId = useId()
  const createEnumOptionsId = useId()
  const createLinkTemplateId = useId()
  const createDefaultValueId = useId()

  // IDs for edit dialog form controls
  const editDisplayNameId = useId()
  const editTypeId = useId()
  const editDefaultValueId = useId()
  const editSensitivityId = useId()
  const editEnumOptionsId = useId()
  const editLinkTemplateId = useId()

  const metaFieldTypes = ['string', 'url', 'boolean', 'enum', 'date'] as const

  // Switching to `boolean` or `date` while the box is ticked would send a pair
  // the server rejects, so the type decides the VALUE sent, not whether it is
  // sent: the payload always carries `allow_multiple`, forced to false for a
  // type that cannot hold several values. Sending it beats omitting it — on an
  // update, omission would leave a previously-true flag standing while the type
  // moved to one that forbids it, which is the pair the server refuses. The
  // checkbox state itself is left alone, so switching back restores the tick.
  const canAllowMultiple = MULTI_VALUE_META_FIELD_TYPES.has(fieldType)
  const canEditAllowMultiple = MULTI_VALUE_META_FIELD_TYPES.has(editFieldType)

  const metaFieldsQuery = useQuery({
    queryKey: metaFieldsKey(slug, branchId),
    queryFn: () => metaFieldsApi.list(slug, branchId),
    // Rendered in the panel below, with a retry.
    meta: SILENT_ERROR_META,
  })
  const metaFields = metaFieldsQuery.data ?? []
  // The PROJECT prefix, not this branch's key. The branch review reads meta
  // fields under the shorter ['metaFields', slug] for its ticket link, and an
  // invalidation of the three-element key never matches it, so a fixed link
  // template kept rendering the old one there for up to a minute.
  // React Query matches by prefix, so this one call refreshes both.
  const invalidateMetaFields = () => qc.invalidateQueries({ queryKey: projectMetaFieldsKey(slug) })

  const createMut = useMutation({
    mutationFn: () => metaFieldsApi.create(slug, {
      name, display_name: displayName, field_type: fieldType, is_required: isRequired,
      allow_multiple: canAllowMultiple && allowMultiple,
      ...(fieldType === 'enum' && enumOptions.length > 0 ? { enum_options: enumOptions } : {}),
      ...(defaultValue ? { default_value: defaultValue } : {}),
      ...(displayAsLink ? { link_template: normalizeLinkTemplate(linkTemplate) || null } : {}),
      sensitivity,
    }, branchId),
    onSuccess: () => {
      invalidateMetaFields()
      setCreateSubmitted(false)
      setShowForm(false); setName(''); setDisplayName(''); setFieldType('string')
      setIsRequired(false); setAllowMultiple(false); setEnumOptions([]); setEnumInput(''); setDefaultValue('')
      setDisplayAsLink(false); setLinkTemplate(''); setSensitivity('none')
    },
  })

  const updateMut = useMutation({
    mutationFn: (id: string) => metaFieldsApi.update(slug, id, {
      display_name: editDisplayName, field_type: editFieldType as MetaFieldDefinition['field_type'], is_required: editIsRequired,
      allow_multiple: canEditAllowMultiple && editAllowMultiple,
      ...(editFieldType === 'enum' ? { enum_options: editEnumOptions } : { enum_options: null }),
      default_value: editDefaultValue || null,
      link_template: editDisplayAsLink ? (normalizeLinkTemplate(editLinkTemplate) || null) : null,
      sensitivity: editSensitivity,
    }, branchId),
    onSuccess: () => {
      invalidateMetaFields()
      setEditingMf(null)
    },
  })

  const deleteMut = useMutation({
    // Its error is rendered under the table.
    meta: SILENT_ERROR_META,
    mutationFn: (id: string) => metaFieldsApi.del(slug, id, branchId),
    onSuccess: invalidateMetaFields,
  })

  const handleDelete = async (mf: MetaFieldDefinition) => {
    deleteMut.reset()
    // The loss is counted when the usage call answers, and named when it does
    // not: a failed count must not block the delete.
    const usage: MetaFieldUsage | null = await metaFieldsApi.usage(slug, mf.id, branchId).then(
      result => result ?? null,
      () => null,
    )
    const ok = await confirm({
      title: 'Delete meta field',
      message: metaFieldDeleteMessage(mf.display_name, usage),
      confirmLabel: 'Delete meta field',
      variant: 'danger',
    })
    if (ok) deleteMut.mutate(mf.id)
  }

  const startEdit = (mf: MetaFieldDefinition) => {
    setEditSubmitted(false)
    setEditingMf(mf)
    setEditDisplayName(mf.display_name)
    setEditFieldType(mf.field_type)
    setEditIsRequired(mf.is_required)
    setEditAllowMultiple(Boolean(mf.allow_multiple))
    setEditEnumOptions(mf.enum_options ?? [])
    setEditEnumInput('')
    setEditDefaultValue(mf.default_value ?? '')
    setEditDisplayAsLink(Boolean(mf.link_template))
    setEditLinkTemplate(mf.link_template ?? '')
    setEditSensitivity(mf.sensitivity)
  }

  const addMetaEnumOption = (option: string, target: 'create' | 'edit') => {
    const trimmed = option.trim()
    if (!trimmed) return
    if (target === 'create') {
      if (!enumOptions.includes(trimmed)) setEnumOptions([...enumOptions, trimmed])
      setEnumInput('')
    } else {
      if (!editEnumOptions.includes(trimmed)) setEditEnumOptions([...editEnumOptions, trimmed])
      setEditEnumInput('')
    }
  }

  return (
    <PageContainer className="space-y-4">
      {dialog}
      {/* The shared page header: the page had no title of its own, only
          the Panel's. "New meta field" names the create action the way the
          dialog does. */}
      {/* "Meta fields", the name the button, the dialog and the event form
          already use; "Schema & fields" sent people looking for a type's
          schema here (#238). The description says how these
          differ from a type's own fields and links there. */}
      <PageHeader
        eyebrow="Plan"
        title="Meta fields"
        description={
          <>
            Extra attributes every event carries whatever its type: owner team, Jira ticket,
            review date. Per-type fields live on each{' '}
            <Link to={projectPath(currentOrgSlug(), slug, '/event-types')} className="text-accent no-underline hover:underline">
              event type
            </Link>
            .
          </>
        }
        actions={
          canWrite && (
            <Button size="sm" onClick={() => setShowForm(true)}>
              <Plus className="size-3.5" />New meta field
            </Button>
          )
        }
      />
      {!canWrite && <ReadOnlyNotice />}

      {/* Create dialog */}
      <Dialog open={showForm} onOpenChange={setShowForm}>
        <DialogContent>
          <form
            noValidate
            className="flex min-h-0 flex-col gap-4"
            onSubmit={e => {
              e.preventDefault()
              setCreateSubmitted(true)
              if (!name.trim() || !displayName.trim() || linkTemplateError(displayAsLink, linkTemplate)) {
                const form = e.currentTarget
                requestAnimationFrame(() => focusFirstInvalid(form))
                return
              }
              createMut.mutate()
            }}
          >
            <DialogHeader><DialogTitle>New meta field</DialogTitle></DialogHeader>
            <DialogBody className="grid gap-4">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="grid gap-2">
                  <Label htmlFor={createNameId}>Name</Label>
                  <Input id={createNameId} className="mono" value={name} onChange={e => setName(e.target.value)} aria-required placeholder={examplePlaceholder('jira_link')} {...invalidAria(createNameId, createErrors.name)} />
                  <FieldError inputId={createNameId} message={createErrors.name} />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor={createDisplayNameId}>Display name</Label>
                  <Input id={createDisplayNameId} value={displayName} onChange={e => setDisplayName(e.target.value)} aria-required placeholder={examplePlaceholder('Jira link')} {...invalidAria(createDisplayNameId, createErrors.displayName)} />
                  <FieldError inputId={createDisplayNameId} message={createErrors.displayName} />
                </div>
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <div className="grid gap-2">
                  <Label htmlFor={createTypeId}>Type</Label>
                  <NativeSelect id={createTypeId} value={fieldType} onChange={setFieldType} options={metaFieldTypes} />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor={createSensitivityId}>Sensitivity</Label>
                  <NativeSelect id={createSensitivityId} value={sensitivity} onChange={value => setSensitivity(value as Sensitivity)} options={SENSITIVITY_OPTIONS} />
                </div>
                <div className="flex flex-col justify-end gap-2 sm:pb-2">
                  <div className="flex items-center gap-2">
                    <Checkbox id="meta-req" checked={isRequired} onCheckedChange={c => setIsRequired(!!c)} />
                    <Label htmlFor="meta-req" className="cursor-pointer">Required</Label>
                  </div>
                  {canAllowMultiple && (
                    <div className="flex items-center gap-2">
                      <Checkbox id="meta-multi" checked={allowMultiple} onCheckedChange={c => setAllowMultiple(!!c)} />
                      <Label htmlFor="meta-multi" className="cursor-pointer">Multiple values</Label>
                    </div>
                  )}
                </div>
              </div>
              {fieldType === 'enum' && (
                <div className="grid gap-2">
                  <Label htmlFor={createEnumOptionsId}>Enum options</Label>
                  <div className="flex gap-2">
                    <Input id={createEnumOptionsId} value={enumInput} onChange={e => setEnumInput(e.target.value)}
                      onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addMetaEnumOption(enumInput, 'create') } }}
                      placeholder="Type option and press Enter" className="flex-1" />
                    <Button type="button" variant="outline" size="sm" onClick={() => addMetaEnumOption(enumInput, 'create')}>Add</Button>
                  </div>
                  {enumOptions.length > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {enumOptions.map(opt => (
                        <EnumOptionChip key={opt} option={opt} onRemove={() => setEnumOptions(enumOptions.filter(o => o !== opt))} />
                      ))}
                    </div>
                  )}
                </div>
              )}
              <div className="rounded-md border border-border/70 bg-muted/20 p-3">
                <div className="flex items-center gap-2">
                  <Checkbox id="meta-link-enabled" checked={displayAsLink} onCheckedChange={checked => setDisplayAsLink(Boolean(checked))} />
                  <Label htmlFor="meta-link-enabled" className="cursor-pointer">Display as link</Label>
                </div>
                {displayAsLink && (
                  <div className="mt-3 grid gap-2">
                    <Label htmlFor={createLinkTemplateId}>Link template</Label>
                    <Input
                      id={createLinkTemplateId}
                      value={linkTemplate}
                      onChange={e => setLinkTemplate(e.target.value)}
                      placeholder={examplePlaceholder(`https://tracker.example.com/issues/${META_FIELD_LINK_PLACEHOLDER}`)}
                      aria-required={displayAsLink}
                      {...invalidAria(createLinkTemplateId, createErrors.linkTemplate)}
                    />
                    <FieldError inputId={createLinkTemplateId} message={createErrors.linkTemplate} />
                    <LinkTemplateHint id={`${createLinkTemplateId}-hint`} template={linkTemplate} />
                  </div>
                )}
              </div>
              <div className="grid gap-2"><Label htmlFor={createDefaultValueId} optional>Default value</Label><Input id={createDefaultValueId} value={defaultValue} onChange={e => setDefaultValue(e.target.value)} /></div>
              {createMut.isError && <p className="text-body text-destructive">{getErrorMessage(createMut.error)}</p>}
            </DialogBody>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setShowForm(false)}>Cancel</Button>
              <Button type="submit" disabled={createMut.isPending}>Create</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Edit dialog */}
      <Dialog open={!!editingMf} onOpenChange={v => { if (!v) setEditingMf(null) }}>
        <DialogContent>
          <form
            noValidate
            className="flex min-h-0 flex-col gap-4"
            onSubmit={e => {
              e.preventDefault()
              setEditSubmitted(true)
              if (linkTemplateError(editDisplayAsLink, editLinkTemplate)) {
                const form = e.currentTarget
                requestAnimationFrame(() => focusFirstInvalid(form))
                return
              }
              if (editingMf) updateMut.mutate(editingMf.id)
            }}
          >
            {/* Named by what people read in the table, not the raw key. */}
            <DialogHeader className="pr-8"><DialogTitle>Edit meta field · {editingMf?.display_name}</DialogTitle></DialogHeader>
            <DialogBody className="grid gap-4">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="grid gap-2"><Label htmlFor={editDisplayNameId}>Display name</Label><Input id={editDisplayNameId} value={editDisplayName} onChange={e => setEditDisplayName(e.target.value)} /></div>
                <div className="grid gap-2">
                  <Label htmlFor={editTypeId}>Type</Label>
                  <NativeSelect id={editTypeId} value={editFieldType} onChange={setEditFieldType} options={metaFieldTypes} />
                </div>
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <div className="grid gap-2"><Label htmlFor={editDefaultValueId} optional>Default value</Label><Input id={editDefaultValueId} value={editDefaultValue} onChange={e => setEditDefaultValue(e.target.value)} placeholder="Optional" /></div>
                <div className="grid gap-2">
                  <Label htmlFor={editSensitivityId}>Sensitivity</Label>
                  <NativeSelect id={editSensitivityId} value={editSensitivity} onChange={value => setEditSensitivity(value as Sensitivity)} options={SENSITIVITY_OPTIONS} />
                </div>
                <div className="flex flex-col justify-end gap-2 sm:pb-2">
                  <div className="flex items-center gap-2">
                    <Checkbox id="edit-meta-req" checked={editIsRequired} onCheckedChange={c => setEditIsRequired(!!c)} />
                    <Label htmlFor="edit-meta-req" className="cursor-pointer">Required</Label>
                  </div>
                  {canEditAllowMultiple && (
                    <div className="flex items-center gap-2">
                      <Checkbox id="edit-meta-multi" checked={editAllowMultiple} onCheckedChange={c => setEditAllowMultiple(!!c)} />
                      <Label htmlFor="edit-meta-multi" className="cursor-pointer">Multiple values</Label>
                    </div>
                  )}
                </div>
              </div>
              {editingMf?.allow_multiple && !(canEditAllowMultiple && editAllowMultiple) && (
                <p className="text-body-sm text-warning">
                  Values already stored stay on their events. The next edit of one of those events
                  keeps the first value only.
                </p>
              )}
              {editFieldType === 'enum' && (
                <div className="grid gap-2">
                  <Label htmlFor={editEnumOptionsId}>Enum options</Label>
                  <div className="flex gap-2">
                    <Input id={editEnumOptionsId} value={editEnumInput} onChange={e => setEditEnumInput(e.target.value)}
                      onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addMetaEnumOption(editEnumInput, 'edit') } }}
                      placeholder="Type option and press Enter" className="flex-1" />
                    <Button type="button" variant="outline" size="sm" onClick={() => addMetaEnumOption(editEnumInput, 'edit')}>Add</Button>
                  </div>
                  {editEnumOptions.length > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {editEnumOptions.map(opt => (
                        <EnumOptionChip key={opt} option={opt} onRemove={() => setEditEnumOptions(editEnumOptions.filter(o => o !== opt))} />
                      ))}
                    </div>
                  )}
                </div>
              )}
              <div className="rounded-md border border-border/70 bg-muted/20 p-3">
                <div className="flex items-center gap-2">
                  <Checkbox id="edit-meta-link-enabled" checked={editDisplayAsLink} onCheckedChange={checked => setEditDisplayAsLink(Boolean(checked))} />
                  <Label htmlFor="edit-meta-link-enabled" className="cursor-pointer">Display as link</Label>
                </div>
                {editDisplayAsLink && (
                  <div className="mt-3 grid gap-2">
                    <Label htmlFor={editLinkTemplateId}>Link template</Label>
                    <Input
                      id={editLinkTemplateId}
                      value={editLinkTemplate}
                      onChange={e => setEditLinkTemplate(e.target.value)}
                      placeholder={examplePlaceholder(`https://tracker.example.com/issues/${META_FIELD_LINK_PLACEHOLDER}`)}
                      aria-required={editDisplayAsLink}
                      {...invalidAria(editLinkTemplateId, editLinkError)}
                    />
                    <FieldError inputId={editLinkTemplateId} message={editLinkError} />
                    <LinkTemplateHint id={`${editLinkTemplateId}-hint`} template={editLinkTemplate} />
                  </div>
                )}
              </div>
              {updateMut.isError && <p className="text-body text-destructive">{getErrorMessage(updateMut.error)}</p>}
            </DialogBody>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setEditingMf(null)}>Cancel</Button>
              <Button type="submit" disabled={updateMut.isPending}>Save</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Panel
        title="All meta fields"
        subtitle={metaFieldsQuery.isPending
          ? 'Loading…'
          : `${metaFields.length} field${metaFields.length === 1 ? '' : 's'}`}
      >
        {metaFieldsQuery.isError && metaFieldsQuery.data !== undefined && (
          // A failed REFRESH keeps the rows on screen: replacing them with an
          // error would unmount whatever is being edited.
          <p role="alert" className="px-4 py-2 text-body-sm text-destructive">
            Couldn't refresh meta fields: {getErrorMessage(metaFieldsQuery.error)}
          </p>
        )}
        {metaFieldsQuery.isPending ? (
          // A pending list is not an empty one: "No meta fields" used to flash
          // on every cold load and stay up on a 500.
          <div className="space-y-2 px-4 py-4" aria-busy="true" aria-label="Loading meta fields">
            {Array.from({ length: 3 }, (_, index) => (
              <Skeleton key={index} className="h-9 w-full" />
            ))}
          </div>
        ) : metaFieldsQuery.isError && metaFieldsQuery.data === undefined ? (
          <div className="p-4">
            <ErrorState
              compact
              title="Couldn't load meta fields"
              error={metaFieldsQuery.error}
              onRetry={() => { void metaFieldsQuery.refetch() }}
              retryLabel="Retry"
            />
          </div>
        ) : metaFields.length > 0 ? (
          <>
          <Table>
            <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Display</TableHead>
                <TableHead className="w-20">Type</TableHead>
                <TableHead className="w-24">Sensitivity</TableHead>
                <TableHead className="w-20">Required</TableHead>
                <TableHead>Default</TableHead>
                {/* Pinned right, so the row actions stay on screen on a
                    phone. */}
                <TableHead className="sticky right-0 w-24 bg-surface"><span className="sr-only">Actions</span></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {metaFields.map((mf: MetaFieldDefinition) => (
                <TableRow key={mf.id}>
                  <TableCell className="font-mono text-body-sm">{mf.name}</TableCell>
                  <TableCell className="text-body-sm">
                    <div className="space-y-1">
                      <div className="text-fg-tertiary">{mf.display_name}</div>
                      {mf.link_template && (
                        <div className="font-mono text-caption text-fg-tertiary/80">
                          Link: {mf.link_template}
                        </div>
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Chip variant="outline" size="xs">{mf.field_type}</Chip>
                    {mf.field_type === 'enum' && mf.enum_options && <span className="text-fg-tertiary text-micro ml-1">({mf.enum_options.length})</span>}
                    {mf.allow_multiple && <span className="text-fg-tertiary text-micro ml-1" title="Holds several values on one event">multi</span>}
                  </TableCell>
                  <TableCell>
                    <SensitivityChip value={mf.sensitivity} />
                  </TableCell>
                  <TableCell>{mf.is_required ? <span className="text-success font-medium text-body-sm">✓</span> : <span className="text-fg-tertiary">—</span>}</TableCell>
                  <TableCell className="text-body-sm text-fg-tertiary">{mf.default_value ?? '—'}</TableCell>
                  <TableCell className="sticky right-0 bg-surface">
                    {canWrite && (
                      <div className="flex gap-1 justify-end">
                        <IconButton variant="ghost" className="h-7 w-7" label={`Edit ${mf.display_name}`} onClick={() => startEdit(mf)}><Pencil className="h-3 w-3" aria-hidden="true" /></IconButton>
                        <IconButton variant="ghost" className="h-7 w-7 text-fg-tertiary hover:text-destructive" label={`Delete ${mf.display_name}`} disabled={deleteMut.isPending} onClick={() => handleDelete(mf)}><Trash2 className="h-3 w-3" aria-hidden="true" /></IconButton>
                      </div>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {deleteMut.isError && (
            <p role="alert" className="px-4 py-2 text-body text-destructive">
              Could not delete the meta field: {getErrorMessage(deleteMut.error)}
            </p>
          )}
          </>
        ) : (
          <div className="px-4 py-8">
            <EmptyState
              icon={List}
              title="No meta fields"
              description="Meta fields are attributes every event carries, like an owner team or a Jira ticket."
              action={canWrite ? (
                <Button type="button" size="sm" onClick={() => setShowForm(true)}>
                  <Plus className="size-3.5" />Create your first meta field
                </Button>
              ) : undefined}
            />
          </div>
        )}
      </Panel>
    </PageContainer>
  )
}

/**
 * An enum option with its remove button: the lucide X, not a bare "×" glyph,
 * in a hit area that grows on phones.
 */
function EnumOptionChip({ option, onRemove }: { option: string; onRemove: () => void }) {
  return (
    <Chip variant="outline" size="md" className="mono gap-1 pr-0.5 font-normal max-sm:h-8">
      {option}
      <button
        type="button"
        aria-label={`Remove option ${option}`}
        onClick={onRemove}
        className="grid size-5 place-items-center rounded-full text-fg-muted hover:bg-surface-hover hover:text-danger max-sm:size-7"
      >
        <X className="size-3" aria-hidden="true" />
      </button>
    </Chip>
  )
}

