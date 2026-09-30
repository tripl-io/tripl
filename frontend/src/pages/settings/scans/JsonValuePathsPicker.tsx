import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { ErrorState } from '@/components/error-state'
import type { ScanConfigPreview } from '@/types'
import { jsonColumnsWithSelectedPaths } from './scanUtils'

/**
 * Picks which nested JSON paths stay real values instead of becoming variables.
 *
 * Split out of ScanPreviewPanel: it is an advanced choice that belongs with the
 * other event-naming controls behind progressive disclosure, while the preview
 * panel itself sits in the always-visible essentials block.
 */
export function JsonValuePathsPicker({
  preview,
  selectedJsonValuePaths,
  onToggleJsonValuePath,
  onDiscoverJsonPaths,
  isDiscoveringJsonPaths,
  jsonPathsError,
  jsonPathsDiscovered,
}: {
  preview: ScanConfigPreview
  selectedJsonValuePaths: string[]
  onToggleJsonValuePath: (path: string) => void
  onDiscoverJsonPaths: () => void
  isDiscoveringJsonPaths: boolean
  jsonPathsError?: unknown
  jsonPathsDiscovered: boolean
}) {
  const jsonColumns = jsonColumnsWithSelectedPaths(preview, selectedJsonValuePaths)
  const hasAnyPaths = jsonColumns.some(column => column.paths.length > 0)

  if (preview.json_columns.length === 0) return null

  return (
    <div className="space-y-3 rounded-lg border bg-muted/20 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-body font-medium">JSON values to keep as-is</div>
          <p className="text-body-sm text-fg-tertiary">
            Selected paths stay as real values in generated JSON. Unselected paths become properties.
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onDiscoverJsonPaths}
          disabled={isDiscoveringJsonPaths}
        >
          {isDiscoveringJsonPaths
            ? 'Discovering…'
            : jsonPathsDiscovered
              ? 'Re-scan JSON keys'
              : 'Discover JSON keys'}
        </Button>
      </div>

      {jsonPathsError != null && (
        <ErrorState compact title="JSON key discovery failed" error={jsonPathsError} />
      )}

      {hasAnyPaths ? (
        <div className="space-y-3">
          {jsonColumns.map(jsonColumn => (
            <div key={jsonColumn.column} className="space-y-2">
              <div className="text-body-sm font-semibold uppercase tracking-wide text-fg-tertiary">
                {jsonColumn.column}
              </div>
              {jsonColumn.paths.length === 0 ? (
                <div className="text-body-sm text-fg-tertiary">No nested keys found in the sampled rows.</div>
              ) : (
                <div className="grid gap-2">
                  {jsonColumn.paths.map(path => (
                    <label
                      key={path.full_path}
                      className="flex items-start gap-2 rounded-md border bg-background p-2 text-body"
                    >
                      <Checkbox
                        checked={selectedJsonValuePaths.includes(path.full_path)}
                        onCheckedChange={() => onToggleJsonValuePath(path.full_path)}
                        aria-label={`Keep JSON path ${path.path} as value`}
                      />
                      <span className="space-y-1">
                        <span className="block font-mono text-body-sm">{path.path}</span>
                        {path.sample_values.length > 0 && (
                          <span className="block text-body-sm text-fg-tertiary">
                            sample: {path.sample_values.join(', ')}
                          </span>
                        )}
                      </span>
                    </label>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div className="text-body-sm text-fg-tertiary">
          {jsonPathsDiscovered
            ? 'No nested JSON keys found in the sampled rows.'
            : 'Discover JSON keys to choose which nested values to keep as-is.'}
        </div>
      )}
    </div>
  )
}
