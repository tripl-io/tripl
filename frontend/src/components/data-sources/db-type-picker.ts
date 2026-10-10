import type { SelectOption } from '@/components/settings/kit'
import { DB_TYPE_OPTIONS, dbTypeLabel, type DbType } from '@/types'

/**
 * The type picker's options. A preview connector says so in its option, the one
 * place a native select can carry it; the label itself stays the bare name,
 * which the name placeholder and the card build on.
 */
export const DB_TYPE_PICKER_OPTIONS: readonly SelectOption[] = DB_TYPE_OPTIONS.map(option => ({
  value: option.value,
  label: option.preview ? `${option.label} (preview)` : option.label,
}))

/** What "preview" means for a warehouse connector, said the same way everywhere. */
export function dbTypePreviewText(dbType: DbType): string {
  return `${dbTypeLabel(dbType)} support is in preview. It has not yet been verified against a live warehouse.`
}
