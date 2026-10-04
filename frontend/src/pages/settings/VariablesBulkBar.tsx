import { useId, useState, type ReactNode } from 'react'
import { ChevronDown, Trash2, X } from 'lucide-react'

import type { VariableType } from '@/types'
import { Button } from '@/components/ui/button'
import { IconButton } from '@/components/ui/icon-button'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { NativeSelect } from '@/components/settings/kit'
import { getErrorMessage } from '@/lib/utils'
import { VALUE_LIST_HINT, splitValueList } from './variableValueValidation'

type BulkMenu = 'type' | 'description' | 'values'

/**
 * One bulk verb: a bar button that opens a small popover with one field and
 * its Apply. The bar used to carry all three fields inline — "Set type… [Set
 * type] | Set description… [Apply] | Add values (comma-sep… [Add values]" —
 * six controls for three operations, with the placeholder cut short.
 */
function BulkPopover({
  label,
  open,
  onOpenChange,
  disabled,
  children,
}: {
  label: string
  open: boolean
  onOpenChange: (open: boolean) => void
  disabled: boolean
  children: ReactNode
}) {
  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button type="button" size="sm" variant="outline" disabled={disabled}>
          {label}
          <ChevronDown aria-hidden="true" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="start"
        className="w-72 space-y-2 p-3"
        // The type and values verbs confirm first. The confirm takes focus, and
        // closing the popover on that would unmount the draft's field while the
        // operator is still deciding; Escape or a click outside still close it.
        onFocusOutside={e => e.preventDefault()}
      >
        {children}
      </PopoverContent>
    </Popover>
  )
}

export function VariablesBulkBar({
  selectedCount,
  isPending,
  error,
  typeLabels,
  onSetType,
  onSetDescription,
  onAddValues,
  onDelete,
  onClear,
}: {
  selectedCount: number
  isPending: boolean
  /** The last bulk action's failure, shown in the bar. */
  error: unknown
  typeLabels: Record<VariableType, string>
  /** Confirms, then applies; resolves true once the type has changed. */
  onSetType: (variableType: VariableType) => Promise<boolean>
  onSetDescription: (description: string) => Promise<unknown>
  /** Checks the values against each selected type first; resolves false when
   * the operator backed out, so the draft stays. */
  onAddValues: (values: string[]) => Promise<boolean>
  onDelete: () => void
  onClear: () => void
}) {
  const [typeDraft, setTypeDraft] = useState<VariableType | ''>('')
  const [description, setDescription] = useState('')
  const [valuesDraft, setValuesDraft] = useState('')
  const [openMenu, setOpenMenu] = useState<BulkMenu | null>(null)
  const valuesHintId = useId()
  if (selectedCount === 0) return null

  const menuProps = (menu: BulkMenu) => ({
    open: openMenu === menu,
    onOpenChange: (open: boolean) => setOpenMenu(open ? menu : null),
    disabled: isPending,
  })

  // Drafts are dropped once the change has LANDED, and the popover closes with
  // them. They used to be cleared on click, so a 403, a stale-id 404 or a 422
  // threw away what was typed and said nothing; a failure now leaves
  // the draft for another try and the reason in the bar. A rejection is already
  // rendered, so it is swallowed. `false` means the operator cancelled a
  // confirm: nothing changed, so the draft stays too.
  const clearOnSuccess = (action: Promise<unknown>, clear: () => void) => {
    action.then(result => {
      if (result !== false) {
        clear()
        setOpenMenu(null)
      }
    }, () => undefined)
  }

  const applyType = () => {
    if (!typeDraft) return
    clearOnSuccess(onSetType(typeDraft), () => setTypeDraft(''))
  }

  const addValues = () => {
    const values = splitValueList(valuesDraft)
    if (values.length === 0) return
    clearOnSuccess(onAddValues(values), () => setValuesDraft(''))
  }

  const applyDescription = () => {
    const next = description.trim()
    if (!next) return
    clearOnSuccess(onSetDescription(next), () => setDescription(''))
  }

  return (
    <div
      role="region"
      aria-label="Bulk actions"
      // Centred with both insets and a capped width. `left-1/2 -translate-x-1/2`
      // sized the bar to the half of the viewport right of centre, so ~700px of
      // controls wrapped into two or three rows below ~1400px and into a 187px
      // column on a phone.
      className="fixed inset-x-4 bottom-[18px] z-(--z-bar) mx-auto flex w-fit max-w-[calc(100vw-2rem)] flex-col gap-1.5 rounded-card border py-1.5 pl-3.5 pr-2"
      style={{
        background: 'var(--bg-elevated)',
        borderColor: 'var(--border-strong)',
        boxShadow: 'var(--shadow-lg)',
      }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-body-sm text-fg-secondary">
          <span className="tnum font-semibold text-fg">{selectedCount}</span> selected
        </span>
        {/* Staged, then applied with a confirm. Changing the select used to
            retype the whole selection at once, and arrowing through a closed
            select fires a change per option. */}
        <BulkPopover label="Set type…" {...menuProps('type')}>
          <NativeSelect
            aria-label="Bulk set type"
            value={typeDraft}
            disabled={isPending}
            onChange={value => setTypeDraft(value as VariableType | '')}
            options={[
              { value: '', label: 'Choose a type…' },
              ...(Object.keys(typeLabels) as VariableType[]).map(t => ({ value: t, label: typeLabels[t] })),
            ]}
          />
          <div className="flex justify-end">
            <Button type="button" size="sm" disabled={isPending || !typeDraft} onClick={applyType}>
              Set type
            </Button>
          </div>
        </BulkPopover>
        <BulkPopover label="Set description…" {...menuProps('description')}>
          <Input
            aria-label="Bulk description"
            placeholder="Description for every selected property"
            value={description}
            onChange={e => setDescription(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); applyDescription() } }}
          />
          <div className="flex justify-end">
            <Button type="button" size="sm" disabled={isPending || !description.trim()} onClick={applyDescription}>
              Apply
            </Button>
          </div>
        </BulkPopover>
        <BulkPopover label="Add values…" {...menuProps('values')}>
          <Input
            aria-label="Bulk add values"
            placeholder="Values, comma-separated"
            aria-describedby={valuesHintId}
            value={valuesDraft}
            onChange={e => setValuesDraft(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addValues() } }}
          />
          <p id={valuesHintId} className="text-caption text-fg-tertiary">{VALUE_LIST_HINT}</p>
          <div className="flex justify-end">
            <Button type="button" size="sm" disabled={isPending || !valuesDraft.trim()} onClick={addValues}>
              Add values
            </Button>
          </div>
        </BulkPopover>
        <Button type="button" size="sm" variant="danger" disabled={isPending} onClick={onDelete}>
          <Trash2 aria-hidden="true" />
          Delete
        </Button>
        <IconButton type="button" variant="ghost" size="icon-sm" label="Clear selection" disabled={isPending} onClick={onClear}>
          <X aria-hidden="true" />
        </IconButton>
      </div>
      {error != null && (
        <p role="alert" className="text-body-sm text-destructive">
          The bulk change failed: {getErrorMessage(error)}
        </p>
      )}
    </div>
  )
}
