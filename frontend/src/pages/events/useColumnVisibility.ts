import { useCallback, useState } from 'react'

import type { EventType, FieldDefinition } from '@/types'

const STORAGE_KEY = 'tripl.eventsHiddenCols'

/**
 * The Health column (F15, #268): a toggleable column like Signal or Δ, shown
 * by default. It exists on the main plan only; on a branch the table leaves it
 * out whatever this preference says, and the Columns menu hides its toggle.
 */
export const HEALTH_COL_KEY = 'health'

/**
 * Columns hidden by default for a first-time user (no persisted preference).
 *
 * a fresh user should meet a lean, scannable table that leads with the
 * monitoring signal — Event, Type, Status, Monitor, Δ, Last seen, 48h (Event,
 * Type, 48h and Actions are pinned and always shown) — instead of an
 * intimidating column spreadsheet. Only the least-essential workflow-metadata
 * columns start hidden: Owner, Reviewed and Tags. They stay one click away in
 * the Columns editor. No column is removed; only the default visibility
 * changes, and only for users who have not customized their columns.
 *
 * Per-event-type field columns (`f:<id>`) and meta columns (`m:<id>`) are the
 * other half of the long tail. Their ids are dynamic and unknown at this layer,
 * so defaulting them hidden has to happen where the field definitions are known
 * (useEventsViewState) — out of scope for this hook.
 */
const DEFAULT_HIDDEN_COLUMNS: readonly string[] = [
  'owner',
  'reviewed',
  'tags',
]

/**
 * Persists which event-table columns the user has hidden via localStorage.
 *
 * A first-time user (no persisted choice) gets the lean default set above; a
 * returning user's saved choice is respected exactly — we never override a
 * persisted preference. The host page wires the resulting Set + toggler into
 * EventsToolbar and into per-column render guards.
 */
export function useColumnVisibility() {
  const [hiddenColumns, setHiddenColumns] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      // Respect a persisted choice exactly; only fresh users get the lean default.
      if (raw !== null) return new Set<string>(JSON.parse(raw) as string[])
      return new Set<string>(DEFAULT_HIDDEN_COLUMNS)
    } catch {
      return new Set<string>(DEFAULT_HIDDEN_COLUMNS)
    }
  })

  const [colMenuOpen, setColMenuOpen] = useState(false)

  const updateColumns = useCallback((add: string[], remove: string[]) => {
    setHiddenColumns((prev) => {
      const next = new Set(prev)
      for (const key of remove) next.delete(key)
      for (const key of add) next.add(key)
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify([...next])) } catch { /* ignore */ }
      return next
    })
  }, [])

  const toggleColumn = useCallback((key: string) => {
    setHiddenColumns((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key); else next.add(key)
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify([...next])) } catch { /* ignore */ }
      return next
    })
  }, [])

  return {
    hiddenColumns,
    toggleColumn,
    updateColumns,
    colMenuOpen,
    setColMenuOpen,
  }
}

/**
 * The stored opt-in for a column that starts hidden on its tab: a type-specific
 * field column on the All / queue tabs. Kept in the same persisted set
 * as the hidden keys, so one localStorage entry still holds every choice.
 */
export function shownColumnKey(key: string): string {
  return `show:${key}`
}

/**
 * The `f:<id>` keys of the field columns that not every event type defines.
 * On the All tab the table lists the union of every type's fields, so each of
 * these is a column of dashes for every other type: about 70% of those cells
 * were "—", and they pushed Last seen and Owner off-screen at 1024.
 */
export function typeSpecificFieldKeys(
  eventTypes: readonly EventType[],
  fieldColumns: readonly FieldDefinition[],
): Set<string> {
  const keys = new Set<string>()
  if (eventTypes.length < 2) return keys
  for (const column of fieldColumns) {
    const everyType = eventTypes.every(type =>
      type.field_definitions.some(field => field.name === column.name),
    )
    if (!everyType) keys.add(`f:${column.id}`)
  }
  return keys
}

/**
 * What the table hides: the stored choices, plus the default-hidden keys the
 * reader has not opted into with {@link shownColumnKey}.
 */
export function withDefaultHidden(
  hiddenColumns: ReadonlySet<string>,
  defaultHidden: ReadonlySet<string>,
): Set<string> {
  const effective = new Set(hiddenColumns)
  for (const key of defaultHidden) {
    if (!hiddenColumns.has(shownColumnKey(key))) effective.add(key)
  }
  return effective
}
