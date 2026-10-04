import { Input } from '@/components/ui/input'
import { Field, NativeSelect, type SelectOption } from '@/components/settings/kit'
import { fieldErrorId } from '@/lib/fieldErrors'
import type { ScanConfigPreview } from '@/types'
import { isJsonPreviewType } from './scanUtils'

/**
 * A column picker's options: the empty choice, a saved column the preview no
 * longer lists (kept so the select does not silently show another value), then
 * the preview's columns.
 */
function columnOptions(
  emptyLabel: string,
  savedMissing: string | null,
  available: { name: string }[],
): SelectOption[] {
  return [
    { value: '', label: emptyLabel },
    ...(savedMissing ? [savedMissing] : []),
    ...available.map(column => column.name),
  ]
}

export function AppVersionFields({
  columns,
  appVersionColumn,
  prereleasePattern,
  activeShareMin,
  platformColumn,
  onAppVersionColumnChange,
  onPrereleasePatternChange,
  onActiveShareMinChange,
  onPlatformColumnChange,
  activeShareMinError,
}: {
  columns: ScanConfigPreview['columns'] | null
  appVersionColumn: string
  prereleasePattern: string
  activeShareMin: string
  platformColumn: string
  onAppVersionColumnChange: (column: string) => void
  onPrereleasePatternChange: (value: string) => void
  onActiveShareMinChange: (value: string) => void
  onPlatformColumnChange: (column: string) => void
  /** Why the share above cannot be saved. */
  activeShareMinError?: string
}) {
  const availableColumns = columns?.filter(column => !isJsonPreviewType(column.type_name)) ?? []
  const hasSelectedColumn = Boolean(appVersionColumn)
  const selectedColumnIsAvailable = availableColumns.some(column => column.name === appVersionColumn)
  const selectDisabled = !columns && !hasSelectedColumn

  const hasSelectedPlatform = Boolean(platformColumn)
  const selectedPlatformIsAvailable = availableColumns.some(column => column.name === platformColumn)
  const platformSelectDisabled = !columns && !hasSelectedPlatform

  // One label column with the rest of the form: `Field` rows, like the
  // essentials card, instead of stacked labels in a two-column grid (#247).
  return (
    <>
      <Field label="App version column" htmlFor="app-version-column">
        <NativeSelect
          id="app-version-column"
          width="fill"
          value={appVersionColumn}
          onChange={onAppVersionColumnChange}
          disabled={selectDisabled}
          options={columnOptions(
            columns || hasSelectedColumn ? 'No app version' : 'Load preview first',
            hasSelectedColumn && !selectedColumnIsAvailable ? appVersionColumn : null,
            availableColumns,
          )}
        />
      </Field>
      <Field label="Platform column" htmlFor="platform-column">
        <NativeSelect
          id="platform-column"
          width="fill"
          value={platformColumn}
          onChange={onPlatformColumnChange}
          disabled={platformSelectDisabled}
          options={columnOptions(
            columns || hasSelectedPlatform ? 'No platform' : 'Load preview first',
            hasSelectedPlatform && !selectedPlatformIsAvailable ? platformColumn : null,
            availableColumns,
          )}
        />
      </Field>
      <Field
        label="Pre-release version pattern"
        htmlFor="app-version-prerelease-pattern"
        hint="Regex marking beta builds, e.g. -(beta|rc). Matching versions stay out of release comparisons."
      >
        <Input
          id="app-version-prerelease-pattern"
          type="text"
          value={prereleasePattern}
          onChange={e => onPrereleasePatternChange(e.target.value)}
          disabled={!appVersionColumn}
          placeholder={appVersionColumn ? 'e.g. -(beta|rc)' : 'Select version column'}
        />
      </Field>
      <Field
        label="Traffic share that counts as released"
        htmlFor="app-version-active-share"
        hint="A version counts as released once it carries this share of traffic. Default 0.05 (5%)."
        error={activeShareMinError}
        last
      >
        <Input
          id="app-version-active-share"
          type="number"
          min={0.01}
          max={0.99}
          step={0.01}
          value={activeShareMin}
          onChange={e => onActiveShareMinChange(e.target.value)}
          disabled={!appVersionColumn}
          placeholder={appVersionColumn ? 'Default 0.05' : 'Select version column'}
          aria-invalid={activeShareMinError ? true : undefined}
          aria-describedby={activeShareMinError ? fieldErrorId('app-version-active-share') : undefined}
        />
      </Field>
    </>
  )
}
