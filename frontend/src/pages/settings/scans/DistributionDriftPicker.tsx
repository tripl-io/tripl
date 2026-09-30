import { Chip } from '@/components/primitives/chip'
import { Checkbox } from '@/components/ui/checkbox'
import type { ScanConfigPreview } from '@/types'
import { PropertyFieldPicker } from './PropertyFieldPicker'
import { isJsonPreviewType } from './scanUtils'

const RESERVED_TITLE = 'Used by this scan as its event type, time, app version or platform column.'

/**
 * The drift column checkboxes. The caption and its explanation belong to the
 * enclosing `Field` row, which also names this group, so the section shares the
 * form's one label column (#247 DA-12).
 */
export function DistributionDriftPicker({
  columns,
  selectedFields,
  eventTypeColumn,
  timeColumn,
  appVersionColumn,
  platformColumn,
  onToggleField,
  jsonColumns = [],
}: {
  columns: ScanConfigPreview['columns']
  selectedFields: string[]
  eventTypeColumn: string
  timeColumn: string
  appVersionColumn: string
  platformColumn: string
  /** The preview's JSON columns: their paths can be picked as properties (F23). */
  jsonColumns?: ScanConfigPreview['json_columns']
  onToggleField: (field: string) => void
}) {
  const availableColumns = columns.filter(column => !isJsonPreviewType(column.type_name))
  const reservedColumns = new Set(
    [eventTypeColumn, timeColumn, appVersionColumn, platformColumn].filter(Boolean),
  )

  const properties = (
    <PropertyFieldPicker
      jsonColumns={jsonColumns}
      selected={selectedFields}
      onToggle={onToggleField}
      ariaPrefix="Distribution"
    />
  )

  if (availableColumns.length === 0) {
    return (
      <>
        <p className="text-body-sm text-fg-tertiary">
          The preview has no plain-value columns to watch.
        </p>
        {properties}
      </>
    )
  }

  return (
    <>
      <div className="grid gap-2 sm:grid-cols-2">
        {availableColumns.map(column => {
          const disabled = reservedColumns.has(column.name)
          return (
            <label
              key={column.name}
              className="flex items-center gap-2 rounded-md border bg-background p-2 text-body"
            >
              <Checkbox
                checked={selectedFields.includes(column.name)}
                disabled={disabled}
                aria-label={`Distribution ${column.name}`}
                onCheckedChange={() => {
                  if (!disabled) onToggleField(column.name)
                }}
              />
              <span className="min-w-0 flex-1 truncate font-mono text-body-sm">{column.name}</span>
              {disabled && (
                <Chip variant="outline" size="xs" title={RESERVED_TITLE}>
                  reserved
                </Chip>
              )}
            </label>
          )
        })}
      </div>
      {properties}
    </>
  )
}
