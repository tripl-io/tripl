import { Chip } from '@/components/primitives/chip'
import { Checkbox } from '@/components/ui/checkbox'
import type { ScanConfigPreview } from '@/types'
import { PropertyFieldPicker } from './PropertyFieldPicker'
import { isJsonPreviewType } from './scanUtils'

/** Why a column cannot be picked: the scan already uses it (#247). */
const RESERVED_TITLE = 'Used by this scan as its event type, time, app version or platform column.'

/**
 * The breakdown column checkboxes, and nothing else: the caption and its
 * explanation are the enclosing `Field` row's, and the value limit is a row of
 * its own, so every scan-form section shares one label column (#247).
 * That row names this group (`role="group"` + `aria-labelledby`); each box
 * keeps its own `aria-label`.
 */
export function MetricBreakdownPicker({
  columns,
  selectedColumns,
  eventTypeColumn,
  timeColumn,
  appVersionColumn,
  platformColumn,
  onToggleColumn,
  jsonColumns = [],
}: {
  columns: ScanConfigPreview['columns']
  selectedColumns: string[]
  eventTypeColumn: string
  timeColumn: string
  appVersionColumn: string
  platformColumn: string
  /** The preview's JSON columns: their paths can be picked as properties (F23). */
  jsonColumns?: ScanConfigPreview['json_columns']
  onToggleColumn: (column: string) => void
}) {
  const availableColumns = columns.filter(column => !isJsonPreviewType(column.type_name))
  const reservedColumns = new Set(
    [eventTypeColumn, timeColumn, appVersionColumn, platformColumn].filter(Boolean),
  )

  const properties = (
    <PropertyFieldPicker
      jsonColumns={jsonColumns}
      selected={selectedColumns}
      onToggle={onToggleColumn}
      ariaPrefix="Breakdown by"
    />
  )

  if (availableColumns.length === 0) {
    return (
      <>
        <p className="text-body-sm text-fg-tertiary">
          The preview has no plain-value columns to break down by.
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
                checked={selectedColumns.includes(column.name)}
                disabled={disabled}
                aria-label={`Breakdown by ${column.name}`}
                onCheckedChange={() => {
                  if (!disabled) onToggleColumn(column.name)
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
