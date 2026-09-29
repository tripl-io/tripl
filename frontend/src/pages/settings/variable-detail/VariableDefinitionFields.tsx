import { useId, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { variablesApi } from '@/api/variables'
import { variableOverridesApi } from '@/api/variableOverrides'
import { ChipListInput } from '@/components/chip-list-input'
import { FieldError } from '@/components/forms/FieldError'
import { invalidAria } from '@/components/forms/validation'
import { CodeToken } from '@/components/primitives/code-token'
import { NativeSelect } from '@/components/settings/kit'
import { ReadOnlyDefinition } from '@/components/states'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { variableOverridesKey, variableValuesKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type { Variable, VariableType } from '@/types'
import type { BindingExample } from '../bindingExample'
import { BindingVersusTokenNote } from '../VariablesBindingNote'
import {
  INVALID_BINDING_MESSAGE,
  isValidBinding,
  TYPE_LABELS,
  VARIABLE_TYPE_OPTIONS,
} from '../variablesShared'
import { invalidValuesFor } from '../variableValueValidation'
import type { VariableDefinitionDraft } from './useVariableDefinitionDraft'

function TokenList({ values }: { values: readonly string[] }) {
  if (values.length === 0) return null
  return (
    <span className="flex flex-wrap gap-1">
      {values.map((value) => (
        <CodeToken key={value} title={value}>{value}</CodeToken>
      ))}
    </span>
  )
}

/**
 * The warehouse paths the scan actually ANSWERED on, distinct and in first-seen
 * order. Not the same question as the bindings, which are what the plan ASKS
 * for: a path here that is missing there is the case worth seeing — the scan
 * reached this variable by name and the binding list is incomplete
 * (tripl-h2sx.30). Same query key as the Observed section, so no second request.
 */
function useObservedSourceColumns(slug: string, branchId: string | null, variableId: string) {
  const { data: contexts = [] } = useQuery({
    queryKey: variableValuesKey(slug, branchId, variableId),
    queryFn: () => variablesApi.values(slug, variableId, branchId),
  })
  return useMemo(
    () => [...new Set(contexts.map((context) => context.source_column).filter(Boolean))],
    [contexts],
  )
}

/**
 * A variable's definition: the form for a writer, a description list for a
 * viewer (#237 rule 4, tripl-i9mt.12). A viewer used to get the same inputs
 * under a disabled fieldset, with live borders and "Type a value, press Enter"
 * hints on controls that did nothing.
 *
 * Renders fields only; the caller owns the `<form>` and its Save.
 */
export function VariableDefinitionFields({
  slug,
  branchId,
  variable,
  draft,
  example,
  canWrite,
}: {
  slug: string
  branchId: string | null
  variable: Variable
  draft: VariableDefinitionDraft
  example: BindingExample
  canWrite: boolean
}) {
  const nameId = useId()
  const typeId = useId()
  const descriptionId = useId()
  const valuesId = useId()
  const bindingsId = useId()
  const observedSourceColumns = useObservedSourceColumns(slug, branchId, variable.id)
  const { data: overrides = [] } = useQuery({
    queryKey: variableOverridesKey(slug, branchId, variable.id),
    queryFn: () => variableOverridesApi.list(slug, variable.id, branchId),
  })

  const observedAt = observedSourceColumns.length > 0 && (
    <div className="flex flex-wrap items-center gap-1 text-caption text-fg-tertiary">
      <span>Observed at:</span>
      {observedSourceColumns.map((column) => (
        <code key={column} className="rounded-sm bg-muted px-1 font-mono">{column}</code>
      ))}
    </div>
  )

  if (!canWrite) {
    return (
      <ReadOnlyDefinition
        items={[
          { label: 'Name', value: <span className="mono">{variable.name}</span> },
          { label: 'Type', value: TYPE_LABELS[variable.variable_type] },
          { label: 'Description', value: variable.description },
          {
            label: 'Possible values (documented)',
            value: variable.allowed_values.length > 0 ? <TokenList values={variable.allowed_values} /> : null,
          },
          {
            label: 'Data bindings',
            value:
              variable.bindings.length > 0 || observedAt ? (
                <span className="grid gap-1">
                  <TokenList values={variable.bindings} />
                  {observedAt}
                </span>
              ) : null,
          },
        ]}
      />
    )
  }

  // Overrides are values too, and a type change strands them the same way
  // (review 204): distinct, in the order the overrides list them.
  const invalidOverrideValues = [
    ...new Set(invalidValuesFor(draft.type, overrides.flatMap((override) => override.values ?? []))),
  ]

  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-4">
      <div className="grid gap-2">
        <Label htmlFor={nameId}>Name</Label>
        <Input
          id={nameId}
          value={draft.name}
          onChange={(e) => draft.setName(e.target.value)}
          aria-required
          className="mono"
          {...invalidAria(nameId, draft.shownNameError)}
        />
        <FieldError inputId={nameId} message={draft.shownNameError} />
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="grid gap-2">
          <Label htmlFor={typeId}>Type</Label>
          <NativeSelect
            id={typeId}
            value={draft.type}
            onChange={(value) => draft.setType(value as VariableType)}
            options={VARIABLE_TYPE_OPTIONS}
          />
        </div>
        <div className="grid gap-2">
          <Label htmlFor={descriptionId}>Description</Label>
          <Input
            id={descriptionId}
            value={draft.description}
            onChange={(e) => draft.setDescription(e.target.value)}
          />
        </div>
      </div>
      <div className="grid gap-2">
        <Label htmlFor={valuesId}>Possible values (documented)</Label>
        <ChipListInput
          inputId={valuesId}
          values={draft.allowedValues}
          onChange={draft.setAllowedValues}
          placeholder="Type a value, press Enter"
          ariaLabel="Add possible value"
          {...draft.valueRule}
        />
        {draft.invalidValues.length > 0 && (
          <p role="alert" className="text-body-sm text-warning">
            Not valid for {TYPE_LABELS[draft.type]}: {draft.invalidValues.join(', ')}.
            {draft.typeChangeBlocked
              ? ' Remove them or keep the previous type before saving.'
              : ' Drift will never match these values.'}
          </p>
        )}
        {invalidOverrideValues.length > 0 && (
          // Overrides are saved on their own, so this warns rather than
          // holding Save; it names what a type change leaves stranded.
          <p role="alert" className="text-body-sm text-warning">
            Per-event overrides hold values not valid for {TYPE_LABELS[draft.type]}:{' '}
            {invalidOverrideValues.join(', ')}. Edit those overrides, or drift will never match them.
          </p>
        )}
      </div>
      <div className="grid gap-2">
        <Label htmlFor={bindingsId}>Data bindings</Label>
        <ChipListInput
          inputId={bindingsId}
          values={draft.bindings}
          onChange={draft.setBindings}
          placeholder={`e.g. ${example.binding}`}
          ariaLabel="Add data binding"
          validate={isValidBinding}
          invalidMessage={INVALID_BINDING_MESSAGE}
        />
        {observedAt}
        {/* Deliberately not "you can leave this empty", which is true of
            creation and misleading here: emptying a binding a scan filled in
            makes the row read as hand-owned to `_human_claim`, and it is then
            exempt from the retirement sweep for good. */}
        <p className="text-caption text-fg-tertiary">Needed only where the warehouse column or JSON path is spelled differently from the name; otherwise scans match on the name. A binding a scan filled in is how it keeps finding this property — removing it marks the property as yours, and retirement stops considering it.</p>
        <BindingVersusTokenNote example={example} />
      </div>
      {draft.updateMut.isError && (
        <p role="alert" className="text-body text-destructive">{getErrorMessage(draft.updateMut.error)}</p>
      )}
    </div>
  )
}
