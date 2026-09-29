import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import type { ScanConfigPreview } from '@/types'
import {
  MAX_PROPERTY_FIELDS,
  countPropertyFields,
  isValidPropertyField,
  propertyOptions,
} from './propertyFields'

/**
 * The property half of the breakdown and distribution-drift pickers (F23 #306):
 * JSON paths, picked from the paths the preview discovered or typed as
 * `<json_column>.<path>`. The warehouse extracts each one per row, so the list
 * is capped like the backend caps it.
 */
export function PropertyFieldPicker({
  jsonColumns,
  selected,
  onToggle,
  ariaPrefix,
}: {
  jsonColumns: ScanConfigPreview['json_columns']
  selected: string[]
  onToggle: (entry: string) => void
  /** Each checkbox's accessible name starts with this, e.g. "Breakdown by". */
  ariaPrefix: string
}) {
  const [draft, setDraft] = useState('')
  if (jsonColumns.length === 0) return null

  const options = propertyOptions(jsonColumns, selected)
  const atCap = countPropertyFields(selected) >= MAX_PROPERTY_FIELDS
  const trimmed = draft.trim()
  const jsonColumnNames = new Set(jsonColumns.map(column => column.column))
  const draftColumn = trimmed.split('.')[0] ?? ''
  const draftError = !trimmed
    ? null
    : !isValidPropertyField(trimmed)
      ? 'Use <json_column>.<path>: letters, digits and underscores, dot-separated.'
      : !jsonColumnNames.has(draftColumn)
        ? `${draftColumn} is not a JSON column of this query.`
        : null
  const canAdd = Boolean(trimmed) && !draftError && !atCap && !selected.includes(trimmed)

  return (
    <div className="mt-3 space-y-2" data-testid="property-field-picker">
      <div className="text-body-sm font-medium text-fg-secondary">Properties</div>
      {options.length === 0 ? (
        <p className="text-body-sm text-fg-tertiary">
          No JSON paths discovered yet. Discover JSON keys under Event naming, or add a path below.
        </p>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2">
          {options.map(option => {
            const checked = selected.includes(option.fullPath)
            const disabled = !checked && atCap
            return (
              <label
                key={option.fullPath}
                className="flex items-start gap-2 rounded-md border bg-background p-2 text-body"
              >
                <Checkbox
                  checked={checked}
                  disabled={disabled}
                  aria-label={`${ariaPrefix} ${option.fullPath}`}
                  onCheckedChange={() => {
                    if (!disabled) onToggle(option.fullPath)
                  }}
                />
                <span className="min-w-0 flex-1 space-y-0.5">
                  <span className="block truncate font-mono text-body-sm">{option.fullPath}</span>
                  {option.sampleValues.length > 0 && (
                    <span className="block truncate text-body-sm text-fg-tertiary">
                      sample: {option.sampleValues.join(', ')}
                    </span>
                  )}
                </span>
              </label>
            )
          })}
        </div>
      )}
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <Input
            value={draft}
            onChange={event => setDraft(event.target.value)}
            placeholder="json_column.path"
            className="font-mono"
            aria-label={`${ariaPrefix} property path`}
            aria-invalid={draftError ? true : undefined}
            onKeyDown={event => {
              if (event.key === 'Enter') {
                event.preventDefault()
                if (canAdd) {
                  onToggle(trimmed)
                  setDraft('')
                }
              }
            }}
          />
          {draftError && <p className="mt-1 text-body-sm text-destructive">{draftError}</p>}
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={!canAdd}
          onClick={() => {
            onToggle(trimmed)
            setDraft('')
          }}
        >
          Add
        </Button>
      </div>
      {atCap && (
        <p className="text-body-sm text-fg-tertiary">
          At most {MAX_PROPERTY_FIELDS} properties: each one is read out of the JSON on every row.
        </p>
      )}
    </div>
  )
}
