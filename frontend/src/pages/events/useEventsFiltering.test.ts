// @vitest-environment jsdom
import { renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { EventFieldValue, EventListItem, EventType, FieldDefinition } from '@/types'

import { resolveFieldValue, resolveFieldValueRow, useEventsFiltering } from './useEventsFiltering'
import { at } from '@/test/at'

// Two event types define a field called `page`. FieldDefinition is unique per
// (event_type_id, name), so these are distinct rows with one shared name — the
// shape the "All" tab's name-deduped columns hand every off-type row.
const PAGE_FIELD_PV: FieldDefinition = {
  id: 'field-pv-page',
  event_type_id: 'et-1',
  name: 'page',
  display_name: 'Page',
  field_type: 'string',
  is_required: false,
  enum_options: null,
  description: '',
  order: 0,
  sensitivity: 'none',
}

const PAGE_FIELD_SE: FieldDefinition = { ...PAGE_FIELD_PV, id: 'field-se-page', event_type_id: 'et-2' }

const COUNT_FIELD: FieldDefinition = { ...PAGE_FIELD_PV, id: 'field-count', name: 'count', display_name: 'Count' }

const ALL_FIELD_DEFS = new Map([
  [PAGE_FIELD_PV.id, PAGE_FIELD_PV],
  [PAGE_FIELD_SE.id, PAGE_FIELD_SE],
  [COUNT_FIELD.id, COUNT_FIELD],
])

function makeEventType(id: string, name: string, fieldDefinitions: FieldDefinition[]): EventType {
  return {
    id,
    project_id: 'proj-1',
    name,
    display_name: name,
    description: '',
    color: '#3355ff',
    order: 0,
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-01T00:00:00Z',
    field_definitions: fieldDefinitions,
  }
}

function makeEvent(overrides: Partial<EventListItem> = {}): EventListItem {
  return {
    id: 'evt-1',
    project_id: 'proj-1',
    event_type_id: 'et-2',
    name: 'checkout_completed',
    source_name: null,
    title: '',
    description: '',
    order: 0,
    status: 'live',
    sunset_at: null,
    last_seen_at: null,
    owner_id: null,
    reviewed: false,
    metric_breakdown_columns: [],
    drift_count: 0,
    monitored: false,
    tags: [],
    field_values: [],
    meta_values: [],
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-01T00:00:00Z',
    ...overrides,
  }
}

describe('resolveFieldValueRow', () => {
  it('returns the value row matching the column id', () => {
    const row = { id: 'fv-1', field_definition_id: PAGE_FIELD_PV.id, value: '/checkout' }

    expect(resolveFieldValueRow(makeEvent({ field_values: [row] }), PAGE_FIELD_PV, ALL_FIELD_DEFS)).toBe(row)
  })

  // The defect's own case: the row answers a column belonging to another event
  // type, so there is no id to match on — only the shared field name.
  it('falls back to the definition name when no id matches', () => {
    const row = { id: 'fv-1', field_definition_id: PAGE_FIELD_SE.id, value: '/checkout' }

    expect(resolveFieldValueRow(makeEvent({ field_values: [row] }), PAGE_FIELD_PV, ALL_FIELD_DEFS)).toBe(row)
  })

  // An exact id match outranks any name match, whatever order the row arrived
  // in: the id pass runs over every value before the name pass starts.
  it('prefers an id match over a name match later in the row', () => {
    const byName = { id: 'fv-name', field_definition_id: PAGE_FIELD_SE.id, value: '/cart' }
    const byId = { id: 'fv-id', field_definition_id: PAGE_FIELD_PV.id, value: '/checkout' }

    expect(
      resolveFieldValueRow(makeEvent({ field_values: [byName, byId] }), PAGE_FIELD_PV, ALL_FIELD_DEFS),
    ).toBe(byId)
  })

  it('returns undefined when the row holds no value for the column, by id or name', () => {
    const row = { id: 'fv-1', field_definition_id: COUNT_FIELD.id, value: '7' }

    expect(
      resolveFieldValueRow(makeEvent({ field_values: [row] }), PAGE_FIELD_PV, ALL_FIELD_DEFS),
    ).toBeUndefined()
  })

  // A value whose definition is not in the map (a stale reference the event
  // types no longer carry) cannot be name-matched, so it must not be returned.
  it('ignores a value whose field definition is unknown', () => {
    const row = { id: 'fv-1', field_definition_id: 'field-vanished', value: '/checkout' }

    expect(
      resolveFieldValueRow(makeEvent({ field_values: [row] }), PAGE_FIELD_PV, ALL_FIELD_DEFS),
    ).toBeUndefined()
  })
})

describe('resolveFieldValue', () => {
  it('projects the resolved row to its value, through the same name fallback', () => {
    const ev = makeEvent({
      field_values: [{ id: 'fv-1', field_definition_id: PAGE_FIELD_SE.id, value: '/checkout' }],
    })

    expect(resolveFieldValue(ev, PAGE_FIELD_PV, ALL_FIELD_DEFS)).toBe('/checkout')
  })

  it('trims an integer-valued float down to its integer form', () => {
    const ev = makeEvent({ field_values: [{ id: 'fv-1', field_definition_id: COUNT_FIELD.id, value: '5.00' }] })

    expect(resolveFieldValue(ev, COUNT_FIELD, ALL_FIELD_DEFS)).toBe('5')
  })

  it('answers with the empty string when nothing matches', () => {
    expect(resolveFieldValue(makeEvent(), PAGE_FIELD_PV, ALL_FIELD_DEFS)).toBe('')
  })
})

// The table renders through the hook's memoized per-event index; the exported
// scan above is the CSV path. Covering only the scan leaves the index free to
// drop its name key — which is an earlier bug back on the "All" tab, with every
// test above still green.
describe('useEventsFiltering field value lookups', () => {
  const PV_TYPE = makeEventType('et-1', 'pv', [PAGE_FIELD_PV])
  const SEARCH_TYPE = makeEventType('et-2', 'search', [PAGE_FIELD_SE])

  // The popover reads its contexts off this row and the cell's text reads off
  // the same one; answering "which value is this?" twice is what split them.
  const OFF_TYPE_PAGE: EventFieldValue = {
    id: 'fv-page',
    field_definition_id: PAGE_FIELD_SE.id,
    value: '/checkout',
    variable_values: [
      {
        id: 'vv-page',
        variable_id: 'var-page',
        variable_name: 'page',
        source_column: 'payload.page',
        value_kind: 'low',
        observed_count: 4820,
        values: ['/checkout', '/cart'],
      },
    ],
  }

  function renderAllTab(
    rawEvents: EventListItem[],
    eventTypes: EventType[] = [PV_TYPE, SEARCH_TYPE],
  ) {
    return renderHook(() =>
      useEventsFiltering({
        rawEvents,
        eventTypes,
        metaFields: [],
        // A null active type IS the "All" tab — the one that dedupes columns by
        // name and hands most rows a column belonging to some other event type.
        activeEt: null,
        debouncedFieldFilters: {},
        debouncedMetaFilters: {},
      }),
    )
  }

  it("answers a name-deduped column with the off-type row's own value", () => {
    const ev = makeEvent({ field_values: [OFF_TYPE_PAGE] })

    const { result } = renderAllTab([ev])

    // Page-view sorts first, so its `page` definition is the column EVERY row
    // renders under — including this search-event row, which carries a different
    // FieldDefinition id for the same field.
    const pageColumn = at(result.current.fieldColumns, 0)
    expect(pageColumn).toBe(PAGE_FIELD_PV)
    expect(result.current.getFieldValueRow(ev, pageColumn)).toBe(OFF_TYPE_PAGE)
    expect(result.current.getFieldValue(ev, pageColumn)).toBe('/checkout')
  })

  // A row the memo never saw — one handed over between a fetch and the next
  // render — has no index entry and must still resolve rather than come back blank.
  it('resolves a row the index was never built over', () => {
    const stray = makeEvent({ id: 'evt-stray', field_values: [OFF_TYPE_PAGE] })

    const { result } = renderAllTab([])

    expect(result.current.getFieldValueRow(stray, PAGE_FIELD_PV)).toBe(OFF_TYPE_PAGE)
    expect(result.current.getFieldValue(stray, PAGE_FIELD_PV)).toBe('/checkout')
  })

  // Ids and names index into separate maps on purpose: one shared map would let
  // a field whose NAME reads like a FieldDefinition id answer for that column.
  it('does not let a field name stand in for a column id', () => {
    const impostor: FieldDefinition = {
      ...PAGE_FIELD_SE,
      id: 'field-se-impostor',
      name: PAGE_FIELD_PV.id,
    }
    const ev = makeEvent({
      field_values: [{ id: 'fv-impostor', field_definition_id: impostor.id, value: 'not-a-page' }],
    })

    const { result } = renderAllTab([ev], [PV_TYPE, makeEventType('et-2', 'search', [impostor])])

    expect(result.current.getFieldValueRow(ev, PAGE_FIELD_PV)).toBeUndefined()
    expect(result.current.getFieldValue(ev, PAGE_FIELD_PV)).toBe('')
  })
})
