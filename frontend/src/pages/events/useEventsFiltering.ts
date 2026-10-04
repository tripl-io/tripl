import { useCallback, useMemo } from 'react'

import type {
  EventFieldValue,
  EventListItem,
  EventType,
  EventTypeBrief,
  FieldDefinition,
  MetaFieldDefinition,
} from '@/types'

// Trim integer-valued floats ("5.0", "-3.00") down to their integer form
// for display and filtering.
function normalizeFieldValue(value: string): string {
  if (value && /^-?\d+\.0+$/.test(value)) return String(parseInt(value, 10))
  return value
}

/**
 * The one of this row's field values that answers a field column, or undefined.
 *
 * The ROW is the primitive, not the string: the cell renders the value AND the
 * observed-values popover hanging off the same `EventFieldValue`. Answering
 * "which value is this?" twice — once for the text, once for the contexts — is
 * what let the two answers disagree, and the popover was the one that lost.
 *
 * The name pass exists for the "All" tab, where columns are deduped by name and
 * keep whichever event type came first; a row of any other type carries a
 * different FieldDefinition id for the same field, so an id-only lookup finds
 * nothing on it.
 *
 * Ties, when two event types share a field name: both passes scan
 * `ev.field_values`, this row's OWN values, so whichever wins, its contexts were
 * recorded against THIS event — no tie-break can borrow another event's. Within
 * a row a tie is near-impossible to begin with: `uq_field_def_event_type_name`
 * makes a name unique inside an event type and `uq_event_field_value_event_field`
 * stops one definition appearing twice, so two same-named candidates require a
 * row still holding a value left behind by an event type it no longer belongs
 * to. That case resolves to the first match in `ev.field_values` order — the
 * array exactly as the API returned it, walked once per response, so every cell
 * of the row and every re-render pick the same value and the row cannot
 * contradict itself. The id pass runs to completion first, so an exact id match
 * always outranks any name match.
 */
export function resolveFieldValueRow(
  ev: EventListItem,
  col: FieldDefinition,
  fieldDefsById: Map<string, FieldDefinition>,
): EventFieldValue | undefined {
  for (const fv of ev.field_values) {
    if (fv.field_definition_id === col.id) return fv
  }
  for (const fv of ev.field_values) {
    const def = fieldDefsById.get(fv.field_definition_id)
    if (def && def.name === col.name) return fv
  }
  return undefined
}

/**
 * A row's value for a field column, resolved straight off the row.
 *
 * The table renders through a memoized per-row index (below); CSV export walks
 * rows that were never rendered and has no such index, so the lookup rules live
 * here in one place — as `resolveFieldValueRow`, of which this is the string
 * projection.
 */
export function resolveFieldValue(
  ev: EventListItem,
  col: FieldDefinition,
  fieldDefsById: Map<string, FieldDefinition>,
): string {
  const fv = resolveFieldValueRow(ev, col, fieldDefsById)
  return fv ? normalizeFieldValue(fv.value) : ''
}

/** Every value a row holds for a meta column, in the order the API returned. */
export function resolveMetaValues(ev: EventListItem, mf: MetaFieldDefinition): string[] {
  const out: string[] = []
  for (const mv of ev.meta_values) {
    if (mv.meta_field_definition_id === mf.id) out.push(mv.value)
  }
  return out
}

/** A row's value for a meta column as one string — several joined.
 *
 * For reading and searching: a CSV cell, a filter match, a truncated column.
 * Anything that turns a value into a LINK needs `resolveMetaValues` instead,
 * since the template wraps one value and a joined string would render a single
 * broken address. */
export function resolveMetaValue(ev: EventListItem, mf: MetaFieldDefinition): string {
  return resolveMetaValues(ev, mf).join(', ')
}

/** One event's field values, addressable by FieldDefinition id and by name. */
type EventFieldValueIndex = {
  byId: Map<string, EventFieldValue>
  byName: Map<string, EventFieldValue>
}

