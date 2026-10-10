import { useId, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { variablesApi } from '@/api/variables'
import type { VariableType } from '@/types'
import { Button } from '@/components/ui/button'
import { Dialog, DialogBody, DialogClose, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ChipListInput } from '@/components/chip-list-input'
import { FieldError } from '@/components/forms/FieldError'
import { examplePlaceholder } from '@/components/forms/placeholders'
import { REQUIRED_MESSAGE, focusFirstInvalid, invalidAria } from '@/components/forms/validation'
import { NativeSelect } from '@/components/settings/kit'
import { useDirtySinceOpen } from '@/hooks/useUnsavedChangesGuard'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { projectKey, variablesKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type { BindingExample } from './bindingExample'
import { BindingVersusTokenNote } from './VariablesBindingNote'
import {
  INVALID_BINDING_MESSAGE,
  VARIABLE_NAME_RULE_MESSAGE,
  VARIABLE_TYPE_OPTIONS,
  isValidBinding,
  isValidVariableName,
} from './variablesShared'
import { invalidValuesFor, valueRuleFor } from './variableValueValidation'

/**
 * The New variable dialog. It owns its form state, so typing in it re-renders
 * the dialog and not the whole variables page behind it. Mounted only
 * while open, which also resets the form each time it opens.
 */
export function VariablesCreateDialog({
  slug,
  branchId,
  example,
  onClose,
}: {
  slug: string
  branchId: string | null
  example: BindingExample
  onClose: () => void
}) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [varType, setVarType] = useState<VariableType>('string')
  const [description, setDescription] = useState('')
  const [allowedValues, setAllowedValues] = useState<string[]>([])
  const [bindings, setBindings] = useState<string[]>([])
  const nameId = useId()
  const typeId = useId()
  const descriptionId = useId()
  const valuesId = useId()
  const bindingsId = useId()

  const formRef = useRef<HTMLFormElement>(null)
  // Shown once Create was pressed: an empty form is not an invalid one.
  const [submitted, setSubmitted] = useState(false)
  const nameError = !name.trim()
    ? REQUIRED_MESSAGE
    : isValidVariableName(name)
      ? null
      : VARIABLE_NAME_RULE_MESSAGE
  const shownNameError = submitted ? nameError : null

  const valueRule = valueRuleFor(varType)
  // Values typed before the type changed are not re-checked by the chip input,
  // so the form says which of them the new type would refuse.
  const invalidValues = invalidValuesFor(varType, allowedValues)

  const createMut = useMutation({
    // Its error is rendered in the dialog.
    meta: SILENT_ERROR_META,
    mutationFn: () =>
      variablesApi.create(
        slug,
        { name, variable_type: varType, description, allowed_values: allowedValues, bindings },
        branchId,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: variablesKey(slug, branchId) })
      // The sidebar's Variables count reads the project summary, which the
      // variables list does not refresh: it kept the old number.
      qc.invalidateQueries({ queryKey: projectKey(slug) })
      onClose()
    },
  })

  // Mounted only while open, so the first render's form is the baseline.
  const dirty = useDirtySinceOpen(true, { name, varType, description, allowedValues, bindings })

  return (
    <Dialog open dirty={dirty} onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent>
        {/* noValidate: the name rule is said inline, not as the browser's
            "Please match the requested format.". Only the body scrolls,
            so the title and Create stay in view. */}
        <form
          ref={formRef}
          noValidate
          className="flex min-h-0 flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault()
            setSubmitted(true)
            if (nameError) {
              requestAnimationFrame(() => {
                if (formRef.current) focusFirstInvalid(formRef.current)
              })
              return
            }
            if (invalidValues.length === 0) createMut.mutate()
          }}
        >
          <DialogHeader><DialogTitle>New property</DialogTitle></DialogHeader>
          <DialogBody className="grid gap-4">
            <div className="grid gap-2">
              <Label htmlFor={nameId}>Name</Label>
              <Input
                id={nameId}
                value={name}
                onChange={e => setName(e.target.value)}
                aria-required
                placeholder={examplePlaceholder('spot_id')}
                className="mono"
                {...invalidAria(nameId, shownNameError)}
              />
              <FieldError inputId={nameId} message={shownNameError} />
            </div>
            {/* One column on phones: two side by side left each ~150px in a
                343px dialog. */}
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div className="grid gap-2">
                <Label htmlFor={typeId}>Type</Label>
                <NativeSelect
                  id={typeId}
                  value={varType}
                  onChange={value => setVarType(value as VariableType)}
                  options={VARIABLE_TYPE_OPTIONS}
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor={descriptionId}>Description</Label>
                <Input id={descriptionId} value={description} onChange={e => setDescription(e.target.value)} placeholder="Optional" />
              </div>
            </div>
            <div className="grid gap-2">
              <Label htmlFor={valuesId} optional>Possible values</Label>
              <ChipListInput
                inputId={valuesId}
                values={allowedValues}
                onChange={setAllowedValues}
                placeholder="Type a value, press Enter"
                ariaLabel="Add possible value"
                {...valueRule}
              />
              {invalidValues.length > 0 && (
                <p role="alert" className="text-body-sm text-destructive">
                  Not valid for this type: {invalidValues.join(', ')}. Remove them or pick another type.
                </p>
              )}
            </div>
            <div className="grid gap-2">
              {/* Three of the four fields here are optional and only Description
                  said so, which read as "the other two are not". Bindings least
                  of all: a scan matches a variable by NAME first, so a variable
                  named after its column needs none. */}
              <Label htmlFor={bindingsId} optional>Data bindings</Label>
              <ChipListInput inputId={bindingsId} values={bindings} onChange={setBindings} placeholder={`e.g. ${example.binding}`} ariaLabel="Add data binding" validate={isValidBinding} invalidMessage={INVALID_BINDING_MESSAGE} />
              <p className="text-caption text-fg-tertiary">Leave it empty and scans match this property by its name. Add a binding only when the warehouse column or JSON path is spelled differently.</p>
              <BindingVersusTokenNote example={example} />
            </div>
            {createMut.isError && <p className="text-body text-destructive">{getErrorMessage(createMut.error)}</p>}
          </DialogBody>
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline">Cancel</Button>
            </DialogClose>
            <Button type="submit" disabled={createMut.isPending || invalidValues.length > 0}>Create</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
