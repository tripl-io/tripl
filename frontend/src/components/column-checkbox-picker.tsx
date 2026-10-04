import { useState } from 'react'
import { Chip } from '@/components/primitives/chip'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'

/**
 * Up to this many columns the grid shows them all, with no inner scroll box
 * and its half-visible last row; past it a filter input narrows them.
 */
const FILTER_THRESHOLD = 18

interface ColumnCheckboxPickerProps {
  id?: string
  /** Known column names to offer as checkboxes. */
  columns: string[]
  /** Currently selected column names. */
  value: string[]
  onChange: (next: string[]) => void
  /** Columns shown disabled with a "reserved" badge (e.g. version/platform dims). */
  reserved?: string[]
  disabled?: boolean
  'aria-label'?: string
}

/**
 * Column picker mirroring the scans UI: a checkbox grid over the known columns
 * (ticking writes state immediately, so a selection can never be lost as an
 * uncommitted draft). A selected column that isn't in `columns` still renders
 * as a checked box, so a saved custom column stays visible and removable.
 *
 * Checkbox-only by design: the free-text "add a column" input
 * this grid used to embed duplicated the checkbox list in its suggestion
 * dropdown and bypassed the reserved-columns guard, so it was removed.
 */
export function ColumnCheckboxPicker({
  id,
  columns,
  value,
  onChange,
  reserved = [],
  disabled,
  'aria-label': ariaLabel,
}: ColumnCheckboxPickerProps) {
  const [filter, setFilter] = useState('')
  const reservedSet = new Set(reserved)
  const known = new Set(columns)
  const options = [...columns, ...value.filter(name => !known.has(name))]
  const filterable = options.length > FILTER_THRESHOLD
  const needle = filterable ? filter.trim().toLowerCase() : ''
  // A ticked column always stays in view, so filtering never hides a choice.
  const shown = needle
    ? options.filter(name => value.includes(name) || name.toLowerCase().includes(needle))
    : options

  const toggle = (name: string) => {
    if (reservedSet.has(name)) return
    onChange(value.includes(name) ? value.filter(column => column !== name) : [...value, name])
  }

  return (
    <div id={id} role="group" aria-label={ariaLabel}>
      {filterable && (
        <Input
          type="search"
          value={filter}
          onChange={event => setFilter(event.target.value)}
          // Inside a form: Enter would submit it. The filter applies as you type.
          onKeyDown={event => {
            if (event.key === 'Enter') event.preventDefault()
          }}
          placeholder={`Filter ${options.length} columns…`}
          aria-label="Filter columns"
          disabled={disabled}
          className="mb-2 max-w-[280px]"
        />
      )}
      {options.length > 0 ? (
        <div className="grid gap-1.5 sm:grid-cols-2 md:grid-cols-3">
          {shown.map(name => {
            const isReserved = reservedSet.has(name)
            return (
              <label
                key={name}
                className="flex items-center gap-2 rounded-control border bg-background p-2 text-body-sm border-border"
              >
                <Checkbox
                  checked={value.includes(name)}
                  disabled={disabled || isReserved}
                  aria-label={`Break down by ${name}`}
                  onCheckedChange={() => toggle(name)}
                />
                <span className="mono min-w-0 flex-1 truncate">{name}</span>
                {isReserved && (
                  <Chip size="xs" variant="outline">
                    reserved
                  </Chip>
                )}
              </label>
            )
          })}
        </div>
      ) : (
        <p className="text-body-sm text-fg-tertiary">
          No columns available yet.
        </p>
      )}
    </div>
  )
}