export type ColumnFilterContext = {
  fieldColumns: FieldDefinition[]
  metaFields: MetaFieldDefinition[]
  fieldFilters: Record<string, string>
  metaFilters: Record<string, string>
  getFieldValue: (ev: EventListItem, col: FieldDefinition) => string
  getMetaValue: (ev: EventListItem, mf: MetaFieldDefinition) => string
}

/**
 * Narrow rows by the per-column (field/meta) filters.
 *
 * Takes value accessors so the table can keep its memoized map lookups on the
 * hot render path while CSV export resolves values directly off freshly fetched
 * rows — one rule set, two callers. Returns the input array UNCHANGED when no
 * column filter is set: callers compare identity to tell whether the server
 * `total` still describes the list (useEventsTableVirtualization).
 */
export function filterEventsByColumns(
  rows: EventListItem[],
  ctx: ColumnFilterContext,
): EventListItem[] {
  const active =
    Object.values(ctx.fieldFilters).some(v => v !== '') ||
    Object.values(ctx.metaFilters).some(v => v !== '')
  if (!active) return rows
  return rows.filter(ev => eventMatchesColumnFilters(ev, ctx))
}

function eventMatchesColumnFilters(ev: EventListItem, ctx: ColumnFilterContext): boolean {
  for (const col of ctx.fieldColumns) {
    const wanted = ctx.fieldFilters[col.name]
    if (!wanted) continue
    const val = ctx.getFieldValue(ev, col)
    const exact = col.field_type === 'enum' || col.field_type === 'boolean'
    if (exact ? val !== wanted : !val.toLowerCase().includes(wanted.toLowerCase())) return false
  }
  for (const mf of ctx.metaFields) {
    const wanted = ctx.metaFilters[mf.name]
    if (!wanted) continue
    const val = ctx.getMetaValue(ev, mf)
    const exact = mf.field_type === 'enum' || mf.field_type === 'boolean'
    if (exact ? val !== wanted : !val.toLowerCase().includes(wanted.toLowerCase())) return false
  }
  return true
}

/**
 * Derived data the events table renders on top of `rawEvents`: per-event
 * field/meta value lookups, the column set, enum options, and the final
 * client-filtered list. Pulled out of EventsPage so the page file stops
 * doing O(N · F²) Object.fromEntries() work inline.
 */
