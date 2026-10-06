import { Checkbox } from '@/components/ui/checkbox'
import type { ScanConfigPreview } from '@/types'
import { Field } from '@/components/settings/kit'
import { JSON_STRING_DB_TYPES, textColumnNames } from './scanSetupPreset'

/**
 * "Parse as JSON" (F23.9): tick the text columns that hold JSON, and the scan
 * reads them as JSON columns — their keys become properties, in the custom
 * setup and as the Event + properties preset's properties column alike.
 *
 * ClickHouse, BigQuery and Databricks only: the row is left out for any other source,
 * unless a column is ticked already, so a saved setting can still be cleared.
 */
export function JsonStringColumnsPicker({
  preview,
  selected,
  dbType,
  stale,
  onToggle,
}: {
  preview: ScanConfigPreview | null
  selected: string[]
  /** The data source's type; undefined before one is chosen. */
  dbType: string | undefined
  /** The preview was loaded with other columns parsed. */
  stale: boolean
  onToggle: (column: string) => void
}) {
  const supported = dbType === undefined || JSON_STRING_DB_TYPES.includes(dbType)
  if (!supported && selected.length === 0) return null
  const choices = textColumnNames(preview, selected)

  return (
    <Field
      label="Parse as JSON"
      htmlFor={false}
      hint="Text columns that hold JSON. A scan reads them as JSON columns, so every key becomes a property. A row whose text is not a JSON object counts as carrying no keys."
    >
      <div
        role="group"
        aria-label="Text columns to parse as JSON"
        data-testid="json-string-columns"
        className="flex flex-col gap-2"
      >
        {!supported && (
          <p className="m-0 text-body-sm text-danger" role="alert">
            Only ClickHouse, BigQuery and Databricks sources can parse text as JSON. Untick the columns
            below to save this scan.
          </p>
        )}
        {choices.length === 0 ? (
          <p className="m-0 text-body-sm text-fg-tertiary">
            {preview ? 'This query returns no text column.' : 'Load preview first to see the text columns.'}
          </p>
        ) : (
          <div className="grid gap-2 sm:grid-cols-2">
            {choices.map(column => (
              <label
                key={column}
                className="flex items-center gap-2 rounded-md border bg-background p-2 text-body"
              >
                <Checkbox
                  checked={selected.includes(column)}
                  aria-label={`Parse ${column} as JSON`}
                  onCheckedChange={() => onToggle(column)}
                />
                <span className="min-w-0 flex-1 truncate font-mono text-body-sm">{column}</span>
              </label>
            ))}
          </div>
        )}
        {stale && (
          <p className="m-0 text-body-sm text-fg-tertiary" data-testid="json-string-columns-stale">
            Reload the preview to read the ticked columns as JSON and see their keys.
          </p>
        )}
      </div>
    </Field>
  )
}