export function useEventsFiltering({
  rawEvents,
  eventTypes,
  metaFields,
  activeEt,
  debouncedFieldFilters,
  debouncedMetaFilters,
}: {
  rawEvents: EventListItem[]
  eventTypes: EventType[]
  metaFields: MetaFieldDefinition[]
  activeEt: EventType | null
  debouncedFieldFilters: Record<string, string>
  debouncedMetaFilters: Record<string, string>
}) {
  const fieldColumns: FieldDefinition[] = useMemo(() => {
    if (activeEt) return [...activeEt.field_definitions].sort((a, b) => a.order - b.order)
    const seen = new Map<string, FieldDefinition>()
    for (const et of eventTypes) {
      for (const fd of [...et.field_definitions].sort((a, b) => a.order - b.order)) {
        if (!seen.has(fd.name)) seen.set(fd.name, fd)
      }
    }
    return Array.from(seen.values())
  }, [activeEt, eventTypes])

  const allFieldDefs = useMemo(() => {
    const map = new Map<string, FieldDefinition>()
    for (const et of eventTypes) {
      for (const fd of et.field_definitions) {
        map.set(fd.id, fd)
      }
    }
    return map
  }, [eventTypes])

  // Slim list responses ship event_type_id only; EventRow looks up the brief
  // here from the cached EventTypes (already loaded for filter tabs).
  const eventTypesById = useMemo(() => {
    const map = new Map<string, EventTypeBrief>()
    for (const et of eventTypes) {
      map.set(et.id, {
        id: et.id,
        name: et.name,
        display_name: et.display_name,
        color: et.color,
      })
    }
    return map
  }, [eventTypes])

  // Collect enum/boolean options per field column for filter dropdowns
  const fieldEnumOptions = useMemo(() => {
    const map: Record<string, Set<string>> = {}
    for (const col of fieldColumns) {
      if (col.field_type === 'enum' && col.enum_options) {
        map[col.id] = new Set(col.enum_options)
      } else if (col.field_type === 'boolean') {
        map[col.id] = new Set(['true', 'false'])
      }
    }
    return map
  }, [fieldColumns])

  // One index per events list, instead of re-building Object.fromEntries(...)
  // inside every filter check and every <TableCell> render (was O(N · F²) per
  // render on the 2000-event path).
  //
  // It carries the name key as well as the id, so `resolveFieldValueRow`'s "All"
  // tab fallback stays off the hot render path: this table virtualizes over
  // thousands of rows, and on that tab the fallback is the COMMON case, not the
  // rare one — an O(fields) rescan per cell would be a per-frame cost.
  //
  // Ids and names get their own map rather than sharing one, so a field whose
  // name happens to look like a FieldDefinition id cannot answer for it.
  const fieldValueIndexByEvent = useMemo(() => {
    const map = new Map<string, EventFieldValueIndex>()
    for (const ev of rawEvents) {
      const byId = new Map<string, EventFieldValue>()
      const byName = new Map<string, EventFieldValue>()
      for (const fv of ev.field_values) {
        byId.set(fv.field_definition_id, fv)
        const def = allFieldDefs.get(fv.field_definition_id)
        // First write wins, matching the scan's first-match tie-break.
        if (def && !byName.has(def.name)) byName.set(def.name, fv)
      }
      map.set(ev.id, { byId, byName })
    }
    return map
  }, [allFieldDefs, rawEvents])

  // A list per field, not a value: a field with `allow_multiple` has several
  // rows, and `set` would have kept only the last one.
  const metaValuesByEvent = useMemo(() => {
    const map = new Map<string, Map<string, string[]>>()
    for (const ev of rawEvents) {
      const mvMap = new Map<string, string[]>()
      for (const mv of ev.meta_values) {
        const existing = mvMap.get(mv.meta_field_definition_id)
        if (existing) existing.push(mv.value)
        else mvMap.set(mv.meta_field_definition_id, [mv.value])
      }
      map.set(ev.id, mvMap)
    }
    return map
  }, [rawEvents])

  const getFieldValueRow = useCallback(
    (ev: EventListItem, col: FieldDefinition): EventFieldValue | undefined => {
      const index = fieldValueIndexByEvent.get(ev.id)
      // A row the memo never saw still resolves, just without the index.
      if (!index) return resolveFieldValueRow(ev, col, allFieldDefs)
      return index.byId.get(col.id) ?? index.byName.get(col.name)
    },
    [allFieldDefs, fieldValueIndexByEvent],
  )

  const getFieldValue = useCallback(
    (ev: EventListItem, col: FieldDefinition) => {
      const fv = getFieldValueRow(ev, col)
      return fv ? normalizeFieldValue(fv.value) : ''
    },
    [getFieldValueRow],
  )

  const getMetaValue = useCallback(
    (ev: EventListItem, mf: MetaFieldDefinition) =>
      (metaValuesByEvent.get(ev.id)?.get(mf.id) ?? []).join(', '),
    [metaValuesByEvent],
  )

  // Client-side filtering by field values and meta values
  const events = useMemo(() => {
    return filterEventsByColumns(rawEvents, {
      fieldColumns,
      metaFields,
      fieldFilters: debouncedFieldFilters,
      metaFilters: debouncedMetaFilters,
      getFieldValue,
      getMetaValue,
    })
  }, [
    rawEvents,
    debouncedFieldFilters,
    debouncedMetaFilters,
    fieldColumns,
    metaFields,
    getFieldValue,
    getMetaValue,
  ])

  return {
    fieldColumns,
    allFieldDefs,
    eventTypesById,
    fieldEnumOptions,
    metaValuesByEvent,
    getFieldValueRow,
    getFieldValue,
    getMetaValue,
    events,
  }
}
